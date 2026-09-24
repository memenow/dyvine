"""The older-chat audit reads history only and binds its evidence to the plan."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from dyvine.db.models import DownloadQueueRow

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts import audit_other_chats as script  # noqa: E402
from scripts.feishu_audit_core import AuditError, FeishuReadError, _digest  # noqa: E402
from scripts.queue_group_attestation import read_other_chat_audit  # noqa: E402
from scripts.queue_group_inputs import load_group_inputs  # noqa: E402

KEY = "weekly0913:sec-one"


def _file(message_id: str, name: str, **fields: Any) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "msg_type": "file",
        "deleted": False,
        "sender": {"sender_type": "app", "id": "app-id"},
        "body": {"content": json.dumps({"file_name": name, "file_key": message_id})},
        **fields,
    }


class Reader:
    def __init__(self) -> None:
        self.status = {
            "oc-gone": "dissolved",
            "oc-old": "normal",
            "oc-broken": "normal",
        }
        self.pages: dict[tuple[str, str], list[dict[str, Any]]] = {
            ("chat", "oc-old"): [
                _file("om-1", "2026-09-01 12-00-00_clip_video.mp4"),
                _file("om-2", "no-date.mp4"),
                _file("om-3", "user.mp4", sender={"sender_type": "user", "id": "u"}),
                _file("om-4", "gone.mp4", deleted=True),
                {"message_id": "om-root", "msg_type": "post", "thread_id": "omt-1"},
            ],
            ("thread", "omt-1"): [
                {"message_id": "om-root", "msg_type": "post", "thread_id": "omt-1"},
                _file("om-5", "2026-08-30 08-00-00_old_image_1.webp"),
            ],
        }
        self.calls: list[tuple[str, str]] = []

    async def get_chat(self, chat_id: str) -> dict[str, Any]:
        self.calls.append(("chat", chat_id))
        return {"chat_id": chat_id, "chat_status": self.status[chat_id]}

    async def get_message(self, message_id: str) -> dict[str, Any]:
        raise AssertionError("the older-chat audit reads history only")

    async def list_messages(
        self, container_type: str, container_id: str, page_token: str | None
    ) -> dict[str, Any]:
        self.calls.append((container_type, container_id))
        if container_id == "oc-broken":
            raise FeishuReadError("Feishu history read failed (HTTP 400)")
        items = self.pages[(container_type, container_id)]
        start = int(page_token or 0)
        return {
            "items": items[start : start + 2],
            "has_more": start + 2 < len(items),
            "page_token": str(start + 2),
        }


def _queue(round_name: str, chat: str) -> DownloadQueueRow:
    return DownloadQueueRow(
        key=f"{round_name}:sec-one",
        round=round_name,
        sec_user_id="sec-one",
        nickname="Alpha",
        chat_id=chat,
    )


async def _history(_report: dict[str, Any]) -> script.History:
    queues = [
        _queue("weekly0913", "oc-chat"),
        _queue("weekly0906", "oc-old"),
        _queue("weekly0830", "oc-gone"),
        _queue("weekly0823", "oc-broken"),
    ]
    return "oc-chat", queues, {"Alpha"}


def _sources(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    report = tmp_path / "frozen.jsonl"
    row = {
        "key": KEY,
        "round": "weekly0913",
        "sec_user_id": "sec-one",
        "nickname": "Alpha",
        "legacy_status": "pending",
        "send_blocked": True,
    }
    report.write_text(json.dumps(row) + "\n", encoding="utf-8")
    progress = tmp_path / "progress.json"
    progress.write_text("{}", encoding="utf-8")
    details = progress.stat()
    work = tmp_path / "work.sqlite3"
    connection = sqlite3.connect(work)
    connection.executescript("""
        CREATE TABLE work_meta (key TEXT, value TEXT);
        CREATE TABLE source_files (path TEXT, size INTEGER, mtime_ns INTEGER);
        CREATE TABLE source_user_stats (
          source_file TEXT, round TEXT, nickname TEXT, chat_id TEXT,
          failed INTEGER, sent INTEGER, total INTEGER
        );
    """)
    connection.execute("INSERT INTO work_meta VALUES ('schema_version', '8')")
    connection.execute(
        "INSERT INTO source_files VALUES (?, ?, ?)",
        (str(progress), details.st_size, details.st_mtime_ns),
    )
    connection.execute(
        "INSERT INTO source_user_stats VALUES (?, ?, ?, ?, ?, ?, ?)",
        (str(progress), "weekly0913", "Alpha", "oc-chat", 0, 1, 1),
    )
    connection.commit()
    connection.close()
    keys = tmp_path / "keys.txt"
    keys.write_text(KEY + "\n", encoding="utf-8")
    keys.chmod(0o600)
    manifest = _digest(
        {
            "legacy_report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
            "round": "weekly0913",
        }
    )
    return report, work, keys, manifest


def _args(tmp_path: Path, report: Path, work: Path, keys: Path) -> Any:
    return script.parse_args(
        [
            "--legacy-report",
            str(report),
            "--round",
            "weekly0913",
            "--legacy-work-db",
            str(work),
            "--keys-file",
            str(keys),
            "--output",
            str(tmp_path / "other-chats.jsonl"),
        ]
    )


@pytest.mark.asyncio
async def test_audit_journals_each_older_chat_without_writing_state(
    tmp_path: Path,
) -> None:
    report, work, keys, manifest = _sources(tmp_path)
    args = _args(tmp_path, report, work, keys)
    reader = Reader()

    counts = await script.audit(
        args, reader=reader, load_history=_history, app_id="app-id"
    )

    assert counts == {"accounts": 1, "dissolved": 1, "scanned": 1, "unreadable": 1}
    assert stat.S_IMODE(args.output.stat().st_mode) == 0o600
    rows = [json.loads(line) for line in args.output.read_text().splitlines()]
    assert rows[1] == {
        "type": "account",
        "key": KEY,
        "group_chat_id": "oc-chat",
        "other_chat_ids": ["oc-broken", "oc-gone", "oc-old"],
    }
    by_chat = {row["chat_id"]: row for row in rows[2:]}
    assert by_chat["oc-gone"]["chat_status"] == "dissolved"
    assert by_chat["oc-gone"]["scan_complete"] is False
    assert ("chat", "oc-gone") in reader.calls
    assert by_chat["oc-broken"]["scan_complete"] is False
    assert "HTTP 400" in by_chat["oc-broken"]["read_error"]
    assert by_chat["oc-old"]["scan_complete"] is True
    assert by_chat["oc-old"]["app_file_names"] == [
        "2026-08-30 08-00-00_old_image_1.webp",
        "2026-09-01 12-00-00_clip_video.mp4",
        "no-date.mp4",
    ]
    assert by_chat["oc-old"]["history_messages"] == 6

    evidence, sha256 = read_other_chat_audit(args.output, manifest, {KEY})
    assert set(evidence[KEY]) == {"oc-broken", "oc-gone", "oc-old"}
    assert sha256 == hashlib.sha256(args.output.read_bytes()).hexdigest()


@pytest.mark.asyncio
async def test_audit_rejects_keys_outside_the_frozen_round(tmp_path: Path) -> None:
    report, work, keys, _manifest = _sources(tmp_path)
    keys.write_text(KEY + "\nweekly0913:sec-two\n", encoding="utf-8")

    with pytest.raises(AuditError, match="absent from the frozen report"):
        await script.audit(
            _args(tmp_path, report, work, keys),
            reader=Reader(),
            load_history=_history,
            app_id="app-id",
        )


@pytest.mark.parametrize("tamper", ["manifest", "outside_key", "repeat", "names"])
def test_reader_rejects_unbound_or_malformed_evidence(
    tmp_path: Path, tamper: str
) -> None:
    path = tmp_path / "other-chats.jsonl"
    manifest = {
        "type": "manifest",
        "schema": 1,
        "source_sha256": script.other_chat_source_digest("manifest"),
    }
    chat = {
        "type": "other_chat",
        "key": KEY,
        "chat_id": "oc-old",
        "chat_status": "normal",
        "scan_complete": True,
        "app_file_names": [],
    }
    rows: list[dict[str, Any]] = [manifest, chat]
    if tamper == "manifest":
        rows[0] = {**manifest, "source_sha256": "other"}
    elif tamper == "outside_key":
        rows[1] = {**chat, "key": "weekly0906:sec-one"}
    elif tamper == "repeat":
        rows.append(chat)
    else:
        rows[1] = {**chat, "app_file_names": "2026-09-01 12-00-00_clip_video.mp4"}
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))

    with pytest.raises(ValueError, match="other-chat audit"):
        read_other_chat_audit(path, "manifest", {KEY})


@pytest.mark.asyncio
async def test_group_inputs_bind_the_older_chat_audit_into_the_plan(
    tmp_path: Path,
) -> None:
    report, work, keys, manifest = _sources(tmp_path)
    args = _args(tmp_path, report, work, keys)
    await script.audit(args, reader=Reader(), load_history=_history, app_id="app-id")
    audit = tmp_path / "audit.jsonl"
    audit.write_text(
        json.dumps({"type": "manifest", "schema": 1, "source_sha256": manifest}) + "\n",
        encoding="utf-8",
    )
    frozen = json.loads(report.read_text(encoding="utf-8"))
    common: dict[str, Any] = {
        "source_report": report,
        "reviewed_rows": [frozen],
        "selected_keys": {KEY},
        "audit_path": audit,
        "work_path": work,
        "active_round": "weekly0913",
    }

    plain = load_group_inputs(**common)
    bound = load_group_inputs(**common, other_chat_audit_path=args.output)

    assert plain.other_chats == {} and plain.other_chat_audit_sha256 is None
    assert set(bound.other_chats[KEY]) == {"oc-broken", "oc-gone", "oc-old"}
    assert bound.evidence_digests == (
        *plain.evidence_digests,
        hashlib.sha256(args.output.read_bytes()).hexdigest(),
    )
