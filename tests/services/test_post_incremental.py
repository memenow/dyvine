"""Tests for PostService incremental (new-post) download logic."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from dyvine.core.exceptions import OperationNotFoundError, UserNotFoundError
from dyvine.core.operations import OperationStore
from dyvine.services import posts as posts_module
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

    new_ids, failed, truncated = await service._collect_new_posts(
        "user01", Path("/tmp/x"), known={"1"}, since_aweme_id=None
    )

    assert new_ids == ["3", "2"]
    assert failed == 0
    assert truncated is False
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

    new_ids, failed, truncated = await service._collect_new_posts(
        "user01", Path("/tmp/x"), known=set(), since_aweme_id=None
    )

    assert new_ids == ["3"]
    assert failed == 1
    assert truncated is False


async def test_collect_new_posts_stops_on_empty_batch() -> None:
    """An empty upstream batch ends pagination."""
    service = _make_service()
    service._fetch_posts_batch = AsyncMock(return_value={})  # type: ignore[method-assign]
    service._download_post_content = AsyncMock()  # type: ignore[method-assign]

    new_ids, failed, truncated = await service._collect_new_posts(
        "user01", Path("/tmp/x"), known=set(), since_aweme_id=None
    )

    assert new_ids == []
    assert failed == 0
    assert truncated is False
    service._download_post_content.assert_not_awaited()


async def test_download_new_posts_rejects_unknown_user() -> None:
    """An unknown profile raises before any operation work begins."""
    service = _make_service()
    service.handler.fetch_user_profile = AsyncMock(return_value=None)

    with pytest.raises(UserNotFoundError):
        await service.download_new_posts("user01", known_aweme_ids=set())


class _FakeUserDB:
    """Minimal async-context stand-in for f2's AsyncUserDB in tests."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *args: object) -> bool:
        return False


async def test_download_new_posts_marks_operation_failed_on_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation mid-download marks the op terminal, not left as 'running'."""
    service = _make_service()
    service.handler.fetch_user_profile = AsyncMock(
        return_value=MagicMock(nickname="someone")
    )
    service.handler.get_or_add_user_data = AsyncMock(return_value=Path("/tmp/u"))
    monkeypatch.setattr(posts_module, "AsyncUserDB", _FakeUserDB)
    monkeypatch.setattr(
        posts_module, "relative_to_download_root", lambda _path: "users/user01"
    )
    service._collect_new_posts = AsyncMock(  # type: ignore[method-assign]
        side_effect=asyncio.CancelledError()
    )

    captured: dict[str, object] = {}
    real_update = service.operation_store.update_operation

    async def _spy_update(operation_id: str, **kwargs: object) -> object:
        captured.update(kwargs)
        return await real_update(operation_id, **kwargs)

    monkeypatch.setattr(service.operation_store, "update_operation", _spy_update)

    with pytest.raises(asyncio.CancelledError):
        await service.download_new_posts("user01", known_aweme_ids=set())

    assert captured.get("status") == "failed"
    assert "cancelled" in str(captured.get("message", "")).lower()


async def test_collect_new_posts_warns_on_max_page_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hitting the page cap logs a warning rather than truncating silently."""
    service = _make_service()
    monkeypatch.setattr(posts_module, "MAX_PAGES_FALLBACK", 3)
    counter = {"n": 0}

    async def _always_more(_uid: str, _cursor: int) -> dict[str, object]:
        counter["n"] += 1
        return {
            "aweme_list": [{"aweme_id": f"id-{counter['n']}"}],
            "has_more": True,
            "max_cursor": counter["n"] * 1000,
        }

    service._fetch_posts_batch = _always_more  # type: ignore[method-assign]
    service._download_post_content = AsyncMock()  # type: ignore[method-assign]

    warnings: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(
        posts_module.logger, "warning", lambda *a, **k: warnings.append((a, k))
    )

    new_ids, failed, truncated = await service._collect_new_posts(
        "user01", Path("/tmp/x"), known=set(), since_aweme_id=None
    )

    assert len(new_ids) == 3  # exactly MAX_PAGES_FALLBACK pages, one post each
    assert failed == 0
    assert truncated is True
    assert warnings, "expected a max-page-fallback warning"
    assert "max page fallback" in str(warnings[0][0][0]).lower()


async def test_get_bulk_download_status_accepts_incremental_op() -> None:
    """Watch-created incremental operations are observable via the status route."""
    service = _make_service()
    op = await service.operation_store.create_operation(
        operation_type="user_posts_incremental_download",
        subject_id="user01",
        status="completed",
        message="Downloaded 2 new post(s)",
        total_items=2,
        completed_items=2,
        metadata={"failed_count": 0, "new_count": 2},
    )

    resp = await service.get_bulk_download_status(op.operation_id)

    assert resp.operation_id == op.operation_id
    assert resp.sec_user_id == "user01"
    assert resp.total_posts == 2
    assert resp.total_downloaded == 2


async def test_get_bulk_download_status_rejects_unrelated_op() -> None:
    """An unrelated operation type still surfaces as not-found."""
    service = _make_service()
    op = await service.operation_store.create_operation(
        operation_type="user_content_download",
        subject_id="user01",
        status="completed",
        message="done",
    )

    with pytest.raises(OperationNotFoundError):
        await service.get_bulk_download_status(op.operation_id)
