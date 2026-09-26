"""Read-only, bounded Feishu history proof for one missing legacy topic.

The legacy sender posted a profile root into an account's chat whenever it
started delivering there, and recorded the most recent root as the round's
topic. When the progress checkpoint lost that record, or the recorded root
was deleted, the complete chat history still identifies it: the latest live
app-authored root that links to exactly this account's profile. A chat whose
profile root predates exact links is accepted when exactly one app-authored
root carries a Douyin short link and none names any profile.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Protocol
from urllib.parse import urlsplit

from scripts.feishu_audit_core import AuditError, _nonempty
from scripts.legacy_group_candidates import Candidate

_MAX_CHAT_PAGES = 2000
_PROFILE_HOSTS = frozenset({"douyin.com", "www.douyin.com"})
_SHORT_LINK_HOST = "v.douyin.com"
_TEXT_URL = re.compile(r"https://[^\s\"'<>]+")


class ReadableHistory(Protocol):
    async def get_chat(self, chat_id: str) -> dict[str, Any]: ...

    async def get_message(self, message_id: str) -> dict[str, Any]: ...

    async def list_messages(
        self, container_type: str, container_id: str, page_token: str | None
    ) -> dict[str, Any]: ...


def _links(content: Any) -> list[str]:
    """Hyperlinks and plain-text https URLs anywhere in a post or text body."""
    found: list[str] = []
    values = [content]
    while values:
        value = values.pop()
        if isinstance(value, list):
            values.extend(value)
        elif isinstance(value, dict):
            href = value.get("href") if value.get("tag") == "a" else None
            if isinstance(href, str):
                found.append(href)
            values.extend(value.values())
        elif isinstance(value, str):
            found.extend(_TEXT_URL.findall(value))
    return found


def _https_host(href: str) -> str | None:
    try:
        url = urlsplit(href)
        return url.hostname if url.scheme == "https" else None
    except ValueError:
        return None


def _profile_owner(href: str) -> str | None:
    """The account an exact ``/user/<sec>`` Douyin profile URL names."""
    if _https_host(href) not in _PROFILE_HOSTS:
        return None
    parts = urlsplit(href).path.split("/")
    if len(parts) == 4 and parts[3] == "":
        parts.pop()
    if len(parts) != 3 or parts[:2] != ["", "user"] or not parts[2]:
        return None
    return parts[2]


def _short_link(href: str) -> bool:
    return _https_host(href) == _SHORT_LINK_HOST


def _root_links(item: dict[str, Any], app_id: str) -> list[str] | None:
    """Links of a live app-authored root post or text; None when unreadable."""
    sender = item.get("sender")
    if (
        item.get("msg_type") not in {"post", "text"}
        or item.get("deleted") is True
        or _nonempty(item.get("root_id"))
        or _nonempty(item.get("parent_id"))
        or not isinstance(sender, dict)
        or sender.get("sender_type") != "app"
        or sender.get("id") != app_id
    ):
        return []
    body = item.get("body")
    content = body.get("content") if isinstance(body, dict) else None
    try:
        parsed = json.loads(content) if isinstance(content, str) else content
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return _links(parsed)


def _created_ms(item: dict[str, Any]) -> int | None:
    value = item.get("create_time")
    return int(value) if isinstance(value, str) and value.isdigit() else None


def _choose_root(
    exact: list[tuple[str, int | None]], short: list[str], other_profiles: bool
) -> tuple[str | None, str]:
    """Pick the topic root, or return why none is provable."""
    if len(exact) == 1:
        return exact[0][0], "only_profile_root"
    if exact:
        dated = [(message_id, created) for message_id, created in exact if created]
        if len(dated) != len(exact):
            return None, "feishu_profile_post_ambiguous"
        latest = max(created for _, created in dated)
        newest = [message_id for message_id, created in dated if created == latest]
        if len(newest) != 1:
            return None, "feishu_profile_post_ambiguous"
        return newest[0], "latest_profile_root"
    if other_profiles or not short:
        return None, "feishu_profile_post_missing"
    if len(short) != 1:
        return None, "feishu_profile_post_ambiguous"
    return short[0], "single_short_link_root"


async def discover(
    candidate: Candidate,
    reader: ReadableHistory,
    app_id: str,
    expected_owner: str | None,
    *,
    unique_chat: bool,
) -> tuple[str, dict[str, Any]]:
    """Find the account's current profile root after exhausting the chat history."""
    if not unique_chat or not all(
        (candidate.round, candidate.sec_user_id, candidate.nickname, candidate.chat_id)
    ):
        return "discovery_identity_conflict", {}
    chat_id = candidate.chat_id
    sec = candidate.sec_user_id
    try:
        chat = await reader.get_chat(chat_id)
        owner = _nonempty(chat.get("owner_id"))
        if chat.get("chat_id") != chat_id:
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
        exact: list[tuple[str, int | None]] = []
        short: list[str] = []
        other_profiles = False
        token: str | None = None
        seen_tokens: set[str | None] = set()
        message_count = file_count = 0
        for page_number in range(1, _MAX_CHAT_PAGES + 1):
            if token in seen_tokens:
                return "feishu_history_cursor_repeated", {}
            seen_tokens.add(token)
            page = await reader.list_messages("chat", chat_id, token)
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
                if item_chat and item_chat != chat_id:
                    return "feishu_history_chat_mismatch", {}
                digest.update(
                    json.dumps(item, ensure_ascii=False, sort_keys=True).encode()
                )
                digest.update(b"\0")
                message_count += 1
                file_count += int(item.get("msg_type") == "file")
                links = _root_links(item, app_id)
                if links is None:
                    return "feishu_profile_post_unreadable", {}
                owners = {_profile_owner(link) for link in links} - {None}
                if sec in owners:
                    exact.append((message_id, _created_ms(item)))
                other_profiles = other_profiles or bool(owners - {sec})
                if any(_short_link(link) for link in links):
                    short.append(message_id)
            if not page["has_more"]:
                chosen, selection = _choose_root(exact, short, other_profiles)
                if chosen is None:
                    return selection, {"profile_post_matches": len(exact)}
                root = await reader.get_message(chosen)
                root_links = _root_links(root, app_id) or []
                if selection == "single_short_link_root":
                    proven = any(_short_link(link) for link in root_links)
                else:
                    proven = any(_profile_owner(link) == sec for link in root_links)
                if (
                    root.get("message_id") != chosen
                    or root.get("chat_id") != chat_id
                    or not proven
                ):
                    return "feishu_profile_post_changed", {}
                return "discovered", {
                    "discovered_topic_message_id": chosen,
                    "topic_selection": selection,
                    "profile_post_matches": len(exact),
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
