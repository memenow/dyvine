"""Regression tests for Feishu audit core identity and validation fixes."""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts import feishu_audit_core as core  # noqa: E402


def _target() -> core.Target:
    return core.Target(
        key="weekly0913:sec-one",
        round="weekly0913",
        sec_user_id="sec-one",
        nickname="Alpha",
        chat_id="oc-chat",
        topic_message_id="om-topic",
        group_status="ready",
        topic_status="ready",
        queue_chat_ids=("oc-chat",),
        legacy=None,
        files=[],
        source_sha256="source-one",
    )


def _cached_success(target: core.Target) -> dict[str, Any]:
    return {
        "type": "account",
        "key": target.key,
        "source_sha256": target.source_sha256,
        "scan_complete": True,
        "discrepancies": [],
        "zero_file_group": False,
        "send_blocked": True,
    }


class _NoReads:
    """Reader that fails the test if the audit touches Feishu."""

    async def list_messages(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("Feishu must not be called")

    async def get_message(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("Feishu must not be called")


def test_no_assert_statements_in_audit_modules() -> None:
    """Runtime validation must survive `python -O` (assert is stripped)."""
    for name in ("scripts/feishu_audit_core.py", "scripts/audit_feishu_delivery.py"):
        source = (ROOT / name).read_text()
        found = [
            node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Assert)
        ]
        assert not found, f"{name} uses assert for runtime validation"


@pytest.mark.asyncio
async def test_resume_revalidates_chat_ownership_before_cached_verdict(
    tmp_path: Path,
) -> None:
    """A cached success must not survive cross-target ownership drift.

    ``chat_owners`` is not part of the per-target ``source_sha256`` binding,
    so ``for_target`` cannot see another account taking over the chat. The
    audit must re-run identity checks before returning a cached verdict.
    """
    target = _target()
    journal = core.Journal(tmp_path / "audit.jsonl", "digest", resume=False)
    try:
        journal.append(_cached_success(target))
        result = await core._audit_target(
            target,
            _NoReads(),
            journal,
            {"oc-chat": {"sec-one", "sec-two"}},
            "cli-app",
        )
    finally:
        journal.close()
    assert result["scan_complete"] is False
    assert result["reason"] == "chat_owned_by_multiple_accounts"
    assert result["send_blocked"] is True


@pytest.mark.asyncio
async def test_resume_returns_cached_verdict_when_identity_unchanged(
    tmp_path: Path,
) -> None:
    """Revalidation must not trigger re-scans when nothing drifted."""
    target = _target()
    journal = core.Journal(tmp_path / "audit.jsonl", "digest", resume=False)
    try:
        journal.append(_cached_success(target))
        result = await core._audit_target(
            target,
            _NoReads(),
            journal,
            {"oc-chat": {"sec-one"}},
            "cli-app",
        )
    finally:
        journal.close()
    assert result["scan_complete"] is True


@pytest.mark.asyncio
async def test_missing_group_or_topic_is_held_without_assert(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The post-identity invariant must hold, never raise AssertionError."""
    target = _target()
    target.chat_id = None
    monkeypatch.setattr(core, "_target_issue", lambda *_args: None)
    journal = core.Journal(tmp_path / "audit.jsonl", "digest", resume=False)
    try:
        result = await core._audit_target(target, _NoReads(), journal, {}, "cli-app")
    finally:
        journal.close()
    assert result["scan_complete"] is False
    assert result["reason"] == "missing_verified_group_or_topic"
    assert result["send_blocked"] is True
