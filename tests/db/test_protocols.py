"""Pins between the protocol Literals and their status frozensets."""

from __future__ import annotations

from typing import get_args

from dyvine.db.protocols import (
    ACTIVE_STATUSES,
    QUEUE_ACTIVE_STATUSES,
    QUEUE_CLAIMABLE_STATUSES,
    TERMINAL_STATUSES,
    OperationStatus,
    QueueEntryStatus,
)
from dyvine.schemas.operations import OperationStatus as SchemaOperationStatus


def test_operation_status_sets_match_the_literal() -> None:
    """``ACTIVE``/``TERMINAL`` partition ``OperationStatus`` exactly."""
    universe = set(get_args(OperationStatus))
    assert ACTIVE_STATUSES | TERMINAL_STATUSES == universe
    assert ACTIVE_STATUSES & TERMINAL_STATUSES == set()


def test_queue_status_sets_match_the_literal() -> None:
    """Queue frozensets stay subsets of ``QueueEntryStatus``."""
    universe = set(get_args(QueueEntryStatus))
    assert QUEUE_CLAIMABLE_STATUSES <= universe
    assert QUEUE_ACTIVE_STATUSES <= universe
    assert QUEUE_CLAIMABLE_STATUSES <= QUEUE_ACTIVE_STATUSES


def test_schema_status_enum_matches_the_db_literal() -> None:
    """The API enum and the DB literal name one vocabulary."""
    assert {member.value for member in SchemaOperationStatus} == set(
        get_args(OperationStatus)
    )
