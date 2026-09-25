"""Tests for the Argus webSign signing layer (stubbed browser)."""

from __future__ import annotations

import queue
import sys
import threading
import types
from typing import Any
from unittest.mock import MagicMock

import pytest

from dyvine.services import websign as websign_mod
from dyvine.services.websign import (
    SignedResult,
    WebSignError,
    WebSignProvider,
    install_fetch_retry,
    install_websign_patch,
    is_argus_block,
    is_argus_block_response,
    is_websign_patched,
    strip_websign_params,
    uninstall_websign_patch,
)

SIGNED_URL = (
    "https://www.douyin.com/aweme/v1/web/aweme/post/?a=1"
    "&uifid=U&timestamp=1&x-secsdk-web-signature=S"
)


@pytest.fixture(autouse=True)
def _clean_patch_state():
    uninstall_websign_patch()
    yield
    uninstall_websign_patch()


# ── pure helpers ─────────────────────────────────────────────────────


def test_strip_websign_params_removes_triple() -> None:
    """Verify strip removes the triple but preserves other params."""
    assert (
        strip_websign_params(SIGNED_URL)
        == "https://www.douyin.com/aweme/v1/web/aweme/post/?a=1"
    )


def test_strip_websign_params_idempotent_and_passthrough() -> None:
    """Verify strip is idempotent and leaves clean URLs alone."""
    clean = "https://www.douyin.com/x/?a=1&b=2"
    assert strip_websign_params(clean) == clean
    assert strip_websign_params(strip_websign_params(SIGNED_URL)) == (
        "https://www.douyin.com/aweme/v1/web/aweme/post/?a=1"
    )
    assert strip_websign_params("https://www.douyin.com/x/") == (
        "https://www.douyin.com/x/"
    )


def test_is_argus_block_matrix() -> None:
    """Verify Argus-block detection across statuses and bodies."""
    assert is_argus_block(403, "Blocked by ArgusSecurityPlugin Uifid Not Found")
    assert is_argus_block(403, b"Blocked by ArgusSecurityPlugin X")
    assert not is_argus_block(403, "Forbidden")
    assert not is_argus_block(403, None)
    assert not is_argus_block(200, "Blocked by ArgusSecurityPlugin")
    assert not is_argus_block(None, "Blocked by ArgusSecurityPlugin")


def test_is_argus_block_response_probes() -> None:
    """Verify response probing tolerates stubs and odd shapes."""
    assert not is_argus_block_response(None)

    class _Resp:
        def __init__(self, status_code: Any, text: Any) -> None:
            self.status_code = status_code
            self._text = text

        @property
        def text(self) -> Any:
            if isinstance(self._text, Exception):
                raise self._text
            return self._text

    assert is_argus_block_response(
        _Resp(403, "Blocked by ArgusSecurityPlugin Uifid Not Found")
    )
    assert not is_argus_block_response(_Resp(200, "ok"))

    class _BinaryResp:
        status_code = 403
        content = b"Blocked by ArgusSecurityPlugin"

    assert is_argus_block_response(_BinaryResp())
    assert not is_argus_block_response(_Resp(403, RuntimeError("decode boom")))


# ── provider with a fake page ────────────────────────────────────────


class _FakePage:
    """Minimal ``evaluate`` protocol over a scripted sign function."""

    def __init__(self, script: Any) -> None:
        self.script = script
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def evaluate(self, snippet: str, arg: dict[str, Any]) -> Any:
        self.calls.append((snippet, arg))
        return self.script(arg["url"])


def _factory_for(script: Any, *, on_open: Any = None) -> Any:
    """Build an instrumented browser factory returning ``_FakePage``."""
    state = {"opens": 0, "closes": 0}

    def factory(**kwargs: Any) -> Any:
        state["opens"] += 1
        if on_open is not None:
            on_open()
        page = _FakePage(script)

        def closer() -> None:
            state["closes"] += 1

        return page, closer

    factory.state = state  # type: ignore[attr-defined]
    return factory


def _provider(factory: Any, **overrides: Any) -> WebSignProvider:
    kwargs: dict[str, Any] = {
        "page_url": "https://www.douyin.com/",
        "user_agent": "fake-agent",
        "init_timeout_seconds": 5.0,
        "sign_timeout_seconds": 5.0,
        "browser_factory": factory,
    }
    kwargs.update(overrides)
    return WebSignProvider(**kwargs)


def test_provider_signs_lazily() -> None:
    """Verify the browser starts on first sign, not construction."""
    factory = _factory_for(
        lambda url: {"url": url + "&uifid=U", "headers": {"uifid": "U"}}
    )
    provider = _provider(factory)
    assert factory.state["opens"] == 0
    with provider:
        result = provider.sign("https://www.douyin.com/x/?a=1")
    assert factory.state["opens"] == 1
    assert factory.state["closes"] == 1
    assert result.url.endswith("&uifid=U")
    assert result.headers == {"uifid": "U"}


def test_provider_passes_snippet_timeout() -> None:
    """Verify the in-page race stays under the sign budget."""
    pages: list[_FakePage] = []

    def factory(**kwargs: Any) -> Any:
        page = _FakePage(lambda url: {"url": url, "headers": {}})
        pages.append(page)
        return page, lambda: None

    with _provider(factory, sign_timeout_seconds=30.0) as provider:
        provider.sign("https://www.douyin.com/x/")
    assert pages[0].calls[0][1]["timeoutMs"] == 25000


def test_provider_bad_shapes_raise_and_reinit() -> None:
    """Verify malformed signer output tears the page down."""
    factory = _factory_for(lambda url: {"nope": True})
    provider = _provider(factory)
    with provider:
        with pytest.raises(WebSignError):
            provider.sign("https://www.douyin.com/x/")
        assert factory.state == {"opens": 1, "closes": 1}
        factory2_script = lambda url: {"url": url}  # noqa: E731
        provider._browser_factory = _factory_for(factory2_script)
        result = provider.sign("https://www.douyin.com/x/")
        assert result.headers == {}


def test_provider_evaluate_error_reinits() -> None:
    """Verify an evaluate failure surfaces and drops the session."""

    def boom(url: str) -> Any:
        raise RuntimeError("context destroyed")

    factory = _factory_for(boom)
    provider = _provider(factory)
    with provider:
        with pytest.raises(WebSignError, match="context destroyed"):
            provider.sign("https://www.douyin.com/x/")
        assert factory.state == {"opens": 1, "closes": 1}


def test_provider_invalidate_rotates_session() -> None:
    """Verify invalidate closes the page so next sign re-opens."""
    factory = _factory_for(lambda url: {"url": url, "headers": {}})
    provider = _provider(factory)
    with provider:
        provider.sign("https://www.douyin.com/x/")
        provider.invalidate()
        provider.sign("https://www.douyin.com/x/?b=2")
        assert factory.state["opens"] == 2
        assert factory.state["closes"] == 1


def test_provider_close_idempotent_and_stops_thread() -> None:
    """Verify close is idempotent and reaps the signer thread."""
    factory = _factory_for(lambda url: {"url": url, "headers": {}})
    provider = _provider(factory)
    provider.sign("https://www.douyin.com/x/")
    thread = provider._thread
    assert thread is not None and thread.is_alive()
    provider.close()
    provider.close()
    assert not thread.is_alive()
    with pytest.raises(WebSignError, match="closed"):
        provider.sign("https://www.douyin.com/x/")
    provider.invalidate()  # No-op once closed.


def test_sign_uses_init_budget_despite_stale_ready() -> None:
    """A stale ready flag never truncates the wait to the sign budget."""
    import time

    factory = _factory_for(
        lambda url: {"url": url, "headers": {}},
        on_open=lambda: time.sleep(0.3),
    )
    provider = _provider(factory, init_timeout_seconds=5.0, sign_timeout_seconds=0.05)
    # Simulate the stale-flag race: set, but no session exists yet, so this
    # sign pays a 0.3s cold open against a 0.05s sign budget.
    provider._ready.set()
    with provider:
        result = provider.sign("https://www.douyin.com/x/")
    assert result.url.endswith("/x/")


def test_open_fits_inside_init_budget(
    fake_playwright: _RecordingPlaywright,
) -> None:
    """The worst-case open never outlasts the caller's init wait."""
    page, closer = websign_mod._open_signing_page(
        page_url="https://www.douyin.com/",
        user_agent="fake-agent",
        init_timeout_seconds=60.0,
        snippet_timeout_ms=25000,
    )
    closer()
    budgets = page.budgets
    assert set(budgets) == {"goto", "idle", "settle", "predicate"}
    assert sum(budgets.values()) <= 60_000


def test_provider_timeout_abandons_without_wedging() -> None:
    """Verify a timed-out sign never wedges later signs."""
    gate = threading.Event()
    factory = _factory_for(lambda url: {"url": url, "headers": {}}, on_open=gate.wait)
    provider = _provider(factory, init_timeout_seconds=0.05, sign_timeout_seconds=5.0)
    with provider:
        with pytest.raises(WebSignError, match="timed out"):
            provider.sign("https://www.douyin.com/x/")
        gate.set()
        assert provider._ready.wait(timeout=5)
        result = provider.sign("https://www.douyin.com/x/?b=2")
        assert result.url.endswith("?b=2")


# ── endpoint patch over stub managers ────────────────────────────────


def _make_manager(
    model_url: str = "https://www.douyin.com/e/?a_bogus=X",
    str_url: str = "https://www.douyin.com/t/?a_bogus=Y",
) -> type:
    class _StubManager:
        @classmethod
        def model_2_endpoint(
            cls, user_agent: str, base_endpoint: str, params: dict
        ) -> str:
            return model_url

        @classmethod
        def str_2_endpoint(
            cls, user_agent: str, params: str, request_type: str = ""
        ) -> str:
            return str_url

    return _StubManager


def _stub_provider(url: str = SIGNED_URL) -> MagicMock:
    provider = MagicMock()
    provider.sign.return_value = SignedResult(url=url, headers={})
    return provider


def test_endpoint_patch_signs_builders() -> None:
    """Verify the patch signs every wrapped builder output."""
    manager = _make_manager()
    provider = _stub_provider()
    install_websign_patch(provider, managers=(manager,))
    assert is_websign_patched()
    try:
        assert (
            manager.model_2_endpoint("ua", "https://www.douyin.com/e/", {})
            == SIGNED_URL
        )
        assert manager.str_2_endpoint("ua", "a=1") == SIGNED_URL
        assert provider.sign.call_count == 2
    finally:
        uninstall_websign_patch()
    assert not is_websign_patched()
    assert manager.model_2_endpoint("ua", "https://www.douyin.com/e/", {}).endswith(
        "?a_bogus=X"
    )


def test_endpoint_patch_idempotent() -> None:
    """Verify double install wraps once."""
    manager = _make_manager()
    provider = _stub_provider()
    install_websign_patch(provider, managers=(manager,))
    install_websign_patch(provider, managers=(manager,))
    try:
        manager.model_2_endpoint("ua", "https://www.douyin.com/e/", {})
        assert provider.sign.call_count == 1
    finally:
        uninstall_websign_patch()


def test_endpoint_patch_skips_foreign_urls_and_shapes() -> None:
    """Verify non-douyin and non-string outputs pass through."""

    class _Foreign:
        @classmethod
        def model_2_endpoint(cls, *args: Any, **kwargs: Any) -> str:
            return "https://example.com/?x=1"

        @classmethod
        def str_2_endpoint(cls, *args: Any, **kwargs: Any) -> Any:
            return None

    provider = _stub_provider()
    install_websign_patch(provider, managers=(_Foreign,))
    try:
        assert _Foreign.model_2_endpoint() == "https://example.com/?x=1"
        assert _Foreign.str_2_endpoint() is None
        provider.sign.assert_not_called()
    finally:
        uninstall_websign_patch()


def test_endpoint_patch_fail_open_on_sign_error() -> None:
    """Verify a signer outage returns the unsigned endpoint."""
    manager = _make_manager()
    provider = MagicMock()
    provider.sign.side_effect = WebSignError("browser down")
    install_websign_patch(provider, managers=(manager,))
    try:
        assert manager.model_2_endpoint("ua", "https://www.douyin.com/e/", {}).endswith(
            "?a_bogus=X"
        )
    finally:
        uninstall_websign_patch()


def test_endpoint_patch_skips_missing_and_odd_shapes() -> None:
    """Verify managers without classmethod builders are left alone."""

    class _Odd:
        model_2_endpoint = staticmethod(lambda *a, **k: "https://x/")

    provider = _stub_provider()
    install_websign_patch(provider, managers=(_Odd,))
    try:
        assert _Odd.model_2_endpoint() == "https://x/"
        provider.sign.assert_not_called()
    finally:
        uninstall_websign_patch()
    assert not is_websign_patched()


def test_install_rejects_f2_version_drift(monkeypatch: Any) -> None:
    """Verify the patch fails fast when f2 drifts."""
    monkeypatch.setattr(
        websign_mod.importlib_metadata,
        "version",
        lambda name: "9.9.9",
    )
    with pytest.raises(WebSignError, match="requires f2=="):
        install_websign_patch(_stub_provider(), managers=(_make_manager(),))
    assert not is_websign_patched()


def test_install_missing_f2_package(monkeypatch: Any) -> None:
    """Verify the patch fails fast when f2 is not installed."""
    from importlib.metadata import PackageNotFoundError

    def _missing(name: str) -> str:
        raise PackageNotFoundError(name)

    monkeypatch.setattr(websign_mod.importlib_metadata, "version", _missing)
    with pytest.raises(WebSignError, match="found None"):
        install_websign_patch(_stub_provider(), managers=(_make_manager(),))


# ── fetch retry over a stub crawler ──────────────────────────────────


class _StubResponse:
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text


def _make_crawler(script: Any) -> type:
    class _StubCrawler:
        def __init__(self) -> None:
            self.script = script
            self.calls: list[str] = []

        async def get_fetch_data(self, url: str) -> Any:
            self.calls.append(url)
            return self.script(url)

        async def post_fetch_data(self, url: str, params: dict | None = None) -> Any:
            self.calls.append(url)
            return self.script(url)

    return _StubCrawler


@pytest.mark.asyncio
async def test_fetch_retry_resigns_on_argus_block() -> None:
    """Verify a 403 Argus block triggers invalidate, re-sign, retry."""
    calls: list[str] = []

    def script(url: str) -> _StubResponse:
        calls.append(url)
        if len(calls) == 1:
            return _StubResponse(403, "Blocked by ArgusSecurityPlugin Uifid Not Found")
        return _StubResponse(200, '{"status_code":0}')

    crawler_cls = _make_crawler(script)
    provider = _stub_provider(url=SIGNED_URL + "&fresh=1")
    install_fetch_retry(provider, crawler_cls=crawler_cls)
    try:
        crawler = crawler_cls()
        response = await crawler.get_fetch_data(
            "https://www.douyin.com/e/?a=1&uifid=OLD&timestamp=1"
            "&x-secsdk-web-signature=OLD"
        )
        assert response.status_code == 200
        provider.invalidate.assert_called_once()
        # The re-sign input strips the stale triple first.
        signed_input = provider.sign.call_args[0][0]
        assert signed_input == "https://www.douyin.com/e/?a=1"
        assert crawler.calls[1] == SIGNED_URL + "&fresh=1"
    finally:
        uninstall_websign_patch()


@pytest.mark.asyncio
async def test_fetch_retry_passes_clean_responses() -> None:
    """Verify non-block responses never trigger a retry."""
    crawler_cls = _make_crawler(lambda url: _StubResponse(200, "{}"))
    provider = _stub_provider()
    install_fetch_retry(provider, crawler_cls=crawler_cls)
    try:
        crawler = crawler_cls()
        response = await crawler.post_fetch_data("https://www.douyin.com/e/")
        assert response.status_code == 200
        provider.invalidate.assert_not_called()
        provider.sign.assert_not_called()
    finally:
        uninstall_websign_patch()


@pytest.mark.asyncio
async def test_fetch_retry_ignores_plain_403() -> None:
    """Verify a 403 without the marker is not retried."""
    crawler_cls = _make_crawler(lambda url: _StubResponse(403, "Forbidden"))
    provider = _stub_provider()
    install_fetch_retry(provider, crawler_cls=crawler_cls)
    try:
        crawler = crawler_cls()
        response = await crawler.get_fetch_data("https://www.douyin.com/e/")
        assert response.status_code == 403
        provider.invalidate.assert_not_called()
    finally:
        uninstall_websign_patch()


@pytest.mark.asyncio
async def test_fetch_retry_fail_open_on_resign_error() -> None:
    """Verify a failed re-sign returns the original response."""
    crawler_cls = _make_crawler(
        lambda url: _StubResponse(403, "Blocked by ArgusSecurityPlugin")
    )
    provider = MagicMock()
    provider.sign.side_effect = WebSignError("browser down")
    install_fetch_retry(provider, crawler_cls=crawler_cls)
    try:
        crawler = crawler_cls()
        response = await crawler.get_fetch_data("https://www.douyin.com/e/")
        assert response.status_code == 403
    finally:
        uninstall_websign_patch()


def test_fetch_retry_skips_odd_shapes() -> None:
    """Verify non-coroutine fetch helpers are left alone."""

    class _SyncCrawler:
        def get_fetch_data(self, url: str) -> str:  # type: ignore[empty-body]
            return "sync"

    provider = _stub_provider()
    install_fetch_retry(provider, crawler_cls=_SyncCrawler)
    try:
        assert _SyncCrawler().get_fetch_data("https://x/") == "sync"
        provider.invalidate.assert_not_called()
    finally:
        uninstall_websign_patch()


# ── fake playwright module for the real open path ────────────────────


class _RecordingPage:
    def __init__(self, fail_goto: bool = False) -> None:
        self.fail_goto = fail_goto
        self.fail_idle = False
        self.gotos: list[str] = []
        self.evaluated: list[tuple[str, dict[str, Any]]] = []
        self.budgets: dict[str, int] = {}

    def goto(self, url: str, **kwargs: Any) -> None:
        self.gotos.append(url)
        self.budgets["goto"] = int(kwargs.get("timeout", 0))
        if self.fail_goto:
            raise RuntimeError("navigation failed")

    def wait_for_load_state(self, *args: Any, **kwargs: Any) -> None:
        self.budgets["idle"] = int(kwargs.get("timeout", 0))
        if self.fail_idle:
            raise RuntimeError("idle timeout")
        return None

    def wait_for_timeout(self, ms: int) -> None:
        self.budgets["settle"] = ms
        return None

    def wait_for_function(self, snippet: str, **kwargs: Any) -> None:
        self.budgets["predicate"] = int(kwargs.get("timeout", 0))
        return None

    def evaluate(self, snippet: str, arg: dict[str, Any]) -> Any:
        self.evaluated.append((snippet, arg))
        return {"url": arg["url"] + "&uifid=U", "headers": {"uifid": "U"}}


class _RecordingPlaywright:
    def __init__(self, *, fail_goto: bool = False, fail_stop: bool = False) -> None:
        self.page = _RecordingPage(fail_goto=fail_goto)
        self.fail_stop = fail_stop
        self.stops = 0
        self.launched_args: dict[str, Any] = {}

    def start(self) -> _RecordingPlaywright:
        return self

    def stop(self) -> None:
        if self.fail_stop:
            raise RuntimeError("stop boom")
        self.stops += 1

    @property
    def chromium(self) -> _RecordingPlaywright:
        return self

    def launch(self, **kwargs: Any) -> _RecordingPlaywright:
        self.launched_args = kwargs
        return self

    def new_context(self, **kwargs: Any) -> _RecordingPlaywright:
        return self

    def add_init_script(self, script: str) -> None:
        return None

    def new_page(self) -> _RecordingPage:
        return self.page

    def close(self) -> None:
        return None


@pytest.fixture()
def fake_playwright(monkeypatch: Any) -> _RecordingPlaywright:
    """Inject a recording ``playwright.sync_api`` module."""
    fake = _RecordingPlaywright()
    module = types.ModuleType("playwright.sync_api")
    module.sync_playwright = lambda: fake  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
    return fake


def test_open_signing_page_uses_headless_chromium(
    fake_playwright: _RecordingPlaywright,
) -> None:
    """Verify the real open path launches and settles the SDK page."""
    page, closer = websign_mod._open_signing_page(
        page_url="https://www.douyin.com/",
        user_agent="fake-agent",
        init_timeout_seconds=60.0,
        snippet_timeout_ms=25000,
    )
    assert page.gotos == ["https://www.douyin.com/"]
    assert fake_playwright.launched_args["headless"] is False
    assert "--headless=new" in fake_playwright.launched_args["args"]
    closer()
    assert fake_playwright.stops == 1


def test_open_signing_page_wraps_navigation_failure(
    monkeypatch: Any,
) -> None:
    """Verify a failed page load stops playwright and raises."""
    fake = _RecordingPlaywright(fail_goto=True)
    module = types.ModuleType("playwright.sync_api")
    module.sync_playwright = lambda: fake  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
    with pytest.raises(WebSignError, match="session init failed"):
        websign_mod._open_signing_page(
            page_url="https://www.douyin.com/",
            user_agent="fake-agent",
            init_timeout_seconds=60.0,
            snippet_timeout_ms=25000,
        )
    assert fake.stops == 1


def test_open_signing_page_without_playwright(monkeypatch: Any) -> None:
    """Verify a missing playwright wheel raises a clear error."""
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    with pytest.raises(WebSignError, match="playwright is not installed"):
        websign_mod._open_signing_page(
            page_url="https://www.douyin.com/",
            user_agent="fake-agent",
            init_timeout_seconds=60.0,
            snippet_timeout_ms=25000,
        )


def test_provider_end_to_end_over_fake_playwright(
    fake_playwright: _RecordingPlaywright,
) -> None:
    """Verify the default factory signs through the fake browser."""
    provider = WebSignProvider(
        page_url="https://www.douyin.com/",
        user_agent="fake-agent",
        init_timeout_seconds=5.0,
        sign_timeout_seconds=30.0,
    )
    with provider:
        result = provider.sign("https://www.douyin.com/x/?a=1")
    assert result.url.endswith("&uifid=U")
    assert result.headers == {"uifid": "U"}
    assert fake_playwright.page.evaluated[0][1]["timeoutMs"] == 25000


# ── fetch retry over the exception path ──────────────────────────────────


class _Fake403(Exception):
    """Mirror of f2's APIError shape for HTTP 403."""

    def __init__(self) -> None:
        self.status_code = 403
        super().__init__("HTTP status error: Status Code: 403")


class _Fake500(Exception):
    def __init__(self) -> None:
        self.status_code = 500
        super().__init__("HTTP status error: Status Code: 500")


def test_exception_status_shapes() -> None:
    """Verify status extraction across exception shapes."""
    assert websign_mod._exception_status(_Fake403()) == 403

    class _Wrapped(Exception):
        def __init__(self) -> None:
            self.response = _StubResponse(403, "x")

    assert websign_mod._exception_status(_Wrapped()) == 403
    assert websign_mod._exception_status(RuntimeError("boom")) is None


@pytest.mark.asyncio
async def test_fetch_retry_on_403_exception() -> None:
    """Verify a raised 403 triggers invalidate, re-sign, retry."""
    calls: list[str] = []

    def script(url: str) -> _StubResponse:
        calls.append(url)
        if len(calls) == 1:
            raise _Fake403()
        return _StubResponse(200, '{"status_code":0}')

    crawler_cls = _make_crawler(script)
    provider = _stub_provider(url=SIGNED_URL + "&fresh=1")
    install_fetch_retry(provider, crawler_cls=crawler_cls)
    try:
        crawler = crawler_cls()
        response = await crawler.get_fetch_data(
            "https://www.douyin.com/e/?a=1&uifid=OLD&timestamp=1"
            "&x-secsdk-web-signature=OLD"
        )
        assert response.status_code == 200
        provider.invalidate.assert_called_once()
        assert provider.sign.call_args[0][0] == "https://www.douyin.com/e/?a=1"
        assert crawler.calls[1] == SIGNED_URL + "&fresh=1"
    finally:
        uninstall_websign_patch()


@pytest.mark.asyncio
async def test_fetch_retry_reraises_non_403_exception() -> None:
    """Verify non-403 exceptions propagate untouched."""

    def script(url: str) -> _StubResponse:
        raise _Fake500()

    crawler_cls = _make_crawler(script)
    provider = _stub_provider()
    install_fetch_retry(provider, crawler_cls=crawler_cls)
    try:
        with pytest.raises(_Fake500):
            await crawler_cls().get_fetch_data("https://www.douyin.com/e/")
        provider.invalidate.assert_not_called()
    finally:
        uninstall_websign_patch()


@pytest.mark.asyncio
async def test_fetch_retry_reraises_original_when_resign_fails() -> None:
    """Verify a failed re-sign on the exception path reraises."""

    def script(url: str) -> _StubResponse:
        raise _Fake403()

    crawler_cls = _make_crawler(script)
    provider = MagicMock()
    provider.sign.side_effect = WebSignError("browser down")
    install_fetch_retry(provider, crawler_cls=crawler_cls)
    try:
        with pytest.raises(_Fake403):
            await crawler_cls().get_fetch_data("https://www.douyin.com/e/")
        provider.invalidate.assert_called_once()
    finally:
        uninstall_websign_patch()


# ── defensive-branch coverage ──────────────────────────────────────────


def test_redact_url_degrades_on_non_url() -> None:
    """Verify redaction degrades instead of raising."""
    assert websign_mod._redact_url(object()) == "unparseable-url"


def test_reply_drops_when_caller_timed_out() -> None:
    """Verify late replies are dropped instead of blocking."""
    reply: queue.Queue[Any] = queue.Queue(maxsize=1)
    reply.put_nowait("first")
    websign_mod._reply(reply, "late")
    assert reply.get_nowait() == "first"


def test_argus_probe_tolerates_raising_callable_body() -> None:
    """Verify a callable body that raises probes as non-block."""

    class _Raising:
        status_code = 403

        def text(self) -> str:
            raise RuntimeError("unreadable")

    assert not is_argus_block_response(_Raising())


def test_provider_rejects_unexpected_headers() -> None:
    """Verify non-dict headers tear the page down."""
    factory = _factory_for(lambda url: {"url": url, "headers": "nope"})
    provider = _provider(factory)
    with provider, pytest.raises(WebSignError, match="unexpected headers"):
        provider.sign("https://www.douyin.com/x/")
    assert factory.state == {"opens": 1, "closes": 1}


def test_provider_restarts_dead_thread() -> None:
    """Verify a dead signer thread is reaped and restarted."""
    factory = _factory_for(lambda url: {"url": url, "headers": {}})
    provider = _provider(factory)
    with provider:
        provider.sign("https://www.douyin.com/x/")
        old_thread = provider._thread
        assert old_thread is not None
        provider._ops.put(websign_mod._STOP)
        old_thread.join(timeout=5)
        assert not old_thread.is_alive()
        result = provider.sign("https://www.douyin.com/x/?b=2")
        assert result.url.endswith("?b=2")
        assert provider._thread is not old_thread
        assert factory.state["opens"] == 2


def test_provider_rejects_unknown_reply_shape(monkeypatch: Any) -> None:
    """Verify a foreign reply payload raises instead of returning Any."""
    factory = _factory_for(lambda url: {"url": url, "headers": {}})
    monkeypatch.setattr(websign_mod, "_sign_with_page", lambda *a: "junk")
    provider = _provider(factory)
    with provider, pytest.raises(WebSignError, match="returned str"):
        provider.sign("https://www.douyin.com/x/")


def test_provider_close_from_signer_thread_is_safe() -> None:
    """Verify close() never deadlocks when called on its own thread."""
    refs: dict[str, Any] = {}

    def factory(**kwargs: Any) -> Any:
        refs["thread"] = threading.current_thread()
        refs["provider"].close()  # Runs on the signer thread.
        return _FakePage(lambda url: {"url": url, "headers": {}}), lambda: None

    provider = _provider(factory)
    refs["provider"] = provider
    result = provider.sign("https://www.douyin.com/x/")
    assert result.url.endswith("/x/")
    assert provider._closed
    assert provider._thread is None
    thread = refs["thread"]
    provider._ops.put(websign_mod._STOP)
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_provider_wraps_unexpected_factory_error() -> None:
    """Verify non-WebSignError failures surface wrapped."""

    def boom(**kwargs: Any) -> Any:
        raise RuntimeError("driver exploded")

    provider = _provider(boom)
    with provider, pytest.raises(WebSignError, match="driver exploded"):
        provider.sign("https://www.douyin.com/x/")


def test_provider_invalidate_tolerates_close_failure() -> None:
    """Verify a raising closer does not break session rotation."""

    def factory(**kwargs: Any) -> Any:
        def closer() -> None:
            raise RuntimeError("close boom")

        return _FakePage(lambda url: {"url": url, "headers": {}}), closer

    provider = _provider(factory)
    with provider:
        provider.sign("https://www.douyin.com/x/")
        provider.invalidate()
        result = provider.sign("https://www.douyin.com/x/?b=2")
        assert result.url.endswith("?b=2")


def test_open_tolerates_missing_networkidle(
    fake_playwright: _RecordingPlaywright,
) -> None:
    """Verify a busy page still settles without network idle."""
    fake_playwright.page.fail_idle = True
    page, closer = websign_mod._open_signing_page(
        page_url="https://www.douyin.com/",
        user_agent="fake-agent",
        init_timeout_seconds=60.0,
        snippet_timeout_ms=25000,
    )
    assert page.gotos == ["https://www.douyin.com/"]
    closer()


def test_open_stop_failure_still_raises(monkeypatch: Any) -> None:
    """Verify playwright.stop failure does not mask init errors."""
    fake = _RecordingPlaywright(fail_goto=True, fail_stop=True)
    module = types.ModuleType("playwright.sync_api")
    module.sync_playwright = lambda: fake  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
    with pytest.raises(WebSignError, match="session init failed"):
        websign_mod._open_signing_page(
            page_url="https://www.douyin.com/",
            user_agent="fake-agent",
            init_timeout_seconds=60.0,
            snippet_timeout_ms=25000,
        )
    assert fake.stops == 0


def test_closer_runs_remaining_steps_after_failure(
    fake_playwright: _RecordingPlaywright,
) -> None:
    """Verify one failing close step does not skip the rest."""
    calls = {"n": 0}

    def flaky_close() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("close boom")

    fake_playwright.close = flaky_close  # type: ignore[method-assign]
    _, closer = websign_mod._open_signing_page(
        page_url="https://www.douyin.com/",
        user_agent="fake-agent",
        init_timeout_seconds=60.0,
        snippet_timeout_ms=25000,
    )
    closer()
    assert calls["n"] == 2
    assert fake_playwright.stops == 1


@pytest.mark.asyncio
async def test_fetch_retry_install_idempotent() -> None:
    """Verify double install wraps fetch helpers once."""
    calls: list[str] = []

    def script(url: str) -> _StubResponse:
        calls.append(url)
        if len(calls) == 1:
            raise _Fake403()
        return _StubResponse(200, "{}")

    crawler_cls = _make_crawler(script)
    provider = _stub_provider()
    install_fetch_retry(provider, crawler_cls=crawler_cls)
    install_fetch_retry(provider, crawler_cls=crawler_cls)
    try:
        response = await crawler_cls().get_fetch_data("https://www.douyin.com/e/")
        assert response.status_code == 200
        assert provider.sign.call_count == 1
    finally:
        uninstall_websign_patch()


def test_uninstall_tolerates_dead_owner() -> None:
    """Verify uninstall skips owners that reject attribute restore."""
    websign_mod._PATCHED.append((object(), "missing", None, None))
    uninstall_websign_patch()
    assert not is_websign_patched()


def test_provider_backs_off_after_repeated_failures() -> None:
    """Verify consecutive failures fail fast without new attempts."""
    opens = {"n": 0}

    def factory(**kwargs: Any) -> Any:
        opens["n"] += 1
        raise WebSignError("browser down")

    provider = _provider(factory)
    with provider:
        for _ in range(3):
            with pytest.raises(WebSignError, match="browser down"):
                provider.sign("https://www.douyin.com/x/")
        assert opens["n"] == 3
        with pytest.raises(WebSignError, match="backing off"):
            provider.sign("https://www.douyin.com/x/")
        assert opens["n"] == 3


def test_provider_backoff_resets_on_success() -> None:
    """Verify a success clears the consecutive-failure count."""
    factory_attempts = {"n": 0}
    sign_attempts = {"n": 0}

    def factory(**kwargs: Any) -> Any:
        factory_attempts["n"] += 1
        if factory_attempts["n"] <= 2:
            raise WebSignError("flaky signer")

        def script(url: str) -> Any:
            sign_attempts["n"] += 1
            if sign_attempts["n"] > 1:
                raise WebSignError("flaky signer")
            return {"url": url, "headers": {}}

        return _FakePage(script), lambda: None

    provider = _provider(factory)
    with provider:
        for _ in range(2):
            with pytest.raises(WebSignError, match="flaky signer"):
                provider.sign("https://www.douyin.com/x/")
        assert provider.sign("https://www.douyin.com/x/").url.endswith("/x/")
        for _ in range(2):
            with pytest.raises(WebSignError, match="flaky signer"):
                provider.sign("https://www.douyin.com/x/")


def test_provider_backoff_expires_after_cooldown(monkeypatch: Any) -> None:
    """Verify the backoff window eventually allows a retry."""
    opens = {"n": 0}

    def factory(**kwargs: Any) -> Any:
        opens["n"] += 1
        raise WebSignError("browser down")

    monkeypatch.setattr(websign_mod, "_FAILURE_COOLDOWN_SECONDS", 0.0)
    provider = _provider(factory)
    with provider:
        for _ in range(4):
            with pytest.raises(WebSignError, match="browser down"):
                provider.sign("https://www.douyin.com/x/")
        assert opens["n"] == 4


def test_invalidate_preserves_backoff() -> None:
    """Verify explicit invalidation does not reset failure backoff."""
    opens = {"n": 0}

    def factory(**kwargs: Any) -> Any:
        opens["n"] += 1
        raise WebSignError("browser down")

    provider = _provider(factory)
    with provider:
        for _ in range(3):
            with pytest.raises(WebSignError):
                provider.sign("https://www.douyin.com/x/")
        provider.invalidate()
        with pytest.raises(WebSignError, match="backing off"):
            provider.sign("https://www.douyin.com/x/")
        assert opens["n"] == 3


# ── sign/close ordering and loop hygiene ──────────────────────────────


def test_sign_after_close_fails_fast_without_waiting() -> None:
    """A sign racing a lost close raises closed, never blocks to timeout."""
    import time

    factory = _factory_for(lambda url: {"url": url, "headers": {}})
    provider = _provider(factory, init_timeout_seconds=30.0, sign_timeout_seconds=30.0)
    provider.sign("https://www.douyin.com/x/")
    provider.close()
    started = time.monotonic()
    with pytest.raises(WebSignError, match="closed"):
        provider.sign("https://www.douyin.com/x/")
    assert time.monotonic() - started < 5.0


@pytest.mark.asyncio
async def test_fetch_resign_yields_the_event_loop() -> None:
    """Re-signing offloads to a thread; the loop stays responsive."""
    import asyncio

    gate = threading.Event()
    script_calls: list[str] = []

    def script(url: str) -> _StubResponse:
        script_calls.append(url)
        if len(script_calls) == 1:
            return _StubResponse(403, "Blocked by ArgusSecurityPlugin")
        return _StubResponse(200, '{"status_code":0}')

    crawler_cls = _make_crawler(script)
    provider = MagicMock()
    provider.sign.side_effect = lambda url: (
        gate.wait(timeout=10),
        SignedResult(url=SIGNED_URL, headers={}),
    )[1]
    install_fetch_retry(provider, crawler_cls=crawler_cls)
    try:
        crawler = crawler_cls()
        fetch_task = asyncio.create_task(
            crawler.get_fetch_data("https://www.douyin.com/e/?a=1")
        )
        ticks = 0
        for _ in range(20):
            await asyncio.sleep(0.01)
            ticks += 1
            if len(script_calls) >= 1 and ticks >= 5:
                break
        # The loop ticked while the re-sign was still gated: proof the
        # blocking sign ran off-loop.
        assert ticks >= 5
        assert not fetch_task.done()
        gate.set()
        response = await asyncio.wait_for(fetch_task, timeout=10)
        assert response.status_code == 200
    finally:
        uninstall_websign_patch()


def test_check_op_rejects_foreign_queue_items() -> None:
    """A queue-protocol violation raises TypeError, never silently proceeds."""
    op = websign_mod._SignOp(url="https://www.douyin.com/", reply=None)
    assert websign_mod._check_op(op) is op
    with pytest.raises(TypeError, match="unexpected websign queue item"):
        websign_mod._check_op(object())
