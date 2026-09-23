"""Group-level cutover evidence never invents per-file Feishu receipts."""

from __future__ import annotations

import json
import sys
import tracemalloc
from pathlib import Path
from typing import Any

import pytest

from dyvine.db.models import DeliveryFileRow, DeliveryGroupRow, DownloadQueueRow

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.queue_group_attestation import (  # noqa: E402
    AuditJournal,
    GroupAttestation,
    _source_digest,
    attest_group,
    read_audit_journal,
)


def _proof_inputs(names: list[str]) -> dict[str, Any]:
    key = "weekly0913:sec-one"
    report = {
        "key": key,
        "round": "weekly0913",
        "sec_user_id": "sec-one",
        "nickname": "Alpha",
        "first_seen_safe_sent_paths": len(names),
        "ambiguous_sent_paths": 0,
        "cache_only_unverified_paths": 0,
        "permanent_failure_paths": 0,
        "permanent_unresolved_paths": 0,
        "failed_paths": 0,
        "snapshot_failed_entries": 0,
        "legacy_user_failed_max": 0,
    }
    queue = DownloadQueueRow(
        key=key,
        round="weekly0913",
        sec_user_id="sec-one",
        nickname="Alpha",
        chat_id="oc-chat",
        status="needs_reconciliation",
    )
    group = DeliveryGroupRow(
        key=key,
        round="weekly0913",
        sec_user_id="sec-one",
        nickname="Alpha",
        chat_id="oc-chat",
        status="ready",
        topic_status="ready",
        topic_message_id="om-topic",
    )
    ledger = [
        DeliveryFileRow(
            media_id=f"media-{index}",
            round="weekly0913",
            sec_user_id="sec-one",
            relative_path=f"post-{index}/{name}",
            status="legacy_confirmed_sent",
        )
        for index, name in enumerate(names)
    ]
    target = _source_digest(report, group, [queue], ledger)
    raw_files = [
        {
            "message_id": f"om-file-{index}",
            "file_name": name,
            "file_key": f"file-{index}",
            "sender_type": "app",
            "sender_id": "app-one",
            "deleted": False,
            "chat_id": "oc-chat",
        }
        for index, name in enumerate(names)
    ]
    page = {
        "type": "page",
        "key": key,
        "source_sha256": target,
        "container_type": "chat",
        "container_id": "oc-chat",
        "request_page_token": None,
        "next_page_token": None,
        "files": raw_files,
        "threads": [],
    }
    account = {
        "type": "account",
        "key": key,
        "source_sha256": target,
        "round": "weekly0913",
        "sec_user_id": "sec-one",
        "nickname": "Alpha",
        "chat_id": "oc-chat",
        "topic_message_id": "om-topic",
        "thread_id": None,
        "scan_complete": True,
        "send_blocked": True,
        "group_file_count": len(names),
        "app_group_file_count": len(names),
        "legacy_safe_sent_paths": len(names),
        "zero_file_group": not names,
        "discrepancies": ["zero_file_group"] if not names else [],
    }
    journal = AuditJournal("journal-hash", "manifest-hash", {key: [page, account]})
    return {
        "report": report,
        "original": report.copy(),
        "all_report_rows": [report],
        "group": group,
        "current_queues": [queue],
        "current_files": ledger,
        "historical_queues": [queue],
        "historical_files": list(ledger),
        "work_chats": {("weekly0913", "Alpha"): {"oc-chat"}},
        "journal": journal,
    }


@pytest.mark.parametrize("names", [[], ["one.mp4"], ["same.mp4", "same.mp4"]])
def test_group_attestation_matches_complete_file_name_multiset(
    names: list[str],
) -> None:
    proof = attest_group(**_proof_inputs(names))
    assert isinstance(proof, GroupAttestation)
    assert proof.historical_file_count == len(names)


@pytest.mark.parametrize(
    "mutation",
    [
        "extra_file",
        "missing_name",
        "deleted_file",
        "non_app_file",
        "incomplete_scan",
        "other_chat",
        "unresolved_failed",
        "missing_page",
        "stale_source",
    ],
)
def test_group_attestation_holds_any_unexplained_evidence(mutation: str) -> None:
    values = _proof_inputs(["one.mp4"])
    page, account = values["journal"].rows[values["report"]["key"]]
    if mutation == "extra_file":
        page["files"].append({**page["files"][0], "message_id": "extra"})
        account["group_file_count"] = 2
        account["app_group_file_count"] = 2
    elif mutation == "missing_name":
        page["files"][0]["file_name"] = None
    elif mutation == "deleted_file":
        page["files"][0]["deleted"] = True
    elif mutation == "non_app_file":
        page["files"][0]["sender_type"] = "user"
    elif mutation == "incomplete_scan":
        account["scan_complete"] = False
    elif mutation == "other_chat":
        values["work_chats"][("weekly0913", "Alpha")].add("oc-other")
    elif mutation == "unresolved_failed":
        values["report"]["failed_paths"] = 1
    elif mutation == "missing_page":
        values["journal"].rows[values["report"]["key"]].remove(page)
    elif mutation == "stale_source":
        account["source_sha256"] = "older-snapshot"
    assert isinstance(attest_group(**values), str)


def test_group_attestation_accepts_old_files_outside_topic_without_receipts() -> None:
    values = _proof_inputs(["one.mp4"])
    _page, account = values["journal"].rows[values["report"]["key"]]
    account["discrepancies"] = [
        "legacy_safe_sent_count_vs_app_topic_files",
        "app_files_outside_verified_topic",
    ]
    assert isinstance(attest_group(**values), GroupAttestation)
    assert values["historical_files"][0].message_id is None
    assert values["historical_files"][0].parent_id is None


def test_group_attestation_matches_prior_rounds_in_same_chat() -> None:
    values = _proof_inputs(["current.mp4"])
    prior = DeliveryFileRow(
        media_id="prior-media",
        round="weekly0906",
        sec_user_id="sec-one",
        relative_path="older/prior.mp4",
        status="legacy_confirmed_sent",
    )
    values["historical_files"].append(prior)
    values["all_report_rows"].append(
        {
            **values["report"],
            "key": "weekly0906:sec-one",
            "round": "weekly0906",
            "first_seen_safe_sent_paths": 1,
        }
    )
    page, account = values["journal"].rows[values["report"]["key"]]
    page["files"].append(
        {
            **page["files"][0],
            "message_id": "om-prior",
            "file_name": "prior.mp4",
        }
    )
    account["group_file_count"] = 2
    account["app_group_file_count"] = 2
    account["discrepancies"] = ["legacy_safe_sent_count_vs_app_group_files"]
    assert isinstance(attest_group(**values), GroupAttestation)


def test_group_attestation_allows_unknown_old_failure_count_with_exact_group() -> None:
    values = _proof_inputs(["one.mp4"])
    values["report"]["legacy_user_failed_max"] = None
    values["original"] = values["report"].copy()
    source = _source_digest(
        values["original"],
        values["group"],
        values["current_queues"],
        values["current_files"],
    )
    for row in values["journal"].rows[values["report"]["key"]]:
        row["source_sha256"] = source
    result = attest_group(**values)
    assert isinstance(result, GroupAttestation)
    assert result.legacy_user_failed_unknown is True


def test_reused_nickname_in_another_round_does_not_steal_chat_identity() -> None:
    values = _proof_inputs(["one.mp4"])
    values["all_report_rows"].append(
        {
            **values["report"],
            "key": "weekly0906:sec-other",
            "round": "weekly0906",
            "sec_user_id": "sec-other",
        }
    )
    values["work_chats"][("weekly0906", "Alpha")] = {"oc-other"}
    assert isinstance(attest_group(**values), GroupAttestation)


def test_reused_nickname_in_same_round_remains_held() -> None:
    values = _proof_inputs(["one.mp4"])
    values["all_report_rows"].append(
        {
            **values["report"],
            "key": "weekly0913:sec-other",
            "sec_user_id": "sec-other",
        }
    )
    assert isinstance(attest_group(**values), str)


def test_changed_nickname_on_same_stable_account_keeps_one_chat() -> None:
    values = _proof_inputs(["one.mp4"])
    values["historical_queues"].append(
        DownloadQueueRow(
            key="weekly0906:sec-one",
            round="weekly0906",
            sec_user_id="sec-one",
            nickname="EarlierName",
            chat_id="oc-chat",
            status="needs_reconciliation",
        )
    )
    values["all_report_rows"].append(
        {
            **values["report"],
            "key": "weekly0906:sec-one",
            "round": "weekly0906",
            "nickname": "EarlierName",
            "first_seen_safe_sent_paths": 0,
        }
    )
    values["work_chats"][("weekly0906", "EarlierName")] = {"oc-chat"}
    assert isinstance(attest_group(**values), GroupAttestation)


def test_supplemental_target_digest_includes_exact_keys_file_sha() -> None:
    values = _proof_inputs(["one.mp4"])
    keys_sha = "private-keys-file-sha"
    target = _source_digest(
        values["original"],
        values["group"],
        values["current_queues"],
        values["current_files"],
        keys_sha,
    )
    for row in values["journal"].rows[values["report"]["key"]]:
        row["source_sha256"] = target
    assert isinstance(attest_group(**values), str)
    proof = attest_group(**values, keys_file_sha256=keys_sha)
    assert isinstance(proof, GroupAttestation)
    assert proof.target_source_sha256 == target


def test_large_audit_is_indexed_without_retaining_page_payloads(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    key = "weekly0913:sec-one"
    with path.open("w", encoding="utf-8") as output:
        output.write(
            json.dumps({"type": "manifest", "schema": 1, "source_sha256": "source"})
            + "\n"
        )
        for number in range(1000):
            output.write(
                json.dumps(
                    {
                        "type": "page",
                        "key": key,
                        "source_sha256": "target",
                        "number": number,
                        "padding": "x" * 10000,
                    }
                )
                + "\n"
            )
    tracemalloc.start()
    journal = read_audit_journal(path, {key})
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 5_000_000
    assert journal.rows is None
    assert len(journal.index[key]) == 1000
    assert journal.rows_for(key)[-1]["number"] == 999
    with path.open("a", encoding="utf-8") as output:
        output.write("\n")
    with pytest.raises(ValueError, match="changed after indexing"):
        journal.rows_for(key)
