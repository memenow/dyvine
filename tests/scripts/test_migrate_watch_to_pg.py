"""Tests for the SQLite -> Postgres watch migration script."""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from dyvine.db import (
    DatabaseSessionFactory,
    PostgresWatchRepository,
)

ROOT_DIR = Path(__file__).resolve().parents[2]


def _load_script() -> Any:
    """Import ``scripts/migrate_watch_to_pg.py`` without side effects."""
    import sys

    path = ROOT_DIR / "scripts" / "migrate_watch_to_pg.py"
    spec = importlib.util.spec_from_file_location("migrate_watch_to_pg", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Dataclass processing resolves annotations through
    # ``sys.modules``; register before executing.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _legacy_db(path: Path, rows: list[tuple[Any, ...]]) -> Path:
    """Create a legacy-format SQLite file with the given rows."""
    connection = sqlite3.connect(path)
    try:
        connection.execute("""
            CREATE TABLE watch_subscriptions (
                subscription_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                enabled INTEGER NOT NULL,
                live_poll_seconds INTEGER NOT NULL,
                post_poll_seconds INTEGER NOT NULL,
                checkpoint TEXT NOT NULL,
                last_live_check TEXT,
                last_post_check TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """)
        connection.execute(
            "CREATE UNIQUE INDEX idx_watch_user " "ON watch_subscriptions (user_id)"
        )
        connection.executemany(
            "INSERT INTO watch_subscriptions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        connection.commit()
    finally:
        connection.close()
    return path


def _row(
    subscription_id: str = "sub-1",
    user_id: str = "user-1",
    *,
    enabled: Any = 1,
    checkpoint: Any = '{"newest_aweme_id": "7"}',
    last_live_check: Any = "2026-01-01T00:00:00+00:00",
    last_post_check: Any = None,
) -> tuple[Any, ...]:
    """Build one legacy row tuple with overridable fields."""
    return (
        subscription_id,
        user_id,
        enabled,
        60,
        300,
        checkpoint,
        last_live_check,
        last_post_check,
        "2026-01-01T00:00:00+00:00",
        "2026-01-02T00:00:00+00:00",
    )


@pytest.fixture
async def clean_watch_table(postgres_url: str) -> Any:
    """Truncate the watch table and yield a reader repository."""
    factory = DatabaseSessionFactory(postgres_url, pool_size=1)
    async with factory.session() as session:
        async with session.begin():
            await session.execute(text("TRUNCATE TABLE watch_subscriptions"))
    try:
        yield PostgresWatchRepository(factory)
    finally:
        await factory.aclose()


async def test_migrate_imports_valid_rows(
    tmp_path: Path, postgres_url: str, clean_watch_table: Any
) -> None:
    """Valid rows land in Postgres with normalised types."""
    script = _load_script()
    db_path = _legacy_db(tmp_path / "legacy.db", [_row(), _row("sub-2", "user-2")])

    args = script.parse_args(
        ["--sqlite-path", str(db_path), "--database-url", postgres_url]
    )
    report = await script.run(args)

    assert (report.scanned, report.valid, report.invalid) == (2, 2, 0)
    assert (report.imported, report.skipped_existing) == (2, 0)
    fetched = await clean_watch_table.get_subscription_by_user("user-1")
    assert fetched is not None
    assert fetched.subscription_id == "sub-1"
    assert fetched.enabled is True
    assert fetched.checkpoint == {"newest_aweme_id": "7"}
    assert fetched.last_live_check == "2026-01-01T00:00:00+00:00"
    assert fetched.last_post_check is None


def test_validated_row_rejects_non_positive_intervals() -> None:
    """Zero/negative cadences are skipped: 0 would busy-spin the loop."""
    script = _load_script()
    base: dict[str, Any] = {
        "subscription_id": "sub-1",
        "user_id": "user-1",
        "enabled": 1,
        "live_poll_seconds": 60,
        "post_poll_seconds": 300,
        "checkpoint": "{}",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-02T00:00:00+00:00",
        "last_live_check": None,
        "last_post_check": None,
    }
    assert isinstance(script._validated_row({**base, "live_poll_seconds": 0}), str)
    assert isinstance(script._validated_row({**base, "post_poll_seconds": -5}), str)
    assert isinstance(script._validated_row(dict(base)), dict)


async def test_migrate_is_idempotent_on_rerun(
    tmp_path: Path, postgres_url: str, clean_watch_table: Any
) -> None:
    """A second run imports nothing and overwrites nothing."""
    script = _load_script()
    db_path = _legacy_db(tmp_path / "legacy.db", [_row()])
    argv = ["--sqlite-path", str(db_path), "--database-url", postgres_url]

    first = await script.run(script.parse_args(argv))
    second = await script.run(script.parse_args(argv))

    assert (first.imported, first.skipped_existing) == (1, 0)
    assert (second.imported, second.skipped_existing) == (0, 1)
    assert await clean_watch_table.count_subscriptions() == 1


async def test_migrate_never_overwrites_native_rows(
    tmp_path: Path, postgres_url: str, clean_watch_table: Any
) -> None:
    """Post-cutover Postgres rows win over stale source rows."""
    script = _load_script()
    await clean_watch_table.create_subscription(
        user_id="user-1",
        live_poll_seconds=60,
        post_poll_seconds=300,
        checkpoint={"newest_aweme_id": "native"},
    )
    db_path = _legacy_db(tmp_path / "legacy.db", [_row()])

    args = script.parse_args(
        ["--sqlite-path", str(db_path), "--database-url", postgres_url]
    )
    report = await script.run(args)

    assert (report.imported, report.skipped_existing) == (0, 1)
    fetched = await clean_watch_table.get_subscription_by_user("user-1")
    assert fetched is not None
    assert fetched.checkpoint == {"newest_aweme_id": "native"}


def _main_in_thread(script: Any, argv: list[str]) -> int:
    """Run the script's sync ``main`` on a thread without a loop.

    ``main`` drives its own ``asyncio.run``, which cannot execute on
    the test's event loop thread.
    """
    import threading

    codes: list[int] = []
    errors: list[BaseException] = []

    def _run() -> None:
        try:
            codes.append(script.main(argv))
        except BaseException as exc:  # propagate to the caller
            errors.append(exc)

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join()
    if errors:
        raise errors[0]
    return codes[0]


async def test_migrate_skips_invalid_rows_with_exit_code_1(
    tmp_path: Path,
    postgres_url: str,
    clean_watch_table: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Invalid rows are reported; valid siblings still import."""
    script = _load_script()
    db_path = _legacy_db(
        tmp_path / "legacy.db",
        [
            _row(),
            _row("sub-bad", "user-bad", checkpoint="{not json"),
            _row("sub-bad2", "user-bad2", enabled=7),
        ],
    )

    code = _main_in_thread(
        script,
        ["--sqlite-path", str(db_path), "--database-url", postgres_url],
    )

    assert code == 1
    out, err = capsys.readouterr()
    assert "scanned=3 valid=1" in out
    assert "invalid=2" in out
    assert "user-bad" in err or "rowid=" in err
    assert await clean_watch_table.count_subscriptions() == 1


async def test_migrate_dry_run_writes_nothing(
    tmp_path: Path, postgres_url: str, clean_watch_table: Any
) -> None:
    """Dry-run validates and counts without touching Postgres."""
    script = _load_script()
    db_path = _legacy_db(tmp_path / "legacy.db", [_row()])

    args = script.parse_args(
        [
            "--sqlite-path",
            str(db_path),
            "--database-url",
            postgres_url,
            "--dry-run",
        ]
    )
    report = await script.run(args)

    assert (report.scanned, report.valid, report.imported) == (1, 1, 0)
    assert await clean_watch_table.count_subscriptions() == 0


def test_migrate_missing_source_is_exit_code_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing SQLite file is a configuration error, not a diff."""
    script = _load_script()
    code = script.main(
        [
            "--sqlite-path",
            str(tmp_path / "nope.db"),
            "--database-url",
            "postgresql+asyncpg://u:p@localhost:1/db",
        ]
    )
    assert code == 2


def test_migrate_missing_table_is_exit_code_2(tmp_path: Path) -> None:
    """A source without the legacy table fails loudly."""
    script = _load_script()
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    with pytest.raises(ValueError, match="watch_subscriptions"):
        script.load_validated_rows(empty)
