"""Tests for the custom exception hierarchy."""

from __future__ import annotations

from dyvine.core.exceptions import (
    AuthenticationError,
    DownloadError,
    DyvineError,
    LivestreamNotFoundError,
    NotFoundError,
    PostNotFoundError,
    RateLimitError,
    ServiceError,
    StorageError,
    UserNotFoundError,
    ValidationError,
)


def test_dyvine_error_stores_message_code_details() -> None:
    """Verify dyvine error stores message code details."""
    err = DyvineError("boom", error_code="E001", details={"x": 1})
    assert err.message == "boom"
    assert err.error_code == "E001"
    assert err.details == {"x": 1}
    assert str(err) == "boom"


def test_dyvine_error_default_error_code_is_class_name() -> None:
    """Verify dyvine error default error code is class name."""
    err = DyvineError("msg")
    assert err.error_code == "DyvineError"


def test_dyvine_error_default_details_is_empty_dict() -> None:
    """Verify dyvine error default details is empty dict."""
    err = DyvineError("msg")
    assert err.details == {}


def test_dyvine_error_is_exception() -> None:
    """Verify dyvine error is exception."""
    assert issubclass(DyvineError, Exception)


def test_not_found_error_inherits_dyvine_error() -> None:
    """Verify not found error inherits dyvine error."""
    err = NotFoundError("nf")
    assert isinstance(err, DyvineError)


def test_user_not_found_error_inherits_not_found_error() -> None:
    """Verify user not found error inherits not found error."""
    err = UserNotFoundError("u")
    assert isinstance(err, NotFoundError)
    assert isinstance(err, DyvineError)


def test_post_not_found_error_inherits_not_found_error() -> None:
    """Verify post not found error inherits not found error."""
    err = PostNotFoundError("p")
    assert isinstance(err, NotFoundError)


def test_livestream_not_found_error_inherits_not_found_error() -> None:
    """Verify livestream not found error inherits not found error."""
    err = LivestreamNotFoundError("l")
    assert isinstance(err, NotFoundError)


def test_service_error_inherits_dyvine_error() -> None:
    """Verify service error inherits dyvine error."""
    err = ServiceError("s")
    assert isinstance(err, DyvineError)
    assert not isinstance(err, NotFoundError)


def test_download_error_inherits_service_error() -> None:
    """Verify download error inherits service error."""
    err = DownloadError("d")
    assert isinstance(err, ServiceError)


def test_storage_error_inherits_service_error() -> None:
    """Verify storage error inherits service error."""
    err = StorageError("st")
    assert isinstance(err, ServiceError)


def test_validation_error_inherits_dyvine_error() -> None:
    """Verify validation error inherits dyvine error."""
    err = ValidationError("v")
    assert isinstance(err, DyvineError)
    assert not isinstance(err, ServiceError)


def test_authentication_error_inherits_dyvine_error() -> None:
    """Verify authentication error inherits dyvine error."""
    err = AuthenticationError("a")
    assert isinstance(err, DyvineError)


def test_rate_limit_error_inherits_dyvine_error() -> None:
    """Verify rate limit error inherits dyvine error."""
    err = RateLimitError("r")
    assert isinstance(err, DyvineError)
