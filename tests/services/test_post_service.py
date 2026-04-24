from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from dyvine.core.exceptions import PostNotFoundError, ServiceError
from dyvine.schemas.posts import DownloadStatus, PostType
from dyvine.services.posts import PostService


def _build_service(handler: MagicMock | None = None) -> PostService:
    """Create PostService without calling __init__ (avoids DouyinHandler)."""
    svc = object.__new__(PostService)  # type: ignore[return-value]
    if handler is not None:
        svc.handler = handler  # type: ignore[attr-defined]
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


# ── download_all_user_posts (async, mocked) ─────────────────────────────


@pytest.mark.asyncio
async def test_download_all_user_posts_user_not_found() -> None:
    from dyvine.core.exceptions import UserNotFoundError

    handler = MagicMock()
    handler.fetch_user_profile = AsyncMock(return_value=None)

    svc = _build_service(handler)
    with pytest.raises(UserNotFoundError):
        await svc.download_all_user_posts("missing-user")


@pytest.mark.asyncio
async def test_download_all_user_posts_no_posts() -> None:
    handler = MagicMock()
    profile = MagicMock()
    profile.aweme_count = 0
    profile.nickname = "EmptyUser"
    handler.fetch_user_profile = AsyncMock(return_value=profile)

    # Mock the db context manager
    from pathlib import Path
    from unittest.mock import patch

    with patch("dyvine.services.posts.AsyncUserDB") as mock_db:
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_db.return_value = mock_ctx
        handler.get_or_add_user_data = AsyncMock(return_value=Path("/tmp/user"))

        # No posts to fetch
        svc = _build_service(handler)
        mock_fetch = AsyncMock(return_value={})
        with patch.object(svc, "_fetch_posts_batch", new=mock_fetch):
            svc.handler = handler
            result = await svc.download_all_user_posts("u1")
            assert result.total_downloaded == 0
            # total_downloaded == total_posts (both 0) → SUCCESS
            assert result.status == DownloadStatus.SUCCESS


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
