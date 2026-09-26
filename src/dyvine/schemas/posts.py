"""Pydantic models for posts.

Provides:

- `PostType` — `StrEnum` describing the materialised content type for
  a Douyin post (`video`, `images`, `mixed`, `live`, `collection`,
  `story`, `unknown`).
- `DownloadStatus` — alias for the canonical `OperationStatus` enum
  defined in `schemas.operations` so post-bulk responses share the
  same vocabulary as every other async operation.
- `PostBase` / `PostDetail` / `VideoInfo` / `ImageInfo` — record shapes
  returned by `GET /posts/{post_id}` and `GET /posts/users/{user_id}/posts`.
- `ListPostsResponse` — Google AIP-158 style page wrapper with
  `next_page_token` and best-effort `total_size`.
- `BulkDownloadResponse` — used by `POST /posts/users/{user_id}/posts:download`
  and `GET /posts/operations/{operation_id}`. Carries `total_posts`,
  `failed_count`, and a per-`PostType` counter map so polling clients
  see consistent totals while work is still running.
"""

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

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
# service layers stored these values as raw strings before the
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
    create_time: int = Field(
        ..., ge=0, description="Post creation timestamp (unix epoch seconds)"
    )


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
    duration: int = Field(..., ge=0, description="Video duration in seconds")
    ratio: str = Field(..., description="Video aspect ratio")
    width: int = Field(..., ge=0, description="Video width in pixels")
    height: int = Field(..., ge=0, description="Video height in pixels")


class ImageInfo(BaseModel):
    """Image information model.

    Attributes:
        URL: Image URL
        width: Image width in pixels
        height: Image height in pixels

    """

    url: HttpUrl = Field(..., description="Image URL")
    width: int = Field(..., ge=0, description="Image width in pixels")
    height: int = Field(..., ge=0, description="Image height in pixels")


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

    @model_validator(mode="after")
    def _payload_must_match_type(self) -> Self:
        """Require the media payload the declared type promises.

        A ``VIDEO`` without playable video (or ``IMAGES`` without
        images) is corrupt upstream data; failing here beats a ``None``
        dereference three layers deeper in SDK branching code.
        """
        if (
            self.post_type in (PostType.VIDEO, PostType.MIXED)
            and self.video_info is None
        ):
            raise ValueError(f"{self.post_type.value} post requires video_info")
        if self.post_type in (PostType.IMAGES, PostType.MIXED) and not self.images:
            raise ValueError(f"{self.post_type.value} post requires images")
        return self


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
            "Opaque cursor for the next page. ``None`` when the feed is exhausted."
        ),
    )
    total_size: int | None = Field(
        None,
        ge=0,
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
    total_posts: int = Field(
        default=0, ge=0, description="Total number of posts available"
    )
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
        ge=0,
        description="Posts encountered during the run that failed to download",
    )
    total_downloaded: int = Field(
        default=0, ge=0, description="Total number of successful downloads"
    )
    status: OperationStatus = Field(
        ..., description="Overall download operation status"
    )
    message: str | None = Field(None, description="Human-readable status message")
    error_details: str | None = Field(
        None, description="Details of any errors encountered"
    )

    model_config = ConfigDict()

    @model_validator(mode="after")
    def _counters_must_reconcile(self) -> Self:
        """Pin the counter invariant: total equals the by-type sum.

        Every writer maintains ``completed_items`` and the
        ``downloaded_count`` breakdown in the same atomic row write, so
        a mismatch is always a bug, never skew. No upper bound against
        ``total_posts`` is enforced: the run can legitimately outgrow
        the start-time profile count (new posts published mid-run).
        """
        by_type = self.downloaded_count or {}
        if any(count < 0 for count in by_type.values()):
            raise ValueError("downloaded_count values must be non-negative")
        if self.total_downloaded != sum(by_type.values()):
            raise ValueError("total_downloaded must equal the downloaded_count sum")
        return self
