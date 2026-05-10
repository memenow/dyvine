"""Pydantic models for the user router.

Provides:

- `UserDownloadRequest` — typed body shape for client SDKs that want
  to construct download requests programmatically. The router itself
  takes the equivalent fields as `Query` parameters; the model is
  exported here so consumers can introspect the contract.
- `UserResponse` — `GET /users/{user_id}` payload. `room_data` is kept
  open as `dict[str, Any]` because Douyin evolves the underlying
  schema; treat it as opaque metadata.
- `DownloadResponse` — alias for `OperationResponse` used by the
  router so the user-download contract stays interchangeable with the
  generic operation envelope.
"""

from __future__ import annotations

from typing import Any

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator

from .operations import OperationResponse


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
    avatar_url: AnyHttpUrl | None = Field(
        None,
        description=(
            "User avatar URL. ``None`` when the upstream payload omits the "
            "avatar so clients receive a typed absence rather than an "
            "empty string."
        ),
    )
    signature: str | None = Field(None, description="User bio/signature")
    following_count: int = Field(..., description="Number of users following")
    follower_count: int = Field(..., description="Number of followers")
    total_favorited: int = Field(..., description="Total likes received")
    is_living: bool = Field(
        False, description="Whether user is currently live streaming"
    )
    room_id: int | None = Field(None, description="Live room ID if streaming")
    room_data: dict[str, Any] | None = Field(
        None,
        description=(
            "Decoded room_data payload for active livestreams. Free-form "
            "object kept open because Douyin evolves its schema; treat as "
            "opaque metadata rather than a typed contract."
        ),
    )
    model_config = ConfigDict()

    @field_validator("avatar_url", mode="before")
    @classmethod
    def _coerce_empty_avatar(cls, value: Any) -> Any:
        """Treat empty strings from the upstream payload as missing data."""
        if isinstance(value, str) and not value.strip():
            return None
        return value


class DownloadResponse(OperationResponse):
    """Backward-compatible alias for user download operations."""
