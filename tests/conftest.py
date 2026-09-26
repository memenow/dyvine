"""Pytest configuration shared across all test modules.

Adds ``src/`` to ``sys.path`` so that imports like ``from dyvine.core import ...``
resolve to the local source tree rather than an installed package. This ensures a
single canonical import path (``dyvine.*``) — using the project root instead would
expose a second path (``src.dyvine.*``), causing double-registration errors in
modules with side effects at import time (e.g. Prometheus metrics in storage.py).
"""

from __future__ import annotations

import asyncio
import os
import sys
import warnings
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Flag the test runtime as "debug" before any ``dyvine.core.settings`` import
# so the composite validator does not reject the localhost database
# default that pydantic-settings supplies when no real env vars are
# present.
os.environ.setdefault("API_DEBUG", "true")

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# ``tests/`` holds shared (non-``test_*``) helpers such as ``fake_repos``.
# pytest only puts each test file's own directory on ``sys.path``, so
# without this a test under ``tests/db/`` could not ``import
# fake_repos`` from its sibling directory.
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))


@pytest.fixture(autouse=True)
def preserve_event_loop_affinity() -> Iterator[None]:
    """Re-anchor the ambient event loop across each test.

    pytest-asyncio lazily side-creates an ambient loop the first time an
    async fixture needs one (on Python <= 3.13 ``get_event_loop`` still
    creates instead of raising, and pytest-asyncio suppresses that
    deprecation internally). The loop is never closed by anyone, which is
    harmless while the event-loop policy keeps referencing it — but stdlib
    ``asyncio.run`` (used by operator-script entry points under test) resets
    the policy's current loop to ``None`` on close, orphaning the ambient
    loop so a later GC fails an unrelated test (or session teardown) with
    ``unclosed event loop`` unraisables. Restoring whatever was current
    before the test keeps the orphan referenced and silent.

    Ownership follows creation: when the policy holds no loop,
    ``get_event_loop`` *creates* one on Python <= 3.13, and a loop this
    fixture created is closed at teardown instead of re-anchored —
    re-anchoring a loop nothing will ever close only defers the
    ``unclosed event loop`` warning to whatever test the GC happens to
    run in. Creation is detected via the DeprecationWarning CPython
    emits exactly on the creating path; a silently returned loop is
    pre-existing and keeps the re-anchor behavior. Loops a test
    abandons itself are unaffected: they are still collected and still
    fail loudly.
    """
    created = False
    try:
        before = asyncio.get_running_loop()
    except RuntimeError:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", DeprecationWarning)
            try:
                before = asyncio.get_event_loop()
            except RuntimeError:  # Python 3.14+: nothing set, nothing to keep
                before = None
            else:
                created = any(
                    isinstance(w.message, DeprecationWarning)
                    and "no current event loop" in str(w.message).lower()
                    for w in caught
                )
    yield
    if created and before is not None:
        if not before.is_closed():
            before.close()
        asyncio.set_event_loop(None)
    else:
        asyncio.set_event_loop(before)


@pytest.fixture(autouse=True)
def reset_singletons(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Reset cached singletons between tests.

    The autouse fixture also strips ``DATABASE_URL`` from the process
    environment before each test runs. ``get_settings`` calls
    ``load_dotenv`` on import, which leaks any real credentials from
    the developer's ``.env`` into ``os.environ`` and would otherwise
    make settings tests assert against live secrets instead of the
    documented defaults.

    Persistence isolation needs no work here: services take their
    repositories by injection, so unit tests use the in-memory fakes
    from ``tests.fake_repos`` while only ``tests/db/`` touches
    a real database.
    """
    from dyvine.core.settings import get_settings

    monkeypatch.delenv("DATABASE_URL", raising=False)
    # ``get_settings`` is ``lru_cache``d on the module so a settings test
    # that monkeypatches an env var would otherwise see a stale Settings
    # instance leaked from a previous test. Clearing the cached
    # singleton keeps the per-test isolation honest.
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def mock_douyin_handler() -> MagicMock:
    """Return a MagicMock mimicking DouyinHandler's async interface."""
    handler = MagicMock()
    handler.kwargs = {"mode": "all", "max_tasks": 3}
    handler.fetch_one_video = AsyncMock(return_value=None)
    handler.fetch_user_profile = AsyncMock(return_value=None)

    async def _empty_feed(*args, **kwargs):
        """Fresh async iterator per call (production returns one per call)."""
        if False:
            yield None

    # A factory, not a shared instance: production returns a new async
    # iterator per call, and sharing one mock across calls would leak
    # configured side effects between cases.
    handler.fetch_user_post_videos = MagicMock(side_effect=_empty_feed)
    handler.fetch_user_live_videos = AsyncMock(return_value=None)
    handler.fetch_user_live_videos_by_room_id = AsyncMock(return_value=None)
    handler.get_or_add_user_data = AsyncMock(return_value=Path("/tmp/test"))
    handler.downloader = MagicMock()
    handler.downloader.create_download_tasks = AsyncMock()
    handler.downloader.create_stream_tasks = AsyncMock()
    handler.enable_bark = False
    return handler


@pytest.fixture
def storage_service_no_init():
    """Create R2StorageService without calling __init__ (avoids boto3)."""
    from dyvine.services.storage import R2StorageService

    service = object.__new__(R2StorageService)
    # Mirror every attribute ``__init__`` sets so the stub exercises
    # the same shape as a disabled service. Update together with
    # ``R2StorageService.__init__``.
    service._executor = None  # type: ignore[attr-defined]
    service._head_executor = None  # type: ignore[attr-defined]
    service._head_pool_warning_emitted = False  # type: ignore[attr-defined]
    service.client = None  # type: ignore[attr-defined]
    service.bucket = None  # type: ignore[attr-defined]
    return service


@pytest.fixture(scope="session")
def postgres_url():
    """Start a containerised Postgres and migrate it to ``head``.

    Shared by ``tests/db`` and ``tests/scripts``. The container URL
    travels on the Alembic config object
    (``dyvine.sqlalchemy.url``), never through the global settings
    singleton, so per-test cache resets cannot split the suite onto a
    different database. ``env.py`` owns the running-loop bridge, so
    the upgrade is a plain ``command.upgrade`` call here.
    """
    from alembic import command
    from alembic.config import Config
    from testcontainers.community.postgres import PostgresContainer

    root_dir = Path(__file__).resolve().parents[1]
    with PostgresContainer("postgres:16") as container:
        raw_url = container.get_connection_url()
        if raw_url.startswith("postgresql+psycopg2://"):
            url = raw_url.replace("postgresql+psycopg2://", "postgresql+asyncpg://", 1)
        elif raw_url.startswith("postgresql://"):
            url = raw_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        else:
            raise ValueError(f"Unrecognized testcontainer URL scheme: {raw_url!r}")
        config = Config(str(root_dir / "alembic.ini"))
        config.attributes["dyvine.sqlalchemy.url"] = url
        command.upgrade(config, "head")
        yield url
