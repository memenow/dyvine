from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from dyvine.core.operations import OperationStore
from dyvine.services.users import DownloadError, DownloadResponse, UserService


@pytest.mark.asyncio
async def test_start_download_tracks_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduled: list[asyncio.Future[None]] = []

    def fake_create_task(coro: object) -> asyncio.Future[None]:
        if hasattr(coro, "close"):
            coro.close()
        future: asyncio.Future[None] = asyncio.Future()
        future.set_result(None)
        scheduled.append(future)
        return future

    monkeypatch.setattr("dyvine.services.users.asyncio.create_task", fake_create_task)

    service = UserService()
    monkeypatch.setattr(service, "get_user_info", AsyncMock(return_value=MagicMock()))

    response = await service.start_download("user-123")

    assert isinstance(response, DownloadResponse)
    assert response.status == "pending"
    assert response.operation_id
    assert response.task_id == response.operation_id
    assert scheduled, "Expected background task to be scheduled"


@pytest.mark.asyncio
async def test_get_download_status_returns_persisted_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def resolved_future(coro: object) -> asyncio.Future[None]:
        if hasattr(coro, "close"):
            coro.close()
        future: asyncio.Future[None] = asyncio.get_event_loop().create_future()
        future.set_result(None)
        return future

    service = UserService()
    monkeypatch.setattr(service, "get_user_info", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr("dyvine.services.users.asyncio.create_task", resolved_future)

    start_response = await service.start_download("user-456")
    status_response = await service.get_download_status(start_response.operation_id)

    assert status_response.operation_id == start_response.operation_id
    assert status_response.status == "pending"
    assert status_response.progress == 0.0


@pytest.mark.asyncio
async def test_get_download_status_raises_for_unknown_task() -> None:
    service = UserService()
    with pytest.raises(DownloadError):
        await service.get_download_status("missing-task")


@pytest.mark.asyncio
async def test_get_download_status_rejects_non_user_operation(tmp_path) -> None:
    store = OperationStore(str(tmp_path / "operations.db"))
    operation = store.create_operation(
        operation_type="livestream_download",
        subject_id="room-1",
        status="pending",
        message="scheduled",
    )
    service = UserService(operation_store=store)

    with pytest.raises(DownloadError):
        await service.get_download_status(operation.operation_id)


@pytest.mark.asyncio
async def test_get_user_info_success(monkeypatch: pytest.MonkeyPatch) -> None:
    from dyvine.services import users as users_mod

    mock_user_data = MagicMock()
    mock_user_data.nickname = "TestUser"
    mock_user_data.avatar_url = "https://example.com/avatar.jpg"
    mock_user_data.signature = "test bio"
    mock_user_data.following_count = 10
    mock_user_data.follower_count = 20
    mock_user_data.total_favorited = 100
    mock_user_data.room_id = None
    mock_user_data._to_raw.return_value = {"user": {}}

    class FakeHandler:
        def __init__(self, kwargs: dict) -> None:
            pass

        fetch_user_profile = AsyncMock(return_value=mock_user_data)

    monkeypatch.setattr(users_mod, "DouyinHandler", FakeHandler)

    service = UserService()
    result = await service.get_user_info("test-user")
    assert result.nickname == "TestUser"
    assert result.user_id == "test-user"
    assert result.is_living is False


@pytest.mark.asyncio
async def test_get_user_info_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    from dyvine.core.exceptions import UserNotFoundError
    from dyvine.services import users as users_mod

    mock_user_data = MagicMock()
    mock_user_data.nickname = ""

    class FakeHandler:
        def __init__(self, kwargs: dict) -> None:
            pass

        fetch_user_profile = AsyncMock(return_value=mock_user_data)

    monkeypatch.setattr(users_mod, "DouyinHandler", FakeHandler)

    service = UserService()
    with pytest.raises(UserNotFoundError):
        await service.get_user_info("missing-user")


@pytest.mark.asyncio
async def test_get_user_info_with_room_data(monkeypatch: pytest.MonkeyPatch) -> None:
    from dyvine.services import users as users_mod

    mock_user_data = MagicMock()
    mock_user_data.nickname = "LiveUser"
    mock_user_data.avatar_url = "https://example.com/a.jpg"
    mock_user_data.signature = "sig"
    mock_user_data.following_count = 5
    mock_user_data.follower_count = 10
    mock_user_data.total_favorited = 50
    mock_user_data.room_id = 42
    mock_user_data._to_raw.return_value = {"user": {"room_data": '{"status":2}'}}

    class FakeHandler:
        def __init__(self, kwargs: dict) -> None:
            pass

        fetch_user_profile = AsyncMock(return_value=mock_user_data)

    monkeypatch.setattr(users_mod, "DouyinHandler", FakeHandler)

    service = UserService()
    result = await service.get_user_info("live-user")
    assert result.is_living is True
    assert result.room_id == 42
    assert result.room_data == '{"status":2}'


@pytest.mark.asyncio
async def test_process_download_no_posts(monkeypatch: pytest.MonkeyPatch) -> None:
    from dyvine.services import users as users_mod

    mock_user_data = MagicMock()
    mock_user_data.nickname = "NoPostUser"
    mock_user_data.aweme_count = 0

    class FakeHandler:
        def __init__(self, kwargs: dict) -> None:
            pass

        fetch_user_profile = AsyncMock(return_value=mock_user_data)

    monkeypatch.setattr(users_mod, "DouyinHandler", FakeHandler)

    service = UserService()
    operation = service.operation_store.create_operation(
        operation_type="user_content_download",
        subject_id="empty-user",
        status="pending",
        message="scheduled",
    )

    await service._process_download(
        operation.operation_id,
        user_id="empty-user",
        include_posts=True,
        include_likes=False,
        max_items=None,
    )

    refreshed = await service.get_download_status(operation.operation_id)
    assert refreshed.status == "completed"
    assert refreshed.progress == 100.0


@pytest.mark.asyncio
async def test_process_download_sets_failed_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dyvine.services import users as users_mod

    class FakeHandler:
        def __init__(self, kwargs: dict) -> None:
            pass

        fetch_user_profile = AsyncMock(side_effect=RuntimeError("api error"))

    monkeypatch.setattr(users_mod, "DouyinHandler", FakeHandler)

    service = UserService()
    operation = service.operation_store.create_operation(
        operation_type="user_content_download",
        subject_id="bad-user",
        status="pending",
        message="scheduled",
    )

    await service._process_download(
        operation.operation_id,
        user_id="bad-user",
        include_posts=True,
        include_likes=False,
        max_items=None,
    )

    refreshed = await service.get_download_status(operation.operation_id)
    assert refreshed.status == "failed"
    assert refreshed.error == "api error"


@pytest.mark.asyncio
async def test_process_download_skips_when_nothing_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A request with neither posts nor likes should complete immediately."""
    from dyvine.services import users as users_mod

    fetch_profile = AsyncMock()

    class FakeHandler:
        def __init__(self, kwargs: dict) -> None:
            pass

        fetch_user_profile = fetch_profile

    monkeypatch.setattr(users_mod, "DouyinHandler", FakeHandler)

    service = UserService()
    operation = service.operation_store.create_operation(
        operation_type="user_content_download",
        subject_id="opt-out-user",
        status="pending",
        message="scheduled",
    )

    await service._process_download(
        operation.operation_id,
        user_id="opt-out-user",
        include_posts=False,
        include_likes=False,
        max_items=None,
    )

    refreshed = await service.get_download_status(operation.operation_id)
    assert refreshed.status == "completed"
    assert refreshed.progress == 100.0
    assert refreshed.total_items == 0
    assert refreshed.completed_items == 0
    fetch_profile.assert_not_called()


@pytest.mark.asyncio
async def test_process_download_runs_when_only_likes_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``include_posts=False`` with ``include_likes=True`` routes to likes.

    Asserts the loop walks ``fetch_user_like_videos`` (not the post feed),
    that ``mode`` is tagged ``"like"`` on the handler kwargs, and that
    ``download_favorite`` stays off so f2 does not double-fetch through
    the posts feed.
    """
    from dyvine.services import users as users_mod

    mock_user_data = MagicMock()
    mock_user_data.nickname = "likes-only-user"
    mock_user_data.aweme_count = 0

    fetch_profile = AsyncMock(return_value=mock_user_data)
    fetch_post_videos = MagicMock()
    captured_kwargs: dict[str, Any] = {}

    async def fake_fetch_likes(*_args: Any, **_kwargs: Any) -> Any:
        # Empty async generator; the loop treats this as "no likes yet"
        # and exits cleanly.
        if False:
            yield None

    class FakeHandler:
        def __init__(self, kwargs: dict) -> None:
            captured_kwargs.update(kwargs)

        fetch_user_profile = fetch_profile
        fetch_user_post_videos = fetch_post_videos
        fetch_user_like_videos = staticmethod(fake_fetch_likes)

    monkeypatch.setattr(users_mod, "DouyinHandler", FakeHandler)

    service = UserService()
    operation = service.operation_store.create_operation(
        operation_type="user_content_download",
        subject_id="likes-only-user",
        status="pending",
        message="scheduled",
    )

    await service._process_download(
        operation.operation_id,
        user_id="likes-only-user",
        include_posts=False,
        include_likes=True,
        max_items=None,
    )

    fetch_profile.assert_awaited()
    fetch_post_videos.assert_not_called()
    assert captured_kwargs["mode"] == "like"
    assert captured_kwargs["download_favorite"] is False


@pytest.mark.asyncio
async def test_process_download_likes_reports_progress_as_indeterminate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A likes-only run must not report a bogus 100% progress mid-flight.

    The profile endpoint cannot expose a total number of liked items, so
    the in-loop update must leave ``progress`` as ``None`` rather than
    pegging it to 100% after the first batch. Construct a fake likes
    generator that yields one page (one aweme), then verify the stored
    record still carries ``progress is None`` while ``completed_items``
    reflects the real count.
    """
    from dyvine.services import users as users_mod

    mock_user_data = MagicMock()
    mock_user_data.nickname = "likes-progress-user"
    mock_user_data.aweme_count = 0

    fetch_profile = AsyncMock(return_value=mock_user_data)

    aweme_batch = MagicMock()
    aweme_batch.has_aweme = True
    aweme_batch.aweme_id = ["aw1"]
    aweme_batch.has_more = False
    aweme_batch.max_cursor = 0
    aweme_batch._to_list = MagicMock(return_value=[])

    async def fake_fetch_likes(*_args: Any, **_kwargs: Any) -> Any:
        yield aweme_batch

    class FakeDownloader:
        create_download_tasks = AsyncMock()

    class FakeHandler:
        def __init__(self, kwargs: dict) -> None:
            pass

        fetch_user_profile = fetch_profile
        fetch_user_like_videos = staticmethod(fake_fetch_likes)
        downloader = FakeDownloader()

    monkeypatch.setattr(users_mod, "DouyinHandler", FakeHandler)

    # Redirect the working directory so ``temp_downloads`` is created
    # inside ``tmp_path`` and we never touch the repo tree.
    monkeypatch.chdir(tmp_path)

    service = UserService()
    operation = service.operation_store.create_operation(
        operation_type="user_content_download",
        subject_id="likes-progress-user",
        status="pending",
        message="scheduled",
    )

    progress_samples: list[float | None] = []
    original_update = service.operation_store.update_operation

    def capture_update(operation_id: str, **fields: Any) -> Any:
        # Only capture in-loop updates (they carry ``completed_items`` but
        # never a ``status`` change; the ``running`` bootstrap update does).
        if "status" not in fields and "completed_items" in fields:
            progress_samples.append(fields.get("progress"))
        return original_update(operation_id, **fields)

    monkeypatch.setattr(service.operation_store, "update_operation", capture_update)

    await service._process_download(
        operation.operation_id,
        user_id="likes-progress-user",
        include_posts=False,
        include_likes=True,
        max_items=None,
    )

    # No in-loop update should carry a numeric progress value because the
    # total is unknown. The initial ``running`` update also omits progress
    # for the same reason, so the captured list must be empty.
    assert all(sample is None for sample in progress_samples), progress_samples

    refreshed = await service.get_download_status(operation.operation_id)
    assert refreshed.status == "completed"
    assert refreshed.completed_items == 1
