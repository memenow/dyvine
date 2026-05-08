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
    Every endpoint requires the ``X-API-Key`` header to match
    ``settings.security.api_key``. The dependency is mounted at the
    router level so individual handlers cannot bypass it.

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

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query

from ..core.decorators import handle_errors
from ..core.dependencies import get_user_service, require_api_key
from ..core.logging import ContextLogger
from ..schemas.users import DownloadResponse, UserResponse
from ..services.users import UserService

# Create router with comprehensive error response documentation
router = APIRouter(
    prefix="/users",
    tags=["users"],
    dependencies=[Depends(require_api_key)],
    responses={
        401: {"description": "Missing or invalid API key"},
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

_USER_ID_PATTERN = r"^[A-Za-z0-9_\-]{6,128}$"
_OPERATION_ID_PATTERN = r"^[A-Za-z0-9_\-]{8,128}$"


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
@handle_errors(logger=logger)
async def get_user(
    user_id: Annotated[
        str,
        Path(
            ...,
            pattern=_USER_ID_PATTERN,
            description="The unique identifier of the user",
        ),
    ],
    service: Annotated[UserService, Depends(get_user_service)],
) -> UserResponse:
    """Retrieves information about a specific Douyin user."""
    logger.info("Processing get_user request", extra={"user_id": user_id})
    async with logger.track_time("get_user"):
        return await service.get_user_info(user_id)


@router.post(
    "/{user_id}/content:download",
    status_code=202,
    response_model=DownloadResponse,
    summary="Download user content",
    description="Initiates a download task for a user's content with specified options",
)
@handle_errors(logger=logger)
async def download_user_content(
    service: Annotated[UserService, Depends(get_user_service)],
    user_id: Annotated[
        str,
        Path(
            ...,
            pattern=_USER_ID_PATTERN,
            description="The unique identifier of the user",
        ),
    ],
    include_posts: bool = Query(True, description="Whether to download user's posts"),
    include_likes: bool = Query(
        False, description="Whether to download user's liked posts"
    ),
    max_items: int | None = Query(
        None, description="Maximum number of items to download (None for all)", gt=0
    ),
) -> DownloadResponse:
    """Initiates a download task for a user's content."""
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


@router.get(
    "/operations/{operation_id}",
    response_model=DownloadResponse,
    summary="Get operation status",
    description="Retrieves the current status of a long-running download operation",
)
@handle_errors(logger=logger)
async def get_operation(
    service: Annotated[UserService, Depends(get_user_service)],
    operation_id: Annotated[
        str,
        Path(
            ...,
            pattern=_OPERATION_ID_PATTERN,
            description="The unique identifier of the download operation",
        ),
    ],
) -> DownloadResponse:
    """Retrieves the status of a long-running download operation."""
    logger.info(
        "Processing get_operation request", extra={"operation_id": operation_id}
    )
    async with logger.track_time("get_operation"):
        return await service.get_download_status(operation_id)
