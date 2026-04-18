"""Schema definitions for asynchronous operation tracking."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    status: str = Field(
        ...,
        description=(
            "Operation status: pending | running | completed | partial | failed"
        ),
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
        description="Filesystem path to the downloaded artifact, when available",
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
