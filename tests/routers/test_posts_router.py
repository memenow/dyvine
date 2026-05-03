from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from dyvine.core.exceptions import (
    DownloadError,
    UserNotFoundError,
)
from dyvine.schemas.posts import (
    BulkDownloadResponse,
    DownloadStatus,
    PostDetail,
    PostType,
)


@pytest.fixture
def mock_post_service() -> MagicMock:
    svc = MagicMock()
    svc.get_post_detail = AsyncMock()
    svc.get_user_posts = AsyncMock()
    svc.start_bulk_download = AsyncMock()
    svc.get_bulk_download_status = AsyncMock()
    return svc


# ── get_post ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_post_success(mock_post_service: MagicMock) -> None:
    from dyvine.routers.posts import get_post

    detail = PostDetail(aweme_id="123", create_time=0, post_type=PostType.VIDEO)
    mock_post_service.get_post_detail.return_value = detail

    result = await get_post(service=mock_post_service, post_id="123")
    assert result.aweme_id == "123"


@pytest.mark.asyncio
async def test_get_post_not_found(mock_post_service: MagicMock) -> None:
    from dyvine.core.exceptions import PostNotFoundError
    from dyvine.routers.posts import get_post

    mock_post_service.get_post_detail.side_effect = PostNotFoundError("nf")

    with pytest.raises(HTTPException) as exc_info:
        await get_post(service=mock_post_service, post_id="bad")
    assert exc_info.value.status_code == 404


# ── list_user_posts ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_user_posts_success(mock_post_service: MagicMock) -> None:
    from dyvine.routers.posts import list_user_posts

    mock_post_service.get_user_posts.return_value = []

    result = await list_user_posts(
        service=mock_post_service, user_id="u1", max_cursor=0, count=20
    )
    assert result == []


@pytest.mark.asyncio
async def test_list_user_posts_user_not_found(
    mock_post_service: MagicMock,
) -> None:
    from dyvine.routers.posts import list_user_posts

    mock_post_service.get_user_posts.side_effect = UserNotFoundError("nf")

    with pytest.raises(HTTPException) as exc_info:
        await list_user_posts(
            service=mock_post_service, user_id="bad", max_cursor=0, count=20
        )
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_list_user_posts_unexpected_error(
    mock_post_service: MagicMock,
) -> None:
    from dyvine.routers.posts import list_user_posts

    mock_post_service.get_user_posts.side_effect = RuntimeError("boom")

    with pytest.raises(HTTPException) as exc_info:
        await list_user_posts(
            service=mock_post_service, user_id="u1", max_cursor=0, count=20
        )
    assert exc_info.value.status_code == 500


# ── download_user_posts ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_download_user_posts_returns_pending_response(
    mock_post_service: MagicMock,
) -> None:
    from dyvine.routers.posts import download_user_posts

    resp = BulkDownloadResponse(
        operation_id="op-123",
        sec_user_id="u1",
        download_path=None,
        total_posts=0,
        status=DownloadStatus.PENDING,
        message="Bulk download scheduled",
    )
    mock_post_service.start_bulk_download.return_value = resp

    result = await download_user_posts(
        service=mock_post_service, user_id="u1", max_cursor=0
    )
    assert result.status == DownloadStatus.PENDING
    assert result.operation_id == "op-123"
    mock_post_service.start_bulk_download.assert_awaited_once_with("u1", 0)


def test_download_user_posts_route_returns_202() -> None:
    """The bulk download endpoint must advertise HTTP 202 Accepted.

    A test client check guards against future regressions where the
    decorator drops the ``status_code`` argument and silently reverts to
    the default 200.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from dyvine.core.dependencies import get_post_service
    from dyvine.routers.posts import router

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    pending = BulkDownloadResponse(
        operation_id="op-202",
        sec_user_id="u1",
        download_path=None,
        total_posts=0,
        status=DownloadStatus.PENDING,
        message="Bulk download scheduled",
    )
    fake_service = MagicMock()
    fake_service.start_bulk_download = AsyncMock(return_value=pending)
    app.dependency_overrides[get_post_service] = lambda: fake_service

    with TestClient(app) as client:
        response = client.post("/api/v1/posts/users/u1/posts:download")

    assert response.status_code == 202
    payload = response.json()
    assert payload["operation_id"] == "op-202"
    assert payload["status"] == DownloadStatus.PENDING.value


@pytest.mark.asyncio
async def test_download_user_posts_user_not_found(
    mock_post_service: MagicMock,
) -> None:
    from dyvine.routers.posts import download_user_posts

    mock_post_service.start_bulk_download.side_effect = UserNotFoundError("nf")

    with pytest.raises(HTTPException) as exc_info:
        await download_user_posts(
            service=mock_post_service, user_id="bad", max_cursor=0
        )
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_download_user_posts_download_error(
    mock_post_service: MagicMock,
) -> None:
    from dyvine.routers.posts import download_user_posts

    mock_post_service.start_bulk_download.side_effect = DownloadError("fail")

    with pytest.raises(HTTPException) as exc_info:
        await download_user_posts(service=mock_post_service, user_id="u1", max_cursor=0)
    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_download_user_posts_unexpected_error(
    mock_post_service: MagicMock,
) -> None:
    from dyvine.routers.posts import download_user_posts

    mock_post_service.start_bulk_download.side_effect = RuntimeError("boom")

    with pytest.raises(HTTPException) as exc_info:
        await download_user_posts(service=mock_post_service, user_id="u1", max_cursor=0)
    assert exc_info.value.status_code == 500


# ── get_bulk_download_operation ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_bulk_download_operation_success(
    mock_post_service: MagicMock,
) -> None:
    from dyvine.routers.posts import get_bulk_download_operation

    resp = BulkDownloadResponse(
        operation_id="op-1",
        sec_user_id="u1",
        download_path="/dl",
        total_posts=5,
        total_downloaded=5,
        status=DownloadStatus.SUCCESS,
        message="done",
    )
    mock_post_service.get_bulk_download_status.return_value = resp

    result = await get_bulk_download_operation(
        service=mock_post_service, operation_id="op-1"
    )
    assert result.operation_id == "op-1"
    assert result.status == DownloadStatus.SUCCESS


@pytest.mark.asyncio
async def test_get_bulk_download_operation_not_found(
    mock_post_service: MagicMock,
) -> None:
    from dyvine.routers.posts import get_bulk_download_operation

    mock_post_service.get_bulk_download_status.side_effect = DownloadError("nf")

    with pytest.raises(HTTPException) as exc_info:
        await get_bulk_download_operation(
            service=mock_post_service, operation_id="missing"
        )
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_get_bulk_download_operation_unexpected_error(
    mock_post_service: MagicMock,
) -> None:
    from dyvine.routers.posts import get_bulk_download_operation

    mock_post_service.get_bulk_download_status.side_effect = RuntimeError("boom")

    with pytest.raises(HTTPException) as exc_info:
        await get_bulk_download_operation(
            service=mock_post_service, operation_id="op-1"
        )
    assert exc_info.value.status_code == 500
