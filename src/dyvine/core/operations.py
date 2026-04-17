"""Persistent operation tracking for asynchronous workflows.

This module provides a lightweight SQLite-backed store for asynchronous
operation metadata. It is intended for API workflows that return immediately
and complete in the background, such as content downloads.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .exceptions import DownloadError
from .settings import settings


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
    """SQLite-backed persistence for asynchronous operation state."""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = Path(db_path or settings.operation_db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._lock, closing(self._connect()) as connection:
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
            connection.commit()

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
            download_path=str(row["download_path"]) if row["download_path"] else None,
            error=str(row["error"]) if row["error"] else None,
            metadata=json.loads(str(row["metadata"])) if row["metadata"] else {},
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def create_operation(
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

    def get_operation(self, operation_id: str) -> OperationRecord:
        """Fetch a single operation or raise ``DownloadError``."""
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        operation = self._from_row(row)
        if operation is None:
            raise DownloadError(f"Operation {operation_id} not found")
        return operation

    def update_operation(self, operation_id: str, **fields: Any) -> OperationRecord:
        """Update selected fields on an operation and return the new state."""
        current = self.get_operation(operation_id)
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

        return self.get_operation(operation_id)

    def mark_incomplete_operations_failed(self) -> int:
        """Mark stale pending/running operations as interrupted.

        Returns:
            Number of operations that were updated.
        """
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
