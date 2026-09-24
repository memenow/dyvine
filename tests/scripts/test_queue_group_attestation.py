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

from dyvine.services.delivery import legacy_upload_file_name  # noqa: E402
from scripts.queue_group_attestation import (  # noqa: E402
    AuditJournal,
    FeishuAdoption,
    GroupAttestation,
    WindowAttestation,
    _source_digest,
    attest_group,
    attest_window,
    plan_feishu_adoption,
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


_IN_WINDOW = "2026-09-10 12-00-00"
_BEFORE_CUTOFF = "2026-09-01 12-00-00"


def _media(stamp: str, caption: str = "clip", slot: str = "_video.mp4") -> str:
    folder = f"{stamp}_{caption}"
    return f"{folder}/{folder}{slot}"


def _chat_file(path: str, **overrides: Any) -> dict[str, Any]:
    return {"file_name": legacy_upload_file_name(Path(path)), **overrides}


def _window_inputs(
    chat: list[dict[str, Any]],
    ledger_paths: list[str],
    *,
    cutoff: str | None = "2026-09-06T08:00:00",
    mode: str = "incremental",
) -> dict[str, Any]:
    key = "weekly0913:sec-one"
    report = {
        "key": key,
        "round": "weekly0913",
        "sec_user_id": "sec-one",
        "nickname": "Alpha",
        "first_seen_safe_sent_paths": len(ledger_paths),
        # The chat settles these, so the window proof must not require zeros.
        "ambiguous_sent_paths": 3,
        "cache_only_unverified_paths": 2,
        "legacy_user_failed_max": 1,
    }
    queue = DownloadQueueRow(
        key=key,
        round="weekly0913",
        sec_user_id="sec-one",
        nickname="Alpha",
        chat_id="oc-chat",
        status="needs_reconciliation",
        mode=mode,
        cutoff=cutoff,
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
            relative_path=path,
            status="legacy_confirmed_sent",
        )
        for index, path in enumerate(ledger_paths)
    ]
    target = _source_digest(report, group, [queue], ledger)
    raw_files = [
        {
            "message_id": f"om-file-{index}",
            "file_key": f"file-{index}",
            "sender_type": "app",
            "sender_id": "app-one",
            "deleted": False,
            "chat_id": "oc-chat",
            **item,
        }
        for index, item in enumerate(chat)
    ]
    active = [item for item in raw_files if not item["deleted"]]
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
        "group_file_count": len(active),
        "app_group_file_count": sum(item["sender_type"] == "app" for item in active),
        "legacy_safe_sent_paths": len(ledger_paths),
        "zero_file_group": not active,
        "discrepancies": ["app_files_outside_verified_topic"],
    }
    journal = AuditJournal("journal-hash", "manifest-hash", {key: [page, account]})
    return {
        "report": report,
        "original": report.copy(),
        "all_report_rows": [report],
        "group": group,
        "queue": queue,
        "current_queues": [queue],
        "current_files": ledger,
        "historical_queues": [queue],
        "historical_files": list(ledger),
        "work_chats": {("weekly0913", "Alpha"): {"oc-chat"}},
        "journal": journal,
        "timezone": "Asia/Shanghai",
    }


def test_window_attestation_ignores_older_rounds_in_the_same_chat() -> None:
    sent = _media(_IN_WINDOW)
    values = _window_inputs(
        [_chat_file(sent), _chat_file(_media(_BEFORE_CUTOFF, "older round"))],
        [sent],
    )
    proof = attest_window(**values)
    assert isinstance(proof, WindowAttestation)
    assert proof.window_file_count == 1
    assert proof.cutoff == "2026-09-06T08:00:00"


@pytest.mark.parametrize(
    ("chat_paths", "ledger_paths"),
    [
        # Sent but unrecorded: releasing would send it twice.
        ([_media(_IN_WINDOW), _media(_IN_WINDOW, "other")], [_media(_IN_WINDOW)]),
        # Recorded but absent from the chat: releasing would never send it.
        ([_media(_IN_WINDOW)], [_media(_IN_WINDOW), _media(_IN_WINDOW, "other")]),
    ],
)
def test_window_attestation_holds_any_in_window_difference(
    chat_paths: list[str], ledger_paths: list[str]
) -> None:
    values = _window_inputs([_chat_file(path) for path in chat_paths], ledger_paths)
    assert attest_window(**values) == (
        "Feishu in-window files differ from the legacy ledger"
    )


def test_window_attestation_counts_only_live_app_files() -> None:
    sent = _media(_IN_WINDOW)
    other = _media(_IN_WINDOW, "other")
    values = _window_inputs(
        [
            _chat_file(sent),
            _chat_file(other, deleted=True),
            _chat_file(other, sender_type="user", sender_id="ou-user"),
        ],
        [sent],
    )
    assert isinstance(attest_window(**values), WindowAttestation)


def test_window_attestation_holds_an_app_file_without_a_post_time() -> None:
    values = _window_inputs([{"file_name": "notes.mp4"}], [])
    assert attest_window(**values) == "Feishu app file name has no post time"


@pytest.mark.parametrize(
    ("mode", "cutoff"), [("full", "2026-09-06T08:00:00"), ("incremental", None)]
)
def test_window_attestation_needs_an_incremental_cutoff(
    mode: str, cutoff: str | None
) -> None:
    values = _window_inputs([], [], mode=mode, cutoff=cutoff)
    assert attest_window(**values) == (
        "window attestation needs an incremental queue cutoff"
    )


def test_window_cutoff_matches_the_weekly_runner_timezone() -> None:
    """A UTC cutoff is compared in the runner's local time, strictly after."""
    at_cutoff = _media("2026-09-06 08-00-00", "at cutoff")
    after = _media("2026-09-06 08-00-01", "after")
    values = _window_inputs(
        [_chat_file(at_cutoff), _chat_file(after)],
        [after],
        cutoff="2026-09-06T00:00:00Z",
    )
    proof = attest_window(**values)
    assert isinstance(proof, WindowAttestation)
    assert (proof.cutoff, proof.window_file_count) == ("2026-09-06T08:00:00", 1)


def test_window_attestation_matches_truncated_upload_names() -> None:
    long_caption = "a caption long enough that Feishu truncates the upload name"
    sent = _media(_IN_WINDOW, long_caption, "_image_1.webp")
    assert legacy_upload_file_name(Path(sent)) != Path(sent).name
    values = _window_inputs([_chat_file(sent)], [sent])
    assert isinstance(attest_window(**values), WindowAttestation)


@pytest.mark.parametrize("mutation", ["stale_source", "other_chat", "sender"])
def test_window_attestation_keeps_the_shared_audit_checks(mutation: str) -> None:
    sent = _media(_IN_WINDOW)
    values = _window_inputs([_chat_file(sent)], [sent])
    page, account = values["journal"].rows[values["report"]["key"]]
    if mutation == "stale_source":
        account["source_sha256"] = "older-snapshot"
    elif mutation == "other_chat":
        values["work_chats"][("weekly0913", "Alpha")].add("oc-other")
    else:
        page["files"].append(
            {**page["files"][0], "message_id": "om-extra", "sender_id": "app-two"}
        )
        account["group_file_count"] = account["app_group_file_count"] = 2
    assert isinstance(attest_window(**values), str)


_LONG_CAPTION = "a caption long enough that Feishu truncates the upload name"


def test_adoption_takes_the_chat_as_the_record_inside_the_window() -> None:
    kept = _media(_IN_WINDOW, "kept")
    exact = _media("2026-09-11 09-00-00", "short", "_image_2.webp")
    shortened = _media("2026-09-12 09-00-00", _LONG_CAPTION, "_image_1.webp")
    disproved = _media("2026-09-13 09-00-00", "gone")
    older = _media(_BEFORE_CUTOFF, "older round")
    values = _window_inputs(
        [
            _chat_file(kept),
            _chat_file(exact),
            _chat_file(shortened),
            _chat_file(shortened),
            _chat_file(older),
        ],
        [kept, disproved],
    )
    plan = plan_feishu_adoption(**values)
    assert isinstance(plan, FeishuAdoption)
    assert (plan.scope, plan.cutoff) == ("window", "2026-09-06T08:00:00")
    post = "2026-09-12 09-00-00_feishu/2026-09-12 09-00-00_feishu_post"
    assert [(item.relative_path, item.precision) for item in plan.adopt] == [
        (exact, "exact"),
        (post, "post"),
    ]
    assert len(plan.adopt[1].message_ids) == 2
    assert plan.demote == ("media-1",)
    assert plan.payload()["adopt"][0] == {
        "relative_path": exact,
        "message_ids": ["om-file-1"],
        "precision": "exact",
    }


def test_adoption_keeps_a_shortened_ledger_name_while_the_chat_has_its_post() -> None:
    first = _media(_IN_WINDOW, _LONG_CAPTION, "_image_1.webp")
    second = _media(_IN_WINDOW, _LONG_CAPTION, "_image_2.webp")
    plan = plan_feishu_adoption(**_window_inputs([_chat_file(first)], [first, second]))
    assert isinstance(plan, FeishuAdoption)
    assert (plan.adopt, plan.demote) == ((), ())


def test_adoption_demotes_a_shortened_post_the_chat_does_not_hold() -> None:
    gone = _media(_IN_WINDOW, _LONG_CAPTION, "_image_1.webp")
    plan = plan_feishu_adoption(**_window_inputs([], [gone]))
    assert isinstance(plan, FeishuAdoption)
    assert plan.demote == ("media-0",)


def test_adoption_covers_the_whole_feed_without_an_incremental_cutoff() -> None:
    older = _media(_BEFORE_CUTOFF, "older", "_image_1.webp")
    values = _window_inputs([_chat_file(older)], [], cutoff=None, mode="full")
    plan = plan_feishu_adoption(**values)
    assert isinstance(plan, FeishuAdoption)
    assert (plan.scope, plan.cutoff) == ("full", None)
    assert [item.relative_path for item in plan.adopt] == [older]


def test_adoption_follows_the_cutoff_delivery_applies_even_in_full_mode() -> None:
    older = _media(_BEFORE_CUTOFF, "older", "_image_1.webp")
    values = _window_inputs([_chat_file(older)], [], mode="full")
    plan = plan_feishu_adoption(**values)
    assert isinstance(plan, FeishuAdoption)
    assert (plan.scope, plan.adopt) == ("window", ())


def test_adoption_holds_an_app_file_without_a_post_time() -> None:
    values = _window_inputs([{"file_name": "no-date.mp4"}], [])
    assert plan_feishu_adoption(**values) == "Feishu app file name has no post time"


def test_adoption_holds_a_shared_chat() -> None:
    values = _window_inputs([], [])
    values["work_chats"] = {("weekly0913", "Alpha"): {"oc-chat", "oc-other"}}
    assert plan_feishu_adoption(**values) == (
        "account has another or unverified historical chat"
    )
