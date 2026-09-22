"""One-shot migration: hermes local state -> Postgres.

Moves every legacy file-backed store into the ``0002`` tables:

- ``download_queue.json`` entries -> ``download_queue`` (+ round headers)
- ``send_status.db:send_status`` -> ``send_status`` (batch coerced to TEXT)
- ``send_status.db:user_send_status`` -> ``user_send_status`` (read-only copy)
- ``seed_users.json`` -> ``seed_accounts``
- ``excluded_accounts.json`` -> ``seed_accounts.excluded`` flags by nickname
- ``weekly_align_ops_*.json`` -> ``delivery_rounds`` + missing queue entries
- ``douyin_users.db:user_info_web`` -> ``user_profiles``
- ``operations.db:operations`` -> ``operations`` (frozen 2026-09-06 history)

Safe to re-run: every INSERT uses ``ON CONFLICT DO NOTHING`` on the
natural key, so a second run only re-validates the source. Queue keys
from weekly files never overwrite existing rows.

Usage:
    PYTHONPATH=src uv run python scripts/migrate_hermes_state_to_pg.py \\
        --state-dir /opt/dyvine/data/douyin/state \\
        --seed-path /opt/dyvine/seed_users.json \\
        --users-db /opt/dyvine/douyin_users.db \\
        --database-url postgresql+asyncpg://... \\
        [--dry-run] [--batch-size 500]

Exit codes: 0 on success (including dry-run with zero invalid rows),
1 when any source row fails validation, 2 on connection/config errors.
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402

from dyvine.db.models import (  # noqa: E402
    DeliveryRoundRow,
    DownloadQueueRow,
    OperationRow,
    SeedAccountRow,
    SendStatusRow,
    UserProfileRow,
    UserSendStatusRow,
)
from dyvine.db.session import DatabaseSessionFactory  # noqa: E402

#: Queue entry keys promoted to real columns; the rest lands in ``extra``.
_QUEUE_HOT_KEYS = frozenset(
    {
        "key",
        "round",
        "kind",
        "nickname",
        "sec_user_id",
        "chat_id",
        "homepage",
        "mode",
        "cutoff",
        "status",
        "operation_id",
        "op_id",
        "op_status",
        "op_message",
        "attempts",
        "serial_group",
        "updated_at",
        "created_at",
        "added_at",
    }
)

_PROFILE_COLUMNS = frozenset(
    {
        "sec_user_id",
        "avatar_url",
        "aweme_count",
        "city",
        "country",
        "favoriting_count",
        "follower_count",
        "following_count",
        "gender",
        "ip_location",
        "is_ban",
        "is_block",
        "is_blocked",
        "is_star",
        "live_status",
        "mix_count",
        "mplatform_followers_count",
        "nickname",
        "nickname_raw",
        "room_id",
        "school_name",
        "short_id",
        "signature",
        "signature_raw",
        "total_favorited",
        "uid",
        "unique_id",
        "user_age",
        "last_aweme_id",
    }
)


@dataclass
class RowIssue:
    """One source row that failed validation."""

    source: str
    rowid: Any
    reason: str


@dataclass
class SourceReport:
    """Counts for a single source."""

    scanned: int = 0
    valid: int = 0
    imported: int = 0
    skipped_existing: int = 0


@dataclass
class MigrationReport:
    """Per-source counts plus issues collected during a run."""

    sources: dict[str, SourceReport] = field(default_factory=dict)
    issues: list[RowIssue] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def for_source(self, name: str) -> SourceReport:
        """Return (creating) the counter for ``name``."""
        return self.sources.setdefault(name, SourceReport())

    @property
    def invalid(self) -> int:
        """Number of source rows that failed validation."""
        return len(self.issues)


def _queue_payload(entry: dict[str, Any]) -> dict[str, Any] | str:
    """Validate one queue entry; return the PG payload or a reason."""
    key = entry.get("key")
    round_name = entry.get("round")
    nickname = entry.get("nickname")
    sec = entry.get("sec_user_id")
    mode = entry.get("mode")
    status = entry.get("status")
    for name, value in (
        ("key", key),
        ("round", round_name),
        ("nickname", nickname),
        ("sec_user_id", sec),
        ("mode", mode),
        ("status", status),
    ):
        if not value or not isinstance(value, str):
            return f"missing or non-string {name}"
    attempts = entry.get("attempts", 0)
    if not isinstance(attempts, int) or isinstance(attempts, bool):
        return f"non-integer attempts: {attempts!r}"
    updated = entry.get("updated_at") or entry.get("created_at") or ""
    if not isinstance(updated, str):
        return f"non-string updated_at: {updated!r}"
    created = entry.get("created_at") or entry.get("added_at") or updated
    operation_id = entry.get("operation_id") or entry.get("op_id")
    extra = {
        name: value for name, value in entry.items() if name not in _QUEUE_HOT_KEYS
    }
    return {
        "key": key,
        "round": round_name,
        "kind": entry.get("kind"),
        "nickname": nickname,
        "sec_user_id": sec,
        "chat_id": entry.get("chat_id"),
        "homepage": entry.get("homepage"),
        "mode": mode,
        "cutoff": entry.get("cutoff"),
        "status": status,
        "operation_id": operation_id,
        "op_status": entry.get("op_status"),
        "op_message": entry.get("op_message"),
        "attempts": attempts,
        "serial_group": entry.get("serial_group"),
        "owner_id": None,
        "heartbeat_at": None,
        "extra": extra,
        "created_at": created,
        "updated_at": updated or created,
    }


def _send_payload(row: dict[str, Any]) -> dict[str, Any] | str:
    """Validate one ``send_status`` row; return the PG payload or a reason."""
    nickname = row.get("nickname")
    if not nickname or not isinstance(nickname, str):
        return "missing or non-string nickname"
    batch = row.get("batch")
    if batch is not None and not isinstance(batch, str):
        batch = str(batch)
    updated = row.get("updated_at") or ""
    if not isinstance(updated, str):
        return f"non-string updated_at: {updated!r}"
    return {
        "nickname": nickname,
        "sec_user_id": row.get("sec_user_id"),
        "chat_id": row.get("chat_id"),
        "batch": batch,
        "total_files": row.get("total_files"),
        "sent_files": row.get("sent_files"),
        "failed_files": row.get("failed_files"),
        "status": row.get("status"),
        "created_at": updated,
        "updated_at": updated,
    }


def _user_send_payload(row: dict[str, Any]) -> dict[str, Any] | str:
    """Validate one legacy ``user_send_status`` row."""
    username = row.get("username")
    if not username or not isinstance(username, str):
        return "missing or non-string username"
    updated = row.get("updated_at") or ""
    if not isinstance(updated, str):
        return f"non-string updated_at: {updated!r}"
    return {
        "username": username,
        "local_files": row.get("local_files") or 0,
        "sent_files": row.get("sent_files") or 0,
        "failed_files": row.get("failed_files") or 0,
        "status": row.get("status") or "pending",
        "failed_details": row.get("failed_details") or "",
        "created_at": updated,
        "updated_at": updated,
    }


def _migration_stamp() -> str:
    """Return the migration run time as an ISO-8601 UTC string."""
    return datetime.now(UTC).isoformat()


def _seed_payload(item: dict[str, Any], stamp: str) -> dict[str, Any] | str:
    """Validate one seed entry; return the PG payload or a reason."""
    sec = item.get("sec_user_id")
    if not sec or not isinstance(sec, str):
        return "missing or non-string sec_user_id"
    return {
        "sec_user_id": sec,
        "nickname": item.get("nickname"),
        "source_url": item.get("source_url"),
        "source": "seed",
        "batch": None,
        "excluded": False,
        "created_at": stamp,
        "updated_at": stamp,
    }


def _profile_payload(row: dict[str, Any], stamp: str) -> dict[str, Any] | str:
    """Validate one ``user_info_web`` row; return the PG payload or a reason."""
    sec = row.get("sec_user_id")
    if not sec or not isinstance(sec, str):
        return "missing or non-string sec_user_id"
    payload = {
        name: row.get(name) for name in _PROFILE_COLUMNS if name != "sec_user_id"
    }
    payload["sec_user_id"] = sec
    payload["created_at"] = stamp
    payload["updated_at"] = stamp
    return payload


def _operation_payload(row: dict[str, Any]) -> dict[str, Any] | str:
    """Validate one frozen ``operations`` row; metadata TEXT becomes JSONB."""
    operation_id = row.get("operation_id")
    if not operation_id or not isinstance(operation_id, str):
        return "missing or non-string operation_id"
    for name in ("operation_type", "subject_id", "status", "message"):
        value = row.get(name)
        if not value or not isinstance(value, str):
            return f"missing or non-string {name}"
    raw_metadata = row.get("metadata") or "{}"
    try:
        metadata = (
            json.loads(raw_metadata)
            if isinstance(raw_metadata, str)
            else dict(raw_metadata)
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    return {
        "operation_id": operation_id,
        "operation_type": row.get("operation_type"),
        "subject_id": row.get("subject_id"),
        "status": row.get("status"),
        "message": row.get("message"),
        "progress": row.get("progress"),
        "total_items": row.get("total_items"),
        "completed_items": row.get("completed_items"),
        "download_path": row.get("download_path"),
        "error": row.get("error"),
        # Attribute name: ``metadata`` on the class is the MetaData object.
        "metadata_": metadata,
        "owner_id": None,
        "heartbeat_at": None,
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _sqlite_rows(path: Path, table: str) -> list[dict[str, Any]]:
    """Read a whole SQLite table into dicts (read-only URI)."""
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(f"SELECT * FROM [{table}]")]
    finally:
        connection.close()


async def _insert_batches(
    factory: DatabaseSessionFactory,
    model: Any,
    payloads: list[dict[str, Any]],
    *,
    batch_size: int,
    conflict_column: str,
) -> tuple[int, int]:
    """Insert payloads with ``ON CONFLICT DO NOTHING``; return (new, skip)."""
    imported = 0
    for start in range(0, len(payloads), batch_size):
        chunk = payloads[start : start + batch_size]
        statement = pg_insert(model).values(chunk)
        statement = statement.on_conflict_do_nothing(index_elements=[conflict_column])
        async with factory.session() as session:
            async with session.begin():
                connection = await session.connection()
                result = await connection.execute(statement)
                imported += int(result.rowcount or 0)
    return imported, len(payloads) - imported


async def _run(args: argparse.Namespace, report: MigrationReport) -> None:
    """Validate every source, then insert (unless ``--dry-run``)."""
    factory = DatabaseSessionFactory(args.database_url, pool_size=2)
    try:
        async with factory.session() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:
        print(f"error: cannot reach Postgres: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    state_dir = Path(args.state_dir)
    queue_path = state_dir / "download_queue.json"
    send_db = state_dir / "send_status.db"
    operations_db = state_dir / "operations.db"

    # 1. download_queue.json ------------------------------------------------
    queue_doc = json.loads(queue_path.read_text(encoding="utf-8"))
    entries = queue_doc.get("entries", [])
    queue_report = report.for_source("download_queue")
    queue_payloads: list[dict[str, Any]] = []
    rounds_seen: dict[str, None] = {}
    for position, entry in enumerate(entries):
        queue_report.scanned += 1
        payload = _queue_payload(entry)
        if isinstance(payload, str):
            report.issues.append(
                RowIssue("download_queue", entry.get("key", position), payload)
            )
            continue
        queue_report.valid += 1
        queue_payloads.append(payload)
        rounds_seen.setdefault(str(entry.get("round")), None)

    # 2. weekly files -> rounds + missing queue entries ----------------------
    weekly_paths = sorted(glob.glob(args.weekly_glob))
    weekly_entries: list[dict[str, Any]] = []
    for weekly_path in weekly_paths:
        doc = json.loads(Path(weekly_path).read_text(encoding="utf-8"))
        round_name = doc.get("round", Path(weekly_path).stem)
        rounds_seen.setdefault(str(round_name), None)
        weekly_report = report.for_source(f"weekly:{round_name}")
        for item in doc.get("entries", []):
            weekly_report.scanned += 1
            sec = item.get("sec_user_id")
            if not sec or not isinstance(sec, str):
                report.issues.append(
                    RowIssue(
                        f"weekly:{round_name}", item.get("nickname"), "missing sec"
                    )
                )
                continue
            weekly_report.valid += 1
            weekly_entries.append(
                {
                    "key": f"{round_name}:{sec}",
                    "round": round_name,
                    "nickname": item.get("nickname", ""),
                    "sec_user_id": sec,
                    "chat_id": item.get("chat_id"),
                    "mode": item.get("mode", "incremental"),
                    "cutoff": item.get("cutoff"),
                    "status": "pending",
                    "operation_id": item.get("operation_id"),
                    "updated_at": doc.get("created_at", ""),
                }
            )
    for item in weekly_entries:
        payload = _queue_payload(item)
        if isinstance(payload, str):
            report.issues.append(RowIssue("weekly-queue", item["key"], payload))
            continue
        queue_payloads.append(payload)
        queue_report.valid += 1

    # 3. send_status.db -------------------------------------------------------
    send_report = report.for_source("send_status")
    send_payloads: list[dict[str, Any]] = []
    for position, row in enumerate(_sqlite_rows(send_db, "send_status")):
        send_report.scanned += 1
        payload = _send_payload(row)
        if isinstance(payload, str):
            report.issues.append(
                RowIssue("send_status", row.get("nickname", position), payload)
            )
            continue
        send_report.valid += 1
        send_payloads.append(payload)

    legacy_report = report.for_source("user_send_status")
    legacy_payloads: list[dict[str, Any]] = []
    for position, row in enumerate(_sqlite_rows(send_db, "user_send_status")):
        legacy_report.scanned += 1
        payload = _user_send_payload(row)
        if isinstance(payload, str):
            report.issues.append(
                RowIssue("user_send_status", row.get("username", position), payload)
            )
            continue
        legacy_report.valid += 1
        legacy_payloads.append(payload)

    # 4. seed_users.json + excluded -------------------------------------------
    stamp = _migration_stamp()
    seed_report = report.for_source("seed_accounts")
    seed_payloads: list[dict[str, Any]] = []
    seed_doc = json.loads(Path(args.seed_path).read_text(encoding="utf-8"))
    seed_items = seed_doc if isinstance(seed_doc, list) else seed_doc.get("users", [])
    for position, item in enumerate(seed_items):
        seed_report.scanned += 1
        payload = _seed_payload(item, stamp)
        if isinstance(payload, str):
            report.issues.append(
                RowIssue("seed_accounts", item.get("sec_user_id", position), payload)
            )
            continue
        seed_report.valid += 1
        seed_payloads.append(payload)
    excluded_path = state_dir / "excluded_accounts.json"
    excluded_names: set[str] = set()
    if excluded_path.exists():
        excluded_names = set(json.loads(excluded_path.read_text(encoding="utf-8")))
    by_nickname = {
        payload["nickname"]: payload
        for payload in seed_payloads
        if payload.get("nickname")
    }
    for name in excluded_names:
        target = by_nickname.get(name)
        if target is None:
            report.warnings.append(f"excluded nickname has no seed row: {name}")
        else:
            target["excluded"] = True

    # 5. douyin_users.db -------------------------------------------------------
    profile_report = report.for_source("user_profiles")
    profile_payloads: list[dict[str, Any]] = []
    for position, row in enumerate(_sqlite_rows(Path(args.users_db), "user_info_web")):
        profile_report.scanned += 1
        payload = _profile_payload(row, stamp)
        if isinstance(payload, str):
            report.issues.append(
                RowIssue("user_profiles", row.get("sec_user_id", position), payload)
            )
            continue
        profile_report.valid += 1
        profile_payloads.append(payload)

    # 6. operations.db (frozen history) ----------------------------------------
    ops_report = report.for_source("operations")
    ops_payloads: list[dict[str, Any]] = []
    if operations_db.exists():
        for position, row in enumerate(_sqlite_rows(operations_db, "operations")):
            ops_report.scanned += 1
            payload = _operation_payload(row)
            if isinstance(payload, str):
                report.issues.append(
                    RowIssue("operations", row.get("operation_id", position), payload)
                )
                continue
            ops_report.valid += 1
            ops_payloads.append(payload)
    else:
        report.warnings.append(f"operations.db not found at {operations_db}, skipped")

    if args.dry_run:
        for source_report in report.sources.values():
            source_report.skipped_existing = 0
        await factory.aclose()
        return

    batches = [
        (DownloadQueueRow, queue_payloads, "key", queue_report),
        (SendStatusRow, send_payloads, "nickname", send_report),
        (UserSendStatusRow, legacy_payloads, "username", legacy_report),
        (SeedAccountRow, seed_payloads, "sec_user_id", seed_report),
        (UserProfileRow, profile_payloads, "sec_user_id", profile_report),
        (OperationRow, ops_payloads, "operation_id", ops_report),
    ]
    for model, payloads, conflict, source_report in batches:
        if not payloads:
            continue
        imported, skipped = await _insert_batches(
            factory,
            model,
            payloads,
            batch_size=args.batch_size,
            conflict_column=conflict,
        )
        source_report.imported = imported
        source_report.skipped_existing = skipped

    round_payloads = [
        {"round": name, "note": None, "created_at": stamp, "updated_at": stamp}
        for name in rounds_seen
    ]
    if round_payloads:
        round_report = report.for_source("delivery_rounds")
        round_report.scanned = len(round_payloads)
        round_report.valid = len(round_payloads)
        imported, skipped = await _insert_batches(
            factory,
            DeliveryRoundRow,
            round_payloads,
            batch_size=args.batch_size,
            conflict_column="round",
        )
        round_report.imported = imported
        round_report.skipped_existing = skipped
    await factory.aclose()


def _print_report(report: MigrationReport, dry_run: bool) -> None:
    """Print per-source counts plus issues and warnings."""
    print("hermes-state migration (dry-run)" if dry_run else "hermes-state migration")
    for name, source in sorted(report.sources.items()):
        print(
            f"  {name}: scanned={source.scanned} valid={source.valid} "
            f"imported={source.imported} skipped_existing={source.skipped_existing}"
        )
    for warning in report.warnings:
        print(f"  warning: {warning}")
    for issue in report.issues[:20]:
        print(f"  issue: [{issue.source}] {issue.rowid}: {issue.reason}")
    if len(report.issues) > 20:
        print(f"  ... and {len(report.issues) - 20} more issues")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns the process exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True, help="state directory")
    parser.add_argument("--seed-path", required=True, help="seed_users.json path")
    parser.add_argument(
        "--weekly-glob",
        default="/root/weekly_align_ops_*.json",
        help="glob for weekly progress files",
    )
    parser.add_argument("--users-db", required=True, help="douyin_users.db path")
    parser.add_argument("--database-url", required=True, help="PG URL")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args(argv)
    report = MigrationReport()
    try:
        asyncio.run(_run(args, report))
    except SystemExit as exc:
        return int(exc.code or 2)
    except (OSError, ValueError, KeyError) as exc:
        print(f"error: migration failed: {exc}", file=sys.stderr)
        return 2
    _print_report(report, args.dry_run)
    return 1 if report.invalid else 0


if __name__ == "__main__":
    raise SystemExit(main())
