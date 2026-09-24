"""Delete settled accounts' local media once it is old enough.

Local-retention mode keeps every download, so the download root only grows.
Weekly delivery reads only media posted after a round's cutoff, and a settled
row (completed, permanent_failure, or skipped) is never delivered again, so an
account whose every queue row is settled never reads its old files. Media
older than the retention age in the folders its checkpoints record is
deleted. A folder that any unsettled row records is never touched, even when
another account shares it, and nothing outside DOUYIN_DOWNLOAD_ROOT is.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from dyvine.services.delivery import scan_media_files

from .weekly_state import _checkpoint, _path_within_root

SETTLED_STATUSES = frozenset({"completed", "permanent_failure", "skipped"})
DEFAULT_RETENTION_DAYS = 14.0


@dataclass(slots=True)
class PruneReport:
    """What one prune pass found and removed (or would remove on a dry run)."""

    accounts: int = 0
    settled_accounts: int = 0
    folders: int = 0
    files: int = 0
    bytes: int = 0
    dry_run: bool = False


def _remove_empty_dirs(folder: Path) -> None:
    for path in sorted(
        folder.rglob("*"), key=lambda item: len(item.parts), reverse=True
    ):
        if path.is_dir() and not path.is_symlink() and not any(path.iterdir()):
            path.rmdir()


async def prune_settled_media(
    engine: Any,
    *,
    download_root: Path,
    older_than_days: float = DEFAULT_RETENTION_DAYS,
    dry_run: bool = False,
    now: float | None = None,
) -> PruneReport:
    """Delete settled accounts' media older than ``older_than_days``."""
    if older_than_days < 1:
        raise ValueError("media retention must be at least one day")
    entries = await engine.queue.list_entries(limit=-1)
    accounts = {entry.sec_user_id for entry in entries}
    unsettled = {
        entry.sec_user_id for entry in entries if entry.status not in SETTLED_STATUSES
    }
    root = download_root.expanduser().resolve()
    folders: dict[Path, set[str]] = defaultdict(set)
    for entry in entries:
        saved = _checkpoint(entry).get("user_dir")
        if not isinstance(saved, str) or not saved:
            continue
        try:
            folder = _path_within_root(saved, root)
        except ValueError:
            continue
        if folder != root:
            folders[folder].add(entry.sec_user_id)
    report = PruneReport(
        accounts=len(accounts),
        settled_accounts=len(accounts - unsettled),
        dry_run=dry_run,
    )
    oldest_kept = (time.time() if now is None else now) - older_than_days * 86400
    for folder, owners in sorted(folders.items()):
        if owners & unsettled or not folder.is_dir():
            continue
        removed = False
        for path in scan_media_files(folder):
            details = path.stat()
            if details.st_mtime >= oldest_kept:
                continue
            report.files += 1
            report.bytes += details.st_size
            removed = True
            if not dry_run:
                path.unlink()
        if removed:
            report.folders += 1
            if not dry_run:
                _remove_empty_dirs(folder)
    return report


def prune_cli(*, older_than_days: float, dry_run: bool) -> None:
    """Synchronous Hermes command handler; prints the pass summary as JSON."""
    from dotenv import load_dotenv

    # Hermes's env must be loaded before settings or the engine read it.
    load_dotenv(Path.home() / ".hermes" / ".env", override=False)
    from dyvine.core.settings import settings
    from dyvine_hermes.context import close_engine, get_engine
    from dyvine_hermes.weekly import single_runner_lock

    async def execute() -> PruneReport:
        engine = get_engine()
        try:
            return await prune_settled_media(
                engine,
                download_root=Path(settings.douyin.download_root),
                older_than_days=older_than_days,
                dry_run=dry_run,
            )
        finally:
            await close_engine()

    try:
        # The weekly runner's lock keeps a prune from racing a delivery step.
        with single_runner_lock() as acquired:
            if not acquired:
                print(json.dumps({"skipped": "weekly runner is active"}))
                return
            report = asyncio.run(execute())
        print(json.dumps(asdict(report)))
    except Exception as error:
        print(f"dyvine media prune failed: {type(error).__name__}", file=sys.stderr)
        raise SystemExit(1) from error
