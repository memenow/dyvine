"""FastAPI router module for livestream-related endpoints.

This module provides API endpoints for:
- Downloading live streams from Douyin users
- Checking download operation status
- Managing stream downloads

The router uses FastAPI's dependency injection for service management
and includes comprehensive error handling and logging.
"""


from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Path

from ..core.exceptions import UserNotFoundError
from ..core.logging import ContextLogger
from ..schemas.livestreams import (
    LiveStreamDownloadResponse,
    LiveStreamURLDownloadRequest,
)
from ..services.livestreams import (
    DownloadError,
    LivestreamError,
    LivestreamService,
)

router = APIRouter(prefix="/livestreams", tags=["livestreams"])
logger = ContextLogger(__name__)

@router.post(
    "/users/{user_id}/stream:download",
    response_model=LiveStreamDownloadResponse,
    responses={
        404: {"description": "User not found or no active livestream"},
        500: {"description": "Download failed"}
    }
)
async def download_livestream(
    service: Annotated[LivestreamService, Depends(LivestreamService)],
    user_id: str = Path(..., description="The unique identifier of the user"),
    output_path: str | None = Body(None, embed=True)
) -> LiveStreamDownloadResponse:
    """Downloads an active livestream from a specific user.

    Args:
        user_id: The unique identifier of the user whose stream to download.
        output_path: Optional custom path where the stream should be saved.
        service: Injected LiveStreamService instance.

    Returns:
        LiveStreamDownloadResponse: Contains download status and path information.

    Raises:
        HTTPException: If user not found (404), livestream not found (404) or
            download fails (500).
    """
    try:
        logger.info(
            "Processing download_livestream request",
            extra={
                "user_id": user_id,
                "output_path": output_path
            }
        )

        async with logger.track_time("download_livestream"):
            status, path = await service.download_stream(
                url=f"https://www.douyin.com/user/{user_id}",
                output_path=output_path
            )

            return LiveStreamDownloadResponse(
                status=status,
                download_path=path
            )

    except DownloadError as e:
        logger.error(
            "Download failed",
            extra={
                "user_id": user_id,
                "error": str(e)
            }
        )
        raise HTTPException(status_code=500, detail=str(e)) from e

    except UserNotFoundError as e:
        logger.warning("User not found", extra={"user_id": user_id})
        raise HTTPException(status_code=404, detail=str(e)) from e

    except LivestreamError as e:
        logger.warning(
            "Livestream error",
            extra={
                "user_id": user_id,
                "error": str(e)
            }
        )
        raise HTTPException(status_code=404, detail=str(e)) from e

    except Exception as e:
        logger.exception(
            "Error processing download_livestream request",
            extra={"user_id": user_id}
        )
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.post(
    "/stream:download",
    response_model=LiveStreamDownloadResponse,
    responses={
        404: {"description": "Livestream not found"},
        500: {"description": "Download failed"}
    }
)
async def download_livestream_url(
    request: LiveStreamURLDownloadRequest,
    service: Annotated[LivestreamService, Depends(LivestreamService)]
) -> LiveStreamDownloadResponse:
    """Downloads a livestream from a direct URL.

    Args:
        request: The download request containing the livestream URL.
        service: Injected LiveStreamService instance.

    Returns:
        LiveStreamDownloadResponse: Contains download status and path information.

    Raises:
        HTTPException: If livestream not found (404) or download fails (500).
    """
    try:
        logger.info(
            "Processing download_livestream_url request",
            extra={"url": request.url}
        )

        async with logger.track_time("download_livestream_url"):
            status, path = await service.download_stream(
                url=request.url,
                output_path=request.output_path
            )

            return LiveStreamDownloadResponse(
                status=status,
                download_path=path
            )

    except DownloadError as e:
        logger.error(
            "Download failed",
            extra={
                "url": request.url,
                "error": str(e)
            }
        )
        raise HTTPException(status_code=500, detail=str(e)) from e

    except LivestreamError as e:
        logger.warning(
            "Livestream error",
            extra={
                "url": request.url,
                "error": str(e)
            }
        )
        raise HTTPException(status_code=404, detail=str(e)) from e

    except Exception as e:
        logger.exception(
            "Error processing download_livestream_url request",
            extra={"url": request.url}
        )
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.get(
    "/operations/{operation_id}",
    response_model=LiveStreamDownloadResponse
)
async def get_download_status(
    service: Annotated[LivestreamService, Depends(LivestreamService)],
    operation_id: str = Path(
        ..., description="The unique identifier of the download operation"
    )
) -> LiveStreamDownloadResponse:
    """Retrieves the status of a livestream download operation.

    Args:
        operation_id: The unique identifier of the operation to check.
        service: Injected LiveStreamService instance.

    Returns:
        LiveStreamDownloadResponse: Contains operation status and download path.

    Raises:
        HTTPException: If operation not found (404) or status check fails (500).
    """
    try:
        logger.info(
            "Processing get_download_status request",
            extra={"operation_id": operation_id}
        )

        async with logger.track_time("get_download_status"):
            try:
                result = await service.get_download_status(operation_id)
                logger.info("get_download_status completed")
                return LiveStreamDownloadResponse(
                    status="success",
                    download_path=result
                )
            except NotImplementedError as e:
                raise DownloadError("Operation not found") from e

    except DownloadError as e:
        logger.warning(
            "Operation not found",
            extra={"operation_id": operation_id}
        )
        raise HTTPException(status_code=404, detail=str(e)) from e

    except Exception as e:
        logger.exception(
            "Error processing get_download_status request",
            extra={"operation_id": operation_id}
        )
        raise HTTPException(status_code=500, detail=str(e)) from e
