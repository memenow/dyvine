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
    FakeProfileRepository,
    FakeQueueRepository,
    FakeQueueState,
    FakeRoundRepository,
    FakeSeedRepository,
    FakeSendStatusRepository,
    FakeWatchRepository,
)
from sqlalchemy import text

import dyvine.db.postgres as postgres_module
from dyvine.core.exceptions import (
    DeliveryRoundNotFoundError,
    OperationNotFoundError,
    QueueEntryNotFoundError,
    RateLimitError,
    SeedAccountNotFoundError,
    SendStatusNotFoundError,
    ServiceError,
    UserProfileNotFoundError,
    WatchDuplicateError,
    WatchSubscriptionNotFoundError,
)
from dyvine.db import (
    DatabaseSessionFactory,
    OperationRepository,
    PostgresOperationRepository,
    PostgresProfileRepository,
    PostgresQueueRepository,
    PostgresRoundRepository,
    PostgresSeedRepository,
    PostgresSendStatusRepository,
    PostgresWatchRepository,
    ProfileRepository,
    QueueRepository,
    RoundRepository,
    SeedRepository,
    SendStatusRepository,
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
    make_queue: Callable[[str], QueueRepository]
    make_send: Callable[[], SendStatusRepository]
    make_seed: Callable[[], SeedRepository]
    make_profile: Callable[[], ProfileRepository]
    make_round: Callable[[], RoundRepository]
    seed_legacy: Callable[..., Any]


async def _seed_legacy_postgres(
    factory: DatabaseSessionFactory, username: str, **fields: Any
) -> None:
    """Insert one legacy ``user_send_status`` row over a fresh session."""
    stamp = "2026-08-12T07:13:06+00:00"
    async with factory.session() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO user_send_status "
                    "(username, local_files, sent_files, failed_files, "
                    "status, failed_details, created_at, updated_at) "
                    "VALUES (:username, :local_files, :sent_files, "
                    ":failed_files, :status, :failed_details, "
                    ":created_at, :updated_at)"
                ),
                {
                    "username": username,
                    "local_files": fields.get("local_files", 0),
                    "sent_files": fields.get("sent_files", 0),
                    "failed_files": fields.get("failed_files", 0),
                    "status": fields.get("status", "pending"),
                    "failed_details": fields.get("failed_details", ""),
                    "created_at": stamp,
                    "updated_at": stamp,
                },
            )


@pytest.fixture(params=["fake", "postgres"])
async def backend(request: pytest.FixtureRequest) -> Any:
    """Yield a backend context; Postgres tables are truncated first.

    The container only starts for the ``postgres`` leg: the URL
    fixture is pulled lazily so the ``fake`` leg stays Docker-free.
    """
    if request.param == "fake":
        state = FakeOperationState()
        queue_state = FakeQueueState()
        send_repo = FakeSendStatusRepository()
        yield BackendContext(
            name="fake",
            make_ops=lambda owner: FakeOperationRepository(owner_id=owner, state=state),
            make_watch=FakeWatchRepository,
            make_queue=lambda owner: FakeQueueRepository(
                owner_id=owner, state=queue_state
            ),
            make_send=lambda: send_repo,
            make_seed=FakeSeedRepository,
            make_profile=FakeProfileRepository,
            make_round=FakeRoundRepository,
            seed_legacy=send_repo.seed_legacy,
        )
        return
    postgres_url: str = request.getfixturevalue("postgres_url")
    factory = DatabaseSessionFactory(postgres_url)
    async with factory.session() as session:
        async with session.begin():
            await session.execute(
                text(
                    # One statement naming children and parents together:
                    # Postgres forbids truncating a referenced parent
                    # alone, and the session database is shared with the
                    # ledger suites.
                    "TRUNCATE TABLE operations, watch_subscriptions, "
                    "download_queue, send_status, user_send_status, "
                    "seed_accounts, user_profiles, delivery_files, "
                    "delivery_groups, delivery_legacy_evidence, "
                    "legacy_excluded_nicknames, delivery_rounds"
                )
            )
    try:
        yield BackendContext(
            name="postgres",
            make_ops=lambda owner: PostgresOperationRepository(factory, owner_id=owner),
            make_watch=lambda: PostgresWatchRepository(factory),
            make_queue=lambda owner: PostgresQueueRepository(factory, owner_id=owner),
            make_send=lambda: PostgresSendStatusRepository(factory),
            make_seed=lambda: PostgresSeedRepository(factory),
            make_profile=lambda: PostgresProfileRepository(factory),
            make_round=lambda: PostgresRoundRepository(factory),
            seed_legacy=lambda username, **fields: _seed_legacy_postgres(
                factory, username, **fields
            ),
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


async def test_update_operation_rejects_unknown_fields(
    backend: BackendContext,
) -> None:
    """Unknown fields raise; empty updates return the row unchanged."""
    repo = backend.make_ops("owner-a")
    created = await repo.create_operation(
        operation_type="t",
        subject_id="user-5",
        status="pending",
        message="queued",
    )
    with pytest.raises(ValueError, match="not_a_column"):
        await repo.update_operation(created.operation_id, not_a_column="x")
    unchanged = await repo.update_operation(created.operation_id)
    assert unchanged == created
    with pytest.raises(OperationNotFoundError):
        await repo.update_operation("no-such-op", message="x")


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
    factory = DatabaseSessionFactory(url)
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


async def _read_heartbeat(
    backend: BackendContext,
    request: pytest.FixtureRequest,
    operation_id: str,
) -> str | None:
    """Read a row's raw heartbeat the way the sweep sees it.

    Fake leg reads the sidecar entry; Postgres leg selects the
    column over its own short-lived session.
    """
    if backend.name == "fake":
        owner = backend.make_ops("owner-a")
        return owner._heartbeats.get(operation_id)  # type: ignore[attr-defined]
    url: str = request.getfixturevalue("postgres_url")
    factory = DatabaseSessionFactory(url)
    try:
        async with factory.session() as session:
            return await session.scalar(
                text("SELECT heartbeat_at FROM operations WHERE operation_id = :id"),
                {"id": operation_id},
            )
    finally:
        await factory.aclose()


async def test_sweep_preserves_stale_heartbeat(
    backend: BackendContext,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """A swept row keeps its stale heartbeat for the next sweep.

    The sweep rewrites status/message/error/updated_at only; if it
    refreshed the heartbeat (e.g. via ``update_operation``), a
    resurrected row would look alive and escape the next sweep.
    """
    owner_a = backend.make_ops("owner-a")
    owner_b = backend.make_ops("owner-b")
    with _freeze(backend, monkeypatch, OLD_STAMP):
        created = await owner_a.create_operation(
            operation_type="t",
            subject_id="stale-beat",
            status="running",
            message="live",
        )
    assert await owner_b.sweep_orphans(stale_after_seconds=60.0) == 1
    assert await _read_heartbeat(backend, request, created.operation_id) == OLD_STAMP


async def test_latest_breaks_full_timestamp_ties_by_id(
    backend: BackendContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Frozen-clock ties resolve identically on both backends."""
    repo = backend.make_ops("owner-a")
    with _freeze(backend, monkeypatch, OLD_STAMP):
        first = await repo.create_operation(
            operation_type="t",
            subject_id="tied",
            status="pending",
            message="one",
        )
        second = await repo.create_operation(
            operation_type="t",
            subject_id="tied",
            status="pending",
            message="two",
        )
    expected = max(
        (first, second),
        key=lambda row: (row.updated_at, row.created_at, row.operation_id),
    )
    latest = await repo.get_latest_operation_for_subject("tied")
    assert latest.operation_id == expected.operation_id
    # Deterministic across calls, not an arbitrary row per query.
    again = await repo.get_latest_operation_for_subject("tied")
    assert again.operation_id == expected.operation_id


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
    with pytest.raises(WatchDuplicateError) as exc_info:
        await repo.create_subscription(**_subscription_kwargs("user-3"))
    assert exc_info.value.details == {"user_id": "user-3"}


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


async def test_create_subscription_duplicate_id_raises(
    backend: BackendContext,
) -> None:
    """An explicit ID collision raises on both backends, never merges."""
    repo = backend.make_watch()
    await repo.create_subscription(
        **_subscription_kwargs("user-a"),
        subscription_id="fixed-id",
    )
    with pytest.raises(WatchDuplicateError) as exc_info:
        await repo.create_subscription(
            **_subscription_kwargs("user-b"),
            subscription_id="fixed-id",
        )
    # Both backends map an explicit-ID collision to the same
    # user-keyed error; callers converge on the user row.
    assert exc_info.value.details == {"user_id": "user-b"}
    assert await repo.count_subscriptions() == 1


async def test_update_subscription_rejects_unknown_fields(
    backend: BackendContext,
) -> None:
    """Unknown fields raise; empty updates return the row unchanged."""
    repo = backend.make_watch()
    created = await repo.create_subscription(**_subscription_kwargs("user-8"))
    with pytest.raises(ValueError, match="not_a_column"):
        await repo.update_subscription(created.subscription_id, not_a_column="x")
    unchanged = await repo.update_subscription(created.subscription_id)
    assert unchanged == created
    with pytest.raises(WatchSubscriptionNotFoundError):
        await repo.update_subscription("no-such-sub", enabled=False)


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


def _queue_kwargs(key: str, **overrides: Any) -> dict[str, Any]:
    """Build queue upsert kwargs with sane defaults."""
    sec = overrides.pop("sec_user_id", f"sec-{key}")
    kwargs: dict[str, Any] = {
        "key": key,
        "round": "round-1",
        "nickname": f"nick-{key}",
        "sec_user_id": sec,
        "mode": "full",
        "status": "pending",
    }
    kwargs.update(overrides)
    return kwargs


async def _ensure_round(backend: BackendContext, *rounds: str) -> None:
    """Seed round headers on the Postgres leg (FK parents).

    The fake leg has no foreign keys; the Postgres leg rejects queue
    rows whose round header is missing, so queue tests seed every
    round they touch. Mirrors the production ``enqueue_round``-first
    ordering.
    """
    if backend.name == "fake":
        return
    repo = backend.make_round()
    for name in rounds:
        await repo.upsert_round(round=name)


async def _seed_legacy(backend: BackendContext, username: str, **fields: Any) -> None:
    """Seed one legacy row on either leg (fake seeder is sync)."""
    result = backend.seed_legacy(username, **fields)
    if asyncio.iscoroutine(result):
        await result


async def test_queue_upsert_round_trip(backend: BackendContext) -> None:
    """Upserted fields echo back; a second upsert replaces the row."""
    repo = backend.make_queue("owner-a")
    await _ensure_round(backend, "round-1")
    created = await repo.upsert_entry(
        **_queue_kwargs("k1", chat_id="chat-1", extra={"note": "x"})
    )
    assert created.extra == {"note": "x"}
    assert created.attempts == 0
    assert created.created_at == created.updated_at
    assert (await repo.get_entry("k1")) == created

    replaced = await repo.upsert_entry(
        **_queue_kwargs("k1", status="op_done", attempts=2)
    )
    assert replaced.status == "op_done"
    assert replaced.attempts == 2
    assert replaced.chat_id is None
    assert replaced.created_at == created.created_at
    assert await repo.count_entries() == 1

    # Omitted attempts/extra keep their stored values on conflict.
    patched = await repo.upsert_entry(**_queue_kwargs("k1", status="pending"))
    assert patched.attempts == 2
    assert patched.extra == {"note": "x"}
    wiped = await repo.upsert_entry(**_queue_kwargs("k1", extra={}))
    assert wiped.extra == {}
    assert wiped.attempts == 2


async def test_queue_upsert_preserves_existing_owner(
    backend: BackendContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An upsert over a live claim touches neither owner nor heartbeat.

    After replica B upserts a row replica A claimed, B's own release
    still sees the row as foreign (owner A) with a stale heartbeat
    and frees it; if the upsert stole the claim or refreshed the
    beat, B would spare a row whose owner is gone.
    """
    owner_a = backend.make_queue("owner-a")
    owner_b = backend.make_queue("owner-b")
    await _ensure_round(backend, "round-1")
    with _freeze(backend, monkeypatch, OLD_STAMP):
        await owner_a.upsert_entry(**_queue_kwargs("owned", status="downloading"))
        await owner_b.upsert_entry(**_queue_kwargs("owned", status="downloading"))
    assert await owner_b.release_stale(stale_after_seconds=60.0, max_attempts=3) == 1
    assert (await owner_a.get_entry("owned")).status == "pending"


async def test_queue_get_missing_raises(backend: BackendContext) -> None:
    """Unknown keys raise ``QueueEntryNotFoundError``."""
    with pytest.raises(QueueEntryNotFoundError):
        await backend.make_queue("owner-a").get_entry("no-such-key")


async def test_queue_upsert_concurrent_same_key_converges(
    backend: BackendContext,
) -> None:
    """Concurrent upserts of one key converge; none raises."""
    repo = backend.make_queue("owner-a")
    await _ensure_round(backend, "round-1")
    await asyncio.gather(
        *[
            repo.upsert_entry(**_queue_kwargs("race", nickname=f"nick-{index}"))
            for index in range(6)
        ]
    )
    assert await repo.count_entries() == 1
    final = await repo.get_entry("race")
    assert final.nickname in {f"nick-{index}" for index in range(6)}
    assert final.attempts == 0


async def test_queue_upsert_leaves_stale_heartbeat_untouched(
    backend: BackendContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A metadata upsert over a stale row does not extend its lease."""
    owner_a = backend.make_queue("owner-a")
    owner_b = backend.make_queue("owner-b")
    owner_c = backend.make_queue("owner-c")
    await _ensure_round(backend, "round-1")
    with _freeze(backend, monkeypatch, OLD_STAMP):
        await owner_a.upsert_entry(**_queue_kwargs("stale-row", status="downloading"))
    await owner_b.upsert_entry(
        **_queue_kwargs("stale-row", status="downloading", op_message="patch")
    )
    assert await owner_c.release_stale(stale_after_seconds=60.0, max_attempts=3) == 1
    assert (await owner_a.get_entry("stale-row")).status == "pending"


async def test_queue_list_filters_and_counts(
    backend: BackendContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Listing filters by round/status, oldest-first, with pagination."""
    repo = backend.make_queue("owner-a")
    await _ensure_round(backend, "round-1", "round-2")
    with _freeze(backend, monkeypatch, "2020-01-01T00:00:00+00:00"):
        await repo.upsert_entry(**_queue_kwargs("old"))
    with _freeze(backend, monkeypatch, "2020-01-01T00:00:01+00:00"):
        await repo.upsert_entry(**_queue_kwargs("r2-a", round="round-2"))
        await repo.upsert_entry(
            **_queue_kwargs("r2-b", status="op_done", round="round-2")
        )
    ordered = await repo.list_entries()
    assert [row.key for row in ordered] == ["old", "r2-a", "r2-b"]
    assert [row.key for row in await repo.list_entries(round="round-2")] == [
        "r2-a",
        "r2-b",
    ]
    assert [row.key for row in await repo.list_entries(status="pending")] == [
        "old",
        "r2-a",
    ]
    assert [row.key for row in await repo.list_entries(limit=1, offset=1)] == ["r2-a"]
    assert await repo.count_entries() == 3
    assert await repo.count_entries(round="round-2", status="pending") == 1
    tallies = await repo.count_by_status()
    assert tallies == {"pending": 2, "op_done": 1}
    assert await repo.count_by_status(round="round-2") == {
        "pending": 1,
        "op_done": 1,
    }
    assert await repo.count_by_status(round="round-9") == {}


async def test_queue_claim_takes_oldest_pending(
    backend: BackendContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claims pop oldest-first and flip the winner to downloading."""
    repo = backend.make_queue("owner-a")
    await _ensure_round(backend, "round-1", "round-9")
    with _freeze(backend, monkeypatch, "2020-01-01T00:00:00+00:00"):
        await repo.upsert_entry(**_queue_kwargs("first"))
    with _freeze(backend, monkeypatch, "2020-01-01T00:00:01+00:00"):
        await repo.upsert_entry(**_queue_kwargs("second"))
    await repo.upsert_entry(**_queue_kwargs("other-round", round="round-9"))

    claimed = await repo.claim_next(round="round-1")
    assert claimed is not None
    assert claimed.key == "first"
    assert claimed.status == "downloading"
    assert (await repo.get_entry("first")).status == "downloading"
    assert (await repo.claim_next(round="round-1")) is not None
    assert await repo.claim_next(round="round-1") is None
    # Other rounds are untouched by a filtered claim.
    assert (await repo.get_entry("other-round")).status == "pending"


async def test_queue_claim_skips_busy_serial_groups(
    backend: BackendContext,
) -> None:
    """Same-nickname accounts never run concurrently."""
    repo = backend.make_queue("owner-a")
    await _ensure_round(backend, "round-1")
    await repo.upsert_entry(**_queue_kwargs("solo"))
    await repo.upsert_entry(**_queue_kwargs("pair-a", serial_group="g1"))
    await repo.upsert_entry(**_queue_kwargs("pair-b", serial_group="g1"))
    assert (await repo.claim_next()).key == "solo"  # type: ignore[union-attr]
    assert (await repo.claim_next()).key == "pair-a"  # type: ignore[union-attr]
    # pair-b shares pair-a's busy group, so the queue reads empty.
    assert await repo.claim_next() is None
    await repo.update_entry("pair-a", status="op_done")
    assert (await repo.claim_next()).key == "pair-b"  # type: ignore[union-attr]


async def test_queue_claim_never_double_issues(
    backend: BackendContext,
) -> None:
    """Concurrent claimers split rows exactly, none shared."""
    owner_a = backend.make_queue("owner-a")
    owner_b = backend.make_queue("owner-b")
    await _ensure_round(backend, "round-1")
    for index in range(4):
        await owner_a.upsert_entry(**_queue_kwargs(f"c{index}"))
    results = await asyncio.gather(
        owner_a.claim_next(),
        owner_b.claim_next(),
        owner_a.claim_next(),
        owner_b.claim_next(),
    )
    keys = [row.key for row in results if row is not None]
    assert sorted(keys) == ["c0", "c1", "c2", "c3"]
    assert await owner_a.claim_next() is None


async def test_queue_claim_filter_is_atomic_and_cannot_take_next_batch(
    backend: BackendContext,
) -> None:
    """Concurrent workers stay within the selected account pair."""
    owner_a = backend.make_queue("owner-a")
    owner_b = backend.make_queue("owner-b")
    await _ensure_round(backend, "round-1")
    for key in ("pair-a", "pair-b", "next-a"):
        await owner_a.upsert_entry(**_queue_kwargs(key))
    allowed = {"pair-a", "pair-b"}
    claimed = await asyncio.gather(
        owner_a.claim_next(keys=allowed),
        owner_b.claim_next(keys=allowed),
        owner_a.claim_next(keys=allowed),
    )
    assert {row.key for row in claimed if row is not None} == allowed
    assert (await owner_a.get_entry("next-a")).status == "pending"


async def test_queue_claim_serial_group_is_mutually_exclusive_under_race(
    backend: BackendContext,
) -> None:
    """Two replicas racing one group split it: at most one downloading.

    The busy-group pre-filter only sees committed rows, so without the
    claim-time group lock both racers would lock different same-group
    rows and flip both to downloading.
    """
    owner_a = backend.make_queue("owner-a")
    owner_b = backend.make_queue("owner-b")
    await _ensure_round(backend, "round-1")
    await owner_a.upsert_entry(**_queue_kwargs("g-a", serial_group="g1"))
    await owner_a.upsert_entry(**_queue_kwargs("g-b", serial_group="g1"))
    first, second = await asyncio.gather(owner_a.claim_next(), owner_b.claim_next())
    winners = [row for row in (first, second) if row is not None]
    assert len(winners) == 1
    assert await owner_a.count_entries(status="downloading") == 1


async def test_queue_release_stale_splits_between_replicas(
    backend: BackendContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two replicas sweeping together split the stale set: no double bump."""
    owner_a = backend.make_queue("owner-a")
    owner_b = backend.make_queue("owner-b")
    await _ensure_round(backend, "round-1")
    with _freeze(backend, monkeypatch, OLD_STAMP):
        await owner_a.upsert_entry(**_queue_kwargs("sweep-a", status="downloading"))
        await owner_a.upsert_entry(**_queue_kwargs("sweep-b", status="downloading"))
    first, second = await asyncio.gather(
        owner_b.release_stale(stale_after_seconds=60.0, max_attempts=8),
        owner_b.release_stale(stale_after_seconds=60.0, max_attempts=8),
    )
    assert first + second == 2
    for key in ("sweep-a", "sweep-b"):
        freed = await owner_a.get_entry(key)
        assert freed.status == "pending"
        assert freed.attempts == 1


async def test_queue_update_merges_and_rejects_unknown(
    backend: BackendContext,
) -> None:
    """Known fields merge; unknown fields raise loud typos."""
    repo = backend.make_queue("owner-a")
    await _ensure_round(backend, "round-1")
    created = await repo.upsert_entry(**_queue_kwargs("u1"))
    updated = await repo.update_entry(
        "u1", status="op_done", op_status="completed", attempts=3
    )
    assert updated.status == "op_done"
    assert updated.op_status == "completed"
    assert updated.attempts == 3
    assert updated.nickname == created.nickname
    assert updated.created_at == created.created_at
    with pytest.raises(ValueError, match="not_a_column"):
        await repo.update_entry("u1", not_a_column="x")
    unchanged = await repo.update_entry("u1")
    assert unchanged == updated
    with pytest.raises(QueueEntryNotFoundError):
        await repo.update_entry("no-such-key", status="failed")


async def test_queue_update_none_extra_clears(backend: BackendContext) -> None:
    """An explicit ``extra=None`` clears to ``{}``, not ``TypeError``."""
    repo = backend.make_queue("owner-a")
    await _ensure_round(backend, "round-1")
    await repo.upsert_entry(**_queue_kwargs("e1", extra={"a": 1}))
    updated = await repo.update_entry("e1", extra=None)
    assert updated.extra == {}


async def test_queue_release_stale_requeues_with_backoff(
    backend: BackendContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stale foreign rows return to pending with attempts bumped."""
    owner_a = backend.make_queue("owner-a")
    owner_b = backend.make_queue("owner-b")
    await _ensure_round(backend, "round-1")
    with _freeze(backend, monkeypatch, OLD_STAMP):
        await owner_a.upsert_entry(**_queue_kwargs("stale"))
    claimed = await owner_a.claim_next()
    assert claimed is not None and claimed.key == "stale"
    # Age the heartbeat without touching the fake/ORM internals twice.
    with _freeze(backend, monkeypatch, OLD_STAMP):
        await owner_a.update_entry("stale", op_message="working")
    released = await owner_b.release_stale(stale_after_seconds=60.0, max_attempts=8)
    assert released == 1
    freed = await owner_a.get_entry("stale")
    assert freed.status == "pending"
    assert freed.attempts == 1
    assert freed.updated_at > OLD_STAMP


async def test_queue_release_stale_only_touches_selected_round(
    backend: BackendContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A weekly runner does not mutate frozen work in another round."""
    owner_a = backend.make_queue("owner-a")
    owner_b = backend.make_queue("owner-b")
    await _ensure_round(backend, "round-1", "round-2")
    with _freeze(backend, monkeypatch, OLD_STAMP):
        await owner_a.upsert_entry(**_queue_kwargs("old-r1", round="round-1"))
        await owner_a.upsert_entry(**_queue_kwargs("old-r2", round="round-2"))
        await owner_a.claim_next(round="round-1")
        await owner_a.claim_next(round="round-2")
        await owner_a.update_entry("old-r1", op_message="working")
        await owner_a.update_entry("old-r2", op_message="working")
    assert (
        await owner_b.release_stale(
            stale_after_seconds=60.0, max_attempts=8, round="round-1"
        )
        == 1
    )
    assert (await owner_a.get_entry("old-r1")).status == "pending"
    assert (await owner_a.get_entry("old-r2")).status == "downloading"


async def test_queue_release_stale_exhausts_and_spares(
    backend: BackendContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exhausted rows flip to op_issue; own and fresh rows are spared."""
    owner_a = backend.make_queue("owner-a")
    owner_b = backend.make_queue("owner-b")
    await _ensure_round(backend, "round-1")
    with _freeze(backend, monkeypatch, OLD_STAMP):
        await owner_a.upsert_entry(**_queue_kwargs("tired", attempts=8))
        await owner_a.upsert_entry(**_queue_kwargs("mine", attempts=0))
    tired = await owner_a.claim_next()
    mine = await owner_a.claim_next()
    assert tired is not None and mine is not None
    with _freeze(backend, monkeypatch, OLD_STAMP):
        await owner_a.update_entry(tired.key, op_message="old")
        await owner_a.update_entry(mine.key, op_message="old")
    fresh = await owner_b.upsert_entry(**_queue_kwargs("fresh"))
    await owner_b.claim_next(round="round-1")  # claims fresh (newest pending)
    assert (await owner_b.get_entry("fresh")).status == "downloading"

    released = await owner_b.release_stale(stale_after_seconds=60.0, max_attempts=8)
    assert released == 2
    assert (await owner_a.get_entry("tired")).status == "op_issue"
    assert (await owner_a.get_entry("mine")).status == "pending"
    # Owner-b's own fresh row is untouched.
    assert (await owner_b.get_entry("fresh")).status == "downloading"
    assert fresh.key == "fresh"


async def test_send_status_round_trip(backend: BackendContext) -> None:
    """Upserts echo back; sec lookup and batch listing work."""
    repo = backend.make_send()
    created = await repo.upsert_send_status(
        nickname="nick-1",
        sec_user_id="sec-1",
        chat_id="chat-1",
        batch="batch",
        total_files=10,
        sent_files=9,
        failed_files=1,
        status="partial",
    )
    assert created.created_at == created.updated_at
    assert (await repo.get_send_status("nick-1")) == created
    assert (await repo.get_send_status_by_sec("sec-1")) == created
    assert await repo.get_send_status_by_sec("nobody") is None
    await repo.upsert_send_status(nickname="nick-2", batch="resend8")
    assert [row.nickname for row in await repo.list_send_status()] == [
        "nick-1",
        "nick-2",
    ]
    assert [row.nickname for row in await repo.list_send_status(batch="resend8")] == [
        "nick-2"
    ]
    replaced = await repo.upsert_send_status(nickname="nick-1", status="completed")
    assert replaced.status == "completed"
    assert replaced.sent_files is None
    assert replaced.created_at == created.created_at
    with pytest.raises(SendStatusNotFoundError):
        await repo.get_send_status("ghost")


async def test_legacy_user_send_status_reads(backend: BackendContext) -> None:
    """Legacy rows resolve by username; unknown names raise."""
    await _seed_legacy(backend, "old-user", sent_files=7, status="completed")
    repo = backend.make_send()
    found = await repo.get_user_send_status("old-user")
    assert found.sent_files == 7
    assert found.status == "completed"
    with pytest.raises(SendStatusNotFoundError):
        await repo.get_user_send_status("ghost-user")


async def test_seed_round_trip_and_exclusion(backend: BackendContext) -> None:
    """Seeds upsert idempotently; excluded rows filter by default."""
    repo = backend.make_seed()
    created = await repo.upsert_seed(
        sec_user_id="sec-1",
        nickname="nick-1",
        source_url="https://v.douyin.com/x/",
        batch="seed-1",
    )
    assert created.source == "seed"
    assert created.excluded is False
    assert (await repo.get_seed("sec-1")) == created
    await repo.upsert_seed(sec_user_id="sec-2", excluded=True)
    assert [row.sec_user_id for row in await repo.list_seeds()] == ["sec-1"]
    assert len(await repo.list_seeds(include_excluded=True)) == 2
    assert await repo.count_seeds() == 1
    assert await repo.count_seeds(include_excluded=True) == 2
    with pytest.raises(SeedAccountNotFoundError):
        await repo.get_seed("ghost-sec")


async def test_profile_upsert_patches_columns(backend: BackendContext) -> None:
    """Snapshots insert fully and patch partially."""
    repo = backend.make_profile()
    created = await repo.upsert_profile(
        sec_user_id="sec-1", nickname="nick-1", follower_count=100
    )
    assert created.nickname == "nick-1"
    assert created.follower_count == 100
    assert created.avatar_url is None
    patched = await repo.upsert_profile(
        sec_user_id="sec-1", avatar_url="https://img/x.jpg"
    )
    assert patched.avatar_url == "https://img/x.jpg"
    assert patched.nickname == "nick-1"
    assert patched.created_at == created.created_at
    assert (await repo.get_profile("sec-1")) == patched
    with pytest.raises(UserProfileNotFoundError):
        await repo.get_profile("ghost-sec")


async def test_profile_upsert_rejects_unknown_fields(
    backend: BackendContext,
) -> None:
    """Unknown snapshot columns raise instead of vanishing."""
    repo = backend.make_profile()
    with pytest.raises(ValueError, match="not_a_column"):
        await repo.upsert_profile(sec_user_id="sec-9", not_a_column="z")


async def test_round_upsert_and_list(backend: BackendContext) -> None:
    """Round headers upsert idempotently and list in creation order."""
    repo = backend.make_round()
    first = await repo.upsert_round(round="r1", note="first")
    await repo.upsert_round(round="r2")
    assert (await repo.get_round("r1")) == first
    assert [row.round for row in await repo.list_rounds()] == ["r1", "r2"]
    touched = await repo.upsert_round(round="r1")
    assert touched.note == "first"
    assert touched.created_at == first.created_at
    renamed = await repo.upsert_round(round="r1", note="updated")
    assert renamed.note == "updated"
    with pytest.raises(DeliveryRoundNotFoundError):
        await repo.get_round("ghost-round")
