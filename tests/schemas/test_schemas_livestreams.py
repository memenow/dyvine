from __future__ import annotations

import pytest
from pydantic import ValidationError

from dyvine.schemas.livestreams import (
    LiveStreamDownloadRequest,
    LiveStreamDownloadResponse,
    LiveStreamURLDownloadRequest,
)


def test_livestream_download_request_required_user_id() -> None:
    req = LiveStreamDownloadRequest(user_id="u1")
    assert req.user_id == "u1"


def test_livestream_download_request_optional_output_path() -> None:
    req = LiveStreamDownloadRequest(user_id="u1")
    assert req.output_path is None

    req2 = LiveStreamDownloadRequest(user_id="u1", output_path="/out")
    assert req2.output_path == "/out"


def test_livestream_download_request_missing_user_id_raises() -> None:
    with pytest.raises(ValidationError):
        LiveStreamDownloadRequest()  # type: ignore[call-arg]


def test_livestream_url_download_request_required_url() -> None:
    req = LiveStreamURLDownloadRequest(url="https://live.douyin.com/123")
    assert req.url == "https://live.douyin.com/123"


def test_livestream_url_download_request_optional_output_path() -> None:
    req = LiveStreamURLDownloadRequest(url="https://live.douyin.com/1")
    assert req.output_path is None


def test_livestream_download_response_fields() -> None:
    resp = LiveStreamDownloadResponse(status="success", download_path="/p")
    assert resp.status == "success"
    assert resp.error is None

    resp2 = LiveStreamDownloadResponse(status="error", error="fail")
    assert resp2.download_path is None
    assert resp2.error == "fail"
