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
