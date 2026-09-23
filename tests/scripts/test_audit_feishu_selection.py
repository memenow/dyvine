"""A private key list narrows a new audit without changing full-run journals."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from dyvine.services.delivery import FeishuCredentials

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts import audit_feishu_delivery as audit  # noqa: E402
from scripts import feishu_audit_core as core  # noqa: E402


def _private(path: Path, content: str) -> Path:
    path.write_text(content)
    os.chmod(path, 0o600)
    return path


def test_keys_file_requires_private_unique_keys_from_selected_round(
    tmp_path: Path,
) -> None:
    path = _private(tmp_path / "keys.txt", "weekly0913:sec-one\n")
    keys, digest = audit._load_keys_file(path, "weekly0913")
    assert keys == {"weekly0913:sec-one"}
    assert len(digest) == 64
    with pytest.raises(core.AuditError, match="requires --round"):
        audit._load_keys_file(path, None)
    _private(path, "weekly0913:sec-one\nweekly0913:sec-one\n")
    with pytest.raises(core.AuditError, match="duplicate"):
        audit._load_keys_file(path, "weekly0913")
    _private(path, "weekly0906:sec-one\n")
    with pytest.raises(core.AuditError, match="wrong-round"):
        audit._load_keys_file(path, "weekly0913")
    os.chmod(path, 0o644)
    with pytest.raises(core.AuditError, match="0600"):
        audit._load_keys_file(path, "weekly0913")


@pytest.mark.asyncio
async def test_selected_new_journal_binds_key_bytes_and_rejects_missing_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = tmp_path / "legacy.jsonl"
    legacy.write_text(
        "\n".join(
            json.dumps(
                {
                    "key": f"weekly0913:{sec}",
                    "round": "weekly0913",
                    "sec_user_id": sec,
                    "nickname": sec,
                }
            )
            for sec in ("sec-one", "sec-two")
        )
        + "\n"
    )
    keys_file = _private(tmp_path / "keys.txt", "weekly0913:sec-one\n")
    output = tmp_path / "selected.jsonl"
    seen: list[tuple[set[str] | None, str | None]] = []

    async def load_targets(
        _url: str,
        _rows: dict[str, dict[str, Any]],
        **kwargs: Any,
    ) -> list[core.Target]:
        selected = kwargs["selected_keys"]
        key_sha = kwargs["keys_file_sha256"]
        seen.append((selected, key_sha))
        if selected != {"weekly0913:sec-one"}:
            return []
        return [
            core.Target(
                key="weekly0913:sec-one",
                round="weekly0913",
                sec_user_id="sec-one",
                nickname="sec-one",
                chat_id=None,
                topic_message_id=None,
                group_status=None,
                topic_status=None,
                queue_chat_ids=(),
                legacy=None,
                files=[],
                source_sha256="target-source",
            )
        ]

    monkeypatch.setattr(audit, "_load_targets", load_targets)
    monkeypatch.setattr(audit, "FeishuReader", lambda *_args: object())
    monkeypatch.setattr(
        FeishuCredentials,
        "from_hermes_default",
        staticmethod(lambda: FeishuCredentials("app", "secret")),
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://unused")
    args = argparse.Namespace(
        legacy_report=legacy,
        round="weekly0913",
        keys_file=keys_file,
        output=output,
        resume=False,
        request_interval=0.25,
    )
    counts = await audit.run(args)
    assert counts["accounts"] == 1
    base = audit._load_legacy(legacy, round_name="weekly0913")[1]
    key_sha = audit._load_keys_file(keys_file, "weekly0913")[1]
    manifest = json.loads(output.read_text().splitlines()[0])
    assert manifest["source_sha256"] == audit._journal_source_digest(base, key_sha)
    assert audit._journal_source_digest(base, None) == base
    assert seen == [({"weekly0913:sec-one"}, key_sha)]

    alternate = _private(tmp_path / "alternate.txt", "weekly0913:sec-one")
    args.keys_file, args.resume = alternate, True
    with pytest.raises(core.AuditError, match="journal source"):
        await audit.run(args)
    absent = _private(tmp_path / "absent.txt", "weekly0913:sec-missing\n")
    args.keys_file, args.output, args.resume = absent, tmp_path / "unused.jsonl", False
    with pytest.raises(core.AuditError, match="absent"):
        await audit.run(args)
    assert not args.output.exists()
