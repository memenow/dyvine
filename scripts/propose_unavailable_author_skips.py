"""Propose reviewed skips for authors Douyin reports gone, without writing.

A deactivated or banned author, or one without posts, can never be
delivered, yet its unclosed cutover row blocks every automatic weekly round.
For each active-round row of the frozen report whose queue row is still
``needs_reconciliation`` in Postgres, the author's Douyin profile is checked.
Unavailable authors get ``{"action": "skip_author_unavailable", "author":
{...}}`` carrying the evidence; applying it closes the row, excludes the seed
from later rounds, and keeps the Feishu group. A failed or unclear profile
answer is counted and never proposed. Preview the output with
``apply_queue_reconciliation.py`` before any separately authorized apply.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import select  # noqa: E402

from dyvine.core.exceptions import ServiceError  # noqa: E402
from dyvine.db.models import DownloadQueueRow  # noqa: E402
from dyvine.db.session import DatabaseSessionFactory  # noqa: E402
from dyvine.schemas.users import AuthorState  # noqa: E402
from dyvine.services.users import fetch_author_state  # noqa: E402
from scripts.propose_group_attested import _write_private_jsonl  # noqa: E402
from scripts.queue_reconciliation_policy import _report_rows  # noqa: E402

AuthorCheck = Callable[[str], Awaitable[AuthorState]]


async def propose_author_skips(
    rows: list[dict[str, Any]],
    active_round: str,
    held_keys: set[str],
    check: AuthorCheck,
    *,
    pace_seconds: float = 1.5,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Return skip proposals for held rows whose author is unavailable.

    The counts name every checked row's outcome: ``available``, ``error``,
    or the unavailability reason.
    """
    if any("resolution" in row for row in rows):
        raise ValueError("proposal source must be the original frozen report")
    candidates = [
        row
        for row in rows
        if row.get("round") == active_round and row.get("key") in held_keys
    ]
    proposed: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for index, row in enumerate(candidates):
        if index and pace_seconds > 0:
            await asyncio.sleep(pace_seconds)
        try:
            state = await check(row["sec_user_id"])
        except ServiceError:
            counts["error"] += 1
            continue
        if state.available or state.reason is None:
            counts["available"] += 1
            continue
        counts[state.reason] += 1
        proposed.append(
            {
                **row,
                "resolution": {
                    "action": "skip_author_unavailable",
                    "author": {
                        "reason": state.reason,
                        "aweme_count": state.aweme_count,
                        "checked_at": datetime.now(UTC).isoformat(),
                    },
                },
            }
        )
    return proposed, dict(counts)


async def _held_keys(url: str, active_round: str) -> set[str]:
    factory = DatabaseSessionFactory(
        url, pool_class="queue", pool_size=1, max_overflow=0
    )
    try:
        async with factory.session() as session:
            result = await session.execute(
                select(DownloadQueueRow.key).where(
                    DownloadQueueRow.round == active_round,
                    DownloadQueueRow.status == "needs_reconciliation",
                )
            )
            return set(result.scalars())
    finally:
        await factory.aclose()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-report", required=True)
    parser.add_argument("--active-round", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--database-url-env", default="DATABASE_URL")
    parser.add_argument("--pace-seconds", type=float, default=1.5)
    args = parser.parse_args(argv)
    if Path(args.output).resolve() == Path(args.source_report).resolve():
        parser.error("--output cannot replace the frozen report")
    if Path(args.output).exists():
        parser.error("--output already exists; choose a new private proposal path")
    return args


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    rows, source_sha256 = _report_rows(Path(args.source_report))
    url = os.environ.get(args.database_url_env)
    if not url:
        raise ValueError(f"{args.database_url_env} is not set")
    held = await _held_keys(url, args.active_round)
    proposed, counts = await propose_author_skips(
        rows,
        args.active_round,
        held,
        fetch_author_state,
        pace_seconds=args.pace_seconds,
    )
    if not proposed:
        raise ValueError("no held active-round row has an unavailable author")
    _write_private_jsonl(Path(args.output), proposed)
    return {
        "mode": "proposal",
        "source_sha256": source_sha256,
        "held_rows": len(held),
        "proposed": len(proposed),
        "counts": counts,
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        summary = asyncio.run(_run(args))
    except (OSError, ValueError) as error:
        print(f"proposal failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
