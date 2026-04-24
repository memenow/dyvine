from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from dyvine.services.storage import (
    ContentType,
    R2StorageService,
    StorageError,
)


def _build_service(*, with_client: bool = False) -> R2StorageService:
    """Create R2StorageService without boto3 initialization."""
    svc = object.__new__(R2StorageService)
    if with_client:
        svc.client = MagicMock()  # type: ignore[attr-defined]
        svc.bucket = "test-bucket"  # type: ignore[attr-defined]
    else:
        svc.client = None  # type: ignore[attr-defined]
        svc.bucket = None  # type: ignore[attr-defined]
    return svc


# ── generate_livestream_path ─────────────────────────────────────────────


def test_generate_livestream_path_format() -> None:
    svc = _build_service()
    path = svc.generate_livestream_path("user-1", "stream-a", 1700000000)
    assert path == "livestreams/user-1/stream-a/recording_1700000000.mp4"


def test_generate_livestream_path_varying_inputs() -> None:
    svc = _build_service()
    path = svc.generate_livestream_path("u", "s", 0)
    assert path.startswith("livestreams/u/s/")
    assert path.endswith(".mp4")


# ── generate_metadata edge cases ─────────────────────────────────────────


def test_generate_metadata_default_language_and_version() -> None:
    svc = _build_service()
    md = svc.generate_metadata(
        author="a",
        category=ContentType.POSTS,
        content_type="video/mp4",
        source="test",
    )
    assert md["language"] == "zh-CN"
    assert md["version"] == "1.0.0"


def test_generate_metadata_custom_language_and_version() -> None:
    svc = _build_service()
    md = svc.generate_metadata(
        author="a",
        category=ContentType.LIVESTREAM,
        content_type="video/mp4",
        source="test",
        language="ja-JP",
        version="2.0.0",
    )
    assert md["language"] == "ja-JP"
    assert md["version"] == "2.0.0"


def test_generate_metadata_file_format_from_content_type() -> None:
    svc = _build_service()
    md = svc.generate_metadata(
        author="a",
        category=ContentType.POSTS,
        content_type="image/png",
        source="test",
    )
    assert md["file-format"] == "png"


# ── generate_ugc_path edge cases ─────────────────────────────────────────


def test_generate_ugc_path_image_type() -> None:
    svc = _build_service()
    path = svc.generate_ugc_path("u1", "photo.png", "image/png")
    assert path.startswith("images/u1/")
    assert path.endswith(".png")


def test_generate_ugc_path_no_extension_guesses() -> None:
    svc = _build_service()
    path = svc.generate_ugc_path("u1", "noext", "image/jpeg")
    assert path.startswith("images/u1/")
    # Should guess an extension from mime type
    assert re.search(r"\.(jpg|jpeg)$", path)


def test_generate_ugc_path_video_standardizes_to_mp4() -> None:
    svc = _build_service()
    path = svc.generate_ugc_path("u1", "clip.avi", "video/avi")
    assert path.endswith(".mp4")
    assert path.startswith("videos/u1/")


# ── upload_file ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_file_disabled_raises() -> None:
    svc = _build_service(with_client=False)
    with pytest.raises(StorageError, match="disabled"):
        await svc.upload_file("/nonexistent", "path", {})


@pytest.mark.asyncio
async def test_upload_file_file_not_found_raises(tmp_path: Path) -> None:
    svc = _build_service(with_client=True)
    bad_path = tmp_path / "missing.bin"
    with pytest.raises(StorageError, match="not found"):
        await svc.upload_file(bad_path, "dest", {"category": "posts"})


@pytest.mark.asyncio
async def test_upload_file_success(tmp_path: Path) -> None:
    svc = _build_service(with_client=True)
    f = tmp_path / "test.mp4"
    f.write_bytes(b"data")

    svc.client.generate_presigned_url.return_value = "https://signed.url"  # type: ignore[union-attr]

    result = await svc.upload_file(
        f,
        "videos/u/test.mp4",
        {"category": "posts"},
        "video/mp4",
    )
    assert result["storage_path"] == "videos/u/test.mp4"
    assert result["presigned_url"] == "https://signed.url"
    svc.client.put_object.assert_called_once()  # type: ignore[union-attr]


# ── get_object_metadata ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_object_metadata_success() -> None:
    svc = _build_service(with_client=True)
    svc.client.head_object.return_value = {  # type: ignore[union-attr]
        "Metadata": {"author": "test"}
    }
    md = await svc.get_object_metadata("key")
    assert md == {"author": "test"}


@pytest.mark.asyncio
async def test_get_object_metadata_404_raises() -> None:
    from botocore.exceptions import ClientError

    svc = _build_service(with_client=True)
    svc.client.head_object.side_effect = ClientError(  # type: ignore[union-attr]
        {"Error": {"Code": "404"}}, "HeadObject"
    )
    with pytest.raises(StorageError, match="not found"):
        await svc.get_object_metadata("missing")


# ── delete_object / list_objects disabled ────────────────────────────────


@pytest.mark.asyncio
async def test_delete_object_disabled_raises() -> None:
    svc = _build_service(with_client=False)
    with pytest.raises(StorageError, match="disabled"):
        await svc.delete_object("key")


@pytest.mark.asyncio
async def test_list_objects_disabled_raises() -> None:
    svc = _build_service(with_client=False)
    with pytest.raises(StorageError, match="disabled"):
        await svc.list_objects("prefix/")


@pytest.mark.asyncio
async def test_list_objects_success() -> None:
    svc = _build_service(with_client=True)
    svc.client.list_objects_v2.return_value = {  # type: ignore[union-attr]
        "Contents": [
            {"Key": "videos/u1/clip.mp4", "Size": 1024, "StorageClass": "STANDARD"}
        ]
    }
    svc.client.head_object.return_value = {"Metadata": {"author": "test"}}  # type: ignore[union-attr]

    results = await svc.list_objects("videos/u1/")
    assert len(results) == 1
    assert results[0]["Key"] == "videos/u1/clip.mp4"
    assert results[0]["Metadata"] == {"author": "test"}


@pytest.mark.asyncio
async def test_delete_object_success() -> None:
    svc = _build_service(with_client=True)
    await svc.delete_object("videos/u1/clip.mp4")
    svc.client.delete_object.assert_called_once_with(  # type: ignore[union-attr]
        Bucket="test-bucket", Key="videos/u1/clip.mp4"
    )


@pytest.mark.asyncio
async def test_upload_file_guesses_content_type(tmp_path: Path) -> None:
    svc = _build_service(with_client=True)
    f = tmp_path / "image.jpg"
    f.write_bytes(b"\xff\xd8\xff")

    svc.client.generate_presigned_url.return_value = "https://url"  # type: ignore[union-attr]

    result = await svc.upload_file(f, "images/u/image.jpg", {"category": "posts"})
    assert "image" in result["content_type"]
