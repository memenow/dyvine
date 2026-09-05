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


async def test_create_subscription_converges_cross_process_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A UNIQUE loss against a sibling replica returns the winner (F8).

    The create lock is per-process, so two replicas can both pass the
    re-check and race the INSERT. The loser must converge to the
    documented idempotent return instead of surfacing a duplicate
    error for a retryable create.
    """
    from typing import Any

    from dyvine.core.exceptions import WatchDuplicateError

    service, _, _ = _make_service(tmp_path)
    service._start_loop = MagicMock()  # type: ignore[method-assign]
    store = service.watch_store

    winner = await store.create_subscription(
        user_id="user-race", live_poll_seconds=60, post_poll_seconds=300
    )
    # Hide the winner from the fast-path and locked re-checks so the
    # service proceeds to INSERT as if no row existed yet.
    reads = 0
    real_get = store.get_subscription_by_user

    async def _flaky_get(user_id: str) -> Any:
        nonlocal reads
        reads += 1
        if reads <= 2:
            return None
        return await real_get(user_id)

    async def _always_duplicate(**kwargs: Any) -> Any:
        raise WatchDuplicateError(
            "Watch subscription for user user-race already exists",
            details={"user_id": "user-race"},
        )

    monkeypatch.setattr(store, "get_subscription_by_user", _flaky_get)
    monkeypatch.setattr(store, "create_subscription", _always_duplicate)

    record, created = await service.create_subscription(
        user_id="user-race", backfill_on_create=True
    )
    assert created is False
    assert record.subscription_id == winner.subscription_id


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


async def test_reconcile_adopts_and_drops_loops(tmp_path: Path) -> None:
    """Reconcile starts loops for new rows, stops them for gone rows."""
    service, _, _ = _make_service(tmp_path)
    service._do_live_check = AsyncMock()  # type: ignore[method-assign]
    service._do_post_check = AsyncMock()  # type: ignore[method-assign]
    try:
        await service.watch_store.create_subscription(
            user_id="user01", live_poll_seconds=3600, post_poll_seconds=3600
        )
        started, stopped = await service.reconcile_loops()
        assert (started, stopped) == (1, 0)
        assert service.active_count == 1

        # Steady state: nothing to do.
        assert await service.reconcile_loops() == (0, 0)

        # A row deleted out-of-band (e.g. via an API replica) stops here.
        doomed = await service.watch_store.get_subscription_by_user("user01")
        assert doomed is not None
        await service.watch_store.delete_subscription(doomed.subscription_id)
        started, stopped = await service.reconcile_loops()
        assert (started, stopped) == (0, 1)
        assert service.active_count == 0
    finally:
        await service.stop_all()


async def _plant_crash(service: WatchService, subscription_id: str) -> None:
    """Retire the live loop and plant a failed handle in its place."""
    original = service._loops.pop(subscription_id)
    original.cancel()
    try:
        await original
    except asyncio.CancelledError:
        pass

    async def _boom() -> None:
        raise RuntimeError("loop boom")

    crashed = asyncio.create_task(_boom())
    with pytest.raises(RuntimeError, match="loop boom"):
        await crashed
    service._loops[subscription_id] = crashed


async def test_reconcile_restarts_crashed_loops_under_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crashed loop restarts once its backoff elapses, not before."""
    import time

    service, _, _ = _make_service(tmp_path)
    service._do_live_check = AsyncMock()  # type: ignore[method-assign]
    service._do_post_check = AsyncMock()  # type: ignore[method-assign]
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    try:
        record = await service.watch_store.create_subscription(
            user_id="user01", live_poll_seconds=3600, post_poll_seconds=3600
        )
        await service.reconcile_loops()
        assert service.active_count == 1

        await _plant_crash(service, record.subscription_id)
        # Crash noted (count 1, 30s backoff) but not yet restarted.
        assert await service.reconcile_loops() == (0, 0)
        assert service.active_count == 0
        assert service._crash_counts[record.subscription_id] == 1

        clock[0] += 31.0
        started, _ = await service.reconcile_loops()
        assert started == 1
        assert service.active_count == 1
    finally:
        await service.stop_all()


async def test_reconcile_parks_flapping_loops_with_alert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Past the crash cap the loop stays down and an alert is logged."""
    import logging
    import time

    from dyvine.services import watch as watch_module

    service, _, _ = _make_service(tmp_path)
    service._do_live_check = AsyncMock()  # type: ignore[method-assign]
    service._do_post_check = AsyncMock()  # type: ignore[method-assign]
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    cap = watch_module._MAX_CONSECUTIVE_CRASHES
    try:
        record = await service.watch_store.create_subscription(
            user_id="user01", live_poll_seconds=3600, post_poll_seconds=3600
        )
        await service.reconcile_loops()
        for _ in range(cap):
            await _plant_crash(service, record.subscription_id)
            assert await service.reconcile_loops() == (0, 0)
            clock[0] += 400.0  # past any backoff
            started, _ = await service.reconcile_loops()
            assert started == 1
        # One crash too many: parked, never restarted again.
        await _plant_crash(service, record.subscription_id)
        with caplog.at_level(logging.ERROR, logger="dyvine.services.watch"):
            started, stopped = await service.reconcile_loops()
        assert (started, stopped) == (0, 1)
        assert service.active_count == 0
        assert any("parked" in r.getMessage() for r in caplog.records)
        clock[0] += 3600.0
        assert await service.reconcile_loops() == (0, 0)
        assert service.active_count == 0
    finally:
        await service.stop_all()


async def test_crashed_loop_stays_registered_with_root_cause(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A real crash is reaped by reconcile with its cause, not None."""
    import logging

    service, _, _ = _make_service(tmp_path)
    service._do_live_check = AsyncMock()  # type: ignore[method-assign]
    service._do_post_check = AsyncMock()  # type: ignore[method-assign]
    record = await service.watch_store.create_subscription(
        user_id="user01", live_poll_seconds=3600, post_poll_seconds=3600
    )
    real_get = service.watch_store.get_subscription
    calls = 0

    async def flaky_get(subscription_id: str):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom-cause")
        return await real_get(subscription_id)

    service.watch_store.get_subscription = flaky_get  # type: ignore[method-assign]
    try:
        await service.reconcile_loops()
        task = service._loops[record.subscription_id]
        result = await asyncio.wait_for(asyncio.shield(task), timeout=5)
        assert isinstance(result, RuntimeError)
        assert "boom-cause" in str(result)
        # Crashed task stays registered for reconcile to reap.
        assert record.subscription_id in service._loops
        with caplog.at_level(logging.WARNING, logger="dyvine.services.watch"):
            await service.reconcile_loops()
        assert service._crash_counts[record.subscription_id] == 1
        assert any(
            getattr(entry, "last_error", "") == "RuntimeError('boom-cause')"
            for entry in caplog.records
        )
    finally:
        await service.stop_all()


async def test_idempotent_create_does_not_rearm_parked_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retry POST for a parked subscription stays down (200, no loop)."""
    import time

    from dyvine.services import watch as watch_module

    service, _, _ = _make_service(tmp_path)
    service._do_live_check = AsyncMock()  # type: ignore[method-assign]
    service._do_post_check = AsyncMock()  # type: ignore[method-assign]
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    cap = watch_module._MAX_CONSECUTIVE_CRASHES
    try:
        record, created = await service.create_subscription(
            user_id="user01", backfill_on_create=True
        )
        assert created is True
        await service.reconcile_loops()
        for _ in range(cap + 1):
            await _plant_crash(service, record.subscription_id)
            await service.reconcile_loops()
            clock[0] += 400.0
            await service.reconcile_loops()
        assert service._crash_counts[record.subscription_id] > cap
        assert service.active_count == 0
        # Idempotent retry returns the row but must not re-arm the loop.
        same, created = await service.create_subscription(user_id="user01")
        assert created is False
        assert same.subscription_id == record.subscription_id
        assert service.active_count == 0
        assert record.subscription_id not in service._loops
    finally:
        await service.stop_all()


async def test_disable_clears_parked_budget_for_reenable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disable→re-enable revives a parked loop from a clean slate."""
    import time

    from dyvine.services import watch as watch_module

    service, _, _ = _make_service(tmp_path)
    service._do_live_check = AsyncMock()  # type: ignore[method-assign]
    service._do_post_check = AsyncMock()  # type: ignore[method-assign]
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    cap = watch_module._MAX_CONSECUTIVE_CRASHES
    try:
        record = await service.watch_store.create_subscription(
            user_id="user01", live_poll_seconds=3600, post_poll_seconds=3600
        )
        await service.reconcile_loops()
        for _ in range(cap + 1):
            await _plant_crash(service, record.subscription_id)
            await service.reconcile_loops()
            clock[0] += 400.0
            await service.reconcile_loops()
        assert service._crash_counts[record.subscription_id] > cap
        # Disable while parked (no live task to cancel).
        await service.watch_store.update_subscription(
            record.subscription_id, enabled=False
        )
        await service.reconcile_loops()
        assert record.subscription_id not in service._crash_counts
        # Re-enable restarts the loop instead of inheriting the park.
        await service.watch_store.update_subscription(
            record.subscription_id, enabled=True
        )
        started, _ = await service.reconcile_loops()
        assert started == 1
        assert service.active_count == 1
    finally:
        await service.stop_all()


async def test_reconcile_resets_budget_after_healthy_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restarted loop that survives a pass clears its crash count."""
    import time

    service, _, _ = _make_service(tmp_path)
    service._do_live_check = AsyncMock()  # type: ignore[method-assign]
    service._do_post_check = AsyncMock()  # type: ignore[method-assign]
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    try:
        record = await service.watch_store.create_subscription(
            user_id="user01", live_poll_seconds=3600, post_poll_seconds=3600
        )
        await service.reconcile_loops()
        await _plant_crash(service, record.subscription_id)
        await service.reconcile_loops()
        assert service._crash_counts[record.subscription_id] == 1
        clock[0] += 31.0
        await service.reconcile_loops()
        # The restarted loop is alive on the next pass: budget reset.
        clock[0] += 30.0
        assert await service.reconcile_loops() == (0, 0)
        assert record.subscription_id not in service._crash_counts
    finally:
        await service.stop_all()


async def test_delete_clears_crash_budget(tmp_path: Path) -> None:
    """Deleting a flapping subscription never restarts it afterwards."""
    service, _, _ = _make_service(tmp_path)
    service._do_live_check = AsyncMock()  # type: ignore[method-assign]
    service._do_post_check = AsyncMock()  # type: ignore[method-assign]
    try:
        record = await service.watch_store.create_subscription(
            user_id="user01", live_poll_seconds=3600, post_poll_seconds=3600
        )
        await service.reconcile_loops()
        await _plant_crash(service, record.subscription_id)
        await service.reconcile_loops()
        assert service._crash_counts[record.subscription_id] == 1
        await service.delete_subscription(record.subscription_id)
        assert record.subscription_id not in service._crash_counts
        assert await service.reconcile_loops() == (0, 0)
        assert service.active_count == 0
    finally:
        await service.stop_all()


async def test_reconcile_restarts_silently_cancelled_handles(
    tmp_path: Path,
) -> None:
    """A cancelled-but-present handle restarts fresh, budget untouched."""
    service, _, _ = _make_service(tmp_path)
    service._do_live_check = AsyncMock()  # type: ignore[method-assign]
    service._do_post_check = AsyncMock()  # type: ignore[method-assign]
    try:
        record = await service.watch_store.create_subscription(
            user_id="user01", live_poll_seconds=3600, post_poll_seconds=3600
        )
        await service.reconcile_loops()
        original = service._loops.pop(record.subscription_id)
        original.cancel()
        try:
            await original
        except asyncio.CancelledError:
            pass
        service._loops[record.subscription_id] = original
        started, _ = await service.reconcile_loops()
        assert started == 1
        assert record.subscription_id not in service._crash_counts
    finally:
        await service.stop_all()


async def test_reconcile_skips_disabled_rows(tmp_path: Path) -> None:
    """Disabled subscriptions neither start nor keep loops."""
    service, _, _ = _make_service(tmp_path)
    record = await service.watch_store.create_subscription(
        user_id="user01",
        live_poll_seconds=3600,
        post_poll_seconds=3600,
        enabled=False,
    )
    try:
        assert await service.reconcile_loops() == (0, 0)
        assert service.active_count == 0
        assert record.subscription_id not in service._loops
    finally:
        await service.stop_all()


async def test_run_loops_false_is_crud_only(tmp_path: Path) -> None:
    """CRUD-only replicas persist rows but never start loops."""
    from fake_repos import FakeWatchRepository

    store = FakeWatchRepository()
    service = WatchService(
        watch_store=store,
        livestream_service=MagicMock(),
        post_service=MagicMock(),
        run_loops=False,
    )
    record, created = await service.create_subscription(
        user_id="user01", backfill_on_create=True
    )
    assert created is True
    assert service.active_count == 0
    assert await service.resume_persisted() == 0
    assert await service.reconcile_loops() == (0, 0)
    fetched = await service.get_subscription(record.subscription_id)
    assert fetched.user_id == "user01"
    await service.delete_subscription(record.subscription_id)
    assert await service.list_subscriptions() == []


async def test_run_reconcile_forever_loops_until_cancelled(
    tmp_path: Path,
) -> None:
    """The reconcile loop repeats passes and propagates cancellation."""
    service, _, _ = _make_service(tmp_path)
    task = asyncio.create_task(service.run_reconcile_forever(interval_seconds=0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_run_reconcile_forever_survives_failed_pass(
    tmp_path: Path,
) -> None:
    """One transient failure is logged and skipped, not fatal."""
    service, _, _ = _make_service(tmp_path)
    calls = 0

    async def flaky() -> tuple[int, int]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient db error")
        return (0, 0)

    service.reconcile_loops = flaky  # type: ignore[method-assign]
    task = asyncio.create_task(service.run_reconcile_forever(interval_seconds=0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls >= 2


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


async def test_start_loop_counts_pending_crash_before_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash awaiting reap is counted when an idempotent create replaces it."""
    service, _, _ = _make_service(tmp_path)
    service._do_live_check = AsyncMock()  # type: ignore[method-assign]
    service._do_post_check = AsyncMock()  # type: ignore[method-assign]
    record = await service.watch_store.create_subscription(
        user_id="user01", live_poll_seconds=3600, post_poll_seconds=3600
    )

    async def _boom() -> None:
        raise RuntimeError("boom")

    crashed = asyncio.create_task(_boom())
    await asyncio.sleep(0.02)
    assert crashed.done()
    service._loops[record.subscription_id] = crashed
    monkeypatch.setattr(service, "_restart_allowed", lambda *a, **k: True)

    service._start_loop(record)

    assert service._crash_counts.get(record.subscription_id) == 1
    assert service._loops[record.subscription_id] is not crashed
    await service.delete_subscription(record.subscription_id)


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
