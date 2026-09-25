"""Read-only Feishu audit of the older chats an account used.

An account whose queue rows or legacy progress name another chat than its
current group stays held, because that chat may hold sends the runner would
repeat. This audit reads each such older chat once: its Feishu status (a
dissolved chat keeps its history from its members) and the name of every
live app-sent file in its complete history, threads included. The Feishu
adoption proposal and its apply take the journal as ``--other-chat-audit``
and clear an older chat that is dissolved or holds no file inside the row's
delivery scope.

The journal contains private chat identifiers and file names. Give
``--output`` a new operator-controlled path; it is created with mode 0600
and binds the frozen report and round. The keys file must be an owned 0600
file of exact queue keys. This command never sends messages or writes
Postgres.

Example::

    PYTHONPATH=src uv run python scripts/audit_other_chats.py \
      --legacy-report <private-reconciliation.jsonl> --round weekly0913 \
      --legacy-work-db <private-staging.sqlite3> \
      --keys-file <private-queue-keys.txt> \
      --output <private-other-chat-audit.jsonl>

``DATABASE_URL`` and the Feishu app credentials are read from the environment
(or the Hermes environment file for Feishu credentials).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from collections import Counter
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from dyvine.db.models import DeliveryGroupRow, DownloadQueueRow  # noqa: E402
from scripts.adopt_legacy_groups import GroupReader, ReadableGroups  # noqa: E402
from scripts.audit_feishu_delivery import _load_keys_file  # noqa: E402
from scripts.feishu_audit_core import (  # noqa: E402
    AuditError,
    Journal,
    _digest,
    _file_message,
    _nonempty,
)
from scripts.queue_group_attestation import (  # noqa: E402
    historical_chats,
    other_chat_source_digest,
    read_work_chats,
)
from scripts.queue_reconciliation_policy import _report_rows  # noqa: E402

_MAX_PAGES = 2000
_DISSOLVED = frozenset({"dissolved", "dissolved_save"})

# The account's current group chat, every queue row, and its seed aliases.
History = tuple[str | None, list[DownloadQueueRow], set[str]]
HistoryLoader = Callable[[dict[str, Any]], Awaitable[History]]


async def _app_file_names(
    reader: ReadableGroups, chat_id: str, app_id: str
) -> tuple[list[str], str, int]:
    """Names of live app files across a chat's complete history and threads."""
    digest = hashlib.sha256()
    names: list[str] = []
    seen: set[str] = set()
    threads: set[str] = set()

    async def scan(kind: str, container: str) -> None:
        token: str | None = None
        tokens: set[str | None] = set()
        for _ in range(_MAX_PAGES):
            if token in tokens:
                raise AuditError("Feishu history cursor repeated")
            tokens.add(token)
            page = await reader.list_messages(kind, container, token)
            items = page.get("items")
            if not isinstance(items, list) or not isinstance(
                page.get("has_more"), bool
            ):
                raise AuditError("Feishu history page is incomplete")
            for item in items:
                if not isinstance(item, dict) or not _nonempty(item.get("message_id")):
                    raise AuditError("Feishu history has a message without an ID")
                if kind == "chat" and (thread := _nonempty(item.get("thread_id"))):
                    threads.add(thread)
                # A thread root is listed by both the chat and its thread.
                if item["message_id"] in seen:
                    continue
                seen.add(item["message_id"])
                digest.update(
                    json.dumps(item, ensure_ascii=False, sort_keys=True).encode()
                )
                digest.update(b"\0")
                file = _file_message(item)
                if (
                    file is not None
                    and not file["deleted"]
                    and (file["sender_type"], file["sender_id"]) == ("app", app_id)
                ):
                    if not file["file_name"]:
                        raise AuditError("Feishu app file message has no name")
                    names.append(file["file_name"])
            if not page["has_more"]:
                return
            next_token = _nonempty(page.get("page_token"))
            if not next_token or next_token == token:
                raise AuditError("Feishu history page token is missing")
            token = next_token
        raise AuditError("Feishu history page limit reached")

    await scan("chat", chat_id)
    for thread in sorted(threads):
        await scan("thread", thread)
    return sorted(names), digest.hexdigest(), len(seen)


async def _audit_chat(
    reader: ReadableGroups, key: str, chat_id: str, app_id: str
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "type": "other_chat",
        "key": key,
        "chat_id": chat_id,
        "chat_status": None,
        "scan_complete": False,
        "app_file_names": [],
    }
    try:
        chat = await reader.get_chat(chat_id)
        if chat.get("chat_id") != chat_id:
            return {**row, "read_error": "Feishu returned another chat"}
        row["chat_status"] = _nonempty(chat.get("chat_status"))
        if row["chat_status"] in _DISSOLVED:
            return row
        names, history_sha256, messages = await _app_file_names(reader, chat_id, app_id)
    except AuditError as error:
        return {**row, "read_error": str(error)}
    return {
        **row,
        "scan_complete": True,
        "app_file_names": names,
        "history_sha256": history_sha256,
        "history_messages": messages,
    }


async def audit(
    args: argparse.Namespace,
    *,
    reader: ReadableGroups,
    load_history: HistoryLoader,
    app_id: str,
) -> dict[str, int]:
    """Journal every older chat of the selected accounts; never write state."""
    keys, _keys_sha256 = _load_keys_file(Path(args.keys_file), args.round)
    rows, report_sha256 = _report_rows(Path(args.legacy_report))
    active = {row["key"]: row for row in rows if row.get("round") == args.round}
    if not keys <= set(active):
        raise AuditError("keys file includes keys absent from the frozen report")
    work_chats, _work_sha256 = read_work_chats(Path(args.legacy_work_db))
    manifest_source = _digest(
        {"legacy_report_sha256": report_sha256, "round": args.round}
    )
    counts: Counter[str] = Counter()
    journal = Journal(
        Path(args.output), other_chat_source_digest(manifest_source), resume=False
    )
    try:
        for key in sorted(keys):
            report = active[key]
            group_chat, queues, aliases = await load_history(report)
            chats = historical_chats(
                report=report,
                historical_queues=queues,
                all_report_rows=rows,
                work_chats=work_chats,
                known_aliases=aliases,
            )
            if isinstance(chats, str) or not group_chat:
                issue = chats if isinstance(chats, str) else "group has no chat"
                journal.append({"type": "account", "key": key, "issue": issue})
                counts["accounts_held"] += 1
                continue
            others = sorted(chats - {group_chat})
            journal.append(
                {
                    "type": "account",
                    "key": key,
                    "group_chat_id": group_chat,
                    "other_chat_ids": others,
                }
            )
            counts["accounts"] += 1
            for chat_id in others:
                row = await _audit_chat(reader, key, chat_id, app_id)
                journal.append(row)
                if row["chat_status"] in _DISSOLVED:
                    counts["dissolved"] += 1
                elif row["scan_complete"]:
                    counts["scanned"] += 1
                else:
                    counts["unreadable"] += 1
    finally:
        journal.close()
    return dict(counts)


def _postgres_history(database_url: str) -> tuple[HistoryLoader, Any]:
    from sqlalchemy import text

    from dyvine.db.session import DatabaseSessionFactory
    from scripts.apply_queue_reconciliation import _account_history

    factory = DatabaseSessionFactory(
        database_url, pool_class="queue", pool_size=1, max_overflow=0
    )

    async def load(report: dict[str, Any]) -> History:
        async with factory.session() as session:
            async with session.begin():
                await session.execute(text("SET TRANSACTION READ ONLY"))
                group = await session.get(DeliveryGroupRow, report["key"])
                queues, _files, aliases = await _account_history(
                    session, report["sec_user_id"], lock=False
                )
        return (group.chat_id if group else None), queues, aliases

    return load, factory


async def run(args: argparse.Namespace) -> dict[str, int]:
    from dyvine.core.exceptions import DeliveryError
    from dyvine.services.delivery import FeishuCredentials

    database_url = os.environ.get(args.database_url_env)
    if not database_url:
        raise AuditError(f"{args.database_url_env} is not set")
    try:
        credentials = FeishuCredentials.from_hermes_default()
    except DeliveryError as error:
        raise AuditError("Feishu credentials are unavailable") from error
    load_history, factory = _postgres_history(database_url)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            return await audit(
                args,
                reader=GroupReader(client, credentials, args.request_interval),
                load_history=load_history,
                app_id=credentials.app_id,
            )
    finally:
        await factory.aclose()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-report", required=True, type=Path)
    parser.add_argument("--round", required=True)
    parser.add_argument("--legacy-work-db", required=True, type=Path)
    parser.add_argument("--keys-file", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--request-interval", type=float, default=0.3)
    parser.add_argument("--database-url-env", default="DATABASE_URL")
    args = parser.parse_args(argv)
    if args.request_interval < 0.25:
        parser.error("--request-interval must be at least 0.25 seconds")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        counts = asyncio.run(run(args))
    except (AuditError, OSError, ValueError) as error:
        print(f"other-chat audit failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(counts, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
