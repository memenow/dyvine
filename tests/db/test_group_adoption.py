"""Postgres contracts for adopting verified Feishu groups across rounds."""

from __future__ import annotations

import asyncio
import itertools

import pytest
from sqlalchemy import text

import dyvine.db.delivery_ledger as ledger_module
from dyvine.db import (
    DatabaseSessionFactory,
    PostgresDeliveryLedgerRepository,
    PostgresQueueRepository,
    PostgresRoundRepository,
)
from dyvine.db.models import DownloadQueueRow


async def _seed_rounds(ledger: PostgresDeliveryLedgerRepository, *rounds: str) -> None:
    """Seed round headers (FK parents of the queue rows below)."""
    repo = PostgresRoundRepository(ledger._sessions)
    for name in rounds:
        await repo.upsert_round(round=name)


@pytest.fixture
async def ledger(postgres_url: str):  # type: ignore[no-untyped-def]
    factory = DatabaseSessionFactory(postgres_url)
    async with factory.session() as session:
        async with session.begin():
            await session.execute(
                text(
                    # Children and parents in one statement (see the
                    # contract suite's fixture note on shared-database
                    # truncates under the round foreign keys).
                    "TRUNCATE TABLE delivery_files, delivery_groups, "
                    "download_queue, delivery_legacy_evidence, "
                    "legacy_excluded_nicknames, delivery_rounds"
                )
            )
    try:
        yield PostgresDeliveryLedgerRepository(factory)
    finally:
        await factory.aclose()


async def test_prior_verified_group_is_adopted_into_new_round_atomically(
    ledger: PostgresDeliveryLedgerRepository,
) -> None:
    queue = PostgresQueueRepository(ledger._sessions, owner_id="test")
    await _seed_rounds(ledger, "old-round", "new-round")
    await queue.upsert_entry(
        key="legacy-raw-key",
        round="old-round",
        nickname="old nick",
        sec_user_id="sec-1",
        chat_id="chat-1",
        mode="post",
        status="completed",
    )
    await ledger.import_legacy_group_topic(
        round="old-round",
        sec_user_id="sec-1",
        nickname="old nick",
        chat_id="chat-1",
        topic_message_id="topic-1",
        source_file="/old/send_progress.json",
    )
    await queue.upsert_entry(
        key="new-round:sec-1",
        round="new-round",
        nickname="new nick",
        sec_user_id="sec-1",
        mode="post",
        status="pending",
    )
    adopted, concurrent = await asyncio.gather(
        ledger.adopt_prior_verified_group_for_round(
            round="new-round",
            sec_user_id="sec-1",
            nickname="new nick",
            owner_open_id="ou-owner",
        ),
        ledger.adopt_prior_verified_group_for_round(
            round="new-round",
            sec_user_id="sec-1",
            nickname="new nick",
            owner_open_id="ou-owner",
        ),
    )
    assert adopted is not None
    assert concurrent == adopted
    assert adopted.status == adopted.topic_status == "ready"
    assert adopted.nickname == "new nick"
    assert adopted.create_name == "old nick"
    assert adopted.owner_open_id == "ou-owner"
    assert adopted.chat_id == "chat-1"
    assert adopted.topic_message_id == "topic-1"
    assert adopted.legacy_source_file == "/old/send_progress.json"
    assert (await queue.get_entry("new-round:sec-1")).chat_id == "chat-1"
    again = await ledger.adopt_prior_verified_group_for_round(
        round="new-round",
        sec_user_id="sec-1",
        nickname="new nick",
        owner_open_id="ou-owner",
    )
    assert again == adopted
    # Renames and owner rotations converge forward instead of failing
    # the idempotent retry; the delivery identity is what is pinned.
    # (The queue row carries the fresh profile name first, as the
    # queue identity check requires; the group row then converges.)
    await queue.update_entry("new-round:sec-1", nickname="new nick v2")
    renamed = await ledger.adopt_prior_verified_group_for_round(
        round="new-round",
        sec_user_id="sec-1",
        nickname="new nick v2",
        owner_open_id="ou-owner-v2",
    )
    assert renamed is not None
    assert renamed.nickname == "new nick v2"
    assert renamed.owner_open_id == "ou-owner-v2"
    assert renamed.chat_id == "chat-1"
    assert renamed.topic_message_id == "topic-1"
    assert renamed.key == adopted.key


async def test_prior_group_adoption_only_allows_new_accounts_to_create(
    ledger: PostgresDeliveryLedgerRepository,
) -> None:
    queue = PostgresQueueRepository(ledger._sessions, owner_id="test")
    await _seed_rounds(ledger, "old-round", "new-round")
    await queue.upsert_entry(
        key="new-round:sec-1",
        round="new-round",
        nickname="nick",
        sec_user_id="sec-1",
        mode="post",
        status="pending",
    )
    fields = {
        "round": "new-round",
        "sec_user_id": "sec-1",
        "nickname": "nick",
        "owner_open_id": "ou-owner",
    }
    assert await ledger.adopt_prior_verified_group_for_round(**fields) is None
    await queue.upsert_entry(
        key="old-round:sec-1",
        round="old-round",
        nickname="nick",
        sec_user_id="sec-1",
        mode="post",
        status="completed",
    )
    # Chat-less history (prior rounds still pending/failed) is the
    # normal "nothing to reuse" case: fall back to creation, not an
    # error that aborts the whole account.
    assert await ledger.adopt_prior_verified_group_for_round(**fields) is None
    async with ledger._sessions.session() as session:
        async with session.begin():
            old = await session.get(DownloadQueueRow, "old-round:sec-1")
            assert old is not None
            old.chat_id = "chat-1"
    with pytest.raises(ValueError, match="verified group"):
        await ledger.adopt_prior_verified_group_for_round(**fields)
    assert (await queue.get_entry("new-round:sec-1")).chat_id is None
    assert await ledger.get_group(round="new-round", sec_user_id="sec-1") is None


async def test_prior_group_adoption_rejects_conflicting_chats(
    ledger: PostgresDeliveryLedgerRepository,
) -> None:
    queue = PostgresQueueRepository(ledger._sessions, owner_id="test")
    await _seed_rounds(ledger, "old-1", "old-2", "new-round")
    for round_name, chat in (("old-1", "chat-1"), ("old-2", "chat-2")):
        await queue.upsert_entry(
            key=f"{round_name}:sec-1",
            round=round_name,
            nickname="nick",
            sec_user_id="sec-1",
            chat_id=chat,
            mode="post",
            status="completed",
        )
    await queue.upsert_entry(
        key="new-round:sec-1",
        round="new-round",
        nickname="nick",
        sec_user_id="sec-1",
        mode="post",
        status="pending",
    )
    with pytest.raises(ValueError, match="multiple historical chats"):
        await ledger.adopt_prior_verified_group_for_round(
            round="new-round",
            sec_user_id="sec-1",
            nickname="nick",
            owner_open_id="ou-owner",
        )
    assert (await queue.get_entry("new-round:sec-1")).chat_id is None


async def test_prior_group_adoption_rejects_conflicting_topics(
    ledger: PostgresDeliveryLedgerRepository,
) -> None:
    queue = PostgresQueueRepository(ledger._sessions, owner_id="test")
    await _seed_rounds(ledger, "old-1", "old-2", "new-round")
    for round_name, topic in (("old-1", "topic-1"), ("old-2", "topic-2")):
        await queue.upsert_entry(
            key=f"{round_name}:sec-1",
            round=round_name,
            nickname="nick",
            sec_user_id="sec-1",
            chat_id="chat-1",
            mode="post",
            status="completed",
        )
        await ledger.import_legacy_group_topic(
            round=round_name,
            sec_user_id="sec-1",
            nickname="nick",
            chat_id="chat-1",
            topic_message_id=topic,
            source_file=f"/old/{round_name}.json",
        )
    await queue.upsert_entry(
        key="new-round:sec-1",
        round="new-round",
        nickname="nick",
        sec_user_id="sec-1",
        mode="post",
        status="pending",
    )
    fields = {
        "round": "new-round",
        "sec_user_id": "sec-1",
        "nickname": "nick",
        "owner_open_id": "ou-owner",
    }
    with pytest.raises(ValueError, match="multiple verified topics"):
        await ledger.adopt_prior_verified_group_for_round(**fields)
    assert (await queue.get_entry("new-round:sec-1")).chat_id is None
    assert await ledger.get_group(round="new-round", sec_user_id="sec-1") is None


async def test_prior_group_adoption_keeps_existing_creation_intent(
    ledger: PostgresDeliveryLedgerRepository,
) -> None:
    queue = PostgresQueueRepository(ledger._sessions, owner_id="test")
    await _seed_rounds(ledger, "old-round", "new-round")
    await queue.upsert_entry(
        key="old-round:sec-1",
        round="old-round",
        nickname="nick",
        sec_user_id="sec-1",
        chat_id="chat-1",
        mode="post",
        status="completed",
    )
    await ledger.import_legacy_group_topic(
        round="old-round",
        sec_user_id="sec-1",
        nickname="nick",
        chat_id="chat-1",
        topic_message_id="topic-1",
        source_file="/old/progress.json",
    )
    await queue.upsert_entry(
        key="new-round:sec-1",
        round="new-round",
        nickname="nick",
        sec_user_id="sec-1",
        mode="post",
        status="pending",
    )
    current = await ledger.reserve_group(
        round="new-round",
        sec_user_id="sec-1",
        nickname="nick",
        owner_open_id="ou-owner",
    )
    with pytest.raises(ValueError, match="Current group conflicts"):
        await ledger.adopt_prior_verified_group_for_round(
            round="new-round",
            sec_user_id="sec-1",
            nickname="nick",
            owner_open_id="ou-owner",
        )
    assert (await ledger.get_group(round="new-round", sec_user_id="sec-1")) == current
    assert (await queue.get_entry("new-round:sec-1")).chat_id is None


async def test_prior_group_adoption_treats_blank_chat_as_unset(
    ledger: PostgresDeliveryLedgerRepository,
) -> None:
    """An empty-string current chat reuses history; only a real one conflicts."""
    queue = PostgresQueueRepository(ledger._sessions, owner_id="test")
    await _seed_rounds(ledger, "old-round", "new-round")
    await queue.upsert_entry(
        key="old-round:sec-1",
        round="old-round",
        nickname="nick",
        sec_user_id="sec-1",
        chat_id="chat-1",
        mode="post",
        status="completed",
    )
    await ledger.import_legacy_group_topic(
        round="old-round",
        sec_user_id="sec-1",
        nickname="nick",
        chat_id="chat-1",
        topic_message_id="topic-1",
        source_file="/old/progress.json",
    )
    await queue.upsert_entry(
        key="new-round:sec-1",
        round="new-round",
        nickname="nick",
        sec_user_id="sec-1",
        chat_id="",
        mode="post",
        status="pending",
    )
    adopted = await ledger.adopt_prior_verified_group_for_round(
        round="new-round",
        sec_user_id="sec-1",
        nickname="nick",
        owner_open_id="ou-owner",
    )
    assert adopted is not None
    assert adopted.chat_id == "chat-1"
    assert (await queue.get_entry("new-round:sec-1")).chat_id == "chat-1"


async def test_prior_group_adoption_prefers_newest_verified_source(
    ledger: PostgresDeliveryLedgerRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The source is the newest-created group, not the max round name."""
    queue = PostgresQueueRepository(ledger._sessions, owner_id="test")
    await _seed_rounds(ledger, "round-b", "round-a", "new-round")
    # Monotonic ticks instead of wall time: creation order -- not
    # round-name spelling -- must decide, deterministically.
    tick = itertools.count()
    monkeypatch.setattr(
        ledger_module,
        "_stamp",
        lambda: f"2020-01-01T00:00:{next(tick):02d}+00:00",
    )
    # Created first, but lexicographically *larger*: a (round, key)
    # ordering would wrongly prefer this stale source.
    await queue.upsert_entry(
        key="round-b:sec-1",
        round="round-b",
        nickname="nick",
        sec_user_id="sec-1",
        chat_id="chat-1",
        mode="post",
        status="completed",
    )
    await ledger.import_legacy_group_topic(
        round="round-b",
        sec_user_id="sec-1",
        nickname="nick",
        chat_id="chat-1",
        topic_message_id="topic-1",
        source_file="/old/stale.json",
    )
    await queue.upsert_entry(
        key="round-a:sec-1",
        round="round-a",
        nickname="nick",
        sec_user_id="sec-1",
        chat_id="chat-1",
        mode="post",
        status="completed",
    )
    await ledger.import_legacy_group_topic(
        round="round-a",
        sec_user_id="sec-1",
        nickname="nick",
        chat_id="chat-1",
        topic_message_id="topic-1",
        source_file="/old/fresh.json",
    )
    await queue.upsert_entry(
        key="new-round:sec-1",
        round="new-round",
        nickname="nick",
        sec_user_id="sec-1",
        mode="post",
        status="pending",
    )
    adopted = await ledger.adopt_prior_verified_group_for_round(
        round="new-round",
        sec_user_id="sec-1",
        nickname="nick",
        owner_open_id="ou-owner",
    )
    assert adopted is not None
    assert adopted.legacy_source_file == "/old/fresh.json"
