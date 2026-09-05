"""Pytest configuration shared across all test modules.

Adds ``src/`` to ``sys.path`` so that imports like ``from dyvine.core import ...``
resolve to the local source tree rather than an installed package. This ensures a
single canonical import path (``dyvine.*``) — using the project root instead would
expose a second path (``src.dyvine.*``), causing double-registration errors in
modules with side effects at import time (e.g. Prometheus metrics in storage.py).
"""

from __future__ import annotations

import asyncio as _asyncio
import gc as _gc
import os
import sys
import threading as _threading
import traceback as _traceback
import warnings as _warnings
import weakref as _weakref
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Flag the test runtime as "debug" before any ``dyvine.core.settings`` import
# so the production-only validator does not reject the default
# ``change-me-in-production`` sentinel values that pydantic-settings supplies
# when no real env vars are present. Production deployments set
# ``API_DEBUG=false`` explicitly; leaving this unset in tests used to silently
# skip the validator. ``SECURITY_REQUIRE_API_KEY=false`` is set here too so
# router tests do not need to embed a header in every request — the
# auth-bypass path is exercised explicitly by dedicated tests.
os.environ.setdefault("API_DEBUG", "true")
os.environ.setdefault("SECURITY_REQUIRE_API_KEY", "false")
# Effectively disable the token-bucket middleware for the shared app:
# buckets key on client IP and ``TestClient`` always dials from the
# same one, so production-sized limits would 429 unrelated tests once
# the suite's cumulative traffic exceeds the burst. Tight-limit
# behavior is covered by ``tests/middleware/`` on purpose-built apps.
os.environ.setdefault("API_RATE_LIMIT_PER_SECOND", "1000000")
os.environ.setdefault("API_RATE_LIMIT_BURST", "1000000")

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# TEMPORARY CI DIAGNOSTIC (revert before merge): identify the event loop
# that survives the session unclosed (with its self-pipe sockets) and fails
# CI legs at teardown with PytestUnraisableExceptionWarning after all tests
# pass. Records per-loop creation/run/ping stacks in memory and prints only
# loops still alive and unclosed at session finish. Prints nothing locally.
_probe_loops: dict[int, dict] = {}


def _probe_record(loop, kind: str) -> None:  # type: ignore[no-untyped-def]
    entry = _probe_loops.get(id(loop))
    if entry is None or entry["ref"]() is not loop:
        return
    slot = entry[kind]
    if len(slot) < 2:
        slot.append("".join(_traceback.format_stack()))


def _probe_wrap(loop, name: str) -> None:  # type: ignore[no-untyped-def]
    orig = getattr(loop, name)
    kind = "runs" if name in ("run_forever", "run_until_complete") else "pings"

    def _wrapped(*args, **kwargs):  # type: ignore[no-untyped-def]
        _probe_record(loop, kind)
        return orig(*args, **kwargs)

    try:
        setattr(loop, name, _wrapped)
    except (AttributeError, TypeError):
        pass


with _warnings.catch_warnings():
    _warnings.simplefilter("ignore", DeprecationWarning)
    _real_policy = _asyncio.get_event_loop_policy()


class _ProbePolicy(_real_policy.__class__):  # type: ignore[misc]
    def new_event_loop(self):  # type: ignore[no-untyped-def]
        loop = super().new_event_loop()
        _probe_loops[id(loop)] = {
            "thread": _threading.current_thread().name,
            "created": "".join(_traceback.format_stack()),
            "ref": _weakref.ref(loop),
            "runs": [],
            "pings": [],
        }
        for name in (
            "run_forever",
            "run_until_complete",
            "call_soon_threadsafe",
            "add_signal_handler",
        ):
            _probe_wrap(loop, name)
        return loop


with _warnings.catch_warnings():
    _warnings.simplefilter("ignore", DeprecationWarning)
    _asyncio.set_event_loop_policy(_ProbePolicy())


def pytest_sessionfinish(session, exitstatus) -> None:  # type: ignore[no-untyped-def]
    """TEMPORARY CI DIAGNOSTIC (revert before merge)."""
    _gc.collect()
    live = [
        o
        for o in _gc.get_objects()
        if isinstance(o, _asyncio.AbstractEventLoop) and not o.is_closed()
    ]
    print(f"\nPROBE-UNCLOSED-COUNT={len(live)}")
    for loop in live:
        owners: list[str] = []
        for ref in _gc.get_referrers(loop):
            if isinstance(ref, dict):
                keys = [k for k, v in list(ref.items())[:50] if v is loop]
                owners.append(f"dict{keys}")
            else:
                owners.append(type(ref).__name__)
        ready = [repr(getattr(h, "_callback", None)) for h in list(loop._ready)][:6]
        print(
            f"\nPROBE-LEAKED-LOOP loop={loop!r} "
            f"self_pipe={loop._ssock is not None} owners={owners[:12]}"
        )
        print(f"PROBE-READY={ready}")
        match = next(
            (e for e in _probe_loops.values() if e["ref"]() is loop),
            None,
        )
        if match is None:
            print("PROBE-ORIGIN=unknown (created before conftest import?)")
            continue
        print(f"PROBE-CREATED-THREAD={match['thread']}")
        print("PROBE-CREATED:" + match["created"][:6500])
        for i, stack in enumerate(match["runs"]):
            print(f"PROBE-RUN-{i}:" + stack[-2500:])
        for i, stack in enumerate(match["pings"]):
            print(f"PROBE-PING-{i}:" + stack[-2500:])


# ``tests/`` holds shared (non-``test_*``) helpers such as ``fake_repos``.
# pytest only puts each test file's own directory on ``sys.path``, so
# without this a test under ``tests/db/`` could not ``import
# fake_repos`` from its sibling directory.
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))


@pytest.fixture(autouse=True)
def reset_singletons(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset cached singletons between tests.

    The autouse fixture also strips ``SECURITY_API_KEY`` and
    ``DATABASE_URL`` from the process environment before each test runs.
    ``get_settings`` calls ``load_dotenv`` on import, which leaks any real
    credentials from the developer's ``.env`` into ``os.environ`` and would
    otherwise make settings tests assert against live secrets instead of
    the documented defaults.

    Persistence isolation needs no work here: services take their
    repositories by injection, so unit tests use the in-memory fakes
    from ``tests.fake_repos`` while only ``tests/db/`` touches
    a real database.
    """
    from dyvine.core.dependencies import get_service_container
    from dyvine.core.settings import get_settings

    monkeypatch.delenv("SECURITY_API_KEY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    # ``get_settings`` is ``lru_cache``d on the module so a settings test
    # that monkeypatches an env var would otherwise see a stale Settings
    # instance leaked from a previous test. Clearing both cached
    # singletons keeps the per-test isolation honest.
    get_settings.cache_clear()
    get_service_container.cache_clear()
    yield
    get_settings.cache_clear()
    get_service_container.cache_clear()


@pytest.fixture
def mock_douyin_handler() -> MagicMock:
    """Return a MagicMock mimicking DouyinHandler's async interface."""
    handler = MagicMock()
    handler.kwargs = {"mode": "all", "max_tasks": 3}
    handler.fetch_one_video = AsyncMock(return_value=None)
    handler.fetch_user_profile = AsyncMock(return_value=None)
    handler.fetch_user_post_videos = MagicMock(return_value=AsyncMock())
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
    service._executor = None  # type: ignore[attr-defined]
    return service


def _upgrade_to_head(config) -> None:
    """Run ``alembic upgrade head`` on a thread without a loop.

    This fixture is pulled from async test context, so the calling
    thread already runs an event loop -- and ``env.py`` drives its
    own ``asyncio.run``. A worker thread gives it a clean loop.
    """
    import threading

    from alembic import command

    errors: list[BaseException] = []

    def _run() -> None:
        try:
            command.upgrade(config, "head")
        except BaseException as exc:  # propagate to the caller
            errors.append(exc)

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join()
    if errors:
        raise errors[0]


@pytest.fixture(scope="session")
def postgres_url():
    """Start a containerised Postgres and migrate it to ``head``.

    Shared by ``tests/db`` and ``tests/scripts``. The Alembic
    environment reads its URL from the already-imported
    ``dyvine.core.settings.settings`` singleton (``cache_clear`` alone
    cannot rebuild it), so the singleton's URL is patched narrowly
    around the upgrade and restored before any test runs.
    """
    from alembic.config import Config
    from testcontainers.community.postgres import PostgresContainer

    import dyvine.core.settings as settings_module

    root_dir = Path(__file__).resolve().parents[1]
    with PostgresContainer("postgres:16") as container:
        raw_url = container.get_connection_url()
        url = raw_url.replace("postgresql+psycopg2://", "postgresql+asyncpg://")
        previous = settings_module.settings.database.url
        settings_module.settings.database.url = url
        try:
            _upgrade_to_head(Config(str(root_dir / "alembic.ini")))
        finally:
            settings_module.settings.database.url = previous
        yield url
