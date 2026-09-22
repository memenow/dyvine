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

import os
import uuid
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


_ENGINE: Engine | None = None


def _ensure_plugin_env() -> None:
    """Default the plugin host into debug mode (verbose logging)."""
    os.environ.setdefault("API_DEBUG", "true")


def get_engine() -> Engine:
    """Return the process engine, building it on first call (not import)."""
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE
    _ensure_plugin_env()

    # Local imports: the dyvine engine (and transitively f2) must not
    # load until a tool actually runs.
    from f2.apps.douyin.handler import DouyinHandler  # type: ignore

    from dyvine.core.background import BackgroundTaskRegistry
    from dyvine.core.settings import settings
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
    tasks = BackgroundTaskRegistry()
    users = UserService(operation_store=operations, task_registry=tasks)
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

    _ENGINE = Engine(
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
    )
    return _ENGINE


async def close_engine() -> None:
    """Release the process engine (pools + registry); idempotent."""
    global _ENGINE
    engine, _ENGINE = _ENGINE, None
    if engine is None:
        return
    await engine.sessions.aclose()
