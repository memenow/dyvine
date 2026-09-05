"""Dependency injection and service container.

`ServiceContainer` owns the long-lived runtime state of the application:

- A `DouyinHandler` configured from the composite `Settings`.
- A Postgres-backed `OperationRepository` / `WatchRepository` pair
  sharing one `DatabaseSessionFactory` (asyncpg pool), plus a
  `RepositoryJanitor` task that heartbeats owned rows, sweeps
  orphans, and purges terminal rows.
- A `UserService`, `PostService`, and `LivestreamService` that all
  share the same operation repository and `BackgroundTaskRegistry`.
- An `R2StorageService` (under `UserService`) attached to two
  separate executors: a 16-worker `r2_executor` for upload / head /
  delete / list operations, and a 16-worker `r2_head_executor` for
  the per-key `head_object` fan-out triggered inside
  `_list_objects_sync`. A 2-worker `audit_executor` is provisioned
  for `LifecycleManager` audit writes (the manager itself is not yet
  wired into the runtime container).

`initialize` is awaited from the FastAPI lifespan; `shutdown` drains
the `BackgroundTaskRegistry`, stops the janitor, disposes the
database pool, and reaps every executor in reverse initialisation
order so a graceful shutdown never tears shared state down before
in-flight work finishes.

`require_api_key` (also exported here) is the FastAPI dependency
mounted at every router; it uses `hmac.compare_digest` and short-
circuits when `SECURITY_REQUIRE_API_KEY=false`.

Example:
    `get_user_service` and friends are FastAPI dependency providers::

        @router.get("/users/{user_id}")
        async def get_user(
            user_id: str,
            service: UserService = Depends(get_user_service),
        ):
            return await service.get_user_info(user_id)
"""

from __future__ import annotations

import asyncio
import hmac
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Header, HTTPException, status

from ..db.janitor import ORPHAN_STALE_AFTER_SECONDS, RepositoryJanitor
from ..db.postgres import (
    PostgresOperationRepository,
    PostgresWatchRepository,
)
from ..db.protocols import OperationRepository, WatchRepository
from ..db.session import DatabaseSessionFactory
from ..services.livestreams import LivestreamService
from ..services.posts import PostService
from ..services.users import UserService
from ..services.watch import WatchService
from .background import BackgroundTaskRegistry
from .logging import ContextLogger
from .settings import settings

if TYPE_CHECKING:
    from f2.apps.douyin.handler import DouyinHandler  # type: ignore
else:
    # Deferred: importing f2 performs real HTTPS requests (see
    # ``core._lazy_f2``), so the SDK loads on first handler use only.
    from ._lazy_f2 import LazyF2Symbol

    DouyinHandler = LazyF2Symbol("f2.apps.douyin.handler", "DouyinHandler")

logger = ContextLogger(__name__)

# Dedicated thread pool sizes per blocking-IO domain. R2 uploads are the
# dominant long-running call and audit log writes are rare but must never
# starve the upload pool. (Database IO is fully async via asyncpg and
# needs no executor.) Keeping each domain in its own bounded pool
# prevents a burst in one from exhausting the default asyncio executor
# (``min(32, cpu+4)``) that every ``asyncio.to_thread`` call would
# otherwise share.
R2_EXECUTOR_MAX_WORKERS = 16
# ``head_object`` fan-out runs inside ``R2StorageService._list_objects_sync``
# while the surrounding listing already occupies a worker on
# ``r2_executor``. A shared global pool puts a single ceiling on concurrent
# ``head_object`` requests across every in-flight listing instead of letting
# each listing spawn its own short-lived ``ThreadPoolExecutor``.
R2_HEAD_EXECUTOR_MAX_WORKERS = 16
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
        self._r2_head_executor: ThreadPoolExecutor | None = None
        self._audit_executor: ThreadPoolExecutor | None = None
        # Shared registry for long-lived background downloads. Services
        # retrieve this via dependency injection and call ``spawn`` instead
        # of bare ``asyncio.create_task`` so the lifespan can drain them
        # before shared state is torn down.
        self._background_tasks = BackgroundTaskRegistry()
        # Database state. The session factory owns the asyncpg pool; the
        # janitor task heartbeats owned rows and sweeps orphans until
        # shutdown stops it after the background registry drains.
        self._db: DatabaseSessionFactory | None = None
        self._janitor_task: asyncio.Task[None] | None = None
        # Watch reconcile loop (watcher replicas only). Cancelled before
        # the watcher loops themselves so it cannot restart a loop
        # mid-shutdown.
        self._watch_reconcile_task: asyncio.Task[Any] | None = None

    async def initialize(
        self,
        *,
        operation_store: OperationRepository | None = None,
        watch_store: WatchRepository | None = None,
    ) -> None:
        """Initialize all registered services with their configurations.

        Coroutine because boot recovery (orphan sweep, retention purge)
        awaits database IO. Safe to ``await`` multiple times; subsequent
        calls are no-ops.

        Args:
            operation_store: Repository override for tests. When omitted
                (production path) a Postgres-backed repository is built
                from settings.
            watch_store: Same override for watch subscriptions. Pass both
                or neither; mixing a real backend with a fake is rejected.

        Services initialized:
            - DouyinHandler: Configured with headers, proxies, and download settings
            - OperationRepository: Postgres-backed operation state
            - WatchRepository: Postgres-backed watch subscriptions
            - UserService: Basic user management service
            - LivestreamService: Livestream download orchestration
            - PostService: Bulk post download orchestration
            - WatchService: Subscription-driven auto-download scheduler
            - RepositoryJanitor: Liveness loop (heartbeat/sweep/purge)

        Executors created:
            - ``r2_executor`` (16 workers): R2 upload/head/delete/list
            - ``r2_head_executor`` (16 workers): per-key ``head_object``
              fan-out triggered inside ``R2StorageService._list_objects_sync``
            - ``audit_executor`` (2 workers): reserved for ``LifecycleManager``
              audit writes; the manager is exercised in tests but not yet
              wired into the runtime container, so the pool is currently
              idle in production.

        Note:
            This method is awaited by the FastAPI lifespan. Direct access
            through the property methods still works but now raises if the
            container has not been initialized yet.

        """
        if self._initialized:
            return
        if (operation_store is None) != (watch_store is None):
            raise ValueError(
                "Pass both operation_store and watch_store overrides, or "
                "neither; mixing a real backend with a fake is not supported."
            )
        # Multi-replica guard (fail fast, before any thread or pool exists).
        # Downloads stage through pod-local workspaces, so a second replica
        # can only work when finished files land somewhere shared.
        if (
            settings.api.multi_replica
            and not settings.r2.is_configured
            and not settings.api.shared_file_storage
        ):
            raise RuntimeError(
                "API_MULTI_REPLICA=true requires downloads to survive pod "
                "boundaries: configure R2 archival (R2_*) or mount a shared "
                "ReadWriteMany volume at DOUYIN_DOWNLOAD_ROOT and set "
                "API_SHARED_FILE_STORAGE=true."
            )

        # Create dedicated thread pool executors for each blocking-IO
        # domain before instantiating any service that might need one.
        # The thread-name prefix shows up in logs/traces so hot threads
        # are easy to spot.
        self._r2_executor = ThreadPoolExecutor(
            max_workers=R2_EXECUTOR_MAX_WORKERS,
            thread_name_prefix="dyvine-r2",
        )
        self._r2_head_executor = ThreadPoolExecutor(
            max_workers=R2_HEAD_EXECUTOR_MAX_WORKERS,
            thread_name_prefix="dyvine-r2-head",
        )
        self._audit_executor = ThreadPoolExecutor(
            max_workers=AUDIT_EXECUTOR_MAX_WORKERS,
            thread_name_prefix="dyvine-audit",
        )

        # Everything below can fail (unreachable database, bad
        # credentials). Unwind what we built so a boot failure neither
        # leaks worker threads nor leaves a half-wired container behind;
        # the caller sees the original error.
        try:
            await self._initialize_services(
                operation_store=operation_store, watch_store=watch_store
            )
        except Exception:
            await self._abort_startup()
            raise

    async def _initialize_services(
        self,
        *,
        operation_store: OperationRepository | None,
        watch_store: WatchRepository | None,
    ) -> None:
        """Build repositories, services, and the janitor (may raise)."""
        # Initialize Douyin handler with configuration
        douyin_config = self._create_douyin_config()
        self._services["douyin_handler"] = DouyinHandler(douyin_config)

        # Initialize repositories. Production builds a Postgres pair over
        # one shared pool; tests inject fakes. The owner identity is fresh
        # per boot so a restarted replica never inherits the previous
        # process's liveness.
        if operation_store is None or watch_store is None:
            self._db = DatabaseSessionFactory(
                settings.database.url,
                pool_size=settings.database.pool_size,
                pool_timeout=settings.database.pool_timeout,
            )
            owner_id = uuid.uuid4().hex
            operation_store = PostgresOperationRepository(self._db, owner_id=owner_id)
            watch_store = PostgresWatchRepository(self._db)
            logger.info(
                "database backend ready",
                extra={"owner_id": owner_id},
            )
        self._services["operation_store"] = operation_store
        self._services["watch_store"] = watch_store

        # Boot recovery: fail rows orphaned by dead replicas, then enforce
        # retention so a fresh deploy does not wait a day for the first
        # janitor purge.
        swept = await operation_store.sweep_orphans(
            stale_after_seconds=ORPHAN_STALE_AFTER_SECONDS
        )
        if swept:
            logger.warning(
                "swept orphaned operations at boot",
                extra={"count": swept},
            )
        retention_days = settings.database.operation_retention_days
        if retention_days > 0:
            cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
            purged = await operation_store.purge_terminal_before(cutoff)
            if purged:
                logger.info(
                    "purged terminal operations at boot",
                    extra={"count": purged, "retention_days": retention_days},
                )

        # Initialize user service and wire its R2 client to the R2 executor.
        # The dedicated head fan-out pool is attached separately so a burst
        # of concurrent ``list_objects`` calls does not nest one
        # ``ThreadPoolExecutor`` per listing inside a worker thread of
        # ``r2_executor``.
        user_service = UserService(
            operation_store=operation_store,
            task_registry=self._background_tasks,
        )
        user_service.storage.set_executor(self._r2_executor)
        user_service.storage.set_head_executor(self._r2_head_executor)
        self._services["user_service"] = user_service

        # Initialize livestream service
        self._services["livestream_service"] = LivestreamService(
            douyin_handler=self._services["douyin_handler"],
            user_service=user_service,
            operation_store=operation_store,
            task_registry=self._background_tasks,
        )

        # Initialize post service. Bulk downloads are scheduled as
        # long-running background tasks, so wire the service to the same
        # operation store and registry the lifespan drains on shutdown.
        self._services["post_service"] = PostService(
            handler=self._services["douyin_handler"],
            operation_store=operation_store,
            task_registry=self._background_tasks,
        )

        # Start the liveness loop before any service schedules work so
        # rows created during startup are heartbeat-covered from birth.
        janitor = RepositoryJanitor(
            operation_store,
            retention_days=settings.database.operation_retention_days,
        )
        self._janitor_task = asyncio.create_task(janitor.run_forever())

        # Initialize the watch scheduler. It shares the background-task
        # registry so its watcher loops drain on shutdown like every
        # other long-lived download task. Watch mode is non-critical --
        # it never gates readiness -- so a failure to build it must not
        # abort startup or leak the executors created above; log and
        # continue without watch instead. ``WATCH_ENABLED=false`` (API
        # replicas) still builds the service so subscription CRUD keeps
        # working against shared Postgres, but no loop ever starts
        # locally; the watcher replica adopts new rows on reconcile.
        try:
            self._services["watch_service"] = WatchService(
                watch_store=watch_store,
                livestream_service=self._services["livestream_service"],
                post_service=self._services["post_service"],
                task_registry=self._background_tasks,
                run_loops=settings.watch_enabled,
            )
        except Exception:
            logger.exception(
                "watch scheduler initialization failed; continuing without watch"
            )
            self._services.pop("watch_service", None)

        self._initialized = True

        # Re-arm watcher loops for persisted subscriptions only after the
        # container is marked ready, since resume schedules tasks that may
        # resolve services back through the container. A resume failure is
        # non-fatal for the same reason: log it and leave the container up.
        # The reconcile loop then keeps adoptions prompt on replicas that
        # run loops; it is tracked separately so shutdown can stop it
        # before the loops it supervises.
        watch_service = self._services.get("watch_service")
        if isinstance(watch_service, WatchService):
            try:
                await watch_service.resume_persisted()
            except Exception:
                logger.exception("watch subscription resume failed; continuing startup")
            if settings.watch_enabled:
                self._watch_reconcile_task = self._background_tasks.spawn(
                    watch_service.run_reconcile_forever(),
                    name="watch-reconcile",
                )

    async def _abort_startup(self) -> None:
        """Unwind a failed ``initialize`` without marking ready.

        Mirrors :meth:`shutdown` but tolerates partially built state:
        the janitor may never have started, the pool may never have
        opened, and executors may be the only thing alive. Every branch
        is best-effort so the original boot error (not a cleanup error)
        is what the caller sees.
        """
        if self._janitor_task is not None:
            task, self._janitor_task = self._janitor_task, None
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if self._watch_reconcile_task is not None:
            task, self._watch_reconcile_task = self._watch_reconcile_task, None
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if self._db is not None:
            try:
                await self._db.aclose()
            except Exception:
                logger.exception("startup abort: pool dispose failed")
            finally:
                self._db = None
        for attr in ("_r2_head_executor", "_r2_executor", "_audit_executor"):
            executor = getattr(self, attr)
            if executor is not None:
                try:
                    executor.shutdown(wait=True)
                except Exception:
                    logger.exception(
                        "startup abort: executor shutdown failed",
                        extra={"executor": attr},
                    )
                finally:
                    setattr(self, attr, None)
        self._services.clear()

    async def shutdown(self) -> None:
        """Release services, stop the janitor, and reap executors and pool.

        Called from the FastAPI lifespan's shutdown branch. Safe to call
        multiple times; subsequent calls are no-ops.

        Order matters: watcher loops stop first (no new work), the
        background registry drains while the janitor still heartbeats
        (so siblings never sweep rows mid-drain), then the janitor
        stops, the database pool disposes, and the executors reap in
        reverse initialisation order.
        """
        if not self._initialized:
            return

        # Stop the reconcile loop before the loops it supervises so it
        # cannot restart a watcher mid-shutdown.
        if self._watch_reconcile_task is not None:
            task, self._watch_reconcile_task = self._watch_reconcile_task, None
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        # Stop watcher loops first so they cannot schedule new downloads
        # while the background-task registry is being drained.
        watch_service = self._services.get("watch_service")
        if isinstance(watch_service, WatchService):
            await watch_service.stop_all()

        # Drain fire-and-forget downloads before tearing down shared
        # state. Any task still running after the registry's drain
        # timeout is cancelled so the shutdown cannot hang on a stuck
        # upstream request. The janitor keeps heartbeating throughout so
        # a sibling replica's sweep cannot mistake draining rows for
        # orphans (drain <= 20s, staleness threshold 120s).
        await self._background_tasks.drain()

        # Stop the liveness loop. Rows left behind (cancelled tasks that
        # never wrote a terminal state) go stale and are swept by the
        # remaining replicas or the next boot.
        if self._janitor_task is not None:
            task, self._janitor_task = self._janitor_task, None
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("janitor shutdown failed")

        # Dispose the database pool (no-op when tests injected fakes).
        if self._db is not None:
            await self._db.aclose()
            self._db = None

        # Reverse of init order. The R2 head pool is drained first so any
        # ``list_objects`` follow-up still has a working main pool to
        # report back through; the audit pool drains last so any final
        # write triggered by an earlier shutdown step still lands.
        for attr in ("_r2_head_executor", "_r2_executor", "_audit_executor"):
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
            "interval": "all",
            "cookie": settings.douyin.cookie,
            "path": settings.douyin.download_root,
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
        requested. ``initialize`` is a coroutine (it awaits the orphan
        sweep and retention purge against the database), so synchronous
        access has no safe way to self-heal. The FastAPI lifespan awaits
        ``initialize`` before any
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
    def operation_store(self) -> OperationRepository:
        """Get the persistent operation repository."""
        service = self.get_service("operation_store")
        if not isinstance(service, OperationRepository):
            raise TypeError("operation_store is not an OperationRepository instance")
        return service

    @property
    def livestream_service(self) -> LivestreamService:
        """Get the livestream management service."""
        service = self.get_service("livestream_service")
        if not isinstance(service, LivestreamService):
            raise TypeError("livestream_service is not a LivestreamService instance")
        return service

    @property
    def post_service(self) -> PostService:
        """Get the post management service."""
        service = self.get_service("post_service")
        if not isinstance(service, PostService):
            raise TypeError("post_service is not a PostService instance")
        return service

    @property
    def watch_service(self) -> WatchService:
        """Get the watch scheduler service."""
        service = self.get_service("watch_service")
        if not isinstance(service, WatchService):
            raise TypeError("watch_service is not a WatchService instance")
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


def get_post_service() -> PostService:
    """FastAPI dependency provider for post service.

    Returns the container-managed ``PostService`` so bulk downloads share
    the same ``OperationRepository`` and ``BackgroundTaskRegistry`` as the rest
    of the application. Routers can depend on this provider directly via
    ``Annotated[PostService, Depends(get_post_service)]``.
    """
    return get_service_container().post_service


def get_watch_service() -> WatchService:
    """FastAPI dependency provider for the watch scheduler service."""
    return get_service_container().watch_service


def require_api_key(
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> None:
    """Reject requests that do not present a matching ``X-API-Key`` header.

    Uses ``settings.security.api_key`` as the expected value. The check is
    bypassed entirely when ``settings.security.require_api_key`` is
    ``False`` so deployments fronted by mTLS or a mesh policy can opt out.
    The composite ``Settings`` validator already guarantees the configured
    key is non-default in production builds, so this dependency does not
    need to re-check that here.
    """
    if not settings.security.require_api_key:
        return
    expected = settings.security.api_key
    # ``hmac.compare_digest`` runs in time independent of the leading
    # matching prefix, which prevents a remote attacker from probing the
    # secret a byte at a time via response-latency timing. The empty
    # fallbacks keep the comparison length-balanced when either side is
    # missing so the rejection path also has uniform timing.
    if not expected or not hmac.compare_digest(x_api_key or "", expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )
