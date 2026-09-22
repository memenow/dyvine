"""Hermes-state tables: download_queue + send_status + seeds + profiles + rounds.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-22

Mirrors ``dyvine.db.models`` exactly; ``alembic check`` in CI guards
that the two never drift apart.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the six hermes-state tables with their indexes."""
    op.create_table(
        "download_queue",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("round", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=True),
        sa.Column("nickname", sa.Text(), nullable=False),
        sa.Column("sec_user_id", sa.Text(), nullable=False),
        sa.Column("chat_id", sa.Text(), nullable=True),
        sa.Column("homepage", sa.Text(), nullable=True),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("cutoff", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("operation_id", sa.Text(), nullable=True),
        sa.Column("op_status", sa.Text(), nullable=True),
        sa.Column("op_message", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("serial_group", sa.Text(), nullable=True),
        sa.Column("owner_id", sa.Text(), nullable=True),
        sa.Column("heartbeat_at", sa.Text(), nullable=True),
        sa.Column("extra", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_index(
        "idx_queue_round_status_updated",
        "download_queue",
        ["round", "status", "updated_at"],
        unique=False,
    )
    op.create_index(
        "idx_queue_sec_updated",
        "download_queue",
        ["sec_user_id", "updated_at"],
        unique=False,
    )
    op.create_index(
        "idx_queue_serial_status",
        "download_queue",
        ["serial_group", "status"],
        unique=False,
    )
    op.create_table(
        "send_status",
        sa.Column("nickname", sa.Text(), nullable=False),
        sa.Column("sec_user_id", sa.Text(), nullable=True),
        sa.Column("chat_id", sa.Text(), nullable=True),
        sa.Column("batch", sa.Text(), nullable=True),
        sa.Column("total_files", sa.Integer(), nullable=True),
        sa.Column("sent_files", sa.Integer(), nullable=True),
        sa.Column("failed_files", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("nickname"),
    )
    op.create_index(
        "idx_send_status_sec",
        "send_status",
        ["sec_user_id"],
        unique=False,
    )
    op.create_index(
        "idx_send_status_batch",
        "send_status",
        ["batch"],
        unique=False,
    )
    op.create_table(
        "user_send_status",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("local_files", sa.Integer(), nullable=False),
        sa.Column("sent_files", sa.Integer(), nullable=False),
        sa.Column("failed_files", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("failed_details", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
    )
    op.create_table(
        "seed_accounts",
        sa.Column("sec_user_id", sa.Text(), nullable=False),
        sa.Column("nickname", sa.Text(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("batch", sa.Text(), nullable=True),
        sa.Column("excluded", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("sec_user_id"),
    )
    op.create_index(
        "idx_seed_excluded",
        "seed_accounts",
        ["excluded"],
        unique=False,
    )
    op.create_table(
        "user_profiles",
        sa.Column("sec_user_id", sa.Text(), nullable=False),
        sa.Column("nickname", sa.Text(), nullable=True),
        sa.Column("nickname_raw", sa.Text(), nullable=True),
        sa.Column("avatar_url", sa.Text(), nullable=True),
        sa.Column("signature", sa.Text(), nullable=True),
        sa.Column("signature_raw", sa.Text(), nullable=True),
        sa.Column("uid", sa.Text(), nullable=True),
        sa.Column("short_id", sa.Text(), nullable=True),
        sa.Column("unique_id", sa.Text(), nullable=True),
        sa.Column("room_id", sa.Text(), nullable=True),
        sa.Column("city", sa.Text(), nullable=True),
        sa.Column("country", sa.Text(), nullable=True),
        sa.Column("ip_location", sa.Text(), nullable=True),
        sa.Column("school_name", sa.Text(), nullable=True),
        sa.Column("gender", sa.Integer(), nullable=True),
        sa.Column("user_age", sa.Integer(), nullable=True),
        sa.Column("aweme_count", sa.Integer(), nullable=True),
        sa.Column("favoriting_count", sa.Integer(), nullable=True),
        sa.Column("follower_count", sa.Integer(), nullable=True),
        sa.Column("following_count", sa.Integer(), nullable=True),
        sa.Column("total_favorited", sa.Integer(), nullable=True),
        sa.Column("mplatform_followers_count", sa.Integer(), nullable=True),
        sa.Column("mix_count", sa.Integer(), nullable=True),
        sa.Column("live_status", sa.Integer(), nullable=True),
        sa.Column("is_ban", sa.Boolean(), nullable=True),
        sa.Column("is_block", sa.Boolean(), nullable=True),
        sa.Column("is_blocked", sa.Boolean(), nullable=True),
        sa.Column("is_star", sa.Boolean(), nullable=True),
        sa.Column("last_aweme_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("sec_user_id"),
    )
    op.create_index(
        "idx_profiles_nickname",
        "user_profiles",
        ["nickname"],
        unique=False,
    )
    op.create_table(
        "delivery_rounds",
        sa.Column("round", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("round"),
    )


def downgrade() -> None:
    """Drop the six hermes-state tables."""
    op.drop_table("delivery_rounds")
    op.drop_index("idx_profiles_nickname", table_name="user_profiles")
    op.drop_table("user_profiles")
    op.drop_index("idx_seed_excluded", table_name="seed_accounts")
    op.drop_table("seed_accounts")
    op.drop_table("user_send_status")
    op.drop_index("idx_send_status_batch", table_name="send_status")
    op.drop_index("idx_send_status_sec", table_name="send_status")
    op.drop_table("send_status")
    op.drop_index("idx_queue_serial_status", table_name="download_queue")
    op.drop_index("idx_queue_sec_updated", table_name="download_queue")
    op.drop_index("idx_queue_round_status_updated", table_name="download_queue")
    op.drop_table("download_queue")
