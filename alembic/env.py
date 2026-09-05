"""Alembic environment: async engine over the configured DATABASE_URL.

The database URL always comes from :mod:`dyvine.core.settings` (env
vars / ``.env``), never from ``alembic.ini``: the ini file only points
at the source tree (``prepend_sys_path = src``) and logging config.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from dyvine.core.settings import settings
from dyvine.db.models import Base

config = context.config

if config.config_file_name is not None:
    # Keep existing loggers alive: the default
    # ``disable_existing_loggers=True`` would silence every logger
    # configured before the migration runs (notably when the test
    # suite drives Alembic in-process).
    fileConfig(config.config_file_name, disable_existing_loggers=False)

config.set_main_option("sqlalchemy.url", settings.database.url)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (SQL script emission)."""
    context.configure(
        url=settings.database.url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run migrations inside a ``run_sync`` worker (sync context)."""
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations against a live database via asyncpg."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_async_migrations() -> None:
    """Bridge the sync Alembic runner onto an event loop."""
    asyncio.run(run_migrations_online())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_async_migrations()
