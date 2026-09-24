"""Pure validation policy for frozen legacy queue decisions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from dyvine.db.models import (
    DeliveryFileRow,
    DeliveryGroupRow,
    DeliveryLegacyEvidenceRow,
    DownloadQueueRow,
)
from scripts.queue_group_attestation import GroupAttestation, WindowAttestation

# Legacy queue statuses whose remaining media may be released for sending.
_RELEASABLE_OLD_STATUSES = frozenset(
    {"op_done", "pending", "downloading", "op_issue", "send_issue"}
)


@dataclass(frozen=True)
class Decision:
    """A status transition plus already verified file receipts to adopt."""

    status: str | None
    action: str
    reason: str
    receipts: tuple[tuple[DeliveryFileRow, str], ...] = ()
    group_attestation: GroupAttestation | None = None
    window_attestation: WindowAttestation | None = None


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _count(row: dict[str, Any], field: str) -> int | None:
    value = row.get(field)
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else None
    )


def _report_rows(path: Path) -> tuple[list[dict[str, Any]], str]:
    payload = path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for position, line in enumerate(payload.splitlines(), start=1):
        try:
            row = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(f"invalid JSONL at line {position}") from error
        if not isinstance(row, dict):
            raise ValueError(f"non-object JSONL row at line {position}")
        key = _string(row.get("key"))
        if key is None or key in seen:
            raise ValueError(f"missing or duplicate queue key at line {position}")
        if row.get("send_blocked") is not True:
            raise ValueError(f"queue row {key} lacks its original send block")
        seen.add(key)
        rows.append(row)
    if not rows:
        raise ValueError("reconciliation report is empty")
    return rows, digest


def _plan_digest(
    source_digest: str,
    active_round: str,
    archive: bool,
    evidence_digests: tuple[str, ...] = (),
) -> str:
    payload = f"{source_digest}\0{active_round}\0{int(archive)}"
    if evidence_digests:
        payload += "\0" + "\0".join(evidence_digests)
    return hashlib.sha256(payload.encode()).hexdigest()


def _relative_path(value: Any) -> str | None:
    path = PurePosixPath(value) if isinstance(value, str) else None
    if path is None or path.is_absolute() or ".." in path.parts or value in {"", "."}:
        return None
    return path.as_posix()


def _receipts(
    resolution: dict[str, Any], files: list[DeliveryFileRow], row: dict[str, Any]
) -> tuple[tuple[DeliveryFileRow, str], ...] | str:
    submitted = resolution.get("verified_sent")
    if not isinstance(submitted, list):
        return "verified_sent must list every historical send"
    safe = _count(row, "first_seen_safe_sent_paths")
    if safe is None or len(submitted) != safe or len(files) != safe:
        return "verified receipts and unique imported paths do not match"
    by_path = {item.relative_path: item for item in files}
    if len(by_path) != len(files) or set(by_path) != {
        item.relative_path for item in files if item.status == "legacy_confirmed_sent"
    }:
        return "file ledger has non-legacy or duplicate entries for this round"
    result: list[tuple[DeliveryFileRow, str]] = []
    seen_paths: set[str] = set()
    seen_messages: set[str] = set()
    for item in submitted:
        if not isinstance(item, dict):
            return "verified_sent contains a non-object receipt"
        path = _relative_path(item.get("relative_path"))
        message_id = _string(item.get("message_id"))
        if (
            path is None
            or message_id is None
            or path in seen_paths
            or message_id in seen_messages
        ):
            return "verified_sent has an invalid or duplicate path/message"
        ledger = by_path.get(path)
        if ledger is None or ledger.status != "legacy_confirmed_sent":
            return "verified_sent path is absent from imported legacy ledger"
        if ledger.send_uuid is not None or ledger.send_started_at is not None:
            return "file ledger already has a new send attempt"
        if ledger.message_id not in (None, message_id):
            return "file ledger has a different message ID"
        seen_paths.add(path)
        seen_messages.add(message_id)
        result.append((ledger, message_id))
    if seen_paths != set(by_path):
        return "verified_sent does not cover the imported round ledger"
    return tuple(result)


def _user_ordered_skip(
    round_name: str | None,
    active_round: str,
    old_status: str | None,
    files: list[DeliveryFileRow],
) -> Decision:
    """Close an active-round row the legacy queue skipped on the user's order."""
    if round_name != active_round:
        return Decision(
            None, "held", "user-ordered skips apply only to the active round"
        )
    if old_status != "skipped_404":
        return Decision(None, "held", "legacy queue did not record a user-ordered skip")
    if any(
        item.status not in {"legacy_confirmed_sent", "permanent_failure"}
        or item.send_uuid is not None
        for item in files
    ):
        return Decision(
            None, "held", "account already has new send attempts in this round"
        )
    return Decision(
        "skipped",
        "skip_user_ordered",
        "legacy queue recorded a user-ordered skip for this round",
    )


_UNAVAILABLE_AUTHOR_REASONS = frozenset({"deactivated", "banned", "no_posts"})


def _author_unavailable_skip(
    resolution: dict[str, Any],
    round_name: str | None,
    active_round: str,
    files: list[DeliveryFileRow],
) -> Decision:
    """Close an active-round row whose author Douyin reports gone for good."""
    if round_name != active_round:
        return Decision(None, "held", "author skips apply only to the active round")
    author = resolution.get("author")
    if (
        not isinstance(author, dict)
        or author.get("reason") not in _UNAVAILABLE_AUTHOR_REASONS
        or not isinstance(author.get("checked_at"), str)
    ):
        return Decision(None, "held", "author unavailability evidence is incomplete")
    if any(
        item.status not in {"legacy_confirmed_sent", "permanent_failure"}
        or item.send_uuid is not None
        for item in files
    ):
        return Decision(
            None, "held", "account already has new send attempts in this round"
        )
    return Decision(
        "skipped",
        "skip_author_unavailable",
        f"Douyin reports the author {author['reason']}",
    )


def _adoption_issue(
    group: DeliveryGroupRow | None,
    key: str | None,
    queue: DownloadQueueRow,
    resolution: dict[str, Any],
) -> str | None:
    if (
        group is None
        or group.key != key
        or group.status != "ready"
        or group.topic_status != "ready"
        or not group.topic_message_id
        or not queue.chat_id
        or group.chat_id != queue.chat_id
        or resolution.get("chat_id") != group.chat_id
        or resolution.get("topic_message_id") != group.topic_message_id
    ):
        return "group and topic have not been adopted exactly"
    return None


def _window_release(
    resolution: dict[str, Any],
    *,
    key: str | None,
    queue: DownloadQueueRow,
    group: DeliveryGroupRow | None,
    round_name: str | None,
    active_round: str,
    old_status: str | None,
    window_attestation: WindowAttestation | str | None,
) -> Decision:
    """Release an active-round row whose download window Feishu confirms."""
    if round_name != active_round or old_status not in _RELEASABLE_OLD_STATUSES:
        return Decision(None, "held", "window attestation cannot release this row")
    issue = _adoption_issue(group, key, queue, resolution)
    if issue:
        return Decision(None, "held", issue)
    if not isinstance(window_attestation, WindowAttestation):
        return Decision(
            None,
            "held",
            (
                window_attestation
                if isinstance(window_attestation, str)
                else "complete window attestation is missing"
            ),
        )
    return Decision(
        "pending",
        "release_pending_window_attested",
        "Feishu in-window files match the legacy ledger",
        window_attestation=window_attestation,
    )


def _assess(
    report: dict[str, Any],
    queue: DownloadQueueRow | None,
    *,
    active_round: str,
    archive_historical: bool,
    identity_ids: set[str],
    group: DeliveryGroupRow | None,
    files: list[DeliveryFileRow],
    evidence: list[DeliveryLegacyEvidenceRow],
    group_attestation: GroupAttestation | str | None = None,
    window_attestation: WindowAttestation | str | None = None,
) -> Decision:
    key = _string(report.get("key"))
    round_name = _string(report.get("round"))
    sec = _string(report.get("sec_user_id"))
    nickname = _string(report.get("nickname"))
    old_status = _string(report.get("legacy_status"))
    legacy_key = _string(report.get("legacy_key"))
    if (
        not all((key, round_name, sec, nickname, old_status))
        or key != f"{round_name}:{sec}"
    ):
        return Decision(None, "held", "report identity is incomplete")
    if queue is None:
        return Decision(None, "held", "queue row is missing")
    if (
        queue.key not in {key, legacy_key}
        or not isinstance(queue.extra, dict)
        or (
            queue.key == key
            and legacy_key not in (None, key)
            and queue.extra.get("legacy_key") != legacy_key
        )
        or queue.round != round_name
        or queue.sec_user_id != sec
        or queue.nickname != nickname
        or queue.status != "needs_reconciliation"
        or queue.extra.get("migration_needs_reconciliation") is not True
        or queue.extra.get("legacy_queue_status") != old_status
    ):
        return Decision(None, "held", "frozen queue identity or old status changed")
    resolution = report.get("resolution")
    if isinstance(resolution, dict) and resolution.get("action") == "skip_user_ordered":
        # A skip sends nothing, so send-evidence gaps below cannot make it unsafe.
        return _user_ordered_skip(round_name, active_round, old_status, files)
    if (
        isinstance(resolution, dict)
        and resolution.get("action") == "skip_author_unavailable"
    ):
        return _author_unavailable_skip(resolution, round_name, active_round, files)
    if (
        isinstance(resolution, dict)
        and resolution.get("action") == "release_pending_window_attested"
    ):
        # The audited chat shows which in-window sends arrived, which settles
        # the ambiguous, cache-only, and failed evidence held below.
        return _window_release(
            resolution,
            key=key,
            queue=queue,
            group=group,
            round_name=round_name,
            active_round=active_round,
            old_status=old_status,
            window_attestation=window_attestation,
        )
    cache_only = _count(report, "cache_only_unverified_paths")
    if cache_only != 0:
        return Decision(None, "held", "cache-only paths have no verified send outcome")
    unresolved_permanent = _count(report, "permanent_unresolved_paths")
    if unresolved_permanent != 0:
        return Decision(None, "held", "unresolved permanent failure evidence remains")
    permanent = _count(report, "permanent_failure_paths")
    if permanent != 0:
        return Decision(
            None,
            "held",
            "legacy permanent failures are terminal; exclude them before release",
        )
    if resolution is None:
        if (
            archive_historical
            and round_name != active_round
            and old_status in {"op_done", "skipped_404"}
            and all(
                _count(report, field) == 0
                for field in (
                    "legacy_zero_zero_snapshots",
                    "ambiguous_sent_paths",
                    "failed_paths",
                    "snapshot_failed_entries",
                    "legacy_user_failed_max",
                )
            )
        ):
            status = "completed" if old_status == "op_done" else "skipped"
            return Decision(
                status, "archive_historical", "historical delivery remains unverified"
            )
        return Decision(None, "held", "no reviewed resolution")
    if not isinstance(resolution, dict):
        return Decision(None, "held", "resolution is not an object")
    action = resolution.get("action")
    if action not in {
        "complete",
        "release_pending",
        "release_pending_group_attested",
    }:
        return Decision(None, "held", "unsupported reviewed action")
    issue = _adoption_issue(group, key, queue, resolution)
    if issue:
        return Decision(None, "held", issue)
    assert group is not None
    if action == "release_pending_group_attested":
        if (
            round_name != active_round
            or old_status not in _RELEASABLE_OLD_STATUSES
            or _count(report, "ambiguous_sent_paths") != 0
            or _count(report, "failed_paths") != 0
            or _count(report, "snapshot_failed_entries") != 0
            or "legacy_user_failed_max" not in report
            or (
                report["legacy_user_failed_max"] is not None
                and _count(report, "legacy_user_failed_max") != 0
            )
        ):
            return Decision(None, "held", "group attestation cannot release this row")
        if not isinstance(group_attestation, GroupAttestation):
            return Decision(
                None,
                "held",
                (
                    group_attestation
                    if isinstance(group_attestation, str)
                    else "complete group attestation is missing"
                ),
            )
        return Decision(
            "pending",
            action,
            "full group file-name multiset matches imported historical paths",
            group_attestation=group_attestation,
        )
    if _count(report, "legacy_zero_zero_snapshots") != 0:
        return Decision(None, "held", "zero-of-zero progress needs individual review")
    if identity_ids != {sec}:
        return Decision(None, "held", "nickname does not map to one account ID")
    reference = _string(resolution.get("feishu_history_reference"))
    if reference is None:
        return Decision(None, "held", "missing Feishu history verification reference")
    if _count(report, "ambiguous_sent_paths") != 0:
        return Decision(None, "held", "ambiguous historical sends remain")
    receipts = _receipts(resolution, files, report)
    if isinstance(receipts, str):
        return Decision(None, "held", receipts)
    if any(
        item.chat_id not in (None, group.chat_id)
        or item.parent_id not in (None, group.topic_message_id)
        for item, _ in receipts
    ):
        return Decision(
            None, "held", "file ledger destination conflicts with group/topic"
        )
    audit = [
        item
        for item in evidence
        if item.nickname == nickname
        and Path(item.source_file).name.startswith(f"send_progress_{round_name}_")
    ]
    if any(item.legacy_state != "failed" for item in audit):
        return Decision(None, "held", "unresolved ambiguous send evidence remains")
    failed_paths = {item.legacy_path for item in audit}
    if len(failed_paths) != _count(report, "failed_paths"):
        return Decision(
            None, "held", "failed path count differs from imported evidence"
        )
    if action == "complete":
        candidate = (
            report.get("classification") == "terminal_candidate"
            and report.get("terminal_candidate") is True
        )
        total = _count(report, "candidate_total_files")
        if (
            old_status != "op_done"
            or not candidate
            or total is None
            or total == 0
            or len(receipts) < total
            or report.get("candidate_chat_id") != queue.chat_id
            or report.get("adoptable_group_chat_id") != group.chat_id
            or report.get("adoptable_topic_message_id") != group.topic_message_id
            or not _string(report.get("candidate_source_file"))
            or not _string(report.get("group_topic_source_file"))
            or failed_paths
            or _count(report, "snapshot_failed_entries") != 0
            or _count(report, "legacy_user_failed_max") != 0
        ):
            return Decision(
                None, "held", "terminal completion lacks exact zero-failure evidence"
            )
        return Decision(
            "completed", action, "every historical send was verified", receipts
        )
    confirmed = resolution.get("confirmed_unsent_legacy_paths")
    snapshot_failed = _count(report, "snapshot_failed_entries")
    user_failed = _count(report, "legacy_user_failed_max")
    if (
        round_name != active_round
        or old_status == "skipped_404"
        or snapshot_failed is None
        or snapshot_failed > len(failed_paths)
        or user_failed is None
        or user_failed > len(failed_paths)
        or resolution.get("confirmed_no_other_sends") is not True
        or not isinstance(confirmed, list)
        or any(not _string(path) for path in confirmed)
        or set(confirmed) != failed_paths
        or len(confirmed) != len(failed_paths)
    ):
        return Decision(
            None, "held", "unsent scope or failed paths are not explicitly verified"
        )
    return Decision(
        "pending", action, "remaining media explicitly confirmed unsent", receipts
    )
