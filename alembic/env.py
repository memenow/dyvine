"""Alembic environment: async engine over the configured DATABASE_URL.

The database URL comes from :mod:`dyvine.core.settings` (env vars /
``.env``), never from ``alembic.ini``: the ini file only points at
the source tree and logging config. In-process drivers (tests) may
override it per-run via the ``dyvine.sqlalchemy.url`` config
attribute instead of mutating the global settings singleton.
"""

from __future__ import annotations

import asyncio
import threading
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

#: Per-run URL override key for in-process drivers. Read before the
#: settings singleton so tests can migrate an arbitrary database
#: without patching global state.
URL_OVERRIDE_ATTRIBUTE = "dyvine.sqlalchemy.url"


def _database_url() -> str:
    """Return the override URL when set, else the settings URL."""
    override = config.attributes.get(URL_OVERRIDE_ATTRIBUTE)
    if isinstance(override, str) and override:
        return override
    return settings.database.url


config.set_main_option("sqlalchemy.url", _database_url())
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (SQL script emission)."""
    context.configure(
        url=_database_url(),
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
    try:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await connectable.dispose()


def run_async_migrations() -> None:
    """Bridge the sync Alembic runner onto an event loop.

    ``asyncio.run`` refuses to run inside a running loop, which is
    exactly where in-process drivers (the test suite) call from, so a
    running loop detours through a worker thread that owns a fresh
    one. The join is bounded: a hung migration fails the run instead
    of hanging the caller forever.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(run_migrations_online())
        return
    errors: list[BaseException] = []

    def _run() -> None:
        try:
            asyncio.run(run_migrations_online())
        except BaseException as exc:  # propagate to the caller
            errors.append(exc)

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(timeout=300.0)
    if worker.is_alive():
        raise TimeoutError("Alembic migration timed out after 300s")
    if errors:
        raise errors[0]


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_async_migrations()
