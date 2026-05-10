"""User domain service.

`UserService` encapsulates the public surface used by the user router:

- ``get_user_info(user_id)`` — fetch and validate a Douyin profile,
  returning a typed ``UserResponse``.
- ``start_download(...)`` — persist a ``user_content_download``
  operation, schedule the long-running fetch loop on the shared
  ``BackgroundTaskRegistry``, and return immediately.
- ``get_download_status(task_id)`` — return the current persisted
  state of a previously scheduled download.

The fetch loop (`_process_download`) walks the f2 paginated feed,
downloads each batch into a per-task workspace under
``temp_downloads/<task_id>``, optionally pushes every artefact into
Cloudflare R2 via the shared ``R2StorageService``, and finalises the
operation row with a clamped ``progress`` value plus a terminal
``status``. The workspace is removed in a ``finally`` branch so a
failed run does not leave files on disk.

External dependencies are wrapped in `try`/`finally`:
``DouyinHandler`` is closed via ``_safely_close_handler`` to avoid
file-descriptor leaks across heavy polling, and ``shutil.rmtree``
uses ``onexc`` to log per-entry failures without masking the
original exception.
"""

import asyncio
import json
import re
import shutil
from pathlib import Path
from typing import Any, cast

from f2.apps.douyin.handler import DouyinHandler  # type: ignore
from pydantic import AnyHttpUrl

from ..core.background import BackgroundTaskRegistry, spawn_or_fallback
from ..core.exceptions import (
    DownloadError,
    OperationNotFoundError,
    ServiceError,
    UserNotFoundError,
)
from ..core.logging import ContextLogger
from ..core.operations import OperationStore
from ..core.pagination import MAX_PAGES_FALLBACK, PAGE_MULTIPLIER, PAGE_SLACK
from ..core.settings import settings
from ..schemas.users import DownloadResponse, UserResponse
from .storage import ContentType, R2StorageService

# Page size requested from the f2 fetcher. The outer loop guard uses the
# shared :mod:`dyvine.core.pagination` constants together with this value
# to bound the number of pages we will walk before giving up.
PAGE_SIZE = 100

# Cool-down between successive page fetches inside ``_process_download``.
# Douyin throttles aggressive callers, so the loop sleeps before pulling
# the next cursor to keep the request cadence below their anti-scrape
# threshold. Centralised here so the value can be tuned without touching
# the loop body.
PAGE_FETCH_DELAY_SECONDS = 5.0

# Parent directory that holds per-task download workspaces. Each running
# ``_process_download`` invocation gets its own ``TEMP_DOWNLOAD_ROOT /
# <task_id>`` subdirectory so two concurrent tasks cannot stomp on each
# other's files or have the cleanup branch in one task delete the other's
# in-flight downloads.
TEMP_DOWNLOAD_ROOT = Path("temp_downloads")


def sanitize_filename(filename: str) -> str:
    """Sanitize filename for cross-platform filesystem compatibility.

    Cleans and normalizes filenames to ensure they work across different
    operating systems and filesystems. Removes potentially problematic
    characters including emojis, special symbols, and filesystem-reserved
    characters.

    Transformations Applied:
        1. Remove non-ASCII characters (including emojis and Unicode symbols)
        2. Replace filesystem-reserved characters with underscores
        3. Collapse multiple consecutive underscores to single underscore
        4. Trim leading/trailing spaces and underscores
        5. Provide fallback name for empty results

    Args:
        filename: Original filename string to sanitize.

    Returns:
        Sanitized filename safe for use across different filesystems.
        Returns 'untitled' if the input results in an empty string.

    Example:
        >>> sanitize_filename("My Video 📱 <2024>.mp4")
        "My_Video_2024.mp4"

        >>> sanitize_filename("文件名/with\\special:chars")
        "with_special_chars"

        >>> sanitize_filename("🎥📹🎬")
        "untitled"

    Note:
        This function is designed for content downloaded from Douyin which
        often contains emojis, Chinese characters, and special symbols in
        titles and descriptions.
    """
    # Remove emoji and non-ASCII characters for compatibility
    filename = re.sub(r"[^\x00-\x7F]+", "", filename)

    # Replace filesystem-reserved characters with underscores
    # Covers Windows, macOS, and Linux reserved characters
    filename = re.sub(r'[<>:"/\\|?*]', "_", filename)

    # Collapse multiple consecutive underscores to improve readability
    filename = re.sub(r"_+", "_", filename)

    # Remove leading/trailing whitespace and underscores
    filename = filename.strip("_ ")

    # Provide fallback for empty filenames
    return filename or "untitled"


def _parse_room_data(raw: Any) -> dict[str, Any] | None:
    """Decode the upstream ``room_data`` payload into a plain dict.

    Douyin returns ``room_data`` as a JSON-encoded string for active
    livestreams and ``None`` otherwise. The schema layer now expects a
    structured object so callers can reason about live metadata without
    rerunning the JSON parser. Malformed payloads are dropped rather
    than surfaced as a parsing error because the field is auxiliary.
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return decoded if isinstance(decoded, dict) else None
    return None


async def _safely_close_handler(handler: DouyinHandler) -> None:
    """Best-effort close of a DouyinHandler instance.

    The upstream ``f2`` SDK does not document a stable ``close`` API: some
    revisions expose ``aclose`` on the wrapped httpx client, others rely
    on garbage collection. Trying every common shape lets us release file
    descriptors today while keeping the helper safe to call against
    future SDK revisions that change the surface again.
    """
    for attr in ("aclose", "close"):
        target = getattr(handler, attr, None)
        if callable(target):
            try:
                result = target()
                if asyncio.iscoroutine(result):
                    await result
                return
            except Exception:  # pragma: no cover - defensive cleanup
                logger.debug(
                    "DouyinHandler close raised; ignoring",
                    extra={"close_method": attr},
                )
                return
    client = getattr(handler, "client", None)
    aclose = getattr(client, "aclose", None)
    if callable(aclose):
        try:
            await aclose()
        except Exception:  # pragma: no cover - defensive cleanup
            logger.debug("Underlying httpx client close raised; ignoring")


logger = ContextLogger(__name__)

# Alias for backward compatibility
UserServiceError = ServiceError
UserDownloadError = DownloadError


class UserService:
    """Domain logic for user profile lookup and bulk downloads.

    Public surface:
        - ``get_user_info(user_id)`` — typed Douyin profile.
        - ``start_download(user_id, include_posts, include_likes, max_items)``
          — persist a ``user_content_download`` operation row and
          schedule the background loop on ``BackgroundTaskRegistry``.
        - ``get_download_status(task_id)`` — current persisted state.

    Internal state is otherwise stateless: every download starts in a
    fresh per-task workspace under ``temp_downloads/<task_id>`` so two
    concurrent downloads cannot stomp on each other's files. The
    workspace is removed in a ``finally`` branch even on failure.
    """

    def __init__(
        self,
        operation_store: OperationStore | None = None,
        *,
        task_registry: BackgroundTaskRegistry | None = None,
    ) -> None:
        """Initialize the user service.

        Args:
            operation_store: Persistent operation record store. A private
                ``OperationStore`` is created when not provided, which is the
                path unit tests take.
            task_registry: Optional registry that owns long-lived
                background downloads. When omitted (e.g. in tests) the
                service falls back to ``asyncio.create_task`` so the public
                API remains testable without a full service container.
        """
        self.operation_store = operation_store or OperationStore()
        self.storage = R2StorageService()
        self._task_registry = task_registry

    async def get_user_info(self, user_id: str) -> UserResponse:
        """Retrieve user information from Douyin.

        The DouyinHandler instance is closed in a ``finally`` block so the
        underlying httpx client does not leak file descriptors when the
        endpoint is polled (livestream availability, profile refreshes,
        etc.). Older revisions instantiated a fresh handler per call and
        relied on garbage collection, which under sustained polling
        eventually exhausted the per-process FD limit.

        Args:
            user_id: The Douyin user ID.

        Returns:
            UserResponse: Object containing the user's information.

        Raises:
            UserNotFoundError: If the requested user cannot be found.
            UserServiceError: If an error occurs during the operation.
        """
        handler_kwargs = {
            "url": f"https://www.douyin.com/user/{user_id}",
            "cookie": settings.douyin_cookie,
            "headers": {
                "User-Agent": settings.douyin_user_agent,
                "Referer": settings.douyin_referer,
            },
            "proxy": settings.douyin_proxy_http,
            "mode": "post",
        }

        handler = DouyinHandler(handler_kwargs)
        try:
            user_data = await handler.fetch_user_profile(user_id)

            if not user_data.nickname:
                raise UserNotFoundError(f"User {user_id} not found")

            raw_user = user_data._to_raw()
            raw_room_data = raw_user.get("user", {}).get("room_data")
            room_data = _parse_room_data(raw_room_data)

            # Pydantic v2 coerces ``str`` to ``AnyHttpUrl`` at validation
            # time, so the explicit cast is safe. mypy still sees the
            # call-site argument as ``str | None``, so resolve the field
            # before the constructor call and pass it through ``cast``.
            avatar_value = str(user_data.avatar_url) if user_data.avatar_url else None
            return UserResponse(
                user_id=user_id,
                nickname=user_data.nickname,
                avatar_url=cast(AnyHttpUrl | None, avatar_value),
                signature=str(user_data.signature or ""),
                following_count=int(user_data.following_count or 0),  # type: ignore
                follower_count=int(user_data.follower_count or 0),  # type: ignore
                total_favorited=int(user_data.total_favorited or 0),  # type: ignore
                is_living=bool(user_data.room_id),  # type: ignore
                room_id=int(user_data.room_id) if user_data.room_id else None,  # type: ignore
                room_data=room_data,
            )
        except UserNotFoundError:
            raise
        except Exception as e:
            logger.exception("Failed to get user info", extra={"user_id": user_id})
            raise UserServiceError(f"Failed to get user info: {str(e)}") from e
        finally:
            await _safely_close_handler(handler)

    async def start_download(
        self,
        user_id: str,
        include_posts: bool = True,
        include_likes: bool = False,
        max_items: int | None = None,
    ) -> DownloadResponse:
        """Start an asynchronous download of user content from Douyin.

        Args:
            user_id: The Douyin user ID.
            include_posts: Whether to include the user's posts in the download.
            include_likes: Whether to include the user's liked posts in the download.
            max_items: Maximum number of items to download.

        Returns:
            DownloadResponse: Object containing details about the initiated download
                task.
        """
        await self.get_user_info(user_id)

        operation = await self.operation_store.create_operation(
            operation_type="user_content_download",
            subject_id=user_id,
            status="pending",
            message="Download scheduled",
            progress=0.0,
            metadata={
                "include_posts": include_posts,
                "include_likes": include_likes,
                "max_items": max_items,
            },
        )
        coro = self._process_download(
            operation.operation_id,
            user_id=user_id,
            include_posts=include_posts,
            include_likes=include_likes,
            max_items=max_items,
        )
        # Route through the shared registry so the FastAPI lifespan can
        # drain in-flight downloads before the executor pools are reaped.
        # ``spawn_or_fallback`` falls back to ``asyncio.create_task`` for
        # unit tests that instantiate ``UserService`` directly.
        spawn_or_fallback(
            self._task_registry,
            coro,
            name=f"user-download-{operation.operation_id}",
        )
        return DownloadResponse(**operation.to_response())

    async def get_download_status(self, task_id: str) -> DownloadResponse:
        """Get the status of a download task.

        Args:
            task_id: The unique identifier of the download task.

        Returns:
            DownloadResponse: Object containing the current status of the download task.

        Raises:
            OperationNotFoundError: If the specified download task is not found.
        """
        operation = await self.operation_store.get_operation(task_id)
        if operation.operation_type != "user_content_download":
            raise OperationNotFoundError(f"Download task {task_id} not found")
        return DownloadResponse(**operation.to_response())

    def _build_handler_kwargs(
        self,
        user_id: str,
        max_items: int | None,
        mode_label: str,
        downloading_likes_only: bool,
        include_likes: bool,
        temp_dir: Path,
    ) -> dict[str, Any]:
        """Assemble the keyword arguments forwarded to ``DouyinHandler``.

        Centralising the payload keeps ``_process_download`` focused on the
        state-machine flow and keeps the f2-specific knobs (mode, favorite
        toggle, naming template) in a single auditable place.

        Args:
            user_id: Target Douyin user identifier.
            max_items: Optional cap on the number of items to download.
            mode_label: ``"post"`` or ``"like"``; selects the f2 fetch mode.
            downloading_likes_only: ``True`` when the run targets the
                liked-items feed exclusively.
            include_likes: Original API flag from the caller; preserved so
                the ``download_favorite`` rule below stays correct.
            temp_dir: Per-task download workspace directory.

        Returns:
            Dict[str, Any]: Keyword payload suitable for ``DouyinHandler``.
        """
        return {
            "url": f"https://www.douyin.com/user/{user_id}",
            "cookie": settings.douyin_cookie,
            "headers": {
                "User-Agent": settings.douyin_user_agent,
                "Referer": settings.douyin_referer,
            },
            "proxy": settings.douyin_proxy_http,
            "download_path": str(temp_dir),
            "max_counts": max_items,
            # ``download_favorite`` only means "also fetch likes" when the
            # loop is already walking the posts feed. In the dedicated
            # likes path we point the fetcher at the likes endpoint
            # directly, so keep the flag off to avoid f2's implicit
            # dual-fetch behavior.
            "download_favorite": (include_likes and not downloading_likes_only),
            "timeout": 5,
            "folderize": True,
            "mode": mode_label,
            "naming": "{create}_{desc}",
            "download_image": True,
            "filename_filter": sanitize_filename,
        }

    @staticmethod
    def _resolve_total_posts(user_data: Any, downloading_likes_only: bool) -> int:
        """Return the expected total post count for the run.

        Likes-only runs leave the total at zero because the profile
        endpoint does not expose a liked-items count, so progress has to
        be tracked as an indeterminate counter. For the posts path the
        value is the profile's ``aweme_count`` coerced to ``int``; any
        non-int payload is treated as zero so the caller can short-circuit
        the empty-profile case.

        Args:
            user_data: The profile payload returned by ``DouyinHandler``.
            downloading_likes_only: ``True`` when the run targets the
                liked-items feed exclusively.

        Returns:
            int: Resolved total post count, or ``0`` if unknown.
        """
        if downloading_likes_only:
            return 0
        aweme_count = user_data.aweme_count
        return int(aweme_count) if isinstance(aweme_count, int) else 0

    async def _upload_directory_to_r2(
        self, user_dir: Path, user_id: str, user_data: Any
    ) -> tuple[int, int]:
        """Upload every file under ``user_dir`` to R2 storage.

        Walks the directory recursively, derives a content type from each
        file extension, generates the corresponding R2 path/metadata, and
        uploads. Local files are deleted only after a successful upload so
        a failed upload leaves the file on disk for the cleanup step to
        sweep -- this keeps the download workspace empty between batches
        without losing the file when the user retries.

        Args:
            user_dir: Local directory holding files just produced by f2.
            user_id: Douyin user identifier used to derive R2 paths.
            user_data: Profile payload supplying the author name for
                metadata.

        Returns:
            Tuple of ``(uploaded_count, failed_count)`` so the caller can
            promote partial-success states without re-walking the tree.
        """
        uploaded_count = 0
        failed_count = 0
        for file_path in user_dir.glob("**/*"):
            if not file_path.is_file():
                continue
            try:
                if file_path.suffix.lower() in [".jpg", ".jpeg", ".png", ".webp"]:
                    content_type = "image/" + file_path.suffix.lower().lstrip(".")
                else:
                    content_type = "video/mp4"
                r2_path = self.storage.generate_ugc_path(
                    user_id, file_path.name, content_type
                )
                metadata = self.storage.generate_metadata(
                    author=user_data.nickname,
                    category=ContentType.POSTS,
                    content_type=content_type,
                    source="douyin",
                )
                await self.storage.upload_file(
                    file_path, r2_path, metadata, content_type
                )
                file_path.unlink()
                uploaded_count += 1
            except Exception as e:
                failed_count += 1
                logger.error(
                    "Failed to upload file to R2",
                    extra={"file_path": str(file_path), "error": str(e)},
                )
        return uploaded_count, failed_count

    async def _finalize_status(
        self,
        task_id: str,
        downloaded: int,
        total_posts: int,
        failed_uploads: int,
        downloading_likes_only: bool,
    ) -> None:
        """Persist the terminal state for a finished download loop.

        Resolves the final status by combining download completeness with
        R2 upload outcomes. The status is downgraded to ``partial`` when
        any upload failed even if every post was fetched, so the operation
        record stays observable instead of silently reporting success.

        Args:
            task_id: Operation identifier to update.
            downloaded: Number of posts the loop successfully fetched.
            total_posts: Expected total from the profile, or ``0`` when
                unknown (likes-only runs).
            failed_uploads: Count of files that failed to upload to R2.
            downloading_likes_only: ``True`` when the run targets the
                liked-items feed exclusively; suppresses the missing-items
                error string because there is no authoritative total.
        """
        if total_posts > 0:
            completion_percentage = (downloaded / total_posts) * 100.0
        else:
            completion_percentage = 100.0

        upload_error: str | None = None
        if failed_uploads > 0:
            upload_error = f"R2 upload failed for {failed_uploads} file(s)"

        missing_error: str | None = None
        downloads_incomplete = (
            not downloading_likes_only and total_posts > 0 and downloaded < total_posts
        )
        if downloads_incomplete:
            missing_error = f"Only downloaded {downloaded} out of {total_posts} posts"

        if missing_error and upload_error:
            combined_error: str | None = f"{missing_error}; {upload_error}"
        else:
            combined_error = missing_error or upload_error

        if downloads_incomplete or failed_uploads > 0:
            status = "partial"
            message = "Download completed with missing items"
            logger.warning(
                "Finalising download with partial status",
                extra={
                    "task_id": task_id,
                    "downloaded": downloaded,
                    "total_posts": total_posts,
                    "failed_uploads": failed_uploads,
                    "completion_percentage": completion_percentage,
                },
            )
        else:
            status = "completed"
            message = "Download completed"
            logger.info(
                "Successfully downloaded %s posts (100%% complete)",
                downloaded,
            )

        # Clamp the partial percentage so the persisted ``progress`` field
        # honors the implicit 0-100 contract. ``completion_percentage`` can
        # exceed 100 when the run mixes posts and likes (``downloaded``
        # accumulates likes while ``total_posts`` only reflects
        # ``aweme_count``); without the clamp a single R2 upload failure
        # could surface a 130%-style progress value to API clients.
        progress_value = (
            100.0 if status == "completed" else min(completion_percentage, 100.0)
        )
        await self.operation_store.update_operation(
            task_id,
            status=status,
            message=message,
            error=combined_error,
            progress=progress_value,
            completed_items=downloaded,
            total_items=total_posts,
        )

    @staticmethod
    def _cleanup_temp_dir(temp_dir: Path | None) -> None:
        """Remove a per-task download workspace and all of its contents.

        Only the workspace itself is touched; the shared
        ``TEMP_DOWNLOAD_ROOT`` parent is preserved so concurrent tasks
        keep their own ``temp_downloads/<task_id>`` siblings intact.

        Args:
            temp_dir: Per-task workspace path. ``None`` and missing
                directories are tolerated so the caller can invoke the
                helper from a ``finally`` block without checking state.
        """
        if temp_dir is None or not temp_dir.exists():
            return

        # ``shutil.rmtree`` handles nested files in a single walk so a file
        # created mid-cleanup cannot strand a non-empty subdirectory the
        # way a hand-rolled two-pass glob walker would. The ``onexc``
        # callback (Python 3.12+) keeps the helper safe to call from a
        # ``finally`` block — a cleanup failure must never mask the
        # original error — but records each failed entry so a permission
        # issue on a production volume is still observable in logs
        # instead of silently leaving orphaned files behind. ``path`` is
        # typed ``str``: on Linux + Python 3.12 ``shutil.rmtree`` runs
        # the fd-based walker which calls ``os.fsdecode(path)`` at entry
        # before invoking ``onexc`` (CPython ``shutil.py``), so a bytes
        # path the kernel surfaced on a non-UTF-8 filesystem never
        # reaches this callback.
        def _on_rmtree_error(func: Any, path: str, exc: BaseException) -> None:
            logger.warning(
                "Failed to remove temp file during workspace cleanup",
                extra={
                    "path": path,
                    "operation": getattr(func, "__name__", str(func)),
                    "error": str(exc),
                },
            )

        shutil.rmtree(temp_dir, onexc=_on_rmtree_error)

    async def _process_download(
        self,
        task_id: str,
        *,
        user_id: str,
        include_posts: bool,
        include_likes: bool,
        max_items: int | None,
    ) -> None:
        """Process a download task asynchronously.

        Args:
            task_id: The unique identifier of the download task.
            user_id: The Douyin user whose content is being downloaded.
            include_posts: Whether to enumerate the user's own posts.
            include_likes: Whether to include the user's liked posts in the
                download batch forwarded to ``DouyinHandler``.
            max_items: Optional cap on the number of items to download.
        """
        if not include_posts and not include_likes:
            # Nothing was requested; record an immediate completion so the
            # operation record still reflects the resolved state instead of
            # leaving a pending row behind.
            await self.operation_store.update_operation(
                task_id,
                status="completed",
                message="Download skipped (nothing requested)",
                progress=100.0,
                total_items=0,
                completed_items=0,
            )
            return

        # ``include_posts=False`` with ``include_likes=True`` pivots the
        # whole loop to the user's liked-items feed so we actually honor
        # the API contract. f2 exposes a parallel ``fetch_user_like_videos``
        # whose page entries match the shape ``fetch_user_post_videos``
        # returns, which means the rest of the pipeline (downloader, R2
        # upload, progress bookkeeping) is unchanged.
        downloading_likes_only = not include_posts and include_likes
        mode_label = "like" if downloading_likes_only else "post"

        # Each task gets its own workspace under ``TEMP_DOWNLOAD_ROOT`` so
        # concurrent downloads cannot share files, and the cleanup branch
        # only ever touches one task's directory.
        temp_dir: Path | None = None
        downloaded_count = 0
        total_posts = 0
        failed_uploads = 0
        try:
            await self.operation_store.update_operation(
                task_id,
                status="running",
                message="Download in progress",
                progress=0.0,
                completed_items=0,
                error=None,
            )

            TEMP_DOWNLOAD_ROOT.mkdir(exist_ok=True)
            temp_dir = TEMP_DOWNLOAD_ROOT / task_id
            temp_dir.mkdir(parents=True, exist_ok=True)

            handler_kwargs = self._build_handler_kwargs(
                user_id=user_id,
                max_items=max_items,
                mode_label=mode_label,
                downloading_likes_only=downloading_likes_only,
                include_likes=include_likes,
                temp_dir=temp_dir,
            )
            handler = DouyinHandler(handler_kwargs)

            # Get user profile to create user directory and verify post count
            user_data = await handler.fetch_user_profile(user_id)
            if not user_data.nickname:
                raise UserNotFoundError(f"User {user_id} not found")

            total_posts = self._resolve_total_posts(user_data, downloading_likes_only)
            if not downloading_likes_only and total_posts == 0:
                logger.info("User %s has no posts", user_id)
                await self.operation_store.update_operation(
                    task_id,
                    status="completed",
                    message="Download completed",
                    progress=100.0,
                    total_items=0,
                    completed_items=0,
                )
                return

            if downloading_likes_only:
                logger.info("User %s requested likes-only download", user_data.nickname)
                await self.operation_store.update_operation(
                    task_id,
                    message="Downloading liked items",
                )
            else:
                logger.info("User %s has %s posts", user_data.nickname, total_posts)
                await self.operation_store.update_operation(
                    task_id,
                    total_items=total_posts,
                    message="Download in progress",
                )

            # Create user directory inside the per-task workspace.
            user_dir = temp_dir / user_data.nickname
            user_dir.mkdir(exist_ok=True)

            # Use the handler to download user posts.
            max_cursor = 0
            has_more = True

            # Guard against an unbounded outer loop. ``max_cursor += 1`` in
            # the "cursor stuck" branch below can spin indefinitely if the
            # upstream API returns a sticky cursor or if every page's
            # uploads fail, because ``downloaded_count`` never catches up
            # to ``total_posts``. The page-budget heuristic uses
            # module-level constants (``PAGE_SIZE``, ``PAGE_SLACK``,
            # ``PAGE_MULTIPLIER``, ``MAX_PAGES_FALLBACK``) so the cap can
            # be retuned in one place if production traffic ever hits a
            # false-positive break.
            if max_items is not None:
                max_pages = (max_items // PAGE_SIZE) + PAGE_SLACK
            elif total_posts > 0:
                max_pages = (total_posts // PAGE_SIZE) * PAGE_MULTIPLIER + PAGE_SLACK
            else:
                max_pages = MAX_PAGES_FALLBACK
            page_count = 0

            # ``fetch_user_like_videos`` has no ``min_cursor`` parameter,
            # so only include it for the posts fetcher.
            if downloading_likes_only:
                fetcher = handler.fetch_user_like_videos
                fetcher_extra: dict[str, Any] = {}
            else:
                fetcher = handler.fetch_user_post_videos
                fetcher_extra = {"min_cursor": 0}

            while has_more and (max_items is None or downloaded_count < max_items):
                page_count += 1
                if page_count > max_pages:
                    logger.warning(
                        "Download loop exceeded max_pages; stopping",
                        extra={
                            "task_id": task_id,
                            "user_id": user_id,
                            "max_pages": max_pages,
                            "downloaded_count": downloaded_count,
                            "total_posts": total_posts,
                        },
                    )
                    break
                iterated = False
                async for aweme_data in fetcher(
                    user_id,
                    max_cursor=max_cursor,
                    page_counts=PAGE_SIZE,
                    max_counts=max_items,
                    **fetcher_extra,
                ):
                    iterated = True
                    if not aweme_data.has_aweme:
                        has_more = False
                        break

                    current_batch_size = len(aweme_data.aweme_id)
                    downloaded_count += current_batch_size
                    # Likes-only runs have no known total up front, so we
                    # cannot express progress as a percentage. Report
                    # ``progress=None`` in that case and only fill in a
                    # numeric percentage for the posts path where
                    # ``total_posts`` reflects the profile's aweme_count.
                    if downloading_likes_only:
                        progress = None
                    elif total_posts > 0:
                        progress = (downloaded_count / total_posts) * 100
                    else:
                        progress = 100.0

                    update_fields: dict[str, Any] = {
                        "completed_items": downloaded_count,
                        "total_items": total_posts,
                        "message": "Download in progress",
                    }
                    if progress is not None:
                        update_fields["progress"] = progress
                    await self.operation_store.update_operation(
                        task_id, **update_fields
                    )

                    if progress is None:
                        logger.info(
                            "Downloaded %s liked items so far",
                            downloaded_count,
                        )
                    else:
                        logger.info(
                            "Downloaded %s/%s posts (%.1f%%)",
                            downloaded_count,
                            total_posts,
                            progress,
                        )

                    # Download files to the per-task workspace.
                    await handler.downloader.create_download_tasks(
                        handler_kwargs, aweme_data._to_list(), user_dir
                    )

                    # Push the freshly downloaded files to R2 and track
                    # any failures so the final status can be downgraded.
                    batch_uploaded, batch_failed = await self._upload_directory_to_r2(
                        user_dir, user_id, user_data
                    )
                    failed_uploads += batch_failed
                    logger.debug(
                        "R2 upload batch finished",
                        extra={
                            "task_id": task_id,
                            "user_id": user_id,
                            "uploaded": batch_uploaded,
                            "failed": batch_failed,
                        },
                    )

                    # Update cursor for next page
                    if (
                        aweme_data.max_cursor
                        and isinstance(aweme_data.max_cursor, int)
                        and aweme_data.max_cursor != max_cursor
                    ):
                        max_cursor = aweme_data.max_cursor
                        has_more = aweme_data.has_more
                    else:
                        # Sticky upstream cursor: previously this branch
                        # did ``max_cursor += 1`` and kept going, but the
                        # synthetic value is not recognised upstream and
                        # the same window keeps being re-fetched, leading
                        # to duplicate downloads. Treat a stuck cursor as
                        # the end of the feed so the loop terminates.
                        logger.info(
                            "Upstream cursor stuck during user download; ending loop",
                            extra={
                                "task_id": task_id,
                                "user_id": user_id,
                                "cursor": max_cursor,
                            },
                        )
                        has_more = False

                    # If max_items is set and we've reached it, stop
                    if max_items and downloaded_count >= max_items:
                        has_more = False
                        break

                    # Add delay between pages
                    await asyncio.sleep(PAGE_FETCH_DELAY_SECONDS)

                if not iterated:
                    # The fetcher produced no items (either because the
                    # upstream feed is empty or because an earlier batch
                    # already advanced past ``max_counts``). Without this
                    # bail-out ``has_more`` stays True and the outer while
                    # loop spins.
                    has_more = False

            await self._finalize_status(
                task_id=task_id,
                downloaded=downloaded_count,
                total_posts=total_posts,
                failed_uploads=failed_uploads,
                downloading_likes_only=downloading_likes_only,
            )

        except Exception as e:
            logger.exception(
                "Download failed",
                extra={"task_id": task_id, "user_id": user_id},
            )
            await self.operation_store.update_operation(
                task_id,
                status="failed",
                message="Download failed",
                error=str(e),
            )

        finally:
            self._cleanup_temp_dir(temp_dir)
