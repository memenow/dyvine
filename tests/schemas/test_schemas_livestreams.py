"""Tests for livestream schema validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from dyvine.schemas.livestreams import (
    LiveStreamDownloadRequest,
    LiveStreamDownloadResponse,
    LiveStreamURLDownloadRequest,
)


def test_livestream_download_request_required_user_id() -> None:
    """Verify livestream download request required user ID."""
    req = LiveStreamDownloadRequest(user_id="user01")
    assert req.user_id == "user01"


def test_livestream_download_request_rejects_too_short_user_id() -> None:
    """The schema rejects user_ids that fail the SSRF-prevention pattern."""
    with pytest.raises(ValidationError):
        LiveStreamDownloadRequest(user_id="u1")


def test_livestream_download_request_rejects_traversal_output_path() -> None:
    """Absolute paths and ``..`` segments are rejected at the schema layer."""
    with pytest.raises(ValidationError):
        LiveStreamDownloadRequest(user_id="user01", output_path="../../etc/passwd")
    with pytest.raises(ValidationError):
        LiveStreamDownloadRequest(user_id="user01", output_path="/abs/path")


def test_livestream_download_request_optional_output_path() -> None:
    """Verify livestream download request optional output path."""
    req = LiveStreamDownloadRequest(user_id="user01")
    assert req.output_path is None

    req2 = LiveStreamDownloadRequest(user_id="user01", output_path="rooms/abc")
    assert req2.output_path == "rooms/abc"


def test_livestream_download_request_missing_user_id_raises() -> None:
    """Verify livestream download request missing user ID raises."""
    with pytest.raises(ValidationError):
        LiveStreamDownloadRequest()  # type: ignore[call-arg]


def test_livestream_url_download_request_required_url() -> None:
    """Verify livestream URL download request required URL."""
    req = LiveStreamURLDownloadRequest(url="https://live.douyin.com/123")
    assert str(req.url) == "https://live.douyin.com/123"


def test_livestream_url_download_request_rejects_non_douyin_host() -> None:
    """Non-douyin hosts are rejected to prevent SSRF via the URL endpoint."""
    with pytest.raises(ValidationError):
        LiveStreamURLDownloadRequest(url="https://evil.example.com/123")


def test_livestream_url_download_request_rejects_non_http_scheme() -> None:
    """Pydantic ``AnyHttpUrl`` rejects ``file://``/``ftp://`` schemes."""
    with pytest.raises(ValidationError):
        LiveStreamURLDownloadRequest(url="file:///etc/passwd")


def test_livestream_url_download_request_optional_output_path() -> None:
    """Verify livestream URL download request optional output path."""
    req = LiveStreamURLDownloadRequest(url="https://live.douyin.com/1")
    assert req.output_path is None


def test_livestream_download_response_fields() -> None:
    """Verify livestream download response fields."""
    resp = LiveStreamDownloadResponse(
        operation_id="op1",
        operation_type="livestream_download",
        subject_id="room-1",
        status="completed",
        message="done",
        download_path="/p",
        created_at="2026-04-17T00:00:00+00:00",
        updated_at="2026-04-17T00:00:01+00:00",
    )
    assert resp.status == "completed"
    assert resp.error is None

    resp2 = LiveStreamDownloadResponse(
        operation_id="op2",
        operation_type="livestream_download",
        subject_id="room-2",
        status="failed",
        message="fail",
        error="fail",
        created_at="2026-04-17T00:00:00+00:00",
        updated_at="2026-04-17T00:00:01+00:00",
    )
    assert resp2.download_path is None
    assert resp2.error == "fail"
