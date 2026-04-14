from __future__ import annotations

import pytest
from pydantic import ValidationError

from dyvine.schemas.users import (
    DownloadResponse,
    UserDownloadRequest,
    UserResponse,
)


def test_user_download_request_required_user_id() -> None:
    req = UserDownloadRequest(user_id="u123")
    assert req.user_id == "u123"


def test_user_download_request_defaults() -> None:
    req = UserDownloadRequest(user_id="u")
    assert req.include_posts is True
    assert req.include_likes is False
    assert req.max_items is None


def test_user_download_request_missing_user_id_raises() -> None:
    with pytest.raises(ValidationError):
        UserDownloadRequest()  # type: ignore[call-arg]


def test_user_response_all_fields() -> None:
    resp = UserResponse(
        user_id="u1",
        nickname="nick",
        avatar_url="https://example.com/img.jpg",
        signature="bio",
        following_count=10,
        follower_count=20,
        total_favorited=100,
        is_living=True,
        room_id=42,
        room_data='{"status": 2}',
    )
    assert resp.user_id == "u1"
    assert resp.is_living is True
    assert resp.room_id == 42


def test_user_response_optional_fields_none() -> None:
    resp = UserResponse(
        user_id="u2",
        nickname="n",
        avatar_url="https://example.com/a.jpg",
        following_count=0,
        follower_count=0,
        total_favorited=0,
    )
    assert resp.signature is None
    assert resp.room_id is None
    assert resp.room_data is None


def test_download_response_all_fields() -> None:
    resp = DownloadResponse(
        task_id="t1",
        status="running",
        message="msg",
        progress=50.0,
        total_items=100,
        downloaded_items=50,
        error=None,
    )
    assert resp.task_id == "t1"
    assert resp.progress == 50.0


def test_download_response_optional_fields_none() -> None:
    resp = DownloadResponse(task_id="t2", status="pending", message="m")
    assert resp.progress is None
    assert resp.total_items is None
    assert resp.downloaded_items is None
    assert resp.error is None
