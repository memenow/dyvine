"""Per-replica token-bucket rate limiting (pure ASGI middleware).

Each caller key gets a bucket holding ``burst_size`` tokens refilled at
``requests_per_second``. The key is the ``X-API-Key`` header when
present, else the client IP. Buckets live in process memory, so limits
are enforced per replica: N replicas behind a load balancer admit
roughly N times the configured rate. That is acceptable for abuse
backpressure (a shared Redis limiter is the follow-up if exact global
accounting is ever needed).

The allow/deny decision performs no awaits, so it is atomic on the
event loop and needs no locks. Denials return the standard error
envelope with HTTP 429 and a ``Retry-After`` header.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from starlette.types import ASGIApp, Receive, Scope, Send

from ..core.error_handlers import ErrorResponse
from ..core.exceptions import RateLimitError
from ..core.logging import ContextLogger

logger = ContextLogger(__name__)

#: Paths that never consume budget: probes, metrics, and the index.
EXEMPT_PATHS = frozenset({"/", "/livez", "/readyz", "/startupz", "/health", "/metrics"})

#: Idle seconds after which a bucket is eligible for eviction.
_BUCKET_IDLE_EVICT_SECONDS = 600.0

#: Soft cap on tracked keys; past it, idle buckets are swept eagerly.
_MAX_TRACKED_KEYS = 10_000


@dataclass(slots=True)
class _Bucket:
    """Mutable token state for one caller key."""

    tokens: float
    last_refill_monotonic: float


class TokenBucketLimiter:
    """In-memory token buckets keyed by caller identity.

    Args:
        requests_per_second: Sustained refill rate per key.
        burst_size: Bucket capacity (maximum tolerated burst).
    """

    def __init__(self, *, requests_per_second: float, burst_size: float) -> None:
        """Create an empty limiter with the given rate and burst."""
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        if burst_size < 1:
            raise ValueError("burst_size must be at least 1")
        self._rate = float(requests_per_second)
        self._capacity = float(burst_size)
        self._buckets: dict[str, _Bucket] = {}

    @property
    def tracked_keys(self) -> int:
        """Number of caller keys currently holding bucket state."""
        return len(self._buckets)

    def _refill(self, bucket: _Bucket, now: float) -> None:
        """Top up ``bucket`` for time elapsed since its last refill."""
        elapsed = now - bucket.last_refill_monotonic
        if elapsed > 0:
            bucket.tokens = min(self._capacity, bucket.tokens + elapsed * self._rate)
            bucket.last_refill_monotonic = now

    def _evict_idle(self, now: float) -> None:
        """Drop buckets idle past the eviction horizon (DoS hygiene).

        Caller keys are unbounded (one per source IP), so without
        eviction a scanner sweeping IPs would grow this dict forever.
        """
        horizon = now - _BUCKET_IDLE_EVICT_SECONDS
        stale = [
            key
            for key, bucket in self._buckets.items()
            if bucket.last_refill_monotonic < horizon
        ]
        for key in stale:
            del self._buckets[key]

    def allow(self, key: str, *, now: float | None = None) -> float:
        """Consume one token for ``key``; return seconds to wait.

        Returns ``0.0`` when the request is admitted. Otherwise returns
        the (positive) delay after which a token becomes available, and
        the caller should answer HTTP 429 with ``Retry-After``.
        """
        moment = time.monotonic() if now is None else now
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(tokens=self._capacity, last_refill_monotonic=moment)
            self._buckets[key] = bucket
            if len(self._buckets) > _MAX_TRACKED_KEYS:
                self._evict_idle(moment)
        self._refill(bucket, moment)
        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return 0.0
        deficit = 1.0 - bucket.tokens
        return deficit / self._rate


class RateLimitMiddleware:
    """Pure ASGI middleware enforcing :class:`TokenBucketLimiter`.

    Must be added after the correlation middleware so denials carry a
    correlation ID. Exempt paths (probes, ``/metrics``, ``/``) pass
    through untouched.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        requests_per_second: float,
        burst_size: int,
        exempt_paths: frozenset[str] = EXEMPT_PATHS,
    ) -> None:
        """Bind the middleware to the downstream app and a fresh limiter."""
        self.app = app
        self._limiter = TokenBucketLimiter(
            requests_per_second=requests_per_second, burst_size=burst_size
        )
        self._exempt_paths = exempt_paths

    @staticmethod
    def _caller_key(scope: Scope) -> str:
        """Return the bucket key: API key header, else client IP."""
        headers = dict(scope.get("headers") or [])
        api_key = headers.get(b"x-api-key")
        if api_key:
            return f"key:{api_key.decode('latin-1')}"
        client = scope.get("client")
        if client is not None:
            return f"ip:{client[0]}"
        return "ip:unknown"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Enforce the bucket, or pass exempt/non-HTTP scopes through."""
        if scope["type"] != "http" or scope.get("path") in self._exempt_paths:
            await self.app(scope, receive, send)
            return
        retry_after = self._limiter.allow(self._caller_key(scope))
        if retry_after <= 0:
            await self.app(scope, receive, send)
            return
        seconds = max(1, math.ceil(retry_after))
        error = RateLimitError(
            f"Rate limit exceeded; retry in {seconds} second(s)",
            details={"retry_after_seconds": seconds},
        )
        logger.warning(
            "RateLimitError: %s",
            error.message,
            extra={"retry_after_seconds": seconds},
        )
        state = scope.get("state")
        correlation_id = None
        if isinstance(state, dict):
            correlation_id = state.get("correlation_id")
        elif state is not None:
            correlation_id = getattr(state, "correlation_id", None)
        response = ErrorResponse.create_response(
            status_code=429,
            message=error.message,
            error_code=error.error_code,
            details=error.details,
            correlation_id=correlation_id,
            headers={"Retry-After": str(seconds)},
        )
        await response(scope, receive, send)


__all__ = [
    "EXEMPT_PATHS",
    "RateLimitMiddleware",
    "TokenBucketLimiter",
]
