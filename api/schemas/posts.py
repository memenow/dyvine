"""Schema definitions for Douyin post data models.

This module defines:
    - Post type enumerations
    - Download status enumerations
    - Response models for bulk operations

Typical usage example:
    response = BulkDownloadResponse(
        sec_user_id="user123",
        download_path="/downloads",
        total_posts=100,
        status=DownloadStatus.SUCCESS
    )
"""

from pydantic import BaseModel
from typing import Optional, List, Dict
from enum import Enum

class PostType(str, Enum):
    """Enumeration of possible post content types."""
    VIDEO = "video"
    IMAGES = "images"
    MIXED = "mixed"
    UNKNOWN = "unknown"

class DownloadStatus(str, Enum):
    """Enumeration of possible download operation statuses."""
    SUCCESS = "success"
    PARTIAL_SUCCESS = "partial_success"
    FAILED = "failed"

class BulkDownloadResponse(BaseModel):
    """Response model for bulk download operations.
    
    Attributes:
        sec_user_id: Target user's identifier.
        download_path: Local path where content was saved.
        total_posts: Total number of posts available.
        downloaded_count: Count of downloads by post type.
        total_downloaded: Total number of successful downloads.
        status: Overall download operation status.
        message: Human-readable status message.
        error_details: Details of any errors encountered.
    """
    sec_user_id: str
    download_path: str
    total_posts: int
    downloaded_count: Dict[PostType, int] = {
        PostType.VIDEO: 0,
        PostType.IMAGES: 0,
        PostType.MIXED: 0,
        PostType.UNKNOWN: 0
    }
    total_downloaded: int = 0
    status: DownloadStatus
    message: Optional[str] = None
    error_details: Optional[str] = None
