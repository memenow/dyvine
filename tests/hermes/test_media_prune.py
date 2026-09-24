"""Media pruning touches only settled accounts' old files inside the root."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dyvine.core.settings import settings
from dyvine.services.queue import QueueService
from dyvine_hermes import context, media_prune
from dyvine_hermes.media_prune import prune_settled_media
from tests.fake_repos import FakeQueueRepository

NOW = 2_000_000_000.0
OLD = NOW - 20 * 86400
FRESH = NOW - 3 * 86400


def _media(folder: Path, stamp: str, mtime: float) -> Path:
    post = folder / f"{stamp}_clip"
    post.mkdir(parents=True, exist_ok=True)
    path = post / f"{stamp}_clip_video.mp4"
    path.write_bytes(b"video")
    os.utime(path, (mtime, mtime))
    return path


async def _row(
    repo: FakeQueueRepository,
    round_name: str,
    sec: str,
    status: str,
    user_dir: Path | None,
) -> None:
    extra = {"weekly": {"user_dir": str(user_dir)}} if user_dir else {}
    await repo.upsert_entry(
        key=f"{round_name}:{sec}",
        round=round_name,
        nickname=sec,
        sec_user_id=sec,
        mode="incremental",
        status=status,
        extra=extra,
    )


def _engine(repo: FakeQueueRepository) -> SimpleNamespace:
    return SimpleNamespace(
        queue=QueueService(queue=repo, seeds=AsyncMock(), rounds=AsyncMock())
    )


async def test_prune_deletes_only_old_media_of_settled_accounts(
    tmp_path: Path,
) -> None:
    root = tmp_path / "downloads"
    settled, busy = root / "douyin" / "settled", root / "douyin" / "busy"
    old = _media(settled, "2026-09-01 08-00-00", OLD)
    fresh = _media(settled, "2026-09-20 08-00-00", FRESH)
    busy_old = _media(busy, "2026-09-01 08-00-00", OLD)
    outside = _media(tmp_path / "elsewhere", "2026-09-01 08-00-00", OLD)
    repo = FakeQueueRepository()
    await _row(repo, "weekly0906", "sec_settled", "completed", settled)
    await _row(repo, "weekly0913", "sec_settled", "skipped", None)
    await _row(repo, "weekly0906", "sec_busy", "completed", busy)
    await _row(repo, "weekly0913", "sec_busy", "pending", None)
    await _row(repo, "weekly0913", "sec_outside", "completed", outside.parent.parent)

    report = await prune_settled_media(
        _engine(repo), download_root=root, older_than_days=14, now=NOW
    )

    assert (report.files, report.folders, report.bytes) == (1, 1, 5)
    assert (report.accounts, report.settled_accounts) == (3, 2)
    assert not old.exists() and not old.parent.exists()
    assert fresh.exists() and busy_old.exists() and outside.exists()


async def test_a_shared_folder_stays_while_any_owner_is_unsettled(
    tmp_path: Path,
) -> None:
    root = tmp_path / "downloads"
    shared = root / "douyin" / "Same Name"
    old = _media(shared, "2026-09-01 08-00-00", OLD)
    repo = FakeQueueRepository()
    await _row(repo, "weekly0913", "sec_a", "completed", shared)
    await _row(repo, "weekly0913", "sec_b", "needs_review", shared)

    report = await prune_settled_media(
        _engine(repo), download_root=root, older_than_days=14, now=NOW
    )

    assert report.files == 0
    assert old.exists()


async def test_dry_run_reports_without_deleting(tmp_path: Path) -> None:
    root = tmp_path / "downloads"
    folder = root / "douyin" / "settled"
    old = _media(folder, "2026-09-01 08-00-00", OLD)
    repo = FakeQueueRepository()
    await _row(repo, "weekly0913", "sec_a", "permanent_failure", folder)

    report = await prune_settled_media(
        _engine(repo), download_root=root, older_than_days=14, dry_run=True, now=NOW
    )

    assert (report.files, report.dry_run) == (1, True)
    assert old.exists()


async def test_retention_below_one_day_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least one day"):
        await prune_settled_media(
            _engine(FakeQueueRepository()), download_root=tmp_path, older_than_days=0.5
        )


def test_cli_routes_the_prune_command(monkeypatch: pytest.MonkeyPatch) -> None:
    from dyvine_hermes.plugin import _handle_cli, _setup_cli

    parser = argparse.ArgumentParser()
    _setup_cli(parser)
    args = parser.parse_args(["media", "prune", "--older-than-days", "21", "--dry-run"])
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(media_prune, "prune_cli", lambda **kwargs: calls.append(kwargs))

    _handle_cli(args)

    assert args.func is _handle_cli
    assert calls == [{"older_than_days": 21.0, "dry_run": True}]


def test_cli_prints_the_pass_and_skips_while_the_runner_holds_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / "downloads"
    folder = root / "douyin" / "settled"
    old = _media(folder, "2026-09-01 08-00-00", 1.0)
    repo = FakeQueueRepository()
    closed = AsyncMock()
    monkeypatch.setattr(context, "get_engine", lambda: _engine(repo))
    monkeypatch.setattr(context, "close_engine", closed)
    monkeypatch.setattr(settings.douyin, "download_root", str(root))

    asyncio.run(_row(repo, "weekly0913", "sec_a", "completed", folder))
    media_prune.prune_cli(older_than_days=14, dry_run=True)
    report = json.loads(capsys.readouterr().out)
    assert (report["files"], report["dry_run"]) == (1, True)
    assert old.exists()
    closed.assert_awaited_once()

    lock = tmp_path / ".hermes" / "dyvine-weekly.lock"
    with lock.open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        media_prune.prune_cli(older_than_days=14, dry_run=False)
    assert json.loads(capsys.readouterr().out) == {"skipped": "weekly runner is active"}
    assert old.exists()
