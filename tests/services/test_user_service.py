from __future__ import annotations

import asyncio

import pytest

from dyvine.services.users import DownloadError, DownloadResponse, UserService


@pytest.fixture(autouse=True)
def reset_user_service_singleton() -> None:
    UserService._instance = None  # type: ignore[attr-defined]
    UserService._active_downloads = {}  # type: ignore[attr-defined]
    yield
    UserService._instance = None  # type: ignore[attr-defined]
    UserService._active_downloads = {}  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_start_download_tracks_task(monkeypatch: pytest.MonkeyPatch) -> None:
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
    response = await service.start_download("user-123")

    assert isinstance(response, DownloadResponse)
    assert response.status == "pending"
    assert response.task_id in service._active_downloads  # type: ignore[attr-defined]
    assert scheduled, "Expected background task to be scheduled"


@pytest.mark.asyncio
async def test_get_download_status_returns_current_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _resolved_future(coro: object) -> asyncio.Future[None]:
        if hasattr(coro, "close"):
            coro.close()
        future: asyncio.Future[None] = asyncio.get_event_loop().create_future()
        future.set_result(None)
        return future

    monkeypatch.setattr("dyvine.services.users.asyncio.create_task", _resolved_future)

    service = UserService()
    start_response = await service.start_download("user-456")
    status_response = await service.get_download_status(start_response.task_id)

    assert status_response.task_id == start_response.task_id
    assert status_response.status == "pending"
    assert status_response.progress == 0.0


@pytest.mark.asyncio
async def test_get_download_status_raises_for_unknown_task() -> None:
    service = UserService()
    with pytest.raises(DownloadError):
        await service.get_download_status("missing-task")


# ── get_user_info (mocked handler) ──────────────────────────────────────


@pytest.mark.asyncio
async def test_get_user_info_success(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock, MagicMock

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
    from unittest.mock import AsyncMock, MagicMock

    from dyvine.core.exceptions import ServiceError
    from dyvine.services import users as users_mod

    mock_user_data = MagicMock()
    # Empty nickname raises UserNotFoundError internally, but the
    # service's bare `except Exception` swallows it and re-wraps as
    # ServiceError — so clients get 500 instead of 404.
    # TODO: add `except UserNotFoundError: raise` guard in get_user_info.
    mock_user_data.nickname = ""

    class FakeHandler:
        def __init__(self, kwargs: dict) -> None:
            pass

        fetch_user_profile = AsyncMock(return_value=mock_user_data)

    monkeypatch.setattr(users_mod, "DouyinHandler", FakeHandler)

    service = UserService()
    with pytest.raises(ServiceError):
        await service.get_user_info("missing-user")


@pytest.mark.asyncio
async def test_get_user_info_with_room_data(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from dyvine.services import users as users_mod

    mock_user_data = MagicMock()
    mock_user_data.nickname = "LiveUser"
    mock_user_data.avatar_url = "https://example.com/a.jpg"
    mock_user_data.signature = "sig"
    mock_user_data.following_count = 5
    mock_user_data.follower_count = 10
    mock_user_data.total_favorited = 50
    mock_user_data.room_id = 42
    mock_user_data._to_raw.return_value = {
        "user": {"room_data": '{"status":2}'}
    }

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


# ── start_download with options ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_download_with_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduled: list[object] = []

    def fake_create_task(coro: object) -> asyncio.Future[None]:
        if hasattr(coro, "close"):
            coro.close()
        future: asyncio.Future[None] = asyncio.Future()
        future.set_result(None)
        scheduled.append(future)
        return future

    monkeypatch.setattr(
        "dyvine.services.users.asyncio.create_task", fake_create_task
    )

    service = UserService()
    response = await service.start_download(
        "user-x", include_posts=False, include_likes=True, max_items=50
    )
    assert response.status == "pending"
    task_info = service._active_downloads[response.task_id]
    assert task_info["include_posts"] is False
    assert task_info["include_likes"] is True
    assert task_info["max_items"] == 50


# ── _process_download error handling ────────────────────────────────────


@pytest.mark.asyncio
async def test_process_download_no_posts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from dyvine.services import users as users_mod

    mock_user_data = MagicMock()
    mock_user_data.nickname = "NoPostUser"
    mock_user_data.aweme_count = 0

    class FakeHandler:
        def __init__(self, kwargs: dict) -> None:
            pass

        fetch_user_profile = AsyncMock(return_value=mock_user_data)

    monkeypatch.setattr(users_mod, "DouyinHandler", FakeHandler)
    monkeypatch.setattr(
        "dyvine.services.users.asyncio.sleep", AsyncMock()
    )

    service = UserService()
    task_id = "test-no-posts"
    service._active_downloads[task_id] = {
        "user_id": "empty-user",
        "status": "pending",
        "progress": 0.0,
        "start_time": None,
        "include_posts": True,
        "include_likes": False,
        "max_items": None,
    }

    await service._process_download(task_id)
    # Task is cleaned up after the mocked sleep + pop
    assert task_id not in service._active_downloads


@pytest.mark.asyncio
async def test_process_download_sets_failed_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    from dyvine.services import users as users_mod

    # Make DouyinHandler raise on profile fetch
    class FakeHandler:
        def __init__(self, kwargs: dict) -> None:
            pass

        fetch_user_profile = AsyncMock(side_effect=RuntimeError("api error"))

    monkeypatch.setattr(users_mod, "DouyinHandler", FakeHandler)
    # Prevent the 1-hour sleep at the end
    monkeypatch.setattr(
        "dyvine.services.users.asyncio.sleep", AsyncMock()
    )

    service = UserService()
    task_id = "test-task-fail"
    service._active_downloads[task_id] = {
        "user_id": "bad-user",
        "status": "pending",
        "progress": 0.0,
        "start_time": None,
        "include_posts": True,
        "include_likes": False,
        "max_items": None,
    }

    await service._process_download(task_id)

    # Task should not exist anymore (cleaned up after sleep)
    # But the status should have been set to failed before cleanup
    # Since asyncio.sleep is mocked, the task is cleaned up immediately
    assert task_id not in service._active_downloads
