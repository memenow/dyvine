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
from typing import Any, Protocol, runtime_checkable

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
        self, rows: Sequence[dict[str, str | None]]
    ) -> tuple[int, int]: ...

    async def reserve_legacy_permanent_failure_batch(
        self, rows: Sequence[dict[str, str | None]]
    ) -> tuple[int, int]: ...

    async def find_legacy_sent(
        self, *, sec_user_id: str, relative_path: str
    ) -> FileDeliveryRecord | None:
        """Legacy send of this path, or of the same post media slot.

        A caption edit renames a re-downloaded file; matching the post
        creation stamp and media slot keeps it from being sent twice.
        """
        ...

    async def find_prior_sent(
        self, *, sec_user_id: str, relative_path: str
    ) -> FileDeliveryRecord | None:
        """Confirmed ``sent`` record of the same post media slot, any caption."""
        ...

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
        self, rows: Sequence[dict[str, str | None]]
    ) -> tuple[int, int]: ...

    async def upsert_excluded_nickname(self, *, nickname: str, source: str) -> None: ...

    async def list_excluded_nicknames(self) -> set[str]: ...

    async def list_files(
        self,
        *,
        sec_user_id: str | None = None,
        round: str | None = None,
        status: str | None = None,
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


#: Statuses no sweep or heartbeat ever touches again.
TERMINAL_STATUSES = frozenset({"completed", "partial", "failed"})

#: Statuses a task is still (or might still be) working through.
ACTIVE_STATUSES = frozenset({"pending", "running"})

#: Queue states eligible for claiming by a runner.
QUEUE_CLAIMABLE_STATUSES = frozenset({"pending"})

#: Queue states a task is still working through.
QUEUE_ACTIVE_STATUSES = frozenset({"pending", "downloading"})


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
        """Create, persist, and return a new operation record."""
        ...

    async def get_operation(self, operation_id: str) -> OperationRecord:
        """Fetch one operation or raise ``OperationNotFoundError``."""
        ...

    async def get_latest_operation_for_subject(
        self, subject_id: str, *, operation_type: str | None = None
    ) -> OperationRecord:
        """Fetch the most recently updated operation for a subject."""
        ...

    async def update_operation(
        self, operation_id: str, **fields: Any
    ) -> OperationRecord:
        """Update allowed fields, refresh liveness, return the new state.

        Unknown field names are ignored; when no known field is passed
        the row is verified to exist and returned unchanged. Every
        update also refreshes the row's heartbeat so active tasks are
        never mistaken for orphans.
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
        """Update allowed fields and return the new state."""
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
        ...

    async def get_entry(self, key: str) -> QueueEntryRecord:
        """Fetch by key or raise ``QueueEntryNotFoundError``."""
        ...

    async def list_entries(
        self,
        *,
        round: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[QueueEntryRecord]:
        """List entries oldest-first, optionally filtered."""
        ...

    async def count_entries(
        self, *, round: str | None = None, status: str | None = None
    ) -> int:
        """Count entries, optionally filtered."""
        ...

    async def claim_next(
        self, *, round: str | None = None, keys: set[str] | None = None
    ) -> QueueEntryRecord | None:
        """Claim the oldest ``pending`` entry, or ``None`` when empty.

        The winner flips to ``downloading`` under this repository's
        owner identity with a fresh heartbeat. Entries whose
        ``serial_group`` already has a ``downloading`` row are skipped so
        same-nickname accounts never run concurrently. Concurrent
        claimers never receive the same row. ``keys`` restricts the
        eligible set for verified two-account batches.
        """
        ...

    async def update_entry(self, key: str, **fields: Any) -> QueueEntryRecord:
        """Update allowed fields, refresh liveness, return the new state.

        Unknown field names are ignored; when no known field is passed
        the row is verified to exist and returned unchanged.
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

        Unknown field names are ignored; patching an existing row only
        touches the supplied columns.
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
