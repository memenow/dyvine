"""FastAPI router module for user-related endpoints.

This module provides endpoints for:
- Retrieving user information
- Downloading user content (posts and liked videos)
- Checking download operation status

Dependencies:
    - UserService: Core service for user operations
    - ContextLogger: Logging utility
    - Pydantic models for request/response validation

Example:
    ```python
    from fastapi import FastAPI
    from .routers import users

    app = FastAPI()
    app.include_router(users.router)
    ```

Error Responses:
    - 404 Not Found: When user or operation is not found
    - 500 Internal Error: For unexpected server errors
"""

import logging
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends, Path, Query, Request
from ..services.users import (
    UserService,
    UserServiceError,
    UserNotFoundError,
    DownloadError
)
from ..schemas.users import (
    UserResponse,
    DownloadResponse,
    UserDownloadRequest
)
from ..core.logging import ContextLogger
from ..schemas.users import UserResponse

router = APIRouter(prefix="/users", tags=["users"])
logger = ContextLogger(logging.getLogger(__name__))

def get_user_service(request: Request) -> UserService:
    """Retrieves the global UserService instance.

    Uses the global UserService instance from the application state.

    Args:
        request: The incoming request object, used to access app state.

    Returns:
        UserService: The global UserService instance.
    """
    return request.app.state.user_service

@router.get(
    "/{user_id}",
    response_model=UserResponse,
    responses={
        404: {"description": "User not found"},
        500: {"description": "Internal server error"}
    },
    summary="Get user information",
    description="Retrieves detailed information about a specific Douyin user"
)
async def get_user(
    user_id: str = Path(..., description="The unique identifier of the user"),
    service: UserService = Depends(get_user_service)
) -> UserResponse:
    """Retrieves information about a specific Douyin user.

    Args:
        user_id: The unique identifier of the user to retrieve.
        service: An instance of UserService for handling the request.

    Returns:
        UserResponse: Detailed information about the requested user.

    Raises:
        HTTPException: If the user is not found or an unexpected error occurs.
    """
    try:
        logger.info("Processing get_user request", extra={"user_id": user_id})
        async with logger.track_time("get_user"):
            return await service.get_user_info(user_id)

    except UserNotFoundError as e:
        logger.warning("User not found", extra={"user_id": user_id})
        raise HTTPException(status_code=404, detail=str(e))

    except Exception as e:
        logger.exception(
            "Error processing get_user request",
            extra={"user_id": user_id}
        )
        raise HTTPException(status_code=500, detail=str(e))

@router.post(
    "/{user_id}/content:download",
    response_model=DownloadResponse,
    summary="Download user content",
    description="Initiates a download task for a user's content with specified options"
)
async def download_user_content(
    user_id: str = Path(..., description="The unique identifier of the user"),
    include_posts: bool = Query(True, description="Whether to download user's posts"),
    include_likes: bool = Query(False, description="Whether to download user's liked posts"),
    max_items: Optional[int] = Query(
        None,
        description="Maximum number of items to download (None for all)",
        gt=0
    ),
    service: UserService = Depends(get_user_service)
) -> DownloadResponse:
    """Initiates a download task for a user's content.

    Args:
        user_id: The unique identifier of the user.
        include_posts: Whether to download user's posts.
        include_likes: Whether to download user's liked posts.
        max_items: Maximum number of items to download, or None for all.
        service: An instance of UserService for handling the request.

    Returns:
        DownloadResponse: Download task information including task ID and initial status.

    Raises:
        HTTPException: If the user is not found or an unexpected error occurs.
    """
    try:
        logger.info(
            "Processing download_user_content request",
            extra={
                "user_id": user_id,
                "include_posts": include_posts,
                "include_likes": include_likes,
                "max_items": max_items
            }
        )
        async with logger.track_time("download_user_content"):
            return await service.start_download(
                user_id,
                include_posts=include_posts,
                include_likes=include_likes,
                max_items=max_items
            )
            
    except UserNotFoundError as e:
        logger.warning("User not found", extra={"user_id": user_id})
        raise HTTPException(status_code=404, detail=str(e))
        
    except Exception as e:
        logger.exception(
            "Error processing download_user_content request",
            extra={"user_id": user_id}
        )
        raise HTTPException(status_code=500, detail=str(e))

@router.get(
    "/operations/{operation_id}",
    response_model=DownloadResponse,
    summary="Get operation status",
    description="Retrieves the current status of a long-running download operation"
)
async def get_operation(
    operation_id: str = Path(..., description="The unique identifier of the download operation"),
    service: UserService = Depends(get_user_service)
) -> DownloadResponse:
    """Retrieves the status of a long-running download operation.

    Args:
        operation_id: The unique identifier of the operation to check.
        service: An instance of UserService for handling the request.

    Returns:
        DownloadResponse: Current status of the download operation including progress.

    Raises:
        HTTPException: If the operation is not found or an unexpected error occurs.
    """
    try:
        logger.info(
            "Processing get_operation request",
            extra={"operation_id": operation_id}
        )
        async with logger.track_time("get_operation"):
            return await service.get_download_status(operation_id)
            
    except DownloadError as e:
        logger.warning(
            "Operation not found",
            extra={"operation_id": operation_id}
        )
        raise HTTPException(status_code=404, detail=str(e))
        
    except Exception as e:
        logger.exception(
            "Error processing get_operation request",
            extra={"operation_id": operation_id}
        )
        raise HTTPException(status_code=500, detail=str(e))
