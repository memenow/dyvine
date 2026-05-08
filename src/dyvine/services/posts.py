"""Service layer for managing Douyin post operations.

This module provides a service class (PostService) that encapsulates the
high-level business logic for handling Douyin post-related operations,
including:

- Fetching detailed information about a specific post
- Retrieving a paginated list of posts from a user
- Downloading post content (videos, images, live streams, collections, stories)
- Managing bulk download operations for all posts of a user

The PostService class implements proper error handling, comprehensive logging,
and follows dependency injection patterns. It interacts with the DouyinHandler
and DouyinDownloader classes from the f2 library to perform the necessary
operations.

The module also defines custom exceptions related to post operations, such as
PostNotFoundError, UserNotFoundError, and DownloadError.
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from f2.apps.douyin.db import AsyncUserDB  # type: ignore
from f2.apps.douyin.handler import DouyinHandler  # type: ignore

from ..core.background import BackgroundTaskRegistry, spawn_or_fallback
from ..core.exceptions import (
    OperationNotFoundError,
    PostNotFoundError,
    ServiceError,
    UserNotFoundError,
)
from ..core.logging import ContextLogger
from ..core.operations import OperationStore
from ..core.pagination import MAX_PAGES_FALLBACK, PAGE_MULTIPLIER, PAGE_SLACK
from ..core.path_safety import relative_to_download_root
from ..schemas.posts import (
    BulkDownloadResponse,
    DownloadStatus,
    ImageInfo,
    PostDetail,
    PostType,
    VideoInfo,
)

logger = ContextLogger(__name__)

# Alias for backward compatibility
PostServiceError = ServiceError

# Page size requested from ``_fetch_posts_batch``. The outer loop guard
# combines this with the shared :mod:`dyvine.core.pagination` constants
# so a sticky upstream cursor cannot keep the ``+1`` cursor advance
# spinning forever. The fallback covers ``MAX_PAGES_FALLBACK * PAGE_SIZE``
# items when ``total_posts`` is unknown.
PAGE_SIZE = 20


@dataclass(slots=True)
class UserPostsPage:
    """Single-page result from :meth:`PostService.get_user_posts`.

    Carries both the materialised ``PostDetail`` items and the raw
    upstream cursor needed to fetch the next page. Routers wrap the
    cursor in an opaque token; service callers receive the integer
    Douyin cursor verbatim. ``next_cursor`` is ``None`` when the feed
    is exhausted (``has_more=False`` upstream).
    """

    posts: list[PostDetail]
    next_cursor: int | None
    has_more: bool


class PostService:
    """Service class for handling Douyin post operations.

    This class encapsulates the business logic for various post-related
    operations, such as fetching post details, retrieving user posts,
    downloading post content, and managing bulk downloads. It maintains the
    following principles:

    - Proper error handling through custom exceptions
    - Comprehensive logging for tracking operations
    - Clean separation of concerns by delegating specific tasks to helper
      methods
    - Type safety through type annotations
    """

    # Class-level default mirrors the pattern used by ``LivestreamService`` so
    # tests that build the service via ``object.__new__`` (bypassing
    # ``__init__``) still see a ``None`` registry and fall through to the bare
    # ``asyncio.create_task`` branch in :func:`spawn_or_fallback`.
    _task_registry: BackgroundTaskRegistry | None = None

    def __init__(
        self,
        handler: DouyinHandler,
        *,
        operation_store: OperationStore | None = None,
        task_registry: BackgroundTaskRegistry | None = None,
    ) -> None:
        """Initialize the PostService instance.

        Args:
            handler: Configured DouyinHandler instance for Douyin operations.
            operation_store: Persistent operation record store. A private
                ``OperationStore`` is created when not provided, which is the
                path unit tests take.
            task_registry: Optional registry that owns long-lived bulk
                download tasks. When omitted (e.g. in tests) the service
                falls back to ``asyncio.create_task`` so the public API
                remains testable without a full service container.
        """
        self.handler = handler
        self.operation_store = operation_store or OperationStore()
        self._task_registry = task_registry
        logger.info("PostService initialized", extra={"handler_config": handler.kwargs})

    async def get_post_detail(self, aweme_id: str) -> PostDetail:
        """Fetch detailed information about a specific Douyin post.

        Args:
            aweme_id: Unique identifier of the post.

        Returns:
            PostDetail: Object containing detailed information about the post.

        Raises:
            PostNotFoundError: If the requested post cannot be found.
            PostServiceError: If an error occurs during the operation.
        """
        try:
            logger.info("Fetching post detail", extra={"aweme_id": aweme_id})
            post = await self.handler.fetch_one_video(aweme_id)

            if not post:
                raise PostNotFoundError(f"Post not found: {aweme_id}")

            post_data = post._to_dict()

            # Parse create_time string into a Unix timestamp (integer)
            create_time_str = post_data.get("create_time")
            create_time = 0
            if create_time_str:
                try:
                    create_time_dt = datetime.strptime(
                        create_time_str, "%Y-%m-%d %H-%M-%S"
                    )
                    create_time = int(create_time_dt.timestamp())
                except (ValueError, TypeError):
                    create_time = 0

            return PostDetail(
                aweme_id=post_data["aweme_id"],
                desc=post_data.get("desc", ""),
                create_time=create_time,
                post_type=self._determine_post_type(post_data),
                video_info=self._extract_video_info(post_data),
                images=self._extract_image_info(post_data),
                statistics=post_data.get("statistics", {}),
            )

        except PostNotFoundError:
            raise
        except Exception as e:
            logger.exception(
                "Error fetching post detail",
                extra={"aweme_id": aweme_id, "error": str(e)},
            )
            raise PostServiceError(f"Failed to fetch post: {str(e)}") from e

    async def get_user_posts(
        self,
        sec_user_id: str,
        max_cursor: int = 0,
        count: int = 20,
    ) -> UserPostsPage:
        """Retrieve a paginated list of posts from a Douyin user.

        ``count`` is intentionally a literal default rather than the
        module-level :data:`PAGE_SIZE`. The latter binds the bulk
        download loop guard to the fetcher used by
        ``_fetch_posts_batch``; this single-page API is a public read
        endpoint whose contract should not silently change if a future
        tuning PR adjusts :data:`PAGE_SIZE`.

        Args:
            sec_user_id: Unique identifier of the user.
            max_cursor: Douyin pagination cursor for fetching the next
                batch of posts. ``0`` requests the first page.
            count: Number of posts to fetch per page.

        Returns:
            UserPostsPage: Materialised posts plus the raw upstream
            ``max_cursor`` for the next request. Callers must echo
            ``next_cursor`` back unchanged on the follow-up call;
            offset arithmetic on it does not produce a valid Douyin
            cursor.

        Raises:
            UserNotFoundError: If the requested user cannot be found.
            PostServiceError: If an error occurs during the operation.
        """
        try:
            logger.info(
                "Fetching user posts",
                extra={
                    "sec_user_id": sec_user_id,
                    "max_cursor": max_cursor,
                    "count": count,
                },
            )

            posts_iterator = self.handler.fetch_user_post_videos(
                sec_user_id=sec_user_id, max_cursor=max_cursor, page_counts=count
            )

            try:
                posts_filter = await posts_iterator.__anext__()
            except StopAsyncIteration:
                logger.warning("No posts found", extra={"sec_user_id": sec_user_id})
                return UserPostsPage(posts=[], next_cursor=None, has_more=False)
            finally:
                aclose = getattr(posts_iterator, "aclose", None)
                if callable(aclose):
                    await aclose()

            raw_data = posts_filter._to_raw()
            aweme_list = raw_data.get("aweme_list") or []

            has_more = bool(raw_data.get("has_more"))
            raw_next = raw_data.get("max_cursor")
            next_cursor: int | None
            if has_more and isinstance(raw_next, int):
                # The upstream cursor is a Douyin-defined sentinel, not
                # an offset; stuck cursors (``raw_next == max_cursor``)
                # mean the feed is exhausted and we expose ``None`` so
                # the router does not invite the caller to re-fetch the
                # same window.
                next_cursor = raw_next if raw_next != max_cursor else None
            else:
                next_cursor = None

            if not aweme_list:
                logger.warning(
                    "User posts response empty",
                    extra={
                        "sec_user_id": sec_user_id,
                        "has_more": has_more,
                        "status_msg": raw_data.get("status_msg"),
                    },
                )
                return UserPostsPage(posts=[], next_cursor=next_cursor, has_more=False)

            posts = [
                PostDetail(
                    aweme_id=post["aweme_id"],
                    desc=post.get("desc", ""),
                    create_time=post.get("create_time", 0),
                    post_type=self._determine_post_type(post),
                    video_info=self._extract_video_info(post),
                    images=self._extract_image_info(post),
                    statistics=post.get("statistics", {}),
                )
                for post in aweme_list
            ]

            return UserPostsPage(
                posts=posts,
                next_cursor=next_cursor,
                has_more=has_more and next_cursor is not None,
            )

        except UserNotFoundError:
            raise
        except Exception as e:
            logger.exception(
                "Error fetching user posts",
                extra={"sec_user_id": sec_user_id, "error": str(e)},
            )
            raise PostServiceError(f"Failed to fetch user posts: {str(e)}") from e

    async def start_bulk_download(
        self,
        sec_user_id: str,
        max_cursor: int = 0,
    ) -> BulkDownloadResponse:
        """Schedule an asynchronous bulk download of every post from a user.

        Validates the user profile up front so the caller receives a 404
        immediately when the account does not exist, then persists a
        ``pending`` operation record and dispatches the long-running
        pagination + download loop onto the shared background task
        registry. The HTTP layer can poll
        :meth:`get_bulk_download_status` with the returned ``operation_id``
        to observe progress.

        Args:
            sec_user_id: Unique identifier of the user.
            max_cursor: Starting pagination cursor for fetching posts.

        Returns:
            BulkDownloadResponse: Pending response carrying the
                ``operation_id`` clients can use to poll for progress.

        Raises:
            UserNotFoundError: If the requested user cannot be found.
            PostServiceError: If the profile lookup itself fails.
        """
        try:
            logger.info(
                "Validating user before scheduling bulk download",
                extra={"sec_user_id": sec_user_id, "max_cursor": max_cursor},
            )
            profile = await self.handler.fetch_user_profile(sec_user_id)
        except UserNotFoundError:
            raise
        except Exception as e:
            logger.exception(
                "Failed to validate user profile",
                extra={"sec_user_id": sec_user_id, "error": str(e)},
            )
            raise PostServiceError(f"Failed to validate user profile: {str(e)}") from e

        if not profile or not getattr(profile, "nickname", None):
            raise UserNotFoundError(f"User not found: {sec_user_id}")

        operation = await self.operation_store.create_operation(
            operation_type="user_posts_bulk_download",
            subject_id=sec_user_id,
            status="pending",
            message="Bulk download scheduled",
            progress=0.0,
            metadata={"max_cursor": max_cursor},
        )

        # Forward the already-fetched profile to the background coroutine so
        # the bulk loop does not have to repeat the upstream call. A second
        # ``fetch_user_profile`` here would double the network cost and open
        # a small failure window where the existence check passed but the
        # bulk loop sees a transient error.
        coro = self._run_bulk_download(
            operation.operation_id, sec_user_id, max_cursor, profile=profile
        )
        # Route through the shared registry so the FastAPI lifespan can
        # drain the in-flight bulk download before the executor pools are
        # reaped. ``spawn_or_fallback`` falls back to ``asyncio.create_task``
        # for unit tests that instantiate ``PostService`` directly.
        spawn_or_fallback(
            self._task_registry,
            coro,
            name=f"posts-bulk-{operation.operation_id}",
        )

        return BulkDownloadResponse(
            operation_id=operation.operation_id,
            sec_user_id=sec_user_id,
            download_path=None,
            total_posts=0,
            downloaded_count=dict.fromkeys(PostType, 0),
            total_downloaded=0,
            status=DownloadStatus.PENDING,
            message="Bulk download scheduled",
            error_details=None,
        )

    async def _run_bulk_download(
        self,
        operation_id: str,
        sec_user_id: str,
        max_cursor: int,
        *,
        profile: Any,
    ) -> None:
        """Execute the bulk download loop and persist progress to the store.

        Mirrors the orchestration shape used by
        ``UserService._process_download``: the operation row is moved to
        ``running`` on entry, refreshed after each successful batch with
        the running tally, and finalized via ``completed`` / ``partial`` /
        ``failed`` once the loop terminates.

        Args:
            operation_id: Identifier of the persisted operation record.
            sec_user_id: Unique identifier of the user.
            max_cursor: Starting pagination cursor for fetching posts.
            profile: The user profile already validated by
                :meth:`start_bulk_download`. Re-using the existing payload
                avoids a second ``fetch_user_profile`` call.
        """
        download_stats: dict[PostType, int] = dict.fromkeys(PostType, 0)
        download_path: str | None = None
        batch_errored = False
        batch_error_message: str | None = None
        failed_count = 0

        aweme_count = getattr(profile, "aweme_count", 0)
        total_posts = aweme_count if isinstance(aweme_count, int) else 0

        try:
            await self.operation_store.update_operation(
                operation_id,
                status="running",
                message="Bulk download in progress",
                progress=0.0,
                completed_items=0,
                error=None,
            )

            # Defensive guard: ``start_bulk_download`` already validated the
            # profile, but ad-hoc callers (or future refactors) may invoke
            # ``_run_bulk_download`` with an invalid payload. Reject it so the
            # operation moves to ``failed`` instead of silently iterating
            # through an empty profile.
            if not profile or not getattr(profile, "nickname", None):
                raise UserNotFoundError(f"User not found: {sec_user_id}")

            logger.info(
                "Bulk download starting with cached profile",
                extra={
                    "sec_user_id": sec_user_id,
                    "operation_id": operation_id,
                    "nickname": getattr(profile, "nickname", None),
                    "total_posts": total_posts,
                },
            )

            await self.operation_store.update_operation(
                operation_id,
                total_items=total_posts,
                message="Bulk download in progress",
            )

            # Set up user directory
            async with AsyncUserDB("douyin_users.db") as db:
                user_path = await self.handler.get_or_add_user_data(
                    self.handler.kwargs, sec_user_id, db
                )
                # Persist the path relative to the configured download
                # root so the public API surface never leaks the on-disk
                # absolute layout.
                download_path = relative_to_download_root(user_path)
                logger.info(
                    "Download directory created",
                    extra={"download_path": download_path},
                )
            await self.operation_store.update_operation(
                operation_id,
                download_path=download_path,
            )

            current_cursor = max_cursor
            # Bound the outer loop so a sticky upstream cursor cannot pin a
            # worker forever even when each page is non-empty. The
            # ``+1`` cursor advance below keeps progress moving, but if
            # the server keeps replying with the same ``max_cursor`` the
            # only stop conditions become ``has_more=False`` or this cap.
            if total_posts > 0:
                max_pages = (total_posts // PAGE_SIZE) * PAGE_MULTIPLIER + PAGE_SLACK
            else:
                max_pages = MAX_PAGES_FALLBACK
            page_count = 0

            while True:
                page_count += 1
                if page_count > max_pages:
                    logger.warning(
                        "Bulk download loop exceeded max_pages; stopping",
                        extra={
                            "sec_user_id": sec_user_id,
                            "operation_id": operation_id,
                            "cursor": current_cursor,
                            "max_pages": max_pages,
                            "total_downloaded": sum(download_stats.values()),
                            "total_posts": total_posts,
                        },
                    )
                    break
                try:
                    posts = await self._fetch_posts_batch(sec_user_id, current_cursor)
                    if not posts:
                        break

                    # Defensive break matching the ``iterated`` sentinel PR #37
                    # added for the livestream likes-only path: if the upstream
                    # page carries ``has_more=True`` but no posts, advancing
                    # ``current_cursor`` would loop forever because the server
                    # keeps echoing the same empty response. Treat an empty
                    # ``aweme_list`` as end-of-feed.
                    aweme_list = posts.get("aweme_list") or []
                    if not aweme_list:
                        logger.info(
                            "Upstream returned empty batch; ending pagination",
                            extra={
                                "cursor": current_cursor,
                                "has_more": posts.get("has_more"),
                            },
                        )
                        break

                    batch_failures = await self._process_posts_batch(
                        posts, download_stats, user_path
                    )
                    failed_count += batch_failures
                    total_downloaded = sum(download_stats.values())

                    progress: float | None
                    if total_posts > 0:
                        progress = min((total_downloaded / total_posts) * 100, 100.0)
                    else:
                        progress = None

                    update_fields: dict[str, Any] = {
                        "completed_items": total_downloaded,
                        "total_items": total_posts,
                        "message": "Bulk download in progress",
                        "metadata": {
                            "max_cursor": max_cursor,
                            "download_stats": _serialize_download_stats(download_stats),
                            "download_path": download_path,
                            "total_posts": total_posts,
                            "failed_count": failed_count,
                        },
                    }
                    if progress is not None:
                        update_fields["progress"] = progress
                    await self.operation_store.update_operation(
                        operation_id, **update_fields
                    )

                    # Handle pagination
                    has_more = posts.get("has_more", False)
                    next_cursor = posts.get("max_cursor", 0)

                    if not has_more or not next_cursor:
                        break

                    if next_cursor == current_cursor:
                        # The server replied with the same cursor it
                        # accepted; advancing to ``current_cursor + 1``
                        # used to spin through the same page repeatedly
                        # because the upstream API treats the synthetic
                        # cursor as out-of-range and returns the
                        # original window. Treat a sticky cursor as the
                        # end of the feed instead.
                        logger.info(
                            "Upstream cursor stuck; ending pagination",
                            extra={
                                "cursor": current_cursor,
                                "operation_id": operation_id,
                            },
                        )
                        break

                    current_cursor = next_cursor
                    logger.info("Moving to next page", extra={"cursor": current_cursor})

                except Exception as batch_error:
                    # A ``continue`` here without advancing ``current_cursor``
                    # would busy-loop on a persistent upstream failure. Break
                    # so the bulk response reflects whatever completed before
                    # the error instead of spinning indefinitely.
                    logger.error(
                        "Error processing batch; ending pagination",
                        extra={
                            "error": str(batch_error),
                            "cursor": current_cursor,
                            "operation_id": operation_id,
                        },
                    )
                    batch_errored = True
                    batch_error_message = str(batch_error)
                    break

        except UserNotFoundError as e:
            logger.warning(
                "User not found during bulk download",
                extra={"sec_user_id": sec_user_id, "operation_id": operation_id},
            )
            await self.operation_store.update_operation(
                operation_id,
                status="failed",
                message="Bulk download failed",
                error=str(e),
                metadata={
                    "max_cursor": max_cursor,
                    "download_stats": _serialize_download_stats(download_stats),
                    "download_path": download_path,
                    "total_posts": total_posts,
                    "failed_count": failed_count,
                },
            )
            return
        except Exception as e:
            logger.exception(
                "Error in bulk download process",
                extra={
                    "sec_user_id": sec_user_id,
                    "operation_id": operation_id,
                    "error": str(e),
                },
            )
            await self.operation_store.update_operation(
                operation_id,
                status="failed",
                message="Bulk download failed",
                error=str(e),
                metadata={
                    "max_cursor": max_cursor,
                    "download_stats": _serialize_download_stats(download_stats),
                    "download_path": download_path,
                    "total_posts": total_posts,
                    "failed_count": failed_count,
                },
            )
            return

        # Terminal classification. The batch-error branch must take precedence
        # over the count-based classifier: otherwise an upstream failure on
        # the very first batch of a zero-post user would fall through as
        # ``completed`` (because ``0 == 0``), and a partial run interrupted by
        # an error would surface as ``partial`` with an empty ``error`` field
        # — both of which lose the failure signal clients rely on.
        total_downloaded = sum(download_stats.values())
        terminal_error: str | None = None
        if batch_errored:
            terminal_error = batch_error_message
            if total_downloaded > 0:
                final_status = "partial"
                terminal_message = (
                    "Bulk download interrupted by upstream error: "
                    f"{total_downloaded}/{total_posts} posts"
                )
            else:
                final_status = "failed"
                terminal_message = (
                    "Bulk download failed before any posts were downloaded"
                )
        elif total_downloaded == total_posts:
            final_status = "completed"
            terminal_message = (
                f"Bulk download completed: {total_downloaded}/{total_posts} posts"
            )
        elif total_downloaded > 0:
            final_status = "partial"
            terminal_message = (
                "Bulk download completed with missing items: "
                f"{total_downloaded}/{total_posts} posts"
            )
        else:
            final_status = "failed"
            terminal_message = "Bulk download failed: no posts were downloaded"

        progress_value: float
        if total_posts > 0:
            progress_value = min((total_downloaded / total_posts) * 100, 100.0)
        else:
            progress_value = 100.0 if final_status == "completed" else 0.0

        await self.operation_store.update_operation(
            operation_id,
            status=final_status,
            message=terminal_message,
            progress=progress_value,
            completed_items=total_downloaded,
            total_items=total_posts,
            download_path=download_path,
            error=terminal_error,
            metadata={
                "max_cursor": max_cursor,
                "download_stats": _serialize_download_stats(download_stats),
                "download_path": download_path,
                "total_posts": total_posts,
                "failed_count": failed_count,
            },
        )

    async def get_bulk_download_status(self, operation_id: str) -> BulkDownloadResponse:
        """Get the current status of a bulk download operation.

        Args:
            operation_id: The unique identifier of the bulk download operation.

        Returns:
            BulkDownloadResponse: Snapshot of the operation including the
                per-PostType counts persisted in the operation metadata.

        Raises:
            OperationNotFoundError: If no bulk download operation matches the
                provided identifier.
        """
        # ``OperationStore.get_operation`` already raises
        # ``OperationNotFoundError`` with a descriptive message when the
        # row is missing; re-wrapping here would just discard the
        # original ``error_code`` and ``details``.
        op = await self.operation_store.get_operation(operation_id)

        if op.operation_type != "user_posts_bulk_download":
            raise OperationNotFoundError(
                f"Bulk download task {operation_id} not found"
            )

        download_stats = _deserialize_download_stats(op.metadata)
        total_posts = int(op.total_items or op.metadata.get("total_posts") or 0)
        total_downloaded = sum(download_stats.values())
        if op.completed_items is not None:
            total_downloaded = max(total_downloaded, int(op.completed_items))

        download_path = op.download_path or op.metadata.get("download_path")
        failed_count = int(op.metadata.get("failed_count") or 0)

        status = _operation_status_to_download_status(op.status)
        message = op.message or _build_bulk_message(
            total_downloaded, total_posts, download_stats, download_path
        )

        return BulkDownloadResponse(
            operation_id=op.operation_id,
            sec_user_id=op.subject_id,
            download_path=download_path,
            total_posts=total_posts,
            downloaded_count=download_stats,
            failed_count=failed_count,
            total_downloaded=total_downloaded,
            status=status,
            message=message,
            error_details=op.error,
        )

    async def _fetch_posts_batch(
        self,
        sec_user_id: str,
        cursor: int,
    ) -> dict[str, Any]:
        """Fetch a batch of posts from a user.

        Returns an empty dict only when the upstream feed is exhausted
        (``StopAsyncIteration``). Any other exception propagates so the
        caller's ``batch_errored`` accounting in ``_run_bulk_download``
        can record the failure and the operation lands in a clear
        ``partial`` / ``failed`` terminal state instead of a silent clean
        break.
        """
        logger.info(
            "Fetching posts batch", extra={"sec_user_id": sec_user_id, "cursor": cursor}
        )

        posts_iterator = self.handler.fetch_user_post_videos(
            sec_user_id=sec_user_id, max_cursor=cursor, page_counts=PAGE_SIZE
        )

        try:
            try:
                posts_filter = await posts_iterator.__anext__()
            except StopAsyncIteration:
                return {}
            return dict(posts_filter._to_dict())
        finally:
            aclose = getattr(posts_iterator, "aclose", None)
            if callable(aclose):
                await aclose()

    async def _process_posts_batch(
        self,
        posts: dict[str, Any],
        download_stats: dict[PostType, int],
        user_path: Path,
    ) -> int:
        """Process and download a batch of posts.

        Args:
            posts: Dictionary containing the batch of posts.
            download_stats: Dictionary for tracking the download statistics
                for each post type. Mutated in place.
            user_path: Path to the user's directory for saving downloaded
                content.

        Returns:
            Number of posts in this batch that failed to download. The
            caller adds this to a running ``failed_count`` so the
            terminal operation record exposes how many items were
            skipped.
        """
        post_list = posts.get("aweme_list", [])
        logger.info("Processing posts batch", extra={"post_count": len(post_list)})

        failed = 0
        for post in post_list:
            try:
                post_type = self._determine_post_type(post)
                await self._download_post_content(post, post_type, user_path)
                download_stats[post_type] += 1

            except Exception as e:
                failed += 1
                logger.error(
                    "Error processing post",
                    extra={"aweme_id": post.get("aweme_id"), "error": str(e)},
                )
        return failed

    def _determine_post_type(self, post: dict[str, Any]) -> PostType:
        """Determine the type of a Douyin post.

        Args:
            post (Dict[str, Any]): Dictionary containing the post data.

        Returns:
            PostType: Enum representing the type of the post (e.g., video,
                image, live, collection, story).
        """
        try:
            aweme_type = int(post.get("aweme_type", post.get("type", -1)))

            # Special post types
            if aweme_type == 1:
                return PostType.LIVE
            elif aweme_type == 3:
                return PostType.COLLECTION
            elif aweme_type == 4:
                return PostType.STORY

            # Check for images and videos
            has_images = bool(post.get("images"))
            has_video = bool(
                post.get("video_play_addr") or post.get("video", {}).get("play_addr")
            )

            if has_images and has_video:
                return PostType.MIXED
            elif has_images:
                return PostType.IMAGES
            elif has_video:
                return PostType.VIDEO

            return PostType.UNKNOWN

        except (ValueError, TypeError):
            return PostType.UNKNOWN

    async def _download_post_content(
        self,
        post: dict[str, Any],
        post_type: PostType,
        user_path: Path,
    ) -> None:
        """Download the content of a Douyin post.

        Args:
            post (Dict[str, Any]): Dictionary containing the post data.
            post_type (PostType): Type of the post.
            user_path (Path): Path to the user's directory for saving
                downloaded content.
        """
        logger.info(
            "Downloading post content",
            extra={"aweme_id": post.get("aweme_id"), "post_type": post_type},
        )

        try:
            await self.handler.downloader.create_download_tasks(
                self.handler.kwargs, [post], user_path
            )

        except Exception as e:
            logger.error(
                "Error downloading content",
                extra={
                    "aweme_id": post.get("aweme_id"),
                    "post_type": post_type,
                    "error": str(e),
                },
            )
            raise

    def _extract_image_urls(self, post: dict[str, Any]) -> list[str]:
        """Extract image URLs from a Douyin post.

        Args:
            post (Dict[str, Any]): Dictionary containing the post data.

        Returns:
            List[str]: List of image URLs extracted from the post.
        """
        image_urls = []
        images = post.get("images", [])
        if isinstance(images, list):
            for img in images:
                if isinstance(img, dict) and img.get("url_list"):
                    image_urls.extend(
                        [
                            url
                            for url in img["url_list"]
                            if url and url.startswith("http")
                        ]
                    )
        return image_urls

    def _extract_video_info(self, post: dict[str, Any]) -> VideoInfo | None:
        """Extract video information from a Douyin post.

        Args:
            post: Raw post dict from the Douyin API containing a ``video`` key.

        Returns:
            A ``VideoInfo`` with play URL and dimensions, or ``None`` if the
            post has no playable video address.
        """
        video = post.get("video", {})
        play_addr = video.get("play_addr", {})
        if play_addr and play_addr.get("url_list"):
            return VideoInfo(
                play_addr=play_addr["url_list"][0],
                duration=video.get("duration", 0),
                ratio=video.get("ratio", ""),
                width=play_addr.get("width", 0),
                height=play_addr.get("height", 0),
            )
        return None

    def _extract_image_info(self, post: dict[str, Any]) -> list[ImageInfo] | None:
        """Extract image information from a Douyin post.

        Args:
            post: Raw post dict from the Douyin API containing an ``images`` key.

        Returns:
            A list of ``ImageInfo`` objects, or ``None`` if the post has no images.
        """
        images = post.get("images", [])
        if not images:
            return None

        image_info = []
        for img in images:
            if isinstance(img, dict) and img.get("url_list"):
                image_info.append(
                    ImageInfo(
                        url=img["url_list"][0],
                        width=img.get("width", 0),
                        height=img.get("height", 0),
                    )
                )
        return image_info if image_info else None


# ----------------------------------------------------------------------
# Module-level helpers shared between ``_run_bulk_download`` and
# ``get_bulk_download_status``. Keeping them at module scope makes the
# serialization shape easy to unit test and avoids leaking sqlite-aware
# logic into the response model.
# ----------------------------------------------------------------------


def _serialize_download_stats(stats: dict[PostType, int]) -> dict[str, int]:
    """Convert the per-PostType counter into JSON-friendly metadata."""
    return {post_type.value: int(count) for post_type, count in stats.items()}


def _deserialize_download_stats(
    metadata: dict[str, Any],
) -> dict[PostType, int]:
    """Rebuild the per-PostType counter from operation metadata.

    Missing or partial dictionaries default to zero counts so callers can
    rely on every ``PostType`` member being present in the result.
    """
    raw = metadata.get("download_stats") or {}
    counts: dict[PostType, int] = dict.fromkeys(PostType, 0)
    if not isinstance(raw, dict):
        return counts
    for key, value in raw.items():
        try:
            post_type = PostType(key)
        except ValueError:
            # Unknown values are skipped silently so the response stays
            # well-formed even after a future ``PostType`` change.
            continue
        try:
            counts[post_type] = int(value)
        except (TypeError, ValueError):
            counts[post_type] = 0
    return counts


def _operation_status_to_download_status(status: str) -> DownloadStatus:
    """Translate persisted operation status to public download status.

    ``DownloadStatus`` is an alias for ``OperationStatus`` so the
    persisted strings round-trip directly. Legacy values from before the
    consolidation (``in_progress`` / ``success`` / ``partial_success``)
    are remapped to their canonical equivalents so old operation records
    keep deserialising cleanly.
    """
    legacy = {
        "in_progress": DownloadStatus.RUNNING,
        "success": DownloadStatus.COMPLETED,
        "partial_success": DownloadStatus.PARTIAL,
    }
    canonical = legacy.get(status, status)
    try:
        return DownloadStatus(canonical)
    except ValueError:
        return DownloadStatus.FAILED


def _build_bulk_message(
    total_downloaded: int,
    total_posts: int,
    download_stats: dict[PostType, int],
    download_path: str | None,
) -> str:
    """Render a human-readable summary for the bulk download response."""
    location = download_path or "(pending)"
    return (
        f"Downloaded {total_downloaded} out of {total_posts} posts. "
        f"(Videos: {download_stats[PostType.VIDEO]}, "
        f"Images: {download_stats[PostType.IMAGES]}, "
        f"Mixed: {download_stats[PostType.MIXED]}, "
        f"Lives: {download_stats[PostType.LIVE]}, "
        f"Collections: {download_stats[PostType.COLLECTION]}, "
        f"Stories: {download_stats[PostType.STORY]}) "
        f"Files saved to {location}"
    )
