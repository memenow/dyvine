"""Tests for background task registry lifecycle behavior."""

from __future__ import annotations

import asyncio

import pytest

from dyvine.core.background import BackgroundTaskRegistry, spawn_or_fallback


@pytest.mark.asyncio
async def test_spawn_tracks_then_discards_completed_tasks() -> None:
    """Verify spawn tracks then discards completed tasks."""
    registry = BackgroundTaskRegistry()

    async def quick() -> int:
        """Test helper for test_spawn_tracks_then_discards_completed_tasks."""
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
    """Verify drain waits for outstanding tasks."""
    registry = BackgroundTaskRegistry(drain_timeout=2.0)
    done_marker: list[str] = []

    async def slow() -> None:
        """Test helper for test_drain_waits_for_outstanding_tasks."""
        await asyncio.sleep(0.05)
        done_marker.append("finished")

    registry.spawn(slow())
    await registry.drain()

    assert done_marker == ["finished"]
    assert registry.active_count == 0


@pytest.mark.asyncio
async def test_drain_cancels_tasks_that_overrun_timeout() -> None:
    """Verify drain cancels tasks that overrun timeout."""
    registry = BackgroundTaskRegistry(drain_timeout=0.05)
    cancelled: list[str] = []

    async def hang() -> None:
        """Test helper for test_drain_cancels_tasks_that_overrun_timeout."""
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
    """Verify drain is noop when nothing is tracked."""
    registry = BackgroundTaskRegistry(drain_timeout=0.01)
    # No tasks registered; drain must return immediately without attempting
    # to ``asyncio.wait_for(asyncio.gather(*[]))`` (which would hit the
    # timeout branch on some event loops).
    await registry.drain()
    assert registry.active_count == 0


@pytest.mark.asyncio
async def test_spawn_propagates_exceptions_to_awaiters() -> None:
    """Verify spawn propagates exceptions to awaiters."""
    registry = BackgroundTaskRegistry()

    async def boom() -> None:
        """Test helper for test_spawn_propagates_exceptions_to_awaiters."""
        raise RuntimeError("planned failure")

    task = registry.spawn(boom(), name="boom")
    with pytest.raises(RuntimeError, match="planned failure"):
        await task
    await asyncio.sleep(0)
    assert registry.active_count == 0


@pytest.mark.asyncio
async def test_spawn_after_drain_raises_and_closes_coroutine() -> None:
    """Once ``drain`` has been entered, ``spawn`` must reject new work.

    Otherwise a stale callback that registers another download after
    shutdown started would silently leak past the executor teardown
    the host performs next.
    """
    registry = BackgroundTaskRegistry(drain_timeout=0.5)

    async def noop() -> None:
        """Test helper for test_spawn_after_drain_raises_and_closes_coroutine."""
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
    """Verify spawn or fallback uses registry when available."""
    registry = BackgroundTaskRegistry()

    async def quick() -> int:
        """Test helper for test_spawn_or_fallback_uses_registry_when_available."""
        return 7

    task = spawn_or_fallback(registry, quick(), name="via-registry")
    assert registry.active_count == 1
    assert await task == 7


@pytest.mark.asyncio
async def test_spawn_or_fallback_falls_back_to_create_task() -> None:
    """Verify spawn or fallback falls back to create task."""

    async def quick() -> int:
        """Test helper for test_spawn_or_fallback_falls_back_to_create_task."""
        return 11

    task = spawn_or_fallback(None, quick())
    assert isinstance(task, asyncio.Task)
    assert await task == 11


@pytest.mark.asyncio
async def test_spawn_or_fallback_isolates_correlation_id() -> None:
    """Background tasks must run with a fresh correlation ID.

    The spawning context's ``_correlation_id_var`` is captured by
    ``contextvars.copy_context()``, then the snapshot is mutated to
    hold a per-task ID before ``asyncio.create_task(..., context=...)``
    runs the coroutine. The caller's context must remain unchanged
    after ``spawn_or_fallback`` returns so request-scoped logging is
    not contaminated by background work.
    """
    from dyvine.core.logging import _correlation_id_var, set_correlation_id

    set_correlation_id("request-abc")
    try:
        captured: dict[str, str | None] = {}

        async def record_id() -> None:
            """Test helper for test_spawn_or_fallback_isolates_correlation_id."""
            captured["task"] = _correlation_id_var.get()

        task = spawn_or_fallback(None, record_id(), name="task-xyz")
        await task

        assert captured["task"] is not None
        assert captured["task"].startswith("task-xyz-")
        assert captured["task"] != "request-abc"
        # Caller's context must be untouched by the spawn.
        assert _correlation_id_var.get() == "request-abc"
    finally:
        set_correlation_id(None)


@pytest.mark.asyncio
async def test_spawn_or_fallback_ids_are_unique_per_spawn() -> None:
    """Two spawns sharing a task name must not share a log timeline."""
    from dyvine.core.logging import _correlation_id_var

    seen: list[str | None] = []

    async def record_id() -> None:
        """Test helper for test_spawn_or_fallback_ids_are_unique_per_spawn."""
        seen.append(_correlation_id_var.get())

    registry = BackgroundTaskRegistry()
    first = spawn_or_fallback(registry, record_id(), name="same-name")
    second = spawn_or_fallback(registry, record_id(), name="same-name")
    await asyncio.gather(first, second)

    assert seen[0] != seen[1]
    assert all(item is not None and item.startswith("same-name-") for item in seen)


@pytest.mark.asyncio
async def test_failed_task_is_logged_and_retrieved(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failure landing between drains is logged, never left unretrieved.

    Before the fix the done-callback only discarded the handle, so a
    task failing outside a ``drain`` snapshot surfaced as ``Task
    exception was never retrieved`` instead of a structured record.
    """
    import logging

    registry = BackgroundTaskRegistry()

    async def boom() -> None:
        """Test helper for test_failed_task_is_logged_and_retrieved."""
        raise RuntimeError("planned failure")

    with caplog.at_level(logging.ERROR, logger="dyvine.core.background"):
        registry.spawn(boom(), name="boom")
        await asyncio.sleep(0.05)

    assert registry.active_count == 0
    assert "Background task failed" in caplog.text
    assert "planned failure" in caplog.text
    assert "never retrieved" not in caplog.text


@pytest.mark.asyncio
async def test_drain_waits_for_follow_up_spawned_mid_drain() -> None:
    """Work spawned by a drained task joins the drain set.

    Closing the registry before waiting turned a follow-up spawn into
    a ``RuntimeError`` inside the (otherwise successful) parent task.
    """
    registry = BackgroundTaskRegistry(drain_timeout=2.0)
    finished: list[str] = []

    async def child() -> None:
        """Test helper for test_drain_waits_for_follow_up_spawned_mid_drain."""
        finished.append("child")

    async def parent() -> None:
        """Test helper for test_drain_waits_for_follow_up_spawned_mid_drain."""
        await asyncio.sleep(0.01)
        registry.spawn(child(), name="child")
        finished.append("parent")

    registry.spawn(parent(), name="parent")
    await registry.drain()

    assert finished == ["parent", "child"]
    assert registry.active_count == 0


@pytest.mark.asyncio
async def test_drain_abandons_tasks_ignoring_cancellation() -> None:
    """A task that swallows ``CancelledError`` cannot hang shutdown.

    After ``cancel_timeout`` the drain gives up and reports the
    stragglers instead of awaiting them forever.
    """
    registry = BackgroundTaskRegistry(drain_timeout=0.02, cancel_timeout=0.02)

    async def stubborn() -> None:
        """Test helper for test_drain_abandons_tasks_ignoring_cancellation."""
        try:
            await asyncio.sleep(10.0)
        except asyncio.CancelledError:
            pass  # Swallow the drain's cancel; stay pending past the deadline.
        await asyncio.sleep(10.0)

    task = registry.spawn(stubborn(), name="stubborn")
    await asyncio.wait_for(registry.drain(), timeout=5.0)

    assert registry.is_closed is True
    # Still pending (abandoned), so stop it explicitly for a clean loop.
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_concurrent_drain_raises() -> None:
    """A second ``drain`` while one is running is a programming error."""
    registry = BackgroundTaskRegistry(drain_timeout=1.0)
    started = asyncio.Event()
    release = asyncio.Event()

    async def gated() -> None:
        """Test helper for test_concurrent_drain_raises."""
        started.set()
        await release.wait()

    registry.spawn(gated())
    first = asyncio.create_task(registry.drain())
    await started.wait()
    await asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="already in progress"):
        await registry.drain()
    release.set()
    await first


@pytest.mark.asyncio
async def test_is_closed_property_reflects_drain_state() -> None:
    """``is_closed`` lets callers probe the registry without raising."""
    # No tracked tasks, so ``drain_timeout`` is irrelevant; use the default.
    registry = BackgroundTaskRegistry()
    assert registry.is_closed is False

    await registry.drain()
    assert registry.is_closed is True

    # Idempotent: a second drain does not flip the flag back.
    await registry.drain()
    assert registry.is_closed is True
