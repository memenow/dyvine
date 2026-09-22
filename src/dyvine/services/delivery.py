"""Delivery channels: Feishu groups first, hermes-native for the rest.

:class:`FeishuGroupChannel` ports the proven
``send_per_account_polling.py`` rules (August 2026 production) into an
async, transport-injectable service:

- group description (author/homepage/cutoff) set BEFORE any file (SOP)
- media scan limited to ``MEDIA_EXTS``; 0-byte files deleted (Feishu
  error 234010); files over ``MAX_UPLOAD`` skipped (error 234006)
- over-long upload names truncated to ``stem[:40] + ext`` (Feishu 40009)
- tenant token refreshed once on ``99991663`` / invalid-token errors
- topic post message created (or a caller-supplied starter reused) and
  every file sent as a threaded reply with ``RATE_SECONDS`` pacing
- failures mentioning 234006/234010 join the permanent-failure set

Credentials come from the hermes default (``~/.hermes/.env``, the same
file ``run_batch_wrapper.py`` reads), falling back to process
environment. They never touch the repository, Postgres, or logs.

:func:`send_via_hermes` covers every other channel by shelling out to
``hermes send`` (exit 0 ok, 1 delivery error, 2 usage error), so this
package never grows a second channel implementation.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from ..core.exceptions import DeliveryError
from ..core.logging import ContextLogger

logger = ContextLogger(__name__)

#: Feishu upload cap is 30MB; pre-skip at 29MB like the sender script.
MAX_UPLOAD_BYTES = 29 * 1024 * 1024

#: Extensions the sender ever ships.
MEDIA_EXTS = frozenset({".mp4", ".webp", ".jpg", ".jpeg", ".png"})

#: Pacing between file sends (anti-throttle, matches the sender).
RATE_SECONDS = 1.0

#: Upload names longer than this are truncated to stem[:40] + ext.
LONG_NAME_CHARS = 50
TRUNCATED_STEM_CHARS = 40

#: Token endpoint + IM base (Feishu open platform).
_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
_IM_FILES_URL = "https://open.feishu.cn/open-apis/im/v1/files"
_IM_MESSAGES_URL = (
    "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
)

#: Hermes default env file carrying FEISHU_APP_ID / FEISHU_APP_SECRET.
_HERMES_ENV_PATH = Path.home() / ".hermes" / ".env"


class FeishuTransport(Protocol):
    """Minimal async HTTP surface the channel needs (mockable)."""

    async def post_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """POST a JSON body and return the decoded response."""
        ...

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
        """POST a multipart file upload and return the decoded response."""
        ...

    async def put_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """PUT a JSON body and return the decoded response."""
        ...


class HttpxFeishuTransport:
    """Production :class:`FeishuTransport` over ``httpx.AsyncClient``."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        """Wrap an explicit client, or create one per call when omitted."""
        self._client = client

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None,
        timeout: float,
        **kwargs: Any,
    ) -> dict[str, Any]:
        try:
            if self._client is not None:
                response = await self._client.request(
                    method, url, headers=headers, timeout=timeout, **kwargs
                )
            else:
                async with httpx.AsyncClient() as client:
                    response = await client.request(
                        method, url, headers=headers, timeout=timeout, **kwargs
                    )
            data = response.json()
            return dict(data) if isinstance(data, dict) else {}
        except Exception as error:
            raise DeliveryError(
                f"Feishu {method} {url} failed: {error}", reason="retryable"
            ) from error

    async def post_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """POST a JSON body and return the decoded response."""
        return await self._request(
            "POST", url, headers=headers, timeout=timeout, json=payload or {}
        )

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
        """POST a multipart file upload and return the decoded response."""
        with file_path.open("rb") as handle:
            content = handle.read()
        files = {"file": (file_name, content, "application/octet-stream")}
        return await self._request(
            "POST",
            url,
            headers=headers,
            timeout=timeout,
            files=files,
            data=fields or {},
        )

    async def put_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """PUT a JSON body and return the decoded response."""
        return await self._request(
            "PUT", url, headers=headers, timeout=timeout, json=payload or {}
        )


@dataclass(frozen=True, slots=True)
class FeishuCredentials:
    """Feishu app credentials (never logged, never persisted)."""

    app_id: str
    app_secret: str

    @staticmethod
    def from_hermes_default(
        env_path: Path | None = None,
    ) -> FeishuCredentials:
        """Load ``FEISHU_APP_ID/SECRET`` from hermes env, else process env.

        The hermes default file wins when it defines both keys; a
        missing file (or missing keys) falls through to
        ``os.environ`` so tests and non-hermes hosts keep working.
        """
        app_id = os.environ.get("FEISHU_APP_ID", "")
        app_secret = os.environ.get("FEISHU_APP_SECRET", "")
        source = "process environment"
        path = env_path or _HERMES_ENV_PATH
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        file_values: dict[str, str] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            file_values[name.strip()] = value.strip().strip("'\"")
        if file_values.get("FEISHU_APP_ID") and file_values.get("FEISHU_APP_SECRET"):
            app_id = file_values["FEISHU_APP_ID"]
            app_secret = file_values["FEISHU_APP_SECRET"]
            source = str(path)
        if not app_id or not app_secret:
            raise DeliveryError(
                f"Missing FEISHU_APP_ID/SECRET (checked {source})",
                reason="failed",
            )
        return FeishuCredentials(app_id=app_id, app_secret=app_secret)


@dataclass(slots=True)
class AccountDelivery:
    """Per-account delivery outcome (mirrors sender progress counters)."""

    nickname: str
    chat_id: str
    total_files: int = 0
    sent_files: int = 0
    failed_files: int = 0
    skipped_old: int = 0
    zero_deleted: int = 0
    starter_message_id: str | None = None
    failed_paths: list[str] = field(default_factory=list)
    permanent_failures: list[str] = field(default_factory=list)
    note: str | None = None

    @property
    def done(self) -> bool:
        """True when every attempted file sent (failures sink the batch)."""
        return self.failed_files == 0

    @property
    def status(self) -> str:
        """Sender-compatible status label for ``send_status`` rows."""
        if self.note == "op_failed_download":
            return "op_failed"
        if self.note == "no_local_dir":
            return "no_local"
        return "completed" if self.done else "partial"


def scan_media_files(base: Path) -> list[Path]:
    """List media files under ``base`` (extension filter only)."""
    return sorted(
        path
        for path in base.rglob("*")
        if path.is_file() and path.suffix.lower() in MEDIA_EXTS
    )


def post_datetime_from_path(path: Path, user_dir: Path) -> datetime | None:
    """Parse the post datetime from the first path segment under ``user_dir``.

    Download roots nest posts as ``%Y-%m-%d %H-%M-%S...`` directories;
    anything unparseable returns ``None`` so the caller can treat the
    file as too old for incremental cutoffs (sender semantics).
    """
    try:
        relative = path.relative_to(user_dir)
    except ValueError:
        return None
    segment = relative.parts[0][:19] if relative.parts else ""
    try:
        return datetime.strptime(segment, "%Y-%m-%d %H-%M-%S")
    except ValueError:
        return None


def upload_file_name(file_path: Path) -> str:
    """Return the Feishu upload name (truncated, on-disk name untouched)."""
    name = file_path.name
    if len(name) > LONG_NAME_CHARS:
        stem, ext = file_path.stem, file_path.suffix
        return stem[:TRUNCATED_STEM_CHARS] + ext
    return name


class FeishuGroupChannel:
    """Deliver one account's media files to one Feishu group chat."""

    def __init__(
        self,
        credentials: FeishuCredentials,
        transport: FeishuTransport | None = None,
    ) -> None:
        """Bind credentials plus an injectable transport."""
        self._credentials = credentials
        self._transport = transport or HttpxFeishuTransport()
        self._token: str | None = None

    async def _auth_token(self) -> str:
        if self._token is None:
            data = await self._transport.post_json(
                _TOKEN_URL,
                payload={
                    "app_id": self._credentials.app_id,
                    "app_secret": self._credentials.app_secret,
                },
                timeout=30.0,
            )
            if data.get("code") != 0:
                raise DeliveryError(f"Feishu token failed: {data}", reason="failed")
            token = data.get("tenant_access_token") or ""
            if not token:
                raise DeliveryError("Feishu token empty", reason="failed")
            self._token = token
        return self._token

    @staticmethod
    def _is_token_error(payload: Any) -> bool:
        text = str(payload)
        return "99991663" in text or "Invalid access token" in text

    async def _refresh_token(self) -> str:
        self._token = None
        return await self._auth_token()

    async def set_group_description(
        self, *, chat_id: str, nickname: str, homepage: str
    ) -> None:
        """Set the group description (SOP: before any file; warn-only)."""
        description = (
            f"发送的作品作者：{nickname} | 抖音主页：{homepage} | "
            f"发送作品截止时间：{datetime.now():%Y-%m-%d %H:%M}"
        )
        token = await self._auth_token()
        url = f"https://open.feishu.cn/open-apis/im/v1/chats/{chat_id}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        try:
            data = await self._transport.put_json(
                url, headers=headers, payload={"description": description}
            )
            if data.get("code") != 0 and self._is_token_error(data):
                token = await self._refresh_token()
                headers["Authorization"] = f"Bearer {token}"
                data = await self._transport.put_json(
                    url, headers=headers, payload={"description": description}
                )
            if data.get("code") != 0:
                logger.warning(
                    "Feishu description warn",
                    extra={"chat_id": chat_id, "code": data.get("code")},
                )
        except DeliveryError as error:
            logger.warning(
                "Feishu description warn",
                extra={"chat_id": chat_id, "error": str(error)},
            )

    async def create_topic(self, *, chat_id: str, nickname: str, homepage: str) -> str:
        """Post the topic starter message; return its message id."""
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
        data, error = await self._send_message(chat_id, "post", content, parent_id=None)
        if error is not None:
            raise DeliveryError(f"Feishu topic failed: {error}", reason="retryable")
        message_id = (data or {}).get("message_id", "")
        if not message_id or not isinstance(message_id, str):
            raise DeliveryError("Feishu topic returned no id", reason="retryable")
        return message_id

    async def _send_message(
        self,
        chat_id: str,
        msg_type: str,
        content: dict[str, Any],
        *,
        parent_id: str | None,
    ) -> tuple[dict[str, Any] | None, Any]:
        """Send one message; refresh the token once on auth errors."""
        import json as _json

        payload: dict[str, Any] = {
            "receive_id": chat_id,
            "msg_type": msg_type,
            "content": _json.dumps(content, ensure_ascii=False),
        }
        if parent_id:
            payload["parent_id"] = parent_id
        token = await self._auth_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        data = await self._transport.post_json(
            _IM_MESSAGES_URL, headers=headers, payload=payload, timeout=120.0
        )
        if data.get("code") != 0:
            if self._is_token_error(data):
                token = await self._refresh_token()
                headers["Authorization"] = f"Bearer {token}"
                data = await self._transport.post_json(
                    _IM_MESSAGES_URL,
                    headers=headers,
                    payload=payload,
                    timeout=120.0,
                )
            if data.get("code") != 0:
                return None, data
        return data.get("data") or {}, None

    async def send_file(
        self, *, file_path: Path, chat_id: str, parent_id: str | None
    ) -> tuple[bool, Any]:
        """Upload one file and send it as a (threaded) message."""
        token = await self._auth_token()
        headers = {"Authorization": f"Bearer {token}"}
        data = await self._transport.post_file(
            _IM_FILES_URL,
            headers=headers,
            file_name=upload_file_name(file_path),
            file_path=file_path,
            fields={"file_type": "stream", "file_name": upload_file_name(file_path)},
        )
        if data.get("code") != 0:
            if self._is_token_error(data):
                token = await self._refresh_token()
                headers = {"Authorization": f"Bearer {token}"}
                data = await self._transport.post_file(
                    _IM_FILES_URL,
                    headers=headers,
                    file_name=upload_file_name(file_path),
                    file_path=file_path,
                    fields={
                        "file_type": "stream",
                        "file_name": upload_file_name(file_path),
                    },
                )
            if data.get("code") != 0:
                return False, data
        file_key = (data.get("data") or {}).get("file_key") or ""
        if not file_key:
            return False, {"code": -1, "msg": "missing file_key"}
        _, error = await self._send_message(
            chat_id, "file", {"file_key": file_key}, parent_id=parent_id
        )
        if error is not None:
            return False, error
        return True, None

    def plan_files(
        self,
        *,
        user_dir: Path,
        cutoff: datetime | None = None,
        already_sent: set[str] | None = None,
        already_failed: set[str] | None = None,
        known_permanent: set[str] | None = None,
    ) -> tuple[list[Path], list[str], int, int]:
        """Filter scanned media into (to_send, new_permanent, skipped, zero).

        0-byte files are deleted on sight (Feishu 234010); oversize
        files join the permanent set without being touched. Files at
        or before ``cutoff`` (or with unparseable dates) are skipped
        for incremental runs. Returns the send list plus the new
        permanent paths, the skipped-old count, and the zero-deleted
        count.
        """
        sent = already_sent or set()
        failed = already_failed or set()
        permanent = set(known_permanent or set())
        to_send: list[Path] = []
        new_permanent: list[str] = []
        skipped_old = 0
        zero_deleted = 0
        for path in scan_media_files(user_dir):
            text = str(path)
            if text in sent or text in failed or text in permanent:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size == 0:
                try:
                    path.unlink()
                    zero_deleted += 1
                except OSError:
                    pass
                new_permanent.append(text)
                continue
            if size > MAX_UPLOAD_BYTES:
                new_permanent.append(text)
                continue
            if cutoff is not None:
                posted = post_datetime_from_path(path, user_dir)
                if posted is None or posted <= cutoff:
                    skipped_old += 1
                    continue
            to_send.append(path)
        return to_send, new_permanent, skipped_old, zero_deleted

    async def send_account(
        self,
        *,
        nickname: str,
        chat_id: str,
        homepage: str,
        user_dir: Path,
        cutoff: datetime | None = None,
        starter_message_id: str | None = None,
        already_sent: set[str] | None = None,
        already_failed: set[str] | None = None,
        known_permanent: set[str] | None = None,
    ) -> AccountDelivery:
        """Deliver one account's pending files; return the counters.

        Sets the group description first (SOP), then creates (or
        reuses) the topic starter and sends every planned file as a
        threaded reply. Persistence is the caller's job: the result
        carries everything a ``send_status`` upsert needs.
        """
        result = AccountDelivery(nickname=nickname, chat_id=chat_id)
        if not user_dir.is_dir():
            result.note = "no_local_dir"
            return result
        await self.set_group_description(
            chat_id=chat_id, nickname=nickname, homepage=homepage
        )
        to_send, new_permanent, skipped_old, zero_deleted = self.plan_files(
            user_dir=user_dir,
            cutoff=cutoff,
            already_sent=already_sent,
            already_failed=already_failed,
            known_permanent=known_permanent,
        )
        result.total_files = len(to_send)
        result.skipped_old = skipped_old
        result.zero_deleted = zero_deleted
        result.permanent_failures = list(new_permanent)
        starter = starter_message_id
        if starter is None:
            starter = await self.create_topic(
                chat_id=chat_id, nickname=nickname, homepage=homepage
            )
        result.starter_message_id = starter
        for file_path in sorted(to_send):
            try:
                ok, error = await self.send_file(
                    file_path=file_path, chat_id=chat_id, parent_id=starter
                )
            except Exception as exc:  # noqa: BLE001 - one bad file skips on
                ok, error = False, str(exc)
            if ok:
                result.sent_files += 1
            else:
                text = str(error)
                result.failed_files += 1
                result.failed_paths.append(str(file_path))
                if "234006" in text or "234010" in text:
                    result.permanent_failures.append(str(file_path))
            await asyncio.sleep(RATE_SECONDS)
        return result


@dataclass(slots=True)
class HermesSendResult:
    """Outcome of one ``hermes send`` invocation."""

    target: str
    ok: bool
    exit_code: int
    stdout: str = ""
    stderr: str = ""


async def send_via_hermes(
    *,
    target: str,
    message: str,
    subject: str | None = None,
    timeout_seconds: float = 120.0,
) -> HermesSendResult:
    """Deliver ``message`` through the hermes gateway's own send path.

    ``target`` uses ``hermes send`` syntax (``platform``,
    ``platform:chat_id``, ...). Exit 0 means delivered; anything
    else raises :class:`DeliveryError` with the tool's stderr.
    """
    if not target or not target.strip():
        raise DeliveryError("hermes send target is required", reason="failed")
    command = ["hermes", "send", "--to", target, "--json"]
    if subject:
        command += ["--subject", subject]
    command.append(message)
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            raw_out, raw_err = await asyncio.wait_for(
                process.communicate(), timeout=timeout_seconds
            )
        except TimeoutError as error:
            process.kill()
            raise DeliveryError(
                f"hermes send timed out after {timeout_seconds}s",
                reason="retryable",
            ) from error
    except FileNotFoundError as error:
        raise DeliveryError("hermes CLI not found on PATH", reason="failed") from error
    assert process.returncode is not None
    stdout = raw_out.decode("utf-8", "replace")
    stderr = raw_err.decode("utf-8", "replace")
    if process.returncode != 0:
        raise DeliveryError(
            f"hermes send failed (exit {process.returncode}): {stderr or stdout}",
            reason="failed" if process.returncode == 2 else "retryable",
        )
    return HermesSendResult(
        target=target, ok=True, exit_code=0, stdout=stdout, stderr=stderr
    )
