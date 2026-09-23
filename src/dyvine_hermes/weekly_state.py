"""Durable queue and path helpers shared by weekly download and delivery."""

from __future__ import annotations

from pathlib import Path
from typing import Any


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
