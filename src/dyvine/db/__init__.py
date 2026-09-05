"""Postgres-backed persistence for Dyvine.

Services depend only on the repository protocols
(:class:`OperationRepository`, :class:`WatchRepository`) and the
record dataclasses. The container wires the Postgres implementations
plus a :class:`RepositoryJanitor` liveness loop; tests inject the
in-memory fakes from ``tests.support.fake_repos`` or run the contract
suite against a real database.
"""

from __future__ import annotations

from .janitor import (
    HEARTBEAT_INTERVAL_SECONDS,
    ORPHAN_STALE_AFTER_SECONDS,
    PURGE_INTERVAL_SECONDS,
    RepositoryJanitor,
)
from .models import Base, OperationRow, WatchSubscriptionRow
from .postgres import PostgresOperationRepository, PostgresWatchRepository
from .protocols import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    OperationRepository,
    WatchRepository,
)
from .records import OperationRecord, WatchSubscriptionRecord
from .session import DatabaseSessionFactory

__all__ = [
    "ACTIVE_STATUSES",
    "TERMINAL_STATUSES",
    "HEARTBEAT_INTERVAL_SECONDS",
    "ORPHAN_STALE_AFTER_SECONDS",
    "PURGE_INTERVAL_SECONDS",
    "Base",
    "DatabaseSessionFactory",
    "OperationRecord",
    "OperationRepository",
    "OperationRow",
    "PostgresOperationRepository",
    "PostgresWatchRepository",
    "RepositoryJanitor",
    "WatchRepository",
    "WatchSubscriptionRecord",
    "WatchSubscriptionRow",
]
