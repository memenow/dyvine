"""Stream and checkpoint optional legacy evidence sources."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Any

from scripts import legacy_send_staging as staging


def array_values(parser: staging.JsonStream) -> Iterator[tuple[Any, int]]:
    """Read an independent top-level array without materializing it."""
    parser.expect("[")
    position = 0
    while parser.peek() != "]":
        yield parser.value(), position
        position += 1
        if parser.peek() != "]":
            parser.expect(",")
    parser.expect("]")
    if parser.peek():
        raise ValueError("trailing content after array")


def index_paths(parser: staging.JsonStream) -> Iterator[tuple[str, Any, int]]:
    """Read the nested cache deltas without loading the 40 MiB index."""
    parser.expect("{")
    found_files = False
    while parser.peek() != "}":
        key = parser.value()
        if not isinstance(key, str):
            raise ValueError("cache keys must be strings")
        parser.expect(":")
        if key == "files":
            if found_files:
                raise ValueError("cache has multiple files objects")
            found_files = True
            parser.expect("{")
            while parser.peek() != "}":
                source_path = parser.value()
                if not isinstance(source_path, str) or not source_path:
                    raise ValueError("cache source path must be a string")
                parser.expect(":")
                parser.expect("{")
                found_delta = False
                while parser.peek() != "}":
                    field = parser.value()
                    if not isinstance(field, str):
                        raise ValueError("cache entry keys must be strings")
                    parser.expect(":")
                    if field == "delta":
                        if found_delta:
                            raise ValueError("cache entry has multiple delta arrays")
                        found_delta = True
                        parser.expect("[")
                        position = 0
                        while parser.peek() != "]":
                            yield source_path, parser.value(), position
                            position += 1
                            if parser.peek() != "]":
                                parser.expect(",")
                        parser.expect("]")
                    else:
                        parser.value()
                    if parser.peek() != "}":
                        parser.expect(",")
                parser.expect("}")
                if not found_delta:
                    raise ValueError("cache entry lacks delta array")
                if parser.peek() != "}":
                    parser.expect(",")
            parser.expect("}")
        else:
            parser.value()
        if parser.peek() != "}":
            parser.expect(",")
    parser.expect("}")
    if parser.peek():
        raise ValueError("trailing content after cache")
    if not found_files:
        raise ValueError("cache lacks files object")


def verify_extra_source_stats(connection: sqlite3.Connection) -> None:
    for row in connection.execute("SELECT path, size, mtime_ns FROM extra_sources"):
        source = Path(row["path"])
        stat = source.stat()
        if (stat.st_size, stat.st_mtime_ns) != (row["size"], row["mtime_ns"]):
            raise ValueError(f"extra source changed after staging: {source}")


def stage_extras(
    connection: sqlite3.Connection,
    sources: dict[str, Path],
    mapping: dict[str, set[str]],
    root: PurePosixPath,
) -> None:
    """Checkpoint each optional evidence source as one atomic transaction."""
    known = {
        row["kind"]: row for row in connection.execute("SELECT * FROM extra_sources")
    }
    if set(known) - set(sources):
        raise ValueError("a checkpointed extra source was omitted")
    imported = connection.execute(
        "SELECT value FROM work_meta WHERE key = 'import_started'"
    ).fetchone()
    if imported and set(sources) - set(known):
        raise ValueError("new extra sources appeared after database import started")
    for kind, source in sources.items():
        stat = source.stat()
        previous = known.get(kind)
        if previous is not None:
            if (previous["path"], previous["size"], previous["mtime_ns"]) != (
                str(source),
                stat.st_size,
                stat.st_mtime_ns,
            ):
                raise ValueError(f"checkpointed extra source changed: {source}")
            continue
        connection.execute("BEGIN IMMEDIATE")
        entries = 0
        try:
            with source.open("r", encoding="utf-8") as handle:
                parser = staging.JsonStream(handle)
                items: Iterator[tuple[str, Any, int]]
                if kind == "permanent":
                    items = (
                        (str(source), value, index)
                        for value, index in array_values(parser)
                    )
                else:
                    items = index_paths(parser)
                for source_path, value, index in items:
                    entries += 1
                    legacy_path = (
                        value
                        if isinstance(value, str)
                        else f"{source_path}#{kind}[{index}]"
                    )
                    identity = (
                        staging._identity(value, root)
                        if isinstance(value, str)
                        else "non-string path entry"
                    )
                    nickname = (
                        identity.nickname
                        if isinstance(identity, staging.PathIdentity)
                        else None
                    )
                    ids = mapping.get(nickname or "", set())
                    sec = next(iter(ids)) if len(ids) == 1 else None
                    reason = identity if isinstance(identity, str) else None
                    if reason is None and sec is None:
                        reason = "nickname is unmapped or maps to multiple account IDs"
                    if kind == "permanent":
                        connection.execute(
                            """INSERT OR IGNORE INTO permanent_paths
                               (legacy_path, nickname, owner, relative_path,
                                sec_user_id, reason) VALUES (?, ?, ?, ?, ?, ?)""",
                            (
                                legacy_path,
                                nickname,
                                (
                                    identity.owner
                                    if isinstance(identity, staging.PathIdentity)
                                    else None
                                ),
                                (
                                    identity.relative_path
                                    if isinstance(identity, staging.PathIdentity)
                                    else None
                                ),
                                sec,
                                reason,
                            ),
                        )
                    else:
                        source_name = Path(source_path).name
                        round_name = (
                            staging._round(Path(source_path))
                            if source_name.startswith("send_progress_weekly")
                            else None
                        )
                        connection.execute(
                            """INSERT OR IGNORE INTO cache_paths
                               (source_file, legacy_path, round, nickname,
                                relative_path, sec_user_id, reason)
                               VALUES (?, ?, ?, ?, ?, ?, ?)""",
                            (
                                source_path,
                                legacy_path,
                                round_name,
                                nickname,
                                (
                                    identity.relative_path
                                    if isinstance(identity, staging.PathIdentity)
                                    else None
                                ),
                                sec,
                                reason,
                            ),
                        )
            if (source.stat().st_size, source.stat().st_mtime_ns) != (
                stat.st_size,
                stat.st_mtime_ns,
            ):
                raise ValueError(f"source changed while being read: {source}")
            connection.execute(
                "INSERT INTO extra_sources VALUES (?, ?, ?, ?, ?)",
                (str(source), kind, stat.st_size, stat.st_mtime_ns, entries),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        print(f"checkpoint {kind}: entries={entries}")


def summarize_extras(connection: sqlite3.Connection, report: staging.Report) -> None:
    report.permanent_paths = staging._count(
        connection, "SELECT COUNT(*) FROM permanent_paths"
    )
    report.cache_paths = staging._count(connection, "SELECT COUNT(*) FROM cache_paths")
    connection.execute("DROP TABLE IF EXISTS temp.permanent_conflicts")
    connection.execute("""CREATE TEMP TABLE permanent_conflicts AS
           SELECT p.sec_user_id, p.relative_path FROM permanent_paths AS p
           WHERE p.sec_user_id IS NOT NULL AND p.relative_path IS NOT NULL
           GROUP BY p.sec_user_id, p.relative_path
           HAVING COUNT(DISTINCT p.owner) > 1
           UNION
           SELECT p.sec_user_id, p.relative_path FROM permanent_paths AS p
           JOIN sent_paths AS s ON s.sec_user_id = p.sec_user_id
             AND s.relative_path = p.relative_path
           UNION
           SELECT p.sec_user_id, p.relative_path FROM permanent_paths AS p
           JOIN cache_paths AS c ON c.sec_user_id = p.sec_user_id
             AND c.relative_path = p.relative_path""")
    connection.execute(
        "CREATE INDEX idx_perm ON permanent_conflicts (sec_user_id, relative_path)"
    )
    report.permanent_safe = staging._count(
        connection,
        """SELECT COUNT(*) FROM permanent_paths AS p
           LEFT JOIN permanent_conflicts AS c ON c.sec_user_id = p.sec_user_id
             AND c.relative_path = p.relative_path
           WHERE p.reason IS NULL AND c.sec_user_id IS NULL""",
    )
    connection.execute("DROP TABLE IF EXISTS temp.cache_only")
    connection.execute("""CREATE TEMP TABLE cache_only AS
           SELECT c.* FROM cache_paths AS c
           WHERE NOT EXISTS (
             SELECT 1 FROM sent_paths AS s
             LEFT JOIN collisions AS x ON x.sec_user_id = s.sec_user_id
               AND x.relative_path = s.relative_path
             WHERE s.legacy_path = c.legacy_path
               AND s.reason IS NULL AND x.sec_user_id IS NULL
           )""")
    report.cache_only = staging._count(
        connection, "SELECT COUNT(DISTINCT legacy_path) FROM cache_only"
    )
    report.needs_review += report.permanent_paths - report.permanent_safe
    report.needs_review += report.cache_only
