"""Postgres checkpoints for group and file delivery across process restarts."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from dyvine.db import (
    DatabaseSessionFactory,
    PostgresDeliveryLedgerRepository,
    PostgresQueueRepository,
)
from dyvine.db.delivery_ledger import post_media_slot

_STAMP = "2026-09-10 12-34-56"


@pytest.fixture
async def ledger(postgres_url: str):  # type: ignore[no-untyped-def]
    factory = DatabaseSessionFactory(postgres_url)
    async with factory.session() as session:
        async with session.begin():
            await session.execute(
                text(
                    "TRUNCATE TABLE delivery_files, delivery_groups, download_queue, "
                    "delivery_legacy_evidence, legacy_excluded_nicknames"
                )
            )
    try:
        yield PostgresDeliveryLedgerRepository(factory)
    finally:
        await factory.aclose()


async def test_group_and_topic_intents_survive_concurrent_reservation(
    ledger: PostgresDeliveryLedgerRepository,
) -> None:
    fields = {
        "round": "r1",
        "sec_user_id": "sec-1",
        "nickname": "nick",
        "owner_open_id": "ou_user",
    }
    first, second = await asyncio.gather(
        ledger.reserve_group(**fields), ledger.reserve_group(**fields)
    )
    assert first.create_uuid == second.create_uuid
    assert first.status == second.status == "creating"
    ready = await ledger.mark_group_ready(first.key, "chat-1")
    assert ready.chat_id == "chat-1"
    topic = await ledger.begin_topic(first.key)
    assert topic.topic_status == "creating"
    assert topic.topic_uuid == (await ledger.begin_topic(first.key)).topic_uuid
    assert (
        await ledger.mark_topic_ready(first.key, "topic-1")
    ).topic_message_id == "topic-1"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (f"{_STAMP}_a/{_STAMP}_a_video.mp4", (_STAMP, "_video.mp4")),
        (f"{_STAMP}_a b/{_STAMP}_a b_image_3.webp", (_STAMP, "_image_3.webp")),
        ("clip.mp4", None),
        (f"{_STAMP}_a/nested/{_STAMP}_a_video.mp4", None),
        (f"{_STAMP}_a/{_STAMP}_b_video.mp4", None),
        (f"{_STAMP}_a/{_STAMP}_a", None),
        ("not-a-stamp_a/not-a-stamp_a_video.mp4", None),
    ],
)
def test_post_media_slot_reads_only_f2_post_media_paths(
    path: str, expected: tuple[str, str] | None
) -> None:
    assert post_media_slot(path) == expected


async def test_legacy_send_matches_the_same_media_under_an_edited_caption(
    ledger: PostgresDeliveryLedgerRepository,
) -> None:
    old = f"{_STAMP}_old caption/{_STAMP}_old caption_image_2.webp"
    rows: list[dict[str, str | None]] = [
        {
            "round": "weekly0913",
            "sec_user_id": "sec-1",
            "relative_path": old,
            "legacy_source_path": f"/old/{old}",
            "legacy_progress_file": "/old/progress.json",
        }
    ]
    assert await ledger.reserve_legacy_sent_batch(rows) == (1, 0)
    renamed = f"{_STAMP}_new caption/{_STAMP}_new caption_image_2.webp"
    found = await ledger.find_legacy_sent(sec_user_id="sec-1", relative_path=renamed)
    assert found is not None and found.relative_path == old
    for other in (
        renamed.replace("_image_2.webp", "_image_3.webp"),
        renamed.replace("12-34-56", "12-34-57"),
    ):
        assert (
            await ledger.find_legacy_sent(sec_user_id="sec-1", relative_path=other)
            is None
        )
    assert (
        await ledger.find_legacy_sent(sec_user_id="sec-2", relative_path=renamed)
        is None
    )


async def test_file_intent_and_legacy_import_are_idempotent(
    ledger: PostgresDeliveryLedgerRepository,
) -> None:
    fields = {
        "media_id": "media-1",
        "round": "r1",
        "sec_user_id": "sec-1",
        "relative_path": "date/clip.mp4",
        "content_sha256": "hash-1",
        "chat_id": "chat-1",
        "parent_id": "topic-1",
    }
    first, second = await asyncio.gather(
        ledger.reserve_file(**fields), ledger.reserve_file(**fields)
    )
    assert first.status == second.status == "planned"
    assert (await ledger.set_file_key("media-1", "key-1")).file_key == "key-1"
    intent = await ledger.begin_send("media-1")
    assert intent.status == "sending"
    assert intent.send_uuid == (await ledger.begin_send("media-1")).send_uuid
    assert (await ledger.mark_sent("media-1", "message-1")).status == "sent"
    assert (await ledger.get_file("media-1")).message_id == "message-1"

    rows: list[dict[str, str | None]] = [
        {
            "round": "old-round",
            "sec_user_id": "sec-1",
            "relative_path": "date/old.mp4",
            "legacy_source_path": "/old/date/old.mp4",
            "legacy_progress_file": "/old/progress.json",
        }
    ]
    assert await ledger.reserve_legacy_sent_batch(rows) == (1, 0)
    assert await ledger.reserve_legacy_sent_batch(rows) == (0, 1)
    legacy = await ledger.find_legacy_sent(
        sec_user_id="sec-1", relative_path="date/old.mp4"
    )
    assert legacy is not None and legacy.content_sha256 is None
    assert legacy.status == "legacy_confirmed_sent"
    assert legacy.legacy_source_path == "/old/date/old.mp4"

    evidence = await ledger.upsert_legacy_evidence(
        source_file="/old/progress.json",
        legacy_path="/old/unknown.mp4",
        legacy_state="sent_ambiguous",
        nickname="nick",
        sec_user_id=None,
        reason="nickname maps to multiple accounts",
    )
    duplicate = await ledger.upsert_legacy_evidence(
        source_file="/old/progress.json",
        legacy_path="/old/unknown.mp4",
        legacy_state="sent_ambiguous",
        nickname="nick",
        sec_user_id=None,
        reason="nickname maps to multiple accounts",
    )
    assert evidence.evidence_id == duplicate.evidence_id
    audit_rows: list[dict[str, str | None]] = [
        {
            "source_file": "/old/progress.json",
            "legacy_path": "/old/failed.mp4",
            "legacy_state": "failed",
            "nickname": "nick",
            "sec_user_id": "sec-1",
            "reason": "legacy failure requires review",
        }
    ]
    assert await ledger.upsert_legacy_evidence_batch(audit_rows) == (1, 0)
    assert await ledger.upsert_legacy_evidence_batch(audit_rows) == (0, 1)
    await ledger.upsert_excluded_nickname(
        nickname="excluded", source="excluded_accounts.json"
    )
    await ledger.upsert_excluded_nickname(
        nickname="excluded", source="excluded_accounts.json"
    )
    assert await ledger.list_excluded_nicknames() == {"excluded"}


async def test_legacy_permanent_failures_are_idempotent_and_preserve_sent(
    ledger: PostgresDeliveryLedgerRepository,
) -> None:
    rows: list[dict[str, str | None]] = [
        {
            "sec_user_id": "sec-1",
            "relative_path": "date/failed.mp4",
            "legacy_source_path": "/old/date/failed.mp4",
            "legacy_progress_file": "/old/permanent_failures.json",
        }
    ]
    assert await ledger.reserve_legacy_permanent_failure_batch(rows) == (1, 0)
    assert await ledger.reserve_legacy_permanent_failure_batch(rows) == (0, 1)
    failure = await ledger.find_legacy_permanent_failure(
        sec_user_id="sec-1", relative_path="date/failed.mp4"
    )
    assert failure is not None
    assert failure.status == "permanent_failure"
    assert failure.round == "legacy"
    assert failure.content_sha256 is None
    assert failure.legacy_source_path == "/old/date/failed.mp4"
    assert (
        await ledger.find_legacy_permanent_failure(
            sec_user_id="sec-2", relative_path="date/failed.mp4"
        )
        is None
    )

    sent_rows: list[dict[str, str | None]] = [
        {
            "round": "old-round",
            "sec_user_id": "sec-1",
            "relative_path": "date/sent.mp4",
            "legacy_source_path": "/old/date/sent.mp4",
        }
    ]
    assert await ledger.reserve_legacy_sent_batch(sent_rows) == (1, 0)
    assert await ledger.reserve_legacy_permanent_failure_batch(
        [
            {
                "sec_user_id": "sec-1",
                "relative_path": "date/sent.mp4",
            }
        ]
    ) == (0, 1)
    sent = await ledger.find_legacy_sent(
        sec_user_id="sec-1", relative_path="date/sent.mp4"
    )
    assert sent is not None and sent.status == "legacy_confirmed_sent"
    with pytest.raises(ValueError, match="account-relative"):
        await ledger.reserve_legacy_permanent_failure_batch(
            [{"sec_user_id": "sec-1", "relative_path": "../escaped.mp4"}]
        )
    with pytest.raises(ValueError, match="at most 500"):
        await ledger.reserve_legacy_permanent_failure_batch(rows * 501)


async def test_legacy_unverified_hold_matches_exact_path(
    ledger: PostgresDeliveryLedgerRepository,
) -> None:
    held_path = "/old/douyin/video/nick/date/clip.mp4"
    assert await ledger.find_legacy_unverified_hold(legacy_path=held_path) is None
    await ledger.upsert_legacy_evidence(
        source_file="/old/report_sent_index.json",
        legacy_path=held_path,
        legacy_state="sent_unverified",
        nickname="nick",
        sec_user_id=None,
        reason="source progress file is unavailable",
    )
    held = await ledger.find_legacy_unverified_hold(legacy_path=held_path)
    assert held is not None and held.legacy_state == "sent_unverified"
    assert held.sec_user_id is None
    assert (
        await ledger.find_legacy_unverified_hold(
            legacy_path="/old/douyin/video/other/date/clip.mp4"
        )
        is None
    )
    assert (
        await ledger.find_legacy_unverified_hold(legacy_path="/old/date/clip.mp4")
        is None
    )
    async with ledger._sessions.session() as session:
        indexes = (
            await session.execute(
                text(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE tablename = 'delivery_legacy_evidence'"
                )
            )
        ).scalars()
        assert "idx_delivery_legacy_evidence_path_state" in indexes.all()


async def test_legacy_group_adoption_requires_matching_queue(
    ledger: PostgresDeliveryLedgerRepository,
) -> None:
    fields = {
        "round": "r2",
        "sec_user_id": "sec-2",
        "nickname": "nick",
        "chat_id": "old-chat",
        "topic_message_id": "old-topic",
        "source_file": "/old/progress.json",
    }
    with pytest.raises(ValueError, match="migrated queue"):
        await ledger.import_legacy_group_topic(**fields)
    queue = PostgresQueueRepository(ledger._sessions, owner_id="test")
    await queue.upsert_entry(
        key="r2:sec-2",
        round="r2",
        nickname="nick",
        sec_user_id="sec-2",
        chat_id="old-chat",
        mode="post",
        status="completed",
    )
    group = await ledger.import_legacy_group_topic(**fields)
    assert group.status == group.topic_status == "ready"
    assert group.owner_open_id is None
    assert group.chat_id == "old-chat"
    assert group.topic_message_id == "old-topic"
    assert (await ledger.import_legacy_group_topic(**fields)).key == group.key
    with pytest.raises(ValueError, match="migrated queue"):
        await ledger.import_legacy_group_topic(**{**fields, "chat_id": "other"})
    with pytest.raises(ValueError, match="conflicts"):
        await ledger.import_legacy_group_topic(
            **{**fields, "topic_message_id": "other"}
        )
    await queue.upsert_entry(
        key="legacy-alias-for-r2",
        round="r2",
        nickname="nick",
        sec_user_id="sec-2",
        chat_id="old-chat",
        mode="post",
        status="completed",
    )
    with pytest.raises(ValueError, match="migrated queue"):
        await ledger.import_legacy_group_topic(**fields)
