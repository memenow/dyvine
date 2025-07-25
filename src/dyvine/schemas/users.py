"""User-related schema definitions.

This module defines Pydantic models for user data validation and serialization.
"""

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class UserDownloadRequest(BaseModel):
    """Schema for user download request."""

    user_id: str = Field(..., description="Douyin user ID")
    include_posts: bool = Field(True, description="Whether to include user posts")
    include_likes: bool = Field(False, description="Whether to include liked posts")
    max_items: Optional[int] = Field(None, description="Maximum items to download")
    model_config = ConfigDict()


class UserResponse(BaseModel):
    """Schema for user information response."""

    user_id: str = Field(..., description="Douyin user ID")
    nickname: str = Field(..., description="User nickname")
    avatar_url: str = Field(..., description="User avatar URL")
    signature: Optional[str] = Field(None, description="User bio/signature")
    following_count: int = Field(..., description="Number of users following")
    follower_count: int = Field(..., description="Number of followers")
    total_favorited: int = Field(..., description="Total likes received")
    is_living: bool = Field(
        False, description="Whether user is currently live streaming"
    )
    room_id: Optional[int] = Field(None, description="Live room ID if streaming")
    model_config = ConfigDict()


class DownloadResponse(BaseModel):
    """Schema for download operation response."""

    task_id: str = Field(..., description="Unique task identifier")
    status: str = Field(
        ..., description="Task status (pending/running/completed/failed)"
    )
    message: str = Field(..., description="Status message")
    progress: Optional[float] = Field(None, description="Download progress (0-100)")
    total_items: Optional[int] = Field(None, description="Total items to download")
    downloaded_items: Optional[int] = Field(
        None, description="Number of items downloaded"
    )
    error: Optional[str] = Field(None, description="Error message if failed")
    model_config = ConfigDict()
