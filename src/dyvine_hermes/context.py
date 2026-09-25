"""Per-process engine bootstrap for the hermes plugin.

The gateway process owns exactly one :class:`Engine`: one database
pool, one process-stable owner identity (queue claims + operation
liveness), one background task registry, and one service graph.
:func:`get_engine` builds it on first tool call, never at import, so
plugin discovery stays side-effect free.

Configuration is environment-driven (``DATABASE_URL``,
``DOUYIN_COOKIE``, ...), matching the ``requires_env`` manifest
declaration. ``API_DEBUG`` defaults to true inside the plugin host
for verbose tool logging.
"""

from __future__ import annotations

import asyncio
import os
import threading
import uuid
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class Engine:
    """Fully wired service graph for one gateway process."""

    owner_id: str
    sessions: Any
    operations: Any
    watch: Any
    queue_repo: Any
    send_repo: Any
    seed_repo: Any
    profile_repo: Any
    round_repo: Any
    users: Any
    posts: Any
    livestreams: Any
    queue: Any
    profiles: Any
    send_status: Any = None
    delivery_ledger: Any = None
    websign_provider: Any = None
    r2_executor: Any = None
    r2_head_executor: Any = None


_ENGINE: Engine | None = None

#: Serializes engine construction and teardown. ``get_engine`` is
#: called from dozens of tool handlers that the gateway may invoke
#: concurrently; without the lock the first burst would build (and
#: leak) several engines. The lock is never held across an ``await``:
#: construction is synchronous, and teardown awaits only after
#: releasing it, so a ``get_engine`` on the closing thread cannot
#: deadlock.
_ENGINE_LOCK = threading.Lock()


def _ensure_plugin_env() -> None:
    """Default the plugin host into debug mode (verbose logging)."""
    os.environ.setdefault("API_DEBUG", "true")


def _await_sync(coro: Coroutine[Any, Any, None], *, timeout: float) -> None:
    """Drive one cleanup coroutine to completion from sync code.

    Used only by the build-rollback path, where the resources being
    released were created (but never used) on this thread. A running
    loop detours through a worker thread with a fresh loop; the join
    is bounded so a stuck cleanup fails fast instead of hanging the
    caller.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(coro)
        return
    errors: list[BaseException] = []

    def _run() -> None:
        try:
            asyncio.run(coro)
        except BaseException as exc:  # propagate to the caller
            errors.append(exc)

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(timeout=timeout)
    if worker.is_alive():
        coro.close()
        raise TimeoutError("engine cleanup timed out")
    if errors:
        raise errors[0]


def _install_websign(douyin_settings: Any) -> Any:
    """Install f2 signing after its handler loads, without starting Chromium."""
    if not douyin_settings.websign_enabled:
        return None
    from dyvine.services.websign import (
        WebSignProvider,
        install_fetch_retry,
        install_websign_patch,
    )

    provider = WebSignProvider(
        page_url=douyin_settings.websign_page_url,
        user_agent=douyin_settings.user_agent,
        init_timeout_seconds=douyin_settings.websign_init_timeout_seconds,
        sign_timeout_seconds=douyin_settings.websign_sign_timeout_seconds,
    )
    try:
        install_websign_patch(provider)
        if douyin_settings.websign_retry_once:
            install_fetch_retry(provider)
    except BaseException:
        _stop_websign(provider)
        raise
    return provider


def _stop_websign(provider: Any) -> None:
    """Restore f2 methods before releasing the signer session."""
    if provider is None:
        return
    from dyvine.services.websign import uninstall_websign_patch

    try:
        uninstall_websign_patch()
    finally:
        provider.close()


def _shutdown_executors(*pools: Any) -> None:
    """Shut down engine-owned thread pools without masking callers."""
    for pool in pools:
        if pool is None:
            continue
        try:
            pool.shutdown(wait=True)
        # Best-effort release: cleanup must not mask the caller's error.
        except BaseException:  # noqa: S112
            continue


def _rollback_build(
    handler: Any | None,
    websign_provider: Any,
    sessions: Any | None,
    executors: tuple[Any, ...] = (),
) -> None:
    """Release resources from a failed build; never masks the cause.

    Cleanup failures are logged, never raised: the build error that
    triggered the rollback is what the caller must see.
    """
    from dyvine.core.logging import ContextLogger

    rollback_log = ContextLogger(__name__)
    _shutdown_executors(*executors)
    if handler is not None:
        try:
            from dyvine.services.users import _safely_close_handler

            _await_sync(_safely_close_handler(handler), timeout=10.0)
        except BaseException as exc:
            rollback_log.error(
                "engine rollback could not close DouyinHandler",
                extra={"error": f"{type(exc).__name__}: {exc}"},
            )
    if sessions is not None:
        try:
            _await_sync(sessions.aclose(), timeout=10.0)
        except BaseException as exc:
            rollback_log.error(
                "engine rollback could not dispose sessions",
                extra={"error": f"{type(exc).__name__}: {exc}"},
            )
    try:
        _stop_websign(websign_provider)
    except BaseException as exc:
        rollback_log.error(
            "engine rollback could not stop websign",
            extra={"error": f"{type(exc).__name__}: {exc}"},
        )


def _build_engine() -> Engine:
    """Construct the process engine. The caller holds ``_ENGINE_LOCK``.

    Any failure rolls back the resources created so far (handler,
    sessions, websign patch) instead of leaking them, and leaves the
    singleton empty so the next call retries cleanly.
    """
    # Local imports: the dyvine engine (and transitively f2) must not
    # load until a tool actually runs.
    from f2.apps.douyin.handler import DouyinHandler  # type: ignore

    from dyvine.core.background import BackgroundTaskRegistry
    from dyvine.core.settings import settings
    from dyvine.db.delivery_ledger import PostgresDeliveryLedgerRepository
    from dyvine.db.postgres import (
        PostgresOperationRepository,
        PostgresProfileRepository,
        PostgresQueueRepository,
        PostgresRoundRepository,
        PostgresSeedRepository,
        PostgresSendStatusRepository,
        PostgresWatchRepository,
    )
    from dyvine.db.session import DatabaseSessionFactory
    from dyvine.services.delivery import FeishuGroupChannel  # noqa: F401
    from dyvine.services.livestreams import LivestreamService
    from dyvine.services.posts import PostService
    from dyvine.services.profiles import ProfileService
    from dyvine.services.queue import QueueService
    from dyvine.services.users import UserService

    owner_id = f"hermes-{uuid.uuid4().hex[:12]}"
    douyin_config = {
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
    handler = DouyinHandler(douyin_config)
    websign_provider = _install_websign(settings.douyin)
    sessions = None
    r2_executor = None
    r2_head_executor = None
    try:
        sessions = DatabaseSessionFactory(
            settings.database.url,
            pool_class=settings.database.pool_class,
            pool_size=settings.database.pool_size,
        )
        operations = PostgresOperationRepository(sessions, owner_id=owner_id)
        watch = PostgresWatchRepository(sessions)
        queue_repo = PostgresQueueRepository(sessions, owner_id=owner_id)
        send_repo = PostgresSendStatusRepository(sessions)
        seed_repo = PostgresSeedRepository(sessions)
        profile_repo = PostgresProfileRepository(sessions)
        round_repo = PostgresRoundRepository(sessions)
        delivery_ledger = PostgresDeliveryLedgerRepository(sessions)
        tasks = BackgroundTaskRegistry()
        users = UserService(operation_store=operations, task_registry=tasks)
        # The storage service is built eagerly inside UserService; wire
        # the shared bounded pools post-hoc so every R2 call (uploads,
        # head fan-outs) shares process-wide budgets instead of
        # borrowing the default loop executor or spawning one-off
        # pools per listing.
        from concurrent.futures import ThreadPoolExecutor

        r2_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="dyvine-r2")
        r2_head_executor = ThreadPoolExecutor(
            max_workers=16, thread_name_prefix="dyvine-r2-head"
        )
        users.storage.set_executor(r2_executor)
        users.storage.set_head_executor(r2_head_executor)
        posts = PostService(
            handler=handler, operation_store=operations, task_registry=tasks
        )
        livestreams = LivestreamService(
            douyin_handler=handler,
            user_service=users,
            operation_store=operations,
            task_registry=tasks,
        )
        queue = QueueService(queue=queue_repo, seeds=seed_repo, rounds=round_repo)
        profiles = ProfileService(profiles=profile_repo)

        return Engine(
            owner_id=owner_id,
            sessions=sessions,
            operations=operations,
            watch=watch,
            queue_repo=queue_repo,
            send_repo=send_repo,
            seed_repo=seed_repo,
            profile_repo=profile_repo,
            round_repo=round_repo,
            users=users,
            posts=posts,
            livestreams=livestreams,
            queue=queue,
            profiles=profiles,
            send_status=send_repo,
            delivery_ledger=delivery_ledger,
            websign_provider=websign_provider,
            r2_executor=r2_executor,
            r2_head_executor=r2_head_executor,
        )
    except BaseException:
        _rollback_build(
            handler, websign_provider, sessions, (r2_executor, r2_head_executor)
        )
        raise


def get_engine() -> Engine:
    """Return the process engine, building it on first call (not import).

    Concurrent first calls serialize on ``_ENGINE_LOCK`` so exactly
    one engine is built; a failed build rolls back and leaves the
    singleton empty for a clean retry.
    """
    global _ENGINE
    engine = _ENGINE
    if engine is not None:
        return engine
    _ensure_plugin_env()
    with _ENGINE_LOCK:
        if _ENGINE is not None:
            return _ENGINE
        _ENGINE = _build_engine()
        return _ENGINE


async def close_engine() -> None:
    """Release the process engine (pools + signer); idempotent.

    The singleton swap and the signer stop each hold ``_ENGINE_LOCK``
    briefly (both synchronous), while the pool disposal awaits outside
    the lock so a ``get_engine`` on the closing thread cannot
    deadlock. A ``get_engine`` racing the disposal window builds a
    fresh engine against independent pools; the websign uninstall
    only restores entries it still owns, so the race cannot unpatch
    the new engine either.
    """
    global _ENGINE
    with _ENGINE_LOCK:
        engine, _ENGINE = _ENGINE, None
    if engine is None:
        return
    try:
        await engine.sessions.aclose()
    finally:
        _shutdown_executors(engine.r2_executor, engine.r2_head_executor)
        with _ENGINE_LOCK:
            _stop_websign(engine.websign_provider)
