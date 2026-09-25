"""Subprocess checks for the weekly cron entrypoint script."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "dyvine_weekly_run_once.sh"


def _run(
    *args: str, env: dict[str, str] | None = None, path: str | None = None
) -> subprocess.CompletedProcess[str]:
    run_env = dict(os.environ)
    if env:
        run_env.update(env)
    if path is not None:
        run_env["PATH"] = path
    return subprocess.run(
        [str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=run_env,
        timeout=30,
    )


def test_missing_hermes_fails_loud_on_stdout(tmp_path: Path) -> None:
    """No binary anywhere: stdout alert plus 127, never a quiet death."""
    proc = _run(env={"HERMES_BIN": str(tmp_path / "nope")}, path="/usr/bin:/bin")
    assert proc.returncode == 127
    assert "hermes binary not found" in proc.stdout


def test_empty_path_without_override_fails_loud(tmp_path: Path) -> None:
    """A cron-minimal PATH without hermes is equally loud."""
    env = dict(os.environ)
    env.pop("HERMES_BIN", None)
    proc = _run(env=env, path=str(tmp_path))
    assert proc.returncode == 127
    assert "hermes binary not found" in proc.stdout


def test_forwards_args_and_streams_to_resolved_binary(tmp_path: Path) -> None:
    """Args pass through; stdout/stderr stay separated for the scheduler."""
    fake = tmp_path / "hermes"
    fake.write_text('#!/bin/sh\necho "OUT:$*" \necho "ERR:log-line" >&2\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    proc = _run(
        "--dry-run",
        "--round",
        "weekly-2026-09-27",
        env={"HERMES_BIN": str(fake)},
    )
    assert proc.returncode == 0
    assert proc.stdout == (
        "OUT:dyvine weekly run-once --dry-run --round weekly-2026-09-27\n"
    )
    assert proc.stderr == "ERR:log-line\n"
