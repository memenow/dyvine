#!/usr/bin/env bash
set -euo pipefail

# Hermes no-agent cron forwards stdout as a message. Keep routine output off it.
exec "${HERMES_BIN:-hermes}" dyvine weekly run-once 1>&2
