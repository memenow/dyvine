"""Supplemental evidence pairings are validated, never asserted."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.queue_group_attestation import AuditJournal  # noqa: E402
from scripts.queue_group_inputs import GroupInputs  # noqa: E402


def _inputs(**overrides: Any) -> GroupInputs:
    journal = AuditJournal("journal-sha", "manifest-sha", {})
    params: dict[str, Any] = {
        "source_rows": {},
        "all_rows": [],
        "source_sha256": "source-sha",
        "work_sha256": "work-sha",
        "work_chats": {},
        "journal": journal,
    }
    params.update(overrides)
    return GroupInputs(**params)


def test_group_inputs_rejects_keys_without_supplemental_journal() -> None:
    """Keys that select a missing journal fail at construction. (P6-B)"""
    with pytest.raises(ValueError, match="require a supplemental journal"):
        _inputs(supplemental_keys=frozenset({"weekly0913:sec-one"}))


def test_group_inputs_rejects_unpaired_supplemental_parts() -> None:
    """A journal without its keys digest (or vice versa) is rejected. (P6-B)"""
    journal = AuditJournal("supp-sha", "supp-manifest", {})
    with pytest.raises(ValueError, match="supplied together"):
        _inputs(supplemental_journal=journal)
    with pytest.raises(ValueError, match="supplied together"):
        _inputs(keys_file_sha256="keys-sha")


def test_group_inputs_accepts_complete_supplemental_pairing() -> None:
    """A fully paired supplemental selection keeps working. (P6-B)"""
    journal = AuditJournal("supp-sha", "supp-manifest", {})
    inputs = _inputs(
        supplemental_journal=journal,
        supplemental_keys=frozenset({"weekly0913:sec-one"}),
        keys_file_sha256="keys-sha",
    )
    selected, keys_sha = inputs.journal_for("weekly0913:sec-one")
    assert selected is journal
    assert keys_sha == "keys-sha"
    assert inputs.evidence_digests == (
        "source-sha",
        "journal-sha",
        "work-sha",
        "supp-sha",
        "keys-sha",
    )
