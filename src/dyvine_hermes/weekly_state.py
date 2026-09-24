"""Durable queue and path helpers shared by weekly download and delivery."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


def _checkpoint(entry: Any) -> dict[str, Any]:
    value = entry.extra.get("weekly", {})
    return dict(value) if isinstance(value, dict) else {}


async def patch_queue(engine: Any, entry: Any, *, status: str, **fields: Any) -> Any:
    return await engine.queue.report_progress(entry.key, status=status, **fields)


def _path_within_root(path: str | Path, root: Path) -> Path:
    base = root.resolve()
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    resolved = candidate.resolve()
    if not resolved.is_relative_to(base):
        raise ValueError("download path falls outside DOUYIN_DOWNLOAD_ROOT")
    return resolved


def entry_cutoff(entry: Any, timezone: str) -> datetime | None:
    """Return the queue cutoff as the runner compares it: naive ``timezone`` time.

    Media folder names carry f2's post time in the same local form, so
    delivery and download both compare against this value.
    """
    if not entry.cutoff:
        return None
    parsed = datetime.fromisoformat(entry.cutoff.replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(ZoneInfo(timezone))
    return parsed.replace(tzinfo=None)
