"""Async engine and session factory for the Postgres backend.

One :class:`DatabaseSessionFactory` per process: it owns the
``asyncpg`` connection pool and hands out short-lived sessions. Every
repository method opens its own session, so callers never share
transactional state across awaits.
"""

from __future__ import annotations

from typing import Literal

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

# Defaults for the ``"queue"``-only knobs, mirrored from the
# ``__init__`` signature. A ``"null"`` factory holding anything but
# these values is a contradiction (the caller tuned a pool that does
# not exist), so the constructor rejects it instead of silently
# dropping the knobs.
_QUEUE_KNOB_DEFAULTS: dict[str, int | float | bool] = {
    "pool_size": 5,
    "max_overflow": 2,
    "pool_timeout": 30.0,
    "pool_recycle": 300.0,
    "pool_pre_ping": True,
}


class DatabaseSessionFactory:
    """Owns the async engine; mints sessions on demand.

    Args:
        database_url: SQLAlchemy URL (``postgresql+asyncpg://...``).
        pool_class: ``"null"`` opens a fresh connection per checkout and
            holds none while idle (the serverless default); ``"queue"``
            keeps a capped pool. The remaining knobs apply to
            ``"queue"`` only and contradict ``"null"`` unless left at
            their defaults.
        pool_size: Steady-state pooled connections per process.
        max_overflow: Burst connections beyond ``pool_size``.
        pool_timeout: Seconds to wait for a pooled connection.
        pool_recycle: Discard pooled connections older than this many
            seconds on next checkout; ``-1`` disables.
        pool_pre_ping: Probe a pooled connection before each checkout.
    """

    def __init__(
        self,
        database_url: str,
        *,
        pool_class: Literal["queue", "null"] = "null",
        pool_size: int = 5,
        max_overflow: int = 2,
        pool_timeout: float = 30.0,
        pool_recycle: float = 300.0,
        pool_pre_ping: bool = True,
    ) -> None:
        """Create the engine and session maker without connecting.

        Engine creation performs no I/O; the first checkout opens the
        first connection. ``"null"`` never holds idle connections, so a
        quiet process keeps zero database connections open; ``"queue"``
        holds ``pool_size`` idle connections and uses ``pool_pre_ping``
        to stay safe across database restarts at the cost of one cheap
        probe per checkout.

        Raises:
            ValueError: If ``database_url`` is not an asyncpg URL,
                ``pool_class`` is unknown, or a ``"queue"``-only knob
                is set to a non-default value under ``pool_class="null"``
                (where it would be silently dropped). Failing fast here
                turns misconfiguration into a loud boot error instead
                of a confusing connect-time failure.
        """
        if not database_url.startswith("postgresql+asyncpg://"):
            raise ValueError("database_url must use the postgresql+asyncpg:// scheme")
        if pool_class == "null":
            provided = {
                "pool_size": pool_size,
                "max_overflow": max_overflow,
                "pool_timeout": pool_timeout,
                "pool_recycle": pool_recycle,
                "pool_pre_ping": pool_pre_ping,
            }
            contradicted = sorted(
                name
                for name, default in _QUEUE_KNOB_DEFAULTS.items()
                if provided[name] != default
            )
            if contradicted:
                raise ValueError(
                    f"{', '.join(contradicted)} are 'queue'-only knobs that "
                    "contradict pool_class='null' (NullPool opens a fresh "
                    "connection per checkout and holds none); use "
                    "pool_class='queue' or leave them at their defaults"
                )
            self._engine: AsyncEngine = create_async_engine(
                database_url, poolclass=NullPool
            )
        elif pool_class == "queue":
            self._engine = create_async_engine(
                database_url,
                pool_size=pool_size,
                max_overflow=max_overflow,
                pool_timeout=pool_timeout,
                pool_recycle=pool_recycle,
                pool_pre_ping=pool_pre_ping,
            )
        else:
            raise ValueError("pool_class must be 'queue' or 'null'")
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
