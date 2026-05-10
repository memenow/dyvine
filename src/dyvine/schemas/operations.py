"""Shared schema for asynchronous operation tracking.

Defines the canonical `OperationStatus` enum (``pending`` /
``running`` / ``completed`` / ``partial`` / ``failed``) and the
`OperationResponse` Pydantic model that every async-download endpoint
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

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class OperationStatus(StrEnum):
    """Canonical operation status values shared across the API surface.

    Centralising the vocabulary avoids the historical drift between
    ``OperationResponse.status`` (free-form string) and
    ``BulkDownloadResponse.status`` (own ``DownloadStatus`` enum).
    Persistence layers and routers both reuse these literals so an SDK
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
    progress: float | None = Field(None, description="Progress percentage (0-100)")
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
    created_at: str = Field(..., description="ISO 8601 creation timestamp")
    updated_at: str = Field(..., description="ISO 8601 last update timestamp")

    @model_validator(mode="after")
    def populate_aliases(self) -> OperationResponse:
        """Populate legacy compatibility aliases from the canonical fields."""
        if self.task_id is None:
            self.task_id = self.operation_id
        if self.downloaded_items is None:
            self.downloaded_items = self.completed_items
        return self

    model_config = ConfigDict()
