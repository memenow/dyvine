"""Plain copies of thread sends are posted once, in order, with durable intent."""

from __future__ import annotations

import sys
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from dyvine.db.records import FileDeliveryRecord

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts import resend_thread_files_flat as script  # noqa: E402


def _record(media_id: str, **fields: Any) -> FileDeliveryRecord:
    values: dict[str, Any] = {
        "media_id": media_id,
        "round": "weekly0913",
        "sec_user_id": "sec-1",
        "relative_path": f"2026-09-20 10-00-00_post/{media_id}.mp4",
        "content_sha256": f"sha-{media_id}",
        "chat_id": "oc-chat",
        "parent_id": "om-topic",
        "status": "sent",
        "file_key": f"key-{media_id}",
        "send_uuid": None,
        "send_started_at": None,
        "message_id": f"om-{media_id}",
        "legacy_source_path": None,
        "legacy_progress_file": None,
        "created_at": "2026-09-24T00:00:00+00:00",
        "updated_at": "2026-09-24T00:00:00+00:00",
    }
    values.update(fields)
    return FileDeliveryRecord(**values)


class Ledger:
    def __init__(self, records: list[FileDeliveryRecord]) -> None:
        self.files = {record.media_id: record for record in records}

    async def list_files(self, *, round: str, status: str, limit: int) -> list[Any]:
        return [
            item
            for item in self.files.values()
            if item.round == round and item.status == status
        ]

    async def get_file(self, media_id: str) -> FileDeliveryRecord | None:
        return self.files.get(media_id)

    async def reserve_file(self, **fields: Any) -> FileDeliveryRecord:
        record = _record(
            fields["media_id"],
            status="planned",
            file_key=None,
            message_id=None,
            **{key: value for key, value in fields.items() if key != "media_id"},
        )
        self.files.setdefault(record.media_id, record)
        return self.files[record.media_id]

    async def set_file_key(self, media_id: str, file_key: str) -> FileDeliveryRecord:
        assert self.files[media_id].status == "planned"
        self.files[media_id] = replace(
            self.files[media_id], status="uploaded", file_key=file_key
        )
        return self.files[media_id]

    async def begin_send(self, media_id: str) -> FileDeliveryRecord:
        assert self.files[media_id].status == "uploaded"
        self.files[media_id] = replace(
            self.files[media_id],
            status="sending",
            send_uuid=uuid.uuid4().hex,
            send_started_at=datetime.now(UTC).isoformat(),
        )
        return self.files[media_id]

    async def mark_sent(self, media_id: str, message_id: str) -> FileDeliveryRecord:
        self.files[media_id] = replace(
            self.files[media_id], status="sent", message_id=message_id
        )
        return self.files[media_id]

    async def mark_file_review(self, media_id: str) -> FileDeliveryRecord:
        self.files[media_id] = replace(self.files[media_id], status="needs_review")
        return self.files[media_id]


class Channel:
    def __init__(self) -> None:
        self.sends: list[tuple[str, str, dict[str, str], str]] = []
        self.fail_next = False

    async def _send_message(
        self, chat_id: str, msg_type: str, content: dict[str, str], *, request_uuid: str
    ) -> tuple[dict[str, str] | None, Any]:
        self.sends.append((chat_id, msg_type, content, request_uuid))
        if self.fail_next:
            self.fail_next = False
            return None, {"code": 99991400}
        return {"message_id": f"om-flat-{len(self.sends)}"}, None


async def _resend(ledger: Ledger, channel: Channel, **options: Any) -> dict[str, int]:
    values = {"round_name": "weekly0913", "apply": True, "limit": 10, "interval": 0}
    values.update(options)
    return await script.resend(ledger, channel, **values)


async def test_dry_run_counts_thread_sends_without_writing() -> None:
    ledger = Ledger([_record("a"), _record("b"), _record("plain", parent_id=None)])
    channel = Channel()

    counts = await _resend(ledger, channel, apply=False)

    assert counts == {"thread_sends": 2, "to_copy": 2}
    assert channel.sends == [] and len(ledger.files) == 3


async def test_each_thread_send_is_copied_once_in_post_order() -> None:
    ledger = Ledger(
        [
            _record("b", relative_path="2026-09-21 10-00-00_post/b.mp4"),
            _record("a", relative_path="2026-09-20 10-00-00_post/a.mp4"),
        ]
    )
    channel = Channel()

    first = await _resend(ledger, channel, limit=1)
    second = await _resend(ledger, channel)
    third = await _resend(ledger, channel)

    assert first == {"thread_sends": 2, "copied": 1, "deferred": 1}
    assert second == {"thread_sends": 2, "already_copied": 1, "copied": 1}
    assert third == {"thread_sends": 2, "already_copied": 2}
    assert [send[2]["file_key"] for send in channel.sends] == ["key-a", "key-b"]
    assert all(send[:2] == ("oc-chat", "file") for send in channel.sends)
    copy = ledger.files[script.copy_media_id("weekly0913-flat", "a")]
    assert (copy.round, copy.parent_id, copy.status) == (
        "weekly0913-flat",
        None,
        "sent",
    )


async def test_an_unconfirmed_copy_retries_with_its_uuid_then_goes_to_review() -> None:
    ledger = Ledger([_record("a")])
    channel = Channel()
    channel.fail_next = True

    failed = await _resend(ledger, channel)
    retried = await _resend(ledger, channel)

    assert failed == {"thread_sends": 1, "unconfirmed_99991400": 1}
    assert retried == {"thread_sends": 1, "copied": 1}
    assert channel.sends[0][3] == channel.sends[1][3]

    stale = Ledger([_record("b")])
    media_id = script.copy_media_id("weekly0913-flat", "b")
    stale.files[media_id] = _record(
        media_id,
        round="weekly0913-flat",
        parent_id=None,
        status="sending",
        send_uuid="old-uuid",
        send_started_at=(datetime.now(UTC) - timedelta(hours=2)).isoformat(),
        message_id=None,
    )
    reviewed = await _resend(stale, channel)
    assert reviewed == {"thread_sends": 1, "review": 1}
    assert stale.files[media_id].status == "needs_review"
    assert len(channel.sends) == 2
