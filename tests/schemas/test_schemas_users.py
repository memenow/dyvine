"""Tests for user schema validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from dyvine.schemas.users import (
    DownloadResponse,
    UserDownloadRequest,
    UserResponse,
)


def test_user_download_request_required_user_id() -> None:
    """Verify user download request required user ID."""
    req = UserDownloadRequest(user_id="user_01")
    assert req.user_id == "user_01"


def test_user_download_request_defaults() -> None:
    """Verify user download request defaults."""
    req = UserDownloadRequest(user_id="user_01")
    assert req.include_posts is True
    assert req.include_likes is False
    assert req.max_items is None


def test_user_download_request_rejects_short_user_id() -> None:
    """The ID alphabet mirrors the service contract (6-128)."""
    with pytest.raises(ValidationError):
        UserDownloadRequest(user_id="u")


def test_user_download_request_rejects_non_positive_max_items() -> None:
    """``max_items`` mirrors the service ``gt=0`` contract."""
    with pytest.raises(ValidationError):
        UserDownloadRequest(user_id="user_01", max_items=0)
    with pytest.raises(ValidationError):
        UserDownloadRequest(user_id="user_01", max_items=-3)


def test_user_download_request_missing_user_id_raises() -> None:
    """Verify user download request missing user ID raises."""
    with pytest.raises(ValidationError):
        UserDownloadRequest()  # type: ignore[call-arg]


def test_user_response_all_fields() -> None:
    """``room_data`` is now a structured dict; the service decodes the JSON."""
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
        room_data={"status": 2},
    )
    assert resp.user_id == "u1"
    assert resp.is_living is True
    assert resp.room_id == 42
    assert resp.room_data == {"status": 2}


def test_user_response_avatar_url_empty_coerces_to_none() -> None:
    """Empty upstream avatars surface as ``None`` rather than an empty URL."""
    resp = UserResponse(
        user_id="u",
        nickname="n",
        avatar_url="",
        following_count=0,
        follower_count=0,
        total_favorited=0,
    )
    assert resp.avatar_url is None


def test_user_response_optional_fields_none() -> None:
    """Verify user response optional fields none."""
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
    """Verify download response all fields."""
    resp = DownloadResponse(
        operation_id="t1",
        operation_type="user_content_download",
        subject_id="u1",
        status="running",
        message="msg",
        progress=50.0,
        total_items=100,
        completed_items=50,
        error=None,
        created_at="2026-04-17T00:00:00+00:00",
        updated_at="2026-04-17T00:00:01+00:00",
    )
    assert resp.task_id == "t1"
    assert resp.progress == 50.0


def test_download_response_optional_fields_none() -> None:
    """Verify download response optional fields none."""
    resp = DownloadResponse(
        operation_id="t2",
        operation_type="user_content_download",
        subject_id="u2",
        status="pending",
        message="m",
        created_at="2026-04-17T00:00:00+00:00",
        updated_at="2026-04-17T00:00:00+00:00",
    )
    assert resp.progress is None
    assert resp.total_items is None
    assert resp.completed_items is None
    assert resp.error is None


def _download_kwargs(**overrides):  # type: ignore[no-untyped-def]
    kwargs = {
        "operation_id": "t1",
        "operation_type": "user_content_download",
        "subject_id": "u1",
        "status": "running",
        "message": "msg",
        "created_at": "2026-04-17T00:00:00+00:00",
        "updated_at": "2026-04-17T00:00:01+00:00",
    }
    kwargs.update(overrides)
    return kwargs


def test_download_response_rejects_conflicting_aliases() -> None:
    """Alias pairs must agree; silent forks are refused."""
    with pytest.raises(ValidationError, match="task_id"):
        DownloadResponse(**_download_kwargs(task_id="other"))
    with pytest.raises(ValidationError, match="downloaded_items"):
        DownloadResponse(**_download_kwargs(completed_items=3, downloaded_items=4))


def test_download_response_rejects_out_of_range_progress() -> None:
    """Progress outside 0-100 never serializes to clients."""
    with pytest.raises(ValidationError, match="progress"):
        DownloadResponse(**_download_kwargs(progress=130.0))
    with pytest.raises(ValidationError, match="progress"):
        DownloadResponse(**_download_kwargs(progress=-1.0))


def test_download_response_rejects_absolute_path() -> None:
    """The path field is root-relative; absolute paths leak layout."""
    with pytest.raises(ValidationError, match="download_path"):
        DownloadResponse(**_download_kwargs(download_path="/abs/path"))
    with pytest.raises(ValidationError, match="download_path"):
        DownloadResponse(**_download_kwargs(download_path="../escape"))
    assert (
        DownloadResponse(
            **_download_kwargs(download_path="nick/2026/a.mp4")
        ).download_path
        == "nick/2026/a.mp4"
    )


def test_download_response_parses_iso_timestamps() -> None:
    """ISO strings become datetimes; garbage is refused."""
    resp = DownloadResponse(**_download_kwargs())
    assert resp.created_at.year == 2026
    assert resp.created_at.tzinfo is not None
    with pytest.raises(ValidationError, match="created_at"):
        DownloadResponse(**_download_kwargs(created_at="not-a-time"))
