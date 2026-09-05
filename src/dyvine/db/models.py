"""SQLAlchemy 2.0 table models for the Postgres backend.

Design notes:

- Timestamps stay ISO-8601 UTC *strings* (``TEXT``), exactly like the
  previous SQLite schema. The service contract (``db.records``) speaks
  strings, lexicographic ordering matches chronological ordering for
  UTC ISO values, and zero timezone conversion means zero drift.
- ``metadata``/``checkpoint`` use ``JSONB``. Note the ``metadata_``
  attribute name: ``metadata`` is reserved by SQLAlchemy's
  ``DeclarativeBase``.
- ``owner_id``/``heartbeat_at`` on operations implement crash-safe
  multi-replica semantics: every row records which replica owns it and
  when that replica last proved liveness, so a boot/periodic sweep can
  fail only genuinely orphaned rows instead of every in-flight row.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Boolean, Float, Index, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base shared by every Dyvine table."""

    pass


class OperationRow(Base):
    """Persisted asynchronous operation state."""

    __tablename__ = "operations"

    operation_id: Mapped[str] = mapped_column(Text, primary_key=True)
    operation_type: Mapped[str] = mapped_column(Text, nullable=False)
    subject_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    progress: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_items: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completed_items: Mapped[int | None] = mapped_column(Integer, nullable=True)
    download_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False)
    owner_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    heartbeat_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        # Latest-operation lookup: WHERE subject + type ORDER BY updated
        # DESC. Postgres serves the DESC order with a backward index
        # scan, so no explicit DESC key is needed.
        Index(
            "idx_operations_subject_type_updated",
            "subject_id",
            "operation_type",
            "updated_at",
        ),
        # Retention purge: terminal rows older than a cutoff.
        Index("idx_operations_status_updated", "status", "updated_at"),
        # Orphan sweep: active rows whose heartbeat went stale.
        Index("idx_operations_status_heartbeat", "status", "heartbeat_at"),
    )


class WatchSubscriptionRow(Base):
    """Persisted watch subscription with its resume checkpoint."""

    __tablename__ = "watch_subscriptions"

    subscription_id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    live_poll_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    post_poll_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    checkpoint: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    last_live_check: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_post_check: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (Index("idx_watch_enabled_created", "enabled", "created_at"),)
