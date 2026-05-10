"""Cloudflare R2 storage facade.

`R2StorageService` wraps the boto3 S3 client used to push downloaded
content into Cloudflare R2. The service:

- Auto-disables when any required setting is missing — every public
  method then short-circuits with a ``StorageError`` so callers can
  treat the disabled state as a recoverable condition.
- Dispatches every blocking boto3 call onto a dedicated executor
  attached by ``ServiceContainer.set_executor`` / ``set_head_executor``
  so the event loop never blocks on a slow R2 round trip.
- Emits Prometheus counters (``r2_upload_requests_total``,
  ``r2_upload_bytes_sum``, ``r2_upload_failures_total``) plus an upload
  duration histogram so production runs are observable through
  ``/metrics``.
- Generates standardised paths for user content
  (``{images,videos}/{user_id}/...``) and livestream recordings
  (``livestreams/{user_id}/{stream_id}/recording_{ts}.mp4``).

Storage-class transitions and retention rules live in
``services/lifecycle.py`` (``LifecycleManager``); that helper is
exercised by tests but is not currently wired into the runtime
container. The dedicated ``audit_executor`` provisioned by
``ServiceContainer`` is reserved for it.
"""

import asyncio
import base64
import functools
import mimetypes
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Executor, ThreadPoolExecutor
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeVar

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from prometheus_client import Counter, Histogram

from ..core.logging import ContextLogger
from ..core.settings import settings

_R = TypeVar("_R")

# Initialize logger
logger = ContextLogger(__name__)

# Prometheus metrics
r2_upload_requests = Counter(
    "r2_upload_requests_total", "Total number of R2 upload requests", ["type", "status"]
)

r2_upload_bytes = Counter(
    "r2_upload_bytes_sum", "Total bytes uploaded to R2", ["category"]
)

r2_upload_duration = Histogram(
    "r2_upload_duration_seconds",
    "Upload duration in seconds",
    buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0],
)

r2_upload_failures = Counter(
    "r2_upload_failures_total", "Total number of R2 upload failures", ["error_type"]
)

# Upper bound on concurrent ``head_object`` calls issued while hydrating
# object metadata inside ``_list_objects_sync``. Capped to keep a single
# listing from exhausting the boto3 connection pool (default 10) by too
# much; the pool is still resized on demand per request.
LIST_OBJECTS_HEAD_MAX_WORKERS = 16


class StorageError(Exception):
    """Base exception for R2 storage operations."""

    pass


class ContentType(StrEnum):
    """Enum for content type categories."""

    POSTS = "posts"
    LIVESTREAM = "livestream"
    STORY = "story"


class R2StorageService:
    """Cloudflare R2 facade with bounded thread-pool dispatch.

    Wraps a boto3 S3 client configured for the R2 endpoint. Every
    blocking call (``put_object``, ``head_object``, ``delete_object``,
    paginated ``list_objects_v2``) is dispatched onto a dedicated
    `concurrent.futures.Executor` attached post-construction by the
    `ServiceContainer`. The R2 head-fan-out used inside
    `_list_objects_sync` runs on a separate executor so concurrent
    listings cannot occupy the same threads that serve uploads.

    Attributes:
        client: Boto3 S3 client configured for R2, or ``None`` when
            the R2 settings are incomplete (the service auto-disables
            and every public method raises `StorageError`).
        bucket: Configured R2 bucket name, or ``None`` when disabled.
    """

    # Instance-level flag flipped the first time ``_list_objects_sync``
    # falls back to a one-off ``ThreadPoolExecutor`` because no shared
    # head pool was injected. Latching the warning to first occurrence
    # keeps the production misconfiguration loud without flooding the
    # log every time tests exercise the fallback path.
    _head_pool_warning_emitted: bool

    def __init__(
        self,
        *,
        executor: Executor | None = None,
        head_executor: Executor | None = None,
    ) -> None:
        """Initialize the R2 storage service.

        Configures the boto3 client with R2-specific settings and retry config.

        Args:
            executor: Optional dedicated ``concurrent.futures.Executor`` used
                to run the synchronous boto3 operations off the event loop.
                When ``None`` the service falls back to the default asyncio
                executor, which keeps historical behavior for tests that
                build the service directly.
            head_executor: Optional dedicated executor used to fan out the
                ``head_object`` calls inside ``_list_objects_sync``. The
                container injects a single shared pool so concurrent
                listings cannot each spawn their own short-lived
                ``ThreadPoolExecutor``. When ``None`` the listing path
                falls back to a one-off pool sized for the page.
        """
        self._executor: Executor | None = executor
        self._head_executor: Executor | None = head_executor
        self._head_pool_warning_emitted = False

        # Check if R2 configuration is available
        if not settings.r2.is_configured:
            logger.warning(
                "R2 configuration incomplete, storage service will be disabled"
            )
            self.client = None
            self.bucket = None
            return

        # Configure retry settings
        config = Config(retries={"max_attempts": 3, "mode": "adaptive"})

        # Format R2 endpoint URL
        endpoint_url = settings.r2_endpoint.format(account_id=settings.r2_account_id)

        # Initialize S3 client for R2
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=settings.r2_access_key_id,
            aws_secret_access_key=settings.r2_secret_access_key,
            config=config,
        )
        self.bucket = settings.r2_bucket_name

        logger.info(
            "R2StorageService initialized",
            extra={"bucket": self.bucket, "endpoint": settings.r2_endpoint},
        )

    def set_executor(self, executor: Executor | None) -> None:
        """Attach a dedicated executor after construction.

        The service is instantiated eagerly inside ``UserService`` today; the
        ``ServiceContainer`` wires in the shared R2 executor post-hoc so all
        R2 calls share one bounded thread pool without changing the
        ``UserService`` constructor signature.
        """
        self._executor = executor

    def set_head_executor(self, executor: Executor | None) -> None:
        """Attach the dedicated ``head_object`` fan-out executor.

        Mirrors :meth:`set_executor` so the container can wire the head
        pool post-construction without changing ``UserService``.
        """
        self._head_executor = executor

    async def _run(self, func: Callable[..., _R], /, *args: Any, **kwargs: Any) -> _R:
        """Dispatch a blocking boto3 call to the configured executor.

        Uses ``loop.run_in_executor`` with ``functools.partial`` so callers can
        pass keyword arguments, and falls back to the default executor when
        none has been attached (e.g. unit tests that exercise the service in
        isolation).
        """
        loop = asyncio.get_running_loop()
        result: _R = await loop.run_in_executor(
            self._executor, functools.partial(func, *args, **kwargs)
        )
        return result

    def generate_ugc_path(
        self, user_id: str, original_filename: str, content_type: str
    ) -> str:
        """Generate a storage path for user-generated content.

        Follows the path specification:
        images/{user_id}/{date_prefix}_{base64_filename}_{uuid8}.{ext}
        videos/{user_id}/{date_prefix}_{base64_filename}_{uuid8}.{ext}

        Args:
            user_id: User's UUID
            original_filename: Original file name
            content_type: MIME type of the content

        Returns:
            str: Generated storage path
        """
        # Get current UTC date for prefix
        date_prefix = datetime.now(UTC).strftime("%Y%m%d")

        # Base64 encode filename (URL safe)
        safe_filename = (
            base64.urlsafe_b64encode(original_filename.encode()).decode().rstrip("=")
        )

        # Generate 8-char UUID
        uuid_str = str(uuid.uuid4())[:8]

        # Get file extension
        ext = Path(original_filename).suffix.lower().lstrip(".")
        if not ext:
            guessed_ext = mimetypes.guess_extension(content_type, strict=False)
            if guessed_ext:
                ext = guessed_ext.lstrip(".")
            else:
                ext = "bin"

        # Determine content directory
        if content_type.startswith("image/"):
            content_dir = "images"
        elif content_type.startswith("video/"):
            content_dir = "videos"
            ext = "mp4"  # Standardize video extension
        else:
            raise StorageError(f"Unsupported content type: {content_type}")

        # Construct path
        path = f"{content_dir}/{user_id}/{date_prefix}_{safe_filename}_{uuid_str}.{ext}"

        logger.debug(
            "Generated UGC path",
            extra={
                "user_id": user_id,
                "original_filename": original_filename,
                "content_type": content_type,
                "path": path,
            },
        )

        return path

    def generate_livestream_path(
        self, user_id: str, stream_id: str, timestamp: int
    ) -> str:
        """Generate a storage path for livestream recordings.

        Follows the path specification:
        livestreams/{user_id}/{stream_id}/recording_{timestamp}.mp4

        Args:
            user_id: User's UUID
            stream_id: Livestream session ID (ULID)
            timestamp: Unix timestamp in milliseconds

        Returns:
            str: Generated storage path
        """
        path = f"livestreams/{user_id}/{stream_id}/recording_{timestamp}.mp4"

        logger.debug(
            "Generated livestream path",
            extra={
                "user_id": user_id,
                "stream_id": stream_id,
                "timestamp": timestamp,
                "path": path,
            },
        )

        return path

    def generate_metadata(
        self,
        author: str,
        category: ContentType,
        content_type: str,
        source: str,
        language: str = "zh-CN",
        version: str = "1.0.0",
    ) -> dict[str, str]:
        """Generate standardized metadata for content.

        Args:
            author: Content author name (will be Base64 encoded)
            category: Content category (posts/livestream/story)
            content_type: MIME type
            source: Source system name
            language: Content language tag (default: zh-CN)
            version: Content version (default: 1.0.0)

        Returns:
            Dict[str, str]: Metadata dictionary
        """
        # Base64 encode author name
        safe_author = base64.b64encode(author.encode()).decode()

        # Get current UTC time
        now = datetime.now(UTC)

        metadata = {
            "author": safe_author,
            "category": category.value,
            "content-type": content_type,
            "created-date": now.isoformat(),
            "file-format": (
                (mimetypes.guess_extension(content_type, strict=False) or "").lstrip(
                    "."
                )
                if content_type
                else "bin"
            ),
            "language": language,
            "source": source,
            "uploaded-date": now.isoformat(),
            "version": version,
        }

        logger.debug("Generated metadata", extra={"metadata": metadata})

        return metadata

    async def upload_file(
        self,
        file_path: str | Path,
        storage_path: str,
        metadata: dict[str, str],
        content_type: str | None = None,
    ) -> dict[str, Any]:
        """Upload a file to R2 storage with retries.

        Args:
            file_path: Path to the local file
            storage_path: Destination path in R2
            metadata: File metadata
            content_type: Optional MIME type (if not provided, will be guessed)

        Returns:
            Dict[str, Any]: Upload result information

        Raises:
            StorageError: If upload fails after retries or storage is disabled
        """
        if self.client is None or self.bucket is None:
            raise StorageError(
                "R2 storage service is disabled due to missing configuration"
            )

        file_path = Path(file_path)
        if not file_path.exists():
            raise StorageError(f"File not found: {file_path}")

        if not content_type:
            content_type = (
                mimetypes.guess_type(file_path)[0] or "application/octet-stream"
            )

        file_size = file_path.stat().st_size

        # Start upload metrics. ``perf_counter`` is monotonic and immune to
        # NTP step adjustments or wall-clock drift, which is exactly what we
        # want when measuring an elapsed duration paired with a single end
        # call below.
        start_time = time.perf_counter()
        try:
            logger.info(
                "Starting R2 upload",
                extra={
                    "file_path": str(file_path),
                    "storage_path": storage_path,
                    "content_type": content_type,
                    "metadata": metadata,
                    "bucket": self.bucket,
                    "endpoint": settings.r2_endpoint,
                    "file_size": file_size,
                },
            )

            url = await self._run(
                self._upload_file_sync,
                file_path=file_path,
                storage_path=storage_path,
                content_type=content_type,
                metadata=metadata,
            )

            duration = time.perf_counter() - start_time

            logger.info(
                "Successfully uploaded file to R2",
                extra={
                    "storage_path": storage_path,
                    "bucket": self.bucket,
                    "size_bytes": file_size,
                    "duration_seconds": duration,
                },
            )

            # Update metrics
            r2_upload_requests.labels(type=metadata["category"], status="success").inc()
            r2_upload_bytes.labels(category=metadata["category"]).inc(file_size)
            r2_upload_duration.observe(duration)

            logger.info(
                "File uploaded successfully",
                extra={
                    "storage_path": storage_path,
                    "size_bytes": file_size,
                    "duration_seconds": duration,
                    "presigned_url": url,
                },
            )

            return {
                "storage_path": storage_path,
                "presigned_url": url,
                "size_bytes": file_size,
                "content_type": content_type,
                "metadata": metadata,
            }

        except (BotoCoreError, ClientError) as e:
            r2_upload_requests.labels(type=metadata["category"], status="error").inc()
            r2_upload_failures.labels(error_type=type(e).__name__).inc()

            logger.exception(
                "Upload failed", extra={"storage_path": storage_path, "error": str(e)}
            )
            raise StorageError(f"Upload failed: {str(e)}") from e

    def _upload_file_sync(
        self,
        *,
        file_path: Path,
        storage_path: str,
        content_type: str,
        metadata: dict[str, str],
    ) -> str:
        """Run the blocking upload + presign pair on a worker thread.

        ``put_object`` needs the file handle to stay open for the duration of
        the request, so the file read and the upload live together. The
        presigned URL is generated in the same thread to keep the entire
        boto3 interaction off the event loop.
        """
        assert self.client is not None and self.bucket is not None
        with open(file_path, "rb") as f:
            self.client.put_object(
                Bucket=self.bucket,
                Key=storage_path,
                Body=f,
                ContentType=content_type,
                Metadata=metadata,
            )
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": storage_path},
            ExpiresIn=3600,  # 1 hour
        )

    async def get_object_metadata(self, storage_path: str) -> dict[str, str]:
        """Get metadata for an object in R2 storage.

        Args:
            storage_path: Path to the object in R2

        Returns:
            Dict[str, str]: Object metadata

        Raises:
            StorageError: If object not found or other error occurs
        """
        if self.client is None or self.bucket is None:
            raise StorageError(
                "R2 storage service is disabled due to missing configuration"
            )

        try:
            response = await self._run(
                self.client.head_object, Bucket=self.bucket, Key=storage_path
            )
            return response.get("Metadata", {})

        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "404":
                raise StorageError(f"Object not found: {storage_path}") from None
            raise StorageError(f"Error getting metadata: {str(e)}") from e

    async def delete_object(self, storage_path: str) -> None:
        """Delete an object from R2 storage.

        Args:
            storage_path: Path to the object in R2

        Raises:
            StorageError: If deletion fails
        """
        if self.client is None or self.bucket is None:
            raise StorageError(
                "R2 storage service is disabled due to missing configuration"
            )

        try:
            await self._run(
                self.client.delete_object, Bucket=self.bucket, Key=storage_path
            )
            logger.info("Object deleted", extra={"storage_path": storage_path})

        except ClientError as e:
            logger.exception(
                "Deletion failed", extra={"storage_path": storage_path, "error": str(e)}
            )
            raise StorageError(f"Deletion failed: {str(e)}") from e

    async def list_objects(
        self, prefix: str, max_keys: int = 1000
    ) -> list[dict[str, Any]]:
        """List objects in R2 storage with the given prefix.

        Args:
            prefix: Path prefix to filter objects
            max_keys: Maximum number of keys to return

        Returns:
            List[Dict[str, Any]]: List of object information including:
                - Key: Object path
                - LastModified: Last modified timestamp
                - Size: Object size in bytes
                - Metadata: Object metadata

        Raises:
            StorageError: If listing fails
        """
        if self.client is None or self.bucket is None:
            raise StorageError(
                "R2 storage service is disabled due to missing configuration"
            )

        try:
            return await self._run(
                self._list_objects_sync, prefix=prefix, max_keys=max_keys
            )
        except ClientError as e:
            logger.exception(
                "List objects failed", extra={"prefix": prefix, "error": str(e)}
            )
            raise StorageError(f"List objects failed: {str(e)}") from e

    def _list_objects_sync(self, *, prefix: str, max_keys: int) -> list[dict[str, Any]]:
        """Run paginated ``list_objects_v2`` plus bounded ``head_object`` fan-out.

        Walks every page returned for ``prefix`` (following
        ``IsTruncated`` / ``NextContinuationToken``) so that buckets with
        more than 1000 objects under a prefix are evaluated in full.
        ``list_objects_v2`` returns objects in lexicographic order; this
        method preserves that ordering across pages. When the container
        has injected a shared head executor the per-key fan-out reuses
        it so several concurrent listings share one bounded thread
        budget; otherwise a one-off pool is built and a warning is
        logged because every call in the missing-pool path keeps the
        calling executor slot occupied for the full ``shutdown(wait=True)``
        window.
        """
        assert self.client is not None and self.bucket is not None
        client = self.client
        bucket = self.bucket

        objects: list[dict[str, Any]] = []
        continuation_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {
                "Bucket": bucket,
                "Prefix": prefix,
                "MaxKeys": max_keys,
            }
            if continuation_token is not None:
                kwargs["ContinuationToken"] = continuation_token
            response = client.list_objects_v2(**kwargs)
            # boto3 stubs type ``Contents`` as ``list[ObjectTypeDef]`` (a
            # TypedDict alias), but downstream code only relies on the
            # ``dict[str, Any]`` shape. Cast through ``list`` so mypy
            # accepts the extend without leaking the boto3-specific type.
            objects.extend(dict(item) for item in response.get("Contents", []))
            if not response.get("IsTruncated"):
                break
            continuation_token = response.get("NextContinuationToken")
            if continuation_token is None:
                # Defensive: a truncated response without a token is a
                # bug in the upstream API but breaking the loop here
                # avoids an infinite request fan-out.
                logger.warning(
                    "list_objects_v2 reported IsTruncated without a continuation token",
                    extra={"prefix": prefix, "fetched": len(objects)},
                )
                break

        if not objects:
            return []

        results: list[dict[str, Any]] = [
            {
                "Key": obj.get("Key"),
                "LastModified": obj.get("LastModified"),
                "ETag": obj.get("ETag"),
                "Size": obj.get("Size"),
                "StorageClass": obj.get("StorageClass"),
            }
            for obj in objects
        ]

        def _fetch_metadata(key: str | None) -> dict[str, str]:
            if not key:
                return {}
            try:
                head = client.head_object(Bucket=bucket, Key=key)
            except ClientError:
                return {}
            return head.get("Metadata", {}) or {}

        keys: list[str | None] = [obj.get("Key") for obj in objects]
        if self._head_executor is not None:
            # Reuse the container-owned pool. ``Executor.map`` preserves
            # input order, so zipping back into ``results`` keeps the
            # lexicographic ordering ``list_objects_v2`` returned.
            metadata_iter = self._head_executor.map(_fetch_metadata, keys)
            for obj_data, metadata in zip(results, metadata_iter, strict=True):
                obj_data["Metadata"] = metadata
            return results

        # ``object.__new__`` paths (test fixtures) skip ``__init__`` so
        # the latch attribute may not exist; ``getattr`` falls back to
        # ``False`` and the explicit setattr below installs the flag for
        # subsequent calls on the same instance.
        if not getattr(self, "_head_pool_warning_emitted", False):
            logger.warning(
                "R2 head executor not attached; falling back to a one-off "
                "pool. Each call holds the calling executor slot for the "
                "full fan-out; wire ``set_head_executor`` from the "
                "service container.",
                extra={"prefix": prefix, "object_count": len(keys)},
            )
            self._head_pool_warning_emitted = True
        workers = min(LIST_OBJECTS_HEAD_MAX_WORKERS, len(keys))
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="r2-head"
        ) as executor:
            metadata_iter = executor.map(_fetch_metadata, keys)
            for obj_data, metadata in zip(results, metadata_iter, strict=True):
                obj_data["Metadata"] = metadata
        return results
