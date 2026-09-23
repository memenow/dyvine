"""Feishu history audit stays read-only, complete, and safely resumable."""

from __future__ import annotations

import asyncio
import json
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from dyvine.services.delivery import FeishuCredentials

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts import audit_feishu_delivery as audit  # noqa: E402
from scripts import feishu_audit_core as core  # noqa: E402


def _target(script: Any, *, files: list[dict[str, Any]] | None = None) -> Any:
    return script.Target(
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
        files=(
            files
            if files is not None
            else [
                {
                    "media_id": "media-one",
                    "relative_path": "2026-09-13/one.mp4",
                    "status": "legacy_confirmed_sent",
                    "file_key": None,
                    "message_id": None,
                }
            ]
        ),
        source_sha256="source-one",
    )


def _message(
    message_id: str,
    *,
    thread_id: str | None = None,
    root_id: str | None = None,
    file_name: str | None = None,
    file_key: str | None = None,
    sender_id: str = "cli-app",
    deleted: bool = False,
) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "chat_id": "oc-chat",
        "thread_id": thread_id,
        "root_id": root_id,
        "msg_type": "file" if file_name else "text",
        "body": {
            "content": json.dumps(
                {"file_name": file_name, "file_key": file_key}
                if file_name
                else {"text": "topic"}
            )
        },
        "sender": {"sender_type": "app", "id": sender_id},
        "deleted": deleted,
    }


def _response(items: list[dict[str, Any]], token: str | None = None) -> dict[str, Any]:
    return {
        "code": 0,
        "data": {"items": items, "has_more": token is not None, "page_token": token},
    }


@pytest.mark.asyncio
async def test_audit_reads_chat_roots_all_threads_and_paginates(tmp_path: Path) -> None:
    script = core
    calls: list[tuple[str, str, str | None]] = []

    def serve(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            assert request.method == "POST"
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "secret-token", "expire": 7200},
            )
        assert request.method == "GET"
        assert request.headers["Authorization"] == "Bearer secret-token"
        if request.url.path.endswith("/messages/om-topic"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"items": [_message("om-topic", thread_id="omt-topic")]},
                },
            )
        params = request.url.params
        scope = (
            params["container_id_type"],
            params["container_id"],
            params.get("page_token"),
        )
        calls.append(scope)
        pages = {
            ("chat", "oc-chat", None): _response(
                [
                    _message("om-topic", thread_id="omt-topic"),
                    _message("om-other", thread_id="omt-other"),
                ],
                "next",
            ),
            ("chat", "oc-chat", "next"): _response(
                [_message("om-root-file", file_name="root.mp4", file_key="root-key")]
            ),
            ("thread", "omt-other", None): _response(
                [
                    _message(
                        "om-other-file",
                        thread_id="omt-other",
                        file_name="other.mp4",
                        file_key="other-key",
                    )
                ]
            ),
            ("thread", "omt-topic", None): _response(
                [
                    _message(
                        "om-file",
                        thread_id="omt-topic",
                        root_id="om-topic",
                        file_name="one.mp4",
                        file_key="key-one",
                    )
                ],
                "thread-next",
            ),
            ("thread", "omt-topic", "thread-next"): _response([]),
        }
        return httpx.Response(200, json=pages[scope])

    target = _target(script)
    output = tmp_path / "audit.jsonl"
    journal = script.Journal(output, "report-digest", resume=False)
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
            reader = script.FeishuReader(
                client, FeishuCredentials("cli-app", "secret"), 0
            )
            result = await script._audit_target(
                target, reader, journal, {"oc-chat": {target.sec_user_id}}, "cli-app"
            )
    finally:
        journal.close()
    assert result["scan_complete"] is True
    assert result["group_file_count"] == 3
    assert result["topic_file_count"] == 1
    assert result["app_group_file_count"] == 3
    assert result["app_topic_file_count"] == 1
    assert set(result["app_outside_topic_message_ids"]) == {
        "om-root-file",
        "om-other-file",
    }
    assert "app_files_outside_verified_topic" in result["discrepancies"]
    assert "legacy_safe_sent_count_vs_app_group_files" in result["discrepancies"]
    assert result["zero_file_group"] is False
    assert result["candidate_receipts"] == [
        {
            "relative_path": "2026-09-13/one.mp4",
            "message_id": "om-file",
            "by": "unique_file_name_only",
        }
    ]
    assert result["verified_receipts"] == []
    assert result["unmatched_ledger_paths"] == ["2026-09-13/one.mp4"]
    assert len(calls) == 5
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert "secret-token" not in output.read_text()


@pytest.mark.asyncio
async def test_resume_uses_saved_cursor_without_repeating_page(tmp_path: Path) -> None:
    script = core
    target = _target(script)
    output = tmp_path / "audit.jsonl"
    journal = script.Journal(output, "report-digest", resume=False)
    journal.append(
        {
            "type": "page",
            "key": target.key,
            "source_sha256": target.source_sha256,
            "container_type": "chat",
            "container_id": "oc-chat",
            "request_page_token": None,
            "next_page_token": "next",
            "message_count": 1,
            "threads": [{"thread_id": "omt-topic", "message_id": "om-topic"}],
            "files": [],
        }
    )
    journal.close()
    requests: list[str | None] = []

    def serve(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "token"})
        requests.append(request.url.params.get("page_token"))
        return httpx.Response(200, json=_response([]))

    resumed = script.Journal(output, "report-digest", resume=True)
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
            reader = script.FeishuReader(client, FeishuCredentials("app", "secret"), 0)
            await script._scan_scope(reader, resumed, target, "chat", "oc-chat")
            await script._scan_scope(reader, resumed, target, "chat", "oc-chat")
    finally:
        resumed.close()
    assert requests == ["next"]
    assert output.read_text().count('"type": "page"') == 2


def test_identity_mismatch_never_calls_feishu(tmp_path: Path) -> None:
    script = core
    target = _target(script)
    target.queue_chat_ids = ("oc-other",)
    journal = script.Journal(tmp_path / "report.jsonl", "digest", resume=False)

    class NoReads:
        async def list_messages(self, *_args: Any) -> None:
            raise AssertionError("Feishu must not be called")

    try:
        result = asyncio.run(
            script._audit_target(
                target,
                NoReads(),
                journal,
                {"oc-chat": {target.sec_user_id}},
                "cli-app",
            )
        )
    finally:
        journal.close()
    assert result["reason"] == "queue_group_chat_mismatch"
    assert result["scan_complete"] is False
    assert result["send_blocked"] is True


def test_duplicate_names_and_unverified_senders_do_not_create_receipts() -> None:
    script = core
    target = _target(
        script,
        files=[
            {
                "relative_path": "a/one.mp4",
                "status": "legacy_confirmed_sent",
                "file_key": None,
                "message_id": None,
            },
            {
                "relative_path": "b/one.mp4",
                "status": "legacy_confirmed_sent",
                "file_key": None,
                "message_id": None,
            },
        ],
    )
    rows = [
        {
            "type": "page",
            "container_type": "thread",
            "container_id": "omt-topic",
            "files": [
                {
                    **script._file_message(
                        _message(
                            "om-file",
                            thread_id="omt-topic",
                            file_name="one.mp4",
                            file_key="key-one",
                        )
                    ),
                    "container_type": "thread",
                    "container_id": "omt-topic",
                },
                {
                    **script._file_message(
                        _message(
                            "om-user-file",
                            thread_id="omt-topic",
                            file_name="two.mp4",
                            sender_id="other",
                        )
                    ),
                    "container_type": "thread",
                    "container_id": "omt-topic",
                },
            ],
        }
    ]
    result = script._summary(target, rows, "omt-topic", "cli-app")
    assert result["topic_file_count"] == 2
    assert result["app_topic_file_count"] == 1
    assert result["candidate_receipts"] == []
    assert result["verified_receipts"] == []
    assert result["unmatched_ledger_paths"] == ["a/one.mp4", "b/one.mp4"]
    assert "legacy_safe_sent_count_vs_ledger_files" in result["discrepancies"]


def test_shared_chat_is_allowed_only_for_the_same_stable_account() -> None:
    target = _target(core)
    assert core._target_issue(target, {"oc-chat": {"sec-one"}}) is None
    assert (
        core._target_issue(target, {"oc-chat": {"sec-one", "sec-two"}})
        == "chat_owned_by_multiple_accounts"
    )
    target.identity_consistent = False
    assert (
        core._target_issue(target, {"oc-chat": {"sec-one"}})
        == "group_queue_account_identity_mismatch"
    )


def test_duplicate_file_keys_cannot_verify_a_ledger_receipt() -> None:
    target = _target(core)
    target.files[0]["file_key"] = "shared-key"
    rows = [
        {
            "type": "page",
            "container_type": "thread",
            "container_id": "omt-topic",
            "files": [
                {
                    **core._file_message(
                        _message(
                            message_id,
                            thread_id="omt-topic",
                            file_name=f"{message_id}.mp4",
                            file_key="shared-key",
                        )
                    ),
                    "container_type": "thread",
                    "container_id": "omt-topic",
                }
                for message_id in ("om-one", "om-two")
            ],
        }
    ]
    result = core._summary(target, rows, "omt-topic", "cli-app")
    assert result["verified_receipts"] == []
    assert result["unmatched_ledger_paths"] == ["2026-09-13/one.mp4"]


def test_resume_rejects_changed_evidence_and_partial_tail(tmp_path: Path) -> None:
    script = core
    target = _target(script)
    output = tmp_path / "journal.jsonl"
    journal = script.Journal(output, "same-report", resume=False)
    journal.append(
        {"type": "page", "key": target.key, "source_sha256": target.source_sha256}
    )
    journal.close()
    with output.open("ab") as stream:
        stream.write(b'{"partial":')
    resumed = script.Journal(output, "same-report", resume=True)
    try:
        assert len(resumed.for_target(target)) == 1
        target.source_sha256 = "changed"
        with pytest.raises(script.AuditError, match="target changed"):
            resumed.for_target(target)
    finally:
        resumed.close()
    assert output.read_bytes().endswith(b"\n")
    with pytest.raises(script.AuditError, match="journal source"):
        script.Journal(output, "different-report", resume=True)


@pytest.mark.asyncio
async def test_business_rate_limit_retries_without_sending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(core.asyncio, "sleep", no_wait)

    def serve(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.method == "POST":
            assert request.url.path.endswith("/tenant_access_token/internal")
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "token"})
        assert request.method == "GET"
        calls += 1
        if calls == 1:
            return httpx.Response(400, json={"code": 99991400})
        return httpx.Response(200, json=_response([]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        reader = core.FeishuReader(client, FeishuCredentials("app", "secret"), 0)
        result = await reader.list_messages("chat", "oc-chat", None)
    assert calls == 2
    assert result["has_more"] is False


@pytest.mark.asyncio
async def test_incomplete_page_cannot_claim_zero_file_group(tmp_path: Path) -> None:
    target = _target(core)
    output = tmp_path / "audit.jsonl"
    journal = core.Journal(output, "report-digest", resume=False)

    def serve(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "token"})
        return httpx.Response(200, json={"code": 0, "data": {"items": []}})

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
            reader = core.FeishuReader(client, FeishuCredentials("app", "secret"), 0)
            result = await core._audit_target(
                target, reader, journal, {"oc-chat": {target.sec_user_id}}, "app"
            )
    finally:
        journal.close()
    assert result["scan_complete"] is False
    assert result["reason"] == "feishu_chat_read_failed"
    assert result["retryable_read_error"] is True
    assert result["send_blocked"] is True
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert all("zero_file_group" not in row for row in rows)


@pytest.mark.parametrize("root_visible_in_chat", [True, False])
@pytest.mark.asyncio
async def test_direct_history_data_and_topic_root_without_chat_id(
    tmp_path: Path, root_visible_in_chat: bool
) -> None:
    target = _target(core, files=[])
    target.legacy = {"first_seen_safe_sent_paths": 0}
    output = tmp_path / "audit.jsonl"
    journal = core.Journal(output, "report-digest", resume=False)

    def serve(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "token"})
        if request.url.path.endswith("/messages/om-topic"):
            root = _message("om-topic", thread_id="omt-topic")
            root.pop("chat_id")
            root["msg_type"] = "post"
            return httpx.Response(200, json={"code": 0, "data": {"items": [root]}})
        if request.url.params["container_id_type"] == "chat":
            root = _message(
                "om-topic" if root_visible_in_chat else "om-unrelated",
                thread_id="omt-topic" if root_visible_in_chat else None,
            )
            root.pop("chat_id")
            return httpx.Response(200, json={"items": [root], "has_more": False})
        return httpx.Response(200, json={"items": [], "has_more": False})

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
            reader = core.FeishuReader(
                client, FeishuCredentials("cli-app", "secret"), 0
            )
            if root_visible_in_chat:
                result = await core._audit_target(
                    target,
                    reader,
                    journal,
                    {"oc-chat": {target.sec_user_id}},
                    "cli-app",
                )
                assert result["scan_complete"] is True
                assert result["zero_file_group"] is True
            else:
                result = await core._audit_target(
                    target,
                    reader,
                    journal,
                    {"oc-chat": {target.sec_user_id}},
                    "cli-app",
                )
                assert result["scan_complete"] is False
                assert result["reason"] == "topic_root_absent_from_chat_history"
                assert result["send_blocked"] is True
    finally:
        journal.close()
    if not root_visible_in_chat:
        assert '"zero_file_group"' not in output.read_text()


@pytest.mark.asyncio
async def test_empty_page_with_next_cursor_still_reaches_topic_root(
    tmp_path: Path,
) -> None:
    target = _target(core, files=[])
    output = tmp_path / "audit.jsonl"
    journal = core.Journal(output, "digest", resume=False)
    cursors: list[str | None] = []

    def serve(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "token"})
        cursor = request.url.params.get("page_token")
        cursors.append(cursor)
        if cursor is None:
            return httpx.Response(
                200, json={"items": [], "has_more": True, "page_token": "next"}
            )
        return httpx.Response(
            200,
            json={
                "items": [_message("om-topic", thread_id="omt-topic")],
                "has_more": False,
            },
        )

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
            reader = core.FeishuReader(
                client, FeishuCredentials("cli-app", "secret"), 0
            )
            await core._scan_scope(reader, journal, target, "chat", "oc-chat")
            await core._scan_scope(reader, journal, target, "chat", "oc-chat")
            pages = [
                row for row in journal.for_target(target) if row.get("type") == "page"
            ]
    finally:
        journal.close()
    assert cursors == [None, "next"]
    assert len(pages) == 2
    assert pages[0]["topic_root_seen"] is False
    assert pages[1]["topic_root_seen"] is True


@pytest.mark.asyncio
async def test_one_thread_read_failure_holds_account_and_later_account_continues(
    tmp_path: Path,
) -> None:
    first = _target(core, files=[])
    first.legacy = {"first_seen_safe_sent_paths": 0}
    second = _target(core, files=[])
    second.key = "weekly0913:sec-two"
    second.sec_user_id = "sec-two"
    second.nickname = "Beta"
    second.chat_id = "oc-two"
    second.topic_message_id = "om-two"
    second.queue_chat_ids = ("oc-two",)
    second.source_sha256 = "source-two"
    second.legacy = {"first_seen_safe_sent_paths": 0}
    owners = {"oc-chat": {"sec-one"}, "oc-two": {"sec-two"}}

    class Reader:
        fail_first_thread = True

        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def list_messages(
            self, container_type: str, container_id: str, _cursor: str | None
        ) -> dict[str, Any]:
            self.calls.append((container_type, container_id))
            if container_id == "omt-topic" and self.fail_first_thread:
                raise core.FeishuReadError("mock permission failure")
            if container_type == "chat":
                topic = "om-topic" if container_id == "oc-chat" else "om-two"
                return {
                    "items": [
                        {
                            "message_id": topic,
                            "chat_id": container_id,
                            "msg_type": "post",
                            "thread_id": "omt-topic" if topic == "om-topic" else None,
                        }
                    ],
                    "has_more": False,
                }
            return {"items": [], "has_more": False}

        async def get_message(self, message_id: str) -> dict[str, Any]:
            return {
                "message_id": message_id,
                "chat_id": "oc-chat" if message_id == "om-topic" else "oc-two",
            }

    reader = Reader()
    journal = core.Journal(tmp_path / "audit.jsonl", "digest", resume=False)
    try:
        held = await core._audit_target(first, reader, journal, owners, "cli-app")
        complete = await core._audit_target(second, reader, journal, owners, "cli-app")
        assert held["scan_complete"] is False
        assert held["reason"] == "feishu_thread_read_failed"
        assert held["retryable_read_error"] is True
        assert complete["scan_complete"] is True
        assert any(row.get("type") == "page" for row in journal.for_target(first))
        reader.fail_first_thread = False
        resumed = await core._audit_target(first, reader, journal, owners, "cli-app")
        assert resumed["scan_complete"] is True
        assert reader.calls.count(("chat", "oc-chat")) == 1
    finally:
        journal.close()


@pytest.mark.asyncio
async def test_source_checkpoint_error_is_not_treated_as_feishu_read_failure(
    tmp_path: Path,
) -> None:
    target = _target(core)
    journal = core.Journal(tmp_path / "audit.jsonl", "digest", resume=False)
    journal.append({"type": "page", "key": target.key, "source_sha256": "stale"})
    try:
        with pytest.raises(core.AuditError, match="target changed"):
            await core._audit_target(target, None, journal, {}, "cli-app")
    finally:
        journal.close()


def test_other_topic_app_file_prevents_zero_send_inference() -> None:
    target = _target(core, files=[])
    target.legacy = {"first_seen_safe_sent_paths": 0}
    file = core._file_message(
        _message("om-other", thread_id="omt-other", file_name="other.mp4")
    )
    assert file is not None
    file.update({"container_type": "thread", "container_id": "omt-other"})
    result = core._summary(
        target,
        [{"type": "page", "files": [file]}],
        "omt-topic",
        "cli-app",
    )
    assert result["app_group_file_count"] == 1
    assert result["app_topic_file_count"] == 0
    assert result["zero_file_group"] is False
    assert "app_files_outside_verified_topic" in result["discrepancies"]
    assert result["send_blocked"] is True


def test_round_filter_limits_legacy_rows_and_binds_resume_manifest(
    tmp_path: Path,
) -> None:
    legacy = tmp_path / "legacy.jsonl"
    legacy.write_text(
        "\n".join(
            json.dumps(
                {"key": f"{round_name}:{sec}", "round": round_name, "sec_user_id": sec}
            )
            for round_name, sec in (
                ("weekly0913", "sec-one"),
                ("weekly0906", "sec-two"),
            )
        )
        + "\n"
    )
    selected, scoped_digest = audit._load_legacy(legacy, round_name="weekly0913")
    other, other_digest = audit._load_legacy(legacy, round_name="weekly0906")
    all_rows, all_digest = audit._load_legacy(legacy)
    assert list(selected) == ["weekly0913:sec-one"]
    assert list(other) == ["weekly0906:sec-two"]
    assert len(all_rows) == 2
    assert len({scoped_digest, other_digest, all_digest}) == 3
    output = tmp_path / "audit.jsonl"
    journal = core.Journal(output, scoped_digest, resume=False)
    journal.close()
    original = output.read_bytes()
    with pytest.raises(core.AuditError, match="journal source"):
        core.Journal(output, other_digest, resume=True)
    assert output.read_bytes() == original
    with pytest.raises(core.AuditError, match="no rows"):
        audit._load_legacy(legacy, round_name="weekly0920")


@pytest.mark.asyncio
async def test_target_loader_excludes_other_rounds_before_auditing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dyvine.db import session as db_session
    from dyvine.db.models import DeliveryFileRow, DeliveryGroupRow, DownloadQueueRow

    group = SimpleNamespace(
        key="weekly0913:sec-one",
        round="weekly0913",
        sec_user_id="sec-one",
        nickname="Alpha",
        status="ready",
        topic_status="ready",
        chat_id="oc-chat",
        topic_message_id="om-topic",
    )

    class Result:
        def __init__(self, rows: list[Any]) -> None:
            self.rows = rows

        def scalars(self) -> Result:
            return self

        def all(self) -> list[Any]:
            return self.rows

    class Session:
        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        def begin(self) -> Session:
            return self

        async def execute(self, statement: Any) -> Result:
            if statement.is_select:
                entity = statement.column_descriptions[0]["entity"]
                if entity is DeliveryGroupRow:
                    assert "weekly0913" in statement.compile().params.values()
                    return Result([group])
                assert entity in {DownloadQueueRow, DeliveryFileRow}
                return Result([])
            assert str(statement) == "SET TRANSACTION READ ONLY"
            return Result([])

    class Factory:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def session(self) -> Session:
            return Session()

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(db_session, "DatabaseSessionFactory", Factory)
    rows = {
        "weekly0913:sec-one": {
            "key": "weekly0913:sec-one",
            "round": "weekly0913",
            "sec_user_id": "sec-one",
            "nickname": "Alpha",
        },
        "weekly0906:sec-two": {
            "key": "weekly0906:sec-two",
            "round": "weekly0906",
            "sec_user_id": "sec-two",
            "nickname": "Beta",
        },
        "weekly0913:sec-three": {
            "key": "weekly0913:sec-three",
            "round": "weekly0913",
            "sec_user_id": "sec-three",
            "nickname": "Gamma",
        },
    }
    targets = await audit._load_targets("unused", rows, round_name="weekly0913")
    assert [target.key for target in targets] == [
        "weekly0913:sec-one",
        "weekly0913:sec-three",
    ]
    selected = await audit._load_targets(
        "unused",
        rows,
        round_name="weekly0913",
        selected_keys={"weekly0913:sec-one"},
        keys_file_sha256="private-file-digest",
    )
    assert [target.key for target in selected] == ["weekly0913:sec-one"]
    assert selected[0].source_sha256 != targets[0].source_sha256
