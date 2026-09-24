"""Single-shot scheduler and Hermes CLI adapter for weekly delivery."""

from __future__ import annotations

import asyncio
import datetime as dt
import fcntl
import json
import os
import shutil
import statistics
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dyvine.core.exceptions import DeliveryError
from dyvine.db.protocols import QUEUE_ACTIVE_STATUSES

from .weekly_account import process_entry
from .weekly_state import _checkpoint, entry_cutoff, patch_queue
from .weekly_types import WeeklyConfig, WeeklyOutcome

MAX_RUN_SECONDS = 2700
# Hermes kills a cron script at 3600 s. A later pair starts only this early,
# so every pair keeps most of the run's MAX_RUN_SECONDS deadline.
PAIR_START_WINDOW_SECONDS = 900
DISK_RESERVE_BYTES = 10 * 1024**3
DISK_RESERVE_FRACTION = 0.10
RATE_SAFETY = 1.5
RATE_MIN_SAMPLES = 10
DEFAULT_RATE_BYTES_PER_DAY = 20 * 1024**2
FULL_DOWNLOAD_ALLOWANCE = 2 * 1024**3
MAX_STEPS_PER_RUN = 100
STALE_AFTER_SECONDS = 3600
MAX_ATTEMPTS = 8
_REVIEW_STATUSES = frozenset(
    {"needs_reconciliation", "needs_review", "op_issue", "pair_needs_review"}
)
_REPORTED_STATUSES = _REVIEW_STATUSES | {"blocked_by_prior_round", "disk_budget"}
_DONE_STATUSES = frozenset({"completed", "permanent_failure", "skipped"})
# The legacy migration parks rows it could not prove. This runner never
# advances them, so they wait for an operator without holding later pairs.
_PARKED_STATUSES = frozenset({"needs_reconciliation"})


def due_round(now: dt.datetime, timezone: str) -> tuple[str, dt.datetime]:
    """Return the name and due time of the most recent Sunday 08:00 round."""
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
    return f"weekly-{sunday.isoformat()}", due


def _automatic_round_date(name: str) -> dt.date | None:
    """Return the Sunday an automatic ``weekly-YYYY-MM-DD`` round is named for."""
    if not name.startswith("weekly-"):
        return None
    try:
        return dt.date.fromisoformat(name.removeprefix("weekly-"))
    except ValueError:
        return None


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
    date = _automatic_round_date(candidate)
    return date is not None and first_auto_date <= date < due_date


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
    """Restrict claims to the first unfinished pair in stable seed order.

    A pair whose only unfinished rows are parked is passed over; parked rows
    still hold back automatic rounds until an operator resolves them.
    """
    rows = await engine.queue.list_entries(round=round_name, limit=-1)
    rows.sort(key=lambda row: (row.created_at, row.key))
    settled = _DONE_STATUSES | _PARKED_STATUSES
    for offset in range(0, len(rows), 2):
        pair = rows[offset : offset + 2]
        if any(row.status not in settled for row in pair):
            return {row.key for row in pair}
    return set()


def _download_rate(rows: list[Any]) -> float:
    """Return the p90 bytes per window day that finished rows downloaded.

    Until RATE_MIN_SAMPLES rows recorded a window, the default rate applies.
    """
    samples: list[float] = []
    for row in rows:
        checkpoint = _checkpoint(row)
        size = checkpoint.get("window_bytes")
        days = checkpoint.get("window_days")
        if (
            row.status in _DONE_STATUSES
            and isinstance(size, int | float)
            and isinstance(days, int | float)
        ):
            samples.append(size / max(days, 1))
    if len(samples) < RATE_MIN_SAMPLES:
        return DEFAULT_RATE_BYTES_PER_DAY
    return statistics.quantiles(samples, n=10)[-1]


async def _disk_shortfall(
    engine: Any,
    config: WeeklyConfig,
    round_name: str,
    pair_keys: set[str],
    local_now: dt.datetime,
) -> str | None:
    """Explain why a pair's estimated download does not fit, or return ``None``.

    Only the pair's in-flight rows count. A cutoff-bounded incremental row
    needs its window days (at least one) times the round's rate times
    RATE_SAFETY; a full-feed row needs FULL_DOWNLOAD_ALLOWANCE. The estimate
    must fit the free space above max(DISK_RESERVE_BYTES, DISK_RESERVE_FRACTION
    of the disk). ``local_now`` is naive weekly-timezone time, as cutoffs are.
    """
    rows = await engine.queue.list_entries(round=round_name, limit=-1)
    rate = _download_rate(rows)
    need = 0.0
    for row in rows:
        if row.key not in pair_keys or row.status not in QUEUE_ACTIVE_STATUSES:
            continue
        # A full or post download fetches the whole feed whatever its cutoff.
        # A malformed cutoff counts as a full feed here and is flagged for
        # review when its row is claimed.
        try:
            cutoff = (
                entry_cutoff(row, config.timezone)
                if row.mode == "incremental"
                else None
            )
        except ValueError:
            cutoff = None
        if cutoff is None:
            need += FULL_DOWNLOAD_ALLOWANCE
        else:
            days = (local_now - cutoff).total_seconds() / 86400
            need += max(days, 1) * rate * RATE_SAFETY
    usage = shutil.disk_usage(config.download_root)
    reserve = max(DISK_RESERVE_BYTES, usage.total * DISK_RESERVE_FRACTION)
    if need <= usage.free - reserve:
        return None
    return f"need={round(need / 1024**2)}MiB free={usage.free // 1024**2}MiB"


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


async def _advance_pair(
    engine: Any,
    config: WeeklyConfig,
    round_name: str,
    pair_keys: set[str],
    deadline: float,
    channel: Any,
    outcomes: list[WeeklyOutcome],
) -> list[WeeklyOutcome]:
    """Step the pair's rows until each stalls; return this pair's outcomes.

    ``outcomes`` gathers every step of the run, so MAX_STEPS_PER_RUN caps
    the whole invocation rather than each pair.
    """
    first = len(outcomes)
    stalled: set[str] = set()
    while len(outcomes) < MAX_STEPS_PER_RUN:
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
    return outcomes[first:]


async def run_once(
    *,
    engine: Any,
    config: WeeklyConfig,
    round_name: str | None = None,
    dry_run: bool = False,
    now: dt.datetime | None = None,
    channel: Any = None,
) -> WeeklyOutcome:
    """Advance pairs in seed order within run budgets; never spawn detached tasks."""
    instant = now or dt.datetime.now(tz=ZoneInfo(config.timezone))
    automatic = round_name is None
    window_start: dt.date | None = None
    if automatic:
        if config.first_auto_date is None:
            raise ValueError("DYVINE_WEEKLY_FIRST_AUTO_DATE is required")
        if not config.cutover_round:
            raise ValueError("DYVINE_WEEKLY_CUTOVER_ROUND is required")
        round_name, due = due_round(instant, config.timezone)
        if due.date() < config.first_auto_date:
            return WeeklyOutcome("before_cutover", round_name)
        if instant < due:
            return WeeklyOutcome("not_due", round_name)
        unresolved = 0
        window_start = config.first_auto_date - dt.timedelta(days=7)
        for header in await engine.round_repo.list_rounds():
            opened = _automatic_round_date(header.round)
            if opened is not None and config.first_auto_date <= opened < due.date():
                window_start = max(window_start, opened)
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
        assert window_start is not None
        excluded = await engine.delivery_ledger.list_excluded_nicknames()
        # Resume where the latest opened automatic window ended, so a week
        # held back by an unfinished round widens this window instead of
        # being skipped; delivery skips media an earlier window already sent.
        await engine.queue.enqueue_round(
            round_name,
            mode="incremental",
            cutoff=dt.datetime.combine(window_start, dt.time(8)).isoformat(),
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
    started = time.monotonic()
    deadline = started + MAX_RUN_SECONDS
    local_now = instant.astimezone(ZoneInfo(config.timezone)).replace(tzinfo=None)
    shortfall = await _disk_shortfall(engine, config, round_name, pair_keys, local_now)
    if shortfall:
        return WeeklyOutcome("disk_budget", round_name, note=shortfall)
    outcomes: list[WeeklyOutcome] = []
    pairs = 0
    while True:
        pairs += 1
        advanced = await _advance_pair(
            engine, config, round_name, pair_keys, deadline, channel, outcomes
        )
        if not advanced or any(item.status in _REVIEW_STATUSES for item in advanced):
            break
        # An unsettled pair stays first, so seed order holds across pairs.
        next_keys = await _current_pair_keys(engine, round_name)
        if (
            not next_keys
            or next_keys == pair_keys
            or len(outcomes) >= MAX_STEPS_PER_RUN
            or time.monotonic() - started >= PAIR_START_WINDOW_SECONDS
            or await _disk_shortfall(engine, config, round_name, next_keys, local_now)
        ):
            break
        pair_keys = next_keys
    if not outcomes:
        return WeeklyOutcome("blocked_pair", round_name)
    if pairs == 1 and len(outcomes) == 1:
        return outcomes[0]
    status = next(
        (item.status for item in outcomes if item.status in _REVIEW_STATUSES), None
    )
    if status is None:
        pair = [
            entry
            for entry in [
                await engine.queue.get_entry(key) for key in sorted(pair_keys)
            ]
            if entry.status not in _PARKED_STATUSES
        ]
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
        note=f"pairs={pairs} steps={len(outcomes)}",
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
        elif outcome.status in _REPORTED_STATUSES:
            detail = " ".join(part for part in (outcome.key, outcome.note) if part)
            print(f"dyvine weekly: {outcome.status} {detail}".rstrip(), file=sys.stderr)
    except Exception as error:
        print(f"dyvine weekly failed: {type(error).__name__}", file=sys.stderr)
        raise SystemExit(1) from error
