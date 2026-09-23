"""Postgres-backed repository implementations.

Every method opens a short-lived session, so no transactional state is
shared across awaits and the pool (owned by
:class:`DatabaseSessionFactory`) is the only long-lived resource. Rows
map one-to-one onto the :mod:`dyvine.db.records` dataclasses, keeping
the service layer free of ORM types.
"""

from __future__ import annotations

import functools
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any, Concatenate, Protocol

from sqlalchemy import and_, delete, desc, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from ..core.exceptions import (
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
from .health import DatabaseHealthTracker
from .models import (
    DeliveryRoundRow,
    DownloadQueueRow,
    OperationRow,
    SeedAccountRow,
    SendStatusRow,
    UserProfileRow,
    UserSendStatusRow,
    WatchSubscriptionRow,
)
from .protocols import (
    ACTIVE_STATUSES,
    QUEUE_CLAIMABLE_STATUSES,
    TERMINAL_STATUSES,
)
from .records import (
    DeliveryRoundRecord,
    OperationRecord,
    QueueEntryRecord,
    SeedAccountRecord,
    SendStatusRecord,
    UserProfileRecord,
    UserSendStatusRecord,
    WatchSubscriptionRecord,
)
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

_QUEUE_UPDATABLE_FIELDS = frozenset(
    {
        "kind",
        "nickname",
        "sec_user_id",
        "chat_id",
        "homepage",
        "mode",
        "cutoff",
        "status",
        "operation_id",
        "op_status",
        "op_message",
        "attempts",
        "serial_group",
        "extra",
    }
)

_PROFILE_COLUMNS = frozenset(
    {
        "nickname",
        "nickname_raw",
        "avatar_url",
        "signature",
        "signature_raw",
        "uid",
        "short_id",
        "unique_id",
        "room_id",
        "city",
        "country",
        "ip_location",
        "school_name",
        "gender",
        "user_age",
        "aweme_count",
        "favoriting_count",
        "follower_count",
        "following_count",
        "total_favorited",
        "mplatform_followers_count",
        "mix_count",
        "live_status",
        "is_ban",
        "is_block",
        "is_blocked",
        "is_star",
        "last_aweme_id",
    }
)

_ORPHAN_MESSAGE = "Operation interrupted: owning replica stopped heartbeating"

#: Advisory-lock key serialising capped watch-subscription creates
#: across replicas (see ``create_subscription_capped``).
_WATCH_CAP_LOCK_KEY = "dyvine_watch_subscription_cap"


class _HealthTrackable(Protocol):
    """Structural hook for repositories carrying a health tracker."""

    _health: DatabaseHealthTracker | None


def _tracked[SelfT: _HealthTrackable, **P, R](
    fn: Callable[Concatenate[SelfT, P], Coroutine[Any, Any, R]],
) -> Callable[Concatenate[SelfT, P], Coroutine[Any, Any, R]]:
    """Record the repository call's outcome on its health tracker.

    Only database-layer failures (``SQLAlchemyError``/``OSError``)
    count as unreachable; any other outcome — including a domain error
    such as "not found" — proves the database answered. Repositories
    built without a tracker skip recording.
    """

    @functools.wraps(fn)
    async def wrapper(self: SelfT, *args: P.args, **kwargs: P.kwargs) -> R:
        health = self._health
        try:
            result = await fn(self, *args, **kwargs)
        except (SQLAlchemyError, OSError):
            if health is not None:
                health.note_failure()
            raise
        except Exception:
            # A domain error (not found, duplicate, over cap) still
            # proves the database answered the call.
            if health is not None:
                health.note_success()
            raise
        if health is not None:
            health.note_success()
        return result

    return wrapper


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


def _queue_to_record(row: DownloadQueueRow) -> QueueEntryRecord:
    """Map an ORM row onto the service-facing record."""
    return QueueEntryRecord(
        key=row.key,
        round=row.round,
        kind=row.kind,
        nickname=row.nickname,
        sec_user_id=row.sec_user_id,
        chat_id=row.chat_id,
        homepage=row.homepage,
        mode=row.mode,
        cutoff=row.cutoff,
        status=row.status,
        operation_id=row.operation_id,
        op_status=row.op_status,
        op_message=row.op_message,
        attempts=row.attempts,
        serial_group=row.serial_group,
        extra=dict(row.extra or {}),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _send_to_record(row: SendStatusRow) -> SendStatusRecord:
    """Map an ORM row onto the service-facing record."""
    return SendStatusRecord(
        nickname=row.nickname,
        sec_user_id=row.sec_user_id,
        chat_id=row.chat_id,
        batch=row.batch,
        total_files=row.total_files,
        sent_files=row.sent_files,
        failed_files=row.failed_files,
        status=row.status,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _user_send_to_record(row: UserSendStatusRow) -> UserSendStatusRecord:
    """Map an ORM row onto the service-facing record."""
    return UserSendStatusRecord(
        id=row.id,
        username=row.username,
        local_files=row.local_files,
        sent_files=row.sent_files,
        failed_files=row.failed_files,
        status=row.status,
        failed_details=row.failed_details,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _seed_to_record(row: SeedAccountRow) -> SeedAccountRecord:
    """Map an ORM row onto the service-facing record."""
    return SeedAccountRecord(
        sec_user_id=row.sec_user_id,
        nickname=row.nickname,
        source_url=row.source_url,
        source=row.source,
        batch=row.batch,
        excluded=row.excluded,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _profile_to_record(row: UserProfileRow) -> UserProfileRecord:
    """Map an ORM row onto the service-facing record."""
    return UserProfileRecord(
        sec_user_id=row.sec_user_id,
        nickname=row.nickname,
        nickname_raw=row.nickname_raw,
        avatar_url=row.avatar_url,
        signature=row.signature,
        signature_raw=row.signature_raw,
        uid=row.uid,
        short_id=row.short_id,
        unique_id=row.unique_id,
        room_id=row.room_id,
        city=row.city,
        country=row.country,
        ip_location=row.ip_location,
        school_name=row.school_name,
        gender=row.gender,
        user_age=row.user_age,
        aweme_count=row.aweme_count,
        favoriting_count=row.favoriting_count,
        follower_count=row.follower_count,
        following_count=row.following_count,
        total_favorited=row.total_favorited,
        mplatform_followers_count=row.mplatform_followers_count,
        mix_count=row.mix_count,
        live_status=row.live_status,
        is_ban=row.is_ban,
        is_block=row.is_block,
        is_blocked=row.is_blocked,
        is_star=row.is_star,
        last_aweme_id=row.last_aweme_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _round_to_record(row: DeliveryRoundRow) -> DeliveryRoundRecord:
    """Map an ORM row onto the service-facing record."""
    return DeliveryRoundRecord(
        round=row.round,
        note=row.note,
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
        health: Tracker recording each call's outcome for passive
            health reporting; ``None`` disables recording.
    """

    def __init__(
        self,
        sessions: DatabaseSessionFactory,
        *,
        owner_id: str,
        health: DatabaseHealthTracker | None = None,
    ) -> None:
        """Bind the repository to a session factory and an owner identity."""
        self._sessions = sessions
        self._owner_id = owner_id
        self._health = health

    @_tracked
    async def healthcheck(self) -> None:
        """Verify the database answers; propagate any failure."""
        async with self._sessions.session() as session:
            await session.execute(text("SELECT 1"))

    @_tracked
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

    @_tracked
    async def get_operation(self, operation_id: str) -> OperationRecord:
        """Fetch one operation or raise ``OperationNotFoundError``."""
        async with self._sessions.session() as session:
            row = await session.get(OperationRow, operation_id)
        if row is None:
            raise OperationNotFoundError(f"Operation {operation_id} not found")
        return _operation_to_record(row)

    @_tracked
    async def get_latest_operation_for_subject(
        self, subject_id: str, *, operation_type: str | None = None
    ) -> OperationRecord:
        """Fetch the most recently updated operation for a subject.

        Ties on both timestamps break on ``operation_id`` descending,
        mirroring the ``list_subscriptions`` id-tiebreak convention, so
        frozen-clock writes resolve identically on every backend.
        """
        statement = select(OperationRow).where(OperationRow.subject_id == subject_id)
        if operation_type is not None:
            statement = statement.where(OperationRow.operation_type == operation_type)
        statement = statement.order_by(
            desc(OperationRow.updated_at),
            desc(OperationRow.created_at),
            desc(OperationRow.operation_id),
        ).limit(1)
        async with self._sessions.session() as session:
            row = (await session.execute(statement)).scalars().first()
        if row is None:
            raise OperationNotFoundError(f"Operation {subject_id} not found")
        return _operation_to_record(row)

    @_tracked
    async def update_operation(
        self, operation_id: str, **fields: Any
    ) -> OperationRecord:
        """Update allowed fields, refresh liveness, return the new state.

        Stored metadata is preserved when the caller passes no explicit
        ``metadata`` value (an explicit ``None`` clears it to ``{}``
        instead of raising); unknown-only field sets verify existence and
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
                            dict(value or {}) if key == "metadata" else value,
                        )
                    stamp = _now_iso()
                    row.updated_at = stamp
                    row.heartbeat_at = stamp
        return _operation_to_record(row)

    @_tracked
    async def sweep_orphans(self, *, stale_after_seconds: float) -> int:
        """Fail active rows whose owner stopped heartbeating.

        A row is orphaned when it is still ``pending``/``running``, its
        heartbeat predates the cutoff, and its owner is either unknown
        or a different replica. Rows with a NULL heartbeat (legacy rows
        predating the liveness columns) fall back to ``created_at`` so
        they stay sweepable instead of lingering forever. Returns the
        number of rows failed.
        """
        cutoff = datetime.now(UTC) - timedelta(seconds=stale_after_seconds)
        cutoff_iso = cutoff.isoformat()
        stamp = _now_iso()
        statement = (
            update(OperationRow)
            .where(OperationRow.status.in_(ACTIVE_STATUSES))
            .where(
                or_(
                    OperationRow.heartbeat_at < cutoff_iso,
                    and_(
                        OperationRow.heartbeat_at.is_(None),
                        OperationRow.created_at < cutoff_iso,
                    ),
                )
            )
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

    @_tracked
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

    @_tracked
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

    def __init__(
        self,
        sessions: DatabaseSessionFactory,
        *,
        health: DatabaseHealthTracker | None = None,
    ) -> None:
        """Bind the repository to a caller-owned session factory.

        Args:
            health: Tracker recording each call's outcome for passive
                health reporting; ``None`` disables recording.
        """
        self._sessions = sessions
        self._health = health

    @_tracked
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

    @_tracked
    async def create_subscription_capped(
        self,
        *,
        user_id: str,
        live_poll_seconds: int,
        post_poll_seconds: int,
        enabled: bool = True,
        checkpoint: dict[str, Any] | None = None,
        subscription_id: str | None = None,
        max_subscriptions: int,
    ) -> WatchSubscriptionRecord:
        """Insert a subscription, enforcing the cap atomically.

        A transaction-scoped advisory lock serialises concurrent
        creators across replicas: without it, two processes could both
        count N < cap and both insert. The lock dies with the
        transaction, so there is no cleanup path to forget. The
        duplicate check comes first so a cross-replica duplicate race
        at cap converges to the idempotent existing row
        (``WatchDuplicateError``) instead of a spurious 429.
        """
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
                    await session.execute(
                        select(
                            func.pg_advisory_xact_lock(
                                func.hashtext(_WATCH_CAP_LOCK_KEY)
                            )
                        )
                    )
                    duplicate = (
                        await session.execute(
                            select(WatchSubscriptionRow.subscription_id).where(
                                WatchSubscriptionRow.user_id == user_id
                            )
                        )
                    ).scalar_one_or_none()
                    if duplicate is not None:
                        raise WatchDuplicateError(
                            f"Watch subscription for user {user_id} " "already exists",
                            details={"user_id": user_id},
                        )
                    total = (
                        await session.execute(
                            select(func.count()).select_from(WatchSubscriptionRow)
                        )
                    ).scalar()
                    if int(total or 0) >= max_subscriptions:
                        raise RateLimitError(
                            "Watch subscription limit reached "
                            f"({max_subscriptions}); "
                            "delete a subscription first",
                            details={"max_subscriptions": max_subscriptions},
                        )
                    session.add(row)
        except IntegrityError as exc:
            raise WatchDuplicateError(
                f"Watch subscription for user {user_id} already exists",
                details={"user_id": user_id},
            ) from exc
        return _watch_to_record(row)

    @_tracked
    async def get_subscription(self, subscription_id: str) -> WatchSubscriptionRecord:
        """Fetch by ID or raise ``WatchSubscriptionNotFoundError``."""
        async with self._sessions.session() as session:
            row = await session.get(WatchSubscriptionRow, subscription_id)
        if row is None:
            raise WatchSubscriptionNotFoundError(
                f"Watch subscription {subscription_id} not found"
            )
        return _watch_to_record(row)

    @_tracked
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

    @_tracked
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

    @_tracked
    async def count_subscriptions(self) -> int:
        """Return the total number of persisted subscriptions."""
        statement = select(func.count()).select_from(WatchSubscriptionRow)
        async with self._sessions.session() as session:
            total = (await session.execute(statement)).scalar()
        return int(total or 0)

    @_tracked
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
                            dict(value or {}) if key == "checkpoint" else value,
                        )
                    row.updated_at = _now_iso()
        return _watch_to_record(row)

    @_tracked
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


class PostgresQueueRepository:
    """Download-queue persistence over Postgres + asyncpg."""

    def __init__(
        self,
        sessions: DatabaseSessionFactory,
        *,
        owner_id: str,
        health: DatabaseHealthTracker | None = None,
    ) -> None:
        """Bind the repository to a session factory and an owner identity."""
        self._sessions = sessions
        self._owner_id = owner_id
        self._health = health

    @_tracked
    async def upsert_entry(
        self,
        *,
        key: str,
        round: str,
        nickname: str,
        sec_user_id: str,
        mode: str,
        status: str,
        kind: str | None = None,
        chat_id: str | None = None,
        homepage: str | None = None,
        cutoff: str | None = None,
        operation_id: str | None = None,
        op_status: str | None = None,
        op_message: str | None = None,
        attempts: int = 0,
        serial_group: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> QueueEntryRecord:
        """Insert or replace the entry at ``key`` and return it."""
        stamp = _now_iso()
        async with self._sessions.session() as session:
            async with session.begin():
                row = await session.get(DownloadQueueRow, key)
                if row is None:
                    row = DownloadQueueRow(
                        key=key,
                        round=round,
                        kind=kind,
                        nickname=nickname,
                        sec_user_id=sec_user_id,
                        chat_id=chat_id,
                        homepage=homepage,
                        mode=mode,
                        cutoff=cutoff,
                        status=status,
                        operation_id=operation_id,
                        op_status=op_status,
                        op_message=op_message,
                        attempts=attempts,
                        serial_group=serial_group,
                        owner_id=self._owner_id,
                        heartbeat_at=stamp,
                        extra=dict(extra or {}),
                        created_at=stamp,
                        updated_at=stamp,
                    )
                    session.add(row)
                else:
                    row.round = round
                    row.kind = kind
                    row.nickname = nickname
                    row.sec_user_id = sec_user_id
                    row.chat_id = chat_id
                    row.homepage = homepage
                    row.mode = mode
                    row.cutoff = cutoff
                    row.status = status
                    row.operation_id = operation_id
                    row.op_status = op_status
                    row.op_message = op_message
                    row.attempts = attempts
                    row.serial_group = serial_group
                    row.extra = dict(extra or {})
                    row.updated_at = stamp
                    row.heartbeat_at = stamp
        return _queue_to_record(row)

    @_tracked
    async def get_entry(self, key: str) -> QueueEntryRecord:
        """Fetch by key or raise ``QueueEntryNotFoundError``."""
        async with self._sessions.session() as session:
            row = await session.get(DownloadQueueRow, key)
        if row is None:
            raise QueueEntryNotFoundError(f"Queue entry {key} not found")
        return _queue_to_record(row)

    @_tracked
    async def list_entries(
        self,
        *,
        round: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[QueueEntryRecord]:
        """List entries oldest-first, optionally filtered."""
        statement = select(DownloadQueueRow)
        if round is not None:
            statement = statement.where(DownloadQueueRow.round == round)
        if status is not None:
            statement = statement.where(DownloadQueueRow.status == status)
        statement = statement.order_by(
            DownloadQueueRow.updated_at, DownloadQueueRow.key
        ).offset(offset)
        if limit >= 0:
            statement = statement.limit(limit)
        async with self._sessions.session() as session:
            rows = (await session.execute(statement)).scalars().all()
        return [_queue_to_record(row) for row in rows]

    @_tracked
    async def count_entries(
        self, *, round: str | None = None, status: str | None = None
    ) -> int:
        """Count entries, optionally filtered."""
        statement = select(func.count()).select_from(DownloadQueueRow)
        if round is not None:
            statement = statement.where(DownloadQueueRow.round == round)
        if status is not None:
            statement = statement.where(DownloadQueueRow.status == status)
        async with self._sessions.session() as session:
            total = (await session.execute(statement)).scalar()
        return int(total or 0)

    @_tracked
    async def claim_next(
        self, *, round: str | None = None, keys: set[str] | None = None
    ) -> QueueEntryRecord | None:
        """Claim the oldest ``pending`` entry, or ``None`` when empty.

        The scan locks one row with ``FOR UPDATE SKIP LOCKED`` so
        concurrent claimers never collide; entries whose
        ``serial_group`` already has a ``downloading`` row are skipped.
        The optional key filter is evaluated inside the locked scan.
        """
        async with self._sessions.session() as session:
            async with session.begin():
                busy_groups = (
                    select(DownloadQueueRow.serial_group)
                    .where(DownloadQueueRow.status == "downloading")
                    .where(DownloadQueueRow.serial_group.is_not(None))
                )
                statement = (
                    select(DownloadQueueRow)
                    .where(DownloadQueueRow.status.in_(QUEUE_CLAIMABLE_STATUSES))
                    .where(
                        or_(
                            DownloadQueueRow.serial_group.is_(None),
                            DownloadQueueRow.serial_group.not_in(busy_groups),
                        )
                    )
                    .order_by(DownloadQueueRow.updated_at, DownloadQueueRow.key)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                if round is not None:
                    statement = statement.where(DownloadQueueRow.round == round)
                if keys is not None:
                    statement = statement.where(DownloadQueueRow.key.in_(keys))
                row = (await session.execute(statement)).scalars().first()
                if row is None:
                    return None
                stamp = _now_iso()
                row.status = "downloading"
                row.owner_id = self._owner_id
                row.heartbeat_at = stamp
                row.updated_at = stamp
        return _queue_to_record(row)

    @_tracked
    async def update_entry(self, key: str, **fields: Any) -> QueueEntryRecord:
        """Update allowed fields, refresh liveness, return the new state."""
        requested = {
            name: value
            for name, value in fields.items()
            if name in _QUEUE_UPDATABLE_FIELDS
        }
        async with self._sessions.session() as session:
            async with session.begin():
                row = await session.get(DownloadQueueRow, key)
                if row is None:
                    raise QueueEntryNotFoundError(f"Queue entry {key} not found")
                if requested:
                    for name, value in requested.items():
                        setattr(
                            row,
                            name,
                            dict(value or {}) if name == "extra" else value,
                        )
                    stamp = _now_iso()
                    row.updated_at = stamp
                    row.heartbeat_at = stamp
        return _queue_to_record(row)

    @_tracked
    async def release_stale(
        self,
        *,
        stale_after_seconds: float,
        max_attempts: int,
        round: str | None = None,
    ) -> int:
        """Requeue ``downloading`` rows whose owner stopped heartbeating.

        Rows load first so the retry-vs-exhausted branch and the
        ``attempts`` bump stay exact even when several rows share one
        write stamp (frozen clocks in tests); stale sets are small
        enough that one select plus per-row writes beats stamp
        matching.
        """
        cutoff = datetime.now(UTC) - timedelta(seconds=stale_after_seconds)
        cutoff_iso = cutoff.isoformat()
        stamp = _now_iso()
        statement = (
            select(DownloadQueueRow)
            .where(DownloadQueueRow.status == "downloading")
            .where(
                or_(
                    DownloadQueueRow.heartbeat_at < cutoff_iso,
                    and_(
                        DownloadQueueRow.heartbeat_at.is_(None),
                        DownloadQueueRow.created_at < cutoff_iso,
                    ),
                )
            )
            .where(
                (DownloadQueueRow.owner_id.is_(None))
                | (DownloadQueueRow.owner_id != self._owner_id)
            )
        )
        if round is not None:
            statement = statement.where(DownloadQueueRow.round == round)
        async with self._sessions.session() as session:
            async with session.begin():
                rows = (await session.execute(statement)).scalars().all()
                for row in rows:
                    if row.attempts >= max_attempts:
                        row.status = "op_issue"
                    else:
                        row.status = "pending"
                        row.attempts += 1
                    row.updated_at = stamp
                    row.heartbeat_at = stamp
                return len(rows)


class PostgresSendStatusRepository:
    """Delivery-counter persistence over Postgres + asyncpg."""

    def __init__(
        self,
        sessions: DatabaseSessionFactory,
        *,
        health: DatabaseHealthTracker | None = None,
    ) -> None:
        """Bind the repository to a caller-owned session factory."""
        self._sessions = sessions
        self._health = health

    @_tracked
    async def upsert_send_status(
        self,
        *,
        nickname: str,
        sec_user_id: str | None = None,
        chat_id: str | None = None,
        batch: str | None = None,
        total_files: int | None = None,
        sent_files: int | None = None,
        failed_files: int | None = None,
        status: str | None = None,
    ) -> SendStatusRecord:
        """Insert or replace the row for ``nickname`` and return it."""
        stamp = _now_iso()
        async with self._sessions.session() as session:
            async with session.begin():
                row = await session.get(SendStatusRow, nickname)
                if row is None:
                    row = SendStatusRow(
                        nickname=nickname,
                        sec_user_id=sec_user_id,
                        chat_id=chat_id,
                        batch=batch,
                        total_files=total_files,
                        sent_files=sent_files,
                        failed_files=failed_files,
                        status=status,
                        created_at=stamp,
                        updated_at=stamp,
                    )
                    session.add(row)
                else:
                    row.sec_user_id = sec_user_id
                    row.chat_id = chat_id
                    row.batch = batch
                    row.total_files = total_files
                    row.sent_files = sent_files
                    row.failed_files = failed_files
                    row.status = status
                    row.updated_at = stamp
        return _send_to_record(row)

    @_tracked
    async def get_send_status(self, nickname: str) -> SendStatusRecord:
        """Fetch by nickname or raise ``SendStatusNotFoundError``."""
        async with self._sessions.session() as session:
            row = await session.get(SendStatusRow, nickname)
        if row is None:
            raise SendStatusNotFoundError(f"Send status for {nickname} not found")
        return _send_to_record(row)

    @_tracked
    async def get_send_status_by_sec(self, sec_user_id: str) -> SendStatusRecord | None:
        """Return the row for ``sec_user_id``, or ``None``."""
        statement = select(SendStatusRow).where(
            SendStatusRow.sec_user_id == sec_user_id
        )
        async with self._sessions.session() as session:
            row = (await session.execute(statement)).scalars().first()
        return _send_to_record(row) if row is not None else None

    @_tracked
    async def list_send_status(
        self, *, batch: str | None = None, limit: int = 100, offset: int = 0
    ) -> list[SendStatusRecord]:
        """List rows oldest-first, optionally filtered by batch."""
        statement = select(SendStatusRow)
        if batch is not None:
            statement = statement.where(SendStatusRow.batch == batch)
        statement = statement.order_by(
            SendStatusRow.updated_at, SendStatusRow.nickname
        ).offset(offset)
        if limit >= 0:
            statement = statement.limit(limit)
        async with self._sessions.session() as session:
            rows = (await session.execute(statement)).scalars().all()
        return [_send_to_record(row) for row in rows]

    @_tracked
    async def get_user_send_status(self, username: str) -> UserSendStatusRecord:
        """Fetch a legacy row or raise ``SendStatusNotFoundError``."""
        statement = select(UserSendStatusRow).where(
            UserSendStatusRow.username == username
        )
        async with self._sessions.session() as session:
            row = (await session.execute(statement)).scalars().first()
        if row is None:
            raise SendStatusNotFoundError(f"Send status for {username} not found")
        return _user_send_to_record(row)


class PostgresSeedRepository:
    """Seed-universe persistence over Postgres + asyncpg."""

    def __init__(
        self,
        sessions: DatabaseSessionFactory,
        *,
        health: DatabaseHealthTracker | None = None,
    ) -> None:
        """Bind the repository to a caller-owned session factory."""
        self._sessions = sessions
        self._health = health

    @_tracked
    async def upsert_seed(
        self,
        *,
        sec_user_id: str,
        nickname: str | None = None,
        source_url: str | None = None,
        source: str = "seed",
        batch: str | None = None,
        excluded: bool = False,
    ) -> SeedAccountRecord:
        """Insert or replace the seed row and return it."""
        stamp = _now_iso()
        async with self._sessions.session() as session:
            async with session.begin():
                row = await session.get(SeedAccountRow, sec_user_id)
                if row is None:
                    row = SeedAccountRow(
                        sec_user_id=sec_user_id,
                        nickname=nickname,
                        source_url=source_url,
                        source=source,
                        batch=batch,
                        excluded=excluded,
                        created_at=stamp,
                        updated_at=stamp,
                    )
                    session.add(row)
                else:
                    row.nickname = nickname
                    row.source_url = source_url
                    row.source = source
                    row.batch = batch
                    row.excluded = excluded
                    row.updated_at = stamp
        return _seed_to_record(row)

    @_tracked
    async def get_seed(self, sec_user_id: str) -> SeedAccountRecord:
        """Fetch by ID or raise ``SeedAccountNotFoundError``."""
        async with self._sessions.session() as session:
            row = await session.get(SeedAccountRow, sec_user_id)
        if row is None:
            raise SeedAccountNotFoundError(f"Seed account {sec_user_id} not found")
        return _seed_to_record(row)

    @_tracked
    async def list_seeds(
        self, *, include_excluded: bool = False
    ) -> list[SeedAccountRecord]:
        """List seeds ordered by creation time."""
        statement = select(SeedAccountRow)
        if not include_excluded:
            statement = statement.where(SeedAccountRow.excluded.is_(False))
        statement = statement.order_by(
            SeedAccountRow.created_at, SeedAccountRow.sec_user_id
        )
        async with self._sessions.session() as session:
            rows = (await session.execute(statement)).scalars().all()
        return [_seed_to_record(row) for row in rows]

    @_tracked
    async def count_seeds(self, *, include_excluded: bool = False) -> int:
        """Count seeds, optionally including excluded rows."""
        statement = select(func.count()).select_from(SeedAccountRow)
        if not include_excluded:
            statement = statement.where(SeedAccountRow.excluded.is_(False))
        async with self._sessions.session() as session:
            total = (await session.execute(statement)).scalar()
        return int(total or 0)


class PostgresProfileRepository:
    """Profile-snapshot persistence over Postgres + asyncpg."""

    def __init__(
        self,
        sessions: DatabaseSessionFactory,
        *,
        health: DatabaseHealthTracker | None = None,
    ) -> None:
        """Bind the repository to a caller-owned session factory."""
        self._sessions = sessions
        self._health = health

    @_tracked
    async def upsert_profile(
        self, *, sec_user_id: str, **fields: Any
    ) -> UserProfileRecord:
        """Insert or patch the snapshot row and return it."""
        known = {
            name: value for name, value in fields.items() if name in _PROFILE_COLUMNS
        }
        stamp = _now_iso()
        async with self._sessions.session() as session:
            async with session.begin():
                row = await session.get(UserProfileRow, sec_user_id)
                if row is None:
                    row = UserProfileRow(sec_user_id=sec_user_id, **known)
                    row.created_at = stamp
                    row.updated_at = stamp
                    session.add(row)
                elif known:
                    for name, value in known.items():
                        setattr(row, name, value)
                    row.updated_at = stamp
        return _profile_to_record(row)

    @_tracked
    async def get_profile(self, sec_user_id: str) -> UserProfileRecord:
        """Fetch by ID or raise ``UserProfileNotFoundError``."""
        async with self._sessions.session() as session:
            row = await session.get(UserProfileRow, sec_user_id)
        if row is None:
            raise UserProfileNotFoundError(f"User profile {sec_user_id} not found")
        return _profile_to_record(row)


class PostgresRoundRepository:
    """Delivery-round persistence over Postgres + asyncpg."""

    def __init__(
        self,
        sessions: DatabaseSessionFactory,
        *,
        health: DatabaseHealthTracker | None = None,
    ) -> None:
        """Bind the repository to a caller-owned session factory."""
        self._sessions = sessions
        self._health = health

    @_tracked
    async def upsert_round(
        self, *, round: str, note: str | None = None
    ) -> DeliveryRoundRecord:
        """Insert or touch the round header and return it."""
        stamp = _now_iso()
        async with self._sessions.session() as session:
            async with session.begin():
                row = await session.get(DeliveryRoundRow, round)
                if row is None:
                    row = DeliveryRoundRow(
                        round=round,
                        note=note,
                        created_at=stamp,
                        updated_at=stamp,
                    )
                    session.add(row)
                else:
                    if note is not None:
                        row.note = note
                    row.updated_at = stamp
        return _round_to_record(row)

    @_tracked
    async def get_round(self, round: str) -> DeliveryRoundRecord:
        """Fetch by name or raise ``DeliveryRoundNotFoundError``."""
        async with self._sessions.session() as session:
            row = await session.get(DeliveryRoundRow, round)
        if row is None:
            raise DeliveryRoundNotFoundError(f"Delivery round {round} not found")
        return _round_to_record(row)

    @_tracked
    async def list_rounds(self) -> list[DeliveryRoundRecord]:
        """List rounds ordered by creation time."""
        statement = select(DeliveryRoundRow).order_by(
            DeliveryRoundRow.created_at, DeliveryRoundRow.round
        )
        async with self._sessions.session() as session:
            rows = (await session.execute(statement)).scalars().all()
        return [_round_to_record(row) for row in rows]
