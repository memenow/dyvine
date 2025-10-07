from __future__ import annotations

import asyncio

import pytest

from src.dyvine.services.users import DownloadError, DownloadResponse, UserService


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

    monkeypatch.setattr("src.dyvine.services.users.asyncio.create_task", fake_create_task)

    service = UserService()
    response = await service.start_download("user-123")

    assert isinstance(response, DownloadResponse)
    assert response.status == "pending"
    assert response.task_id in service._active_downloads  # type: ignore[attr-defined]
    assert scheduled, "Expected background task to be scheduled"


@pytest.mark.asyncio
async def test_get_download_status_returns_current_state(monkeypatch: pytest.MonkeyPatch) -> None:
    def _resolved_future(coro: object) -> asyncio.Future[None]:
        if hasattr(coro, "close"):
            coro.close()
        future: asyncio.Future[None] = asyncio.get_event_loop().create_future()
        future.set_result(None)
        return future

    monkeypatch.setattr("src.dyvine.services.users.asyncio.create_task", _resolved_future)

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
