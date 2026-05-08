"""FastAPI router module for livestream-related endpoints.

This module provides API endpoints for:
- Downloading live streams from Douyin users
- Checking download operation status
- Managing stream downloads

The router uses FastAPI's dependency injection for service management
and includes comprehensive error handling and logging.
"""

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Path, status

from ..core.decorators import handle_errors
from ..core.dependencies import get_livestream_service, require_api_key
from ..core.exceptions import LivestreamError
from ..core.logging import ContextLogger
from ..schemas.livestreams import (
    LiveStreamDownloadResponse,
    LiveStreamURLDownloadRequest,
)
from ..services.livestreams import LivestreamService

router = APIRouter(
    prefix="/livestreams",
    tags=["livestreams"],
    dependencies=[Depends(require_api_key)],
)
logger = ContextLogger(__name__)

_USER_ID_PATTERN = r"^[A-Za-z0-9_\-]{6,128}$"
_OPERATION_ID_PATTERN = r"^[A-Za-z0-9_\-]{8,128}$"

# ``LivestreamError`` is raised whenever the upstream room is offline,
# unreachable, or already being downloaded. The historical contract
# returns 404 for those cases (the resource is not "available"); the
# global ``ServiceError → 500`` mapping would otherwise reclassify a
# user-visible "not streaming" condition as an internal error.
# The annotation pins the dict to the broader ``type[Exception]`` key
# type the decorator declares so mypy does not reject this constant
# under invariant generic key types.
_LIVESTREAM_ERROR_MAPPING: dict[type[Exception], int] = {LivestreamError: 404}


@router.post(
    "/users/{user_id}/stream:download",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=LiveStreamDownloadResponse,
    responses={
        401: {"description": "Missing or invalid API key"},
        404: {"description": "User not found or no active livestream"},
        422: {"description": "Invalid user_id or output_path"},
        500: {"description": "Download failed"},
    },
)
@handle_errors(logger=logger, error_mapping=_LIVESTREAM_ERROR_MAPPING)
async def download_livestream(
    service: Annotated[LivestreamService, Depends(get_livestream_service)],
    user_id: str = Path(
        ...,
        pattern=_USER_ID_PATTERN,
        description="The unique identifier of the user",
    ),
    output_path: str | None = Body(None, embed=True),
) -> LiveStreamDownloadResponse:
    """Downloads an active livestream from a specific user."""
    logger.info(
        "Processing download_livestream request",
        extra={"user_id": user_id, "output_path": output_path},
    )

    async with logger.track_time("download_livestream"):
        return await service.download_stream(
            url=f"https://www.douyin.com/user/{user_id}", output_path=output_path
        )


@router.post(
    "/stream:download",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=LiveStreamDownloadResponse,
    responses={
        401: {"description": "Missing or invalid API key"},
        404: {"description": "Livestream not found"},
        422: {"description": "Invalid url or output_path"},
        500: {"description": "Download failed"},
    },
)
@handle_errors(logger=logger, error_mapping=_LIVESTREAM_ERROR_MAPPING)
async def download_livestream_url(
    request: LiveStreamURLDownloadRequest,
    service: Annotated[LivestreamService, Depends(get_livestream_service)],
) -> LiveStreamDownloadResponse:
    """Downloads a livestream from a direct URL."""
    logger.info(
        "Processing download_livestream_url request", extra={"url": str(request.url)}
    )

    async with logger.track_time("download_livestream_url"):
        return await service.download_stream(
            url=str(request.url), output_path=request.output_path
        )


@router.get(
    "/operations/{operation_id}",
    response_model=LiveStreamDownloadResponse,
    responses={
        401: {"description": "Missing or invalid API key"},
        404: {"description": "Operation not found"},
    },
)
@handle_errors(logger=logger)
async def get_download_status(
    service: Annotated[LivestreamService, Depends(get_livestream_service)],
    operation_id: str = Path(
        ...,
        pattern=_OPERATION_ID_PATTERN,
        description="The unique identifier of the download operation",
    ),
) -> LiveStreamDownloadResponse:
    """Retrieves the status of a livestream download operation."""
    logger.info(
        "Processing get_download_status request",
        extra={"operation_id": operation_id},
    )

    async with logger.track_time("get_download_status"):
        return await service.get_download_status(operation_id)
