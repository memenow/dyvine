"""Round selection and pair-by-pair progression."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dyvine.services.queue import QueueService
from dyvine_hermes import weekly as weekly_module
from dyvine_hermes.weekly import WeeklyConfig, run_once
from tests.fake_repos import (
    FakeQueueRepository,
    FakeRoundRepository,
    FakeSeedRepository,
)

_MIB = 1024**2
_GIB = 1024**3


def _config(root: Path, *, cutover_round: str | None = "weekly0913") -> WeeklyConfig:
    return WeeklyConfig(
        timezone="Asia/Shanghai",
        owner_open_id="ou_recipient",
        download_root=root,
        first_auto_date=date(2026, 9, 27),
        cutover_round=cutover_round,
    )


def _engine(
    repo: FakeQueueRepository,
    rounds: FakeRoundRepository,
    seeds: FakeSeedRepository | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        queue=QueueService(
            queue=repo, seeds=seeds or FakeSeedRepository(), rounds=rounds
        ),
        round_repo=rounds,
        delivery_ledger=SimpleNamespace(
            list_files=AsyncMock(return_value=[]),
            list_excluded_nicknames=AsyncMock(return_value=set()),
        ),
    )


async def test_automatic_round_ignores_held_pre_cutover_history(
    tmp_path: Path,
) -> None:
    repo = FakeQueueRepository()
    rounds = FakeRoundRepository()
    first_old = date(2025, 10, 12)
    for index in range(49):
        name = f"weekly-{first_old + timedelta(weeks=index)}"
        await rounds.upsert_round(round=name)
        await repo.upsert_entry(
            key=f"{name}:sec_old",
            round=name,
            nickname="Historical",
            sec_user_id="sec_old",
            mode="post",
            status="needs_reconciliation",
        )
    await rounds.upsert_round(round="weekly0913")
    await repo.upsert_entry(
        key="weekly0913:sec_current",
        round="weekly0913",
        nickname="Current",
        sec_user_id="sec_current",
        mode="post",
        status="completed",
    )
    outcome = await run_once(
        engine=_engine(repo, rounds),
        config=_config(tmp_path),
        now=datetime(2026, 9, 27, 0, 0, tzinfo=UTC),
    )
    assert outcome.status == "idle"
    assert outcome.round == "weekly-2026-09-27"
    assert (await repo.get_entry("weekly-2025-10-12:sec_old")).status == (
        "needs_reconciliation"
    )


async def test_automatic_round_blocks_on_previous_post_cutover_round(
    tmp_path: Path,
) -> None:
    repo = FakeQueueRepository()
    rounds = FakeRoundRepository()
    await rounds.upsert_round(round="weekly-2026-09-27")
    await repo.upsert_entry(
        key="weekly-2026-09-27:sec_1",
        round="weekly-2026-09-27",
        nickname="Account",
        sec_user_id="sec_1",
        mode="incremental",
        status="needs_review",
    )
    outcome = await run_once(
        engine=_engine(repo, rounds),
        config=_config(tmp_path),
        now=datetime(2026, 10, 4, 0, 0, tzinfo=UTC),
    )
    assert outcome.status == "blocked_by_prior_round"
    assert outcome.round == "weekly-2026-10-04"


@pytest.mark.parametrize(
    ("opened", "now", "round_name", "cutoff"),
    [
        # The first window reaches back a week before the first automatic
        # Sunday even when the cutover round held it back a week longer.
        (["weekly-2026-09-13"], datetime(2026, 10, 4), "weekly-2026-10-04", "09-20"),
        (["weekly-2026-09-27"], datetime(2026, 10, 4), "weekly-2026-10-04", "09-27"),
        # A week whose round never opened widens the next window.
        (["weekly-2026-09-27"], datetime(2026, 10, 11), "weekly-2026-10-11", "09-27"),
    ],
)
async def test_automatic_window_resumes_at_the_last_opened_round(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    opened: list[str],
    now: datetime,
    round_name: str,
    cutoff: str,
) -> None:
    repo = FakeQueueRepository()
    rounds = FakeRoundRepository()
    seeds = FakeSeedRepository()
    engine = _engine(repo, rounds, seeds)
    await engine.queue.import_seeds([{"sec_user_id": "sec_1", "nickname": "Account"}])
    for name in ["weekly0913", *opened]:
        await rounds.upsert_round(round=name)
    # A zero budget stops after enqueueing, before any claim.
    monkeypatch.setattr(weekly_module, "MAX_RUN_SECONDS", 0)
    outcome = await run_once(
        engine=engine, config=_config(tmp_path), now=now.replace(tzinfo=UTC)
    )
    assert outcome.round == round_name
    entry = await repo.get_entry(f"{round_name}:sec_1")
    assert entry.cutoff == f"2026-{cutoff}T08:00:00"


async def test_parked_rows_do_not_hold_later_pairs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One pair per run keeps each hand-off between pairs observable.
    monkeypatch.setattr(weekly_module, "PAIR_START_WINDOW_SECONDS", 0)
    user_dir = tmp_path / "Account"
    user_dir.mkdir()
    repo = FakeQueueRepository()
    rounds = FakeRoundRepository()
    statuses = ["needs_reconciliation", "pending"] + ["needs_reconciliation"] * 2
    for index, status in enumerate([*statuses, "pending", "pending"], start=1):
        await repo.upsert_entry(
            key=f"weekly0913:sec_{index}",
            round="weekly0913",
            nickname=f"Account {index}",
            sec_user_id=f"sec_{index}",
            mode="incremental",
            status=status,
            extra={
                "weekly": {
                    "download_complete": True,
                    "fresh_download_confirmed": True,
                    "user_dir": str(user_dir),
                }
            },
        )
    engine = _engine(repo, rounds)
    first = await run_once(
        engine=engine, config=_config(tmp_path), round_name="weekly0913"
    )
    assert first.key == "weekly0913:sec_2"
    assert (await repo.get_entry("weekly0913:sec_5")).status == "pending"
    second = await run_once(
        engine=engine, config=_config(tmp_path), round_name="weekly0913"
    )
    assert second.status == "pair_complete"
    assert (await repo.get_entry("weekly0913:sec_6")).status == "completed"
    idle = await run_once(
        engine=engine, config=_config(tmp_path), round_name="weekly0913"
    )
    assert idle.status == "idle"
    for key in ("sec_1", "sec_3", "sec_4"):
        assert (await repo.get_entry(f"weekly0913:{key}")).status == (
            "needs_reconciliation"
        )


async def test_runtime_review_beside_a_parked_row_still_holds_the_queue(
    tmp_path: Path,
) -> None:
    repo = FakeQueueRepository()
    rounds = FakeRoundRepository()
    statuses = ("needs_reconciliation", "needs_review", "pending", "pending")
    for index, status in enumerate(statuses, start=1):
        await repo.upsert_entry(
            key=f"weekly0913:sec_{index}",
            round="weekly0913",
            nickname=f"Account {index}",
            sec_user_id=f"sec_{index}",
            mode="incremental",
            status=status,
        )
    outcome = await run_once(
        engine=_engine(repo, rounds),
        config=_config(tmp_path),
        round_name="weekly0913",
    )
    assert outcome.status == "blocked_pair"
    assert (await repo.get_entry("weekly0913:sec_3")).status == "pending"


async def test_automatic_round_requires_explicit_cutover_round(tmp_path: Path) -> None:
    repo = FakeQueueRepository()
    rounds = FakeRoundRepository()
    with pytest.raises(ValueError, match="DYVINE_WEEKLY_CUTOVER_ROUND"):
        await run_once(
            engine=_engine(repo, rounds),
            config=_config(tmp_path, cutover_round=None),
            now=datetime(2026, 9, 27, 0, 0, tzinfo=UTC),
        )
    assert await repo.count_entries(round="weekly-2026-09-27") == 0


async def _downloaded(
    repo: FakeQueueRepository, user_dir: Path, count: int, *, cutoff: str | None = None
) -> None:
    """Queue ``count`` pending accounts whose download already finished."""
    for index in range(1, count + 1):
        await repo.upsert_entry(
            key=f"weekly0913:sec_{index}",
            round="weekly0913",
            nickname=f"Account {index}",
            sec_user_id=f"sec_{index}",
            mode="incremental",
            status="pending",
            cutoff=cutoff,
            extra={
                "weekly": {
                    "download_complete": True,
                    "fresh_download_confirmed": True,
                    "user_dir": str(user_dir),
                }
            },
        )


def _disk(*free: int, total: int = 2**50) -> Callable[[Path], SimpleNamespace]:
    """Report each free-space reading in turn, one per disk check."""
    readings = iter(free)
    return lambda _path: SimpleNamespace(total=total, used=0, free=next(readings))


async def test_one_invocation_advances_pairs_in_seed_order(tmp_path: Path) -> None:
    user_dir = tmp_path / "Account"
    user_dir.mkdir()
    repo = FakeQueueRepository()
    await _downloaded(repo, user_dir, 4)
    engine = _engine(repo, FakeRoundRepository())
    claims: list[tuple[str, dict[str, str]]] = []
    claim_next = engine.queue.claim_next

    async def record_claim(**kwargs: object) -> object:
        entry = await claim_next(**kwargs)
        if entry is not None:
            rows = await repo.list_entries(limit=-1)
            claims.append((entry.key, {row.key: row.status for row in rows}))
        return entry

    engine.queue.claim_next = record_claim
    outcome = await run_once(
        engine=engine, config=_config(tmp_path), round_name="weekly0913"
    )
    assert outcome.status == "pair_complete"
    assert outcome.note == "pairs=2 steps=4"
    assert [key for key, _ in claims] == [f"weekly0913:sec_{i}" for i in range(1, 5)]
    # The second pair starts only after both first-pair accounts finished.
    statuses_at_third_claim = claims[2][1]
    assert statuses_at_third_claim["weekly0913:sec_1"] == "completed"
    assert statuses_at_third_claim["weekly0913:sec_2"] == "completed"


async def test_malformed_cutoff_is_claimed_and_held_for_review(
    tmp_path: Path,
) -> None:
    """The disk estimate never crashes a run; claiming the row flags it."""
    user_dir = tmp_path / "Account"
    user_dir.mkdir()
    repo = FakeQueueRepository()
    await _downloaded(repo, user_dir, 2, cutoff="not-a-date")
    outcome = await run_once(
        engine=_engine(repo, FakeRoundRepository()),
        config=_config(tmp_path),
        round_name="weekly0913",
    )
    assert outcome.status == "needs_review"
    assert (await repo.get_entry("weekly0913:sec_1")).status == "needs_review"


async def test_review_outcome_holds_later_pairs(tmp_path: Path) -> None:
    user_dir = tmp_path / "Account"
    user_dir.mkdir()
    repo = FakeQueueRepository()
    await _downloaded(repo, user_dir, 4)
    await repo.update_entry(
        "weekly0913:sec_1", extra={"migration_needs_reconciliation": True}
    )
    outcome = await run_once(
        engine=_engine(repo, FakeRoundRepository()),
        config=_config(tmp_path),
        round_name="weekly0913",
    )
    assert outcome.status == "needs_review"
    assert outcome.note == "pairs=1 steps=2"
    assert (await repo.get_entry("weekly0913:sec_1")).status == "needs_review"
    assert (await repo.get_entry("weekly0913:sec_2")).status == "completed"
    assert (await repo.get_entry("weekly0913:sec_3")).status == "pending"
    assert (await repo.get_entry("weekly0913:sec_4")).status == "pending"


@pytest.mark.parametrize(
    ("total", "free", "admitted"),
    [
        # Two accounts two days past the cutoff need 2 x 2 x 20 MiB x 1.5.
        (64 * _GIB, 10 * _GIB + 120 * _MIB, True),
        (64 * _GIB, 10 * _GIB + 120 * _MIB - 1, False),
        # A tenth of a large disk outweighs the 10 GiB floor.
        (200 * _GIB, 10 * _GIB + 120 * _MIB, False),
    ],
)
async def test_disk_budget_starts_first_pair_only_above_the_reserve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    total: int,
    free: int,
    admitted: bool,
) -> None:
    user_dir = tmp_path / "Account"
    user_dir.mkdir()
    repo = FakeQueueRepository()
    await _downloaded(repo, user_dir, 2, cutoff="2026-09-18T08:00:00")
    monkeypatch.setattr(weekly_module.shutil, "disk_usage", _disk(free, total=total))
    outcome = await run_once(
        engine=_engine(repo, FakeRoundRepository()),
        config=_config(tmp_path),
        round_name="weekly0913",
        now=datetime(2026, 9, 20, 0, 0, tzinfo=UTC),
    )
    if admitted:
        assert outcome.status == "pair_complete"
        return
    assert outcome.status == "disk_budget"
    assert outcome.note == f"need=120MiB free={free // _MIB}MiB"
    for key in ("weekly0913:sec_1", "weekly0913:sec_2"):
        entry = await repo.get_entry(key)
        assert (entry.status, entry.attempts) == ("pending", 0)


async def test_disk_budget_holds_the_next_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_dir = tmp_path / "Account"
    user_dir.mkdir()
    repo = FakeQueueRepository()
    await _downloaded(repo, user_dir, 4)
    # The disk fills while the first pair runs.
    monkeypatch.setattr(weekly_module.shutil, "disk_usage", _disk(2**50, 0))
    outcome = await run_once(
        engine=_engine(repo, FakeRoundRepository()),
        config=_config(tmp_path),
        round_name="weekly0913",
    )
    assert outcome.status == "pair_complete"
    assert outcome.note == "pairs=1 steps=2"
    assert (await repo.get_entry("weekly0913:sec_3")).status == "pending"
    assert (await repo.get_entry("weekly0913:sec_4")).status == "pending"


@pytest.mark.parametrize(("samples", "need_mib"), [(10, 59), (9, 120)])
async def test_disk_estimate_learns_the_round_p90_rate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, samples: int, need_mib: int
) -> None:
    """Ten finished windows replace the 20 MiB/day default with their p90."""
    user_dir = tmp_path / "Account"
    user_dir.mkdir()
    repo = FakeQueueRepository()
    await _downloaded(repo, user_dir, 2, cutoff="2026-09-18T08:00:00")
    # Rates of 1..10 MiB per window day have a p90 of 9.9 MiB per day.
    for index in range(1, samples + 1):
        await repo.upsert_entry(
            key=f"weekly0913:sec_done_{index:02d}",
            round="weekly0913",
            nickname=f"Done {index}",
            sec_user_id=f"sec_done_{index:02d}",
            mode="incremental",
            status="completed",
            extra={"weekly": {"window_bytes": 2 * index * _MIB, "window_days": 2.0}},
        )
    # Unfinished accounts and other rounds never feed the rate.
    for key, round_name, status in (
        ("weekly0913:sec_late", "weekly0913", "pending"),
        ("weekly0906:sec_old", "weekly0906", "completed"),
    ):
        await repo.upsert_entry(
            key=key,
            round=round_name,
            nickname="Outlier",
            sec_user_id=key,
            mode="incremental",
            status=status,
            extra={"weekly": {"window_bytes": 100 * _GIB, "window_days": 1.0}},
        )
    monkeypatch.setattr(weekly_module.shutil, "disk_usage", _disk(0, total=64 * _GIB))
    outcome = await run_once(
        engine=_engine(repo, FakeRoundRepository()),
        config=_config(tmp_path),
        round_name="weekly0913",
        now=datetime(2026, 9, 20, 0, 0, tzinfo=UTC),
    )
    assert outcome.status == "disk_budget"
    assert outcome.note == f"need={need_mib}MiB free=0MiB"
