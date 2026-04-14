from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from dyvine.core.exceptions import (
    DownloadError,
    NotFoundError,
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
    svc.download_all_user_posts = AsyncMock()
    return svc


# ── get_post ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_post_success(mock_post_service: MagicMock) -> None:
    from dyvine.routers.posts import get_post

    detail = PostDetail(
        aweme_id="123", create_time=0, post_type=PostType.VIDEO
    )
    mock_post_service.get_post_detail.return_value = detail

    result = await get_post(service=mock_post_service, post_id="123")
    assert result.aweme_id == "123"


@pytest.mark.asyncio
async def test_get_post_not_found(mock_post_service: MagicMock) -> None:
    from dyvine.routers.posts import get_post

    # handle_errors uses exact type matching; NotFoundError maps to 404
    mock_post_service.get_post_detail.side_effect = NotFoundError("nf")

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
async def test_download_user_posts_success(
    mock_post_service: MagicMock,
) -> None:
    from dyvine.routers.posts import download_user_posts

    resp = BulkDownloadResponse(
        sec_user_id="u1",
        download_path="/dl",
        total_posts=5,
        status=DownloadStatus.SUCCESS,
    )
    mock_post_service.download_all_user_posts.return_value = resp

    result = await download_user_posts(
        service=mock_post_service, user_id="u1", max_cursor=0
    )
    assert result.status == DownloadStatus.SUCCESS


@pytest.mark.asyncio
async def test_download_user_posts_user_not_found(
    mock_post_service: MagicMock,
) -> None:
    from dyvine.routers.posts import download_user_posts

    mock_post_service.download_all_user_posts.side_effect = UserNotFoundError("nf")

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

    mock_post_service.download_all_user_posts.side_effect = DownloadError("fail")

    with pytest.raises(HTTPException) as exc_info:
        await download_user_posts(
            service=mock_post_service, user_id="u1", max_cursor=0
        )
    assert exc_info.value.status_code == 500
