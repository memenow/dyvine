"""FastAPI router module for livestream-related endpoints.

This module provides endpoints for:
- Downloading active user livestreams
- Checking livestream download status

Process Flow:
    1. Client requests livestream download
    2. Service validates user and stream availability
    3. Download process starts asynchronously
    4. Client can check download status with operation ID

Response Examples:
    Success:
        {
            "status": "success",
            "download_path": "/downloads/user123/live_20230901.mp4"
        }
        
    Error:
        {
            "detail": "User not found or no active livestream"
        }

Dependencies:
    - LivestreamService: Core service for livestream operations
    - ContextLogger: Logging utility
"""

from typing import Optional
from fastapi import APIRouter, HTTPException, Depends, Path, Body
from ..core.logging import ContextLogger
from ..schemas.livestreams import LiveStreamDownloadRequest, LiveStreamDownloadResponse
from ..services.livestreams import (
    LivestreamService,
    LivestreamError,
    UserNotFoundError,
    DownloadError
)

router = APIRouter(prefix="/livestreams", tags=["livestreams"])
logger = ContextLogger(__name__)

def get_livestream_service() -> LivestreamService:
    """Creates a configured LivestreamService instance.

    Returns:
        A configured LivestreamService instance ready for use.
    """
    return LivestreamService()

@router.post(
    "/users/{user_id}/stream:download",
    response_model=LiveStreamDownloadResponse,
    responses={
        404: {"description": "User not found or no active livestream"},
        500: {"description": "Download failed"}
    },
    summary="Download user livestream",
    description="""
    Initiates a download of a user's active livestream.
    The download runs asynchronously and can be monitored via the operations endpoint.
    """
)
async def download_livestream(
    user_id: str = Path(..., description="The unique identifier of the user"),
    output_path: Optional[str] = Body(
        None,
        description="Custom path for saving the downloaded stream"
    ),
    service: LivestreamService = Depends(get_livestream_service)
) -> LiveStreamDownloadResponse:
    """Downloads an active livestream from a specific user.

    Args:
        user_id: The unique identifier of the user.
        output_path: Optional custom path for saving the downloaded stream.
        service: An instance of LivestreamService for handling the request.

    Returns:
        Download status and path information for the livestream.

    Raises:
        HTTPException(404): If the user or livestream is not found.
        HTTPException(500): If the download fails or an unexpected error occurs.
    """
    try:
        logger.info(
            "Processing download_livestream request",
            extra={
                "user_id": user_id,
                "output_path": output_path
            }
        )
        with logger.track_time("download_livestream"):
            result = await service.download_stream(
                user_id=user_id,
                output_path=output_path
            )
            
            return LiveStreamDownloadResponse(
                status="success",
                download_path=result
            )
            
    except UserNotFoundError as e:
        logger.warning("User not found", extra={"user_id": user_id})
        raise HTTPException(status_code=404, detail=str(e))
        
    except LivestreamError as e:
        logger.warning(
            "Livestream error",
            extra={
                "user_id": user_id,
                "error": str(e)
            }
        )
        raise HTTPException(status_code=404, detail=str(e))
        
    except DownloadError as e:
        logger.error(
            "Download failed",
            extra={
                "user_id": user_id,
                "error": str(e)
            }
        )
        raise HTTPException(status_code=500, detail=str(e))
        
    except Exception as e:
        logger.exception(
            "Error processing download_livestream request",
            extra={"user_id": user_id}
        )
        raise HTTPException(status_code=500, detail=str(e))

@router.get(
    "/operations/{operation_id}",
    response_model=LiveStreamDownloadResponse,
    summary="Get download status",
    description="Retrieves the current status of a livestream download operation"
)
async def get_download_status(
    operation_id: str = Path(..., description="The unique identifier of the download operation"),
    service: LivestreamService = Depends(get_livestream_service)
) -> LiveStreamDownloadResponse:
    """Retrieves the status of a livestream download operation.

    Args:
        operation_id: The unique identifier of the operation to check.
        service: An instance of LivestreamService for handling the request.

    Returns:
        Current status of the download operation including progress.

    Raises:
        HTTPException(404): If the operation is not found.
        HTTPException(500): If an unexpected error occurs.
    """
    try:
        logger.info(
            "Processing get_download_status request",
            extra={"operation_id": operation_id}
        )
        with logger.track_time("get_download_status"):
            result = await service.get_download_status(operation_id)
            return LiveStreamDownloadResponse(
                status="success",
                download_path=result
            )
            
    except DownloadError as e:
        logger.warning(
            "Operation not found",
            extra={"operation_id": operation_id}
        )
        raise HTTPException(status_code=404, detail=str(e))
        
    except Exception as e:
        logger.exception(
            "Error processing get_download_status request",
            extra={"operation_id": operation_id}
        )
        raise HTTPException(status_code=500, detail=str(e))
