"""A plain chat root is not an API thread until Feishu supplies thread_id."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts import feishu_audit_core as core  # noqa: E402


@pytest.mark.asyncio
async def test_unlinked_legacy_files_do_not_guess_a_thread_or_receipt(
    tmp_path: Path,
) -> None:
    target = core.Target(
        key="weekly0913:sec-one",
        round="weekly0913",
        sec_user_id="sec-one",
        nickname="Alpha",
        chat_id="oc-chat",
        topic_message_id="om-topic",
        group_status="ready",
        topic_status="ready",
        queue_chat_ids=("oc-chat",),
        legacy={"first_seen_safe_sent_paths": 1},
        files=[
            {
                "relative_path": "post/one.mp4",
                "status": "legacy_confirmed_sent",
                "message_id": None,
                "file_key": None,
            }
        ],
        source_sha256="source",
    )

    class Reader:
        thread_calls = 0

        async def list_messages(
            self, container_type: str, _container_id: str, _cursor: str | None
        ) -> dict[str, Any]:
            if container_type == "thread":
                self.thread_calls += 1
                raise AssertionError("a root message ID is not a thread ID")
            return {
                "items": [
                    {
                        "message_id": "om-topic",
                        "msg_type": "post",
                        "chat_id": "oc-chat",
                    },
                    {
                        "message_id": "om-file",
                        "msg_type": "file",
                        "chat_id": "oc-chat",
                        "sender": {"sender_type": "app", "id": "cli-app"},
                        "body": {
                            "content": json.dumps(
                                {"file_key": "key-one", "file_name": "one.mp4"}
                            )
                        },
                    },
                ],
                "has_more": False,
            }

        async def get_message(self, _message_id: str) -> dict[str, Any]:
            return {"message_id": "om-topic", "msg_type": "post", "chat_id": "oc-chat"}

    reader = Reader()
    journal = core.Journal(tmp_path / "audit.jsonl", "digest", resume=False)
    journal.append(
        {
            "type": "topic_root",
            "key": target.key,
            "source_sha256": target.source_sha256,
            "message_id": "om-topic",
            "thread_id": "om-topic",
        }
    )
    try:
        result = await core._audit_target(
            target, reader, journal, {"oc-chat": {"sec-one"}}, "cli-app"
        )
        roots = [
            row for row in journal.for_target(target) if row.get("type") == "topic_root"
        ]
    finally:
        journal.close()
    assert reader.thread_calls == 0
    assert roots[-1]["thread_id"] is None
    assert roots[-1]["thread_id_source"] == "absent"
    assert result["scan_complete"] is True
    assert result["group_file_count"] == 1
    assert result["topic_file_count"] == 0
    assert result["app_group_file_count"] == 1
    assert result["app_outside_topic_message_ids"] == ["om-file"]
    assert result["verified_receipts"] == result["candidate_receipts"] == []
    assert result["unmatched_ledger_paths"] == ["post/one.mp4"]
    assert result["zero_file_group"] is False
    assert result["send_blocked"] is True
