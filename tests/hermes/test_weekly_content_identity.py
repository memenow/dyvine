"""Media content identity controls weekly dedupe and permanent holds."""

from __future__ import annotations

from datetime import date
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dyvine.services.queue import QueueService
from dyvine_hermes.weekly import WeeklyConfig, run_once
from tests.fake_repos import FakeQueueRepository


@pytest.mark.parametrize(
    ("old_status", "current_bytes", "expected_status", "should_send"),
    [
        ("sent", b"old", "completed", False),
        ("sent", b"new", "needs_review", False),
        ("permanent_failure", b"new", "completed", True),
        ("permanent_failure", b"old", "permanent_failure", False),
    ],
)
async def test_modern_record_only_resolves_matching_file_content(
    tmp_path: Path,
    old_status: str,
    current_bytes: bytes,
    expected_status: str,
    should_send: bool,
) -> None:
    """Changed sent paths stop; changed failed paths can safely be retried."""
    user_dir = tmp_path / "Account"
    post_dir = user_dir / "2026-09-13 09-00-00 post"
    post_dir.mkdir(parents=True)
    file_path = post_dir / "clip.mp4"
    file_path.write_bytes(current_bytes)
    relative = file_path.relative_to(user_dir).as_posix()
    old = SimpleNamespace(
        round="weekly0913",
        relative_path=relative,
        content_sha256=sha256(b"old").hexdigest(),
        status=old_status,
    )
    new: list[SimpleNamespace] = []

    async def list_files(**kwargs: object) -> list[SimpleNamespace]:
        if kwargs.get("round") == "legacy":
            return []
        if kwargs.get("status") == old_status:
            return [old]
        if kwargs.get("status"):
            return []
        return [old, *new]

    async def deliver(**_kwargs: object) -> SimpleNamespace:
        record = SimpleNamespace(
            round="weekly0913",
            relative_path=relative,
            content_sha256=sha256(current_bytes).hexdigest(),
            status="sent",
        )
        new.append(record)
        return record

    repo = FakeQueueRepository()
    await repo.upsert_entry(
        key="weekly0913:sec_1",
        round="weekly0913",
        nickname="Account",
        sec_user_id="sec_1",
        mode="incremental",
        status="pending",
        extra={"weekly": {"download_complete": True, "user_dir": str(user_dir)}},
    )
    group = SimpleNamespace(
        status="ready",
        chat_id="oc_new",
        topic_status="ready",
        topic_message_id="om_topic",
    )
    ledger = SimpleNamespace(
        list_files=AsyncMock(side_effect=list_files),
        get_group=AsyncMock(return_value=None),
        adopt_prior_verified_group_for_round=AsyncMock(return_value=None),
    )
    engine = SimpleNamespace(
        queue=QueueService(queue=repo, seeds=AsyncMock(), rounds=AsyncMock()),
        delivery_ledger=ledger,
        profiles=SimpleNamespace(
            get_profile=AsyncMock(
                return_value=SimpleNamespace(
                    avatar_url="https://example.com/avatar.png"
                )
            )
        ),
    )
    channel = SimpleNamespace(
        ensure_group=AsyncMock(return_value=group),
        ensure_topic=AsyncMock(return_value=group),
        deliver_file=AsyncMock(side_effect=deliver),
    )
    config = WeeklyConfig(
        timezone="Asia/Shanghai",
        owner_open_id="ou_recipient",
        download_root=tmp_path,
        first_auto_date=date(2026, 9, 27),
        cutover_round="weekly0913",
    )
    outcome = await run_once(
        engine=engine, config=config, round_name="weekly0913", channel=channel
    )
    assert outcome.status == expected_status
    if should_send:
        channel.deliver_file.assert_awaited_once()
    else:
        channel.deliver_file.assert_not_called()
