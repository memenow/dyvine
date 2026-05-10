"""Pydantic models for the livestream router.

Provides:

- `LiveStreamDownloadRequest` — SDK-facing request body for downloads
  identified by `user_id`. The router itself takes `user_id` as a
  path parameter and `output_path` as an embedded body field; the
  model is exported here for consumers that want a typed contract.
- `LiveStreamURLDownloadRequest` — body for the URL-based download
  endpoint (`POST /livestreams/stream:download`). Validates the host
  against the `douyin.com` allowlist to prevent SSRF and rejects
  output paths that contain absolute prefixes or `..` segments.
- `LiveStreamDownloadResponse` — alias for `OperationResponse`.

The schema-layer `output_path` validator is intentionally cheap: the
authoritative jail check (including symlink-segment scanning before
and after `mkdir`) runs in `core.path_safety.resolve_within_root`
once the request hits the service.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl, BaseModel, Field, field_validator

from .operations import OperationResponse

_USER_ID_PATTERN = r"^[A-Za-z0-9_\-]{6,128}$"

# Hostnames the livestream URL endpoint is allowed to point at. Anything
# else is rejected at the schema layer so the service never builds an
# outbound HTTP request to an attacker-controlled host (SSRF).
_ALLOWED_LIVESTREAM_HOSTS: frozenset[str] = frozenset(
    {
        "douyin.com",
        "www.douyin.com",
        "live.douyin.com",
        "v.douyin.com",
    }
)


def _validate_output_path(value: str | None) -> str | None:
    """Reject absolute paths and traversal segments at the schema boundary.

    The full jail check happens server-side via
    :func:`dyvine.core.path_safety.resolve_within_root`; this validator
    catches the cheap structural cases (``../`` segments, absolute paths)
    early so an obviously-malicious request never reaches the service
    layer.
    """
    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    posix = PurePosixPath(candidate)
    if posix.is_absolute() or any(part == ".." for part in posix.parts):
        raise ValueError(
            "output_path must be a relative path within the configured download root"
        )
    return candidate


class LiveStreamDownloadRequest(BaseModel):
    """Request model for initiating a livestream download.

    Attributes:
        user_id: The unique identifier of the user whose stream to download.
        output_path: Optional custom path where the stream should be saved.
            If not provided, a default path will be used.
    """

    user_id: str = Field(
        ...,
        pattern=_USER_ID_PATTERN,
        description=(
            "The Douyin user identifier (sec_user_id). Restricted to the "
            "alphabet Douyin actually emits to prevent injection into "
            "generated upstream URLs."
        ),
    )
    output_path: str | None = Field(
        None,
        description=(
            "Optional path relative to the configured download root. "
            "Absolute paths and traversal segments are rejected."
        ),
    )

    @field_validator("output_path")
    @classmethod
    def _check_output_path(cls, value: str | None) -> str | None:
        return _validate_output_path(value)


class LiveStreamURLDownloadRequest(BaseModel):
    """Request model for initiating a livestream download via URL.

    Attributes:
        url: The livestream URL (user profile or direct room URL).
        output_path: Optional custom path where the stream should be saved.
            If not provided, a default path will be used.
    """

    url: AnyHttpUrl = Field(
        ...,
        description=(
            "Douyin livestream URL (user profile, room URL, or short link). "
            "Only ``douyin.com`` family hosts are accepted."
        ),
    )
    output_path: str | None = Field(
        None,
        description=(
            "Optional path relative to the configured download root. "
            "Absolute paths and traversal segments are rejected."
        ),
    )

    @field_validator("url")
    @classmethod
    def _check_host(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        # Pydantic preserves the parsed host as ``value.host``; fall back
        # to ``urlsplit`` only if a custom validator path bypasses
        # ``HttpUrl``.
        host = (value.host or urlsplit(str(value)).hostname or "").lower()
        if host not in _ALLOWED_LIVESTREAM_HOSTS:
            raise ValueError(f"URL host {host!r} is not on the douyin.com allowlist")
        return value

    @field_validator("output_path")
    @classmethod
    def _check_output_path(cls, value: str | None) -> str | None:
        return _validate_output_path(value)


class LiveStreamDownloadResponse(OperationResponse):
    """Backward-compatible alias for livestream download operations."""
