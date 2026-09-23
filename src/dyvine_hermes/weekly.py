"""Single-shot scheduler and Hermes CLI adapter for weekly delivery."""

from __future__ import annotations

import asyncio
import datetime as dt
import fcntl
import json
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dyvine.core.exceptions import DeliveryError

from .weekly_account import process_entry
from .weekly_state import patch_queue
from .weekly_types import WeeklyConfig, WeeklyOutcome

MAX_RUN_SECONDS = 2700
MAX_STEPS_PER_RUN = 100
STALE_AFTER_SECONDS = 3600
MAX_ATTEMPTS = 8
_REVIEW_STATUSES = frozenset(
    {"needs_reconciliation", "needs_review", "op_issue", "pair_needs_review"}
)
_DONE_STATUSES = frozenset({"completed", "permanent_failure", "skipped"})


def due_round(now: dt.datetime, timezone: str) -> tuple[str, dt.datetime, dt.datetime]:
    """Return the most recent Sunday 08:00 and its prior-week cutoff."""
    if now.tzinfo is None:
        raise ValueError("weekly clock must be timezone-aware")
    zone = ZoneInfo(timezone)
    local = now.astimezone(zone)
    days_since_sunday = (local.weekday() + 1) % 7
    sunday = local.date() - dt.timedelta(days=days_since_sunday)
    due = dt.datetime.combine(sunday, dt.time(8), tzinfo=zone)
    if local < due:
        sunday -= dt.timedelta(days=7)
        due = dt.datetime.combine(sunday, dt.time(8), tzinfo=zone)
    previous = dt.datetime.combine(
        sunday - dt.timedelta(days=7), dt.time(8), tzinfo=zone
    )
    return f"weekly-{sunday.isoformat()}", due, previous


def _blocks_automatic_round(
    candidate: str,
    *,
    cutover_round: str,
    first_auto_date: dt.date,
    due_date: dt.date,
) -> bool:
    """Hold the active cutover round and earlier post-cutover rounds only."""
    if candidate == cutover_round:
        return True
    if not candidate.startswith("weekly-"):
        return False
    try:
        date = dt.date.fromisoformat(candidate.removeprefix("weekly-"))
    except ValueError:
        return False
    return first_auto_date <= date < due_date


@contextmanager
def single_runner_lock() -> Iterator[bool]:
    """Keep overlapping cron invocations on one host from racing to send."""
    lock_path = Path.home() / ".hermes" / "dyvine-weekly.lock"
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(lock_path, flags, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


async def _current_pair_keys(engine: Any, round_name: str) -> set[str]:
    """Restrict claims to the first unfinished pair in stable seed order."""
    rows = await engine.queue.list_entries(round=round_name, limit=-1)
    rows.sort(key=lambda row: (row.created_at, row.key))
    for offset in range(0, len(rows), 2):
        pair = rows[offset : offset + 2]
        if any(row.status not in _DONE_STATUSES for row in pair):
            return {row.key for row in pair}
    return set()


async def _advance_claimed(
    engine: Any,
    entry: Any,
    config: WeeklyConfig,
    deadline: float,
    channel: Any,
) -> WeeklyOutcome:
    """Advance one claimed row and persist a safe terminal or retry state."""
    try:
        return await asyncio.wait_for(
            process_entry(engine, entry, config, deadline, channel),
            timeout=max(0.0, deadline - time.monotonic()),
        )
    except (ValueError, KeyError) as error:
        await patch_queue(engine, entry, status="needs_review", op_message=str(error))
        return WeeklyOutcome("needs_review", entry.round, entry.key, note=str(error))
    except (TimeoutError, DeliveryError) as error:
        retryable = isinstance(error, TimeoutError) or error.reason == "retryable"
        next_attempt = entry.attempts + 1
        status = (
            "pending" if retryable and next_attempt < MAX_ATTEMPTS else "needs_review"
        )
        await patch_queue(
            engine,
            entry,
            status=status,
            attempts=next_attempt,
            op_message=f"Weekly step stopped: {type(error).__name__}",
        )
        return WeeklyOutcome(status, entry.round, entry.key)
    except Exception:
        await patch_queue(
            engine,
            entry,
            status="needs_review",
            op_message="Unexpected weekly runner failure",
        )
        raise


async def run_once(
    *,
    engine: Any,
    config: WeeklyConfig,
    round_name: str | None = None,
    dry_run: bool = False,
    now: dt.datetime | None = None,
    channel: Any = None,
) -> WeeklyOutcome:
    """Advance the current pair within the timeout; never spawn detached tasks."""
    instant = now or dt.datetime.now(tz=ZoneInfo(config.timezone))
    automatic = round_name is None
    if automatic:
        if config.first_auto_date is None:
            raise ValueError("DYVINE_WEEKLY_FIRST_AUTO_DATE is required")
        if not config.cutover_round:
            raise ValueError("DYVINE_WEEKLY_CUTOVER_ROUND is required")
        round_name, due, previous = due_round(instant, config.timezone)
        if due.date() < config.first_auto_date:
            return WeeklyOutcome("before_cutover", round_name)
        if instant < due:
            return WeeklyOutcome("not_due", round_name)
        unresolved = 0
        for header in await engine.round_repo.list_rounds():
            if not _blocks_automatic_round(
                header.round,
                cutover_round=config.cutover_round,
                first_auto_date=config.first_auto_date,
                due_date=due.date(),
            ):
                continue
            status = await engine.queue.round_status(header.round)
            unresolved += sum(
                count
                for name, count in status.by_status.items()
                if name not in _DONE_STATUSES
            )
        if unresolved:
            return WeeklyOutcome(
                "blocked_by_prior_round", round_name, note=str(unresolved)
            )
    assert round_name is not None
    if dry_run:
        pending = await engine.queue.list_entries(
            round=round_name, status="pending", limit=1
        )
        return WeeklyOutcome("dry_run", round_name, pending[0].key if pending else None)
    if automatic:
        excluded = await engine.delivery_ledger.list_excluded_nicknames()
        await engine.queue.enqueue_round(
            round_name,
            mode="incremental",
            cutoff=previous.replace(tzinfo=None).isoformat(),
            excluded_nicknames=excluded,
        )
    await engine.queue.release_stale(
        stale_after_seconds=STALE_AFTER_SECONDS,
        max_attempts=MAX_ATTEMPTS,
        round=round_name,
    )
    pair_keys = await _current_pair_keys(engine, round_name)
    if not pair_keys:
        return WeeklyOutcome("idle", round_name)
    deadline = time.monotonic() + MAX_RUN_SECONDS
    outcomes: list[WeeklyOutcome] = []
    stalled: set[str] = set()
    for _ in range(MAX_STEPS_PER_RUN):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or (outcomes and remaining < 1):
            break
        allowed = pair_keys - stalled
        if not allowed:
            break
        entry = await engine.queue.claim_next(round=round_name, keys=allowed)
        if entry is None:
            break
        outcome = await _advance_claimed(engine, entry, config, deadline, channel)
        outcomes.append(outcome)
        if outcome.status != "pending" or outcome.note == "send_intent":
            stalled.add(entry.key)
        elif (
            not outcome.processed
            and not outcome.files
            and outcome.note != "full_continuation"
        ):
            stalled.add(entry.key)
    if not outcomes:
        return WeeklyOutcome("blocked_pair", round_name)
    if len(outcomes) == 1:
        return outcomes[0]
    pair = [await engine.queue.get_entry(key) for key in sorted(pair_keys)]
    if all(entry.status in _DONE_STATUSES for entry in pair):
        status = "pair_complete"
    elif any(entry.status in _REVIEW_STATUSES for entry in pair):
        status = "pair_needs_review"
    else:
        status = "pair_progress"
    return WeeklyOutcome(
        status,
        round_name,
        files=sum(item.files for item in outcomes),
        note=f"steps={len(outcomes)}",
        processed=sum(item.processed for item in outcomes),
    )


def run_cli(round_name: str | None, dry_run: bool) -> None:
    """Synchronous Hermes command handler; stdout stays empty in normal runs."""
    from dyvine_hermes.context import close_engine, get_engine

    async def execute() -> WeeklyOutcome:
        config = WeeklyConfig.from_environment()
        engine = get_engine()
        try:
            return await run_once(
                engine=engine, config=config, round_name=round_name, dry_run=dry_run
            )
        finally:
            await close_engine()

    try:
        with single_runner_lock() as acquired:
            if not acquired:
                return
            outcome = asyncio.run(execute())
        if dry_run:
            print(json.dumps(asdict(outcome), ensure_ascii=False))
        elif outcome.status in _REVIEW_STATUSES | {"blocked_by_prior_round"}:
            print(
                f"dyvine weekly: {outcome.status} {outcome.key or ''}", file=sys.stderr
            )
    except Exception as error:
        print(f"dyvine weekly failed: {type(error).__name__}", file=sys.stderr)
        raise SystemExit(1) from error
