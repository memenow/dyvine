"""Frozen queue decisions require exact evidence before becoming runnable."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dyvine.db.models import (
    DeliveryFileRow,
    DeliveryGroupRow,
    DeliveryLegacyEvidenceRow,
    DownloadQueueRow,
)

ROOT = Path(__file__).resolve().parents[2]


def _load_script() -> Any:
    path = ROOT / "scripts" / "apply_queue_reconciliation.py"
    spec = importlib.util.spec_from_file_location("apply_queue_reconciliation", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _report(
    round_name: str = "weekly0913", old_status: str = "op_done"
) -> dict[str, Any]:
    return {
        "key": f"{round_name}:sec-one",
        "round": round_name,
        "nickname": "Alpha",
        "sec_user_id": "sec-one",
        "legacy_status": old_status,
        "classification": "terminal_candidate",
        "terminal_candidate": True,
        "candidate_total_files": 1,
        "candidate_chat_id": "oc-old",
        "adoptable_group_chat_id": "oc-old",
        "adoptable_topic_message_id": "om-topic",
        "candidate_source_file": "/state/send_progress_weekly0913_w167.json",
        "group_topic_source_file": "/state/send_progress_weekly0913_w167.json",
        "snapshot_sent_entries": 1,
        "first_seen_safe_sent_paths": 1,
        "snapshot_failed_entries": 0,
        "ambiguous_sent_paths": 0,
        "failed_paths": 0,
        "legacy_user_failed_max": 0,
        "legacy_zero_zero_snapshots": 0,
        "cache_only_unverified_paths": 0,
        "permanent_failure_paths": 0,
        "permanent_unresolved_paths": 0,
        "send_blocked": True,
    }


def _queue(report: dict[str, Any]) -> DownloadQueueRow:
    return DownloadQueueRow(
        key=report["key"],
        round=report["round"],
        nickname=report["nickname"],
        sec_user_id=report["sec_user_id"],
        chat_id="oc-old",
        status="needs_reconciliation",
        extra={
            "migration_needs_reconciliation": True,
            "legacy_queue_status": report["legacy_status"],
        },
    )


def _group(report: dict[str, Any]) -> DeliveryGroupRow:
    return DeliveryGroupRow(
        key=report["key"],
        status="ready",
        topic_status="ready",
        chat_id="oc-old",
        topic_message_id="om-topic",
    )


def _file(report: dict[str, Any]) -> DeliveryFileRow:
    return DeliveryFileRow(
        media_id="media-one",
        round=report["round"],
        sec_user_id="sec-one",
        relative_path="2026-09-13_post/one.mp4",
        status="legacy_confirmed_sent",
        chat_id=None,
        parent_id=None,
        message_id=None,
    )


def _resolution(action: str) -> dict[str, Any]:
    return {
        "action": action,
        "feishu_history_reference": "audited chat export 2026-09-23",
        "chat_id": "oc-old",
        "topic_message_id": "om-topic",
        "verified_sent": [
            {
                "relative_path": "2026-09-13_post/one.mp4",
                "message_id": "om-file",
            }
        ],
    }


def _assess(
    script: Any,
    report: dict[str, Any],
    *,
    active_round: str = "weekly0913",
    archive: bool = False,
    identity_ids: set[str] | None = None,
    evidence: list[DeliveryLegacyEvidenceRow] | None = None,
) -> Any:
    return script._assess(
        report,
        _queue(report),
        active_round=active_round,
        archive_historical=archive,
        identity_ids=identity_ids if identity_ids is not None else {"sec-one"},
        group=_group(report),
        files=[_file(report)],
        evidence=evidence or [],
    )


def test_historical_archive_never_releases_active_or_issue_rows() -> None:
    script = _load_script()
    old = _report("weekly0906")
    archived = _assess(script, old, archive=True)
    assert (archived.action, archived.status) == ("archive_historical", "completed")
    assert not archived.receipts

    skipped = _report("weekly0906", "skipped_404")
    assert _assess(script, skipped, archive=True).status == "skipped"
    assert _assess(script, _report(), archive=True).status is None
    assert (
        _assess(script, _report("weekly0906", "send_issue"), archive=True).status
        is None
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("legacy_zero_zero_snapshots", 1),
        ("ambiguous_sent_paths", 1),
        ("failed_paths", 1),
        ("snapshot_failed_entries", 1),
        ("legacy_user_failed_max", 1),
        ("cache_only_unverified_paths", 1),
        ("permanent_failure_paths", 1),
        ("permanent_unresolved_paths", 1),
    ],
)
def test_historical_archive_holds_unresolved_evidence(field: str, value: int) -> None:
    script = _load_script()
    old = _report("weekly0906")
    old[field] = value
    assert _assess(script, old, archive=True).status is None


def test_false_legacy_op_done_cannot_become_verified_completed() -> None:
    script = _load_script()
    report = _report()
    report["resolution"] = _resolution("complete")
    report["classification"] = "needs_review"
    report["terminal_candidate"] = False
    assert _assess(script, report).status is None
    report["classification"] = "terminal_candidate"
    report["terminal_candidate"] = True
    report["snapshot_failed_entries"] = 1
    assert _assess(script, report).status is None


def test_completion_requires_consistent_candidate_and_no_zero_zero() -> None:
    script = _load_script()
    report = _report()
    report["resolution"] = _resolution("complete")
    report["classification"] = "needs_review"
    assert _assess(script, report).status is None
    report["classification"] = "terminal_candidate"
    report["terminal_candidate"] = False
    assert _assess(script, report).status is None
    report["terminal_candidate"] = True
    report["legacy_zero_zero_snapshots"] = 1
    assert _assess(script, report).status is None


def test_completed_requires_exact_message_receipts_and_identity() -> None:
    script = _load_script()
    report = _report()
    report["resolution"] = _resolution("complete")
    complete = _assess(script, report)
    assert complete.status == "completed"
    assert [(item.relative_path, message) for item, message in complete.receipts] == [
        ("2026-09-13_post/one.mp4", "om-file")
    ]
    assert _assess(script, report, identity_ids={"sec-one", "sec-two"}).status is None
    report["resolution"]["verified_sent"][0]["relative_path"] = "other.mp4"
    assert _assess(script, report).status is None


def test_existing_new_send_attempt_cannot_be_adopted_as_legacy_receipt() -> None:
    script = _load_script()
    report = _report()
    report["resolution"] = _resolution("complete")
    file = _file(report)
    file.send_uuid = "already-started"
    decision = script._assess(
        report,
        _queue(report),
        active_round="weekly0913",
        archive_historical=False,
        identity_ids={"sec-one"},
        group=_group(report),
        files=[file],
        evidence=[],
    )
    assert decision.status is None


def test_pending_requires_explicit_unsent_scope_and_adopted_destination() -> None:
    script = _load_script()
    report = _report(old_status="send_issue")
    report["classification"] = "needs_review"
    resolution = _resolution("release_pending")
    resolution["confirmed_no_other_sends"] = True
    resolution["confirmed_unsent_legacy_paths"] = ["/downloads/failed.mp4"]
    report["resolution"] = resolution
    report["failed_paths"] = 1
    report["snapshot_failed_entries"] = 1
    report["legacy_user_failed_max"] = 1
    evidence = [
        DeliveryLegacyEvidenceRow(
            source_file="/state/send_progress_weekly0913_w167.json",
            legacy_path="/downloads/failed.mp4",
            legacy_state="failed",
            nickname="Alpha",
        )
    ]
    assert _assess(script, report, evidence=evidence).status == "pending"
    resolution["confirmed_unsent_legacy_paths"] = []
    assert _assess(script, report, evidence=evidence).status is None
    resolution["confirmed_unsent_legacy_paths"] = ["/downloads/failed.mp4"]
    assert (
        _assess(script, report, active_round="weekly0920", evidence=evidence).status
        is None
    )
    resolution["topic_message_id"] = "other-topic"
    assert _assess(script, report, evidence=evidence).status is None


def test_pending_holds_zero_zero_and_unaccounted_failed_counts() -> None:
    script = _load_script()
    report = _report(old_status="pending")
    report["resolution"] = {
        **_resolution("release_pending"),
        "confirmed_no_other_sends": True,
        "confirmed_unsent_legacy_paths": [],
    }
    assert _assess(script, report).status == "pending"
    report["legacy_zero_zero_snapshots"] = 1
    assert _assess(script, report).status is None
    report["legacy_zero_zero_snapshots"] = 0
    report["snapshot_failed_entries"] = 1
    assert _assess(script, report).status is None
    report["snapshot_failed_entries"] = 0
    report["legacy_user_failed_max"] = 1
    assert _assess(script, report).status is None


def test_group_attested_pending_uses_stable_sec_despite_reused_nickname() -> None:
    script = _load_script()
    report = _report(old_status="pending")
    report["legacy_zero_zero_snapshots"] = 1
    report["legacy_user_failed_max"] = None
    report["resolution"] = {
        "action": "release_pending_group_attested",
        "chat_id": "oc-old",
        "topic_message_id": "om-topic",
    }
    proof = script.GroupAttestation("audit", "target", 1, True)
    decision = script._assess(
        report,
        _queue(report),
        active_round="weekly0913",
        archive_historical=False,
        identity_ids={"sec-one", "sec-other"},
        group=_group(report),
        files=[_file(report)],
        evidence=[],
        group_attestation=proof,
    )
    assert decision.status == "pending"
    assert decision.receipts == ()
    assert decision.group_attestation is proof


@pytest.mark.parametrize("action", ["complete", "release_pending"])
def test_cache_only_unverified_paths_hold_even_with_reviewed_resolution(
    action: str,
) -> None:
    script = _load_script()
    report = _report(old_status="pending" if action == "release_pending" else "op_done")
    report["resolution"] = {
        **_resolution(action),
        "confirmed_no_other_sends": True,
        "confirmed_unsent_legacy_paths": [],
    }
    report["cache_only_unverified_paths"] = 1
    assert _assess(script, report).status is None


@pytest.mark.parametrize("action", ["complete", "release_pending"])
def test_legacy_permanent_failures_cannot_be_sent_or_counted_as_sent(
    action: str,
) -> None:
    script = _load_script()
    report = _report(old_status="pending" if action == "release_pending" else "op_done")
    report["resolution"] = {
        **_resolution(action),
        "confirmed_no_other_sends": True,
        "confirmed_unsent_legacy_paths": [],
    }
    report["permanent_failure_paths"] = 1
    decision = _assess(script, report)
    assert decision.status is None
    assert "permanent" in decision.reason


def test_unresolved_permanent_failure_evidence_holds_release() -> None:
    script = _load_script()
    report = _report(old_status="pending")
    report["resolution"] = {
        **_resolution("release_pending"),
        "confirmed_no_other_sends": True,
        "confirmed_unsent_legacy_paths": [],
    }
    report["permanent_unresolved_paths"] = 1
    decision = _assess(script, report)
    assert decision.status is None
    assert "unresolved" in decision.reason


def test_malformed_queue_extra_is_held_without_crashing() -> None:
    script = _load_script()
    report = _report()
    report["resolution"] = _resolution("complete")
    queue = _queue(report)
    queue.extra = None
    decision = script._assess(
        report,
        queue,
        active_round="weekly0913",
        archive_historical=False,
        identity_ids={"sec-one"},
        group=_group(report),
        files=[_file(report)],
        evidence=[],
    )
    assert decision.status is None


def test_ambiguous_sends_and_404_remain_frozen() -> None:
    script = _load_script()
    report = _report(old_status="skipped_404")
    report["resolution"] = {
        **_resolution("release_pending"),
        "confirmed_no_other_sends": True,
        "confirmed_unsent_legacy_paths": [],
    }
    assert _assess(script, report).status is None
    report["legacy_status"] = "pending"
    report["ambiguous_sent_paths"] = 1
    assert _assess(script, report).status is None


def test_report_file_must_remain_blocked_and_unique(tmp_path: Path) -> None:
    script = _load_script()
    path = tmp_path / "report.jsonl"
    row = _report()
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    rows, source_digest = script._report_rows(path)
    assert rows == [row]
    assert script._plan_digest(
        source_digest, "weekly0913", True
    ) != script._plan_digest(source_digest, "weekly0913", False)
    path.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        script._report_rows(path)
    row["send_blocked"] = False
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="send block"):
        script._report_rows(path)


def test_apply_rejects_a_changed_preview_before_database_access(tmp_path: Path) -> None:
    script = _load_script()
    path = tmp_path / "report.jsonl"
    path.write_text(json.dumps(_report()) + "\n", encoding="utf-8")
    args = argparse.Namespace(
        report=str(path),
        active_round="weekly0913",
        archive_historical=False,
        apply=True,
        expect_plan_sha256="stale-preview",
        expected_count=1,
    )
    with pytest.raises(ValueError, match="exact preview digest"):
        asyncio.run(script.run(args))


@pytest.mark.parametrize("write_counts", [[1, 1], [0], [1, 0]])
def test_apply_uses_guarded_queue_and_receipt_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    write_counts: list[int],
) -> None:
    script = _load_script()
    report = _report()
    report["resolution"] = _resolution("complete")
    path = tmp_path / "report.jsonl"
    path.write_text(json.dumps(report) + "\n", encoding="utf-8")
    _, source_digest = script._report_rows(path)
    statements: list[str] = []

    class FakeConnection:
        async def execute(self, statement: Any) -> SimpleNamespace:
            statements.append(str(statement))
            return SimpleNamespace(rowcount=write_counts[len(statements) - 1])

    class FakeSession:
        async def __aenter__(self) -> FakeSession:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        def begin(self) -> FakeSession:
            return self

        async def connection(self) -> FakeConnection:
            return FakeConnection()

    class FakeFactory:
        def __init__(self, _url: str, **_options: Any) -> None:
            pass

        def session(self) -> FakeSession:
            return FakeSession()

        async def aclose(self) -> None:
            return None

    async def inspect(*_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        return (
            script.Decision(
                "completed", "complete", "verified", ((_file(report), "om-file"),)
            ),
            _queue(report),
        )

    monkeypatch.setattr(script, "DatabaseSessionFactory", FakeFactory)
    monkeypatch.setattr(script, "_inspect_row", inspect)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://example")
    args = argparse.Namespace(
        report=str(path),
        active_round="weekly0913",
        archive_historical=False,
        database_url_env="DATABASE_URL",
        apply=True,
        expect_plan_sha256=script._plan_digest(source_digest, "weekly0913", False),
        expected_count=1,
    )
    if write_counts == [1, 1]:
        result = asyncio.run(script.run(args))
        assert result["counts"] == {"applied": 1}
        assert result["rows"][0]["cache_only_unverified_paths"] == 0
        assert result["rows"][0]["permanent_failure_paths"] == 0
        assert result["rows"][0]["permanent_unresolved_paths"] == 0
        assert len(statements) == 2
        assert "download_queue.status" in statements[0]
        assert "download_queue.extra" in statements[0]
        assert "delivery_files.send_uuid IS NULL" in statements[1]
        assert "delivery_files.message_id IS NULL" in statements[1]
    else:
        with pytest.raises(ValueError, match="compare-and-set failed"):
            asyncio.run(script.run(args))
        assert len(statements) == len(write_counts)
