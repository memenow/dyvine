"""Configuration management for the Dyvine API.

This module handles all configuration settings for the application, including
API settings, server configuration, authentication, and Douyin-specific settings.
It uses environment variables and .env file for configuration.
"""

from pydantic_settings import BaseSettings
from typing import List, Optional, Dict, Any, Generator
from dotenv import load_dotenv
import os
from f2.apps.douyin.handler import DouyinHandler

# Load .env file
load_dotenv()

class Settings(BaseSettings):
    """Application settings management.
    
    This class handles all configuration settings for the application.
    It uses Pydantic's BaseSettings for automatic environment variable loading
    and validation.
    
    Attributes:
        API_V1_STR: API version prefix string.
        PROJECT_NAME: Name of the project.
        DEBUG: Debug mode flag.
        HOST: Server host address.
        PORT: Server port number.
        CORS_ORIGINS: List of allowed CORS origins.
        SECRET_KEY: Secret key for security.
        API_KEY: API key for authentication.
        ACCESS_TOKEN_EXPIRE_MINUTES: Token expiration time.
        RATE_LIMIT_PER_SECOND: API rate limit.
        DOUYIN_COOKIE: Douyin authentication cookie.
        DOUYIN_USER_AGENT: User agent for Douyin requests.
        DOUYIN_REFERER: Referer for Douyin requests.
        DOUYIN_PROXY_HTTP: HTTP proxy settings.
        DOUYIN_PROXY_HTTPS: HTTPS proxy settings.
    """
    # API Settings
    API_V1_STR: str = "/api/v1"
    PROJECT_NAME: str = "Douyin API"
    DEBUG: bool = False
    
    # Server Settings
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    
    # CORS Settings
    CORS_ORIGINS: List[str] = ["*"]
    
    # Auth Settings 
    SECRET_KEY: str = "default-secret-key-please-change-in-production"
    API_KEY: str = "default-api-key-please-change-in-production"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    
    # Rate Limiting
    RATE_LIMIT_PER_SECOND: int = 10
    
    # Douyin Settings
    DOUYIN_COOKIE: str = ""
    DOUYIN_USER_AGENT: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    DOUYIN_REFERER: str = "https://www.douyin.com/"
    DOUYIN_PROXY_HTTP: Optional[str] = None
    DOUYIN_PROXY_HTTPS: Optional[str] = None

    @property
    def DOUYIN_HEADERS(self) -> Dict[str, str]:
        """Generate headers for Douyin requests.
        
        Returns:
            Dict[str, str]: Headers including User-Agent, Referer, and Cookie.
        """
        return {
            "User-Agent": self.DOUYIN_USER_AGENT,
            "Referer": self.DOUYIN_REFERER,
            "Cookie": self.DOUYIN_COOKIE
        }
    
    @property
    def DOUYIN_PROXIES(self) -> Dict[str, Optional[str]]:
        """Generate proxy configuration for Douyin requests.
        
        Returns:
            Dict[str, Optional[str]]: Proxy configuration for HTTP and HTTPS.
        """
        return {
            "http://": self.DOUYIN_PROXY_HTTP,
            "https://": self.DOUYIN_PROXY_HTTPS
        }

    class Config:
        case_sensitive = True
        env_file = ".env"

settings = Settings()

# Add warning if using default keys
if settings.API_KEY == "default-api-key-please-change-in-production":
    import warnings
    warnings.warn("Using default API key! Please set API_KEY in your .env file for production.")

# Add dependency injection functions
def get_douyin_handler() -> Generator[DouyinHandler, None, None]:
    """Create and yield a configured DouyinHandler instance.
    
    Returns:
        Generator yielding a DouyinHandler configured with application settings.
    """
    kwargs = {
        "headers": settings.DOUYIN_HEADERS,
        "proxies": settings.DOUYIN_PROXIES,
        "mode": "post",
        "cookie": settings.DOUYIN_COOKIE,
        "path": "downloads",
        "max_retries": 5,
        "timeout": 30,
        "chunk_size": 1024 * 1024,
        "max_tasks": 3,
        "folderize": True,
        "download_image": True,
        "download_video": True
    }
    handler = DouyinHandler(kwargs)
    try:
        yield handler 
    finally:
        pass
