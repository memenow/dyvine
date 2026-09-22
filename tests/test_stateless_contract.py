"""Contract: read-only service paths never touch the operation stores.

Each service below is built without an ``operation_store`` attribute,
so any store access raises ``AttributeError`` and fails the test. This
pins the plugin idle shape: read-only lookups must work with upstream
data alone, and only real task execution may open database connections.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from dyvine.services.livestreams import LivestreamService
from dyvine.services.posts import PostService
from dyvine.services.users import UserService


@pytest.mark.asyncio
async def test_post_detail_needs_no_store() -> None:
    """Post detail reads flow through the handler only."""
    handler = MagicMock()
    post_mock = MagicMock()
    post_mock._to_dict.return_value = {
        "aweme_id": "789",
        "desc": "test post",
        "create_time": "2024-01-15 10-30-00",
        "video": {
            "play_addr": {"url_list": ["https://example.com/v.mp4"]},
        },
        "statistics": {},
    }
    handler.fetch_one_video = AsyncMock(return_value=post_mock)

    svc = object.__new__(PostService)
    svc.handler = handler  # type: ignore[attr-defined]
    assert not hasattr(svc, "operation_store")
    result = await svc.get_post_detail("789")
    assert result.aweme_id == "789"


@pytest.mark.asyncio
async def test_user_posts_needs_no_store() -> None:
    """User post listing reads flow through the handler only."""
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
        "max_cursor": 0,
    }

    async def _iter(*args: Any, **kwargs: Any) -> Any:
        yield posts_filter

    handler.fetch_user_post_videos = _iter

    svc = object.__new__(PostService)
    svc.handler = handler  # type: ignore[attr-defined]
    assert not hasattr(svc, "operation_store")
    page = await svc.get_user_posts("user1")
    assert len(page.posts) == 1
    assert page.posts[0].aweme_id == "p1"


@pytest.mark.asyncio
async def test_user_info_needs_no_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """User profile reads flow through the handler only."""
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
        """Test double standing in for the f2 DouyinHandler."""

        def __init__(self, kwargs: dict[str, Any]) -> None:
            pass

        fetch_user_profile = AsyncMock(return_value=mock_user_data)

    monkeypatch.setattr(users_mod, "DouyinHandler", FakeHandler)

    svc = object.__new__(UserService)
    assert not hasattr(svc, "operation_store")
    result = await svc.get_user_info("test-user")
    assert result.nickname == "TestUser"


@pytest.mark.asyncio
async def test_room_info_needs_no_store() -> None:
    """Livestream metadata reads flow through the handler only."""
    live_filter = MagicMock()
    live_filter._to_dict.return_value = {"room_id": "room-1"}
    live_filter.live_status = 2
    live_filter.room_id = "room-1"
    live_filter.m3u8_pull_url = {"HD1": "https://example.com/hls.m3u8"}

    async def _load_live_filter(*args: Any, **kwargs: Any) -> MagicMock:
        return live_filter

    svc = object.__new__(LivestreamService)
    svc._load_live_filter = _load_live_filter  # type: ignore[method-assign]
    assert not hasattr(svc, "operation_store")
    info = await svc.get_room_info("webcast-1")
    assert info["status"] == 2
    assert info["room_id"] == "room-1"
