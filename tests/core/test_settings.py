"""Tests for settings validation and convenience properties."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from dyvine.core.settings import (
    DatabaseSettings,
    DouyinSettings,
    R2Settings,
    RuntimeSettings,
    Settings,
    get_settings,
)

# ── RuntimeSettings ──────────────────────────────────────────────────────


def test_runtime_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Debug is off out of the box (production semantics)."""
    # The shared test conftest sets ``API_DEBUG=true``; drop it here so
    # we are asserting the true out-of-the-box default rather than our
    # test-runtime override.
    monkeypatch.delenv("API_DEBUG", raising=False)
    s = RuntimeSettings()
    assert s.debug is False


def test_runtime_settings_reads_api_debug_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``API_`` prefix is kept for environment stability."""
    monkeypatch.setenv("API_DEBUG", "true")
    assert RuntimeSettings().debug is True


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
    assert s.pool_class == "null"
    assert s.pool_size == 5
    assert s.pool_max_overflow == 2
    assert s.pool_timeout == 30.0
    assert s.pool_recycle_seconds == 300.0
    assert s.pool_pre_ping is True
    assert s.janitor_interval_seconds == 0.0
    assert s.operation_retention_days == 30


def test_database_settings_read_pool_and_janitor_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pool strategy and janitor cadence come from ``DATABASE_*`` env."""
    monkeypatch.setenv("DATABASE_POOL_CLASS", "queue")
    monkeypatch.setenv("DATABASE_POOL_SIZE", "3")
    monkeypatch.setenv("DATABASE_POOL_MAX_OVERFLOW", "4")
    monkeypatch.setenv("DATABASE_POOL_TIMEOUT", "7")
    monkeypatch.setenv("DATABASE_POOL_RECYCLE_SECONDS", "60")
    monkeypatch.setenv("DATABASE_POOL_PRE_PING", "false")
    monkeypatch.setenv("DATABASE_JANITOR_INTERVAL_SECONDS", "120")
    s = DatabaseSettings()
    assert s.pool_class == "queue"
    assert s.pool_size == 3
    assert s.pool_max_overflow == 4
    assert s.pool_timeout == 7.0
    assert s.pool_recycle_seconds == 60.0
    assert s.pool_pre_ping is False
    assert s.janitor_interval_seconds == 120.0


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
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError):
        Settings()


def test_settings_accepts_explicit_database_url_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit URL boots a non-debug build."""
    monkeypatch.setenv("API_DEBUG", "false")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://db.internal:5432/dyvine")
    s = Settings()
    assert s.database.url == "postgresql+asyncpg://db.internal:5432/dyvine"


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://dyvine:dyvine@localhost:5432/dyvine",
        "postgresql+asyncpg://dyvine:dyvine@127.0.0.1:5432/dyvine",
        "postgresql+asyncpg://other:other@localhost:5432/other/",
        "postgresql+asyncpg://dyvine:dyvine@[::1]:5432/dyvine",
        "",
        "not a url",
    ],
)
def test_settings_rejects_loopback_database_urls_in_production(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    """Loopback synonyms cannot smuggle a local DB into production.

    Matching the raw default string let ``127.0.0.1``, trailing
    slashes, or different credentials bypass the guard; the check now
    parses the hostname.
    """
    monkeypatch.setenv("API_DEBUG", "false")
    monkeypatch.setenv("DATABASE_URL", url)
    with pytest.raises(ValidationError):
        Settings()


def test_nested_settings_read_dotenv_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Direct construction reads the same `.env` as the composite.

    Nested groups previously ignored `env_file`, so `Settings()` and
    `DatabaseSettings()` disagreed whenever a value lived in `.env`
    rather than the real environment.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_POOL_SIZE", raising=False)
    (tmp_path / ".env").write_text("DATABASE_POOL_SIZE=7\n")
    assert DatabaseSettings().pool_size == 7
    monkeypatch.setenv("API_DEBUG", "true")
    assert Settings().database.pool_size == 7


def test_settings_proxy_tracks_cache_resets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `settings` binding always reflects the cached instance.

    A directly bound singleton goes stale for early importers after
    `cache_clear()`; the proxy delegates every read to whatever
    `get_settings()` currently returns.
    """
    import dyvine.core.settings as settings_module

    monkeypatch.setenv("API_DEBUG", "true")
    first = settings_module.settings.runtime
    assert first is get_settings().runtime
    get_settings.cache_clear()
    second = settings_module.settings.runtime
    assert second is get_settings().runtime
    assert second is not first
    get_settings.cache_clear()


def test_importing_settings_module_never_validates() -> None:
    """Importing the module must not validate, even when misconfigured.

    Runs in a subprocess with a production env and no database URL:
    attribute access would raise, but the bare import (plus proxy
    creation) must exit cleanly.
    """
    import os
    import subprocess
    import sys

    repo_root = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    env["API_DEBUG"] = "false"
    env.pop("DATABASE_URL", None)
    env["PYTHONPATH"] = str(repo_root / "src") + os.pathsep + env.get("PYTHONPATH", "")
    completed = subprocess.run(
        [sys.executable, "-c", "from dyvine.core.settings import settings"],
        capture_output=True,
        text=True,
        env=env,
        cwd=repo_root,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr


# ── Settings (composite) ────────────────────────────────────────────────


def test_settings_convenience_properties() -> None:
    """Verify settings convenience properties."""
    s = Settings()
    assert s.debug == s.runtime.debug
    assert s.douyin_cookie == s.douyin.cookie
    assert s.douyin_headers == s.douyin.headers
    assert s.r2_endpoint == s.r2.endpoint


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
