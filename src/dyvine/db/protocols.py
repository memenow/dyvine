"""Repository protocols decoupling services from the storage backend.

Services depend only on these ``@runtime_checkable`` protocols: the
production container injects the Postgres implementations from
``dyvine.db.postgres``, while unit tests inject the in-memory fakes
from ``tests.support.fake_repos``. The ``isinstance`` guards in the
dependency container keep working because runtime-checkable protocols
verify method presence.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .records import OperationRecord, WatchSubscriptionRecord

#: Statuses no sweep or heartbeat ever touches again.
TERMINAL_STATUSES = frozenset({"completed", "partial", "failed"})

#: Statuses a task is still (or might still be) working through.
ACTIVE_STATUSES = frozenset({"pending", "running"})


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
