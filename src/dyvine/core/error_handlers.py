import traceback
from collections.abc import Mapping
from typing import Any

from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse

from .exceptions import (
    AuthenticationError,
    DyvineError,
    NotFoundError,
    RateLimitError,
    ServiceError,
    ValidationError,
)
from .logging import ContextLogger
from .settings import settings

logger = ContextLogger(__name__)

# Status-code mapping ordered from most-specific to least-specific so the
# subclass check below picks the tightest match. ``DyvineError`` is the
# implicit fallback (HTTP 400) and not listed here.
_DYVINE_STATUS_MAPPING: tuple[tuple[type[DyvineError], int], ...] = (
    (NotFoundError, status.HTTP_404_NOT_FOUND),
    (AuthenticationError, status.HTTP_401_UNAUTHORIZED),
    (RateLimitError, status.HTTP_429_TOO_MANY_REQUESTS),
    (ValidationError, status.HTTP_422_UNPROCESSABLE_CONTENT),
    (ServiceError, status.HTTP_500_INTERNAL_SERVER_ERROR),
)


class ErrorResponse:
    """Standardized error response structure."""

    @staticmethod
    def create_response(
        status_code: int,
        message: str,
        error_code: str | None = None,
        details: dict[str, Any] | None = None,
        correlation_id: str | None = None,
        headers: Mapping[str, str] | None = None,
        include_traceback: bool = False,
        exception: Exception | None = None,
    ) -> JSONResponse:
        """Create a standardized error JSON response.

        Args:
            status_code: HTTP status code for the response.
            message: Human-readable error message.
            error_code: Machine-readable error code (defaults to ``UNKNOWN_ERROR``).
            details: Optional extra context about the error.
            correlation_id: Request correlation ID for tracing.
            headers: Optional HTTP headers that must be preserved.
            include_traceback: Whether to include the Python traceback.
            exception: The exception instance (only used when
                *include_traceback* is True).

        Returns:
            A ``JSONResponse`` with the error payload and the given status code.
        """
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

        return JSONResponse(status_code=status_code, content=content, headers=headers)


async def dyvine_error_handler(request: Request, exc: DyvineError) -> JSONResponse:
    """Handle all ``DyvineError`` subclasses in a unified way.

    Maps exception types to HTTP status codes per ``_DYVINE_STATUS_MAPPING``,
    logs with the appropriate severity, and returns a structured
    :class:`ErrorResponse`.

    The 5xx branch never echoes the raw exception message back to the
    client because service-layer messages routinely embed internal
    paths, upstream API payloads, or storage keys. The full message is
    still logged for operators.
    """
    correlation_id = getattr(request.state, "correlation_id", None)

    status_code = status.HTTP_400_BAD_REQUEST
    for exc_type, mapped_code in _DYVINE_STATUS_MAPPING:
        if isinstance(exc, exc_type):
            status_code = mapped_code
            break

    extra = {
        "error_code": exc.error_code,
        "correlation_id": correlation_id,
        "exception_type": exc.__class__.__name__,
    }

    if status_code >= 500:
        logger.error(
            "%s: %s",
            exc.__class__.__name__,
            exc.message,
            extra=extra,
            exc_info=True,
        )
        # Surface only a generic message in the response body; the real
        # exception text remains in the logs above for operators. In
        # debug builds the original message is preserved so local
        # development still shows the underlying cause.
        client_message = (
            exc.message
            if settings.debug
            else "Internal service error; see correlation_id in server logs"
        )
        client_details: dict[str, Any] | None = exc.details if settings.debug else None
    else:
        logger.warning(
            "%s: %s",
            exc.__class__.__name__,
            exc.message,
            extra=extra,
        )
        client_message = exc.message
        client_details = exc.details

    return ErrorResponse.create_response(
        status_code=status_code,
        message=client_message,
        error_code=exc.error_code,
        details=client_details,
        correlation_id=correlation_id,
    )


async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handle unexpected (non-Dyvine) exceptions as 500 Internal Server Error.

    Logs the full traceback at error level. In debug mode the traceback
    is included in the response body; in production only a generic
    message is returned.
    """
    correlation_id = getattr(request.state, "correlation_id", None)

    logger.error(
        "Unexpected error: %s",
        exc,
        extra={
            "correlation_id": correlation_id,
            "exception_type": exc.__class__.__name__,
            "path": request.url.path,
            "method": request.method,
        },
        exc_info=True,
    )

    include_traceback = settings.debug

    return ErrorResponse.create_response(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        message="Internal server error occurred",
        error_code="INTERNAL_SERVER_ERROR",
        correlation_id=correlation_id,
        include_traceback=include_traceback,
        exception=exc if include_traceback else None,
    )


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Normalize ``HTTPException`` responses into the standard error envelope."""
    correlation_id = getattr(request.state, "correlation_id", None)
    detail = exc.detail
    if isinstance(detail, dict):
        message = str(detail.get("message") or detail.get("error") or exc.detail)
        error_code = str(detail.get("error_code") or f"HTTP_{exc.status_code}")
        details = detail.get("details")
    else:
        message = str(detail) if detail is not None else ""
        error_code = f"HTTP_{exc.status_code}"
        details = None

    return ErrorResponse.create_response(
        status_code=exc.status_code,
        message=message,
        error_code=error_code,
        details=details if isinstance(details, dict) else None,
        correlation_id=correlation_id,
        headers=exc.headers,
    )


def register_error_handlers(app: Any) -> None:
    """Register Dyvine and generic exception handlers with the FastAPI app.

    Args:
        app: The FastAPI application instance.
    """

    # Handle all DyvineError subclasses with one handler
    app.add_exception_handler(DyvineError, dyvine_error_handler)

    app.add_exception_handler(HTTPException, http_exception_handler)

    # Handle unexpected exceptions
    app.add_exception_handler(Exception, generic_exception_handler)
