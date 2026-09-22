"""Postgres-backed persistence for Dyvine.

Services depend only on the repository protocols
(:class:`OperationRepository`, :class:`WatchRepository`,
:class:`QueueRepository`, :class:`SendStatusRepository`,
:class:`SeedRepository`, :class:`ProfileRepository`,
:class:`RoundRepository`) and the record dataclasses. The container
wires the Postgres implementations plus a :class:`RepositoryJanitor`
liveness loop; tests inject the in-memory fakes from
``tests.fake_repos`` or run the contract suite against a real
database.
"""

from __future__ import annotations

from .health import DatabaseHealthTracker, HealthStatus
from .janitor import (
    HEARTBEAT_INTERVAL_SECONDS,
    ORPHAN_STALE_AFTER_SECONDS,
    PURGE_INTERVAL_SECONDS,
    RepositoryJanitor,
)
from .models import (
    Base,
    DeliveryRoundRow,
    DownloadQueueRow,
    OperationRow,
    SeedAccountRow,
    SendStatusRow,
    UserProfileRow,
    UserSendStatusRow,
    WatchSubscriptionRow,
)
from .postgres import (
    PostgresOperationRepository,
    PostgresProfileRepository,
    PostgresQueueRepository,
    PostgresRoundRepository,
    PostgresSeedRepository,
    PostgresSendStatusRepository,
    PostgresWatchRepository,
)
from .protocols import (
    ACTIVE_STATUSES,
    QUEUE_ACTIVE_STATUSES,
    QUEUE_CLAIMABLE_STATUSES,
    TERMINAL_STATUSES,
    OperationRepository,
    ProfileRepository,
    QueueRepository,
    RoundRepository,
    SeedRepository,
    SendStatusRepository,
    WatchRepository,
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

__all__ = [
    "ACTIVE_STATUSES",
    "QUEUE_ACTIVE_STATUSES",
    "QUEUE_CLAIMABLE_STATUSES",
    "TERMINAL_STATUSES",
    "HEARTBEAT_INTERVAL_SECONDS",
    "ORPHAN_STALE_AFTER_SECONDS",
    "PURGE_INTERVAL_SECONDS",
    "Base",
    "DatabaseHealthTracker",
    "DatabaseSessionFactory",
    "DeliveryRoundRecord",
    "DeliveryRoundRow",
    "DownloadQueueRow",
    "HealthStatus",
    "OperationRecord",
    "OperationRepository",
    "OperationRow",
    "PostgresOperationRepository",
    "PostgresProfileRepository",
    "PostgresQueueRepository",
    "PostgresRoundRepository",
    "PostgresSeedRepository",
    "PostgresSendStatusRepository",
    "PostgresWatchRepository",
    "ProfileRepository",
    "QueueEntryRecord",
    "QueueRepository",
    "RepositoryJanitor",
    "RoundRepository",
    "SeedAccountRecord",
    "SeedAccountRow",
    "SeedRepository",
    "SendStatusRecord",
    "SendStatusRepository",
    "SendStatusRow",
    "UserProfileRecord",
    "UserProfileRow",
    "UserSendStatusRecord",
    "UserSendStatusRow",
    "WatchRepository",
    "WatchSubscriptionRecord",
    "WatchSubscriptionRow",
]
