"""User-ordered skips are proposed only for recorded active-round skips."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts import propose_user_ordered_skips as proposer  # noqa: E402


def _row(
    sec: str, *, round_name: str = "weekly0913", status: str = "skipped_404"
) -> dict[str, Any]:
    return {
        "key": f"{round_name}:{sec}",
        "round": round_name,
        "sec_user_id": sec,
        "nickname": sec.title(),
        "legacy_status": status,
        "send_blocked": True,
    }


def _write_report(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


def _argv(source: Path, output: Path) -> list[str]:
    return [
        "--source-report",
        str(source),
        "--active-round",
        "weekly0913",
        "--output",
        str(output),
    ]


def test_proposes_only_active_round_user_ordered_skips() -> None:
    rows = [
        _row("sec-skip"),
        _row("sec-done", status="op_done"),
        _row("sec-old", round_name="weekly0906"),
    ]
    proposed = proposer.propose_skips(rows, "weekly0913")
    assert [row["key"] for row in proposed] == ["weekly0913:sec-skip"]
    assert proposed[0]["resolution"] == {"action": "skip_user_ordered"}
    assert proposed[0]["send_blocked"] is True
    assert "resolution" not in rows[0]


def test_refuses_reviewed_or_skipless_sources() -> None:
    reviewed = _row("sec-skip")
    reviewed["resolution"] = {"action": "complete"}
    with pytest.raises(ValueError, match="original frozen report"):
        proposer.propose_skips([reviewed], "weekly0913")
    with pytest.raises(ValueError, match="no user-ordered skips"):
        proposer.propose_skips([_row("sec-done", status="op_done")], "weekly0913")


def test_main_writes_a_private_proposal_and_never_replaces_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _write_report(
        tmp_path / "frozen.jsonl",
        [_row("sec-skip"), _row("sec-done", status="op_done")],
    )
    output = tmp_path / "skips.jsonl"
    assert proposer.main(_argv(source, output)) == 0
    summary = json.loads(capsys.readouterr().out)
    assert (summary["total_rows"], summary["proposed"]) == (2, 1)
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    written = [json.loads(line) for line in output.read_text().splitlines()]
    assert [row["key"] for row in written] == ["weekly0913:sec-skip"]
    with pytest.raises(SystemExit):
        proposer.main(_argv(source, output))
    with pytest.raises(SystemExit):
        proposer.main(_argv(source, source))


def test_main_reports_a_source_without_skips(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _write_report(
        tmp_path / "frozen.jsonl", [_row("sec-done", status="op_done")]
    )
    output = tmp_path / "skips.jsonl"
    assert proposer.main(_argv(source, output)) == 2
    assert "no user-ordered skips" in capsys.readouterr().err
    assert not output.exists()
