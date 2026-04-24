from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from dyvine.services.storage import (
    LIST_OBJECTS_HEAD_MAX_WORKERS,
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
async def test_delete_object_client_error_surfaces_as_storage_error() -> None:
    from botocore.exceptions import ClientError

    svc = _build_service(with_client=True)
    svc.client.delete_object.side_effect = ClientError(  # type: ignore[union-attr]
        {"Error": {"Code": "AccessDenied"}}, "DeleteObject"
    )

    with pytest.raises(StorageError, match="Deletion failed"):
        await svc.delete_object("videos/u1/clip.mp4")


@pytest.mark.asyncio
async def test_list_objects_per_item_head_error_returns_empty_metadata() -> None:
    """A per-item ``head_object`` failure is tolerated and ordering is preserved.

    The fan-out is parallel, so the test dispatches ``head_object`` by key
    rather than by call index to stay deterministic regardless of thread
    scheduling. A mid-list failure still yields ``Metadata: {}`` for that
    item while neighbors retain their metadata.
    """
    from botocore.exceptions import ClientError

    svc = _build_service(with_client=True)
    keys = [f"videos/u1/{c}.mp4" for c in ("a", "b", "c", "d", "e")]
    svc.client.list_objects_v2.return_value = {  # type: ignore[union-attr]
        "Contents": [{"Key": k, "Size": i + 1} for i, k in enumerate(keys)]
    }

    def head_by_key(**kwargs: Any) -> dict[str, Any]:
        if kwargs["Key"] == "videos/u1/c.mp4":
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "HeadObject")
        return {"Metadata": {"author": Path(kwargs["Key"]).stem}}

    svc.client.head_object.side_effect = head_by_key  # type: ignore[union-attr]

    results = await svc.list_objects("videos/u1/")

    assert [r["Key"] for r in results] == keys
    assert results[0]["Metadata"] == {"author": "a"}
    assert results[1]["Metadata"] == {"author": "b"}
    assert results[2]["Metadata"] == {}  # mid-list failure
    assert results[3]["Metadata"] == {"author": "d"}
    assert results[4]["Metadata"] == {"author": "e"}


@pytest.mark.asyncio
async def test_list_objects_client_error_surfaces_as_storage_error() -> None:
    from botocore.exceptions import ClientError

    svc = _build_service(with_client=True)
    svc.client.list_objects_v2.side_effect = ClientError(  # type: ignore[union-attr]
        {"Error": {"Code": "AccessDenied"}}, "ListObjectsV2"
    )

    with pytest.raises(StorageError, match="List objects failed"):
        await svc.list_objects("videos/u1/")


@pytest.mark.asyncio
async def test_upload_file_guesses_content_type(tmp_path: Path) -> None:
    svc = _build_service(with_client=True)
    f = tmp_path / "image.jpg"
    f.write_bytes(b"\xff\xd8\xff")

    svc.client.generate_presigned_url.return_value = "https://url"  # type: ignore[union-attr]

    result = await svc.upload_file(f, "images/u/image.jpg", {"category": "posts"})
    assert "image" in result["content_type"]


# ── list_objects parallel fan-out behavior ───────────────────────────────


@pytest.mark.asyncio
async def test_list_objects_preserves_order_under_variable_latency() -> None:
    """``list_objects`` returns items in ``Contents`` order regardless of
    how long each per-item ``head_object`` takes.

    Early keys are made slow and later keys fast so that, under a parallel
    fan-out, results would arrive out of order if ``_list_objects_sync``
    naively yielded in completion order.
    """
    svc = _build_service(with_client=True)
    keys = [f"videos/u1/{i:02d}.mp4" for i in range(10)]
    svc.client.list_objects_v2.return_value = {  # type: ignore[union-attr]
        "Contents": [{"Key": k, "Size": i} for i, k in enumerate(keys)]
    }

    # Early keys sleep longer than later keys; parallel execution would
    # otherwise let the fast ones finish first.
    delays = {k: (len(keys) - i) * 0.01 for i, k in enumerate(keys)}

    def head_by_key(**kwargs: Any) -> dict[str, Any]:
        key = kwargs["Key"]
        time.sleep(delays[key])
        return {"Metadata": {"k": key}}

    svc.client.head_object.side_effect = head_by_key  # type: ignore[union-attr]

    results = await svc.list_objects("videos/u1/")

    assert [r["Key"] for r in results] == keys
    assert [r["Metadata"]["k"] for r in results] == keys


@pytest.mark.asyncio
async def test_list_objects_bounded_concurrency() -> None:
    """``head_object`` fan-out never exceeds :data:`LIST_OBJECTS_HEAD_MAX_WORKERS`.

    A counter tracks in-flight calls; the peak must not exceed the cap
    even when the listing contains many more objects than workers.
    """
    svc = _build_service(with_client=True)
    # Use comfortably more than the cap so the bound is actually hit.
    keys = [f"videos/u1/{i:03d}.mp4" for i in range(LIST_OBJECTS_HEAD_MAX_WORKERS * 3)]
    svc.client.list_objects_v2.return_value = {  # type: ignore[union-attr]
        "Contents": [{"Key": k, "Size": 1} for k in keys]
    }

    lock = threading.Lock()
    in_flight = 0
    peak = 0

    def head_counting(**_: Any) -> dict[str, Any]:
        nonlocal in_flight, peak
        with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        try:
            time.sleep(0.01)
        finally:
            with lock:
                in_flight -= 1
        return {"Metadata": {}}

    svc.client.head_object.side_effect = head_counting  # type: ignore[union-attr]

    results = await svc.list_objects("videos/u1/")

    assert len(results) == len(keys)
    assert peak <= LIST_OBJECTS_HEAD_MAX_WORKERS
    # Sanity check: with this many keys the cap really was exercised.
    assert peak >= 2


@pytest.mark.asyncio
async def test_list_objects_workers_capped_for_small_listings() -> None:
    """For small listings, worker count is capped by item count, not the max.

    Exercises the ``min(max, len(objects))`` sizing behavior so a small
    page does not spin up a needlessly large pool.
    """
    svc = _build_service(with_client=True)
    keys = ["videos/u1/only.mp4"]
    svc.client.list_objects_v2.return_value = {  # type: ignore[union-attr]
        "Contents": [{"Key": k, "Size": 1} for k in keys]
    }

    lock = threading.Lock()
    seen_threads: set[str] = set()

    def head_record_thread(**_: Any) -> dict[str, Any]:
        with lock:
            seen_threads.add(threading.current_thread().name)
        return {"Metadata": {}}

    svc.client.head_object.side_effect = head_record_thread  # type: ignore[union-attr]

    results = await svc.list_objects("videos/u1/")

    assert len(results) == 1
    assert len(seen_threads) == 1


@pytest.mark.asyncio
async def test_list_objects_empty_returns_no_results() -> None:
    """An empty ``Contents`` list short-circuits without launching a pool."""
    svc = _build_service(with_client=True)
    svc.client.list_objects_v2.return_value = {"Contents": []}  # type: ignore[union-attr]

    results = await svc.list_objects("videos/u1/")

    assert results == []
    svc.client.head_object.assert_not_called()  # type: ignore[union-attr]
