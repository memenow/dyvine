"""Persistent storage for watch-mode subscriptions.

A watch subscription is a long-lived, mutable configuration row: which
Douyin user to monitor, how often to poll for live status and new posts,
and an incremental-download checkpoint. Unlike :class:`OperationStore`
records (one row per unit of background work), subscriptions must survive a
process restart and be enumerated on startup so their watcher loops can be
resumed. That is why they live in a dedicated ``watch_subscriptions`` table
rather than the ``operations`` table, whose
``mark_incomplete_operations_failed`` sweep flips every ``pending`` /
``running`` row to ``failed`` on boot and would otherwise clobber a
long-lived subscription.

The store mirrors :class:`dyvine.core.operations.OperationStore`'s
connection discipline: a single lazily-opened writer connection guarded by
a ``threading.Lock`` plus one autocommit reader connection per worker
thread, with every public coroutine dispatching its blocking ``sqlite3``
call to the container-owned executor so the event loop never stalls. It
shares the same SQLite file as the operation store (WAL is a
database-level mode), adding its own table alongside ``operations``. The
writer-slot holder and finalizer helper are reused from
:mod:`dyvine.core.operations` so both stores release their handles the same
way before Python flags an unclosed database under ``-W error``.
"""

from __future__ import annotations

import asyncio
import functools
import json
import sqlite3
import threading
import uuid
import weakref
from collections.abc import Callable
from concurrent.futures import Executor
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from .exceptions import WatchSubscriptionNotFoundError
from .operations import _close_connections, _WriterSlot
from .settings import settings

_R = TypeVar("_R")

# Columns a caller may update after creation. ``subject_id`` / ``user_id``
# and the immutable identifiers are intentionally excluded so an update
# cannot break the ``UNIQUE(user_id)`` invariant or repoint a row.
_UPDATABLE_FIELDS = frozenset(
    {
        "enabled",
        "live_poll_seconds",
        "post_poll_seconds",
        "checkpoint",
        "last_live_check",
        "last_post_check",
    }
)


@dataclass(slots=True)
class WatchSubscriptionRecord:
    """Structured representation of a persisted watch subscription."""

    subscription_id: str
    user_id: str
    enabled: bool
    live_poll_seconds: int
    post_poll_seconds: int
    checkpoint: dict[str, Any]
    last_live_check: str | None
    last_post_check: str | None
    created_at: str
    updated_at: str


class WatchSubscriptionStore:
    """SQLite-backed persistence for watch subscriptions.

    Public methods are coroutines that dispatch their blocking SQLite work
    to the configured executor, exactly like :class:`OperationStore`. The
    store keeps one writer connection guarded by a ``threading.Lock`` and
    one reader connection per worker thread (opened lazily); readers never
    take the lock so concurrent reads run in parallel.
    """

    def __init__(
        self,
        db_path: str | None = None,
        *,
        executor: Executor | None = None,
    ) -> None:
        """Initialize the SQLite watch-subscription store and schema.

        Args:
            db_path: Optional database path. Defaults to the same operation
                state database the rest of the service uses.
            executor: Optional executor for blocking SQLite calls.
        """
        # Default to the shared operation database so a single SQLite file
        # (already in WAL mode) carries both tables; the container hands us
        # the same dedicated sqlite executor for write serialisation.
        self.db_path = Path(db_path or settings.operation_db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._executor: Executor | None = executor
        self._reader_local = threading.local()
        self._reader_connections: dict[int, sqlite3.Connection] = {}
        self._reader_lock = threading.Lock()
        self._writer_slot = _WriterSlot()
        self._closed = False
        self._finalizer = weakref.finalize(
            self,
            _close_connections,
            self._reader_connections,
            self._writer_slot,
        )
        self._initialize()

    def set_executor(self, executor: Executor | None) -> None:
        """Attach a dedicated executor after construction.

        Late binding lets the store bootstrap synchronously inside
        ``ServiceContainer`` (no running event loop yet) while still routing
        every later async call through the dedicated worker pool.
        """
        self._executor = executor

    @property
    def is_closed(self) -> bool:
        """Whether :meth:`shutdown` has been called."""
        return self._closed

    async def _run(self, func: Callable[..., _R], /, *args: Any, **kwargs: Any) -> _R:
        """Dispatch a blocking SQLite call to the configured executor."""
        if self._closed:
            raise RuntimeError(
                "WatchSubscriptionStore has been shut down; cannot dispatch new work"
            )
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor, functools.partial(func, *args, **kwargs)
        )

    def _connect(self, *, autocommit: bool = False) -> sqlite3.Connection:
        """Open and configure a SQLite connection for the store."""
        isolation_level: str | None = None if autocommit else ""
        connection = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            isolation_level=isolation_level,  # type: ignore[arg-type]
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA synchronous=NORMAL;")
            connection.execute("PRAGMA busy_timeout=5000;")
        except Exception:
            connection.close()
            raise
        return connection

    def _initialize(self) -> None:
        """Create the watch_subscriptions table, index, and WAL journal mode."""
        with self._lock, closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL;")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS watch_subscriptions (
                    subscription_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    live_poll_seconds INTEGER NOT NULL,
                    post_poll_seconds INTEGER NOT NULL,
                    checkpoint TEXT NOT NULL,
                    last_live_check TEXT,
                    last_post_check TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """)
            # One subscription per user. The UNIQUE constraint is the
            # backstop that makes ``POST /watch`` idempotent even under a
            # race between two concurrent creates for the same user.
            connection.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_watch_user
                ON watch_subscriptions (user_id)
                """)
            connection.commit()

    def _reader_connection(self) -> sqlite3.Connection:
        """Return the current thread's reader connection, opening one lazily."""
        existing: sqlite3.Connection | None = getattr(
            self._reader_local, "connection", None
        )
        if existing is not None:
            return existing
        new_connection = self._connect(autocommit=True)
        self._reader_local.connection = new_connection
        with self._reader_lock:
            self._reader_connections[threading.get_ident()] = new_connection
        return new_connection

    def _writer_connection(self) -> sqlite3.Connection:
        """Return the shared writer connection, opening it on first use.

        Callers must hold ``self._lock`` before invoking this.
        """
        if self._writer_slot.connection is None:
            self._writer_slot.connection = self._connect()
        return self._writer_slot.connection

    def shutdown(self) -> None:
        """Close every live reader connection and the writer connection.

        Called from ``ServiceContainer.shutdown`` before the owning SQLite
        executor reaps its worker threads. Safe to call multiple times.
        """
        with self._reader_lock:
            self._closed = True
            readers = list(self._reader_connections.values())
            self._reader_connections.clear()
        for connection in readers:
            try:
                connection.close()
            except sqlite3.Error:
                pass
        if hasattr(self._reader_local, "connection"):
            del self._reader_local.connection

        with self._lock:
            if self._writer_slot.connection is not None:
                try:
                    self._writer_slot.connection.close()
                except sqlite3.Error:
                    pass
                self._writer_slot.connection = None

        self._finalizer.detach()

    @staticmethod
    def _now() -> str:
        """Return the current UTC timestamp in ISO 8601 format."""
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _from_row(row: sqlite3.Row | None) -> WatchSubscriptionRecord | None:
        """Convert a SQLite row into a watch-subscription record."""
        if row is None:
            return None
        checkpoint_raw = row["checkpoint"]
        checkpoint = json.loads(str(checkpoint_raw)) if checkpoint_raw else {}
        return WatchSubscriptionRecord(
            subscription_id=str(row["subscription_id"]),
            user_id=str(row["user_id"]),
            enabled=bool(row["enabled"]),
            live_poll_seconds=int(row["live_poll_seconds"]),
            post_poll_seconds=int(row["post_poll_seconds"]),
            checkpoint=checkpoint if isinstance(checkpoint, dict) else {},
            last_live_check=(
                str(row["last_live_check"])
                if row["last_live_check"] is not None
                else None
            ),
            last_post_check=(
                str(row["last_post_check"])
                if row["last_post_check"] is not None
                else None
            ),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

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
        """Create and persist a new watch subscription.

        Raises:
            sqlite3.IntegrityError: If a subscription for ``user_id`` already
                exists (the ``UNIQUE(user_id)`` backstop). Callers that want
                idempotent behaviour should check
                :meth:`get_subscription_by_user` under a lock first.
        """
        return await self._run(
            self._create_subscription_sync,
            user_id=user_id,
            live_poll_seconds=live_poll_seconds,
            post_poll_seconds=post_poll_seconds,
            enabled=enabled,
            checkpoint=checkpoint,
            subscription_id=subscription_id,
        )

    def _create_subscription_sync(
        self,
        *,
        user_id: str,
        live_poll_seconds: int,
        post_poll_seconds: int,
        enabled: bool,
        checkpoint: dict[str, Any] | None,
        subscription_id: str | None,
    ) -> WatchSubscriptionRecord:
        """Synchronously insert and return a new subscription record."""
        created_at = self._now()
        record = WatchSubscriptionRecord(
            subscription_id=subscription_id or str(uuid.uuid4()),
            user_id=user_id,
            enabled=enabled,
            live_poll_seconds=live_poll_seconds,
            post_poll_seconds=post_poll_seconds,
            checkpoint=checkpoint or {},
            last_live_check=None,
            last_post_check=None,
            created_at=created_at,
            updated_at=created_at,
        )
        with self._lock:
            connection = self._writer_connection()
            connection.execute(
                """
                INSERT INTO watch_subscriptions (
                    subscription_id, user_id, enabled, live_poll_seconds,
                    post_poll_seconds, checkpoint, last_live_check,
                    last_post_check, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.subscription_id,
                    record.user_id,
                    1 if record.enabled else 0,
                    record.live_poll_seconds,
                    record.post_poll_seconds,
                    json.dumps(record.checkpoint, sort_keys=True),
                    record.last_live_check,
                    record.last_post_check,
                    record.created_at,
                    record.updated_at,
                ),
            )
            connection.commit()
        return record

    async def get_subscription(self, subscription_id: str) -> WatchSubscriptionRecord:
        """Fetch a subscription by ID or raise ``WatchSubscriptionNotFoundError``."""
        return await self._run(self._get_subscription_sync, subscription_id)

    def _get_subscription_sync(self, subscription_id: str) -> WatchSubscriptionRecord:
        """Synchronously fetch a subscription by ID or raise when missing."""
        connection = self._reader_connection()
        row = connection.execute(
            "SELECT * FROM watch_subscriptions WHERE subscription_id = ?",
            (subscription_id,),
        ).fetchone()
        record = self._from_row(row)
        if record is None:
            raise WatchSubscriptionNotFoundError(
                f"Watch subscription {subscription_id} not found"
            )
        return record

    async def get_subscription_by_user(
        self, user_id: str
    ) -> WatchSubscriptionRecord | None:
        """Return the subscription for ``user_id``, or ``None`` when absent."""
        return await self._run(self._get_subscription_by_user_sync, user_id)

    def _get_subscription_by_user_sync(
        self, user_id: str
    ) -> WatchSubscriptionRecord | None:
        """Synchronously fetch the subscription for a user without raising."""
        connection = self._reader_connection()
        row = connection.execute(
            "SELECT * FROM watch_subscriptions WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return self._from_row(row)

    async def list_subscriptions(
        self, *, enabled_only: bool = False
    ) -> list[WatchSubscriptionRecord]:
        """Return all subscriptions, optionally only the enabled ones.

        This is the enumeration the resume-on-restart path depends on; the
        operation store has no equivalent query, which is one reason watch
        subscriptions need their own table.
        """
        return await self._run(self._list_subscriptions_sync, enabled_only)

    def _list_subscriptions_sync(
        self, enabled_only: bool
    ) -> list[WatchSubscriptionRecord]:
        """Synchronously return subscriptions ordered by creation time."""
        connection = self._reader_connection()
        query = "SELECT * FROM watch_subscriptions"
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY created_at ASC, subscription_id ASC"
        rows = connection.execute(query).fetchall()
        records = [self._from_row(row) for row in rows]
        return [record for record in records if record is not None]

    async def count_subscriptions(self) -> int:
        """Return the total number of persisted subscriptions."""
        return await self._run(self._count_subscriptions_sync)

    def _count_subscriptions_sync(self) -> int:
        """Synchronously count persisted subscriptions."""
        connection = self._reader_connection()
        row = connection.execute(
            "SELECT COUNT(*) AS total FROM watch_subscriptions"
        ).fetchone()
        return int(row["total"]) if row is not None else 0

    async def update_subscription(
        self, subscription_id: str, **fields: Any
    ) -> WatchSubscriptionRecord:
        """Update selected fields on a subscription and return the new state."""
        return await self._run(
            self._update_subscription_sync, subscription_id, **fields
        )

    def _update_subscription_sync(
        self, subscription_id: str, **fields: Any
    ) -> WatchSubscriptionRecord:
        """Synchronously update allowed fields and return the new record."""
        requested = {
            key: value for key, value in fields.items() if key in _UPDATABLE_FIELDS
        }
        with self._lock:
            connection = self._writer_connection()
            if not requested:
                # Nothing to change: verify the row exists under the lock so
                # callers still serialise with concurrent writers.
                row = connection.execute(
                    "SELECT * FROM watch_subscriptions WHERE subscription_id = ?",
                    (subscription_id,),
                ).fetchone()
                record = self._from_row(row)
                if record is None:
                    raise WatchSubscriptionNotFoundError(
                        f"Watch subscription {subscription_id} not found"
                    )
                return record

            updates: dict[str, Any] = {}
            for key, value in requested.items():
                if key == "checkpoint":
                    updates[key] = json.dumps(value, sort_keys=True)
                elif key == "enabled":
                    updates[key] = 1 if value else 0
                else:
                    updates[key] = value
            updates["updated_at"] = self._now()

            assignments = ", ".join(f"{column} = ?" for column in updates)
            values: list[Any] = list(updates.values())
            values.append(subscription_id)

            cursor = connection.execute(
                f"UPDATE watch_subscriptions SET {assignments} "
                f"WHERE subscription_id = ? RETURNING *",
                values,
            )
            updated = cursor.fetchone()
            connection.commit()
            record = self._from_row(updated)
            if record is None:
                raise WatchSubscriptionNotFoundError(
                    f"Watch subscription {subscription_id} not found"
                )
            return record

    async def delete_subscription(self, subscription_id: str) -> bool:
        """Delete a subscription, returning ``True`` when a row was removed."""
        return await self._run(self._delete_subscription_sync, subscription_id)

    def _delete_subscription_sync(self, subscription_id: str) -> bool:
        """Synchronously delete a subscription and report whether it existed."""
        with self._lock:
            connection = self._writer_connection()
            cursor = connection.execute(
                "DELETE FROM watch_subscriptions WHERE subscription_id = ?",
                (subscription_id,),
            )
            connection.commit()
            return int(cursor.rowcount) > 0
