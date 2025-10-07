"""User-related schema definitions.

This module defines Pydantic models for user data validation and serialization.
"""

from pydantic import BaseModel, ConfigDict, Field


class UserDownloadRequest(BaseModel):
    """Schema for user download request."""

    user_id: str = Field(..., description="Douyin user ID")
    include_posts: bool = Field(True, description="Whether to include user posts")
    include_likes: bool = Field(False, description="Whether to include liked posts")
    max_items: int | None = Field(None, description="Maximum items to download")
    model_config = ConfigDict()


class UserResponse(BaseModel):
    """Schema for user information response."""

    user_id: str = Field(..., description="Douyin user ID")
    nickname: str = Field(..., description="User nickname")
    avatar_url: str = Field(..., description="User avatar URL")
    signature: str | None = Field(None, description="User bio/signature")
    following_count: int = Field(..., description="Number of users following")
    follower_count: int = Field(..., description="Number of followers")
    total_favorited: int = Field(..., description="Total likes received")
    is_living: bool = Field(
        False, description="Whether user is currently live streaming"
    )
    room_id: int | None = Field(None, description="Live room ID if streaming")
    room_data: str | None = Field(
        None, description="Raw room_data payload for active livestreams"
    )
    model_config = ConfigDict()


class DownloadResponse(BaseModel):
    """Schema for download operation response."""

    task_id: str = Field(..., description="Unique task identifier")
    status: str = Field(
        ..., description="Task status (pending/running/completed/failed)"
    )
    message: str = Field(..., description="Status message")
    progress: float | None = Field(None, description="Download progress (0-100)")
    total_items: int | None = Field(None, description="Total items to download")
    downloaded_items: int | None = Field(None, description="Number of items downloaded")
    error: str | None = Field(None, description="Error message if failed")
    model_config = ConfigDict()
