"""Postgres checkpoints for Feishu effects that cannot be rolled back."""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import PurePosixPath

from sqlalchemy import Select, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from .group_adoption import adopt_prior_verified_group_for_round as adopt_group
from .models import (
    DeliveryFileRow,
    DeliveryGroupRow,
    DeliveryLegacyEvidenceRow,
    DeliveryRoundRow,
    DownloadQueueRow,
    LegacyExcludedNicknameRow,
)
from .protocols import (
    BatchOutcome,
    LegacyEvidenceBatchRow,
    LegacyFailureBatchRow,
    LegacySentBatchRow,
)
from .records import DeliveryGroupRecord, FileDeliveryRecord, LegacyEvidenceRecord
from .session import DatabaseSessionFactory


def _stamp() -> str:
    return datetime.now(UTC).isoformat()


async def _ensure_rounds(session: AsyncSession, rounds: set[str], stamp: str) -> None:
    """Insert missing round headers so child FKs never dangle.

    Every ledger child (group/file) names a round; the parent header
    carries no payload of its own, so ensuring it idempotently in the
    same transaction keeps the foreign key bulletproof for every
    caller (including one-shot scripts with legacy round names).
    """
    if not rounds:
        return
    await session.execute(
        insert(DeliveryRoundRow)
        .values(
            [
                {
                    "round": name,
                    "note": None,
                    "created_at": stamp,
                    "updated_at": stamp,
                }
                for name in sorted(rounds)
            ]
        )
        .on_conflict_do_nothing(index_elements=["round"])
    )


def _checked_legacy_path(relative_path: str) -> str:
    """Normalize an account-relative legacy path or raise ``ValueError``."""
    path = PurePosixPath(relative_path)
    if (
        path.is_absolute()
        or ".." in path.parts
        or not relative_path
        or path.as_posix() == "."
    ):
        raise ValueError("Legacy path must be account-relative")
    return path.as_posix()


# f2 saves post media as ``<create>_<desc>/<create>_<desc><slot>``, where the
# slot suffix names the media (``_video.mp4``, ``_image_3.webp``, ...).
_POST_CREATE_STAMP = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}-\d{2}-\d{2}")


# A Feishu history adoption that knows a post but not which of its media the
# chat holds records this slot; it stands for every media slot of the post.
POST_LEVEL_SLOT = "_post"


def post_media_slot(relative_path: str) -> tuple[str, str] | None:
    """Return the post creation stamp and media slot of an f2 media path.

    Authors can edit a caption after a legacy send, so a re-download of the
    same media gets a different folder and file name; the creation stamp and
    the slot suffix stay the same. Any other path shape returns ``None`` so
    callers match exactly instead of guessing.
    """
    folder, separator, name = relative_path.partition("/")
    if not separator or "/" in name or not name.startswith(folder):
        return None
    stamp, slot = folder[:19], name[len(folder) :]
    if not _POST_CREATE_STAMP.fullmatch(stamp) or not slot:
        return None
    return stamp, slot


def _group_record(row: DeliveryGroupRow) -> DeliveryGroupRecord:
    return DeliveryGroupRecord(
        key=row.key,
        round=row.round,
        sec_user_id=row.sec_user_id,
        nickname=row.nickname,
        create_name=row.create_name,
        owner_open_id=row.owner_open_id,
        status=row.status,
        create_uuid=row.create_uuid,
        create_started_at=row.create_started_at,
        chat_id=row.chat_id,
        topic_status=row.topic_status,
        topic_uuid=row.topic_uuid,
        topic_started_at=row.topic_started_at,
        topic_message_id=row.topic_message_id,
        avatar_url=row.avatar_url,
        avatar_key=row.avatar_key,
        legacy_source_file=row.legacy_source_file,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _file_record(row: DeliveryFileRow) -> FileDeliveryRecord:
    return FileDeliveryRecord(
        media_id=row.media_id,
        round=row.round,
        sec_user_id=row.sec_user_id,
        relative_path=row.relative_path,
        content_sha256=row.content_sha256,
        chat_id=row.chat_id,
        parent_id=row.parent_id,
        status=row.status,
        file_key=row.file_key,
        send_uuid=row.send_uuid,
        send_started_at=row.send_started_at,
        message_id=row.message_id,
        legacy_source_path=row.legacy_source_path,
        legacy_progress_file=row.legacy_progress_file,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _same_post_media(
    session: AsyncSession,
    candidates: Select[tuple[DeliveryFileRow]],
    relative_path: str,
) -> FileDeliveryRecord | None:
    """Return the first candidate holding the same post media as this path.

    A candidate recorded at ``POST_LEVEL_SLOT`` covers every slot of its post.
    """
    slot = post_media_slot(relative_path)
    if slot is None:
        return None
    covering = {slot, (slot[0], POST_LEVEL_SLOT)}
    same_post = candidates.where(
        DeliveryFileRow.relative_path.startswith(slot[0], autoescape=True)
    ).order_by(DeliveryFileRow.relative_path)
    for candidate in (await session.execute(same_post)).scalars():
        if post_media_slot(candidate.relative_path) in covering:
            return _file_record(candidate)
    return None


def _evidence_record(row: DeliveryLegacyEvidenceRow) -> LegacyEvidenceRecord:
    return LegacyEvidenceRecord(
        evidence_id=row.evidence_id,
        source_file=row.source_file,
        legacy_path=row.legacy_path,
        legacy_state=row.legacy_state,
        nickname=row.nickname,
        sec_user_id=row.sec_user_id,
        reason=row.reason,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class PostgresDeliveryLedgerRepository:
    """Keep one durable intent for each group, topic, and media identity."""

    def __init__(self, sessions: DatabaseSessionFactory) -> None:
        self._sessions = sessions

    async def upsert_excluded_nickname(self, *, nickname: str, source: str) -> None:
        """Keep a nickname-only exclusion without inventing an account ID."""
        cleaned = nickname.strip()
        if not cleaned:
            raise ValueError("Excluded nickname must not be blank")
        statement = (
            insert(LegacyExcludedNicknameRow)
            .values(nickname=cleaned, source=source, created_at=_stamp())
            .on_conflict_do_nothing(index_elements=["nickname"])
        )
        async with self._sessions.session() as session:
            async with session.begin():
                await session.execute(statement)

    async def list_excluded_nicknames(self) -> set[str]:
        """Return names to filter before creating new queue rows."""
        async with self._sessions.session() as session:
            rows = (
                (await session.execute(select(LegacyExcludedNicknameRow.nickname)))
                .scalars()
                .all()
            )
            return set(rows)

    async def reserve_group(
        self,
        *,
        round: str,
        sec_user_id: str,
        nickname: str,
        owner_open_id: str,
        avatar_url: str | None = None,
    ) -> DeliveryGroupRecord:
        key = f"{round}:{sec_user_id}"
        stamp = _stamp()
        statement = (
            insert(DeliveryGroupRow)
            .values(
                key=key,
                round=round,
                sec_user_id=sec_user_id,
                nickname=nickname,
                create_name=nickname,
                owner_open_id=owner_open_id,
                status="creating",
                create_uuid=uuid.uuid4().hex,
                create_started_at=stamp,
                chat_id=None,
                topic_status="unstarted",
                topic_uuid=None,
                topic_started_at=None,
                topic_message_id=None,
                avatar_url=avatar_url,
                avatar_key=None,
                legacy_source_file=None,
                created_at=stamp,
                updated_at=stamp,
            )
            .on_conflict_do_nothing(index_elements=["key"])
        )
        async with self._sessions.session() as session:
            async with session.begin():
                await _ensure_rounds(session, {round}, stamp)
                await session.execute(statement)
                row = await session.get(DeliveryGroupRow, key, with_for_update=True)
                if row is None:
                    raise ValueError("Group intent does not exist")
                if row.owner_open_id is not None and row.owner_open_id != owner_open_id:
                    raise ValueError("Group owner differs from persisted intent")
                if row.nickname != nickname:
                    raise ValueError("Group nickname differs from persisted intent")
                return _group_record(row)

    async def import_legacy_group_topic(
        self,
        *,
        round: str,
        sec_user_id: str,
        nickname: str,
        chat_id: str,
        topic_message_id: str,
        source_file: str,
    ) -> DeliveryGroupRecord:
        """Adopt only queue-verified legacy destinations; never create a chat."""
        if not chat_id or not topic_message_id or not source_file:
            raise ValueError("Legacy group evidence is incomplete")
        key = f"{round}:{sec_user_id}"
        stamp = _stamp()
        statement = (
            insert(DeliveryGroupRow)
            .values(
                key=key,
                round=round,
                sec_user_id=sec_user_id,
                nickname=nickname,
                create_name=nickname,
                owner_open_id=None,
                status="ready",
                create_uuid=None,
                create_started_at=None,
                chat_id=chat_id,
                topic_status="ready",
                topic_uuid=None,
                topic_started_at=None,
                topic_message_id=topic_message_id,
                avatar_url=None,
                avatar_key=None,
                legacy_source_file=source_file,
                created_at=stamp,
                updated_at=stamp,
            )
            .on_conflict_do_nothing(index_elements=["key"])
        )
        async with self._sessions.session() as session:
            async with session.begin():
                queue_rows = (
                    (
                        await session.execute(
                            select(DownloadQueueRow)
                            .where(DownloadQueueRow.round == round)
                            .where(DownloadQueueRow.sec_user_id == sec_user_id)
                        )
                    )
                    .scalars()
                    .all()
                )
                if (
                    len(queue_rows) != 1
                    or queue_rows[0].nickname != nickname
                    or queue_rows[0].chat_id != chat_id
                ):
                    raise ValueError("Legacy group does not match migrated queue")
                # No round ensure needed: the queue-verified check above
                # proves a queue row (hence its round parent) exists.
                await session.execute(statement)
                row = await session.get(DeliveryGroupRow, key, with_for_update=True)
                if row is None:
                    raise ValueError("Group intent does not exist")
                if (
                    row.status != "ready"
                    or row.chat_id != chat_id
                    or row.topic_status != "ready"
                    or row.topic_message_id != topic_message_id
                ):
                    raise ValueError("Legacy group conflicts with persisted intent")
                return _group_record(row)

    async def adopt_prior_verified_group_for_round(
        self,
        *,
        round: str,
        sec_user_id: str,
        nickname: str,
        owner_open_id: str,
    ) -> DeliveryGroupRecord | None:
        """Reuse one verified historical chat and topic without a Feishu write."""
        row = await adopt_group(
            self._sessions,
            round=round,
            sec_user_id=sec_user_id,
            nickname=nickname,
            owner_open_id=owner_open_id,
        )
        return _group_record(row) if row is not None else None

    async def get_group(
        self, *, round: str, sec_user_id: str
    ) -> DeliveryGroupRecord | None:
        async with self._sessions.session() as session:
            row = await session.get(DeliveryGroupRow, f"{round}:{sec_user_id}")
            return _group_record(row) if row else None

    async def _update_group(
        self, key: str, *, expected: str | None = None, **fields: str | None
    ) -> DeliveryGroupRecord:
        async with self._sessions.session() as session:
            async with session.begin():
                row = await session.get(DeliveryGroupRow, key, with_for_update=True)
                if row is None:
                    raise ValueError("Group intent does not exist")
                if expected is not None and row.status != expected:
                    return _group_record(row)
                for name, value in fields.items():
                    setattr(row, name, value)
                row.updated_at = _stamp()
                return _group_record(row)

    async def mark_group_ready(self, key: str, chat_id: str) -> DeliveryGroupRecord:
        async with self._sessions.session() as session:
            async with session.begin():
                row = await session.get(DeliveryGroupRow, key, with_for_update=True)
                if row is None:
                    raise ValueError("Group intent does not exist")
                if row.chat_id and row.chat_id != chat_id:
                    raise ValueError("Group has a different persisted chat_id")
                row.status = "ready"
                row.chat_id = chat_id
                row.updated_at = _stamp()
                return _group_record(row)

    async def mark_group_review(self, key: str) -> DeliveryGroupRecord:
        return await self._update_group(key, expected="creating", status="needs_review")

    async def rotate_group_uuid(
        self, key: str, create_name: str
    ) -> DeliveryGroupRecord:
        """Retry a definitively rejected name with a fresh request identity."""
        return await self._update_group(
            key,
            expected="creating",
            create_uuid=uuid.uuid4().hex,
            create_started_at=_stamp(),
            create_name=create_name,
        )

    async def begin_topic(self, key: str) -> DeliveryGroupRecord:
        async with self._sessions.session() as session:
            async with session.begin():
                row = await session.get(DeliveryGroupRow, key, with_for_update=True)
                if row is None or row.status != "ready" or not row.chat_id:
                    raise ValueError("Group must be ready before topic creation")
                if row.topic_status == "unstarted":
                    row.topic_status = "creating"
                    row.topic_uuid = uuid.uuid4().hex
                    row.topic_started_at = _stamp()
                    row.updated_at = row.topic_started_at
                return _group_record(row)

    async def mark_topic_ready(self, key: str, message_id: str) -> DeliveryGroupRecord:
        async with self._sessions.session() as session:
            async with session.begin():
                row = await session.get(DeliveryGroupRow, key, with_for_update=True)
                if row is None:
                    raise ValueError("Group intent does not exist")
                if row.topic_message_id and row.topic_message_id != message_id:
                    raise ValueError("Group has a different persisted topic")
                row.topic_status = "ready"
                row.topic_message_id = message_id
                row.updated_at = _stamp()
                return _group_record(row)

    async def mark_topic_review(self, key: str) -> DeliveryGroupRecord:
        async with self._sessions.session() as session:
            async with session.begin():
                row = await session.get(DeliveryGroupRow, key, with_for_update=True)
                if row is None:
                    raise ValueError("Group intent does not exist")
                if row.topic_status == "creating":
                    row.topic_status = "needs_review"
                    row.updated_at = _stamp()
                return _group_record(row)

    async def set_avatar_key(self, key: str, image_key: str) -> DeliveryGroupRecord:
        async with self._sessions.session() as session:
            async with session.begin():
                row = await session.get(DeliveryGroupRow, key, with_for_update=True)
                if row is None:
                    raise ValueError("Group intent does not exist")
                if not row.avatar_key:
                    row.avatar_key = image_key
                    row.updated_at = _stamp()
                return _group_record(row)

    async def reserve_file(
        self,
        *,
        media_id: str,
        round: str,
        sec_user_id: str,
        relative_path: str,
        content_sha256: str,
        chat_id: str,
        parent_id: str | None,
    ) -> FileDeliveryRecord:
        stamp = _stamp()
        statement = (
            insert(DeliveryFileRow)
            .values(
                media_id=media_id,
                round=round,
                sec_user_id=sec_user_id,
                relative_path=relative_path,
                content_sha256=content_sha256,
                chat_id=chat_id,
                parent_id=parent_id,
                status="planned",
                file_key=None,
                send_uuid=None,
                send_started_at=None,
                message_id=None,
                legacy_source_path=None,
                legacy_progress_file=None,
                created_at=stamp,
                updated_at=stamp,
            )
            .on_conflict_do_nothing(index_elements=["media_id"])
        )
        async with self._sessions.session() as session:
            async with session.begin():
                await _ensure_rounds(session, {round}, stamp)
                await session.execute(statement)
                row = await session.get(DeliveryFileRow, media_id, with_for_update=True)
                if row is None:
                    raise ValueError("Media intent does not exist")
                if (
                    row.sec_user_id != sec_user_id
                    or row.content_sha256 != content_sha256
                ):
                    raise ValueError("Media identity collision")
                return _file_record(row)

    async def get_file(self, media_id: str) -> FileDeliveryRecord | None:
        async with self._sessions.session() as session:
            row = await session.get(DeliveryFileRow, media_id)
            return _file_record(row) if row else None

    async def reserve_legacy_sent(
        self,
        *,
        round: str,
        sec_user_id: str,
        relative_path: str,
        chat_id: str | None = None,
        parent_id: str | None = None,
        legacy_source_path: str | None = None,
        legacy_progress_file: str | None = None,
    ) -> FileDeliveryRecord:
        """Record path-only confirmed history without inventing a file hash."""
        normalized = _checked_legacy_path(relative_path)
        media_id = sha256(f"legacy\0{sec_user_id}\0{normalized}".encode()).hexdigest()
        stamp = _stamp()
        statement = (
            insert(DeliveryFileRow)
            .values(
                media_id=media_id,
                round=round,
                sec_user_id=sec_user_id,
                relative_path=normalized,
                content_sha256=None,
                chat_id=chat_id,
                parent_id=parent_id,
                status="legacy_confirmed_sent",
                file_key=None,
                send_uuid=None,
                send_started_at=None,
                message_id=None,
                legacy_source_path=legacy_source_path,
                legacy_progress_file=legacy_progress_file,
                created_at=stamp,
                updated_at=stamp,
            )
            .on_conflict_do_nothing(index_elements=["media_id"])
        )
        async with self._sessions.session() as session:
            async with session.begin():
                await _ensure_rounds(session, {round}, stamp)
                await session.execute(statement)
                row = await session.get(DeliveryFileRow, media_id)
                if row is None:
                    raise ValueError("Media intent does not exist")
                return _file_record(row)

    async def reserve_legacy_sent_batch(
        self, rows: Sequence[LegacySentBatchRow]
    ) -> BatchOutcome:
        """Insert a bounded batch of confirmed legacy paths in one transaction."""
        if not rows:
            return BatchOutcome(0, 0)
        if len(rows) > 500:
            raise ValueError("Legacy batches must contain at most 500 rows")
        stamp = _stamp()
        values = []
        rounds: set[str] = set()
        for row in rows:
            sec_user_id = row.get("sec_user_id")
            round_name = row.get("round")
            relative_path = row.get("relative_path")
            if not sec_user_id or not round_name or not relative_path:
                raise ValueError("Legacy batch row is missing its identity")
            rounds.add(round_name)
            normalized = _checked_legacy_path(relative_path)
            values.append(
                {
                    "media_id": sha256(
                        f"legacy\0{sec_user_id}\0{normalized}".encode()
                    ).hexdigest(),
                    "round": round_name,
                    "sec_user_id": sec_user_id,
                    "relative_path": normalized,
                    "content_sha256": None,
                    "chat_id": row.get("chat_id"),
                    "parent_id": row.get("parent_id"),
                    "status": "legacy_confirmed_sent",
                    "file_key": None,
                    "send_uuid": None,
                    "send_started_at": None,
                    "message_id": None,
                    "legacy_source_path": row.get("legacy_source_path"),
                    "legacy_progress_file": row.get("legacy_progress_file"),
                    "created_at": stamp,
                    "updated_at": stamp,
                }
            )
        statement = (
            insert(DeliveryFileRow)
            .values(values)
            .on_conflict_do_nothing(index_elements=["media_id"])
            .returning(DeliveryFileRow.media_id)
        )
        async with self._sessions.session() as session:
            async with session.begin():
                await _ensure_rounds(session, rounds, stamp)
                result = await session.execute(statement)
                inserted = len(result.scalars().all())
        return BatchOutcome(inserted, len(values) - inserted)

    async def reserve_legacy_permanent_failure_batch(
        self, rows: Sequence[LegacyFailureBatchRow]
    ) -> BatchOutcome:
        """Hold verified legacy failures by the same identity as sent history."""
        if not rows:
            return BatchOutcome(0, 0)
        if len(rows) > 500:
            raise ValueError("Legacy batches must contain at most 500 rows")
        stamp = _stamp()
        values = []
        for row in rows:
            sec_user_id = row.get("sec_user_id")
            relative_path = row.get("relative_path")
            if not sec_user_id or not relative_path:
                raise ValueError("Legacy batch row is missing its identity")
            normalized = _checked_legacy_path(relative_path)
            values.append(
                {
                    "media_id": sha256(
                        f"legacy\0{sec_user_id}\0{normalized}".encode()
                    ).hexdigest(),
                    "round": "legacy",
                    "sec_user_id": sec_user_id,
                    "relative_path": normalized,
                    "content_sha256": None,
                    "chat_id": None,
                    "parent_id": None,
                    "status": "permanent_failure",
                    "file_key": None,
                    "send_uuid": None,
                    "send_started_at": None,
                    "message_id": None,
                    "legacy_source_path": row.get("legacy_source_path"),
                    "legacy_progress_file": row.get("legacy_progress_file"),
                    "created_at": stamp,
                    "updated_at": stamp,
                }
            )
        statement = (
            insert(DeliveryFileRow)
            .values(values)
            .on_conflict_do_nothing(index_elements=["media_id"])
            .returning(DeliveryFileRow.media_id)
        )
        async with self._sessions.session() as session:
            async with session.begin():
                await _ensure_rounds(session, {"legacy"}, stamp)
                result = await session.execute(statement)
                inserted = len(result.scalars().all())
        return BatchOutcome(inserted, len(values) - inserted)

    async def find_legacy_sent(
        self, *, sec_user_id: str, relative_path: str
    ) -> FileDeliveryRecord | None:
        """Find the legacy send of this media, tolerating caption renames.

        Writers store the normalized path, so reads normalize first:
        ``a//b`` and ``a/./b`` resolve to the same identity as ``a/b``.
        An exact normalized-path match wins. Otherwise a legacy send of
        the same post creation stamp and media slot (see
        ``post_media_slot``) is the same media under an edited caption,
        so sending it again would duplicate it in the group.
        """
        normalized = _checked_legacy_path(relative_path)
        legacy_sends = (
            select(DeliveryFileRow)
            .where(DeliveryFileRow.sec_user_id == sec_user_id)
            .where(DeliveryFileRow.status == "legacy_confirmed_sent")
        )
        async with self._sessions.session() as session:
            exact = legacy_sends.where(
                DeliveryFileRow.relative_path == normalized
            ).limit(1)
            row = (await session.execute(exact)).scalars().first()
            if row is not None:
                return _file_record(row)
            return await _same_post_media(session, legacy_sends, normalized)

    async def find_prior_sent(
        self, *, sec_user_id: str, relative_path: str
    ) -> FileDeliveryRecord | None:
        """Find this runner's confirmed send of the same post media.

        ``reserve_file`` dedupes only the exact path and content. Weekly
        windows overlap, so a caption edited between two downloads renames
        the file; a ``sent`` record with the same post creation stamp and
        media slot (see ``post_media_slot``) is the same media.
        """
        sends = (
            select(DeliveryFileRow)
            .where(DeliveryFileRow.sec_user_id == sec_user_id)
            .where(DeliveryFileRow.status == "sent")
        )
        async with self._sessions.session() as session:
            return await _same_post_media(session, sends, relative_path)

    async def find_legacy_permanent_failure(
        self, *, sec_user_id: str, relative_path: str
    ) -> FileDeliveryRecord | None:
        """Find a historical permanent failure before a new send is reserved."""
        normalized = _checked_legacy_path(relative_path)
        media_id = sha256(f"legacy\0{sec_user_id}\0{normalized}".encode()).hexdigest()
        async with self._sessions.session() as session:
            row = await session.get(DeliveryFileRow, media_id)
            if row is None or row.status != "permanent_failure":
                return None
            return _file_record(row)

    async def find_legacy_unverified_hold(
        self, *, legacy_path: str
    ) -> LegacyEvidenceRecord | None:
        """Find cache-only evidence by its original absolute media path."""
        query = (
            select(DeliveryLegacyEvidenceRow)
            .where(DeliveryLegacyEvidenceRow.legacy_path == legacy_path)
            .where(DeliveryLegacyEvidenceRow.legacy_state == "sent_unverified")
            .limit(1)
        )
        async with self._sessions.session() as session:
            row = (await session.execute(query)).scalars().first()
            return _evidence_record(row) if row else None

    async def upsert_legacy_evidence(
        self,
        *,
        source_file: str,
        legacy_path: str,
        legacy_state: str,
        nickname: str | None,
        sec_user_id: str | None,
        reason: str,
    ) -> LegacyEvidenceRecord:
        """Keep unresolved history queryable without claiming it is delivered."""
        evidence_id = sha256(
            f"{source_file}\0{legacy_state}\0{legacy_path}".encode()
        ).hexdigest()
        stamp = _stamp()
        statement = (
            insert(DeliveryLegacyEvidenceRow)
            .values(
                evidence_id=evidence_id,
                source_file=source_file,
                legacy_path=legacy_path,
                legacy_state=legacy_state,
                nickname=nickname,
                sec_user_id=sec_user_id,
                reason=reason,
                created_at=stamp,
                updated_at=stamp,
            )
            .on_conflict_do_update(
                index_elements=["evidence_id"],
                set_={
                    "nickname": nickname,
                    "sec_user_id": sec_user_id,
                    "reason": reason,
                    "updated_at": stamp,
                },
            )
        )
        async with self._sessions.session() as session:
            async with session.begin():
                await session.execute(statement)
                row = await session.get(DeliveryLegacyEvidenceRow, evidence_id)
                if row is None:
                    raise ValueError("Legacy evidence does not exist")
                return _evidence_record(row)

    async def upsert_legacy_evidence_batch(
        self, rows: Sequence[LegacyEvidenceBatchRow]
    ) -> BatchOutcome:
        """Insert bounded audit evidence without changing send eligibility."""
        if not rows:
            return BatchOutcome(0, 0)
        if len(rows) > 500:
            raise ValueError("Evidence batches must contain at most 500 rows")
        stamp = _stamp()
        values = []
        for row in rows:
            source_file = row.get("source_file")
            legacy_path = row.get("legacy_path")
            legacy_state = row.get("legacy_state")
            reason = row.get("reason")
            if not source_file or not legacy_path or not legacy_state or not reason:
                raise ValueError("Evidence row is missing required fields")
            values.append(
                {
                    "evidence_id": sha256(
                        f"{source_file}\0{legacy_state}\0{legacy_path}".encode()
                    ).hexdigest(),
                    "source_file": source_file,
                    "legacy_path": legacy_path,
                    "legacy_state": legacy_state,
                    "nickname": row.get("nickname"),
                    "sec_user_id": row.get("sec_user_id"),
                    "reason": reason,
                    "created_at": stamp,
                    "updated_at": stamp,
                }
            )
        statement = (
            insert(DeliveryLegacyEvidenceRow)
            .values(values)
            .on_conflict_do_nothing(index_elements=["evidence_id"])
            .returning(DeliveryLegacyEvidenceRow.evidence_id)
        )
        async with self._sessions.session() as session:
            async with session.begin():
                result = await session.execute(statement)
                inserted = len(result.scalars().all())
        return BatchOutcome(inserted, len(values) - inserted)

    async def list_files(
        self,
        *,
        sec_user_id: str | None = None,
        round: str | None = None,
        status: str | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> list[FileDeliveryRecord]:
        query = select(DeliveryFileRow)
        if sec_user_id is not None:
            query = query.where(DeliveryFileRow.sec_user_id == sec_user_id)
        if round is not None:
            query = query.where(DeliveryFileRow.round == round)
        if status is not None:
            query = query.where(DeliveryFileRow.status == status)
        query = query.order_by(DeliveryFileRow.created_at, DeliveryFileRow.media_id)
        query = query.offset(offset)
        if limit >= 0:
            query = query.limit(limit)
        async with self._sessions.session() as session:
            rows = (await session.execute(query)).scalars().all()
            return [_file_record(row) for row in rows]

    async def _update_file(
        self, media_id: str, *, expected: str | None = None, **fields: str | None
    ) -> FileDeliveryRecord:
        async with self._sessions.session() as session:
            async with session.begin():
                row = await session.get(DeliveryFileRow, media_id, with_for_update=True)
                if row is None:
                    raise ValueError("Media intent does not exist")
                if expected is not None and row.status != expected:
                    return _file_record(row)
                for name, value in fields.items():
                    setattr(row, name, value)
                row.updated_at = _stamp()
                return _file_record(row)

    async def set_file_key(self, media_id: str, file_key: str) -> FileDeliveryRecord:
        return await self._update_file(
            media_id, expected="planned", status="uploaded", file_key=file_key
        )

    async def begin_send(self, media_id: str) -> FileDeliveryRecord:
        async with self._sessions.session() as session:
            async with session.begin():
                row = await session.get(DeliveryFileRow, media_id, with_for_update=True)
                if row is None:
                    raise ValueError("Media intent does not exist")
                if row.status == "uploaded" and row.file_key:
                    row.status = "sending"
                    row.send_uuid = uuid.uuid4().hex
                    row.send_started_at = _stamp()
                    row.updated_at = row.send_started_at
                return _file_record(row)

    async def mark_sent(self, media_id: str, message_id: str) -> FileDeliveryRecord:
        async with self._sessions.session() as session:
            async with session.begin():
                row = await session.get(DeliveryFileRow, media_id, with_for_update=True)
                if row is None or row.status not in {"sending", "sent"}:
                    raise ValueError("Media has no sending intent")
                if row.message_id and row.message_id != message_id:
                    raise ValueError("Media has a different persisted message")
                row.status = "sent"
                row.message_id = message_id
                row.updated_at = _stamp()
                return _file_record(row)

    async def mark_file_review(self, media_id: str) -> FileDeliveryRecord:
        async with self._sessions.session() as session:
            async with session.begin():
                row = await session.get(DeliveryFileRow, media_id, with_for_update=True)
                if row is None:
                    raise ValueError("Media intent does not exist")
                if row.status in {"planned", "uploaded", "sending"}:
                    row.status = "needs_review"
                    row.updated_at = _stamp()
                return _file_record(row)

    async def mark_permanent_failure(self, media_id: str) -> FileDeliveryRecord:
        return await self._update_file(
            media_id, expected="planned", status="permanent_failure"
        )
