from __future__ import annotations

import asyncio

import pytest

from dyvine.core.background import BackgroundTaskRegistry, spawn_or_fallback


@pytest.mark.asyncio
async def test_spawn_tracks_then_discards_completed_tasks() -> None:
    registry = BackgroundTaskRegistry()

    async def quick() -> int:
        await asyncio.sleep(0)
        return 42

    task = registry.spawn(quick())
    assert registry.active_count == 1

    result = await task
    assert result == 42
    # The done-callback shrinks the tracking set without an explicit drain.
    await asyncio.sleep(0)
    assert registry.active_count == 0


@pytest.mark.asyncio
async def test_drain_waits_for_outstanding_tasks() -> None:
    registry = BackgroundTaskRegistry(drain_timeout=2.0)
    done_marker: list[str] = []

    async def slow() -> None:
        await asyncio.sleep(0.05)
        done_marker.append("finished")

    registry.spawn(slow())
    await registry.drain()

    assert done_marker == ["finished"]
    assert registry.active_count == 0


@pytest.mark.asyncio
async def test_drain_cancels_tasks_that_overrun_timeout() -> None:
    registry = BackgroundTaskRegistry(drain_timeout=0.05)
    cancelled: list[str] = []

    async def hang() -> None:
        try:
            await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            cancelled.append("hang")
            raise

    task = registry.spawn(hang())
    await registry.drain()

    assert task.cancelled()
    assert cancelled == ["hang"]
    assert registry.active_count == 0


@pytest.mark.asyncio
async def test_drain_is_noop_when_nothing_is_tracked() -> None:
    registry = BackgroundTaskRegistry(drain_timeout=0.01)
    # No tasks registered; drain must return immediately without attempting
    # to ``asyncio.wait_for(asyncio.gather(*[]))`` (which would hit the
    # timeout branch on some event loops).
    await registry.drain()
    assert registry.active_count == 0


@pytest.mark.asyncio
async def test_spawn_propagates_exceptions_to_awaiters() -> None:
    registry = BackgroundTaskRegistry()

    async def boom() -> None:
        raise RuntimeError("planned failure")

    task = registry.spawn(boom(), name="boom")
    with pytest.raises(RuntimeError, match="planned failure"):
        await task
    await asyncio.sleep(0)
    assert registry.active_count == 0


@pytest.mark.asyncio
async def test_spawn_after_drain_raises_and_closes_coroutine() -> None:
    """Once ``drain`` has been entered, ``spawn`` must reject new work.

    Otherwise a stale callback that registers another download after the
    lifespan started shutdown would silently leak past the executor
    teardown that ``ServiceContainer.shutdown`` performs next.
    """
    registry = BackgroundTaskRegistry(drain_timeout=0.5)

    async def noop() -> None:
        return None

    await registry.drain()

    coro = noop()
    with pytest.raises(RuntimeError, match="closed"):
        registry.spawn(coro)
    # The supplied coroutine must have been closed before the raise so the
    # ``-W error`` test runtime does not trip ``RuntimeWarning: coroutine
    # was never awaited``.
    assert coro.cr_frame is None


@pytest.mark.asyncio
async def test_spawn_or_fallback_uses_registry_when_available() -> None:
    registry = BackgroundTaskRegistry()

    async def quick() -> int:
        return 7

    task = spawn_or_fallback(registry, quick(), name="via-registry")
    assert registry.active_count == 1
    assert await task == 7


@pytest.mark.asyncio
async def test_spawn_or_fallback_falls_back_to_create_task() -> None:
    async def quick() -> int:
        return 11

    task = spawn_or_fallback(None, quick())
    assert isinstance(task, asyncio.Task)
    assert await task == 11


@pytest.mark.asyncio
async def test_is_closed_property_reflects_drain_state() -> None:
    """``is_closed`` lets callers probe the registry without raising."""
    registry = BackgroundTaskRegistry(drain_timeout=0.1)
    assert registry.is_closed is False

    await registry.drain()
    assert registry.is_closed is True

    # Idempotent: a second drain does not flip the flag back.
    await registry.drain()
    assert registry.is_closed is True
