"""Crash and retry behavior for Feishu's durable delivery boundary."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from dyvine.core.exceptions import DeliveryError
from dyvine.db.delivery_ledger import post_media_slot
from dyvine.db.records import DeliveryGroupRecord, FileDeliveryRecord
from dyvine.services.delivery import FeishuCredentials, FeishuGroupChannel
from dyvine.services.delivery_durable import media_identity


class Transport:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.uploads: list[dict[str, Any]] = []
        self.responses: list[dict[str, Any] | Exception] = []

    async def post_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        if "tenant_access_token" in url:
            return {"code": 0, "tenant_access_token": "token"}
        self.posts.append({"url": url, "payload": payload})
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return {"code": 0, "data": {"message_id": "message-1", "chat_id": "chat-1"}}

    async def post_file(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        file_name: str,
        file_path: Path,
        fields: dict[str, str] | None = None,
        file_field: str = "file",
        timeout: float = 300.0,
    ) -> dict[str, Any]:
        self.uploads.append(
            {"url": url, "path": file_path, "field": file_field, "name": file_name}
        )
        if "/images" in url:
            return {"code": 0, "data": {"image_key": "avatar-key-1"}}
        return {"code": 0, "data": {"file_key": "file-key-1"}}

    async def put_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        return {"code": 0}


class Ledger:
    def __init__(self) -> None:
        self.files: dict[str, FileDeliveryRecord] = {}
        self.groups: dict[str, DeliveryGroupRecord] = {}
        self.legacy: dict[tuple[str, str], FileDeliveryRecord] = {}
        self.legacy_permanent: dict[tuple[str, str], FileDeliveryRecord] = {}
        self.legacy_holds: set[str] = set()

    async def reserve_file(self, **fields: Any) -> FileDeliveryRecord:
        media_id = fields["media_id"]
        if media_id not in self.files:
            self.files[media_id] = FileDeliveryRecord(
                **fields,
                status="planned",
                file_key=None,
                send_uuid=None,
                send_started_at=None,
                message_id=None,
                legacy_source_path=None,
                legacy_progress_file=None,
                created_at="now",
                updated_at="now",
            )
        return self.files[media_id]

    async def find_legacy_sent(
        self, *, sec_user_id: str, relative_path: str
    ) -> FileDeliveryRecord | None:
        return self.legacy.get((sec_user_id, relative_path))

    async def find_prior_sent(
        self, *, sec_user_id: str, relative_path: str
    ) -> FileDeliveryRecord | None:
        slot = post_media_slot(relative_path)
        return next(
            (
                row
                for row in self.files.values()
                if slot is not None
                and row.sec_user_id == sec_user_id
                and row.status == "sent"
                and post_media_slot(row.relative_path) == slot
            ),
            None,
        )

    async def find_legacy_permanent_failure(
        self, *, sec_user_id: str, relative_path: str
    ) -> FileDeliveryRecord | None:
        return self.legacy_permanent.get((sec_user_id, relative_path))

    async def find_legacy_unverified_hold(self, *, legacy_path: str) -> bool:
        return legacy_path in self.legacy_holds

    async def set_file_key(self, media_id: str, file_key: str) -> FileDeliveryRecord:
        row = self.files[media_id]
        if row.status == "planned":
            self.files[media_id] = replace(row, status="uploaded", file_key=file_key)
        return self.files[media_id]

    async def begin_send(self, media_id: str) -> FileDeliveryRecord:
        row = self.files[media_id]
        if row.status == "uploaded":
            self.files[media_id] = replace(
                row,
                status="sending",
                send_uuid="stable-uuid",
                send_started_at=datetime.now(UTC).isoformat(),
            )
        return self.files[media_id]

    async def mark_sent(self, media_id: str, message_id: str) -> FileDeliveryRecord:
        self.files[media_id] = replace(
            self.files[media_id], status="sent", message_id=message_id
        )
        return self.files[media_id]

    async def mark_file_review(self, media_id: str) -> FileDeliveryRecord:
        self.files[media_id] = replace(self.files[media_id], status="needs_review")
        return self.files[media_id]

    async def mark_permanent_failure(self, media_id: str) -> FileDeliveryRecord:
        self.files[media_id] = replace(self.files[media_id], status="permanent_failure")
        return self.files[media_id]

    async def reserve_group(self, **fields: Any) -> DeliveryGroupRecord:
        key = f"{fields['round']}:{fields['sec_user_id']}"
        if key not in self.groups:
            self.groups[key] = DeliveryGroupRecord(
                key=key,
                round=fields["round"],
                sec_user_id=fields["sec_user_id"],
                nickname=fields["nickname"],
                create_name=fields["nickname"],
                owner_open_id=fields["owner_open_id"],
                status="creating",
                create_uuid="group-uuid",
                create_started_at=datetime.now(UTC).isoformat(),
                chat_id=None,
                topic_status="unstarted",
                topic_uuid=None,
                topic_started_at=None,
                topic_message_id=None,
                avatar_url=fields.get("avatar_url"),
                avatar_key=None,
                legacy_source_file=None,
                created_at="now",
                updated_at="now",
            )
        return self.groups[key]

    async def get_group(
        self, *, round: str, sec_user_id: str
    ) -> DeliveryGroupRecord | None:
        return self.groups.get(f"{round}:{sec_user_id}")

    async def mark_group_ready(self, key: str, chat_id: str) -> DeliveryGroupRecord:
        self.groups[key] = replace(self.groups[key], status="ready", chat_id=chat_id)
        return self.groups[key]

    async def mark_group_review(self, key: str) -> DeliveryGroupRecord:
        self.groups[key] = replace(self.groups[key], status="needs_review")
        return self.groups[key]

    async def rotate_group_uuid(
        self, key: str, create_name: str
    ) -> DeliveryGroupRecord:
        self.groups[key] = replace(
            self.groups[key], create_name=create_name, create_uuid="fallback-uuid"
        )
        return self.groups[key]

    async def set_avatar_key(self, key: str, image_key: str) -> DeliveryGroupRecord:
        self.groups[key] = replace(self.groups[key], avatar_key=image_key)
        return self.groups[key]

    async def begin_topic(self, key: str) -> DeliveryGroupRecord:
        row = self.groups[key]
        if row.topic_status == "unstarted":
            self.groups[key] = replace(
                row,
                topic_status="creating",
                topic_uuid="topic-uuid",
                topic_started_at=datetime.now(UTC).isoformat(),
            )
        return self.groups[key]

    async def mark_topic_ready(self, key: str, message_id: str) -> DeliveryGroupRecord:
        self.groups[key] = replace(
            self.groups[key], topic_status="ready", topic_message_id=message_id
        )
        return self.groups[key]

    async def mark_topic_review(self, key: str) -> DeliveryGroupRecord:
        self.groups[key] = replace(self.groups[key], topic_status="needs_review")
        return self.groups[key]


def _channel(transport: Transport) -> FeishuGroupChannel:
    return FeishuGroupChannel(
        FeishuCredentials(app_id="id", app_secret="secret"), transport
    )


def _media(tmp_path: Path, content: bytes = b"media") -> tuple[Path, Path]:
    user_dir = tmp_path / "nickname"
    path = user_dir / "2026-09-22 10-00-00_post" / "clip.mp4"
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    return user_dir, path


async def test_deliver_file_posts_a_plain_message_with_persisted_identity(
    tmp_path: Path,
) -> None:
    user_dir, path = _media(tmp_path)
    ledger, transport = Ledger(), Transport()
    channel = _channel(transport)
    kwargs = {
        "ledger": ledger,
        "round": "r1",
        "sec_user_id": "sec-1",
        "user_dir": user_dir,
        "file_path": path,
        "chat_id": "chat-1",
    }
    result = await channel.deliver_file(**kwargs)
    assert result.status == "sent"
    assert result.message_id == "message-1"
    assert result.file_key == "file-key-1"
    assert result.send_uuid == "stable-uuid"
    assert transport.posts[0]["url"].endswith("/im/v1/messages?receive_id_type=chat_id")
    assert transport.posts[0]["payload"]["receive_id"] == "chat-1"
    assert "reply_in_thread" not in transport.posts[0]["payload"]
    assert transport.posts[0]["payload"]["uuid"] == "stable-uuid"
    assert (await channel.deliver_file(**kwargs)).status == "sent"
    assert len(transport.posts) == len(transport.uploads) == 1


async def test_prior_send_under_an_edited_caption_is_not_sent_again(
    tmp_path: Path,
) -> None:
    stamp = "2026-09-22 10-00-00"
    user_dir = tmp_path / "nickname"
    ledger, transport = Ledger(), Transport()
    channel = _channel(transport)
    paths = []
    for caption, content in (("old caption", b"media"), ("new caption", b"re-encoded")):
        path = user_dir / f"{stamp}_{caption}" / f"{stamp}_{caption}_video.mp4"
        path.parent.mkdir(parents=True)
        path.write_bytes(content)
        paths.append(path)
    kwargs = {
        "ledger": ledger,
        "round": "r1",
        "sec_user_id": "sec-1",
        "user_dir": user_dir,
        "chat_id": "chat-1",
    }
    first = await channel.deliver_file(**kwargs, file_path=paths[0])
    assert first.status == "sent"
    again = await channel.deliver_file(**kwargs, file_path=paths[1])
    assert again.status == "sent"
    assert again.relative_path == paths[0].relative_to(user_dir).as_posix()
    assert len(transport.posts) == len(transport.uploads) == 1


async def test_images_of_a_long_caption_post_upload_with_distinct_names(
    tmp_path: Path,
) -> None:
    folder = "2026-09-22 10-00-00_" + "a caption long enough to be truncated" * 2
    user_dir = tmp_path / "nickname"
    (user_dir / folder).mkdir(parents=True)
    ledger, transport = Ledger(), Transport()
    channel = _channel(transport)
    for n in (1, 2):
        path = user_dir / folder / f"{folder}_image_{n}.webp"
        path.write_bytes(f"image {n}".encode())
        result = await channel.deliver_file(
            ledger=ledger,
            round="r1",
            sec_user_id="sec-1",
            user_dir=user_dir,
            file_path=path,
            chat_id="chat-1",
        )
        assert result.status == "sent"
    names = [upload["name"] for upload in transport.uploads]
    assert [name[-13:] for name in names] == ["_image_1.webp", "_image_2.webp"]


async def test_ambiguous_send_reuses_key_and_uuid(tmp_path: Path) -> None:
    user_dir, path = _media(tmp_path)
    ledger, transport = Ledger(), Transport()
    transport.responses.append(DeliveryError("connection lost", reason="retryable"))
    channel = _channel(transport)
    kwargs = {
        "ledger": ledger,
        "round": "r1",
        "sec_user_id": "sec-1",
        "user_dir": user_dir,
        "file_path": path,
        "chat_id": "chat-1",
    }
    assert (await channel.deliver_file(**kwargs)).status == "sending"
    assert (await channel.deliver_file(**kwargs)).status == "sent"
    assert len(transport.uploads) == 1
    assert [call["payload"]["uuid"] for call in transport.posts] == [
        "stable-uuid",
        "stable-uuid",
    ]


async def test_an_intent_reserved_for_a_topic_reply_waits_for_review(
    tmp_path: Path,
) -> None:
    user_dir, path = _media(tmp_path)
    ledger, transport = Ledger(), Transport()
    channel = _channel(transport)
    media_id, relative, content_hash = media_identity(
        sec_user_id="sec-1", user_dir=user_dir, file_path=path
    )
    row = await ledger.reserve_file(
        media_id=media_id,
        round="r1",
        sec_user_id="sec-1",
        relative_path=relative,
        content_sha256=content_hash,
        chat_id="chat-1",
        parent_id="topic-1",
    )
    ledger.files[media_id] = replace(
        row,
        status="sending",
        file_key="old-key",
        send_uuid="old-uuid",
        send_started_at=datetime.now(UTC).isoformat(),
    )
    result = await channel.deliver_file(
        ledger=ledger,
        round="r1",
        sec_user_id="sec-1",
        user_dir=user_dir,
        file_path=path,
        chat_id="chat-1",
    )
    assert result.status == "needs_review"
    assert transport.posts == [] and transport.uploads == []


async def test_old_ambiguous_send_requires_review(tmp_path: Path) -> None:
    user_dir, path = _media(tmp_path)
    ledger, transport = Ledger(), Transport()
    channel = _channel(transport)
    media_id, relative, content_hash = media_identity(
        sec_user_id="sec-1", user_dir=user_dir, file_path=path
    )
    row = await ledger.reserve_file(
        media_id=media_id,
        round="r1",
        sec_user_id="sec-1",
        relative_path=relative,
        content_sha256=content_hash,
        chat_id="chat-1",
        parent_id=None,
    )
    ledger.files[media_id] = replace(
        row,
        status="sending",
        file_key="old-key",
        send_uuid="old-uuid",
        send_started_at=(datetime.now(UTC) - timedelta(hours=2)).isoformat(),
    )
    result = await channel.deliver_file(
        ledger=ledger,
        round="r1",
        sec_user_id="sec-1",
        user_dir=user_dir,
        file_path=path,
        chat_id="chat-1",
    )
    assert result.status == "needs_review"
    assert transport.posts == [] and transport.uploads == []


async def test_legacy_sent_and_zero_file_never_send(tmp_path: Path) -> None:
    user_dir, path = _media(tmp_path, b"")
    ledger, transport = Ledger(), Transport()
    channel = _channel(transport)
    kwargs = {
        "ledger": ledger,
        "round": "r1",
        "sec_user_id": "sec-1",
        "user_dir": user_dir,
        "file_path": path,
        "chat_id": "chat-1",
    }
    result = await channel.deliver_file(**kwargs)
    assert result.status == "permanent_failure"
    assert path.exists()
    ledger.legacy[("sec-1", result.relative_path)] = replace(
        result, status="legacy_confirmed_sent"
    )
    assert (await channel.deliver_file(**kwargs)).status == "legacy_confirmed_sent"
    assert transport.posts == [] and transport.uploads == []


async def test_legacy_permanent_failure_never_uploads(tmp_path: Path) -> None:
    user_dir, path = _media(tmp_path)
    ledger, transport = Ledger(), Transport()
    channel = _channel(transport)
    media_id, relative, content_hash = media_identity(
        sec_user_id="sec-1", user_dir=user_dir, file_path=path
    )
    row = await ledger.reserve_file(
        media_id=media_id,
        round="legacy",
        sec_user_id="sec-1",
        relative_path=relative,
        content_sha256=content_hash,
        chat_id="chat-1",
        parent_id=None,
    )
    ledger.legacy_permanent[("sec-1", relative)] = replace(
        row, status="permanent_failure"
    )
    result = await channel.deliver_file(
        ledger=ledger,
        round="r1",
        sec_user_id="sec-1",
        user_dir=user_dir,
        file_path=path,
        chat_id="chat-1",
    )
    assert result.status == "permanent_failure"
    assert transport.posts == [] and transport.uploads == []


async def test_cache_only_legacy_path_requires_review(tmp_path: Path) -> None:
    user_dir, path = _media(tmp_path)
    ledger, transport = Ledger(), Transport()
    ledger.legacy_holds.add(str(path))
    result = await _channel(transport).deliver_file(
        ledger=ledger,
        round="r1",
        sec_user_id="sec-1",
        user_dir=user_dir,
        file_path=path,
        chat_id="chat-1",
    )
    assert result.status == "needs_review"
    assert transport.posts == [] and transport.uploads == []


async def test_group_unknown_result_requires_review_without_recreation() -> None:
    ledger, transport = Ledger(), Transport()
    channel = _channel(transport)
    kwargs = {
        "ledger": ledger,
        "round": "r1",
        "sec_user_id": "sec-1",
        "nickname": "nick",
        "owner_open_id": "ou_user",
    }
    transport.responses.append(DeliveryError("unknown result", reason="retryable"))
    with pytest.raises(DeliveryError):
        await channel.ensure_group(**kwargs)
    assert ledger.groups["r1:sec-1"].status == "needs_review"
    with pytest.raises(DeliveryError):
        await channel.ensure_group(**kwargs)
    assert len(transport.posts) == 1
    assert "uuid=group-uuid" in transport.posts[0]["url"]
    assert "uuid" not in transport.posts[0]["payload"]


async def test_rejected_group_name_uses_persisted_fallback() -> None:
    ledger, transport = Ledger(), Transport()
    transport.responses.append({"code": 232023, "msg": "name rejected"})
    channel = _channel(transport)
    group = await channel.ensure_group(
        ledger=ledger,
        round="r1",
        sec_user_id="sec-1",
        nickname="橘",
        owner_open_id="ou_user",
    )
    assert group.status == "ready" and group.create_name == "橘🍊"
    assert [call["url"].split("uuid=", 1)[1] for call in transport.posts] == [
        "group-uuid",
        "fallback-uuid",
    ]
    assert all("uuid" not in call["payload"] for call in transport.posts)
    assert transport.posts[0]["payload"]["owner_id"] == "ou_user"


async def test_group_avatar_is_uploaded_before_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class AvatarClient:
        async def __aenter__(self) -> AvatarClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        @asynccontextmanager
        async def stream(
            self, method: str, url: str, *, timeout: float
        ) -> AsyncIterator[httpx.Response]:
            response = httpx.Response(
                200, content=b"image", request=httpx.Request("GET", url)
            )
            yield response

    monkeypatch.setattr(
        "dyvine.services.delivery_durable.httpx.AsyncClient",
        lambda **kwargs: AvatarClient(),
    )
    monkeypatch.setattr(
        "dyvine.services.delivery_durable.socket.getaddrinfo",
        lambda *args, **kwargs: [
            (2, 1, 6, "", ("93.184.216.34", 443)),
        ],
    )
    ledger, transport = Ledger(), Transport()
    channel = _channel(transport)
    group = await channel.ensure_group(
        ledger=ledger,
        round="r1",
        sec_user_id="sec-1",
        nickname="nick",
        owner_open_id="ou_user",
        avatar_url="https://img.example/avatar.jpg",
    )
    assert group.avatar_key == "avatar-key-1"
    assert transport.uploads[0]["field"] == "image"
    assert transport.posts[0]["payload"]["avatar"] == "avatar-key-1"


def _public_dns(monkeypatch: pytest.MonkeyPatch, ip: str = "93.184.216.34") -> None:
    """Stub DNS resolution to one address (tests must not touch the net)."""
    monkeypatch.setattr(
        "dyvine.services.delivery_durable.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", (ip, 443))],
    )


async def test_avatar_domain_resolving_private_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A domain pointing at link-local space never gets an HTTP request."""

    def _no_http(**kwargs: Any) -> Any:
        raise AssertionError("must not fetch")

    monkeypatch.setattr("dyvine.services.delivery_durable.httpx.AsyncClient", _no_http)
    _public_dns(monkeypatch, "169.254.169.254")
    ledger, transport = Ledger(), Transport()
    channel = _channel(transport)
    with pytest.raises(DeliveryError, match="host is invalid"):
        await channel.ensure_group(
            ledger=ledger,
            round="r1",
            sec_user_id="sec-1",
            nickname="nick",
            owner_open_id="ou_user",
            avatar_url="https://evil.example/avatar.jpg",
        )


async def test_avatar_rejects_port_and_userinfo_before_any_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-443 ports and userinfo fail closed without DNS or HTTP."""

    def _no_net(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("must not touch the network")

    monkeypatch.setattr("dyvine.services.delivery_durable.socket.getaddrinfo", _no_net)
    monkeypatch.setattr("dyvine.services.delivery_durable.httpx.AsyncClient", _no_net)
    ledger, transport = Ledger(), Transport()
    channel = _channel(transport)
    for url in (
        "https://img.example:8443/avatar.jpg",
        "https://user:pass@img.example/avatar.jpg",
        "https://127.0.0.1/avatar.jpg",
    ):
        with pytest.raises(DeliveryError, match="host is invalid|must use HTTPS"):
            await channel.ensure_group(
                ledger=ledger,
                round="r1",
                sec_user_id="sec-1",
                nickname="nick",
                owner_open_id="ou_user",
                avatar_url=url,
            )


async def test_avatar_redirect_body_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 302 body is not stored as avatar bytes (no redirect following)."""

    class RedirectClient:
        async def __aenter__(self) -> RedirectClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        @asynccontextmanager
        async def stream(
            self, method: str, url: str, *, timeout: float
        ) -> AsyncIterator[httpx.Response]:
            response = httpx.Response(
                302,
                content=b"redirect",
                headers={"location": "https://evil.example/x"},
                request=httpx.Request("GET", url),
            )
            yield response

    monkeypatch.setattr(
        "dyvine.services.delivery_durable.httpx.AsyncClient",
        lambda **kwargs: RedirectClient(),
    )
    _public_dns(monkeypatch)
    ledger, transport = Ledger(), Transport()
    channel = _channel(transport)
    with pytest.raises(DeliveryError, match="status 302"):
        await channel.ensure_group(
            ledger=ledger,
            round="r1",
            sec_user_id="sec-1",
            nickname="nick",
            owner_open_id="ou_user",
            avatar_url="https://img.example/avatar.jpg",
        )


async def test_terminal_send_failure_parks_for_review(tmp_path: Path) -> None:
    """A terminal (non-retryable) send error parks instead of spinning."""
    user_dir, path = _media(tmp_path)
    ledger, transport = Ledger(), Transport()
    transport.responses.append(DeliveryError("client misused", reason="failed"))
    channel = _channel(transport)
    result = await channel.deliver_file(
        ledger=ledger,
        round="r1",
        sec_user_id="sec-1",
        user_dir=user_dir,
        file_path=path,
        chat_id="chat-1",
    )
    assert result.status == "needs_review"


async def test_api_error_send_stays_retryable(tmp_path: Path) -> None:
    """A Feishu API rejection keeps the UUID for a later retry."""
    user_dir, path = _media(tmp_path)
    ledger, transport = Ledger(), Transport()
    transport.responses.append({"code": 1, "msg": "rate limited"})
    channel = _channel(transport)
    kwargs = {
        "ledger": ledger,
        "round": "r1",
        "sec_user_id": "sec-1",
        "user_dir": user_dir,
        "file_path": path,
        "chat_id": "chat-1",
    }
    assert (await channel.deliver_file(**kwargs)).status == "sending"
    assert (await channel.deliver_file(**kwargs)).status == "sent"


async def test_send_ack_without_message_id_stays_retryable(tmp_path: Path) -> None:
    """A malformed success ack keeps sending instead of marking sent."""
    user_dir, path = _media(tmp_path)
    ledger, transport = Ledger(), Transport()
    transport.responses.append({"code": 0, "data": {}})
    channel = _channel(transport)
    kwargs = {
        "ledger": ledger,
        "round": "r1",
        "sec_user_id": "sec-1",
        "user_dir": user_dir,
        "file_path": path,
        "chat_id": "chat-1",
    }
    assert (await channel.deliver_file(**kwargs)).status == "sending"
    assert (await channel.deliver_file(**kwargs)).status == "sent"


async def test_topic_uses_persisted_uuid() -> None:
    ledger, transport = Ledger(), Transport()
    channel = _channel(transport)
    group = await ledger.reserve_group(
        round="r1", sec_user_id="sec-1", nickname="nick", owner_open_id="ou_user"
    )
    await ledger.mark_group_ready(group.key, "chat-1")
    topic = await channel.ensure_topic(
        ledger=ledger,
        round="r1",
        sec_user_id="sec-1",
        chat_id="chat-1",
        nickname="nick",
        homepage="https://example.com/profile",
    )
    assert topic.topic_message_id == "message-1"
    assert transport.posts[0]["payload"]["uuid"] == "topic-uuid"
    assert transport.posts[0]["payload"]["msg_type"] == "post"
    assert (
        await channel.ensure_topic(
            ledger=ledger,
            round="r1",
            sec_user_id="sec-1",
            chat_id="chat-1",
            nickname="nick",
            homepage="https://example.com/profile",
        )
    ).topic_message_id == "message-1"
    assert len(transport.posts) == 1


async def test_topic_failure_retries_with_same_uuid() -> None:
    ledger, transport = Ledger(), Transport()
    channel = _channel(transport)
    group = await ledger.reserve_group(
        round="r1", sec_user_id="sec-1", nickname="nick", owner_open_id="ou_user"
    )
    await ledger.mark_group_ready(group.key, "chat-1")
    transport.responses.append({"code": 500, "msg": "temporarily unavailable"})
    kwargs = {
        "ledger": ledger,
        "round": "r1",
        "sec_user_id": "sec-1",
        "chat_id": "chat-1",
        "nickname": "nick",
        "homepage": "https://example.com/profile",
    }
    with pytest.raises(DeliveryError, match="topic send"):
        await channel.ensure_topic(**kwargs)
    assert ledger.groups[group.key].topic_status == "creating"
    assert (await channel.ensure_topic(**kwargs)).topic_status == "ready"
    assert [call["payload"]["uuid"] for call in transport.posts] == [
        "topic-uuid",
        "topic-uuid",
    ]


async def test_expired_group_and_topic_hold_for_review() -> None:
    ledger, transport = Ledger(), Transport()
    channel = _channel(transport)
    group = await ledger.reserve_group(
        round="r1",
        sec_user_id="sec-1",
        nickname="nick",
        owner_open_id="ou_user",
    )
    ledger.groups[group.key] = replace(
        group,
        create_started_at=(datetime.now(UTC) - timedelta(hours=11)).isoformat(),
    )
    with pytest.raises(DeliveryError, match="review"):
        await channel.ensure_group(
            ledger=ledger,
            round="r1",
            sec_user_id="sec-1",
            nickname="nick",
            owner_open_id="ou_user",
        )
    assert ledger.groups[group.key].status == "needs_review"
    assert transport.posts == []

    ledger.groups[group.key] = replace(
        ledger.groups[group.key],
        status="ready",
        chat_id="chat-1",
        topic_status="creating",
        topic_uuid="topic-uuid",
        topic_started_at=(datetime.now(UTC) - timedelta(hours=2)).isoformat(),
    )
    with pytest.raises(DeliveryError, match="review"):
        await channel.ensure_topic(
            ledger=ledger,
            round="r1",
            sec_user_id="sec-1",
            chat_id="chat-1",
            nickname="nick",
            homepage="https://example.com",
        )
    assert ledger.groups[group.key].topic_status == "needs_review"
    assert transport.posts == []
