"""Composite Pydantic settings for Dyvine.

The composite `Settings` aggregates five `BaseSettings` subclasses,
each scoped by a distinct environment-variable prefix:

- `RuntimeSettings` (`API_`) — debug flag. The `API_` prefix is kept
  for environment stability (`API_DEBUG`); there is no HTTP server.
- `DatabaseSettings` (`DATABASE_`) — Postgres URL, pool strategy,
  janitor cadence, and operation retention.
- `R2Settings` (`R2_`) — Cloudflare R2 credentials and endpoint.
- `DouyinSettings` (`DOUYIN_`) — session cookie, headers, proxy,
  download root, livestream-specific HTTP headers, and the Argus
  webSign signing session.
- `WatchSettings` (`DOUYIN_WATCH_`) — watch-mode polling cadences
  and subscription guardrails.

A model-level validator (`_validate_production_placeholders`)
refuses to instantiate the container when `runtime.debug` is `false`
and `database.url` still matches the localhost development default.
The cross-field check lives on the composite class so the validator
sees the parsed payload rather than reading `os.environ` directly,
which used to silently disagree with `.env`-supplied values.

`get_settings()` is `lru_cache`d and loads `.env` at first call.
Tests reset the cache via `tests/conftest.py::reset_singletons` so
each test sees pristine settings.

Convenience properties (`debug`, the legacy `douyin_*` / `r2_*`
accessors) keep older call sites working without forcing them
through the nested `settings.runtime.*` / `settings.douyin.*`
addresses.
"""

from functools import lru_cache
from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class RuntimeSettings(BaseSettings):
    """Runtime configuration settings.

    Attributes:
        debug: Enable debug mode with verbose logging. Non-debug
            builds must override development-default placeholders
            (enforced by the composite validator below).

    Environment Variables:
        API_DEBUG.
    """

    debug: bool = Field(
        default=False, description="Enable debug mode with verbose logging"
    )
    # ``API_`` stays the prefix (not ``RUNTIME_``) so existing
    # environments and the plugin host's ``API_DEBUG`` default keep
    # working unchanged.
    model_config = SettingsConfigDict(env_prefix="API_")


_DEFAULT_DATABASE_URL = "postgresql+asyncpg://dyvine:dyvine@localhost:5432/dyvine"


class DatabaseSettings(BaseSettings):
    """Postgres connection settings for operation and watch state.

    Postgres is the only production backend. The default URL targets a
    local development database (``docker run -e POSTGRES_USER=dyvine -e
    POSTGRES_PASSWORD=dyvine -e POSTGRES_DB=dyvine -p 5432:5432
    postgres:16``); non-debug builds must override it, enforced by the
    composite validator below.

    Attributes:
        url: SQLAlchemy database URL (``postgresql+asyncpg://...``).
        pool_class: Connection-pool strategy. ``"null"`` opens a fresh
            connection per use and holds none while idle (the serverless
            default: zero idle connections); ``"queue"`` keeps a capped
            ``pool_size`` + ``pool_max_overflow`` pool for deployments
            that prefer bounded concurrency over zero idle state.
        pool_size: Steady-state pooled connections per process
            (``"queue"`` only; ignored by ``"null"``).
        pool_max_overflow: Extra connections beyond ``pool_size`` under
            burst (``"queue"`` only; ignored by ``"null"``).
        pool_timeout: Seconds to wait for a pooled connection
            (``"queue"`` only; ignored by ``"null"``).
        pool_recycle_seconds: Pooled connections older than this are
            discarded on next checkout (``"queue"`` only; ``-1``
            disables); ``"null"`` ignores it.
        pool_pre_ping: Probe a pooled connection before each checkout
            (``"queue"`` only; ``"null"`` always skips the probe because
            every checkout already opens a fresh connection).
        janitor_interval_seconds: Seconds between janitor
            heartbeat/sweep passes. ``0`` (default) disables the loop:
            single processes keep boot recovery plus a daily retention
            purge and otherwise never touch the database while idle.
        operation_retention_days: Terminal operation rows older than
            this are purged at boot and daily; ``0`` disables purging.

    Environment Variables:
        DATABASE_URL, DATABASE_POOL_CLASS, DATABASE_POOL_SIZE,
        DATABASE_POOL_MAX_OVERFLOW, DATABASE_POOL_TIMEOUT,
        DATABASE_POOL_RECYCLE_SECONDS, DATABASE_POOL_PRE_PING,
        DATABASE_JANITOR_INTERVAL_SECONDS,
        DATABASE_OPERATION_RETENTION_DAYS.
    """

    url: str = Field(
        default=_DEFAULT_DATABASE_URL,
        description="SQLAlchemy database URL for operation/watch state",
    )
    pool_class: Literal["queue", "null"] = Field(
        default="null",
        description="Connection-pool strategy: per-use connects or a capped pool",
    )
    pool_size: int = Field(
        default=5, ge=1, description="Steady-state pooled DB connections"
    )
    pool_max_overflow: int = Field(
        default=2, ge=0, description="Burst connections beyond pool_size"
    )
    pool_timeout: float = Field(
        default=30.0, ge=1.0, description="Seconds to wait for a DB connection"
    )
    pool_recycle_seconds: float = Field(
        default=300.0,
        ge=-1.0,
        description="Discard pooled connections older than this (-1 disables)",
    )
    pool_pre_ping: bool = Field(
        default=True, description="Probe pooled connections before checkout"
    )
    janitor_interval_seconds: float = Field(
        default=0.0,
        ge=0.0,
        description="Janitor heartbeat/sweep cadence (0 disables the loop)",
    )
    operation_retention_days: int = Field(
        default=30,
        ge=0,
        description="Purge terminal operations older than this (0 disables)",
    )

    model_config = SettingsConfigDict(env_prefix="DATABASE_")


class R2Settings(BaseSettings):
    """Cloudflare R2 object storage configuration settings.

    Contains all settings required for Cloudflare R2 storage integration
    including authentication credentials and bucket configuration.

    Attributes:
        account_id: Cloudflare account identifier.
        access_key_id: R2 access key ID for authentication.
        secret_access_key: R2 secret access key for authentication.
        bucket_name: Name of the R2 storage bucket.
        endpoint: R2 API endpoint URL.

    Environment Variables:
        R2_ACCOUNT_ID: Cloudflare account ID.
        R2_ACCESS_KEY_ID: R2 access key ID.
        R2_SECRET_ACCESS_KEY: R2 secret access key.
        R2_BUCKET_NAME: R2 bucket name.
        R2_ENDPOINT: R2 endpoint URL.

    Example:
        Check if R2 is properly configured:
            r2_settings = R2Settings()
            if r2_settings.is_configured:
                print("R2 storage is ready")
    """

    account_id: str = Field(default="", description="Cloudflare account identifier")
    access_key_id: str = Field(
        default="", description="R2 access key ID for authentication"
    )
    secret_access_key: str = Field(
        default="", description="R2 secret access key for authentication"
    )
    bucket_name: str = Field(default="", description="Name of the R2 storage bucket")
    endpoint: str = Field(default="", description="R2 API endpoint URL")

    @property
    def is_configured(self) -> bool:
        """Check if all required R2 settings are configured.

        Returns:
            True if all required R2 credentials and settings are provided,
            False otherwise.
        """
        return all(
            [
                self.account_id,
                self.access_key_id,
                self.secret_access_key,
                self.bucket_name,
                self.endpoint,
            ]
        )

    model_config = SettingsConfigDict(env_prefix="R2_")


class DouyinSettings(BaseSettings):
    """Douyin platform-specific configuration settings.

    Contains all settings required for interacting with the Douyin platform
    including authentication cookies, HTTP headers, and proxy settings.

    Attributes:
        cookie: Douyin authentication cookie string.
        user_agent: HTTP User-Agent header for requests.
        referer: HTTP Referer header for requests.
        proxy_http: HTTP proxy URL (optional).
        proxy_https: HTTPS proxy URL (optional).
        download_root: Filesystem root that bounds every user-supplied
            output path.
        retain_local_downloads: Keep per-task files locally instead of
            deleting them after each run (implied when R2 is unconfigured).
        retain_max_gb: Optional GiB cap that prunes the oldest retained
            workspaces once exceeded; ``0`` disables pruning.
        websign_enabled: Master switch for the Argus webSign layer.
        websign_page_url: Page loaded to initialize the signing session.
        websign_init_timeout_seconds: Chromium launch and SDK settle budget.
        websign_sign_timeout_seconds: Per-request signing budget.
        websign_retry_once: Re-sign and retry once on an Argus block.

    Environment Variables:
        DOUYIN_COOKIE, DOUYIN_USER_AGENT, DOUYIN_REFERER,
        DOUYIN_PROXY_HTTP, DOUYIN_PROXY_HTTPS, DOUYIN_DOWNLOAD_ROOT,
        DOUYIN_RETAIN_LOCAL_DOWNLOADS, DOUYIN_RETAIN_MAX_GB,
        DOUYIN_WEBSIGN_ENABLED, DOUYIN_WEBSIGN_PAGE_URL,
        DOUYIN_WEBSIGN_INIT_TIMEOUT_SECONDS,
        DOUYIN_WEBSIGN_SIGN_TIMEOUT_SECONDS, DOUYIN_WEBSIGN_RETRY_ONCE.

    Note:
        A valid cookie is required for most Douyin API operations.
        The default User-Agent mimics a Windows Chrome browser.
    """

    cookie: str = Field(default="", description="Douyin authentication cookie string")
    user_agent: str = Field(
        default="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        description="HTTP User-Agent header for requests",
    )
    referer: str = Field(
        default="https://www.douyin.com/",
        description="HTTP Referer header for requests",
    )
    proxy_http: str | None = Field(
        default=None, description="HTTP proxy URL (optional)"
    )
    proxy_https: str | None = Field(
        default=None, description="HTTPS proxy URL (optional)"
    )
    download_root: str = Field(
        default="data/douyin/downloads",
        description=(
            "Filesystem root used to jail user-supplied output paths. "
            "Requests whose ``output_path`` resolves outside this root are "
            "rejected at the schema layer."
        ),
    )
    # Livestream-specific headers sent to live.douyin.com.
    # Extract the CSRF token from browser DevTools (Network tab ->
    # x-secsdk-csrf-token header on any live.douyin.com request).
    # When empty, the header is omitted entirely.
    live_csrf_token: str = Field(
        default="",
        description="x-secsdk-csrf-token for livestream requests (leave empty to omit)",
    )
    # sec-ch-ua Client Hints header. Keep the version numbers aligned
    # with the Chrome version used to obtain DOUYIN_COOKIE.
    live_sec_ch_ua: str = Field(
        default='"Not A(Brand";v="99", "Google Chrome";v="131", "Chromium";v="131"',
        description="sec-ch-ua header value for livestream requests",
    )
    retain_local_downloads: bool = Field(
        default=False,
        description=(
            "Keep each task's downloaded files in the local workspace "
            "instead of deleting them after the run. Treated as enabled "
            "whenever R2 is not configured, so a completed download is "
            "never silently discarded with nowhere to archive it."
        ),
    )
    retain_max_gb: float = Field(
        default=0.0,
        ge=0.0,
        description=(
            "Soft cap, in GiB, on the retained-downloads workspace. When "
            "greater than zero, the oldest per-task directories are pruned "
            "after each run until the total falls back under the cap. Zero "
            "leaves the workspace unbounded (pruning disabled)."
        ),
    )
    websign_enabled: bool = Field(
        default=True,
        description=(
            "Append the Argus webSign triple (uifid/timestamp/"
            "x-secsdk-web-signature) to every Douyin web API URL via a "
            "headless-Chromium signing session. Disable only as a "
            "kill-switch; gated endpoints return HTTP 403 without it."
        ),
    )
    websign_page_url: str = Field(
        default="https://www.douyin.com/",
        description="Page loaded to initialize the signing session.",
    )
    websign_init_timeout_seconds: float = Field(
        default=120.0,
        gt=0.0,
        description="Budget for launching Chromium and settling the SDK.",
    )
    websign_sign_timeout_seconds: float = Field(
        default=30.0,
        gt=0.0,
        description="Per-request signing budget once the session is ready.",
    )
    websign_retry_once: bool = Field(
        default=True,
        description=(
            "On an observed Argus block, invalidate the signing session "
            "and retry the request once with a fresh signature."
        ),
    )

    @property
    def headers(self) -> dict[str, str]:
        """Generate HTTP headers dictionary for Douyin requests.

        Returns:
            Dictionary containing User-Agent, Referer, and Cookie headers
            formatted for use with HTTP clients.
        """
        return {
            "User-Agent": self.user_agent,
            "Referer": self.referer,
            "Cookie": self.cookie,
        }

    @property
    def proxies(self) -> dict[str, str | None]:
        """Generate proxy configuration dictionary.

        Returns:
            Dictionary containing HTTP and HTTPS proxy URLs,
            compatible with common HTTP client libraries.
        """
        return {"http://": self.proxy_http, "https://": self.proxy_https}

    model_config = SettingsConfigDict(env_prefix="DOUYIN_")


class WatchSettings(BaseSettings):
    """Watch-mode scheduler configuration settings.

    Controls the in-process watcher that polls subscribed Douyin users
    and auto-downloads new posts and livestreams. Every interval is a
    configurable default; per-subscription overrides supplied through the
    ``/watch`` API are validated against the same bounds.

    Attributes:
        live_poll_seconds: Default cadence for live-status checks. The
            60-second floor keeps aggressive polling from tripping
            Douyin's rate limiting / cookie risk controls.
        post_poll_seconds: Default cadence for new-post checks, kept much
            longer than the live cadence because posts are durable.
        recent_id_cap: Upper bound on the per-subscription ``aweme_id``
            dedupe set persisted in the checkpoint so the JSON blob stays
            small.
        max_subscriptions: Guardrail on the number of concurrent watch
            subscriptions; ``POST /watch`` is rejected past this count.
        backfill_on_create: Whether a brand-new subscription downloads
            the user's existing posts once before switching to
            incremental-only mode.

    Environment Variables:
        DOUYIN_WATCH_LIVE_POLL_SECONDS, DOUYIN_WATCH_POST_POLL_SECONDS,
        DOUYIN_WATCH_RECENT_ID_CAP, DOUYIN_WATCH_MAX_SUBSCRIPTIONS,
        DOUYIN_WATCH_BACKFILL_ON_CREATE.

    Note:
        ``DOUYIN_WATCH_`` is a distinct prefix from ``DouyinSettings``'
        ``DOUYIN_`` prefix. Pydantic resolves each settings class against
        its own declared field names, so the two never collide as long as
        ``DouyinSettings`` declares no ``watch_*`` field.
    """

    live_poll_seconds: int = Field(
        default=300,
        ge=60,
        le=3600,
        description="Default seconds between live-status checks (60s floor).",
    )
    post_poll_seconds: int = Field(
        default=2700,
        ge=300,
        le=86400,
        description="Default seconds between new-post checks.",
    )
    recent_id_cap: int = Field(
        default=200,
        ge=20,
        le=2000,
        description="Max aweme_id values kept in the per-subscription dedupe set.",
    )
    max_subscriptions: int = Field(
        default=50,
        ge=1,
        le=10000,
        description="Maximum number of concurrent watch subscriptions.",
    )
    backfill_on_create: bool = Field(
        default=False,
        description=(
            "Download a user's existing posts once when a subscription is "
            "created, instead of only fetching posts published afterwards."
        ),
    )

    model_config = SettingsConfigDict(env_prefix="DOUYIN_WATCH_")


class Settings(BaseSettings):
    """Composite settings container with nested configuration groups.

    Aggregates `RuntimeSettings`, `DatabaseSettings`, `R2Settings`,
    `DouyinSettings`, and `WatchSettings` so a single import gives
    access to every Pydantic-validated env-driven knob the engine
    reads. The model-level `_validate_production_placeholders`
    validator refuses to instantiate when `runtime.debug` is False
    and the database URL still matches the localhost default.

    Attributes:
        runtime: Debug flag.
        database: Postgres URL, pool strategy, janitor cadence, and
            operation retention.
        r2: Cloudflare R2 credentials and endpoint.
        douyin: Session cookie, headers, proxy, download root,
            livestream-specific HTTP headers, and the Argus webSign
            signing session.
        watch: Watch-mode polling cadences and subscription
            guardrails.

    Example:
        Standard access through the cached singleton::

            from dyvine.core.settings import settings

            if settings.debug:
                print(f"Database: {settings.database.url}")

        Override for tests by mutating the parsed instance::

            settings.database.url = "postgresql+asyncpg://test/test"

        Or via env vars in `.env` / the process environment::

            API_DEBUG=true
            DATABASE_URL=postgresql+asyncpg://...
            DOUYIN_COOKIE=your_cookie_here

    """

    # Define nested settings as fields
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    r2: R2Settings = Field(default_factory=R2Settings)
    douyin: DouyinSettings = Field(default_factory=DouyinSettings)
    watch: WatchSettings = Field(default_factory=WatchSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)

    # Root-level (unprefixed) flag. ``WATCH_ENABLED`` lives here rather
    # than under ``DOUYIN_WATCH_`` so the loop split reads as a
    # deployment concern, not a polling knob.
    watch_enabled: bool = Field(
        default=True,
        description=(
            "Run watch-subscription loops in this process. Processes "
            "that only serve CRUD set ``WATCH_ENABLED=false`` while a "
            "single watcher process runs the loops."
        ),
    )

    @model_validator(mode="after")
    def _validate_production_placeholders(self) -> Self:
        """Reject the localhost database default outside debug mode.

        The cross-field check lives on the composite container so the
        validator sees ``runtime.debug`` from the same parsed payload
        that populated the nested models. Reading ``API_DEBUG``
        straight off ``os.environ`` (the previous approach) silently
        disagreed with the parsed value whenever it lived in a
        ``.env`` file rather than a real environment variable.

        ``database.url`` is always validated: the localhost default is
        a development convenience, never a production target.
        """
        if self.runtime.debug:
            return self

        if self.database.url in {"", _DEFAULT_DATABASE_URL}:
            raise ValueError(
                "database.url must be set to a non-default value when "
                "API_DEBUG is false; rotate the placeholder before deploying."
            )
        return self

    # Convenience properties for frequently accessed settings
    @property
    def debug(self) -> bool:
        """Get debug mode status from runtime settings."""
        return self.runtime.debug

    @property
    def douyin_cookie(self) -> str:
        """Get Douyin cookie from Douyin settings."""
        return self.douyin.cookie

    @property
    def douyin_headers(self) -> dict[str, str]:
        """Get Douyin headers from Douyin settings."""
        return self.douyin.headers

    @property
    def douyin_proxies(self) -> dict[str, str | None]:
        """Get Douyin proxies from Douyin settings."""
        return self.douyin.proxies

    @property
    def douyin_user_agent(self) -> str:
        """Get Douyin user agent from Douyin settings."""
        return self.douyin.user_agent

    @property
    def douyin_referer(self) -> str:
        """Get Douyin referer from Douyin settings."""
        return self.douyin.referer

    @property
    def r2_account_id(self) -> str:
        """Get R2 account ID from R2 settings."""
        return self.r2.account_id

    @property
    def r2_access_key_id(self) -> str:
        """Get R2 access key ID from R2 settings."""
        return self.r2.access_key_id

    @property
    def r2_secret_access_key(self) -> str:
        """Get R2 secret access key from R2 settings."""
        return self.r2.secret_access_key

    @property
    def r2_bucket_name(self) -> str:
        """Get R2 bucket name from R2 settings."""
        return self.r2.bucket_name

    @property
    def r2_endpoint(self) -> str:
        """Get R2 endpoint from R2 settings."""
        return self.r2.endpoint

    # No ``case_sensitive`` here: pydantic-settings matches env names
    # against field names exactly when it is set, which would require a
    # lowercase ``watch_enabled`` variable for the root flag below. Each
    # nested group carries its own prefixed config, so root-level
    # case-insensitivity cannot collide with them.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance with environment variables loaded.

    This function creates and caches a Settings instance, automatically
    loading environment variables from .env files and system environment.
    The instance is cached to avoid repeated initialization overhead.

    Returns:
        Fully configured Settings instance with all nested configurations
        loaded from environment variables and defaults.

    Example:
        from dyvine.core.settings import get_settings

        settings = get_settings()
        print(f"Debug: {settings.debug}")
    """
    from dotenv import load_dotenv

    load_dotenv()
    return Settings()


# Global settings instance for convenient access throughout the application
settings = get_settings()
