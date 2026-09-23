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


def _service() -> (
    tuple[QueueService, FakeQueueRepository, FakeSeedRepository, FakeRoundRepository]
):
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
