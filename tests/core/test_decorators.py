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
    @handle_errors()
    async def ok() -> str:
        return "ok"

    assert await ok() == "ok"


@pytest.mark.asyncio
async def test_handle_errors_maps_not_found_to_404() -> None:
    @handle_errors()
    async def fail() -> None:
        raise NotFoundError("nf")

    with pytest.raises(HTTPException) as exc_info:
        await fail()
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_handle_errors_maps_validation_error_to_400() -> None:
    @handle_errors()
    async def fail() -> None:
        raise ValidationError("bad")

    with pytest.raises(HTTPException) as exc_info:
        await fail()
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_handle_errors_maps_authentication_error_to_401() -> None:
    @handle_errors()
    async def fail() -> None:
        raise AuthenticationError("auth")

    with pytest.raises(HTTPException) as exc_info:
        await fail()
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_handle_errors_maps_rate_limit_error_to_429() -> None:
    @handle_errors()
    async def fail() -> None:
        raise RateLimitError("limit")

    with pytest.raises(HTTPException) as exc_info:
        await fail()
    assert exc_info.value.status_code == 429


@pytest.mark.asyncio
async def test_handle_errors_maps_service_error_to_500() -> None:
    @handle_errors()
    async def fail() -> None:
        raise ServiceError("svc")

    with pytest.raises(HTTPException) as exc_info:
        await fail()
    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_handle_errors_unexpected_exception_returns_500() -> None:
    @handle_errors()
    async def fail() -> None:
        raise RuntimeError("oops")

    with pytest.raises(HTTPException) as exc_info:
        await fail()
    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_handle_errors_custom_error_mapping() -> None:
    @handle_errors(error_mapping={NotFoundError: 410})
    async def fail() -> None:
        raise NotFoundError("gone")

    with pytest.raises(HTTPException) as exc_info:
        await fail()
    assert exc_info.value.status_code == 410


@pytest.mark.asyncio
async def test_handle_errors_logs_when_logger_provided() -> None:
    mock_logger = MagicMock()

    @handle_errors(logger=mock_logger)
    async def fail() -> None:
        raise ServiceError("svc")

    with pytest.raises(HTTPException):
        await fail()

    mock_logger.error.assert_called_once()
