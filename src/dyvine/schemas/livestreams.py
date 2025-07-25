"""Schema models for livestream operations.

This module defines Pydantic models for livestream-related operations,
including request and response models for downloads and status tracking.

Typical usage example:
    from .schemas.livestreams import LiveStreamDownloadRequest

    request = LiveStreamDownloadRequest(
        user_id="123456",
        output_path="/downloads/stream.mp4"
    )
"""


from pydantic import BaseModel, ConfigDict, Field


class LiveStreamDownloadRequest(BaseModel):
    """Request model for initiating a livestream download.

    Attributes:
        user_id: The unique identifier of the user whose stream to download.
        output_path: Optional custom path where the stream should be saved.
            If not provided, a default path will be used.
    """
    user_id: str = Field(
        ...,
        description="The unique identifier of the user whose stream to download"
    )
    output_path: str | None = Field(
        None,
        description="Optional custom path where the stream should be saved"
    )

class LiveStreamURLDownloadRequest(BaseModel):
    """Request model for initiating a livestream download via URL.

    Attributes:
        url: The livestream URL (user profile or direct room URL).
        output_path: Optional custom path where the stream should be saved.
            If not provided, a default path will be used.
    """
    url: str = Field(
        ...,
        description="The livestream URL (user profile or direct room URL)"
    )
    output_path: str | None = Field(
        None,
        description="Optional custom path where the stream should be saved"
    )

class LiveStreamDownloadResponse(BaseModel):
    """Response model for livestream download operations.

    Attributes:
        status: The status of the download operation ('success' or 'error').
        download_path: The path where the stream was saved (only present on success).
        error: Error message if the operation failed (only present on error).
    """
    status: str = Field(
        ...,
        description="Status of the download operation"
    )
    download_path: str | None = Field(
        None,
        description="Path where the stream was saved"
    )
    error: str | None = Field(
        None,
        description="Error message if the operation failed"
    )

    model_config = ConfigDict()
