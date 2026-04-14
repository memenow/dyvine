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


def test_api_settings_defaults() -> None:
    s = APISettings()
    assert s.version == "1.0.0"
    assert s.prefix == "/api/v1"
    assert s.project_name == "Dyvine API"
    assert s.debug is False
    assert s.host == "0.0.0.0"
    assert s.port == 8000


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
    monkeypatch.setenv("API_DEBUG", "true")
    s = SecuritySettings()
    assert s.secret_key == "change-me-in-production"


def test_security_settings_rejects_defaults_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("API_DEBUG", "false")
    with pytest.raises(ValidationError):
        SecuritySettings()


# ── R2Settings ───────────────────────────────────────────────────────────


def test_r2_settings_is_configured_true() -> None:
    s = R2Settings(
        account_id="acc",
        access_key_id="key",
        secret_access_key="secret",
        bucket_name="bucket",
    )
    assert s.is_configured is True


def test_r2_settings_is_configured_false_missing_field() -> None:
    s = R2Settings(
        account_id="acc",
        access_key_id="",
        secret_access_key="secret",
        bucket_name="bucket",
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
