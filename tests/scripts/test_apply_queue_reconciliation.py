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


def _user_skip(round_name: str = "weekly0913") -> dict[str, Any]:
    report = _report(round_name, "skipped_404")
    report["resolution"] = {"action": "skip_user_ordered"}
    return report


@pytest.mark.parametrize(
    "field",
    [
        "cache_only_unverified_paths",
        "permanent_failure_paths",
        "permanent_unresolved_paths",
        "ambiguous_sent_paths",
    ],
)
def test_user_ordered_skip_closes_active_row_without_send_evidence(
    field: str,
) -> None:
    """A skip sends nothing, so unresolved send evidence cannot hold it."""
    script = _load_script()
    report = _user_skip()
    report[field] = 3
    decision = _assess(script, report)
    assert (decision.status, decision.action) == ("skipped", "skip_user_ordered")
    assert not decision.receipts


def test_user_ordered_skip_requires_a_recorded_skip_in_the_active_round() -> None:
    script = _load_script()
    not_skipped = _report()
    not_skipped["resolution"] = {"action": "skip_user_ordered"}
    assert _assess(script, not_skipped).status is None
    assert _assess(script, _user_skip("weekly0906")).status is None


def test_user_ordered_skip_cannot_override_the_migrated_queue_state() -> None:
    """The report alone cannot turn a row the queue did not skip into a skip."""
    script = _load_script()
    report = _user_skip()
    queue = _queue(report)
    queue.extra["legacy_queue_status"] = "op_done"
    decision = script._assess(
        report,
        queue,
        active_round="weekly0913",
        archive_historical=False,
        identity_ids={"sec-one"},
        group=_group(report),
        files=[],
        evidence=[],
    )
    assert decision.status is None


@pytest.mark.parametrize(
    ("status", "send_uuid"),
    [("sent", "uuid-new"), ("sending", "uuid-new"), ("legacy_confirmed_sent", "u")],
)
def test_user_ordered_skip_holds_accounts_with_new_send_attempts(
    status: str, send_uuid: str
) -> None:
    script = _load_script()
    report = _user_skip()
    attempt = _file(report)
    attempt.status = status
    attempt.send_uuid = send_uuid
    decision = script._assess(
        report,
        _queue(report),
        active_round="weekly0913",
        archive_historical=False,
        identity_ids={"sec-one"},
        group=_group(report),
        files=[attempt],
        evidence=[],
    )
    assert decision.status is None
    assert decision.reason == "account already has new send attempts in this round"


def _author_skip(round_name: str = "weekly0913") -> dict[str, Any]:
    report = _report(round_name)
    report["resolution"] = {
        "action": "skip_author_unavailable",
        "author": {
            "reason": "deactivated",
            "aweme_count": None,
            "checked_at": "2026-09-24T07:26:00+00:00",
        },
    }
    return report


def test_author_skip_closes_active_row_without_send_evidence() -> None:
    """A skip sends nothing, so unresolved send evidence cannot hold it."""
    script = _load_script()
    report = _author_skip()
    report["cache_only_unverified_paths"] = 3
    report["permanent_failure_paths"] = 2
    decision = _assess(script, report)
    assert (decision.status, decision.action) == ("skipped", "skip_author_unavailable")
    assert not decision.receipts


@pytest.mark.parametrize(
    "author",
    [
        None,
        {"reason": "renamed", "checked_at": "2026-09-24T07:26:00+00:00"},
        {"reason": "banned"},
    ],
    ids=["missing", "unknown_reason", "unchecked"],
)
def test_author_skip_requires_complete_evidence(author: Any) -> None:
    script = _load_script()
    report = _author_skip()
    report["resolution"]["author"] = author
    decision = _assess(script, report)
    assert decision.status is None
    assert decision.reason == "author unavailability evidence is incomplete"


def test_author_skip_applies_only_to_the_active_round() -> None:
    script = _load_script()
    assert _assess(script, _author_skip("weekly0906")).status is None


def test_author_skip_holds_accounts_with_new_send_attempts() -> None:
    script = _load_script()
    report = _author_skip()
    attempt = _file(report)
    attempt.status = "sent"
    attempt.send_uuid = "uuid-new"
    decision = script._assess(
        report,
        _queue(report),
        active_round="weekly0913",
        archive_historical=False,
        identity_ids={"sec-one"},
        group=_group(report),
        files=[attempt],
        evidence=[],
    )
    assert decision.status is None
    assert decision.reason == "account already has new send attempts in this round"


def test_apply_author_skip_excludes_the_seed_after_the_queue_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _load_script()
    report = _author_skip()
    path = tmp_path / "report.jsonl"
    path.write_text(json.dumps(report) + "\n", encoding="utf-8")
    _, source_digest = script._report_rows(path)
    statements: list[Any] = []

    class FakeConnection:
        async def execute(self, statement: Any) -> SimpleNamespace:
            statements.append(statement)
            return SimpleNamespace(rowcount=1)

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
            script.Decision("skipped", "skip_author_unavailable", "author gone"),
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
    result = asyncio.run(script.run(args))
    assert result["counts"] == {"applied": 1}
    assert [statement.table.name for statement in statements] == [
        "download_queue",
        "seed_accounts",
    ]
    queue_values = {
        column.key: value for column, value in statements[0]._values.items()
    }
    written = queue_values["extra"].value
    assert written["reconciliation"]["action"] == "skip_author_unavailable"
    assert written["reconciliation"]["author"]["reason"] == "deactivated"
    assert "migration_needs_reconciliation" not in written
    seed_values = {
        column.key: value.value for column, value in statements[1]._values.items()
    }
    assert seed_values["excluded"] is True


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


def _window_report(old_status: str = "op_done") -> dict[str, Any]:
    report = _report(old_status=old_status)
    report["resolution"] = {
        "action": "release_pending_window_attested",
        "chat_id": "oc-old",
        "topic_message_id": "om-topic",
    }
    return report


def _assess_window(script: Any, report: dict[str, Any], proof: Any) -> Any:
    return script._assess(
        report,
        _queue(report),
        active_round="weekly0913",
        archive_historical=False,
        identity_ids={"sec-one"},
        group=_group(report),
        files=[_file(report)],
        evidence=[],
        window_attestation=proof,
    )


def test_window_attested_release_does_not_require_zero_send_evidence() -> None:
    """The audited chat settles ambiguous, cache-only, and failed paths."""
    script = _load_script()
    report = _window_report(old_status="send_issue")
    for field in (
        "cache_only_unverified_paths",
        "permanent_failure_paths",
        "permanent_unresolved_paths",
        "ambiguous_sent_paths",
        "failed_paths",
    ):
        report[field] = 2
    proof = script.WindowAttestation("audit", "target", "2026-09-06T08:00:00", 3)
    decision = _assess_window(script, report, proof)
    assert (decision.status, decision.action) == (
        "pending",
        "release_pending_window_attested",
    )
    assert decision.window_attestation is proof
    assert decision.receipts == ()


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("no_proof", "complete window attestation is missing"),
        ("held_proof", "Feishu in-window files differ from the legacy ledger"),
        ("other_chat", "group and topic have not been adopted exactly"),
        ("historical_round", "window attestation cannot release this row"),
        ("recorded_skip", "window attestation cannot release this row"),
    ],
)
def test_window_attested_release_holds_without_exact_proof(
    change: str, reason: str
) -> None:
    script = _load_script()
    report = _window_report(
        old_status="skipped_404" if change == "recorded_skip" else "op_done"
    )
    proof: Any = script.WindowAttestation("audit", "target", "2026-09-06T08:00:00", 1)
    if change == "no_proof":
        proof = None
    elif change == "held_proof":
        proof = reason
    elif change == "other_chat":
        report["resolution"]["chat_id"] = "oc-other"
    elif change == "historical_round":
        report = {**report, "key": "weekly0906:sec-one", "round": "weekly0906"}
    decision = _assess_window(script, report, proof)
    assert decision.status is None
    assert decision.reason == reason


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


def test_window_plan_is_bound_to_the_weekly_timezone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _load_script()
    path = tmp_path / "report.jsonl"
    path.write_text(json.dumps(_window_report()) + "\n", encoding="utf-8")
    args = argparse.Namespace(
        report=str(path),
        active_round="weekly0913",
        archive_historical=False,
        apply=False,
        timezone=None,
    )
    monkeypatch.setattr(
        script, "_group_inputs", lambda *_args: SimpleNamespace(evidence_digests=())
    )
    with pytest.raises(ValueError, match="requires --timezone"):
        asyncio.run(script.run(args))


def test_apply_records_window_proof_and_forces_a_fresh_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _load_script()
    report = _window_report()
    path = tmp_path / "report.jsonl"
    path.write_text(json.dumps(report) + "\n", encoding="utf-8")
    _, source_digest = script._report_rows(path)
    inputs = SimpleNamespace(
        journal=SimpleNamespace(sha256="journal"),
        supplemental_journal=None,
        keys_file_sha256=None,
        source_sha256="source",
        work_sha256="work",
        evidence_digests=("source", "journal", "work"),
    )
    written: list[Any] = []

    class FakeConnection:
        async def execute(self, statement: Any) -> SimpleNamespace:
            written.append(statement)
            return SimpleNamespace(rowcount=1)

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

    proof = script.WindowAttestation("audit", "target", "2026-09-06T08:00:00", 4)
    seen_timezones: list[Any] = []

    async def inspect(*_args: Any, **kwargs: Any) -> tuple[Any, Any]:
        seen_timezones.append(kwargs.get("timezone"))
        return (
            script.Decision(
                "pending",
                "release_pending_window_attested",
                "matched",
                window_attestation=proof,
            ),
            _queue(report),
        )

    monkeypatch.setattr(script, "DatabaseSessionFactory", FakeFactory)
    monkeypatch.setattr(script, "_inspect_row", inspect)
    monkeypatch.setattr(script, "_group_inputs", lambda *_args: inputs)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://example")
    plain = script._plan_digest(
        source_digest, "weekly0913", False, inputs.evidence_digests
    )
    args = argparse.Namespace(
        report=str(path),
        active_round="weekly0913",
        archive_historical=False,
        database_url_env="DATABASE_URL",
        apply=True,
        expect_plan_sha256=plain,
        expected_count=1,
        timezone="Asia/Shanghai",
    )
    with pytest.raises(ValueError, match="exact preview digest"):
        asyncio.run(script.run(args))
    preview = asyncio.run(
        script.run(argparse.Namespace(**{**vars(args), "apply": False}))
    )
    args.expect_plan_sha256 = preview["plan_sha256"]
    result = asyncio.run(script.run(args))
    assert result["counts"] == {"applied": 1}
    assert seen_timezones[-1] == "Asia/Shanghai"
    extra = written[-1].compile().params["extra"]
    assert extra["weekly"] == {"fresh_download_confirmed": False}
    assert "migration_needs_reconciliation" not in extra
    assert extra["reconciliation"] | {"applied_at": None} == {
        "action": "release_pending_window_attested",
        "plan_sha256": preview["plan_sha256"],
        "applied_at": None,
        "feishu_history_reference": None,
        "main_audit_sha256": "journal",
        "supplemental_audit_sha256": None,
        "supplemental_keys_sha256": None,
        "audit_source_sha256": "source",
        "work_sha256": "work",
        "audit_sha256": "audit",
        "target_source_sha256": "target",
        "window_cutoff": "2026-09-06T08:00:00",
        "window_file_count": 4,
        "timezone": "Asia/Shanghai",
        "proof": "feishu_window_file_name_multiset",
    }


def _adoption(script: Any) -> Any:
    attestation = sys.modules["scripts.queue_group_attestation"]
    return script.FeishuAdoption(
        audit_sha256="audit",
        target_source_sha256="target",
        scope="window",
        cutoff="2026-09-06T08:00:00",
        adopt=(
            attestation.AdoptedFile(
                "2026-09-11 09-00-00_a/2026-09-11 09-00-00_a_image_1.webp",
                ("om-1",),
                "exact",
            ),
        ),
        demote=("media-gone",),
    )


def _adoption_report(plan: dict[str, Any] | None) -> dict[str, Any]:
    report = _report()
    report["resolution"] = {
        "action": "release_pending_feishu_adopted",
        "chat_id": "oc-old",
        "topic_message_id": "om-topic",
    }
    if plan is not None:
        report["resolution"]["plan"] = plan
    return report


def _assess_adoption(script: Any, report: dict[str, Any], adoption: Any) -> Any:
    return script._assess(
        report,
        _queue(report),
        active_round="weekly0913",
        archive_historical=False,
        identity_ids={"sec-one"},
        group=_group(report),
        files=[_file(report)],
        evidence=[],
        feishu_adoption=adoption,
    )


def test_feishu_adoption_releases_only_with_the_recomputed_plan() -> None:
    script = _load_script()
    adoption = _adoption(script)
    released = _assess_adoption(script, _adoption_report(adoption.payload()), adoption)
    assert (released.status, released.action) == (
        "pending",
        "release_pending_feishu_adopted",
    )
    stale = dict(adoption.payload(), demote=[])
    held = _assess_adoption(script, _adoption_report(stale), adoption)
    assert held.status is None
    assert held.reason == "Feishu adoption plan differs from the audited chat"
    assert held.feishu_adoption == adoption
    missing = _assess_adoption(script, _adoption_report(None), "audit is incomplete")
    assert (missing.status, missing.reason) == (None, "audit is incomplete")


def test_apply_feishu_adoption_writes_queue_then_adopts_then_demotes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _load_script()
    adoption = _adoption(script)
    report = _adoption_report(adoption.payload())
    path = tmp_path / "report.jsonl"
    path.write_text(json.dumps(report) + "\n", encoding="utf-8")
    inputs = SimpleNamespace(
        journal=SimpleNamespace(sha256="journal"),
        supplemental_journal=None,
        keys_file_sha256=None,
        source_sha256="source",
        work_sha256="work",
        evidence_digests=("source", "journal", "work"),
    )
    written: list[Any] = []
    rowcounts: list[int] = [1, 1, 1]

    class FakeConnection:
        async def execute(self, statement: Any) -> SimpleNamespace:
            written.append(statement)
            return SimpleNamespace(rowcount=rowcounts[len(written) - 1])

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
                "pending",
                "release_pending_feishu_adopted",
                "adopted",
                feishu_adoption=adoption,
            ),
            _queue(report),
        )

    monkeypatch.setattr(script, "DatabaseSessionFactory", FakeFactory)
    monkeypatch.setattr(script, "_inspect_row", inspect)
    monkeypatch.setattr(script, "_group_inputs", lambda *_args: inputs)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://example")
    args = argparse.Namespace(
        report=str(path),
        active_round="weekly0913",
        archive_historical=False,
        database_url_env="DATABASE_URL",
        apply=False,
        expect_plan_sha256=None,
        expected_count=None,
        timezone="Asia/Shanghai",
    )
    preview = asyncio.run(script.run(args))
    args.apply = True
    args.expect_plan_sha256 = preview["plan_sha256"]
    args.expected_count = 1
    result = asyncio.run(script.run(args))
    assert result["counts"] == {"applied": 1}
    assert [statement.table.name for statement in written] == [
        "download_queue",
        "delivery_files",
        "delivery_files",
    ]
    extra = written[0].compile().params["extra"]
    assert extra["weekly"] == {"fresh_download_confirmed": False}
    assert extra["reconciliation"]["proof"] == "feishu_chat_adoption"
    assert (
        extra["reconciliation"]["adopted_count"],
        extra["reconciliation"]["demoted_count"],
        extra["reconciliation"]["scope"],
    ) == (1, 1, "window")
    adopted = written[1].compile().params
    assert adopted["round_m0"] == "feishu_adopted"
    assert adopted["status_m0"] == "legacy_confirmed_sent"
    assert adopted["message_id_m0"] == "om-1"
    demoted = written[2].compile().params
    assert (demoted["status"], demoted["round"]) == (
        "legacy_not_in_chat",
        "legacy_disproved",
    )
    written.clear()
    rowcounts[2] = 0
    with pytest.raises(ValueError, match="demotion compare-and-set failed"):
        asyncio.run(script.run(args))
