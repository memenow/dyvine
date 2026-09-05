"""One-shot migration: SQLite ``watch_subscriptions`` -> Postgres.

Reads the legacy pod-local SQLite database, validates every
``watch_subscriptions`` row, and inserts the valid ones into Postgres.
Safe to re-run: rows are keyed by ``user_id`` and existing users are
skipped (``ON CONFLICT DO NOTHING``), so a second run is a no-op that
only re-validates the source.

``operations`` rows are intentionally NOT migrated: in-flight work from
the migration window is marked failed at first boot of the new
revision (documented in the release notes), and terminal rows are
transient status, not configuration.

Usage:
    PYTHONPATH=src uv run python scripts/migrate_watch_to_pg.py \\
        --sqlite-path data/douyin/state/operations.db \\
        --database-url postgresql+asyncpg://... \\
        [--dry-run]

Exit codes: 0 on success (including dry-run with zero invalid rows),
1 when any source row fails validation, 2 on connection/config errors.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402

from dyvine.db.models import WatchSubscriptionRow  # noqa: E402
from dyvine.db.session import DatabaseSessionFactory  # noqa: E402

LEGACY_TABLE = "watch_subscriptions"


@dataclass
class RowIssue:
    """One source row that failed validation."""

    rowid: Any
    reason: str


@dataclass
class MigrationReport:
    """Counts and issues collected during a migration run."""

    scanned: int = 0
    valid: int = 0
    imported: int = 0
    skipped_existing: int = 0
    issues: list[RowIssue] = field(default_factory=list)

    @property
    def invalid(self) -> int:
        """Number of source rows that failed validation."""
        return len(self.issues)


def _validated_row(raw: dict[str, Any]) -> dict[str, Any] | str:
    """Validate one legacy row; return the PG payload or a reason.

    The legacy schema stores ``enabled`` as INTEGER 0/1 and
    ``checkpoint`` as a JSON TEXT blob; both are normalised here so
    the INSERT path only ever sees clean values.
    """
    subscription_id = raw.get("subscription_id")
    user_id = raw.get("user_id")
    if not subscription_id or not isinstance(subscription_id, str):
        return "missing or non-string subscription_id"
    if not user_id or not isinstance(user_id, str):
        return "missing or non-string user_id"
    enabled = raw.get("enabled")
    if enabled not in (0, 1):
        return f"enabled must be 0 or 1, got {enabled!r}"
    for column in ("live_poll_seconds", "post_poll_seconds"):
        value = raw.get(column)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return f"{column} must be a non-negative int, got {value!r}"
    try:
        checkpoint = json.loads(str(raw.get("checkpoint") or "{}"))
    except json.JSONDecodeError as exc:
        return f"checkpoint is not valid JSON: {exc}"
    if not isinstance(checkpoint, dict):
        return "checkpoint JSON must decode to an object"
    for column in ("created_at", "updated_at"):
        value = raw.get(column)
        if not value or not isinstance(value, str):
            return f"missing or non-string {column}"
    last_live = raw.get("last_live_check")
    last_post = raw.get("last_post_check")
    if last_live is not None and not isinstance(last_live, str):
        return "last_live_check must be a string or NULL"
    if last_post is not None and not isinstance(last_post, str):
        return "last_post_check must be a string or NULL"
    return {
        "subscription_id": subscription_id,
        "user_id": user_id,
        "enabled": bool(enabled),
        "live_poll_seconds": raw["live_poll_seconds"],
        "post_poll_seconds": raw["post_poll_seconds"],
        "checkpoint": checkpoint,
        "last_live_check": last_live,
        "last_post_check": last_post,
        "created_at": raw["created_at"],
        "updated_at": raw["updated_at"],
    }


def load_validated_rows(
    sqlite_path: Path,
) -> tuple[list[dict[str, Any]], MigrationReport]:
    """Read and validate every subscription row from SQLite.

    Raises:
        FileNotFoundError: If ``sqlite_path`` does not exist.
        ValueError: If the legacy table is missing.
    """
    if not sqlite_path.exists():
        raise FileNotFoundError(f"SQLite database not found: {sqlite_path}")
    report = MigrationReport()
    payloads: list[dict[str, Any]] = []
    connection = sqlite3.connect(sqlite_path)
    try:
        connection.row_factory = sqlite3.Row
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if LEGACY_TABLE not in tables:
            raise ValueError(f"table {LEGACY_TABLE!r} not found in {sqlite_path}")
        # Table name is a module constant, not user input.
        rows = connection.execute(
            f"SELECT rowid, * FROM {LEGACY_TABLE} ORDER BY rowid"
        ).fetchall()
    finally:
        connection.close()
    for row in rows:
        report.scanned += 1
        raw = dict(row)
        validated = _validated_row(raw)
        if isinstance(validated, str):
            report.issues.append(RowIssue(rowid=raw.get("rowid"), reason=validated))
        else:
            report.valid += 1
            payloads.append(validated)
    return payloads, report


async def import_rows(
    database_url: str,
    payloads: list[dict[str, Any]],
    report: MigrationReport,
) -> None:
    """Insert validated rows; existing users are skipped, not overwritten.

    A row that already exists in Postgres (same ``user_id``) is left
    untouched: post-cutover rows are authoritative and re-running the
    script must never clobber a subscription created natively.
    """
    factory = DatabaseSessionFactory(database_url)
    try:
        async with factory.session() as session:
            async with session.begin():
                for payload in payloads:
                    statement = (
                        pg_insert(WatchSubscriptionRow)
                        .values(**payload)
                        .on_conflict_do_nothing(index_elements=["user_id"])
                    )
                    connection = await session.connection()
                    result = await connection.execute(statement)
                    if (result.rowcount or 0) > 0:
                        report.imported += 1
                    else:
                        report.skipped_existing += 1
    finally:
        await factory.aclose()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the migration CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Migrate SQLite watch_subscriptions to Postgres.",
    )
    parser.add_argument(
        "--sqlite-path",
        required=True,
        type=Path,
        help="Legacy SQLite database file to read from.",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Target Postgres URL (default: $DATABASE_URL).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate the source and report counts without writing.",
    )
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> MigrationReport:
    """Execute the migration described by ``args``."""
    import os

    payloads, report = load_validated_rows(args.sqlite_path)
    if args.dry_run or not payloads:
        return report
    database_url = args.database_url or os.environ.get("DATABASE_URL")
    if not database_url:
        raise ValueError("no target: pass --database-url or set $DATABASE_URL")
    await import_rows(database_url, payloads, report)
    return report


def main(argv: list[str] | None = None) -> int:
    """CLI entry point mapping outcomes to exit codes."""
    args = parse_args(argv)
    try:
        report = asyncio.run(run(args))
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: cannot reach Postgres: {exc}", file=sys.stderr)
        return 2
    print(
        f"scanned={report.scanned} valid={report.valid} "
        f"imported={report.imported} "
        f"skipped_existing={report.skipped_existing} "
        f"invalid={report.invalid}"
    )
    for issue in report.issues[:20]:
        print(f"invalid rowid={issue.rowid}: {issue.reason}", file=sys.stderr)
    if len(report.issues) > 20:
        print(
            f"... and {len(report.issues) - 20} more invalid rows",
            file=sys.stderr,
        )
    return 1 if report.invalid else 0


if __name__ == "__main__":
    raise SystemExit(main())
