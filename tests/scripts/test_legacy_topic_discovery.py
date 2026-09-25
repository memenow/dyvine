"""Bounded read-only proof for one missing legacy group topic."""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts import legacy_topic_discovery as discovery  # noqa: E402
from scripts.feishu_audit_core import AuditError  # noqa: E402
from scripts.legacy_group_candidates import Candidate  # noqa: E402

APP_ID = "cli_test_app"
SEC = "sec-alpha"
CHAT = "oc_test_chat"


def _candidate(**fields: Any) -> Candidate:
    payload = {
        "row_id": "queue:0",
        "round": "weekly0913",
        "sec_user_id": SEC,
        "nickname": "Alpha",
        "chat_id": CHAT,
        "topic_message_id": None,
        "source_file": None,
        "issue": "missing_or_conflicting_topic",
    }
    payload.update(fields)
    return Candidate(**payload)


def _profile_post(message_id: str, sec: str = SEC) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "chat_id": CHAT,
        "msg_type": "post",
        "sender": {"sender_type": "app", "id": APP_ID},
        "body": {
            "content": {
                "title": "profile",
                "content": [[{"tag": "a", "href": f"https://douyin.com/user/{sec}"}]],
            }
        },
    }


class _FakeReader:
    def __init__(
        self,
        items: list[dict[str, Any]] | None = None,
        failure: Exception | None = None,
    ) -> None:
        self.items = items if items is not None else [_profile_post("om_root")]
        self.failure = failure

    async def get_chat(self, chat_id: str) -> dict[str, Any]:
        if self.failure is not None:
            raise self.failure
        return {
            "chat_id": chat_id,
            "chat_mode": "group",
            "chat_status": "normal",
            "owner_id": "owner-9",
            "owner_id_type": "open_id",
        }

    async def get_message(self, message_id: str) -> dict[str, Any]:
        if self.failure is not None:
            raise self.failure
        for item in self.items:
            if item.get("message_id") == message_id:
                return dict(item)
        raise AuditError("message not found")

    async def list_messages(
        self, container_type: str, container_id: str, page_token: str | None
    ) -> dict[str, Any]:
        if self.failure is not None:
            raise self.failure
        assert container_type == "chat" and container_id == CHAT
        assert page_token is None
        return {"items": list(self.items), "has_more": False}


async def test_incomplete_identity_is_a_conflict_not_a_crash() -> None:
    for fields in (
        {"round": None},
        {"sec_user_id": None},
        {"nickname": None},
        {"chat_id": None},
    ):
        status, _ = await discovery.discover(
            _candidate(**fields), _FakeReader(), APP_ID, None, unique_chat=True
        )
        assert status == "discovery_identity_conflict"
    status, _ = await discovery.discover(
        _candidate(), _FakeReader(), APP_ID, None, unique_chat=False
    )
    assert status == "discovery_identity_conflict"


async def test_exact_profile_root_is_discovered_with_history_proof() -> None:
    status, proof = await discovery.discover(
        _candidate(), _FakeReader(), APP_ID, "owner-9", unique_chat=True
    )

    assert status == "discovered"
    assert proof["discovered_topic_message_id"] == "om_root"
    assert proof["history_messages"] == 1
    assert proof["history_pages"] == 1
    assert proof["chat_owner_open_id"] == "owner-9"
    assert len(proof["chat_history_sha256"]) == 64
    assert len(proof["root_message_sha256"]) == 64


async def test_missing_profile_post_reports_zero_matches() -> None:
    reader = _FakeReader(items=[])
    status, proof = await discovery.discover(
        _candidate(), reader, APP_ID, None, unique_chat=True
    )

    assert status == "feishu_profile_post_missing"
    assert proof == {"profile_post_matches": 0}


async def test_read_failure_reports_the_underlying_cause() -> None:
    reader = _FakeReader(failure=AuditError("tenant token denied for chat"))
    status, proof = await discovery.discover(
        _candidate(), reader, APP_ID, None, unique_chat=True
    )

    assert status == "feishu_read_failed"
    assert proof == {"read_error": "tenant token denied for chat"}


def test_script_uses_no_runtime_asserts() -> None:
    tree = ast.parse(
        (ROOT / "scripts" / "legacy_topic_discovery.py").read_text(encoding="utf-8")
    )
    assert [
        node.lineno for node in ast.walk(tree) if isinstance(node, ast.Assert)
    ] == []
