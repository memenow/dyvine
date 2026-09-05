"""Service-facing persistence records.

These dataclasses are the stable contract between the repository layer
(``dyvine.db``) and the domain services. They intentionally mirror the
HTTP response shapes (ISO-8601 timestamp strings, plain dicts) so
services never touch ORM objects or driver rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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
