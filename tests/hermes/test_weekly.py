"""Focused safety checks for the single-shot weekly runner."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
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
        users=SimpleNamespace(get_user_info=AsyncMock()),
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


async def test_one_run_finishes_current_pair_without_claiming_third(
    tmp_path: Path,
) -> None:
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

    engine.delivery_ledger.list_files = AsyncMock(side_effect=lambda **_: list(records))
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
    assert channel.deliver_file.await_args.kwargs["parent_id"] == "om_topic"
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

    async def list_files(**kwargs: str) -> list[SimpleNamespace]:
        if kwargs.get("round") == "legacy":
            return []
        return [r for r in records if kwargs.get("status") in (None, r.status)]

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
    assert outcome.note == "steps=2"
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
    engine.delivery_ledger.list_files = AsyncMock(
        return_value=[SimpleNamespace(relative_path="missing.mp4", status="uploaded")]
    )
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
