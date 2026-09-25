"""Shared schema for asynchronous operation tracking.

Defines the canonical `OperationStatus` enum (``pending`` /
``running`` / ``completed`` / ``partial`` / ``failed``) and the
`OperationResponse` Pydantic model that every async-download tool
returns. Per-domain response classes
(`schemas.users.DownloadResponse`, `schemas.livestreams.LiveStreamDownloadResponse`,
`schemas.posts.BulkDownloadResponse`) reuse these primitives so SDK
clients can branch on the same vocabulary regardless of operation
type.

Two backward-compatibility aliases are kept inside `OperationResponse`:
``task_id`` mirrors ``operation_id`` and ``downloaded_items`` mirrors
``completed_items``. They populate automatically via the post-init
validator, so callers should treat the canonical fields as
authoritative.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class OperationStatus(StrEnum):
    """Canonical operation status values shared across the API surface.

    Centralising the vocabulary avoids the historical drift between
    ``OperationResponse.status`` (free-form string) and
    ``BulkDownloadResponse.status`` (own ``DownloadStatus`` enum).
    Persistence layers and services both reuse these literals so callers
    can branch on the same set of values regardless of the operation
    type.
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class OperationResponse(BaseModel):
    """Response model for asynchronous operation state."""

    operation_id: str = Field(..., description="Unique operation identifier")
    task_id: str | None = Field(
        None,
        description=(
            "Deprecated alias for operation_id retained for backward compatibility"
        ),
    )
    operation_type: str = Field(..., description="Logical operation type")
    subject_id: str = Field(
        ..., description="Domain identifier the operation belongs to"
    )
    status: OperationStatus = Field(
        ...,
        description="Operation status (see ``OperationStatus`` for the enum members)",
    )
    message: str = Field(..., description="Human-readable status message")
    progress: float | None = Field(
        None, ge=0.0, le=100.0, description="Progress percentage (0-100)"
    )
    total_items: int | None = Field(
        None, description="Total work items in the operation"
    )
    completed_items: int | None = Field(
        None, description="Number of completed work items"
    )
    downloaded_items: int | None = Field(
        None,
        description=(
            "Deprecated alias for completed_items retained for backward compatibility"
        ),
    )
    download_path: str | None = Field(
        None,
        description=(
            "Path to the downloaded artefact, expressed relative to the "
            "configured download root. Internal absolute paths are never "
            "exposed."
        ),
    )
    error: str | None = Field(None, description="Terminal error message, when failed")
    created_at: datetime = Field(..., description="ISO 8601 creation timestamp")
    updated_at: datetime = Field(..., description="ISO 8601 last update timestamp")

    @field_validator("download_path")
    @classmethod
    def _path_must_be_relative(cls, value: str | None) -> str | None:
        """Reject absolute or escaping paths: the field is root-relative."""
        if value is None:
            return None
        if not value or value.startswith("/") or ".." in PurePosixPath(value).parts:
            raise ValueError("download_path must be relative to the download root")
        return value

    @model_validator(mode="after")
    def populate_aliases(self) -> OperationResponse:
        """Populate legacy compatibility aliases from the canonical fields.

        Aliases are tolerated spellings of one value: when both sides
        are supplied they must agree, otherwise every downstream
        consumer would fork onto a different truth.
        """
        if self.task_id is None:
            self.task_id = self.operation_id
        elif self.task_id != self.operation_id:
            raise ValueError("task_id must equal operation_id")
        if self.downloaded_items is None:
            self.downloaded_items = self.completed_items
        elif self.downloaded_items != self.completed_items:
            raise ValueError("downloaded_items must equal completed_items")
        return self

    model_config = ConfigDict()
