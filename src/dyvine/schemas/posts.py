"""Schema definitions for Douyin post data models.

This module defines Pydantic models and enums for:
- Post content types
- Download operation statuses
- Response models for bulk operations
- Request/response models for post operations

All models include proper type hints and validation.
"""

from enum import Enum
from typing import Dict, Optional, List
from pydantic import BaseModel, Field, HttpUrl

class PostType(str, Enum):
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

class DownloadStatus(str, Enum):
    """Enumeration of possible download operation statuses.
    
    Attributes:
        SUCCESS: All content downloaded successfully
        PARTIAL_SUCCESS: Some content downloaded successfully
        FAILED: No content downloaded successfully
    """
    SUCCESS = "success"
    PARTIAL_SUCCESS = "partial_success"
    FAILED = "failed"

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
    video_info: Optional[VideoInfo] = Field(None, description="Video information")
    images: Optional[List[ImageInfo]] = Field(None, description="List of image information")
    statistics: Dict[str, int] = Field(
        default_factory=dict,
        description="Post engagement statistics"
    )

class BulkDownloadResponse(BaseModel):
    """Response model for bulk download operations.
    
    Attributes:
        sec_user_id: Target user's identifier
        download_path: Local path where content was saved
        total_posts: Total number of posts available
        downloaded_count: Count of downloads by post type
        total_downloaded: Total number of successful downloads
        status: Overall download operation status
        message: Human-readable status message
        error_details: Details of any errors encountered
    """
    sec_user_id: str = Field(..., description="Target user's identifier")
    download_path: str = Field(..., description="Local path where content was saved")
    total_posts: int = Field(..., description="Total number of posts available")
    downloaded_count: Dict[PostType, int] = Field(
        default_factory=lambda: {
            PostType.VIDEO: 0,
            PostType.IMAGES: 0,
            PostType.MIXED: 0,
            PostType.LIVE: 0,
            PostType.COLLECTION: 0,
            PostType.STORY: 0,
            PostType.UNKNOWN: 0
        },
        description="Count of downloads by post type"
    )
    total_downloaded: int = Field(
        default=0,
        description="Total number of successful downloads"
    )
    status: DownloadStatus = Field(..., description="Overall download operation status")
    message: Optional[str] = Field(None, description="Human-readable status message")
    error_details: Optional[str] = Field(None, description="Details of any errors encountered")

    class Config:
        """Pydantic model configuration."""
        json_schema_extra = {
            "example": {
                "sec_user_id": "user123",
                "download_path": "/downloads/user123",
                "total_posts": 100,
                "downloaded_count": {
                    "video": 50,
                    "images": 30,
                    "mixed": 10,
                    "live": 5,
                    "collection": 3,
                    "story": 2,
                    "unknown": 0
                },
                "total_downloaded": 100,
                "status": "success",
                "message": "All posts downloaded successfully",
                "error_details": None
            }
        }
