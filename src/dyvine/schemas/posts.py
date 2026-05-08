"""Schema definitions for Douyin post data models.

This module defines Pydantic models and enums for:
- Post content types
- Download operation statuses
- Response models for bulk operations
- Request/response models for post operations

All models include proper type hints and validation.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from .operations import OperationStatus


class PostType(StrEnum):
    """Enumeration of possible post content types.

    Attributes:
        VIDEO: Single video content
        IMAGES: Image or multiple images
        MIXED: Both video and image content
        LIVE: Live streaming content
        COLLECTION: Collection of posts
        STORY: Story format content
        UNKNOWN: Unrecognized content type
    """

    VIDEO = "video"
    IMAGES = "images"
    MIXED = "mixed"
    LIVE = "live"
    COLLECTION = "collection"
    STORY = "story"
    UNKNOWN = "unknown"


# Backwards-compatible alias for the canonical operation status enum. The
# router/service layers stored these values as raw strings before the
# ``OperationStatus`` consolidation; keeping the alias avoids touching
# every import site while still routing through one source of truth.
DownloadStatus = OperationStatus


class PostBase(BaseModel):
    """Base model for post data.

    Attributes:
        aweme_id: Unique identifier for the post
        desc: Post description/caption
        create_time: Post creation timestamp
    """

    aweme_id: str = Field(..., description="Unique identifier for the post")
    desc: str = Field(default="", description="Post description/caption")
    create_time: int = Field(..., description="Post creation timestamp")


class VideoInfo(BaseModel):
    """Video information model.

    Attributes:
        play_addr: Video playback URL
        duration: Video duration in seconds
        ratio: Video aspect ratio
        width: Video width in pixels
        height: Video height in pixels
    """

    play_addr: HttpUrl = Field(..., description="Video playback URL")
    duration: int = Field(..., description="Video duration in seconds")
    ratio: str = Field(..., description="Video aspect ratio")
    width: int = Field(..., description="Video width in pixels")
    height: int = Field(..., description="Video height in pixels")


class ImageInfo(BaseModel):
    """Image information model.

    Attributes:
        url: Image URL
        width: Image width in pixels
        height: Image height in pixels
    """

    url: HttpUrl = Field(..., description="Image URL")
    width: int = Field(..., description="Image width in pixels")
    height: int = Field(..., description="Image height in pixels")


class PostDetail(PostBase):
    """Detailed post information model.

    Attributes:
        post_type: Type of post content
        video_info: Video information if applicable
        images: List of image information if applicable
        statistics: Post engagement statistics
    """

    post_type: PostType = Field(..., description="Type of post content")
    video_info: VideoInfo | None = Field(None, description="Video information")
    images: list[ImageInfo] | None = Field(
        None, description="List of image information"
    )
    statistics: dict[str, int] = Field(
        default_factory=dict, description="Post engagement statistics"
    )


class ListPostsResponse(BaseModel):
    """Paginated wrapper for ``GET /posts/users/{user_id}/posts``.

    The shape follows Google AIP-158: callers iterate by passing the
    opaque ``next_page_token`` back into the request, and stop when the
    field is ``None``. ``total_size`` is best-effort metadata sourced
    from the upstream profile and is not authoritative for hidden or
    geo-blocked posts.
    """

    posts: list[PostDetail] = Field(
        default_factory=list, description="Post records on this page"
    )
    next_page_token: str | None = Field(
        None,
        description=(
            "Opaque cursor for the next page. ``None`` when the feed is "
            "exhausted."
        ),
    )
    total_size: int | None = Field(
        None,
        description=(
            "Best-effort total number of posts available, when the upstream "
            "profile provides a count."
        ),
    )

    model_config = ConfigDict()


class BulkDownloadResponse(BaseModel):
    """Response model for bulk download operations.

    Attributes:
        operation_id: Identifier for tracking the asynchronous bulk download
        sec_user_id: Target user's identifier
        download_path: Local path where content was saved
        total_posts: Total number of posts available
        downloaded_count: Count of downloads by post type
        failed_count: Count of posts that failed to download
        total_downloaded: Total number of successful downloads
        status: Overall download operation status
        message: Human-readable status message
        error_details: Details of any errors encountered
    """

    operation_id: str = Field(..., description="Operation tracking identifier")
    sec_user_id: str = Field(..., description="Target user's identifier")
    download_path: str | None = Field(
        default=None,
        description=(
            "Path to the downloaded artefacts, expressed relative to the "
            "configured download root."
        ),
    )
    total_posts: int = Field(default=0, description="Total number of posts available")
    downloaded_count: dict[PostType, int] = Field(
        default_factory=lambda: {
            PostType.VIDEO: 0,
            PostType.IMAGES: 0,
            PostType.MIXED: 0,
            PostType.LIVE: 0,
            PostType.COLLECTION: 0,
            PostType.STORY: 0,
            PostType.UNKNOWN: 0,
        },
        description="Count of downloads by post type",
    )
    failed_count: int = Field(
        default=0,
        description="Posts encountered during the run that failed to download",
    )
    total_downloaded: int = Field(
        default=0, description="Total number of successful downloads"
    )
    status: OperationStatus = Field(
        ..., description="Overall download operation status"
    )
    message: str | None = Field(None, description="Human-readable status message")
    error_details: str | None = Field(
        None, description="Details of any errors encountered"
    )

    model_config = ConfigDict()
