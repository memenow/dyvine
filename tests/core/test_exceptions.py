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
    err = DyvineError("boom", error_code="E001", details={"x": 1})
    assert err.message == "boom"
    assert err.error_code == "E001"
    assert err.details == {"x": 1}
    assert str(err) == "boom"


def test_dyvine_error_default_error_code_is_class_name() -> None:
    err = DyvineError("msg")
    assert err.error_code == "DyvineError"


def test_dyvine_error_default_details_is_empty_dict() -> None:
    err = DyvineError("msg")
    assert err.details == {}


def test_dyvine_error_is_exception() -> None:
    assert issubclass(DyvineError, Exception)


def test_not_found_error_inherits_dyvine_error() -> None:
    err = NotFoundError("nf")
    assert isinstance(err, DyvineError)


def test_user_not_found_error_inherits_not_found_error() -> None:
    err = UserNotFoundError("u")
    assert isinstance(err, NotFoundError)
    assert isinstance(err, DyvineError)


def test_post_not_found_error_inherits_not_found_error() -> None:
    err = PostNotFoundError("p")
    assert isinstance(err, NotFoundError)


def test_livestream_not_found_error_inherits_not_found_error() -> None:
    err = LivestreamNotFoundError("l")
    assert isinstance(err, NotFoundError)


def test_service_error_inherits_dyvine_error() -> None:
    err = ServiceError("s")
    assert isinstance(err, DyvineError)
    assert not isinstance(err, NotFoundError)


def test_download_error_inherits_service_error() -> None:
    err = DownloadError("d")
    assert isinstance(err, ServiceError)


def test_storage_error_inherits_service_error() -> None:
    err = StorageError("st")
    assert isinstance(err, ServiceError)


def test_validation_error_inherits_dyvine_error() -> None:
    err = ValidationError("v")
    assert isinstance(err, DyvineError)
    assert not isinstance(err, ServiceError)


def test_authentication_error_inherits_dyvine_error() -> None:
    err = AuthenticationError("a")
    assert isinstance(err, DyvineError)


def test_rate_limit_error_inherits_dyvine_error() -> None:
    err = RateLimitError("r")
    assert isinstance(err, DyvineError)
