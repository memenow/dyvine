"""Newly adopted groups can be proposed without changing frozen source rows."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dyvine.db.models import DeliveryGroupRow

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts import propose_group_attested as proposer  # noqa: E402
from scripts.queue_group_attestation import (  # noqa: E402
    AuditJournal,
    FeishuAdoption,
)
from scripts.queue_group_inputs import GroupInputs  # noqa: E402
from scripts.queue_reconciliation_policy import Decision  # noqa: E402


def _source() -> dict[str, Any]:
    return {
        "key": "weekly0913:sec-one",
        "round": "weekly0913",
        "sec_user_id": "sec-one",
        "nickname": "Alpha",
        "legacy_status": "pending",
        "send_blocked": True,
        "adoptable_group_chat_id": None,
        "adoptable_topic_message_id": None,
    }


def _group(*, status: str = "ready", sec: str = "sec-one") -> DeliveryGroupRow:
    return DeliveryGroupRow(
        key="weekly0913:sec-one",
        round="weekly0913",
        sec_user_id=sec,
        nickname="Alpha",
        status=status,
        topic_status="ready",
        chat_id="oc-adopted",
        topic_message_id="om-adopted",
    )


class _Session:
    def __init__(self, group: DeliveryGroupRow | None) -> None:
        self.group = group
        self.read_only = False
        self.group_reads = 0

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    def begin(self) -> _Session:
        return self

    async def execute(self, statement: Any) -> SimpleNamespace:
        assert str(statement) == "SET TRANSACTION READ ONLY"
        self.read_only = True
        return SimpleNamespace()

    async def get(self, model: Any, key: str) -> DeliveryGroupRow | None:
        assert self.read_only
        assert model is DeliveryGroupRow
        assert key == "weekly0913:sec-one"
        self.group_reads += 1
        return self.group


class _Factory:
    def __init__(self, group: DeliveryGroupRow | None) -> None:
        self.transaction = _Session(group)

    def session(self) -> _Session:
        return self.transaction

    async def aclose(self) -> None:
        return None


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    group: DeliveryGroupRow | None,
    *,
    expected: int,
) -> tuple[dict[str, Any], list[dict[str, Any]], _Session]:
    frozen = _source()
    source = tmp_path / "frozen.jsonl"
    output = tmp_path / "reviewed.jsonl"
    source.write_text(json.dumps(frozen) + "\n", encoding="utf-8")
    main = AuditJournal(
        "main-sha",
        "main-source",
        {frozen["key"]: [{"type": "account", "scan_complete": False}]},
    )
    supplemental = AuditJournal(
        "supp-sha",
        "supp-source",
        {frozen["key"]: [{"type": "account", "scan_complete": True}]},
    )
    inputs = GroupInputs(
        source_rows={frozen["key"]: frozen},
        all_rows=[frozen],
        source_sha256="source-sha",
        work_sha256="work-sha",
        work_chats={},
        journal=main,
    )
    inputs = replace(
        inputs,
        supplemental_journal=supplemental,
        supplemental_keys=frozenset({frozen["key"]}),
        keys_file_sha256="keys-sha",
    )
    factory = _Factory(group)
    monkeypatch.setattr(proposer, "load_group_inputs", lambda **_kw: inputs)
    monkeypatch.setattr(proposer, "DatabaseSessionFactory", lambda *_a, **_kw: factory)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://placeholder")

    async def inspect(
        session: _Session, candidate: dict[str, Any], *_args: Any, **_kwargs: Any
    ) -> tuple[Decision, None]:
        assert session.read_only
        assert candidate["resolution"] == {
            "action": "release_pending_group_attested",
            "chat_id": "oc-adopted",
            "topic_message_id": "om-adopted",
        }
        selected, keys_sha = inputs.journal_for(candidate["key"])
        assert selected is supplemental
        assert keys_sha == "keys-sha"
        assert selected.rows_for(candidate["key"])[0]["scan_complete"] is True
        return Decision("pending", "release_pending_group_attested", "matched"), None

    monkeypatch.setattr(proposer, "_inspect_row", inspect)
    args = argparse.Namespace(
        source_report=str(source),
        feishu_audit="main.jsonl",
        supplemental_feishu_audit="supp.jsonl",
        supplemental_keys_file="keys.txt",
        legacy_work_db="work.sqlite3",
        active_round="weekly0913",
        output=str(output),
        database_url_env="DATABASE_URL",
    )
    result = asyncio.run(proposer.propose(args))
    reviewed = [json.loads(line) for line in output.read_text().splitlines()]
    assert result["proposed"] == expected
    assert result["supplemental_audit_sha256"] == "supp-sha"
    assert reviewed[0]["adoptable_group_chat_id"] is None
    assert reviewed[0]["adoptable_topic_message_id"] is None
    return result, reviewed, factory.transaction


def test_missing_frozen_adoption_uses_ready_pg_group_and_supplemental_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _summary, rows, session = _run(tmp_path, monkeypatch, _group(), expected=1)
    assert session.group_reads == 1
    assert rows[0]["resolution"]["chat_id"] == "oc-adopted"


@pytest.mark.parametrize(
    "group", [None, _group(status="creating"), _group(sec="other")]
)
def test_missing_frozen_adoption_holds_unready_or_wrong_account_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    group: DeliveryGroupRow | None,
) -> None:
    _summary, rows, session = _run(tmp_path, monkeypatch, group, expected=0)
    assert session.group_reads == 1
    assert "resolution" not in rows[0]


def test_main_reports_database_failure_cause_and_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Database failures name their cause instead of only the type. (P6-A)"""
    source = tmp_path / "frozen.jsonl"
    source.write_text("{}\n", encoding="utf-8")
    output = tmp_path / "reviewed.jsonl"

    async def _boom(_args: argparse.Namespace) -> dict[str, Any]:
        raise RuntimeError("connection reset by peer")

    monkeypatch.setattr(proposer, "propose", _boom)
    code = proposer.main(
        [
            "--source-report",
            str(source),
            "--feishu-audit",
            str(tmp_path / "main.jsonl"),
            "--legacy-work-db",
            str(tmp_path / "work.sqlite3"),
            "--active-round",
            "weekly0913",
            "--output",
            str(output),
        ]
    )
    assert code == 2
    captured = capsys.readouterr()
    assert "connection reset by peer" in captured.err
    assert "Traceback" in captured.err


def test_feishu_adoption_proposal_carries_the_recomputed_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frozen = _source()
    source = tmp_path / "frozen.jsonl"
    output = tmp_path / "reviewed.jsonl"
    source.write_text(json.dumps(frozen) + "\n", encoding="utf-8")
    journal = AuditJournal("main-sha", "main-source", {frozen["key"]: []})
    inputs = GroupInputs(
        source_rows={frozen["key"]: frozen},
        all_rows=[frozen],
        source_sha256="source-sha",
        work_sha256="work-sha",
        work_chats={},
        journal=journal,
    )
    monkeypatch.setattr(proposer, "load_group_inputs", lambda **_kw: inputs)
    monkeypatch.setattr(
        proposer, "DatabaseSessionFactory", lambda *_a, **_kw: _Factory(_group())
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://placeholder")
    adoption = FeishuAdoption("audit", "target", "full", None, (), ("media-1",))
    seen: list[dict[str, Any]] = []

    async def inspect(
        _session: Any, candidate: dict[str, Any], *_args: Any, **kwargs: Any
    ) -> tuple[Decision, None]:
        assert kwargs["timezone"] == "Asia/Shanghai"
        seen.append(dict(candidate["resolution"]))
        if candidate["resolution"].get("plan") != adoption.payload():
            return (
                Decision(None, "held", "plan differs", feishu_adoption=adoption),
                None,
            )
        return (
            Decision(
                "pending",
                "release_pending_feishu_adopted",
                "adopted",
                feishu_adoption=adoption,
            ),
            None,
        )

    monkeypatch.setattr(proposer, "_inspect_row", inspect)
    args = argparse.Namespace(
        source_report=str(source),
        feishu_audit="main.jsonl",
        legacy_work_db="work.sqlite3",
        active_round="weekly0913",
        output=str(output),
        database_url_env="DATABASE_URL",
        action="release_pending_feishu_adopted",
        timezone="Asia/Shanghai",
    )
    result = asyncio.run(proposer.propose(args))
    reviewed = [json.loads(line) for line in output.read_text().splitlines()]
    assert result["proposed"] == 1
    assert [("plan" in item) for item in seen] == [False, True]
    assert reviewed[0]["resolution"]["plan"] == adoption.payload()


@pytest.mark.parametrize(
    "action", ["release_pending_window_attested", "release_pending_feishu_adopted"]
)
def test_window_action_requires_the_weekly_timezone(
    tmp_path: Path, action: str
) -> None:
    argv = [
        "--source-report",
        str(tmp_path / "frozen.jsonl"),
        "--feishu-audit",
        str(tmp_path / "audit.jsonl"),
        "--legacy-work-db",
        str(tmp_path / "work.sqlite3"),
        "--active-round",
        "weekly0913",
        "--output",
        str(tmp_path / "proposal.jsonl"),
        "--action",
        action,
    ]
    with pytest.raises(SystemExit):
        proposer.parse_args(argv)
    args = proposer.parse_args([*argv, "--timezone", "Asia/Shanghai"])
    assert (args.action, args.timezone) == (action, "Asia/Shanghai")
