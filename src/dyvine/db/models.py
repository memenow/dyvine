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


class DownloadQueueRow(Base):
    """Durable per-account download queue entry (one row per round+user).

    Migrated from the legacy ``download_queue.json`` ``entries`` list, whose
    ``key`` (``{round}:{sec_user_id}``) is the primary key. Hot query fields
    are real columns; the long tail of 34 sparse legacy keys lands in
    ``extra`` so no migrated field is ever dropped.
    """

    __tablename__ = "download_queue"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    round: Mapped[str] = mapped_column("round", Text, nullable=False)
    kind: Mapped[str | None] = mapped_column(Text, nullable=True)
    nickname: Mapped[str] = mapped_column(Text, nullable=False)
    sec_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    chat_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    homepage: Mapped[str | None] = mapped_column(Text, nullable=True)
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    cutoff: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    operation_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    op_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    op_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    serial_group: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    heartbeat_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        # Claim scan: pending entries of a round, oldest first.
        Index("idx_queue_round_status_updated", "round", "status", "updated_at"),
        # Per-user history across rounds.
        Index("idx_queue_sec_updated", "sec_user_id", "updated_at"),
        # Same-nickname mutual exclusion during claims.
        Index("idx_queue_serial_status", "serial_group", "status"),
    )


class SendStatusRow(Base):
    """Per-account Feishu delivery counters (migrated 1:1 from SQLite).

    ``batch`` is TEXT because the legacy column mixes integers (``20260806``)
    with labels (``'batch'``, ``'resend8'``); values migrate verbatim.
    """

    __tablename__ = "send_status"

    nickname: Mapped[str] = mapped_column(Text, primary_key=True)
    sec_user_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    chat_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    batch: Mapped[str | None] = mapped_column(Text, nullable=True)
    total_files: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sent_files: Mapped[int | None] = mapped_column(Integer, nullable=True)
    failed_files: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("idx_send_status_sec", "sec_user_id"),
        Index("idx_send_status_batch", "batch"),
    )


class UserSendStatusRow(Base):
    """Legacy per-username delivery counters (migrated read-only).

    Stale since 2026-08-12 upstream; kept so historical reports keep
    resolving, not for new writes.
    """

    __tablename__ = "user_send_status"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    local_files: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sent_files: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_files: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    failed_details: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class SeedAccountRow(Base):
    """Seed universe: every known account plus its exclusion flag."""

    __tablename__ = "seed_accounts"

    sec_user_id: Mapped[str] = mapped_column(Text, primary_key=True)
    nickname: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(Text, nullable=False, default="seed")
    batch: Mapped[str | None] = mapped_column(Text, nullable=True)
    excluded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (Index("idx_seed_excluded", "excluded"),)


class UserProfileRow(Base):
    """Cached Douyin profile snapshot (migrated 1:1 from ``user_info_web``)."""

    __tablename__ = "user_profiles"

    sec_user_id: Mapped[str] = mapped_column(Text, primary_key=True)
    nickname: Mapped[str | None] = mapped_column(Text, nullable=True)
    nickname_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    signature: Mapped[str | None] = mapped_column(Text, nullable=True)
    signature_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    uid: Mapped[str | None] = mapped_column(Text, nullable=True)
    short_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    unique_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    room_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    city: Mapped[str | None] = mapped_column(Text, nullable=True)
    country: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_location: Mapped[str | None] = mapped_column(Text, nullable=True)
    school_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    gender: Mapped[int | None] = mapped_column(Integer, nullable=True)
    user_age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    aweme_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    favoriting_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    follower_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    following_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_favorited: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mplatform_followers_count: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    mix_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    live_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_ban: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    is_block: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    is_blocked: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    is_star: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_aweme_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (Index("idx_profiles_nickname", "nickname"),)


class DeliveryRoundRow(Base):
    """Delivery round header; entries live in ``download_queue`` by round."""

    __tablename__ = "delivery_rounds"

    round: Mapped[str] = mapped_column("round", Text, primary_key=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
