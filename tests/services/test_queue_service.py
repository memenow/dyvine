"""Tests for QueueService orchestration over the fake repositories."""

from __future__ import annotations

import pytest
from fake_repos import (
    FakeQueueRepository,
    FakeRoundRepository,
    FakeSeedRepository,
)

from dyvine.core.exceptions import ServiceError
from dyvine.services.queue import QueueService


def _service() -> tuple[
    QueueService, FakeQueueRepository, FakeSeedRepository, FakeRoundRepository
]:
    """Build a service wired to fresh fakes."""
    queue = FakeQueueRepository(owner_id="owner-1")
    seeds = FakeSeedRepository()
    rounds = FakeRoundRepository()
    return QueueService(queue=queue, seeds=seeds, rounds=rounds), queue, seeds, rounds


async def test_import_seeds_accepts_batch() -> None:
    """Valid seeds upsert and the count echoes back."""
    svc, _, seeds, _ = _service()
    accepted = await svc.import_seeds(
        [
            {"sec_user_id": "s1", "nickname": "n1"},
            {"sec_user_id": "s2", "excluded": True},
        ],
        batch="b1",
    )
    assert accepted == 2
    assert (await seeds.get_seed("s1")).batch == "b1"
    assert (await seeds.get_seed("s2")).excluded is True


async def test_import_seeds_rejects_before_writing() -> None:
    """A bad entry aborts the whole batch with its position named."""
    svc, _, seeds, _ = _service()
    with pytest.raises(ServiceError, match="Seed entry 1 misses sec_user_id"):
        await svc.import_seeds([{"sec_user_id": "s1"}, {"nickname": "no-sec"}])
    assert await seeds.count_seeds(include_excluded=True) == 0


async def test_enqueue_round_is_idempotent() -> None:
    """Second enqueue adds nothing and never resets entry progress."""
    svc, queue, _, _ = _service()
    await svc.import_seeds(
        [{"sec_user_id": "s1"}, {"sec_user_id": "s2", "excluded": True}]
    )
    assert await svc.enqueue_round("r1", mode="full") == 1
    await svc.report_progress("r1:s1", status="op_done")
    assert await svc.enqueue_round("r1", mode="full") == 0
    assert (await svc.get_entry("r1:s1")).status == "op_done"


async def test_enqueue_round_keeps_legacy_nickname_exclusions() -> None:
    """A newly seeded account with a historically excluded name stays out."""
    svc, queue, _, _ = _service()
    await svc.import_seeds(
        [
            {"sec_user_id": "s1", "nickname": "Excluded"},
            {"sec_user_id": "s2", "nickname": "Included"},
        ]
    )
    created = await svc.enqueue_round(
        "r1", mode="incremental", excluded_nicknames={"Excluded"}
    )
    assert created == 1
    assert [row.sec_user_id for row in await queue.list_entries(round="r1")] == ["s2"]


async def test_enqueue_round_does_not_duplicate_legacy_key_for_same_account() -> None:
    """A noncanonical historical key still owns its round/account slot."""
    svc, queue, _, _ = _service()
    await svc.import_seeds([{"sec_user_id": "s1", "nickname": "Account"}])
    await queue.upsert_entry(
        key="legacy-key",
        round="r1",
        nickname="Account",
        sec_user_id="s1",
        mode="incremental",
        status="needs_reconciliation",
    )
    assert await svc.enqueue_round("r1", mode="incremental") == 0
    assert [row.key for row in await queue.list_entries(round="r1")] == ["legacy-key"]


async def test_claim_and_status_tally() -> None:
    """Claims pop oldest-first; tallies group the open status set."""
    svc, _, _, _ = _service()
    await svc.import_seeds([{"sec_user_id": "s1"}, {"sec_user_id": "s2"}])
    await svc.enqueue_round("r1", mode="full")
    claimed = await svc.claim_next(round="r1")
    assert claimed is not None and claimed.status == "downloading"
    status = await svc.round_status("r1")
    assert status.total == 2
    assert status.by_status == {"downloading": 1, "pending": 1}
    assert await svc.active_count(round="r1") == 2
    # Claiming s1 refreshed its stamp, so s2 now sorts first.
    assert [entry.key for entry in await svc.list_entries(round="r1")] == [
        "r1:s2",
        "r1:s1",
    ]


async def test_release_stale_delegates() -> None:
    """Stale release passes its knobs straight to the repository."""
    svc, _, _, _ = _service()
    assert await svc.release_stale(stale_after_seconds=60.0, max_attempts=3) == 0


async def test_enqueue_round_skips_without_per_key_probes() -> None:
    """Enqueue decides from one snapshot; no per-key existence probes."""
    from unittest.mock import AsyncMock

    service, queue, seeds, _ = _service()
    await seeds.upsert_seed(sec_user_id="sec-1", nickname="n1")
    await seeds.upsert_seed(sec_user_id="sec-2", nickname="n2")
    queue.get_entry = AsyncMock(side_effect=AssertionError("must not probe"))  # type: ignore[method-assign]
    assert await service.enqueue_round("r1", mode="full") == 2
    assert await service.enqueue_round("r1", mode="full") == 0


async def test_report_progress_rejects_unknown_fields() -> None:
    """Typos fail loud at the service boundary, never silent-drop."""
    service, queue, _, _ = _service()
    await queue.upsert_entry(
        key="k",
        round="r1",
        nickname="n",
        sec_user_id="sec-1",
        mode="full",
        status="pending",
    )
    with pytest.raises(ServiceError, match="read-only.*not_a_field"):
        await service.report_progress("k", not_a_field="x")


async def test_report_progress_refuses_identity_rewrite() -> None:
    """Repo-legal identity fields are still refused at the trust boundary."""
    service, queue, _, _ = _service()
    await queue.upsert_entry(
        key="r1:s1",
        round="r1",
        nickname="n",
        sec_user_id="s1",
        mode="full",
        status="pending",
    )
    for field in ("sec_user_id", "mode", "round", "cutoff", "nickname", "serial_group"):
        with pytest.raises(ServiceError, match="read-only"):
            await service.report_progress("r1:s1", **{field: "evil"})
    assert (await queue.get_entry("r1:s1")).sec_user_id == "s1"


async def test_report_progress_accepts_progress_fields() -> None:
    """The full legitimate patch surface passes through untouched."""
    service, queue, _, _ = _service()
    await queue.upsert_entry(
        key="r1:s1",
        round="r1",
        nickname="n",
        sec_user_id="s1",
        mode="full",
        status="pending",
    )
    updated = await service.report_progress(
        "r1:s1",
        status="downloading",
        operation_id="op-1",
        op_status="running",
        op_message="working",
        attempts=2,
        chat_id="chat-9",
        extra={"weekly": {"since": "a1"}},
    )
    assert updated.status == "downloading"
    assert updated.operation_id == "op-1"
    assert updated.attempts == 2
    assert updated.chat_id == "chat-9"


async def test_enqueue_round_rejects_unknown_mode() -> None:
    """A bad mode fails the seeding call, not a mid-run pair. (P5-28)"""
    service, _, _, _ = _service()
    with pytest.raises(ServiceError, match="Unknown queue mode"):
        await service.enqueue_round("r1", mode="yearly")  # type: ignore[arg-type]


async def test_enqueue_round_pages_past_first_chunk() -> None:
    """Membership dedupe sees rows beyond the first page (no -1 sentinel)."""
    service, queue, seeds, _ = _service()
    for index in range(600):
        await seeds.upsert_seed(sec_user_id=f"s{index}")
    for index in range(600):
        await queue.upsert_entry(
            key=f"r1:s{index}",
            round="r1",
            nickname=f"s{index}",
            sec_user_id=f"s{index}",
            mode="full",
            status="pending",
        )
    real_list = queue.list_entries
    seen_limits: list[int] = []

    async def _spy(**kwargs: object) -> object:
        seen_limits.append(kwargs["limit"])  # type: ignore[typeddict-item]
        return await real_list(**kwargs)  # type: ignore[arg-type]

    queue.list_entries = _spy  # type: ignore[method-assign]
    assert await service.enqueue_round("r1", mode="full") == 0
    assert seen_limits and all(limit > 0 for limit in seen_limits)
    assert len(seen_limits) >= 2  # 600 rows cannot fit one 500-row page


async def test_release_stale_rejects_nonsense_knobs() -> None:
    """Negative windows fail fast instead of mass-requeueing."""
    service, _, _, _ = _service()
    with pytest.raises(ServiceError, match="stale_after_seconds"):
        await service.release_stale(stale_after_seconds=0, max_attempts=3)
    with pytest.raises(ServiceError, match="stale_after_seconds"):
        await service.release_stale(stale_after_seconds=-5, max_attempts=3)
    with pytest.raises(ServiceError, match="max_attempts"):
        await service.release_stale(stale_after_seconds=60, max_attempts=-1)


async def test_round_status_total_matches_tally() -> None:
    """Total always equals the tally sum (single atomic snapshot)."""
    service, queue, _, _ = _service()
    await queue.upsert_entry(
        key="r1:a",
        round="r1",
        nickname="a",
        sec_user_id="a",
        mode="full",
        status="pending",
    )
    await queue.upsert_entry(
        key="r1:b",
        round="r1",
        nickname="b",
        sec_user_id="b",
        mode="full",
        status="legacy_weird_status",
    )
    status = await service.round_status("r1")
    assert status.total == 2
    assert status.by_status == {"pending": 1, "legacy_weird_status": 1}
    assert (await service.round_status("nope")).total == 0
