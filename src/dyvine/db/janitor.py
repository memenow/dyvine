"""Periodic liveness loop for multi-replica operation rows.

Each replica runs one :class:`RepositoryJanitor` as a background task.
Every interval it heartbeats the rows it owns and fails rows whose
owner went quiet; once a day it also purges long-terminal rows so the
table stays bounded on pods that are rarely restarted.

The cadence (30s beat, 120s staleness) tolerates a missed beat and a
slow event loop while still failing genuinely dead work within a few
minutes. Rolling updates are safe: the draining replica keeps
heartbeating until its tasks finish, so a freshly booted sibling never
sweeps rows that are still being worked.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

from ..core.logging import ContextLogger
from .protocols import OperationRepository

logger = ContextLogger(__name__)

#: Seconds between heartbeat/sweep passes.
HEARTBEAT_INTERVAL_SECONDS = 30.0

#: A row counts as orphaned when its heartbeat is older than this.
ORPHAN_STALE_AFTER_SECONDS = 120.0

#: Seconds between retention purges inside the loop.
PURGE_INTERVAL_SECONDS = 24 * 60 * 60.0


class RepositoryJanitor:
    """Own the periodic heartbeat, orphan-sweep, and purge passes.

    Args:
        operations: Repository holding this replica's rows.
        retention_days: Terminal rows older than this are purged;
            ``0`` or negative disables purging.
        heartbeat_interval: Seconds between heartbeat/sweep passes.
        stale_after_seconds: Heartbeat age that marks a row orphaned.
        purge_interval_seconds: Seconds between retention purges.
    """

    def __init__(
        self,
        operations: OperationRepository,
        *,
        retention_days: int = 30,
        heartbeat_interval: float = HEARTBEAT_INTERVAL_SECONDS,
        stale_after_seconds: float = ORPHAN_STALE_AFTER_SECONDS,
        purge_interval_seconds: float = PURGE_INTERVAL_SECONDS,
    ) -> None:
        """Store the repository handle and the loop cadence."""
        self._operations = operations
        self._retention_days = retention_days
        self._heartbeat_interval = heartbeat_interval
        self._stale_after_seconds = stale_after_seconds
        self._purge_interval_seconds = purge_interval_seconds
        self._last_purge_monotonic = time.monotonic()

    async def run_once(self) -> None:
        """Run one heartbeat + sweep pass, purging when due."""
        await self._operations.heartbeat_owned()
        await self._operations.sweep_orphans(
            stale_after_seconds=self._stale_after_seconds
        )
        now = time.monotonic()
        if (
            self._retention_days > 0
            and now - self._last_purge_monotonic >= self._purge_interval_seconds
        ):
            cutoff = (
                datetime.now(UTC) - timedelta(days=self._retention_days)
            ).isoformat()
            await self._operations.purge_terminal_before(cutoff)
            self._last_purge_monotonic = now

    async def run_forever(self) -> None:
        """Loop :meth:`run_once` until cancelled.

        A failed pass is logged and skipped so one transient database
        error does not kill heartbeats permanently (siblings would
        otherwise sweep this replica's live rows as orphans).
        Cancellation (including during the sleep) propagates to the
        caller, so the container stops the loop with a plain
        ``task.cancel()`` during shutdown.
        """
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "janitor pass failed; continuing on next interval",
                    extra={"interval_seconds": self._heartbeat_interval},
                )
            await asyncio.sleep(self._heartbeat_interval)
