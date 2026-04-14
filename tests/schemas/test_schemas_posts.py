from __future__ import annotations

import pytest
from pydantic import ValidationError

from dyvine.schemas.posts import (
    BulkDownloadResponse,
    DownloadStatus,
    ImageInfo,
    PostBase,
    PostDetail,
    PostType,
    VideoInfo,
)

# ── Enums ────────────────────────────────────────────────────────────────


def test_post_type_enum_values() -> None:
    assert PostType.VIDEO == "video"
    assert PostType.IMAGES == "images"
    assert PostType.MIXED == "mixed"
    assert PostType.LIVE == "live"
    assert PostType.COLLECTION == "collection"
    assert PostType.STORY == "story"
    assert PostType.UNKNOWN == "unknown"
    assert len(PostType) == 7


def test_download_status_enum_values() -> None:
    assert DownloadStatus.SUCCESS == "success"
    assert DownloadStatus.PARTIAL_SUCCESS == "partial_success"
    assert DownloadStatus.FAILED == "failed"


# ── PostBase ─────────────────────────────────────────────────────────────


def test_post_base_required_fields() -> None:
    p = PostBase(aweme_id="123", create_time=1000)
    assert p.aweme_id == "123"
    assert p.create_time == 1000


def test_post_base_desc_defaults_empty() -> None:
    p = PostBase(aweme_id="123", create_time=0)
    assert p.desc == ""


# ── VideoInfo ────────────────────────────────────────────────────────────


def test_video_info_valid() -> None:
    v = VideoInfo(
        play_addr="https://example.com/video.mp4",
        duration=60,
        ratio="16:9",
        width=1920,
        height=1080,
    )
    assert v.duration == 60


def test_video_info_rejects_invalid_url() -> None:
    with pytest.raises(ValidationError):
        VideoInfo(
            play_addr="not-a-url",
            duration=0,
            ratio="",
            width=0,
            height=0,
        )


# ── ImageInfo ────────────────────────────────────────────────────────────


def test_image_info_valid() -> None:
    img = ImageInfo(
        url="https://example.com/img.jpg",
        width=800,
        height=600,
    )
    assert img.width == 800


def test_image_info_rejects_invalid_url() -> None:
    with pytest.raises(ValidationError):
        ImageInfo(url="bad", width=0, height=0)


# ── PostDetail ───────────────────────────────────────────────────────────


def test_post_detail_with_video_only() -> None:
    pd = PostDetail(
        aweme_id="1",
        create_time=0,
        post_type=PostType.VIDEO,
        video_info=VideoInfo(
            play_addr="https://example.com/v.mp4",
            duration=10,
            ratio="4:3",
            width=640,
            height=480,
        ),
    )
    assert pd.video_info is not None
    assert pd.images is None


def test_post_detail_with_no_media() -> None:
    pd = PostDetail(
        aweme_id="2",
        create_time=0,
        post_type=PostType.UNKNOWN,
    )
    assert pd.video_info is None
    assert pd.images is None
    assert pd.statistics == {}


# ── BulkDownloadResponse ────────────────────────────────────────────────


def test_bulk_download_response_defaults() -> None:
    resp = BulkDownloadResponse(
        sec_user_id="u1",
        download_path="/tmp",
        total_posts=0,
        status=DownloadStatus.FAILED,
    )
    assert resp.total_downloaded == 0
    assert all(v == 0 for v in resp.downloaded_count.values())
    assert set(resp.downloaded_count.keys()) == set(PostType)
