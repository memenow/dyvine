"""Propose reviewed attested release decisions without changing Postgres.

The output is a private JSONL copy of the frozen report. Only rows with a
complete proof receive the chosen action: ``release_pending_group_attested``
(the whole chat matches imported history) or, with ``--action``,
``release_pending_window_attested`` (the chat matches the legacy ledger for
media posted after the queue cutoff) or ``release_pending_feishu_adopted``
(the chat is taken as the record of what was sent, and the proposal carries
the ledger rows to adopt and demote). Run ``apply_queue_reconciliation.py``
in preview mode against this output before any separately authorized apply.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import text as sql_text  # noqa: E402

from dyvine.db.models import DeliveryGroupRow  # noqa: E402
from dyvine.db.session import DatabaseSessionFactory  # noqa: E402
from scripts.apply_queue_reconciliation import _inspect_row  # noqa: E402
from scripts.queue_group_inputs import load_group_inputs  # noqa: E402
from scripts.queue_reconciliation_policy import (  # noqa: E402
    Decision,
    _report_rows,
    _string,
)


def _write_private_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    """Create a complete mode-0600 proposal without replacing any old file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as output:
            temporary = Path(output.name)
            os.chmod(temporary, 0o600)
            for row in rows:
                output.write(json.dumps(row, ensure_ascii=False) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _adopted_destination(
    source: dict[str, Any], group: DeliveryGroupRow | None
) -> tuple[str, str] | None:
    """Use a ready matching PG group only where the frozen report is incomplete."""
    chat = _string(source.get("adoptable_group_chat_id"))
    topic = _string(source.get("adoptable_topic_message_id"))
    if chat is not None and topic is not None:
        return chat, topic
    if (
        group is None
        or group.key != source.get("key")
        or group.round != source.get("round")
        or group.sec_user_id != source.get("sec_user_id")
        or group.nickname != source.get("nickname")
        or group.status != "ready"
        or group.topic_status != "ready"
        or not _string(group.chat_id)
        or not _string(group.topic_message_id)
        or (chat is not None and chat != group.chat_id)
        or (topic is not None and topic != group.topic_message_id)
    ):
        return None
    assert group.chat_id is not None and group.topic_message_id is not None
    return group.chat_id, group.topic_message_id


async def propose(args: argparse.Namespace) -> dict[str, Any]:
    """Evaluate every active-round account against immutable audit evidence."""
    action = getattr(args, "action", "release_pending_group_attested")
    timezone = getattr(args, "timezone", None)
    source_rows, source_sha256 = _report_rows(Path(args.source_report))
    if any("resolution" in row for row in source_rows):
        raise ValueError("proposal source must be the original frozen report")
    selected = {
        row["key"] for row in source_rows if row.get("round") == args.active_round
    }
    if not selected:
        raise ValueError("active round is absent from the frozen report")
    inputs = load_group_inputs(
        source_report=Path(args.source_report),
        reviewed_rows=source_rows,
        selected_keys=selected,
        audit_path=Path(args.feishu_audit),
        work_path=Path(args.legacy_work_db),
        active_round=args.active_round,
        supplemental_audit_path=(
            Path(args.supplemental_feishu_audit)
            if getattr(args, "supplemental_feishu_audit", None)
            else None
        ),
        supplemental_keys_path=(
            Path(args.supplemental_keys_file)
            if getattr(args, "supplemental_keys_file", None)
            else None
        ),
    )
    database_url = os.environ.get(args.database_url_env)
    if not database_url:
        raise ValueError(f"{args.database_url_env} is not set")
    factory = DatabaseSessionFactory(
        database_url, pool_class="queue", pool_size=1, max_overflow=0
    )
    proposed: list[dict[str, Any]] = []
    held: Counter[str] = Counter()
    count = 0
    try:
        for source in source_rows:
            if source.get("round") != args.active_round:
                proposed.append(source)
                continue
            candidate: dict[str, Any] | None = None
            async with factory.session() as session:
                async with session.begin():
                    await session.execute(sql_text("SET TRANSACTION READ ONLY"))
                    group = None
                    if not (
                        _string(source.get("adoptable_group_chat_id"))
                        and _string(source.get("adoptable_topic_message_id"))
                    ):
                        group = await session.get(DeliveryGroupRow, source["key"])
                    destination = _adopted_destination(source, group)
                    if destination is None:
                        decision = Decision(
                            None, "held", "group or topic has no adopted identity"
                        )
                    else:
                        candidate = {
                            **source,
                            "resolution": {
                                "action": action,
                                "chat_id": destination[0],
                                "topic_message_id": destination[1],
                            },
                        }
                        decision, _queue = await _inspect_row(
                            session,
                            candidate,
                            args.active_round,
                            False,
                            set(),
                            inputs,
                            lock=False,
                            timezone=timezone,
                        )
                        if (
                            decision.status is None
                            and decision.feishu_adoption is not None
                        ):
                            # Carry the plan the audit and Postgres imply, then
                            # confirm the row releases with exactly that plan.
                            candidate["resolution"][
                                "plan"
                            ] = decision.feishu_adoption.payload()
                            decision, _queue = await _inspect_row(
                                session,
                                candidate,
                                args.active_round,
                                False,
                                set(),
                                inputs,
                                lock=False,
                                timezone=timezone,
                            )
            if decision.status == "pending" and candidate is not None:
                proposed.append(candidate)
                count += 1
            else:
                proposed.append(source)
                held[decision.reason] += 1
    finally:
        await factory.aclose()
    _write_private_jsonl(Path(args.output), proposed)
    return {
        "mode": "proposal",
        "action": action,
        "source_sha256": source_sha256,
        "feishu_audit_sha256": inputs.journal.sha256,
        "supplemental_audit_sha256": (
            inputs.supplemental_journal.sha256 if inputs.supplemental_journal else None
        ),
        "supplemental_keys_sha256": inputs.keys_file_sha256,
        "legacy_work_sha256": inputs.work_sha256,
        "total_rows": len(source_rows),
        "active_rows": len(selected),
        "proposed": count,
        "held_reasons": dict(held),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-report", required=True)
    parser.add_argument("--feishu-audit", required=True)
    parser.add_argument("--supplemental-feishu-audit")
    parser.add_argument("--supplemental-keys-file")
    parser.add_argument("--legacy-work-db", required=True)
    parser.add_argument("--active-round", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--action",
        choices=(
            "release_pending_group_attested",
            "release_pending_window_attested",
            "release_pending_feishu_adopted",
        ),
        default="release_pending_group_attested",
    )
    parser.add_argument(
        "--timezone",
        help="weekly runner timezone (DYVINE_WEEKLY_TIMEZONE) for window cutoffs",
    )
    parser.add_argument("--database-url-env", default="DATABASE_URL")
    args = parser.parse_args(argv)
    if args.action != "release_pending_group_attested" and not args.timezone:
        parser.error("window attestation requires --timezone")
    if bool(args.supplemental_feishu_audit) != bool(args.supplemental_keys_file):
        parser.error("supplemental audit and keys file must be supplied together")
    output = Path(args.output).resolve()
    if output in {
        Path(value).resolve()
        for value in (
            args.source_report,
            args.feishu_audit,
            args.supplemental_feishu_audit,
            args.supplemental_keys_file,
            args.legacy_work_db,
        )
        if value
    }:
        parser.error("--output cannot replace an evidence source")
    if Path(args.output).exists():
        parser.error("--output already exists; choose a new private proposal path")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = asyncio.run(propose(args))
    except (OSError, ValueError) as error:
        print(f"proposal failed: {error}", file=sys.stderr)
        return 2
    except Exception as error:
        print(f"proposal database failure: {type(error).__name__}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
