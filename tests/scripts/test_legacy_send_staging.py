"""Pure parsing checks for legacy progress and optional sources."""

from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts import legacy_extra_staging as extra  # noqa: E402
from scripts import legacy_send_staging as staging  # noqa: E402

DOWNLOAD_ROOT = PurePosixPath("/opt/dyvine/data/douyin/downloads")


def _load_script() -> SimpleNamespace:
    return SimpleNamespace(staging=staging, extra=extra)


def _path(media_type: str, nickname: str, filename: str) -> str:
    return str(
        DOWNLOAD_ROOT / "douyin" / media_type / nickname / "2026-09-13_post" / filename
    )


def test_json_stream_handles_chunk_boundaries_and_unicode() -> None:
    script = _load_script()
    document = {
        "users": {"中": {"chat_id": "hidden"}},
        "sent": ["/path/\u4e2d.mp4", "/path/a\\b.mp4"],
        "failed": ["/path/b.mp4"],
        "completed": True,
    }
    source = StringIO(json.dumps(document, ensure_ascii=True))

    assert list(script.staging.JsonStream(source, chunk_size=7).paths()) == [
        ("sent", "/path/\u4e2d.mp4", 0),
        ("sent", "/path/a\\b.mp4", 1),
        ("failed", "/path/b.mp4", 0),
    ]


def test_path_identity_rejects_unsafe_or_unrecognised_paths() -> None:
    script = _load_script()
    valid = script.staging._identity(_path("post", "Alpha", "one.mp4"), DOWNLOAD_ROOT)
    assert valid == script.staging.PathIdentity(
        "Alpha", "post/Alpha", "2026-09-13_post/one.mp4"
    )
    assert isinstance(script.staging._identity("/outside/a.mp4", DOWNLOAD_ROOT), str)
    assert isinstance(
        script.staging._identity(
            "/opt/dyvine/data/douyin/downloads/../escape/file.mp4", DOWNLOAD_ROOT
        ),
        str,
    )


def test_stream_rejects_incomplete_document() -> None:
    script = _load_script()
    with pytest.raises(ValueError, match="requires sent and failed"):
        list(script.staging.JsonStream(StringIO('{"sent":[]}')).paths())


def test_extra_sources_stream_nested_cache_paths_across_small_chunks() -> None:
    script = _load_script()
    source = "/missing/send_progress_weekly0913_w999_L0.json"
    cache = {"files": {source: {"m": 1.5, "s": 123, "delta": ["a", "b"]}}}
    parser = script.staging.JsonStream(StringIO(json.dumps(cache)), chunk_size=5)
    assert list(script.extra.index_paths(parser)) == [
        (source, "a", 0),
        (source, "b", 1),
    ]
    assert list(
        script.extra.array_values(script.staging.JsonStream(StringIO('["x", "y"]'), 2))
    ) == [
        ("x", 0),
        ("y", 1),
    ]
