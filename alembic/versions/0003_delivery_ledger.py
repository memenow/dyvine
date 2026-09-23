"""Persist exact Feishu group, topic, and per-file send checkpoints.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the durable delivery ledgers."""
    op.create_table(
        "delivery_groups",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("round", sa.Text(), nullable=False),
        sa.Column("sec_user_id", sa.Text(), nullable=False),
        sa.Column("nickname", sa.Text(), nullable=False),
        sa.Column("create_name", sa.Text(), nullable=False),
        sa.Column("owner_open_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("create_uuid", sa.Text(), nullable=True),
        sa.Column("create_started_at", sa.Text(), nullable=True),
        sa.Column("chat_id", sa.Text(), nullable=True),
        sa.Column("topic_status", sa.Text(), nullable=False),
        sa.Column("topic_uuid", sa.Text(), nullable=True),
        sa.Column("topic_started_at", sa.Text(), nullable=True),
        sa.Column("topic_message_id", sa.Text(), nullable=True),
        sa.Column("avatar_url", sa.Text(), nullable=True),
        sa.Column("avatar_key", sa.Text(), nullable=True),
        sa.Column("legacy_source_file", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_index(
        "idx_delivery_groups_round_status", "delivery_groups", ["round", "status"]
    )
    op.create_index("idx_delivery_groups_chat", "delivery_groups", ["chat_id"])
    op.create_table(
        "delivery_files",
        sa.Column("media_id", sa.Text(), nullable=False),
        sa.Column("round", sa.Text(), nullable=False),
        sa.Column("sec_user_id", sa.Text(), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.Text(), nullable=True),
        sa.Column("chat_id", sa.Text(), nullable=True),
        sa.Column("parent_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("file_key", sa.Text(), nullable=True),
        sa.Column("send_uuid", sa.Text(), nullable=True),
        sa.Column("send_started_at", sa.Text(), nullable=True),
        sa.Column("message_id", sa.Text(), nullable=True),
        sa.Column("legacy_source_path", sa.Text(), nullable=True),
        sa.Column("legacy_progress_file", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("media_id"),
    )
    op.create_index(
        "idx_delivery_files_sec_status", "delivery_files", ["sec_user_id", "status"]
    )
    op.create_index(
        "idx_delivery_files_sec_path",
        "delivery_files",
        ["sec_user_id", "relative_path"],
    )
    op.create_index(
        "idx_delivery_files_round_status", "delivery_files", ["round", "status"]
    )
    op.create_table(
        "delivery_legacy_evidence",
        sa.Column("evidence_id", sa.Text(), nullable=False),
        sa.Column("source_file", sa.Text(), nullable=False),
        sa.Column("legacy_path", sa.Text(), nullable=False),
        sa.Column("legacy_state", sa.Text(), nullable=False),
        sa.Column("nickname", sa.Text(), nullable=True),
        sa.Column("sec_user_id", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("evidence_id"),
    )
    op.create_index(
        "idx_delivery_legacy_evidence_state",
        "delivery_legacy_evidence",
        ["legacy_state"],
    )
    op.create_index(
        "idx_delivery_legacy_evidence_sec", "delivery_legacy_evidence", ["sec_user_id"]
    )
    op.create_index(
        "idx_delivery_legacy_evidence_path_state",
        "delivery_legacy_evidence",
        ["legacy_path", "legacy_state"],
    )
    op.create_table(
        "legacy_excluded_nicknames",
        sa.Column("nickname", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("nickname"),
    )


def downgrade() -> None:
    """Remove only the delivery ledgers."""
    op.drop_table("legacy_excluded_nicknames")
    op.drop_index(
        "idx_delivery_legacy_evidence_path_state",
        table_name="delivery_legacy_evidence",
    )
    op.drop_index(
        "idx_delivery_legacy_evidence_sec", table_name="delivery_legacy_evidence"
    )
    op.drop_index(
        "idx_delivery_legacy_evidence_state", table_name="delivery_legacy_evidence"
    )
    op.drop_table("delivery_legacy_evidence")
    op.drop_index("idx_delivery_files_round_status", table_name="delivery_files")
    op.drop_index("idx_delivery_files_sec_path", table_name="delivery_files")
    op.drop_index("idx_delivery_files_sec_status", table_name="delivery_files")
    op.drop_table("delivery_files")
    op.drop_index("idx_delivery_groups_chat", table_name="delivery_groups")
    op.drop_index("idx_delivery_groups_round_status", table_name="delivery_groups")
    op.drop_table("delivery_groups")
