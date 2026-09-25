"""Repository protocols decoupling services from the storage backend.

Services depend only on these ``@runtime_checkable`` protocols: the
production container injects the Postgres implementations from
``dyvine.db.postgres``, while unit tests inject the in-memory fakes
from ``tests.support.fake_repos``. The ``isinstance`` guards in the
dependency container keep working because runtime-checkable protocols
verify method presence.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import (
    Any,
    Literal,
    NamedTuple,
    Protocol,
    Required,
    TypedDict,
    runtime_checkable,
)

from .records import (
    DeliveryGroupRecord,
    DeliveryRoundRecord,
    FileDeliveryRecord,
    LegacyEvidenceRecord,
    OperationRecord,
    QueueEntryRecord,
    SeedAccountRecord,
    SendStatusRecord,
    UserProfileRecord,
    UserSendStatusRecord,
    WatchSubscriptionRecord,
)

#: Lifecycle of one tracked async operation. New writes must use these
#: values; records keep plain ``str`` because migrated rows predate the
#: closed set.
OperationStatus = Literal["pending", "running", "completed", "partial", "failed"]

#: Lifecycle of one download-queue entry. ``op_status`` mirrors
#: :data:`OperationStatus` (the linked operation's state).
QueueEntryStatus = Literal[
    "pending",
    "downloading",
    "op_done",
    "op_issue",
    "send_issue",
    "needs_review",
    "needs_reconciliation",
    "pair_needs_review",
    "skipped_404",
]

#: Queue entry download modes.
QueueMode = Literal["full", "post", "incremental"]

#: Lifecycle of one ledger file row.
FileDeliveryStatus = Literal[
    "planned",
    "uploaded",
    "sending",
    "sent",
    "needs_review",
    "permanent_failure",
    "legacy_confirmed_sent",
]


class BatchOutcome(NamedTuple):
    """Result of one idempotent legacy batch: ``(inserted, skipped)``.

    A ``NamedTuple`` so existing ``inserted, skipped = ...`` unpacking
    and ``== (n, m)`` comparisons keep working unchanged.
    """

    inserted: int
    skipped: int


class LegacySentBatchRow(TypedDict, total=False):
    """One ``reserve_legacy_sent_batch`` input row."""

    sec_user_id: Required[str]
    round: Required[str]
    relative_path: Required[str]
    chat_id: str | None
    parent_id: str | None
    legacy_source_path: str | None
    legacy_progress_file: str | None


class LegacyFailureBatchRow(TypedDict, total=False):
    """One ``reserve_legacy_permanent_failure_batch`` input row.

    The round is fixed to ``"legacy"`` by the implementation; callers
    do not supply it.
    """

    sec_user_id: Required[str]
    relative_path: Required[str]
    legacy_source_path: str | None
    legacy_progress_file: str | None


class LegacyEvidenceBatchRow(TypedDict, total=False):
    """One ``upsert_legacy_evidence_batch`` input row."""

    source_file: Required[str]
    legacy_path: Required[str]
    legacy_state: Required[str]
    reason: Required[str]
    nickname: str | None
    sec_user_id: str | None


@runtime_checkable
class DeliveryLedgerRepository(Protocol):
    """Atomic checkpoints around Feishu group, topic, and file writes."""

    async def reserve_group(
        self,
        *,
        round: str,
        sec_user_id: str,
        nickname: str,
        owner_open_id: str,
        avatar_url: str | None = None,
    ) -> DeliveryGroupRecord: ...

    async def import_legacy_group_topic(
        self,
        *,
        round: str,
        sec_user_id: str,
        nickname: str,
        chat_id: str,
        topic_message_id: str,
        source_file: str,
    ) -> DeliveryGroupRecord: ...

    async def adopt_prior_verified_group_for_round(
        self,
        *,
        round: str,
        sec_user_id: str,
        nickname: str,
        owner_open_id: str,
    ) -> DeliveryGroupRecord | None: ...

    async def get_group(
        self, *, round: str, sec_user_id: str
    ) -> DeliveryGroupRecord | None: ...

    async def mark_group_ready(self, key: str, chat_id: str) -> DeliveryGroupRecord: ...

    async def mark_group_review(self, key: str) -> DeliveryGroupRecord: ...

    async def rotate_group_uuid(
        self, key: str, create_name: str
    ) -> DeliveryGroupRecord: ...

    async def begin_topic(self, key: str) -> DeliveryGroupRecord: ...

    async def mark_topic_ready(
        self, key: str, message_id: str
    ) -> DeliveryGroupRecord: ...

    async def mark_topic_review(self, key: str) -> DeliveryGroupRecord: ...

    async def set_avatar_key(self, key: str, image_key: str) -> DeliveryGroupRecord: ...

    async def reserve_file(
        self,
        *,
        media_id: str,
        round: str,
        sec_user_id: str,
        relative_path: str,
        content_sha256: str,
        chat_id: str,
        parent_id: str,
    ) -> FileDeliveryRecord: ...

    async def get_file(self, media_id: str) -> FileDeliveryRecord | None: ...

    async def reserve_legacy_sent(
        self,
        *,
        round: str,
        sec_user_id: str,
        relative_path: str,
        chat_id: str | None = None,
        parent_id: str | None = None,
        legacy_source_path: str | None = None,
        legacy_progress_file: str | None = None,
    ) -> FileDeliveryRecord: ...

    async def reserve_legacy_sent_batch(
        self, rows: Sequence[LegacySentBatchRow]
    ) -> BatchOutcome:
        """Insert a bounded batch; return ``(inserted, skipped)``.

        ``skipped`` counts rows already present (idempotent replays).
        """
        ...

    async def reserve_legacy_permanent_failure_batch(
        self, rows: Sequence[LegacyFailureBatchRow]
    ) -> BatchOutcome:
        """Insert a bounded batch; return ``(inserted, skipped)``.

        ``skipped`` counts rows already present (idempotent replays).
        """
        ...

    async def find_legacy_sent(
        self, *, sec_user_id: str, relative_path: str
    ) -> FileDeliveryRecord | None: ...

    async def find_legacy_permanent_failure(
        self, *, sec_user_id: str, relative_path: str
    ) -> FileDeliveryRecord | None: ...

    async def find_legacy_unverified_hold(
        self, *, legacy_path: str
    ) -> LegacyEvidenceRecord | None: ...

    async def upsert_legacy_evidence(
        self,
        *,
        source_file: str,
        legacy_path: str,
        legacy_state: str,
        nickname: str | None,
        sec_user_id: str | None,
        reason: str,
    ) -> LegacyEvidenceRecord: ...

    async def upsert_legacy_evidence_batch(
        self, rows: Sequence[LegacyEvidenceBatchRow]
    ) -> BatchOutcome:
        """Insert a bounded batch; return ``(inserted, skipped)``.

        ``skipped`` counts rows already present (idempotent replays).
        """
        ...

    async def upsert_excluded_nickname(self, *, nickname: str, source: str) -> None: ...

    async def list_excluded_nicknames(self) -> set[str]: ...

    async def list_files(
        self,
        *,
        sec_user_id: str | None = None,
        round: str | None = None,
        status: FileDeliveryStatus | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> list[FileDeliveryRecord]: ...

    async def set_file_key(
        self, media_id: str, file_key: str
    ) -> FileDeliveryRecord: ...

    async def begin_send(self, media_id: str) -> FileDeliveryRecord: ...

    async def mark_sent(self, media_id: str, message_id: str) -> FileDeliveryRecord: ...

    async def mark_file_review(self, media_id: str) -> FileDeliveryRecord: ...

    async def mark_permanent_failure(self, media_id: str) -> FileDeliveryRecord: ...


#: Statuses no sweep or heartbeat ever touches again. Subset of
#: :data:`OperationStatus` (pinned by ``tests/db/test_protocols.py``).
TERMINAL_STATUSES: frozenset[OperationStatus] = frozenset(
    {"completed", "partial", "failed"}
)

#: Statuses a task is still (or might still be) working through.
#: Subset of :data:`OperationStatus`.
ACTIVE_STATUSES: frozenset[OperationStatus] = frozenset({"pending", "running"})

#: Queue states eligible for claiming by a runner. Subset of
#: :data:`QueueEntryStatus`.
QUEUE_CLAIMABLE_STATUSES: frozenset[QueueEntryStatus] = frozenset({"pending"})

#: Queue states a task is still working through. Subset of
#: :data:`QueueEntryStatus`.
QUEUE_ACTIVE_STATUSES: frozenset[QueueEntryStatus] = frozenset(
    {"pending", "downloading"}
)


@runtime_checkable
class OperationRepository(Protocol):
    """Persistence contract for asynchronous operation state."""

    async def healthcheck(self) -> None:
        """Verify the backend is reachable; raise when it is not."""
        ...

    async def create_operation(
        self,
        *,
        operation_type: str,
        subject_id: str,
        status: OperationStatus,
        message: str,
        progress: float | None = None,
        total_items: int | None = None,
        completed_items: int | None = None,
        download_path: str | None = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
        operation_id: str | None = None,
    ) -> OperationRecord:
        """Create, persist, and return a new operation record."""
        ...

    async def get_operation(self, operation_id: str) -> OperationRecord:
        """Fetch one operation or raise ``OperationNotFoundError``."""
        ...

    async def get_latest_operation_for_subject(
        self, subject_id: str, *, operation_type: str | None = None
    ) -> OperationRecord:
        """Fetch the most recently updated operation for a subject.

        Raises:
            OperationNotFoundError: If no operation exists for the
                subject (and type filter).
        """
        ...

    async def update_operation(
        self, operation_id: str, **fields: Any
    ) -> OperationRecord:
        """Update allowed fields, refresh liveness, return the new state.

        Unknown field names raise ``ValueError`` (a typo must never
        read as success); when no field is passed the row is verified
        to exist and returned unchanged. Every update also refreshes
        the row's heartbeat so active tasks are never mistaken for
        orphans.
        """
        ...

    async def sweep_orphans(self, *, stale_after_seconds: float) -> int:
        """Fail active rows whose owner stopped heartbeating.

        Only rows that are still ``pending``/``running``, whose
        heartbeat is older than ``stale_after_seconds``, and that are
        not owned by this repository's owner are touched. Returns the
        number of rows failed.
        """
        ...

    async def heartbeat_owned(self) -> int:
        """Refresh the heartbeat of every active row this owner holds."""
        ...

    async def purge_terminal_before(self, cutoff_iso: str) -> int:
        """Delete terminal rows last updated before ``cutoff_iso``."""
        ...


@runtime_checkable
class WatchRepository(Protocol):
    """Persistence contract for watch subscriptions."""

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
        """Create and persist a subscription.

        Raises:
            WatchDuplicateError: If ``user_id`` already has one.
        """
        ...

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
        """Create a subscription, enforcing the cap atomically.

        The cap check and the insert are one atomic unit: concurrent
        creators on other replicas or processes cannot both slip under
        a stale count the way a separate ``count_subscriptions`` check
        allows. The duplicate check comes first, so a duplicate at cap
        raises ``WatchDuplicateError`` (idempotent convergence) rather
        than ``RateLimitError``. Backends without cross-process locking
        (the in-memory fake) implement the same ordering, which is
        exact wherever only one process writes.

        Raises:
            RateLimitError: If the table already holds
                ``max_subscriptions`` rows.
            WatchDuplicateError: If ``user_id`` already has one.
        """
        ...

    async def get_subscription(self, subscription_id: str) -> WatchSubscriptionRecord:
        """Fetch by ID or raise ``WatchSubscriptionNotFoundError``."""
        ...

    async def get_subscription_by_user(
        self, user_id: str
    ) -> WatchSubscriptionRecord | None:
        """Return the subscription for ``user_id``, or ``None``."""
        ...

    async def list_subscriptions(
        self, *, enabled_only: bool = False
    ) -> list[WatchSubscriptionRecord]:
        """Return subscriptions ordered by creation time."""
        ...

    async def count_subscriptions(self) -> int:
        """Return the total number of persisted subscriptions."""
        ...

    async def update_subscription(
        self, subscription_id: str, **fields: Any
    ) -> WatchSubscriptionRecord:
        """Update allowed fields and return the new state.

        Unknown field names raise ``ValueError``; when no field is
        passed the row is verified to exist and returned unchanged.
        """
        ...

    async def delete_subscription(self, subscription_id: str) -> bool:
        """Delete a subscription; ``True`` when a row was removed."""
        ...


@runtime_checkable
class QueueRepository(Protocol):
    """Persistence contract for durable download-queue entries."""

    async def upsert_entry(
        self,
        *,
        key: str,
        round: str,
        nickname: str,
        sec_user_id: str,
        mode: QueueMode,
        status: QueueEntryStatus,
        kind: str | None = None,
        chat_id: str | None = None,
        homepage: str | None = None,
        cutoff: str | None = None,
        operation_id: str | None = None,
        op_status: OperationStatus | None = None,
        op_message: str | None = None,
        attempts: int | None = None,
        serial_group: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> QueueEntryRecord:
        """Insert or replace the entry at ``key`` and return it.

        Concurrent upserts of the same key converge on one row (atomic
        upsert, never an integrity error). The update branch rewrites
        the payload fields but never touches liveness (``owner_id`` /
        ``heartbeat_at`` belong to the claim/release paths), and
        ``attempts``/``extra`` change only when explicitly passed (a
        ``None`` keeps the stored value on conflict and means ``0`` /
        ``{}`` on insert), so a field-patching call cannot zero the
        retry budget.
        """
        ...

    async def get_entry(self, key: str) -> QueueEntryRecord:
        """Fetch by key or raise ``QueueEntryNotFoundError``."""
        ...

    async def list_entries(
        self,
        *,
        round: str | None = None,
        status: QueueEntryStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[QueueEntryRecord]:
        """List entries oldest-first, optionally filtered."""
        ...

    async def count_entries(
        self, *, round: str | None = None, status: QueueEntryStatus | None = None
    ) -> int:
        """Count entries, optionally filtered."""
        ...

    async def count_by_status(self, *, round: str | None = None) -> dict[str, int]:
        """Tally entries by status in one query (atomic snapshot).

        Statuses are an open set inherited from legacy data, so the keys
        are plain strings rather than :data:`QueueEntryStatus`. Backs
        ``QueueService.round_status`` so its ``total`` and ``by_status``
        always describe the same instant.
        """
        ...

    async def claim_next(
        self, *, round: str | None = None, keys: set[str] | None = None
    ) -> QueueEntryRecord | None:
        """Claim the oldest ``pending`` entry, or ``None`` when empty.

        The winner flips to ``downloading`` under this repository's
        owner identity with a fresh heartbeat. Entries whose
        ``serial_group`` already has a ``downloading`` row are skipped so
        same-nickname accounts never run concurrently -- not even when
        two replicas claim the same group at the same instant (the
        loser sees ``None`` and retries on the next round). Concurrent
        claimers never receive the same row. ``keys`` restricts the
        eligible set for verified two-account batches.
        """
        ...

    async def update_entry(self, key: str, **fields: Any) -> QueueEntryRecord:
        """Update allowed fields, refresh liveness, return the new state.

        Unknown field names raise ``ValueError`` (a typo must never
        read as success); when no field is passed the row is verified
        to exist and returned unchanged.
        """
        ...

    async def release_stale(
        self,
        *,
        stale_after_seconds: float,
        max_attempts: int,
        round: str | None = None,
    ) -> int:
        """Requeue ``downloading`` rows whose owner stopped heartbeating.

        A stale row returns to ``pending`` with ``attempts`` incremented
        when it still has retries left, else flips to ``op_issue``.
        Returns the number of rows touched.
        """
        ...


@runtime_checkable
class SendStatusRepository(Protocol):
    """Persistence contract for per-account delivery counters."""

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
        ...

    async def get_send_status(self, nickname: str) -> SendStatusRecord:
        """Fetch by nickname or raise ``SendStatusNotFoundError``."""
        ...

    async def get_send_status_by_sec(self, sec_user_id: str) -> SendStatusRecord | None:
        """Return the row for ``sec_user_id``, or ``None``."""
        ...

    async def list_send_status(
        self, *, batch: str | None = None, limit: int = 100, offset: int = 0
    ) -> list[SendStatusRecord]:
        """List rows oldest-first, optionally filtered by batch."""
        ...

    async def get_user_send_status(self, username: str) -> UserSendStatusRecord:
        """Fetch a legacy row or raise ``SendStatusNotFoundError``."""
        ...


@runtime_checkable
class SeedRepository(Protocol):
    """Persistence contract for the seed account universe."""

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
        ...

    async def get_seed(self, sec_user_id: str) -> SeedAccountRecord:
        """Fetch by ID or raise ``SeedAccountNotFoundError``."""
        ...

    async def list_seeds(
        self, *, include_excluded: bool = False
    ) -> list[SeedAccountRecord]:
        """List seeds ordered by creation time."""
        ...

    async def count_seeds(self, *, include_excluded: bool = False) -> int:
        """Count seeds, optionally including excluded rows."""
        ...


@runtime_checkable
class ProfileRepository(Protocol):
    """Persistence contract for cached Douyin profile snapshots."""

    async def upsert_profile(
        self, *, sec_user_id: str, **fields: Any
    ) -> UserProfileRecord:
        """Insert or patch the snapshot row and return it.

        Unknown field names raise ``ValueError``; patching an existing
        row only touches the supplied columns.
        """
        ...

    async def get_profile(self, sec_user_id: str) -> UserProfileRecord:
        """Fetch by ID or raise ``UserProfileNotFoundError``."""
        ...


@runtime_checkable
class RoundRepository(Protocol):
    """Persistence contract for delivery-round headers."""

    async def upsert_round(
        self, *, round: str, note: str | None = None
    ) -> DeliveryRoundRecord:
        """Insert or touch the round header and return it."""
        ...

    async def get_round(self, round: str) -> DeliveryRoundRecord:
        """Fetch by name or raise ``DeliveryRoundNotFoundError``."""
        ...

    async def list_rounds(self) -> list[DeliveryRoundRecord]:
        """List rounds ordered by creation time."""
        ...
