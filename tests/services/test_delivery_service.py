"""Tests for the Feishu group channel and hermes-native notify path."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from dyvine.core.exceptions import DeliveryError
from dyvine.services.delivery import (
    FeishuCredentials,
    FeishuGroupChannel,
    post_datetime_from_path,
    scan_media_files,
    send_via_hermes,
    upload_file_name,
)


class FakeTransport:
    """Scriptable :class:`FeishuTransport` recording every call."""

    def __init__(self) -> None:
        """Create empty scripts and call logs."""
        self.posts: list[dict[str, Any]] = []
        self.puts: list[dict[str, Any]] = []
        self.uploads: list[dict[str, Any]] = []
        self.post_script: list[dict[str, Any]] = []
        self.put_script: list[dict[str, Any]] = []
        self.upload_script: list[dict[str, Any]] = []

    async def post_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Record the call and pop the next scripted response."""
        self.posts.append({"url": url, "payload": payload})
        if self.post_script:
            return self.post_script.pop(0)
        return {"code": 0, "data": {"message_id": "mid-1"}}

    async def post_file(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        file_name: str,
        file_path: Path,
        fields: dict[str, str] | None = None,
        timeout: float = 300.0,
    ) -> dict[str, Any]:
        """Record the upload and pop the next scripted response."""
        self.uploads.append({"file_name": file_name, "path": str(file_path)})
        if self.upload_script:
            return self.upload_script.pop(0)
        return {"code": 0, "data": {"file_key": "fk-1"}}

    async def put_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Record the PUT and pop the next scripted response."""
        self.puts.append({"url": url, "payload": payload})
        if self.put_script:
            return self.put_script.pop(0)
        return {"code": 0}


def _channel(transport: FakeTransport | None = None) -> FeishuGroupChannel:
    """Build a channel with dummy credentials and the fake transport."""
    transport = transport or FakeTransport()
    transport.post_script.append({"code": 0, "tenant_access_token": "token-1"})
    creds = FeishuCredentials(app_id="id", app_secret="secret")
    return FeishuGroupChannel(creds, transport)


def test_upload_file_name_truncates_long_stems() -> None:
    """Names over 50 chars shrink to stem[:40] + ext."""
    long = Path("x" * 48 + ".mp4")
    assert upload_file_name(long) == "x" * 40 + ".mp4"
    assert upload_file_name(Path("short.mp4")) == "short.mp4"


def test_post_datetime_from_path_parses_first_segment(tmp_path: Path) -> None:
    """Nested post dirs parse; foreign paths return None."""
    nested = tmp_path / "2026-08-06 10-20-30_desc" / "file.mp4"
    assert post_datetime_from_path(nested, tmp_path) == datetime(2026, 8, 6, 10, 20, 30)
    assert post_datetime_from_path(tmp_path / "notes.txt", tmp_path) is None
    assert post_datetime_from_path(Path("/elsewhere/x.mp4"), tmp_path) is None


def test_scan_media_files_filters_extensions(tmp_path: Path) -> None:
    """Only media extensions are collected, sorted."""
    (tmp_path / "b.mp4").write_text("x")
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "c.JPG").write_text("x")
    assert [path.name for path in scan_media_files(tmp_path)] == ["b.mp4", "c.JPG"]


async def test_set_group_description_sends_before_files() -> None:
    """Description PUT carries author/homepage/cutoff metadata."""
    transport = FakeTransport()
    channel = _channel(transport)
    await channel.set_group_description(
        chat_id="chat-1", nickname="nick", homepage="https://h"
    )
    assert len(transport.puts) == 1
    assert "chat-1" in transport.puts[0]["url"]
    assert "nick" in transport.puts[0]["payload"]["description"]


async def test_set_group_description_warn_only_on_failure() -> None:
    """Description errors never fail the delivery."""
    transport = FakeTransport()
    transport.put_script.append({"code": 500, "msg": "boom"})
    channel = _channel(transport)
    await channel.set_group_description(
        chat_id="chat-1", nickname="nick", homepage="https://h"
    )


async def test_send_file_refreshes_token_once() -> None:
    """99991663 triggers exactly one token refresh + retry."""
    transport = FakeTransport()
    transport.upload_script.append({"code": 99991663, "msg": "token"})
    channel = _channel(transport)
    transport.post_script.append({"code": 0, "tenant_access_token": "token-2"})
    ok, error = await channel.send_file(
        file_path=Path("x.mp4"), chat_id="chat-1", parent_id="mid-0"
    )
    assert ok is True and error is None
    token_posts = [call for call in transport.posts if "auth" in call["url"]]
    assert len(token_posts) == 2


async def test_create_topic_failure_raises_retryable() -> None:
    """Topic errors raise with the retryable reason."""
    transport = FakeTransport()
    channel = _channel(transport)
    transport.post_script.append({"code": 500, "msg": "down"})
    with pytest.raises(DeliveryError) as excinfo:
        await channel.create_topic(chat_id="chat-1", nickname="n", homepage="https://h")
    assert excinfo.value.reason == "retryable"


async def test_plan_files_filters_and_deletes(tmp_path: Path) -> None:
    """Zero-byte deleted, oversize permanent, old skipped, sent skipped."""
    user_dir = tmp_path / "nick"
    old_dir = user_dir / "2026-08-01 10-00-00_old"
    new_dir = user_dir / "2026-08-10 10-00-00_new"
    old_dir.mkdir(parents=True)
    new_dir.mkdir(parents=True)
    (old_dir / "a.mp4").write_bytes(b"x")
    fresh = new_dir / "b.mp4"
    fresh.write_bytes(b"x")
    zero = new_dir / "z.mp4"
    zero.write_bytes(b"")
    big = new_dir / "big.mp4"
    big.write_bytes(b"x")
    channel = _channel()
    to_send, new_perm, skipped, zero_deleted = channel.plan_files(
        user_dir=user_dir,
        cutoff=datetime(2026, 8, 5),
        already_sent={str(fresh)},
        known_permanent={str(big)},
    )
    assert to_send == []
    assert skipped == 1
    assert zero_deleted == 1
    assert not zero.exists()
    assert str(zero) in new_perm


async def test_send_account_full_flow(tmp_path: Path) -> None:
    """One account delivers end to end with sender-compatible counters."""
    transport = FakeTransport()
    channel = _channel(transport)
    user_dir = tmp_path / "nick"
    post_dir = user_dir / "2026-08-10 10-00-00_post"
    post_dir.mkdir(parents=True)
    (post_dir / "a.mp4").write_bytes(b"x")
    (post_dir / "b.mp4").write_bytes(b"y")
    result = await channel.send_account(
        nickname="nick",
        chat_id="chat-1",
        homepage="https://h",
        user_dir=user_dir,
    )
    assert result.total_files == 2
    assert result.sent_files == 2
    assert result.failed_files == 0
    assert result.status == "completed"
    assert result.starter_message_id == "mid-1"
    # Description first, then topic post, then one message per file.
    assert len(transport.puts) == 1
    assert len(transport.uploads) == 2


async def test_send_account_missing_dir_reports_no_local(tmp_path: Path) -> None:
    """Missing user dirs report ``no_local`` without any network use."""
    transport = FakeTransport()
    channel = _channel(transport)
    result = await channel.send_account(
        nickname="nick",
        chat_id="chat-1",
        homepage="https://h",
        user_dir=tmp_path / "ghost",
    )
    assert result.status == "no_local"
    assert transport.puts == [] and transport.posts == []


async def test_send_account_marks_234006_permanent(tmp_path: Path) -> None:
    """Feishu 234006 failures join the permanent set."""
    transport = FakeTransport()
    transport.upload_script.append({"code": 234006, "msg": "too large"})
    channel = _channel(transport)
    user_dir = tmp_path / "nick"
    post_dir = user_dir / "2026-08-10 10-00-00_post"
    post_dir.mkdir(parents=True)
    victim = post_dir / "a.mp4"
    victim.write_bytes(b"x")
    result = await channel.send_account(
        nickname="nick",
        chat_id="chat-1",
        homepage="https://h",
        user_dir=user_dir,
    )
    assert result.failed_files == 1
    assert result.status == "partial"
    assert result.permanent_failures == [str(victim)]


def test_credentials_prefer_hermes_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hermes env file wins over process environment."""
    env_file = tmp_path / ".env"
    env_file.write_text('FEISHU_APP_ID=file-id\nFEISHU_APP_SECRET="file-secret"\n')
    monkeypatch.setenv("FEISHU_APP_ID", "env-id")
    monkeypatch.setenv("FEISHU_APP_SECRET", "env-secret")
    creds = FeishuCredentials.from_hermes_default(env_file)
    assert (creds.app_id, creds.app_secret) == ("file-id", "file-secret")


def test_credentials_require_both_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing keys raise instead of authenticating half-empty."""
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    with pytest.raises(DeliveryError, match="Missing FEISHU_APP_ID"):
        FeishuCredentials.from_hermes_default(tmp_path / "missing.env")


class _FakeProcess:
    """Minimal ``asyncio`` subprocess double."""

    def __init__(
        self, returncode: int, stdout: bytes = b"", stderr: bytes = b""
    ) -> None:
        """Preset the exit code and pipes."""
        self.returncode: int | None = returncode
        self._out = stdout
        self._err = stderr
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        """Return the preset pipes."""
        return self._out, self._err

    def kill(self) -> None:
        """Record the kill (timeout path)."""
        self.killed = True


async def test_send_via_hermes_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exit 0 returns the target plus raw output."""

    async def _spawn(*args: Any, **kwargs: Any) -> _FakeProcess:
        assert args[:4] == ("hermes", "send", "--to", "telegram")
        assert "--json" in args
        return _FakeProcess(0, stdout=b'{"ok":true}')

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    result = await send_via_hermes(target="telegram", message="hi")
    assert result.ok is True and result.exit_code == 0


async def test_send_via_hermes_delivery_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exit 1 raises retryable; exit 2 raises failed."""

    async def _spawn_fail(*args: Any, **kwargs: Any) -> _FakeProcess:
        return _FakeProcess(1, stderr=b"backend down")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn_fail)
    with pytest.raises(DeliveryError) as excinfo:
        await send_via_hermes(target="telegram", message="hi")
    assert excinfo.value.reason == "retryable"

    async def _spawn_usage(*args: Any, **kwargs: Any) -> _FakeProcess:
        return _FakeProcess(2, stderr=b"bad target")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn_usage)
    with pytest.raises(DeliveryError) as excinfo:
        await send_via_hermes(target="telegram", message="hi")
    assert excinfo.value.reason == "failed"


async def test_send_via_hermes_validates_target() -> None:
    """Blank targets fail before spawning anything."""
    with pytest.raises(DeliveryError, match="target is required"):
        await send_via_hermes(target="  ", message="hi")
