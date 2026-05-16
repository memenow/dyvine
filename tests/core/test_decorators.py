"""Tests for error-handling decorators."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from dyvine.core.decorators import handle_errors
from dyvine.core.exceptions import (
    AuthenticationError,
    NotFoundError,
    RateLimitError,
    ServiceError,
    ValidationError,
)


@pytest.mark.asyncio
async def test_handle_errors_passes_through_on_success() -> None:
    """Verify handle errors passes through on success."""

    @handle_errors()
    async def ok() -> str:
        """Test helper for test_handle_errors_passes_through_on_success."""
        return "ok"

    assert await ok() == "ok"


@pytest.mark.asyncio
async def test_handle_errors_maps_not_found_to_404() -> None:
    """Verify handle errors maps not found to 404."""

    @handle_errors()
    async def fail() -> None:
        """Test helper for test_handle_errors_maps_not_found_to_404."""
        raise NotFoundError("nf")

    with pytest.raises(HTTPException) as exc_info:
        await fail()
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_handle_errors_maps_validation_error_to_422() -> None:
    """``ValidationError`` maps to 422 (Unprocessable Content) by HTTP spec."""

    @handle_errors()
    async def fail() -> None:
        """Test helper for test_handle_errors_maps_validation_error_to_422."""
        raise ValidationError("bad")

    with pytest.raises(HTTPException) as exc_info:
        await fail()
    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_handle_errors_maps_authentication_error_to_401() -> None:
    """Verify handle errors maps authentication error to 401."""

    @handle_errors()
    async def fail() -> None:
        """Test helper for test_handle_errors_maps_authentication_error_to_401."""
        raise AuthenticationError("auth")

    with pytest.raises(HTTPException) as exc_info:
        await fail()
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_handle_errors_maps_rate_limit_error_to_429() -> None:
    """Verify handle errors maps rate limit error to 429."""

    @handle_errors()
    async def fail() -> None:
        """Test helper for test_handle_errors_maps_rate_limit_error_to_429."""
        raise RateLimitError("limit")

    with pytest.raises(HTTPException) as exc_info:
        await fail()
    assert exc_info.value.status_code == 429


@pytest.mark.asyncio
async def test_handle_errors_maps_service_error_to_500() -> None:
    """Verify handle errors maps service error to 500."""

    @handle_errors()
    async def fail() -> None:
        """Test helper for test_handle_errors_maps_service_error_to_500."""
        raise ServiceError("svc")

    with pytest.raises(HTTPException) as exc_info:
        await fail()
    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_handle_errors_unexpected_exception_returns_500() -> None:
    """Verify handle errors unexpected exception returns 500."""

    @handle_errors()
    async def fail() -> None:
        """Test helper for test_handle_errors_unexpected_exception_returns_500."""
        raise RuntimeError("oops")

    with pytest.raises(HTTPException) as exc_info:
        await fail()
    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_handle_errors_custom_error_mapping() -> None:
    """Verify handle errors custom error mapping."""

    @handle_errors(error_mapping={NotFoundError: 410})
    async def fail() -> None:
        """Test helper for test_handle_errors_custom_error_mapping."""
        raise NotFoundError("gone")

    with pytest.raises(HTTPException) as exc_info:
        await fail()
    assert exc_info.value.status_code == 410


@pytest.mark.asyncio
async def test_handle_errors_resolves_subclass_via_isinstance() -> None:
    """Verify handle errors resolves subclass via isinstance."""
    from dyvine.core.exceptions import UserNotFoundError

    @handle_errors()
    async def fail() -> None:
        """Test helper for test_handle_errors_resolves_subclass_via_isinstance."""
        raise UserNotFoundError("gone")

    with pytest.raises(HTTPException) as exc_info:
        await fail()
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_handle_errors_logs_when_logger_provided() -> None:
    """Verify handle errors logs when logger provided."""
    mock_logger = MagicMock()

    @handle_errors(logger=mock_logger)
    async def fail() -> None:
        """Test helper for test_handle_errors_logs_when_logger_provided."""
        raise ServiceError("svc")

    with pytest.raises(HTTPException):
        await fail()

    mock_logger.error.assert_called_once()


# ── async generator support ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_errors_supports_async_generators_success() -> None:
    """Yields from a wrapped async generator must reach the caller."""

    @handle_errors()
    async def gen() -> object:
        """Test helper for test_handle_errors_supports_async_generators_success."""
        for i in range(3):
            yield i

    received = [value async for value in gen()]
    assert received == [0, 1, 2]


@pytest.mark.asyncio
async def test_handle_errors_translates_error_raised_mid_stream() -> None:
    """Exceptions raised after partial yields still produce HTTP errors.

    Regression guard for the naive ``return await func(...)`` wrapping
    path: calling ``await`` on the return value of an async generator
    function would raise ``TypeError`` before the decorator could run the
    translation. The generator-aware branch iterates instead, so earlier
    yields still reach the caller and the exception is translated in
    place.
    """

    @handle_errors()
    async def gen() -> object:
        """Test helper for test_handle_errors_translates_error_raised_mid_stream."""
        yield "before"
        yield "middle"
        raise NotFoundError("gone")

    received: list[str] = []
    with pytest.raises(HTTPException) as exc_info:
        async for value in gen():
            received.append(value)

    # The earlier yields must have been delivered before the exception
    # bubbled up; otherwise we've degraded streaming semantics.
    assert received == ["before", "middle"]
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_handle_errors_async_generator_unexpected_exception_to_500() -> None:
    """Non-Dyvine errors from async generators still become 500s."""

    @handle_errors()
    async def gen() -> object:
        """Test helper for
        test_handle_errors_async_generator_unexpected_exception_to_500.
        """
        yield 1
        raise RuntimeError("kaboom")

    received: list[int] = []
    with pytest.raises(HTTPException) as exc_info:
        async for value in gen():
            received.append(value)

    assert received == [1]
    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_handle_errors_async_generator_logs_when_logger_provided() -> None:
    """Logger hook must still fire for streamed handlers."""
    mock_logger = MagicMock()

    @handle_errors(logger=mock_logger)
    async def gen() -> object:
        """Test helper for
        test_handle_errors_async_generator_logs_when_logger_provided.
        """
        yield 1
        raise ServiceError("svc")

    with pytest.raises(HTTPException):
        async for _ in gen():
            pass

    mock_logger.error.assert_called_once()
