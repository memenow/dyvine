from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from dyvine.core.exceptions import DownloadError, PostNotFoundError, ServiceError
from dyvine.core.operations import OperationStore
from dyvine.schemas.posts import DownloadStatus, PostType
from dyvine.services.posts import PostService


def _build_service(
    handler: MagicMock | None = None,
    *,
    operation_store: OperationStore | None = None,
) -> PostService:
    """Create PostService without calling __init__ (avoids DouyinHandler).

    Tests that exercise the bulk download paths must pass an
    ``operation_store`` so the service can persist progress; the simple
    helper-coverage tests only touch private helpers and can omit it.
    """
    svc = object.__new__(PostService)  # type: ignore[return-value]
    if handler is not None:
        svc.handler = handler  # type: ignore[attr-defined]
    if operation_store is not None:
        svc.operation_store = operation_store  # type: ignore[attr-defined]
    return svc


# ── _determine_post_type ─────────────────────────────────────────────────


def test_determine_post_type_live() -> None:
    svc = _build_service()
    assert svc._determine_post_type({"aweme_type": 1}) == PostType.LIVE


def test_determine_post_type_collection() -> None:
    svc = _build_service()
    assert svc._determine_post_type({"aweme_type": 3}) == PostType.COLLECTION


def test_determine_post_type_story() -> None:
    svc = _build_service()
    assert svc._determine_post_type({"aweme_type": 4}) == PostType.STORY


def test_determine_post_type_video() -> None:
    svc = _build_service()
    post = {"aweme_type": 0, "video": {"play_addr": {"url_list": ["http://v"]}}}
    assert svc._determine_post_type(post) == PostType.VIDEO


def test_determine_post_type_images() -> None:
    svc = _build_service()
    post = {"aweme_type": 0, "images": [{"url_list": ["http://i"]}]}
    assert svc._determine_post_type(post) == PostType.IMAGES


def test_determine_post_type_mixed() -> None:
    svc = _build_service()
    post = {
        "aweme_type": 0,
        "images": [{"url_list": ["http://i"]}],
        "video": {"play_addr": {"url_list": ["http://v"]}},
    }
    assert svc._determine_post_type(post) == PostType.MIXED


def test_determine_post_type_unknown_no_media() -> None:
    svc = _build_service()
    assert svc._determine_post_type({"aweme_type": 0}) == PostType.UNKNOWN


def test_determine_post_type_invalid_aweme_type() -> None:
    svc = _build_service()
    assert svc._determine_post_type({"aweme_type": "bad"}) == PostType.UNKNOWN


# ── _extract_video_info ──────────────────────────────────────────────────


def test_extract_video_info_valid() -> None:
    svc = _build_service()
    post = {
        "video": {
            "play_addr": {
                "url_list": ["https://example.com/v.mp4"],
                "width": 1920,
                "height": 1080,
            },
            "duration": 60,
            "ratio": "16:9",
        }
    }
    vi = svc._extract_video_info(post)
    assert vi is not None
    assert vi.duration == 60


def test_extract_video_info_no_video() -> None:
    svc = _build_service()
    assert svc._extract_video_info({}) is None


def test_extract_video_info_empty_url_list() -> None:
    svc = _build_service()
    post = {"video": {"play_addr": {"url_list": []}}}
    assert svc._extract_video_info(post) is None


# ── _extract_image_info ──────────────────────────────────────────────────


def test_extract_image_info_valid() -> None:
    svc = _build_service()
    post = {
        "images": [
            {
                "url_list": ["https://example.com/i.jpg"],
                "width": 800,
                "height": 600,
            }
        ]
    }
    imgs = svc._extract_image_info(post)
    assert imgs is not None
    assert len(imgs) == 1
    assert imgs[0].width == 800


def test_extract_image_info_no_images() -> None:
    svc = _build_service()
    assert svc._extract_image_info({}) is None


def test_extract_image_info_invalid_entries() -> None:
    svc = _build_service()
    post = {"images": ["not-a-dict", 42]}
    assert svc._extract_image_info(post) is None


# ── _extract_image_urls ──────────────────────────────────────────────────


def test_extract_image_urls_valid() -> None:
    svc = _build_service()
    post = {
        "images": [
            {"url_list": ["https://example.com/a.jpg", "https://example.com/b.jpg"]},
            {"url_list": ["http://example.com/c.jpg"]},
        ]
    }
    urls = svc._extract_image_urls(post)
    assert len(urls) == 3


def test_extract_image_urls_empty() -> None:
    svc = _build_service()
    assert svc._extract_image_urls({}) == []


# ── _create_download_response ────────────────────────────────────────────


def _stats(**kw: int) -> dict:
    base = dict.fromkeys(PostType, 0)
    for k, v in kw.items():
        base[PostType(k)] = v
    return base


def test_create_download_response_success() -> None:
    svc = _build_service()
    stats = _stats(video=5)
    resp = svc._create_download_response("u1", "/dl", 5, stats)
    assert resp.status == DownloadStatus.SUCCESS
    assert resp.total_downloaded == 5


def test_create_download_response_partial() -> None:
    svc = _build_service()
    stats = _stats(video=3)
    resp = svc._create_download_response("u1", "/dl", 10, stats)
    assert resp.status == DownloadStatus.PARTIAL_SUCCESS


def test_create_download_response_failed() -> None:
    svc = _build_service()
    stats = _stats()
    resp = svc._create_download_response("u1", "/dl", 10, stats)
    assert resp.status == DownloadStatus.FAILED
    assert resp.total_downloaded == 0


def test_create_download_response_message_format() -> None:
    svc = _build_service()
    stats = _stats(video=2, images=1)
    resp = svc._create_download_response("u1", "/dl", 10, stats)
    assert "Videos: 2" in resp.message
    assert "Images: 1" in resp.message
    assert "/dl" in resp.message


# ── get_post_detail (async, mocked handler) ─────────────────────────────


@pytest.mark.asyncio
async def test_get_post_detail_success() -> None:
    handler = MagicMock()
    post_mock = MagicMock()
    post_mock._to_dict.return_value = {
        "aweme_id": "789",
        "desc": "test post",
        "create_time": "2024-01-15 10-30-00",
        "video": {
            "play_addr": {
                "url_list": ["https://example.com/v.mp4"],
                "width": 1920,
                "height": 1080,
            },
            "duration": 30,
            "ratio": "16:9",
        },
        "statistics": {"digg_count": 100},
    }
    handler.fetch_one_video = AsyncMock(return_value=post_mock)

    svc = _build_service(handler)
    result = await svc.get_post_detail("789")
    assert result.aweme_id == "789"
    assert result.desc == "test post"
    assert result.create_time > 0


@pytest.mark.asyncio
async def test_get_post_detail_not_found() -> None:
    handler = MagicMock()
    handler.fetch_one_video = AsyncMock(return_value=None)

    svc = _build_service(handler)
    with pytest.raises(PostNotFoundError):
        await svc.get_post_detail("missing")


@pytest.mark.asyncio
async def test_get_post_detail_invalid_create_time() -> None:
    handler = MagicMock()
    post_mock = MagicMock()
    post_mock._to_dict.return_value = {
        "aweme_id": "111",
        "create_time": "bad-date",
    }
    handler.fetch_one_video = AsyncMock(return_value=post_mock)

    svc = _build_service(handler)
    result = await svc.get_post_detail("111")
    assert result.create_time == 0


@pytest.mark.asyncio
async def test_get_post_detail_handler_error() -> None:
    handler = MagicMock()
    handler.fetch_one_video = AsyncMock(side_effect=RuntimeError("api fail"))

    svc = _build_service(handler)
    with pytest.raises(ServiceError, match="Failed to fetch post"):
        await svc.get_post_detail("err")


# ── get_user_posts (async, mocked handler) ──────────────────────────────


@pytest.mark.asyncio
async def test_get_user_posts_success() -> None:
    handler = MagicMock()
    posts_filter = MagicMock()
    posts_filter._to_raw.return_value = {
        "aweme_list": [
            {
                "aweme_id": "p1",
                "desc": "post 1",
                "create_time": 1000,
                "aweme_type": 0,
                "video": {"play_addr": {"url_list": ["https://example.com/v.mp4"]}},
            }
        ],
        "has_more": False,
    }

    async def _iter(*a, **kw):
        yield posts_filter

    handler.fetch_user_post_videos = _iter
    svc = _build_service(handler)
    result = await svc.get_user_posts("user1")
    assert len(result) == 1
    assert result[0].aweme_id == "p1"


@pytest.mark.asyncio
async def test_get_user_posts_empty() -> None:
    handler = MagicMock()

    async def _iter(*a, **kw):
        return
        yield  # make this an async generator

    handler.fetch_user_post_videos = _iter
    svc = _build_service(handler)
    result = await svc.get_user_posts("user-empty")
    assert result == []


@pytest.mark.asyncio
async def test_get_user_posts_empty_aweme_list() -> None:
    handler = MagicMock()
    posts_filter = MagicMock()
    posts_filter._to_raw.return_value = {"aweme_list": [], "has_more": False}

    async def _iter(*a, **kw):
        yield posts_filter

    handler.fetch_user_post_videos = _iter
    svc = _build_service(handler)
    result = await svc.get_user_posts("user-no-posts")
    assert result == []


# ── _fetch_posts_batch (async) ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_posts_batch_success() -> None:
    handler = MagicMock()
    posts_filter = MagicMock()
    posts_filter._to_dict.return_value = {"aweme_list": [{"aweme_id": "1"}]}

    async def _iter(*a, **kw):
        yield posts_filter

    handler.fetch_user_post_videos = _iter
    svc = _build_service(handler)
    result = await svc._fetch_posts_batch("u1", 0)
    assert "aweme_list" in result


@pytest.mark.asyncio
async def test_fetch_posts_batch_empty() -> None:
    handler = MagicMock()

    async def _iter(*a, **kw):
        return
        yield

    handler.fetch_user_post_videos = _iter
    svc = _build_service(handler)
    result = await svc._fetch_posts_batch("u1", 0)
    assert result == {}


# ── _download_post_content (async) ──────────────────────────────────────


@pytest.mark.asyncio
async def test_download_post_content_success() -> None:
    handler = MagicMock()
    handler.downloader = MagicMock()
    handler.downloader.create_download_tasks = AsyncMock()
    handler.kwargs = {}
    from pathlib import Path

    svc = _build_service(handler)
    await svc._download_post_content({"aweme_id": "123"}, PostType.VIDEO, Path("/tmp"))
    handler.downloader.create_download_tasks.assert_awaited_once()


# ── start_bulk_download (async, mocked) ─────────────────────────────────


@pytest.mark.asyncio
async def test_start_bulk_download_user_not_found(tmp_path) -> None:
    from dyvine.core.exceptions import UserNotFoundError

    handler = MagicMock()
    handler.fetch_user_profile = AsyncMock(return_value=None)

    store = OperationStore(str(tmp_path / "operations.db"))
    svc = _build_service(handler, operation_store=store)

    with pytest.raises(UserNotFoundError):
        await svc.start_bulk_download("missing-user")


@pytest.mark.asyncio
async def test_start_bulk_download_returns_pending_response_immediately(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """``start_bulk_download`` must persist the operation and return without
    awaiting the long-running download loop."""
    handler = MagicMock()
    profile = MagicMock()
    profile.aweme_count = 0
    profile.nickname = "PendingUser"
    handler.fetch_user_profile = AsyncMock(return_value=profile)

    store = OperationStore(str(tmp_path / "operations.db"))
    svc = _build_service(handler, operation_store=store)

    scheduled: list[asyncio.Future[None]] = []

    def fake_create_task(coro: Any, **_kwargs: Any) -> asyncio.Future[None]:
        if hasattr(coro, "close"):
            coro.close()
        future: asyncio.Future[None] = asyncio.get_event_loop().create_future()
        future.set_result(None)
        scheduled.append(future)
        return future

    monkeypatch.setattr("dyvine.core.background.asyncio.create_task", fake_create_task)

    response = await svc.start_bulk_download("pending-user", max_cursor=42)

    assert response.status == DownloadStatus.PENDING
    assert response.operation_id
    assert response.sec_user_id == "pending-user"
    assert response.total_posts == 0
    assert response.total_downloaded == 0
    assert response.message == "Bulk download scheduled"
    assert scheduled, "Expected the bulk download loop to be scheduled"

    persisted = await store.get_operation(response.operation_id)
    assert persisted.operation_type == "user_posts_bulk_download"
    assert persisted.status == "pending"
    assert persisted.metadata == {"max_cursor": 42}


# ── _run_bulk_download (async, awaited inline) ──────────────────────────


@pytest.mark.asyncio
async def test_run_bulk_download_marks_completed_for_zero_post_user(
    tmp_path,
) -> None:
    """A user with no posts walks through ``_run_bulk_download`` cleanly."""
    handler = MagicMock()
    profile = MagicMock()
    profile.aweme_count = 0
    profile.nickname = "EmptyUser"
    handler.fetch_user_profile = AsyncMock(return_value=profile)

    from pathlib import Path
    from unittest.mock import patch

    handler.get_or_add_user_data = AsyncMock(return_value=Path("/tmp/user"))
    store = OperationStore(str(tmp_path / "operations.db"))
    operation = await store.create_operation(
        operation_type="user_posts_bulk_download",
        subject_id="u1",
        status="pending",
        message="scheduled",
        metadata={"max_cursor": 0},
    )

    svc = _build_service(handler, operation_store=store)

    with patch("dyvine.services.posts.AsyncUserDB") as mock_db:
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_db.return_value = mock_ctx

        with patch.object(svc, "_fetch_posts_batch", new=AsyncMock(return_value={})):
            await svc._run_bulk_download(
                operation.operation_id, "u1", 0, profile=profile
            )

    refreshed = await store.get_operation(operation.operation_id)
    assert refreshed.status == "completed"
    # ``total_downloaded == total_posts == 0`` is treated as a clean run.
    assert refreshed.completed_items == 0
    assert refreshed.total_items == 0


@pytest.mark.asyncio
async def test_run_bulk_download_records_failure_when_user_disappears(
    tmp_path,
) -> None:
    """If the user disappears between scheduling and execution, the operation
    is marked failed rather than letting ``UserNotFoundError`` propagate."""
    handler = MagicMock()
    handler.fetch_user_profile = AsyncMock(return_value=None)

    store = OperationStore(str(tmp_path / "operations.db"))
    operation = await store.create_operation(
        operation_type="user_posts_bulk_download",
        subject_id="ghost-user",
        status="pending",
        message="scheduled",
        metadata={"max_cursor": 0},
    )

    svc = _build_service(handler, operation_store=store)
    # Pass ``profile=None`` to exercise the defensive guard inside
    # ``_run_bulk_download``: when the caller supplies an invalid profile
    # (e.g. the user disappeared between scheduling and execution) the
    # operation must be marked failed instead of letting the
    # ``UserNotFoundError`` propagate uncaught.
    await svc._run_bulk_download(operation.operation_id, "ghost-user", 0, profile=None)

    refreshed = await store.get_operation(operation.operation_id)
    assert refreshed.status == "failed"
    assert refreshed.error and "ghost-user" in refreshed.error


@pytest.mark.asyncio
async def test_download_post_content_error() -> None:
    handler = MagicMock()
    handler.downloader = MagicMock()
    handler.downloader.create_download_tasks = AsyncMock(
        side_effect=RuntimeError("dl fail")
    )
    handler.kwargs = {}
    from pathlib import Path

    svc = _build_service(handler)
    with pytest.raises(RuntimeError):
        await svc._download_post_content(
            {"aweme_id": "123"}, PostType.VIDEO, Path("/tmp")
        )


# ── _run_bulk_download pagination guards ────────────────────────────────


@pytest.mark.asyncio
async def test_run_bulk_download_breaks_on_empty_aweme_list(tmp_path) -> None:
    """An empty ``aweme_list`` with ``has_more=True`` must not spin the loop.

    Before the fix, a page with ``aweme_list=[]`` but ``has_more=True``
    would fall through to the cursor-advance branch and reissue the same
    request forever. The guarded ``break`` ends pagination as soon as the
    upstream stops returning posts, mirroring the ``iterated`` sentinel
    that PR #37 added to the livestream likes-only path.
    """
    from pathlib import Path
    from unittest.mock import patch

    handler = MagicMock()
    profile = MagicMock()
    profile.aweme_count = 5
    profile.nickname = "LoopUser"
    handler.fetch_user_profile = AsyncMock(return_value=profile)
    handler.get_or_add_user_data = AsyncMock(return_value=Path("/tmp/loop-user"))

    store = OperationStore(str(tmp_path / "operations.db"))
    operation = await store.create_operation(
        operation_type="user_posts_bulk_download",
        subject_id="loop-user",
        status="pending",
        message="scheduled",
        metadata={"max_cursor": 0},
    )

    with patch("dyvine.services.posts.AsyncUserDB") as mock_db:
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_db.return_value = mock_ctx

        svc = _build_service(handler, operation_store=store)

        # Each invocation returns ``has_more=True`` and a fresh cursor, so
        # without the empty-batch guard the outer ``while True`` would
        # advance forever. The test caps the fetch call count to ensure
        # the loop exits instead of spinning.
        call_count = 0

        async def fake_fetch(
            sec_user_id: str, cursor: int
        ) -> dict:  # type: ignore[override]
            nonlocal call_count
            call_count += 1
            if call_count > 5:  # safety net; the fix should cap at 1.
                raise AssertionError("Outer pagination loop spun past the empty batch")
            return {
                "aweme_list": [],
                "has_more": True,
                "max_cursor": cursor + 1,
            }

        with patch.object(svc, "_fetch_posts_batch", side_effect=fake_fetch):
            await svc._run_bulk_download(
                operation.operation_id, "loop-user", 0, profile=profile
            )

        # The first empty batch must short-circuit the loop.
        assert call_count == 1

    refreshed = await svc.get_bulk_download_status(operation.operation_id)
    assert refreshed.total_downloaded == 0


@pytest.mark.asyncio
async def test_run_bulk_download_caps_pagination_under_sticky_cursor(
    tmp_path,
) -> None:
    """A non-empty batch with a sticky cursor must terminate via ``max_pages``.

    Without the cap, the ``+1`` cursor advance keeps the loop running
    forever when the upstream returns the same non-empty batch on every
    call. The new ``max_pages`` ceiling mirrors the guard PR #44 added
    to the user-content download path in ``services/users.py``.
    """
    from pathlib import Path
    from unittest.mock import patch

    handler = MagicMock()
    profile = MagicMock()
    profile.aweme_count = 100
    profile.nickname = "StickyUser"
    handler.fetch_user_profile = AsyncMock(return_value=profile)
    handler.get_or_add_user_data = AsyncMock(return_value=Path("/tmp/sticky-user"))

    store = OperationStore(str(tmp_path / "operations.db"))
    operation = await store.create_operation(
        operation_type="user_posts_bulk_download",
        subject_id="sticky-user",
        status="pending",
        message="scheduled",
        metadata={"max_cursor": 0},
    )

    with patch("dyvine.services.posts.AsyncUserDB") as mock_db:
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_db.return_value = mock_ctx

        svc = _build_service(handler, operation_store=store)

        call_count = 0

        async def sticky_fetch(sec_user_id: str, cursor: int) -> dict:
            nonlocal call_count
            call_count += 1
            if call_count > 200:  # safety net; the cap should fire long before.
                raise AssertionError(
                    "Outer pagination loop exceeded 200 fetches "
                    "despite the max_pages guard"
                )
            # Sticky cursor: server keeps echoing the same non-empty
            # batch with ``has_more=True`` and ``max_cursor=0``. Without
            # the new guard, the ``+1`` advance below would keep the
            # loop running forever.
            return {
                "aweme_list": [{"aweme_id": f"p{call_count}", "aweme_type": 0}],
                "has_more": True,
                "max_cursor": 0,
            }

        process_batch = AsyncMock()
        with patch.object(svc, "_fetch_posts_batch", side_effect=sticky_fetch):
            with patch.object(svc, "_process_posts_batch", new=process_batch):
                await svc._run_bulk_download(
                    operation.operation_id, "sticky-user", 0, profile=profile
                )

        # max_pages = (100 // 20) * 2 + 20 = 30. The loop breaks when
        # ``page_count > max_pages``, so call_count should be at most 31.
        assert (
            call_count <= 31
        ), f"max_pages cap failed; loop ran {call_count} iterations"
        # Forward progress was made before the cap kicked in.
        assert process_batch.await_count == call_count

    refreshed = await svc.get_bulk_download_status(operation.operation_id)
    # ``total_downloaded`` is computed from ``download_stats`` which the
    # patched ``_process_posts_batch`` never increments, so the response
    # status must be FAILED. The important contract for this test is
    # termination, not the stats.
    assert refreshed.total_downloaded == 0


@pytest.mark.asyncio
async def test_run_bulk_download_stops_on_batch_error(tmp_path) -> None:
    """A persistent batch-level exception must not busy-loop.

    The previous ``except Exception: continue`` path swallowed the error
    without advancing ``current_cursor``, so any recurrent failure (e.g.
    a flaky upstream) spun the loop. The fix converts ``continue`` to
    ``break`` and surfaces the resulting state via the bulk response.
    """
    from pathlib import Path
    from unittest.mock import patch

    handler = MagicMock()
    profile = MagicMock()
    profile.aweme_count = 3
    profile.nickname = "ErrorUser"
    handler.fetch_user_profile = AsyncMock(return_value=profile)
    handler.get_or_add_user_data = AsyncMock(return_value=Path("/tmp/error-user"))

    store = OperationStore(str(tmp_path / "operations.db"))
    operation = await store.create_operation(
        operation_type="user_posts_bulk_download",
        subject_id="error-user",
        status="pending",
        message="scheduled",
        metadata={"max_cursor": 0},
    )

    with patch("dyvine.services.posts.AsyncUserDB") as mock_db:
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_db.return_value = mock_ctx

        svc = _build_service(handler, operation_store=store)

        call_count = 0

        async def failing_fetch(
            sec_user_id: str, cursor: int
        ) -> dict:  # type: ignore[override]
            nonlocal call_count
            call_count += 1
            if call_count > 3:
                raise AssertionError("Outer pagination loop spun past the batch error")
            raise RuntimeError("upstream flaky")

        with patch.object(svc, "_fetch_posts_batch", side_effect=failing_fetch):
            await svc._run_bulk_download(
                operation.operation_id, "error-user", 0, profile=profile
            )

        assert call_count == 1

    refreshed = await svc.get_bulk_download_status(operation.operation_id)
    assert refreshed.total_downloaded == 0
    # The loop terminated with no successful downloads but did not raise,
    # so the operation lands in ``failed`` status (mapped to FAILED).
    assert refreshed.status == DownloadStatus.FAILED


# ── runtime polling consistency ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_bulk_download_persists_runtime_download_stats(
    tmp_path,
    monkeypatch,
) -> None:
    """Runtime progress updates include the per-type counters clients poll."""
    from pathlib import Path
    from unittest.mock import patch

    handler = MagicMock()
    profile = MagicMock()
    profile.aweme_count = 2
    profile.nickname = "RuntimeUser"
    user_path = tmp_path / "runtime-user"
    handler.fetch_user_profile = AsyncMock(return_value=profile)
    handler.get_or_add_user_data = AsyncMock(return_value=user_path)

    store = OperationStore(str(tmp_path / "operations.db"))
    operation = await store.create_operation(
        operation_type="user_posts_bulk_download",
        subject_id="runtime-user",
        status="pending",
        message="scheduled",
        metadata={"max_cursor": 0},
    )

    runtime_metadata_samples: list[dict[str, Any] | None] = []
    original_update = store.update_operation

    async def capture_update(operation_id: str, **fields: Any) -> Any:
        if (
            fields.get("message") == "Bulk download in progress"
            and fields.get("completed_items", 0) > 0
        ):
            runtime_metadata_samples.append(fields.get("metadata"))
        return await original_update(operation_id, **fields)

    async def process_batch(
        posts: dict[str, Any],
        download_stats: dict[PostType, int],
        path: Path,
    ) -> None:
        assert path == user_path
        download_stats[PostType.VIDEO] += 1
        download_stats[PostType.IMAGES] += 1

    monkeypatch.setattr(store, "update_operation", capture_update)

    with patch("dyvine.services.posts.AsyncUserDB") as mock_db:
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_db.return_value = mock_ctx

        svc = _build_service(handler, operation_store=store)
        batch = {
            "aweme_list": [
                {"aweme_id": "video-1", "aweme_type": 0},
                {"aweme_id": "image-1", "aweme_type": 0},
            ],
            "has_more": False,
            "max_cursor": 0,
        }
        with patch.object(svc, "_fetch_posts_batch", new=AsyncMock(return_value=batch)):
            with patch.object(svc, "_process_posts_batch", side_effect=process_batch):
                await svc._run_bulk_download(
                    operation.operation_id, "runtime-user", 0, profile=profile
                )

    assert runtime_metadata_samples
    runtime_metadata = runtime_metadata_samples[-1]
    assert runtime_metadata is not None
    assert runtime_metadata["max_cursor"] == 0
    assert runtime_metadata["download_path"] == str(user_path)
    assert runtime_metadata["total_posts"] == 2
    assert runtime_metadata["download_stats"]["video"] == 1
    assert runtime_metadata["download_stats"]["images"] == 1

    refreshed = await svc.get_bulk_download_status(operation.operation_id)
    assert refreshed.total_downloaded == 2
    assert refreshed.downloaded_count[PostType.VIDEO] == 1
    assert refreshed.downloaded_count[PostType.IMAGES] == 1


# ── get_bulk_download_status (async) ────────────────────────────────────


@pytest.mark.asyncio
async def test_get_bulk_download_status_returns_persisted_state(tmp_path) -> None:
    store = OperationStore(str(tmp_path / "operations.db"))
    operation = await store.create_operation(
        operation_type="user_posts_bulk_download",
        subject_id="poll-user",
        status="running",
        message="Bulk download in progress",
        progress=42.5,
        total_items=100,
        completed_items=42,
        download_path="/tmp/poll-user",
        metadata={
            "download_stats": {
                "video": 30,
                "images": 10,
                "mixed": 2,
            },
            "download_path": "/tmp/poll-user",
        },
    )

    svc = _build_service(operation_store=store)

    response = await svc.get_bulk_download_status(operation.operation_id)
    assert response.operation_id == operation.operation_id
    assert response.status == DownloadStatus.IN_PROGRESS
    assert response.sec_user_id == "poll-user"
    assert response.download_path == "/tmp/poll-user"
    assert response.total_posts == 100
    assert response.downloaded_count[PostType.VIDEO] == 30
    assert response.downloaded_count[PostType.IMAGES] == 10
    assert response.downloaded_count[PostType.MIXED] == 2
    # ``completed_items`` always wins when it exceeds the per-PostType sum.
    assert response.total_downloaded == 42


@pytest.mark.asyncio
async def test_get_bulk_download_status_raises_for_unknown_id(tmp_path) -> None:
    store = OperationStore(str(tmp_path / "operations.db"))
    svc = _build_service(operation_store=store)

    with pytest.raises(DownloadError):
        await svc.get_bulk_download_status("missing-id")


@pytest.mark.asyncio
async def test_get_bulk_download_status_rejects_non_post_operation(
    tmp_path,
) -> None:
    store = OperationStore(str(tmp_path / "operations.db"))
    operation = await store.create_operation(
        operation_type="livestream_download",
        subject_id="room-1",
        status="pending",
        message="scheduled",
    )

    svc = _build_service(operation_store=store)
    with pytest.raises(DownloadError):
        await svc.get_bulk_download_status(operation.operation_id)
