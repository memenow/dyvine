"""Persistent operation tracking for asynchronous workflows.

This module provides a lightweight SQLite-backed store for asynchronous
operation metadata. It is intended for API workflows that return immediately
and complete in the background, such as content downloads.

All public methods are ``async`` and delegate the synchronous ``sqlite3``
calls to a dedicated ``concurrent.futures.Executor`` (owned by
``ServiceContainer``) so they never block the event loop. Per-page progress
updates on long-running downloads used to stall other requests on a
single-worker uvicorn deployment; moving the IO off-loop restores fairness.
"""

from __future__ import annotations

import asyncio
import functools
import json
import sqlite3
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import Executor
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from .exceptions import DownloadError
from .settings import settings

_R = TypeVar("_R")


@dataclass(slots=True)
class OperationRecord:
    """Structured representation of an asynchronous operation."""

    operation_id: str
    operation_type: str
    subject_id: str
    status: str
    message: str
    progress: float | None
    total_items: int | None
    completed_items: int | None
    download_path: str | None
    error: str | None
    metadata: dict[str, Any]
    created_at: str
    updated_at: str

    def to_response(self) -> dict[str, Any]:
        """Convert the record into the public API response shape."""
        return {
            "operation_id": self.operation_id,
            "task_id": self.operation_id,
            "operation_type": self.operation_type,
            "subject_id": self.subject_id,
            "status": self.status,
            "message": self.message,
            "progress": self.progress,
            "total_items": self.total_items,
            "completed_items": self.completed_items,
            "downloaded_items": self.completed_items,
            "download_path": self.download_path,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class OperationStore:
    """SQLite-backed persistence for asynchronous operation state.

    Public methods are coroutines. They perform no sqlite IO on the calling
    thread: every database call is dispatched to a dedicated executor owned
    by the container so the event loop stays responsive during progress
    updates, healthchecks, and cross-request writes. The synchronous
    implementation is kept private (``_<name>_sync``) so it can be unit
    tested directly and reused from non-async bootstrapping paths
    (currently only ``__init__``).
    """

    def __init__(
        self,
        db_path: str | None = None,
        *,
        executor: Executor | None = None,
    ) -> None:
        self.db_path = Path(db_path or settings.operation_db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # ``threading.Lock`` because the sqlite worker pool hands work to
        # multiple threads; several worker threads can race for the same
        # connection attempt if we only relied on asyncio coordination.
        self._lock = threading.Lock()
        self._executor: Executor | None = executor
        self._initialize()

    def set_executor(self, executor: Executor | None) -> None:
        """Attach a dedicated executor after construction.

        The container owns the sqlite executor lifecycle. Late binding lets
        the store bootstrap synchronously inside ``ServiceContainer`` (no
        running event loop yet) while still routing every later async call
        through the dedicated worker pool.
        """
        self._executor = executor

    def shutdown(self) -> None:
        """Release any per-store resources held outside the executor.

        The default implementation is a no-op; subsequent refactors add a
        per-thread reader connection pool that hooks in here to close its
        handles before the owning executor reaps its worker threads.
        """
        return None

    async def _run(self, func: Callable[..., _R], /, *args: Any, **kwargs: Any) -> _R:
        """Dispatch a blocking sqlite call to the configured executor.

        Falls back to the default asyncio executor when no dedicated pool is
        attached, which keeps tests that instantiate the store directly
        working without a container.
        """
        loop = asyncio.get_running_loop()
        result: _R = await loop.run_in_executor(
            self._executor, functools.partial(func, *args, **kwargs)
        )
        return result

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, check_same_thread=False)
        try:
            connection.row_factory = sqlite3.Row
            # synchronous is a per-connection PRAGMA, so reapply on every connect.
            connection.execute("PRAGMA synchronous=NORMAL;")
            connection.execute("PRAGMA busy_timeout=5000;")
        except Exception:
            # A post-open configuration failure (e.g. attempting to set
            # synchronous on a read-only WAL database) must not leak the
            # already-opened handle, or Python 3.13 will flag it as an
            # unclosed database on garbage collection.
            connection.close()
            raise
        return connection

    def _initialize(self) -> None:
        with self._lock, closing(self._connect()) as connection:
            # WAL is a database-level mode that persists across connections
            # and enables concurrent readers while a writer is active.
            connection.execute("PRAGMA journal_mode=WAL;")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY,
                    operation_type TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT NOT NULL,
                    progress REAL,
                    total_items INTEGER,
                    completed_items INTEGER,
                    download_path TEXT,
                    error TEXT,
                    metadata TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_operations_subject_type_updated
                ON operations (subject_id, operation_type, updated_at DESC)
                """
            )
            connection.commit()

    async def healthcheck(self) -> None:
        """Verify the backing store is reachable and writable.

        Raises:
            Exception: Propagates any sqlite3.Error / OSError from opening
                the connection or executing a trivial write-capable probe,
                so callers can treat failure as "not ready".
        """
        await self._run(self._healthcheck_sync)

    def _healthcheck_sync(self) -> None:
        # BEGIN IMMEDIATE acquires a RESERVED lock, which proves the database
        # file is writable without mutating any rows. The rollback lives in
        # ``finally`` so a failed BEGIN never leaves a pending transaction
        # on the connection -- Python 3.13 turns that into a ResourceWarning
        # at close time, which becomes an error under ``-W error``.
        with closing(self._connect()) as connection:
            try:
                connection.execute("SELECT 1").fetchone()
                connection.execute("BEGIN IMMEDIATE")
            finally:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _from_row(row: sqlite3.Row | None) -> OperationRecord | None:
        if row is None:
            return None
        return OperationRecord(
            operation_id=str(row["operation_id"]),
            operation_type=str(row["operation_type"]),
            subject_id=str(row["subject_id"]),
            status=str(row["status"]),
            message=str(row["message"]),
            progress=float(row["progress"]) if row["progress"] is not None else None,
            total_items=(
                int(row["total_items"]) if row["total_items"] is not None else None
            ),
            completed_items=(
                int(row["completed_items"])
                if row["completed_items"] is not None
                else None
            ),
            download_path=(
                str(row["download_path"]) if row["download_path"] is not None else None
            ),
            error=str(row["error"]) if row["error"] is not None else None,
            metadata=json.loads(str(row["metadata"])) if row["metadata"] else {},
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

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
        """Create and persist a new operation."""
        return await self._run(
            self._create_operation_sync,
            operation_type=operation_type,
            subject_id=subject_id,
            status=status,
            message=message,
            progress=progress,
            total_items=total_items,
            completed_items=completed_items,
            download_path=download_path,
            error=error,
            metadata=metadata,
            operation_id=operation_id,
        )

    def _create_operation_sync(
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
        created_at = self._now()
        operation = OperationRecord(
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
            metadata=metadata or {},
            created_at=created_at,
            updated_at=created_at,
        )
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO operations (
                    operation_id, operation_type, subject_id, status, message,
                    progress, total_items, completed_items, download_path, error,
                    metadata, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    operation.operation_id,
                    operation.operation_type,
                    operation.subject_id,
                    operation.status,
                    operation.message,
                    operation.progress,
                    operation.total_items,
                    operation.completed_items,
                    operation.download_path,
                    operation.error,
                    json.dumps(operation.metadata, sort_keys=True),
                    operation.created_at,
                    operation.updated_at,
                ),
            )
            connection.commit()
        return operation

    async def get_operation(self, operation_id: str) -> OperationRecord:
        """Fetch a single operation or raise ``DownloadError``."""
        return await self._run(self._get_operation_sync, operation_id)

    def _get_operation_sync(self, operation_id: str) -> OperationRecord:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        operation = self._from_row(row)
        if operation is None:
            raise DownloadError(f"Operation {operation_id} not found")
        return operation

    async def get_latest_operation_for_subject(
        self, subject_id: str, *, operation_type: str | None = None
    ) -> OperationRecord:
        """Fetch the most recently updated operation for a subject.

        Args:
            subject_id: Domain identifier associated with the operation.
            operation_type: Optional logical operation type filter.

        Returns:
            The latest matching operation.

        Raises:
            DownloadError: If no operation matches the subject identifier.
        """
        return await self._run(
            self._get_latest_operation_for_subject_sync,
            subject_id,
            operation_type=operation_type,
        )

    def _get_latest_operation_for_subject_sync(
        self, subject_id: str, *, operation_type: str | None = None
    ) -> OperationRecord:
        query = """
            SELECT * FROM operations
            WHERE subject_id = ?
        """
        params: list[str] = [subject_id]
        if operation_type is not None:
            query += " AND operation_type = ?"
            params.append(operation_type)
        query += " ORDER BY updated_at DESC, created_at DESC LIMIT 1"

        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(query, params).fetchone()
        operation = self._from_row(row)
        if operation is None:
            raise DownloadError(f"Operation {subject_id} not found")
        return operation

    async def update_operation(
        self, operation_id: str, **fields: Any
    ) -> OperationRecord:
        """Update selected fields on an operation and return the new state."""
        return await self._run(self._update_operation_sync, operation_id, **fields)

    def _update_operation_sync(
        self, operation_id: str, **fields: Any
    ) -> OperationRecord:
        current = self._get_operation_sync(operation_id)
        allowed_fields = {
            "status",
            "message",
            "progress",
            "total_items",
            "completed_items",
            "download_path",
            "error",
            "metadata",
        }
        updates = {key: value for key, value in fields.items() if key in allowed_fields}
        if not updates:
            return current

        updates["updated_at"] = self._now()
        if "metadata" not in updates:
            updates["metadata"] = current.metadata

        assignments = ", ".join(f"{column} = ?" for column in updates)
        values = [
            json.dumps(value, sort_keys=True) if key == "metadata" else value
            for key, value in updates.items()
        ]
        values.append(operation_id)

        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                f"UPDATE operations SET {assignments} WHERE operation_id = ?",
                values,
            )
            connection.commit()

        return self._get_operation_sync(operation_id)

    async def mark_incomplete_operations_failed(self) -> int:
        """Mark stale pending/running operations as interrupted.

        Returns:
            Number of operations that were updated.
        """
        return await self._run(self._mark_incomplete_operations_failed_sync)

    def _mark_incomplete_operations_failed_sync(self) -> int:
        updated_at = self._now()
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE operations
                SET status = ?,
                    message = ?,
                    error = ?,
                    updated_at = ?
                WHERE status IN (?, ?)
                """,
                (
                    "failed",
                    "Operation interrupted during process restart",
                    "Operation interrupted during process restart",
                    updated_at,
                    "pending",
                    "running",
                ),
            )
            connection.commit()
            return int(cursor.rowcount)
