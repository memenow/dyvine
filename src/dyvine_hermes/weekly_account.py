"""Resolve one weekly account's group, media, and delivery ledger."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from dyvine.db.delivery_ledger import POST_LEVEL_SLOT, post_media_slot
from dyvine.services.delivery import FeishuCredentials, FeishuGroupChannel
from dyvine.services.delivery_durable import media_identity

from .weekly_download import download_entry
from .weekly_state import (
    MIN_SEND_WINDOW_SECONDS,
    _checkpoint,
    _path_within_root,
    entry_cutoff,
    media_after_cutoff,
    patch_queue,
)
from .weekly_types import WeeklyConfig, WeeklyOutcome

MAX_FILES_PER_RUN = 20


async def _list_all_files(ledger: Any, **filters: Any) -> list[Any]:
    """Page through ``list_files`` in bounded chunks.

    Replaces the undocumented ``limit=-1`` dump the reconciliation scan
    used to depend on; per-account file sets are small, but the protocol
    never promised the sentinel.
    """
    rows: list[Any] = []
    offset = 0
    while True:
        page = await ledger.list_files(limit=500, offset=offset, **filters)
        if not page:
            return rows
        rows.extend(page)
        offset += len(page)


async def _avatar_url(engine: Any, entry: Any) -> str | None:
    saved = entry.extra.get("avatar_url")
    if isinstance(saved, str) and saved:
        return saved
    try:
        profile = await engine.profiles.get_profile(entry.sec_user_id)
    except Exception:
        try:
            profile = await engine.users.get_user_info(entry.sec_user_id)
        except Exception:
            return None
    return str(profile.avatar_url) if profile.avatar_url else None


async def _resolve_group(
    channel: Any, engine: Any, entry: Any, config: WeeklyConfig
) -> Any:
    group = await engine.delivery_ledger.get_group(
        round=entry.round, sec_user_id=entry.sec_user_id
    )
    if entry.chat_id and (group is None or group.chat_id != entry.chat_id):
        raise ValueError("legacy chat has no matching reconciled group ledger")
    if group is None:
        group = await engine.delivery_ledger.adopt_prior_verified_group_for_round(
            round=entry.round,
            sec_user_id=entry.sec_user_id,
            nickname=entry.nickname,
            owner_open_id=config.owner_open_id,
        )
        if group is not None:
            if (
                group.status != "ready"
                or not group.chat_id
                or group.topic_status != "ready"
                or not group.topic_message_id
            ):
                raise ValueError("adopted group or topic is not verified")
            entry = await patch_queue(
                engine, entry, status="downloading", chat_id=group.chat_id
            )
            return entry, group
    avatar = await _avatar_url(engine, entry) if group is None else None
    if group is None and not avatar:
        raise ValueError("author avatar is unavailable for new group")
    group = await channel.ensure_group(
        ledger=engine.delivery_ledger,
        round=entry.round,
        sec_user_id=entry.sec_user_id,
        nickname=entry.nickname,
        owner_open_id=config.owner_open_id,
        avatar_url=avatar,
    )
    if group.status != "ready" or not group.chat_id:
        raise ValueError("group creation requires reconciliation")
    if entry.chat_id != group.chat_id:
        entry = await patch_queue(
            engine, entry, status="downloading", chat_id=group.chat_id
        )
    topic = await channel.ensure_topic(
        ledger=engine.delivery_ledger,
        round=entry.round,
        sec_user_id=entry.sec_user_id,
        chat_id=group.chat_id,
        nickname=entry.nickname,
        homepage=entry.homepage or f"https://www.douyin.com/user/{entry.sec_user_id}",
    )
    if topic.topic_status != "ready" or not topic.topic_message_id:
        raise ValueError("topic creation requires reconciliation")
    return entry, topic


def _media_candidates(entry: Any, user_dir: Path, timezone: str) -> list[Path]:
    cutoff = entry_cutoff(entry, timezone)
    if not user_dir.is_dir():
        raise ValueError("recorded download directory is missing")
    return media_after_cutoff(user_dir, cutoff)


async def _deliver(
    engine: Any, entry: Any, config: WeeklyConfig, channel: Any, deadline: float
) -> WeeklyOutcome:
    checkpoint = _checkpoint(entry)
    user_dir = _path_within_root(checkpoint["user_dir"], config.download_root)
    files = _media_candidates(entry, user_dir, config.timezone)
    if not files and checkpoint.get("downloaded_posts", 0):
        raise ValueError("download reported new posts but no eligible media exists")
    candidate_by_path = {path.relative_to(user_dir).as_posix(): path for path in files}
    content_hashes: dict[str, str] = {}

    async def same_content(record: Any) -> bool:
        relative = record.relative_path
        content_sha256 = getattr(record, "content_sha256", None)
        if relative not in candidate_by_path or not isinstance(content_sha256, str):
            return False
        if relative not in content_hashes:
            # Hash off the loop: the chunked read walks up to 29 MB per
            # file, which would stall the runner's event loop.
            content_hashes[relative] = (
                await asyncio.to_thread(
                    media_identity,
                    sec_user_id=entry.sec_user_id,
                    user_dir=user_dir,
                    file_path=candidate_by_path[relative],
                )
            )[2]
        return content_hashes[relative] == content_sha256

    prior_records = await _list_all_files(
        engine.delivery_ledger, round=entry.round, sec_user_id=entry.sec_user_id
    )
    if any(record.status == "needs_review" for record in prior_records):
        raise ValueError("File ledger requires reconciliation")
    sending_paths = {
        record.relative_path for record in prior_records if record.status == "sending"
    }
    unresolved = [
        record
        for record in prior_records
        if record.status not in {"sent", "permanent_failure", "legacy_confirmed_sent"}
    ]
    for record in unresolved:
        if record.relative_path not in candidate_by_path or (
            getattr(record, "content_sha256", None) and not await same_content(record)
        ):
            raise ValueError("Unresolved file is missing or changed")
    legacy_records = await _list_all_files(
        engine.delivery_ledger,
        sec_user_id=entry.sec_user_id,
        status="legacy_confirmed_sent",
    )
    sent_history = await _list_all_files(
        engine.delivery_ledger, sec_user_id=entry.sec_user_id, status="sent"
    )
    matching_sent = set()
    for record in sent_history:
        if await same_content(record):
            matching_sent.add(record.relative_path)
    changed_sent = {
        record.relative_path
        for record in sent_history
        if record.relative_path in candidate_by_path
    } - matching_sent
    if changed_sent:
        raise ValueError("Previously sent file path has changed content")
    legacy_failures = await _list_all_files(
        engine.delivery_ledger,
        round="legacy",
        sec_user_id=entry.sec_user_id,
        status="permanent_failure",
    )
    failure_history = await _list_all_files(
        engine.delivery_ledger,
        sec_user_id=entry.sec_user_id,
        status="permanent_failure",
    )
    resolved = {record.relative_path for record in legacy_records}
    resolved.update(matching_sent)
    failed_paths = set()
    for record in failure_history:
        if await same_content(record):
            failed_paths.add(record.relative_path)
    failed_paths.update(
        record.relative_path
        for record in legacy_failures
        if record.relative_path in candidate_by_path
    )
    resolved.update(failed_paths)
    # The ledger answers a send with an earlier record of the same post media:
    # a send under an edited caption, or a post-level adoption covering every
    # slot of its post. Settle those files here, so they never take the run's
    # send budget from media that still needs a send.
    covering = {
        slot
        for record in (*legacy_records, *sent_history)
        if (slot := post_media_slot(record.relative_path)) is not None
    }
    resolved.update(
        relative
        for relative in candidate_by_path
        if (slot := post_media_slot(relative)) is not None
        and (slot in covering or (slot[0], POST_LEVEL_SLOT) in covering)
    )
    if any(
        record.status in {"sent", "permanent_failure"}
        and not getattr(record, "content_sha256", None)
        and getattr(record, "round", None) != "legacy"
        for record in prior_records
    ):
        raise ValueError("Current-round terminal file lacks a content identity")
    active = [
        path for path in files if path.relative_to(user_dir).as_posix() not in resolved
    ]
    if not active:
        status = "permanent_failure" if failed_paths else "completed"
        message = (
            f"Weekly delivery has {len(failed_paths)} permanent file failure(s)"
            if failed_paths
            else "No unsent media"
        )
        await patch_queue(engine, entry, status=status, op_message=message)
        return WeeklyOutcome(status, entry.round, entry.key)
    if deadline - time.monotonic() < MIN_SEND_WINDOW_SECONDS:
        await patch_queue(engine, entry, status="pending")
        return WeeklyOutcome("pending", entry.round, entry.key)
    if channel is None:
        channel = FeishuGroupChannel(FeishuCredentials.from_hermes_default())
    entry, group = await _resolve_group(channel, engine, entry, config)
    if sending_paths:
        active = sorted(
            active,
            key=lambda path: path.relative_to(user_dir).as_posix() not in sending_paths,
        )
    sent_now = 0
    processed_now = 0
    returned_resolved: set[str] = set()
    for path in active:
        if (
            processed_now >= MAX_FILES_PER_RUN
            or deadline - time.monotonic() < MIN_SEND_WINDOW_SECONDS
        ):
            break
        record = await channel.deliver_file(
            ledger=engine.delivery_ledger,
            round=entry.round,
            sec_user_id=entry.sec_user_id,
            user_dir=user_dir,
            file_path=path,
            chat_id=group.chat_id,
        )
        processed_now += 1
        if record.status == "needs_review":
            await patch_queue(
                engine,
                entry,
                status="needs_review",
                op_message="File send requires reconciliation",
            )
            return WeeklyOutcome(
                "needs_review",
                entry.round,
                entry.key,
                sent_now,
                processed=processed_now,
            )
        if record.status == "sending":
            await patch_queue(
                engine, entry, status="pending", op_message="Retrying send intent"
            )
            return WeeklyOutcome(
                "pending",
                entry.round,
                entry.key,
                sent_now,
                note="send_intent",
                processed=processed_now,
            )
        relative = path.relative_to(user_dir).as_posix()
        # A renamed re-download returns the earlier send of the same media.
        if record.status == "sent" and record.relative_path == relative:
            sent_now += 1
        if record.status == "permanent_failure":
            failed_paths.add(relative)
        if record.status in {"sent", "permanent_failure", "legacy_confirmed_sent"}:
            returned_resolved.add(relative)
    ledger_files = await _list_all_files(
        engine.delivery_ledger, round=entry.round, sec_user_id=entry.sec_user_id
    )
    if any(record.status == "needs_review" for record in ledger_files):
        await patch_queue(
            engine,
            entry,
            status="needs_review",
            op_message="File ledger requires reconciliation",
        )
        return WeeklyOutcome(
            "needs_review", entry.round, entry.key, sent_now, processed=processed_now
        )
    if any(record.status == "sending" for record in ledger_files):
        await patch_queue(
            engine, entry, status="pending", op_message="Retrying send intent"
        )
        return WeeklyOutcome(
            "pending",
            entry.round,
            entry.key,
            sent_now,
            note="send_intent",
            processed=processed_now,
        )
    resolved_paths = resolved | returned_resolved
    remaining = [
        path
        for path in files
        if path.relative_to(user_dir).as_posix() not in resolved_paths
    ]
    if remaining:
        await patch_queue(engine, entry, status="pending")
        return WeeklyOutcome(
            "pending", entry.round, entry.key, sent_now, processed=processed_now
        )
    for record in ledger_files:
        if record.status == "permanent_failure" and await same_content(record):
            failed_paths.add(record.relative_path)
    if failed_paths:
        await patch_queue(
            engine,
            entry,
            status="permanent_failure",
            op_message=(
                f"Weekly delivery has {len(failed_paths)} permanent file failure(s)"
            ),
        )
        return WeeklyOutcome(
            "permanent_failure",
            entry.round,
            entry.key,
            sent_now,
            processed=processed_now,
        )
    await patch_queue(
        engine, entry, status="completed", op_message="Weekly delivery verified"
    )
    return WeeklyOutcome(
        "completed", entry.round, entry.key, sent_now, processed=processed_now
    )


async def process_entry(
    engine: Any, entry: Any, config: WeeklyConfig, deadline: float, channel: Any
) -> WeeklyOutcome:
    if entry.extra.get("migration_needs_reconciliation"):
        raise ValueError("migrated entry has unresolved historical delivery")
    if entry.chat_id:
        group = await engine.delivery_ledger.get_group(
            round=entry.round, sec_user_id=entry.sec_user_id
        )
        if group is None or group.chat_id != entry.chat_id:
            raise ValueError("legacy chat has no matching reconciled group ledger")
    entry = await download_entry(engine, entry, config, deadline)
    if entry.status == "skipped":
        return WeeklyOutcome(
            "skipped", entry.round, entry.key, note="author_unavailable"
        )
    if entry.status == "op_issue":
        # Carry the cause: without the note the CLI alert names only the
        # status, and the recorded reason stays buried in the queue row.
        return WeeklyOutcome("op_issue", entry.round, entry.key, note=entry.op_message)
    if entry.status == "pending":
        note = (
            "full_continuation"
            if entry.op_message == "Continuing full download from saved page"
            else None
        )
        return WeeklyOutcome("pending", entry.round, entry.key, note=note)
    return await _deliver(engine, entry, config, channel, deadline)
