"""Author skips are proposed only for held active-round rows Douyin reports gone."""

from __future__ import annotations

import asyncio
import json
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dyvine.core.exceptions import ServiceError  # noqa: E402
from dyvine.schemas.users import AuthorState  # noqa: E402
from scripts import propose_unavailable_author_skips as proposer  # noqa: E402

_STATES = {
    "sec-gone": AuthorState(available=False, reason="deactivated"),
    "sec-banned": AuthorState(available=False, reason="banned", aweme_count=0),
    "sec-live": AuthorState(available=True, aweme_count=9),
}


async def _check(sec: str) -> AuthorState:
    if sec not in _STATES:
        raise ServiceError("profile request failed")
    return _STATES[sec]


def _row(sec: str, *, round_name: str = "weekly0913") -> dict[str, Any]:
    return {
        "key": f"{round_name}:{sec}",
        "round": round_name,
        "sec_user_id": sec,
        "nickname": sec.title(),
        "legacy_status": "op_done",
        "send_blocked": True,
    }


def test_proposes_only_held_active_round_rows_with_unavailable_authors() -> None:
    rows = [
        _row("sec-gone"),
        _row("sec-banned"),
        _row("sec-live"),
        _row("sec-error"),
        _row("sec-gone", round_name="weekly0906"),
    ]
    held = {row["key"] for row in rows} - {"weekly0913:sec-banned"}
    proposed, counts = asyncio.run(
        proposer.propose_author_skips(rows, "weekly0913", held, _check, pace_seconds=0)
    )
    assert [row["key"] for row in proposed] == ["weekly0913:sec-gone"]
    author = proposed[0]["resolution"]["author"]
    assert proposed[0]["resolution"]["action"] == "skip_author_unavailable"
    assert (author["reason"], author["aweme_count"]) == ("deactivated", None)
    assert isinstance(author["checked_at"], str)
    assert counts == {"deactivated": 1, "available": 1, "error": 1}
    assert "resolution" not in rows[0]


def test_rejects_a_report_that_already_carries_resolutions() -> None:
    row = _row("sec-gone")
    row["resolution"] = {"action": "complete"}
    with pytest.raises(ValueError, match="original frozen report"):
        asyncio.run(
            proposer.propose_author_skips(
                [row], "weekly0913", {row["key"]}, _check, pace_seconds=0
            )
        )


def test_main_writes_a_private_proposal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "report.jsonl"
    source.write_text(
        json.dumps(_row("sec-gone")) + "\n" + json.dumps(_row("sec-live")) + "\n"
    )
    output = tmp_path / "proposal.jsonl"

    async def held(_url: str, _round: str) -> set[str]:
        return {"weekly0913:sec-gone", "weekly0913:sec-live"}

    monkeypatch.setattr(proposer, "_held_keys", held)
    monkeypatch.setattr(proposer, "fetch_author_state", _check)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://example")
    argv = [
        "--source-report",
        str(source),
        "--active-round",
        "weekly0913",
        "--output",
        str(output),
        "--pace-seconds",
        "0",
    ]
    assert proposer.main(argv) == 0
    summary = json.loads(capsys.readouterr().out)
    assert (summary["held_rows"], summary["proposed"]) == (2, 1)
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    written = [json.loads(line) for line in output.read_text().splitlines()]
    assert [row["key"] for row in written] == ["weekly0913:sec-gone"]
    with pytest.raises(SystemExit):
        proposer.main(argv)
