"""FastAPI router module for post-related endpoints.

This module provides endpoints for:
- Retrieving post details
- Listing user posts with pagination
- Bulk downloading user posts

Dependencies:
    - PostService: Core service for post operations
    - DouyinHandler: Handles Douyin API interactions
    - Settings: Configuration management

Configuration:
    The DouyinHandler is configured with the following settings:
    - Custom headers and proxies
    - Download paths and options
    - Rate limiting and retry logic
    - Content type filters

Error Responses:
    - 404: Post or user not found
    - 500: Download or server errors
"""

from typing import List
from fastapi import APIRouter, Depends, HTTPException, Query, Path
from fastapi.responses import JSONResponse

from ..core.logging import ContextLogger
from ..services.posts import (
    PostService,
    PostNotFoundError,
    UserNotFoundError,
    DownloadError
)
from ..schemas.posts import PostDetail, BulkDownloadResponse
from ..core.settings import settings
from f2.apps.douyin.handler import DouyinHandler

logger = ContextLogger(__name__)

router = APIRouter(prefix="/posts", tags=["posts"])

async def get_post_service() -> PostService:
    """Creates and configures a PostService instance.

    Configuration parameters:
        - headers: Custom headers for API requests
        - proxies: Network proxy settings
        - cookie: Authentication cookie
        - path: Download directory
        - max_retries: Maximum retry attempts
        - timeout: Request timeout in seconds
        - chunk_size: Download chunk size
        - max_tasks: Concurrent download limit

    Creates a DouyinHandler with the current settings and initializes a
    PostService instance. Uses environment configuration for setup.

    Returns:
        A configured PostService instance ready for use.
    """
    handler_kwargs = {
        "headers": settings.douyin_headers,
        "proxies": settings.douyin_proxies,
        "mode": "all",
        "cookie": settings.douyin_cookie,
        "path": "downloads",
        "max_retries": 5,
        "timeout": 30,
        "chunk_size": 1024 * 1024,
        "max_tasks": 3,
        "folderize": True,
        "download_image": True,
        "download_video": True,
        "download_live": True,
        "download_collection": True,
        "download_story": True,
        "naming": "{create}_{desc}",
        "page_counts": 100
    }
    handler = DouyinHandler(handler_kwargs)
    return PostService(handler)

@router.get(
    "/{post_id}",
    response_model=PostDetail,
    summary="Get post details",
    description="Retrieves detailed information about a specific Douyin post"
)
async def get_post(
    post_id: str = Path(..., description="The unique identifier of the post"),
    service: PostService = Depends(get_post_service)
) -> PostDetail:
    """Retrieves detailed information about a specific post.

    Args:
        post_id: The unique identifier of the post to retrieve.
        service: An instance of PostService for handling the request.

    Returns:
        Detailed information about the requested post.

    Raises:
        HTTPException(404): If the post is not found.
        HTTPException(500): If an unexpected error occurs.
    """
    try:
        logger.info("Processing get_post request", extra={"post_id": post_id})
        return await service.get_post_detail(post_id)
        
    except PostNotFoundError as e:
        logger.warning("Post not found", extra={"post_id": post_id})
        raise HTTPException(status_code=404, detail=str(e))
        
    except Exception as e:
        logger.exception(
            "Error processing get_post request",
            extra={"post_id": post_id}
        )
        raise HTTPException(status_code=500, detail=str(e))

@router.get(
    "/users/{user_id}/posts",
    response_model=List[PostDetail],
    summary="List user posts",
    description="Retrieves a paginated list of posts from a specific user"
)
async def list_user_posts(
    user_id: str = Path(..., description="The unique identifier of the user"),
    max_cursor: int = Query(0, description="Pagination cursor for fetching next page"),
    count: int = Query(20, ge=1, le=100, description="Number of posts per page (1-100)"),
    service: PostService = Depends(get_post_service)
) -> List[PostDetail]:
    """Lists posts from a specific user with pagination.

    Args:
        user_id: The unique identifier of the user.
        max_cursor: Cursor for pagination, 0 for first page.
        count: Number of posts to return per page (1-100).
        service: An instance of PostService for handling the request.

    Returns:
        A list of posts from the specified user.

    Raises:
        HTTPException(404): If the user is not found.
        HTTPException(500): If an unexpected error occurs.
    """
    try:
        logger.info(
            "Processing list_user_posts request",
            extra={
                "user_id": user_id,
                "max_cursor": max_cursor,
                "count": count
            }
        )
        return await service.get_user_posts(user_id, max_cursor, count)
        
    except UserNotFoundError as e:
        logger.warning("User not found", extra={"user_id": user_id})
        raise HTTPException(status_code=404, detail=str(e))
        
    except Exception as e:
        logger.exception(
            "Error processing list_user_posts request",
            extra={"user_id": user_id}
        )
        raise HTTPException(status_code=500, detail=str(e))

@router.post(
    "/users/{user_id}/posts:download",
    response_model=BulkDownloadResponse,
    summary="Download user posts",
    description="Downloads all available posts from a specific user"
)
async def download_user_posts(
    user_id: str = Path(..., description="The unique identifier of the user"),
    max_cursor: int = Query(0, description="Starting pagination cursor"),
    service: PostService = Depends(get_post_service)
) -> BulkDownloadResponse:
    """Downloads all available posts from a user.

    Args:
        user_id: The unique identifier of the user.
        max_cursor: Starting point for pagination, 0 for beginning.
        service: An instance of PostService for handling the request.

    Returns:
        A response containing download operation results.

    Raises:
        HTTPException(404): If the user is not found.
        HTTPException(500): If the download fails or an unexpected error occurs.
    """
    try:
        logger.info(
            "Processing download_user_posts request",
            extra={
                "user_id": user_id,
                "max_cursor": max_cursor
            }
        )
        return await service.download_all_user_posts(user_id, max_cursor)
        
    except UserNotFoundError as e:
        logger.warning("User not found", extra={"user_id": user_id})
        raise HTTPException(status_code=404, detail=str(e))
        
    except DownloadError as e:
        logger.error(
            "Download failed",
            extra={"user_id": user_id, "error": str(e)}
        )
        raise HTTPException(status_code=500, detail=str(e))
        
    except Exception as e:
        logger.exception(
            "Error processing download_user_posts request",
            extra={"user_id": user_id}
        )
        raise HTTPException(status_code=500, detail=str(e))
