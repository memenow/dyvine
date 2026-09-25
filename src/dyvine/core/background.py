"""Registry for tracking long-lived asyncio background tasks.

Bulk downloads outlive the single tool call that starts them, so bare
``asyncio.create_task`` handles would be tracked nowhere: on process
shutdown the executor pools were reaped before the tasks drained,
which produced ``RuntimeError: cannot schedule new futures after
shutdown`` inside active R2 uploads / audit writes.

``BackgroundTaskRegistry`` centralizes spawn and drain so shutdown
waits for active downloads before reaping the executor pools. State
always lands in Postgres first, so a gateway restart loses at most
in-flight progress, never records.
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
    """Track long-lived ``asyncio.Task`` handles so shutdown can drain them.

    Tasks are registered via :meth:`spawn`, auto-removed from the tracking set
    on completion (failures are logged with their traceback at that
    point, so no exception is ever left unretrieved), and drained by
    :meth:`drain` during shutdown. ``drain`` stays open to follow-up
    work spawned by the tasks it waits for; only once :meth:`drain`
    has finished is the registry closed, and subsequent :meth:`spawn`
    calls raise ``RuntimeError`` rather than silently leaking work
    past the executor teardown that follows.

    Attributes:
        drain_timeout: Maximum seconds ``drain`` waits for tasks to finish
            gracefully before cancelling anything still outstanding.
        cancel_timeout: Maximum seconds ``drain`` waits after cancelling
            before abandoning tasks that ignore cancellation.
    """

    def __init__(
        self, *, drain_timeout: float = 20.0, cancel_timeout: float = 5.0
    ) -> None:
        """Initialize the registry with no tracked tasks.

        The defaults fit inside a conventional 25s graceful-shutdown
        window so a drain never gets SIGKILLed mid-flight.
        """
        self._tasks: set[asyncio.Task[Any]] = set()
        self.drain_timeout = drain_timeout
        self.cancel_timeout = cancel_timeout
        # Set while ``drain`` runs so ``spawn`` can warn about
        # shutdown-time follow-ups (which still join the drain set);
        # ``_closed`` is set when ``drain`` finishes so any post-drain
        # ``spawn`` is rejected explicitly rather than being added to
        # a registry nobody will await again.
        self._draining = False
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
                "after drain has finished"
            )
        task = asyncio.create_task(coro, name=name, context=context)
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)
        if self._draining:
            logger.warning(
                "Task spawned while the registry is draining; it joins the drain set",
                extra={"task_name": task.get_name()},
            )
        return task

    def _on_task_done(self, task: asyncio.Task[Any]) -> None:
        """Drop a finished task and record its failure, if any.

        Retrieving the exception here (rather than only for tasks
        inside a ``drain`` snapshot) keeps ``asyncio`` from emitting
        ``Task exception was never retrieved`` for failures that land
        between drains. A failure that reaches the task boundary is
        unhandled by definition, so it is logged as an error; awaiters
        still observe the exception itself.
        """
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(
                "Background task failed",
                extra={
                    "task_name": task.get_name(),
                    "error": f"{type(exc).__name__}: {exc}",
                },
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    async def drain(self) -> None:
        """Wait for tracked tasks to finish, cancelling anything that overruns.

        Called during host shutdown before the executor pools are torn
        down so in-flight dispatches can resolve. Follow-up work
        spawned by drained tasks joins the drain set (bounded by the
        overall ``drain_timeout``); tasks still outstanding past the
        deadline are cancelled and given ``cancel_timeout`` more
        seconds, after which stragglers that ignore cancellation are
        logged and abandoned so shutdown cannot hang. The registry is
        marked closed only when draining finishes.

        Raises:
            RuntimeError: If another ``drain`` is already in progress.
        """
        if self._closed:
            return
        if self._draining:
            raise RuntimeError("BackgroundTaskRegistry drain already in progress")
        self._draining = True
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.drain_timeout
            # Re-snapshot every round: the done-callback shrinks the set
            # while follow-up spawns grow it.
            while self._tasks:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                await asyncio.wait(set(self._tasks), timeout=remaining)
            if not self._tasks:
                return
            logger.warning(
                "Background tasks did not finish before drain timeout; cancelling",
                extra={
                    "timeout_seconds": self.drain_timeout,
                    "outstanding": len(self._tasks),
                },
            )
            for task in set(self._tasks):
                task.cancel()
            _, still_pending = await asyncio.wait(
                set(self._tasks), timeout=self.cancel_timeout
            )
            if still_pending:
                logger.error(
                    "Background tasks ignored cancellation; abandoning",
                    extra={
                        "tasks": sorted(t.get_name() for t in still_pending),
                    },
                )
        finally:
            self._draining = False
            self._closed = True

    @property
    def active_count(self) -> int:
        """Number of tasks currently tracked (not yet completed)."""
        return len(self._tasks)

    @property
    def is_closed(self) -> bool:
        """Whether ``drain`` has finished.

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
    the spawning request's context is left untouched. The ID is always
    unique per spawn (a task ``name`` is only a prefix) so two tasks
    sharing a name never share a log timeline.
    """
    if name:
        correlation_id = f"{name}-{uuid.uuid4().hex[:8]}"
    else:
        correlation_id = f"task-{uuid.uuid4()}"
    task_context = _make_task_context(correlation_id)
    if registry is not None:
        return registry.spawn(coro, name=name, context=task_context)
    logger.warning(
        "spawn_or_fallback invoked without a BackgroundTaskRegistry; the "
        "task will not be drained on shutdown",
        extra={"task_name": name},
    )
    return asyncio.create_task(coro, name=name, context=task_context)
