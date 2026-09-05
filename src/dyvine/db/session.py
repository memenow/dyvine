"""Async engine and session factory for the Postgres backend.

One :class:`DatabaseSessionFactory` per process: it owns the
``asyncpg`` connection pool and hands out short-lived sessions. Every
repository method opens its own session, so callers never share
transactional state across awaits.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


class DatabaseSessionFactory:
    """Owns the async engine; mints sessions on demand.

    Args:
        database_url: SQLAlchemy URL (``postgresql+asyncpg://...``).
        pool_size: Steady-state pooled connections per process.
        pool_timeout: Seconds to wait for a pooled connection.
    """

    def __init__(
        self, database_url: str, *, pool_size: int = 5, pool_timeout: float = 30.0
    ) -> None:
        """Create the engine and session maker without connecting.

        Engine creation performs no I/O; the first checkout opens the
        pool. ``pool_pre_ping`` keeps long-lived pods safe across
        database restarts at the cost of one cheap probe per checkout.

        Raises:
            ValueError: If ``database_url`` is not an asyncpg URL. Failing
                fast here turns a misconfigured ``DATABASE_URL`` (e.g. a
                leftover ``sqlite://`` path) into a loud boot error
                instead of a confusing connect-time failure.
        """
        if not database_url.startswith("postgresql+asyncpg://"):
            raise ValueError("database_url must use the postgresql+asyncpg:// scheme")
        self._engine: AsyncEngine = create_async_engine(
            database_url,
            pool_size=pool_size,
            pool_timeout=pool_timeout,
            pool_pre_ping=True,
        )
        self._sessions: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    @property
    def engine(self) -> AsyncEngine:
        """The underlying async engine (for health probes and tests)."""
        return self._engine

    def session(self) -> AsyncSession:
        """Open a new session; the caller owns its lifecycle."""
        return self._sessions()

    async def aclose(self) -> None:
        """Dispose the pool; safe to call more than once."""
        await self._engine.dispose()
