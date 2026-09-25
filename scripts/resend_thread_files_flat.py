"""Post plain copies of a round's topic-thread file sends into their chats.

Until files became plain chat messages, delivery replied to each account's
profile post in a thread, so a reused legacy group showed none of the round's
files in its main chat. For every ``sent`` file of the round whose record has
a reply parent, this posts the same Feishu file (its recorded ``file_key``)
as a plain chat message, once and in post order per account. Each copy is its
own ledger record in round ``<round>-flat``, reserved before the send and sent
with a stable UUID that Feishu dedupes for an hour; a copy whose outcome is
still unknown after that window waits for review instead of a second send.

The default is a dry run. ``--apply`` sends up to ``--limit`` copies per
invocation, so a first run can be small and checked in the chat before the
rest. ``DATABASE_URL`` and the Feishu app credentials are read from the
environment (or the Hermes environment file for Feishu credentials).

Example::

    PYTHONPATH=src uv run python scripts/resend_thread_files_flat.py \\
      --round weekly0913
    PYTHONPATH=src uv run python scripts/resend_thread_files_flat.py \\
      --round weekly0913 --apply --limit 3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from dyvine.core.exceptions import DeliveryError  # noqa: E402
from dyvine.services.delivery_durable import _within  # noqa: E402

# Feishu dedupes a message UUID for one hour; keep a margin before trusting it.
_SAFE_RETRY_WINDOW = timedelta(minutes=50)


def copy_media_id(flat_round: str, original_media_id: str) -> str:
    """The ledger identity of one plain copy of a thread send."""
    return sha256(f"{flat_round}\0{original_media_id}".encode()).hexdigest()


async def resend(
    ledger: Any,
    channel: Any,
    *,
    round_name: str,
    apply: bool,
    limit: int,
    interval: float,
) -> dict[str, int]:
    """Post each thread send of ``round_name`` once as a plain chat message."""
    flat_round = f"{round_name}-flat"
    sends = await ledger.list_files(round=round_name, status="sent", limit=-1)
    threaded = sorted(
        (item for item in sends if item.parent_id),
        key=lambda item: (item.sec_user_id, item.relative_path),
    )
    counts: Counter[str] = Counter(thread_sends=len(threaded))
    posted = 0
    for item in threaded:
        media_id = copy_media_id(flat_round, item.media_id)
        copy = await ledger.get_file(media_id)
        if copy is not None and copy.status == "sent":
            counts["already_copied"] += 1
            continue
        if copy is not None and copy.status not in {"planned", "uploaded", "sending"}:
            counts["held"] += 1
            continue
        if not item.chat_id or not item.file_key or not item.content_sha256:
            counts["incomplete_original"] += 1
            continue
        if not apply:
            counts["to_copy"] += 1
            continue
        if posted >= limit:
            counts["deferred"] += 1
            continue
        if copy is None:
            copy = await ledger.reserve_file(
                media_id=media_id,
                round=flat_round,
                sec_user_id=item.sec_user_id,
                relative_path=item.relative_path,
                content_sha256=item.content_sha256,
                chat_id=item.chat_id,
                parent_id=None,
            )
        if copy.status == "planned":
            copy = await ledger.set_file_key(media_id, item.file_key)
        if copy.status == "uploaded":
            copy = await ledger.begin_send(media_id)
        if (
            copy.status != "sending"
            or not copy.send_uuid
            or copy.chat_id != item.chat_id
            or not _within(copy.send_started_at, _SAFE_RETRY_WINDOW)
        ):
            await ledger.mark_file_review(media_id)
            counts["review"] += 1
            continue
        posted += 1
        try:
            response, error = await channel._send_message(
                item.chat_id,
                "file",
                {"file_key": copy.file_key},
                request_uuid=copy.send_uuid,
            )
        except DeliveryError:
            response, error = None, {"code": "transport"}
        message_id = (response or {}).get("message_id")
        if error is not None or not isinstance(message_id, str) or not message_id:
            # The copy keeps its UUID, so a rerun inside the window cannot repeat it.
            code = error.get("code") if isinstance(error, dict) else None
            counts[f"unconfirmed_{code}"] += 1
        else:
            await ledger.mark_sent(media_id, message_id)
            counts["copied"] += 1
        await asyncio.sleep(interval)
    return dict(counts)


async def run(args: argparse.Namespace) -> dict[str, int]:
    from dyvine.db.delivery_ledger import PostgresDeliveryLedgerRepository
    from dyvine.db.session import DatabaseSessionFactory
    from dyvine.services.delivery import FeishuCredentials, FeishuGroupChannel

    database_url = os.environ.get(args.database_url_env)
    if not database_url:
        raise ValueError(f"{args.database_url_env} is not set")
    factory = DatabaseSessionFactory(
        database_url, pool_class="queue", pool_size=1, max_overflow=0
    )
    try:
        return await resend(
            PostgresDeliveryLedgerRepository(factory),
            FeishuGroupChannel(FeishuCredentials.from_hermes_default()),
            round_name=args.round,
            apply=args.apply,
            limit=args.limit,
            interval=args.request_interval,
        )
    finally:
        await factory.aclose()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--round", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=100_000)
    parser.add_argument("--request-interval", type=float, default=0.35)
    parser.add_argument("--database-url-env", default="DATABASE_URL")
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("--limit must be positive")
    if args.request_interval < 0.25:
        parser.error("--request-interval must be at least 0.25 seconds")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        counts = asyncio.run(run(args))
    except (OSError, ValueError) as error:
        print(f"resend failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"mode": "apply" if args.apply else "dry_run", **counts}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
