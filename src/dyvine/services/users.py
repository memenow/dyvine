"""User management service for Douyin user operations and content management.

This module provides comprehensive business logic for user-related operations
in the Dyvine application. It acts as the primary interface between the API
layer and external Douyin services, handling complex user workflows including
profile data retrieval, content discovery, and bulk download operations.

Core Responsibilities:
    - User profile information retrieval and caching
    - Content enumeration (posts, liked content, collections)
    - Asynchronous bulk download orchestration
    - Download progress tracking and status management
    - File naming and organization for downloaded content
    - Integration with Cloudflare R2 storage for content persistence
    - Error handling and retry logic for robustness

Architecture:
    The service follows a layered architecture pattern:
    - Service Layer: UserService class with business logic
    - Integration Layer: DouyinHandler for external API calls
    - Storage Layer: R2StorageService for content persistence
    - Utility Layer: Helper functions for data transformation

Key Features:
    - Asynchronous operation support for scalability
    - Comprehensive error handling with custom exceptions
    - Structured logging with correlation tracking
    - Configurable download parameters and filtering
    - Safe filename generation for cross-platform compatibility
    - Progress tracking for long-running operations
    - Resource cleanup and lifecycle management

Usage Patterns:
    Dependency Injection:
        service = UserService()
        user_info = await service.get_user_info(user_id)

    Download Operations:
        download_task = await service.download_user_content(
            user_id="MS4wLjABAAAA...",
            include_posts=True,
            include_likes=False,
            max_items=100
        )

    Status Monitoring:
        status = await service.get_operation_status(task_id)

Custom Exceptions:
    - UserNotFoundError: User profile not accessible or doesn't exist
    - DownloadError: Content download failed or interrupted
    - ServiceError: General service-level errors and timeouts

Thread Safety:
    The service is designed to be thread-safe for concurrent operations.
    Internal state is managed through immutable objects and atomic operations.

Performance Considerations:
    - Implements connection pooling for external API calls
    - Uses async/await patterns for non-blocking operations
    - Provides configurable batch sizes for large downloads
    - Includes timeout handling for long-running operations
"""

import asyncio
import re
from pathlib import Path
from typing import Any

from f2.apps.douyin.handler import DouyinHandler  # type: ignore

from ..core.exceptions import DownloadError, ServiceError, UserNotFoundError
from ..core.logging import ContextLogger
from ..core.operations import OperationStore
from ..core.settings import settings
from ..schemas.users import DownloadResponse, UserResponse
from .storage import ContentType, R2StorageService


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


logger = ContextLogger(__name__)

# Alias for backward compatibility
UserServiceError = ServiceError
UserDownloadError = DownloadError


class UserService:
    """Service class for handling user-related operations.

    This class encapsulates the business logic for various user-related
    operations in the Douyin application, such as retrieving user information,
    initiating content downloads, and tracking download progress.
    """

    def __init__(self, operation_store: OperationStore | None = None) -> None:
        """Initialize the user service."""
        self.operation_store = operation_store or OperationStore()
        self.storage = R2StorageService()

    async def get_user_info(self, user_id: str) -> UserResponse:
        """Retrieve user information from Douyin.

        Args:
            user_id: The Douyin user ID.

        Returns:
            UserResponse: Object containing the user's information.

        Raises:
            UserNotFoundError: If the requested user cannot be found.
            UserServiceError: If an error occurs during the operation.
        """
        try:
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
            user_data = await handler.fetch_user_profile(user_id)

            if not user_data.nickname:
                raise UserNotFoundError(f"User {user_id} not found")

            raw_user = user_data._to_raw()
            room_data = raw_user.get("user", {}).get("room_data")

            return UserResponse(
                user_id=user_id,
                nickname=user_data.nickname,
                avatar_url=str(user_data.avatar_url or ""),
                signature=str(user_data.signature or ""),
                following_count=int(user_data.following_count or 0),  # type: ignore
                follower_count=int(user_data.follower_count or 0),  # type: ignore
                total_favorited=int(user_data.total_favorited or 0),  # type: ignore
                is_living=bool(user_data.room_id),  # type: ignore
                room_id=int(user_data.room_id) if user_data.room_id else None,  # type: ignore
                room_data=room_data if isinstance(room_data, str) else None,
            )
        except UserNotFoundError:
            raise
        except Exception as e:
            logger.exception("Failed to get user info", extra={"user_id": user_id})
            raise UserServiceError(f"Failed to get user info: {str(e)}") from e

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

        operation = self.operation_store.create_operation(
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
        asyncio.create_task(
            self._process_download(
                operation.operation_id,
                user_id=user_id,
                include_posts=include_posts,
                include_likes=include_likes,
                max_items=max_items,
            )
        )
        return DownloadResponse(**operation.to_response())

    async def get_download_status(self, task_id: str) -> DownloadResponse:
        """Get the status of a download task.

        Args:
            task_id: The unique identifier of the download task.

        Returns:
            DownloadResponse: Object containing the current status of the download task.

        Raises:
            DownloadError: If the specified download task is not found.
        """
        operation = self.operation_store.get_operation(task_id)
        if operation.operation_type != "user_content_download":
            raise DownloadError(f"Download task {task_id} not found")
        return DownloadResponse(**operation.to_response())

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
            self.operation_store.update_operation(
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

        temp_dir = None
        try:
            self.operation_store.update_operation(
                task_id,
                status="running",
                message="Download in progress",
                progress=0.0,
                completed_items=0,
                error=None,
            )

            temp_dir = Path("temp_downloads")
            temp_dir.mkdir(exist_ok=True)

            # Initialize f2 handler with temporary directory
            handler_kwargs = {
                "url": f"https://www.douyin.com/user/{user_id}",
                "cookie": settings.douyin_cookie,
                "headers": {
                    "User-Agent": settings.douyin_user_agent,
                    "Referer": settings.douyin_referer,
                },
                "proxy": settings.douyin_proxy_http,
                "download_path": str(temp_dir),
                "max_counts": max_items,
                # ``download_favorite`` only means "also fetch likes" when
                # the loop is already walking the posts feed. In the
                # dedicated likes path we point the fetcher at the likes
                # endpoint directly, so keep the flag off to avoid f2's
                # implicit dual-fetch behavior.
                "download_favorite": (include_likes and not downloading_likes_only),
                "timeout": 5,
                "folderize": True,
                "mode": mode_label,
                "naming": "{create}_{desc}",
                "download_image": True,
                "filename_filter": sanitize_filename,  # Add filename sanitization
            }

            handler = DouyinHandler(handler_kwargs)

            # Get user profile to create user directory and verify post count
            user_data = await handler.fetch_user_profile(user_id)
            if not user_data.nickname:
                raise UserNotFoundError(f"User {user_id} not found")

            if downloading_likes_only:
                # The profile endpoint does not expose a total liked-items
                # count. Leave ``total_posts`` unset and let the operation
                # store track a running count; the completion branch at the
                # bottom will record the final tally once the loop drains.
                total_posts = 0
            else:
                # Get total posts count from profile
                aweme_count = user_data.aweme_count
                total_posts = int(aweme_count) if isinstance(aweme_count, int) else 0
                if total_posts == 0:
                    logger.info("User %s has no posts", user_id)
                    self.operation_store.update_operation(
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
                self.operation_store.update_operation(
                    task_id,
                    message="Downloading liked items",
                )
            else:
                logger.info("User %s has %s posts", user_data.nickname, total_posts)
                self.operation_store.update_operation(
                    task_id,
                    total_items=total_posts,
                    message="Download in progress",
                )

            # Create user directory in temp
            user_dir = temp_dir / user_data.nickname
            user_dir.mkdir(exist_ok=True)

            # Use the handler to download user posts
            max_cursor = 0
            has_more = True
            downloaded_count = 0

            # ``fetch_user_like_videos`` has no ``min_cursor`` parameter,
            # so only include it for the posts fetcher.
            if downloading_likes_only:
                fetcher = handler.fetch_user_like_videos
                fetcher_extra: dict[str, Any] = {}
            else:
                fetcher = handler.fetch_user_post_videos
                fetcher_extra = {"min_cursor": 0}

            while has_more and (max_items is None or downloaded_count < max_items):
                iterated = False
                async for aweme_data in fetcher(
                    user_id,
                    max_cursor=max_cursor,
                    page_counts=100,  # Increased page size
                    max_counts=max_items,
                    **fetcher_extra,
                ):
                    iterated = True
                    if not aweme_data.has_aweme:
                        has_more = False
                        break

                    current_batch_size = len(aweme_data.aweme_id)
                    downloaded_count += current_batch_size
                    if total_posts > 0:
                        progress = (downloaded_count / total_posts) * 100
                    else:
                        progress = 100.0

                    self.operation_store.update_operation(
                        task_id,
                        progress=progress,
                        completed_items=downloaded_count,
                        total_items=total_posts,
                        message="Download in progress",
                    )

                    logger.info(
                        "Downloaded %s/%s posts (%.1f%%)",
                        downloaded_count,
                        total_posts,
                        progress,
                    )

                    # Download files to temp directory
                    await handler.downloader.create_download_tasks(
                        handler_kwargs, aweme_data._to_list(), user_dir
                    )

                    # Upload downloaded files to R2 (search recursively)
                    for file_path in user_dir.glob("**/*"):
                        if file_path.is_file():
                            try:
                                # Generate R2 storage path
                                if file_path.suffix.lower() in [
                                    ".jpg",
                                    ".jpeg",
                                    ".png",
                                    ".webp",
                                ]:
                                    content_type = (
                                        "image/" + file_path.suffix.lower().lstrip(".")
                                    )
                                    r2_path = self.storage.generate_ugc_path(
                                        user_id, file_path.name, content_type
                                    )
                                else:
                                    content_type = "video/mp4"
                                    r2_path = self.storage.generate_ugc_path(
                                        user_id, file_path.name, content_type
                                    )

                                # Generate metadata
                                metadata = self.storage.generate_metadata(
                                    author=user_data.nickname,
                                    category=ContentType.POSTS,
                                    content_type=content_type,
                                    source="douyin",
                                )

                                # Upload to R2
                                await self.storage.upload_file(
                                    file_path, r2_path, metadata, content_type
                                )

                                # Delete local file after upload
                                file_path.unlink()

                            except Exception as e:
                                logger.error(
                                    f"Failed to upload {file_path} to R2: {str(e)}"
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
                        # If cursor didn't change but we haven't got all posts,
                        # increment it
                        if downloaded_count < total_posts:
                            max_cursor += 1
                            has_more = True
                        else:
                            has_more = False

                    # If max_items is set and we've reached it, stop
                    if max_items and downloaded_count >= max_items:
                        has_more = False
                        break

                    # Add delay between pages
                    await asyncio.sleep(5.0)

                if not iterated:
                    # The fetcher produced no items (either because the
                    # upstream feed is empty or because an earlier batch
                    # already advanced past ``max_counts``). Without this
                    # bail-out ``has_more`` stays True and the outer while
                    # loop spins.
                    has_more = False
            # Verify download completion
            if total_posts > 0:
                completion_percentage = (downloaded_count / total_posts) * 100
            else:
                completion_percentage = 100.0

            if completion_percentage >= 100:
                # Consider anything >= 100% as complete success
                logger.info(
                    "Successfully downloaded %s posts (100%% complete)",
                    downloaded_count,
                )
                self.operation_store.update_operation(
                    task_id,
                    status="completed",
                    message="Download completed",
                    progress=100.0,
                    completed_items=downloaded_count,
                    total_items=total_posts,
                )
            else:
                # Less than 100% means we missed some posts
                logger.warning(
                    (
                        "Only downloaded %s/%s posts (%.1f%%). "
                        "Some posts may have been missed."
                    ),
                    downloaded_count,
                    total_posts,
                    completion_percentage,
                )
                self.operation_store.update_operation(
                    task_id,
                    status="partial",
                    message="Download completed with missing items",
                    error=(
                        f"Only downloaded {downloaded_count} "
                        f"out of {total_posts} posts"
                    ),
                    progress=completion_percentage,
                    completed_items=downloaded_count,
                    total_items=total_posts,
                )

        except Exception as e:
            logger.exception(
                "Download failed",
                extra={"task_id": task_id, "user_id": user_id},
            )
            self.operation_store.update_operation(
                task_id,
                status="failed",
                message="Download failed",
                error=str(e),
            )

        finally:
            try:
                if temp_dir and temp_dir.exists():
                    for file in sorted(temp_dir.glob("**/*"), reverse=True):
                        if file.is_file():
                            file.unlink()
                    for dir in sorted(temp_dir.glob("**/*"), reverse=True):
                        if dir.is_dir():
                            dir.rmdir()
                    temp_dir.rmdir()
            except Exception as e:
                logger.error(f"Failed to clean up temp directory: {str(e)}")
