"""Tests for the WatchService scheduler, checkpointing, and lifecycle."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fake_repos import FakeWatchRepository

from dyvine.core.exceptions import LivestreamError, WatchSubscriptionNotFoundError
from dyvine.core.settings import settings
from dyvine.db import WatchSubscriptionRecord
from dyvine.schemas.posts import PostDetail, PostType
from dyvine.services.posts import IncrementalDownloadResult, UserPostsPage
from dyvine.services.watch import WatchService


def _make_service(tmp_path: Path) -> tuple[WatchService, MagicMock, MagicMock]:
    """Build a WatchService over a real store with mocked sub-services."""
    store = FakeWatchRepository()
    livestream = MagicMock()
    livestream.download_stream = AsyncMock()
    post = MagicMock()
    post.download_new_posts = AsyncMock()
    post.get_user_posts = AsyncMock()
    service = WatchService(
        watch_store=store,
        livestream_service=livestream,
        post_service=post,
    )
    return service, livestream, post


def test_merge_recent_dedups_and_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Newly seen ids go first, duplicates drop, and the cap is honoured."""
    service = object.__new__(WatchService)
    service.settings = settings
    monkeypatch.setattr(settings.watch, "recent_id_cap", 3)
    merged = service._merge_recent(["a", "b"], ["b", "c", "d"])
    assert merged == ["a", "b", "c"]


async def test_create_subscription_is_idempotent(tmp_path: Path) -> None:
    """A second create for the same user returns the existing record."""
    service, _, _ = _make_service(tmp_path)
    service._start_loop = MagicMock()  # type: ignore[method-assign]

    rec1, created1 = await service.create_subscription(
        user_id="user01", backfill_on_create=True
    )
    rec2, created2 = await service.create_subscription(
        user_id="user01", backfill_on_create=True
    )

    assert created1 is True
    assert created2 is False
    assert rec1.subscription_id == rec2.subscription_id
    assert len(await service.list_subscriptions()) == 1


async def test_create_subscription_enforces_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exceeding max_subscriptions raises RateLimitError."""
    from dyvine.core.exceptions import RateLimitError

    service, _, _ = _make_service(tmp_path)
    service._start_loop = MagicMock()  # type: ignore[method-assign]
    monkeypatch.setattr(settings.watch, "max_subscriptions", 1)

    await service.create_subscription(user_id="user01", backfill_on_create=True)
    with pytest.raises(RateLimitError):
        await service.create_subscription(user_id="user02", backfill_on_create=True)


async def test_create_subscription_backfill_false_snapshots_baseline(
    tmp_path: Path,
) -> None:
    """A non-backfill subscription seeds its checkpoint from the first page."""
    service, _, post = _make_service(tmp_path)
    service._start_loop = MagicMock()  # type: ignore[method-assign]
    post.get_user_posts.return_value = UserPostsPage(
        posts=[
            PostDetail(
                aweme_id="9",
                desc="",
                create_time=0,
                post_type=PostType.VIDEO,
                video_info=None,
                images=None,
                statistics={},
            )
        ],
        next_cursor=None,
        has_more=False,
    )

    record, created = await service.create_subscription(
        user_id="user01", backfill_on_create=False
    )

    assert created is True
    assert record.checkpoint["newest_aweme_id"] == "9"
    assert record.checkpoint["recent_aweme_ids"] == ["9"]
    assert record.checkpoint["first_run_complete"] is True


async def test_do_live_check_swallows_offline(tmp_path: Path) -> None:
    """An offline user (LivestreamError) does not propagate from a check."""
    service, livestream, _ = _make_service(tmp_path)
    livestream.download_stream.side_effect = LivestreamError("not streaming")
    record = await service.watch_store.create_subscription(
        user_id="user01", live_poll_seconds=300, post_poll_seconds=600
    )

    await service._do_live_check(record)  # must not raise

    livestream.download_stream.assert_awaited_once()


async def test_do_post_check_advances_checkpoint(tmp_path: Path) -> None:
    """A successful post check folds new ids into the checkpoint."""
    service, _, post = _make_service(tmp_path)
    post.download_new_posts.return_value = IncrementalDownloadResult(
        operation_id="op",
        new_count=2,
        newest_aweme_id="200",
        seen_aweme_ids=["200", "199"],
        failed_count=0,
    )
    record = await service.watch_store.create_subscription(
        user_id="user01",
        live_poll_seconds=300,
        post_poll_seconds=600,
        checkpoint={
            "newest_aweme_id": "100",
            "recent_aweme_ids": ["100"],
            "first_run_complete": True,
        },
    )

    await service._do_post_check(record)

    updated = await service.watch_store.get_subscription(record.subscription_id)
    assert updated.checkpoint["newest_aweme_id"] == "200"
    assert "200" in updated.checkpoint["recent_aweme_ids"]
    assert "100" in updated.checkpoint["recent_aweme_ids"]


async def test_do_post_check_keeps_checkpoint_when_no_new_posts(
    tmp_path: Path,
) -> None:
    """A run with no new posts leaves the sentinel untouched."""
    service, _, post = _make_service(tmp_path)
    post.download_new_posts.return_value = IncrementalDownloadResult(
        operation_id="op",
        new_count=0,
        newest_aweme_id=None,
        seen_aweme_ids=[],
        failed_count=0,
    )
    record = await service.watch_store.create_subscription(
        user_id="user01",
        live_poll_seconds=300,
        post_poll_seconds=600,
        checkpoint={"newest_aweme_id": "100", "recent_aweme_ids": ["100"]},
    )

    await service._do_post_check(record)

    updated = await service.watch_store.get_subscription(record.subscription_id)
    assert updated.checkpoint["newest_aweme_id"] == "100"
    assert updated.last_post_check is not None


async def test_resume_persisted_starts_loops_and_stop_all_cancels(
    tmp_path: Path,
) -> None:
    """resume_persisted arms one loop per enabled subscription; stop_all clears."""
    service, _, _ = _make_service(tmp_path)
    service._do_live_check = AsyncMock()  # type: ignore[method-assign]
    service._do_post_check = AsyncMock()  # type: ignore[method-assign]
    await service.watch_store.create_subscription(
        user_id="user01", live_poll_seconds=3600, post_poll_seconds=3600
    )
    await service.watch_store.create_subscription(
        user_id="user02", live_poll_seconds=3600, post_poll_seconds=3600
    )

    count = await service.resume_persisted()
    await asyncio.sleep(0.05)

    assert count == 2
    assert service.active_count == 2

    await service.stop_all()
    assert service.active_count == 0


async def test_watch_loop_runs_first_cycle_then_cancellable(
    tmp_path: Path,
) -> None:
    """The loop checks immediately on start and exits cleanly on cancel."""
    service, _, _ = _make_service(tmp_path)
    service._do_live_check = AsyncMock()  # type: ignore[method-assign]
    service._do_post_check = AsyncMock()  # type: ignore[method-assign]
    record = await service.watch_store.create_subscription(
        user_id="user01", live_poll_seconds=3600, post_poll_seconds=3600
    )

    task = asyncio.create_task(service._watch_loop(record.subscription_id))
    await asyncio.sleep(0.05)

    assert service._do_live_check.await_count >= 1
    assert service._do_post_check.await_count >= 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_watch_loop_exits_when_subscription_deleted(tmp_path: Path) -> None:
    """A loop whose subscription disappears returns instead of erroring."""
    service, _, _ = _make_service(tmp_path)
    service._do_live_check = AsyncMock()  # type: ignore[method-assign]
    service._do_post_check = AsyncMock()  # type: ignore[method-assign]
    record = await service.watch_store.create_subscription(
        user_id="user01", live_poll_seconds=3600, post_poll_seconds=3600
    )
    await service.watch_store.delete_subscription(record.subscription_id)

    # Loop should observe the missing row on its first iteration and return.
    await asyncio.wait_for(service._watch_loop(record.subscription_id), timeout=1.0)


def test_to_response_surfaces_checkpoint_newest(tmp_path: Path) -> None:
    """to_response lifts newest_aweme_id out of the checkpoint blob."""
    service, _, _ = _make_service(tmp_path)
    record = WatchSubscriptionRecord(
        subscription_id="sub-1",
        user_id="user01",
        enabled=True,
        live_poll_seconds=300,
        post_poll_seconds=600,
        checkpoint={"newest_aweme_id": "42"},
        last_live_check=None,
        last_post_check=None,
        created_at="2026-06-22T00:00:00+00:00",
        updated_at="2026-06-22T00:00:00+00:00",
    )
    response = service.to_response(record)
    assert response.user_id == "user01"
    assert response.newest_aweme_id == "42"


def test_jittered_interval_within_bounds(tmp_path: Path) -> None:
    """Jitter keeps the interval within +/-10% of the configured value."""
    service, _, _ = _make_service(tmp_path)
    for _ in range(20):
        value = service._jittered_interval(100)
        assert 90.0 <= value <= 110.0


async def test_delete_subscription_cancels_loop_and_removes_row(
    tmp_path: Path,
) -> None:
    """Deleting cancels the watcher loop and removes the persisted row."""
    service, _, _ = _make_service(tmp_path)
    service._do_live_check = AsyncMock()  # type: ignore[method-assign]
    service._do_post_check = AsyncMock()  # type: ignore[method-assign]
    record = await service.watch_store.create_subscription(
        user_id="user01", live_poll_seconds=3600, post_poll_seconds=3600
    )
    service._start_loop(record)
    await asyncio.sleep(0.02)
    assert service.active_count == 1

    await service.delete_subscription(record.subscription_id)

    assert service.active_count == 0
    assert await service.watch_store.get_subscription_by_user("user01") is None


async def test_delete_unknown_subscription_raises(tmp_path: Path) -> None:
    """Deleting a non-existent subscription raises the typed not-found error."""
    service, _, _ = _make_service(tmp_path)
    with pytest.raises(WatchSubscriptionNotFoundError):
        await service.delete_subscription("missing")


async def test_do_post_check_keeps_checkpoint_on_service_error(
    tmp_path: Path,
) -> None:
    """An upstream failure leaves the checkpoint untouched for a later retry."""
    from dyvine.core.exceptions import ServiceError

    service, _, post = _make_service(tmp_path)
    post.download_new_posts.side_effect = ServiceError("upstream down")
    record = await service.watch_store.create_subscription(
        user_id="user01",
        live_poll_seconds=300,
        post_poll_seconds=600,
        checkpoint={"newest_aweme_id": "100", "recent_aweme_ids": ["100"]},
    )

    await service._do_post_check(record)  # must not raise

    updated = await service.watch_store.get_subscription(record.subscription_id)
    assert updated.checkpoint["newest_aweme_id"] == "100"


async def test_do_post_check_holds_checkpoint_on_partial_failure(
    tmp_path: Path,
) -> None:
    """Any failed download in the window must NOT advance the checkpoint.

    Advancing past a post that was downloaded while an older sibling failed
    would make the newest-first early-stop skip the failure forever; holding
    the boundary lets the next cycle re-scan and retry it.
    """
    service, _, post = _make_service(tmp_path)
    post.download_new_posts.return_value = IncrementalDownloadResult(
        operation_id="op",
        new_count=1,
        newest_aweme_id="200",
        seen_aweme_ids=["200"],
        failed_count=1,
    )
    record = await service.watch_store.create_subscription(
        user_id="user01",
        live_poll_seconds=300,
        post_poll_seconds=600,
        checkpoint={
            "newest_aweme_id": "100",
            "recent_aweme_ids": ["100"],
            "first_run_complete": True,
        },
    )

    await service._do_post_check(record)

    updated = await service.watch_store.get_subscription(record.subscription_id)
    # Boundary held at the prior value despite a "new" post being reported.
    assert updated.checkpoint["newest_aweme_id"] == "100"
    assert updated.checkpoint["recent_aweme_ids"] == ["100"]
    # The check itself still happened, only the checkpoint advance is withheld.
    assert updated.last_post_check is not None


async def test_delete_subscription_serialises_under_create_lock(
    tmp_path: Path,
) -> None:
    """Delete takes the same lock as create, so the two cannot interleave."""
    service, _, _ = _make_service(tmp_path)
    service._do_live_check = AsyncMock()  # type: ignore[method-assign]
    service._do_post_check = AsyncMock()  # type: ignore[method-assign]
    record = await service.watch_store.create_subscription(
        user_id="user01", live_poll_seconds=3600, post_poll_seconds=3600
    )

    lock = service._get_lock()
    await lock.acquire()
    try:
        delete_task = asyncio.create_task(
            service.delete_subscription(record.subscription_id)
        )
        await asyncio.sleep(0.02)
        # While we hold the lock, delete must be parked rather than finished.
        assert not delete_task.done()
        assert await service.watch_store.get_subscription_by_user("user01") is not None
    finally:
        lock.release()

    await asyncio.wait_for(delete_task, timeout=1.0)
    assert await service.watch_store.get_subscription_by_user("user01") is None


async def test_do_post_check_holds_checkpoint_on_truncation(
    tmp_path: Path,
) -> None:
    """A truncated run (page cap hit) must NOT advance the checkpoint.

    Advancing would make the next newest-first scan stop at this run's newest
    post and skip every post past the cap permanently.
    """
    service, _, post = _make_service(tmp_path)
    post.download_new_posts.return_value = IncrementalDownloadResult(
        operation_id="op",
        new_count=2,
        newest_aweme_id="200",
        seen_aweme_ids=["200", "199"],
        failed_count=0,
        truncated=True,
    )
    record = await service.watch_store.create_subscription(
        user_id="user01",
        live_poll_seconds=300,
        post_poll_seconds=600,
        checkpoint={
            "newest_aweme_id": "100",
            "recent_aweme_ids": ["100"],
            "first_run_complete": True,
        },
    )

    await service._do_post_check(record)

    updated = await service.watch_store.get_subscription(record.subscription_id)
    assert updated.checkpoint["newest_aweme_id"] == "100"
    assert updated.checkpoint["recent_aweme_ids"] == ["100"]
    assert updated.last_post_check is not None


async def test_create_subscription_existing_skips_baseline_snapshot(
    tmp_path: Path,
) -> None:
    """A repeat non-backfill create returns the existing row without snapshotting.

    The pre-lock existence check means a transient profile/upstream failure
    during the baseline snapshot cannot turn an idempotent retry into an error.
    """
    service, _, post = _make_service(tmp_path)
    service._start_loop = MagicMock()  # type: ignore[method-assign]
    post.get_user_posts.return_value = UserPostsPage(
        posts=[
            PostDetail(
                aweme_id="9",
                desc="",
                create_time=0,
                post_type=PostType.VIDEO,
                video_info=None,
                images=None,
                statistics={},
            )
        ],
        next_cursor=None,
        has_more=False,
    )

    rec1, created1 = await service.create_subscription(
        user_id="user01", backfill_on_create=False
    )
    assert created1 is True
    assert post.get_user_posts.await_count == 1  # baseline snapshot for the new row

    # A second non-backfill create must short-circuit BEFORE snapshotting again.
    post.get_user_posts.side_effect = AssertionError("must not snapshot again")
    rec2, created2 = await service.create_subscription(
        user_id="user01", backfill_on_create=False
    )
    assert created2 is False
    assert rec2.subscription_id == rec1.subscription_id
    assert post.get_user_posts.await_count == 1  # unchanged: no second snapshot
