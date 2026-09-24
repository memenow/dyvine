"""Awaited incremental and full downloads for one weekly queue entry."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from dyvine.services.delivery import MEDIA_EXTS

from .weekly_state import _checkpoint, _path_within_root, patch_queue
from .weekly_types import WeeklyConfig


def _has_local_media(user_dir: Path) -> bool:
    return user_dir.is_dir() and any(
        path.is_file() and path.suffix.lower() in MEDIA_EXTS
        for path in user_dir.rglob("*")
    )


async def download_entry(
    engine: Any, entry: Any, config: WeeklyConfig, deadline: float
) -> Any:
    checkpoint = _checkpoint(entry)
    reconciliation = entry.extra.get("reconciliation")
    # Attested cutover releases proved what the chat already holds, not that
    # any legacy download or operation still describes the media on disk.
    attested_recheck = bool(
        isinstance(reconciliation, dict)
        and reconciliation.get("action")
        in {"release_pending_group_attested", "release_pending_window_attested"}
        and not checkpoint.get("fresh_download_confirmed")
    )
    cutover_entry = entry.round == config.cutover_round
    saved_dir = checkpoint.get("user_dir")
    local_media = bool(
        cutover_entry
        and isinstance(saved_dir, str)
        and _has_local_media(_path_within_root(saved_dir, config.download_root))
    )
    force_download = attested_recheck or bool(
        cutover_entry
        and checkpoint.get("download_complete")
        and not local_media
        and not checkpoint.get("fresh_download_confirmed")
    )
    if checkpoint.get("download_complete") and not force_download:
        return entry
    previous = None
    if entry.operation_id and not attested_recheck:
        previous = await engine.operations.get_operation(entry.operation_id)
        expected_type = (
            "user_posts_bulk_download"
            if entry.mode in {"full", "post"}
            else "user_posts_incremental_download"
        )
        if (
            previous.subject_id != entry.sec_user_id
            or previous.operation_type != expected_type
        ):
            raise ValueError("recorded download operation does not match the account")
        if previous.status in {"completed", "partial"}:
            failed_count = int(previous.metadata.get("failed_count") or 0)
            interrupted = "interrupted by upstream error" in (previous.message or "")
            truncated = bool(previous.metadata.get("truncated"))
            cursor_stalled = bool(previous.metadata.get("cursor_stalled"))
            has_more = previous.metadata.get("resume_cursor") is not None
            previous_dir = (
                _path_within_root(previous.download_path, config.download_root)
                if previous.download_path
                else None
            )
            previous_has_media = bool(previous_dir and _has_local_media(previous_dir))
            if (
                cutover_entry
                and not previous_has_media
                and not checkpoint.get("fresh_download_confirmed")
            ):
                force_download = True
            if (
                not failed_count
                and not interrupted
                and not truncated
                and not cursor_stalled
                and not has_more
                and previous_dir
                and not force_download
                and (
                    not cutover_entry
                    or previous_has_media
                    or checkpoint.get("fresh_download_confirmed")
                )
            ):
                checkpoint.update(
                    download_complete=True,
                    user_dir=str(previous_dir),
                    downloaded_posts=int(
                        previous.metadata.get("new_count")
                        or previous.completed_items
                        or 0
                    ),
                )
                return await patch_queue(
                    engine,
                    entry,
                    status="downloading",
                    extra={**entry.extra, "weekly": checkpoint},
                )
        if previous.status in {"pending", "running"}:
            if entry.attempts == 0:
                raise ValueError("previous download operation is still active")
            await engine.operations.update_operation(
                previous.operation_id,
                status="failed",
                message="Recovered after stale weekly claim",
                error="previous runner stopped",
            )
    if entry.mode in {"full", "post"}:
        return await _download_full(
            engine, entry, config, deadline, previous, restart_from_zero=force_download
        )
    if entry.mode != "incremental":
        raise ValueError(f"weekly runner cannot inline-download mode {entry.mode!r}")
    prior = checkpoint.get("since_aweme_id") or entry.extra.get("since_aweme_id")
    since = prior if isinstance(prior, str) and prior and not force_download else None
    remaining = deadline - time.monotonic()
    if remaining <= 30:
        return await patch_queue(engine, entry, status="pending", extra=entry.extra)
    operation = await engine.operations.create_operation(
        operation_type="user_posts_incremental_download",
        subject_id=entry.sec_user_id,
        status="pending",
        message="Inline incremental download scheduled",
        progress=0.0,
        metadata={"since_aweme_id": since},
    )
    entry = await patch_queue(
        engine,
        entry,
        status="downloading",
        operation_id=operation.operation_id,
        op_status="pending",
    )
    result = await asyncio.wait_for(
        engine.posts.download_new_posts(
            entry.sec_user_id,
            since_aweme_id=since,
            operation_id=operation.operation_id,
        ),
        timeout=remaining,
    )
    operation = await engine.operations.get_operation(result.operation_id)
    if (
        result.failed_count
        or result.truncated
        or operation.status not in {"completed", "partial"}
    ):
        return await patch_queue(
            engine,
            entry,
            status="op_issue",
            operation_id=result.operation_id,
            op_status=operation.status,
            op_message="Incremental download is incomplete; inspect before delivery",
        )
    if not operation.download_path:
        raise ValueError("completed download has no recorded directory")
    checkpoint.update(
        download_complete=True,
        fresh_download_confirmed=True,
        user_dir=str(_path_within_root(operation.download_path, config.download_root)),
        downloaded_posts=result.new_count,
        newest_aweme_id=result.newest_aweme_id,
    )
    return await patch_queue(
        engine,
        entry,
        status="downloading",
        operation_id=result.operation_id,
        op_status=operation.status,
        op_message=operation.message,
        extra={**entry.extra, "weekly": checkpoint},
    )


async def _download_full(
    engine: Any,
    entry: Any,
    config: WeeklyConfig,
    deadline: float,
    previous: Any,
    *,
    restart_from_zero: bool,
) -> Any:
    """Run the complete post feed inline, resuming only after a saved page."""
    cursor = 0
    if (
        previous is not None
        and not restart_from_zero
        and not previous.metadata.get("failed_count")
    ):
        saved = previous.metadata.get("resume_cursor")
        if isinstance(saved, int) and saved >= 0:
            cursor = saved
    remaining = deadline - time.monotonic()
    if remaining <= 30:
        return await patch_queue(engine, entry, status="pending")
    operation = await engine.operations.create_operation(
        operation_type="user_posts_bulk_download",
        subject_id=entry.sec_user_id,
        status="pending",
        message="Inline full download scheduled",
        progress=0.0,
        metadata={"max_cursor": cursor, "mode": "post"},
    )
    entry = await patch_queue(
        engine,
        entry,
        status="downloading",
        operation_id=operation.operation_id,
        op_status="pending",
    )
    await asyncio.wait_for(
        engine.posts.download_bulk_inline(
            entry.sec_user_id,
            operation_id=operation.operation_id,
            max_cursor=cursor,
            mode="post",
        ),
        timeout=remaining,
    )
    finished = await engine.operations.get_operation(operation.operation_id)
    failed_count = int(finished.metadata.get("failed_count") or 0)
    interrupted = "interrupted by upstream error" in (finished.message or "")
    resume = finished.metadata.get("resume_cursor")
    if finished.metadata.get("cursor_stalled"):
        return await patch_queue(
            engine,
            entry,
            status="op_issue",
            op_status=finished.status,
            op_message="Full download cursor stalled; inspect before delivery",
        )
    if failed_count:
        return await patch_queue(
            engine,
            entry,
            status="op_issue",
            op_status=finished.status,
            op_message="Full download has failed posts; inspect before delivery",
        )
    if interrupted or finished.status == "failed":
        next_attempt = entry.attempts + 1
        retry_status = "pending" if next_attempt < 8 else "op_issue"
        return await patch_queue(
            engine,
            entry,
            status=retry_status,
            attempts=next_attempt,
            op_status=finished.status,
            op_message="Interrupted full download requires another attempt",
        )
    if resume is not None:
        return await patch_queue(
            engine,
            entry,
            status="pending",
            op_status=finished.status,
            op_message="Continuing full download from saved page",
        )
    if finished.status not in {"completed", "partial"} or not finished.download_path:
        raise ValueError("full download has no complete operation or directory")
    checkpoint = _checkpoint(entry)
    checkpoint.update(
        download_complete=True,
        fresh_download_confirmed=True,
        user_dir=str(_path_within_root(finished.download_path, config.download_root)),
        downloaded_posts=int(finished.completed_items or 0),
    )
    return await patch_queue(
        engine,
        entry,
        status="downloading",
        op_status=finished.status,
        op_message=finished.message,
        extra={**entry.extra, "weekly": checkpoint},
    )
