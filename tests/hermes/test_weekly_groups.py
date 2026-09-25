"""Explicit group reuse policy for weekly account delivery."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dyvine.services.queue import QueueService
from dyvine_hermes.weekly import run_once
from dyvine_hermes.weekly_types import WeeklyConfig
from tests.fake_repos import FakeQueueRepository


def _config(root: Path) -> WeeklyConfig:
    return WeeklyConfig(
        timezone="Asia/Shanghai",
        owner_open_id="ou_recipient",
        download_root=root,
        first_auto_date=date(2026, 9, 27),
        cutover_round="weekly0913",
    )


@pytest.mark.parametrize("policy", ["", "unknown", "new_each_round"])
def test_group_policy_must_be_explicit_and_supported(
    monkeypatch: pytest.MonkeyPatch, policy: str
) -> None:
    monkeypatch.setattr("dotenv.load_dotenv", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("DYVINE_WEEKLY_TIMEZONE", "Asia/Shanghai")
    monkeypatch.setenv("DYVINE_WEEKLY_OWNER_OPEN_ID", "ou_recipient")
    monkeypatch.setenv("DYVINE_WEEKLY_FIRST_AUTO_DATE", "2026-09-27")
    monkeypatch.setenv("DYVINE_WEEKLY_CUTOVER_ROUND", "weekly0913")
    monkeypatch.setenv("DYVINE_WEEKLY_GROUP_POLICY", policy)
    with pytest.raises(ValueError, match="DYVINE_WEEKLY_GROUP_POLICY"):
        WeeklyConfig.from_environment()


def _ready_group(chat_id: str = "oc_prior") -> SimpleNamespace:
    return SimpleNamespace(
        status="ready",
        chat_id=chat_id,
        topic_status="ready",
        topic_message_id="om_prior_topic",
    )


async def _setup(
    tmp_path: Path, *, chat_id: str | None = None, nickname: str = "Account"
) -> tuple:
    user_dir = tmp_path / "Account"
    post_dir = user_dir / "2026-09-28 09-00-00 post"
    post_dir.mkdir(parents=True)
    file_path = post_dir / "clip.mp4"
    file_path.write_bytes(b"clip")
    repo = FakeQueueRepository()
    await repo.upsert_entry(
        key="weekly-2026-09-27:sec_1",
        round="weekly-2026-09-27",
        nickname=nickname,
        sec_user_id="sec_1",
        mode="incremental",
        status="pending",
        chat_id=chat_id,
        cutoff="2026-09-20T08:00:00",
        extra={"weekly": {"download_complete": True, "user_dir": str(user_dir)}},
    )
    ledger = SimpleNamespace(
        get_group=AsyncMock(return_value=None),
        list_files=AsyncMock(return_value=[]),
        adopt_prior_verified_group_for_round=AsyncMock(return_value=None),
    )
    engine = SimpleNamespace(
        queue=QueueService(queue=repo, seeds=AsyncMock(), rounds=AsyncMock()),
        delivery_ledger=ledger,
        profiles=SimpleNamespace(
            get_profile=AsyncMock(
                return_value=SimpleNamespace(avatar_url="https://example.com/a.png")
            )
        ),
        users=SimpleNamespace(get_user_info=AsyncMock()),
    )
    channel = SimpleNamespace(
        ensure_group=AsyncMock(return_value=_ready_group("oc_new")),
        ensure_topic=AsyncMock(return_value=_ready_group("oc_new")),
        deliver_file=AsyncMock(
            return_value=SimpleNamespace(
                status="sent", relative_path=file_path.relative_to(user_dir).as_posix()
            )
        ),
    )
    return repo, engine, channel, file_path


async def test_reuse_policy_adopts_verified_chat_and_topic(tmp_path: Path) -> None:
    repo, engine, channel, file_path = await _setup(
        tmp_path, nickname="Renamed Account"
    )
    engine.delivery_ledger.adopt_prior_verified_group_for_round.return_value = (
        _ready_group()
    )
    outcome = await run_once(
        engine=engine,
        config=_config(tmp_path),
        round_name="weekly-2026-09-27",
        channel=channel,
    )
    assert outcome.status == "completed"
    assert (await repo.get_entry(outcome.key)).chat_id == "oc_prior"
    engine.delivery_ledger.adopt_prior_verified_group_for_round.assert_awaited_once_with(
        round="weekly-2026-09-27",
        sec_user_id="sec_1",
        nickname="Renamed Account",
        owner_open_id="ou_recipient",
    )
    channel.ensure_group.assert_not_called()
    channel.ensure_topic.assert_not_called()
    channel.deliver_file.assert_awaited_once()
    assert channel.deliver_file.await_args.kwargs["file_path"] == file_path
    assert channel.deliver_file.await_args.kwargs["chat_id"] == "oc_prior"
    assert "parent_id" not in channel.deliver_file.await_args.kwargs


async def test_reuse_policy_holds_ambiguous_prior_group(tmp_path: Path) -> None:
    repo, engine, channel, _ = await _setup(tmp_path)
    engine.delivery_ledger.adopt_prior_verified_group_for_round.side_effect = (
        ValueError("prior account has no unique verified group and topic")
    )
    outcome = await run_once(
        engine=engine,
        config=_config(tmp_path),
        round_name="weekly-2026-09-27",
        channel=channel,
    )
    assert outcome.status == "needs_review"
    assert (await repo.get_entry(outcome.key)).status == "needs_review"
    channel.ensure_group.assert_not_called()
    channel.deliver_file.assert_not_called()


async def test_reuse_policy_creates_group_for_truly_new_account(tmp_path: Path) -> None:
    repo, engine, channel, _ = await _setup(tmp_path)
    outcome = await run_once(
        engine=engine,
        config=_config(tmp_path),
        round_name="weekly-2026-09-27",
        channel=channel,
    )
    assert outcome.status == "completed"
    assert (await repo.get_entry(outcome.key)).chat_id == "oc_new"
    engine.delivery_ledger.adopt_prior_verified_group_for_round.assert_awaited_once()
    channel.ensure_group.assert_awaited_once()
    channel.ensure_topic.assert_awaited_once()


async def test_existing_round_chat_does_not_adopt_another_group(tmp_path: Path) -> None:
    repo, engine, channel, _ = await _setup(tmp_path, chat_id="oc_prior")
    engine.delivery_ledger.get_group.return_value = _ready_group()
    channel.ensure_group.return_value = _ready_group()
    channel.ensure_topic.return_value = _ready_group()
    outcome = await run_once(
        engine=engine,
        config=_config(tmp_path),
        round_name="weekly-2026-09-27",
        channel=channel,
    )
    assert outcome.status == "completed"
    assert (await repo.get_entry(outcome.key)).chat_id == "oc_prior"
    engine.delivery_ledger.adopt_prior_verified_group_for_round.assert_not_called()
