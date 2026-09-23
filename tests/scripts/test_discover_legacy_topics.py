"""Opt-in recovery of a missing legacy topic from complete Feishu history."""

from __future__ import annotations

import json
import sqlite3
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from dyvine.services.delivery import FeishuCredentials

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts import adopt_legacy_groups as script  # noqa: E402
from scripts import legacy_topic_discovery as discovery  # noqa: E402
from scripts.legacy_group_candidates import Candidate  # noqa: E402


def _post(
    message_id: str, sec: str = "sec-a", nickname: str = "Alpha"
) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "chat_id": "oc_one",
        "msg_type": "post",
        "deleted": False,
        "sender": {"sender_type": "app", "id": "app-id"},
        "body": {
            "content": json.dumps(
                {
                    "zh_cn": {
                        "content": [
                            [
                                {
                                    "tag": "a",
                                    "text": nickname,
                                    "href": f"https://www.douyin.com/user/{sec}",
                                }
                            ]
                        ]
                    }
                }
            )
        },
    }


class HistoryReader:
    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.messages = messages
        self.calls: list[tuple[str, str | None]] = []
        self.chat_status = "normal"

    async def get_chat(self, chat_id: str) -> dict[str, Any]:
        self.calls.append(("chat", chat_id))
        return {
            "chat_id": chat_id,
            "chat_mode": "group",
            "chat_status": self.chat_status,
            "owner_id_type": "open_id",
            "owner_id": "ou_owner",
        }

    async def get_message(self, message_id: str) -> dict[str, Any]:
        self.calls.append(("message", message_id))
        return next(item for item in self.messages if item["message_id"] == message_id)

    async def list_messages(
        self, container_type: str, container_id: str, page_token: str | None
    ) -> dict[str, Any]:
        assert (container_type, container_id) == ("chat", "oc_one")
        self.calls.append(("page", page_token))
        start = int(page_token or 0)
        end = start + 50
        return {
            "items": self.messages[start:end],
            "has_more": end < len(self.messages),
            "page_token": str(end) if end < len(self.messages) else None,
        }


class FakeLedger:
    def __init__(self) -> None:
        self.imports: list[dict[str, str]] = []

    async def import_legacy_group_topic(self, **fields: str) -> None:
        self.imports.append(fields)


def _candidate() -> Candidate:
    return Candidate(
        "queue:0",
        "weekly0913",
        "sec-a",
        "Alpha",
        "oc_one",
        None,
        None,
        "missing_or_conflicting_topic",
    )


def _sources(tmp_path: Path) -> tuple[Path, Path]:
    queue = tmp_path / "queue.json"
    queue.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "round": "weekly0913",
                        "sec_user_id": "sec-a",
                        "nickname": "Alpha",
                        "chat_id": "oc_one",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    progress = tmp_path / "send_progress_weekly0913_w1.json"
    progress.write_text("frozen", encoding="utf-8")
    info = progress.stat()
    work_db = tmp_path / "work.sqlite3"
    connection = sqlite3.connect(work_db)
    try:
        connection.executescript("""
            CREATE TABLE source_files (path TEXT, size INTEGER, mtime_ns INTEGER);
            CREATE TABLE source_topics (
              source_file TEXT, round TEXT, nickname TEXT, topic_message_id TEXT
            );
            CREATE TABLE work_meta (key TEXT, value TEXT);
        """)
        connection.execute(
            "INSERT INTO source_files VALUES (?, ?, ?)",
            (str(progress), info.st_size, info.st_mtime_ns),
        )
        connection.execute(
            "INSERT INTO work_meta VALUES ('identity_fingerprint', 'frozen')"
        )
        connection.commit()
    finally:
        connection.close()
    return queue, work_db


def _args(queue: Path, work_db: Path, output: Path, *more: str) -> Any:
    return script.parse_args(
        [
            "--queue-path",
            str(queue),
            "--work-db",
            str(work_db),
            "--round",
            "weekly0913",
            "--output",
            str(output),
            "--discover-missing-topics",
            *more,
        ]
    )


def test_profile_identity_uses_exact_https_host_and_stable_user_path() -> None:
    def link(href: str) -> dict[str, str]:
        return {"tag": "a", "text": "Old Alpha", "href": href}

    assert discovery._exact_profile_link(
        link("https://www.douyin.com/user/sec-a"), "sec-a"
    )
    for href in (
        "http://www.douyin.com/user/sec-a",
        "https://evil.example/user/sec-a",
        "https://www.douyin.com/user/sec-a-other",
        "https://www.douyin.com/user/sec-a/extra",
    ):
        assert not discovery._exact_profile_link(link(href), "sec-a")


@pytest.fixture(autouse=True)
def _credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DYVINE_WEEKLY_OWNER_OPEN_ID", "ou_owner")
    monkeypatch.setattr(
        script.FeishuCredentials,
        "from_hermes_default",
        staticmethod(lambda: FeishuCredentials("app-id", "secret")),
    )


@pytest.mark.asyncio
async def test_complete_paging_selects_one_exact_profile_post() -> None:
    files = [
        {"message_id": f"om_file_{index}", "chat_id": "oc_one", "msg_type": "file"}
        for index in range(255)
    ]
    reader = HistoryReader([*files, _post("om_root")])

    decision, evidence = await script._discover(
        _candidate(), reader, "app-id", "ou_owner", unique_chat=True
    )

    assert decision == "discovered"
    assert evidence["discovered_topic_message_id"] == "om_root"
    assert (evidence["history_pages"], evidence["history_messages"]) == (6, 256)
    assert evidence["history_file_messages"] == 255
    assert len(evidence["chat_history_sha256"]) == 64
    assert sum(kind == "page" for kind, _ in reader.calls) == 6


@pytest.mark.asyncio
async def test_absent_ambiguous_or_conflicting_profile_never_discovers() -> None:
    reader = HistoryReader([_post("om_wrong", "sec-a-other")])
    assert (
        await script._discover(
            _candidate(), reader, "app-id", "ou_owner", unique_chat=True
        )
    )[0] == "feishu_profile_post_missing"
    reader.messages = [_post("om_wrong_name", nickname="Old Alpha")]
    decision, evidence = await script._discover(
        _candidate(), reader, "app-id", "ou_owner", unique_chat=True
    )
    assert decision == "discovered"
    assert evidence["discovered_topic_message_id"] == "om_wrong_name"
    reader.messages = [_post("om_one"), _post("om_two")]
    assert (
        await script._discover(
            _candidate(), reader, "app-id", "ou_owner", unique_chat=True
        )
    )[0] == "feishu_profile_post_ambiguous"
    before = len(reader.calls)
    assert (
        await script._discover(
            _candidate(), reader, "app-id", "ou_owner", unique_chat=False
        )
    )[0] == "discovery_identity_conflict"
    assert len(reader.calls) == before
    reader.chat_status = "dissolved"
    assert (
        await script._discover(
            _candidate(), reader, "app-id", "ou_owner", unique_chat=True
        )
    )[0] == "feishu_chat_not_active"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("other_sec", "other_nickname", "expected"),
    [
        ("sec-a", "Alpha", "discovered"),
        ("sec-b", "Beta", "discovery_identity_conflict"),
    ],
)
async def test_chat_reuse_is_unique_by_stable_account_across_rounds(
    tmp_path: Path, other_sec: str, other_nickname: str, expected: str
) -> None:
    queue, work_db = _sources(tmp_path)
    document = json.loads(queue.read_text(encoding="utf-8"))
    document["entries"].append(
        {
            "round": "weekly0920",
            "sec_user_id": other_sec,
            "nickname": other_nickname,
            "chat_id": "oc_one",
        }
    )
    queue.write_text(json.dumps(document), encoding="utf-8")
    reader = HistoryReader([_post("om_root")])

    result = await script.run(
        _args(queue, work_db, tmp_path / "private.jsonl"), reader=reader
    )

    assert result[expected] == 1
    assert bool(reader.calls) is (expected == "discovered")


@pytest.mark.asyncio
async def test_discovery_dry_run_journal_and_apply_rechecks_history(
    tmp_path: Path,
) -> None:
    queue, work_db = _sources(tmp_path)
    output = tmp_path / "private.jsonl"
    reader = HistoryReader([_post("om_root")])
    ledger = FakeLedger()

    dry = await script.run(_args(queue, work_db, output), reader=reader)
    assert dry["discovered"] == 1
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert rows[1]["discovered_topic_message_id"] == "om_root"
    assert rows[1]["decision"] == "discovered"

    default_args = script.parse_args(
        [
            "--queue-path",
            str(queue),
            "--work-db",
            str(work_db),
            "--round",
            "weekly0913",
            "--output",
            str(output),
            "--resume",
        ]
    )
    with pytest.raises(script.AuditError, match="journal source or schema differs"):
        await script.run(default_args, reader=reader)
    frozen_queue = queue.read_text(encoding="utf-8")
    queue.write_text(frozen_queue.replace("Alpha", "Old Alpha"), encoding="utf-8")
    with pytest.raises(script.AuditError, match="journal source or schema differs"):
        await script.run(
            _args(queue, work_db, output, "--resume", "--apply"),
            reader=reader,
            ledger=ledger,
        )
    queue.write_text(frozen_queue, encoding="utf-8")

    reader.messages.append(
        {"message_id": "om_new", "chat_id": "oc_one", "msg_type": "file"}
    )
    held = await script.run(
        _args(queue, work_db, output, "--resume", "--apply"),
        reader=reader,
        ledger=ledger,
    )
    assert held["apply_held"] == 1
    assert ledger.imports == []

    reader.messages.pop()
    applied = await script.run(
        _args(queue, work_db, output, "--resume", "--apply"),
        reader=reader,
        ledger=ledger,
    )
    assert applied["applied"] == 1
    assert ledger.imports[0]["topic_message_id"] == "om_root"
    assert ledger.imports[0]["source_file"] == str(output.resolve())
    assert ledger.imports[0]["sec_user_id"] == "sec-a"
