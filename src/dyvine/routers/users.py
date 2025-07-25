"""User management API endpoints for Douyin user operations.

This module provides comprehensive RESTful API endpoints for managing Douyin users:

Core Features:
- User profile information retrieval with detailed metadata
- Bulk content downloading (posts, liked videos, collections)
- Asynchronous download operations with progress tracking
- Operation status monitoring and result retrieval
- Error handling for various user-related scenarios

Supported User Operations:
- Profile data fetching (followers, following, statistics)
- Content enumeration with pagination support
- Batch download operations with configurable options
- Real-time operation status checking
- Download result management and cleanup

Authentication & Authorization:
    All endpoints require valid Douyin authentication cookies.
    Configure DOUYIN_COOKIE environment variable with session data.

Rate Limiting:
    User endpoints are subject to rate limiting:
    - Profile requests: 10 per minute per IP
    - Download operations: 2 concurrent per user
    - Status checks: 30 per minute per operation

Data Privacy:
    This API respects user privacy settings and only accesses
    publicly available content or content accessible with provided
    authentication credentials.

Example Usage:
    Get user profile:
        GET /api/v1/users/MS4wLjABAAAA-kxe2_w-i_5F_q_b_rX_vIDqfwyTNYvM-oDD_eRjQVc

    Download user content:
        POST /api/v1/users/MS4wLjABAAAA.../content:download
        {
            "include_posts": true,
            "include_likes": false,
            "max_items": 100
        }

    Check download status:
        GET /api/v1/users/operations/550e8400-e29b-41d4-a716-446655440000

Dependencies:
    - UserService: Core business logic for user operations
    - ContextLogger: Structured logging with correlation tracking
    - Pydantic models: Request/response validation and serialization
    - Dependency injection: Service lifecycle management
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from ..core.dependencies import get_user_service
from ..core.logging import ContextLogger
from ..schemas.users import DownloadResponse, UserResponse
from ..services.users import DownloadError, UserNotFoundError, UserService

# Create router with comprehensive error response documentation
router = APIRouter(
    prefix="/users",
    tags=["users"],
    responses={
        404: {
            "description": "User not found or inaccessible",
            "content": {
                "application/json": {
                    "example": {
                        "error": True,
                        "message": "User not found",
                        "error_code": "USER_NOT_FOUND",
                    }
                }
            },
        },
        422: {
            "description": "Validation error in request parameters",
            "content": {
                "application/json": {
                    "example": {
                        "error": True,
                        "message": "Invalid user ID format",
                        "error_code": "VALIDATION_ERROR",
                    }
                }
            },
        },
        500: {
            "description": "Internal server error",
            "content": {
                "application/json": {
                    "example": {
                        "error": True,
                        "message": "Internal server error occurred",
                        "error_code": "INTERNAL_SERVER_ERROR",
                    }
                }
            },
        },
    },
)

# Initialize structured logger for this module
logger = ContextLogger(__name__)


@router.get(
    "/{user_id}",
    response_model=UserResponse,
    responses={
        404: {"description": "User not found"},
        500: {"description": "Internal server error"},
    },
    summary="Get user information",
    description="Retrieves detailed information about a specific Douyin user",
)
async def get_user(
    user_id: str = Path(..., description="The unique identifier of the user"),
    service: UserService = Depends(get_user_service),
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
        raise HTTPException(status_code=404, detail=str(e)) from e

    except Exception as e:
        logger.exception(
            "Error processing get_user request", extra={"user_id": user_id}
        )
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post(
    "/{user_id}/content:download",
    response_model=DownloadResponse,
    summary="Download user content",
    description="Initiates a download task for a user's content with specified options",
)
async def download_user_content(
    user_id: str = Path(..., description="The unique identifier of the user"),
    include_posts: bool = Query(True, description="Whether to download user's posts"),
    include_likes: bool = Query(
        False, description="Whether to download user's liked posts"
    ),
    max_items: Optional[int] = Query(
        None, description="Maximum number of items to download (None for all)", gt=0
    ),
    service: UserService = Depends(get_user_service),
) -> DownloadResponse:
    """Initiates a download task for a user's content.

    Args:
        user_id: The unique identifier of the user.
        include_posts: Whether to download user's posts.
        include_likes: Whether to download user's liked posts.
        max_items: Maximum number of items to download, or None for all.
        service: An instance of UserService for handling the request.

    Returns:
        DownloadResponse: Download task information including task ID and
        initial status.

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
                "max_items": max_items,
            },
        )
        async with logger.track_time("download_user_content"):
            return await service.start_download(
                user_id,
                include_posts=include_posts,
                include_likes=include_likes,
                max_items=max_items,
            )

    except UserNotFoundError as e:
        logger.warning("User not found", extra={"user_id": user_id})
        raise HTTPException(status_code=404, detail=str(e)) from e

    except Exception as e:
        logger.exception(
            "Error processing download_user_content request",
            extra={"user_id": user_id},
        )
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get(
    "/operations/{operation_id}",
    response_model=DownloadResponse,
    summary="Get operation status",
    description="Retrieves the current status of a long-running download operation",
)
async def get_operation(
    operation_id: str = Path(
        ..., description="The unique identifier of the download operation"
    ),
    service: UserService = Depends(get_user_service),
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
            "Processing get_operation request", extra={"operation_id": operation_id}
        )
        async with logger.track_time("get_operation"):
            return await service.get_download_status(operation_id)

    except DownloadError as e:
        logger.warning("Operation not found", extra={"operation_id": operation_id})
        raise HTTPException(status_code=404, detail=str(e)) from e

    except Exception as e:
        logger.exception(
            "Error processing get_operation request",
            extra={"operation_id": operation_id},
        )
        raise HTTPException(status_code=500, detail=str(e)) from e
