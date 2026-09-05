"""Repository contract suite: identical assertions, two backends.

Every test in this module runs twice -- once against the in-memory
fakes from ``tests.fake_repos`` and once against a real Postgres
started via testcontainers with the Alembic migrations applied. A
behavior asserted here is therefore guaranteed identical in unit
tests and in production; if the two backends ever diverge, the same
test fails on exactly one leg.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import fake_repos
import pytest
from fake_repos import (
    FakeOperationRepository,
    FakeOperationState,
    FakeWatchRepository,
)
from sqlalchemy import text

import dyvine.db.postgres as postgres_module
from dyvine.core.exceptions import (
    OperationNotFoundError,
    RateLimitError,
    ServiceError,
    WatchDuplicateError,
    WatchSubscriptionNotFoundError,
)
from dyvine.db import (
    DatabaseSessionFactory,
    OperationRepository,
    PostgresOperationRepository,
    PostgresWatchRepository,
    WatchRepository,
)

#: Frozen clock value that is unambiguously older than any real write.
OLD_STAMP = "2020-01-01T00:00:00+00:00"


@dataclass
class BackendContext:
    """Per-test backend handle shared by every contract test."""

    name: str
    make_ops: Callable[[str], OperationRepository]
    make_watch: Callable[[], WatchRepository]


@pytest.fixture(params=["fake", "postgres"])
async def backend(request: pytest.FixtureRequest) -> Any:
    """Yield a backend context; Postgres tables are truncated first.

    The container only starts for the ``postgres`` leg: the URL
    fixture is pulled lazily so the ``fake`` leg stays Docker-free.
    """
    if request.param == "fake":
        state = FakeOperationState()
        yield BackendContext(
            name="fake",
            make_ops=lambda owner: FakeOperationRepository(owner_id=owner, state=state),
            make_watch=FakeWatchRepository,
        )
        return
    postgres_url: str = request.getfixturevalue("postgres_url")
    factory = DatabaseSessionFactory(postgres_url, pool_size=2)
    async with factory.session() as session:
        async with session.begin():
            await session.execute(
                text("TRUNCATE TABLE operations, watch_subscriptions")
            )
    try:
        yield BackendContext(
            name="postgres",
            make_ops=lambda owner: PostgresOperationRepository(factory, owner_id=owner),
            make_watch=lambda: PostgresWatchRepository(factory),
        )
    finally:
        await factory.aclose()


@contextlib.contextmanager
def _freeze(
    backend: BackendContext, monkeypatch: pytest.MonkeyPatch, stamp: str
) -> Iterator[None]:
    """Freeze ``backend``'s write clock at ``stamp`` (auto-undone).

    A nested ``monkeypatch.context`` scopes the patch to the ``with``
    block; patching through the test-level ``monkeypatch`` directly
    would leak the frozen clock into the rest of the test.
    """
    module = fake_repos if backend.name == "fake" else postgres_module
    with monkeypatch.context() as scoped:
        scoped.setattr(module, "_now_iso", lambda: stamp)
        yield


async def test_healthcheck_passes(backend: BackendContext) -> None:
    """Both backends answer the readiness probe."""
    await backend.make_ops("owner-a").healthcheck()


async def test_create_operation_round_trip(backend: BackendContext) -> None:
    """Created fields echo back; metadata defaults to ``{}``."""
    repo = backend.make_ops("owner-a")
    created = await repo.create_operation(
        operation_type="user_content_download",
        subject_id="user-1",
        status="pending",
        message="queued",
        total_items=10,
    )
    assert created.operation_id
    assert created.metadata == {}
    assert created.created_at == created.updated_at

    fetched = await repo.get_operation(created.operation_id)
    assert fetched == created


async def test_create_operation_honours_explicit_id(
    backend: BackendContext,
) -> None:
    """Callers can pin the primary key (idempotent retries)."""
    repo = backend.make_ops("owner-a")
    created = await repo.create_operation(
        operation_type="post_bulk_download",
        subject_id="user-2",
        status="running",
        message="started",
        operation_id="fixed-id",
    )
    assert created.operation_id == "fixed-id"


async def test_create_operation_duplicate_id_raises(
    backend: BackendContext,
) -> None:
    """A repeated primary key surfaces ``ServiceError``, not a driver error."""
    repo = backend.make_ops("owner-a")
    kwargs: dict[str, Any] = {
        "operation_type": "post_bulk_download",
        "subject_id": "user-2",
        "status": "running",
        "message": "started",
        "operation_id": "dup-id",
    }
    await repo.create_operation(**kwargs)
    with pytest.raises(ServiceError):
        await repo.create_operation(**kwargs)


async def test_get_operation_missing_raises(backend: BackendContext) -> None:
    """Unknown IDs raise ``OperationNotFoundError``."""
    with pytest.raises(OperationNotFoundError):
        await backend.make_ops("owner-a").get_operation("no-such-op")


async def test_get_latest_operation_orders_by_recency(
    backend: BackendContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Latest means greatest ``updated_at`` (``created_at`` breaks ties)."""
    repo = backend.make_ops("owner-a")
    with _freeze(backend, monkeypatch, "2020-01-01T00:00:00+00:00"):
        older = await repo.create_operation(
            operation_type="t",
            subject_id="user-9",
            status="completed",
            message="done",
        )
    with _freeze(backend, monkeypatch, "2020-01-01T00:00:01+00:00"):
        newer = await repo.create_operation(
            operation_type="t",
            subject_id="user-9",
            status="running",
            message="live",
        )
    latest = await repo.get_latest_operation_for_subject("user-9")
    assert latest.operation_id == newer.operation_id
    assert older.operation_id != newer.operation_id


async def test_get_latest_operation_filters_by_type(
    backend: BackendContext,
) -> None:
    """The type filter narrows the latest-operation lookup."""
    repo = backend.make_ops("owner-a")
    await repo.create_operation(
        operation_type="alpha",
        subject_id="user-3",
        status="pending",
        message="queued",
    )
    beta = await repo.create_operation(
        operation_type="beta",
        subject_id="user-3",
        status="pending",
        message="queued",
    )
    latest = await repo.get_latest_operation_for_subject(
        "user-3", operation_type="beta"
    )
    assert latest.operation_id == beta.operation_id


async def test_get_latest_operation_missing_subject_raises(
    backend: BackendContext,
) -> None:
    """Subjects with no rows raise ``OperationNotFoundError``."""
    with pytest.raises(OperationNotFoundError):
        await backend.make_ops("owner-a").get_latest_operation_for_subject(
            "ghost-subject"
        )


async def test_update_operation_merges_fields(
    backend: BackendContext,
) -> None:
    """Known fields merge; untouched columns and identity survive."""
    repo = backend.make_ops("owner-a")
    created = await repo.create_operation(
        operation_type="t",
        subject_id="user-4",
        status="pending",
        message="queued",
        metadata={"cursor": "a"},
    )
    updated = await repo.update_operation(
        created.operation_id, status="running", progress=0.5
    )
    assert updated.status == "running"
    assert updated.progress == 0.5
    assert updated.message == "queued"
    assert updated.metadata == {"cursor": "a"}
    assert updated.created_at == created.created_at


async def test_update_operation_none_metadata_clears(
    backend: BackendContext,
) -> None:
    """An explicit ``metadata=None`` clears to ``{}``, not ``TypeError``."""
    repo = backend.make_ops("owner-a")
    created = await repo.create_operation(
        operation_type="t",
        subject_id="user-4b",
        status="pending",
        message="queued",
        metadata={"cursor": "a"},
    )
    updated = await repo.update_operation(created.operation_id, metadata=None)
    assert updated.metadata == {}


async def test_update_operation_ignores_unknown_fields(
    backend: BackendContext,
) -> None:
    """Unknown-only updates verify existence and return the row unchanged."""
    repo = backend.make_ops("owner-a")
    created = await repo.create_operation(
        operation_type="t",
        subject_id="user-5",
        status="pending",
        message="queued",
    )
    unchanged = await repo.update_operation(created.operation_id, not_a_column="x")
    assert unchanged == created
    with pytest.raises(OperationNotFoundError):
        await repo.update_operation("no-such-op", not_a_column="x")


async def test_update_operation_missing_raises(
    backend: BackendContext,
) -> None:
    """Updates against unknown IDs raise ``OperationNotFoundError``."""
    with pytest.raises(OperationNotFoundError):
        await backend.make_ops("owner-a").update_operation(
            "no-such-op", status="failed"
        )


async def test_sweep_fails_stale_foreign_rows(
    backend: BackendContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stale active rows owned elsewhere flip to failed with context."""
    owner_a = backend.make_ops("owner-a")
    owner_b = backend.make_ops("owner-b")
    with _freeze(backend, monkeypatch, OLD_STAMP):
        created = await owner_a.create_operation(
            operation_type="t",
            subject_id="user-6",
            status="running",
            message="live",
        )
    swept = await owner_b.sweep_orphans(stale_after_seconds=60.0)
    assert swept == 1
    failed = await owner_a.get_operation(created.operation_id)
    assert failed.status == "failed"
    assert "stopped heartbeating" in failed.message
    assert failed.error == failed.message
    assert failed.updated_at > OLD_STAMP


async def test_sweep_skips_fresh_and_terminal_rows(
    backend: BackendContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fresh rows and terminal rows are never swept, whatever the owner."""
    owner_a = backend.make_ops("owner-a")
    owner_b = backend.make_ops("owner-b")
    fresh = await owner_a.create_operation(
        operation_type="t",
        subject_id="fresh",
        status="running",
        message="live",
    )
    with _freeze(backend, monkeypatch, OLD_STAMP):
        terminal = await owner_a.create_operation(
            operation_type="t",
            subject_id="terminal",
            status="completed",
            message="done",
        )
    swept = await owner_b.sweep_orphans(stale_after_seconds=60.0)
    assert swept == 0
    assert (await owner_a.get_operation(fresh.operation_id)).status == "running"
    assert (await owner_a.get_operation(terminal.operation_id)).status == "completed"


async def _null_heartbeat(
    backend: BackendContext,
    request: pytest.FixtureRequest,
    operation_id: str,
) -> None:
    """Wipe a row's heartbeat the way legacy pre-liveness rows look.

    Fake leg drops the sidecar entry; Postgres leg NULLs the column
    over its own short-lived session.
    """
    if backend.name == "fake":
        owner = backend.make_ops("owner-a")
        owner._heartbeats.pop(operation_id, None)  # type: ignore[attr-defined]
        return
    url: str = request.getfixturevalue("postgres_url")
    factory = DatabaseSessionFactory(url, pool_size=1)
    try:
        async with factory.session() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "UPDATE operations SET heartbeat_at = NULL "
                        "WHERE operation_id = :id"
                    ),
                    {"id": operation_id},
                )
    finally:
        await factory.aclose()


async def test_sweep_fails_null_heartbeat_with_old_creation(
    backend: BackendContext,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """NULL-heartbeat rows fall back to ``created_at`` and stay sweepable."""
    owner_a = backend.make_ops("owner-a")
    owner_b = backend.make_ops("owner-b")
    with _freeze(backend, monkeypatch, OLD_STAMP):
        created = await owner_a.create_operation(
            operation_type="t",
            subject_id="null-beat",
            status="running",
            message="live",
        )
    await _null_heartbeat(backend, request, created.operation_id)
    assert await owner_b.sweep_orphans(stale_after_seconds=60.0) == 1
    assert (await owner_a.get_operation(created.operation_id)).status == "failed"


async def test_sweep_spares_null_heartbeat_with_fresh_creation(
    backend: BackendContext, request: pytest.FixtureRequest
) -> None:
    """A freshly created NULL-heartbeat row is not an orphan yet."""
    owner_a = backend.make_ops("owner-a")
    owner_b = backend.make_ops("owner-b")
    created = await owner_a.create_operation(
        operation_type="t",
        subject_id="null-beat-fresh",
        status="running",
        message="live",
    )
    await _null_heartbeat(backend, request, created.operation_id)
    assert await owner_b.sweep_orphans(stale_after_seconds=60.0) == 0
    assert (await owner_a.get_operation(created.operation_id)).status == "running"


async def test_sweep_never_touches_own_rows(
    backend: BackendContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replica never fails its own rows, even when they look stale.

    This is the regression test for rolling updates: the sweeping
    replica must exclude its own owner ID so a slow-but-alive owner
    cannot fail work it still holds.
    """
    owner_a = backend.make_ops("owner-a")
    with _freeze(backend, monkeypatch, OLD_STAMP):
        created = await owner_a.create_operation(
            operation_type="t",
            subject_id="user-7",
            status="running",
            message="live",
        )
    swept = await owner_a.sweep_orphans(stale_after_seconds=60.0)
    assert swept == 0
    assert (await owner_a.get_operation(created.operation_id)).status == "running"


async def test_heartbeat_protects_owned_rows(
    backend: BackendContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A heartbeat moves owned rows out of the sweep window."""
    owner_a = backend.make_ops("owner-a")
    owner_b = backend.make_ops("owner-b")
    with _freeze(backend, monkeypatch, OLD_STAMP):
        created = await owner_a.create_operation(
            operation_type="t",
            subject_id="user-8",
            status="running",
            message="live",
        )
    assert await owner_a.heartbeat_owned() == 1
    swept = await owner_b.sweep_orphans(stale_after_seconds=60.0)
    assert swept == 0
    assert (await owner_a.get_operation(created.operation_id)).status == "running"


async def test_heartbeat_counts_only_owned_active_rows(
    backend: BackendContext,
) -> None:
    """Foreign and terminal rows are invisible to ``heartbeat_owned``."""
    owner_a = backend.make_ops("owner-a")
    owner_b = backend.make_ops("owner-b")
    await owner_a.create_operation(
        operation_type="t",
        subject_id="s1",
        status="running",
        message="live",
    )
    await owner_a.create_operation(
        operation_type="t",
        subject_id="s2",
        status="pending",
        message="queued",
    )
    await owner_a.create_operation(
        operation_type="t",
        subject_id="s3",
        status="completed",
        message="done",
    )
    await owner_b.create_operation(
        operation_type="t",
        subject_id="s4",
        status="running",
        message="live",
    )
    assert await owner_a.heartbeat_owned() == 2


async def test_update_refreshes_heartbeat(
    backend: BackendContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any semantic write re-arms the row against the sweep."""
    owner_a = backend.make_ops("owner-a")
    owner_b = backend.make_ops("owner-b")
    with _freeze(backend, monkeypatch, OLD_STAMP):
        created = await owner_a.create_operation(
            operation_type="t",
            subject_id="user-10",
            status="running",
            message="live",
        )
    await owner_a.update_operation(created.operation_id, progress=0.1)
    swept = await owner_b.sweep_orphans(stale_after_seconds=60.0)
    assert swept == 0


async def test_purge_deletes_only_old_terminal_rows(
    backend: BackendContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retention deletes old terminal rows and nothing else."""
    repo = backend.make_ops("owner-a")
    with _freeze(backend, monkeypatch, OLD_STAMP):
        old_terminal = await repo.create_operation(
            operation_type="t",
            subject_id="old",
            status="completed",
            message="done",
        )
        old_active = await repo.create_operation(
            operation_type="t",
            subject_id="old-active",
            status="running",
            message="live",
        )
    new_terminal = await repo.create_operation(
        operation_type="t", subject_id="new", status="failed", message="bad"
    )
    purged = await repo.purge_terminal_before("2021-01-01T00:00:00+00:00")
    assert purged == 1
    with pytest.raises(OperationNotFoundError):
        await repo.get_operation(old_terminal.operation_id)
    assert (await repo.get_operation(old_active.operation_id)).status == "running"
    assert (await repo.get_operation(new_terminal.operation_id)).status == "failed"


def _subscription_kwargs(user_id: str) -> dict[str, Any]:
    """Minimal valid subscription payload for ``user_id``."""
    return {
        "user_id": user_id,
        "live_poll_seconds": 60,
        "post_poll_seconds": 300,
    }


async def test_create_subscription_round_trip(
    backend: BackendContext,
) -> None:
    """Created subscriptions echo back with defaults applied."""
    repo = backend.make_watch()
    created = await repo.create_subscription(**_subscription_kwargs("user-1"))
    assert created.subscription_id
    assert created.enabled is True
    assert created.checkpoint == {}
    assert created.last_live_check is None
    assert created.last_post_check is None
    assert created.created_at == created.updated_at

    fetched = await repo.get_subscription(created.subscription_id)
    assert fetched == created


async def test_create_subscription_honours_explicit_fields(
    backend: BackendContext,
) -> None:
    """Explicit IDs, flags, and checkpoints survive the round trip."""
    repo = backend.make_watch()
    created = await repo.create_subscription(
        user_id="user-2",
        live_poll_seconds=30,
        post_poll_seconds=120,
        enabled=False,
        checkpoint={"newest_aweme_id": "123"},
        subscription_id="fixed-sub",
    )
    assert created.subscription_id == "fixed-sub"
    assert created.enabled is False
    assert created.checkpoint == {"newest_aweme_id": "123"}


async def test_create_subscription_duplicate_user_raises(
    backend: BackendContext,
) -> None:
    """One subscription per user; the second create raises."""
    repo = backend.make_watch()
    await repo.create_subscription(**_subscription_kwargs("user-3"))
    with pytest.raises(WatchDuplicateError):
        await repo.create_subscription(**_subscription_kwargs("user-3"))


async def test_get_subscription_missing_raises(
    backend: BackendContext,
) -> None:
    """Unknown subscription IDs raise."""
    with pytest.raises(WatchSubscriptionNotFoundError):
        await backend.make_watch().get_subscription("no-such-sub")


async def test_get_subscription_by_user(
    backend: BackendContext,
) -> None:
    """User lookup returns the row, or ``None`` when absent."""
    repo = backend.make_watch()
    assert await repo.get_subscription_by_user("nobody") is None
    created = await repo.create_subscription(**_subscription_kwargs("user-4"))
    found = await repo.get_subscription_by_user("user-4")
    assert found is not None
    assert found.subscription_id == created.subscription_id


async def test_list_subscriptions_orders_by_creation(
    backend: BackendContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Listing returns creation order, optionally enabled-only."""
    repo = backend.make_watch()
    with _freeze(backend, monkeypatch, "2020-01-01T00:00:00+00:00"):
        first = await repo.create_subscription(**_subscription_kwargs("user-5"))
    with _freeze(backend, monkeypatch, "2020-01-01T00:00:01+00:00"):
        second = await repo.create_subscription(
            user_id="user-6",
            live_poll_seconds=60,
            post_poll_seconds=300,
            enabled=False,
        )
    ordered = await repo.list_subscriptions()
    assert [row.subscription_id for row in ordered] == [
        first.subscription_id,
        second.subscription_id,
    ]
    enabled = await repo.list_subscriptions(enabled_only=True)
    assert [row.subscription_id for row in enabled] == [first.subscription_id]
    assert await repo.count_subscriptions() == 2


async def test_update_subscription_merges_fields(
    backend: BackendContext,
) -> None:
    """Known fields merge; identity columns never move."""
    repo = backend.make_watch()
    created = await repo.create_subscription(**_subscription_kwargs("user-7"))
    updated = await repo.update_subscription(
        created.subscription_id,
        enabled=False,
        checkpoint={"newest_aweme_id": "9"},
        last_live_check="2020-05-05T00:00:00+00:00",
    )
    assert updated.enabled is False
    assert updated.checkpoint == {"newest_aweme_id": "9"}
    assert updated.last_live_check == "2020-05-05T00:00:00+00:00"
    assert updated.user_id == "user-7"
    assert updated.live_poll_seconds == 60
    assert updated.created_at == created.created_at


async def test_update_subscription_none_checkpoint_clears(
    backend: BackendContext,
) -> None:
    """An explicit ``checkpoint=None`` clears to ``{}``, not ``TypeError``."""
    repo = backend.make_watch()
    created = await repo.create_subscription(**_subscription_kwargs("user-7b"))
    await repo.update_subscription(
        created.subscription_id, checkpoint={"newest_aweme_id": "9"}
    )
    updated = await repo.update_subscription(created.subscription_id, checkpoint=None)
    assert updated.checkpoint == {}


async def test_create_subscription_capped_enforces_cap(
    backend: BackendContext,
) -> None:
    """At-cap inserts raise with the documented message on both legs."""
    repo = backend.make_watch()
    await repo.create_subscription_capped(
        **_subscription_kwargs("cap-1"), max_subscriptions=2
    )
    await repo.create_subscription_capped(
        **_subscription_kwargs("cap-2"), max_subscriptions=2
    )
    with pytest.raises(RateLimitError, match="limit reached"):
        await repo.create_subscription_capped(
            **_subscription_kwargs("cap-3"), max_subscriptions=2
        )
    assert await repo.count_subscriptions() == 2


async def test_capped_create_never_overshoots_cap(
    backend: BackendContext,
) -> None:
    """Concurrent creators split the cap exactly, none over."""
    repo = backend.make_watch()
    results = await asyncio.gather(
        *(
            repo.create_subscription_capped(
                **_subscription_kwargs(f"race-{index}"), max_subscriptions=3
            )
            for index in range(6)
        ),
        return_exceptions=True,
    )
    succeeded = [r for r in results if not isinstance(r, BaseException)]
    refused = [r for r in results if isinstance(r, RateLimitError)]
    assert len(succeeded) == 3
    assert len(refused) == 3
    assert await repo.count_subscriptions() == 3


async def test_capped_create_prefers_duplicate_over_cap(
    backend: BackendContext,
) -> None:
    """A duplicate at cap converges idempotently, not 429."""
    repo = backend.make_watch()
    await repo.create_subscription_capped(
        **_subscription_kwargs("dup-1"), max_subscriptions=1
    )
    with pytest.raises(WatchDuplicateError):
        await repo.create_subscription_capped(
            **_subscription_kwargs("dup-1"), max_subscriptions=1
        )
    assert await repo.count_subscriptions() == 1


async def test_update_subscription_ignores_unknown_fields(
    backend: BackendContext,
) -> None:
    """Unknown-only updates verify existence and return the row unchanged."""
    repo = backend.make_watch()
    created = await repo.create_subscription(**_subscription_kwargs("user-8"))
    unchanged = await repo.update_subscription(
        created.subscription_id, not_a_column="x"
    )
    assert unchanged == created
    with pytest.raises(WatchSubscriptionNotFoundError):
        await repo.update_subscription("no-such-sub", not_a_column="x")


async def test_update_subscription_missing_raises(
    backend: BackendContext,
) -> None:
    """Updates against unknown IDs raise."""
    with pytest.raises(WatchSubscriptionNotFoundError):
        await backend.make_watch().update_subscription("no-such-sub", enabled=False)


async def test_delete_subscription_reports_existence(
    backend: BackendContext,
) -> None:
    """Delete returns ``True`` once, then ``False``; the user is freed."""
    repo = backend.make_watch()
    created = await repo.create_subscription(**_subscription_kwargs("user-9"))
    assert await repo.delete_subscription(created.subscription_id) is True
    assert await repo.delete_subscription(created.subscription_id) is False
    with pytest.raises(WatchSubscriptionNotFoundError):
        await repo.get_subscription(created.subscription_id)
    # The UNIQUE(user_id) slot is released by the delete.
    recreated = await repo.create_subscription(**_subscription_kwargs("user-9"))
    assert recreated.subscription_id != created.subscription_id


async def test_concurrent_create_same_user_yields_single_row(
    backend: BackendContext,
) -> None:
    """Concurrent creates race the UNIQUE bound: one row, rest duplicate."""
    repo = backend.make_watch()
    results = await asyncio.gather(
        *(
            repo.create_subscription(**_subscription_kwargs("user-race"))
            for _ in range(5)
        ),
        return_exceptions=True,
    )
    winners = [item for item in results if not isinstance(item, BaseException)]
    losers = [item for item in results if isinstance(item, BaseException)]
    assert len(winners) == 1
    assert len(losers) == 4
    assert all(isinstance(item, WatchDuplicateError) for item in losers)
    assert await repo.count_subscriptions() == 1
