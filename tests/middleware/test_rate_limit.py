"""Tests for the token-bucket rate-limit middleware."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from dyvine.middleware import RateLimitMiddleware, TokenBucketLimiter


def test_limiter_allows_burst_then_denies() -> None:
    """The first ``burst`` takes succeed; the next one names a wait."""
    limiter = TokenBucketLimiter(requests_per_second=1.0, burst_size=3)
    assert [limiter.allow("k", now=1000.0) for _ in range(3)] == [0.0, 0.0, 0.0]
    wait = limiter.allow("k", now=1000.0)
    assert wait == pytest.approx(1.0)


def test_limiter_refills_over_time() -> None:
    """Idle time restores tokens up to the bucket capacity."""
    limiter = TokenBucketLimiter(requests_per_second=2.0, burst_size=2)
    assert limiter.allow("k", now=1000.0) == 0.0
    assert limiter.allow("k", now=1000.0) == 0.0
    assert limiter.allow("k", now=1000.0) > 0
    # Half a second at 2/s restores exactly one token.
    assert limiter.allow("k", now=1000.5) == 0.0
    assert limiter.allow("k", now=1000.5) > 0


def test_limiter_isolates_keys() -> None:
    """One exhausted caller never spends another caller's budget."""
    limiter = TokenBucketLimiter(requests_per_second=1.0, burst_size=1)
    assert limiter.allow("alice", now=1000.0) == 0.0
    assert limiter.allow("alice", now=1000.0) > 0
    assert limiter.allow("bob", now=1000.0) == 0.0


def test_limiter_rejects_nonsense_config() -> None:
    """Zero rates and sub-one bursts fail loudly at construction."""
    with pytest.raises(ValueError, match="requests_per_second"):
        TokenBucketLimiter(requests_per_second=0, burst_size=5)
    with pytest.raises(ValueError, match="burst_size"):
        TokenBucketLimiter(requests_per_second=5, burst_size=0)


def test_limiter_evicts_idle_buckets_past_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past the key cap, long-idle buckets are swept for fresh ones."""
    import dyvine.middleware.rate_limit as module

    monkeypatch.setattr(module, "_MAX_TRACKED_KEYS", 2)
    limiter = TokenBucketLimiter(requests_per_second=1.0, burst_size=1)
    assert limiter.allow("old", now=0.0) == 0.0
    assert limiter.allow("newer", now=1000.0) == 0.0
    # Third key trips the cap; the ancient bucket is evicted.
    assert limiter.allow("fresh", now=1000.0) == 0.0
    assert limiter.tracked_keys == 2


async def _stub_app(scope: Scope, receive: Receive, send: Send) -> None:
    """Downstream app recording that it was reached."""
    response = JSONResponse({"ok": True})
    await response(scope, receive, send)


def _scope(
    path: str = "/api/v1/users/x", headers: list[tuple[bytes, bytes]] | None = None
) -> Scope:
    """Build a minimal HTTP scope for direct middleware calls."""
    return {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "query_string": b"",
        "headers": headers or [],
        "client": ("10.0.0.9", 1234),
        "server": ("testserver", 80),
        "state": {},
    }


async def _drain(
    app: RateLimitMiddleware, scope: Scope
) -> tuple[int, dict[str, str], bytes]:
    """Drive one request through ``app`` and capture the response."""
    status = 0
    response_headers: dict[str, str] = {}
    body = bytearray()

    async def _receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _send(message: dict[str, object]) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = int(str(message["status"]))
            raw = message.get("headers") or []
            for name, value in raw:
                assert isinstance(name, bytes) and isinstance(value, bytes)
                response_headers[name.decode("latin-1")] = value.decode("latin-1")
        elif message["type"] == "http.response.body":
            chunk = message.get("body", b"")
            assert isinstance(chunk, bytes)
            body.extend(chunk)

    await app(scope, _receive, _send)
    return status, response_headers, bytes(body)


async def test_middleware_denies_with_envelope_and_retry_after() -> None:
    """Denials use the standard envelope plus a ``Retry-After`` header."""
    import json

    app = RateLimitMiddleware(_stub_app, requests_per_second=1.0, burst_size=1)
    assert (await _drain(app, _scope()))[0] == 200
    status, headers, body = await _drain(app, _scope())
    assert status == 429
    assert headers["retry-after"] == "1"
    payload = json.loads(body)
    assert payload["error"] is True
    assert payload["error_code"] == "RateLimitError"
    assert payload["status_code"] == 429
    assert "retry" in payload["message"].lower()
    assert payload["details"]["retry_after_seconds"] == 1


async def test_middleware_keys_on_api_key_header() -> None:
    """Present API keys bucket per key; absent keys bucket per IP."""
    app = RateLimitMiddleware(_stub_app, requests_per_second=1.0, burst_size=1)
    alice = _scope(headers=[(b"x-api-key", b"alice")])
    bob = _scope(headers=[(b"x-api-key", b"bob")])
    assert (await _drain(app, alice))[0] == 200
    assert (await _drain(app, alice))[0] == 429
    assert (await _drain(app, bob))[0] == 200
    # No header: the shared client IP has its own untouched bucket.
    assert (await _drain(app, _scope()))[0] == 200
    assert (await _drain(app, _scope()))[0] == 429


async def test_middleware_exempts_probes_metrics_and_index() -> None:
    """Exempt paths never consume budget, however often they are hit."""
    app = RateLimitMiddleware(_stub_app, requests_per_second=1.0, burst_size=1)
    for path in ("/", "/livez", "/readyz", "/startupz", "/health", "/metrics"):
        for _ in range(3):
            status, _, _ = await _drain(app, _scope(path))
            assert status == 200
    # The guarded path still has its full burst untouched.
    assert (await _drain(app, _scope()))[0] == 200


async def test_middleware_passes_non_http_scopes_through() -> None:
    """Lifespan and websocket scopes bypass the limiter entirely."""
    reached: list[Scope] = []

    async def _recording_app(scope: Scope, receive: Receive, send: Send) -> None:
        reached.append(scope)

    async def _receive() -> dict[str, object]:
        return {"type": "websocket.connect"}

    async def _send(message: dict[str, object]) -> None:
        raise AssertionError("downstream must not respond")

    app = RateLimitMiddleware(_recording_app, requests_per_second=1.0, burst_size=1)
    scope: Scope = {"type": "websocket", "path": "/ws", "headers": []}
    await app(scope, _receive, _send)
    assert reached == [scope]
    assert app._limiter.tracked_keys == 0


async def test_middleware_admits_exactly_burst_under_concurrency() -> None:
    """Concurrent arrivals split the burst with no over-admission.

    The decision path performs no awaits, so interleavings cannot
    spend one token twice: exactly ``burst`` of the racing requests
    reach the downstream app.
    """
    reached = 0

    async def _counting_app(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal reached
        reached += 1
        await _stub_app(scope, receive, send)

    app = RateLimitMiddleware(_counting_app, requests_per_second=1.0, burst_size=5)
    results = await asyncio.gather(*(_drain(app, _scope()) for _ in range(20)))
    assert reached == 5
    assert sorted(status for status, _, _ in results).count(200) == 5
    assert sorted(status for status, _, _ in results).count(429) == 15


def test_rate_limit_end_to_end_envelope() -> None:
    """A tight app returns the envelope, header, and correlation ID."""
    import uuid

    inner = FastAPI()

    @inner.get("/items")
    async def _items() -> dict[str, bool]:
        return {"ok": True}

    inner.add_middleware(RateLimitMiddleware, requests_per_second=1.0, burst_size=1)
    client = TestClient(inner)
    assert client.get("/items").status_code == 200
    denied = client.get("/items", headers={"X-Request-ID": str(uuid.uuid4())})
    assert denied.status_code == 429
    assert denied.headers["Retry-After"] == "1"
    payload = denied.json()
    assert payload["error"] is True
    assert payload["error_code"] == "RateLimitError"
