"""Registry for tracking long-lived asyncio background tasks.

FastAPI's ``BackgroundTasks`` runs after the response is sent but before the
endpoint coroutine returns, so it is unsuitable for fire-and-forget downloads
that must outlive a single request. Previously those downloads were spawned
with bare ``asyncio.create_task`` and tracked nowhere. On a graceful shutdown
the FastAPI lifespan tore down ``ServiceContainer`` executors before the
tasks drained, which produced ``RuntimeError: cannot schedule new futures
after shutdown`` inside active R2 uploads / audit writes.

``BackgroundTaskRegistry`` centralizes spawn and drain so the lifespan can
wait for active downloads before reaping the executor pools.
"""

from __future__ import annotations

import asyncio
import contextvars
import uuid
from collections.abc import Coroutine
from typing import Any

from .logging import ContextLogger, _correlation_id_var

logger = ContextLogger(__name__)


class BackgroundTaskRegistry:
    """Track long-lived ``asyncio.Task`` handles so the lifespan can drain them.

    Tasks are registered via :meth:`spawn`, auto-removed from the tracking set
    on completion, and drained by :meth:`drain` during shutdown. Once
    :meth:`drain` is entered the registry is closed: subsequent
    :meth:`spawn` calls raise ``RuntimeError`` rather than silently leaking
    work past the executor teardown that follows in
    ``ServiceContainer.shutdown``.

    Attributes:
        drain_timeout: Maximum seconds ``drain`` waits for tasks to finish
            gracefully before cancelling anything still outstanding.
    """

    def __init__(self, *, drain_timeout: float = 30.0) -> None:
        """Initialize the registry with no tracked tasks."""
        self._tasks: set[asyncio.Task[Any]] = set()
        self.drain_timeout = drain_timeout
        # Set inside ``drain`` so any post-drain ``spawn`` is rejected
        # explicitly rather than being added to a registry nobody will
        # await again.
        self._closed = False

    def spawn(
        self,
        coro: Coroutine[Any, Any, Any],
        *,
        name: str | None = None,
        context: contextvars.Context | None = None,
    ) -> asyncio.Task[Any]:
        """Schedule ``coro`` on the running loop and track the resulting task.

        The task is auto-removed from the registry on completion via a
        done-callback, so the registry never retains a handle to a finished
        task.

        Args:
            coro: Coroutine to schedule.
            name: Optional task name surfaced through ``Task.get_name``.
            context: Optional ``contextvars.Context`` to pin the task to.
                Used by :func:`spawn_or_fallback` to assign a fresh
                correlation ID without mutating the caller's context.

        Raises:
            RuntimeError: If the registry has already been drained. The
                supplied coroutine is closed before raising so it does not
                leak an ``unawaited coroutine`` warning under ``-W error``.
        """
        if self._closed:
            coro.close()
            raise RuntimeError(
                "BackgroundTaskRegistry is closed; cannot spawn new tasks "
                "after drain has been entered"
            )
        task = asyncio.create_task(coro, name=name, context=context)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def drain(self) -> None:
        """Wait for tracked tasks to finish, cancelling anything that overruns.

        Returns once every tracked task has terminated -- successfully, with an
        exception, or via cancellation. Called from
        :meth:`ServiceContainer.shutdown` before the executor pools are torn
        down so in-flight dispatches can resolve. The registry is marked
        closed before awaiting so any concurrent ``spawn`` cannot add work
        the drain would not see.
        """
        # Mark closed even when there is nothing to drain so a follow-up
        # ``spawn`` (e.g. from a stale callback) is rejected consistently.
        self._closed = True
        if not self._tasks:
            return

        # Snapshot so we don't iterate a set that the done-callbacks are
        # concurrently shrinking.
        pending = set(self._tasks)

        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True),
                timeout=self.drain_timeout,
            )
        except TimeoutError:
            logger.warning(
                "Background tasks did not finish before drain timeout; cancelling",
                extra={
                    "timeout_seconds": self.drain_timeout,
                    "outstanding": len(pending),
                },
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    @property
    def active_count(self) -> int:
        """Number of tasks currently tracked (not yet completed)."""
        return len(self._tasks)

    @property
    def is_closed(self) -> bool:
        """Whether ``drain`` has been entered.

        Callers can probe this to avoid scheduling work that is
        guaranteed to be rejected by :meth:`spawn`. The flag never flips
        back to ``False``; the registry is single-use.
        """
        return self._closed


def _make_task_context(correlation_id: str) -> contextvars.Context:
    """Return a snapshot of the current context with a fresh correlation ID.

    ``asyncio.create_task(..., context=ctx)`` runs the task inside ``ctx``,
    which keeps the spawning request's context untouched while giving the
    background task its own correlation ID for log aggregation. Using a
    context object avoids wrapping the coroutine in a second
    ``async def`` whose own coroutine handle could trigger an
    ``unawaited coroutine`` warning if the task is GC'd before the loop
    schedules it.
    """
    ctx = contextvars.copy_context()
    ctx.run(_correlation_id_var.set, correlation_id)
    return ctx


def spawn_or_fallback(
    registry: BackgroundTaskRegistry | None,
    coro: Coroutine[Any, Any, Any],
    *,
    name: str | None = None,
) -> asyncio.Task[Any]:
    """Schedule ``coro`` via ``registry`` when available, else bare ``create_task``.

    Centralizes the fallback used by services that may run inside the
    container (with a real registry) or as bare ``object.__new__`` stubs in
    unit tests (with no registry). Keeping the branch in one place stops
    each new long-lived service from hand-rolling the same ``if registry is
    not None`` pattern.

    Each spawned task gets its own correlation ID so background work does
    not pollute the spawning request's log timeline. The ID lives in a
    cloned ``contextvars.Context`` passed to ``asyncio.create_task`` so
    the spawning request's context is left untouched.
    """
    correlation_id = name or f"task-{uuid.uuid4()}"
    task_context = _make_task_context(correlation_id)
    if registry is not None:
        return registry.spawn(coro, name=name, context=task_context)
    logger.warning(
        "spawn_or_fallback invoked without a BackgroundTaskRegistry; the "
        "task will not be drained on shutdown",
        extra={"task_name": name},
    )
    return asyncio.create_task(coro, name=name, context=task_context)
