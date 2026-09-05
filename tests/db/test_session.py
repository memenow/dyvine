"""Tests for the async session factory."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from dyvine.db import DatabaseSessionFactory


async def test_session_factory_rejects_non_asyncpg_urls() -> None:
    """Only ``postgresql+asyncpg://`` URLs are accepted."""
    with pytest.raises(ValueError, match="postgresql\\+asyncpg"):
        DatabaseSessionFactory("sqlite:///x.db")
    with pytest.raises(ValueError, match="postgresql\\+asyncpg"):
        DatabaseSessionFactory("postgresql+psycopg2://u:p@h/db")


async def test_session_factory_opens_and_closes() -> None:
    """Engine creation is lazy; close is idempotent."""
    factory = DatabaseSessionFactory(
        "postgresql+asyncpg://u:p@localhost:1/db", pool_size=1
    )
    await factory.aclose()
    await factory.aclose()


async def test_session_executes_statements(postgres_url: str) -> None:
    """Sessions from the factory run real queries (container leg)."""
    factory = DatabaseSessionFactory(postgres_url, pool_size=1)
    try:
        async with factory.session() as session:
            value = (await session.execute(text("SELECT 1 AS one"))).scalar()
        assert value == 1
    finally:
        await factory.aclose()
