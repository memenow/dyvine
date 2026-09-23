"""Propose reviewed user-ordered skips without changing Postgres.

The legacy queue recorded ``skipped_404`` for accounts the user ordered skipped
for a round (content could not be fetched; the group is kept). No other
reviewed action can close such a row in the active cutover round, and an
unclosed row blocks every automatic weekly round. The output is a private
JSONL holding only the active-round ``skipped_404`` rows of the frozen report,
each with ``{"action": "skip_user_ordered"}``. Preview it with
``apply_queue_reconciliation.py`` before any separately authorized apply; the
policy re-checks every row against the migrated queue state, so the report
alone cannot turn any other row into a skip.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from scripts.propose_group_attested import _write_private_jsonl  # noqa: E402
from scripts.queue_reconciliation_policy import _report_rows  # noqa: E402


def propose_skips(
    rows: list[dict[str, Any]], active_round: str
) -> list[dict[str, Any]]:
    """Return the active round's user-ordered skips with the reviewed action."""
    if any("resolution" in row for row in rows):
        raise ValueError("proposal source must be the original frozen report")
    proposed = [
        {**row, "resolution": {"action": "skip_user_ordered"}}
        for row in rows
        if row.get("round") == active_round
        and row.get("legacy_status") == "skipped_404"
    ]
    if not proposed:
        raise ValueError("active round has no user-ordered skips")
    return proposed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-report", required=True)
    parser.add_argument("--active-round", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    if Path(args.output).resolve() == Path(args.source_report).resolve():
        parser.error("--output cannot replace the frozen report")
    if Path(args.output).exists():
        parser.error("--output already exists; choose a new private proposal path")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        rows, source_sha256 = _report_rows(Path(args.source_report))
        proposed = propose_skips(rows, args.active_round)
        _write_private_jsonl(Path(args.output), proposed)
    except (OSError, ValueError) as error:
        print(f"proposal failed: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "mode": "proposal",
                "source_sha256": source_sha256,
                "total_rows": len(rows),
                "proposed": len(proposed),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
