"""Tests for PostService incremental (new-post) download logic."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fake_repos import FakeOperationRepository

from dyvine.core.exceptions import (
    OperationNotFoundError,
    ServiceError,
    UserNotFoundError,
)
from dyvine.services import posts as posts_module
from dyvine.services.posts import PostService


def _make_service() -> PostService:
    """Build a PostService with a mock handler and isolated store."""
    handler = MagicMock()
    handler.kwargs = {"mode": "all"}
    return PostService(handler=handler, operation_store=FakeOperationRepository())


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


async def test_download_new_posts_uses_caller_persisted_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _make_service()
    service.handler.fetch_user_profile = AsyncMock(
        return_value=MagicMock(nickname="someone")
    )
    service.handler.get_or_add_user_data = AsyncMock(return_value=Path("/tmp/u"))
    monkeypatch.setattr(posts_module, "AsyncUserDB", _FakeUserDB)
    monkeypatch.setattr(
        posts_module, "relative_to_download_root", lambda _path: "users/user01"
    )
    service._collect_new_posts = AsyncMock(return_value=(["post-1"], 0, False))  # type: ignore[method-assign]
    saved = await service.operation_store.create_operation(
        operation_type="user_posts_incremental_download",
        subject_id="user01",
        status="pending",
        message="scheduled",
    )
    result = await service.download_new_posts("user01", operation_id=saved.operation_id)
    assert result.operation_id == saved.operation_id
    assert (await service.operation_store.get_operation(saved.operation_id)).status == (
        "completed"
    )


async def test_download_new_posts_rejects_wrong_persisted_operation() -> None:
    service = _make_service()
    saved = await service.operation_store.create_operation(
        operation_type="user_posts_incremental_download",
        subject_id="someone-else",
        status="pending",
        message="scheduled",
    )
    with pytest.raises(ServiceError, match="does not match"):
        await service.download_new_posts("user01", operation_id=saved.operation_id)
    service.handler.fetch_user_profile.assert_not_called()


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


_CUTOFF = datetime(2026, 9, 6, 8, 0, 0)


def _paged(pages: list[list[dict[str, object]]]) -> AsyncMock:
    """Serve ``pages`` newest-first, advertising more until the last one."""

    async def fetch(_uid: str, cursor: int) -> dict[str, object]:
        index = cursor // 1000
        return {
            "aweme_list": pages[index],
            "has_more": index + 1 < len(pages),
            "max_cursor": (index + 1) * 1000,
        }

    return AsyncMock(side_effect=fetch)


def _post(aweme_id: str, created: object) -> dict[str, object]:
    return {"aweme_id": aweme_id, "create_time": created}


async def test_collect_new_posts_stops_after_a_page_older_than_the_cutoff() -> None:
    """A cutoff-bounded scan fetches no page past the first all-old page."""
    service = _make_service()
    service._fetch_posts_batch = _paged(  # type: ignore[method-assign]
        [
            [_post("new", "2026-09-10 12-00-00"), _post("old", "2026-09-01 12-00-00")],
            [_post("older", "2026-08-20 12-00-00")],
            [_post("never", "2026-08-01 12-00-00")],
        ]
    )
    service._download_post_content = AsyncMock()  # type: ignore[method-assign]

    new_ids, failed, truncated = await service._collect_new_posts(
        "user01", Path("/tmp/x"), known=set(), since_aweme_id=None, posted_after=_CUTOFF
    )

    assert (new_ids, failed, truncated) == (["new"], 0, False)
    assert service._fetch_posts_batch.await_count == 2
    assert service._download_post_content.await_count == 1


async def test_collect_new_posts_scans_past_an_old_pinned_post() -> None:
    """An old pinned post at the top does not end the scan early."""
    service = _make_service()
    service._fetch_posts_batch = _paged(  # type: ignore[method-assign]
        [
            [
                _post("pinned-old", "2025-01-01 00-00-00"),
                _post("at-cutoff", "2026-09-06 08-00-00"),
                _post("newest", "2026-09-10 12-00-00"),
            ],
            [_post("new", "2026-09-07 09-00-00")],
            [_post("old", "2026-09-01 12-00-00")],
        ]
    )
    service._download_post_content = AsyncMock()  # type: ignore[method-assign]

    new_ids, _failed, _truncated = await service._collect_new_posts(
        "user01", Path("/tmp/x"), known=set(), since_aweme_id=None, posted_after=_CUTOFF
    )

    assert new_ids == ["newest", "new"]
    assert service._fetch_posts_batch.await_count == 3


async def test_collect_new_posts_reads_epoch_times_and_skips_unreadable_ones() -> None:
    """Epoch times use f2's UTC+8 naming zone; unreadable times are skipped."""
    service = _make_service()
    # 2026-09-06 08:00:01 UTC+8 is 2026-09-06 00:00:01 UTC.
    after_cutoff = int(datetime.fromisoformat("2026-09-06T00:00:01+00:00").timestamp())
    at_cutoff = after_cutoff - 1
    service._fetch_posts_batch = _paged(  # type: ignore[method-assign]
        [
            [
                _post("epoch-new", after_cutoff),
                _post("epoch-at", at_cutoff),
                _post("undated", None),
            ]
        ]
    )
    service._download_post_content = AsyncMock()  # type: ignore[method-assign]

    new_ids, _failed, _truncated = await service._collect_new_posts(
        "user01", Path("/tmp/x"), known=set(), since_aweme_id=None, posted_after=_CUTOFF
    )

    assert new_ids == ["epoch-new"]


async def test_download_new_posts_records_the_cutoff_it_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _make_service()
    service.handler.fetch_user_profile = AsyncMock(
        return_value=MagicMock(nickname="someone")
    )
    service.handler.get_or_add_user_data = AsyncMock(return_value=Path("/tmp/u"))
    monkeypatch.setattr(posts_module, "AsyncUserDB", _FakeUserDB)
    monkeypatch.setattr(
        posts_module, "relative_to_download_root", lambda _path: "users/user01"
    )
    service._collect_new_posts = AsyncMock(return_value=([], 0, False))  # type: ignore[method-assign]

    result = await service.download_new_posts("user01", posted_after=_CUTOFF)

    assert service._collect_new_posts.await_args.kwargs["posted_after"] == _CUTOFF
    operation = await service.operation_store.get_operation(result.operation_id)
    assert operation.metadata["posted_after"] == "2026-09-06T08:00:00"
