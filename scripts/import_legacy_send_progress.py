"""Import legacy weekly per-file send evidence without reading media files.

The legacy ``sent`` array is evidence of a successful send, but its paths
contain a nickname rather than a stable account ID. Only a nickname mapping
to exactly one ID in the frozen queue/seed sources can suppress a new send.
Failed, unmapped, malformed, and path-collision entries remain audit evidence.

Usage::

    PYTHONPATH=src uv run python scripts/import_legacy_send_progress.py \
      --state-dir /opt/dyvine/data/douyin/state \
      --seed-path /opt/dyvine/seed_users.json \
      --download-root /opt/dyvine/data/douyin/downloads \
      --work-db /opt/dyvine/data/douyin/state/legacy_send_import.sqlite3 \
      --dry-run

Set ``DATABASE_URL`` in the environment and omit ``--dry-run`` to import.
The work database checkpoints each complete source file and can be reused
with an unchanged source set. Database writes remain idempotent on rerun.
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import sqlite3
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

if TYPE_CHECKING:
    from dyvine.db.delivery_ledger import PostgresDeliveryLedgerRepository
    from dyvine.db.session import DatabaseSessionFactory

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.append(str(REPO_ROOT))

from scripts import legacy_extra_staging as extra  # noqa: E402
from scripts import legacy_send_staging as staging  # noqa: E402


def _topic_identity(raw_key: str) -> tuple[str, str] | None:
    nickname, separator, chat_suffix = raw_key.rpartition(":oc_")
    if not separator or not nickname or not chat_suffix:
        return None
    return nickname, f"oc_{chat_suffix}"


def _group_topic_candidates(
    connection: sqlite3.Connection,
    entries: list[Any],
    mapping: dict[str, set[str]],
) -> dict[tuple[str, str], dict[str, str]]:
    """Adopt topics only when raw chat keys and frozen queue agree uniquely."""
    queue_pairs: dict[tuple[str, str], list[tuple[str, str]]] = {}
    queue_groups: dict[tuple[str, str], list[tuple[str, str]]] = {}
    queue_chats: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        round_name, sec, nickname, chat = (
            entry.get("round"),
            entry.get("sec_user_id"),
            entry.get("nickname"),
            entry.get("chat_id"),
        )
        if not (
            isinstance(round_name, str)
            and round_name
            and isinstance(chat, str)
            and chat
            and isinstance(sec, str)
            and sec
            and isinstance(nickname, str)
            and nickname
        ):
            continue
        queue_pairs.setdefault((nickname, chat), []).append((round_name, sec))
        queue_groups.setdefault((round_name, sec), []).append((nickname, chat))
        queue_chats.setdefault((round_name, chat), []).append((nickname, sec))
    topics: dict[tuple[str, str], list[sqlite3.Row]] = {}
    chat_names: dict[str, set[str]] = {}
    topic_owners: dict[str, set[tuple[str, str]]] = {}
    for row in connection.execute(
        "SELECT source_file, round, nickname, topic_message_id FROM source_topics"
    ):
        identity = _topic_identity(row["nickname"])
        if identity is None:
            continue
        nickname, chat = identity
        topics.setdefault(identity, []).append(row)
        chat_names.setdefault(chat, set()).add(nickname)
        topic_owners.setdefault(row["topic_message_id"], set()).add(identity)
    results: dict[tuple[str, str], dict[str, str]] = {}
    for (nickname, chat), rows in topics.items():
        owners = queue_pairs.get((nickname, chat), [])
        if not owners or len({sec for _, sec in owners}) != 1:
            continue
        sec = owners[0][1]
        topic_ids = {row["topic_message_id"] for row in rows}
        if not (
            sec in mapping.get(nickname, set())
            and chat_names[chat] == {nickname}
            and len(topic_ids) == 1
            and topic_owners[next(iter(topic_ids))] == {(nickname, chat)}
        ):
            continue
        for round_name, owner_sec in owners:
            if queue_groups.get((round_name, owner_sec)) != [
                (nickname, chat)
            ] or queue_chats.get((round_name, chat)) != [(nickname, owner_sec)]:
                continue
            row = max(
                rows,
                key=lambda item: (
                    item["round"] == round_name,
                    staging._source_order(Path(item["source_file"])),
                ),
            )
            results[(round_name, owner_sec)] = {
                "round": round_name,
                "sec_user_id": owner_sec,
                "nickname": nickname,
                "chat_id": chat,
                "topic_message_id": row["topic_message_id"],
                "source_file": row["source_file"],
                "raw_topic_key": row["nickname"],
            }
    return results


def _reconciliation_rows(
    connection: sqlite3.Connection,
    queue_path: Path,
    mapping: dict[str, set[str]],
    groups: dict[tuple[str, str], dict[str, str]],
) -> Iterator[dict[str, Any]]:
    """List every frozen queue row without changing its claimable status."""
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    if not isinstance(queue, dict) or not isinstance(queue.get("entries"), list):
        raise ValueError("queue source must contain an entries list")
    sent_counts = {
        (row[0], row[1]): row[2]
        for row in connection.execute(
            """SELECT s.round, s.sec_user_id, COUNT(*) FROM sent_paths AS s
               LEFT JOIN collisions AS c ON c.sec_user_id = s.sec_user_id
                 AND c.relative_path = s.relative_path
               WHERE s.reason IS NULL AND c.sec_user_id IS NULL
               GROUP BY s.round, s.sec_user_id"""
        )
    }
    review_counts = {
        (row[0], row[1]): row[2]
        for row in connection.execute(
            """SELECT s.round, s.nickname, COUNT(*) FROM sent_paths AS s
               LEFT JOIN collisions AS c ON c.sec_user_id = s.sec_user_id
                 AND c.relative_path = s.relative_path
               WHERE s.reason IS NOT NULL OR c.sec_user_id IS NOT NULL
               GROUP BY s.round, s.nickname"""
        )
    }
    failed_counts = {
        (row[0], row[1]): row[2]
        for row in connection.execute(
            """SELECT round, nickname, COUNT(DISTINCT legacy_path) FROM failed_paths
               GROUP BY round, nickname"""
        )
    }
    permanent_counts = {
        row[0]: row[1]
        for row in connection.execute(
            """SELECT p.sec_user_id, COUNT(*) FROM permanent_paths AS p
               LEFT JOIN permanent_conflicts AS c
                 ON c.sec_user_id = p.sec_user_id
                AND c.relative_path = p.relative_path
               WHERE p.reason IS NULL AND c.sec_user_id IS NULL
               GROUP BY p.sec_user_id"""
        )
    }
    unresolved_permanent: dict[str | None, int] = {}
    for row in connection.execute(
        """SELECT p.nickname, COUNT(*) FROM permanent_paths AS p
           LEFT JOIN permanent_conflicts AS c
             ON c.sec_user_id = p.sec_user_id
            AND c.relative_path = p.relative_path
           WHERE p.reason IS NOT NULL OR c.sec_user_id IS NOT NULL
           GROUP BY p.nickname"""
    ):
        unresolved_permanent[row[0]] = row[1]
    cache_counts = {
        (row[0], row[1]): row[2]
        for row in connection.execute(
            """SELECT sec_user_id, nickname, COUNT(DISTINCT legacy_path)
               FROM cache_only GROUP BY sec_user_id, nickname"""
        )
    }
    snapshot_counts = {
        (row[0], row[1], row[2]): row[3]
        for row in connection.execute("""SELECT round, nickname, state, MAX(entries)
               FROM source_account_counts
               GROUP BY round, nickname, state""")
    }
    user_stats = {
        (row[0], row[1]): (row[2], row[3])
        for row in connection.execute("""SELECT round, nickname, MAX(failed),
                      SUM(CASE WHEN total = 0 AND sent = 0 THEN 1 ELSE 0 END)
               FROM source_user_stats GROUP BY round, nickname""")
    }
    latest_stats = {
        (row["round"], row["nickname"]): row["source_file"]
        for row in sorted(
            connection.execute(
                "SELECT round, nickname, source_file FROM source_user_stats"
            ),
            key=lambda row: staging._source_order(Path(row["source_file"])),
        )
    }
    candidate_snapshots: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in connection.execute(
        """SELECT u.source_file, u.round, u.nickname, u.chat_id, u.total
           FROM source_user_stats AS u
           LEFT JOIN source_account_counts AS sent
             ON sent.source_file = u.source_file AND sent.nickname = u.nickname
             AND sent.state = 'sent'
           LEFT JOIN source_account_counts AS failed
             ON failed.source_file = u.source_file AND failed.nickname = u.nickname
             AND failed.state = 'failed'
           WHERE u.done = 1 AND u.failed = 0 AND u.total > 0
             AND u.sent = u.total AND COALESCE(sent.entries, 0) = u.total
             AND COALESCE(failed.entries, 0) = 0"""
    ):
        candidate_snapshots.setdefault((row["round"], row["nickname"]), []).append(row)
    for index, entry in enumerate(queue["entries"]):
        if not isinstance(entry, dict):
            yield {
                "source_index": index,
                "classification": "needs_review",
                "reason": "non-object queue entry",
            }
            continue
        round_name = entry.get("round")
        nickname = entry.get("nickname")
        sec = entry.get("sec_user_id")
        status = entry.get("status")
        if (
            not isinstance(round_name, str)
            or not isinstance(nickname, str)
            or not isinstance(sec, str)
            or not isinstance(status, str)
        ):
            yield {
                "source_index": index,
                "key": entry.get("key"),
                "classification": "needs_review",
                "reason": "queue identity or status is malformed",
                "send_blocked": True,
            }
            continue
        unique_mapping = mapping.get(nickname) == {sec}
        safe = sent_counts.get((round_name, sec), 0)
        review = review_counts.get((round_name, nickname), 0)
        failed = failed_counts.get((round_name, nickname), 0)
        legacy_failed_max, zero_zero_snapshots = user_stats.get(
            (round_name, nickname), (None, 0)
        )
        snapshot_failed = snapshot_counts.get((round_name, nickname, "failed"), 0)
        permanent = permanent_counts.get(sec, 0)
        permanent_unresolved = unresolved_permanent.get(nickname, 0) + (
            unresolved_permanent.get(None, 0)
        )
        cache_unverified = sum(
            count
            for (cache_sec, cache_nickname), count in cache_counts.items()
            if cache_sec == sec or cache_nickname in (None, nickname)
        )
        chat_id = entry.get("chat_id")
        group = groups.get((round_name, sec))
        chat_unique = group is not None and group["chat_id"] == chat_id
        candidate = next(
            (
                source
                for source in candidate_snapshots.get((round_name, nickname), [])
                if chat_unique
                and source["chat_id"] == chat_id
                and source["source_file"] == latest_stats.get((round_name, nickname))
                and safe == source["total"]
            ),
            None,
        )
        if (
            not unique_mapping
            or review
            or failed
            or snapshot_failed
            or permanent
            or permanent_unresolved
            or cache_unverified
            or (legacy_failed_max or 0) > 0
            or zero_zero_snapshots
        ):
            classification = "needs_review"
            reason = "identity or historical file evidence needs review"
        elif status == "op_done" and candidate is not None and group is not None:
            classification = "terminal_candidate"
            reason = "legacy file and chat identifiers agree; verify Feishu history"
        elif status in {"pending", "downloading", "op_issue", "send_issue"}:
            classification = "resume_required"
            reason = "prior work is incomplete; delivery remains frozen"
        elif status == "op_done":
            classification = "needs_review"
            reason = "done, failed, total, sent, path, or chat evidence is incomplete"
        else:
            classification = "needs_review"
            reason = "legacy terminal state needs individual verification"
        yield {
            "source_index": index,
            "key": f"{round_name}:{sec}",
            "legacy_key": entry.get("key"),
            "round": round_name,
            "nickname": nickname,
            "sec_user_id": sec,
            "legacy_status": status,
            "classification": classification,
            "terminal_candidate": classification == "terminal_candidate",
            "reason": reason,
            "first_seen_safe_sent_paths": safe,
            "snapshot_sent_entries": snapshot_counts.get(
                (round_name, nickname, "sent"), 0
            ),
            "snapshot_failed_entries": snapshot_failed,
            "ambiguous_sent_paths": review,
            "failed_paths": failed,
            "permanent_failure_paths": permanent,
            "permanent_unresolved_paths": permanent_unresolved,
            "cache_only_unverified_paths": cache_unverified,
            "legacy_user_failed_max": legacy_failed_max,
            "legacy_zero_zero_snapshots": zero_zero_snapshots,
            "candidate_source_file": (
                candidate["source_file"]
                if candidate is not None and classification == "terminal_candidate"
                else None
            ),
            "candidate_total_files": (
                candidate["total"]
                if candidate is not None and classification == "terminal_candidate"
                else None
            ),
            "candidate_chat_id": (
                chat_id if classification == "terminal_candidate" else None
            ),
            "adoptable_group_chat_id": group["chat_id"] if group else None,
            "adoptable_topic_message_id": (
                group["topic_message_id"] if group else None
            ),
            "group_topic_source_file": group["source_file"] if group else None,
            "group_topic_raw_key": group["raw_topic_key"] if group else None,
            "feishu_history_verified": False,
            "send_blocked": True,
        }


def _write_reconciliation(path: Path, rows: Iterator[dict[str, Any]]) -> None:
    """Replace the report atomically only after every queue row was rendered."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as output:
            temporary = Path(output.name)
            os.chmod(temporary, 0o600)
            for row in rows:
                output.write(json.dumps(row, ensure_ascii=False) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


async def _import(
    connection: sqlite3.Connection,
    ledger: PostgresDeliveryLedgerRepository,
    report: staging.Report,
    batch_size: int,
) -> None:
    connection.execute("INSERT OR IGNORE INTO work_meta VALUES ('import_started', '1')")
    connection.commit()
    rows = connection.execute(
        """SELECT s.*, c.sec_user_id AS collision FROM sent_paths AS s
           LEFT JOIN collisions AS c ON c.sec_user_id = s.sec_user_id
             AND c.relative_path = s.relative_path ORDER BY s.id"""
    )
    batch: list[dict[str, str | None]] = []
    audit_batch: list[dict[str, str | None]] = []
    for row in rows:
        if row["reason"] is not None or row["collision"] is not None:
            reason = row["reason"] or (
                "same relative path has multiple owner directories"
            )
            audit_batch.append(
                {
                    "source_file": row["source_file"],
                    "legacy_path": row["legacy_path"],
                    "legacy_state": "sent_ambiguous",
                    "nickname": row["nickname"],
                    "sec_user_id": row["sec_user_id"],
                    "reason": reason,
                }
            )
            if len(audit_batch) >= batch_size:
                inserted, existing = await ledger.upsert_legacy_evidence_batch(
                    audit_batch
                )
                report.audit_inserted += inserted
                report.audit_existing += existing
                audit_batch.clear()
            continue
        batch.append(
            {
                "round": row["round"],
                "sec_user_id": row["sec_user_id"],
                "relative_path": row["relative_path"],
                "chat_id": None,
                "parent_id": None,
                "legacy_source_path": row["legacy_path"],
                "legacy_progress_file": row["source_file"],
            }
        )
        if len(batch) >= batch_size:
            inserted, existing = await ledger.reserve_legacy_sent_batch(batch)
            report.inserted += inserted
            report.existing += existing
            batch.clear()
    if batch:
        inserted, existing = await ledger.reserve_legacy_sent_batch(batch)
        report.inserted += inserted
        report.existing += existing
    for row in connection.execute("SELECT * FROM failed_paths ORDER BY id"):
        audit_batch.append(
            {
                "source_file": row["source_file"],
                "legacy_path": row["legacy_path"],
                "legacy_state": "failed",
                "nickname": row["nickname"],
                "sec_user_id": row["sec_user_id"],
                "reason": row["reason"] or "legacy progress reports failed send",
            }
        )
        if len(audit_batch) >= batch_size:
            inserted, existing = await ledger.upsert_legacy_evidence_batch(audit_batch)
            report.audit_inserted += inserted
            report.audit_existing += existing
            audit_batch.clear()
    if audit_batch:
        inserted, existing = await ledger.upsert_legacy_evidence_batch(audit_batch)
        report.audit_inserted += inserted
        report.audit_existing += existing
    await _import_extra_evidence(connection, ledger, report, batch_size)


async def _import_extra_evidence(
    connection: sqlite3.Connection,
    ledger: PostgresDeliveryLedgerRepository,
    report: staging.Report,
    batch_size: int,
) -> None:
    permanent_source = connection.execute(
        "SELECT path FROM extra_sources WHERE kind = 'permanent'"
    ).fetchone()
    sent_index = connection.execute(
        "SELECT path FROM extra_sources WHERE kind = 'sent_index'"
    ).fetchone()
    permanent_batch: list[dict[str, str | None]] = []
    audit_batch: list[dict[str, str | None]] = []
    for row in connection.execute(
        """SELECT p.*, c.sec_user_id AS collision FROM permanent_paths AS p
           LEFT JOIN permanent_conflicts AS c
             ON c.sec_user_id = p.sec_user_id
            AND c.relative_path = p.relative_path ORDER BY p.id"""
    ):
        safe = row["reason"] is None and row["collision"] is None
        if safe:
            permanent_batch.append(
                {
                    "sec_user_id": row["sec_user_id"],
                    "relative_path": row["relative_path"],
                    "legacy_source_path": row["legacy_path"],
                    "legacy_progress_file": permanent_source["path"],
                }
            )
            if len(permanent_batch) >= batch_size:
                inserted, existing = (
                    await ledger.reserve_legacy_permanent_failure_batch(permanent_batch)
                )
                report.permanent_inserted += inserted
                report.permanent_existing += existing
                permanent_batch.clear()
        audit_batch.append(
            {
                "source_file": permanent_source["path"],
                "legacy_path": row["legacy_path"],
                "legacy_state": (
                    "permanent_failure" if safe else "permanent_failure_unresolved"
                ),
                "nickname": row["nickname"],
                "sec_user_id": row["sec_user_id"],
                "reason": row["reason"]
                or (
                    "path conflicts with sent, cache, or another owner"
                    if not safe
                    else "listed as a permanent failure in frozen legacy state"
                ),
            }
        )
        if len(audit_batch) >= batch_size:
            inserted, existing = await ledger.upsert_legacy_evidence_batch(audit_batch)
            report.audit_inserted += inserted
            report.audit_existing += existing
            audit_batch.clear()
    if permanent_batch:
        inserted, existing = await ledger.reserve_legacy_permanent_failure_batch(
            permanent_batch
        )
        report.permanent_inserted += inserted
        report.permanent_existing += existing
    for row in connection.execute("SELECT * FROM cache_only ORDER BY id"):
        audit_batch.append(
            {
                "source_file": row["source_file"],
                "legacy_path": row["legacy_path"],
                "legacy_state": "sent_unverified",
                "nickname": row["nickname"],
                "sec_user_id": row["sec_user_id"],
                "reason": (
                    f"cache-only entry in {Path(sent_index['path']).name}; "
                    "original source is unavailable"
                ),
            }
        )
        if len(audit_batch) >= batch_size:
            inserted, existing = await ledger.upsert_legacy_evidence_batch(audit_batch)
            report.audit_inserted += inserted
            report.audit_existing += existing
            audit_batch.clear()
    if audit_batch:
        inserted, existing = await ledger.upsert_legacy_evidence_batch(audit_batch)
        report.audit_inserted += inserted
        report.audit_existing += existing


async def run(
    args: argparse.Namespace,
    ledger: PostgresDeliveryLedgerRepository | None = None,
) -> staging.Report:
    """Stage a frozen source set, then persist only unambiguous sends."""
    pattern = args.progress_glob or str(
        Path(args.state_dir) / "send_progress_weekly*.json"
    )
    paths = sorted(
        {Path(value).resolve() for value in glob.glob(pattern)},
        key=staging._source_order,
    )
    if not paths:
        raise ValueError(f"no progress files match {pattern}")
    root = PurePosixPath(args.download_root)
    if not root.is_absolute() or ".." in root.parts:
        raise ValueError("download root must be absolute and traversal-free")
    identity_sources = [Path(args.queue_path), Path(args.seed_path)]
    extra_sources = {
        kind: Path(value).resolve()
        for kind, value in (
            ("permanent", args.permanent_failures),
            ("sent_index", args.sent_index),
        )
        if value is not None
    }
    protected_sources = {
        *paths,
        *[source.resolve() for source in identity_sources],
    }
    if len(set(extra_sources.values())) != len(extra_sources) or any(
        source in protected_sources for source in extra_sources.values()
    ):
        raise ValueError("extra evidence source overlaps another source")
    protected_sources.update(extra_sources.values())
    identity_fingerprint = staging._source_fingerprint(identity_sources, root)
    mapping = staging._nicknames(*identity_sources)
    if identity_fingerprint != staging._source_fingerprint(identity_sources, root):
        raise ValueError("queue or seed changed while reading identities")
    report = staging.Report(sources=len(paths))
    temporary: tempfile.TemporaryDirectory[str] | None = None
    if args.work_db is None:
        temporary = tempfile.TemporaryDirectory(prefix="dyvine-legacy-send-")
        work_path = Path(temporary.name) / "staging.sqlite3"
    else:
        work_path = Path(args.work_db)
    if work_path.resolve() in protected_sources:
        raise ValueError("work database would overwrite a source file")
    factory: DatabaseSessionFactory | None = None
    connection: sqlite3.Connection | None = None
    try:
        if not args.dry_run and ledger is None:
            from dyvine.db.session import DatabaseSessionFactory

            url = os.environ.get(args.database_url_env)
            if not url:
                raise ValueError(f"{args.database_url_env} is not set")
            factory = DatabaseSessionFactory(url)
            async with factory.session() as session:
                for table in (
                    "delivery_files",
                    "delivery_legacy_evidence",
                    "delivery_groups",
                ):
                    await session.execute(text(f"SELECT 1 FROM {table} LIMIT 1"))
        connection = staging._work_database(work_path)
        staging._check_work_inputs(connection, identity_fingerprint)
        staging._stage(connection, paths, mapping, root, report)
        extra.stage_extras(connection, extra_sources, mapping, root)
        staging._summarize(connection, report)
        extra.summarize_extras(connection, report)
        queue_doc = json.loads(Path(args.queue_path).read_text(encoding="utf-8"))
        if not isinstance(queue_doc, dict) or not isinstance(
            queue_doc.get("entries"), list
        ):
            raise ValueError("queue source must contain an entries list")
        groups = _group_topic_candidates(connection, queue_doc["entries"], mapping)
        report.group_candidates = len(groups)
        if args.reconcile_output:
            output_path = Path(args.reconcile_output)
            protected = {*protected_sources, work_path.resolve()}
            if output_path.resolve() in protected:
                raise ValueError("reconciliation output would replace a source file")
            _write_reconciliation(
                output_path,
                _reconciliation_rows(
                    connection, Path(args.queue_path), mapping, groups
                ),
            )
        if not args.dry_run:
            staging._verify_source_stats(connection, paths)
            extra.verify_extra_source_stats(connection)
            if identity_fingerprint != staging._source_fingerprint(
                identity_sources, root
            ):
                raise ValueError("queue or seed changed after staging")
            if ledger is None:
                from dyvine.db.delivery_ledger import PostgresDeliveryLedgerRepository

                assert factory is not None
                ledger = PostgresDeliveryLedgerRepository(factory)
            await _import(connection, ledger, report, args.batch_size)
    finally:
        if connection is not None:
            connection.close()
        if factory is not None:
            await factory.aclose()
        if temporary is not None:
            temporary.cleanup()
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--seed-path", required=True)
    parser.add_argument("--queue-path")
    parser.add_argument("--progress-glob")
    parser.add_argument(
        "--permanent-failures", help="frozen permanent_failures.json path"
    )
    parser.add_argument("--sent-index", help="frozen report_sent_index.json cache path")
    parser.add_argument("--download-root", default="/opt/dyvine/data/douyin/downloads")
    parser.add_argument("--work-db", help="persistent per-source checkpoint database")
    parser.add_argument(
        "--reconcile-output", help="write one frozen queue decision per JSONL row"
    )
    parser.add_argument("--database-url-env", default="DATABASE_URL")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.batch_size < 1 or args.batch_size > 500:
        parser.error("--batch-size must be between 1 and 500")
    args.queue_path = args.queue_path or str(
        Path(args.state_dir) / "download_queue.json"
    )
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = asyncio.run(run(args))
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"error: legacy send import failed: {exc}", file=sys.stderr)
        return 2
    except Exception:
        print(
            "error: Postgres import failed; inspect schema and target before retrying",
            file=sys.stderr,
        )
        return 2
    print(
        f"sources={report.sources} staged={report.staged} reused={report.reused} "
        f"sent={report.sent} unique_sent={report.sent_unique} "
        f"duplicate_sent={report.sent_duplicates} failed={report.failed} "
        f"unique_failed={report.failed_unique} safe_sent={report.safe_sent} "
        f"needs_review={report.needs_review} inserted={report.inserted} "
        f"existing={report.existing} audit_inserted={report.audit_inserted} "
        f"audit_existing={report.audit_existing} "
        f"group_candidates={report.group_candidates} "
        f"permanent_paths={report.permanent_paths} "
        f"permanent_safe={report.permanent_safe} "
        f"permanent_inserted={report.permanent_inserted} "
        f"permanent_existing={report.permanent_existing} "
        f"cache_paths={report.cache_paths} cache_only={report.cache_only}"
    )
    return 1 if report.needs_review else 0


if __name__ == "__main__":
    raise SystemExit(main())
