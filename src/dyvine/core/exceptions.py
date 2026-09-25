"""Domain exceptions raised by the engine and surfaced through tools.

Every service-layer failure raises a subclass of `DyvineError` with a
stable `error_code` (the class name) and optional structured `details`.
Tool handlers let these propagate: the hermes registry renders them as
tool errors, so messages must stay human-readable and secret-free.

Families: `NotFoundError` (missing rows/upstream objects),
`ValidationError` (bad input: paths, arguments), `RateLimitError`
(exhausted guardrails such as the subscription cap), and
`ServiceError` (upstream/dependency failures, with domain subclasses).
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
        """Initialize the error with a message, code, and details.

        Args:
            message: Human-readable failure message.
            error_code: Optional stable error code. Defaults to the class name.
            details: Optional structured detail fields for API responses.
        """
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


class WatchSubscriptionNotFoundError(NotFoundError):
    """Watch subscription not found in the persistent store."""

    pass


class QueueEntryNotFoundError(NotFoundError):
    """Download-queue entry not found in the persistent store."""

    pass


class SendStatusNotFoundError(NotFoundError):
    """Send-status row not found in the persistent store."""

    pass


class SeedAccountNotFoundError(NotFoundError):
    """Seed account not found in the persistent store."""

    pass


class UserProfileNotFoundError(NotFoundError):
    """Cached user profile not found in the persistent store."""

    pass


class DeliveryRoundNotFoundError(NotFoundError):
    """Delivery round not found in the persistent store."""

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


class DeliveryError(ServiceError):
    """Raised when message/file delivery fails.

    ``reason`` is one of ``too_large`` (over the channel upload cap),
    ``empty`` (zero-byte payload), ``failed`` (terminal channel error),
    or ``retryable`` (transient error worth one more attempt).
    """

    def __init__(
        self,
        message: str,
        *,
        reason: str = "failed",
        error_code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Attach a machine-readable ``reason`` to the message.

        ``error_code``/``details`` pass through to :class:`DyvineError`
        so callers catching the base type keep working.
        """
        super().__init__(message, error_code=error_code, details=details)
        self.reason = reason


class WatchDuplicateError(ServiceError):
    """A watch subscription for the subject already exists."""

    pass


class ValidationError(DyvineError):
    """Request validation failed."""

    pass


class RateLimitError(DyvineError):
    """Rate limit exceeded."""

    pass
