"""Alembic upgrade/downgrade round-trip on a scratch database."""

from __future__ import annotations

import threading
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from testcontainers.community.postgres import PostgresContainer

import dyvine.core.settings as settings_module
from dyvine.db import DatabaseSessionFactory


def _alembic(fn_name: str, cfg: Config, target: str) -> None:
    """Run an Alembic command on a thread without a loop (see conftest)."""
    errors: list[BaseException] = []

    def _run() -> None:
        try:
            getattr(command, fn_name)(cfg, target)
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join()
    if errors:
        raise errors[0]


async def _table_exists(url: str, table: str) -> bool:
    # Single-shot probe: the default NullPool opens one connection and
    # holds none. (A ``pool_size`` here would be a placebo — queue-only
    # knobs are rejected under ``pool_class="null"``.)
    factory = DatabaseSessionFactory(url)
    try:
        async with factory.session() as session:
            row = (
                await session.execute(
                    text("SELECT to_regclass(:name)"), {"name": table}
                )
            ).scalar()
            return row is not None
    finally:
        await factory.aclose()


async def test_migration_round_trip() -> None:
    """``head -> base -> head`` is lossless on a scratch database.

    Uses its own container so the downgrade leg cannot disturb the
    session-scoped ``postgres_url`` database shared by other tests.
    """
    root_dir = Path(__file__).resolve().parents[2]
    with PostgresContainer("postgres:16") as container:
        raw_url = container.get_connection_url()
        url = raw_url.replace("postgresql+psycopg2://", "postgresql+asyncpg://")
        previous = settings_module.settings.database.url
        settings_module.settings.database.url = url
        try:
            cfg = Config(str(root_dir / "alembic.ini"))
            _alembic("upgrade", cfg, "head")
            assert await _table_exists(url, "public.operations")
            assert await _table_exists(url, "public.watch_subscriptions")
            _alembic("downgrade", cfg, "base")
            assert not await _table_exists(url, "public.operations")
            assert not await _table_exists(url, "public.watch_subscriptions")
            _alembic("upgrade", cfg, "head")
            assert await _table_exists(url, "public.operations")
            assert await _table_exists(url, "public.watch_subscriptions")
        finally:
            settings_module.settings.database.url = previous


async def test_0004_backfills_orphan_round_headers_before_constraining() -> None:
    """Pre-existing children with headerless rounds survive the 0004 FKs.

    One-shot scripts wrote ``legacy``/``feishu_adopted``-style rounds
    directly; the upgrade must backfill a header per referenced round
    instead of failing the FK build (production 0003 -> 0004 incident).
    """
    root_dir = Path(__file__).resolve().parents[2]
    with PostgresContainer("postgres:16") as container:
        raw_url = container.get_connection_url()
        url = raw_url.replace("postgresql+psycopg2://", "postgresql+asyncpg://")
        previous = settings_module.settings.database.url
        settings_module.settings.database.url = url
        try:
            cfg = Config(str(root_dir / "alembic.ini"))
            _alembic("upgrade", cfg, "0003")
            factory = DatabaseSessionFactory(url)
            try:
                async with factory.session() as session:
                    await session.execute(
                        text(
                            "INSERT INTO delivery_files "
                            "(media_id, round, sec_user_id, relative_path,"
                            " status, created_at, updated_at) VALUES "
                            "('m1', 'orphan_round', 'sec', 'a/b.mp4',"
                            " 'legacy_confirmed_sent',"
                            " '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
                        )
                    )
                    await session.execute(
                        text(
                            "INSERT INTO delivery_groups "
                            "(key, round, sec_user_id, nickname, create_name,"
                            " status, topic_status, created_at, updated_at) VALUES "
                            "('g1', 'orphan_round', 'sec', 'n', 'c',"
                            " 'ready', 'ready',"
                            " '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
                        )
                    )
                    await session.execute(
                        text(
                            "INSERT INTO download_queue "
                            "(key, round, nickname, sec_user_id, mode, status,"
                            " attempts, created_at, updated_at, extra) VALUES "
                            "('q1', 'orphan_round', 'n', 'sec', 'post',"
                            " 'pending', 0,"
                            " '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z',"
                            " '{}')"
                        )
                    )
                    await session.commit()
            finally:
                await factory.aclose()
            _alembic("upgrade", cfg, "head")
            factory = DatabaseSessionFactory(url)
            try:
                async with factory.session() as session:
                    header = (
                        await session.execute(
                            text(
                                "SELECT note FROM delivery_rounds"
                                " WHERE round = 'orphan_round'"
                            )
                        )
                    ).scalar()
                    assert header == "backfilled by 0004"
                    constraints = (
                        await session.execute(
                            text(
                                "SELECT conname FROM pg_constraint"
                                " WHERE conname LIKE 'fk\\_%\\_round'"
                                " ESCAPE '\\' ORDER BY 1"
                            )
                        )
                    ).scalars().all()
                    assert constraints == [
                        "fk_delivery_files_round",
                        "fk_delivery_groups_round",
                        "fk_download_queue_round",
                    ]
            finally:
                await factory.aclose()
        finally:
            settings_module.settings.database.url = previous
