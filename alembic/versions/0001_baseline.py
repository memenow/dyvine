"""Baseline schema: operations + watch_subscriptions.

Revision ID: 0001
Revises: None
Create Date: 2026-09-05

Mirrors ``dyvine.db.models`` exactly; ``alembic check`` in CI guards
that the two never drift apart.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create both tables with their indexes."""
    op.create_table(
        "operations",
        sa.Column("operation_id", sa.Text(), nullable=False),
        sa.Column("operation_type", sa.Text(), nullable=False),
        sa.Column("subject_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("progress", sa.Float(), nullable=True),
        sa.Column("total_items", sa.Integer(), nullable=True),
        sa.Column("completed_items", sa.Integer(), nullable=True),
        sa.Column("download_path", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=False),
        sa.Column("owner_id", sa.Text(), nullable=True),
        sa.Column("heartbeat_at", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("operation_id"),
    )
    op.create_index(
        "idx_operations_subject_type_updated",
        "operations",
        ["subject_id", "operation_type", "updated_at"],
        unique=False,
    )
    op.create_index(
        "idx_operations_status_updated",
        "operations",
        ["status", "updated_at"],
        unique=False,
    )
    op.create_index(
        "idx_operations_status_heartbeat",
        "operations",
        ["status", "heartbeat_at"],
        unique=False,
    )
    op.create_table(
        "watch_subscriptions",
        sa.Column("subscription_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("live_poll_seconds", sa.Integer(), nullable=False),
        sa.Column("post_poll_seconds", sa.Integer(), nullable=False),
        sa.Column("checkpoint", postgresql.JSONB(), nullable=False),
        sa.Column("last_live_check", sa.Text(), nullable=True),
        sa.Column("last_post_check", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("subscription_id"),
        sa.UniqueConstraint("user_id"),
    )
    op.create_index(
        "idx_watch_enabled_created",
        "watch_subscriptions",
        ["enabled", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    """Drop both tables."""
    op.drop_index("idx_watch_enabled_created", table_name="watch_subscriptions")
    op.drop_table("watch_subscriptions")
    op.drop_index("idx_operations_status_heartbeat", table_name="operations")
    op.drop_index("idx_operations_status_updated", table_name="operations")
    op.drop_index("idx_operations_subject_type_updated", table_name="operations")
    op.drop_table("operations")
