"""Argus webSign request signing for Douyin web APIs.

Douyin's ``/aweme/v1/web`` JSON APIs sit behind two independent
signature layers:

* the *bogus* layer (the ``a_bogus`` query parameter, computed by
  f2's pure-Python ``ABogusManager``), which unlocks response *data*,
  and
* the *Argus webSign* layer (``uifid`` / ``timestamp`` /
  ``x-secsdk-web-signature``), computed inside the SecureSDK virtual
  machine of a real browser session, which unlocks the HTTP *gate*
  itself.

f2 implements only the first layer, so requests dyvine sends through
it are rejected with ``403 Blocked by ArgusSecurityPlugin Uifid Not
Found``. The webSign algorithm is VM bytecode backed by a
per-session key that never leaves browser memory, so dyvine does not
reimplement it: :class:`WebSignProvider` keeps one headless Chromium
page on ``douyin.com`` and evaluates the SDK's own ``webSignUrl``
signer once per request URL. The browser never fetches resources --
all API traffic and downloads stay on plain ``httpx`` -- it only
signs strings.

Install with :func:`install_websign_patch` after the f2 handler
exists (f2 imports lazily and performs network IO on import, so the
patch resolves f2 symbols at install time, never at module scope).
The patch wraps the bogus endpoint builders (every f2 Douyin fetch
builds its URL through ``ABogusManager`` / ``XBogusManager``) plus
the raw fetch helpers (to detect an Argus block, invalidate the
signing session, and retry once with a fresh signature).

Fail-open by design: if the signer is unavailable the endpoint
builders return the unsigned URL exactly as f2 built it, so
endpoints that never needed webSign keep working and gated endpoints
fail the way they did before the patch (HTTP 403) instead of raising
new errors.
"""

from __future__ import annotations

import functools
import inspect
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from importlib import metadata as importlib_metadata
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..core.logging import ContextLogger

if TYPE_CHECKING:
    from playwright.sync_api import Page

logger = ContextLogger(__name__)

# f2 is pinned; the patch reaches into its internals, so a version
# drift must fail fast at install time instead of silently missing.
SUPPORTED_F2_VERSION = "0.0.1.7"
# Verbatim body of the Argus gate rejection (HTTP 403, text/plain).
ARGUS_BLOCK_MARKER = "Blocked by ArgusSecurityPlugin"
# Query parameters appended by the webSign layer, in append order.
WEBSIGN_QUERY_PARAMS = ("uifid", "timestamp", "x-secsdk-web-signature")

# Backoff after repeated signing failures: without it, a broken signer
# would burn a full init budget on every request before failing open.
_FAILURE_THRESHOLD = 3
_FAILURE_COOLDOWN_SECONDS = 300.0

# Chromium flags proven against the live SecureSDK: full engine in
# new-headless mode (the headless shell lacks features the SDK
# probes), no sandbox (required when running as root, as in minimal
# containers), and the automation flag removed so the SDK initialises
# its signer.
CHROMIUM_ARGS = (
    "--headless=new",
    "--no-sandbox",
    "--disable-blink-features=AutomationControlled",
)

# Predicate polled until the SDK registers its URL signer.
READY_SNIPPET = (
    '() => { try { return typeof window.use === "function" '
    '&& typeof window.use("webSignUrl") === "function"; } '
    "catch (e) { return false; } }"
)

# Signing payload. Takes ``{url, timeoutMs}`` and returns
# ``{url, headers}``. The in-page race bounds every call even if the
# VM signer wedges, so the provider thread can never block forever
# inside ``evaluate``.
SIGN_SNIPPET = """async (args) => {
  var limit = (args && args.timeoutMs) || 25000;
  var target = args && args.url;
  var signer = window.use("webSignUrl");
  var signed = await Promise.race([
    signer(target),
    new Promise(function (_, reject) {
      setTimeout(function () { reject(new Error("websign snippet timeout")); }, limit);
    }),
  ]);
  return { url: signed.url, headers: signed.headers || {} };
}"""


class WebSignError(Exception):
    """The signing session is unavailable or rejected the request."""


@dataclass(frozen=True)
class SignedResult:
    """A URL with the webSign triple appended plus its header echo."""

    url: str
    headers: dict[str, str]


@dataclass(frozen=True)
class _SignOp:
    url: str
    reply: queue.Queue[Any]  # maxsize=1; receives SignedResult or WebSignError


_STOP = object()
_REINIT = object()

# (owner, attribute, original) for every installed wrapper so tests
# and shutdown can restore f2 untouched. Install order is endpoints
# first, fetch helpers second; uninstall runs reversed.
_PATCHED: list[tuple[Any, str, Any]] = []


def strip_websign_params(url: str) -> str:
    """Remove a previous webSign triple so the URL can be re-signed."""
    parts = urlsplit(url)
    kept = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key not in WEBSIGN_QUERY_PARAMS
    ]
    return urlunsplit(parts._replace(query=urlencode(kept)))


def is_argus_block(status_code: Any, body: Any) -> bool:
    """Whether a response is the Argus gate rejection (403 + marker)."""
    if status_code != 403 or body is None:
        return False
    if isinstance(body, bytes):
        text = body.decode("utf-8", "replace")
    else:
        text = str(body)
    return ARGUS_BLOCK_MARKER in text


def is_argus_block_response(response: Any) -> bool:
    """Best-effort Argus-block probe over an httpx-style response."""
    if response is None:
        return False
    status = getattr(response, "status_code", None)
    body: Any = None
    for attr in ("text", "content"):
        try:
            body = getattr(response, attr, None)
        except Exception:
            body = None
        if body is not None:
            break
    if callable(body):
        try:
            body = body()
        except Exception:
            body = None
    return is_argus_block(status, body)


def _redact_url(url: str) -> str:
    """Host plus path only; query values carry session secrets."""
    try:
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}{parts.path}"
    except Exception:
        return "unparseable-url"


def _reply(reply: queue.Queue[Any], payload: Any) -> None:
    """Deliver a sign result unless the caller already timed out."""
    try:
        reply.put_nowait(payload)
    except queue.Full:
        pass


def _sign_with_page(page: Page, url: str, snippet_timeout_ms: int) -> SignedResult:
    """Evaluate the SDK signer once and validate its shape."""
    try:
        result = page.evaluate(
            SIGN_SNIPPET, {"url": url, "timeoutMs": snippet_timeout_ms}
        )
    except Exception as exc:
        raise WebSignError(f"websign evaluate failed: {exc}") from exc
    if not isinstance(result, dict) or not isinstance(result.get("url"), str):
        raise WebSignError("websign signer returned an unexpected shape")
    headers = result.get("headers") or {}
    if not isinstance(headers, dict):
        raise WebSignError("websign signer returned unexpected headers")
    return SignedResult(
        url=result["url"], headers={str(k): str(v) for k, v in headers.items()}
    )


def _open_signing_page(
    *,
    page_url: str,
    user_agent: str,
    init_timeout_seconds: float,
    snippet_timeout_ms: int,
) -> tuple[Page, Callable[[], None]]:
    """Launch Chromium and settle a page whose SDK exposes webSignUrl.

    Playwright imports here, not at module scope, so merely importing
    dyvine never requires the browser stack; only signing does.
    """
    del snippet_timeout_ms  # Consumed per sign, not at open time.
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise WebSignError("playwright is not installed") from exc
    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.launch(headless=False, args=[*CHROMIUM_ARGS])
        context = browser.new_context(
            user_agent=user_agent,
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
        )
        context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',"
            "{get:function(){return undefined;}});"
        )
        page = context.new_page()
        budget_ms = int(init_timeout_seconds * 1000)
        page.goto(page_url, wait_until="domcontentloaded", timeout=budget_ms)
        try:
            page.wait_for_load_state("networkidle", timeout=25000)
        except Exception:
            pass
        # Let the SPA settle past its client-side redirect before the
        # predicate starts polling; polling across a navigation can
        # otherwise observe a torn-down execution context.
        page.wait_for_timeout(8000)
        page.wait_for_function(READY_SNIPPET, timeout=budget_ms)
    except Exception as exc:
        try:
            playwright.stop()
        except Exception:
            pass
        raise WebSignError(f"websign session init failed: {exc}") from exc

    def _close() -> None:
        for step in (context.close, browser.close, playwright.stop):
            try:
                step()
            except Exception:
                pass

    return page, _close


class WebSignProvider:
    """Thread-bound headless signer with lazy init and self-healing.

    Playwright's sync API must live on one thread, so the provider
    owns a daemon thread that serialises every operation through a
    queue: at most one browser, one page, one in-flight sign. The
    browser starts lazily on the first :meth:`sign` so an idle
    process pays nothing; any operation error tears the page down and
    the next sign re-initialises from scratch. :meth:`invalidate`
    forces that rotation (used after an observed Argus block).
    After repeated failures the provider fails fast until a cooldown
    passes, so a broken signer adds no per-request latency.
    """

    def __init__(
        self,
        *,
        page_url: str,
        user_agent: str,
        init_timeout_seconds: float = 120.0,
        sign_timeout_seconds: float = 30.0,
        browser_factory: Callable[..., tuple[Page, Callable[[], None]]] | None = None,
    ) -> None:
        self._page_url = page_url
        self._user_agent = user_agent
        self._init_timeout = float(init_timeout_seconds)
        self._sign_timeout = float(sign_timeout_seconds)
        self._browser_factory = browser_factory or _open_signing_page
        self._ops: queue.Queue[Any] = queue.Queue()
        self._state = threading.Lock()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._closed = False
        self._consecutive_failures = 0
        self._last_failure_ts = 0.0

    @property
    def _snippet_timeout_ms(self) -> int:
        # In-page race stays comfortably under the caller-side timeout
        # so a wedged VM surfaces as a clean WebSignError, never a hang.
        return max(5000, int((self._sign_timeout - 5.0) * 1000))

    def sign(self, url: str) -> SignedResult:
        """Sign one request URL, initialising the session on first use."""
        with self._state:
            if self._closed:
                raise WebSignError("websign provider is closed")
            if (
                self._consecutive_failures >= _FAILURE_THRESHOLD
                and time.monotonic() - self._last_failure_ts < _FAILURE_COOLDOWN_SECONDS
            ):
                raise WebSignError("websign backing off after repeated failures")
            if self._thread is None or not self._thread.is_alive():
                if self._thread is not None:
                    self._thread.join(timeout=5)
                self._ready.clear()
                self._thread = threading.Thread(
                    target=self._run, name="dyvine-websign", daemon=True
                )
                self._thread.start()
        reply: queue.Queue[Any] = queue.Queue(maxsize=1)
        self._ops.put(_SignOp(url=url, reply=reply))
        timeout = self._init_timeout if not self._ready.is_set() else self._sign_timeout
        try:
            result = reply.get(timeout=timeout)
        except queue.Empty:
            raise WebSignError(f"websign sign timed out after {timeout:.0f}s") from None
        if isinstance(result, SignedResult):
            return result
        if isinstance(result, Exception):
            raise result
        raise WebSignError(f"websign signer returned {type(result).__name__}")

    def invalidate(self) -> None:
        """Drop the signing session; the next sign builds a fresh one."""
        with self._state:
            alive = (
                not self._closed
                and self._thread is not None
                and self._thread.is_alive()
            )
        self._ready.clear()
        if alive:
            self._ops.put(_REINIT)

    def close(self) -> None:
        """Stop the signer thread and release the browser. Idempotent."""
        with self._state:
            if self._closed:
                return
            self._closed = True
            thread = self._thread
            self._thread = None
        if thread is not None and thread.is_alive():
            if threading.get_ident() == thread.ident:
                return
            self._ops.put(_STOP)
            thread.join(timeout=30)

    def __enter__(self) -> WebSignProvider:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def _record_failure(self) -> None:
        with self._state:
            self._consecutive_failures += 1
            self._last_failure_ts = time.monotonic()

    def _run(self) -> None:
        page: Page | None = None
        closer: Callable[[], None] | None = None
        try:
            while True:
                op = self._ops.get()
                if op is _STOP:
                    return
                if op is _REINIT:
                    closer = self._close_page(page, closer)
                    page = None
                    continue
                assert isinstance(op, _SignOp)
                try:
                    if page is None:
                        page, closer = self._browser_factory(
                            page_url=self._page_url,
                            user_agent=self._user_agent,
                            init_timeout_seconds=self._init_timeout,
                            snippet_timeout_ms=self._snippet_timeout_ms,
                        )
                        self._ready.set()
                    result = _sign_with_page(page, op.url, self._snippet_timeout_ms)
                except WebSignError as exc:
                    closer = self._close_page(page, closer)
                    page = None
                    self._ready.clear()
                    self._record_failure()
                    _reply(op.reply, exc)
                except Exception as exc:
                    closer = self._close_page(page, closer)
                    page = None
                    self._ready.clear()
                    self._record_failure()
                    _reply(op.reply, WebSignError(str(exc)))
                else:
                    with self._state:
                        self._consecutive_failures = 0
                    _reply(op.reply, result)
        finally:
            self._close_page(page, closer)
            self._ready.clear()

    @staticmethod
    def _close_page(page: Page | None, closer: Callable[[], None] | None) -> None:
        del page  # Owned by the closer closure.
        if closer is not None:
            try:
                closer()
            except Exception:
                logger.debug("websign page close failed")
        return None


def _wrap_endpoint_method(provider: WebSignProvider, manager: type, name: str) -> None:
    """Wrap one bogus URL builder so its output carries webSign triple."""
    raw = manager.__dict__.get(name)
    if raw is None:
        return
    if not isinstance(raw, classmethod):
        logger.warning(
            "websign patch skipped unexpected builder shape",
            extra={"manager": manager.__name__, "method": name},
        )
        return
    if getattr(raw.__func__, "__dyvine_websign_wrapped__", False):
        return
    bound_original = getattr(manager, name)

    @functools.wraps(raw.__func__)
    def wrapper(cls: type, *args: Any, **kwargs: Any) -> Any:
        del cls  # Bound through the captured original instead.
        endpoint = bound_original(*args, **kwargs)
        if not isinstance(endpoint, str) or "douyin.com" not in endpoint:
            return endpoint
        try:
            return provider.sign(endpoint).url
        except Exception:
            logger.exception(
                "websign sign failed; using unsigned endpoint",
                extra={"url": _redact_url(endpoint)},
            )
            return endpoint

    wrapper.__dyvine_websign_wrapped__ = True  # type: ignore[attr-defined]
    setattr(manager, name, classmethod(wrapper))
    _PATCHED.append((manager, name, raw))


def _exception_status(exc: BaseException) -> int | None:
    """Best-effort HTTP status over f2 and httpx exception shapes."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def _fresh_signed_url(provider: WebSignProvider, url: str) -> str | None:
    """Invalidate the session and re-sign a stripped URL (None on failure)."""
    provider.invalidate()
    try:
        return provider.sign(strip_websign_params(url)).url
    except Exception:
        logger.exception("websign re-sign failed")
        return None


def _wrap_fetch_method(provider: WebSignProvider, crawler_cls: type, name: str) -> None:
    """Wrap one raw fetch helper with re-sign-and-retry on Argus block."""
    raw = crawler_cls.__dict__.get(name)
    if raw is None:
        return
    if getattr(raw, "__dyvine_websign_wrapped__", False):
        return
    if not inspect.iscoroutinefunction(raw):
        logger.warning(
            "websign patch skipped unexpected fetch shape",
            extra={"method": name},
        )
        return

    @functools.wraps(raw)
    async def wrapper(self: Any, url: str, *args: Any, **kwargs: Any) -> Any:
        # f2 raises (APIError with .status_code) on HTTP errors instead
        # of returning the response, so both the exception and the
        # response path need the Argus-block check.
        try:
            response = await raw(self, url, *args, **kwargs)
        except Exception as exc:
            if _exception_status(exc) != 403:
                raise
            logger.warning(
                "argus block (exception path); re-signing and retrying once",
                extra={"url": _redact_url(url)},
            )
            resigned = _fresh_signed_url(provider, url)
            if resigned is None:
                raise
            return await raw(self, resigned, *args, **kwargs)
        if not is_argus_block_response(response):
            return response
        logger.warning(
            "argus block; invalidating websign session and retrying once",
            extra={"url": _redact_url(url)},
        )
        resigned = _fresh_signed_url(provider, url)
        if resigned is None:
            return response
        return await raw(self, resigned, *args, **kwargs)

    wrapper.__dyvine_websign_wrapped__ = True  # type: ignore[attr-defined]
    setattr(crawler_cls, name, wrapper)
    _PATCHED.append((crawler_cls, name, raw))


def install_websign_patch(provider: WebSignProvider, managers: Any = None) -> None:
    """Wrap f2's bogus URL builders so every endpoint is webSigned.

    Args:
        provider: Live signing session used for each built URL.
        managers: Override for tests (stub classes with the same
            builder shape). Resolves the real f2 managers otherwise.

    Raises:
        WebSignError: If the installed f2 version is not the one the
            patch was validated against.
    """
    try:
        installed = importlib_metadata.version("f2")
    except importlib_metadata.PackageNotFoundError:
        installed = None
    if installed != SUPPORTED_F2_VERSION:
        raise WebSignError(
            f"websign patch requires f2=={SUPPORTED_F2_VERSION}, " f"found {installed}"
        )
    if managers is None:
        from f2.apps.douyin.utils import (  # type: ignore
            ABogusManager,
            XBogusManager,
        )

        managers = (ABogusManager, XBogusManager)
    for manager in managers:
        for name in ("model_2_endpoint", "str_2_endpoint"):
            _wrap_endpoint_method(provider, manager, name)


def install_fetch_retry(provider: WebSignProvider, crawler_cls: Any = None) -> None:
    """Wrap f2's raw fetch helpers with Argus-block re-sign retry.

    Args:
        provider: Live signing session, invalidated before the retry.
        crawler_cls: Override for tests (stub class with async
            ``get_fetch_data`` / ``post_fetch_data``). Resolves the
            real f2 base crawler otherwise.
    """
    if crawler_cls is None:
        from f2.crawlers.base_crawler import BaseCrawler  # type: ignore

        crawler_cls = BaseCrawler
    for name in ("get_fetch_data", "post_fetch_data"):
        _wrap_fetch_method(provider, crawler_cls, name)


def uninstall_websign_patch() -> None:
    """Restore every wrapped f2 attribute. Idempotent."""
    while _PATCHED:
        owner, name, raw = _PATCHED.pop()
        try:
            setattr(owner, name, raw)
        except Exception:
            logger.debug("websign uninstall passed a dead owner")


def is_websign_patched() -> bool:
    """Whether any f2 wrapper is currently installed (tests/debug)."""
    return bool(_PATCHED)
