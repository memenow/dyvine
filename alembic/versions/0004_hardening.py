"""Harden the schema: server defaults, stamp CHECKs, round foreign keys.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-25

- Counters, flags, and JSONB columns gain ``DEFAULT``\\ s matching the
  ORM client defaults, so bare-SQL inserts outside the ORM cannot trip
  ``NOT NULL``. (``MutableDict`` tracking is client-side only and
  needs no DDL.)
- Tables that only ever carry live UTC ISO stamps gain regex ``CHECK``
  constraints pinning that format, which the sweep/claim queries rely
  on for lexicographic ordering. Tables preserving legacy stamps
  verbatim (operations, watch, queue, send-status, seeds, profiles)
  stay unconstrained -- only their heartbeat columns (live-only) are
  checked. Timestamps deliberately get no ``DEFAULT now()``: its
  ``YYYY-MM-DD HH:MM:SS+TZ`` rendering would break TEXT ordering.
- ``download_queue``/``delivery_groups``/``delivery_files`` gain real
  foreign keys to ``delivery_rounds``: every child insert flows
  through an ``enqueue_round``-first path. Pre-existing children may
  name rounds with no header (one-shot scripts and adoptions wrote
  ``legacy``/``feishu_adopted``-style rounds directly), so the upgrade
  backfills a header for every referenced round before constraining.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Self-contained copy of ``dyvine.db.models._ISO_UTC_TEXT_RE``.
#: Migrations must not import the models (history must keep running
#: even as the models evolve), so the pattern is duplicated here.
_ISO_UTC_TEXT_RE = (
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}" r"(\.[0-9]+)?(\+00:00|Z)$"
)

_SERVER_DEFAULTS: tuple[tuple[str, str, str], ...] = (
    # (table, column, SQL default). Plain strings render as quoted
    # literals (``'pending'``), which Postgres coerces to the column
    # type -- including ``'{}'`` to JSONB.
    ("operations", "metadata", "{}"),
    ("watch_subscriptions", "checkpoint", "{}"),
    ("download_queue", "attempts", "0"),
    ("download_queue", "extra", "{}"),
    ("user_send_status", "local_files", "0"),
    ("user_send_status", "sent_files", "0"),
    ("user_send_status", "failed_files", "0"),
    ("user_send_status", "status", "pending"),
    ("user_send_status", "failed_details", ""),
    ("seed_accounts", "source", "seed"),
    ("seed_accounts", "excluded", "false"),
)

_ISO_CHECKS: tuple[tuple[str, str], ...] = (
    # (table, column); constraint name mirrors the models helper.
    ("operations", "heartbeat_at"),
    ("download_queue", "heartbeat_at"),
    ("delivery_rounds", "created_at"),
    ("delivery_rounds", "updated_at"),
    ("delivery_groups", "created_at"),
    ("delivery_groups", "updated_at"),
    ("delivery_groups", "create_started_at"),
    ("delivery_groups", "topic_started_at"),
    ("delivery_files", "created_at"),
    ("delivery_files", "updated_at"),
    ("delivery_files", "send_started_at"),
    ("delivery_legacy_evidence", "created_at"),
    ("delivery_legacy_evidence", "updated_at"),
    ("legacy_excluded_nicknames", "created_at"),
)

_FOREIGN_KEYS: tuple[tuple[str, str], ...] = (
    # (child table, constraint name) -> delivery_rounds(round).
    ("download_queue", "fk_download_queue_round"),
    ("delivery_groups", "fk_delivery_groups_round"),
    ("delivery_files", "fk_delivery_files_round"),
)


def upgrade() -> None:
    """Add defaults, stamp CHECKs, and round foreign keys."""
    for table, column, default in _SERVER_DEFAULTS:
        op.alter_column(table, column, server_default=default)
    for table, column in _ISO_CHECKS:
        op.create_check_constraint(
            f"ck_{table}_{column}_iso",
            table,
            f"{column} ~ '{_ISO_UTC_TEXT_RE}'",
        )
    # Backfill headers for rounds children already reference: the stamp
    # complies with the CHECKs added above, and ON CONFLICT absorbs a
    # round orphaned by more than one child table.
    stamp = "to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')"
    for table, _name in _FOREIGN_KEYS:
        op.execute(
            sa.text(
                "INSERT INTO delivery_rounds (round, note, created_at, updated_at) "
                f"SELECT DISTINCT child.round, 'backfilled by 0004', "
                f"{stamp}, {stamp} "
                f"FROM {table} AS child "
                "LEFT JOIN delivery_rounds AS r ON r.round = child.round "
                "WHERE r.round IS NULL AND child.round IS NOT NULL "
                "ON CONFLICT (round) DO NOTHING"
            )
        )
    for table, name in _FOREIGN_KEYS:
        op.create_foreign_key(
            name,
            table,
            "delivery_rounds",
            ["round"],
            ["round"],
        )


def downgrade() -> None:
    """Drop the 0004 constraints and defaults."""
    for table, name in reversed(_FOREIGN_KEYS):
        op.drop_constraint(name, table, type_="foreignkey")
    for table, column in reversed(_ISO_CHECKS):
        op.drop_constraint(f"ck_{table}_{column}_iso", table, type_="check")
    for table, column, _default in reversed(_SERVER_DEFAULTS):
        op.alter_column(table, column, server_default=None)
