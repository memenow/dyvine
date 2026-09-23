"""Legacy group adoption needs exact queue identity and live Feishu reads."""

from __future__ import annotations

import json
import sqlite3
import stat
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from dyvine.services.delivery import FeishuCredentials

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts import adopt_legacy_groups as script  # noqa: E402
from scripts import legacy_group_candidates as candidates  # noqa: E402


class FakeReader:
    def __init__(self) -> None:
        self.chats: dict[str, dict[str, Any]] = {}
        self.messages: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail_after: int | None = None

    async def get_chat(self, chat_id: str) -> dict[str, Any]:
        self.calls.append(("chat", chat_id))
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise RuntimeError("interrupted")
        return self.chats[chat_id]

    async def get_message(self, message_id: str) -> dict[str, Any]:
        self.calls.append(("message", message_id))
        return self.messages[message_id]

    def add(self, chat_id: str, topic: str, *, owner: str = "ou_owner") -> None:
        self.chats[chat_id] = {
            "chat_id": chat_id,
            "chat_mode": "group",
            "chat_status": "normal",
            "owner_id_type": "open_id",
            "owner_id": owner,
        }
        self.messages[topic] = {
            "message_id": topic,
            "chat_id": chat_id,
            "deleted": False,
            "sender": {"sender_type": "app", "id": "app-id"},
        }


class FakeLedger:
    def __init__(self) -> None:
        self.imports: list[dict[str, str]] = []

    async def import_legacy_group_topic(self, **fields: str) -> None:
        self.imports.append(fields)


def _queue(round_name: str, sec: str, nickname: str, chat: str) -> dict[str, str]:
    return {
        "round": round_name,
        "sec_user_id": sec,
        "nickname": nickname,
        "chat_id": chat,
    }


def _sources(
    tmp_path: Path,
    entries: list[dict[str, str]],
    topics: list[tuple[str, str, str]],
) -> tuple[Path, Path, Path]:
    queue_path = tmp_path / "queue.json"
    queue_path.write_text(json.dumps({"entries": entries}), encoding="utf-8")
    progress = tmp_path / "send_progress_weekly0913_w1.json"
    progress.write_text("frozen", encoding="utf-8")
    details = progress.stat()
    work_db = tmp_path / "stage.sqlite3"
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
            (str(progress), details.st_size, details.st_mtime_ns),
        )
        connection.execute(
            "INSERT INTO work_meta VALUES ('identity_fingerprint', 'frozen')"
        )
        connection.executemany(
            "INSERT INTO source_topics VALUES (?, ?, ?, ?)",
            [
                (str(progress), round_name, raw_name, topic)
                for round_name, raw_name, topic in topics
            ],
        )
        connection.commit()
    finally:
        connection.close()
    return queue_path, work_db, progress


def _args(queue_path: Path, work_db: Path, output: Path, *extra: str) -> Any:
    return script.parse_args(
        [
            "--queue-path",
            str(queue_path),
            "--work-db",
            str(work_db),
            "--output",
            str(output),
            *extra,
        ]
    )


@pytest.fixture(autouse=True)
def _feishu_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DYVINE_WEEKLY_OWNER_OPEN_ID", "ou_owner")
    monkeypatch.setattr(
        script.FeishuCredentials,
        "from_hermes_default",
        staticmethod(lambda: FeishuCredentials("app-id", "secret")),
    )


@pytest.mark.asyncio
async def test_group_reader_unwraps_live_chat_response() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "test-token"}
            )
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "chat": {
                        "chat_id": "oc_one",
                        "chat_status": "normal",
                        "owner_id": "ou_owner",
                    }
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        reader = script.GroupReader(client, FeishuCredentials("app-id", "secret"), 0.1)
        chat = await reader.get_chat("oc_one")
    assert chat == {
        "chat_id": "oc_one",
        "chat_status": "normal",
        "owner_id": "ou_owner",
    }


@pytest.mark.asyncio
async def test_group_reader_uses_requested_id_when_chat_response_omits_it() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "test-token"}
            )
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "chat_status": "normal",
                    "owner_id": "ou_owner",
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        reader = script.GroupReader(client, FeishuCredentials("app-id", "secret"), 0.1)
        chat = await reader.get_chat("oc_one")
    assert chat["chat_id"] == "oc_one"
    assert chat["owner_id"] == "ou_owner"


@pytest.mark.asyncio
async def test_feishu_mismatch_and_deleted_topic_never_verify() -> None:
    candidate = candidates.Candidate(
        "queue:0", "r", "sec-a", "Alpha", "oc_one", "om_one", "/f", None
    )
    reader = FakeReader()
    reader.add("oc_one", "om_one")
    assert (await script._verify(candidate, reader, "app-id", "ou_owner"))[
        0
    ] == "verified"

    reader.chats["oc_one"]["chat_status"] = "dissolved"
    assert (await script._verify(candidate, reader, "app-id", "ou_owner"))[0] == (
        "feishu_chat_not_active"
    )
    reader.chats["oc_one"]["chat_status"] = "normal"
    reader.chats["oc_one"]["owner_id"] = "ou_other"
    assert (await script._verify(candidate, reader, "app-id", "ou_owner"))[0] == (
        "feishu_chat_owner_mismatch"
    )
    reader.chats["oc_one"]["owner_id"] = "ou_owner"
    reader.messages["om_one"]["chat_id"] = "oc_other"
    assert (await script._verify(candidate, reader, "app-id", "ou_owner"))[0] == (
        "feishu_topic_chat_mismatch"
    )
    reader.messages["om_one"]["chat_id"] = "oc_one"
    reader.messages["om_one"]["deleted"] = True
    assert (await script._verify(candidate, reader, "app-id", "ou_owner"))[0] == (
        "feishu_topic_deleted"
    )
    reader.messages["om_one"]["deleted"] = False
    reader.messages["om_one"]["sender"]["id"] = "other-app"
    assert (await script._verify(candidate, reader, "app-id", "ou_owner"))[0] == (
        "feishu_topic_app_mismatch"
    )


@pytest.mark.asyncio
async def test_dry_run_resume_then_apply_is_bounded_and_idempotent(
    tmp_path: Path,
) -> None:
    queue_path, work_db, progress = _sources(
        tmp_path,
        [_queue("weekly0913", "sec-a", "Alpha", "oc_one")],
        [("weekly0913", "Alpha:oc_one", "om_one")],
    )
    output = tmp_path / "private.jsonl"
    reader = FakeReader()
    reader.add("oc_one", "om_one")
    ledger = FakeLedger()

    dry = await script.run(_args(queue_path, work_db, output), reader=reader)
    assert dry["verified"] == 1
    assert len(dry["source_sha256"]) == 64
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    first_calls = len(reader.calls)
    resumed = await script.run(
        _args(queue_path, work_db, output, "--resume"), reader=reader
    )
    assert resumed == dry
    assert len(reader.calls) == first_calls

    applied = await script.run(
        _args(queue_path, work_db, output, "--resume", "--apply"),
        reader=reader,
        ledger=ledger,
    )
    assert applied["applied"] == 1
    assert ledger.imports == [
        {
            "round": "weekly0913",
            "sec_user_id": "sec-a",
            "nickname": "Alpha",
            "chat_id": "oc_one",
            "topic_message_id": "om_one",
            "source_file": str(progress),
        }
    ]
    await script.run(
        _args(queue_path, work_db, output, "--resume", "--apply"),
        reader=reader,
        ledger=ledger,
    )
    assert len(ledger.imports) == 1


@pytest.mark.asyncio
async def test_interrupted_dry_run_resumes_without_repeating_checked_row(
    tmp_path: Path,
) -> None:
    queue_path, work_db, _ = _sources(
        tmp_path,
        [
            _queue("r", "sec-a", "Alpha", "oc_one"),
            _queue("r", "sec-b", "Beta", "oc_two"),
        ],
        [("r", "Alpha:oc_one", "om_one"), ("r", "Beta:oc_two", "om_two")],
    )
    output = tmp_path / "private.jsonl"
    reader = FakeReader()
    reader.add("oc_one", "om_one")
    reader.add("oc_two", "om_two")
    reader.fail_after = 2
    with pytest.raises(RuntimeError, match="interrupted"):
        await script.run(_args(queue_path, work_db, output), reader=reader)
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert [row["type"] for row in rows] == ["manifest", "candidate"]

    resumed_reader = FakeReader()
    resumed_reader.add("oc_one", "om_one")
    resumed_reader.add("oc_two", "om_two")
    result = await script.run(
        _args(queue_path, work_db, output, "--resume"), reader=resumed_reader
    )
    assert result["verified"] == 2
    assert resumed_reader.calls == [("chat", "oc_two"), ("message", "om_two")]


@pytest.mark.asyncio
async def test_changed_source_rejects_resume(tmp_path: Path) -> None:
    queue_path, work_db, progress = _sources(
        tmp_path,
        [_queue("r", "sec-a", "Alpha", "oc_one")],
        [("r", "Alpha:oc_one", "om_one")],
    )
    output = tmp_path / "private.jsonl"
    reader = FakeReader()
    reader.add("oc_one", "om_one")
    await script.run(_args(queue_path, work_db, output), reader=reader)
    progress.write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="source changed"):
        await script.run(_args(queue_path, work_db, output, "--resume"), reader=reader)


@pytest.mark.asyncio
async def test_missing_protected_owner_stops_before_feishu_or_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue_path, work_db, _ = _sources(
        tmp_path,
        [_queue("r", "sec-a", "Alpha", "oc_one")],
        [("r", "Alpha:oc_one", "om_one")],
    )
    output = tmp_path / "private.jsonl"
    reader = FakeReader()
    monkeypatch.delenv("DYVINE_WEEKLY_OWNER_OPEN_ID")

    with pytest.raises(ValueError, match="DYVINE_WEEKLY_OWNER_OPEN_ID"):
        await script.run(_args(queue_path, work_db, output), reader=reader)
    assert reader.calls == []
    assert not output.exists()


@pytest.mark.asyncio
async def test_round_scope_excludes_other_history_and_binds_resume(
    tmp_path: Path,
) -> None:
    queue_path, work_db, _ = _sources(
        tmp_path,
        [
            _queue("weekly0913", "sec-a", "Alpha", "oc_one"),
            _queue("weekly0920", "sec-a", "Alpha", "oc_two"),
        ],
        [
            ("weekly0913", "Alpha:oc_one", "om_one"),
            ("weekly0913", "Missing:oc_missing", "om_missing"),
            ("weekly0920", "Alpha:oc_two", "om_two"),
            ("weekly0920", "Other:oc_other", "om_other"),
        ],
    )
    output = tmp_path / "scoped.jsonl"
    reader = FakeReader()
    reader.add("oc_one", "om_one")

    result = await script.run(
        _args(queue_path, work_db, output, "--round", "weekly0913"),
        reader=reader,
    )

    assert result["round"] == "weekly0913"
    assert result["queue_rows"] == result["unmatched_topics"] == 1
    assert result["verified"] == 1
    assert reader.calls == [("chat", "oc_one"), ("message", "om_one")]
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert [row["type"] for row in rows] == [
        "manifest",
        "candidate",
        "unmatched_topic",
        "summary",
    ]
    assert all(row.get("round") == "weekly0913" for row in rows[1:])

    with pytest.raises(script.AuditError, match="journal source or schema differs"):
        await script.run(
            _args(
                queue_path,
                work_db,
                output,
                "--round",
                "weekly0920",
                "--resume",
            ),
            reader=reader,
        )
    assert reader.calls == [("chat", "oc_one"), ("message", "om_one")]


@pytest.mark.asyncio
async def test_group_reader_only_uses_read_endpoints() -> None:
    requests: list[tuple[str, str]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "token", "expire": 3600},
            )
        if request.url.path.endswith("/chats/oc_one"):
            assert request.url.params["user_id_type"] == "open_id"
            return httpx.Response(200, json={"code": 0, "data": {"chat_id": "oc_one"}})
        return httpx.Response(
            200, json={"code": 0, "data": {"items": [{"message_id": "om_one"}]}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        reader = script.GroupReader(client, FeishuCredentials("app-id", "secret"), 0)
        assert (await reader.get_chat("oc_one"))["chat_id"] == "oc_one"
        assert (await reader.get_message("om_one"))["message_id"] == "om_one"
    assert requests == [
        ("POST", "/open-apis/auth/v3/tenant_access_token/internal"),
        ("GET", "/open-apis/im/v1/chats/oc_one"),
        ("GET", "/open-apis/im/v1/messages/om_one"),
    ]
