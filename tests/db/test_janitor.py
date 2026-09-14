"""Tests for the repository liveness loop."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import fake_repos
import pytest
from fake_repos import FakeOperationRepository, FakeOperationState

from dyvine.db import RepositoryJanitor


async def test_run_once_heartbeats_and_sweeps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One pass beats owned rows and fails stale rows owned elsewhere."""
    state = FakeOperationState()
    owner_a = FakeOperationRepository(owner_id="a", state=state)
    owner_b = FakeOperationRepository(owner_id="b", state=state)

    old = (datetime.now(UTC) - timedelta(seconds=3600)).isoformat()
    monkeypatch.setattr(fake_repos, "_now_iso", lambda: old)
    stale_id = (
        await owner_a.create_operation(
            operation_type="t",
            subject_id="stale",
            status="running",
            message="live",
        )
    ).operation_id
    fresh_id = (
        await owner_b.create_operation(
            operation_type="t",
            subject_id="fresh",
            status="running",
            message="live",
        )
    ).operation_id
    monkeypatch.undo()

    janitor = RepositoryJanitor(owner_b, retention_days=0)
    await janitor.run_once()

    # B's heartbeat re-armed its own row; A's stale row was failed.
    assert (await owner_b.get_operation(fresh_id)).status == "running"
    assert (await owner_a.get_operation(stale_id)).status == "failed"


async def test_run_once_purges_only_when_due_and_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retention purges old terminal rows; ``0`` disables it."""
    repo = FakeOperationRepository(owner_id="a")
    monkeypatch.setattr(fake_repos, "_now_iso", lambda: "2020-01-01T00:00:00+00:00")
    old_id = (
        await repo.create_operation(
            operation_type="t",
            subject_id="old",
            status="completed",
            message="done",
        )
    ).operation_id
    monkeypatch.undo()

    janitor = RepositoryJanitor(repo, retention_days=30, purge_interval_seconds=0)
    await janitor.run_once()
    assert old_id not in state_rows(repo)

    # Disabled retention leaves even ancient rows alone.
    monkeypatch.setattr(fake_repos, "_now_iso", lambda: "2020-01-01T00:00:00+00:00")
    kept_id = (
        await repo.create_operation(
            operation_type="t",
            subject_id="old-2",
            status="completed",
            message="done",
        )
    ).operation_id
    monkeypatch.undo()
    disabled = RepositoryJanitor(repo, retention_days=0, purge_interval_seconds=0)
    await disabled.run_once()
    assert kept_id in state_rows(repo)


def state_rows(repo: FakeOperationRepository) -> set[str]:
    """Return the operation IDs currently held by a fake."""
    return set(repo._rows)


async def test_run_forever_loops_until_cancelled() -> None:
    """The loop repeats passes and propagates cancellation."""
    repo = FakeOperationRepository(owner_id="a")
    janitor = RepositoryJanitor(repo, retention_days=0, heartbeat_interval=0.01)
    task = asyncio.create_task(janitor.run_forever())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_run_forever_survives_failed_pass() -> None:
    """One transient failure is logged and skipped, not fatal."""
    repo = FakeOperationRepository(owner_id="a")
    janitor = RepositoryJanitor(repo, retention_days=0, heartbeat_interval=0.01)
    calls = 0
    real_run_once = janitor.run_once

    async def flaky() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient db error")
        await real_run_once()

    janitor.run_once = flaky  # type: ignore[method-assign]
    task = asyncio.create_task(janitor.run_forever())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls >= 2


async def test_run_purge_only_purges_old_terminal_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The purge-only pass deletes terminal rows past retention."""
    repo = FakeOperationRepository(owner_id="a")
    monkeypatch.setattr(fake_repos, "_now_iso", lambda: "2020-01-01T00:00:00+00:00")
    old_id = (
        await repo.create_operation(
            operation_type="t",
            subject_id="old",
            status="completed",
            message="done",
        )
    ).operation_id
    monkeypatch.undo()

    janitor = RepositoryJanitor(repo, retention_days=30)
    await janitor.run_purge_only()
    assert old_id not in state_rows(repo)


async def test_run_purge_only_is_noop_when_disabled() -> None:
    """Disabled retention never reaches the store."""
    repo = FakeOperationRepository(owner_id="a")

    async def _boom(cutoff_iso: str) -> int:
        raise AssertionError("purge must not run when retention is disabled")

    repo.purge_terminal_before = _boom  # type: ignore[method-assign]
    janitor = RepositoryJanitor(repo, retention_days=0)
    await janitor.run_purge_only()


async def test_run_purge_forever_loops_until_cancelled() -> None:
    """The purge loop repeats passes and propagates cancellation."""
    repo = FakeOperationRepository(owner_id="a")
    janitor = RepositoryJanitor(repo, retention_days=30, purge_interval_seconds=0.01)
    calls = 0
    real_purge_only = janitor.run_purge_only

    async def counting() -> None:
        nonlocal calls
        calls += 1
        await real_purge_only()

    janitor.run_purge_only = counting  # type: ignore[method-assign]
    task = asyncio.create_task(janitor.run_purge_forever())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls >= 1


async def test_run_purge_forever_survives_failed_pass() -> None:
    """One transient purge failure is logged and skipped, not fatal."""
    repo = FakeOperationRepository(owner_id="a")
    janitor = RepositoryJanitor(repo, retention_days=30, purge_interval_seconds=0.01)
    calls = 0
    real_purge_only = janitor.run_purge_only

    async def flaky() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient db error")
        await real_purge_only()

    janitor.run_purge_only = flaky  # type: ignore[method-assign]
    task = asyncio.create_task(janitor.run_purge_forever())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls >= 2


async def test_run_purge_forever_returns_immediately_when_disabled() -> None:
    """Disabled retention never starts the purge loop."""
    repo = FakeOperationRepository(owner_id="a")
    janitor = RepositoryJanitor(repo, retention_days=0)
    await asyncio.wait_for(janitor.run_purge_forever(), timeout=1.0)
