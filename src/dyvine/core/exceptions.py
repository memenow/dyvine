"""Unified exception hierarchy for Dyvine."""

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


class ServiceError(DyvineError):
    """Base exception for service-level errors."""

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
