from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from dyvine.core.exceptions import UserNotFoundError
from dyvine.schemas.livestreams import (
    LiveStreamDownloadResponse,
    LiveStreamURLDownloadRequest,
)
from dyvine.services.livestreams import DownloadError, LivestreamError


@pytest.fixture
def mock_livestream_service() -> MagicMock:
    svc = MagicMock()
    svc.download_stream = AsyncMock()
    svc.get_download_status = AsyncMock()
    return svc


# ── download_livestream ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_download_livestream_success(
    mock_livestream_service: MagicMock,
) -> None:
    from dyvine.routers.livestreams import download_livestream

    mock_livestream_service.download_stream.return_value = LiveStreamDownloadResponse(
        operation_id="op1",
        operation_type="livestream_download",
        subject_id="room-1",
        status="pending",
        message="scheduled",
        download_path="/path",
        created_at="2026-04-17T00:00:00+00:00",
        updated_at="2026-04-17T00:00:00+00:00",
    )

    result = await download_livestream(service=mock_livestream_service, user_id="u1")
    assert result.status == "pending"
    assert result.download_path == "/path"


@pytest.mark.asyncio
async def test_download_livestream_user_not_found(
    mock_livestream_service: MagicMock,
) -> None:
    from dyvine.routers.livestreams import download_livestream

    mock_livestream_service.download_stream.side_effect = UserNotFoundError("nf")

    with pytest.raises(HTTPException) as exc_info:
        await download_livestream(service=mock_livestream_service, user_id="bad")
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_download_livestream_download_error(
    mock_livestream_service: MagicMock,
) -> None:
    from dyvine.routers.livestreams import download_livestream

    mock_livestream_service.download_stream.side_effect = DownloadError("fail")

    with pytest.raises(HTTPException) as exc_info:
        await download_livestream(service=mock_livestream_service, user_id="u1")
    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_download_livestream_livestream_error(
    mock_livestream_service: MagicMock,
) -> None:
    from dyvine.routers.livestreams import download_livestream

    mock_livestream_service.download_stream.side_effect = LivestreamError("no stream")

    with pytest.raises(HTTPException) as exc_info:
        await download_livestream(service=mock_livestream_service, user_id="u1")
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_download_livestream_unexpected_error(
    mock_livestream_service: MagicMock,
) -> None:
    from dyvine.routers.livestreams import download_livestream

    mock_livestream_service.download_stream.side_effect = RuntimeError("boom")

    with pytest.raises(HTTPException) as exc_info:
        await download_livestream(service=mock_livestream_service, user_id="u1")
    assert exc_info.value.status_code == 500


# ── download_livestream_url ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_download_livestream_url_success(
    mock_livestream_service: MagicMock,
) -> None:
    from dyvine.routers.livestreams import download_livestream_url

    mock_livestream_service.download_stream.return_value = LiveStreamDownloadResponse(
        operation_id="op2",
        operation_type="livestream_download",
        subject_id="room-2",
        status="pending",
        message="scheduled",
        download_path="/p",
        created_at="2026-04-17T00:00:00+00:00",
        updated_at="2026-04-17T00:00:00+00:00",
    )
    request = LiveStreamURLDownloadRequest(url="https://live.douyin.com/123")

    result = await download_livestream_url(
        request=request, service=mock_livestream_service
    )
    assert result.status == "pending"


@pytest.mark.asyncio
async def test_download_livestream_url_livestream_error(
    mock_livestream_service: MagicMock,
) -> None:
    from dyvine.routers.livestreams import download_livestream_url

    mock_livestream_service.download_stream.side_effect = LivestreamError("no")
    request = LiveStreamURLDownloadRequest(url="https://live.douyin.com/123")

    with pytest.raises(HTTPException) as exc_info:
        await download_livestream_url(request=request, service=mock_livestream_service)
    assert exc_info.value.status_code == 404


# ── get_download_status ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_download_status_success(
    mock_livestream_service: MagicMock,
) -> None:
    from dyvine.routers.livestreams import get_download_status

    mock_livestream_service.get_download_status.return_value = (
        LiveStreamDownloadResponse(
            operation_id="op1",
            operation_type="livestream_download",
            subject_id="room-1",
            status="completed",
            message="done",
            download_path="/path/file.flv",
            progress=100.0,
            created_at="2026-04-17T00:00:00+00:00",
            updated_at="2026-04-17T00:00:01+00:00",
        )
    )

    result = await get_download_status(
        service=mock_livestream_service, operation_id="op1"
    )
    assert result.status == "completed"


@pytest.mark.asyncio
async def test_get_download_status_not_found(
    mock_livestream_service: MagicMock,
) -> None:
    from dyvine.routers.livestreams import get_download_status

    mock_livestream_service.get_download_status.side_effect = DownloadError("nf")

    with pytest.raises(HTTPException) as exc_info:
        await get_download_status(service=mock_livestream_service, operation_id="bad")
    assert exc_info.value.status_code == 404
