"""Derive exact legacy group candidates from frozen queue and topic evidence."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.feishu_audit_core import _nonempty


@dataclass(frozen=True, slots=True)
class Candidate:
    """One queue row and the exact legacy topic it may own."""

    row_id: str
    round: str | None
    sec_user_id: str | None
    nickname: str | None
    chat_id: str | None
    topic_message_id: str | None
    source_file: str | None
    issue: str | None


def _topic_identity(raw: str) -> tuple[str, str] | None:
    nickname, separator, suffix = raw.rpartition(":oc_")
    if (
        not separator
        or not nickname
        or not suffix
        or any(ch.isspace() for ch in suffix)
    ):
        return None
    return nickname, f"oc_{suffix}"


def _unmatched_row(**fields: Any) -> dict[str, Any]:
    payload = json.dumps(fields, ensure_ascii=False, sort_keys=True).encode()
    return {
        "type": "unmatched_topic",
        "row_id": f"topic:{hashlib.sha256(payload).hexdigest()}",
        **fields,
    }


def _load_sources(
    queue_path: Path,
    work_db: Path,
    seed_path: Path | None,
    app_id: str,
    expected_owner: str | None,
    scope_round: str | None,
) -> tuple[list[Any], list[tuple[str, str, str, str]], str]:
    queue_bytes = queue_path.read_bytes()
    queue = json.loads(queue_bytes)
    if not isinstance(queue, dict) or not isinstance(queue.get("entries"), list):
        raise ValueError("frozen queue must contain an entries list")
    seed_bytes = seed_path.read_bytes() if seed_path else b""
    if seed_path:
        seeds = json.loads(seed_bytes)
        if isinstance(seeds, dict):
            seeds = seeds.get("users")
        if not isinstance(seeds, list):
            raise ValueError("seed source must be a list or users object")
    else:
        seeds = []
    connection = sqlite3.connect(
        f"{work_db.resolve(strict=True).as_uri()}?mode=ro", uri=True
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        source_files = [
            tuple(row)
            for row in connection.execute(
                "SELECT path, size, mtime_ns FROM source_files ORDER BY path"
            )
        ]
        staged = {str(path) for path, _, _ in source_files}
        for path, size, mtime_ns in source_files:
            current = Path(str(path)).stat()
            if (current.st_size, current.st_mtime_ns) != (size, mtime_ns):
                raise ValueError("progress source changed since staging")
        topics: list[tuple[str, str, str, str]] = [
            (str(row[0]), str(row[1]), str(row[2]), str(row[3]))
            for row in connection.execute(
                """SELECT source_file, round, nickname, topic_message_id
                   FROM source_topics
                   ORDER BY source_file, round, nickname, topic_message_id"""
            )
        ]
        if any(source_file not in staged for source_file, _, _, _ in topics):
            raise ValueError("topic source was not checkpointed")
        identity = connection.execute(
            "SELECT value FROM work_meta WHERE key = 'identity_fingerprint'"
        ).fetchone()
        if identity is None:
            raise ValueError("work database lacks an identity fingerprint")
    finally:
        connection.close()
    source = {
        "queue_sha256": hashlib.sha256(queue_bytes).hexdigest(),
        "seed_sha256": hashlib.sha256(seed_bytes).hexdigest() if seed_path else None,
        "source_files": source_files,
        "topics": topics,
        "identity_fingerprint": identity[0],
        "app_id": app_id,
        "expected_owner": expected_owner,
        "scope_round": scope_round,
    }
    fingerprint = hashlib.sha256(
        json.dumps(source, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    return list(queue["entries"]), topics, fingerprint


def _candidates(
    entries: list[Any],
    topics: list[tuple[str, str, str, str]],
) -> tuple[list[Candidate], list[dict[str, Any]]]:
    """Require a unique account, chat, and exact nickname/chat topic per round."""
    parsed: list[tuple[str | None, str | None, str | None, str | None]] = []
    by_sec: dict[tuple[str, str], list[int]] = defaultdict(list)
    by_chat: dict[tuple[str, str], list[int]] = defaultdict(list)
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            parsed.append((None, None, None, None))
            continue
        queue_round, queue_sec, queue_nickname, queue_chat = (
            _nonempty(entry.get("round")),
            _nonempty(entry.get("sec_user_id")),
            _nonempty(entry.get("nickname")),
            _nonempty(entry.get("chat_id")),
        )
        parsed.append((queue_round, queue_sec, queue_nickname, queue_chat))
        if queue_round and queue_sec:
            by_sec[(queue_round, queue_sec)].append(index)
        if queue_round and queue_chat:
            by_chat[(queue_round, queue_chat)].append(index)
    by_topic: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    topic_sources: dict[tuple[str, str, str, str], set[str]] = defaultdict(set)
    unmatched: list[dict[str, Any]] = []
    for source_file, topic_round, raw_name, source_topic in topics:
        identity = _topic_identity(raw_name)
        if identity is None:
            unmatched.append(
                _unmatched_row(
                    round=topic_round,
                    raw_name=raw_name,
                    topic_message_id=source_topic,
                    source_file=source_file,
                    reason="topic_key_has_no_chat_id",
                )
            )
            continue
        topic_nickname, topic_chat = identity
        key = (topic_round, topic_nickname, topic_chat)
        by_topic[key].add(source_topic)
        topic_sources[(*key, source_topic)].add(source_file)
    candidates: list[Candidate] = []
    matching_topics: set[tuple[str, str, str]] = set()
    for index, (round_name, sec, nickname, chat) in enumerate(parsed):
        issue: str | None = None
        matched_topic: str | None = None
        matched_source: str | None = None
        if not all((round_name, sec, nickname, chat)):
            issue = "incomplete_queue_identity"
        else:
            assert round_name and sec and nickname and chat
            key = (round_name, nickname, chat)
            matching_topics.add(key)
            if len(by_sec[(round_name, sec)]) != 1:
                issue = "round_account_has_multiple_queue_rows"
            elif len(by_chat[(round_name, chat)]) != 1:
                issue = "round_chat_has_multiple_queue_rows"
            elif len(by_topic.get(key, set())) != 1:
                issue = "missing_or_conflicting_topic"
            else:
                matched_topic = next(iter(by_topic[key]))
                matched_source = min(topic_sources[(*key, matched_topic)])
        candidates.append(
            Candidate(
                f"queue:{index}",
                round_name,
                sec,
                nickname,
                chat,
                matched_topic,
                matched_source,
                issue,
            )
        )
    for (round_name, nickname, chat), topic_ids in sorted(by_topic.items()):
        if (round_name, nickname, chat) in matching_topics:
            continue
        for unmatched_topic in sorted(topic_ids):
            unmatched.append(
                _unmatched_row(
                    round=round_name,
                    nickname=nickname,
                    chat_id=chat,
                    topic_message_id=unmatched_topic,
                    source_file=min(
                        topic_sources[(round_name, nickname, chat, unmatched_topic)]
                    ),
                    reason="no_exact_queue_identity",
                )
            )
    return candidates, unmatched
