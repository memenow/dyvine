"""Runner repetition and checkpoint recovery within one cron invocation."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dyvine.services.queue import QueueService
from dyvine_hermes import weekly as weekly_module
from dyvine_hermes.weekly import WeeklyConfig, WeeklyOutcome, run_once
from tests.fake_repos import FakeQueueRepository


async def test_run_once_reclaims_same_pending_account_until_pair_is_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 20-file checkpoint does not force a ten-minute wait for more media."""
    repo = FakeQueueRepository()
    await repo.upsert_entry(
        key="weekly0913:sec_1",
        round="weekly0913",
        nickname="Account",
        sec_user_id="sec_1",
        mode="incremental",
        status="pending",
    )
    queue = QueueService(queue=repo, seeds=AsyncMock(), rounds=AsyncMock())
    engine = SimpleNamespace(queue=queue)
    config = WeeklyConfig(
        timezone="Asia/Shanghai",
        owner_open_id="ou_recipient",
        download_root=tmp_path,
        first_auto_date=date(2026, 9, 27),
        cutover_round="weekly0913",
    )
    steps = 0

    async def advance(
        _engine: object,
        entry: object,
        _config: object,
        _deadline: float,
        _channel: object,
    ) -> WeeklyOutcome:
        nonlocal steps
        steps += 1
        status = "pending" if steps == 1 else "completed"
        await queue.report_progress("weekly0913:sec_1", status=status)
        return WeeklyOutcome(status, "weekly0913", "weekly0913:sec_1", files=20)

    monkeypatch.setattr(weekly_module, "process_entry", advance)
    outcome = await run_once(engine=engine, config=config, round_name="weekly0913")
    assert steps == 2
    assert outcome.status == "pair_complete"
    assert outcome.files == 40
    assert (await repo.get_entry("weekly0913:sec_1")).status == "completed"
