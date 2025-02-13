"""Core settings management for the Dyvine package.

This module provides configuration management through environment variables,
handling API, security, and Douyin-specific settings.
"""

from typing import Dict, Optional, List
from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache


class Settings(BaseSettings):
    """Application settings management.

    Handles all configuration settings including API, security, and
    Douyin-specific settings through environment variables.

    All settings can be configured through environment variables or .env file.
    See .env.example for available settings and their default values.
    """

    # API Settings
    version: str = "1.0.0"
    prefix: str = "/api/v1"
    project_name: str = "Dyvine API"
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 8000
    rate_limit_per_second: int = 10
    cors_origins: List[str] = ["*"]

    # Security Settings
    secret_key: str = "default-secret-key-please-change-in-production"
    api_key: str = "default-api-key-please-change-in-production"
    access_token_expire_minutes: int = 60

    # Cloudflare R2 Settings
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket_name: str = ""
    r2_endpoint: str = ""  # e.g. https://<account_id>.r2.cloudflarestorage.com
    
    # Douyin Settings
    douyin_cookie: str = ""
    douyin_user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    douyin_referer: str = "https://www.douyin.com/"
    douyin_proxy_http: Optional[str] = None
    douyin_proxy_https: Optional[str] = None

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=True,
        env_prefix="",
        extra="allow"
    )

    @property
    def douyin_headers(self) -> Dict[str, str]:
        """Generate headers for Douyin requests.

        Returns:
            Dict[str, str]: Headers including User-Agent, Referer, and Cookie.
        """
        return {
            "User-Agent": self.douyin_user_agent,
            "Referer": self.douyin_referer,
            "Cookie": self.douyin_cookie
        }

    @property
    def douyin_proxies(self) -> Dict[str, Optional[str]]:
        """Generate proxy configuration.

        Returns:
            Dict[str, Optional[str]]: Proxy configuration for HTTP and HTTPS.
        """
        return {
            "http://": self.douyin_proxy_http,
            "https://": self.douyin_proxy_https
        }

@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance.

    Returns:
        Settings: Global settings instance.
    """
    return Settings()

# Global settings instance
settings = get_settings()
