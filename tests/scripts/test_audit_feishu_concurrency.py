"""Bounded account concurrency shares one Feishu pace and journal."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from dyvine.services.delivery import FeishuCredentials

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts import audit_feishu_delivery as audit  # noqa: E402
from scripts import feishu_audit_core as core  # noqa: E402


@pytest.mark.asyncio
async def test_shared_pace_and_token_lock_limit_concurrent_requests() -> None:
    starts: list[tuple[float, str]] = []

    def serve(request: httpx.Request) -> httpx.Response:
        starts.append((time.monotonic(), request.method))
        if request.method == "POST":
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "token"})
        return httpx.Response(
            200, json={"code": 0, "data": {"items": [], "has_more": False}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        reader = core.FeishuReader(client, FeishuCredentials("app", "secret"), 0.25)
        await asyncio.gather(
            *(reader.list_messages("chat", f"oc-{index}", None) for index in range(3))
        )
    assert [method for _, method in starts].count("POST") == 1
    assert [method for _, method in starts].count("GET") == 3
    assert all(
        later - earlier >= 0.22
        for (earlier, _), (later, _) in zip(starts, starts[1:], strict=False)
    )


@pytest.mark.asyncio
async def test_three_accounts_interleave_page_checkpoints_without_remote_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    targets = [
        core.Target(
            key=f"weekly0913:sec-{index}",
            round="weekly0913",
            sec_user_id=f"sec-{index}",
            nickname=f"Account {index}",
            chat_id=f"oc-{index}",
            topic_message_id=f"om-{index}",
            group_status="ready",
            topic_status="ready",
            queue_chat_ids=(f"oc-{index}",),
            legacy={"first_seen_safe_sent_paths": 0},
            files=[],
            source_sha256=f"source-{index}",
        )
        for index in range(4)
    ]

    async def load_targets(*_args: Any, **_kwargs: Any) -> list[core.Target]:
        return targets

    class Reader:
        instances: list[Reader] = []

        def __init__(self, *_args: Any) -> None:
            self.active = 0
            self.peak = 0
            self.instances.append(self)

        async def list_messages(
            self, _container_type: str, chat_id: str, _cursor: str | None
        ) -> dict[str, Any]:
            self.active += 1
            self.peak = max(self.peak, self.active)
            await asyncio.sleep(0.01)
            self.active -= 1
            return {
                "items": [
                    {"message_id": chat_id.replace("oc-", "om-"), "chat_id": chat_id}
                ],
                "has_more": False,
            }

        async def get_message(self, message_id: str) -> dict[str, Any]:
            await asyncio.sleep(0.02)
            return {
                "message_id": message_id,
                "chat_id": message_id.replace("om-", "oc-"),
            }

    monkeypatch.setattr(audit, "_load_legacy", lambda *_args, **_kwargs: ({}, "digest"))
    monkeypatch.setattr(audit, "_load_targets", load_targets)
    monkeypatch.setattr(audit, "FeishuReader", Reader)
    monkeypatch.setattr(
        FeishuCredentials,
        "from_hermes_default",
        staticmethod(lambda: FeishuCredentials("app", "secret")),
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://unused")
    output = tmp_path / "audit.jsonl"
    args = argparse.Namespace(
        legacy_report=tmp_path / "unused.jsonl",
        round="weekly0913",
        output=output,
        resume=False,
        request_interval=0.25,
    )
    counts = await audit.run(args)
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert counts["accounts"] == 4
    assert Reader.instances[0].peak == 3
    assert len({row["key"] for row in rows[1:4]}) == 3
    assert all(row["type"] == "page" for row in rows[1:4])
    assert len([row for row in rows if row["type"] == "account"]) == 4


def test_old_jsonl_resume_uses_offsets_and_truncates_only_partial_tail(
    tmp_path: Path,
) -> None:
    path = tmp_path / "audit.jsonl"
    manifest = {"type": "manifest", "schema": 1, "source_sha256": "digest"}
    page = {
        "type": "page",
        "key": "weekly0913:sec-one",
        "source_sha256": "source",
        "container_type": "chat",
        "container_id": "oc-chat",
        "request_page_token": None,
        "next_page_token": "next",
        "message_count": 0,
        "threads": [],
        "files": [],
    }
    prefix = (json.dumps(manifest) + "\n" + json.dumps(page) + "\n").encode()
    path.write_bytes(prefix + b'{"partial":')
    os.chmod(path, 0o600)
    target = core.Target(
        key=page["key"],
        round="weekly0913",
        sec_user_id="sec-one",
        nickname="Alpha",
        chat_id="oc-chat",
        topic_message_id="om-topic",
        group_status="ready",
        topic_status="ready",
        queue_chat_ids=("oc-chat",),
        legacy=None,
        files=[],
        source_sha256="source",
    )
    journal = core.Journal(path, "digest", resume=True)
    try:
        assert journal.for_target(target) == [page]
        earlier = journal.rows
        assert [row["type"] for row in earlier] == ["manifest", "page"]
        journal.append({**page, "request_page_token": "next", "next_page_token": None})
        assert len(journal.for_target(target)) == 2
        assert len(earlier) == 2
        assert len(journal.rows) == 3
    finally:
        journal.close()
    assert path.read_bytes().startswith(prefix)
    assert b'"partial"' not in path.read_bytes()


def test_complete_corrupt_jsonl_line_is_never_truncated(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    contents = b'{"type":"manifest","schema":1,"source_sha256":"digest"}\n{invalid}\n'
    path.write_bytes(contents)
    os.chmod(path, 0o600)
    with pytest.raises(core.AuditError, match="invalid JSONL"):
        core.Journal(path, "digest", resume=True)
    assert path.read_bytes() == contents


@pytest.mark.asyncio
async def test_batch_keeps_source_error_fatal_and_closes_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
        legacy=None,
        files=[],
        source_sha256="source",
    )

    async def load_targets(*_args: Any, **_kwargs: Any) -> list[core.Target]:
        return [target]

    async def fail_source(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise core.AuditError("target changed since checkpoint")

    monkeypatch.setattr(audit, "_load_legacy", lambda *_args, **_kwargs: ({}, "digest"))
    monkeypatch.setattr(audit, "_load_targets", load_targets)
    monkeypatch.setattr(audit, "_audit_target", fail_source)
    monkeypatch.setattr(audit, "FeishuReader", lambda *_args: object())
    monkeypatch.setattr(
        FeishuCredentials,
        "from_hermes_default",
        staticmethod(lambda: FeishuCredentials("app", "secret")),
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://unused")
    output = tmp_path / "audit.jsonl"
    args = argparse.Namespace(
        legacy_report=tmp_path / "unused.jsonl",
        round="weekly0913",
        output=output,
        resume=False,
        request_interval=0.25,
    )
    with pytest.raises(core.AuditError, match="target changed"):
        await audit.run(args)
    reopened = core.Journal(output, "digest", resume=True)
    reopened.close()
