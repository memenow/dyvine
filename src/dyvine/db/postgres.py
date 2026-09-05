"""Postgres-backed repository implementations.

Every method opens a short-lived session, so no transactional state is
shared across awaits and the pool (owned by
:class:`DatabaseSessionFactory`) is the only long-lived resource. Rows
map one-to-one onto the :mod:`dyvine.db.records` dataclasses, keeping
the service layer free of ORM types.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, desc, func, select, text, update
from sqlalchemy.exc import IntegrityError

from ..core.exceptions import (
    OperationNotFoundError,
    ServiceError,
    WatchDuplicateError,
    WatchSubscriptionNotFoundError,
)
from .models import OperationRow, WatchSubscriptionRow
from .protocols import ACTIVE_STATUSES, TERMINAL_STATUSES
from .records import OperationRecord, WatchSubscriptionRecord
from .session import DatabaseSessionFactory

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


def _operation_to_record(row: OperationRow) -> OperationRecord:
    """Map an ORM row onto the service-facing record."""
    return OperationRecord(
        operation_id=row.operation_id,
        operation_type=row.operation_type,
        subject_id=row.subject_id,
        status=row.status,
        message=row.message,
        progress=row.progress,
        total_items=row.total_items,
        completed_items=row.completed_items,
        download_path=row.download_path,
        error=row.error,
        metadata=dict(row.metadata_ or {}),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _watch_to_record(row: WatchSubscriptionRow) -> WatchSubscriptionRecord:
    """Map an ORM row onto the service-facing record."""
    return WatchSubscriptionRecord(
        subscription_id=row.subscription_id,
        user_id=row.user_id,
        enabled=row.enabled,
        live_poll_seconds=row.live_poll_seconds,
        post_poll_seconds=row.post_poll_seconds,
        checkpoint=dict(row.checkpoint or {}),
        last_live_check=row.last_live_check,
        last_post_check=row.last_post_check,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class PostgresOperationRepository:
    """Operation persistence over Postgres + asyncpg.

    Args:
        sessions: Session factory owned by the caller (the container
            disposes it; the repository never closes shared state).
        owner_id: Replica identity stamped onto created rows so the
            orphan sweep can tell live owners from dead ones.
    """

    def __init__(self, sessions: DatabaseSessionFactory, *, owner_id: str) -> None:
        """Bind the repository to a session factory and an owner identity."""
        self._sessions = sessions
        self._owner_id = owner_id

    async def healthcheck(self) -> None:
        """Verify the database answers; propagate any failure."""
        async with self._sessions.session() as session:
            await session.execute(text("SELECT 1"))

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
        """Insert a row stamped with this repository's owner identity."""
        stamp = _now_iso()
        row = OperationRow(
            operation_id=operation_id or str(uuid.uuid4()),
            operation_type=operation_type,
            subject_id=subject_id,
            status=status,
            message=message,
            progress=progress,
            total_items=total_items,
            completed_items=completed_items,
            download_path=download_path,
            error=error,
            metadata_=dict(metadata or {}),
            owner_id=self._owner_id,
            heartbeat_at=stamp,
            created_at=stamp,
            updated_at=stamp,
        )
        try:
            async with self._sessions.session() as session:
                async with session.begin():
                    session.add(row)
        except IntegrityError as exc:
            raise ServiceError(
                f"Operation {row.operation_id} already exists",
                details={"operation_id": row.operation_id},
            ) from exc
        return _operation_to_record(row)

    async def get_operation(self, operation_id: str) -> OperationRecord:
        """Fetch one operation or raise ``OperationNotFoundError``."""
        async with self._sessions.session() as session:
            row = await session.get(OperationRow, operation_id)
        if row is None:
            raise OperationNotFoundError(f"Operation {operation_id} not found")
        return _operation_to_record(row)

    async def get_latest_operation_for_subject(
        self, subject_id: str, *, operation_type: str | None = None
    ) -> OperationRecord:
        """Fetch the most recently updated operation for a subject."""
        statement = select(OperationRow).where(OperationRow.subject_id == subject_id)
        if operation_type is not None:
            statement = statement.where(OperationRow.operation_type == operation_type)
        statement = statement.order_by(
            desc(OperationRow.updated_at), desc(OperationRow.created_at)
        ).limit(1)
        async with self._sessions.session() as session:
            row = (await session.execute(statement)).scalars().first()
        if row is None:
            raise OperationNotFoundError(f"Operation {subject_id} not found")
        return _operation_to_record(row)

    async def update_operation(
        self, operation_id: str, **fields: Any
    ) -> OperationRecord:
        """Update allowed fields, refresh liveness, return the new state.

        Stored metadata is preserved when the caller passes no explicit
        ``metadata`` value; unknown-only field sets verify existence and
        return the row unchanged. Every update refreshes ``heartbeat_at``
        (but not ``updated_at`` semantics beyond the write itself) so
        active tasks are never mistaken for orphans.
        """
        requested = {
            key: value
            for key, value in fields.items()
            if key in _OPERATION_UPDATABLE_FIELDS
        }
        async with self._sessions.session() as session:
            async with session.begin():
                row = await session.get(OperationRow, operation_id)
                if row is None:
                    raise OperationNotFoundError(f"Operation {operation_id} not found")
                if requested:
                    for key, value in requested.items():
                        setattr(
                            row,
                            "metadata_" if key == "metadata" else key,
                            dict(value) if key == "metadata" else value,
                        )
                    stamp = _now_iso()
                    row.updated_at = stamp
                    row.heartbeat_at = stamp
        return _operation_to_record(row)

    async def sweep_orphans(self, *, stale_after_seconds: float) -> int:
        """Fail active rows whose owner stopped heartbeating.

        A row is orphaned when it is still ``pending``/``running``, its
        heartbeat predates the cutoff, and its owner is either unknown
        or a different replica. Returns the number of rows failed.
        """
        cutoff = datetime.now(UTC) - timedelta(seconds=stale_after_seconds)
        cutoff_iso = cutoff.isoformat()
        stamp = _now_iso()
        statement = (
            update(OperationRow)
            .where(OperationRow.status.in_(ACTIVE_STATUSES))
            .where(OperationRow.heartbeat_at < cutoff_iso)
            .where(
                (OperationRow.owner_id.is_(None))
                | (OperationRow.owner_id != self._owner_id)
            )
            .values(
                status="failed",
                message=_ORPHAN_MESSAGE,
                error=_ORPHAN_MESSAGE,
                updated_at=stamp,
            )
        )
        async with self._sessions.session() as session:
            async with session.begin():
                connection = await session.connection()
                result = await connection.execute(statement)
                return int(result.rowcount or 0)

    async def heartbeat_owned(self) -> int:
        """Refresh the heartbeat of every active row this owner holds.

        Only ``heartbeat_at`` moves: ``updated_at`` keeps tracking the
        last semantic write so recency queries and retention stay
        meaningful for long-running tasks.
        """
        statement = (
            update(OperationRow)
            .where(OperationRow.status.in_(ACTIVE_STATUSES))
            .where(OperationRow.owner_id == self._owner_id)
            .values(heartbeat_at=_now_iso())
        )
        async with self._sessions.session() as session:
            async with session.begin():
                connection = await session.connection()
                result = await connection.execute(statement)
                return int(result.rowcount or 0)

    async def purge_terminal_before(self, cutoff_iso: str) -> int:
        """Delete terminal rows last updated before ``cutoff_iso``."""
        statement = (
            delete(OperationRow)
            .where(OperationRow.status.in_(TERMINAL_STATUSES))
            .where(OperationRow.updated_at < cutoff_iso)
        )
        async with self._sessions.session() as session:
            async with session.begin():
                connection = await session.connection()
                result = await connection.execute(statement)
                return int(result.rowcount or 0)


class PostgresWatchRepository:
    """Watch-subscription persistence over Postgres + asyncpg."""

    def __init__(self, sessions: DatabaseSessionFactory) -> None:
        """Bind the repository to a caller-owned session factory."""
        self._sessions = sessions

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
        """Insert a subscription; duplicate users raise ``WatchDuplicateError``."""
        stamp = _now_iso()
        row = WatchSubscriptionRow(
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
        try:
            async with self._sessions.session() as session:
                async with session.begin():
                    session.add(row)
        except IntegrityError as exc:
            raise WatchDuplicateError(
                f"Watch subscription for user {user_id} already exists",
                details={"user_id": user_id},
            ) from exc
        return _watch_to_record(row)

    async def get_subscription(self, subscription_id: str) -> WatchSubscriptionRecord:
        """Fetch by ID or raise ``WatchSubscriptionNotFoundError``."""
        async with self._sessions.session() as session:
            row = await session.get(WatchSubscriptionRow, subscription_id)
        if row is None:
            raise WatchSubscriptionNotFoundError(
                f"Watch subscription {subscription_id} not found"
            )
        return _watch_to_record(row)

    async def get_subscription_by_user(
        self, user_id: str
    ) -> WatchSubscriptionRecord | None:
        """Return the subscription for ``user_id``, or ``None``."""
        statement = select(WatchSubscriptionRow).where(
            WatchSubscriptionRow.user_id == user_id
        )
        async with self._sessions.session() as session:
            row = (await session.execute(statement)).scalars().first()
        return _watch_to_record(row) if row is not None else None

    async def list_subscriptions(
        self, *, enabled_only: bool = False
    ) -> list[WatchSubscriptionRecord]:
        """Return subscriptions ordered by creation time."""
        statement = select(WatchSubscriptionRow)
        if enabled_only:
            statement = statement.where(WatchSubscriptionRow.enabled.is_(True))
        statement = statement.order_by(
            WatchSubscriptionRow.created_at,
            WatchSubscriptionRow.subscription_id,
        )
        async with self._sessions.session() as session:
            rows = (await session.execute(statement)).scalars().all()
        return [_watch_to_record(row) for row in rows]

    async def count_subscriptions(self) -> int:
        """Return the total number of persisted subscriptions."""
        statement = select(func.count()).select_from(WatchSubscriptionRow)
        async with self._sessions.session() as session:
            total = (await session.execute(statement)).scalar()
        return int(total or 0)

    async def update_subscription(
        self, subscription_id: str, **fields: Any
    ) -> WatchSubscriptionRecord:
        """Update allowed fields and return the new state."""
        requested = {
            key: value
            for key, value in fields.items()
            if key in _SUBSCRIPTION_UPDATABLE_FIELDS
        }
        async with self._sessions.session() as session:
            async with session.begin():
                row = await session.get(WatchSubscriptionRow, subscription_id)
                if row is None:
                    raise WatchSubscriptionNotFoundError(
                        f"Watch subscription {subscription_id} not found"
                    )
                if requested:
                    for key, value in requested.items():
                        setattr(
                            row,
                            key,
                            dict(value) if key == "checkpoint" else value,
                        )
                    row.updated_at = _now_iso()
        return _watch_to_record(row)

    async def delete_subscription(self, subscription_id: str) -> bool:
        """Delete a subscription; ``True`` when a row was removed."""
        statement = delete(WatchSubscriptionRow).where(
            WatchSubscriptionRow.subscription_id == subscription_id
        )
        async with self._sessions.session() as session:
            async with session.begin():
                connection = await session.connection()
                result = await connection.execute(statement)
                return int(result.rowcount or 0) > 0
