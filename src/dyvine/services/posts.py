"""Post domain service.

`PostService` encapsulates the post domain surface used by tools:

- ``get_post_detail(aweme_id)`` — return a typed ``PostDetail`` for a
  single Douyin post.
- ``get_user_posts(sec_user_id, max_cursor, count)`` — paginated post
  listing wrapped in ``UserPostsPage``. Callers must echo the upstream
  cursor verbatim because Douyin treats it as a sentinel rather than
  an offset.
- ``start_bulk_download(sec_user_id, max_cursor)`` — validate the
  profile, persist a ``user_posts_bulk_download`` operation row, and
  schedule the long-running pagination + download loop on
  ``BackgroundTaskRegistry``. Returns immediately with the
  ``operation_id``.
- ``get_bulk_download_status(operation_id)`` — return a
  ``BulkDownloadResponse`` snapshot, including per-``PostType``
  counters persisted in the operation metadata so polling clients see
  consistent totals while work is still running.

The bulk loop (`_run_bulk_download`) bounds itself with the
``core.pagination`` constants and exits via dedicated terminals
(sticky cursor, empty batch, or batch error) so a misbehaving upstream
cursor cannot pin a worker. Terminal classification favours the
batch-error branch over the count-based classifier: an upstream
failure on the first batch is recorded as ``failed`` (or ``partial``
when downloads already succeeded) rather than silently passing as
``completed``.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.background import BackgroundTaskRegistry, spawn_or_fallback
from ..core.exceptions import (
    OperationNotFoundError,
    PostNotFoundError,
    ServiceError,
    UserNotFoundError,
)
from ..core.logging import ContextLogger
from ..core.pagination import MAX_PAGES_FALLBACK, PAGE_MULTIPLIER, PAGE_SLACK
from ..core.path_safety import relative_to_download_root
from ..db import OperationRepository
from ..schemas.posts import (
    BulkDownloadResponse,
    DownloadStatus,
    ImageInfo,
    PostDetail,
    PostType,
    VideoInfo,
)

if TYPE_CHECKING:
    from f2.apps.douyin.crawler import DouyinCrawler  # type: ignore
    from f2.apps.douyin.db import AsyncUserDB  # type: ignore
    from f2.apps.douyin.handler import DouyinHandler  # type: ignore
    from f2.apps.douyin.model import PostDetail as F2PostDetail  # type: ignore
else:
    # Deferred: importing f2 performs real HTTPS requests (see
    # ``core._lazy_f2``), so the SDK loads on first real use only.
    from ..core._lazy_f2 import LazyF2Symbol

    AsyncUserDB = LazyF2Symbol("f2.apps.douyin.db", "AsyncUserDB")
    DouyinCrawler = LazyF2Symbol("f2.apps.douyin.crawler", "DouyinCrawler")
    DouyinHandler = LazyF2Symbol("f2.apps.douyin.handler", "DouyinHandler")
    F2PostDetail = LazyF2Symbol("f2.apps.douyin.model", "PostDetail")

logger = ContextLogger(__name__)

# Alias for backward compatibility
PostServiceError = ServiceError

# Page size requested from ``_fetch_posts_batch``. The outer loop guard
# combines this with the shared :mod:`dyvine.core.pagination` constants
# so a sticky upstream cursor cannot keep the ``+1`` cursor advance
# spinning forever. The fallback covers ``MAX_PAGES_FALLBACK * PAGE_SIZE``
# items when ``total_posts`` is unknown.
PAGE_SIZE = 20

# f2 renders post times as UTC+8 wall-clock strings (``timestamp_2_str``),
# which is also the form they take in media folder names.
_F2_NAMING_ZONE = timezone(timedelta(hours=8))

#: Bulk-downloadable per-user feeds, mapped to the f2 handler iterator
#: each one paginates. ``post``/``like`` iterators take a target
#: ``sec_user_id``; ``collection``/``music`` are scoped to the login
#: cookie owner (Douyin exposes nobody else's) and take only cursor
#: arguments, with ``sec_user_id`` anchoring the output directory just
#: like the f2 CLI does. All four yield pages with an ``aweme_list``,
#: so one loop serves every mode.
BULK_FETCHERS: dict[str, str] = {
    "post": "fetch_user_post_videos",
    "like": "fetch_user_like_videos",
    "collection": "fetch_user_collection_videos",
    "music": "fetch_user_music_collection",
}

#: Modes whose f2 iterator is scoped to the cookie owner and takes no
#: ``sec_user_id`` argument.
OWNER_SCOPED_MODES = frozenset({"collection", "music"})

#: Operation types the status snapshot accepts. The ``post`` mode keeps
#: its historic ``user_posts_bulk_download`` type; every other mode uses
#: ``user_{mode}_bulk_download``.
BULK_OPERATION_TYPES = frozenset(
    {
        "user_posts_bulk_download",
        "user_like_bulk_download",
        "user_collection_bulk_download",
        "user_music_bulk_download",
        "user_mix_bulk_download",
        "user_collects_bulk_download",
        "single_post_download",
        "user_posts_incremental_download",
    }
)


@dataclass(slots=True)
class UserPostsPage:
    """Single-page result from :meth:`PostService.get_user_posts`.

    Carries both the materialised ``PostDetail`` items and the raw
    upstream cursor needed to fetch the next page. Callers receive
    the integer Douyin cursor verbatim. ``next_cursor`` is ``None`` when the feed
    is exhausted (``has_more=False`` upstream).
    """

    posts: list[PostDetail]
    next_cursor: int | None
    has_more: bool


@dataclass(slots=True)
class SinglePostDownloadResult:
    """Outcome of an inline single-post download.

    Unlike the bulk loop this coroutine finishes the work before
    returning, so the result carries the downloaded file paths
    directly alongside the auditable operation row id.
    """

    operation_id: str
    aweme_id: str
    post_type: PostType
    download_path: str
    files: list[str]


@dataclass(slots=True)
class IncrementalDownloadResult:
    """Outcome of an incremental new-post download for a watch cycle.

    ``seen_aweme_ids`` lists the posts downloaded this run, newest first,
    so a watch loop can fold them into its dedupe checkpoint.
    ``newest_aweme_id`` is the first of that list (or ``None`` when nothing
    new was found, in which case the caller keeps its existing sentinel).
    """

    operation_id: str
    new_count: int
    newest_aweme_id: str | None
    seen_aweme_ids: list[str]
    failed_count: int
    # True when pagination stopped at the MAX_PAGES_FALLBACK cap with more
    # pages still available. The caller must NOT advance its checkpoint on a
    # truncated run or it would skip every post past the cap permanently.
    truncated: bool = False


class PostService:
    """Domain logic for individual posts and bulk-download operations.

    Public surface:
        - ``get_post_detail(aweme_id)`` — typed `PostDetail`.
        - ``get_user_posts(sec_user_id, max_cursor, count)`` — single-page
          listing wrapped in `UserPostsPage` with the upstream cursor
          to echo back unchanged.
        - ``start_bulk_download(sec_user_id, max_cursor)`` — validate
          the profile, persist a ``user_posts_bulk_download`` operation
          row, and dispatch the long-running pagination + download
          loop on `BackgroundTaskRegistry`.
        - ``get_bulk_download_status(operation_id)`` — `BulkDownloadResponse`
          snapshot, including per-`PostType` counters persisted in the
          operation metadata.

    Per-post download is delegated to the f2 SDK via
    ``handler.downloader.create_download_tasks``. The bulk loop bounds
    itself with `core.pagination` constants and exits via dedicated
    terminals (sticky cursor, empty batch, batch error) so a misbehaving
    upstream cursor cannot pin a worker.
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
        operation_store: OperationRepository,
        task_registry: BackgroundTaskRegistry | None = None,
    ) -> None:
        """Initialize the PostService instance.

        Args:
            handler: Configured DouyinHandler instance for Douyin operations.
            operation_store: Persistent operation record repository.
                Required; the container injects the Postgres-backed
                implementation and unit tests inject a fake.
            task_registry: Optional registry that owns long-lived bulk
                download tasks. When omitted (e.g. in tests) the service
                falls back to ``asyncio.create_task`` so the public API
                remains testable without a full service container.
        """
        self.handler = handler
        self.operation_store = operation_store
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

            # Parse create_time string into a Unix timestamp (integer).
            # The upstream format carries no zone; interpret it as UTC so
            # the result does not depend on the server's local timezone.
            create_time_str = post_data.get("create_time")
            create_time = 0
            if create_time_str:
                try:
                    create_time_dt = datetime.strptime(
                        create_time_str, "%Y-%m-%d %H-%M-%S"
                    ).replace(tzinfo=UTC)
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

    async def download_single_post(self, aweme_id: str) -> SinglePostDownloadResult:
        """Download one post (video or album) inline and return its files.

        Unlike :meth:`start_bulk_download` this coroutine finishes the
        work before returning: a single post is small enough to await
        directly, which keeps the plugin tool synchronous. An operation
        row of type ``single_post_download`` records the outcome for
        auditing either way.

        Args:
            aweme_id: Unique identifier of the post.

        Returns:
            The downloaded file paths plus the operation id.

        Raises:
            PostNotFoundError: If the post does not exist.
            PostServiceError: If the author cannot be resolved or the
                download itself fails.
        """
        operation = await self.operation_store.create_operation(
            operation_type="single_post_download",
            subject_id=aweme_id,
            status="running",
            message="Single post download in progress",
            progress=0.0,
        )
        try:
            fetched = await self.handler.fetch_one_video(aweme_id)
            if not fetched:
                raise PostNotFoundError(f"Post not found: {aweme_id}")
            post_data = _mapping_from(fetched, "_to_dict") or {}
            prepared = _prepare_post_for_downloader(post_data)
            sec_user_id = prepared.get("sec_user_id")
            if not sec_user_id or not isinstance(sec_user_id, str):
                raise PostServiceError(f"Cannot resolve author of post: {aweme_id}")

            async with AsyncUserDB("douyin_users.db") as db:
                user_path = await self.handler.get_or_add_user_data(
                    self.handler.kwargs, sec_user_id, db
                )
            post_type = self._determine_post_type(prepared)
            before = {entry.name for entry in user_path.iterdir()}
            await self._download_post_content(prepared, post_type, user_path)
            files = sorted(
                str(user_path / name)
                for name in {entry.name for entry in user_path.iterdir()} - before
            )
            # ``user_path`` is never ``None`` here, so the helper always
            # returns a string; the fallback only satisfies the type
            # checker while keeping the basename-only privacy posture.
            download_path = relative_to_download_root(user_path) or user_path.name
            await self.operation_store.update_operation(
                operation.operation_id,
                status="completed",
                message="Single post download completed",
                progress=100.0,
                completed_items=1,
                total_items=1,
                download_path=download_path,
                metadata={"aweme_id": aweme_id, "files": files},
            )
            return SinglePostDownloadResult(
                operation_id=operation.operation_id,
                aweme_id=aweme_id,
                post_type=post_type,
                download_path=download_path,
                files=files,
            )
        except (PostNotFoundError, PostServiceError) as e:
            await self.operation_store.update_operation(
                operation.operation_id,
                status="failed",
                message="Single post download failed",
                error=str(e),
            )
            raise
        except Exception as e:
            logger.exception(
                "Error downloading single post",
                extra={"aweme_id": aweme_id, "error": str(e)},
            )
            await self.operation_store.update_operation(
                operation.operation_id,
                status="failed",
                message="Single post download failed",
                error=str(e),
            )
            raise PostServiceError(f"Failed to download post: {str(e)}") from e

    async def list_collects(self) -> list[dict[str, Any]]:
        """List the login cookie owner's collects folders.

        Douyin only exposes the cookie owner's own folders, so the
        upstream iterator takes no user argument, exactly like
        ``collection`` bulk mode.
        """
        folders: list[dict[str, Any]] = []
        iterator = self.handler.fetch_user_collects()
        try:
            async for collects in iterator:
                data = _mapping_from(collects, "_to_dict")
                if data:
                    folders.append(data)
        finally:
            aclose = getattr(iterator, "aclose", None)
            if callable(aclose):
                await aclose()
        return folders

    async def get_post_stats(
        self, aweme_id: str, aweme_type: int = 0
    ) -> dict[str, Any]:
        """Fetch upstream statistics for one post.

        Args:
            aweme_id: Unique identifier of the post.
            aweme_type: Post kind code (``0`` video, ``68`` album, ...).
                Defaults to video; pass the value from
                :meth:`get_post_detail` when known.

        Raises:
            PostServiceError: If the upstream fetch fails.
        """
        try:
            stats = await self.handler.fetch_post_stats(
                aweme_id=aweme_id, aweme_type=aweme_type
            )
            return _mapping_from(stats, "_to_dict") or {}
        except Exception as e:
            logger.exception(
                "Error fetching post stats",
                extra={"aweme_id": aweme_id, "error": str(e)},
            )
            raise PostServiceError(f"Failed to fetch post stats: {str(e)}") from e

    async def get_user_feed(
        self, sec_user_id: str, count: int = 20
    ) -> list[dict[str, Any]]:
        """List a user's feed videos (first ``count`` items).

        Raises:
            PostServiceError: If the upstream fetch fails.
        """
        return await self._collect_feed_items(
            "fetch_user_feed_videos",
            {
                "sec_user_id": sec_user_id,
                "max_cursor": 0,
                "page_counts": PAGE_SIZE,
            },
            count,
            extra={"sec_user_id": sec_user_id},
        )

    async def get_related_posts(
        self, aweme_id: str, count: int = 20
    ) -> list[dict[str, Any]]:
        """List posts related to ``aweme_id`` (first ``count`` items).

        Raises:
            PostServiceError: If the upstream fetch fails.
        """
        return await self._collect_feed_items(
            "fetch_related_videos",
            {"aweme_id": aweme_id, "page_counts": PAGE_SIZE},
            count,
            extra={"aweme_id": aweme_id},
        )

    async def get_friend_feed(self, count: int = 20) -> list[dict[str, Any]]:
        """List friend-feed videos for the login cookie owner.

        The iterator is owner-scoped and takes no user argument.

        Raises:
            PostServiceError: If the upstream fetch fails.
        """
        return await self._collect_feed_items(
            "fetch_friend_feed_videos",
            {"cursor": 0, "max_counts": count},
            count,
            extra={},
        )

    async def _collect_feed_items(
        self,
        fetcher_name: str,
        fetcher_kwargs: dict[str, Any],
        count: int,
        *,
        extra: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Page one feed iterator up to ``count`` normalized items."""
        items: list[dict[str, Any]] = []
        try:
            iterator = getattr(self.handler, fetcher_name)(**fetcher_kwargs)
            try:
                async for page in iterator:
                    for item in _list_from(page, "_to_list") or []:
                        items.append(item)
                        if len(items) >= count:
                            return items[:count]
            finally:
                aclose = getattr(iterator, "aclose", None)
                if callable(aclose):
                    await aclose()
        except Exception as e:
            logger.exception(
                "Error fetching feed",
                extra={**extra, "fetcher": fetcher_name, "error": str(e)},
            )
            raise PostServiceError(f"Failed to fetch feed: {str(e)}") from e
        return items

    async def get_post_comments(
        self, aweme_id: str, count: int = 20
    ) -> list[dict[str, Any]]:
        """List top-level comments of one post (first ``count`` items).

        f2 exposes comments at crawler level only (no handler
        wrapper), so this drives ``DouyinCrawler`` directly with the
        service handler's kwargs and normalizes the raw ``comments``
        array. Replies stay out of scope: the endpoint returns one
        level only.

        Raises:
            PostServiceError: If the upstream fetch fails.
        """
        try:
            kwargs = dict(getattr(self.handler, "kwargs", {}) or {})
            async with DouyinCrawler(kwargs) as crawler:
                response = await crawler.fetch_post_comment(
                    F2PostDetail(aweme_id=aweme_id)
                )
        except Exception as e:
            logger.exception(
                "Error fetching post comments",
                extra={"aweme_id": aweme_id, "error": str(e)},
            )
            raise PostServiceError(f"Failed to fetch post comments: {str(e)}") from e
        comments = (response or {}).get("comments") or []
        return [dict(item) for item in comments if isinstance(item, dict)][
            : max(count, 0)
        ]

    async def start_mix_download(self, mix_id: str) -> BulkDownloadResponse:
        """Schedule an asynchronous download of every post in a mix album.

        Args:
            mix_id: The mix (collection album) identifier.

        Returns:
            Pending response carrying the ``operation_id`` to poll.
        """
        return await self._start_container_download(
            container_id=mix_id,
            operation_type="user_mix_bulk_download",
            fetcher_name="fetch_user_mix_videos",
            task_name="posts-mix",
        )

    async def start_collects_download(self, collects_id: str) -> BulkDownloadResponse:
        """Schedule an asynchronous download of a collects folder.

        Args:
            collects_id: The collects folder identifier.

        Returns:
            Pending response carrying the ``operation_id`` to poll.
        """
        return await self._start_container_download(
            container_id=collects_id,
            operation_type="user_collects_bulk_download",
            fetcher_name="fetch_user_collects_videos",
            task_name="posts-collects",
        )

    async def _start_container_download(
        self,
        *,
        container_id: str,
        operation_type: str,
        fetcher_name: str,
        task_name: str,
    ) -> BulkDownloadResponse:
        """Persist the operation row and spawn a container download loop."""
        if not container_id or not container_id.strip():
            raise PostServiceError("Container id is required")
        operation = await self.operation_store.create_operation(
            operation_type=operation_type,
            subject_id=container_id,
            status="pending",
            message="Container download scheduled",
            progress=0.0,
        )
        coro = self._run_container_download(
            operation.operation_id, container_id, fetcher_name
        )
        spawn_or_fallback(
            self._task_registry, coro, name=f"{task_name}-{operation.operation_id}"
        )
        return BulkDownloadResponse(
            operation_id=operation.operation_id,
            sec_user_id=container_id,
            download_path=None,
            total_posts=0,
            downloaded_count=dict.fromkeys(PostType, 0),
            total_downloaded=0,
            status=DownloadStatus.PENDING,
            message="Container download scheduled",
            error_details=None,
        )

    async def _run_container_download(
        self, operation_id: str, container_id: str, fetcher_name: str
    ) -> None:
        """Paginate one mix/collects container and download every post.

        The total is unknown up front (no profile equivalent), so
        progress stays ``None`` and the terminal state is
        ``completed`` unless a batch fails mid-run. The author directory
        anchors on the first post's ``sec_user_id``, mirroring the f2
        CLI, which may arrive as a bare string or a one-item list.
        """
        download_stats: dict[PostType, int] = dict.fromkeys(PostType, 0)
        failed_count = 0
        user_path: Path | None = None
        try:
            await self.operation_store.update_operation(
                operation_id,
                status="running",
                message="Container download in progress",
                error=None,
            )
            current_cursor = 0
            page_count = 0
            fetcher = getattr(self.handler, fetcher_name)
            while True:
                page_count += 1
                if page_count > MAX_PAGES_FALLBACK:
                    logger.warning(
                        "Container download exceeded page cap; stopping",
                        extra={
                            "container_id": container_id,
                            "operation_id": operation_id,
                        },
                    )
                    break
                iterator = fetcher(
                    container_id, max_cursor=current_cursor, page_counts=PAGE_SIZE
                )
                try:
                    try:
                        page = await iterator.__anext__()
                    except StopAsyncIteration:
                        break
                    items = _list_from(page, "_to_list") or []
                    if not items:
                        break
                    if user_path is None:
                        sec = items[0].get("sec_user_id")
                        if isinstance(sec, list):
                            sec = sec[0] if sec else None
                        if not sec or not isinstance(sec, str):
                            raise PostServiceError(
                                "Cannot resolve author of container " f"{container_id}"
                            )
                        async with AsyncUserDB("douyin_users.db") as db:
                            user_path = await self.handler.get_or_add_user_data(
                                self.handler.kwargs, sec, db
                            )
                    failed_count += await self._process_posts_batch(
                        {"aweme_list": items}, download_stats, user_path
                    )
                    await self.operation_store.update_operation(
                        operation_id,
                        completed_items=sum(download_stats.values()),
                        message="Container download in progress",
                        metadata={
                            "download_stats": _serialize_download_stats(download_stats),
                            "failed_count": failed_count,
                        },
                    )
                    raw = _mapping_from(page, "_to_raw") or {}
                    has_more = raw.get("has_more", False)
                    next_cursor = raw.get("max_cursor", 0)
                    if not has_more or not next_cursor or next_cursor == current_cursor:
                        break
                    current_cursor = next_cursor
                finally:
                    aclose = getattr(iterator, "aclose", None)
                    if callable(aclose):
                        await aclose()
            total_downloaded = sum(download_stats.values())
            await self.operation_store.update_operation(
                operation_id,
                status="completed",
                message=(
                    f"Container download completed: "
                    f"{total_downloaded} posts, {failed_count} failed"
                ),
                completed_items=total_downloaded,
                download_path=(
                    relative_to_download_root(user_path) if user_path else None
                ),
                metadata={
                    "download_stats": _serialize_download_stats(download_stats),
                    "failed_count": failed_count,
                },
            )
        except Exception as e:
            logger.exception(
                "Error in container download",
                extra={
                    "container_id": container_id,
                    "operation_id": operation_id,
                    "error": str(e),
                },
            )
            total_downloaded = sum(download_stats.values())
            await self.operation_store.update_operation(
                operation_id,
                status="partial" if total_downloaded else "failed",
                message="Container download failed",
                error=str(e),
                metadata={
                    "download_stats": _serialize_download_stats(download_stats),
                    "failed_count": failed_count,
                },
            )

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
                # callers are not invited to re-fetch the
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
        mode: str = "post",
    ) -> BulkDownloadResponse:
        """Schedule an asynchronous bulk download of a user's feed.

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
            mode: Which per-user feed to download (``post``, ``like``,
                ``collection`` or ``music``). ``post`` keeps the historic
                ``user_posts_bulk_download`` operation type; other modes
                use ``user_{mode}_bulk_download``. ``collection`` and
                ``music`` always fetch the login cookie owner's items
                (Douyin exposes nobody else's); ``sec_user_id`` only
                anchors the output directory for those two modes.

        Returns:
            BulkDownloadResponse: Pending response carrying the
                ``operation_id`` clients can use to poll for progress.

        Raises:
            UserNotFoundError: If the requested user cannot be found.
            PostServiceError: If the profile lookup itself fails, or the
                requested mode is unknown.
        """
        if mode not in BULK_FETCHERS:
            raise PostServiceError(
                f"Unknown bulk download mode: {mode} "
                f"(expected one of {sorted(BULK_FETCHERS)})"
            )
        try:
            logger.info(
                "Validating user before scheduling bulk download",
                extra={
                    "sec_user_id": sec_user_id,
                    "max_cursor": max_cursor,
                    "mode": mode,
                },
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

        operation_type = (
            "user_posts_bulk_download"
            if mode == "post"
            else f"user_{mode}_bulk_download"
        )
        operation = await self.operation_store.create_operation(
            operation_type=operation_type,
            subject_id=sec_user_id,
            status="pending",
            message="Bulk download scheduled",
            progress=0.0,
            metadata={"max_cursor": max_cursor, "mode": mode},
        )

        # Forward the already-fetched profile to the background coroutine so
        # the bulk loop does not have to repeat the upstream call. A second
        # ``fetch_user_profile`` here would double the network cost and open
        # a small failure window where the existence check passed but the
        # bulk loop sees a transient error.
        coro = self._run_bulk_download(
            operation.operation_id, sec_user_id, max_cursor, mode=mode, profile=profile
        )
        # Route through the shared registry so host shutdown can
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

    async def download_bulk_inline(
        self,
        sec_user_id: str,
        *,
        operation_id: str,
        max_cursor: int = 0,
        mode: str = "post",
    ) -> BulkDownloadResponse:
        """Await a full bulk download against a caller-persisted operation.

        The weekly runner persists ``operation_id`` on its queue row before
        calling this method, so a process exit never loses the operation
        reference. No background task is created. ``max_cursor`` may be a
        cursor recorded after a fully processed earlier page.
        """
        if mode not in BULK_FETCHERS:
            raise PostServiceError(f"Unknown bulk download mode: {mode}")
        operation = await self.operation_store.get_operation(operation_id)
        expected_type = (
            "user_posts_bulk_download"
            if mode == "post"
            else f"user_{mode}_bulk_download"
        )
        if (
            operation.operation_type != expected_type
            or operation.subject_id != sec_user_id
            or operation.status != "pending"
        ):
            raise PostServiceError(
                "Bulk operation does not match the requested download"
            )
        try:
            profile = await self.handler.fetch_user_profile(sec_user_id)
            if not profile or not getattr(profile, "nickname", None):
                raise UserNotFoundError(f"User not found: {sec_user_id}")
            await self._run_bulk_download(
                operation_id, sec_user_id, max_cursor, mode=mode, profile=profile
            )
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await self.operation_store.update_operation(
                    operation_id,
                    status="failed",
                    message="Inline bulk download cancelled",
                    error="cancelled",
                )
            raise
        except Exception:
            await self.operation_store.update_operation(
                operation_id,
                status="failed",
                message="Inline bulk download failed",
                error="download failed",
            )
            raise
        return await self.get_bulk_download_status(operation_id)

    async def _run_bulk_download(
        self,
        operation_id: str,
        sec_user_id: str,
        max_cursor: int,
        *,
        profile: Any,
        mode: str = "post",
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
            mode: Which feed iterator to paginate (see ``BULK_FETCHERS``).
        """
        download_stats: dict[PostType, int] = dict.fromkeys(PostType, 0)
        download_path: str | None = None
        batch_errored = False
        batch_error_message: str | None = None
        failed_count = 0
        resume_cursor: int | None = max_cursor
        cursor_stalled = False

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
                    posts = await self._fetch_posts_batch(
                        sec_user_id, current_cursor, mode
                    )
                    if not posts:
                        resume_cursor = None
                        cursor_stalled = total_posts > sum(download_stats.values())
                        break

                    # Defensive break matching the ``iterated`` sentinel PR #37
                    # added for the livestream likes-only path: if the upstream
                    # page carries ``has_more=True`` but no posts, advancing
                    # ``current_cursor`` would loop forever because the server
                    # keeps echoing the same empty response. Treat an empty
                    # ``aweme_list`` as end-of-feed.
                    aweme_list = posts.get("aweme_list") or []
                    if not aweme_list:
                        resume_cursor = None
                        cursor_stalled = bool(posts.get("has_more"))
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
                    has_more = posts.get("has_more", False)
                    next_cursor = posts.get("max_cursor", 0)
                    cursor_stalled = bool(
                        has_more and (not next_cursor or next_cursor == current_cursor)
                    )
                    resume_cursor = (
                        next_cursor
                        if has_more and next_cursor and next_cursor != current_cursor
                        else None
                    )

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
                            "resume_cursor": resume_cursor,
                            "cursor_stalled": cursor_stalled,
                        },
                    }
                    if progress is not None:
                        update_fields["progress"] = progress
                    await self.operation_store.update_operation(
                        operation_id, **update_fields
                    )

                    # Handle pagination
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
                    "resume_cursor": resume_cursor,
                    "cursor_stalled": cursor_stalled,
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
                    "resume_cursor": resume_cursor,
                    "cursor_stalled": cursor_stalled,
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
                "resume_cursor": resume_cursor,
                "cursor_stalled": cursor_stalled,
            },
        )

    async def get_bulk_download_status(self, operation_id: str) -> BulkDownloadResponse:
        """Get the current status of a bulk or incremental download operation.

        Both the user-triggered bulk loop and the watch-mode incremental loop
        persist their progress as operation rows, so this endpoint serves
        either type and watch-created downloads remain observable through the
        existing polling route.

        Args:
            operation_id: The unique identifier of the download operation.

        Returns:
            BulkDownloadResponse: Snapshot of the operation including the
                per-PostType counts persisted in the operation metadata.

        Raises:
            OperationNotFoundError: If no bulk or incremental download
                operation matches the provided identifier.
        """
        # ``OperationRepository.get_operation`` already raises
        # ``OperationNotFoundError`` with a descriptive message when the
        # row is missing; re-wrapping here would just discard the
        # original ``error_code`` and ``details``.
        op = await self.operation_store.get_operation(operation_id)

        if op.operation_type not in BULK_OPERATION_TYPES:
            raise OperationNotFoundError(f"Download task {operation_id} not found")

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

    async def download_new_posts(
        self,
        sec_user_id: str,
        *,
        since_aweme_id: str | None = None,
        known_aweme_ids: set[str] | None = None,
        subscription_id: str | None = None,
        operation_id: str | None = None,
        posted_after: datetime | None = None,
    ) -> IncrementalDownloadResult:
        """Download only the posts newer than the caller's checkpoint.

        Unlike :meth:`start_bulk_download` (fire-and-forget), this coroutine
        runs the incremental pagination + download inline and returns once
        finished, so a watch loop can persist an updated checkpoint from the
        result. Pagination walks newest-first and stops at the first post
        whose ``aweme_id`` is already known, so a steady-state run exits
        after one or two pages.

        An operation row of type ``user_posts_incremental_download`` is
        created and advanced to a terminal state so the work is auditable
        through the operation store. On failure the operation is marked
        ``failed`` and the error is re-raised, so the caller does not
        advance its checkpoint past posts that were never fetched.

        Args:
            sec_user_id: Unique identifier of the user.
            since_aweme_id: Newest aweme_id downloaded on a previous run;
                pagination stops when it reappears upstream.
            known_aweme_ids: Recently-downloaded aweme_ids used as the
                authoritative dedupe set (membership, not ordering).
            subscription_id: Optional watch subscription id stamped into the
                operation metadata for correlation.
            operation_id: Optional pending operation already persisted by a
                caller that also records the ID on its own queue row.
            posted_after: Optional naive post time in f2's naming zone
                (UTC+8). Posts at or before it are not downloaded, and
                pagination stops after a page with no newer post, so a
                run without ``since_aweme_id`` does not walk the whole
                feed history.

        Returns:
            IncrementalDownloadResult: counts plus the newly downloaded
                aweme_ids (newest first) for checkpoint maintenance.

        Raises:
            UserNotFoundError: If the user cannot be found.
            PostServiceError: If the profile lookup or download loop fails.
        """
        known = set(known_aweme_ids or set())

        operation = None
        if operation_id is not None:
            operation = await self.operation_store.get_operation(operation_id)
            if (
                operation.operation_type != "user_posts_incremental_download"
                or operation.subject_id != sec_user_id
                or operation.status != "pending"
            ):
                raise PostServiceError(
                    "Incremental operation does not match the requested download"
                )

        try:
            profile = await self.handler.fetch_user_profile(sec_user_id)
        except UserNotFoundError:
            if operation is not None:
                await self.operation_store.update_operation(
                    operation.operation_id,
                    status="failed",
                    message="Incremental download profile unavailable",
                    error="user not found",
                )
            raise
        except Exception as e:
            if operation is not None:
                await self.operation_store.update_operation(
                    operation.operation_id,
                    status="failed",
                    message="Incremental download profile lookup failed",
                    error="profile lookup failed",
                )
            raise PostServiceError(f"Failed to validate user profile: {str(e)}") from e
        if not profile or not getattr(profile, "nickname", None):
            if operation is not None:
                await self.operation_store.update_operation(
                    operation.operation_id,
                    status="failed",
                    message="Incremental download profile unavailable",
                    error="user not found",
                )
            raise UserNotFoundError(f"User not found: {sec_user_id}")

        if operation is None:
            operation = await self.operation_store.create_operation(
                operation_type="user_posts_incremental_download",
                subject_id=sec_user_id,
                status="running",
                message="Incremental download in progress",
                progress=0.0,
                metadata=(
                    {"subscription_id": subscription_id} if subscription_id else {}
                ),
            )
        else:
            operation = await self.operation_store.update_operation(
                operation.operation_id,
                status="running",
                message="Incremental download in progress",
                progress=0.0,
            )
        operation_id = operation.operation_id

        try:
            # Resolve (and create) the per-user download directory the same
            # way the bulk loop does so incremental files land alongside any
            # prior backfill instead of in a second location.
            async with AsyncUserDB("douyin_users.db") as db:
                user_path = await self.handler.get_or_add_user_data(
                    self.handler.kwargs, sec_user_id, db
                )
            download_path = relative_to_download_root(user_path)

            new_aweme_ids, failed_count, truncated = await self._collect_new_posts(
                sec_user_id,
                user_path,
                known=known,
                since_aweme_id=since_aweme_id,
                posted_after=posted_after,
            )
        except asyncio.CancelledError:
            # The watch loop was cancelled (DELETE or shutdown) mid-download.
            # Mark the row terminal so it does not linger as "running" until
            # the next boot sweep, then propagate the cancellation. The write
            # is best-effort: a second cancellation must not mask the re-raise.
            with contextlib.suppress(Exception):
                await self.operation_store.update_operation(
                    operation_id,
                    status="failed",
                    message="Incremental download cancelled",
                    error="cancelled",
                )
            raise
        except UserNotFoundError as e:
            await self.operation_store.update_operation(
                operation_id,
                status="failed",
                message="Incremental download failed",
                error=str(e),
            )
            raise
        except Exception as e:
            logger.exception(
                "Incremental download failed",
                extra={"sec_user_id": sec_user_id, "operation_id": operation_id},
            )
            await self.operation_store.update_operation(
                operation_id,
                status="failed",
                message="Incremental download failed",
                error=str(e),
            )
            raise PostServiceError(f"Incremental download failed: {str(e)}") from e

        new_count = len(new_aweme_ids)
        newest_aweme_id = new_aweme_ids[0] if new_aweme_ids else None
        # A truncated run is incomplete, so report it as "partial" rather than a
        # clean, exhaustive pass.
        final_status = "partial" if (failed_count or truncated) else "completed"
        message = (
            f"Downloaded {new_count} new post(s)" if new_count else "No new posts found"
        )
        if truncated:
            message += " (page cap reached; older posts not fetched this run)"
        await self.operation_store.update_operation(
            operation_id,
            status=final_status,
            message=message,
            progress=100.0,
            total_items=new_count,
            completed_items=new_count,
            download_path=download_path,
            metadata={
                "subscription_id": subscription_id,
                "since_aweme_id": since_aweme_id,
                "posted_after": posted_after.isoformat() if posted_after else None,
                "new_count": new_count,
                "failed_count": failed_count,
                "truncated": truncated,
                "newest_aweme_id": newest_aweme_id,
                "download_path": download_path,
            },
        )

        return IncrementalDownloadResult(
            operation_id=operation_id,
            new_count=new_count,
            newest_aweme_id=newest_aweme_id,
            seen_aweme_ids=new_aweme_ids,
            failed_count=failed_count,
            truncated=truncated,
        )

    async def _collect_new_posts(
        self,
        sec_user_id: str,
        user_path: Path,
        *,
        known: set[str],
        since_aweme_id: str | None,
        posted_after: datetime | None = None,
    ) -> tuple[list[str], int, bool]:
        """Paginate newest-first and download posts until a known id appears.

        Returns the newly downloaded aweme_ids (newest first), the count of
        posts that failed to download, and a truncation flag that is True when
        pagination hit ``MAX_PAGES_FALLBACK`` with more pages still available.
        Stops at the first post whose
        ``aweme_id`` is in ``known`` or equals ``since_aweme_id``; the
        upstream feed is newest-first, so that boundary marks
        previously-seen territory. ``MAX_PAGES_FALLBACK`` bounds a first run
        with an empty checkpoint so a misbehaving cursor cannot pin the loop.

        With ``posted_after``, posts at or before it (or without a readable
        post time) are skipped, and pagination stops after the first page
        with no newer post. Pinned posts can sit old at the top of the first
        page, so a single old post never ends the scan; a whole old page
        does, because the rest of the feed is older still.
        """
        new_aweme_ids: list[str] = []
        failed_count = 0
        current_cursor = 0
        page_count = 0
        truncated = False

        while page_count < MAX_PAGES_FALLBACK:
            page_count += 1
            batch = await self._fetch_posts_batch(sec_user_id, current_cursor)
            if not batch:
                break
            aweme_list = batch.get("aweme_list") or []
            if not aweme_list:
                break

            reached_known = False
            page_in_window = False
            for post in aweme_list:
                aweme_id = str(post.get("aweme_id") or "")
                if aweme_id and (aweme_id in known or aweme_id == since_aweme_id):
                    # Newest-first feed: the first already-known post marks
                    # the start of previously-downloaded territory.
                    reached_known = True
                    break
                if posted_after is not None:
                    posted = _post_created_at(post)
                    if posted is None or posted <= posted_after:
                        continue
                    page_in_window = True
                try:
                    post_type = self._determine_post_type(post)
                    await self._download_post_content(post, post_type, user_path)
                    if aweme_id:
                        new_aweme_ids.append(aweme_id)
                except Exception as e:
                    failed_count += 1
                    logger.error(
                        "Error downloading incremental post",
                        extra={"aweme_id": post.get("aweme_id"), "error": str(e)},
                    )

            if reached_known or (posted_after is not None and not page_in_window):
                break

            has_more = batch.get("has_more", False)
            next_cursor = batch.get("max_cursor", 0)
            if not has_more or not next_cursor or next_cursor == current_cursor:
                break
            current_cursor = next_cursor
        else:
            # Loop exhausted MAX_PAGES_FALLBACK without breaking, so the feed
            # still advertised more pages: posts beyond the cap were not
            # fetched this run. Signal truncation so the caller holds its
            # checkpoint -- advancing it would stop the next newest-first scan
            # at this run's newest post and skip every post past the cap.
            truncated = True
            logger.warning(
                "Incremental download hit max page fallback; posts beyond the "
                "cap were not fetched this run",
                extra={
                    "sec_user_id": sec_user_id,
                    "max_pages": MAX_PAGES_FALLBACK,
                    "downloaded": len(new_aweme_ids),
                },
            )

        return new_aweme_ids, failed_count, truncated

    async def _fetch_posts_batch(
        self,
        sec_user_id: str,
        cursor: int,
        mode: str = "post",
    ) -> dict[str, Any]:
        """Fetch a batch of posts from a user's feed.

        Returns an empty dict only when the upstream feed is exhausted
        (``StopAsyncIteration``). Any other exception propagates so the
        caller's ``batch_errored`` accounting in ``_run_bulk_download``
        can record the failure and the operation lands in a clear
        ``partial`` / ``failed`` terminal state instead of a silent clean
        break.
        """
        try:
            fetcher_name = BULK_FETCHERS[mode]
        except KeyError:
            raise PostServiceError(f"Unknown bulk download mode: {mode}") from None
        logger.info(
            "Fetching posts batch",
            extra={"sec_user_id": sec_user_id, "cursor": cursor, "mode": mode},
        )

        fetcher = getattr(self.handler, fetcher_name)
        if mode in OWNER_SCOPED_MODES:
            posts_iterator = fetcher(max_cursor=cursor, page_counts=PAGE_SIZE)
        else:
            posts_iterator = fetcher(
                sec_user_id=sec_user_id, max_cursor=cursor, page_counts=PAGE_SIZE
            )

        try:
            try:
                posts_filter = await posts_iterator.__anext__()
            except StopAsyncIteration:
                return {}

            raw_data = _mapping_from(posts_filter, "_to_raw")
            list_data = _list_from(posts_filter, "_to_list")
            if list_data:
                batch_data = dict(raw_data or {})
                batch_data["aweme_list"] = list_data
                return batch_data

            dict_data = _mapping_from(posts_filter, "_to_dict")
            if raw_data is not None and (
                raw_data.get("aweme_list")
                or dict_data is None
                or not dict_data.get("aweme_list")
            ):
                return raw_data
            return dict_data or {}
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
            post_payload = _prepare_post_for_downloader(post)
            await self.handler.downloader.create_download_tasks(
                self.handler.kwargs, [post_payload], user_path
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
# serialization shape easy to unit test and avoids leaking
# persistence-aware logic into the response model.
# ----------------------------------------------------------------------


def _mapping_from(obj: Any, method_name: str) -> dict[str, Any] | None:
    """Return a dict from a zero-argument conversion method when available."""
    method = getattr(obj, method_name, None)
    if not callable(method):
        return None
    value = method()
    if isinstance(value, dict):
        return dict(value)
    return None


def _list_from(obj: Any, method_name: str) -> list[dict[str, Any]] | None:
    """Return a list of dictionaries from a zero-argument conversion method."""
    method = getattr(obj, method_name, None)
    if not callable(method):
        return None
    value = method()
    if not isinstance(value, list):
        return None
    items = [dict(item) for item in value if isinstance(item, dict)]
    return items or None


def _post_created_at(post: dict[str, Any]) -> datetime | None:
    """Post time as f2 writes it into media folder names (naive UTC+8)."""
    value = post.get("create_time")
    if isinstance(value, str):
        try:
            return datetime.strptime(value[:19], "%Y-%m-%d %H-%M-%S")
        except ValueError:
            return None
    if isinstance(value, int | float) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, _F2_NAMING_ZONE).replace(tzinfo=None)
    return None


def _prepare_post_for_downloader(post: dict[str, Any]) -> dict[str, Any]:
    """Adapt raw Douyin post payloads to the f2 downloader field contract."""
    prepared = dict(post)

    author = prepared.get("author")
    if not prepared.get("sec_user_id") and isinstance(author, dict):
        sec_user_id = author.get("sec_uid") or author.get("sec_user_id")
        if isinstance(sec_user_id, str) and sec_user_id:
            prepared["sec_user_id"] = sec_user_id

    status = prepared.get("status")
    if isinstance(status, dict):
        if "private_status" not in prepared:
            prepared["private_status"] = status.get("private_status")
        if "is_prohibited" not in prepared:
            prepared["is_prohibited"] = status.get("is_prohibited")

    if not prepared.get("video_play_addr"):
        video_urls = _video_urls_from_raw_post(prepared)
        if video_urls:
            prepared["video_play_addr"] = video_urls

    normalized_live_images = _image_video_urls_from_raw_post(prepared)
    if normalized_live_images:
        prepared["images_video"] = normalized_live_images

    normalized_images = _image_urls_from_raw_post(prepared)
    if normalized_images:
        prepared["images"] = normalized_images

    return prepared


def _video_urls_from_raw_post(post: dict[str, Any]) -> list[str]:
    """Extract playable video URLs from raw or semi-normalized post data."""
    video = post.get("video")
    if not isinstance(video, dict):
        return []

    bit_rates = video.get("bit_rate")
    if isinstance(bit_rates, list):
        for bit_rate in bit_rates:
            if isinstance(bit_rate, dict):
                urls = _url_list_from(bit_rate.get("play_addr"))
                if urls:
                    return urls

    return _url_list_from(video.get("play_addr"))


def _image_urls_from_raw_post(post: dict[str, Any]) -> list[str | list[str]]:
    """Extract image URLs in the shape expected by f2's image downloader."""
    images = post.get("images")
    if not isinstance(images, list):
        return []

    normalized: list[str | list[str]] = []
    for image in images:
        urls = _url_list_from(image)
        if urls:
            normalized.append(_url_or_urls(urls))
    return normalized


def _image_video_urls_from_raw_post(post: dict[str, Any]) -> list[str | list[str]]:
    """Extract live-photo video URLs from raw image entries."""
    existing = post.get("images_video")
    if isinstance(existing, list):
        normalized_existing = [
            _url_or_urls(urls) for item in existing if (urls := _url_list_from(item))
        ]
        if normalized_existing:
            return normalized_existing

    images = post.get("images")
    if not isinstance(images, list):
        return []

    normalized: list[str | list[str]] = []
    for image in images:
        if not isinstance(image, dict):
            continue
        video = image.get("video")
        if not isinstance(video, dict):
            continue
        urls = _url_list_from(video.get("play_addr"))
        if urls:
            normalized.append(_url_or_urls(urls))
    return normalized


def _url_list_from(value: Any) -> list[str]:
    """Return HTTP URLs from a string, list, or Douyin URL container dict."""
    if isinstance(value, str):
        return [value] if value.startswith("http") else []
    if isinstance(value, list):
        return [
            item for item in value if isinstance(item, str) and item.startswith("http")
        ]
    if isinstance(value, dict):
        return _url_list_from(value.get("url_list"))
    return []


def _url_or_urls(urls: list[str]) -> str | list[str]:
    """Collapse single-link fallbacks while preserving multi-link alternatives."""
    return urls[0] if len(urls) == 1 else urls


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
