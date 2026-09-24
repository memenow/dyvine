"""Verify a frozen account against complete, group-level Feishu file evidence.

Both proofs compare multisets of upload names. The group proof covers every
file in the chat; the window proof covers only media posted after the queue
cutoff, which is all the weekly runner can ever re-send. Neither associates
an old message with an individual media path or infers an old topic parent.
The Feishu adoption plan instead treats the chat as the record of what was
sent and says which ledger rows to add or demote so the ledger agrees.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from dyvine.db.delivery_ledger import POST_LEVEL_SLOT
from dyvine.db.models import DeliveryFileRow, DeliveryGroupRow, DownloadQueueRow
from dyvine.services.delivery import (
    LONG_NAME_CHARS,
    legacy_upload_file_name,
    post_datetime_from_path,
)
from dyvine_hermes.weekly_state import entry_cutoff
from scripts.feishu_audit_core import _digest


@dataclass(frozen=True)
class GroupAttestation:
    """A group-wide count match, without path-to-message attribution."""

    audit_sha256: str
    target_source_sha256: str
    historical_file_count: int
    legacy_user_failed_unknown: bool = False


@dataclass(frozen=True)
class WindowAttestation:
    """A download-window count match, without path-to-message attribution."""

    audit_sha256: str
    target_source_sha256: str
    cutoff: str
    window_file_count: int


@dataclass(frozen=True)
class AdoptedFile:
    """A chat file the ledger lacks, under the path the runner's dedupe matches."""

    relative_path: str
    message_ids: tuple[str, ...]
    precision: str


@dataclass(frozen=True)
class FeishuAdoption:
    """The audited chat as the record of what was sent, within one scope."""

    audit_sha256: str
    target_source_sha256: str
    scope: str
    cutoff: str | None
    adopt: tuple[AdoptedFile, ...]
    demote: tuple[str, ...]

    def payload(self) -> dict[str, Any]:
        """Return the plan as a proposal must carry it, for exact comparison."""
        return {
            "scope": self.scope,
            "cutoff": self.cutoff,
            "adopt": [
                {
                    "relative_path": item.relative_path,
                    "message_ids": list(item.message_ids),
                    "precision": item.precision,
                }
                for item in self.adopt
            ],
            "demote": list(self.demote),
        }


@dataclass(frozen=True)
class AuditJournal:
    """Selected private journal rows and a digest of the complete file."""

    sha256: str
    manifest_source_sha256: str
    rows: dict[str, list[dict[str, Any]]] | None = None
    path: Path | None = None
    index: dict[str, list[tuple[int, int]]] | None = None
    size: int = 0
    mtime_ns: int = 0

    def rows_for(self, key: str) -> list[dict[str, Any]]:
        """Load one account's pages after checking the indexed file is stable."""
        if self.rows is not None:
            return self.rows.get(key, [])
        if self.path is None or self.index is None:
            return []
        details = self.path.stat()
        if (details.st_size, details.st_mtime_ns) != (self.size, self.mtime_ns):
            raise ValueError("Feishu audit journal changed after indexing")
        result: list[dict[str, Any]] = []
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path, flags)
        try:
            for offset, length in self.index.get(key, []):
                payload = os.pread(descriptor, length, offset)
                if len(payload) != length:
                    raise ValueError("Feishu audit journal page changed after indexing")
                row = json.loads(payload)
                if not isinstance(row, dict) or row.get("key") != key:
                    raise ValueError("Feishu audit journal index no longer matches")
                result.append(row)
        finally:
            os.close(descriptor)
        return result


def read_audit_journal(
    path: Path, keys: set[str], *, reject_unselected: bool = False
) -> AuditJournal:
    """Read selected accounts while hashing every byte of the audit journal."""
    digest = hashlib.sha256()
    selected: dict[str, list[tuple[int, int]]] = defaultdict(list)
    manifest: dict[str, Any] | None = None
    with path.open("rb") as source:
        number = 0
        while True:
            offset = source.tell()
            line = source.readline()
            if not line:
                break
            number += 1
            digest.update(line)
            if not line.endswith(b"\n"):
                raise ValueError("Feishu audit journal has an incomplete final row")
            try:
                row = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValueError(
                    f"invalid Feishu audit JSONL at line {number}"
                ) from error
            if not isinstance(row, dict):
                raise ValueError("Feishu audit journal has a non-object row")
            if number == 1:
                manifest = row
            elif row.get("type") == "manifest":
                raise ValueError("Feishu audit journal has multiple manifests")
            if (
                reject_unselected
                and row.get("type") != "manifest"
                and row.get("key") not in keys
            ):
                raise ValueError(
                    "supplemental audit contains a key outside its keys file"
                )
            if row.get("key") in keys:
                selected[str(row["key"])].append((offset, len(line)))
        details = os.fstat(source.fileno())
    if (
        manifest is None
        or manifest.get("type") != "manifest"
        or manifest.get("schema") != 1
        or not isinstance(manifest.get("source_sha256"), str)
    ):
        raise ValueError("Feishu audit journal has an invalid manifest")
    return AuditJournal(
        digest.hexdigest(),
        manifest["source_sha256"],
        path=path,
        index=dict(selected),
        size=details.st_size,
        mtime_ns=details.st_mtime_ns,
    )


def read_work_chats(path: Path) -> tuple[dict[tuple[str, str], set[str]], str]:
    """Read staged progress chat IDs without modifying the SQLite checkpoint."""
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        version = connection.execute(
            "SELECT value FROM work_meta WHERE key = 'schema_version'"
        ).fetchone()
        if version is None or version[0] != "8":
            raise ValueError("legacy work database has an unsupported schema")
        sources = connection.execute(
            "SELECT path, size, mtime_ns FROM source_files ORDER BY path"
        ).fetchall()
        if not sources:
            raise ValueError("legacy work database has no staged progress files")
        for source_path, size, mtime_ns in sources:
            stat = Path(source_path).stat()
            if (stat.st_size, stat.st_mtime_ns) != (size, mtime_ns):
                raise ValueError("a staged progress source changed after import")
        stats = connection.execute(
            """SELECT source_file, round, nickname, chat_id, failed, sent, total
               FROM source_user_stats ORDER BY source_file, round, nickname"""
        ).fetchall()
        chats: dict[tuple[str, str], set[str]] = defaultdict(set)
        for _source, round_name, nickname, chat_id, *_counts in stats:
            if isinstance(chat_id, str) and chat_id:
                chats[(round_name, nickname)].add(chat_id)
        fingerprint = connection.execute(
            "SELECT value FROM work_meta WHERE key = 'identity_fingerprint'"
        ).fetchone()
        return dict(chats), _digest(
            {
                "sources": sources,
                "stats": stats,
                "identity_fingerprint": fingerprint[0] if fingerprint else None,
            }
        )
    finally:
        connection.close()


def _complete_pages(
    rows: list[dict[str, Any]], chat_id: str, topic_thread_id: str | None
) -> tuple[list[dict[str, Any]], str | None]:
    scopes: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("type") != "page":
            continue
        container_type = row.get("container_type")
        container_id = row.get("container_id")
        if container_type not in {"chat", "thread"} or not isinstance(
            container_id, str
        ):
            return [], "audit page has an invalid scope"
        scopes[(container_type, container_id)].append(row)
    if ("chat", chat_id) not in scopes or any(
        scope_type == "chat" and scope_id != chat_id for scope_type, scope_id in scopes
    ):
        return [], "complete chat history is absent"
    discovered_threads: set[str] = set()
    pages: list[dict[str, Any]] = []
    for (scope_type, _scope_id), scope_pages in scopes.items():
        cursors = {row.get("request_page_token"): row for row in scope_pages}
        if len(cursors) != len(scope_pages) or None not in cursors:
            return [], "audit pagination has a gap or duplicate cursor"
        visited: set[str | None] = set()
        cursor: str | None = None
        while cursor not in visited:
            page = cursors.get(cursor)
            if page is None:
                return [], "audit pagination has a missing page"
            visited.add(cursor)
            pages.append(page)
            if not isinstance(page.get("files"), list):
                return [], "audit page lacks its file list"
            if scope_type == "chat":
                threads = page.get("threads")
                if not isinstance(threads, list):
                    return [], "chat audit page lacks its thread list"
                for thread in threads:
                    if not isinstance(thread, dict) or not isinstance(
                        thread.get("thread_id"), str
                    ):
                        return [], "chat audit page has an invalid thread"
                    discovered_threads.add(thread["thread_id"])
            next_cursor = page.get("next_page_token")
            if next_cursor is None:
                break
            if not isinstance(next_cursor, str) or not next_cursor:
                return [], "audit pagination has an invalid cursor"
            cursor = next_cursor
        else:
            return [], "audit pagination cursor repeated"
        if len(visited) != len(scope_pages):
            return [], "audit pagination has an unlinked page"
    if topic_thread_id:
        discovered_threads.add(topic_thread_id)
    if {scope_id for scope_type, scope_id in scopes if scope_type == "thread"} != (
        discovered_threads
    ):
        return [], "not every historical thread was audited"
    return pages, None


def _source_digest(
    original: dict[str, Any],
    group: DeliveryGroupRow,
    current_queues: list[DownloadQueueRow],
    current_files: list[DeliveryFileRow],
    keys_file_sha256: str | None = None,
) -> str:
    """Mirror the read-only audit's frozen Postgres target fingerprint."""
    source = {
        "key": original["key"],
        "group": {
            "round": group.round,
            "sec_user_id": group.sec_user_id,
            "nickname": group.nickname,
            "status": group.status,
            "topic_status": group.topic_status,
            "chat_id": group.chat_id,
            "topic_message_id": group.topic_message_id,
        },
        "queues": [
            (queue.key, queue.nickname, queue.chat_id, queue.status)
            for queue in current_queues
        ],
        "files": sorted(
            [
                {
                    "media_id": file.media_id,
                    "relative_path": file.relative_path,
                    "status": file.status,
                    "file_key": file.file_key,
                    "message_id": file.message_id,
                    "chat_id": file.chat_id,
                    "parent_id": file.parent_id,
                }
                for file in current_files
            ],
            key=lambda item: str(item["media_id"]),
        ),
        "legacy": original,
    }
    if keys_file_sha256 is not None:
        source["keys_file_sha256"] = keys_file_sha256
    return _digest(source)


_EXPLAINED_DISCREPANCIES = frozenset(
    {
        "legacy_safe_sent_count_vs_app_topic_files",
        "legacy_safe_sent_count_vs_app_group_files",
        "app_files_outside_verified_topic",
        "zero_file_group",
    }
)


def _single_chat_issue(
    *,
    report: dict[str, Any],
    group: DeliveryGroupRow,
    current_queues: list[DownloadQueueRow],
    historical_queues: list[DownloadQueueRow],
    all_report_rows: list[dict[str, Any]],
    work_chats: dict[tuple[str, str], set[str]],
    known_aliases: set[str] | None,
) -> str | None:
    """Require every historical send of this account to share one chat."""
    sec = report["sec_user_id"]
    chat_id = group.chat_id
    if not chat_id or len(current_queues) != 1:
        return "current queue or group is not unique"
    aliases = set(known_aliases or ())
    aliases.update(row.nickname for row in historical_queues)
    aliases.update(
        row["nickname"] for row in all_report_rows if row.get("sec_user_id") == sec
    )
    queue_chats = {row.chat_id for row in historical_queues if row.chat_id}
    owners_by_round: dict[tuple[str, str], set[str]] = defaultdict(set)
    owners_by_name: dict[str, set[str]] = defaultdict(set)
    for item in all_report_rows:
        round_name = item.get("round")
        nickname = item.get("nickname")
        owner = item.get("sec_user_id")
        if (
            isinstance(round_name, str)
            and round_name
            and isinstance(nickname, str)
            and nickname
            and isinstance(owner, str)
            and owner
        ):
            owners_by_round[(round_name, nickname)].add(owner)
            owners_by_name[nickname].add(owner)
    progress_chats: set[str] = set()
    for (round_name, nickname), chats in work_chats.items():
        if nickname not in aliases:
            continue
        owners = owners_by_round.get((round_name, nickname)) or owners_by_name.get(
            nickname, set()
        )
        if owners == {sec}:
            progress_chats.update(chats)
        elif sec in owners or not owners:
            return "historical nickname chat cannot be attributed to one account"
    if queue_chats | progress_chats != {chat_id}:
        return "account has another or unverified historical chat"
    return None


def _safe_count(row: dict[str, Any]) -> int | None:
    safe = row.get("first_seen_safe_sent_paths")
    if not isinstance(safe, int) or isinstance(safe, bool) or safe < 0:
        return None
    return safe


def _ledger_issue(
    historical_files: list[DeliveryFileRow],
    sec: str,
    chat_id: str | None,
    safe_total: int,
) -> str | None:
    if safe_total != len(historical_files) or any(
        file.status != "legacy_confirmed_sent"
        or file.sec_user_id != sec
        or file.chat_id not in (None, chat_id)
        for file in historical_files
    ):
        return "imported historical file ledger is incomplete or conflicting"
    return None


def _audited_messages(
    *,
    report: dict[str, Any],
    group: DeliveryGroupRow,
    journal: AuditJournal,
    target_source: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]] | str:
    """Deduplicated file messages of a complete audit bound to this target."""
    key = report["key"]
    sec = report["sec_user_id"]
    chat_id = group.chat_id
    rows = journal.rows_for(key)
    accounts = [row for row in rows if row.get("type") == "account"]
    if not accounts or accounts[-1].get("scan_complete") is not True:
        return "Feishu account audit is incomplete"
    account = accounts[-1]
    if any(row.get("source_sha256") != target_source for row in rows):
        return "Feishu audit target no longer matches Postgres and frozen report"
    if (
        account.get("send_blocked") is not True
        or account.get("key") != key
        or account.get("round") != report["round"]
        or account.get("sec_user_id") != sec
        or account.get("nickname") != report["nickname"]
        or account.get("chat_id") != chat_id
        or account.get("topic_message_id") != group.topic_message_id
        or account.get("legacy_safe_sent_paths")
        != report.get("first_seen_safe_sent_paths")
    ):
        return "Feishu audit account identity or history count differs"
    pages, page_issue = _complete_pages(rows, chat_id, account.get("thread_id"))
    if page_issue:
        return page_issue
    files_by_message: dict[str, dict[str, Any]] = {}
    for page in pages:
        for file in page["files"]:
            if not isinstance(file, dict):
                return "Feishu audit has an invalid file message"
            if file.get("chat_id") not in (None, chat_id):
                return "Feishu audit file belongs to another chat"
            message_id = file.get("message_id")
            if not isinstance(message_id, str) or not message_id:
                return "Feishu audit has a file without a message ID"
            prior = files_by_message.get(message_id)
            signature = (
                file.get("file_name"),
                file.get("file_key"),
                file.get("sender_type"),
                file.get("sender_id"),
                file.get("deleted"),
            )
            if prior is not None and prior["signature"] != signature:
                return "Feishu audit has conflicting copies of a file message"
            files_by_message[message_id] = {"signature": signature, "file": file}
    return [item["file"] for item in files_by_message.values()], account


def _audited_app_files(
    *,
    report: dict[str, Any],
    group: DeliveryGroupRow,
    journal: AuditJournal,
    target_source: str,
) -> list[dict[str, Any]] | str:
    """Named, non-deleted files one app sent to the chat, per a complete audit."""
    audited = _audited_messages(
        report=report, group=group, journal=journal, target_source=target_source
    )
    if isinstance(audited, str):
        return audited
    messages, account = audited
    if any(not isinstance(file.get("deleted"), bool) for file in messages):
        return "Feishu audit has a file message without a deletion state"
    active = [file for file in messages if not file["deleted"]]
    app_files = [file for file in active if file.get("sender_type") == "app"]
    if any(
        not isinstance(file.get("sender_id"), str)
        or not file.get("sender_id")
        or not isinstance(file.get("file_name"), str)
        or not file.get("file_name")
        for file in app_files
    ):
        return "Feishu audit has an unnamed or unattributed app file"
    if len({file["sender_id"] for file in app_files}) > 1:
        return "Feishu audit files have multiple app senders"
    if account.get("group_file_count") != len(active) or account.get(
        "app_group_file_count"
    ) != len(app_files):
        return "Feishu audit group file counts differ from raw pages"
    issue = _discrepancy_issue(account)
    if issue:
        return issue
    return app_files


def _discrepancy_issue(account: dict[str, Any]) -> str | None:
    discrepancies = account.get("discrepancies")
    if not isinstance(discrepancies, list) or any(
        item not in _EXPLAINED_DISCREPANCIES for item in discrepancies
    ):
        return "Feishu audit has an unexplained discrepancy"
    return None


def attest_group(
    *,
    report: dict[str, Any],
    original: dict[str, Any],
    all_report_rows: list[dict[str, Any]],
    group: DeliveryGroupRow,
    current_queues: list[DownloadQueueRow],
    current_files: list[DeliveryFileRow],
    historical_queues: list[DownloadQueueRow],
    historical_files: list[DeliveryFileRow],
    work_chats: dict[tuple[str, str], set[str]],
    journal: AuditJournal,
    known_aliases: set[str] | None = None,
    keys_file_sha256: str | None = None,
) -> GroupAttestation | str:
    """Require a complete one-chat file-name multiset proof for this account."""
    sec = report["sec_user_id"]
    chat_id = group.chat_id
    issue = _single_chat_issue(
        report=report,
        group=group,
        current_queues=current_queues,
        historical_queues=historical_queues,
        all_report_rows=all_report_rows,
        work_chats=work_chats,
        known_aliases=known_aliases,
    )
    if issue:
        return issue
    scoped = [row for row in all_report_rows if row.get("sec_user_id") == sec]
    safe_total = 0
    failed_unknown = False
    for row in scoped:
        for field in (
            "ambiguous_sent_paths",
            "cache_only_unverified_paths",
            "permanent_failure_paths",
            "permanent_unresolved_paths",
            "failed_paths",
            "snapshot_failed_entries",
        ):
            if row.get(field) != 0 or isinstance(row.get(field), bool):
                return f"account has unresolved historical {field}"
        if "legacy_user_failed_max" not in row:
            return "account lacks a historical failure summary field"
        if row["legacy_user_failed_max"] is None:
            failed_unknown = True
        elif row["legacy_user_failed_max"] != 0 or isinstance(
            row["legacy_user_failed_max"], bool
        ):
            return "account has reported historical download failures"
        safe = _safe_count(row)
        if safe is None:
            return "account has an invalid safe-sent count"
        safe_total += safe
    issue = _ledger_issue(historical_files, sec, chat_id, safe_total)
    if issue:
        return issue
    target_source = _source_digest(
        original, group, current_queues, current_files, keys_file_sha256
    )
    audited = _audited_messages(
        report=report, group=group, journal=journal, target_source=target_source
    )
    if isinstance(audited, str):
        return audited
    messages, account = audited
    if any(
        file.get("deleted") is not False
        or file.get("sender_type") != "app"
        or not isinstance(file.get("sender_id"), str)
        or not file.get("sender_id")
        or file.get("chat_id") not in (None, chat_id)
        or not isinstance(file.get("file_name"), str)
        or not file.get("file_name")
        for file in messages
    ):
        return "Feishu audit has deleted, non-app, or unnamed file messages"
    if len({file["sender_id"] for file in messages}) > 1:
        return "Feishu audit files have multiple app senders"
    if account.get("group_file_count") != len(messages) or account.get(
        "app_group_file_count"
    ) != len(messages):
        return "Feishu audit group file counts differ from raw pages"
    issue = _discrepancy_issue(account)
    if issue:
        return issue
    expected = Counter(
        legacy_upload_file_name(Path(file.relative_path)) for file in historical_files
    )
    actual = Counter(file["file_name"] for file in messages)
    if expected != actual:
        return "Feishu group file-name multiset differs from imported history"
    if bool(account.get("zero_file_group")) != (not messages):
        return "Feishu zero-file finding differs from raw pages"
    return GroupAttestation(
        journal.sha256, target_source, len(historical_files), failed_unknown
    )


def _posted(name: str) -> datetime | None:
    """Post time exactly as the weekly runner reads it from a media path."""
    root = Path("/")
    return post_datetime_from_path(root / name, root)


def attest_window(
    *,
    report: dict[str, Any],
    original: dict[str, Any],
    all_report_rows: list[dict[str, Any]],
    group: DeliveryGroupRow,
    queue: DownloadQueueRow,
    current_queues: list[DownloadQueueRow],
    current_files: list[DeliveryFileRow],
    historical_queues: list[DownloadQueueRow],
    historical_files: list[DeliveryFileRow],
    work_chats: dict[tuple[str, str], set[str]],
    journal: AuditJournal,
    timezone: str,
    known_aliases: set[str] | None = None,
    keys_file_sha256: str | None = None,
) -> WindowAttestation | str:
    """Require Feishu and the legacy ledger to agree inside the download window.

    The weekly runner re-sends only media posted after the queue cutoff and
    skips anything the legacy ledger records, so the ledger must name
    exactly the app files already in the chat for that window: an extra
    Feishu file would be sent twice, and an extra ledger file would never
    be sent. Older rounds in the same chat are out of the runner's reach.
    Ambiguous or unverified legacy sends need no separate proof here,
    because the chat itself shows whether each one arrived.
    """
    sec = report["sec_user_id"]
    chat_id = group.chat_id
    issue = _single_chat_issue(
        report=report,
        group=group,
        current_queues=current_queues,
        historical_queues=historical_queues,
        all_report_rows=all_report_rows,
        work_chats=work_chats,
        known_aliases=known_aliases,
    )
    if issue:
        return issue
    safe_total = 0
    for row in all_report_rows:
        if row.get("sec_user_id") != sec:
            continue
        safe = _safe_count(row)
        if safe is None:
            return "account has an invalid safe-sent count"
        safe_total += safe
    issue = _ledger_issue(historical_files, sec, chat_id, safe_total)
    if issue:
        return issue
    if queue.mode != "incremental" or not queue.cutoff:
        return "window attestation needs an incremental queue cutoff"
    cutoff = entry_cutoff(queue, timezone)
    assert cutoff is not None
    target_source = _source_digest(
        original, group, current_queues, current_files, keys_file_sha256
    )
    app_files = _audited_app_files(
        report=report, group=group, journal=journal, target_source=target_source
    )
    if isinstance(app_files, str):
        return app_files
    in_chat: Counter[str] = Counter()
    for file in app_files:
        posted = _posted(file["file_name"])
        if posted is None:
            return "Feishu app file name has no post time"
        if posted > cutoff:
            in_chat[file["file_name"]] += 1
    in_ledger: Counter[str] = Counter()
    for item in historical_files:
        posted = _posted(item.relative_path)
        if posted is not None and posted > cutoff:
            in_ledger[legacy_upload_file_name(Path(item.relative_path))] += 1
    if in_chat != in_ledger:
        return "Feishu in-window files differ from the legacy ledger"
    return WindowAttestation(
        journal.sha256, target_source, cutoff.isoformat(), sum(in_ledger.values())
    )


# f2 media slots that close an untruncated file name, such as ``_image_3.webp``.
_MEDIA_SLOT_SUFFIX = re.compile(
    r"_(?:video|image_\d+|live_\d+|cover|music)\.[A-Za-z0-9]+$"
)


def _adoption_path(name: str) -> tuple[str, str]:
    """Map a chat file name to a ledger path and its precision.

    An untruncated f2 name still ends in its media slot, and its folder is the
    name without that slot, so the path is exact. A shortened name lost the
    slot; its post is recorded at ``POST_LEVEL_SLOT``, which covers every
    media of that post.
    """
    match = _MEDIA_SLOT_SUFFIX.search(name)
    if len(name) <= LONG_NAME_CHARS and match:
        return f"{name[: match.start()]}/{name}", "exact"
    stamp = name[:19]
    return f"{stamp}_feishu/{stamp}_feishu{POST_LEVEL_SLOT}", "post"


def plan_feishu_adoption(
    *,
    report: dict[str, Any],
    original: dict[str, Any],
    all_report_rows: list[dict[str, Any]],
    group: DeliveryGroupRow,
    queue: DownloadQueueRow,
    current_queues: list[DownloadQueueRow],
    current_files: list[DeliveryFileRow],
    historical_queues: list[DownloadQueueRow],
    historical_files: list[DeliveryFileRow],
    work_chats: dict[tuple[str, str], set[str]],
    journal: AuditJournal,
    timezone: str,
    known_aliases: set[str] | None = None,
    keys_file_sha256: str | None = None,
) -> FeishuAdoption | str:
    """Plan the ledger changes that make it agree with the audited chat.

    The chat is the record of what the legacy sender delivered. Chat files
    the ledger lacks are adopted: an untruncated name at its exact path, a
    shortened one for its whole post. Ledger records the chat does not hold
    are demoted so the runner sends them again; a shortened ledger name
    counts as held while the chat has any file of its post. The scope is
    what delivery sends: media after the queue cutoff when the row has one,
    whatever its download mode, or the whole feed otherwise.
    """
    sec = report["sec_user_id"]
    chat_id = group.chat_id
    issue = _single_chat_issue(
        report=report,
        group=group,
        current_queues=current_queues,
        historical_queues=historical_queues,
        all_report_rows=all_report_rows,
        work_chats=work_chats,
        known_aliases=known_aliases,
    )
    if issue:
        return issue
    if any(
        file.sec_user_id != sec or file.chat_id not in (None, chat_id)
        for file in historical_files
    ):
        return "imported historical file ledger belongs to another account or chat"
    cutoff = entry_cutoff(queue, timezone)
    target_source = _source_digest(
        original, group, current_queues, current_files, keys_file_sha256
    )
    app_files = _audited_app_files(
        report=report, group=group, journal=journal, target_source=target_source
    )
    if isinstance(app_files, str):
        return app_files
    if any(_posted(file["file_name"]) is None for file in app_files):
        return "Feishu app file name has no post time"

    def in_scope(name: str) -> bool:
        posted = _posted(name)
        return posted is not None and (cutoff is None or posted > cutoff)

    chat = sorted(
        (file for file in app_files if in_scope(file["file_name"])),
        key=lambda file: (file["file_name"], file["message_id"]),
    )
    ledger = sorted(
        (
            item
            for item in historical_files
            if item.status == "legacy_confirmed_sent" and in_scope(item.relative_path)
        ),
        key=lambda item: item.relative_path,
    )
    ledger_names = Counter(
        legacy_upload_file_name(Path(item.relative_path)) for item in ledger
    )
    chat_names = Counter(file["file_name"] for file in chat)
    spare = chat_names - ledger_names
    adopted: dict[str, tuple[list[str], str]] = {}
    for file in chat:
        name = file["file_name"]
        if spare[name] <= 0:
            continue
        spare[name] -= 1
        path, precision = _adoption_path(name)
        adopted.setdefault(path, ([], precision))[0].append(file["message_id"])
    chat_posts = {name[:19] for name in chat_names}
    missing = ledger_names - chat_names
    demote: list[str] = []
    for item in ledger:
        name = legacy_upload_file_name(Path(item.relative_path))
        if missing[name] <= 0:
            continue
        if name != Path(item.relative_path).name and name[:19] in chat_posts:
            # A shortened name cannot say which media of the post the chat has.
            continue
        missing[name] -= 1
        demote.append(item.media_id)
    return FeishuAdoption(
        audit_sha256=journal.sha256,
        target_source_sha256=target_source,
        scope="window" if cutoff else "full",
        cutoff=cutoff.isoformat() if cutoff else None,
        adopt=tuple(
            AdoptedFile(path, tuple(ids), precision)
            for path, (ids, precision) in sorted(adopted.items())
        ),
        demote=tuple(sorted(demote)),
    )
