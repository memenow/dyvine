"""Route-level error handling decorators.

Provides ``handle_errors``, a parameterized decorator that maps Dyvine
exception types to HTTP status codes so individual route handlers don't
need repetitive try/except blocks.
"""

import inspect
from collections.abc import AsyncIterator, Callable
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
    """Decorator for handling exceptions in route handlers.

    Wraps an async route function so that ``DyvineError`` subclasses are
    automatically translated to ``HTTPException`` responses using the
    appropriate status code, while unexpected exceptions become 500s.

    Async generator functions (``async def`` with ``yield``) are supported
    transparently: the wrapper iterates the underlying generator and
    re-yields each value, translating any exception raised mid-iteration
    through the same mapping used for regular coroutines. Without this
    branch, ``await func(...)`` would either raise ``TypeError`` on the
    returned ``async_generator`` object or — for older Python stacks —
    silently return the generator before the ``try`` block could catch
    anything raised during iteration.

    Args:
        error_mapping: Optional overrides merged into the default
            exception-to-status-code map.
        logger: When provided, errors are logged before raising.

    Returns:
        A decorator that wraps an async callable (coroutine or async
        generator).
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

    def _translate(exc: Exception) -> HTTPException:
        """Convert a service-layer exception into an HTTPException.

        Centralizes the mapping + logging so both the coroutine and the
        async-generator wrappers share a single implementation.
        """
        if isinstance(exc, DyvineError):
            status_code = next(
                (
                    code
                    for exc_type, code in default_mapping.items()
                    if isinstance(exc, exc_type)
                ),
                400,
            )
            if logger:
                logger.error(
                    f"{type(exc).__name__}: {str(exc)}",
                    extra={"error_code": exc.error_code, "details": exc.details},
                )
            return HTTPException(
                status_code=status_code,
                detail={
                    "error": str(exc),
                    "error_code": exc.error_code,
                    "details": exc.details,
                },
            )
        if logger:
            logger.exception(f"Unexpected error: {str(exc)}")
        return HTTPException(status_code=500, detail="Internal server error")

    def decorator(func: Callable) -> Callable:
        if inspect.isasyncgenfunction(func):

            @wraps(func)
            async def async_gen_wrapper(
                *args: Any, **kwargs: Any
            ) -> AsyncIterator[Any]:
                agen = func(*args, **kwargs)
                try:
                    async for item in agen:
                        yield item
                except HTTPException:
                    # An inner ``handle_errors`` (or the handler itself)
                    # already translated the error; don't wrap twice.
                    raise
                except Exception as exc:
                    raise _translate(exc) from exc
                finally:
                    aclose = getattr(agen, "aclose", None)
                    if callable(aclose):
                        await aclose()

            return async_gen_wrapper

        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await func(*args, **kwargs)
            except HTTPException:
                raise
            except Exception as exc:
                raise _translate(exc) from exc

        return wrapper

    return decorator
