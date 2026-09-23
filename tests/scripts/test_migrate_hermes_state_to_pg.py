"""Synthetic source tests for the one-shot Hermes state migration."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from dyvine.db import DatabaseSessionFactory

ROOT_DIR = Path(__file__).resolve().parents[2]
STAMP = "2026-09-22T00:00:00+00:00"


def _load_script() -> Any:
    """Import the operator script without invoking its CLI."""
    path = ROOT_DIR / "scripts" / "migrate_hermes_state_to_pg.py"
    spec = importlib.util.spec_from_file_location("migrate_hermes_state_to_pg", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def _source_tree(tmp_path: Path) -> dict[str, Path]:
    """Create every source type without using private production data."""
    state = tmp_path / "state"
    state.mkdir()
    _write_json(
        state / "download_queue.json",
        {
            "entries": [
                {
                    "key": "round-1:sec-a",
                    "round": "round-1",
                    "nickname": "Alpha",
                    "sec_user_id": "sec-a",
                    "chat_id": "chat-a",
                    "mode": "incremental",
                    "status": "completed",
                    "updated_at": STAMP,
                }
            ]
        },
    )
    _write_json(state / "excluded_accounts.json", ["Alpha", "Orphan", "Another"])
    send = sqlite3.connect(state / "send_status.db")
    try:
        send.execute("CREATE TABLE send_status (nickname TEXT, updated_at TEXT)")
        send.execute("CREATE TABLE user_send_status (username TEXT, updated_at TEXT)")
        send.execute("INSERT INTO send_status VALUES (?, ?)", ("Alpha", STAMP))
        send.commit()
    finally:
        send.close()
    users_db = tmp_path / "users.db"
    users = sqlite3.connect(users_db)
    try:
        users.execute("CREATE TABLE user_info_web (sec_user_id TEXT, nickname TEXT)")
        users.execute("INSERT INTO user_info_web VALUES (?, ?)", ("sec-a", "Alpha"))
        users.commit()
    finally:
        users.close()
    seed_path = tmp_path / "seed_users.json"
    _write_json(
        seed_path,
        [
            {"sec_user_id": "sec-a", "nickname": "Alpha"},
            {"sec_user_id": "sec-c", "nickname": "Alpha"},
        ],
    )
    weekly_path = tmp_path / "weekly_align_ops_round-1.json"
    _write_json(
        weekly_path,
        {
            "round": "round-1",
            "created_at": STAMP,
            "entries": [
                {
                    "sec_user_id": "sec-a",
                    "nickname": "Alpha",
                    "status": "sent",
                },
                {
                    "sec_user_id": "sec-b",
                    "nickname": "Beta",
                    "status": "in_progress",
                    "sent_files": 2,
                    "operation_id": "",
                },
            ],
        },
    )
    return {
        "state": state,
        "users_db": users_db,
        "seed_path": seed_path,
        "weekly_path": weekly_path,
    }


def _args(script: Any, source: dict[str, Path], url: str, *extra: str) -> Any:
    """Build the CLI's source paths, while keeping the URL out of argv."""
    import argparse

    return argparse.Namespace(
        state_dir=str(source["state"]),
        seed_path=str(source["seed_path"]),
        weekly_glob=str(source["weekly_path"]),
        users_db=str(source["users_db"]),
        database_url=url,
        dry_run="--dry-run" in extra,
        batch_size=2,
    )


async def _truncate(factory: DatabaseSessionFactory) -> None:
    async with factory.session() as session:
        async with session.begin():
            await session.execute(
                text(
                    "TRUNCATE TABLE operations, download_queue, send_status, "
                    "user_send_status, seed_accounts, legacy_excluded_nicknames, "
                    "user_profiles, delivery_rounds"
                )
            )


async def test_weekly_only_rows_require_reconciliation_and_rerun_is_idempotent(
    tmp_path: Path, postgres_url: str
) -> None:
    """A queue row wins over weekly progress; a rerun changes no row."""
    script = _load_script()
    source = _source_tree(tmp_path)
    factory = DatabaseSessionFactory(postgres_url, pool_size=1)
    try:
        await _truncate(factory)
        first = script.MigrationReport()
        second = script.MigrationReport()
        await script._run(_args(script, source, postgres_url), first)
        await script._run(_args(script, source, postgres_url), second)

        assert first.invalid == second.invalid == 0
        assert (
            first.sources["download_queue"].imported,
            first.sources["weekly_queue"].imported,
        ) == (1, 1)
        assert (
            second.sources["download_queue"].imported,
            second.sources["weekly_queue"].imported,
        ) == (0, 0)
        assert second.sources["weekly_queue"].skipped_existing == 2
        assert first.sources["legacy_excluded_nicknames"].imported == 3
        assert second.sources["legacy_excluded_nicknames"].imported == 0
        assert second.sources["legacy_excluded_nicknames"].skipped_existing == 3
        async with factory.session() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT key, status, extra FROM download_queue " "ORDER BY key"
                    )
                )
            ).all()
            assert len(rows) == 2
            assert rows[0].key == "round-1:sec-a"
            assert rows[0].status == "needs_reconciliation"
            assert rows[0].extra["legacy_queue_status"] == "completed"
            assert rows[0].extra["migration_needs_reconciliation"] is True
            assert "legacy_weekly_status" not in rows[0].extra
            assert rows[1].key == "round-1:sec-b"
            assert rows[1].status == "needs_reconciliation"
            assert rows[1].extra["legacy_weekly_status"] == "in_progress"
            assert rows[1].extra["sent_files"] == 2
            assert (
                await session.execute(text("SELECT count(*) FROM delivery_rounds"))
            ).scalar_one() == 1
            excluded_rows = (
                await session.execute(
                    text(
                        "SELECT nickname, source FROM legacy_excluded_nicknames "
                        "ORDER BY nickname"
                    )
                )
            ).all()
            assert [(row.nickname, row.source) for row in excluded_rows] == [
                ("Alpha", "legacy"),
                ("Another", "legacy"),
                ("Orphan", "legacy"),
            ]
            seed_rows = (
                await session.execute(
                    text(
                        "SELECT sec_user_id, excluded FROM seed_accounts "
                        "ORDER BY sec_user_id"
                    )
                )
            ).all()
            assert [(row.sec_user_id, row.excluded) for row in seed_rows] == [
                ("sec-a", True),
                ("sec-c", True),
            ]
    finally:
        await factory.aclose()


async def test_distinct_accounts_sharing_legacy_key_are_rekeyed_once(
    tmp_path: Path, postgres_url: str
) -> None:
    """A colliding nickname key preserves both accounts and their source key."""
    script = _load_script()
    source = _source_tree(tmp_path)
    queue_path = source["state"] / "download_queue.json"
    _write_json(
        queue_path,
        {
            "entries": [
                {
                    "key": "round-1:shared-name",
                    "round": "round-1",
                    "nickname": "Shared Name",
                    "sec_user_id": "sec-a",
                    "chat_id": "chat-a",
                    "mode": "incremental",
                    "status": "completed",
                    "updated_at": STAMP,
                },
                {
                    "key": "round-1:shared-name",
                    "round": "round-1",
                    "nickname": "Other Name",
                    "sec_user_id": "sec-b",
                    "chat_id": "chat-b",
                    "mode": "incremental",
                    "status": "completed",
                    "updated_at": STAMP,
                },
                {
                    "key": "round-1:historic-name",
                    "round": "round-1",
                    "nickname": "Historic Name",
                    "sec_user_id": "sec-c",
                    "chat_id": "chat-c",
                    "mode": "incremental",
                    "status": "completed",
                    "updated_at": STAMP,
                },
            ]
        },
    )
    factory = DatabaseSessionFactory(postgres_url, pool_size=1)
    try:
        await _truncate(factory)
        dry_run = script.MigrationReport()
        first = script.MigrationReport()
        second = script.MigrationReport()
        await script._run(_args(script, source, "", "--dry-run"), dry_run)
        await script._run(_args(script, source, postgres_url), first)
        await script._run(_args(script, source, postgres_url), second)

        assert dry_run.invalid == first.invalid == second.invalid == 0
        assert dry_run.sources["download_queue"].valid == 3
        assert first.sources["download_queue"].imported == 3
        assert second.sources["download_queue"].skipped_existing == 3
        assert second.sources["download_queue"].imported == 0
        async with factory.session() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT key, sec_user_id, chat_id, extra FROM download_queue "
                        "ORDER BY key"
                    )
                )
            ).all()
        assert [(row.key, row.sec_user_id, row.chat_id) for row in rows] == [
            ("round-1:historic-name", "sec-c", "chat-c"),
            ("round-1:sec-a", "sec-a", "chat-a"),
            ("round-1:sec-b", "sec-b", "chat-b"),
        ]
        assert rows[0].extra.get("legacy_key") is None
        assert rows[1].extra["legacy_key"] == "round-1:shared-name"
        assert rows[2].extra["legacy_key"] == "round-1:shared-name"
    finally:
        await factory.aclose()


async def test_rekey_aborts_when_canonical_key_is_already_in_source(
    tmp_path: Path, postgres_url: str
) -> None:
    """An occupied canonical key must be resolved before any database write."""
    script = _load_script()
    source = _source_tree(tmp_path)
    queue_path = source["state"] / "download_queue.json"
    doc = json.loads(queue_path.read_text(encoding="utf-8"))
    doc["entries"][0]["key"] = "round-1:shared-name"
    doc["entries"] += [
        {
            "key": "round-1:shared-name",
            "round": "round-1",
            "nickname": "Other",
            "sec_user_id": "sec-b",
            "mode": "incremental",
            "status": "pending",
            "updated_at": STAMP,
        },
        {
            "key": "round-1:sec-a",
            "round": "round-1",
            "nickname": "Third",
            "sec_user_id": "sec-c",
            "mode": "incremental",
            "status": "pending",
            "updated_at": STAMP,
        },
    ]
    _write_json(queue_path, doc)
    factory = DatabaseSessionFactory(postgres_url, pool_size=1)
    try:
        await _truncate(factory)
        report = script.MigrationReport()
        await script._run(_args(script, source, postgres_url), report)
        assert report.invalid == 1
        assert "canonical rekey collides" in report.issues[0].reason
        async with factory.session() as session:
            count = (
                await session.execute(text("SELECT count(*) FROM download_queue"))
            ).scalar_one()
        assert count == 0
    finally:
        await factory.aclose()


async def test_rekey_aborts_when_old_key_is_already_in_target(
    tmp_path: Path, postgres_url: str
) -> None:
    """An earlier partial import cannot silently drop a colliding account."""
    script = _load_script()
    source = _source_tree(tmp_path)
    queue_path = source["state"] / "download_queue.json"
    doc = json.loads(queue_path.read_text(encoding="utf-8"))
    doc["entries"][0]["key"] = "round-1:shared-name"
    _write_json(queue_path, doc)
    factory = DatabaseSessionFactory(postgres_url, pool_size=1)
    try:
        await _truncate(factory)
        first = script.MigrationReport()
        await script._run(_args(script, source, postgres_url), first)
        assert first.invalid == 0
        doc["entries"].append(
            {
                "key": "round-1:shared-name",
                "round": "round-1",
                "nickname": "Other",
                "sec_user_id": "sec-b",
                "chat_id": "chat-b",
                "mode": "incremental",
                "status": "completed",
                "updated_at": STAMP,
            }
        )
        _write_json(queue_path, doc)
        second = script.MigrationReport()
        await script._run(_args(script, source, postgres_url), second)
        assert second.invalid == 1
        assert second.issues[0].reason == "target contains an old colliding key"
        assert second.sources["download_queue"].imported == 0
        async with factory.session() as session:
            keys = (
                (
                    await session.execute(
                        text("SELECT key FROM download_queue ORDER BY key")
                    )
                )
                .scalars()
                .all()
            )
        assert keys == [
            "round-1:sec-a",
            "round-1:sec-b",
            "round-1:shared-name",
        ]
    finally:
        await factory.aclose()


async def test_invalid_source_prevents_every_table_write(
    tmp_path: Path, postgres_url: str
) -> None:
    """A late invalid seed row cannot leave earlier queue rows imported."""
    script = _load_script()
    source = _source_tree(tmp_path)
    _write_json(
        source["seed_path"],
        [{"sec_user_id": "sec-a"}, {"nickname": "Missing sec"}],
    )
    factory = DatabaseSessionFactory(postgres_url, pool_size=1)
    try:
        await _truncate(factory)
        report = script.MigrationReport()
        await script._run(_args(script, source, postgres_url), report)
        assert report.invalid == 1
        assert report.sources["download_queue"].imported == 0
        async with factory.session() as session:
            for table in (
                "download_queue",
                "send_status",
                "seed_accounts",
                "legacy_excluded_nicknames",
                "user_profiles",
                "delivery_rounds",
            ):
                count = (
                    await session.execute(text(f"SELECT count(*) FROM {table}"))
                ).scalar_one()
                assert count == 0, table
    finally:
        await factory.aclose()


async def test_dry_run_counts_nickname_only_exclusions_without_writes(
    tmp_path: Path,
) -> None:
    """Excluded names absent from seeds remain visible in the preflight."""
    script = _load_script()
    source = _source_tree(tmp_path)
    report = script.MigrationReport()
    await script._run(_args(script, source, "", "--dry-run"), report)

    excluded = report.sources["legacy_excluded_nicknames"]
    assert (excluded.scanned, excluded.valid, excluded.imported) == (3, 3, 0)
    assert report.invalid == 0
    assert report.warnings == [
        f"operations.db not found at {source['state'] / 'operations.db'}, skipped"
    ]


def test_dry_run_needs_no_database_url_and_reports_invalid_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operators can inspect disk data before obtaining database access."""
    script = _load_script()
    source = _source_tree(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    argv = [
        "--state-dir",
        str(source["state"]),
        "--seed-path",
        str(source["seed_path"]),
        "--users-db",
        str(source["users_db"]),
        "--weekly-glob",
        str(source["weekly_path"]),
        "--dry-run",
    ]
    assert script.main(argv) == 0
    _write_json(source["seed_path"], [{"nickname": "Missing sec"}])
    assert script.main(argv) == 1


def test_database_url_is_read_from_named_environment_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live connection URL never has to appear in process arguments."""
    script = _load_script()
    source = _source_tree(tmp_path)
    seen: list[str] = []

    async def capture(args: Any, report: Any) -> None:
        seen.append(args.database_url)

    monkeypatch.setattr(script, "_run", capture)
    monkeypatch.setenv("MIGRATION_TEST_URL", "postgresql+asyncpg://example")
    code = script.main(
        [
            "--state-dir",
            str(source["state"]),
            "--seed-path",
            str(source["seed_path"]),
            "--users-db",
            str(source["users_db"]),
            "--weekly-glob",
            str(source["weekly_path"]),
            "--database-url-env",
            "MIGRATION_TEST_URL",
        ]
    )
    assert code == 0
    assert seen == ["postgresql+asyncpg://example"]
