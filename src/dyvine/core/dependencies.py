"""Dependency injection and service initialization for Dyvine.

This module implements a service container pattern for managing application
dependencies and their lifecycles. It provides a centralized location for
service initialization and configuration, making the application more
testable and maintainable.

The module provides:
- ServiceContainer: Main container for managing service instances
- Dependency providers: FastAPI-compatible dependency functions
- Service configuration: Centralized service setup and initialization

Example:
    Using dependency injection in FastAPI routes:
        from fastapi import Depends
        from dyvine.core.dependencies import get_user_service

        @router.get("/users/{user_id}")
        async def get_user(
            user_id: str,
            user_service: UserService = Depends(get_user_service)
        ):
            return await user_service.get_user(user_id)

    Direct service access:
        from dyvine.core.dependencies import get_service_container

        container = get_service_container()
        user_service = container.user_service
"""

from functools import lru_cache
from typing import Any

from f2.apps.douyin.handler import DouyinHandler

from dyvine.core.settings import settings
from dyvine.services.users import UserService


class ServiceContainer:
    """Service container for dependency injection and lifecycle management.

    This class implements the service container pattern, providing centralized
    management of application services and their dependencies. Services are
    lazily initialized and cached for reuse throughout the application lifecycle.

    Attributes:
        _services: Internal dictionary storing initialized service instances.
        _initialized: Flag indicating whether the container has been initialized.

    Example:
        Basic usage:
            container = ServiceContainer()
            container.initialize()

            # Access services via properties
            douyin_handler = container.douyin_handler
            user_service = container.user_service

        Custom service registration:
            container = ServiceContainer()
            custom_service = MyCustomService()
            container._services['custom'] = custom_service
    """

    def __init__(self) -> None:
        """Initialize empty service container.

        Services are not initialized until initialize() is called explicitly
        or accessed via property methods.
        """
        self._services: dict[str, Any] = {}
        self._initialized = False

    def initialize(self) -> None:
        """Initialize all registered services with their configurations.

        This method sets up all core application services with proper
        configuration. It's idempotent - calling it multiple times has
        no additional effect.

        Services initialized:
            - DouyinHandler: Configured with headers, proxies, and download settings
            - UserService: Basic user management service

        Note:
            This method is automatically called when services are accessed
            via property methods, but can be called explicitly for eager
            initialization.
        """
        if self._initialized:
            return

        # Initialize Douyin handler with configuration
        douyin_config = self._create_douyin_config()
        self._services['douyin_handler'] = DouyinHandler(douyin_config)

        # Initialize user service
        self._services['user_service'] = UserService()

        self._initialized = True

    def _create_douyin_config(self) -> dict[str, Any]:
        """Create Douyin handler configuration from application settings.

        Builds a configuration dictionary for the DouyinHandler based on
        current application settings including authentication, proxy settings,
        and download preferences.

        Returns:
            Dictionary containing all DouyinHandler configuration parameters
            including headers, proxies, download settings, and file naming rules.
        """
        return {
            "headers": settings.douyin.headers,
            "proxies": settings.douyin.proxies,
            "mode": "all",
            "cookie": settings.douyin.cookie,
            "path": "downloads",
            "max_retries": 5,
            "timeout": 30,
            "chunk_size": 1024 * 1024,
            "max_tasks": 3,
            "folderize": True,
            "download_image": True,
            "download_video": True,
            "download_live": True,
            "download_collection": True,
            "download_story": True,
            "naming": "{create}_{desc}",
            "page_counts": 100,
        }

    def get_service(self, service_name: str) -> Any:
        """Get a service instance by name.

        Retrieves a registered service instance, initializing the container
        if not already done. Returns None if the service is not registered.

        Args:
            service_name: Name of the service to retrieve.

        Returns:
            Service instance if found, None otherwise.

        Example:
            container = ServiceContainer()
            user_service = container.get_service('user_service')
        """
        if not self._initialized:
            self.initialize()
        return self._services.get(service_name)

    @property
    def douyin_handler(self) -> DouyinHandler:
        """Get the configured Douyin handler service.

        Returns:
            DouyinHandler instance configured with application settings.
        """
        return self.get_service('douyin_handler')

    @property
    def user_service(self) -> UserService:
        """Get the user management service.

        Returns:
            UserService instance for user-related operations.
        """
        return self.get_service("user_service")


@lru_cache
def get_service_container() -> ServiceContainer:
    """Get cached service container instance.

    Returns a singleton ServiceContainer instance, creating it on first
    access and caching it for subsequent calls. This ensures consistent
    service instances throughout the application lifecycle.

    Returns:
        ServiceContainer singleton instance.

    Example:
        # Both calls return the same instance
        container1 = get_service_container()
        container2 = get_service_container()
        assert container1 is container2
    """
    return ServiceContainer()


# FastAPI dependency provider functions
def get_douyin_handler() -> DouyinHandler:
    """FastAPI dependency provider for Douyin handler service.

    This function provides a DouyinHandler instance for FastAPI dependency
    injection. It can be used with the Depends() function in route handlers.

    Returns:
        Configured DouyinHandler instance.

    Example:
        from fastapi import Depends

        @router.get("/posts/{post_id}")
        async def get_post(
            post_id: str,
            handler: DouyinHandler = Depends(get_douyin_handler)
        ):
            return await handler.get_post(post_id)
    """
    return get_service_container().douyin_handler


def get_user_service() -> UserService:
    """FastAPI dependency provider for user service.

    This function provides a UserService instance for FastAPI dependency
    injection. It can be used with the Depends() function in route handlers.

    Returns:
        UserService instance.

    Example:
        from fastapi import Depends

        @router.get("/users/{user_id}")
        async def get_user(
            user_id: str,
            service: UserService = Depends(get_user_service)
        ):
            return await service.get_user(user_id)
    """
    return get_service_container().user_service
