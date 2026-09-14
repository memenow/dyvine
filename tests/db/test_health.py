"""Tests for passive database health tracking."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import OperationalError

from dyvine.core.exceptions import OperationNotFoundError
from dyvine.db import DatabaseHealthTracker
from dyvine.db.postgres import PostgresOperationRepository


def test_tracker_starts_unknown() -> None:
    """No observation exists before the first repository call."""
    tracker = DatabaseHealthTracker()
    status, checked_at = tracker.snapshot
    assert status == "unknown"
    assert checked_at is None


def test_tracker_records_transitions_with_timestamps() -> None:
    """Success and failure flip the state and stamp each change."""
    tracker = DatabaseHealthTracker()
    tracker.note_success()
    status, checked_at = tracker.snapshot
    assert status == "available"
    assert checked_at is not None
    tracker.note_failure()
    status, failed_at = tracker.snapshot
    assert status == "unavailable"
    assert failed_at is not None
    assert failed_at >= checked_at


def _repo_with_session(
    session: MagicMock, tracker: DatabaseHealthTracker | None
) -> PostgresOperationRepository:
    """Build an operation repository around a stubbed session."""
    sessions = MagicMock()
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    sessions.session = MagicMock(return_value=context)
    return PostgresOperationRepository(sessions, owner_id="owner", health=tracker)


async def test_tracked_counts_domain_errors_as_success() -> None:
    """A "not found" answer proves the database is reachable."""
    tracker = DatabaseHealthTracker()
    session = MagicMock()
    session.get = AsyncMock(return_value=None)
    repo = _repo_with_session(session, tracker)

    with pytest.raises(OperationNotFoundError):
        await repo.get_operation("missing")
    assert tracker.snapshot[0] == "available"


async def test_tracked_counts_db_errors_as_failure() -> None:
    """SQLAlchemy errors mark the database unreachable."""
    tracker = DatabaseHealthTracker()
    session = MagicMock()
    session.get = AsyncMock(
        side_effect=OperationalError("SELECT 1", {}, Exception("boom"))
    )
    repo = _repo_with_session(session, tracker)

    with pytest.raises(OperationalError):
        await repo.get_operation("any")
    assert tracker.snapshot[0] == "unavailable"


async def test_tracked_counts_os_errors_as_failure() -> None:
    """Raw connection errors mark the database unreachable."""
    tracker = DatabaseHealthTracker()
    session = MagicMock()
    session.get = AsyncMock(side_effect=ConnectionRefusedError("refused"))
    repo = _repo_with_session(session, tracker)

    with pytest.raises(ConnectionRefusedError):
        await repo.get_operation("any")
    assert tracker.snapshot[0] == "unavailable"


async def test_tracked_skips_recording_without_tracker() -> None:
    """Repositories built without a tracker behave as before."""
    session = MagicMock()
    session.get = AsyncMock(return_value=None)
    repo = _repo_with_session(session, None)

    with pytest.raises(OperationNotFoundError):
        await repo.get_operation("missing")
