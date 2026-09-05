"""In-memory repository doubles for unit tests.

These fakes implement :mod:`dyvine.db.protocols` with plain dicts and
mirror the Postgres semantics exactly (same error types, same update
rules, same sweep/heartbeat/purge behavior). The contract suite in
``tests/db/`` runs identical assertions against both the fakes and a
real database so the two can never silently diverge.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from dyvine.core.exceptions import (
    OperationNotFoundError,
    ServiceError,
    WatchDuplicateError,
    WatchSubscriptionNotFoundError,
)
from dyvine.db.protocols import ACTIVE_STATUSES, TERMINAL_STATUSES
from dyvine.db.records import OperationRecord, WatchSubscriptionRecord

_OPERATION_UPDATABLE_FIELDS = frozenset(
    {
        "status",
        "message",
        "progress",
        "total_items",
        "completed_items",
        "download_path",
        "error",
        "metadata",
    }
)

_SUBSCRIPTION_UPDATABLE_FIELDS = frozenset(
    {
        "enabled",
        "live_poll_seconds",
        "post_poll_seconds",
        "checkpoint",
        "last_live_check",
        "last_post_check",
    }
)

_ORPHAN_MESSAGE = "Operation interrupted: owning replica stopped heartbeating"


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()


@dataclass
class FakeOperationState:
    """Shared backing store for multi-owner fake scenarios.

    Two fakes over one state behave like two replicas over one
    database: rows created by one owner are visible to (and sweepable
    by) the other. Single-owner tests can ignore this entirely.
    """

    rows: dict[str, OperationRecord] = field(default_factory=dict)
    owners: dict[str, str | None] = field(default_factory=dict)
    heartbeats: dict[str, str | None] = field(default_factory=dict)


class FakeOperationRepository:
    """Dict-backed ``OperationRepository`` for unit tests."""

    def __init__(
        self, *, owner_id: str = "test-owner", state: FakeOperationState | None = None
    ) -> None:
        """Create an empty store stamped with ``owner_id``.

        Args:
            owner_id: Replica identity stamped onto created rows.
            state: Optional shared backing store; a private one is
                created when omitted.
        """
        self._owner_id = owner_id
        self._state = state or FakeOperationState()

    @property
    def _rows(self) -> dict[str, OperationRecord]:
        """Rows keyed by operation ID."""
        return self._state.rows

    @property
    def _owners(self) -> dict[str, str | None]:
        """Owner identity per operation ID."""
        return self._state.owners

    @property
    def _heartbeats(self) -> dict[str, str | None]:
        """Heartbeat stamp per operation ID."""
        return self._state.heartbeats

    async def healthcheck(self) -> None:
        """In-memory backends are always reachable."""

    async def create_operation(
        self,
        *,
        operation_type: str,
        subject_id: str,
        status: str,
        message: str,
        progress: float | None = None,
        total_items: int | None = None,
        completed_items: int | None = None,
        download_path: str | None = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
        operation_id: str | None = None,
    ) -> OperationRecord:
        """Insert a row stamped with this fake's owner identity."""
        key = operation_id or str(uuid.uuid4())
        if key in self._rows:
            raise ServiceError(
                f"Operation {key} already exists",
                details={"operation_id": key},
            )
        stamp = _now_iso()
        record = OperationRecord(
            operation_id=key,
            operation_type=operation_type,
            subject_id=subject_id,
            status=status,
            message=message,
            progress=progress,
            total_items=total_items,
            completed_items=completed_items,
            download_path=download_path,
            error=error,
            metadata=dict(metadata or {}),
            created_at=stamp,
            updated_at=stamp,
        )
        self._rows[key] = record
        self._owners[key] = self._owner_id
        self._heartbeats[key] = stamp
        return record

    async def get_operation(self, operation_id: str) -> OperationRecord:
        """Fetch one operation or raise ``OperationNotFoundError``."""
        try:
            return self._rows[operation_id]
        except KeyError:
            raise OperationNotFoundError(
                f"Operation {operation_id} not found"
            ) from None

    async def get_latest_operation_for_subject(
        self, subject_id: str, *, operation_type: str | None = None
    ) -> OperationRecord:
        """Fetch the most recently updated operation for a subject."""
        candidates = [
            row
            for row in self._rows.values()
            if row.subject_id == subject_id
            and (operation_type is None or row.operation_type == operation_type)
        ]
        if not candidates:
            raise OperationNotFoundError(f"Operation {subject_id} not found")
        return max(candidates, key=lambda row: (row.updated_at, row.created_at))

    async def update_operation(
        self, operation_id: str, **fields: Any
    ) -> OperationRecord:
        """Update allowed fields, refresh liveness, return the new state."""
        try:
            current = self._rows[operation_id]
        except KeyError:
            raise OperationNotFoundError(
                f"Operation {operation_id} not found"
            ) from None
        requested = {
            key: value
            for key, value in fields.items()
            if key in _OPERATION_UPDATABLE_FIELDS
        }
        if not requested:
            return current
        values: dict[str, Any] = {
            "operation_id": current.operation_id,
            "operation_type": current.operation_type,
            "subject_id": current.subject_id,
            "status": current.status,
            "message": current.message,
            "progress": current.progress,
            "total_items": current.total_items,
            "completed_items": current.completed_items,
            "download_path": current.download_path,
            "error": current.error,
            "metadata": dict(current.metadata),
            "created_at": current.created_at,
            "updated_at": current.updated_at,
        }
        for key, value in requested.items():
            values[key] = dict(value or {}) if key == "metadata" else value
        stamp = _now_iso()
        values["updated_at"] = stamp
        updated = OperationRecord(**values)
        self._rows[operation_id] = updated
        self._heartbeats[operation_id] = stamp
        return updated

    async def sweep_orphans(self, *, stale_after_seconds: float) -> int:
        """Fail active rows whose owner stopped heartbeating.

        Mirrors the SQL NULL semantics: a missing heartbeat falls back
        to ``created_at`` so legacy rows stay sweepable.
        """
        cutoff = (
            datetime.now(UTC) - timedelta(seconds=stale_after_seconds)
        ).isoformat()
        swept = 0
        for key, row in list(self._rows.items()):
            heartbeat = self._heartbeats.get(key)
            alive = heartbeat if heartbeat is not None else row.created_at
            owner = self._owners.get(key)
            if (
                row.status in ACTIVE_STATUSES
                and alive < cutoff
                and owner != self._owner_id
            ):
                await self.update_operation(
                    key,
                    status="failed",
                    message=_ORPHAN_MESSAGE,
                    error=_ORPHAN_MESSAGE,
                )
                swept += 1
        return swept

    async def heartbeat_owned(self) -> int:
        """Refresh the heartbeat of every active row this owner holds."""
        stamp = _now_iso()
        beaten = 0
        for key, row in self._rows.items():
            if row.status in ACTIVE_STATUSES and self._owners.get(key) == (
                self._owner_id
            ):
                self._heartbeats[key] = stamp
                beaten += 1
        return beaten

    async def purge_terminal_before(self, cutoff_iso: str) -> int:
        """Delete terminal rows last updated before ``cutoff_iso``."""
        victims = [
            key
            for key, row in self._rows.items()
            if row.status in TERMINAL_STATUSES and row.updated_at < cutoff_iso
        ]
        for key in victims:
            del self._rows[key]
            self._owners.pop(key, None)
            self._heartbeats.pop(key, None)
        return len(victims)


class FakeWatchRepository:
    """Dict-backed ``WatchRepository`` for unit tests."""

    def __init__(self) -> None:
        """Create an empty subscription store."""
        self._rows: dict[str, WatchSubscriptionRecord] = {}
        self._by_user: dict[str, str] = {}

    async def create_subscription(
        self,
        *,
        user_id: str,
        live_poll_seconds: int,
        post_poll_seconds: int,
        enabled: bool = True,
        checkpoint: dict[str, Any] | None = None,
        subscription_id: str | None = None,
    ) -> WatchSubscriptionRecord:
        """Insert a subscription; duplicate users raise."""
        if user_id in self._by_user:
            raise WatchDuplicateError(
                f"Watch subscription for user {user_id} already exists",
                details={"user_id": user_id},
            )
        stamp = _now_iso()
        record = WatchSubscriptionRecord(
            subscription_id=subscription_id or str(uuid.uuid4()),
            user_id=user_id,
            enabled=enabled,
            live_poll_seconds=live_poll_seconds,
            post_poll_seconds=post_poll_seconds,
            checkpoint=dict(checkpoint or {}),
            last_live_check=None,
            last_post_check=None,
            created_at=stamp,
            updated_at=stamp,
        )
        self._rows[record.subscription_id] = record
        self._by_user[user_id] = record.subscription_id
        return record

    async def get_subscription(self, subscription_id: str) -> WatchSubscriptionRecord:
        """Fetch by ID or raise ``WatchSubscriptionNotFoundError``."""
        try:
            return self._rows[subscription_id]
        except KeyError:
            raise WatchSubscriptionNotFoundError(
                f"Watch subscription {subscription_id} not found"
            ) from None

    async def get_subscription_by_user(
        self, user_id: str
    ) -> WatchSubscriptionRecord | None:
        """Return the subscription for ``user_id``, or ``None``."""
        key = self._by_user.get(user_id)
        return self._rows[key] if key is not None else None

    async def list_subscriptions(
        self, *, enabled_only: bool = False
    ) -> list[WatchSubscriptionRecord]:
        """Return subscriptions ordered by creation time."""
        rows = [row for row in self._rows.values() if not enabled_only or row.enabled]
        rows.sort(key=lambda row: (row.created_at, row.subscription_id))
        return rows

    async def count_subscriptions(self) -> int:
        """Return the total number of persisted subscriptions."""
        return len(self._rows)

    async def update_subscription(
        self, subscription_id: str, **fields: Any
    ) -> WatchSubscriptionRecord:
        """Update allowed fields and return the new state."""
        try:
            current = self._rows[subscription_id]
        except KeyError:
            raise WatchSubscriptionNotFoundError(
                f"Watch subscription {subscription_id} not found"
            ) from None
        requested = {
            key: value
            for key, value in fields.items()
            if key in _SUBSCRIPTION_UPDATABLE_FIELDS
        }
        if not requested:
            return current
        values: dict[str, Any] = {
            "subscription_id": current.subscription_id,
            "user_id": current.user_id,
            "enabled": current.enabled,
            "live_poll_seconds": current.live_poll_seconds,
            "post_poll_seconds": current.post_poll_seconds,
            "checkpoint": dict(current.checkpoint),
            "last_live_check": current.last_live_check,
            "last_post_check": current.last_post_check,
            "created_at": current.created_at,
            "updated_at": current.updated_at,
        }
        for key, value in requested.items():
            values[key] = dict(value or {}) if key == "checkpoint" else value
        values["updated_at"] = _now_iso()
        updated = WatchSubscriptionRecord(**values)
        self._rows[subscription_id] = updated
        return updated

    async def delete_subscription(self, subscription_id: str) -> bool:
        """Delete a subscription; ``True`` when a row was removed."""
        record = self._rows.pop(subscription_id, None)
        if record is None:
            return False
        self._by_user.pop(record.user_id, None)
        return True
