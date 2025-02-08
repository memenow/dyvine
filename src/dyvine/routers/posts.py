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
import logging
from fastapi import APIRouter, Depends, HTTPException, Query, Path, Request
from fastapi.responses import JSONResponse

from ..core.logging import ContextLogger
from ..services.posts import (
    PostService,
    PostNotFoundError,
    UserNotFoundError,
    DownloadError
)
from ..schemas.posts import PostDetail, BulkDownloadResponse

logger = ContextLogger(logging.getLogger(__name__))

router = APIRouter(prefix="/posts", tags=["posts"])

async def get_post_service(request: Request) -> PostService:
    """Retrieves the global DouyinHandler instance and creates a PostService.

    Uses the global DouyinHandler instance from the application state to avoid
    creating a new handler for each request.

    Args:
        request: The incoming request object, used to access app state.

    Returns:
        PostService: A PostService instance using the global DouyinHandler.
    """
    handler = request.app.state.douyin_handler
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
        PostDetail: Detailed information about the requested post.

    Raises:
        HTTPException: If the post is not found or an unexpected error occurs.
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
        List[PostDetail]: A list of posts from the specified user.

    Raises:
        HTTPException: If the user is not found or an unexpected error occurs.
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
        BulkDownloadResponse: A response containing download operation results.

    Raises:
        HTTPException: If the user is not found, the download fails, or an unexpected error occurs.
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
