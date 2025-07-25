"""Unified error handling for Dyvine API."""

import traceback
from typing import Any

from fastapi import Request, status
from fastapi.responses import JSONResponse

from .exceptions import DyvineError, NotFoundError, ServiceError
from .logging import ContextLogger
from dyvine.core.settings import settings

logger = ContextLogger(__name__)


class ErrorResponse:
    """Standardized error response structure."""

    @staticmethod
    def create_response(
        status_code: int,
        message: str,
        error_code: str | None = None,
        details: dict[str, Any] | None = None,
        correlation_id: str | None = None,
        include_traceback: bool = False,
        exception: Exception | None = None,
    ) -> JSONResponse:
        """Create standardized error response."""
        content = {
            "error": True,
            "message": message,
            "error_code": error_code or "UNKNOWN_ERROR",
            "status_code": status_code,
        }

        if details:
            content["details"] = details

        if correlation_id:
            content["correlation_id"] = correlation_id

        if include_traceback and exception:
            content["traceback"] = traceback.format_exception(
                type(exception), exception, exception.__traceback__
            )

        return JSONResponse(status_code=status_code, content=content)


async def dyvine_error_handler(request: Request, exc: DyvineError) -> JSONResponse:
    """Handle all Dyvine-specific errors in a unified way."""
    correlation_id = getattr(request.state, 'correlation_id', None)

    # Determine status code based on exception type
    if isinstance(exc, NotFoundError):
        status_code = status.HTTP_404_NOT_FOUND
        log_level = "warning"
    elif isinstance(exc, ServiceError):
        status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
        log_level = "error"
    else:
        status_code = status.HTTP_400_BAD_REQUEST
        log_level = "warning"

    # Log the error with appropriate level
    log_message = f"{exc.__class__.__name__}: {exc.message}"
    extra = {
        "error_code": exc.error_code,
        "correlation_id": correlation_id,
        "exception_type": exc.__class__.__name__,
    }

    if log_level == "error":
        logger.error(log_message, extra=extra, exc_info=True)
    else:
        logger.warning(log_message, extra=extra)

    return ErrorResponse.create_response(
        status_code=status_code,
        message=exc.message,
        error_code=exc.error_code,
        details=exc.details,
        correlation_id=correlation_id,
    )


async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handle unexpected exceptions."""
    correlation_id = getattr(request.state, 'correlation_id', None)

    logger.error(
        f"Unexpected error: {exc}",
        extra={
            "correlation_id": correlation_id,
            "exception_type": exc.__class__.__name__,
            "path": request.url.path,
            "method": request.method,
        },
        exc_info=True,
    )

    # Don't expose internal error details in production

    include_traceback = settings.debug

    return ErrorResponse.create_response(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        message="Internal server error occurred",
        error_code="INTERNAL_SERVER_ERROR",
        correlation_id=correlation_id,
        include_traceback=include_traceback,
        exception=exc if include_traceback else None,
    )


def register_error_handlers(app) -> None:
    """Register all error handlers with the FastAPI app."""

    # Handle all DyvineError subclasses with one handler
    app.add_exception_handler(DyvineError, dyvine_error_handler)

    # Handle unexpected exceptions
    app.add_exception_handler(Exception, generic_exception_handler)
