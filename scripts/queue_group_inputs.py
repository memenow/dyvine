"""Bind reviewed queue decisions to frozen source and Feishu audit artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.audit_feishu_delivery import _journal_source_digest, _load_keys_file
from scripts.feishu_audit_core import _digest
from scripts.queue_group_attestation import (
    AuditJournal,
    read_audit_journal,
    read_work_chats,
)
from scripts.queue_reconciliation_policy import _report_rows


@dataclass(frozen=True)
class GroupInputs:
    """Evidence sources for selected group-attested queue rows."""

    source_rows: dict[str, dict[str, Any]]
    all_rows: list[dict[str, Any]]
    source_sha256: str
    work_sha256: str
    work_chats: dict[tuple[str, str], set[str]]
    journal: AuditJournal
    supplemental_journal: AuditJournal | None = None
    supplemental_keys: frozenset[str] = frozenset()
    keys_file_sha256: str | None = None

    def __post_init__(self) -> None:
        """Reject cross-field evidence pairings no loader can produce."""
        if self.supplemental_keys and self.supplemental_journal is None:
            raise ValueError("supplemental keys require a supplemental journal")
        if (self.supplemental_journal is None) != (self.keys_file_sha256 is None):
            raise ValueError(
                "supplemental journal and keys digest must be supplied together"
            )

    def journal_for(self, key: str) -> tuple[AuditJournal, str | None]:
        """Choose the sole permitted source for this reviewed account."""
        if key in self.supplemental_keys:
            journal = self.supplemental_journal
            if journal is None:
                raise ValueError("supplemental keys require a supplemental journal")
            return journal, self.keys_file_sha256
        return self.journal, None

    @property
    def evidence_digests(self) -> tuple[str, ...]:
        values = [self.source_sha256, self.journal.sha256, self.work_sha256]
        if self.supplemental_journal is not None:
            keys_sha256 = self.keys_file_sha256
            if keys_sha256 is None:
                raise ValueError("supplemental journal requires its keys digest")
            values.extend((self.supplemental_journal.sha256, keys_sha256))
        return tuple(values)


def load_group_inputs(
    *,
    source_report: Path,
    reviewed_rows: list[dict[str, Any]],
    selected_keys: set[str],
    audit_path: Path,
    work_path: Path,
    active_round: str,
    supplemental_audit_path: Path | None = None,
    supplemental_keys_path: Path | None = None,
) -> GroupInputs:
    """Require the reviewed rows to differ from the audit source only by decisions."""
    source_rows, source_sha256 = _report_rows(source_report)
    if len(source_rows) != len(reviewed_rows) or any(
        {key: value for key, value in reviewed.items() if key != "resolution"}
        != original
        for reviewed, original in zip(reviewed_rows, source_rows, strict=True)
    ):
        raise ValueError("reviewed report differs from the frozen audit source")
    if (supplemental_audit_path is None) != (supplemental_keys_path is None):
        raise ValueError("supplemental audit and keys file must be supplied together")
    supplemental_keys: set[str] = set()
    keys_sha256: str | None = None
    if supplemental_keys_path is not None:
        supplemental_keys, keys_sha256 = _load_keys_file(
            supplemental_keys_path, active_round
        )
        source_keys = {
            row["key"] for row in source_rows if row.get("round") == active_round
        }
        if not supplemental_keys <= source_keys:
            raise ValueError("supplemental keys are absent from the frozen report")
    journal = read_audit_journal(audit_path, selected_keys | supplemental_keys)
    manifest_source = _digest(
        {"legacy_report_sha256": source_sha256, "round": active_round}
    )
    if journal.manifest_source_sha256 != manifest_source:
        raise ValueError("Feishu journal does not match the frozen source report")
    supplemental: AuditJournal | None = None
    if supplemental_audit_path is not None:
        # Provably-held narrowing: _load_keys_file always returns a str
        # digest in the branch that sets this path.
        assert keys_sha256 is not None  # noqa: S101
        if supplemental_audit_path.resolve() == audit_path.resolve():
            raise ValueError("main and supplemental audit paths must differ")
        supplemental = read_audit_journal(
            supplemental_audit_path,
            supplemental_keys,
            reject_unselected=True,
        )
        expected = _journal_source_digest(manifest_source, keys_sha256)
        if supplemental.manifest_source_sha256 != expected:
            raise ValueError("supplemental audit manifest differs from exact keys")
        for key in supplemental_keys:
            main_rows = journal.rows_for(key)
            main_sources = {row.get("source_sha256") for row in main_rows}
            if len(main_sources) > 1:
                raise ValueError("main audit has conflicting target snapshots")
            if any(
                row.get("type") != "account" or row.get("scan_complete") is True
                for row in main_rows
            ):
                raise ValueError(
                    "main audit already has conflicting evidence for a supplemental key"
                )
            if not supplemental.rows_for(key):
                raise ValueError("supplemental audit is missing a selected key")
    for key in selected_keys:
        chosen = supplemental if key in supplemental_keys else journal
        # Provably-held narrowing: __post_init__ forbids keys without a
        # journal, so the supplemental arm is never None here.
        assert chosen is not None  # noqa: S101
        completed = sum(
            row.get("type") == "account" and row.get("scan_complete") is True
            for row in chosen.rows_for(key)
        )
        if completed > 1:
            raise ValueError("selected account has duplicate completed audit rows")
    work_chats, work_sha256 = read_work_chats(work_path)
    return GroupInputs(
        {row["key"]: row for row in source_rows},
        reviewed_rows,
        source_sha256,
        work_sha256,
        work_chats,
        journal,
        supplemental,
        frozenset(supplemental_keys),
        keys_sha256,
    )
