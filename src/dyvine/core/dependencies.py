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

from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from typing import Any

from f2.apps.douyin.handler import DouyinHandler  # type: ignore

from ..services.livestreams import LivestreamService
from ..services.users import UserService
from .background import BackgroundTaskRegistry
from .operations import OperationStore
from .settings import settings

# Dedicated thread pool sizes per IO domain. The defaults are tuned for the
# single-worker uvicorn deployment: R2 uploads are the dominant long-running
# call, sqlite writes are short but frequent, and audit log writes are rare
# but must never starve the other two pools. Keeping each domain in its own
# bounded pool prevents a burst in one from exhausting the default asyncio
# executor (``min(32, cpu+4)``) that every ``asyncio.to_thread`` call would
# otherwise share.
R2_EXECUTOR_MAX_WORKERS = 16
SQLITE_EXECUTOR_MAX_WORKERS = 4
AUDIT_EXECUTOR_MAX_WORKERS = 2


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
        self._r2_executor: ThreadPoolExecutor | None = None
        self._sqlite_executor: ThreadPoolExecutor | None = None
        self._audit_executor: ThreadPoolExecutor | None = None
        # Shared registry for long-lived background downloads. Services
        # retrieve this via dependency injection and call ``spawn`` instead
        # of bare ``asyncio.create_task`` so the lifespan can drain them
        # before the executor pools are reaped.
        self._background_tasks = BackgroundTaskRegistry()

    async def initialize(self) -> None:
        """Initialize all registered services with their configurations.

        Coroutine because ``OperationStore`` recovery uses async sqlite IO
        (``mark_incomplete_operations_failed`` dispatches to a worker
        thread). Safe to ``await`` multiple times; subsequent calls are
        no-ops.

        Services initialized:
            - DouyinHandler: Configured with headers, proxies, and download settings
            - OperationStore: Persistent state for asynchronous work
            - UserService: Basic user management service
            - LivestreamService: Livestream download orchestration

        Executors created:
            - ``r2_executor`` (16 workers): R2 upload/head/delete/list
            - ``sqlite_executor`` (4 workers): OperationStore writes/reads
            - ``audit_executor`` (2 workers): LifecycleManager audit writes

        Note:
            This method is awaited by the FastAPI lifespan. Direct access
            through the property methods still works but now raises if the
            container has not been initialized yet.
        """
        if self._initialized:
            return

        # Create dedicated thread pool executors for each IO domain before
        # instantiating any service that might need one. The thread-name
        # prefix shows up in logs/traces so hot threads are easy to spot.
        self._r2_executor = ThreadPoolExecutor(
            max_workers=R2_EXECUTOR_MAX_WORKERS,
            thread_name_prefix="dyvine-r2",
        )
        self._sqlite_executor = ThreadPoolExecutor(
            max_workers=SQLITE_EXECUTOR_MAX_WORKERS,
            thread_name_prefix="dyvine-sqlite",
        )
        self._audit_executor = ThreadPoolExecutor(
            max_workers=AUDIT_EXECUTOR_MAX_WORKERS,
            thread_name_prefix="dyvine-audit",
        )

        # Initialize Douyin handler with configuration
        douyin_config = self._create_douyin_config()
        self._services["douyin_handler"] = DouyinHandler(douyin_config)

        # Initialize operation store (sqlite bootstrap happens synchronously
        # inside the constructor, which is cheap and keeps the OperationStore
        # usable from both async and sync contexts). The dedicated sqlite
        # executor is attached immediately so the recovery sweep below
        # already runs on the bounded pool.
        operation_store = OperationStore(executor=self._sqlite_executor)
        self._services["operation_store"] = operation_store
        await operation_store.mark_incomplete_operations_failed()

        # Initialize user service and wire its R2 client to the R2 executor.
        user_service = UserService(
            operation_store=operation_store,
            task_registry=self._background_tasks,
        )
        user_service.storage.set_executor(self._r2_executor)
        self._services["user_service"] = user_service

        # Initialize livestream service
        self._services["livestream_service"] = LivestreamService(
            douyin_handler=self._services["douyin_handler"],
            user_service=user_service,
            operation_store=operation_store,
            task_registry=self._background_tasks,
        )

        self._initialized = True

    async def shutdown(self) -> None:
        """Release services and tear down the dedicated executor pools.

        Called from the FastAPI lifespan's shutdown branch so that the
        worker threads held by each ``ThreadPoolExecutor`` do not outlive
        the application. Safe to call multiple times; subsequent calls are
        no-ops.

        Each executor is shut down with ``wait=True`` so pending work (e.g.
        a final audit-log write) drains before the process exits. Executors
        are shut down in reverse initialization order so downstream
        dependencies finish before their producers go away.
        """
        if not self._initialized:
            return

        # Drain fire-and-forget downloads before tearing down the executor
        # pools they dispatch onto. Any task still running after the
        # registry's drain timeout is cancelled so the shutdown cannot hang
        # on a stuck upstream request.
        await self._background_tasks.drain()

        # Let the operation store close its per-thread reader connections
        # before we reap the sqlite executor that owns those worker threads.
        operation_store = self._services.get("operation_store")
        if isinstance(operation_store, OperationStore):
            operation_store.shutdown()

        for attr in ("_audit_executor", "_sqlite_executor", "_r2_executor"):
            executor = getattr(self, attr)
            if executor is not None:
                executor.shutdown(wait=True)
                setattr(self, attr, None)

        self._services.clear()
        self._initialized = False

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

        The container must have been initialized before any service is
        requested. ``initialize`` is a coroutine (it awaits SQLite recovery
        in the operation store), so synchronous access has no safe way to
        self-heal. The FastAPI lifespan awaits ``initialize`` before any
        request can reach a dependency, so this only fires when tests or
        ad-hoc scripts forget to bootstrap.

        Args:
            service_name: Name of the service to retrieve.

        Returns:
            Service instance if found, None otherwise.

        Raises:
            RuntimeError: If ``initialize`` has not been awaited yet.
        """
        if not self._initialized:
            raise RuntimeError(
                "ServiceContainer has not been initialized; await "
                "container.initialize() (normally via the FastAPI lifespan) "
                "before requesting services."
            )
        return self._services.get(service_name)

    @property
    def douyin_handler(self) -> DouyinHandler:
        """Get the configured Douyin handler service.

        Returns:
            DouyinHandler instance configured with application settings.
        """
        return self.get_service("douyin_handler")

    @property
    def user_service(self) -> UserService:
        """Get the user management service.

        Returns:
            UserService instance for user-related operations.
        """
        service = self.get_service("user_service")
        if not isinstance(service, UserService):
            raise TypeError("user_service is not a UserService instance")
        return service

    @property
    def operation_store(self) -> OperationStore:
        """Get the persistent operation store."""
        service = self.get_service("operation_store")
        if not isinstance(service, OperationStore):
            raise TypeError("operation_store is not an OperationStore instance")
        return service

    @property
    def livestream_service(self) -> LivestreamService:
        """Get the livestream management service."""
        service = self.get_service("livestream_service")
        if not isinstance(service, LivestreamService):
            raise TypeError("livestream_service is not a LivestreamService instance")
        return service


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


def get_livestream_service() -> LivestreamService:
    """FastAPI dependency provider for livestream service."""
    return get_service_container().livestream_service
