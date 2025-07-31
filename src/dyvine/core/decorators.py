from collections.abc import Callable
from functools import wraps
from typing import Any

from fastapi import HTTPException

from .exceptions import (
    AuthenticationError,
    DyvineError,
    NotFoundError,
    RateLimitError,
    ServiceError,
    ValidationError,
)
from .logging import ContextLogger


def handle_errors(
    error_mapping: dict[type[Exception], int] | None = None,
    logger: ContextLogger | None = None,
) -> Callable:
    """
    Decorator for handling exceptions in route handlers.

    Args:
        error_mapping: Custom exception to status code mapping
        logger: Optional logger instance
    """
    default_mapping: dict[type[Exception], int] = {
        NotFoundError: 404,
        ValidationError: 400,
        AuthenticationError: 401,
        RateLimitError: 429,
        ServiceError: 500,
    }

    if error_mapping:
        default_mapping.update(error_mapping)

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await func(*args, **kwargs)
            except DyvineError as e:
                status_code = default_mapping.get(type(e), 400)
                if logger:
                    logger.error(
                        f"{type(e).__name__}: {str(e)}",
                        extra={"error_code": e.error_code, "details": e.details},
                    )
                raise HTTPException(
                    status_code=status_code,
                    detail={
                        "error": str(e),
                        "error_code": e.error_code,
                        "details": e.details,
                    },
                ) from e
            except Exception as e:
                if logger:
                    logger.exception(f"Unexpected error: {str(e)}")
                raise HTTPException(
                    status_code=500, detail="Internal server error"
                ) from e

        return wrapper

    return decorator
