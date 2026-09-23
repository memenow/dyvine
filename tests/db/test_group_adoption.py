"""Postgres contracts for adopting verified Feishu groups across rounds."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from dyvine.db import (
    DatabaseSessionFactory,
    PostgresDeliveryLedgerRepository,
    PostgresQueueRepository,
)
from dyvine.db.models import DownloadQueueRow


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


async def test_prior_verified_group_is_adopted_into_new_round_atomically(
    ledger: PostgresDeliveryLedgerRepository,
) -> None:
    queue = PostgresQueueRepository(ledger._sessions, owner_id="test")
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


async def test_prior_group_adoption_only_allows_new_accounts_to_create(
    ledger: PostgresDeliveryLedgerRepository,
) -> None:
    queue = PostgresQueueRepository(ledger._sessions, owner_id="test")
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
    with pytest.raises(ValueError, match="historical chat"):
        await ledger.adopt_prior_verified_group_for_round(**fields)
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
