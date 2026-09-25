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


async def test_send_account_requires_ledger_before_network(tmp_path: Path) -> None:
    """The former direct sender cannot bypass the durable ledger."""
    transport = FakeTransport()
    channel = _channel(transport)
    with pytest.raises(DeliveryError, match="ledger"):
        await channel.send_account(
            nickname="nick",
            chat_id="chat-1",
            homepage="https://h",
            user_dir=tmp_path / "ghost",
        )
    assert transport.puts == [] and transport.posts == []


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
        self, returncode: int | None, stdout: bytes = b"", stderr: bytes = b""
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

    async def wait(self) -> int | None:
        """Reap the process (timeout path must not leave zombies)."""
        self.returncode = -9
        return self.returncode


async def test_send_via_hermes_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exit 0 returns the target plus raw output."""

    async def _spawn(*args: Any, **kwargs: Any) -> _FakeProcess:
        assert args[:3] == ("hermes", "send", "--to=telegram")
        assert "--json" in args
        assert args[-2:] == ("--", "hi")
        return _FakeProcess(0, stdout=b'{"ok":true}')

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    result = await send_via_hermes(target="telegram", message="hi")
    assert result.ok is True and result.exit_code == 0


async def test_send_via_hermes_dash_values_not_parsed_as_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Leading-dash values can never be mistaken for hermes options."""

    async def _spawn(*args: Any, **kwargs: Any) -> _FakeProcess:
        assert args == (
            "hermes",
            "send",
            "--to=-100123",
            "--json",
            "--subject=--help",
            "--",
            "--danger",
        )
        return _FakeProcess(0, stdout=b"{}")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    result = await send_via_hermes(
        target="-100123", message="--danger", subject="--help"
    )
    assert result.ok is True


async def test_send_via_hermes_timeout_reaps_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Timeout kills AND waits: no zombie, no leaked pipes."""

    class _Hanging(_FakeProcess):
        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.sleep(60)
            raise AssertionError("unreachable")

    spawned: list[_Hanging] = []

    async def _spawn(*args: Any, **kwargs: Any) -> _Hanging:
        proc = _Hanging(None)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    with pytest.raises(DeliveryError, match="timed out"):
        await send_via_hermes(target="telegram", message="hi", timeout_seconds=0.01)
    assert spawned[0].killed is True
    assert spawned[0].returncode == -9  # wait() ran


async def test_send_via_hermes_unknown_returncode_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing returncode raises explicitly (never bare assert)."""

    async def _spawn(*args: Any, **kwargs: Any) -> _FakeProcess:
        return _FakeProcess(None)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    with pytest.raises(DeliveryError, match="returncode unknown"):
        await send_via_hermes(target="telegram", message="hi")


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


# ── HttpxFeishuTransport error mapping ───────────────────────────────────


class _StubClient:
    """Minimal async httpx client stub with a scripted outcome."""

    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.seen: dict[str, Any] = {}

    async def request(self, method: str, url: str, **kwargs: Any) -> Any:
        self.seen = {"method": method, "url": url, **kwargs}
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class _StubResponse:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def json(self) -> Any:
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload


async def test_transport_maps_network_errors_to_retryable() -> None:
    """Timeouts and connect failures are worth another attempt."""
    import httpx

    from dyvine.services.delivery import HttpxFeishuTransport

    transport = HttpxFeishuTransport(
        _StubClient(httpx.TimeoutException("slow"))  # type: ignore[arg-type]
    )
    with pytest.raises(DeliveryError) as exc_info:
        await transport.post_json("https://x", payload={})
    assert exc_info.value.reason == "retryable"


async def test_transport_maps_closed_client_to_failed() -> None:
    """Client misuse is deterministic: fail, don't burn retries."""
    from dyvine.services.delivery import HttpxFeishuTransport

    transport = HttpxFeishuTransport(
        _StubClient(RuntimeError("Cannot open a client instance"))  # type: ignore[arg-type]
    )
    with pytest.raises(DeliveryError) as exc_info:
        await transport.post_json("https://x", payload={})
    assert exc_info.value.reason == "failed"


async def test_transport_lets_cancellation_through() -> None:
    """Cancelled sends abort instead of converting into retryable work."""
    import asyncio

    from dyvine.services.delivery import HttpxFeishuTransport

    transport = HttpxFeishuTransport(
        _StubClient(asyncio.CancelledError())  # type: ignore[arg-type]
    )
    with pytest.raises(asyncio.CancelledError):
        await transport.post_json("https://x", payload={})


async def test_transport_treats_non_dict_json_as_empty() -> None:
    """Non-object bodies degrade to {} so code checks fail terminally."""
    from dyvine.services.delivery import HttpxFeishuTransport

    transport = HttpxFeishuTransport(_StubClient(_StubResponse([1, 2])))  # type: ignore[arg-type]
    assert await transport.post_json("https://x", payload={}) == {}


async def test_post_file_streams_handle_instead_of_reading_all(
    tmp_path: Path,
) -> None:
    """Uploads pass the open handle so memory stays constant."""
    from dyvine.services.delivery import HttpxFeishuTransport

    blob = tmp_path / "clip.mp4"
    blob.write_bytes(b"0123456789")
    seen: dict[str, Any] = {}

    class _CapturingClient(_StubClient):
        async def request(self, method: str, url: str, **kwargs: Any) -> Any:
            # Read inside the call: the transport closes the handle
            # once the send completes.
            name, payload, mime = kwargs["files"]["file"]
            seen["name"] = name
            seen["is_stream"] = not isinstance(payload, (bytes, bytearray))
            seen["content"] = payload.read()
            seen["mime"] = mime
            return self.outcome

    transport = HttpxFeishuTransport(
        _CapturingClient(_StubResponse({"code": 0}))  # type: ignore[arg-type]
    )
    await transport.post_file("https://x", file_name="clip.mp4", file_path=blob)
    assert seen == {
        "name": "clip.mp4",
        "is_stream": True,
        "content": b"0123456789",
        "mime": "application/octet-stream",
    }
