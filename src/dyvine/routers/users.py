"""User-facing FastAPI router.

Endpoints exposed under ``/api/v1/users``:

- ``GET /{user_id}`` — fetch the public Douyin profile.
- ``POST /{user_id}/content:download`` — schedule an asynchronous
  bulk download of the user's posts and/or liked items. Returns
  ``202`` with an ``operation_id`` clients poll via the next endpoint.
- ``GET /operations/{operation_id}`` — return the current status of a
  scheduled download.

Authentication:
    The ``require_api_key`` dependency is attached at the router level,
    so every handler enforces ``X-API-Key`` validation when
    ``SECURITY_REQUIRE_API_KEY`` is ``true``. Set the variable to
    ``false`` only when the API is fronted by another authenticated
    layer.

Rate limiting is intentionally **not** enforced inside the application;
the ``API_RATE_LIMIT_PER_SECOND`` setting is reserved for an external
gateway / ingress to honour. Internally, the only concurrency control
comes from the dedupe lock inside ``LivestreamService`` and the bounded
thread-pool executors owned by ``ServiceContainer``.
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
                        "message": "User MS4wLjABAAAA... not found",
                        "error_code": "UserNotFoundError",
                        "status_code": 404,
                        "correlation_id": "550e8400-e29b-41d4-a716-446655440000",
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
                        "message": ("Path escapes the configured download root"),
                        "error_code": "ValidationError",
                        "status_code": 422,
                        "correlation_id": "550e8400-e29b-41d4-a716-446655440000",
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
                        "message": (
                            "Internal service error; see correlation_id in "
                            "server logs"
                        ),
                        "error_code": "ServiceError",
                        "status_code": 500,
                        "correlation_id": "550e8400-e29b-41d4-a716-446655440000",
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
