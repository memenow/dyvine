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
    factory = DatabaseSessionFactory(url, pool_size=1)
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
