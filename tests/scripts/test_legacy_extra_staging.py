"""Checkpoint rules for optional legacy evidence sources."""

from __future__ import annotations

import ast
import json
import sqlite3
import sys
from pathlib import Path, PurePosixPath

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts import legacy_extra_staging as extra  # noqa: E402
from scripts import legacy_send_staging as staging  # noqa: E402

DOWNLOAD_ROOT = PurePosixPath("/opt/dyvine/data/douyin/downloads")


def _path(nickname: str, filename: str) -> str:
    return str(
        DOWNLOAD_ROOT / "douyin" / "post" / nickname / "2026-09-13_post" / filename
    )


def _connection(tmp_path: Path) -> sqlite3.Connection:
    return staging._work_database(tmp_path / "work.sqlite3")


def test_unknown_kind_is_rejected_without_staging_rows(tmp_path: Path) -> None:
    cache_file = tmp_path / "weird.json"
    cache_file.write_text(
        json.dumps(
            {
                "files": {
                    "/missing/send_progress_weekly0913_w999_L0.json": {
                        "delta": [_path("Alpha", "x.mp4")]
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    connection = _connection(tmp_path)
    try:
        with pytest.raises(ValueError, match="unknown extra source kind"):
            extra.stage_extras(
                connection,
                {"bogus_kind_typo": cache_file},
                {"Alpha": {"sec-alpha"}},
                DOWNLOAD_ROOT,
            )
        assert connection.execute("SELECT COUNT(*) FROM cache_paths").fetchone()[0] == 0
        assert (
            connection.execute("SELECT COUNT(*) FROM extra_sources").fetchone()[0] == 0
        )
    finally:
        connection.close()


def test_known_kinds_stage_into_their_own_tables(tmp_path: Path) -> None:
    permanent_file = tmp_path / "permanent_failures.json"
    permanent_file.write_text(
        json.dumps([_path("Alpha", "gone.mp4"), 42]), encoding="utf-8"
    )
    cache_file = tmp_path / "report_sent_index.json"
    cache_file.write_text(
        json.dumps(
            {
                "files": {
                    "/missing/send_progress_weekly0913_w999_L0.json": {
                        "delta": [_path("Alpha", "cached.mp4")]
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    connection = _connection(tmp_path)
    try:
        extra.stage_extras(
            connection,
            {"permanent": permanent_file, "sent_index": cache_file},
            {"Alpha": {"sec-alpha"}},
            DOWNLOAD_ROOT,
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM permanent_paths").fetchone()[0]
            == 2
        )
        assert connection.execute("SELECT COUNT(*) FROM cache_paths").fetchone()[0] == 1
        assert (
            connection.execute("SELECT COUNT(*) FROM extra_sources").fetchone()[0] == 2
        )
        report = staging.Report()
        staging._summarize(connection, report)
        extra.summarize_extras(connection, report)
        assert (report.permanent_paths, report.cache_paths) == (2, 1)
    finally:
        connection.close()


def test_checkpoint_rerun_reuses_extra_sources(tmp_path: Path) -> None:
    permanent_file = tmp_path / "permanent_failures.json"
    permanent_file.write_text(
        json.dumps([_path("Alpha", "gone.mp4")]), encoding="utf-8"
    )
    connection = _connection(tmp_path)
    try:
        mapping: dict[str, set[str]] = {"Alpha": {"sec-alpha"}}
        extra.stage_extras(
            connection, {"permanent": permanent_file}, mapping, DOWNLOAD_ROOT
        )
        extra.stage_extras(
            connection, {"permanent": permanent_file}, mapping, DOWNLOAD_ROOT
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM permanent_paths").fetchone()[0]
            == 1
        )
        assert (
            connection.execute("SELECT entries FROM extra_sources").fetchone()[0] == 1
        )
    finally:
        connection.close()


def test_script_uses_no_runtime_asserts() -> None:
    tree = ast.parse(
        (ROOT / "scripts" / "legacy_extra_staging.py").read_text(encoding="utf-8")
    )
    assert [
        node.lineno for node in ast.walk(tree) if isinstance(node, ast.Assert)
    ] == []
