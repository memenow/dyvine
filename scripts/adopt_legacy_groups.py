"""Verify and adopt frozen legacy Feishu groups without sending messages.

The progress checkpoint stores topic keys as ``nickname:oc_<chat suffix>``.
The frozen queue supplies the stable account ID. A private 0600 JSONL report
records every queue decision and its Feishu read outcome before import.

Example::

    PYTHONPATH=src uv run python scripts/adopt_legacy_groups.py \
      --queue-path /opt/dyvine/data/douyin/state/download_queue.json \
      --work-db /opt/dyvine/data/douyin/state/legacy_send_import.sqlite3 \
      --round weekly0913 \
      --output /opt/dyvine/data/douyin/state/legacy_groups.jsonl

After reviewing the complete report, rerun with ``--resume --apply`` and a
``DATABASE_URL`` in the environment. The protected environment must also
provide ``DYVINE_WEEKLY_OWNER_OPEN_ID``; do not place that ID in command
arguments. Apply rechecks Feishu before each idempotent Postgres import.
It never creates a group, topic, or message.

``--discover-missing-topics`` requires ``--round`` and uses complete read-only
chat history to identify one app-authored profile root when legacy topic keys
are absent. It writes a separate, mode-bound journal and rechecks history on
apply; default adoption remains unchanged.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import sys
import traceback
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from dyvine.services.delivery import FeishuCredentials  # noqa: E402
from scripts.feishu_audit_core import (  # noqa: E402
    AuditError,
    FeishuReader,
    Journal,
    _nonempty,
)
from scripts.legacy_group_candidates import (  # noqa: E402
    Candidate,
    _candidates,
    _load_sources,
    _topic_identity,
)
from scripts.legacy_topic_discovery import discover as _discover  # noqa: E402


class GroupReader(FeishuReader):
    """Add the official read-only chat lookup to the shared Feishu reader."""

    async def get_chat(self, chat_id: str) -> dict[str, Any]:
        data = await self._get(
            f"https://open.feishu.cn/open-apis/im/v1/chats/{quote(chat_id, safe='')}",
            {"user_id_type": "open_id"},
        )
        chat = data.get("chat")
        details = chat if isinstance(chat, dict) else data
        if "chat_id" not in details:
            # Never backfill the identity under check: echoing the
            # request id would make the caller's
            # ``chat_id != candidate.chat_id`` comparison tautological
            # and launder error responses as verified reads.
            raise AuditError("chat details are missing chat_id")
        return details


class ReadableGroups(Protocol):
    async def get_chat(self, chat_id: str) -> dict[str, Any]: ...

    async def get_message(self, message_id: str) -> dict[str, Any]: ...

    async def list_messages(
        self, container_type: str, container_id: str, page_token: str | None
    ) -> dict[str, Any]: ...


class ImportableGroups(Protocol):
    async def import_legacy_group_topic(
        self,
        *,
        round: str,
        sec_user_id: str,
        nickname: str,
        chat_id: str,
        topic_message_id: str,
        source_file: str,
    ) -> Any: ...


async def _verify(
    candidate: Candidate,
    reader: ReadableGroups,
    app_id: str,
    expected_owner: str | None,
) -> tuple[str, dict[str, Any]]:
    if candidate.issue:
        return candidate.issue, {}
    if not candidate.chat_id or not candidate.topic_message_id:
        # Frozen-input guard, never ``assert`` (stripped under -O):
        # hold the row with its own decision instead of crashing the
        # batch scan or probing Feishu with an empty identity.
        return "frozen_identity_incomplete", {}
    try:
        chat = await reader.get_chat(candidate.chat_id)
        if chat.get("chat_id") != candidate.chat_id:
            return "feishu_chat_identity_mismatch", {}
        if chat.get("chat_mode") not in (None, "group"):
            return "feishu_chat_is_not_group", {}
        if chat.get("chat_status") != "normal":
            return "feishu_chat_not_active", {}
        owner = _nonempty(chat.get("owner_id"))
        if expected_owner and (
            chat.get("owner_id_type") not in (None, "open_id")
            or owner != expected_owner
        ):
            return "feishu_chat_owner_mismatch", {}
        message = await reader.get_message(candidate.topic_message_id)
        if message.get("message_id") != candidate.topic_message_id:
            return "feishu_topic_identity_mismatch", {}
        if message.get("chat_id") != candidate.chat_id:
            return "feishu_topic_chat_mismatch", {}
        if message.get("deleted") is True:
            return "feishu_topic_deleted", {}
        sender = message.get("sender")
        if not isinstance(sender, dict) or (
            sender.get("sender_type") != "app" or sender.get("id") != app_id
        ):
            return "feishu_topic_app_mismatch", {}
        return "verified", {
            "chat_owner_open_id": owner,
            "chat_mode": chat.get("chat_mode"),
            "sender_app_id": app_id,
            "thread_id": _nonempty(message.get("thread_id")),
        }
    except AuditError as error:
        return "feishu_read_failed", {"read_error": str(error)}


async def _review_candidate(
    candidate: Candidate,
    reader: ReadableGroups,
    app_id: str,
    expected_owner: str | None,
    discovery_ids: set[str],
    unique_chats: set[str],
    *,
    discover: bool,
) -> tuple[str, dict[str, Any]]:
    if discover and candidate.row_id in discovery_ids:
        return await _discover(
            candidate,
            reader,
            app_id,
            expected_owner,
            unique_chat=candidate.chat_id in unique_chats,
        )
    return await _verify(candidate, reader, app_id, expected_owner)


def _report_rows(
    journal: Journal, candidates: list[Candidate]
) -> dict[str, dict[str, Any]]:
    expected = {candidate.row_id: candidate for candidate in candidates}
    rows: dict[str, dict[str, Any]] = {}
    for row in journal.rows:
        if row.get("type") != "candidate":
            continue
        row_id = row.get("row_id")
        if row_id not in expected or row_id in rows:
            raise AuditError("journal has an unknown or duplicate queue row")
        candidate = expected[str(row_id)]
        if any(row.get(field) != value for field, value in asdict(candidate).items()):
            raise AuditError("journal queue evidence differs from frozen sources")
        rows[str(row_id)] = row
    return rows


def _unmatched_rows(journal: Journal, unmatched: list[dict[str, Any]]) -> set[str]:
    expected = {row["row_id"]: row for row in unmatched}
    seen: set[str] = set()
    for row in journal.rows:
        if row.get("type") != "unmatched_topic":
            continue
        row_id = row.get("row_id")
        if row_id not in expected or row_id in seen or row != expected[row_id]:
            raise AuditError("journal topic evidence differs from frozen sources")
        seen.add(str(row_id))
    return seen


async def run(
    args: argparse.Namespace,
    *,
    reader: ReadableGroups | None = None,
    ledger: ImportableGroups | None = None,
) -> dict[str, Any]:
    credentials = FeishuCredentials.from_hermes_default()
    expected_owner = os.environ.get("DYVINE_WEEKLY_OWNER_OPEN_ID", "").strip()
    if not expected_owner.startswith("ou_"):
        raise ValueError("DYVINE_WEEKLY_OWNER_OPEN_ID must be an open_id")
    entries, topics, fingerprint = _load_sources(
        args.queue_path,
        args.work_db,
        args.seed_path,
        credentials.app_id,
        expected_owner,
        args.round,
    )
    candidates, unmatched = _candidates(entries, topics)
    chat_owners: dict[str, set[str | None]] = {}
    for item in candidates:
        if item.chat_id:
            chat_owners.setdefault(item.chat_id, set()).add(item.sec_user_id)
    unique_chats = {
        chat
        for chat, owners in chat_owners.items()
        if len(owners) == 1 and None not in owners
    }
    topic_keys = {
        (round_name, *identity)
        for _, round_name, raw_key, _ in topics
        if (identity := _topic_identity(raw_key)) is not None
    }
    discovery_ids = {
        item.row_id
        for item in candidates
        if item.issue == "missing_or_conflicting_topic"
        and (item.round, item.nickname, item.chat_id) not in topic_keys
    }
    if args.round is not None:
        candidates = [item for item in candidates if item.round == args.round]
        unmatched = [item for item in unmatched if item.get("round") == args.round]
    if args.discover_missing_topics:
        fingerprint = hashlib.sha256(
            f"legacy-topic-discovery-v2\0{fingerprint}".encode()
        ).hexdigest()
    journal = Journal(args.output, fingerprint, resume=args.resume)
    try:
        rows = _report_rows(journal, candidates)
        unmatched_ids = _unmatched_rows(journal, unmatched)
        summaries = [row for row in journal.rows if row.get("type") == "summary"]
        if len(summaries) > 1:
            raise AuditError("journal has multiple dry-run summaries")
        if reader is None:
            async with httpx.AsyncClient(timeout=30.0) as client:
                reader = GroupReader(client, credentials, args.request_interval)
                result = await _run_with_reader(
                    args,
                    reader,
                    ledger,
                    journal,
                    candidates,
                    unmatched,
                    rows,
                    unmatched_ids,
                    summaries,
                    credentials.app_id,
                    expected_owner,
                    discovery_ids,
                    unique_chats,
                )
        else:
            result = await _run_with_reader(
                args,
                reader,
                ledger,
                journal,
                candidates,
                unmatched,
                rows,
                unmatched_ids,
                summaries,
                credentials.app_id,
                expected_owner,
                discovery_ids,
                unique_chats,
            )
        return {
            "source_sha256": fingerprint,
            "round": args.round,
            "queue_rows": len(candidates),
            "unmatched_topics": len(unmatched),
            **result,
        }
    finally:
        journal.close()


async def _run_with_reader(
    args: argparse.Namespace,
    reader: ReadableGroups,
    ledger: ImportableGroups | None,
    journal: Journal,
    candidates: list[Candidate],
    unmatched: list[dict[str, Any]],
    rows: dict[str, dict[str, Any]],
    unmatched_ids: set[str],
    summaries: list[dict[str, Any]],
    app_id: str,
    expected_owner: str | None,
    discovery_ids: set[str],
    unique_chats: set[str],
) -> dict[str, int]:
    if args.apply:
        if (
            not args.resume
            or not summaries
            or len(rows) != len(candidates)
            or len(unmatched_ids) != len(unmatched)
        ):
            raise AuditError("apply requires a complete resumed dry-run report")
        if (
            summaries[0].get("queue_rows") != len(candidates)
            or summaries[0].get("unmatched_topics") != len(unmatched)
            or summaries[0].get("round") != args.round
        ):
            raise AuditError("dry-run summary does not match frozen sources")
        if ledger is None:
            from dyvine.db.delivery_ledger import PostgresDeliveryLedgerRepository
            from dyvine.db.session import DatabaseSessionFactory

            url = os.environ.get(args.database_url_env)
            if not url:
                raise ValueError(f"{args.database_url_env} is not set")
            factory = DatabaseSessionFactory(url)
            ledger = PostgresDeliveryLedgerRepository(factory)
        else:
            factory = None
        try:
            applied = {
                row.get("row_id")
                for row in journal.rows
                if row.get("type") == "applied"
            }
            for candidate in candidates:
                report = rows[candidate.row_id]
                discovered = (
                    args.discover_missing_topics
                    and candidate.row_id in discovery_ids
                    and report["decision"] == "discovered"
                )
                if candidate.row_id in applied or (
                    report["decision"] != "verified" and not discovered
                ):
                    continue
                decision, evidence = await _review_candidate(
                    candidate,
                    reader,
                    app_id,
                    expected_owner,
                    discovery_ids,
                    unique_chats,
                    discover=args.discover_missing_topics,
                )
                if (
                    discovered
                    and decision == "discovered"
                    and any(
                        evidence.get(field) != report.get(field)
                        for field in (
                            "discovered_topic_message_id",
                            "chat_history_sha256",
                            "root_message_sha256",
                            "history_pages",
                            "history_messages",
                            "history_file_messages",
                            "chat_owner_open_id",
                        )
                    )
                ):
                    decision = "feishu_history_changed"
                if decision != report["decision"]:
                    journal.append(
                        {
                            "type": "apply_held",
                            "row_id": candidate.row_id,
                            "reason": decision,
                            **evidence,
                        }
                    )
                    continue
                if ledger is None:
                    raise AuditError("apply requires a delivery ledger")
                if not (
                    candidate.round and candidate.sec_user_id and candidate.nickname
                ):
                    raise AuditError("verified account identity is incomplete")
                if not candidate.chat_id:
                    raise AuditError("verified chat identity is incomplete")
                topic_id = (
                    _nonempty(evidence.get("discovered_topic_message_id"))
                    if discovered
                    else candidate.topic_message_id
                )
                source_file = (
                    str(args.output.resolve()) if discovered else candidate.source_file
                )
                if not topic_id or not source_file:
                    raise AuditError("verified topic identity is incomplete")
                await ledger.import_legacy_group_topic(
                    round=candidate.round,
                    sec_user_id=candidate.sec_user_id,
                    nickname=candidate.nickname,
                    chat_id=candidate.chat_id,
                    topic_message_id=topic_id,
                    source_file=source_file,
                )
                journal.append({"type": "applied", "row_id": candidate.row_id})
        finally:
            if factory is not None:
                await factory.aclose()
        return dict(
            Counter(
                str(row["type"])
                for row in journal.rows
                if row.get("type") in {"applied", "apply_held"}
            )
        )
    if summaries:
        if len(rows) != len(candidates) or len(unmatched_ids) != len(unmatched):
            raise AuditError("journal summary precedes incomplete queue scan")
        return dict(Counter(str(row["decision"]) for row in rows.values()))
    for candidate in candidates:
        if candidate.row_id in rows:
            continue
        decision, evidence = await _review_candidate(
            candidate,
            reader,
            app_id,
            expected_owner,
            discovery_ids,
            unique_chats,
            discover=args.discover_missing_topics,
        )
        journal.append(
            {"type": "candidate", **asdict(candidate), "decision": decision, **evidence}
        )
    for row in unmatched:
        if row["row_id"] not in unmatched_ids:
            journal.append(row)
    counts = Counter(
        str(row["decision"]) for row in journal.rows if row.get("type") == "candidate"
    )
    journal.append(
        {
            "type": "summary",
            "round": args.round,
            "queue_rows": len(candidates),
            "unmatched_topics": len(unmatched),
            "decisions": dict(counts),
        }
    )
    return dict(counts)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue-path", required=True, type=Path)
    parser.add_argument("--work-db", required=True, type=Path)
    parser.add_argument("--seed-path", type=Path)
    parser.add_argument("--round", help="limit Feishu reads and imports to one round")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--database-url-env", default="DATABASE_URL")
    parser.add_argument("--request-interval", type=float, default=0.25)
    parser.add_argument("--discover-missing-topics", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.request_interval < 0.1:
        parser.error("--request-interval must be at least 0.1 seconds")
    if args.round is not None and not args.round.strip():
        parser.error("--round must be nonempty")
    if args.discover_missing_topics and args.round is None:
        parser.error("--discover-missing-topics requires --round")
    if args.apply and not args.resume:
        parser.error("--apply requires --resume")
    if args.output.resolve() in {args.queue_path.resolve(), args.work_db.resolve()} or (
        args.seed_path and args.output.resolve() == args.seed_path.resolve()
    ):
        parser.error("output must not replace an input")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = asyncio.run(run(args))
    except (AuditError, OSError, ValueError, sqlite3.Error) as error:
        print(f"legacy group adoption failed: {error}", file=sys.stderr)
        return 1
    except Exception:
        print(
            "legacy group adoption failed; inspect target before retrying",
            file=sys.stderr,
        )
        traceback.print_exc()
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
