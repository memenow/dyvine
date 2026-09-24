"""Review a frozen legacy queue report and apply only evidenced resolutions.

The input is the JSONL emitted by ``import_legacy_send_progress.py``. Every
row starts blocked. A reviewer may add a ``resolution`` object to selected
rows; historical terminal rows may instead be archived with the explicit
``--archive-historical`` option. No action sends a Feishu message.

Run without ``--apply`` first. Applying requires the printed plan digest and
expected input count, so the reviewed file and options cannot drift between
preview and write. ``DATABASE_URL`` is read from the environment.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import select, update  # noqa: E402

from dyvine.db.models import (  # noqa: E402
    DeliveryFileRow,
    DeliveryGroupRow,
    DeliveryLegacyEvidenceRow,
    DownloadQueueRow,
    SeedAccountRow,
)
from dyvine.db.session import DatabaseSessionFactory  # noqa: E402
from scripts.queue_group_attestation import (  # noqa: E402
    GroupAttestation,
    WindowAttestation,
    attest_group,
    attest_window,
)
from scripts.queue_group_inputs import GroupInputs, load_group_inputs  # noqa: E402
from scripts.queue_reconciliation_policy import (  # noqa: E402
    Decision,
    _assess,
    _count,
    _plan_digest,
    _report_rows,
    _string,
)

_ATTESTED_ACTIONS = frozenset(
    {"release_pending_group_attested", "release_pending_window_attested"}
)


def _action(row: dict[str, Any]) -> Any:
    resolution = row.get("resolution")
    return resolution.get("action") if isinstance(resolution, dict) else None


def _group_inputs(
    args: argparse.Namespace, rows: list[dict[str, Any]]
) -> GroupInputs | None:
    selected = {row["key"] for row in rows if _action(row) in _ATTESTED_ACTIONS}
    if not selected:
        return None
    if not all(
        getattr(args, name, None)
        for name in ("audit_source_report", "feishu_audit", "legacy_work_db")
    ):
        raise ValueError(
            "attested release requires --audit-source-report, "
            "--feishu-audit, and --legacy-work-db"
        )
    return load_group_inputs(
        source_report=Path(args.audit_source_report),
        reviewed_rows=rows,
        selected_keys=selected,
        audit_path=Path(args.feishu_audit),
        work_path=Path(args.legacy_work_db),
        active_round=args.active_round,
        supplemental_audit_path=(
            Path(args.supplemental_feishu_audit)
            if getattr(args, "supplemental_feishu_audit", None)
            else None
        ),
        supplemental_keys_path=(
            Path(args.supplemental_keys_file)
            if getattr(args, "supplemental_keys_file", None)
            else None
        ),
    )


async def _account_history(
    session: Any, sec: str, *, lock: bool
) -> tuple[list[DownloadQueueRow], list[DeliveryFileRow], set[str]]:
    """Every queue row, file ledger row, and seed alias of one account."""
    queue_query = select(DownloadQueueRow).where(DownloadQueueRow.sec_user_id == sec)
    history_query = select(DeliveryFileRow).where(DeliveryFileRow.sec_user_id == sec)
    if lock:
        queue_query = queue_query.with_for_update()
        history_query = history_query.with_for_update()
    history_queues = list((await session.execute(queue_query)).scalars().all())
    history_files = list((await session.execute(history_query)).scalars().all())
    seed_aliases = set(
        (
            await session.execute(
                select(SeedAccountRow.nickname).where(SeedAccountRow.sec_user_id == sec)
            )
        )
        .scalars()
        .all()
    )
    return history_queues, history_files, seed_aliases


async def _inspect_row(
    session: Any,
    row: dict[str, Any],
    active_round: str,
    archive: bool,
    duplicate_legacy_keys: set[str],
    group_inputs: GroupInputs | None,
    *,
    lock: bool,
    timezone: str | None = None,
) -> tuple[Decision, DownloadQueueRow | None]:
    queue = await session.get(DownloadQueueRow, row["key"], with_for_update=lock)
    legacy_key = _string(row.get("legacy_key"))
    if queue is None and legacy_key and legacy_key not in duplicate_legacy_keys:
        queue = await session.get(DownloadQueueRow, legacy_key, with_for_update=lock)
    resolution = row.get("resolution")
    if resolution is None:
        identity_ids: set[str] = set()
        group = None
        files: list[DeliveryFileRow] = []
        evidence: list[DeliveryLegacyEvidenceRow] = []
    else:
        nickname = row.get("nickname")
        queue_ids = (
            (
                await session.execute(
                    select(DownloadQueueRow.sec_user_id).where(
                        DownloadQueueRow.nickname == nickname
                    )
                )
            )
            .scalars()
            .all()
        )
        seed_ids = (
            (
                await session.execute(
                    select(SeedAccountRow.sec_user_id).where(
                        SeedAccountRow.nickname == nickname
                    )
                )
            )
            .scalars()
            .all()
        )
        identity_ids = set(queue_ids) | set(seed_ids)
        group = await session.get(DeliveryGroupRow, row["key"], with_for_update=lock)
        file_query = select(DeliveryFileRow).where(
            DeliveryFileRow.round == row.get("round"),
            DeliveryFileRow.sec_user_id == row.get("sec_user_id"),
        )
        if lock:
            file_query = file_query.with_for_update()
        files = list((await session.execute(file_query)).scalars().all())
        evidence = list(
            (
                await session.execute(
                    select(DeliveryLegacyEvidenceRow).where(
                        DeliveryLegacyEvidenceRow.nickname == nickname
                    )
                )
            )
            .scalars()
            .all()
        )
    action = _action(row)
    group_attestation: GroupAttestation | str | None = None
    window_attestation: WindowAttestation | str | None = None
    if action == "release_pending_window_attested":
        if group_inputs is None or group is None or queue is None:
            window_attestation = (
                "window attestation sources or adopted group are missing"
            )
        elif not timezone:
            window_attestation = "window attestation needs the weekly timezone"
        else:
            sec = row["sec_user_id"]
            history_queues, history_files, seed_aliases = await _account_history(
                session, sec, lock=lock
            )
            # Legacy permanent failures were never sent and the runner skips
            # them; any other non-legacy state is a new send attempt.
            if any(
                item.status != "legacy_confirmed_sent"
                and not (item.status == "permanent_failure" and item.round == "legacy")
                for item in history_files
            ):
                window_attestation = "account has new send attempts in the file ledger"
            else:
                journal, keys_sha256 = group_inputs.journal_for(row["key"])
                window_attestation = attest_window(
                    report=row,
                    original=group_inputs.source_rows[row["key"]],
                    all_report_rows=group_inputs.all_rows,
                    group=group,
                    queue=queue,
                    current_queues=[
                        item for item in history_queues if item.round == row["round"]
                    ],
                    current_files=files,
                    historical_queues=history_queues,
                    historical_files=[
                        item
                        for item in history_files
                        if item.status == "legacy_confirmed_sent"
                    ],
                    work_chats=group_inputs.work_chats,
                    journal=journal,
                    timezone=timezone,
                    known_aliases=seed_aliases,
                    keys_file_sha256=keys_sha256,
                )
    if action == "release_pending_group_attested":
        if group_inputs is None or group is None:
            group_attestation = "group attestation sources or adopted group are missing"
        else:
            sec = row["sec_user_id"]
            history_queues, history_files, seed_aliases = await _account_history(
                session, sec, lock=lock
            )
            current_queues = [
                item for item in history_queues if item.round == row["round"]
            ]
            legacy_files = [
                item for item in history_files if item.status == "legacy_confirmed_sent"
            ]
            if len(legacy_files) != len(history_files):
                group_attestation = "account has non-legacy file ledger states"
            else:
                journal, keys_sha256 = group_inputs.journal_for(row["key"])
                group_attestation = attest_group(
                    report=row,
                    original=group_inputs.source_rows[row["key"]],
                    all_report_rows=group_inputs.all_rows,
                    group=group,
                    current_queues=current_queues,
                    current_files=files,
                    historical_queues=history_queues,
                    historical_files=legacy_files,
                    work_chats=group_inputs.work_chats,
                    journal=journal,
                    known_aliases=seed_aliases,
                    keys_file_sha256=keys_sha256,
                )
    decision = _assess(
        row,
        queue,
        active_round=active_round,
        archive_historical=archive,
        identity_ids=identity_ids,
        group=group,
        files=files,
        evidence=evidence,
        group_attestation=group_attestation,
        window_attestation=window_attestation,
    )
    return decision, queue


async def run(args: argparse.Namespace) -> dict[str, Any]:
    """Inspect every row; write only when the exact preview is authorized."""
    rows, source_digest = _report_rows(Path(args.report))
    group_inputs = _group_inputs(args, rows)
    legacy_counts = Counter(row.get("legacy_key", row["key"]) for row in rows)
    duplicate_legacy_keys = {key for key, count in legacy_counts.items() if count > 1}
    if not any(row.get("round") == args.active_round for row in rows):
        raise ValueError("active round is absent from the frozen report")
    evidence_digests = group_inputs.evidence_digests if group_inputs else ()
    timezone = getattr(args, "timezone", None)
    if any(_action(row) == "release_pending_window_attested" for row in rows):
        if not timezone:
            raise ValueError("window attestation requires --timezone")
        # The timezone decides every window cutoff, so bind it to the plan.
        evidence_digests += (
            hashlib.sha256(f"timezone\0{timezone}".encode()).hexdigest(),
        )
    plan_digest = _plan_digest(
        source_digest, args.active_round, args.archive_historical, evidence_digests
    )
    if args.apply and (
        args.expect_plan_sha256 != plan_digest or args.expected_count != len(rows)
    ):
        raise ValueError("apply requires the exact preview digest and row count")
    url = os.environ.get(args.database_url_env)
    if not url:
        raise ValueError(f"{args.database_url_env} is not set")
    # The report can contain thousands of rows; reuse one checked connection
    # during this finite operation instead of reconnecting for every row.
    factory = DatabaseSessionFactory(
        url, pool_class="queue", pool_size=1, max_overflow=0
    )
    results: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    try:
        for row in rows:
            async with factory.session() as session:
                async with session.begin():
                    decision, queue = await _inspect_row(
                        session,
                        row,
                        args.active_round,
                        args.archive_historical,
                        duplicate_legacy_keys,
                        group_inputs,
                        lock=args.apply,
                        timezone=timezone,
                    )
                    outcome = "held"
                    if decision.status is not None:
                        if args.apply:
                            assert queue is not None
                            stamp = datetime.now(UTC).isoformat()
                            extra = dict(queue.extra)
                            extra.pop("migration_needs_reconciliation", None)
                            extra["reconciliation"] = {
                                "action": decision.action,
                                "plan_sha256": plan_digest,
                                "applied_at": stamp,
                                "feishu_history_reference": (
                                    (row.get("resolution") or {}).get(
                                        "feishu_history_reference"
                                    )
                                ),
                            }
                            if decision.action in _ATTESTED_ACTIONS:
                                assert group_inputs is not None
                                extra["reconciliation"].update(
                                    main_audit_sha256=group_inputs.journal.sha256,
                                    supplemental_audit_sha256=(
                                        group_inputs.supplemental_journal.sha256
                                        if group_inputs.supplemental_journal
                                        else None
                                    ),
                                    supplemental_keys_sha256=(
                                        group_inputs.keys_file_sha256
                                    ),
                                    audit_source_sha256=group_inputs.source_sha256,
                                    work_sha256=group_inputs.work_sha256,
                                )
                            if decision.action == "release_pending_group_attested":
                                assert decision.group_attestation is not None
                                extra["reconciliation"].update(
                                    audit_sha256=(
                                        decision.group_attestation.audit_sha256
                                    ),
                                    target_source_sha256=(
                                        decision.group_attestation.target_source_sha256
                                    ),
                                    historical_file_count=(
                                        decision.group_attestation.historical_file_count
                                    ),
                                    legacy_user_failed_unknown=(
                                        decision.group_attestation.legacy_user_failed_unknown
                                    ),
                                    proof="group_file_name_multiset",
                                )
                            if decision.action == "release_pending_window_attested":
                                assert decision.window_attestation is not None
                                extra["reconciliation"].update(
                                    audit_sha256=(
                                        decision.window_attestation.audit_sha256
                                    ),
                                    target_source_sha256=(
                                        decision.window_attestation.target_source_sha256
                                    ),
                                    window_cutoff=decision.window_attestation.cutoff,
                                    window_file_count=(
                                        decision.window_attestation.window_file_count
                                    ),
                                    timezone=timezone,
                                    proof="feishu_window_file_name_multiset",
                                )
                            if decision.action in _ATTESTED_ACTIONS:
                                # Local media is gone; force a fresh download.
                                weekly = extra.get("weekly")
                                weekly = (
                                    dict(weekly) if isinstance(weekly, dict) else {}
                                )
                                weekly["fresh_download_confirmed"] = False
                                extra["weekly"] = weekly
                            if decision.action == "archive_historical":
                                extra["legacy_unverified_delivery"] = True
                            statement = (
                                update(DownloadQueueRow)
                                .where(
                                    DownloadQueueRow.key == queue.key,
                                    DownloadQueueRow.status == "needs_reconciliation",
                                    DownloadQueueRow.round == row["round"],
                                    DownloadQueueRow.sec_user_id == row["sec_user_id"],
                                    DownloadQueueRow.nickname == row["nickname"],
                                    DownloadQueueRow.extra == queue.extra,
                                )
                                .values(
                                    status=decision.status,
                                    extra=extra,
                                    updated_at=stamp,
                                )
                            )
                            connection = await session.connection()
                            changed = (await connection.execute(statement)).rowcount
                            if changed != 1:
                                raise ValueError(
                                    f"queue compare-and-set failed for {queue.key}"
                                )
                            for file, message_id in decision.receipts:
                                receipt_update = (
                                    update(DeliveryFileRow)
                                    .where(
                                        DeliveryFileRow.media_id == file.media_id,
                                        DeliveryFileRow.round == row["round"],
                                        DeliveryFileRow.sec_user_id
                                        == row["sec_user_id"],
                                        DeliveryFileRow.relative_path
                                        == file.relative_path,
                                        DeliveryFileRow.status
                                        == "legacy_confirmed_sent",
                                        DeliveryFileRow.chat_id == file.chat_id,
                                        DeliveryFileRow.parent_id == file.parent_id,
                                        DeliveryFileRow.message_id == file.message_id,
                                        DeliveryFileRow.send_uuid.is_(None),
                                    )
                                    .values(
                                        chat_id=queue.chat_id,
                                        parent_id=row["resolution"]["topic_message_id"],
                                        message_id=message_id,
                                        updated_at=stamp,
                                    )
                                )
                                receipt_changed = (
                                    await connection.execute(receipt_update)
                                ).rowcount
                                if receipt_changed != 1:
                                    raise ValueError(
                                        "file receipt compare-and-set failed for "
                                        f"{file.media_id}"
                                    )
                            outcome = "applied"
                        else:
                            outcome = "eligible"
                    elif queue is not None and queue.status != "needs_reconciliation":
                        previous = (
                            queue.extra.get("reconciliation")
                            if isinstance(queue.extra, dict)
                            else None
                        )
                        if (
                            isinstance(previous, dict)
                            and previous.get("plan_sha256") == plan_digest
                        ):
                            outcome = "already_applied"
                            decision = Decision(
                                queue.status,
                                str(previous.get("action")),
                                "the exact decision was already applied",
                            )
                    counts[outcome] += 1
                    results.append(
                        {
                            "key": row["key"],
                            "outcome": outcome,
                            "action": decision.action,
                            "target_status": decision.status or "",
                            "reason": decision.reason,
                            "cache_only_unverified_paths": _count(
                                row, "cache_only_unverified_paths"
                            ),
                            "permanent_failure_paths": _count(
                                row, "permanent_failure_paths"
                            ),
                            "permanent_unresolved_paths": _count(
                                row, "permanent_unresolved_paths"
                            ),
                        }
                    )
    finally:
        await factory.aclose()
    return {
        "mode": "apply" if args.apply else "dry_run",
        "source_sha256": source_digest,
        "audit_source_sha256": group_inputs.source_sha256 if group_inputs else None,
        "feishu_audit_sha256": group_inputs.journal.sha256 if group_inputs else None,
        "supplemental_audit_sha256": (
            group_inputs.supplemental_journal.sha256
            if group_inputs and group_inputs.supplemental_journal
            else None
        ),
        "supplemental_keys_sha256": (
            group_inputs.keys_file_sha256 if group_inputs else None
        ),
        "legacy_work_sha256": group_inputs.work_sha256 if group_inputs else None,
        "plan_sha256": plan_digest,
        "expected_count": len(rows),
        "counts": dict(counts),
        "held_reasons": dict(
            Counter(item["reason"] for item in results if item["outcome"] == "held")
        ),
        "archived_unverified": [
            item["key"]
            for item in results
            if item["action"] == "archive_historical"
            and item["outcome"] in {"eligible", "applied", "already_applied"}
        ],
        "rows": results,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, help="frozen JSONL report")
    parser.add_argument("--active-round", required=True)
    parser.add_argument("--archive-historical", action="store_true")
    parser.add_argument(
        "--audit-source-report", help="unchanged report used by Feishu audit"
    )
    parser.add_argument("--feishu-audit", help="complete private Feishu audit JSONL")
    parser.add_argument(
        "--supplemental-feishu-audit", help="private keys-file Feishu audit JSONL"
    )
    parser.add_argument(
        "--supplemental-keys-file", help="exact owned 0600 supplemental key list"
    )
    parser.add_argument(
        "--legacy-work-db", help="staged legacy progress SQLite database"
    )
    parser.add_argument(
        "--timezone",
        help="weekly runner timezone (DYVINE_WEEKLY_TIMEZONE) for window cutoffs",
    )
    parser.add_argument("--database-url-env", default="DATABASE_URL")
    parser.add_argument("--output", help="write the machine-readable result as JSON")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expect-plan-sha256")
    parser.add_argument("--expected-count", type=int)
    args = parser.parse_args(argv)
    if args.apply and (not args.expect_plan_sha256 or args.expected_count is None):
        parser.error("--apply requires --expect-plan-sha256 and --expected-count")
    if args.timezone:
        try:
            ZoneInfo(args.timezone)
        except (ValueError, ZoneInfoNotFoundError):
            parser.error("--timezone must be an IANA timezone name")
    if bool(args.supplemental_feishu_audit) != bool(args.supplemental_keys_file):
        parser.error("supplemental audit and keys file must be supplied together")
    if args.output and Path(args.output).resolve() in {
        Path(value).resolve()
        for value in (
            args.report,
            args.audit_source_report,
            args.feishu_audit,
            args.supplemental_feishu_audit,
            args.supplemental_keys_file,
            args.legacy_work_db,
        )
        if value
    }:
        parser.error("--output cannot replace an input file")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = asyncio.run(run(args))
    except (OSError, ValueError) as error:
        print(f"error: queue reconciliation failed: {error}", file=sys.stderr)
        return 2
    except Exception as error:
        print(
            f"error: queue reconciliation database failure: {type(error).__name__}",
            file=sys.stderr,
        )
        return 2
    payload = json.dumps(result, ensure_ascii=False, sort_keys=True)
    if args.output:
        path = Path(args.output)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, delete=False
            ) as handle:
                temporary = Path(handle.name)
                os.chmod(temporary, 0o600)
                handle.write(payload + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    print(
        json.dumps(
            {
                key: value
                for key, value in result.items()
                if key not in {"rows", "archived_unverified"}
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
