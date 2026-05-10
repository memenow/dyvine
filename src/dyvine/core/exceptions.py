"""Domain exceptions surfaced through the public API.

Every service-layer failure raises a subclass of `DyvineError`. The
`@handle_errors` decorator and the global `dyvine_error_handler` map
each subclass to an HTTP status code:

- `NotFoundError` (and friends: `UserNotFoundError`, `PostNotFoundError`,
  `LivestreamNotFoundError`, `OperationNotFoundError`) -> `404`.
- `AuthenticationError` -> `401`.
- `RateLimitError` -> `429`.
- `ValidationError` -> `422`.
- `ServiceError` (and subclasses: `LivestreamError`, `DownloadError`,
  `StorageError`) -> `500`.

Routers can override per-handler mappings (for example, the livestream
router maps `LivestreamError` to `404` because "user not currently
streaming" is a not-found condition rather than a service fault).
"""

from typing import Any


class DyvineError(Exception):
    """Base exception for all Dyvine errors."""

    def __init__(
        self,
        message: str,
        error_code: str | None = None,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.error_code = error_code or self.__class__.__name__
        self.details = details or {}


class NotFoundError(DyvineError):
    """Base exception for resource not found errors."""

    pass


class UserNotFoundError(NotFoundError):
    """User not found."""

    pass


class PostNotFoundError(NotFoundError):
    """Post not found."""

    pass


class LivestreamNotFoundError(NotFoundError):
    """Livestream not found."""

    pass


class OperationNotFoundError(NotFoundError):
    """Operation record not found in the persistent store."""

    pass


class ServiceError(DyvineError):
    """Base exception for service-level errors."""

    pass


class LivestreamError(ServiceError):
    """Livestream-specific service error."""

    pass


class DownloadError(ServiceError):
    """Download operation failed."""

    pass


class StorageError(ServiceError):
    """Storage operation failed."""

    pass


class ValidationError(DyvineError):
    """Request validation failed."""

    pass


class AuthenticationError(DyvineError):
    """Authentication failed."""

    pass


class RateLimitError(DyvineError):
    """Rate limit exceeded."""

    pass
