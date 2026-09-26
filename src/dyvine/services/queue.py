"""Download-queue orchestration over the ``0002`` tables.

:class:`QueueService` is the seam between plugin tools and the raw
repositories: seed imports, round setup, entry claiming with runner
liveness, progress reports, and status tallies. It owns no SQL and no
network; every method delegates to an injected repository protocol so
unit tests inject the fakes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, get_args

from ..core.exceptions import SeedAccountNotFoundError, ServiceError
from ..db.protocols import (
    QUEUE_ACTIVE_STATUSES,
    QueueEntryStatus,
    QueueMode,
    QueueRepository,
    RoundRepository,
    SeedRepository,
)
from ..db.records import QueueEntryRecord


@dataclass(slots=True)
class RoundStatus:
    """Per-status tallies plus totals for one round (or every round)."""

    round: str | None
    total: int
    by_status: dict[str, int]


class QueueService:
    """Seed, claim, and track download-queue entries."""

    #: Fields a progress patch may touch. Identity (``key``/``round``/
    #: ``sec_user_id``), cohort (``mode``/``cutoff``/``serial_group``), and
    #: the remaining linkage (``kind``/``nickname``/``homepage``) are owned
    #: by enqueue/claim and must never move under a progress report: the
    #: LLM-facing ``dyvine.queue.update`` tool forwards raw caller fields
    #: here, so this is a trust boundary, not a convenience check.
    #: ``chat_id`` stays mutable because group resolution persists the
    #: verified chat onto the entry through this same path (rewriting it
    #: cannot break the ``key == {round}:{sec}`` invariant).
    PROGRESS_MUTABLE_FIELDS = frozenset(
        {
            "status",
            "operation_id",
            "op_status",
            "op_message",
            "extra",
            "attempts",
            "chat_id",
        }
    )

    def __init__(
        self,
        *,
        queue: QueueRepository,
        seeds: SeedRepository,
        rounds: RoundRepository,
    ) -> None:
        """Bind the service to its repository protocols."""
        self._queue = queue
        self._seeds = seeds
        self._rounds = rounds

    async def import_seeds(
        self, items: list[dict[str, Any]], *, batch: str | None = None
    ) -> int:
        """Upsert seed rows; return the number accepted.

        Entries without a string ``sec_user_id`` raise
        :class:`ServiceError` naming the offender instead of
        half-importing the batch.
        """
        for position, item in enumerate(items):
            sec = item.get("sec_user_id")
            if not sec or not isinstance(sec, str):
                raise ServiceError(
                    f"Seed entry {position} misses sec_user_id: {item!r}"
                )
        for item in items:
            await self._seeds.upsert_seed(
                sec_user_id=item["sec_user_id"],
                nickname=item.get("nickname"),
                source_url=item.get("source_url"),
                source=item.get("source", "seed"),
                batch=batch or item.get("batch"),
                excluded=bool(item.get("excluded", False)),
            )
        return len(items)

    async def exclude_seed(self, sec_user_id: str) -> bool:
        """Keep a seed out of later rounds; return whether the seed exists.

        Nickname, source, and batch are preserved, so re-importing the seed
        with ``excluded`` false restores it.
        """
        try:
            seed = await self._seeds.get_seed(sec_user_id)
        except SeedAccountNotFoundError:
            return False
        if not seed.excluded:
            await self._seeds.upsert_seed(
                sec_user_id=seed.sec_user_id,
                nickname=seed.nickname,
                source_url=seed.source_url,
                source=seed.source,
                batch=seed.batch,
                excluded=True,
            )
        return True

    async def ensure_round(self, round_name: str, note: str | None = None) -> None:
        """Create the round header when missing (idempotent)."""
        await self._rounds.upsert_round(round=round_name, note=note)

    async def enqueue_round(
        self,
        round_name: str,
        *,
        mode: QueueMode,
        cutoff: str | None = None,
        note: str | None = None,
        excluded_nicknames: set[str] | None = None,
    ) -> int:
        """Enqueue every non-excluded seed into ``round_name`` once.

        Entries key ``{round}:{sec}`` and start ``pending``; seeds
        already enqueued (any status) are left untouched so a second
        call never resets progress. One round snapshot (matched by
        account, not by key) decides membership: legacy rows carry
        non-canonical keys a per-key probe would miss, duplicating
        the account slot. The snapshot pages through the listing in
        bounded chunks instead of depending on an undocumented
        ``limit=-1`` all-rows sentinel no protocol promises. A
        concurrent double-enqueue still converges because the
        repository upsert is atomic, and both writers insert the
        identical pending row. Returns the number of new rows.

        ``mode`` is validated here, at enqueue time: the weekly runner
        only inlines ``full``/``post``/``incremental``, and a bad mode
        must fail the seeding call, not surface mid-run as a stuck pair.
        """
        valid_modes = get_args(QueueMode)
        if mode not in valid_modes:
            raise ServiceError(
                f"Unknown queue mode {mode!r} (expected one of {sorted(valid_modes)})"
            )
        await self.ensure_round(round_name, note)
        seeds = await self._seeds.list_seeds(include_excluded=False)
        excluded = excluded_nicknames or set()
        existing_secs: set[str] = set()
        offset = 0
        while True:
            page = await self._queue.list_entries(
                round=round_name, limit=500, offset=offset
            )
            if not page:
                break
            existing_secs.update(row.sec_user_id for row in page)
            offset += len(page)
        created = 0
        for seed in seeds:
            if seed.nickname and seed.nickname in excluded:
                continue
            if seed.sec_user_id in existing_secs:
                continue
            await self._queue.upsert_entry(
                key=f"{round_name}:{seed.sec_user_id}",
                round=round_name,
                nickname=seed.nickname or seed.sec_user_id,
                sec_user_id=seed.sec_user_id,
                mode=mode,
                status="pending",
                cutoff=cutoff,
            )
            existing_secs.add(seed.sec_user_id)
            created += 1
        return created

    async def claim_next(
        self, *, round: str | None = None, keys: set[str] | None = None
    ) -> QueueEntryRecord | None:
        """Claim the oldest pending entry in the allowed batch."""
        return await self._queue.claim_next(round=round, keys=keys)

    async def report_progress(self, key: str, **fields: Any) -> QueueEntryRecord:
        """Patch a claimed entry (status/checkpoint/counters) and return it.

        Only :data:`PROGRESS_MUTABLE_FIELDS` pass through: anything else
        (notably identity and cohort fields) is refused loudly so a typo'd
        tool call can never silently rewrite ``sec_user_id`` and break the
        ``key == {round}:{sec}`` invariant.
        """
        refused = set(fields) - self.PROGRESS_MUTABLE_FIELDS
        if refused:
            raise ServiceError(
                "Refusing to update read-only queue fields: "
                f"{sorted(refused)} (mutable: {sorted(self.PROGRESS_MUTABLE_FIELDS)})"
            )
        return await self._queue.update_entry(key, **fields)

    async def get_entry(self, key: str) -> QueueEntryRecord:
        """Fetch one entry by key."""
        return await self._queue.get_entry(key)

    async def list_entries(
        self,
        *,
        round: str | None = None,
        status: QueueEntryStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[QueueEntryRecord]:
        """List entries oldest-first, optionally filtered."""
        return await self._queue.list_entries(
            round=round, status=status, limit=limit, offset=offset
        )

    async def round_status(self, round: str | None = None) -> RoundStatus:
        """Tally entries by status for one round (or every round).

        One ``GROUP BY`` snapshot backs both numbers, so ``total`` always
        equals the tally sum even under concurrent claims (the old
        count-then-scan read two different instants and could disagree).
        """
        by_status = await self._queue.count_by_status(round=round)
        return RoundStatus(
            round=round, total=sum(by_status.values()), by_status=by_status
        )

    async def active_count(self, *, round: str | None = None) -> int:
        """Count entries still in flight (pending + downloading)."""
        total = 0
        for status in QUEUE_ACTIVE_STATUSES:
            total += await self._queue.count_entries(round=round, status=status)
        return total

    async def release_stale(
        self,
        *,
        stale_after_seconds: float,
        max_attempts: int,
        round: str | None = None,
    ) -> int:
        """Requeue entries whose claimer stopped heartbeating."""
        if not stale_after_seconds > 0:
            raise ServiceError(f"Invalid stale_after_seconds: {stale_after_seconds!r}")
        if max_attempts < 0:
            raise ServiceError(f"Invalid max_attempts: {max_attempts!r}")
        return await self._queue.release_stale(
            stale_after_seconds=stale_after_seconds,
            max_attempts=max_attempts,
            round=round,
        )
