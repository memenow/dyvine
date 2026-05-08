from __future__ import annotations

import pytest
from pydantic import ValidationError

from dyvine.core.settings import (
    APISettings,
    DouyinSettings,
    R2Settings,
    SecuritySettings,
    Settings,
    get_settings,
)

# ── APISettings ──────────────────────────────────────────────────────────


def test_api_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    # The shared test conftest sets ``API_DEBUG=true`` so the
    # production-only validator in ``SecuritySettings`` does not fire on the
    # sentinel defaults. Drop it here so we are asserting the true
    # out-of-the-box defaults rather than our test-runtime override.
    monkeypatch.delenv("API_DEBUG", raising=False)
    s = APISettings()
    assert s.version == "1.0.0"
    assert s.prefix == "/api/v1"
    assert s.project_name == "Dyvine API"
    assert s.debug is False
    assert s.host == "0.0.0.0"
    assert s.port == 8000
    assert s.operation_db_path == "data/douyin/state/operations.db"


def test_api_settings_port_too_low() -> None:
    with pytest.raises(ValidationError):
        APISettings(port=0)


def test_api_settings_port_too_high() -> None:
    with pytest.raises(ValidationError):
        APISettings(port=70000)


# ── SecuritySettings ─────────────────────────────────────────────────────


def test_security_settings_defaults_pass_in_debug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The placeholder secret values are tolerated when ``API_DEBUG=true``.

    The cross-field check now lives on the composite ``Settings`` model
    so that the validator sees the same ``api.debug`` value the rest of
    the application reads. ``SecuritySettings`` on its own is therefore
    permissive — the gate fires only when the placeholder is paired with
    a non-debug build.
    """
    monkeypatch.setenv("API_DEBUG", "true")
    monkeypatch.setenv("SECURITY_SECRET_KEY", "change-me-in-production")
    monkeypatch.setenv("SECURITY_API_KEY", "change-me-in-production")
    s = Settings()
    assert s.security.secret_key == "change-me-in-production"


def test_security_settings_rejects_defaults_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("API_DEBUG", "false")
    monkeypatch.setenv("SECURITY_SECRET_KEY", "change-me-in-production")
    monkeypatch.setenv("SECURITY_API_KEY", "change-me-in-production")
    with pytest.raises(ValidationError):
        Settings()


def test_security_settings_rejects_defaults_when_api_debug_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``API_DEBUG`` left unset must default to "production" semantics.

    Previously the ad-hoc ``os.getenv("API_DEBUG", "false")`` lookup
    inside ``SecuritySettings`` could disagree with ``settings.api.debug``
    when the value lived in ``.env`` rather than the live environment.
    The composite-level validator now relies on ``self.api.debug``, which
    pydantic-settings derives from the same payload as the rest of the
    config, so the unset case is rejected consistently.
    """
    monkeypatch.delenv("API_DEBUG", raising=False)
    monkeypatch.setenv("SECURITY_SECRET_KEY", "change-me-in-production")
    monkeypatch.setenv("SECURITY_API_KEY", "change-me-in-production")
    with pytest.raises(ValidationError):
        Settings()


def test_security_settings_isolated_construct_is_permissive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``SecuritySettings`` alone no longer enforces the production gate.

    The cross-field check moved up to the composite ``Settings`` model,
    so building a bare ``SecuritySettings`` with placeholder values must
    succeed; otherwise downstream fixtures that monkeypatch only the
    inner model would lose all flexibility.
    """
    monkeypatch.setenv("API_DEBUG", "false")
    monkeypatch.setenv("SECURITY_SECRET_KEY", "change-me-in-production")
    monkeypatch.setenv("SECURITY_API_KEY", "change-me-in-production")
    s = SecuritySettings()
    assert s.secret_key == "change-me-in-production"


# ── R2Settings ───────────────────────────────────────────────────────────


def test_r2_settings_is_configured_true() -> None:
    s = R2Settings(
        account_id="acc",
        access_key_id="key",
        secret_access_key="secret",
        bucket_name="bucket",
        endpoint="https://example.r2.cloudflarestorage.com",
    )
    assert s.is_configured is True


def test_r2_settings_is_configured_false_missing_field() -> None:
    s = R2Settings(
        account_id="acc",
        access_key_id="",
        secret_access_key="secret",
        bucket_name="bucket",
        endpoint="https://example.r2.cloudflarestorage.com",
    )
    assert s.is_configured is False


def test_r2_settings_is_configured_false_missing_endpoint() -> None:
    s = R2Settings(
        account_id="acc",
        access_key_id="key",
        secret_access_key="secret",
        bucket_name="bucket",
        endpoint="",
    )
    assert s.is_configured is False


def test_r2_settings_is_configured_false_all_empty() -> None:
    s = R2Settings()
    assert s.is_configured is False


# ── DouyinSettings ───────────────────────────────────────────────────────


def test_douyin_settings_headers_property() -> None:
    s = DouyinSettings(cookie="ck", user_agent="ua", referer="ref")
    headers = s.headers
    assert headers["User-Agent"] == "ua"
    assert headers["Referer"] == "ref"
    assert headers["Cookie"] == "ck"


def test_douyin_settings_proxies_property() -> None:
    s = DouyinSettings(proxy_http="http://p", proxy_https="https://p")
    proxies = s.proxies
    assert proxies["http://"] == "http://p"
    assert proxies["https://"] == "https://p"


def test_douyin_settings_proxies_none_by_default() -> None:
    s = DouyinSettings()
    assert s.proxies["http://"] is None
    assert s.proxies["https://"] is None


# ── Settings (composite) ────────────────────────────────────────────────


def test_settings_convenience_properties() -> None:
    s = Settings()
    assert s.debug == s.api.debug
    assert s.version == s.api.version
    assert s.prefix == s.api.prefix
    assert s.project_name == s.api.project_name
    assert s.operation_db_path == s.api.operation_db_path


def test_settings_backward_compat_properties() -> None:
    s = Settings()
    assert s.host == s.api.host
    assert s.port == s.api.port
    assert s.douyin_cookie == s.douyin.cookie
    assert s.douyin_headers == s.douyin.headers


# ── get_settings ─────────────────────────────────────────────────────────


def test_get_settings_returns_settings_instance() -> None:
    get_settings.cache_clear()
    s = get_settings()
    assert isinstance(s, Settings)
    get_settings.cache_clear()


def test_get_settings_caches() -> None:
    get_settings.cache_clear()
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2
    get_settings.cache_clear()
