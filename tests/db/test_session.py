"""Tests for the async session factory."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.pool import NullPool, QueuePool

from dyvine.db import DatabaseSessionFactory


async def test_session_factory_rejects_non_asyncpg_urls() -> None:
    """Only ``postgresql+asyncpg://`` URLs are accepted."""
    with pytest.raises(ValueError, match="postgresql\\+asyncpg"):
        DatabaseSessionFactory("sqlite:///x.db")
    with pytest.raises(ValueError, match="postgresql\\+asyncpg"):
        DatabaseSessionFactory("postgresql+psycopg2://u:p@h/db")


async def test_session_factory_defaults_to_null_pool() -> None:
    """The default holds no idle connections (serverless-friendly)."""
    factory = DatabaseSessionFactory("postgresql+asyncpg://u:p@localhost:1/db")
    try:
        assert isinstance(factory.engine.pool, NullPool)
    finally:
        await factory.aclose()


async def test_session_factory_null_pool_rejects_queue_knobs() -> None:
    """Queue tunables contradict ``pool_class="null"`` and fail fast.

    ``NullPool`` opens a fresh connection per checkout, so a caller
    passing ``pool_size`` believes in a cap that does not exist;
    accepting it silently would mask capacity expectations.
    """
    with pytest.raises(ValueError, match="queue'-only knobs"):
        DatabaseSessionFactory(
            "postgresql+asyncpg://u:p@localhost:1/db",
            pool_class="null",
            pool_size=9,
        )
    with pytest.raises(ValueError, match="pool_pre_ping"):
        DatabaseSessionFactory(
            "postgresql+asyncpg://u:p@localhost:1/db",
            pool_class="null",
            pool_pre_ping=False,
        )


async def test_session_factory_queue_pool_honours_knobs() -> None:
    """``pool_class="queue"`` wires size/overflow/recycle/pre-ping."""
    factory = DatabaseSessionFactory(
        "postgresql+asyncpg://u:p@localhost:1/db",
        pool_class="queue",
        pool_size=3,
        max_overflow=4,
        pool_timeout=7.0,
        pool_recycle=60.0,
        pool_pre_ping=False,
    )
    try:
        pool = factory.engine.pool
        assert isinstance(pool, QueuePool)
        assert pool._max_overflow == 4
        assert pool._timeout == 7.0
        assert pool._recycle == 60.0
        assert pool._pre_ping is False
    finally:
        await factory.aclose()


async def test_session_factory_rejects_unknown_pool_class() -> None:
    """A misspelled pool class fails fast instead of silently pooling."""
    with pytest.raises(ValueError, match="pool_class"):
        DatabaseSessionFactory(
            "postgresql+asyncpg://u:p@localhost:1/db",
            pool_class="bogus",  # type: ignore[arg-type]
        )


async def test_session_factory_opens_and_closes() -> None:
    """Engine creation is lazy; close is idempotent."""
    factory = DatabaseSessionFactory("postgresql+asyncpg://u:p@localhost:1/db")
    await factory.aclose()
    await factory.aclose()


async def test_session_executes_statements(postgres_url: str) -> None:
    """Sessions from the factory run real queries (container leg)."""
    factory = DatabaseSessionFactory(postgres_url)
    try:
        async with factory.session() as session:
            value = (await session.execute(text("SELECT 1 AS one"))).scalar()
        assert value == 1
    finally:
        await factory.aclose()


async def test_session_executes_statements_with_queue_pool(
    postgres_url: str,
) -> None:
    """Queue pools run the same queries (container leg)."""
    factory = DatabaseSessionFactory(
        postgres_url, pool_class="queue", pool_size=1, max_overflow=0
    )
    try:
        async with factory.session() as session:
            value = (await session.execute(text("SELECT 1 AS one"))).scalar()
        assert value == 1
    finally:
        await factory.aclose()
