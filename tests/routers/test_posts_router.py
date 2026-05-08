from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from dyvine.core.exceptions import (
    OperationNotFoundError,
    PostNotFoundError,
    ServiceError,
    UserNotFoundError,
)
from dyvine.schemas.posts import (
    BulkDownloadResponse,
    DownloadStatus,
    ListPostsResponse,
    PostDetail,
    PostType,
)
from dyvine.services.posts import UserPostsPage


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

    detail = PostDetail(aweme_id="1234567890", create_time=0, post_type=PostType.VIDEO)
    mock_post_service.get_post_detail.return_value = detail

    result = await get_post(service=mock_post_service, post_id="1234567890")
    assert result.aweme_id == "1234567890"


@pytest.mark.asyncio
async def test_get_post_not_found(mock_post_service: MagicMock) -> None:
    from dyvine.routers.posts import get_post

    mock_post_service.get_post_detail.side_effect = PostNotFoundError("nf")

    with pytest.raises(HTTPException) as exc_info:
        await get_post(service=mock_post_service, post_id="1234567890")
    assert exc_info.value.status_code == 404


# ── list_user_posts ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_user_posts_success(mock_post_service: MagicMock) -> None:
    from dyvine.routers.posts import list_user_posts

    mock_post_service.get_user_posts.return_value = UserPostsPage(
        posts=[], next_cursor=None, has_more=False
    )

    result = await list_user_posts(
        service=mock_post_service, user_id="user01", page_token=None, count=20
    )
    assert isinstance(result, ListPostsResponse)
    assert result.posts == []
    assert result.next_page_token is None


@pytest.mark.asyncio
async def test_list_user_posts_paginates_with_upstream_cursor(
    mock_post_service: MagicMock,
) -> None:
    """The next-page token must echo the upstream Douyin ``max_cursor``.

    Earlier revisions synthesised the token from ``cursor + len(posts)``,
    which is not a valid Douyin cursor — the upstream API ignored it
    and the client either re-fetched the same window or skipped pages.
    The router now base64-encodes whatever ``max_cursor`` the service
    surfaced.
    """
    from dyvine.routers.posts import (
        _decode_page_token,
        list_user_posts,
    )

    detail = PostDetail(aweme_id="9876543210", create_time=0, post_type=PostType.VIDEO)
    mock_post_service.get_user_posts.return_value = UserPostsPage(
        posts=[detail], next_cursor=12345, has_more=True
    )

    first = await list_user_posts(
        service=mock_post_service, user_id="user01", page_token=None, count=20
    )
    assert first.posts == [detail]
    assert first.next_page_token is not None
    assert _decode_page_token(first.next_page_token) == 12345

    # The follow-up call hands the decoded upstream cursor back to the
    # service untouched.
    mock_post_service.get_user_posts.return_value = UserPostsPage(
        posts=[], next_cursor=None, has_more=False
    )
    second = await list_user_posts(
        service=mock_post_service,
        user_id="user01",
        page_token=first.next_page_token,
        count=20,
    )
    second_call_cursor = mock_post_service.get_user_posts.await_args_list[-1].args[1]
    assert second_call_cursor == 12345
    assert isinstance(second, ListPostsResponse)
    assert second.next_page_token is None


@pytest.mark.asyncio
async def test_list_user_posts_omits_token_when_feed_exhausted(
    mock_post_service: MagicMock,
) -> None:
    """When the service reports no further cursor, the response omits the token."""
    from dyvine.routers.posts import list_user_posts

    detail = PostDetail(aweme_id="1111111111", create_time=0, post_type=PostType.VIDEO)
    mock_post_service.get_user_posts.return_value = UserPostsPage(
        posts=[detail], next_cursor=None, has_more=False
    )

    result = await list_user_posts(
        service=mock_post_service, user_id="user01", page_token=None, count=20
    )
    assert result.posts == [detail]
    assert result.next_page_token is None


@pytest.mark.asyncio
async def test_list_user_posts_user_not_found(
    mock_post_service: MagicMock,
) -> None:
    from dyvine.routers.posts import list_user_posts

    mock_post_service.get_user_posts.side_effect = UserNotFoundError("nf")

    with pytest.raises(HTTPException) as exc_info:
        await list_user_posts(
            service=mock_post_service, user_id="user01", page_token=None, count=20
        )
    assert exc_info.value.status_code == 404


# ── download_user_posts ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_download_user_posts_returns_pending_response(
    mock_post_service: MagicMock,
) -> None:
    from dyvine.routers.posts import download_user_posts

    resp = BulkDownloadResponse(
        operation_id="op-12345",
        sec_user_id="user01",
        download_path=None,
        total_posts=0,
        status=DownloadStatus.PENDING,
        message="Bulk download scheduled",
    )
    mock_post_service.start_bulk_download.return_value = resp

    result = await download_user_posts(
        service=mock_post_service, user_id="user01", page_token=None
    )
    assert result.status == DownloadStatus.PENDING
    assert result.operation_id == "op-12345"
    mock_post_service.start_bulk_download.assert_awaited_once_with("user01", 0)


def test_download_user_posts_route_returns_202() -> None:
    """The bulk download endpoint must advertise HTTP 202 Accepted."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from dyvine.core.dependencies import get_post_service
    from dyvine.routers.posts import router

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    pending = BulkDownloadResponse(
        operation_id="op-202",
        sec_user_id="user01",
        download_path=None,
        total_posts=0,
        status=DownloadStatus.PENDING,
        message="Bulk download scheduled",
    )
    fake_service = MagicMock()
    fake_service.start_bulk_download = AsyncMock(return_value=pending)
    app.dependency_overrides[get_post_service] = lambda: fake_service

    with TestClient(app) as client:
        response = client.post("/api/v1/posts/users/user01/posts:download")

    assert response.status_code == 202
    payload = response.json()
    assert payload["operation_id"] == "op-202"
    assert payload["status"] == DownloadStatus.PENDING.value


def test_decode_page_token_round_trips() -> None:
    from dyvine.routers.posts import _decode_page_token, _encode_page_token

    token = _encode_page_token(987)
    assert _decode_page_token(token) == 987


def test_decode_page_token_falls_back_to_zero_for_invalid_input() -> None:
    """Garbage tokens must not surface as a 5xx; the cursor restarts."""
    from dyvine.routers.posts import _decode_page_token

    assert _decode_page_token(None) == 0
    assert _decode_page_token("") == 0
    assert _decode_page_token("!!!not-base64!!!") == 0
    # Valid base64 that decodes to non-numeric ASCII also resets.
    assert _decode_page_token("YWJj") == 0  # base64 for "abc"


def test_router_registers_operation_route_before_post_id() -> None:
    """``/operations/{id}`` must take precedence over ``/{post_id}``.

    The previous router registration order made ``GET /operations/{id}``
    unreachable because ``/{post_id}`` matched first; this check guards
    against a future revert.
    """
    from dyvine.routers.posts import router

    paths_in_order = [route.path for route in router.routes]
    assert paths_in_order.index(
        "/posts/operations/{operation_id}"
    ) < paths_in_order.index("/posts/{post_id}")


@pytest.mark.asyncio
async def test_download_user_posts_user_not_found(
    mock_post_service: MagicMock,
) -> None:
    from dyvine.routers.posts import download_user_posts

    mock_post_service.start_bulk_download.side_effect = UserNotFoundError("nf")

    with pytest.raises(HTTPException) as exc_info:
        await download_user_posts(
            service=mock_post_service, user_id="user01", page_token=None
        )
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_download_user_posts_service_error_maps_to_5xx(
    mock_post_service: MagicMock,
) -> None:
    """``ServiceError`` from the service layer maps to 5xx with sanitised body."""
    from dyvine.routers.posts import download_user_posts

    mock_post_service.start_bulk_download.side_effect = ServiceError(
        "upstream profile fetch timed out"
    )

    with pytest.raises(HTTPException) as exc_info:
        await download_user_posts(
            service=mock_post_service, user_id="user01", page_token=None
        )
    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_download_user_posts_unexpected_error_returns_500(
    mock_post_service: MagicMock,
) -> None:
    from dyvine.routers.posts import download_user_posts

    mock_post_service.start_bulk_download.side_effect = RuntimeError("boom")

    with pytest.raises(HTTPException) as exc_info:
        await download_user_posts(
            service=mock_post_service, user_id="user01", page_token=None
        )
    assert exc_info.value.status_code == 500


# ── get_bulk_download_operation ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_bulk_download_operation_success(
    mock_post_service: MagicMock,
) -> None:
    from dyvine.routers.posts import get_bulk_download_operation

    resp = BulkDownloadResponse(
        operation_id="op-12345",
        sec_user_id="user01",
        download_path="/dl",
        total_posts=5,
        total_downloaded=5,
        status=DownloadStatus.COMPLETED,
        message="done",
    )
    mock_post_service.get_bulk_download_status.return_value = resp

    result = await get_bulk_download_operation(
        service=mock_post_service, operation_id="op-12345"
    )
    assert result.operation_id == "op-12345"
    assert result.status == DownloadStatus.COMPLETED


@pytest.mark.asyncio
async def test_get_bulk_download_operation_not_found(
    mock_post_service: MagicMock,
) -> None:
    from dyvine.routers.posts import get_bulk_download_operation

    mock_post_service.get_bulk_download_status.side_effect = OperationNotFoundError(
        "nf"
    )

    with pytest.raises(HTTPException) as exc_info:
        await get_bulk_download_operation(
            service=mock_post_service, operation_id="missing-op"
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
            service=mock_post_service, operation_id="op-12345"
        )
    assert exc_info.value.status_code == 500
