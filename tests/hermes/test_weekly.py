"""Focused safety checks for the single-shot weekly runner."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from dyvine.services.queue import QueueService
from dyvine_hermes import weekly as weekly_module
from dyvine_hermes import weekly_account as weekly_account_module
from dyvine_hermes.weekly import WeeklyConfig, due_round, run_once
from tests.fake_repos import (
    FakeQueueRepository,
    FakeRoundRepository,
    FakeSeedRepository,
)


def _config(
    root: Path,
    *,
    first_auto_date: date = date(2026, 9, 20),
    cutover_round: str = "weekly0913",
) -> WeeklyConfig:
    return WeeklyConfig(
        timezone="Asia/Shanghai",
        owner_open_id="ou_recipient",
        download_root=root,
        first_auto_date=first_auto_date,
        cutover_round=cutover_round,
    )


def _engine(repo: FakeQueueRepository) -> SimpleNamespace:
    return SimpleNamespace(
        queue=QueueService(queue=repo, seeds=AsyncMock(), rounds=AsyncMock()),
        round_repo=SimpleNamespace(list_rounds=AsyncMock(return_value=[])),
        delivery_ledger=SimpleNamespace(
            get_group=AsyncMock(return_value=None),
            adopt_prior_verified_group_for_round=AsyncMock(return_value=None),
            list_files=AsyncMock(return_value=[]),
            list_excluded_nicknames=AsyncMock(return_value=set()),
        ),
        profiles=SimpleNamespace(
            get_profile=AsyncMock(
                return_value=SimpleNamespace(
                    avatar_url="https://example.com/avatar.png"
                )
            )
        ),
        users=SimpleNamespace(
            get_user_info=AsyncMock(),
            get_author_state=AsyncMock(
                return_value=SimpleNamespace(available=True, reason=None)
            ),
        ),
        posts=SimpleNamespace(download_new_posts=AsyncMock()),
        operations=SimpleNamespace(get_operation=AsyncMock()),
    )


async def _entry(
    repo: FakeQueueRepository,
    *,
    root: Path,
    extra: dict | None = None,
    chat_id: str | None = None,
    cutoff: str | None = None,
) -> None:
    await repo.upsert_entry(
        key="weekly0913:sec_1",
        round="weekly0913",
        nickname="Account",
        sec_user_id="sec_1",
        mode="incremental",
        status="pending",
        chat_id=chat_id,
        cutoff=cutoff,
        extra=(
            extra
            if extra is not None
            else {
                "weekly": {
                    "download_complete": True,
                    "fresh_download_confirmed": True,
                    "user_dir": str(root),
                }
            }
        ),
    )


def test_due_round_uses_explicit_shanghai_sunday_eight() -> None:
    before = datetime(2026, 9, 26, 23, 59, tzinfo=UTC)
    after = datetime(2026, 9, 27, 0, 0, tzinfo=UTC)
    name_before, due_before = due_round(before, "Asia/Shanghai")
    name_after, due_after = due_round(after, "Asia/Shanghai")
    assert name_before == "weekly-2026-09-20"
    assert due_before.isoformat() == "2026-09-20T08:00:00+08:00"
    assert name_after == "weekly-2026-09-27"
    assert due_after.isoformat() == "2026-09-27T08:00:00+08:00"


async def test_dry_run_does_not_claim_or_send(tmp_path: Path) -> None:
    repo = FakeQueueRepository()
    await _entry(repo, root=tmp_path)
    engine = _engine(repo)
    outcome = await run_once(
        engine=engine, config=_config(tmp_path), round_name="weekly0913", dry_run=True
    )
    assert outcome.status == "dry_run"
    assert outcome.key == "weekly0913:sec_1"
    assert (await repo.get_entry(outcome.key)).status == "pending"
    engine.posts.download_new_posts.assert_not_called()
    engine.delivery_ledger.get_group.assert_not_called()


async def test_run_once_cancels_step_after_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = FakeQueueRepository()
    await _entry(repo, root=tmp_path)
    engine = _engine(repo)

    async def slow_step(*_args: object) -> None:
        await asyncio.sleep(0.1)

    monkeypatch.setattr(weekly_module, "process_entry", slow_step)
    monkeypatch.setattr(weekly_module, "MAX_RUN_SECONDS", 0.01)
    outcome = await run_once(
        engine=engine, config=_config(tmp_path), round_name="weekly0913"
    )
    assert outcome.status == "pending"
    saved = await repo.get_entry("weekly0913:sec_1")
    assert saved.status == "pending"
    assert saved.attempts == 1


async def test_migration_marker_blocks_every_external_step(tmp_path: Path) -> None:
    repo = FakeQueueRepository()
    await _entry(repo, root=tmp_path, extra={"migration_needs_reconciliation": True})
    engine = _engine(repo)
    outcome = await run_once(
        engine=engine, config=_config(tmp_path), round_name="weekly0913"
    )
    assert outcome.status == "needs_review"
    assert (await repo.get_entry("weekly0913:sec_1")).status == "needs_review"
    engine.posts.download_new_posts.assert_not_called()
    engine.delivery_ledger.get_group.assert_not_called()


async def test_legacy_chat_without_adopted_ledger_blocks_download(
    tmp_path: Path,
) -> None:
    repo = FakeQueueRepository()
    await _entry(repo, root=tmp_path, chat_id="oc_old")
    engine = _engine(repo)
    outcome = await run_once(
        engine=engine, config=_config(tmp_path), round_name="weekly0913"
    )
    assert outcome.status == "needs_review"
    assert (await repo.get_entry("weekly0913:sec_1")).status == "needs_review"
    engine.posts.download_new_posts.assert_not_called()


async def test_unfinished_first_pair_prevents_next_pair_claim(tmp_path: Path) -> None:
    repo = FakeQueueRepository()
    for sec, status in (
        ("sec_1", "pending"),
        ("sec_2", "completed"),
        ("sec_3", "pending"),
    ):
        await repo.upsert_entry(
            key=f"weekly0913:{sec}",
            round="weekly0913",
            nickname=sec,
            sec_user_id=sec,
            mode="incremental",
            status=status,
            extra={"migration_needs_reconciliation": sec == "sec_1"},
        )
    await repo.update_entry("weekly0913:sec_1", status="pending")
    engine = _engine(repo)
    outcome = await run_once(
        engine=engine, config=_config(tmp_path), round_name="weekly0913"
    )
    assert outcome.key == "weekly0913:sec_1"
    assert (await repo.get_entry("weekly0913:sec_3")).status == "pending"


@pytest.mark.parametrize(
    ("budget", "spent"), [("PAIR_START_WINDOW_SECONDS", 0), ("MAX_STEPS_PER_RUN", 2)]
)
async def test_spent_run_budget_finishes_current_pair_without_claiming_third(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, budget: str, spent: int
) -> None:
    """A closed start window or a used-up step cap holds the next pair."""
    monkeypatch.setattr(weekly_module, budget, spent)
    repo = FakeQueueRepository()
    for index in range(1, 4):
        user_dir = tmp_path / f"Account-{index}"
        user_dir.mkdir()
        await repo.upsert_entry(
            key=f"weekly0913:sec_{index}",
            round="weekly0913",
            nickname=f"Account-{index}",
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
    engine = _engine(repo)
    outcome = await run_once(
        engine=engine, config=_config(tmp_path), round_name="weekly0913"
    )
    assert outcome.status == "pair_complete"
    assert outcome.note == "pairs=1 steps=2"
    assert (await repo.get_entry("weekly0913:sec_1")).status == "completed"
    assert (await repo.get_entry("weekly0913:sec_2")).status == "completed"
    assert (await repo.get_entry("weekly0913:sec_3")).status == "pending"


async def test_auto_round_waits_for_prior_reconciliation(tmp_path: Path) -> None:
    repo = FakeQueueRepository()
    await repo.upsert_entry(
        key="weekly0913:sec_1",
        round="weekly0913",
        nickname="Account",
        sec_user_id="sec_1",
        mode="incremental",
        status="needs_reconciliation",
    )
    engine = _engine(repo)
    engine.round_repo.list_rounds = AsyncMock(
        return_value=[SimpleNamespace(round="weekly0913")]
    )
    outcome = await run_once(
        engine=engine,
        config=_config(tmp_path, first_auto_date=date(2026, 9, 27)),
        now=datetime(2026, 9, 27, 0, 0, tzinfo=UTC),
    )
    assert outcome.status == "blocked_by_prior_round"
    assert outcome.round == "weekly-2026-09-27"
    engine.posts.download_new_posts.assert_not_called()


async def test_cutover_anchor_skips_legacy_sunday_but_allows_manual_round(
    tmp_path: Path,
) -> None:
    repo = FakeQueueRepository()
    await _entry(repo, root=tmp_path, extra={"migration_needs_reconciliation": True})
    engine = _engine(repo)
    config = _config(tmp_path, first_auto_date=date(2026, 9, 27))
    automatic = await run_once(
        engine=engine,
        config=config,
        now=datetime(2026, 9, 20, 0, 0, tzinfo=UTC),
    )
    assert automatic.status == "before_cutover"
    assert automatic.round == "weekly-2026-09-20"
    assert (await repo.get_entry("weekly0913:sec_1")).status == "pending"
    manual = await run_once(engine=engine, config=config, round_name="weekly0913")
    assert manual.status == "needs_review"
    assert manual.key == "weekly0913:sec_1"


async def test_auto_round_honors_legacy_nickname_exclusion(tmp_path: Path) -> None:
    repo = FakeQueueRepository()
    seeds = FakeSeedRepository()
    rounds = FakeRoundRepository()
    queue = QueueService(queue=repo, seeds=seeds, rounds=rounds)
    await queue.import_seeds([{"sec_user_id": "sec_1", "nickname": "Excluded"}])
    engine = _engine(repo)
    engine.queue = queue
    engine.round_repo = rounds
    engine.delivery_ledger.list_excluded_nicknames = AsyncMock(
        return_value={"Excluded"}
    )
    outcome = await run_once(
        engine=engine,
        config=_config(tmp_path, first_auto_date=date(2026, 9, 27)),
        now=datetime(2026, 9, 27, 0, 0, tzinfo=UTC),
    )
    assert outcome.status == "idle"
    assert await repo.count_entries(round="weekly-2026-09-27") == 0
    engine.delivery_ledger.list_excluded_nicknames.assert_awaited_once()


async def test_one_account_sends_only_unresolved_media_then_completes(
    tmp_path: Path,
) -> None:
    user_dir = tmp_path / "douyin" / "Account"
    post_dir = user_dir / "2026-09-13 09-00-00 post"
    post_dir.mkdir(parents=True)
    video = post_dir / "clip.mp4"
    video.write_bytes(b"video")
    repo = FakeQueueRepository()
    await _entry(repo, root=user_dir, cutoff="2026-09-12T08:00:00")
    engine = _engine(repo)
    group = SimpleNamespace(
        status="ready",
        chat_id="oc_new",
        topic_status="ready",
        topic_message_id="om_topic",
    )
    records: list[SimpleNamespace] = []

    async def deliver(**kwargs: object) -> SimpleNamespace:
        record = SimpleNamespace(
            status="sent", relative_path=str(video.relative_to(user_dir))
        )
        records.append(record)
        return record

    async def _list_records(**kwargs: object) -> list[SimpleNamespace]:
        if kwargs.get("offset"):
            return []
        return list(records)

    engine.delivery_ledger.list_files = AsyncMock(side_effect=_list_records)
    channel = SimpleNamespace(
        ensure_group=AsyncMock(return_value=group),
        ensure_topic=AsyncMock(return_value=group),
        deliver_file=AsyncMock(side_effect=deliver),
    )
    outcome = await run_once(
        engine=engine,
        config=_config(tmp_path),
        round_name="weekly0913",
        channel=channel,
    )
    assert outcome.status == "completed"
    assert outcome.files == 1
    assert (await repo.get_entry("weekly0913:sec_1")).chat_id == "oc_new"
    assert (await repo.get_entry("weekly0913:sec_1")).status == "completed"
    channel.deliver_file.assert_awaited_once()
    assert "parent_id" not in channel.deliver_file.await_args.kwargs
    engine.posts.download_new_posts.assert_not_called()


async def test_parked_partner_does_not_mark_a_finished_pair_for_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(weekly_account_module, "MAX_FILES_PER_RUN", 1)
    user_dir = tmp_path / "Account"
    post_dir = user_dir / "2026-09-13 09-00-00 post"
    post_dir.mkdir(parents=True)
    for name in ("a.mp4", "b.mp4"):
        (post_dir / name).write_bytes(name.encode())
    repo = FakeQueueRepository()
    await _entry(repo, root=user_dir, cutoff="2026-09-12T08:00:00")
    await repo.upsert_entry(
        key="weekly0913:sec_2",
        round="weekly0913",
        nickname="Parked",
        sec_user_id="sec_2",
        mode="incremental",
        status="needs_reconciliation",
    )
    engine = _engine(repo)
    records: list[SimpleNamespace] = []

    async def deliver(**kwargs: Path) -> SimpleNamespace:
        path = kwargs["file_path"]
        record = SimpleNamespace(
            round="weekly0913",
            status="sent",
            relative_path=path.relative_to(user_dir).as_posix(),
            content_sha256=sha256(path.read_bytes()).hexdigest(),
        )
        records.append(record)
        return record

    async def list_files(**kwargs: Any) -> list[SimpleNamespace]:
        if kwargs.get("round") == "legacy":
            return []
        rows = [r for r in records if kwargs.get("status") in (None, r.status)]
        offset = kwargs.get("offset", 0) or 0
        limit = kwargs.get("limit", -1)
        rows = rows[offset:]
        return rows if limit is None or limit < 0 else rows[:limit]

    engine.delivery_ledger.list_files = AsyncMock(side_effect=list_files)
    group = SimpleNamespace(
        status="ready",
        chat_id="oc_new",
        topic_status="ready",
        topic_message_id="om_topic",
    )
    channel = SimpleNamespace(
        ensure_group=AsyncMock(return_value=group),
        ensure_topic=AsyncMock(return_value=group),
        deliver_file=AsyncMock(side_effect=deliver),
    )
    # Later steps find the group the first step recorded in the ledger.
    engine.delivery_ledger.get_group = AsyncMock(
        side_effect=lambda **_: group if channel.ensure_group.await_count else None
    )
    outcome = await run_once(
        engine=engine,
        config=_config(tmp_path),
        round_name="weekly0913",
        channel=channel,
    )
    assert outcome.status == "pair_complete"
    assert outcome.note == "pairs=1 steps=2"
    assert outcome.files == 2
    assert (await repo.get_entry("weekly0913:sec_2")).status == "needs_reconciliation"


async def test_prior_send_under_an_edited_caption_is_not_counted_as_sent(
    tmp_path: Path,
) -> None:
    user_dir = tmp_path / "Account"
    stamp = "2026-09-13 09-00-00"
    post_dir = user_dir / f"{stamp}_new caption"
    post_dir.mkdir(parents=True)
    (post_dir / f"{stamp}_new caption_video.mp4").write_bytes(b"video")
    repo = FakeQueueRepository()
    await _entry(repo, root=user_dir, cutoff="2026-09-12T08:00:00")
    engine = _engine(repo)
    group = SimpleNamespace(
        status="ready",
        chat_id="oc_new",
        topic_status="ready",
        topic_message_id="om_topic",
    )
    prior = SimpleNamespace(
        status="sent",
        relative_path=f"{stamp}_old caption/{stamp}_old caption_video.mp4",
    )
    channel = SimpleNamespace(
        ensure_group=AsyncMock(return_value=group),
        ensure_topic=AsyncMock(return_value=group),
        deliver_file=AsyncMock(return_value=prior),
    )
    outcome = await run_once(
        engine=engine,
        config=_config(tmp_path),
        round_name="weekly0913",
        channel=channel,
    )
    assert outcome.status == "completed"
    assert outcome.files == 0
    channel.deliver_file.assert_awaited_once()


async def test_post_covered_media_never_takes_the_send_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(weekly_account_module, "MAX_FILES_PER_RUN", 2)
    user_dir = tmp_path / "Account"
    adopted, fresh = "2026-09-13 09-00-00", "2026-09-14 09-00-00"
    adopted_dir = user_dir / f"{adopted}_caption"
    adopted_dir.mkdir(parents=True)
    for index in (1, 2, 3):
        (adopted_dir / f"{adopted}_caption_image_{index}.webp").write_bytes(
            f"image {index}".encode()
        )
    fresh_dir = user_dir / f"{fresh}_clip"
    fresh_dir.mkdir()
    video = fresh_dir / f"{fresh}_clip_video.mp4"
    video.write_bytes(b"video")
    repo = FakeQueueRepository()
    await _entry(repo, root=user_dir, cutoff="2026-09-12T08:00:00")
    engine = _engine(repo)
    post_level = SimpleNamespace(
        relative_path=f"{adopted}_feishu/{adopted}_feishu_post",
        status="legacy_confirmed_sent",
    )

    async def list_files(**kwargs: object) -> list[SimpleNamespace]:
        if kwargs.get("offset"):
            return []
        return [post_level] if kwargs.get("status") == "legacy_confirmed_sent" else []

    engine.delivery_ledger.list_files = AsyncMock(side_effect=list_files)
    group = SimpleNamespace(
        status="ready",
        chat_id="oc_new",
        topic_status="ready",
        topic_message_id="om_topic",
    )

    async def deliver_file(**kwargs: Path) -> SimpleNamespace:
        relative = kwargs["file_path"].relative_to(user_dir).as_posix()
        return SimpleNamespace(status="sent", relative_path=relative)

    channel = SimpleNamespace(
        ensure_group=AsyncMock(return_value=group),
        ensure_topic=AsyncMock(return_value=group),
        deliver_file=AsyncMock(side_effect=deliver_file),
    )
    outcome = await run_once(
        engine=engine,
        config=_config(tmp_path),
        round_name="weekly0913",
        channel=channel,
    )
    assert (outcome.status, outcome.files) == ("completed", 1)
    channel.deliver_file.assert_awaited_once()
    assert channel.deliver_file.await_args.kwargs["file_path"] == video


async def test_legacy_confirmed_path_completes_without_new_group(
    tmp_path: Path,
) -> None:
    user_dir = tmp_path / "Account"
    post_dir = user_dir / "2026-09-13 09-00-00 post"
    post_dir.mkdir(parents=True)
    video = post_dir / "clip.mp4"
    video.write_bytes(b"video")
    repo = FakeQueueRepository()
    await _entry(repo, root=user_dir)
    engine = _engine(repo)
    historical = SimpleNamespace(
        relative_path=video.relative_to(user_dir).as_posix(),
        status="legacy_confirmed_sent",
    )

    async def list_files(**kwargs: object) -> list[SimpleNamespace]:
        if kwargs.get("offset"):
            return []
        return [historical] if kwargs.get("status") == "legacy_confirmed_sent" else []

    engine.delivery_ledger.list_files = AsyncMock(side_effect=list_files)
    channel = SimpleNamespace(
        ensure_group=AsyncMock(), ensure_topic=AsyncMock(), deliver_file=AsyncMock()
    )
    outcome = await run_once(
        engine=engine,
        config=_config(tmp_path),
        round_name="weekly0913",
        channel=channel,
    )
    assert outcome.status == "completed"
    channel.ensure_group.assert_not_called()
    channel.deliver_file.assert_not_called()


async def test_legacy_permanent_failure_is_not_sent_or_completed(
    tmp_path: Path,
) -> None:
    user_dir = tmp_path / "Account"
    post_dir = user_dir / "2026-09-13 09-00-00 post"
    post_dir.mkdir(parents=True)
    video = post_dir / "clip.mp4"
    video.write_bytes(b"video")
    repo = FakeQueueRepository()
    await _entry(repo, root=user_dir)
    engine = _engine(repo)
    failure = SimpleNamespace(
        relative_path=video.relative_to(user_dir).as_posix(),
        status="permanent_failure",
    )

    async def list_files(**kwargs: object) -> list[SimpleNamespace]:
        if kwargs.get("offset"):
            return []
        if kwargs.get("round") == "legacy" and kwargs.get("status") == (
            "permanent_failure"
        ):
            return [failure]
        return []

    engine.delivery_ledger.list_files = AsyncMock(side_effect=list_files)
    channel = SimpleNamespace(
        ensure_group=AsyncMock(), ensure_topic=AsyncMock(), deliver_file=AsyncMock()
    )
    outcome = await run_once(
        engine=engine,
        config=_config(tmp_path),
        round_name="weekly0913",
        channel=channel,
    )
    assert outcome.status == "permanent_failure"
    assert (await repo.get_entry("weekly0913:sec_1")).status == "permanent_failure"
    channel.ensure_group.assert_not_called()
    channel.deliver_file.assert_not_called()


async def test_legacy_failure_does_not_hide_other_unsent_media(tmp_path: Path) -> None:
    user_dir = tmp_path / "Account"
    post_dir = user_dir / "2026-09-13 09-00-00 post"
    post_dir.mkdir(parents=True)
    failed = post_dir / "failed.mp4"
    failed.write_bytes(b"failed")
    unsent = post_dir / "unsent.mp4"
    unsent.write_bytes(b"unsent")
    repo = FakeQueueRepository()
    await _entry(repo, root=user_dir)
    engine = _engine(repo)
    failure = SimpleNamespace(
        relative_path=failed.relative_to(user_dir).as_posix(),
        status="permanent_failure",
    )

    async def list_files(**kwargs: object) -> list[SimpleNamespace]:
        if kwargs.get("offset"):
            return []
        if kwargs.get("round") == "legacy" and kwargs.get("status") == (
            "permanent_failure"
        ):
            return [failure]
        return []

    engine.delivery_ledger.list_files = AsyncMock(side_effect=list_files)
    group = SimpleNamespace(
        status="ready",
        chat_id="oc_new",
        topic_status="ready",
        topic_message_id="om_topic",
    )
    channel = SimpleNamespace(
        ensure_group=AsyncMock(return_value=group),
        ensure_topic=AsyncMock(return_value=group),
        deliver_file=AsyncMock(
            return_value=SimpleNamespace(
                status="sent", relative_path=unsent.relative_to(user_dir).as_posix()
            )
        ),
    )
    outcome = await run_once(
        engine=engine,
        config=_config(tmp_path),
        round_name="weekly0913",
        channel=channel,
    )
    assert outcome.status == "permanent_failure"
    assert outcome.files == 1
    assert (await repo.get_entry("weekly0913:sec_1")).status == "permanent_failure"
    channel.deliver_file.assert_awaited_once()
    assert channel.deliver_file.await_args.kwargs["file_path"] == unsent


async def test_ambiguous_file_send_stops_without_second_send(tmp_path: Path) -> None:
    user_dir = tmp_path / "Account"
    post_dir = user_dir / "2026-09-13 09-00-00 post"
    post_dir.mkdir(parents=True)
    (post_dir / "one.mp4").write_bytes(b"one")
    (post_dir / "two.mp4").write_bytes(b"two")
    repo = FakeQueueRepository()
    await _entry(repo, root=user_dir)
    engine = _engine(repo)
    group = SimpleNamespace(
        status="ready",
        chat_id="oc_new",
        topic_status="ready",
        topic_message_id="om_topic",
    )
    channel = SimpleNamespace(
        ensure_group=AsyncMock(return_value=group),
        ensure_topic=AsyncMock(return_value=group),
        deliver_file=AsyncMock(return_value=SimpleNamespace(status="needs_review")),
    )
    outcome = await run_once(
        engine=engine,
        config=_config(tmp_path),
        round_name="weekly0913",
        channel=channel,
    )
    assert outcome.status == "needs_review"
    assert (await repo.get_entry("weekly0913:sec_1")).status == "needs_review"
    channel.deliver_file.assert_awaited_once()


async def test_missing_uploaded_file_keeps_account_open(tmp_path: Path) -> None:
    user_dir = tmp_path / "Account"
    user_dir.mkdir()
    repo = FakeQueueRepository()
    await _entry(repo, root=user_dir)
    engine = _engine(repo)

    async def _list_missing(**kwargs: object) -> list[SimpleNamespace]:
        if kwargs.get("offset"):
            return []
        return [SimpleNamespace(relative_path="missing.mp4", status="uploaded")]

    engine.delivery_ledger.list_files = AsyncMock(side_effect=_list_missing)
    outcome = await run_once(
        engine=engine, config=_config(tmp_path), round_name="weekly0913"
    )
    assert outcome.status == "needs_review"
    assert (await repo.get_entry("weekly0913:sec_1")).status == "needs_review"


async def test_sending_intent_requeues_for_same_uuid_retry(tmp_path: Path) -> None:
    user_dir = tmp_path / "Account"
    post_dir = user_dir / "2026-09-13 09-00-00 post"
    post_dir.mkdir(parents=True)
    (post_dir / "clip.mp4").write_bytes(b"clip")
    repo = FakeQueueRepository()
    await _entry(repo, root=user_dir)
    engine = _engine(repo)
    group = SimpleNamespace(
        status="ready",
        chat_id="oc_new",
        topic_status="ready",
        topic_message_id="om_topic",
    )
    channel = SimpleNamespace(
        ensure_group=AsyncMock(return_value=group),
        ensure_topic=AsyncMock(return_value=group),
        deliver_file=AsyncMock(return_value=SimpleNamespace(status="sending")),
    )
    outcome = await run_once(
        engine=engine,
        config=_config(tmp_path),
        round_name="weekly0913",
        channel=channel,
    )
    assert outcome.status == "pending"
    assert (await repo.get_entry("weekly0913:sec_1")).status == "pending"
    channel.deliver_file.assert_awaited_once()


# ── P5-17/18/19/22/23/24/25/27/29 regressions ─────────────────────────────


def test_single_runner_lock_falls_back_to_msvcrt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without fcntl the Windows byte-lock path still excludes contenders."""
    from unittest.mock import MagicMock

    monkeypatch.setattr(weekly_module, "fcntl", None)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    msvcrt = MagicMock()
    msvcrt.LK_NBLCK = 1
    msvcrt.LK_UNLCK = 2
    monkeypatch.setattr(weekly_module, "msvcrt", msvcrt)
    with weekly_module.single_runner_lock() as acquired:
        assert acquired is True
    assert msvcrt.locking.call_count == 2  # lock + unlock
    msvcrt.locking.reset_mock()
    msvcrt.locking.side_effect = [OSError("held"), None]
    with weekly_module.single_runner_lock() as acquired:
        assert acquired is False


def test_single_runner_lock_warns_without_any_primitive(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With neither primitive the run proceeds loudly, never crashes."""
    monkeypatch.setattr(weekly_module, "fcntl", None)
    monkeypatch.setattr(weekly_module, "msvcrt", None)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    with weekly_module.single_runner_lock() as acquired:
        assert acquired is True
    assert "without host lock" in capsys.readouterr().err


async def test_current_pair_keys_pages_past_first_chunk() -> None:
    """Pair selection sees rows beyond the first 500 (no -1 sentinel)."""
    from dyvine_hermes.weekly import _current_pair_keys

    repo = FakeQueueRepository()
    for index in range(502):
        await repo.upsert_entry(
            key=f"r1:s{index:04d}",
            round="r1",
            nickname=f"s{index}",
            sec_user_id=f"s{index}",
            mode="incremental",
            status="completed" if index < 500 else "pending",
        )
    keys = await _current_pair_keys(SimpleNamespace(queue=repo), "r1")
    assert keys == {"r1:s0500", "r1:s0501"}


async def test_advance_claimed_dumps_traceback_on_unexpected_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Unexpected step failures park the row AND keep the traceback."""
    from dyvine_hermes.weekly import _advance_claimed

    repo = FakeQueueRepository()
    await _entry(repo, root=tmp_path / "Account", extra={"weekly": {}})
    engine = _engine(repo)
    engine.posts.download_new_posts = AsyncMock(side_effect=RuntimeError("boom-x"))
    engine.operations.create_operation = AsyncMock(
        return_value=SimpleNamespace(operation_id="op-1")
    )
    entry = await repo.get_entry("weekly0913:sec_1")
    config = _config(tmp_path)
    with pytest.raises(RuntimeError, match="boom-x"):
        await _advance_claimed(
            engine, entry, config, __import__("time").monotonic() + 3600, None
        )
    assert (await repo.get_entry("weekly0913:sec_1")).status == "needs_review"
    assert "boom-x" in capsys.readouterr().err


def test_run_cli_reports_lock_skip_on_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A contended lock skips with a stderr line (stdout stays empty)."""
    from contextlib import contextmanager

    from dyvine_hermes.weekly import run_cli

    @contextmanager
    def _held():  # type: ignore[no-untyped-def]
        yield False

    monkeypatch.setattr(weekly_module, "single_runner_lock", _held)

    def _no_engine() -> None:
        raise AssertionError("engine must not boot on a skip")

    monkeypatch.setattr("dyvine_hermes.context.get_engine", _no_engine)
    assert run_cli(round_name="weekly0913", dry_run=False) is None
    captured = capsys.readouterr()
    assert "holds the lock" in captured.err
    assert captured.out == ""


async def test_op_issue_carries_cause_note(tmp_path: Path) -> None:
    """op_issue outcomes keep the recorded reason for the CLI alert."""
    repo = FakeQueueRepository()
    await _entry(repo, root=tmp_path / "Account", extra={"weekly": {}})
    engine = _engine(repo)
    result = SimpleNamespace(
        operation_id="op-1", failed_count=1, truncated=False, new_count=0
    )
    engine.posts.download_new_posts = AsyncMock(return_value=result)
    engine.operations.create_operation = AsyncMock(
        return_value=SimpleNamespace(operation_id="op-1")
    )
    engine.operations.get_operation = AsyncMock(
        return_value=SimpleNamespace(status="completed")
    )
    outcome = await run_once(
        engine=engine, config=_config(tmp_path), round_name="weekly0913"
    )
    assert outcome.status == "op_issue"
    assert outcome.note == "Incremental download is incomplete; inspect before delivery"


async def test_stale_running_operation_is_superseded_without_raise(
    tmp_path: Path,
) -> None:
    """A reclaimed row's stale op fails loudly even at attempts 0. (P5-27)"""
    from dyvine_hermes.weekly_download import download_entry

    repo = FakeQueueRepository()
    await _entry(repo, root=tmp_path / "Account", extra={"weekly": {}})
    await repo.update_entry("weekly0913:sec_1", operation_id="op-stale")
    engine = _engine(repo)
    stale = SimpleNamespace(
        operation_id="op-stale",
        subject_id="sec_1",
        operation_type="user_posts_incremental_download",
        status="running",
        metadata={},
    )
    engine.operations.get_operation = AsyncMock(return_value=stale)
    engine.operations.update_operation = AsyncMock()
    engine.operations.create_operation = AsyncMock(
        return_value=SimpleNamespace(operation_id="op-new")
    )
    engine.posts.download_new_posts = AsyncMock(
        return_value=SimpleNamespace(
            operation_id="op-new",
            failed_count=0,
            truncated=False,
            new_count=0,
            newest_aweme_id=None,
        )
    )
    entry = await repo.get_entry("weekly0913:sec_1")
    assert entry.attempts == 0
    user_dir = tmp_path / "Account"
    user_dir.mkdir()
    fresh = SimpleNamespace(
        status="completed", download_path=str(user_dir), message="done"
    )

    async def _get_op(operation_id: str) -> Any:
        if operation_id == "op-stale":
            return stale
        return fresh

    engine.operations.get_operation = AsyncMock(side_effect=_get_op)
    config = _config(tmp_path)
    advanced = await download_entry(
        engine, entry, config, __import__("time").monotonic() + 3600
    )
    engine.operations.update_operation.assert_awaited_once()
    assert advanced.status == "downloading"


async def test_download_stops_a_send_window_early(tmp_path: Path) -> None:
    """Downloads requeue when only the send window remains. (P5-29)"""
    from dyvine_hermes.weekly_download import download_entry

    repo = FakeQueueRepository()
    await _entry(repo, root=tmp_path / "Account", extra={"weekly": {}})
    engine = _engine(repo)
    engine.operations.create_operation = AsyncMock()
    entry = await repo.get_entry("weekly0913:sec_1")
    config = _config(tmp_path)
    advanced = await download_entry(
        engine, entry, config, __import__("time").monotonic() + 300
    )
    assert advanced.status == "pending"
    engine.operations.create_operation.assert_not_called()


async def test_list_all_files_pages_without_sentinel() -> None:
    """Ledger scans page in bounded chunks. (P5-24)"""
    from dyvine_hermes.weekly_account import _list_all_files

    seen: list[dict[str, Any]] = []
    rows = [SimpleNamespace(media_id=f"m{i}") for i in range(600)]

    async def _list_files(**kwargs: object) -> list[SimpleNamespace]:
        seen.append(dict(kwargs))
        offset = kwargs.get("offset", 0)
        limit = kwargs.get("limit", 500)
        assert isinstance(offset, int) and isinstance(limit, int)
        return rows[offset : offset + limit]

    ledger = SimpleNamespace(list_files=_list_files)
    assert await _list_all_files(ledger, round="r1") == rows
    assert [call["offset"] for call in seen] == [0, 500, 600]
    assert all(call["limit"] == 500 for call in seen)


async def test_same_content_hashes_off_the_event_loop(tmp_path: Path) -> None:
    """Reconciliation hashing runs in a worker thread. (P5-23)"""
    import asyncio

    from dyvine.services.delivery_durable import media_identity

    calls: list[str] = []
    real_to_thread = asyncio.to_thread

    async def _spy(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        calls.append(getattr(func, "__name__", "?"))
        return await real_to_thread(func, *args, **kwargs)

    user_dir = tmp_path / "Account"
    post_dir = user_dir / "2026-09-13 09-00-00 post"
    post_dir.mkdir(parents=True)
    video = post_dir / "clip.mp4"
    video.write_bytes(b"video")
    digest = media_identity(sec_user_id="sec_1", user_dir=user_dir, file_path=video)[2]
    repo = FakeQueueRepository()
    await _entry(repo, root=user_dir)
    engine = _engine(repo)
    historical = SimpleNamespace(
        relative_path=video.relative_to(user_dir).as_posix(),
        status="sent",
        content_sha256=digest,
    )

    async def _list_files(**kwargs: object) -> list[SimpleNamespace]:
        if kwargs.get("offset"):
            return []
        return [historical] if kwargs.get("status") == "sent" else []

    engine.delivery_ledger.list_files = AsyncMock(side_effect=_list_files)
    group = SimpleNamespace(
        status="ready",
        chat_id="oc_new",
        topic_status="ready",
        topic_message_id="om_topic",
    )
    channel = SimpleNamespace(
        ensure_group=AsyncMock(return_value=group),
        ensure_topic=AsyncMock(return_value=group),
        deliver_file=AsyncMock(),
    )
    saved_to_thread = asyncio.to_thread
    asyncio.to_thread = _spy  # type: ignore[method-assign]
    try:
        outcome = await run_once(
            engine=engine,
            config=_config(tmp_path),
            round_name="weekly0913",
            channel=channel,
        )
    finally:
        asyncio.to_thread = saved_to_thread
    assert outcome.status == "completed"
    assert "media_identity" in calls
