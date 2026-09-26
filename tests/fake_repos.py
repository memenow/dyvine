"""In-memory repository doubles for unit tests.

These fakes implement :mod:`dyvine.db.protocols` with plain dicts and
mirror the Postgres semantics exactly (same error types, same update
rules, same sweep/heartbeat/purge behavior). The contract suite in
``tests/db/`` runs identical assertions against both the fakes and a
real database so the two can never silently diverge.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

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
from dyvine.db.protocols import (
    ACTIVE_STATUSES,
    QUEUE_CLAIMABLE_STATUSES,
    TERMINAL_STATUSES,
)
from dyvine.db.records import (
    DeliveryRoundRecord,
    OperationRecord,
    QueueEntryRecord,
    SeedAccountRecord,
    SendStatusRecord,
    UserProfileRecord,
    UserSendStatusRecord,
    WatchSubscriptionRecord,
)

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


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()


def _checked_fields(
    allowed: frozenset[str], fields: dict[str, Any], *, method: str
) -> dict[str, Any]:
    """Return ``fields`` unchanged, rejecting unknown names loudly.

    Mirrors ``dyvine.db.postgres._checked_fields`` exactly (same
    message), minus the import: the fake leg stays dependency-free.
    """
    unknown = sorted(name for name in fields if name not in allowed)
    if unknown:
        raise ValueError(f"{method} got unknown field(s): {', '.join(unknown)}")
    return dict(fields)


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
        """Fetch the most recently updated operation for a subject.

        Ties break on ``operation_id`` descending, mirroring the SQL
        backend, so frozen-clock writes resolve identically.
        """
        candidates = [
            row
            for row in self._rows.values()
            if row.subject_id == subject_id
            and (operation_type is None or row.operation_type == operation_type)
        ]
        if not candidates:
            raise OperationNotFoundError(f"Operation {subject_id} not found")
        return max(
            candidates,
            key=lambda row: (
                row.updated_at,
                row.created_at,
                row.operation_id,
            ),
        )

    async def update_operation(
        self, operation_id: str, **fields: Any
    ) -> OperationRecord:
        """Update allowed fields, refresh liveness, return the new state.

        Unknown field names raise ``ValueError``; an empty field set
        verifies existence and returns the row unchanged.
        """
        try:
            current = self._rows[operation_id]
        except KeyError:
            raise OperationNotFoundError(
                f"Operation {operation_id} not found"
            ) from None
        requested = _checked_fields(
            _OPERATION_UPDATABLE_FIELDS, fields, method="update_operation"
        )
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
        to ``created_at`` so legacy rows stay sweepable. Swept rows
        keep their stale heartbeat and owner, exactly like the SQL
        ``UPDATE`` which only rewrites status/message/error/updated_at.
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
                # Write the failed state directly: routing through
                # ``update_operation`` would refresh the heartbeat,
                # which the SQL sweep never does.
                self._rows[key] = replace(
                    row,
                    status="failed",
                    message=_ORPHAN_MESSAGE,
                    error=_ORPHAN_MESSAGE,
                    updated_at=_now_iso(),
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
        """Insert a subscription; duplicate users or IDs raise."""
        if subscription_id is not None and subscription_id in self._rows:
            # Postgres maps ANY unique violation on this insert (user
            # key or explicit ID) to the same user-keyed error, so the
            # fake mirrors that mapping exactly; callers converge on
            # the user row, never on the collided ID.
            raise WatchDuplicateError(
                f"Watch subscription for user {user_id} already exists",
                details={"user_id": user_id},
            )
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
        """Check-then-insert; exact wherever one process writes.

        The in-memory fake has no cross-process lock to take, so this
        mirrors the ordering (duplicate first, then cap) without the
        advisory lock the Postgres backend uses.
        """
        if user_id in self._by_user:
            raise WatchDuplicateError(
                f"Watch subscription for user {user_id} already exists",
                details={"user_id": user_id},
            )
        if len(self._rows) >= max_subscriptions:
            raise RateLimitError(
                "Watch subscription limit reached "
                f"({max_subscriptions}); "
                "delete a subscription first",
                details={"max_subscriptions": max_subscriptions},
            )
        return await self.create_subscription(
            user_id=user_id,
            live_poll_seconds=live_poll_seconds,
            post_poll_seconds=post_poll_seconds,
            enabled=enabled,
            checkpoint=checkpoint,
            subscription_id=subscription_id,
        )

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
        """Update allowed fields and return the new state.

        Unknown field names raise ``ValueError``; an empty field set
        verifies existence and returns the row unchanged.
        """
        try:
            current = self._rows[subscription_id]
        except KeyError:
            raise WatchSubscriptionNotFoundError(
                f"Watch subscription {subscription_id} not found"
            ) from None
        requested = _checked_fields(
            _SUBSCRIPTION_UPDATABLE_FIELDS, fields, method="update_subscription"
        )
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


@dataclass
class FakeQueueState:
    """Shared backing store for multi-owner fake queue scenarios."""

    rows: dict[str, QueueEntryRecord] = field(default_factory=dict)
    owners: dict[str, str | None] = field(default_factory=dict)
    heartbeats: dict[str, str | None] = field(default_factory=dict)


class FakeQueueRepository:
    """Dict-backed ``QueueRepository`` for unit tests."""

    def __init__(
        self, *, owner_id: str = "test-owner", state: FakeQueueState | None = None
    ) -> None:
        """Create an empty queue stamped with ``owner_id``."""
        self._owner_id = owner_id
        self._state = state or FakeQueueState()

    @property
    def _rows(self) -> dict[str, QueueEntryRecord]:
        """Rows keyed by queue key."""
        return self._state.rows

    @property
    def _owners(self) -> dict[str, str | None]:
        """Owner identity per queue key."""
        return self._state.owners

    @property
    def _heartbeats(self) -> dict[str, str | None]:
        """Heartbeat stamp per queue key."""
        return self._state.heartbeats

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
        attempts: int | None = None,
        serial_group: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> QueueEntryRecord:
        """Insert or replace the entry at ``key`` and return it.

        Mirrors the SQL ``ON CONFLICT`` semantics: conflicts rewrite
        the payload but never touch liveness (no owner steal, no
        heartbeat refresh), and ``attempts``/``extra`` change only
        when explicitly passed.
        """
        stamp = _now_iso()
        existing = self._rows.get(key)
        if existing is None:
            attempts_value = 0 if attempts is None else attempts
            extra_value = dict(extra or {})
        else:
            attempts_value = existing.attempts if attempts is None else attempts
            extra_value = dict(extra) if extra is not None else dict(existing.extra)
        record = QueueEntryRecord(
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
            attempts=attempts_value,
            serial_group=serial_group,
            extra=extra_value,
            created_at=existing.created_at if existing else stamp,
            updated_at=stamp,
        )
        self._rows[key] = record
        if existing is None:
            self._owners[key] = self._owner_id
            self._heartbeats[key] = stamp
        return record

    async def get_entry(self, key: str) -> QueueEntryRecord:
        """Fetch by key or raise ``QueueEntryNotFoundError``."""
        try:
            return self._rows[key]
        except KeyError:
            raise QueueEntryNotFoundError(f"Queue entry {key} not found") from None

    async def list_entries(
        self,
        *,
        round: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[QueueEntryRecord]:
        """List entries oldest-first, optionally filtered."""
        rows = [
            row
            for row in self._rows.values()
            if (round is None or row.round == round)
            and (status is None or row.status == status)
        ]
        rows.sort(key=lambda row: (row.updated_at, row.key))
        if offset:
            rows = rows[offset:]
        if limit >= 0:
            rows = rows[:limit]
        return rows

    async def count_entries(
        self, *, round: str | None = None, status: str | None = None
    ) -> int:
        """Count entries, optionally filtered."""
        return sum(
            1
            for row in self._rows.values()
            if (round is None or row.round == round)
            and (status is None or row.status == status)
        )

    async def count_by_status(self, *, round: str | None = None) -> dict[str, int]:
        """Tally entries by status in one snapshot."""
        tallies: dict[str, int] = {}
        for row in self._rows.values():
            if round is not None and row.round != round:
                continue
            tallies[row.status] = tallies.get(row.status, 0) + 1
        return tallies

    async def claim_next(
        self, *, round: str | None = None, keys: set[str] | None = None
    ) -> QueueEntryRecord | None:
        """Claim the oldest ``pending`` entry, or ``None`` when empty.

        The single-process fake needs no row lock; ordering and the
        ``serial_group`` skip mirror the SQL backend exactly.
        """
        busy = {
            row.serial_group
            for row in self._rows.values()
            if row.status == "downloading" and row.serial_group is not None
        }
        candidates = [
            row
            for row in self._rows.values()
            if row.status in QUEUE_CLAIMABLE_STATUSES
            and (round is None or row.round == round)
            and (keys is None or row.key in keys)
            and (row.serial_group is None or row.serial_group not in busy)
        ]
        if not candidates:
            return None
        winner = min(candidates, key=lambda row: (row.updated_at, row.key))
        stamp = _now_iso()
        claimed = QueueEntryRecord(
            key=winner.key,
            round=winner.round,
            kind=winner.kind,
            nickname=winner.nickname,
            sec_user_id=winner.sec_user_id,
            chat_id=winner.chat_id,
            homepage=winner.homepage,
            mode=winner.mode,
            cutoff=winner.cutoff,
            status="downloading",
            operation_id=winner.operation_id,
            op_status=winner.op_status,
            op_message=winner.op_message,
            attempts=winner.attempts,
            serial_group=winner.serial_group,
            extra=dict(winner.extra),
            created_at=winner.created_at,
            updated_at=stamp,
        )
        self._rows[winner.key] = claimed
        self._owners[winner.key] = self._owner_id
        self._heartbeats[winner.key] = stamp
        return claimed

    async def update_entry(self, key: str, **fields: Any) -> QueueEntryRecord:
        """Update allowed fields, refresh liveness, return the new state.

        Unknown field names raise ``ValueError``; an empty field set
        verifies existence and returns the row unchanged.
        """
        try:
            current = self._rows[key]
        except KeyError:
            raise QueueEntryNotFoundError(f"Queue entry {key} not found") from None
        requested = _checked_fields(
            _QUEUE_UPDATABLE_FIELDS, fields, method="update_entry"
        )
        if not requested:
            return current
        values: dict[str, Any] = {
            "key": current.key,
            "round": current.round,
            "kind": current.kind,
            "nickname": current.nickname,
            "sec_user_id": current.sec_user_id,
            "chat_id": current.chat_id,
            "homepage": current.homepage,
            "mode": current.mode,
            "cutoff": current.cutoff,
            "status": current.status,
            "operation_id": current.operation_id,
            "op_status": current.op_status,
            "op_message": current.op_message,
            "attempts": current.attempts,
            "serial_group": current.serial_group,
            "extra": dict(current.extra),
            "created_at": current.created_at,
            "updated_at": current.updated_at,
        }
        for name, value in requested.items():
            values[name] = dict(value or {}) if name == "extra" else value
        stamp = _now_iso()
        values["updated_at"] = stamp
        updated = QueueEntryRecord(**values)
        self._rows[key] = updated
        self._heartbeats[key] = stamp
        return updated

    async def release_stale(
        self,
        *,
        stale_after_seconds: float,
        max_attempts: int,
        round: str | None = None,
    ) -> int:
        """Requeue ``downloading`` rows whose owner stopped heartbeating."""
        cutoff = (
            datetime.now(UTC) - timedelta(seconds=stale_after_seconds)
        ).isoformat()
        stamp = _now_iso()
        touched = 0
        for key, row in list(self._rows.items()):
            heartbeat = self._heartbeats.get(key)
            alive = heartbeat if heartbeat is not None else row.created_at
            owner = self._owners.get(key)
            if not (
                row.status == "downloading"
                and (round is None or row.round == round)
                and alive < cutoff
                and owner != self._owner_id
            ):
                continue
            if row.attempts >= max_attempts:
                freed = replace(row, status="op_issue", updated_at=stamp)
            else:
                freed = replace(
                    row,
                    status="pending",
                    attempts=row.attempts + 1,
                    updated_at=stamp,
                )
            self._rows[key] = freed
            self._heartbeats[key] = stamp
            touched += 1
        return touched


class FakeSendStatusRepository:
    """Dict-backed ``SendStatusRepository`` for unit tests."""

    def __init__(self) -> None:
        """Create empty live and legacy stores."""
        self._rows: dict[str, SendStatusRecord] = {}
        self._legacy: dict[str, UserSendStatusRecord] = {}
        self._legacy_seq = 0

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
        existing = self._rows.get(nickname)
        record = SendStatusRecord(
            nickname=nickname,
            sec_user_id=sec_user_id,
            chat_id=chat_id,
            batch=batch,
            total_files=total_files,
            sent_files=sent_files,
            failed_files=failed_files,
            status=status,
            created_at=existing.created_at if existing else stamp,
            updated_at=stamp,
        )
        self._rows[nickname] = record
        return record

    async def get_send_status(self, nickname: str) -> SendStatusRecord:
        """Fetch by nickname or raise ``SendStatusNotFoundError``."""
        try:
            return self._rows[nickname]
        except KeyError:
            raise SendStatusNotFoundError(
                f"Send status for {nickname} not found"
            ) from None

    async def get_send_status_by_sec(self, sec_user_id: str) -> SendStatusRecord | None:
        """Return the row for ``sec_user_id``, or ``None``."""
        for row in self._rows.values():
            if row.sec_user_id == sec_user_id:
                return row
        return None

    async def list_send_status(
        self, *, batch: str | None = None, limit: int = 100, offset: int = 0
    ) -> list[SendStatusRecord]:
        """List rows oldest-first, optionally filtered by batch."""
        rows = [
            row for row in self._rows.values() if batch is None or row.batch == batch
        ]
        rows.sort(key=lambda row: (row.updated_at, row.nickname))
        if offset:
            rows = rows[offset:]
        if limit >= 0:
            rows = rows[:limit]
        return rows

    async def get_user_send_status(self, username: str) -> UserSendStatusRecord:
        """Fetch a legacy row or raise ``SendStatusNotFoundError``."""
        try:
            return self._legacy[username]
        except KeyError:
            raise SendStatusNotFoundError(
                f"Send status for {username} not found"
            ) from None

    def seed_legacy(
        self,
        username: str,
        *,
        local_files: int = 0,
        sent_files: int = 0,
        failed_files: int = 0,
        status: str = "pending",
        failed_details: str = "",
    ) -> UserSendStatusRecord:
        """Insert a legacy row for tests (not part of the protocol)."""
        self._legacy_seq += 1
        stamp = _now_iso()
        record = UserSendStatusRecord(
            id=self._legacy_seq,
            username=username,
            local_files=local_files,
            sent_files=sent_files,
            failed_files=failed_files,
            status=status,
            failed_details=failed_details,
            created_at=stamp,
            updated_at=stamp,
        )
        self._legacy[username] = record
        return record


class FakeSeedRepository:
    """Dict-backed ``SeedRepository`` for unit tests."""

    def __init__(self) -> None:
        """Create an empty seed store."""
        self._rows: dict[str, SeedAccountRecord] = {}

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
        existing = self._rows.get(sec_user_id)
        record = SeedAccountRecord(
            sec_user_id=sec_user_id,
            nickname=nickname,
            source_url=source_url,
            source=source,
            batch=batch,
            excluded=excluded,
            created_at=existing.created_at if existing else stamp,
            updated_at=stamp,
        )
        self._rows[sec_user_id] = record
        return record

    async def get_seed(self, sec_user_id: str) -> SeedAccountRecord:
        """Fetch by ID or raise ``SeedAccountNotFoundError``."""
        try:
            return self._rows[sec_user_id]
        except KeyError:
            raise SeedAccountNotFoundError(
                f"Seed account {sec_user_id} not found"
            ) from None

    async def list_seeds(
        self, *, include_excluded: bool = False
    ) -> list[SeedAccountRecord]:
        """List seeds ordered by creation time."""
        rows = [
            row for row in self._rows.values() if include_excluded or not row.excluded
        ]
        rows.sort(key=lambda row: (row.created_at, row.sec_user_id))
        return rows

    async def count_seeds(self, *, include_excluded: bool = False) -> int:
        """Count seeds, optionally including excluded rows."""
        return sum(
            1 for row in self._rows.values() if include_excluded or not row.excluded
        )


class FakeProfileRepository:
    """Dict-backed ``ProfileRepository`` for unit tests."""

    def __init__(self) -> None:
        """Create an empty profile store."""
        self._rows: dict[str, UserProfileRecord] = {}

    async def upsert_profile(
        self, *, sec_user_id: str, **fields: Any
    ) -> UserProfileRecord:
        """Insert or patch the snapshot row and return it.

        Unknown field names raise ``ValueError``.
        """
        known = _checked_fields(_PROFILE_COLUMNS, fields, method="upsert_profile")
        stamp = _now_iso()
        existing = self._rows.get(sec_user_id)
        if existing is None:
            record = UserProfileRecord(
                sec_user_id=sec_user_id,
                **known,  # type: ignore[arg-type]
                created_at=stamp,
                updated_at=stamp,
            )
        elif not known:
            return existing
        else:
            record = replace(existing, **known, updated_at=stamp)  # type: ignore[arg-type]
        self._rows[sec_user_id] = record
        return record

    async def get_profile(self, sec_user_id: str) -> UserProfileRecord:
        """Fetch by ID or raise ``UserProfileNotFoundError``."""
        try:
            return self._rows[sec_user_id]
        except KeyError:
            raise UserProfileNotFoundError(
                f"User profile {sec_user_id} not found"
            ) from None


class FakeRoundRepository:
    """Dict-backed ``RoundRepository`` for unit tests."""

    def __init__(self) -> None:
        """Create an empty round store."""
        self._rows: dict[str, DeliveryRoundRecord] = {}

    async def upsert_round(
        self, *, round: str, note: str | None = None
    ) -> DeliveryRoundRecord:
        """Insert or touch the round header and return it."""
        stamp = _now_iso()
        existing = self._rows.get(round)
        if existing is None:
            record = DeliveryRoundRecord(
                round=round, note=note, created_at=stamp, updated_at=stamp
            )
        else:
            record = DeliveryRoundRecord(
                round=round,
                note=note if note is not None else existing.note,
                created_at=existing.created_at,
                updated_at=stamp,
            )
        self._rows[round] = record
        return record

    async def get_round(self, round: str) -> DeliveryRoundRecord:
        """Fetch by name or raise ``DeliveryRoundNotFoundError``."""
        try:
            return self._rows[round]
        except KeyError:
            raise DeliveryRoundNotFoundError(
                f"Delivery round {round} not found"
            ) from None

    async def list_rounds(self) -> list[DeliveryRoundRecord]:
        """List rounds ordered by creation time."""
        rows = list(self._rows.values())
        rows.sort(key=lambda row: (row.created_at, row.round))
        return rows
