"""Atomic reuse of a verified historical Feishu group in a new round."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import DeliveryGroupRow, DownloadQueueRow
from .session import DatabaseSessionFactory


def _historical_chat(history: Sequence[DownloadQueueRow]) -> str | None:
    if not history:
        return None
    chats = {row.chat_id for row in history if row.chat_id}
    if not chats:
        raise ValueError("Historical account has no verified historical chat")
    if len(chats) != 1:
        raise ValueError("Account has multiple historical chats")
    return chats.pop()


async def _verified_source(
    session: AsyncSession,
    history: Sequence[DownloadQueueRow],
    sec_user_id: str,
    chat_id: str,
) -> DeliveryGroupRow:
    rounds = {row.round for row in history if row.chat_id == chat_id}
    sources = (
        (
            await session.execute(
                select(DeliveryGroupRow)
                .where(DeliveryGroupRow.round.in_(rounds))
                .where(DeliveryGroupRow.sec_user_id == sec_user_id)
                .where(DeliveryGroupRow.chat_id == chat_id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    ready = [
        row
        for row in sources
        if row.status == "ready"
        and row.topic_status == "ready"
        and row.topic_message_id
    ]
    if not ready:
        raise ValueError("Historical chat has no verified group and topic")
    if len({row.topic_message_id for row in ready}) != 1:
        raise ValueError("Historical chat has multiple verified topics")
    return max(ready, key=lambda row: (row.round, row.key))


def _adopted_row(
    *,
    key: str,
    round: str,
    sec_user_id: str,
    nickname: str,
    owner_open_id: str,
    source: DeliveryGroupRow,
) -> DeliveryGroupRow:
    stamp = datetime.now(UTC).isoformat()
    return DeliveryGroupRow(
        key=key,
        round=round,
        sec_user_id=sec_user_id,
        nickname=nickname,
        create_name=source.create_name,
        owner_open_id=owner_open_id,
        status="ready",
        create_uuid=None,
        create_started_at=None,
        chat_id=source.chat_id,
        topic_status="ready",
        topic_uuid=None,
        topic_started_at=None,
        topic_message_id=source.topic_message_id,
        avatar_url=source.avatar_url,
        avatar_key=source.avatar_key,
        legacy_source_file=source.legacy_source_file,
        created_at=stamp,
        updated_at=stamp,
    )


def _check_existing(
    row: DeliveryGroupRow,
    *,
    round: str,
    sec_user_id: str,
    nickname: str,
    owner_open_id: str,
    source: DeliveryGroupRow,
) -> None:
    if (
        row.round != round
        or row.sec_user_id != sec_user_id
        or row.nickname != nickname
        or row.owner_open_id != owner_open_id
        or row.status != "ready"
        or row.chat_id != source.chat_id
        or row.topic_status != "ready"
        or row.topic_message_id != source.topic_message_id
    ):
        raise ValueError("Current group conflicts with historical evidence")


async def adopt_prior_verified_group_for_round(
    sessions: DatabaseSessionFactory,
    *,
    round: str,
    sec_user_id: str,
    nickname: str,
    owner_open_id: str,
) -> DeliveryGroupRow | None:
    """Reuse one verified historical chat and topic in the queue's transaction."""
    if not round or not sec_user_id or not nickname or not owner_open_id:
        raise ValueError("Group adoption requires round, account, name, and owner")
    key = f"{round}:{sec_user_id}"
    async with sessions.session() as session:
        async with session.begin():
            rows = (
                (
                    await session.execute(
                        select(DownloadQueueRow)
                        .where(DownloadQueueRow.sec_user_id == sec_user_id)
                        .order_by(DownloadQueueRow.key)
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            current_queue = next((row for row in rows if row.key == key), None)
            if (
                current_queue is None
                or current_queue.round != round
                or current_queue.nickname != nickname
            ):
                raise ValueError("Current round queue identity is unavailable")
            history = [row for row in rows if row.key != key]
            chat_id = _historical_chat(history)
            if chat_id is None:
                return None
            if current_queue.chat_id not in (None, chat_id):
                raise ValueError("Current queue conflicts with historical chat")
            source = await _verified_source(session, history, sec_user_id, chat_id)
            current_group = await session.get(
                DeliveryGroupRow, key, with_for_update=True
            )
            if current_group is None:
                current_group = _adopted_row(
                    key=key,
                    round=round,
                    sec_user_id=sec_user_id,
                    nickname=nickname,
                    owner_open_id=owner_open_id,
                    source=source,
                )
                session.add(current_group)
            else:
                _check_existing(
                    current_group,
                    round=round,
                    sec_user_id=sec_user_id,
                    nickname=nickname,
                    owner_open_id=owner_open_id,
                    source=source,
                )
            current_queue.chat_id = chat_id
            current_queue.updated_at = datetime.now(UTC).isoformat()
            await session.flush()
            return current_group
