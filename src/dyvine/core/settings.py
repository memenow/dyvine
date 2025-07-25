"""Settings management for Dyvine.

This module provides centralized configuration management for the Dyvine application
using Pydantic Settings with environment variable support and validation.

The settings are organized into logical groups:
- APISettings: Core API configuration
- SecuritySettings: Authentication and security settings  
- R2Settings: Cloudflare R2 storage configuration
- DouyinSettings: Douyin platform-specific settings

Example:
    Basic usage:
        from dyvine.core.settings import settings
        
        if settings.debug:
            print(f"Running {settings.project_name} v{settings.version}")

    Environment variables:
        API_DEBUG=true
        DOUYIN_COOKIE=your_cookie_here
        R2_BUCKET_NAME=your_bucket_name
"""

from functools import lru_cache
from typing import Dict, List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
        default=10, ge=1, description="API rate limiting threshold per second"
    )
    cors_origins: List[str] = Field(
        default=["*"], description="List of allowed CORS origins"
    )

    model_config = SettingsConfigDict(env_prefix="API_")


class SecuritySettings(BaseSettings):
    """Security and authentication configuration settings.

    Contains security-related settings including API keys, tokens, and
    authentication parameters with production safety validations.

    Attributes:
        secret_key: Secret key for cryptographic operations.
        api_key: API authentication key.
        access_token_expire_minutes: JWT token expiration time in minutes.

    Environment Variables:
        SECURITY_SECRET_KEY: Override default secret key.
        SECURITY_API_KEY: Override default API key.
        SECURITY_ACCESS_TOKEN_EXPIRE_MINUTES: Token expiration time.

    Note:
        Default values must be changed in production environments.
        The validator will raise an error if defaults are used in non-debug mode.
    """

    secret_key: str = Field(
        default="change-me-in-production",
        description="Secret key for cryptographic operations",
    )
    api_key: str = Field(
        default="change-me-in-production", description="API authentication key"
    )
    access_token_expire_minutes: int = Field(
        default=60, ge=1, description="JWT token expiration time in minutes"
    )

    @field_validator("secret_key", "api_key")
    def validate_not_default(cls, v: str, info) -> str:
        """Validate that production secrets are not using default values.

        Args:
            v: The field value to validate.
            info: Field information from pydantic.

        Returns:
            The validated field value.

        Raises:
            ValueError: If default values are used in production.
        """
        import os

        if (
            v == "change-me-in-production"
            and os.getenv("API_DEBUG", "true").lower() != "true"
        ):
            raise ValueError(f"{info.field_name} must be changed in production")
        return v

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

    Environment Variables:
        DOUYIN_COOKIE: Authentication cookie string.
        DOUYIN_USER_AGENT: Custom User-Agent header.
        DOUYIN_REFERER: Custom Referer header.
        DOUYIN_PROXY_HTTP: HTTP proxy URL.
        DOUYIN_PROXY_HTTPS: HTTPS proxy URL.

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
    proxy_http: Optional[str] = Field(
        default=None, description="HTTP proxy URL (optional)"
    )
    proxy_https: Optional[str] = Field(
        default=None, description="HTTPS proxy URL (optional)"
    )

    @property
    def headers(self) -> Dict[str, str]:
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
    def proxies(self) -> Dict[str, Optional[str]]:
        """Generate proxy configuration dictionary.

        Returns:
            Dictionary containing HTTP and HTTPS proxy URLs,
            compatible with common HTTP client libraries.
        """
        return {"http://": self.proxy_http, "https://": self.proxy_https}

    model_config = SettingsConfigDict(env_prefix="DOUYIN_")


class Settings(BaseSettings):
    """Composite settings container with nested configuration groups.

    This is the main settings class that combines all configuration groups
    into a single, easy-to-use interface. It automatically initializes all
    nested settings and provides convenient property access to frequently
    used configuration values.

    Attributes:
        api: API server configuration settings.
        security: Security and authentication settings.
        r2: Cloudflare R2 storage settings.
        douyin: Douyin platform-specific settings.

    Example:
        Basic usage:
            from dyvine.core.settings import settings

            # Access nested settings
            print(f"Server running on {settings.api.host}:{settings.api.port}")

            # Use convenience properties
            if settings.debug:
                print("Debug mode enabled")

        Environment file:
            The settings automatically load from .env file if present:
                API_DEBUG=true
                API_PORT=8080
                DOUYIN_COOKIE=your_cookie_here
    """

    # Define nested settings as fields
    api: APISettings = Field(default_factory=APISettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    r2: R2Settings = Field(default_factory=R2Settings)
    douyin: DouyinSettings = Field(default_factory=DouyinSettings)

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
    def cors_origins(self) -> List[str]:
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
    def secret_key(self) -> str:
        """Get secret key from security settings."""
        return self.security.secret_key

    @property
    def api_key(self) -> str:
        """Get API key from security settings."""
        return self.security.api_key

    @property
    def douyin_cookie(self) -> str:
        """Get Douyin cookie from Douyin settings."""
        return self.douyin.cookie

    @property
    def douyin_headers(self) -> Dict[str, str]:
        """Get Douyin headers from Douyin settings."""
        return self.douyin.headers

    @property
    def douyin_proxies(self) -> Dict[str, Optional[str]]:
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
    def douyin_proxy_http(self) -> Optional[str]:
        """Get HTTP proxy from Douyin settings."""
        return self.douyin.proxy_http

    @property
    def douyin_proxy_https(self) -> Optional[str]:
        """Get HTTPS proxy from Douyin settings."""
        return self.douyin.proxy_https

    model_config = SettingsConfigDict(
        env_file=".env", case_sensitive=True, extra="ignore"
    )


@lru_cache()
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
