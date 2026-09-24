"""Round selection and one-account-at-a-time progression."""

from __future__ import annotations

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


async def test_parked_rows_do_not_hold_later_pairs(tmp_path: Path) -> None:
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


async def test_one_invocation_advances_current_pair_only(
    tmp_path: Path,
) -> None:
    user_dir = tmp_path / "Account"
    user_dir.mkdir()
    repo = FakeQueueRepository()
    rounds = FakeRoundRepository()
    for index in range(1, 4):
        await repo.upsert_entry(
            key=f"weekly0913:sec_{index}",
            round="weekly0913",
            nickname=f"Account {index}",
            sec_user_id=f"sec_{index}",
            mode="incremental",
            status="pending",
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
    assert first.status == "pair_complete"
    assert (await repo.get_entry("weekly0913:sec_1")).status == "completed"
    assert (await repo.get_entry("weekly0913:sec_2")).status == "completed"
    assert (await repo.get_entry("weekly0913:sec_3")).status == "pending"
    second = await run_once(
        engine=engine, config=_config(tmp_path), round_name="weekly0913"
    )
    assert second.key == "weekly0913:sec_3"
    assert (await repo.get_entry("weekly0913:sec_3")).status == "completed"
