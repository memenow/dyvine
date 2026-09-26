"""SQLAlchemy 2.0 table models for the Postgres backend.

Design notes:

- Timestamps stay ISO-8601 UTC *strings* (``TEXT``), exactly like the
  previous SQLite schema. The service contract (``db.records``) speaks
  strings, lexicographic ordering matches chronological ordering for
  UTC ISO values, and zero timezone conversion means zero drift.
- Every live writer emits ``datetime.now(UTC).isoformat()``. Tables
  that only ever see live writes additionally carry a ``CHECK``
  constraint pinning that format (see ``_ISO_UTC_TEXT_RE``); tables
  that preserve legacy stamps verbatim (operations, watch, queue,
  send-status, seeds, profiles) cannot be constrained without
  rewriting history, so their ordering is best-effort for migrated
  rows and exact for new ones. ``cutoff`` is deliberately naive local
  ISO and is never constrained.
- Timestamps have no ``server_default`` on purpose: ``now()`` renders
  ``YYYY-MM-DD HH:MM:SS+TZ``, which would silently break the
  lexicographic ordering the queries rely on. Repositories always pass
  stamps explicitly.
- Counters, flags, and JSONB columns carry matching client
  ``default=`` and ``server_default`` so bare-SQL inserts outside the
  ORM cannot trip ``NOT NULL``.
- ``metadata``/``checkpoint``/``extra`` use ``MutableDict.as_mutable``
  ``JSONB`` so in-place dict edits are tracked; whole-replace writes
  keep working unchanged. Note the ``metadata_`` attribute name:
  ``metadata`` is reserved by SQLAlchemy's ``DeclarativeBase``.
- ``owner_id``/``heartbeat_at`` on operations implement crash-safe
  multi-replica semantics: every row records which replica owns it and
  when that replica last proved liveness, so a boot/periodic sweep can
  fail only genuinely orphaned rows instead of every in-flight row.
- ``download_queue``/``delivery_groups``/``delivery_files`` reference
  ``delivery_rounds`` via real foreign keys: every child insert flows
  through an ``enqueue_round``-first path, so the parent always
  exists. ``sec_user_id`` is deliberately *not* a foreign key to
  ``seed_accounts``: legacy and watch-sourced rows legitimately name
  accounts outside the seed universe.
- The physical ``"round"`` column keeps its name on purpose. Renaming
  it would churn every migration, index, and legacy payload for zero
  behavior gain: SQLAlchemy quotes the identifier everywhere, no
  hand-written Postgres SQL names the column, and the attribute never
  shadows the ``round()`` builtin (attribute access only). The
  ``round=`` parameter names likewise never collide with a ``round()``
  call in their bodies.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

#: POSIX regex pinning the UTC ISO-8601 text format every live stamp
#: writer emits (``datetime.now(UTC).isoformat()``). Microseconds are
#: optional (``isoformat`` omits them when zero); any other shape --
#: naive stamps, non-UTC offsets, date-only values -- breaks the
#: lexicographic ordering the sweep/claim queries rely on.
_ISO_UTC_TEXT_RE = (
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}" r"(\.[0-9]+)?(\+00:00|Z)$"
)


def _iso_check(table: str, *columns: str) -> list[CheckConstraint]:
    """Build one ISO-stamp ``CHECK`` per column (NULLs pass through)."""
    return [
        CheckConstraint(
            f"{column} ~ '{_ISO_UTC_TEXT_RE}'",
            name=f"ck_{table}_{column}_iso",
        )
        for column in columns
    ]


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
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        MutableDict.as_mutable(JSONB),
        nullable=False,
        default=dict,
        server_default="{}",
    )
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
        # Only the heartbeat is constrained: created/updated preserve
        # legacy stamps verbatim (see module notes).
        *_iso_check("operations", "heartbeat_at"),
    )


class WatchSubscriptionRow(Base):
    """Persisted watch subscription with its resume checkpoint."""

    __tablename__ = "watch_subscriptions"

    subscription_id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    live_poll_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    post_poll_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    checkpoint: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSONB),
        nullable=False,
        default=dict,
        server_default="{}",
    )
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
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    serial_group: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    heartbeat_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSONB),
        nullable=False,
        default=dict,
        server_default="{}",
    )
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        # Claim scan: pending entries of a round, oldest first.
        Index("idx_queue_round_status_updated", "round", "status", "updated_at"),
        # Per-user history across rounds.
        Index("idx_queue_sec_updated", "sec_user_id", "updated_at"),
        # Same-nickname mutual exclusion during claims.
        Index("idx_queue_serial_status", "serial_group", "status"),
        ForeignKeyConstraint(
            ["round"], ["delivery_rounds.round"], name="fk_download_queue_round"
        ),
        # Only the heartbeat is constrained: created/updated preserve
        # legacy stamps verbatim (see module notes).
        *_iso_check("download_queue", "heartbeat_at"),
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
    local_files: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    sent_files: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    failed_files: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="pending", server_default="pending"
    )
    failed_details: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class SeedAccountRow(Base):
    """Seed universe: every known account plus its exclusion flag."""

    __tablename__ = "seed_accounts"

    sec_user_id: Mapped[str] = mapped_column(Text, primary_key=True)
    nickname: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(
        Text, nullable=False, default="seed", server_default="seed"
    )
    batch: Mapped[str | None] = mapped_column(Text, nullable=True)
    excluded: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
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

    __table_args__ = (
        # Round headers only ever carry live stamps (the migration
        # backfills fresh ones), so both columns are constrained.
        *_iso_check("delivery_rounds", "created_at", "updated_at"),
    )


class DeliveryGroupRow(Base):
    """One Feishu group and topic for a round/account pair."""

    __tablename__ = "delivery_groups"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    round: Mapped[str] = mapped_column("round", Text, nullable=False)
    sec_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    nickname: Mapped[str] = mapped_column(Text, nullable=False)
    create_name: Mapped[str] = mapped_column(Text, nullable=False)
    owner_open_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    create_uuid: Mapped[str | None] = mapped_column(Text, nullable=True)
    create_started_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    chat_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    topic_status: Mapped[str] = mapped_column(Text, nullable=False)
    topic_uuid: Mapped[str | None] = mapped_column(Text, nullable=True)
    topic_started_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    topic_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    avatar_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    legacy_source_file: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("idx_delivery_groups_round_status", "round", "status"),
        Index("idx_delivery_groups_chat", "chat_id"),
        ForeignKeyConstraint(
            ["round"], ["delivery_rounds.round"], name="fk_delivery_groups_round"
        ),
        # Groups are written only through the ledger with live stamps.
        *_iso_check(
            "delivery_groups",
            "created_at",
            "updated_at",
            "create_started_at",
            "topic_started_at",
        ),
    )


class DeliveryFileRow(Base):
    """A stable media identity and its sole automatic Feishu send attempt."""

    __tablename__ = "delivery_files"

    media_id: Mapped[str] = mapped_column(Text, primary_key=True)
    round: Mapped[str] = mapped_column("round", Text, nullable=False)
    sec_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    chat_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    file_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    send_uuid: Mapped[str | None] = mapped_column(Text, nullable=True)
    send_started_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    legacy_source_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    legacy_progress_file: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("idx_delivery_files_sec_status", "sec_user_id", "status"),
        Index("idx_delivery_files_sec_path", "sec_user_id", "relative_path"),
        Index("idx_delivery_files_round_status", "round", "status"),
        ForeignKeyConstraint(
            ["round"], ["delivery_rounds.round"], name="fk_delivery_files_round"
        ),
        # Files are written only through the ledger with live stamps.
        *_iso_check("delivery_files", "created_at", "updated_at", "send_started_at"),
    )


class DeliveryLegacyEvidenceRow(Base):
    """Unresolved legacy evidence; this table never drives auto-delivery."""

    __tablename__ = "delivery_legacy_evidence"

    evidence_id: Mapped[str] = mapped_column(Text, primary_key=True)
    source_file: Mapped[str] = mapped_column(Text, nullable=False)
    legacy_path: Mapped[str] = mapped_column(Text, nullable=False)
    legacy_state: Mapped[str] = mapped_column(Text, nullable=False)
    nickname: Mapped[str | None] = mapped_column(Text, nullable=True)
    sec_user_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("idx_delivery_legacy_evidence_state", "legacy_state"),
        Index("idx_delivery_legacy_evidence_sec", "sec_user_id"),
        Index("idx_delivery_legacy_evidence_path_state", "legacy_path", "legacy_state"),
        # Evidence rows are written only through the ledger.
        *_iso_check("delivery_legacy_evidence", "created_at", "updated_at"),
    )


class LegacyExcludedNicknameRow(Base):
    """Legacy nickname-level exclusions, including future seed accounts."""

    __tablename__ = "legacy_excluded_nicknames"

    nickname: Mapped[str] = mapped_column(Text, primary_key=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (*_iso_check("legacy_excluded_nicknames", "created_at"),)
