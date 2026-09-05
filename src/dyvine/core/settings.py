"""Composite Pydantic settings for Dyvine.

The composite `Settings` aggregates four `BaseSettings` subclasses,
each scoped by a distinct environment-variable prefix:

- `APISettings` (`API_`) — server, CORS, operation DB path.
- `SecuritySettings` (`SECURITY_`) — API key and gating flag.
- `R2Settings` (`R2_`) — Cloudflare R2 credentials and endpoint.
- `DouyinSettings` (`DOUYIN_`) — session cookie, headers, proxy,
  download root, and livestream-specific HTTP headers.

A model-level validator (`_validate_security_in_production`) refuses
to instantiate the container when `api.debug` is `false` and
`security.api_key` (when `require_api_key` is on) still matches the
placeholder `change-me-in-production` sentinel. The cross-field check
lives on the composite class so the
validator sees the parsed payload rather than reading `os.environ`
directly, which used to silently disagree with `.env`-supplied
values.

`get_settings()` is `lru_cache`d and loads `.env` at first call.
Tests reset the cache via `tests/conftest.py::reset_singletons` so
each test sees pristine settings.

Convenience properties (`debug`, `version`, `prefix`, the legacy
`douyin_*` accessors) keep older call sites working without forcing
them through the nested `settings.api.*` / `settings.douyin.*`
addresses.
"""

from functools import lru_cache
from typing import Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_SECRET_SENTINEL = "change-me-in-production"


class APISettings(BaseSettings):
    """API server configuration settings.

    Contains all settings related to the FastAPI server configuration
    including host, port, debugging, and CORS settings.

    Attributes:
        version: Application version string.
        prefix: API URL prefix (e.g., '/api/v1').
        project_name: Human-readable project name.
        debug: Enable debug mode with verbose logging.
        host: Server bind address.
        port: Server bind port (1-65535).
        rate_limit_per_second: API rate limiting threshold.
        cors_origins: List of allowed CORS origins.

    Environment Variables:
        All attributes can be configured via environment variables with
        the 'API_' prefix (e.g., API_DEBUG, API_PORT).

    """

    version: str = Field(default="1.0.0", description="Application version string")
    prefix: str = Field(default="/api/v1", description="API URL prefix")
    project_name: str = Field(
        default="Dyvine API", description="Human-readable project name"
    )
    debug: bool = Field(
        default=False, description="Enable debug mode with verbose logging"
    )
    host: str = Field(default="0.0.0.0", description="Server bind address")
    port: int = Field(default=8000, ge=1, le=65535, description="Server bind port")
    rate_limit_per_second: int = Field(
        default=10,
        ge=1,
        description=(
            "Sustained request budget per caller per second, enforced "
            "per replica by the token-bucket middleware. Buckets key on "
            "the X-API-Key header when present, else the client IP."
        ),
    )
    rate_limit_burst: int = Field(
        default=20,
        ge=1,
        description=(
            "Maximum tolerated burst per caller before 429s. Buckets "
            "refill at rate_limit_per_second; probes, /metrics, and / "
            "never consume budget."
        ),
    )
    multi_replica: bool = Field(
        default=False,
        description=(
            "More than one API replica serves traffic behind a shared "
            "Postgres database. Boot refuses to complete unless downloads "
            "can survive pod boundaries: either R2 archival is configured "
            "or ``shared_file_storage`` confirms a shared volume."
        ),
    )
    shared_file_storage: bool = Field(
        default=False,
        description=(
            "``DOUYIN_DOWNLOAD_ROOT`` is backed by storage every replica "
            "can see (e.g. a ReadWriteMany volume). Only consulted when "
            "``multi_replica`` is true and R2 is unconfigured."
        ),
    )
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:3000"],
        description=(
            "Allowed browser origins. Defaults to localhost development; "
            "production deployments must replace this with an explicit "
            'allowlist via the ``API_CORS_ORIGINS`` env var. ``["*"]`` is '
            "accepted but disables credentialed CORS automatically (see "
            "``main.py`` middleware)."
        ),
    )
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
        pool_size: Steady-state pooled connections per process.
        pool_timeout: Seconds to wait for a pooled connection.
        operation_retention_days: Terminal operation rows older than
            this are purged at boot and daily; ``0`` disables purging.

    Environment Variables:
        DATABASE_URL, DATABASE_POOL_SIZE, DATABASE_POOL_TIMEOUT,
        DATABASE_OPERATION_RETENTION_DAYS.
    """

    url: str = Field(
        default=_DEFAULT_DATABASE_URL,
        description="SQLAlchemy database URL for operation/watch state",
    )
    pool_size: int = Field(
        default=5, ge=1, description="Steady-state pooled DB connections"
    )
    pool_timeout: float = Field(
        default=30.0, ge=1.0, description="Seconds to wait for a DB connection"
    )
    operation_retention_days: int = Field(
        default=30,
        ge=0,
        description="Purge terminal operations older than this (0 disables)",
    )

    model_config = SettingsConfigDict(env_prefix="DATABASE_")


class SecuritySettings(BaseSettings):
    """Security and authentication configuration settings.

    Attributes:
        api_key: API authentication key. Required in production unless
            ``require_api_key`` is explicitly set to ``False``.
        require_api_key: When ``True`` (the default), every router request
            must carry the ``X-API-Key`` header set to ``api_key``.

    Environment Variables:
        SECURITY_API_KEY, SECURITY_REQUIRE_API_KEY.

    Note:
        Default values must be replaced before any production deployment.
        The composite :class:`Settings` validator below cross-checks the
        secret value against ``API_DEBUG`` so a non-debug build that ships
        with the placeholder secret fails to boot.
    """

    api_key: str = Field(
        default=_DEFAULT_SECRET_SENTINEL,
        description="API authentication key matched against ``X-API-Key``",
    )
    require_api_key: bool = Field(
        default=True,
        description=(
            "Reject router requests that do not present a matching "
            "``X-API-Key`` header. Set to ``false`` only when fronting the "
            "API with another authenticated layer (mTLS, mesh policy)."
        ),
    )

    model_config = SettingsConfigDict(env_prefix="SECURITY_")


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

    Environment Variables:
        DOUYIN_COOKIE, DOUYIN_USER_AGENT, DOUYIN_REFERER,
        DOUYIN_PROXY_HTTP, DOUYIN_PROXY_HTTPS, DOUYIN_DOWNLOAD_ROOT,
        DOUYIN_RETAIN_LOCAL_DOWNLOADS, DOUYIN_RETAIN_MAX_GB.

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

    Aggregates `APISettings`, `SecuritySettings`, `R2Settings`, and
    `DouyinSettings` so a single import gives access to every Pydantic-
    validated env-driven knob the application reads. The model-level
    `_validate_security_in_production` validator refuses to instantiate
    when `api.debug` is False and a placeholder secret remains.

    Attributes:
        api: Server, CORS, and operation DB configuration.
        security: Secret + API keys and the `require_api_key` gate.
        r2: Cloudflare R2 credentials and endpoint.
        douyin: Session cookie, headers, proxy, download root, and
            livestream-specific HTTP headers.

    Example:
        Standard access through the cached singleton::

            from dyvine.core.settings import settings

            if settings.debug:
                print(f"Running {settings.project_name} v{settings.version}")
                print(f"Listening on {settings.api.host}:{settings.api.port}")

        Override for tests by mutating the parsed instance::

            settings.api.port = 8080

        Or via env vars in `.env` / the process environment::

            API_DEBUG=true
            API_PORT=8080
            DOUYIN_COOKIE=your_cookie_here

    """

    # Define nested settings as fields
    api: APISettings = Field(default_factory=APISettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    r2: R2Settings = Field(default_factory=R2Settings)
    douyin: DouyinSettings = Field(default_factory=DouyinSettings)
    watch: WatchSettings = Field(default_factory=WatchSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)

    # Root-level (unprefixed) flags. ``WATCH_ENABLED`` lives here rather
    # than under ``DOUYIN_WATCH_`` so the API/watcher split reads as a
    # deployment concern, not a polling knob.
    watch_enabled: bool = Field(
        default=True,
        description=(
            "Run watch-subscription loops in this process. API replicas "
            "set ``WATCH_ENABLED=false`` (CRUD-only against shared "
            "Postgres) while a single watcher replica runs the loops."
        ),
    )

    @model_validator(mode="after")
    def _validate_security_in_production(self) -> Self:
        """Reject placeholder production values when ``api.debug`` is False.

        The cross-field check lives on the composite container so the
        validator sees ``api.debug`` from the same parsed payload that
        populated the nested models. Reading ``API_DEBUG`` straight off
        ``os.environ`` (the previous approach) silently disagreed with
        ``api.debug`` whenever the value lived in a ``.env`` file rather
        than a real environment variable.

        ``api_key`` is only validated when ``require_api_key`` is on, so
        deployments that delegate authentication to mTLS or a service
        mesh (and therefore set ``SECURITY_REQUIRE_API_KEY=false``) do
        not need to mint a never-used key just to satisfy a startup
        check. ``database.url`` is always validated: the localhost
        default is a development convenience, never a production target.

        """
        if self.api.debug:
            return self

        offenders: list[str] = []
        if self.security.require_api_key and self.security.api_key in {
            "",
            _DEFAULT_SECRET_SENTINEL,
        }:
            offenders.append("security.api_key")
        if self.database.url in {"", _DEFAULT_DATABASE_URL}:
            offenders.append("database.url")
        if offenders:
            joined = ", ".join(offenders)
            raise ValueError(
                f"{joined} must be set to non-default values when API_DEBUG "
                "is false; rotate the placeholders before deploying."
            )
        return self

    # Convenience properties for frequently accessed settings
    @property
    def debug(self) -> bool:
        """Get debug mode status from API settings."""
        return self.api.debug

    @property
    def version(self) -> str:
        """Get application version from API settings."""
        return self.api.version

    @property
    def prefix(self) -> str:
        """Get API URL prefix from API settings."""
        return self.api.prefix

    @property
    def project_name(self) -> str:
        """Get human-readable project name from API settings."""
        return self.api.project_name

    @property
    def cors_origins(self) -> list[str]:
        """Get CORS allowed origins from API settings."""
        return self.api.cors_origins

    # Backward compatibility properties for legacy code
    @property
    def host(self) -> str:
        """Get server host from API settings."""
        return self.api.host

    @property
    def port(self) -> int:
        """Get server port from API settings."""
        return self.api.port

    @property
    def api_key(self) -> str:
        """Get API key from security settings."""
        return self.security.api_key

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

    @property
    def douyin_proxy_http(self) -> str | None:
        """Get HTTP proxy from Douyin settings."""
        return self.douyin.proxy_http

    @property
    def douyin_proxy_https(self) -> str | None:
        """Get HTTPS proxy from Douyin settings."""
        return self.douyin.proxy_https

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
        print(f"Running {settings.project_name} v{settings.version}")
    """
    from dotenv import load_dotenv

    load_dotenv()
    return Settings()


# Global settings instance for convenient access throughout the application
settings = get_settings()
