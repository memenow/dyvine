"""Post-related API endpoints for Douyin content management.

This module provides RESTful API endpoints for managing Douyin posts including:
- Retrieving individual post details and metadata
- Listing posts from specific users with pagination
- Bulk downloading of user posts with progress tracking
- Error handling for various post-related operations

The endpoints support various post types including:
- Video posts with playback URLs and metadata
- Image gallery posts with multiple images
- Live stream recordings and metadata
- Story posts and collections

Authentication:
    All endpoints require valid Douyin cookies for authentication.
    The cookie should be configured in the application settings.

Rate Limiting:
    Endpoints are subject to API rate limiting to prevent abuse.
    Default limit is 10 requests per second per client.

Example Usage:
    Get post details:
        GET /api/v1/posts/7123456789012345678

    List user posts:
        GET /api/v1/posts/users/MS4wLjABAAAA.../posts?count=20&max_cursor=0

    Download user posts:
        POST /api/v1/posts/users/MS4wLjABAAAA.../posts:download
"""

from typing import Annotated

from f2.apps.douyin.handler import DouyinHandler  # type: ignore
from fastapi import APIRouter, Depends, HTTPException, Path, Query

from ..core.decorators import handle_errors
from ..core.dependencies import get_douyin_handler
from ..core.exceptions import DownloadError, UserNotFoundError
from ..core.logging import ContextLogger
from ..schemas.posts import BulkDownloadResponse, PostDetail
from ..services.posts import PostService

# Initialize logger for this module
logger = ContextLogger(__name__)

# Create router with posts prefix and OpenAPI tags
router = APIRouter(
    prefix="/posts",
    tags=["posts"],
    responses={
        404: {"description": "Post or user not found"},
        422: {"description": "Validation error"},
        500: {"description": "Internal server error"},
    },
)


async def get_post_service(
    douyin_handler: Annotated[DouyinHandler, Depends(get_douyin_handler)]
) -> PostService:
    """Create PostService instance with injected Douyin handler."""
    return PostService(douyin_handler)


@router.get(
    "/{post_id}",
    response_model=PostDetail,
    summary="Get detailed information about a specific post",
    description=(
        "Retrieves comprehensive details about a Douyin post including metadata, "
        "media URLs, and engagement statistics"
    ),
    response_description="Complete post information with media details and statistics",
)
@handle_errors(logger=logger)
async def get_post(
    service: Annotated[PostService, Depends(get_post_service)],
    post_id: str = Path(
        ...,
        description="Unique Douyin post identifier (aweme_id)",
        examples={
            "default": {
                "summary": "Sample post identifier",
                "value": "7123456789012345678",
            }
        },
    ),
) -> PostDetail:
    """Retrieve detailed information about a specific Douyin post.

    This endpoint fetches comprehensive information about a single Douyin post
    including metadata, media content URLs, user information, and engagement
    statistics like likes, comments, and shares.

    Args:
        post_id: The unique Douyin post identifier (aweme_id). This is typically
                a long numeric string that uniquely identifies the post.
        service: PostService instance for handling the request (dependency injected).

    Returns:
        PostDetail: Complete post information including:
            - Basic metadata (title, description, creation time)
            - Media content (video URLs, image URLs, thumbnails)
            - User information (author details)
            - Engagement statistics (likes, comments, shares)
            - Content classification and tags

    Raises:
        HTTPException:
            - 404: Post not found or inaccessible
            - 422: Invalid post ID format
            - 500: Internal server error during processing

    Example:
        ```bash
        curl -X GET "https://api.example.com/api/v1/posts/7123456789012345678"
        ```

        Response:
        ```json
        {
            "aweme_id": "7123456789012345678",
            "desc": "Amazing video content!",
            "create_time": 1678886400,
            "post_type": "video",
            "video_info": {
                "play_addr": "https://example.com/video.mp4",
                "duration": 60
            },
            "statistics": {
                "digg_count": 1000,
                "comment_count": 50
            }
        }
        ```
    """
    logger.info(
        "Fetching post details",
        extra={"post_id": post_id, "operation": "get_post_detail"},
    )
    return await service.get_post_detail(post_id)


@router.get(
    "/users/{user_id}/posts",
    response_model=list[PostDetail],
    summary="List posts from a specific user with pagination",
    description=(
        "Retrieves a paginated list of posts from a specific Douyin user, "
        "ordered by creation time"
    ),
    response_description="List of post details with pagination support",
)
async def list_user_posts(
    service: Annotated[PostService, Depends(get_post_service)],
    user_id: str = Path(
        ...,
        description="Unique Douyin user identifier (sec_user_id)",
        examples={
            "default": {
                "summary": "Sample sec_user_id",
                "value": "MS4wLjABAAAA-kxe2_w-i_5F_q_b_rX_vIDqfwyTNYvM-oDD_eRjQVc",
            }
        },
    ),
    max_cursor: int = Query(
        0,
        description=(
            "Pagination cursor for fetching next page. Use 0 for first page, "
            "then use the cursor from previous response"
        ),
        examples={
            "initial": {"summary": "First page", "value": 0},
            "subsequent": {"summary": "Subsequent page cursor", "value": 1678886400},
        },
    ),
    count: int = Query(
        20,
        ge=1,
        le=100,
        description="Number of posts to return per page. Must be between 1 and 100",
        examples={
            "default": {"summary": "Default page size", "value": 20},
            "max": {"summary": "Maximum per request", "value": 100},
        },
    ),
) -> list[PostDetail]:
    """Retrieve a paginated list of posts from a specific Douyin user.

    This endpoint fetches posts from a user's profile in reverse chronological
    order (newest first) with support for pagination. Each post includes complete
    metadata, media URLs, and engagement statistics.

    Args:
        user_id: The unique Douyin user identifier (sec_user_id). This is typically
                a long encoded string that uniquely identifies the user.
        max_cursor: Pagination cursor indicating where to start fetching results.
                   Use 0 for the first page, then use the cursor value returned
                   in the response for subsequent pages.
        count: Number of posts to return per page. Must be between 1 and 100.
              Larger values may increase response time and memory usage.
        service: PostService instance for handling the request (dependency injected).

    Returns:
        List[PostDetail]: Ordered list of post details including:
            - Post metadata (ID, description, creation time)
            - Media content (videos, images, thumbnails)
            - User information (author details)
            - Engagement metrics (likes, comments, shares)
            - Content type and classification

    Raises:
        HTTPException:
            - 404: User not found or profile is private/inaccessible
            - 422: Invalid user ID format or parameter validation error
            - 500: Internal server error during data fetching

    Example:
        ```bash
        # Get first page of posts
        curl -X GET "https://api.example.com/api/v1/posts/users/MS4wLjABAAAA.../posts?count=10&max_cursor=0"

        # Get next page using cursor from previous response
        curl -X GET "https://api.example.com/api/v1/posts/users/MS4wLjABAAAA.../posts?count=10&max_cursor=1678886400"
        ```

        Response:
        ```json
        [
            {
                "aweme_id": "7123456789012345678",
                "desc": "User's latest post",
                "create_time": 1678886400,
                "post_type": "video",
                "statistics": {
                    "digg_count": 1000,
                    "comment_count": 50
                }
            }
        ]
        ```

    Note:
        - Results are ordered by creation time (newest first)
        - Empty list is returned if no more posts are available
        - Some posts may be excluded if they're private or restricted
        - Rate limiting applies to prevent API abuse
    """
    try:
        logger.info(
            "Processing list_user_posts request",
            extra={"user_id": user_id, "max_cursor": max_cursor, "count": count},
        )
        return await service.get_user_posts(user_id, max_cursor, count)

    except UserNotFoundError as e:
        logger.warning("User not found", extra={"user_id": user_id})
        raise HTTPException(status_code=404, detail=str(e)) from e

    except Exception as e:
        logger.exception(
            "Error processing list_user_posts request", extra={"user_id": user_id}
        )
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post(
    "/users/{user_id}/posts:download",
    response_model=BulkDownloadResponse,
    summary="Download user posts",
    description="Downloads all available posts from a specific user",
)
async def download_user_posts(
    service: Annotated[PostService, Depends(get_post_service)],
    user_id: str = Path(..., description="The unique identifier of the user"),
    max_cursor: int = Query(0, description="Starting pagination cursor"),
) -> BulkDownloadResponse:
    """Downloads all available posts from a user.

    Args:
        user_id: The unique identifier of the user.
        max_cursor: Starting point for pagination, 0 for beginning.
        service: An instance of PostService for handling the request.

    Returns:
        BulkDownloadResponse: A response containing download operation results.

    Raises:
        HTTPException: If the user is not found, the download fails, or an
            unexpected error occurs.
    """
    try:
        logger.info(
            "Processing download_user_posts request",
            extra={"user_id": user_id, "max_cursor": max_cursor},
        )
        return await service.download_all_user_posts(user_id, max_cursor)

    except UserNotFoundError as e:
        logger.warning("User not found", extra={"user_id": user_id})
        raise HTTPException(status_code=404, detail=str(e)) from e

    except DownloadError as e:
        logger.error("Download failed", extra={"user_id": user_id, "error": str(e)})
        raise HTTPException(status_code=500, detail=str(e)) from e

    except Exception as e:
        logger.exception(
            "Error processing download_user_posts request", extra={"user_id": user_id}
        )
        raise HTTPException(status_code=500, detail=str(e)) from e
