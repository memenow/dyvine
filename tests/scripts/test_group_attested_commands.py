"""Private proposals and exact evidence-bound queue previews are reversible."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dyvine.db.models import DownloadQueueRow

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts import apply_queue_reconciliation as apply  # noqa: E402
from scripts import propose_group_attested as propose  # noqa: E402
from scripts.feishu_audit_core import AuditError, _digest  # noqa: E402
from scripts.queue_group_attestation import AuditJournal  # noqa: E402
from scripts.queue_group_inputs import GroupInputs, load_group_inputs  # noqa: E402


def _row(sec: str) -> dict[str, Any]:
    return {
        "key": f"weekly0913:{sec}",
        "round": "weekly0913",
        "sec_user_id": sec,
        "nickname": sec,
        "legacy_status": "op_done",
        "send_blocked": True,
        "adoptable_group_chat_id": "oc-chat",
        "adoptable_topic_message_id": "om-topic",
    }


def _write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


class _Session:
    def __init__(self, statements: list[Any]) -> None:
        self.statements = statements

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    def begin(self) -> _Session:
        return self

    async def execute(self, statement: Any) -> Any:
        self.statements.append(statement)
        return SimpleNamespace(rowcount=1)

    async def connection(self) -> _Session:
        return self


class _Factory:
    def __init__(self, _url: str, **_options: Any) -> None:
        self.statements: list[Any] = []

    def session(self) -> _Session:
        return _Session(self.statements)

    async def aclose(self) -> None:
        return None


def _inputs(rows: list[dict[str, Any]]) -> GroupInputs:
    return GroupInputs(
        source_rows={
            row["key"]: {k: v for k, v in row.items() if k != "resolution"}
            for row in rows
        },
        all_rows=rows,
        source_sha256="source-hash",
        work_sha256="work-hash",
        work_chats={},
        journal=AuditJournal("journal-hash", "manifest-hash", {}),
    )


def test_group_preview_and_apply_bind_evidence_without_file_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _row("sec-one")
    row["resolution"] = {
        "action": "release_pending_group_attested",
        "chat_id": "oc-chat",
        "topic_message_id": "om-topic",
    }
    report = tmp_path / "reviewed.jsonl"
    _write_rows(report, [row])
    factory = _Factory("ignored")
    monkeypatch.setattr(apply, "DatabaseSessionFactory", lambda *_a, **_kw: factory)
    monkeypatch.setattr(apply, "_group_inputs", lambda *_a: _inputs([row]))
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://placeholder")

    async def inspect(*_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        queue = DownloadQueueRow(
            key=row["key"],
            round=row["round"],
            sec_user_id=row["sec_user_id"],
            nickname=row["nickname"],
            status="needs_reconciliation",
            extra={"migration_needs_reconciliation": True, "weekly": {}},
        )
        return (
            apply.Decision(
                "pending",
                "release_pending_group_attested",
                "matched",
                group_attestation=apply.GroupAttestation(
                    "journal-hash", "target-hash", 0, True
                ),
            ),
            queue,
        )

    monkeypatch.setattr(apply, "_inspect_row", inspect)
    args = argparse.Namespace(
        report=str(report),
        active_round="weekly0913",
        archive_historical=False,
        database_url_env="DATABASE_URL",
        apply=False,
        expect_plan_sha256=None,
        expected_count=None,
    )
    preview = asyncio.run(apply.run(args))
    assert preview["counts"] == {"eligible": 1}
    assert preview["feishu_audit_sha256"] == "journal-hash"
    assert preview["plan_sha256"] != apply._plan_digest(
        preview["source_sha256"],
        "weekly0913",
        False,
        ("source-hash", "changed-journal", "work-hash"),
    )
    assert factory.statements == []
    args.apply = True
    args.expect_plan_sha256 = preview["plan_sha256"]
    args.expected_count = 1
    applied = asyncio.run(apply.run(args))
    assert applied["counts"] == {"applied": 1}
    assert len(factory.statements) == 1
    parameters = factory.statements[0].compile().params
    extra = next(value for value in parameters.values() if isinstance(value, dict))
    assert extra["reconciliation"]["action"] == "release_pending_group_attested"
    assert extra["reconciliation"]["audit_sha256"] == "journal-hash"
    assert extra["reconciliation"]["target_source_sha256"] == "target-hash"
    assert extra["reconciliation"]["legacy_user_failed_unknown"] is True
    assert extra["weekly"]["fresh_download_confirmed"] is False


def test_load_group_inputs_binds_original_report_journal_and_work_db(
    tmp_path: Path,
) -> None:
    source = tmp_path / "frozen.jsonl"
    reviewed = tmp_path / "reviewed.jsonl"
    original = _row("sec-one")
    changed = {**original, "resolution": {"action": "release_pending_group_attested"}}
    _write_rows(source, [original])
    _write_rows(reviewed, [changed])
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest_hash = _digest(
        {"legacy_report_sha256": source_hash, "round": "weekly0913"}
    )
    journal = tmp_path / "audit.jsonl"
    _write_rows(
        journal, [{"type": "manifest", "schema": 1, "source_sha256": manifest_hash}]
    )
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
        "INSERT INTO work_meta VALUES ('identity_fingerprint', 'identity')"
    )
    connection.execute(
        "INSERT INTO source_files VALUES (?, ?, ?)",
        (str(progress), details.st_size, details.st_mtime_ns),
    )
    connection.execute(
        "INSERT INTO source_user_stats VALUES (?, ?, ?, ?, ?, ?, ?)",
        (str(progress), "weekly0913", "sec-one", "oc-chat", 0, 0, 0),
    )
    connection.commit()
    connection.close()
    inputs = load_group_inputs(
        source_report=source,
        reviewed_rows=[changed],
        selected_keys={original["key"]},
        audit_path=journal,
        work_path=work,
        active_round="weekly0913",
    )
    assert inputs.source_sha256 == source_hash
    assert inputs.work_chats == {("weekly0913", "sec-one"): {"oc-chat"}}
    changed["legacy_status"] = "pending"
    with pytest.raises(ValueError, match="differs from the frozen"):
        load_group_inputs(
            source_report=source,
            reviewed_rows=[changed],
            selected_keys={original["key"]},
            audit_path=journal,
            work_path=work,
            active_round="weekly0913",
        )


def test_private_proposal_adds_only_eligible_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [_row("sec-one"), _row("sec-two")]
    source = tmp_path / "frozen.jsonl"
    output = tmp_path / "reviewed.jsonl"
    _write_rows(source, rows)
    monkeypatch.setattr(propose, "load_group_inputs", lambda **_kw: _inputs(rows))
    monkeypatch.setattr(propose, "DatabaseSessionFactory", _Factory)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://placeholder")

    async def inspect(
        _session: Any, row: dict[str, Any], *_a: Any, **_kw: Any
    ) -> tuple[Any, Any]:
        if row["sec_user_id"] == "sec-one":
            return (
                apply.Decision("pending", "release_pending_group_attested", "matched"),
                None,
            )
        return apply.Decision(None, "held", "extra file in chat"), None

    monkeypatch.setattr(propose, "_inspect_row", inspect)
    args = argparse.Namespace(
        source_report=str(source),
        feishu_audit="audit.jsonl",
        legacy_work_db="work.sqlite3",
        active_round="weekly0913",
        output=str(output),
        database_url_env="DATABASE_URL",
    )
    summary = asyncio.run(propose.propose(args))
    proposed, _sha = apply._report_rows(output)
    assert summary["proposed"] == 1
    assert proposed[0]["resolution"]["action"] == "release_pending_group_attested"
    assert "resolution" not in proposed[1]
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_preview_stdout_is_summary_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fake_run(_args: argparse.Namespace) -> dict[str, Any]:
        return {
            "mode": "dry_run",
            "counts": {"held": 1},
            "held_reasons": {"needs review": 1},
            "rows": [{"key": "weekly0913:private-sec", "reason": "needs review"}],
            "archived_unverified": ["weekly0906:private-sec"],
        }

    monkeypatch.setattr(apply, "run", fake_run)
    output = tmp_path / "private-preview.json"
    assert (
        apply.main(
            [
                "--report",
                str(tmp_path / "reviewed.jsonl"),
                "--active-round",
                "weekly0913",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert "private-sec" not in capsys.readouterr().out
    assert "private-sec" in output.read_text()
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def _supplemental_sources(
    tmp_path: Path,
    *,
    main_complete: bool = False,
    main_page: bool = False,
    extra_supp_key: bool = False,
) -> tuple[Path, list[dict[str, Any]], Path, Path, Path, Path]:
    original = _row("sec-one")
    original["resolution"] = {
        "action": "release_pending_group_attested",
        "chat_id": "oc-chat",
        "topic_message_id": "om-topic",
    }
    frozen = {key: value for key, value in original.items() if key != "resolution"}
    source = tmp_path / "frozen.jsonl"
    _write_rows(source, [frozen])
    base_digest = _digest(
        {
            "legacy_report_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "round": "weekly0913",
        }
    )
    main = tmp_path / "main.jsonl"
    main_rows = [
        {"type": "manifest", "schema": 1, "source_sha256": base_digest},
        {
            "type": "account",
            "key": frozen["key"],
            "source_sha256": "old-target",
            "scan_complete": main_complete,
        },
    ]
    if main_page:
        main_rows.append(
            {"type": "page", "key": frozen["key"], "source_sha256": "old-target"}
        )
    _write_rows(main, main_rows)
    keys = tmp_path / "keys.txt"
    keys.write_text(frozen["key"] + "\n", encoding="utf-8")
    keys.chmod(0o600)
    keys_sha = hashlib.sha256(keys.read_bytes()).hexdigest()
    supplemental_digest = _digest(
        {"legacy_source_sha256": base_digest, "keys_file_sha256": keys_sha}
    )
    supplemental = tmp_path / "supplemental.jsonl"
    rows = [
        {
            "type": "manifest",
            "schema": 1,
            "source_sha256": supplemental_digest,
        },
        {
            "type": "account",
            "key": frozen["key"],
            "source_sha256": "new-target",
            "scan_complete": True,
        },
    ]
    if extra_supp_key:
        rows.append(
            {
                "type": "account",
                "key": "weekly0913:outside-keys",
                "source_sha256": "other-target",
                "scan_complete": True,
            }
        )
    _write_rows(supplemental, rows)
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
    connection.commit()
    connection.close()
    return source, [original], main, supplemental, keys, work


def test_supplemental_audit_binds_exact_keys_and_selects_new_complete_target(
    tmp_path: Path,
) -> None:
    source, reviewed, main, supplemental, keys, work = _supplemental_sources(tmp_path)
    inputs = load_group_inputs(
        source_report=source,
        reviewed_rows=reviewed,
        selected_keys={reviewed[0]["key"]},
        audit_path=main,
        supplemental_audit_path=supplemental,
        supplemental_keys_path=keys,
        work_path=work,
        active_round="weekly0913",
    )
    selected, keys_sha = inputs.journal_for(reviewed[0]["key"])
    assert selected is inputs.supplemental_journal
    assert selected.rows_for(reviewed[0]["key"])[0]["source_sha256"] == "new-target"
    assert keys_sha == hashlib.sha256(keys.read_bytes()).hexdigest()
    assert inputs.evidence_digests == (
        inputs.source_sha256,
        inputs.journal.sha256,
        inputs.work_sha256,
        inputs.supplemental_journal.sha256,
        keys_sha,
    )
    assert apply._plan_digest(
        "reviewed", "weekly0913", False, inputs.evidence_digests
    ) != apply._plan_digest(
        "reviewed",
        "weekly0913",
        False,
        (inputs.source_sha256, inputs.journal.sha256, inputs.work_sha256),
    )


@pytest.mark.parametrize(
    "conflict", ["main_complete", "main_page", "extra_supp_key", "bad_keys_mode"]
)
def test_supplemental_rejects_duplicate_or_untrusted_evidence(
    tmp_path: Path, conflict: str
) -> None:
    source, reviewed, main, supplemental, keys, work = _supplemental_sources(
        tmp_path,
        main_complete=conflict == "main_complete",
        main_page=conflict == "main_page",
        extra_supp_key=conflict == "extra_supp_key",
    )
    if conflict == "bad_keys_mode":
        keys.chmod(0o644)
    expected = {
        "main_complete": (ValueError, "conflicting evidence"),
        "main_page": (ValueError, "conflicting evidence"),
        "extra_supp_key": (ValueError, "outside its keys file"),
        "bad_keys_mode": (AuditError, "owned regular 0600"),
    }[conflict]
    with pytest.raises(expected[0], match=expected[1]):
        load_group_inputs(
            source_report=source,
            reviewed_rows=reviewed,
            selected_keys={reviewed[0]["key"]},
            audit_path=main,
            supplemental_audit_path=supplemental,
            supplemental_keys_path=keys,
            work_path=work,
            active_round="weekly0913",
        )


def test_supplemental_manifest_or_keys_change_invalidates_evidence(
    tmp_path: Path,
) -> None:
    source, reviewed, main, supplemental, keys, work = _supplemental_sources(tmp_path)
    keys.write_text(reviewed[0]["key"] + "\n" + "weekly0913:sec-two\n")
    with pytest.raises(ValueError, match="absent from the frozen report"):
        load_group_inputs(
            source_report=source,
            reviewed_rows=reviewed,
            selected_keys={reviewed[0]["key"]},
            audit_path=main,
            supplemental_audit_path=supplemental,
            supplemental_keys_path=keys,
            work_path=work,
            active_round="weekly0913",
        )
    keys.write_text(reviewed[0]["key"] + "\n", encoding="utf-8")
    lines = supplemental.read_text(encoding="utf-8").splitlines()
    manifest = json.loads(lines[0])
    manifest["source_sha256"] = "wrong-manifest"
    supplemental.write_text(
        json.dumps(manifest) + "\n" + "\n".join(lines[1:]) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="manifest differs"):
        load_group_inputs(
            source_report=source,
            reviewed_rows=reviewed,
            selected_keys={reviewed[0]["key"]},
            audit_path=main,
            supplemental_audit_path=supplemental,
            supplemental_keys_path=keys,
            work_path=work,
            active_round="weekly0913",
        )
