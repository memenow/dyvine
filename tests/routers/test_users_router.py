"""Tests for user router endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from dyvine.core.exceptions import OperationNotFoundError
from dyvine.schemas.users import DownloadResponse, UserResponse
from dyvine.services.users import UserNotFoundError


@pytest.fixture
def mock_user_service() -> MagicMock:
    """Test helper for this module."""
    svc = MagicMock()
    svc.get_user_info = AsyncMock()
    svc.start_download = AsyncMock()
    svc.get_download_status = AsyncMock()
    return svc


def _user_response() -> UserResponse:
    """Test helper for this module."""
    return UserResponse(
        user_id="u1",
        nickname="nick",
        avatar_url="https://example.com/a.jpg",
        following_count=0,
        follower_count=0,
        total_favorited=0,
    )


def _download_response() -> DownloadResponse:
    """Test helper for this module."""
    return DownloadResponse(
        operation_id="t1",
        operation_type="user_content_download",
        subject_id="u1",
        status="pending",
        message="ok",
        created_at="2026-04-17T00:00:00+00:00",
        updated_at="2026-04-17T00:00:00+00:00",
    )


# ── get_user ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_user_success(mock_user_service: MagicMock) -> None:
    """Verify get user success."""
    from dyvine.routers.users import get_user

    mock_user_service.get_user_info.return_value = _user_response()
    result = await get_user(user_id="u1", service=mock_user_service)
    assert result.user_id == "u1"


@pytest.mark.asyncio
async def test_get_user_not_found(mock_user_service: MagicMock) -> None:
    """Verify get user not found."""
    from dyvine.routers.users import get_user

    mock_user_service.get_user_info.side_effect = UserNotFoundError("nf")

    with pytest.raises(HTTPException) as exc_info:
        await get_user(user_id="bad", service=mock_user_service)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_get_user_unexpected_error(mock_user_service: MagicMock) -> None:
    """Verify get user unexpected error."""
    from dyvine.routers.users import get_user

    mock_user_service.get_user_info.side_effect = RuntimeError("boom")

    with pytest.raises(HTTPException) as exc_info:
        await get_user(user_id="u1", service=mock_user_service)
    assert exc_info.value.status_code == 500


# ── download_user_content ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_download_user_content_success(
    mock_user_service: MagicMock,
) -> None:
    """Verify download user content success."""
    from dyvine.routers.users import download_user_content

    mock_user_service.start_download.return_value = _download_response()
    result = await download_user_content(user_id="u1", service=mock_user_service)
    assert result.operation_id == "t1"


@pytest.mark.asyncio
async def test_download_user_content_not_found(
    mock_user_service: MagicMock,
) -> None:
    """Verify download user content not found."""
    from dyvine.routers.users import download_user_content

    mock_user_service.start_download.side_effect = UserNotFoundError("nf")

    with pytest.raises(HTTPException) as exc_info:
        await download_user_content(user_id="bad", service=mock_user_service)
    assert exc_info.value.status_code == 404


# ── get_operation ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_operation_success(mock_user_service: MagicMock) -> None:
    """Verify get operation success."""
    from dyvine.routers.users import get_operation

    mock_user_service.get_download_status.return_value = _download_response()
    result = await get_operation(operation_id="op1", service=mock_user_service)
    assert result.status == "pending"


@pytest.mark.asyncio
async def test_get_operation_not_found(mock_user_service: MagicMock) -> None:
    """Verify get operation not found."""
    from dyvine.routers.users import get_operation

    mock_user_service.get_download_status.side_effect = OperationNotFoundError("nf")

    with pytest.raises(HTTPException) as exc_info:
        await get_operation(operation_id="missing-op", service=mock_user_service)
    assert exc_info.value.status_code == 404
