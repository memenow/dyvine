from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from dyvine.core.error_handlers import (
    ErrorResponse,
    dyvine_error_handler,
    generic_exception_handler,
    http_exception_handler,
    register_error_handlers,
)
from dyvine.core.exceptions import (
    ServiceError,
    UserNotFoundError,
    ValidationError,
)


def _make_request(correlation_id: str | None = None) -> MagicMock:
    req = MagicMock()
    req.state = MagicMock()
    if correlation_id is not None:
        req.state.correlation_id = correlation_id
    else:
        del req.state.correlation_id
    req.url.path = "/test"
    req.method = "GET"
    return req


# ── ErrorResponse.create_response ────────────────────────────────────────


def test_error_response_basic() -> None:
    resp = ErrorResponse.create_response(400, "bad request")
    assert resp.status_code == 400
    body = resp.body.decode()
    assert '"error": true' in body or '"error":true' in body


def test_error_response_includes_details() -> None:
    resp = ErrorResponse.create_response(400, "bad", details={"field": "name"})
    body = resp.body.decode()
    assert "name" in body


def test_error_response_includes_correlation_id() -> None:
    resp = ErrorResponse.create_response(400, "bad", correlation_id="abc-123")
    body = resp.body.decode()
    assert "abc-123" in body


def test_error_response_includes_traceback_when_enabled() -> None:
    try:
        raise ValueError("test-exc")
    except ValueError as e:
        resp = ErrorResponse.create_response(
            500, "fail", include_traceback=True, exception=e
        )
    body = resp.body.decode()
    assert "traceback" in body


def test_error_response_omits_traceback_when_disabled() -> None:
    try:
        raise ValueError("test-exc")
    except ValueError as e:
        resp = ErrorResponse.create_response(
            500, "fail", include_traceback=False, exception=e
        )
    body = resp.body.decode()
    assert "traceback" not in body


# ── dyvine_error_handler ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dyvine_error_handler_not_found_returns_404() -> None:
    req = _make_request("cid-1")
    resp = await dyvine_error_handler(req, UserNotFoundError("gone"))
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_dyvine_error_handler_service_error_returns_500() -> None:
    req = _make_request("cid-2")
    resp = await dyvine_error_handler(req, ServiceError("boom"))
    assert resp.status_code == 500


@pytest.mark.asyncio
async def test_dyvine_error_handler_generic_dyvine_returns_400() -> None:
    req = _make_request("cid-3")
    resp = await dyvine_error_handler(req, ValidationError("bad"))
    assert resp.status_code == 400


# ── generic_exception_handler ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generic_exception_handler_returns_500() -> None:
    req = _make_request("cid-4")
    resp = await generic_exception_handler(req, RuntimeError("unexpected"))
    assert resp.status_code == 500
    body = resp.body.decode()
    assert "Internal server error" in body


@pytest.mark.asyncio
async def test_generic_exception_handler_traceback_in_debug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dyvine.core import settings as settings_mod

    monkeypatch.setattr(settings_mod.settings.api, "debug", True)
    req = _make_request("cid-5")
    resp = await generic_exception_handler(req, RuntimeError("dbg"))
    body = resp.body.decode()
    assert "traceback" in body


@pytest.mark.asyncio
async def test_http_exception_handler_normalizes_response() -> None:
    req = _make_request("cid-6")
    resp = await http_exception_handler(
        req, HTTPException(status_code=404, detail="nf")
    )
    assert resp.status_code == 404
    body = resp.body.decode()
    assert "HTTP_404" in body


@pytest.mark.asyncio
async def test_http_exception_handler_preserves_headers() -> None:
    req = _make_request("cid-7")
    resp = await http_exception_handler(
        req,
        HTTPException(
            status_code=405,
            detail="method not allowed",
            headers={"Allow": "GET"},
        ),
    )
    assert resp.status_code == 405
    assert resp.headers["Allow"] == "GET"


@pytest.mark.asyncio
async def test_http_exception_handler_handles_none_detail() -> None:
    req = _make_request("cid-8")
    exc = HTTPException(status_code=500, detail=None)
    # Starlette substitutes a default HTTP phrase when ``detail`` is ``None``;
    # override it to exercise the genuine ``None`` branch in the handler.
    exc.detail = None
    resp = await http_exception_handler(req, exc)
    assert resp.status_code == 500
    payload = json.loads(resp.body)
    assert payload["message"] == ""
    assert payload["error_code"] == "HTTP_500"


# ── register_error_handlers ─────────────────────────────────────────────


def test_register_error_handlers_adds_handlers() -> None:
    app = MagicMock()
    register_error_handlers(app)
    assert app.add_exception_handler.call_count == 3
