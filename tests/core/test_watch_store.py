"""Tests for the SQLite-backed watch subscription store."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from dyvine.core.exceptions import WatchSubscriptionNotFoundError
from dyvine.core.operations import OperationStore
from dyvine.core.watch_store import WatchSubscriptionStore


def _make_store(tmp_path: Path) -> WatchSubscriptionStore:
    """Build a store backed by an isolated SQLite file."""
    return WatchSubscriptionStore(db_path=str(tmp_path / "watch.db"))


async def test_create_and_get_round_trip(tmp_path: Path) -> None:
    """A created subscription reads back with every field intact."""
    store = _make_store(tmp_path)
    record = await store.create_subscription(
        user_id="user01",
        live_poll_seconds=120,
        post_poll_seconds=600,
        checkpoint={
            "newest_aweme_id": "7",
            "recent_aweme_ids": ["7"],
            "first_run_complete": True,
        },
    )
    fetched = await store.get_subscription(record.subscription_id)
    assert fetched.user_id == "user01"
    assert fetched.enabled is True
    assert fetched.live_poll_seconds == 120
    assert fetched.post_poll_seconds == 600
    assert fetched.checkpoint["newest_aweme_id"] == "7"


async def test_get_by_user_returns_none_when_absent(tmp_path: Path) -> None:
    """Looking up an unknown user returns None rather than raising."""
    store = _make_store(tmp_path)
    assert await store.get_subscription_by_user("nobody01") is None


async def test_unique_user_constraint_rejects_duplicate(tmp_path: Path) -> None:
    """A second subscription for the same user violates the UNIQUE index."""
    store = _make_store(tmp_path)
    await store.create_subscription(
        user_id="user01", live_poll_seconds=120, post_poll_seconds=600
    )
    with pytest.raises(sqlite3.IntegrityError):
        await store.create_subscription(
            user_id="user01", live_poll_seconds=120, post_poll_seconds=600
        )


async def test_list_filters_disabled(tmp_path: Path) -> None:
    """enabled_only excludes disabled subscriptions."""
    store = _make_store(tmp_path)
    enabled = await store.create_subscription(
        user_id="user01", live_poll_seconds=120, post_poll_seconds=600
    )
    disabled = await store.create_subscription(
        user_id="user02", live_poll_seconds=120, post_poll_seconds=600
    )
    await store.update_subscription(disabled.subscription_id, enabled=False)

    all_subs = await store.list_subscriptions()
    enabled_subs = await store.list_subscriptions(enabled_only=True)
    assert {s.subscription_id for s in all_subs} == {
        enabled.subscription_id,
        disabled.subscription_id,
    }
    assert [s.subscription_id for s in enabled_subs] == [enabled.subscription_id]


async def test_update_persists_checkpoint_and_advances_timestamp(
    tmp_path: Path,
) -> None:
    """Updating the checkpoint persists JSON and bumps updated_at."""
    store = _make_store(tmp_path)
    record = await store.create_subscription(
        user_id="user01", live_poll_seconds=120, post_poll_seconds=600
    )
    updated = await store.update_subscription(
        record.subscription_id,
        checkpoint={"newest_aweme_id": "42", "recent_aweme_ids": ["42"]},
        last_post_check="2026-06-22T01:00:00+00:00",
    )
    assert updated.checkpoint["newest_aweme_id"] == "42"
    assert updated.last_post_check == "2026-06-22T01:00:00+00:00"
    assert updated.updated_at >= record.updated_at


async def test_delete_reports_existence(tmp_path: Path) -> None:
    """Deleting returns True the first time and False afterwards."""
    store = _make_store(tmp_path)
    record = await store.create_subscription(
        user_id="user01", live_poll_seconds=120, post_poll_seconds=600
    )
    assert await store.delete_subscription(record.subscription_id) is True
    assert await store.delete_subscription(record.subscription_id) is False


async def test_count_subscriptions(tmp_path: Path) -> None:
    """count_subscriptions reflects the number of rows."""
    store = _make_store(tmp_path)
    assert await store.count_subscriptions() == 0
    await store.create_subscription(
        user_id="user01", live_poll_seconds=120, post_poll_seconds=600
    )
    assert await store.count_subscriptions() == 1


async def test_get_missing_raises(tmp_path: Path) -> None:
    """A missing subscription raises the typed not-found error."""
    store = _make_store(tmp_path)
    with pytest.raises(WatchSubscriptionNotFoundError):
        await store.get_subscription("does-not-exist")


async def test_mark_incomplete_does_not_touch_subscriptions(tmp_path: Path) -> None:
    """The operation store's restart sweep leaves subscriptions untouched.

    This is the core safety property behind keeping subscriptions in their
    own table: ``mark_incomplete_operations_failed`` flips running operations
    to ``failed`` on boot, and a long-lived subscription must survive it.
    """
    db_path = str(tmp_path / "shared.db")
    op_store = OperationStore(db_path=db_path)
    watch_store = WatchSubscriptionStore(db_path=db_path)

    op = await op_store.create_operation(
        operation_type="user_posts_bulk_download",
        subject_id="user01",
        status="running",
        message="in progress",
    )
    sub = await watch_store.create_subscription(
        user_id="user01",
        live_poll_seconds=120,
        post_poll_seconds=600,
        checkpoint={"newest_aweme_id": "1"},
    )

    swept = await op_store.mark_incomplete_operations_failed()
    assert swept == 1

    op_after = await op_store.get_operation(op.operation_id)
    sub_after = await watch_store.get_subscription(sub.subscription_id)
    assert op_after.status == "failed"
    assert sub_after.enabled is True
    assert sub_after.checkpoint == {"newest_aweme_id": "1"}

    op_store.shutdown()
    watch_store.shutdown()


async def test_shutdown_is_idempotent(tmp_path: Path) -> None:
    """shutdown can be called repeatedly without raising."""
    store = _make_store(tmp_path)
    await store.create_subscription(
        user_id="user01", live_poll_seconds=120, post_poll_seconds=600
    )
    store.shutdown()
    store.shutdown()
    assert store.is_closed is True
