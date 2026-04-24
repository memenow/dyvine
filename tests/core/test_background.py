from __future__ import annotations

import asyncio

import pytest

from dyvine.core.background import BackgroundTaskRegistry


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
