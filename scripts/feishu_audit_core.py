"""Private read-only Feishu page journal and delivery reconciliation core."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import math
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

import httpx

_BASE_URL = "https://open.feishu.cn/open-apis"
_TOKEN_URL = f"{_BASE_URL}/auth/v3/tenant_access_token/internal"
_MESSAGES_URL = f"{_BASE_URL}/im/v1/messages"
_RETRYABLE_CODES = {429, 500, 502, 503, 504}


class AuditError(Exception):
    """A safe, secret-free reason an audit cannot be completed."""


class FeishuReadError(AuditError):
    """One chat or thread read failed without invalidating the audit source."""


class FeishuCredentials(Protocol):
    @property
    def app_id(self) -> str: ...

    @property
    def app_secret(self) -> str: ...


def _nonempty(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(slots=True)
class Target:
    """Frozen database and legacy evidence for one round/account pair."""

    key: str
    round: str
    sec_user_id: str
    nickname: str
    chat_id: str | None
    topic_message_id: str | None
    group_status: str | None
    topic_status: str | None
    queue_chat_ids: tuple[str | None, ...]
    legacy: dict[str, Any] | None
    files: list[dict[str, Any]]
    source_sha256: str
    identity_consistent: bool = True


def _target_issue(target: Target, chat_owners: dict[str, set[str]]) -> str | None:
    if not target.identity_consistent:
        return "group_queue_account_identity_mismatch"
    if (
        target.key != f"{target.round}:{target.sec_user_id}"
        or target.group_status != "ready"
        or target.topic_status != "ready"
        or not target.chat_id
        or not target.topic_message_id
    ):
        return "missing_verified_group_or_topic"
    if target.queue_chat_ids != (target.chat_id,):
        return "queue_group_chat_mismatch"
    if chat_owners.get(target.chat_id) != {target.sec_user_id}:
        return "chat_owned_by_multiple_accounts"
    if target.legacy:
        chat = target.legacy.get("adoptable_group_chat_id")
        topic = target.legacy.get("adoptable_topic_message_id")
        if chat not in (None, target.chat_id) or topic not in (
            None,
            target.topic_message_id,
        ):
            return "legacy_group_topic_mismatch"
    return None


class Journal:
    """Append-only private JSONL page checkpoints and account summaries."""

    def __init__(self, path: Path, source_sha256: str, *, resume: bool) -> None:
        flags = os.O_RDWR | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        if not resume:
            flags |= os.O_CREAT | os.O_EXCL
        self._fd = os.open(path, flags, 0o600)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            details = os.fstat(self._fd)
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_nlink != 1
                or details.st_uid != os.getuid()
            ):
                raise AuditError("output must be an owned regular file with one link")
            if resume and stat.S_IMODE(details.st_mode) != 0o600:
                raise AuditError("existing journal must already have mode 0600")
            self._row_offsets: dict[str, list[tuple[int, int]]] = {}
            if resume:
                first: dict[str, Any] | None = None
                complete = 0
                with os.fdopen(os.dup(self._fd), "rb") as stream:
                    while line := stream.readline():
                        start = complete
                        if not line.endswith(b"\n"):
                            break
                        try:
                            row = json.loads(line)
                        except (UnicodeDecodeError, json.JSONDecodeError) as error:
                            raise AuditError(
                                "existing journal has invalid JSONL"
                            ) from error
                        if not isinstance(row, dict):
                            raise AuditError("existing journal has a non-object row")
                        if first is None:
                            first = row
                        key = row.get("key")
                        if isinstance(key, str):
                            self._row_offsets.setdefault(key, []).append(
                                (start, len(line))
                            )
                        complete += len(line)
                if first != {
                    "type": "manifest",
                    "schema": 1,
                    "source_sha256": source_sha256,
                }:
                    raise AuditError(
                        "journal source or schema differs; use a new output"
                    )
                if complete != details.st_size:
                    os.ftruncate(self._fd, complete)
            else:
                self.append(
                    {"type": "manifest", "schema": 1, "source_sha256": source_sha256}
                )
        except BaseException:
            os.close(self._fd)
            raise

    def close(self) -> None:
        os.close(self._fd)

    @property
    def rows(self) -> list[dict[str, Any]]:
        """Materialize the small group-adoption report on demand for its reviewer."""
        result: list[dict[str, Any]] = []
        with os.fdopen(os.dup(self._fd), "rb") as stream:
            stream.seek(0)
            for line in stream:
                if not line.endswith(b"\n"):
                    raise AuditError("audit journal has an incomplete row")
                try:
                    row = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise AuditError("audit journal has invalid JSONL") from error
                if not isinstance(row, dict):
                    raise AuditError("audit journal has a non-object row")
                result.append(row)
        return result

    def append(self, row: dict[str, Any]) -> None:
        payload = (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode()
        offset = os.lseek(self._fd, 0, os.SEEK_END)
        if os.write(self._fd, payload) != len(payload):
            raise OSError("short audit journal write")
        os.fsync(self._fd)
        key = row.get("key")
        if isinstance(key, str):
            self._row_offsets.setdefault(key, []).append((offset, len(payload)))

    def for_target(self, target: Target) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for offset, length in self._row_offsets.get(target.key, []):
            payload = os.pread(self._fd, length, offset)
            if len(payload) != length:
                raise AuditError("audit journal page could not be read")
            try:
                row = json.loads(payload)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise AuditError("audit journal page is invalid JSONL") from error
            if not isinstance(row, dict):
                raise AuditError("audit journal page is not an object")
            rows.append(row)
        if any(row.get("source_sha256") != target.source_sha256 for row in rows):
            raise AuditError("target changed since checkpoint; use a new output")
        return rows


class FeishuReader:
    """Rate-limited Feishu GET reader with in-memory tenant token refresh."""

    def __init__(
        self, client: httpx.AsyncClient, credentials: FeishuCredentials, interval: float
    ) -> None:
        self._client = client
        self._credentials = credentials
        if not math.isfinite(interval):
            raise ValueError("request interval must be finite")
        self._interval = max(interval, 0.25)
        self._next_request = 0.0
        self._pace_lock = asyncio.Lock()
        self._token_lock = asyncio.Lock()
        self._token = ""
        self._token_expiry = 0.0

    async def _paced_request(
        self, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        for attempt in range(5):
            async with self._pace_lock:
                wait = self._next_request - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                self._next_request = time.monotonic() + self._interval
            try:
                response = await self._client.request(method, url, **kwargs)
            except httpx.RequestError as error:
                if attempt == 4:
                    raise FeishuReadError("Feishu network request failed") from error
                await asyncio.sleep(min(2**attempt, 16))
                continue
            if response.status_code in _RETRYABLE_CODES:
                if attempt == 4:
                    raise FeishuReadError(
                        f"Feishu transient HTTP {response.status_code} persisted"
                    )
                retry_after = response.headers.get("retry-after", "")
                delay = float(retry_after) if retry_after.isdigit() else 2**attempt
                await asyncio.sleep(min(max(delay, 1), 30))
                continue
            return response
        raise FeishuReadError("Feishu request retry limit reached")

    async def _access_token(self) -> str:
        if self._token and time.monotonic() < self._token_expiry:
            return self._token
        async with self._token_lock:
            if self._token and time.monotonic() < self._token_expiry:
                return self._token
            try:
                response = await self._paced_request(
                    "POST",
                    _TOKEN_URL,
                    json={
                        "app_id": self._credentials.app_id,
                        "app_secret": self._credentials.app_secret,
                    },
                )
            except FeishuReadError as error:
                raise AuditError("Feishu authentication is unavailable") from error
            try:
                data = response.json()
            except ValueError as error:
                raise AuditError("Feishu token response was not JSON") from error
            if not isinstance(data, dict):
                raise AuditError("Feishu token response was not an object")
            if response.status_code != 200 or data.get("code") != 0:
                raise AuditError("Feishu token request failed")
            token = _nonempty(data.get("tenant_access_token"))
            if not token:
                raise AuditError("Feishu token response was empty")
            self._token = token
            expires = data.get("expire")
            self._token_expiry = time.monotonic() + max(
                (expires if isinstance(expires, int) else 3600) - 120, 60
            )
            return token

    async def _get(
        self, url: str, params: dict[str, str] | None = None
    ) -> dict[str, Any]:
        for attempt in range(3):
            for refreshed in (False, True):
                token = await self._access_token()
                response = await self._paced_request(
                    "GET",
                    url,
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                )
                if response.status_code == 401 and not refreshed:
                    async with self._token_lock:
                        if self._token == token:
                            self._token = ""
                    continue
                try:
                    data = response.json()
                except ValueError as error:
                    raise FeishuReadError(
                        "Feishu history response was not JSON"
                    ) from error
                if not isinstance(data, dict):
                    raise FeishuReadError("Feishu history response was not an object")
                if data.get("code") == 99991400:
                    break
                if response.status_code != 200 or data.get("code", 0) != 0:
                    code = data.get("code")
                    raise FeishuReadError(
                        "Feishu history read failed "
                        f"(HTTP {response.status_code}, code {code})"
                    )
                result = data.get("data", data)
                if not isinstance(result, dict):
                    raise FeishuReadError("Feishu history response lacked data")
                return result
            if attempt < 2:
                await asyncio.sleep(2**attempt)
        raise FeishuReadError("Feishu history rate limit persisted")

    async def list_messages(
        self, container_type: str, container_id: str, page_token: str | None
    ) -> dict[str, Any]:
        params = {
            "container_id_type": container_type,
            "container_id": container_id,
            "page_size": "50",
            "sort_type": "ByCreateTimeAsc",
        }
        if page_token:
            params["page_token"] = page_token
        return await self._get(_MESSAGES_URL, params)

    async def get_message(self, message_id: str) -> dict[str, Any]:
        data = await self._get(f"{_MESSAGES_URL}/{quote(message_id, safe='')}")
        items = data.get("items")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and item.get("message_id") == message_id:
                    return item
        if data.get("message_id") == message_id:
            return data
        raise FeishuReadError("Feishu topic root was not returned")


def _file_message(item: dict[str, Any]) -> dict[str, Any] | None:
    if item.get("msg_type") != "file":
        return None
    message_id = _nonempty(item.get("message_id"))
    if not message_id:
        raise FeishuReadError("Feishu file message lacked message_id")
    body = item.get("body")
    content = body.get("content") if isinstance(body, dict) else None
    try:
        parsed = json.loads(content) if isinstance(content, str) else {}
    except json.JSONDecodeError:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    sender = item.get("sender")
    sender = sender if isinstance(sender, dict) else {}
    return {
        "message_id": message_id,
        "chat_id": _nonempty(item.get("chat_id")),
        "root_id": _nonempty(item.get("root_id")),
        "parent_id": _nonempty(item.get("parent_id")),
        "thread_id": _nonempty(item.get("thread_id")),
        "file_key": _nonempty(parsed.get("file_key")),
        "file_name": _nonempty(parsed.get("file_name") or parsed.get("name")),
        "sender_type": _nonempty(sender.get("sender_type")),
        "sender_id": _nonempty(sender.get("id")),
        "deleted": item.get("deleted") is True,
    }


async def _scan_scope(
    reader: FeishuReader,
    journal: Journal,
    target: Target,
    container_type: str,
    container_id: str,
) -> None:
    """Persist a page before advancing its opaque cursor."""
    rows = [
        row
        for row in journal.for_target(target)
        if row.get("type") == "page"
        and row.get("container_type") == container_type
        and row.get("container_id") == container_id
    ]
    if rows and rows[-1]["next_page_token"] is None:
        return
    page_token = rows[-1]["next_page_token"] if rows else None
    seen_tokens = {row["request_page_token"] for row in rows}
    while True:
        if page_token in seen_tokens:
            raise FeishuReadError("Feishu pagination cursor repeated")
        response = await reader.list_messages(container_type, container_id, page_token)
        items = response.get("items")
        if not isinstance(items, list):
            raise FeishuReadError("Feishu history page lacked items")
        files: list[dict[str, Any]] = []
        threads: list[dict[str, str]] = []
        topic_root_seen = False
        for item in items:
            if not isinstance(item, dict):
                raise FeishuReadError(
                    "Feishu history page contained an invalid message"
                )
            chat = _nonempty(item.get("chat_id"))
            if chat and chat != target.chat_id:
                raise FeishuReadError("Feishu history returned another chat")
            thread_id = _nonempty(item.get("thread_id"))
            if (
                container_type == "chat"
                and item.get("message_id") == target.topic_message_id
            ):
                topic_root_seen = True
            if container_type == "chat" and thread_id:
                threads.append(
                    {
                        "thread_id": thread_id,
                        "message_id": str(item.get("message_id") or ""),
                    }
                )
            file = _file_message(item)
            if file:
                file["container_type"] = container_type
                file["container_id"] = container_id
                files.append(file)
        has_more = response.get("has_more")
        if not isinstance(has_more, bool):
            raise FeishuReadError("Feishu history page lacked has_more")
        next_token = _nonempty(response.get("page_token")) if has_more else None
        if has_more and (not next_token or next_token == page_token):
            raise FeishuReadError("Feishu history page lacked a new cursor")
        journal.append(
            {
                "type": "page",
                "key": target.key,
                "source_sha256": target.source_sha256,
                "container_type": container_type,
                "container_id": container_id,
                "request_page_token": page_token,
                "next_page_token": next_token,
                "message_count": len(items),
                "topic_root_seen": topic_root_seen,
                "threads": threads,
                "files": files,
            }
        )
        if next_token is None:
            return
        seen_tokens.add(page_token)
        page_token = next_token


def _scope_files(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [file for row in rows if row.get("type") == "page" for file in row["files"]]


def _summary(
    target: Target, rows: list[dict[str, Any]], thread_id: str | None, app_id: str
) -> dict[str, Any]:
    from dyvine.services.delivery import legacy_upload_file_name

    page_rows = [row for row in rows if row.get("type") == "page"]
    by_message: dict[str, dict[str, Any]] = {}
    for file in _scope_files(page_rows):
        by_message[file["message_id"]] = file
    active = [file for file in by_message.values() if not file["deleted"]]
    topic_files = [
        file
        for file in active
        if (
            thread_id is not None
            and (
                (file["container_type"], file["container_id"]) == ("thread", thread_id)
                or file["thread_id"] == thread_id
            )
        )
        or file["root_id"] == target.topic_message_id
        or file["parent_id"] == target.topic_message_id
    ]
    app_files = [
        file
        for file in topic_files
        if file["sender_type"] == "app" and file["sender_id"] == app_id
    ]
    app_group_files = [
        file
        for file in active
        if file["sender_type"] == "app" and file["sender_id"] == app_id
    ]
    topic_message_ids = {file["message_id"] for file in topic_files}
    app_outside_topic = [
        file["message_id"]
        for file in app_group_files
        if file["message_id"] not in topic_message_ids
    ]
    by_id = {file["message_id"]: file for file in app_files}
    keys: dict[str, list[dict[str, Any]]] = {}
    for file in app_files:
        if file["file_key"]:
            keys.setdefault(file["file_key"], []).append(file)
    names: dict[str, list[dict[str, Any]]] = {}
    for file in app_files:
        if file["file_name"]:
            names.setdefault(file["file_name"], []).append(file)
    path_names: dict[str, int] = {}
    for file in target.files:
        name = legacy_upload_file_name(Path(file["relative_path"]))
        path_names[name] = path_names.get(name, 0) + 1
    receipts: list[dict[str, str]] = []
    candidates: list[dict[str, str]] = []
    missing: list[str] = []
    used_messages: set[str] = set()
    repeated_receipt = False
    for ledger in target.files:
        path = ledger["relative_path"]
        match = by_id.get(ledger["message_id"]) if ledger["message_id"] else None
        method = "message_id"
        if match is None and ledger["file_key"]:
            matches_by_key = keys.get(ledger["file_key"], [])
            match = matches_by_key[0] if len(matches_by_key) == 1 else None
            method = "file_key"
        if match is not None and match["message_id"] in used_messages:
            repeated_receipt = True
            missing.append(path)
            continue
        if match is not None:
            used_messages.add(match["message_id"])
            receipts.append(
                {"relative_path": path, "message_id": match["message_id"], "by": method}
            )
            continue
        missing.append(path)
        name = legacy_upload_file_name(Path(path))
        matches = names.get(name, [])
        if (
            path_names[name] == 1
            and len(matches) == 1
            and matches[0]["message_id"] not in used_messages
        ):
            candidates.append(
                {
                    "relative_path": path,
                    "message_id": matches[0]["message_id"],
                    "by": "unique_file_name_only",
                }
            )
    legacy = target.legacy or {}
    legacy_safe = legacy.get("first_seen_safe_sent_paths")
    discrepancies: list[str] = []
    legacy_ledger_count = sum(
        1 for file in target.files if file["status"] == "legacy_confirmed_sent"
    )
    if isinstance(legacy_safe, int) and legacy_safe != legacy_ledger_count:
        discrepancies.append("legacy_safe_sent_count_vs_ledger_files")
    if isinstance(legacy_safe, int) and legacy_safe != len(app_files):
        discrepancies.append("legacy_safe_sent_count_vs_app_topic_files")
    if isinstance(legacy_safe, int) and legacy_safe != len(app_group_files):
        discrepancies.append("legacy_safe_sent_count_vs_app_group_files")
    sent_ledger = sum(1 for file in target.files if file["status"] == "sent")
    if sent_ledger > len(receipts):
        discrepancies.append("sent_ledger_receipt_missing_from_app_topic")
    if repeated_receipt:
        discrepancies.append("receipt_reused_by_multiple_ledger_files")
    if app_outside_topic:
        discrepancies.append("app_files_outside_verified_topic")
    if len(active) == 0:
        discrepancies.append("zero_file_group")
    return {
        "type": "account",
        "key": target.key,
        "source_sha256": target.source_sha256,
        "round": target.round,
        "sec_user_id": target.sec_user_id,
        "nickname": target.nickname,
        "chat_id": target.chat_id,
        "topic_message_id": target.topic_message_id,
        "thread_id": thread_id,
        "scan_complete": True,
        "group_file_count": len(active),
        "topic_file_count": len(topic_files),
        "app_topic_file_count": len(app_files),
        "app_group_file_count": len(app_group_files),
        "app_outside_topic_message_ids": app_outside_topic,
        "legacy_safe_sent_paths": legacy_safe,
        "ledger_file_count": len(target.files),
        "ledger_sent_count": sent_ledger,
        "verified_receipts": receipts,
        "candidate_receipts": candidates,
        "unmatched_ledger_paths": missing,
        "discrepancies": discrepancies,
        "zero_file_group": not active,
        "send_blocked": True,
    }


async def _audit_target(
    target: Target,
    reader: FeishuReader,
    journal: Journal,
    chat_owners: dict[str, set[str]],
    app_id: str,
) -> dict[str, Any]:
    rows = journal.for_target(target)
    completed = [row for row in rows if row.get("type") == "account"]
    if completed and completed[-1].get("retryable_read_error") is not True:
        return completed[-1]
    issue = _target_issue(target, chat_owners)
    if issue:
        return _hold_target(target, journal, issue)
    assert target.chat_id and target.topic_message_id
    try:
        await _scan_scope(reader, journal, target, "chat", target.chat_id)
    except FeishuReadError:
        return _hold_target(
            target, journal, "feishu_chat_read_failed", retryable_read_error=True
        )
    rows = journal.for_target(target)
    chat_root_seen = any(
        row.get("topic_root_seen") is True
        or any(
            thread.get("message_id") == target.topic_message_id
            for thread in row.get("threads", [])
        )
        for row in rows
        if row.get("type") == "page" and row.get("container_type") == "chat"
    )
    if not chat_root_seen:
        return _hold_target(target, journal, "topic_root_absent_from_chat_history")
    chat_root_threads = {
        thread["thread_id"]
        for row in rows
        if row.get("type") == "page" and row.get("container_type") == "chat"
        for thread in row["threads"]
        if thread.get("message_id") == target.topic_message_id
    }
    if len(chat_root_threads) > 1:
        return _hold_target(target, journal, "topic_root_thread_conflict")
    roots = [
        row
        for row in rows
        if row.get("type") == "topic_root"
        and (
            row.get("thread_id_source") in {"api", "chat", "absent"}
            or row.get("thread_id") != target.topic_message_id
        )
    ]
    if roots:
        root = roots[-1]
    else:
        try:
            message = await reader.get_message(target.topic_message_id)
        except FeishuReadError:
            return _hold_target(
                target, journal, "feishu_topic_read_failed", retryable_read_error=True
            )
        if (
            message.get("message_id") != target.topic_message_id
            or (
                message.get("chat_id") != target.chat_id
                and message.get("chat_id") is not None
            )
            or message.get("deleted") is True
        ):
            return _hold_target(target, journal, "topic_root_does_not_match_group")
        api_thread_id = _nonempty(message.get("thread_id"))
        chat_thread_id = next(iter(chat_root_threads), None)
        if api_thread_id and chat_thread_id and api_thread_id != chat_thread_id:
            return _hold_target(target, journal, "topic_root_thread_conflict")
        actual_thread_id = api_thread_id or chat_thread_id
        root = {
            "type": "topic_root",
            "key": target.key,
            "source_sha256": target.source_sha256,
            "message_id": target.topic_message_id,
            "thread_id": actual_thread_id,
            "thread_id_source": (
                "api" if api_thread_id else "chat" if chat_thread_id else "absent"
            ),
        }
        journal.append(root)
    thread_id = root["thread_id"]
    thread_ids = {
        thread["thread_id"]
        for row in journal.for_target(target)
        if row.get("type") == "page" and row.get("container_type") == "chat"
        for thread in row["threads"]
    }
    if thread_id:
        thread_ids.add(thread_id)
    for candidate in sorted(thread_ids):
        try:
            await _scan_scope(reader, journal, target, "thread", candidate)
        except FeishuReadError:
            return _hold_target(
                target,
                journal,
                "feishu_thread_read_failed",
                retryable_read_error=True,
            )
    result = _summary(target, journal.for_target(target), thread_id, app_id)
    journal.append(result)
    return result


def _hold_target(
    target: Target,
    journal: Journal,
    reason: str,
    *,
    retryable_read_error: bool = False,
) -> dict[str, Any]:
    """Persist an incomplete account without drawing a delivery conclusion."""
    result = {
        "type": "account",
        "key": target.key,
        "source_sha256": target.source_sha256,
        "round": target.round,
        "sec_user_id": target.sec_user_id,
        "nickname": target.nickname,
        "chat_id": target.chat_id,
        "topic_message_id": target.topic_message_id,
        "scan_complete": False,
        "reason": reason,
        "retryable_read_error": retryable_read_error,
        "send_blocked": True,
    }
    journal.append(result)
    return result
