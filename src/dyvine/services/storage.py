"""Storage service for managing Cloudflare R2 object storage operations.

This module provides a service class (R2StorageService) that encapsulates all
R2 storage operations including:
- Path generation for user content and livestreams
- Metadata management
- Error handling with retries
- Monitoring metrics
- Lifecycle management

"""

import base64
import mimetypes
import time
import uuid
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from prometheus_client import Counter, Histogram

from ..core.logging import ContextLogger
from ..core.settings import settings

# Initialize logger
logger = ContextLogger(__name__)

# Prometheus metrics
r2_upload_requests = Counter(
    "r2_upload_requests_total",
    "Total number of R2 upload requests",
    ["type", "status"]
)

r2_upload_bytes = Counter(
    "r2_upload_bytes_sum",
    "Total bytes uploaded to R2",
    ["category"]
)

r2_upload_duration = Histogram(
    "r2_upload_duration_seconds",
    "Upload duration in seconds",
    buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0]
)

r2_upload_failures = Counter(
    "r2_upload_failures_total",
    "Total number of R2 upload failures",
    ["error_type"]
)

class StorageError(Exception):
    """Base exception for R2 storage operations."""
    pass

class ContentType(str, Enum):
    """Enum for content type categories."""
    POSTS = "posts"
    LIVESTREAM = "livestream"
    STORY = "story"

class R2StorageService:
    """Service for managing Cloudflare R2 object storage operations.

    This class implements the storage path specifications, metadata management,
    retry logic, and monitoring for R2 storage operations.

    Attributes:
        client: Boto3 S3 client configured for R2
        bucket: Name of the R2 bucket
    """

    def __init__(self) -> None:
        """Initialize the R2 storage service.

        Configures the boto3 client with R2-specific settings and retry config.
        """
        # Check if R2 configuration is available
        if not all([
            settings.r2_endpoint,
            settings.r2_account_id,
            settings.r2_access_key_id,
            settings.r2_secret_access_key,
            settings.r2_bucket_name
        ]):
            logger.warning(
                "R2 configuration incomplete, storage service will be disabled"
            )
            self.client = None
            self.bucket = None
            return

        # Configure retry settings
        config = Config(
            retries={
                "max_attempts": 3,
                "mode": "adaptive"
            }
        )

        # Format R2 endpoint URL
        endpoint_url = settings.r2_endpoint.format(account_id=settings.r2_account_id)

        # Initialize S3 client for R2
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=settings.r2_access_key_id,
            aws_secret_access_key=settings.r2_secret_access_key,
            config=config
        )
        self.bucket = settings.r2_bucket_name

        logger.info(
            "R2StorageService initialized",
            extra={"bucket": self.bucket, "endpoint": settings.r2_endpoint}
        )

    def generate_ugc_path(
        self,
        user_id: str,
        original_filename: str,
        content_type: str
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
        safe_filename = base64.urlsafe_b64encode(
            original_filename.encode()
        ).decode().rstrip("=")

        # Generate 8-char UUID
        uuid_str = str(uuid.uuid4())[:8]

        # Get file extension
        ext = Path(original_filename).suffix.lower().lstrip(".")
        if not ext:
            ext = mimetypes.guess_extension(content_type, strict=False)
            if ext:
                ext = ext.lstrip(".")
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
                "path": path
            }
        )

        return path

    def generate_livestream_path(
        self,
        user_id: str,
        stream_id: str,
        timestamp: int
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
                "path": path
            }
        )

        return path

    def generate_metadata(
        self,
        author: str,
        category: ContentType,
        content_type: str,
        source: str,
        language: str = "zh-CN",
        version: str = "1.0.0"
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
        safe_author = base64.b64encode(
            author.encode()
        ).decode()

        # Get current UTC time
        now = datetime.now(UTC)

        metadata = {
            "author": safe_author,
            "category": category.value,
            "content-type": content_type,
            "created-date": now.isoformat(),
            "file-format": (
                mimetypes.guess_extension(content_type, strict=False) or ""
            ).lstrip(".") if content_type else "bin",
            "language": language,
            "source": source,
            "uploaded-date": now.isoformat(),
            "version": version
        }

        logger.debug(
            "Generated metadata",
            extra={"metadata": metadata}
        )

        return metadata

    async def upload_file(
        self,
        file_path: str | Path,
        storage_path: str,
        metadata: dict[str, str],
        content_type: str | None = None
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
        if self.client is None:
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

        # Start upload metrics
        start_time = time.time()
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
                    "file_size": file_size
                }
            )

            with open(file_path, "rb") as f:
                self.client.put_object(
                    Bucket=self.bucket,
                    Key=storage_path,
                    Body=f,
                    ContentType=content_type,
                    Metadata=metadata
                )

            duration = time.time() - start_time

            logger.info(
                "Successfully uploaded file to R2",
                extra={
                    "storage_path": storage_path,
                    "bucket": self.bucket,
                    "size_bytes": file_size,
                    "duration_seconds": duration
                }
            )

            # Update metrics
            r2_upload_requests.labels(
                type=metadata["category"],
                status="success"
            ).inc()
            r2_upload_bytes.labels(
                category=metadata["category"]
            ).inc(file_size)
            r2_upload_duration.observe(duration)

            # Generate presigned URL for temporary access (default 1 hour)
            url = self.client.generate_presigned_url(
                'get_object',
                Params={
                    'Bucket': self.bucket,
                    'Key': storage_path
                },
                ExpiresIn=3600  # 1 hour
            )

            logger.info(
                "File uploaded successfully",
                extra={
                    "storage_path": storage_path,
                    "size_bytes": file_size,
                    "duration_seconds": duration,
                    "presigned_url": url
                }
            )

            return {
                "storage_path": storage_path,
                "presigned_url": url,
                "size_bytes": file_size,
                "content_type": content_type,
                "metadata": metadata
            }

        except (BotoCoreError, ClientError) as e:
            r2_upload_requests.labels(
                type=metadata["category"],
                status="error"
            ).inc()
            r2_upload_failures.labels(
                error_type=type(e).__name__
            ).inc()

            logger.exception(
                "Upload failed",
                extra={
                    "storage_path": storage_path,
                    "error": str(e)
                }
            )
            raise StorageError(f"Upload failed: {str(e)}") from e

    async def get_object_metadata(self, storage_path: str) -> dict[str, str]:
        """Get metadata for an object in R2 storage.

        Args:
            storage_path: Path to the object in R2

        Returns:
            Dict[str, str]: Object metadata

        Raises:
            StorageError: If object not found or other error occurs
        """
        if self.client is None:
            raise StorageError(
                "R2 storage service is disabled due to missing configuration"
            )

        try:
            response = self.client.head_object(
                Bucket=self.bucket,
                Key=storage_path
            )
            return response.get("Metadata", {})

        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            if error_code == "404":
                raise StorageError(f"Object not found: {storage_path}") from None
            raise StorageError(f"Error getting metadata: {str(e)}") from e

    async def delete_object(self, storage_path: str) -> None:
        """Delete an object from R2 storage.

        Args:
            storage_path: Path to the object in R2

        Raises:
            StorageError: If deletion fails
        """
        if self.client is None:
            raise StorageError(
                "R2 storage service is disabled due to missing configuration"
            )

        try:
            self.client.delete_object(
                Bucket=self.bucket,
                Key=storage_path
            )
            logger.info(
                "Object deleted",
                extra={"storage_path": storage_path}
            )

        except ClientError as e:
            logger.exception(
                "Deletion failed",
                extra={
                    "storage_path": storage_path,
                    "error": str(e)
                }
            )
            raise StorageError(f"Deletion failed: {str(e)}") from e

    async def list_objects(
        self,
        prefix: str,
        max_keys: int = 1000
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
        if self.client is None:
            raise StorageError(
                "R2 storage service is disabled due to missing configuration"
            )

        try:
            response = self.client.list_objects_v2(
                Bucket=self.bucket,
                Prefix=prefix,
                MaxKeys=max_keys
            )

            objects = []
            for obj in response.get("Contents", []):
                # Get metadata for each object
                try:
                    head = self.client.head_object(
                        Bucket=self.bucket,
                        Key=obj["Key"]
                    )
                    obj["Metadata"] = head.get("Metadata", {})
                except ClientError:
                    obj["Metadata"] = {}

                objects.append(obj)

            return objects

        except ClientError as e:
            logger.exception(
                "List objects failed",
                extra={
                    "prefix": prefix,
                    "error": str(e)
                }
            )
            raise StorageError(f"List objects failed: {str(e)}") from e
