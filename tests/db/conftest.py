"""Shared fixtures for the database test package.

The ``postgres_url`` session fixture starts one containerised Postgres
for the whole ``tests/db`` package and migrates it to Alembic ``head``.
Per-test isolation is each test's own job (truncate, or use the fakes).
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from testcontainers.community.postgres import PostgresContainer

from alembic import command

ROOT_DIR = Path(__file__).resolve().parents[2]


def _upgrade_to_head(config: Config) -> None:
    """Run ``alembic upgrade head`` on a thread without a loop.

    This fixture is pulled from async test context, so the calling
    thread already runs an event loop -- and ``env.py`` drives its
    own ``asyncio.run``. A worker thread gives it a clean loop.
    """
    errors: list[BaseException] = []

    def _run() -> None:
        try:
            command.upgrade(config, "head")
        except BaseException as exc:  # propagate to the caller
            errors.append(exc)

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join()
    if errors:
        raise errors[0]


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """Start a containerised Postgres and migrate it to ``head``.

    The Alembic environment reads its URL from the already-imported
    ``dyvine.core.settings.settings`` singleton (``cache_clear`` alone
    cannot rebuild it), so the singleton's URL is patched narrowly
    around the upgrade and restored before any test runs.
    """
    import dyvine.core.settings as settings_module

    with PostgresContainer("postgres:16") as container:
        raw_url = container.get_connection_url()
        url = raw_url.replace("postgresql+psycopg2://", "postgresql+asyncpg://")
        previous = settings_module.settings.database.url
        settings_module.settings.database.url = url
        try:
            _upgrade_to_head(Config(str(ROOT_DIR / "alembic.ini")))
        finally:
            settings_module.settings.database.url = previous
        yield url
