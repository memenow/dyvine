"""Crash-safe Feishu group, topic, and per-file delivery operations."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import quote, urlparse

import httpx

from ..core.exceptions import DeliveryError
from ..db.protocols import DeliveryLedgerRepository
from ..db.records import DeliveryGroupRecord, FileDeliveryRecord

if TYPE_CHECKING:
    from .delivery import AccountDelivery

_CHATS_URL = "https://open.feishu.cn/open-apis/im/v1/chats?user_id_type=open_id"
_IM_IMAGES_URL = "https://open.feishu.cn/open-apis/im/v1/images"
_IM_FILES_URL = "https://open.feishu.cn/open-apis/im/v1/files"
_SAFE_SEND_WINDOW = timedelta(minutes=55)
_SAFE_GROUP_WINDOW = timedelta(minutes=55)
_MAX_UPLOAD_BYTES = 29 * 1024 * 1024
_MAX_AVATAR_BYTES = 5 * 1024 * 1024


class _Channel(Protocol):
    _transport: Any

    async def _auth_token(self) -> str: ...

    async def _refresh_token(self) -> str: ...

    @staticmethod
    def _is_token_error(payload: Any) -> bool: ...

    async def _send_message(
        self,
        chat_id: str,
        msg_type: str,
        content: dict[str, Any],
        *,
        request_uuid: str,
    ) -> tuple[dict[str, Any] | None, Any]: ...


def _within(started_at: str | None, window: timedelta) -> bool:
    if not started_at:
        return False
    try:
        started = datetime.fromisoformat(started_at)
    except ValueError:
        return False
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return datetime.now(UTC) - started < window


def media_identity(
    *, sec_user_id: str, user_dir: Path, file_path: Path
) -> tuple[str, str, str]:
    """Hash account-relative path plus content without loading media at once.

    Example:
        >>> import tempfile
        >>> from pathlib import Path
        >>> with tempfile.TemporaryDirectory() as root:
        ...     base = Path(root)
        ...     file = base / "clip.mp4"
        ...     _ = file.write_bytes(b"x")
        ...     media_identity(
        ...         sec_user_id="sec", user_dir=base, file_path=file
        ...     )[1]
        'clip.mp4'
    """
    root = user_dir.resolve(strict=True)
    resolved = file_path.resolve(strict=True)
    try:
        resolved.relative_to(root)
        relative = file_path.relative_to(user_dir).as_posix()
    except ValueError as error:
        raise ValueError("Media path is outside account directory") from error
    if not relative or relative.startswith("../"):
        raise ValueError("Media path is outside account directory")
    digest = hashlib.sha256()
    with file_path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    content_sha256 = digest.hexdigest()
    media_id = hashlib.sha256(
        f"{sec_user_id}\0{relative}\0{content_sha256}".encode()
    ).hexdigest()
    return media_id, relative, content_sha256


async def _post_json(
    channel: _Channel, url: str, payload: dict[str, Any]
) -> dict[str, Any]:
    token = await channel._auth_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    data = await channel._transport.post_json(
        url, headers=headers, payload=payload, timeout=120.0
    )
    if data.get("code") != 0 and channel._is_token_error(data):
        token = await channel._refresh_token()
        headers["Authorization"] = f"Bearer {token}"
        data = await channel._transport.post_json(
            url, headers=headers, payload=payload, timeout=120.0
        )
    return dict(data) if isinstance(data, dict) else {}


async def _upload_avatar(channel: _Channel, avatar_url: str) -> str:
    parsed = urlparse(avatar_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise DeliveryError("Avatar URL must use HTTPS", reason="failed")
    if parsed.hostname == "localhost":
        raise DeliveryError("Avatar URL host is invalid", reason="failed")
    try:
        literal_ip = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        literal_ip = None
    if literal_ip is not None and not literal_ip.is_global:
        raise DeliveryError("Avatar URL host is invalid", reason="failed")
    async with httpx.AsyncClient(follow_redirects=False) as client:
        async with client.stream("GET", avatar_url, timeout=30.0) as response:
            response.raise_for_status()
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > _MAX_AVATAR_BYTES:
                    raise DeliveryError("Avatar image size is invalid", reason="failed")
                chunks.append(chunk)
    content = b"".join(chunks)
    if not content or len(content) > _MAX_AVATAR_BYTES:
        raise DeliveryError("Avatar image size is invalid", reason="failed")
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as temp:
        temp.write(content)
        path = Path(temp.name)
    try:
        token = await channel._auth_token()
        headers = {"Authorization": f"Bearer {token}"}
        data = await channel._transport.post_file(
            _IM_IMAGES_URL,
            headers=headers,
            file_name="avatar.jpg",
            file_path=path,
            file_field="image",
            fields={"image_type": "avatar"},
        )
        if data.get("code") != 0 and channel._is_token_error(data):
            token = await channel._refresh_token()
            headers["Authorization"] = f"Bearer {token}"
            data = await channel._transport.post_file(
                _IM_IMAGES_URL,
                headers=headers,
                file_name="avatar.jpg",
                file_path=path,
                file_field="image",
                fields={"image_type": "avatar"},
            )
        if data.get("code") != 0:
            raise DeliveryError(
                f"Feishu avatar upload failed (code {data.get('code')})",
                reason="retryable",
            )
        image_key = (data.get("data") or {}).get("image_key")
        if not isinstance(image_key, str) or not image_key:
            raise DeliveryError(
                "Feishu avatar upload returned no key", reason="retryable"
            )
        return image_key
    finally:
        path.unlink(missing_ok=True)


async def ensure_group(
    channel: _Channel,
    *,
    ledger: DeliveryLedgerRepository,
    round: str,
    sec_user_id: str,
    nickname: str,
    owner_open_id: str,
    avatar_url: str | None = None,
) -> DeliveryGroupRecord:
    """Create a group once; ambiguous outcomes keep the persisted UUID."""
    if not owner_open_id.startswith("ou_"):
        raise DeliveryError("Group owner must be an open_id", reason="failed")
    group = await ledger.reserve_group(
        round=round,
        sec_user_id=sec_user_id,
        nickname=nickname,
        owner_open_id=owner_open_id,
        avatar_url=avatar_url,
    )
    if group.status == "ready":
        return group
    if group.status == "needs_review":
        raise DeliveryError("Group creation needs review", reason="failed")
    if not _within(group.create_started_at, _SAFE_GROUP_WINDOW):
        await ledger.mark_group_review(group.key)
        raise DeliveryError(
            "Group creation window expired; review required", reason="failed"
        )
    if not group.create_uuid or not group.owner_open_id:
        await ledger.mark_group_review(group.key)
        raise DeliveryError(
            "Group intent is incomplete; review required", reason="failed"
        )
    if group.avatar_url and not group.avatar_key:
        avatar_key = await _upload_avatar(channel, group.avatar_url)
        group = await ledger.set_avatar_key(group.key, avatar_key)
    create_uuid = group.create_uuid
    owner = group.owner_open_id
    if not create_uuid or not owner:
        await ledger.mark_group_review(group.key)
        raise DeliveryError(
            "Group intent changed during avatar upload", reason="failed"
        )
    payload: dict[str, Any] = {
        "name": group.create_name,
        "owner_id": owner,
        "user_id_list": [owner],
        "chat_mode": "group",
        "chat_type": "private",
        "group_message_type": "thread",
    }
    if group.avatar_key:
        payload["avatar"] = group.avatar_key
    create_url = f"{_CHATS_URL}&uuid={quote(create_uuid, safe='')}"
    try:
        data = await _post_json(channel, create_url, payload)
    except Exception as error:
        await ledger.mark_group_review(group.key)
        raise DeliveryError(
            "Group creation response is unknown; review required", reason="failed"
        ) from error
    if data.get("code") == 232023 and group.create_name == group.nickname:
        group = await ledger.rotate_group_uuid(group.key, f"{nickname}🍊")
        payload = {**payload, "name": group.create_name}
        if not group.create_uuid:
            await ledger.mark_group_review(group.key)
            raise DeliveryError("Group retry has no UUID", reason="failed")
        retry_url = f"{_CHATS_URL}&uuid={quote(group.create_uuid, safe='')}"
        try:
            data = await _post_json(channel, retry_url, payload)
        except Exception as error:
            await ledger.mark_group_review(group.key)
            raise DeliveryError(
                "Group creation response is unknown; review required", reason="failed"
            ) from error
    if data.get("code") != 0:
        await ledger.mark_group_review(group.key)
        raise DeliveryError(
            f"Feishu group creation requires review (code {data.get('code')})",
            reason="failed",
        )
    chat_id = (data.get("data") or {}).get("chat_id")
    if not isinstance(chat_id, str) or not chat_id:
        await ledger.mark_group_review(group.key)
        raise DeliveryError("Feishu group response lacks chat_id", reason="failed")
    return await ledger.mark_group_ready(group.key, chat_id)


async def ensure_topic(
    channel: _Channel,
    *,
    ledger: DeliveryLedgerRepository,
    round: str,
    sec_user_id: str,
    chat_id: str,
    nickname: str,
    homepage: str,
) -> DeliveryGroupRecord:
    """Create the first profile post once, then persist its message ID."""
    group = await ledger.get_group(round=round, sec_user_id=sec_user_id)
    if group is None or group.status != "ready" or group.chat_id != chat_id:
        raise DeliveryError("Group is not verified in delivery ledger", reason="failed")
    if group.topic_status == "ready":
        return group
    if group.topic_status == "needs_review":
        raise DeliveryError("Topic creation needs review", reason="failed")
    group = await ledger.begin_topic(group.key)
    if not _within(group.topic_started_at, _SAFE_SEND_WINDOW):
        await ledger.mark_topic_review(group.key)
        raise DeliveryError(
            "Topic creation window expired; review required", reason="failed"
        )
    if not group.topic_uuid:
        await ledger.mark_topic_review(group.key)
        raise DeliveryError(
            "Topic intent has no UUID; review required", reason="failed"
        )
    content = {
        "zh_cn": {
            "title": f"👤 {nickname}",
            "content": [
                [
                    {"tag": "text", "text": "抖音主页：", "style": ["bold"]},
                    {"tag": "a", "text": nickname, "href": homepage},
                ]
            ],
        }
    }
    response, error = await channel._send_message(
        chat_id, "post", content, request_uuid=group.topic_uuid
    )
    if error is not None:
        raise DeliveryError("Feishu topic send failed", reason="retryable")
    message_id = (response or {}).get("message_id")
    if not isinstance(message_id, str) or not message_id:
        raise DeliveryError("Feishu topic returned no id", reason="retryable")
    return await ledger.mark_topic_ready(group.key, message_id)


async def _upload_media(
    channel: _Channel, file_path: Path
) -> tuple[str | None, int | None]:
    from .delivery import upload_file_name

    token = await channel._auth_token()
    headers = {"Authorization": f"Bearer {token}"}
    name = upload_file_name(file_path)
    fields = {"file_type": "stream", "file_name": name}
    data = await channel._transport.post_file(
        _IM_FILES_URL,
        headers=headers,
        file_name=name,
        file_path=file_path,
        fields=fields,
    )
    if data.get("code") != 0 and channel._is_token_error(data):
        token = await channel._refresh_token()
        headers["Authorization"] = f"Bearer {token}"
        data = await channel._transport.post_file(
            _IM_FILES_URL,
            headers=headers,
            file_name=name,
            file_path=file_path,
            fields=fields,
        )
    if data.get("code") != 0:
        return None, data.get("code")
    file_key = (data.get("data") or {}).get("file_key")
    return file_key if isinstance(file_key, str) and file_key else None, None


async def deliver_file(
    channel: _Channel,
    *,
    ledger: DeliveryLedgerRepository,
    round: str,
    sec_user_id: str,
    user_dir: Path,
    file_path: Path,
    chat_id: str,
) -> FileDeliveryRecord:
    """Send at most once outside Feishu's one-hour UUID dedupe window.

    Files go to the chat as plain messages, after the account's profile post,
    as the legacy sender posted them.
    """
    media_id, relative, content_hash = media_identity(
        sec_user_id=sec_user_id, user_dir=user_dir, file_path=file_path
    )
    legacy = await ledger.find_legacy_sent(
        sec_user_id=sec_user_id, relative_path=relative
    )
    if legacy is not None:
        return legacy
    prior = await ledger.find_prior_sent(
        sec_user_id=sec_user_id, relative_path=relative
    )
    if prior is not None:
        return prior
    legacy_permanent = await ledger.find_legacy_permanent_failure(
        sec_user_id=sec_user_id, relative_path=relative
    )
    if legacy_permanent is not None:
        return legacy_permanent
    item = await ledger.reserve_file(
        media_id=media_id,
        round=round,
        sec_user_id=sec_user_id,
        relative_path=relative,
        content_sha256=content_hash,
        chat_id=chat_id,
        parent_id=None,
    )
    if item.status in {"sent", "needs_review", "permanent_failure"}:
        return item
    if await ledger.find_legacy_unverified_hold(legacy_path=str(file_path)):
        return await ledger.mark_file_review(media_id)
    # An intent reserved for a topic-thread reply may already sit in that
    # thread; a plain resend could repeat it, so it waits for review.
    if item.chat_id != chat_id or item.parent_id is not None:
        return await ledger.mark_file_review(media_id)
    if item.status == "planned":
        size = file_path.stat().st_size
        if size == 0 or size > _MAX_UPLOAD_BYTES:
            return await ledger.mark_permanent_failure(media_id)
        file_key, code = await _upload_media(channel, file_path)
        if code in {234006, 234010}:
            return await ledger.mark_permanent_failure(media_id)
        if code is not None or not file_key:
            raise DeliveryError(
                f"Feishu file upload failed (code {code})", reason="retryable"
            )
        item = await ledger.set_file_key(media_id, file_key)
    if item.status == "uploaded":
        item = await ledger.begin_send(media_id)
    if item.status != "sending" or not item.file_key or not item.send_uuid:
        return await ledger.mark_file_review(media_id)
    if not _within(item.send_started_at, _SAFE_SEND_WINDOW):
        return await ledger.mark_file_review(media_id)
    try:
        response, error = await channel._send_message(
            chat_id, "file", {"file_key": item.file_key}, request_uuid=item.send_uuid
        )
    except DeliveryError:
        return item
    if error is not None:
        return item
    message_id = (response or {}).get("message_id")
    if not isinstance(message_id, str) or not message_id:
        return item
    return await ledger.mark_sent(media_id, message_id)


async def send_account_durable(
    channel: Any,
    *,
    ledger: DeliveryLedgerRepository,
    round: str,
    sec_user_id: str,
    nickname: str,
    chat_id: str,
    homepage: str,
    user_dir: Path,
    cutoff: datetime | None = None,
    starter_message_id: str | None = None,
) -> AccountDelivery:
    """Run the legacy tool through the same durable file checkpoints."""
    from .delivery import AccountDelivery, post_datetime_from_path, scan_media_files

    result = AccountDelivery(nickname=nickname, chat_id=chat_id)
    if not user_dir.is_dir():
        result.note = "no_local_dir"
        return result
    group = await ledger.get_group(round=round, sec_user_id=sec_user_id)
    if group is None or group.status != "ready" or group.chat_id != chat_id:
        raise DeliveryError(
            "Chat is not verified in the delivery ledger", reason="failed"
        )
    if group.topic_status != "ready" or not group.topic_message_id:
        raise DeliveryError(
            "Topic is not verified in the delivery ledger", reason="failed"
        )
    if starter_message_id and starter_message_id != group.topic_message_id:
        raise DeliveryError("Topic differs from persisted ledger", reason="failed")
    await channel.set_group_description(
        chat_id=chat_id, nickname=nickname, homepage=homepage
    )
    result.starter_message_id = group.topic_message_id
    for file_path in scan_media_files(user_dir):
        if cutoff is not None:
            posted = post_datetime_from_path(file_path, user_dir)
            if posted is None or posted <= cutoff:
                result.skipped_old += 1
                continue
        item = await deliver_file(
            channel,
            ledger=ledger,
            round=round,
            sec_user_id=sec_user_id,
            user_dir=user_dir,
            file_path=file_path,
            chat_id=chat_id,
        )
        if item.status == "legacy_confirmed_sent":
            continue
        result.total_files += 1
        if item.status == "sent":
            result.sent_files += 1
            await asyncio.sleep(1.0)
        elif item.status == "permanent_failure":
            result.failed_files += 1
            result.failed_paths.append(str(file_path))
            result.permanent_failures.append(str(file_path))
        else:
            result.failed_files += 1
            result.failed_paths.append(str(file_path))
            result.note = item.status
            break
    return result
