"""Durable queue and path helpers shared by weekly download and delivery."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dyvine.services.delivery import post_datetime_from_path, scan_media_files


def _checkpoint(entry: Any) -> dict[str, Any]:
    value = entry.extra.get("weekly", {})
    return dict(value) if isinstance(value, dict) else {}


async def patch_queue(engine: Any, entry: Any, *, status: str, **fields: Any) -> Any:
    return await engine.queue.report_progress(entry.key, status=status, **fields)


#: Minimum run budget (seconds) reserved for the send phase. Downloads
#: stop early when less remains, and delivery requeues instead of
#: starting a send it cannot finish inside the run.
MIN_SEND_WINDOW_SECONDS = 330.0

#: Minimum remaining budget (seconds) worth starting a download slice
#: for; below this the entry requeues immediately.
MIN_DOWNLOAD_SLICE_SECONDS = 30.0


def parse_cutoff(text: str, *, timezone: str | None = None) -> datetime:
    """Parse an ISO cutoff into naive filename-stamp time.

    On-disk post stamps are naive wall-clock directory names, so the
    comparison value must be naive too: naive input passes through
    verbatim, aware input converts through ``timezone`` (required in
    that case) and drops the offset. Garbage raises ``ValueError``
    instead of degrading to ``None`` (which would silently switch an
    incremental delivery to a full one).
    """
    parsed = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed
    if not timezone:
        raise ValueError("cutoff carries an offset but no timezone was provided")
    return parsed.astimezone(ZoneInfo(timezone)).replace(tzinfo=None)


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


def media_after_cutoff(user_dir: Path, cutoff: datetime | None) -> list[Path]:
    """List the media a window covers: posts after ``cutoff``, or all without one.

    A path with no parseable post time counts as posted before the cutoff.
    """
    files: list[Path] = []
    for path in scan_media_files(user_dir):
        if cutoff is not None:
            posted = post_datetime_from_path(path, user_dir)
            if posted is None or posted <= cutoff:
                continue
        files.append(path)
    return files
