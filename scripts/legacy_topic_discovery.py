"""Read-only, bounded Feishu history proof for one missing legacy topic."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol
from urllib.parse import urlsplit

from scripts.feishu_audit_core import AuditError, _nonempty
from scripts.legacy_group_candidates import Candidate

_MAX_CHAT_PAGES = 2000


class ReadableHistory(Protocol):
    async def get_chat(self, chat_id: str) -> dict[str, Any]: ...

    async def get_message(self, message_id: str) -> dict[str, Any]: ...

    async def list_messages(
        self, container_type: str, container_id: str, page_token: str | None
    ) -> dict[str, Any]: ...


def _exact_profile_link(content: Any, sec_user_id: str) -> bool:
    values = [content]
    while values:
        value = values.pop()
        if isinstance(value, list):
            values.extend(value)
        elif isinstance(value, dict):
            href = value.get("href") if value.get("tag") == "a" else None
            if isinstance(href, str):
                try:
                    url = urlsplit(href)
                    if (
                        url.scheme == "https"
                        and url.hostname in {"douyin.com", "www.douyin.com"}
                        and url.path
                        in {f"/user/{sec_user_id}", f"/user/{sec_user_id}/"}
                    ):
                        return True
                except ValueError:
                    pass
            values.extend(value.values())
    return False


def _profile_post(item: dict[str, Any], app_id: str, sec_user_id: str) -> bool | None:
    sender = item.get("sender")
    if (
        item.get("msg_type") != "post"
        or item.get("deleted") is True
        or _nonempty(item.get("root_id"))
        or _nonempty(item.get("parent_id"))
        or not isinstance(sender, dict)
        or sender.get("sender_type") != "app"
        or sender.get("id") != app_id
    ):
        return False
    body = item.get("body")
    content = body.get("content") if isinstance(body, dict) else None
    try:
        parsed = json.loads(content) if isinstance(content, str) else content
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return _exact_profile_link(parsed, sec_user_id)


async def discover(
    candidate: Candidate,
    reader: ReadableHistory,
    app_id: str,
    expected_owner: str | None,
    *,
    unique_chat: bool,
) -> tuple[str, dict[str, Any]]:
    """Find one stable profile root after exhausting a frozen chat history."""
    if not unique_chat or not all(
        (candidate.round, candidate.sec_user_id, candidate.nickname, candidate.chat_id)
    ):
        return "discovery_identity_conflict", {}
    assert candidate.chat_id and candidate.sec_user_id and candidate.nickname
    try:
        chat = await reader.get_chat(candidate.chat_id)
        owner = _nonempty(chat.get("owner_id"))
        if chat.get("chat_id") != candidate.chat_id:
            return "feishu_chat_identity_mismatch", {}
        if chat.get("chat_mode") not in (None, "group"):
            return "feishu_chat_is_not_group", {}
        if chat.get("chat_status") != "normal":
            return "feishu_chat_not_active", {}
        if expected_owner and (
            chat.get("owner_id_type") not in (None, "open_id")
            or owner != expected_owner
        ):
            return "feishu_chat_owner_mismatch", {}
        digest = hashlib.sha256()
        message_ids: set[str] = set()
        matches: list[str] = []
        token: str | None = None
        seen_tokens: set[str | None] = set()
        message_count = file_count = 0
        for page_number in range(1, _MAX_CHAT_PAGES + 1):
            if token in seen_tokens:
                return "feishu_history_cursor_repeated", {}
            seen_tokens.add(token)
            page = await reader.list_messages("chat", candidate.chat_id, token)
            items = page.get("items")
            if not isinstance(items, list) or not isinstance(
                page.get("has_more"), bool
            ):
                return "feishu_history_incomplete", {}
            for item in items:
                if not isinstance(item, dict):
                    return "feishu_history_incomplete", {}
                message_id = _nonempty(item.get("message_id"))
                if not message_id or message_id in message_ids:
                    return "feishu_history_incomplete", {}
                message_ids.add(message_id)
                item_chat = _nonempty(item.get("chat_id"))
                if item_chat and item_chat != candidate.chat_id:
                    return "feishu_history_chat_mismatch", {}
                digest.update(
                    json.dumps(item, ensure_ascii=False, sort_keys=True).encode()
                )
                digest.update(b"\0")
                message_count += 1
                file_count += int(item.get("msg_type") == "file")
                is_profile = _profile_post(item, app_id, candidate.sec_user_id)
                if is_profile is None:
                    return "feishu_profile_post_unreadable", {}
                if is_profile:
                    matches.append(message_id)
            if not page["has_more"]:
                if len(matches) != 1:
                    return (
                        (
                            "feishu_profile_post_missing"
                            if not matches
                            else "feishu_profile_post_ambiguous"
                        ),
                        {"profile_post_matches": len(matches)},
                    )
                root = await reader.get_message(matches[0])
                if (
                    root.get("message_id") != matches[0]
                    or root.get("chat_id") != candidate.chat_id
                    or _profile_post(root, app_id, candidate.sec_user_id) is not True
                ):
                    return "feishu_profile_post_changed", {}
                return "discovered", {
                    "discovered_topic_message_id": matches[0],
                    "chat_history_sha256": digest.hexdigest(),
                    "root_message_sha256": hashlib.sha256(
                        json.dumps(root, ensure_ascii=False, sort_keys=True).encode()
                    ).hexdigest(),
                    "history_pages": page_number,
                    "history_messages": message_count,
                    "history_file_messages": file_count,
                    "chat_owner_open_id": owner,
                }
            next_token = _nonempty(page.get("page_token"))
            if not next_token or next_token == token:
                return "feishu_history_incomplete", {}
            token = next_token
        return "feishu_history_page_limit", {}
    except AuditError as error:
        return "feishu_read_failed", {"read_error": str(error)}
