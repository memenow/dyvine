"""Tests for PostService incremental (new-post) download logic."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from dyvine.core.exceptions import UserNotFoundError
from dyvine.core.operations import OperationStore
from dyvine.services.posts import PostService


def _make_service() -> PostService:
    """Build a PostService with a mock handler and isolated store."""
    handler = MagicMock()
    handler.kwargs = {"mode": "all"}
    return PostService(handler=handler, operation_store=OperationStore())


async def test_collect_new_posts_stops_at_known_id() -> None:
    """Pagination downloads only the strictly-new prefix before a known id."""
    service = _make_service()
    batch = {
        "aweme_list": [
            {"aweme_id": "3"},
            {"aweme_id": "2"},
            {"aweme_id": "1"},  # already known -> stop here
        ],
        "has_more": True,
        "max_cursor": 999,
    }
    service._fetch_posts_batch = AsyncMock(return_value=batch)  # type: ignore[method-assign]
    service._download_post_content = AsyncMock()  # type: ignore[method-assign]

    new_ids, failed = await service._collect_new_posts(
        "user01", Path("/tmp/x"), known={"1"}, since_aweme_id=None
    )

    assert new_ids == ["3", "2"]
    assert failed == 0
    assert service._download_post_content.await_count == 2


async def test_collect_new_posts_counts_failures() -> None:
    """A per-post download failure is counted, not fatal."""
    service = _make_service()
    batch = {
        "aweme_list": [{"aweme_id": "3"}, {"aweme_id": "2"}],
        "has_more": False,
        "max_cursor": 0,
    }
    service._fetch_posts_batch = AsyncMock(return_value=batch)  # type: ignore[method-assign]
    service._download_post_content = AsyncMock(  # type: ignore[method-assign]
        side_effect=[None, RuntimeError("boom")]
    )

    new_ids, failed = await service._collect_new_posts(
        "user01", Path("/tmp/x"), known=set(), since_aweme_id=None
    )

    assert new_ids == ["3"]
    assert failed == 1


async def test_collect_new_posts_stops_on_empty_batch() -> None:
    """An empty upstream batch ends pagination."""
    service = _make_service()
    service._fetch_posts_batch = AsyncMock(return_value={})  # type: ignore[method-assign]
    service._download_post_content = AsyncMock()  # type: ignore[method-assign]

    new_ids, failed = await service._collect_new_posts(
        "user01", Path("/tmp/x"), known=set(), since_aweme_id=None
    )

    assert new_ids == []
    assert failed == 0
    service._download_post_content.assert_not_awaited()


async def test_download_new_posts_rejects_unknown_user() -> None:
    """An unknown profile raises before any operation work begins."""
    service = _make_service()
    service.handler.fetch_user_profile = AsyncMock(return_value=None)

    with pytest.raises(UserNotFoundError):
        await service.download_new_posts("user01", known_aweme_ids=set())
