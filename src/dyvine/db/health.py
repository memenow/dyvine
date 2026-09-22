"""Last-known database reachability for passive health reporting.

The :class:`DatabaseHealthTracker` records the outcome of real
repository calls: every Postgres repository method reports success when
the database answers (even when the answer is a domain error such as
"not found") and failure only when the database itself is unreachable.
Readers then consult the last-known state instead of opening a
connection of their own, so idle health checks never touch the
database.

``"unknown"`` (no call observed yet) counts as healthy: a genuinely
unreachable database fails the first real call loudly instead of
lingering in this state.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

#: Reachability states observed from real repository calls.
HealthStatus = Literal["unknown", "available", "unavailable"]


class DatabaseHealthTracker:
    """Record the last-known database reachability.

    Single-threaded by design: repositories only call it from the
    application's event loop, so plain attribute writes are enough.
    """

    def __init__(self) -> None:
        """Start with no observation recorded."""
        self._status: HealthStatus = "unknown"
        self._checked_at: str | None = None

    def note_success(self) -> None:
        """Record that the database answered a real call."""
        self._status = "available"
        self._checked_at = datetime.now(UTC).isoformat()

    def note_failure(self) -> None:
        """Record that the database was unreachable for a real call."""
        self._status = "unavailable"
        self._checked_at = datetime.now(UTC).isoformat()

    @property
    def snapshot(self) -> tuple[HealthStatus, str | None]:
        """Return the ``(status, checked_at)`` pair for probes."""
        return (self._status, self._checked_at)
