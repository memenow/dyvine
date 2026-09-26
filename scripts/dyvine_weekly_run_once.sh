#!/bin/bash
set -euo pipefail

# Weekly single-shot entry point for cron / Hermes no-agent schedules.
#
# Concurrency model: weekly.py takes a NON-BLOCKING flock on
# ~/.hermes/dyvine-weekly.lock and skips (exit 0) when another runner on
# THIS host holds it. That lock is single-host only and follows $HOME:
# under multi-host scheduling, or a cron without a stable HOME, treat it
# as advisory and rely on the DB claim_next atomicity, which is the real
# cross-host backstop. A skip still logs one line to stderr.
#
# Stream discipline: weekly.py keeps stdout empty on routine runs (only
# --dry-run prints JSON there); progress and alerts go to stderr. Do NOT
# re-merge the streams here: Hermes no-agent cron forwards stdout as a
# message, and collapsing them would either spam routine logs as
# messages or bury dry-run JSON in stderr.

# Resolve the gateway binary absolutely: cron ships a minimal PATH that
# often lacks hermes, and a missing binary must fail LOUDLY on stdout
# (so the scheduler forwards the alert) instead of dying quiet.
if [ -n "${HERMES_BIN:-}" ]; then
  HERMES="$HERMES_BIN"
else
  HERMES="$(command -v hermes || true)"
fi
if [ -z "$HERMES" ] || [ ! -x "$HERMES" ]; then
  echo "dyvine weekly: hermes binary not found (set HERMES_BIN to its absolute path)"
  exit 127
fi

exec "$HERMES" dyvine weekly run-once "$@"
