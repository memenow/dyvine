"""Bounded JSON parsing and resumable local staging for legacy send evidence."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, TextIO


@dataclass(frozen=True)
class PathIdentity:
    """Lexical legacy path evidence, with no filesystem access."""

    nickname: str
    owner: str
    relative_path: str


@dataclass
class Report:
    """Counts from a frozen source set and the resulting database writes."""

    sources: int = 0
    staged: int = 0
    reused: int = 0
    sent: int = 0
    failed: int = 0
    sent_unique: int = 0
    failed_unique: int = 0
    sent_duplicates: int = 0
    failed_duplicates: int = 0
    safe_sent: int = 0
    needs_review: int = 0
    inserted: int = 0
    existing: int = 0
    audit_inserted: int = 0
    audit_existing: int = 0
    group_candidates: int = 0
    permanent_paths: int = 0
    permanent_safe: int = 0
    cache_paths: int = 0
    cache_only: int = 0
    permanent_inserted: int = 0
    permanent_existing: int = 0


class JsonStream:
    """Read top-level JSON arrays one element at a time in bounded chunks."""

    def __init__(self, source: TextIO, chunk_size: int = 65536) -> None:
        self.source = source
        self.chunk_size = chunk_size
        self.buffer = ""
        self.position = 0
        self.eof = False
        self.decoder = json.JSONDecoder()
        self.users: dict[str, dict[str, Any]] = {}
        self.user_topics: dict[str, str] = {}

    def _fill(self) -> None:
        chunk = self.source.read(self.chunk_size)
        if chunk:
            self.buffer += chunk
        else:
            self.eof = True

    def _compact(self) -> None:
        if self.position >= self.chunk_size:
            self.buffer = self.buffer[self.position :]
            self.position = 0

    def peek(self) -> str:
        while True:
            while (
                self.position < len(self.buffer)
                and self.buffer[self.position].isspace()
            ):
                self.position += 1
            self._compact()
            if self.position < len(self.buffer):
                return self.buffer[self.position]
            if self.eof:
                return ""
            self._fill()

    def expect(self, character: str) -> None:
        found = self.peek()
        if found != character:
            raise ValueError(f"expected {character!r}, found {found!r}")
        self.position += 1
        self._compact()

    def value(self) -> Any:
        self.peek()
        while True:
            try:
                value, end = self.decoder.raw_decode(self.buffer, self.position)
                self.position = end
                self._compact()
                return value
            except json.JSONDecodeError as exc:
                if self.eof:
                    raise ValueError(f"invalid JSON near offset {exc.pos}") from exc
                if len(self.buffer) - self.position > 8 * 1024 * 1024:
                    raise ValueError(
                        "one JSON value exceeds the 8 MiB safety limit"
                    ) from exc
                self._fill()

    def paths(self) -> Iterator[tuple[str, Any, int]]:
        """Yield entries from sent/failed arrays; validate the full document."""
        self.expect("{")
        found_sent = False
        found_failed = False
        while self.peek() != "}":
            key = self.value()
            if not isinstance(key, str):
                raise ValueError("top-level keys must be strings")
            self.expect(":")
            if key in ("sent", "failed"):
                if key == "sent":
                    found_sent = True
                else:
                    found_failed = True
                self.expect("[")
                position = 0
                while self.peek() != "]":
                    yield key, self.value(), position
                    position += 1
                    if self.peek() != "]":
                        self.expect(",")
                self.expect("]")
            elif key == "users":
                users = self.value()
                if not isinstance(users, dict):
                    raise ValueError("users must be an object")
                self.users = {
                    nickname: value
                    for nickname, value in users.items()
                    if isinstance(nickname, str) and isinstance(value, dict)
                }
            elif key == "user_topics":
                topics = self.value()
                if not isinstance(topics, dict):
                    raise ValueError("user_topics must be an object")
                self.user_topics = {
                    nickname: message_id
                    for nickname, message_id in topics.items()
                    if isinstance(nickname, str)
                    and isinstance(message_id, str)
                    and message_id
                }
            else:
                self.value()
            if self.peek() != "}":
                self.expect(",")
        self.expect("}")
        if self.peek():
            raise ValueError("trailing content after progress document")
        if not found_sent or not found_failed:
            raise ValueError("progress document requires sent and failed arrays")


def _identity(path: str, download_root: PurePosixPath) -> PathIdentity | str:
    """Extract the actual legacy nickname directory without resolving files."""
    if not path or "\x00" in path:
        return "empty or NUL-containing path"
    candidate = PurePosixPath(path)
    if not candidate.is_absolute() or ".." in candidate.parts:
        return "path must be absolute and traversal-free"
    try:
        relative = candidate.relative_to(download_root)
    except ValueError:
        return "path is outside the configured download root"
    parts = relative.parts
    if len(parts) < 5 or parts[0] != "douyin":
        return "path lacks douyin/type/nickname/directory/file layout"
    media_type, nickname = parts[1:3]
    if not media_type or not nickname:
        return "path has an empty media type or nickname"
    return PathIdentity(
        nickname=nickname,
        owner=f"{media_type}/{nickname}",
        relative_path=PurePosixPath(*parts[3:]).as_posix(),
    )


def _round(source: Path) -> str:
    suffix = source.stem.removeprefix("send_progress_")
    return re.sub(r"_w\d+(?:_.*)?$", "", suffix)


def _source_order(source: Path) -> tuple[str, int, str]:
    """Order snapshots by their legacy worker number within each round."""
    match = re.search(r"_w(\d+)(?:_.*)?$", source.stem)
    return (_round(source), int(match.group(1)) if match else -1, source.name)


def _nicknames(queue_path: Path, seed_path: Path) -> dict[str, set[str]]:
    """Preserve conflicting identities so an old nickname is never guessed."""
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    seeds = json.loads(seed_path.read_text(encoding="utf-8"))
    if not isinstance(queue, dict) or not isinstance(queue.get("entries"), list):
        raise ValueError("queue source must contain an entries list")
    if isinstance(seeds, dict):
        seeds = seeds.get("users")
    if not isinstance(seeds, list):
        raise ValueError("seed source must be a list or users object")
    mapping: dict[str, set[str]] = {}
    for item in [*queue["entries"], *seeds]:
        if not isinstance(item, dict):
            continue
        nickname, sec = item.get("nickname"), item.get("sec_user_id")
        if isinstance(nickname, str) and nickname and isinstance(sec, str) and sec:
            mapping.setdefault(nickname, set()).add(sec)
    return mapping


def _work_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=60)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=60000")
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS source_files (
          path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
          sent INTEGER NOT NULL, failed INTEGER NOT NULL,
          sent_unique INTEGER NOT NULL, failed_unique INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sent_paths (
          id INTEGER PRIMARY KEY, legacy_path TEXT NOT NULL UNIQUE,
          source_file TEXT NOT NULL, round TEXT NOT NULL,
          nickname TEXT, owner TEXT, relative_path TEXT,
          sec_user_id TEXT, reason TEXT
        );
        CREATE TABLE IF NOT EXISTS failed_paths (
          id INTEGER PRIMARY KEY, legacy_path TEXT NOT NULL,
          source_file TEXT NOT NULL, round TEXT NOT NULL, nickname TEXT,
          sec_user_id TEXT, reason TEXT,
          UNIQUE (source_file, legacy_path)
        );
        CREATE TABLE IF NOT EXISTS source_account_counts (
          source_file TEXT NOT NULL, round TEXT NOT NULL,
          nickname TEXT NOT NULL, state TEXT NOT NULL,
          entries INTEGER NOT NULL,
          PRIMARY KEY (source_file, state, nickname)
        );
        CREATE TABLE IF NOT EXISTS source_user_stats (
          source_file TEXT NOT NULL, round TEXT NOT NULL,
          nickname TEXT NOT NULL, chat_id TEXT, failed INTEGER,
          sent INTEGER, total INTEGER, done INTEGER,
          PRIMARY KEY (source_file, nickname)
        );
        CREATE TABLE IF NOT EXISTS source_topics (
          source_file TEXT NOT NULL, round TEXT NOT NULL,
          nickname TEXT NOT NULL, topic_message_id TEXT NOT NULL,
          PRIMARY KEY (source_file, nickname)
        );
        CREATE TABLE IF NOT EXISTS work_meta (
          key TEXT PRIMARY KEY, value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS extra_sources (
          path TEXT PRIMARY KEY, kind TEXT NOT NULL,
          size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
          entries INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS permanent_paths (
          id INTEGER PRIMARY KEY, legacy_path TEXT NOT NULL UNIQUE,
          nickname TEXT, owner TEXT, relative_path TEXT,
          sec_user_id TEXT, reason TEXT
        );
        CREATE TABLE IF NOT EXISTS cache_paths (
          id INTEGER PRIMARY KEY, source_file TEXT NOT NULL,
          legacy_path TEXT NOT NULL, round TEXT, nickname TEXT,
          relative_path TEXT, sec_user_id TEXT, reason TEXT,
          UNIQUE (source_file, legacy_path)
        );
        CREATE INDEX IF NOT EXISTS idx_sent_identity
          ON sent_paths (sec_user_id, relative_path);
        CREATE INDEX IF NOT EXISTS idx_permanent_identity
          ON permanent_paths (sec_user_id, relative_path);
        CREATE INDEX IF NOT EXISTS idx_cache_identity
          ON cache_paths (round, sec_user_id, relative_path);
        """)
    version = connection.execute(
        "SELECT value FROM work_meta WHERE key = 'schema_version'"
    ).fetchone()
    if version is None:
        connection.execute("INSERT INTO work_meta VALUES ('schema_version', '8')")
        connection.commit()
    elif version[0] != "8":
        raise ValueError("unsupported work database schema version")
    return connection


def _source_fingerprint(paths: list[Path], root: PurePosixPath) -> str:
    """Pin identity evidence and the lexical root used by staged rows."""
    digest = hashlib.sha256(str(root).encode("utf-8"))
    for path in paths:
        digest.update(b"\0")
        with path.open("rb") as source:
            while chunk := source.read(65536):
                digest.update(chunk)
    return digest.hexdigest()


def _check_work_inputs(connection: sqlite3.Connection, fingerprint: str) -> None:
    existing = connection.execute(
        "SELECT value FROM work_meta WHERE key = 'identity_fingerprint'"
    ).fetchone()
    if existing is None:
        connection.execute(
            "INSERT INTO work_meta VALUES ('identity_fingerprint', ?)",
            (fingerprint,),
        )
        connection.commit()
    elif existing[0] != fingerprint:
        raise ValueError("queue, seed, or download root changed since checkpoint")


def _verify_source_stats(connection: sqlite3.Connection, paths: list[Path]) -> None:
    known = {
        row["path"]: (row["size"], row["mtime_ns"])
        for row in connection.execute("SELECT path, size, mtime_ns FROM source_files")
    }
    for source in paths:
        stat = source.stat()
        if known.get(str(source)) != (stat.st_size, stat.st_mtime_ns):
            raise ValueError(f"source changed after staging: {source}")


def _nonnegative_count(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _stage(
    connection: sqlite3.Connection,
    paths: list[Path],
    mapping: dict[str, set[str]],
    root: PurePosixPath,
    report: Report,
) -> None:
    known = {
        row["path"]: row for row in connection.execute("SELECT * FROM source_files")
    }
    current = {str(path) for path in paths}
    if set(known) - current:
        raise ValueError("a checkpointed progress file is missing from the source set")
    imported = connection.execute(
        "SELECT value FROM work_meta WHERE key = 'import_started'"
    ).fetchone()
    if imported and current - set(known):
        raise ValueError("new source files appeared after database import started")
    nickname_pool = {nickname: nickname for nickname in mapping}
    sent_nicknames: dict[str, str | None] = {
        row[0]: nickname_pool.setdefault(row[1], row[1]) if row[1] is not None else None
        for row in connection.execute("SELECT legacy_path, nickname FROM sent_paths")
    }
    for source in paths:
        stat = source.stat()
        previous = known.get(str(source))
        if previous is not None:
            if (previous["size"], previous["mtime_ns"]) != (
                stat.st_size,
                stat.st_mtime_ns,
            ):
                raise ValueError(f"checkpointed source changed: {source}")
            report.reused += 1
            continue
        sent = failed = sent_unique = failed_unique = 0
        account_counts: dict[tuple[str, str], int] = {}
        connection.execute("BEGIN IMMEDIATE")
        try:
            with source.open("r", encoding="utf-8") as handle:
                parser = JsonStream(handle)
                for state, item, position in parser.paths():
                    if state == "sent":
                        sent += 1
                    else:
                        failed += 1
                    if isinstance(item, str):
                        old_path = item
                        if state == "sent" and old_path in sent_nicknames:
                            nickname = sent_nicknames[old_path]
                            if nickname:
                                count_key = (state, nickname)
                                account_counts[count_key] = (
                                    account_counts.get(count_key, 0) + 1
                                )
                            continue
                        identity = _identity(item, root)
                        reason = identity if isinstance(identity, str) else None
                    else:
                        old_path = f"{source}#{state}[{position}]"
                        identity = "non-string array element"
                        reason = identity
                    nickname = (
                        identity.nickname
                        if isinstance(identity, PathIdentity)
                        else None
                    )
                    if nickname is not None:
                        nickname = nickname_pool.setdefault(nickname, nickname)
                    sec_set = mapping.get(nickname or "", set())
                    sec = next(iter(sec_set)) if len(sec_set) == 1 else None
                    if nickname:
                        count_key = (state, nickname)
                        account_counts[count_key] = account_counts.get(count_key, 0) + 1
                    if reason is None and sec is None:
                        reason = "nickname is unmapped or maps to multiple account IDs"
                    if state == "sent":
                        cursor = connection.execute(
                            """INSERT OR IGNORE INTO sent_paths
                               (legacy_path, source_file, round, nickname, owner,
                                relative_path, sec_user_id, reason)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                old_path,
                                str(source),
                                _round(source),
                                nickname,
                                (
                                    identity.owner
                                    if isinstance(identity, PathIdentity)
                                    else None
                                ),
                                (
                                    identity.relative_path
                                    if isinstance(identity, PathIdentity)
                                    else None
                                ),
                                sec,
                                reason,
                            ),
                        )
                        sent_unique += int(cursor.rowcount > 0)
                        sent_nicknames[old_path] = nickname
                    else:
                        cursor = connection.execute(
                            """INSERT OR IGNORE INTO failed_paths
                               (legacy_path, source_file, round, nickname,
                                sec_user_id, reason)
                               VALUES (?, ?, ?, ?, ?, ?)""",
                            (
                                old_path,
                                str(source),
                                _round(source),
                                nickname,
                                sec,
                                reason,
                            ),
                        )
                        failed_unique += int(cursor.rowcount > 0)
            if (source.stat().st_size, source.stat().st_mtime_ns) != (
                stat.st_size,
                stat.st_mtime_ns,
            ):
                raise ValueError(f"source changed while being read: {source}")
            connection.execute(
                "INSERT INTO source_files VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(source),
                    stat.st_size,
                    stat.st_mtime_ns,
                    sent,
                    failed,
                    sent_unique,
                    failed_unique,
                ),
            )
            connection.executemany(
                """INSERT INTO source_account_counts
                   (source_file, round, nickname, state, entries)
                   VALUES (?, ?, ?, ?, ?)""",
                [
                    (str(source), _round(source), nickname, state, count)
                    for (state, nickname), count in account_counts.items()
                ],
            )
            connection.executemany(
                """INSERT INTO source_user_stats
                   (source_file, round, nickname, chat_id, failed, sent, total, done)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        str(source),
                        _round(source),
                        nickname,
                        (
                            stats["chat_id"]
                            if isinstance(stats.get("chat_id"), str)
                            else None
                        ),
                        _nonnegative_count(stats.get("failed")),
                        _nonnegative_count(stats.get("sent")),
                        _nonnegative_count(stats.get("total")),
                        (
                            int(stats["done"])
                            if isinstance(stats.get("done"), bool)
                            else None
                        ),
                    )
                    for nickname, stats in parser.users.items()
                ],
            )
            connection.executemany(
                """INSERT INTO source_topics
                   (source_file, round, nickname, topic_message_id)
                   VALUES (?, ?, ?, ?)""",
                [
                    (str(source), _round(source), nickname, message_id)
                    for nickname, message_id in parser.user_topics.items()
                ],
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        report.staged += 1
        print(
            f"checkpoint {report.staged + report.reused}/{len(paths)} "
            f"{source.name}: sent={sent} failed={failed}"
        )


def _count(connection: sqlite3.Connection, query: str) -> int:
    return int(connection.execute(query).fetchone()[0])


def _summarize(connection: sqlite3.Connection, report: Report) -> None:
    totals = connection.execute(
        """SELECT COALESCE(SUM(sent), 0), COALESCE(SUM(failed), 0),
                  COALESCE(SUM(sent_unique), 0), COALESCE(SUM(failed_unique), 0)
           FROM source_files"""
    ).fetchone()
    report.sent, report.failed, report.sent_unique, _ = totals
    report.failed_unique = _count(
        connection, "SELECT COUNT(DISTINCT legacy_path) FROM failed_paths"
    )
    report.sent_duplicates = report.sent - report.sent_unique
    report.failed_duplicates = report.failed - report.failed_unique
    connection.execute("DROP TABLE IF EXISTS temp.collisions")
    connection.execute("""CREATE TEMP TABLE collisions AS
           SELECT sec_user_id, relative_path FROM sent_paths
           WHERE sec_user_id IS NOT NULL AND relative_path IS NOT NULL
           GROUP BY sec_user_id, relative_path
           HAVING COUNT(DISTINCT owner) > 1""")
    connection.execute(
        "CREATE INDEX idx_collisions ON collisions (sec_user_id, relative_path)"
    )
    report.safe_sent = _count(
        connection,
        """SELECT COUNT(*) FROM sent_paths AS s
           LEFT JOIN collisions AS c ON c.sec_user_id = s.sec_user_id
             AND c.relative_path = s.relative_path
           WHERE s.reason IS NULL AND c.sec_user_id IS NULL""",
    )
    report.needs_review = report.sent_unique - report.safe_sent + report.failed_unique
