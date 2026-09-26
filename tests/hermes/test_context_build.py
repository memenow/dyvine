"""Hermes engine build, rollback, and singleton adoption."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import dyvine_hermes.context as context_mod
from dyvine_hermes.context import _await_sync


def test_await_sync_runs_cleanup_without_running_loop() -> None:
    """No running loop: the coroutine runs inline to completion."""
    completed: list[bool] = []

    async def _mark() -> None:
        completed.append(True)

    _await_sync(_mark(), timeout=5)
    assert completed == [True]


async def test_await_sync_offloads_when_loop_running() -> None:
    """A running loop detours through a worker thread with a fresh loop."""
    completed: list[bool] = []

    async def _mark() -> None:
        completed.append(True)

    _await_sync(_mark(), timeout=5)
    assert completed == [True]


async def test_await_sync_reraises_worker_errors() -> None:
    """Worker failures propagate to the caller instead of vanishing."""

    async def _boom() -> None:
        raise ValueError("worker blew up")

    with pytest.raises(ValueError, match="worker blew up"):
        _await_sync(_boom(), timeout=5)


async def test_await_sync_times_out_hung_cleanup() -> None:
    """A stuck cleanup fails fast instead of hanging the caller."""

    async def _hang() -> None:
        await asyncio.sleep(5)

    with pytest.raises(TimeoutError, match="engine cleanup timed out"):
        _await_sync(_hang(), timeout=0.05)


def test_rollback_build_tolerates_every_resource_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each rollback step runs even when every resource resists release."""

    async def _boom_handler(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("handler stuck")

    monkeypatch.setattr("dyvine.services.users._safely_close_handler", _boom_handler)

    class _Sessions:
        async def aclose(self) -> None:
            raise RuntimeError("pool stuck")

    class _Provider:
        def close(self) -> None:
            raise RuntimeError("browser stuck")

    class _Pool:
        def shutdown(self, wait: bool = True) -> None:
            raise RuntimeError("executor stuck")

    context_mod._rollback_build(object(), _Provider(), _Sessions(), (_Pool(),))


def test_build_failure_rolls_back_partial_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mid-build failure releases what was created and keeps no engine."""
    import sys
    import types

    monkeypatch.setenv("API_DEBUG", "true")
    created: dict[str, Any] = {}

    class StubHandler:
        def __init__(self, config: Any) -> None:
            created["config"] = config

    stub = types.ModuleType("f2.apps.douyin.handler")
    stub.DouyinHandler = StubHandler
    monkeypatch.setitem(sys.modules, "f2.apps.douyin.handler", stub)
    monkeypatch.setattr(context_mod, "_install_websign", lambda settings: None)

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("no database here")

    monkeypatch.setattr("dyvine.db.session.DatabaseSessionFactory", _boom)
    with pytest.raises(RuntimeError, match="no database here"):
        context_mod._build_engine()
    assert created  # handler was built, then rolled back


def test_get_engine_adopts_racer_built_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A build lost to a racing thread returns the winner, never rebuilds."""
    sentinel = object()
    real_lock = context_mod._ENGINE_LOCK

    class _RaceLock:
        def __enter__(self) -> Any:
            context_mod._ENGINE = sentinel  # a racing builder won first
            return real_lock.__enter__()

        def __exit__(self, *args: Any) -> Any:
            return real_lock.__exit__(*args)

    def _must_not_build() -> Any:
        raise AssertionError("racer already built; must not rebuild")

    monkeypatch.setattr(context_mod, "_ENGINE", None)
    monkeypatch.setattr(context_mod, "_ENGINE_LOCK", _RaceLock())
    monkeypatch.setattr(context_mod, "_build_engine", _must_not_build)
    monkeypatch.setattr(context_mod, "_ensure_plugin_env", lambda: None)
    assert context_mod.get_engine() is sentinel
