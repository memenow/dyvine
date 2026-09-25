"""Feishu transport and ledger-backed delivery, plus Hermes notifications.

Credentials come from the Hermes environment and never enter Postgres
or logs. Durable group, topic, and file effects live in
``delivery_durable``; this module exposes the transport and channel
interface. ``send_via_hermes`` uses the gateway's other channels.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from ..core.exceptions import DeliveryError
from ..core.logging import ContextLogger
from ..db.protocols import DeliveryLedgerRepository
from ..db.records import DeliveryGroupRecord, FileDeliveryRecord

logger = ContextLogger(__name__)

#: Extensions the sender ever ships.
MEDIA_EXTS = frozenset({".mp4", ".webp", ".jpg", ".jpeg", ".png"})

#: Upload names longer than this are truncated to stem[:40] + ext.
LONG_NAME_CHARS = 50
TRUNCATED_STEM_CHARS = 40

#: Token endpoint + IM base (Feishu open platform).
_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
_IM_MESSAGES_URL = (
    "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
)
_IM_REPLY_URL = "https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply"

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
        file_field: str = "file",
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
        # Cancellation (BaseException) deliberately propagates: a
        # cancelled send must abort, never convert into a retryable
        # error that the weekly loop would re-queue.
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
        except (httpx.HTTPError, ValueError) as error:
            # Transport failures (timeouts, connects) and undecodable
            # bodies (transient gateway HTML): worth another attempt.
            raise DeliveryError(
                f"Feishu {method} {url} failed: {error}", reason="retryable"
            ) from error
        except RuntimeError as error:
            # Client misuse (used after close, shutdown pool): the same
            # call can never succeed, so fail terminally instead of
            # burning the weekly retry budget on a deterministic error.
            raise DeliveryError(
                f"Feishu {method} {url} failed: {error}", reason="failed"
            ) from error
        except Exception as error:
            # Unexpected transport bugs surface terminally (chained, so
            # the traceback survives) rather than masquerading as
            # retryable work.
            raise DeliveryError(
                f"Feishu {method} {url} failed: {error}", reason="failed"
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
        file_field: str = "file",
        timeout: float = 300.0,
    ) -> dict[str, Any]:
        """POST a multipart file upload and return the decoded response.

        Streams the open handle (constant memory) instead of reading
        the whole file: callers gate size upstream, but the transport
        must not turn a concurrently growing file into an OOM.
        """
        with file_path.open("rb") as handle:
            files = {file_field: (file_name, handle, "application/octet-stream")}
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

    async def ensure_group(
        self,
        *,
        ledger: DeliveryLedgerRepository,
        round: str,
        sec_user_id: str,
        nickname: str,
        owner_open_id: str,
        avatar_url: str | None = None,
    ) -> DeliveryGroupRecord:
        """Create or reuse a checkpointed group for one round/account."""
        from .delivery_durable import ensure_group

        return await ensure_group(
            self,
            ledger=ledger,
            round=round,
            sec_user_id=sec_user_id,
            nickname=nickname,
            owner_open_id=owner_open_id,
            avatar_url=avatar_url,
        )

    async def ensure_topic(
        self,
        *,
        ledger: DeliveryLedgerRepository,
        round: str,
        sec_user_id: str,
        chat_id: str,
        nickname: str,
        homepage: str,
    ) -> DeliveryGroupRecord:
        """Create or reuse the checkpointed profile topic."""
        from .delivery_durable import ensure_topic

        return await ensure_topic(
            self,
            ledger=ledger,
            round=round,
            sec_user_id=sec_user_id,
            chat_id=chat_id,
            nickname=nickname,
            homepage=homepage,
        )

    async def deliver_file(
        self,
        *,
        ledger: DeliveryLedgerRepository,
        round: str,
        sec_user_id: str,
        user_dir: Path,
        file_path: Path,
        chat_id: str,
        parent_id: str,
    ) -> FileDeliveryRecord:
        """Deliver a media file with durable pre-send intent."""
        from .delivery_durable import deliver_file

        return await deliver_file(
            self,
            ledger=ledger,
            round=round,
            sec_user_id=sec_user_id,
            user_dir=user_dir,
            file_path=file_path,
            chat_id=chat_id,
            parent_id=parent_id,
        )

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

    async def _send_message(
        self,
        chat_id: str,
        msg_type: str,
        content: dict[str, Any],
        *,
        parent_id: str | None,
        request_uuid: str,
    ) -> tuple[dict[str, Any] | None, Any]:
        """Send one message; refresh the token once on auth errors."""
        import json as _json

        payload: dict[str, Any] = {
            "msg_type": msg_type,
            "content": _json.dumps(content, ensure_ascii=False),
        }
        if parent_id:
            url = _IM_REPLY_URL.format(message_id=parent_id)
            payload["reply_in_thread"] = True
        else:
            url = _IM_MESSAGES_URL
            payload["receive_id"] = chat_id
        payload["uuid"] = request_uuid
        token = await self._auth_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        data = await self._transport.post_json(
            url, headers=headers, payload=payload, timeout=120.0
        )
        if data.get("code") != 0:
            if self._is_token_error(data):
                token = await self._refresh_token()
                headers["Authorization"] = f"Bearer {token}"
                data = await self._transport.post_json(
                    url,
                    headers=headers,
                    payload=payload,
                    timeout=120.0,
                )
            if data.get("code") != 0:
                return None, data
        return data.get("data") or {}, None

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
        ledger: DeliveryLedgerRepository | None = None,
        round: str | None = None,
        sec_user_id: str | None = None,
    ) -> AccountDelivery:
        """Deliver through the ledger, rejecting unverified legacy path lists."""
        from .delivery_durable import send_account_durable

        if ledger is None or not round or not sec_user_id:
            raise DeliveryError(
                "Delivery ledger, round, and sec_user_id are required",
                reason="failed",
            )
        if already_sent or already_failed or known_permanent:
            raise DeliveryError(
                "Legacy path lists require import into the delivery ledger first",
                reason="failed",
            )
        return await send_account_durable(
            self,
            ledger=ledger,
            round=round,
            sec_user_id=sec_user_id,
            nickname=nickname,
            chat_id=chat_id,
            homepage=homepage,
            user_dir=user_dir,
            cutoff=cutoff,
            starter_message_id=starter_message_id,
        )


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
    # Option values use the ``--opt=value`` form and the positional message
    # sits behind ``--``: targets (``-100…`` group ids), subjects, and
    # messages are all external strings, and a leading ``-`` would otherwise
    # be parsed as a hermes flag (option injection / failed sends).
    command = ["hermes", "send", f"--to={target}", "--json"]
    if subject:
        command += [f"--subject={subject}"]
    command += ["--", message]
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
            # Reap the child: kill without wait leaves a zombie and leaks
            # the stdout/stderr pipes. (On Python >= 3.11
            # ``asyncio.TimeoutError`` IS ``TimeoutError``, so this one
            # clause covers ``wait_for`` on this codebase's >= 3.12 floor.)
            process.kill()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=5.0)
            raise DeliveryError(
                f"hermes send timed out after {timeout_seconds}s",
                reason="retryable",
            ) from error
    except FileNotFoundError as error:
        raise DeliveryError("hermes CLI not found on PATH", reason="failed") from error
    if process.returncode is None:
        # Never ``assert``: it compiles out under ``python -O`` and would
        # leave the exit-code branch below unguarded.
        raise DeliveryError("hermes send returncode unknown", reason="retryable")
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
