"""Read-only Feishu history audit for the Dyvine weekly cutover.

The journal contains private chat, account, file, and message identifiers. Give
``--output`` an operator-controlled path; the file is created with mode 0600.
Each completed API page is a durable checkpoint, so ``--resume`` can continue
without repeating previously read pages. This command never sends messages or
changes Postgres state. Freeze the old sender before auditing: Feishu does not
offer a single snapshot across a chat and all of its threads.

Example::

    PYTHONPATH=src uv run python scripts/audit_feishu_delivery.py \
      --legacy-report /opt/dyvine/data/douyin/state/legacy_reconciliation.jsonl \
      --round weekly0913 \
      --output /opt/dyvine/data/douyin/state/feishu_audit.jsonl

``DATABASE_URL`` and ``FEISHU_APP_ID``/``FEISHU_APP_SECRET`` are read from the
environment (or the Hermes environment file for Feishu credentials).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import stat
import sys
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from scripts.feishu_audit_core import (  # noqa: E402
    AuditError,
    FeishuReader,
    Journal,
    Target,
    _audit_target,
    _digest,
    _nonempty,
)


def _load_legacy(
    path: Path, *, round_name: str | None = None
) -> tuple[dict[str, dict[str, Any]], str]:
    payload = path.read_bytes()
    rows: dict[str, dict[str, Any]] = {}
    seen_keys: set[str] = set()
    for line_number, line in enumerate(payload.splitlines(), 1):
        try:
            row = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AuditError(f"invalid legacy JSONL at line {line_number}") from error
        if not isinstance(row, dict):
            raise AuditError(f"invalid legacy row at line {line_number}")
        key = _nonempty(row.get("key"))
        row_round = _nonempty(row.get("round"))
        sec = _nonempty(row.get("sec_user_id"))
        if not key or not row_round or not sec or key != f"{row_round}:{sec}":
            raise AuditError(f"invalid legacy identity at line {line_number}")
        if key in seen_keys:
            raise AuditError("legacy report has duplicate account keys")
        seen_keys.add(key)
        if round_name is not None and row_round != round_name:
            continue
        rows[key] = row
    if not rows:
        raise AuditError("legacy report has no rows for the selected round")
    source_sha256 = _digest(
        {
            "legacy_report_sha256": hashlib.sha256(payload).hexdigest(),
            "round": round_name if round_name is not None else "*",
        }
    )
    return rows, source_sha256


def _load_keys_file(path: Path, round_name: str | None) -> tuple[set[str], str]:
    """Read exact private queue keys and bind their bytes to the new journal."""
    if not round_name:
        raise AuditError("--keys-file requires --round")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
            or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) != 0o600
        ):
            raise AuditError("keys file must be an owned regular 0600 file")
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            payload = stream.read()
    finally:
        os.close(descriptor)
    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise AuditError("keys file is not UTF-8") from error
    if not lines:
        raise AuditError("keys file is empty")
    keys: set[str] = set()
    prefix = f"{round_name}:"
    for line in lines:
        if (
            not line.startswith(prefix)
            or len(line) == len(prefix)
            or line != line.strip()
            or "\x00" in line
        ):
            raise AuditError("keys file has a malformed or wrong-round key")
        if line in keys:
            raise AuditError("keys file has duplicate queue keys")
        keys.add(line)
    return keys, hashlib.sha256(payload).hexdigest()


def _journal_source_digest(base_digest: str, keys_digest: str | None) -> str:
    if keys_digest is None:
        return base_digest
    return _digest(
        {"legacy_source_sha256": base_digest, "keys_file_sha256": keys_digest}
    )


async def _load_targets(
    database_url: str,
    legacy_rows: dict[str, dict[str, Any]],
    *,
    round_name: str | None = None,
    selected_keys: set[str] | None = None,
    keys_file_sha256: str | None = None,
) -> list[Target]:
    """Read a consistent Postgres snapshot using a read-only transaction."""
    from sqlalchemy import select, text

    from dyvine.db.models import DeliveryFileRow, DeliveryGroupRow, DownloadQueueRow
    from dyvine.db.session import DatabaseSessionFactory

    factory = DatabaseSessionFactory(database_url, pool_class="queue", pool_size=1)
    try:
        async with factory.session() as session:
            async with session.begin():
                await session.execute(text("SET TRANSACTION READ ONLY"))
                group_query = select(DeliveryGroupRow)
                if round_name is not None:
                    group_query = group_query.where(
                        DeliveryGroupRow.round == round_name
                    )
                groups = {
                    group.key: group
                    for group in (await session.execute(group_query)).scalars().all()
                }
                chat_owners: dict[str, set[str]] = {}
                for owner_group in groups.values():
                    if owner_group.chat_id:
                        chat_owners.setdefault(owner_group.chat_id, set()).add(
                            owner_group.sec_user_id
                        )
                targets: list[Target] = []
                scoped_legacy = {
                    key: row
                    for key, row in legacy_rows.items()
                    if (round_name is None or row.get("round") == round_name)
                    and (selected_keys is None or key in selected_keys)
                }
                target_keys = set(scoped_legacy) | set(groups)
                if selected_keys is not None:
                    target_keys &= selected_keys
                for key in sorted(target_keys):
                    legacy = scoped_legacy.get(key)
                    group = groups.get(key)
                    if legacy:
                        target_round = str(legacy["round"])
                        sec = str(legacy["sec_user_id"])
                        nickname = str(legacy.get("nickname") or "")
                    elif group:
                        target_round, sec, nickname = (
                            group.round,
                            group.sec_user_id,
                            group.nickname,
                        )
                    else:
                        continue
                    queues = list(
                        (
                            await session.execute(
                                select(DownloadQueueRow).where(
                                    DownloadQueueRow.round == target_round,
                                    DownloadQueueRow.sec_user_id == sec,
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    file_rows = list(
                        (
                            await session.execute(
                                select(DeliveryFileRow).where(
                                    DeliveryFileRow.round == target_round,
                                    DeliveryFileRow.sec_user_id == sec,
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    files = [
                        {
                            "media_id": row.media_id,
                            "relative_path": row.relative_path,
                            "status": row.status,
                            "file_key": row.file_key,
                            "message_id": row.message_id,
                            "chat_id": row.chat_id,
                            "parent_id": row.parent_id,
                        }
                        for row in file_rows
                    ]
                    source = {
                        "key": key,
                        "group": (
                            {
                                "round": group.round,
                                "sec_user_id": group.sec_user_id,
                                "nickname": group.nickname,
                                "status": group.status,
                                "topic_status": group.topic_status,
                                "chat_id": group.chat_id,
                                "topic_message_id": group.topic_message_id,
                            }
                            if group
                            else None
                        ),
                        "queues": [
                            (q.key, q.nickname, q.chat_id, q.status) for q in queues
                        ],
                        "files": sorted(files, key=lambda item: str(item["media_id"])),
                        "legacy": legacy,
                    }
                    if keys_file_sha256 is not None:
                        source["keys_file_sha256"] = keys_file_sha256
                    targets.append(
                        Target(
                            key=key,
                            round=target_round,
                            sec_user_id=sec,
                            nickname=nickname,
                            chat_id=group.chat_id if group else None,
                            topic_message_id=(
                                group.topic_message_id if group else None
                            ),
                            group_status=group.status if group else None,
                            topic_status=group.topic_status if group else None,
                            queue_chat_ids=tuple(q.chat_id for q in queues),
                            legacy=legacy,
                            files=files,
                            source_sha256=_digest(source),
                            identity_consistent=(
                                group is None
                                or (
                                    group.round == target_round
                                    and group.sec_user_id == sec
                                    and group.nickname == nickname
                                )
                            )
                            and all(q.nickname == nickname for q in queues)
                            and (
                                selected_keys is None
                                or group is None
                                or not group.chat_id
                                or chat_owners.get(group.chat_id) == {sec}
                            ),
                        )
                    )
                return targets
    finally:
        await factory.aclose()


async def run(args: argparse.Namespace) -> dict[str, int]:
    """Audit every verified account without changing Feishu or Postgres state."""
    from dyvine.core.exceptions import DeliveryError
    from dyvine.services.delivery import FeishuCredentials

    keys_path = getattr(args, "keys_file", None)
    selected_keys, keys_sha256 = (
        _load_keys_file(Path(keys_path), args.round)
        if keys_path is not None
        else (None, None)
    )
    legacy_rows, legacy_sha256 = _load_legacy(
        Path(args.legacy_report), round_name=args.round
    )
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise AuditError("DATABASE_URL is not set")
    targets = await _load_targets(
        database_url,
        legacy_rows,
        round_name=args.round,
        selected_keys=selected_keys,
        keys_file_sha256=keys_sha256,
    )
    if selected_keys is not None and selected_keys != {item.key for item in targets}:
        raise AuditError("keys file includes keys absent from selected round evidence")
    owners: dict[str, set[str]] = {}
    for target in targets:
        if target.chat_id:
            owners.setdefault(target.chat_id, set()).add(target.sec_user_id)
    try:
        credentials = FeishuCredentials.from_hermes_default()
    except DeliveryError as error:
        raise AuditError("Feishu credentials are unavailable") from error
    counts = {
        "accounts": 0,
        "complete": 0,
        "needs_review": 0,
        "zero_file_groups": 0,
        "discrepancies": 0,
    }
    journal = Journal(
        Path(args.output),
        _journal_source_digest(legacy_sha256, keys_sha256),
        resume=args.resume,
    )
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            reader = FeishuReader(client, credentials, args.request_interval)
            for offset in range(0, len(targets), 3):
                # Settle the whole batch before a fatal error can close the journal.
                results = await asyncio.gather(
                    *(
                        _audit_target(
                            target, reader, journal, owners, credentials.app_id
                        )
                        for target in targets[offset : offset + 3]
                    ),
                    return_exceptions=True,
                )
                for result in results:
                    if isinstance(result, BaseException):
                        raise result
                    counts["accounts"] += 1
                    if result["scan_complete"]:
                        counts["complete"] += 1
                        counts["zero_file_groups"] += int(result["zero_file_group"])
                        counts["discrepancies"] += int(bool(result["discrepancies"]))
                    else:
                        counts["needs_review"] += 1
    finally:
        journal.close()
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-report", required=True, type=Path)
    parser.add_argument(
        "--round", help="audit one delivery round (for example weekly0913)"
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--keys-file", type=Path, help="private 0600 file of exact queue keys"
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--request-interval", type=float, default=0.25)
    args = parser.parse_args()
    if args.round is not None and (not args.round or args.round != args.round.strip()):
        parser.error("--round must be a non-empty round label")
    if args.keys_file is not None and args.round is None:
        parser.error("--keys-file requires --round")
    if not math.isfinite(args.request_interval) or args.request_interval < 0.25:
        parser.error("--request-interval must be finite and at least 0.25 seconds")
    try:
        counts = asyncio.run(run(args))
    except (AuditError, ValueError, OSError) as error:
        print(f"audit failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(counts, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
