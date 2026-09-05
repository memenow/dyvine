"""Tests for settings validation and convenience properties."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from dyvine.core.settings import (
    APISettings,
    DatabaseSettings,
    DouyinSettings,
    R2Settings,
    SecuritySettings,
    Settings,
    get_settings,
)

# ── APISettings ──────────────────────────────────────────────────────────


def test_api_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify API settings defaults."""
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


def test_api_settings_port_too_low() -> None:
    """Verify API settings port too low."""
    with pytest.raises(ValidationError):
        APISettings(port=0)


def test_api_settings_port_too_high() -> None:
    """Verify API settings port too high."""
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
    monkeypatch.setenv("SECURITY_API_KEY", "change-me-in-production")
    s = Settings()
    assert s.security.api_key == "change-me-in-production"


def test_security_settings_rejects_defaults_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify security settings rejects defaults in production."""
    monkeypatch.setenv("API_DEBUG", "false")
    # The shared conftest defaults REQUIRE to false (router-test
    # convenience); the gate under test only fires when auth is on.
    monkeypatch.setenv("SECURITY_REQUIRE_API_KEY", "true")
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
    # The shared conftest defaults REQUIRE to false (router-test
    # convenience); the gate under test only fires when auth is on.
    monkeypatch.setenv("SECURITY_REQUIRE_API_KEY", "true")
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
    monkeypatch.setenv("SECURITY_API_KEY", "change-me-in-production")
    s = SecuritySettings()
    assert s.api_key == "change-me-in-production"


# ── R2Settings ───────────────────────────────────────────────────────────


def test_r2_settings_is_configured_true() -> None:
    """Verify R2 settings is configured true."""
    s = R2Settings(
        account_id="acc",
        access_key_id="key",
        secret_access_key="secret",
        bucket_name="bucket",
        endpoint="https://example.r2.cloudflarestorage.com",
    )
    assert s.is_configured is True


def test_r2_settings_is_configured_false_missing_field() -> None:
    """Verify R2 settings is configured false missing field."""
    s = R2Settings(
        account_id="acc",
        access_key_id="",
        secret_access_key="secret",
        bucket_name="bucket",
        endpoint="https://example.r2.cloudflarestorage.com",
    )
    assert s.is_configured is False


def test_r2_settings_is_configured_false_missing_endpoint() -> None:
    """Verify R2 settings is configured false missing endpoint."""
    s = R2Settings(
        account_id="acc",
        access_key_id="key",
        secret_access_key="secret",
        bucket_name="bucket",
        endpoint="",
    )
    assert s.is_configured is False


def test_r2_settings_is_configured_false_all_empty() -> None:
    """Verify R2 settings is configured false all empty."""
    s = R2Settings()
    assert s.is_configured is False


# ── DouyinSettings ───────────────────────────────────────────────────────


def test_douyin_settings_headers_property() -> None:
    """Verify douyin settings headers property."""
    s = DouyinSettings(cookie="ck", user_agent="ua", referer="ref")
    headers = s.headers
    assert headers["User-Agent"] == "ua"
    assert headers["Referer"] == "ref"
    assert headers["Cookie"] == "ck"


def test_douyin_settings_proxies_property() -> None:
    """Verify douyin settings proxies property."""
    s = DouyinSettings(proxy_http="http://p", proxy_https="https://p")
    proxies = s.proxies
    assert proxies["http://"] == "http://p"
    assert proxies["https://"] == "https://p"


def test_douyin_settings_proxies_none_by_default() -> None:
    """Verify douyin settings proxies none by default."""
    s = DouyinSettings()
    assert s.proxies["http://"] is None
    assert s.proxies["https://"] is None


# ── DatabaseSettings ─────────────────────────────────────────────────────


def test_database_settings_defaults() -> None:
    """Localhost Postgres default for development."""
    s = DatabaseSettings()
    assert s.url == "postgresql+asyncpg://dyvine:dyvine@localhost:5432/dyvine"
    assert s.pool_size == 5
    assert s.pool_timeout == 30.0
    assert s.operation_retention_days == 30


def test_multi_replica_settings_defaults() -> None:
    """Single-replica local development needs no shared storage."""
    s = APISettings()
    assert s.multi_replica is False
    assert s.shared_file_storage is False


def test_watch_enabled_defaults_true() -> None:
    """One process runs watch loops unless explicitly split."""
    assert Settings().watch_enabled is True


def test_watch_enabled_reads_unprefixed_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``WATCH_ENABLED`` (not ``DOUYIN_WATCH_*``) flips the split."""
    monkeypatch.setenv("WATCH_ENABLED", "false")
    assert Settings().watch_enabled is False


def test_settings_rejects_default_database_url_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-debug builds must override the localhost database default."""
    monkeypatch.setenv("API_DEBUG", "false")
    monkeypatch.setenv("SECURITY_REQUIRE_API_KEY", "true")
    monkeypatch.setenv("SECURITY_API_KEY", "real-key-value")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError):
        Settings()


def test_settings_accepts_explicit_database_url_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit URL + key boot a non-debug build."""
    monkeypatch.setenv("API_DEBUG", "false")
    monkeypatch.setenv("SECURITY_REQUIRE_API_KEY", "true")
    monkeypatch.setenv("SECURITY_API_KEY", "real-key-value")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://db.internal:5432/dyvine")
    s = Settings()
    assert s.database.url == "postgresql+asyncpg://db.internal:5432/dyvine"


# ── Settings (composite) ────────────────────────────────────────────────


def test_settings_convenience_properties() -> None:
    """Verify settings convenience properties."""
    s = Settings()
    assert s.debug == s.api.debug
    assert s.version == s.api.version
    assert s.prefix == s.api.prefix
    assert s.project_name == s.api.project_name


def test_settings_backward_compat_properties() -> None:
    """Verify settings backward compat properties."""
    s = Settings()
    assert s.host == s.api.host
    assert s.port == s.api.port
    assert s.douyin_cookie == s.douyin.cookie
    assert s.douyin_headers == s.douyin.headers


# ── get_settings ─────────────────────────────────────────────────────────


def test_get_settings_returns_settings_instance() -> None:
    """Verify get settings returns settings instance."""
    get_settings.cache_clear()
    s = get_settings()
    assert isinstance(s, Settings)
    get_settings.cache_clear()


def test_get_settings_caches() -> None:
    """Verify get settings caches."""
    get_settings.cache_clear()
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2
    get_settings.cache_clear()
