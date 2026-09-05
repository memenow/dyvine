"""Watch-mode domain service.

`WatchService` turns Dyvine from a request-driven downloader into a
monitor: each subscription runs a long-lived ``asyncio`` loop that, on two
independent cadences, checks whether a Douyin user is live (and records the
stream) and fetches the user's new posts.

Design:

- **One loop task per subscription.** ``DELETE /watch/{id}`` cancels
  exactly that task; an exception in one user's loop cannot stall another.
  Tasks are scheduled through the shared ``BackgroundTaskRegistry`` so the
  FastAPI lifespan drains them on shutdown.
- **Persistence + resume.** Subscriptions live in ``WatchRepository``
  (a dedicated Postgres table), so ``resume_persisted`` can re-arm every
  enabled subscription after a restart -- something the operation store
  cannot do because it has no enumeration query and its boot sweep would
  fail any long-lived row.
- **Reuse, not reinvention.** Live recording delegates to
  ``LivestreamService.download_stream`` (which dedupes by ``room_id`` and
  rejects offline users), and incremental post fetching delegates to
  ``PostService.download_new_posts``. Each triggered download therefore
  creates its own regular operation row that existing polling endpoints can
  observe.
- **Monotonic scheduling.** Due-times use ``time.monotonic`` (consistent
  with the app's uptime accounting) so NTP steps cannot cause negative
  sleeps or catch-up storms. Each due-time carries +/-10% jitter so several
  subscriptions never hammer upstream in lockstep.
- **Split-brain-free scaling.** API replicas run with ``run_loops=False``
  (``WATCH_ENABLED=false``): CRUD still works against shared Postgres,
  but loops run on exactly one watcher replica, which adopts new rows
  and drops deleted ones through periodic ``reconcile_loops`` passes.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from datetime import UTC, datetime
from typing import Any

from ..core.background import BackgroundTaskRegistry, spawn_or_fallback
from ..core.exceptions import (
    LivestreamError,
    ServiceError,
    UserNotFoundError,
    WatchDuplicateError,
    WatchSubscriptionNotFoundError,
)
from ..core.logging import ContextLogger
from ..core.settings import settings
from ..db import WatchRepository, WatchSubscriptionRecord
from ..schemas.watch import WatchSubscriptionResponse
from .livestreams import LivestreamService
from .posts import PostService

logger = ContextLogger(__name__)

#: Consecutive loop crashes tolerated before the supervisor parks the
#: subscription (delete + recreate to resume polling).
_MAX_CONSECUTIVE_CRASHES = 5

#: Backoff ceiling between crash restarts (5 minutes).
_MAX_CRASH_BACKOFF_SECONDS = 300.0


def _crash_backoff_seconds(consecutive_crashes: int) -> float:
    """Return the restart delay after ``consecutive_crashes`` crashes.

    Exponential from a 30s base (one reconcile interval), capped so a
    flapping loop never sleeps longer than five minutes between tries.
    """
    return min(
        _MAX_CRASH_BACKOFF_SECONDS, 30.0 * 2.0 ** max(0, consecutive_crashes - 1)
    )


class WatchService:
    """Schedule and persist per-user watch subscriptions.

    The service owns a dict of ``subscription_id -> asyncio.Task`` watcher
    loops plus an ``asyncio.Lock`` (created lazily, so the service stays
    usable from ``object.__new__`` stubs in unit tests) that serialises the
    check-and-create window for idempotent subscription creation.
    """

    # Class-level default so tests that build the service via
    # ``object.__new__`` still see a ``None`` registry and fall through to
    # the bare ``create_task`` branch in :func:`spawn_or_fallback`.
    _task_registry: BackgroundTaskRegistry | None = None

    def __init__(
        self,
        *,
        watch_store: WatchRepository,
        livestream_service: LivestreamService,
        post_service: PostService,
        task_registry: BackgroundTaskRegistry | None = None,
        run_loops: bool = True,
    ) -> None:
        """Initialize the watch service from injected dependencies.

        Args:
            run_loops: When ``False`` (``WATCH_ENABLED=false`` on API
                replicas) the CRUD surface keeps working against shared
                Postgres but no watcher loop is ever started locally;
                the dedicated watcher replica adopts new rows through
                :meth:`reconcile_loops`.
        """
        self.settings = settings
        self.watch_store = watch_store
        self.livestream_service = livestream_service
        self.post_service = post_service
        self._task_registry = task_registry
        self._run_loops = run_loops
        self._loops: dict[str, asyncio.Task[Any]] = {}
        # Supervisor state: consecutive unexplained loop exits per
        # subscription, and when the latest one happened. A loop that
        # survives a full reconcile interval resets its count; one that
        # keeps crashing backs off exponentially and is parked after
        # ``_MAX_CONSECUTIVE_CRASHES`` until the subscription is
        # deleted and recreated (delete clears the crash budget).
        self._crash_counts: dict[str, int] = {}
        self._last_crash_monotonic: dict[str, float] = {}
        self._lock: asyncio.Lock | None = None

    # ------------------------------------------------------------------
    # Public CRUD surface (called by the router)
    # ------------------------------------------------------------------

    async def create_subscription(
        self,
        *,
        user_id: str,
        live_poll_seconds: int | None = None,
        post_poll_seconds: int | None = None,
        backfill_on_create: bool | None = None,
    ) -> tuple[WatchSubscriptionRecord, bool]:
        """Create (or return the existing) watch subscription for a user.

        Idempotent on ``user_id``: a second create for the same user returns
        the existing subscription rather than erroring, backed by the
        ``UNIQUE(user_id)`` index. When ``backfill_on_create`` is false the
        current feed is snapshotted into the checkpoint up front so the
        first post check skips the user's existing posts and downloads only
        what is published afterwards.

        Returns:
            A ``(record, created)`` tuple; ``created`` is ``True`` for a new
            subscription and ``False`` when an existing one was returned.

        Raises:
            RateLimitError: When the configured ``max_subscriptions`` cap is
                reached (mapped to HTTP 429).
            UserNotFoundError / PostServiceError: When a non-backfill
                baseline snapshot cannot be taken (the user is unknown or
                upstream failed), so a faulty subscription is never created.
        """
        watch_cfg = self.settings.watch
        live = live_poll_seconds or watch_cfg.live_poll_seconds
        post = post_poll_seconds or watch_cfg.post_poll_seconds
        backfill = (
            watch_cfg.backfill_on_create
            if backfill_on_create is None
            else backfill_on_create
        )

        # Fast path: if a subscription already exists for this user, return it
        # idempotently WITHOUT paying for -- or failing on -- a baseline
        # snapshot. Without this, a transient profile/upstream error during the
        # snapshot would turn an idempotent retry into an error instead of the
        # documented 200-with-existing-row. The locked re-check below remains
        # the authority against a concurrent create.
        existing = await self.watch_store.get_subscription_by_user(user_id)
        if existing is not None:
            self._start_loop(existing)
            return existing, False

        # Snapshot the current feed BEFORE taking the lock so a non-backfill
        # subscription records a baseline of existing aweme_ids without
        # holding the lock across an upstream call. The newest-first feed
        # means recording just the first page is enough: the first post
        # check will early-exit at that boundary and download nothing older.
        baseline: list[str] = []
        if not backfill:
            baseline = await self._snapshot_baseline(user_id)

        async with self._get_lock():
            existing = await self.watch_store.get_subscription_by_user(user_id)
            if existing is not None:
                # Self-heal: re-arm the loop if a prior one crashed/exited.
                self._start_loop(existing)
                return existing, False

            checkpoint: dict[str, Any] = {
                "newest_aweme_id": baseline[0] if baseline else None,
                "recent_aweme_ids": baseline,
                "first_run_complete": not backfill,
            }
            try:
                # Capped insert: the cap check and the row insert are one
                # atomic unit inside the repository (advisory-locked on
                # Postgres), so concurrent creators on other replicas
                # cannot both slip under a stale count. A separate
                # ``count_subscriptions`` check here would reintroduce
                # exactly that race.
                record = await self.watch_store.create_subscription_capped(
                    user_id=user_id,
                    live_poll_seconds=live,
                    post_poll_seconds=post,
                    checkpoint=checkpoint,
                    max_subscriptions=watch_cfg.max_subscriptions,
                )
            except WatchDuplicateError:
                # Cross-process race: a sibling replica won the
                # ``UNIQUE(user_id)`` insert between our re-check and
                # our write (the lock above is per-process). Converge
                # to the documented idempotent return instead of
                # letting a 409/500 escape for a retryable create.
                winner = await self.watch_store.get_subscription_by_user(user_id)
                if winner is None:  # deleted between our write and re-read
                    raise
                self._start_loop(winner)
                return winner, False

        self._start_loop(record)
        logger.info(
            "watch subscription created",
            extra={
                "subscription_id": record.subscription_id,
                "user_id": user_id,
                "backfill": backfill,
            },
        )
        return record, True

    async def get_subscription(self, subscription_id: str) -> WatchSubscriptionRecord:
        """Return a subscription or raise ``WatchSubscriptionNotFoundError``."""
        return await self.watch_store.get_subscription(subscription_id)

    async def list_subscriptions(self) -> list[WatchSubscriptionRecord]:
        """Return every persisted subscription."""
        return await self.watch_store.list_subscriptions()

    async def delete_subscription(self, subscription_id: str) -> None:
        """Cancel a subscription's watcher loop and delete its record.

        Note: a livestream recording already in progress runs in its own
        background task and continues to completion; deleting the
        subscription only stops *future* checks.

        Raises:
            WatchSubscriptionNotFoundError: If no subscription matches.
        """
        # Serialise with create_subscription under the same lock so a
        # concurrent POST for this user cannot observe (and re-arm) a row that
        # this delete is removing. Validate existence first so an unknown id
        # surfaces as 404 before we touch the loop registry.
        async with self._get_lock():
            await self.watch_store.get_subscription(subscription_id)
            await self._cancel_loop(subscription_id)
            await self.watch_store.delete_subscription(subscription_id)
        logger.info(
            "watch subscription deleted", extra={"subscription_id": subscription_id}
        )

    # ------------------------------------------------------------------
    # Lifecycle (called by ServiceContainer)
    # ------------------------------------------------------------------

    async def resume_persisted(self) -> int:
        """Re-arm a watcher loop for every enabled persisted subscription.

        Returns the number of loops started. Called from
        ``ServiceContainer.initialize`` after the container is marked ready.
        A no-op returning ``0`` when this replica does not run loops.
        """
        if not self._run_loops:
            return 0
        records = await self.watch_store.list_subscriptions(enabled_only=True)
        for record in records:
            self._start_loop(record)
        if records:
            logger.info("resumed watch subscriptions", extra={"count": len(records)})
        return len(records)

    async def reconcile_loops(self) -> tuple[int, int]:
        """Adopt new rows and drop dead ones; return ``(started, stopped)``.

        The watcher replica runs this periodically so subscriptions
        created through a (CRUD-only) API replica start looping without
        a watcher restart, and rows deleted or disabled elsewhere stop
        promptly. Crashed loops (done tasks) restart here under the
        supervisor policy: exponential backoff, a consecutive-crash
        cap after which the loop is parked with an alert log, and a
        reset once a restarted loop survives a full interval.
        """
        if not self._run_loops:
            return (0, 0)
        now = time.monotonic()
        records = {
            record.subscription_id: record
            for record in await self.watch_store.list_subscriptions()
        }
        stopped = 0
        for subscription_id in list(self._loops):
            record = records.get(subscription_id)
            task = self._loops.get(subscription_id)
            if record is None or not record.enabled:
                await self._cancel_loop(subscription_id)
                stopped += 1
            elif task is not None and task.done():
                self._loops.pop(subscription_id, None)
                if task.cancelled():
                    # Deliberately stopped elsewhere; not a crash.
                    continue
                stopped += self._note_crash(subscription_id, task, now=now)
            elif task is not None and subscription_id in self._crash_counts:
                # Survived a full reconcile interval: healthy again.
                self._crash_counts.pop(subscription_id, None)
                self._last_crash_monotonic.pop(subscription_id, None)
        started = 0
        for subscription_id, record in records.items():
            if not record.enabled:
                continue
            task = self._loops.get(subscription_id)
            if task is not None and not task.done():
                continue
            if not self._restart_allowed(subscription_id, now=now):
                continue
            self._start_loop(record)
            started += 1
        if started or stopped:
            logger.info(
                "reconciled watch loops",
                extra={"started": started, "stopped": stopped},
            )
        return (started, stopped)

    def _note_crash(
        self, subscription_id: str, task: asyncio.Task[Any], *, now: float
    ) -> int:
        """Record a crashed loop; return 1 when it is parked for good.

        Returns 0 when the loop stays eligible for restart (possibly
        after a backoff delay enforced by :meth:`_restart_allowed`).
        Only called for done, non-cancelled tasks. The loop swallows
        its own exceptions and returns the root cause, so the task
        result carries it; a task that raised instead (never in
        production, but possible in tests) surfaces it via re-raise.
        """
        count = self._crash_counts.get(subscription_id, 0) + 1
        self._crash_counts[subscription_id] = count
        self._last_crash_monotonic[subscription_id] = now
        try:
            result = task.result()
        except BaseException as raised:
            failure: BaseException | None = raised
        else:
            failure = result if isinstance(result, BaseException) else None
        if count > _MAX_CONSECUTIVE_CRASHES:
            logger.error(
                "watch loop parked after repeated crashes; "
                "delete and recreate the subscription once fixed",
                extra={
                    "subscription_id": subscription_id,
                    "consecutive_crashes": count,
                    "last_error": repr(failure),
                },
            )
            return 1
        logger.warning(
            "watch loop crashed; restarting under backoff",
            extra={
                "subscription_id": subscription_id,
                "attempt": count,
                "max_attempts": _MAX_CONSECUTIVE_CRASHES,
                "backoff_seconds": _crash_backoff_seconds(count),
                "last_error": repr(failure),
            },
        )
        return 0

    def _restart_allowed(self, subscription_id: str, *, now: float) -> bool:
        """Return whether a crashed loop may restart on this pass."""
        count = self._crash_counts.get(subscription_id, 0)
        if count == 0:
            return True
        if count > _MAX_CONSECUTIVE_CRASHES:
            return False
        last_crash = self._last_crash_monotonic.get(subscription_id, 0.0)
        return now - last_crash >= _crash_backoff_seconds(count)

    async def run_reconcile_forever(self, *, interval_seconds: float = 30.0) -> None:
        """Loop :meth:`reconcile_loops` until cancelled.

        A failed pass is logged and skipped so one transient database
        error does not stop subscription adoption and crash
        supervision permanently. Cancellation propagates to the
        caller, so the container stops the loop with a plain
        ``task.cancel()`` during shutdown.
        """
        while True:
            try:
                await self.reconcile_loops()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "watch reconcile pass failed; continuing on next interval",
                    extra={"interval_seconds": interval_seconds},
                )
            await asyncio.sleep(interval_seconds)

    async def stop_all(self) -> None:
        """Cancel every watcher loop.

        Called at the very start of ``ServiceContainer.shutdown`` so the
        loops stop scheduling new downloads before the background-task
        registry is drained.
        """
        for subscription_id in list(self._loops):
            await self._cancel_loop(subscription_id)

    @property
    def active_count(self) -> int:
        """Number of watcher loops currently running."""
        return sum(1 for task in self._loops.values() if not task.done())

    def to_response(self, record: WatchSubscriptionRecord) -> WatchSubscriptionResponse:
        """Render a subscription record as the public response model."""
        checkpoint = record.checkpoint or {}
        newest = checkpoint.get("newest_aweme_id")
        return WatchSubscriptionResponse(
            subscription_id=record.subscription_id,
            user_id=record.user_id,
            enabled=record.enabled,
            live_poll_seconds=record.live_poll_seconds,
            post_poll_seconds=record.post_poll_seconds,
            last_live_check=record.last_live_check,
            last_post_check=record.last_post_check,
            newest_aweme_id=str(newest) if newest is not None else None,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_lock(self) -> asyncio.Lock:
        """Return the create lock, creating it lazily on first use.

        ``asyncio.Lock`` binds to the running event loop, so constructing it
        on first access keeps the service usable from bare ``object.__new__``
        stubs in unit tests and avoids tying it to a specific loop.
        """
        lock = self._lock
        if lock is None:
            lock = asyncio.Lock()
            self._lock = lock
        return lock

    def _start_loop(self, record: WatchSubscriptionRecord) -> None:
        """Spawn the watcher loop for a subscription if not already running.

        A no-op on CRUD-only replicas (``run_loops=False``): the row
        persists, and the watcher replica adopts it on reconcile. Also
        a no-op while the supervisor budget forbids a restart (backoff
        window or parked after repeated crashes): an idempotent create
        retry must not silently re-arm a parked loop — only
        delete-and-recreate resumes it.
        """
        if not self._run_loops:
            return
        existing = self._loops.get(record.subscription_id)
        if existing is not None and not existing.done():
            return
        if not self._restart_allowed(record.subscription_id, now=time.monotonic()):
            return
        task = spawn_or_fallback(
            self._task_registry,
            self._watch_loop(record.subscription_id),
            name=f"watch-{record.subscription_id}",
        )
        self._loops[record.subscription_id] = task

    async def _cancel_loop(self, subscription_id: str) -> None:
        """Cancel and await a subscription's watcher loop if present.

        Also clears the supervisor's crash budget: a deliberate stop
        (delete, disable, reconcile-drop) is not a crash, so a later
        re-enable starts from a clean slate.
        """
        task = self._loops.pop(subscription_id, None)
        self._crash_counts.pop(subscription_id, None)
        self._last_crash_monotonic.pop(subscription_id, None)
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _watch_loop(self, subscription_id: str) -> BaseException | None:
        """Drive one subscription's live + post cadences until cancelled.

        Each cadence has its own monotonic due-time recomputed from the
        moment work *starts*, so a slow check pushes the next tick later
        rather than letting ticks pile up. Both due-times start "now" so a
        freshly created or resumed subscription is checked immediately.

        Live and post checks run serially in this single loop and the post
        check awaits its download inline, so a long post run (a fresh backfill
        or catch-up after downtime) delays *this* subscription's next live
        check until it finishes. That is an accepted trade-off of the
        single-loop design: livestreams are long enough that a few minutes'
        detection delay is tolerable, and every subscription has its own loop,
        so one user's backfill never stalls another user's live detection.

        Returns the root-cause exception when the loop crashes (the task
        stays registered so reconcile reaps it via ``_note_crash``), and
        ``None`` on a clean exit (deleted/disabled subscription).
        """
        next_live = time.monotonic()
        next_post = time.monotonic()
        crashed: BaseException | None = None
        try:
            while True:
                try:
                    record = await self.watch_store.get_subscription(subscription_id)
                except WatchSubscriptionNotFoundError:
                    return None  # deleted out from under us
                if not record.enabled:
                    return None

                now = time.monotonic()
                sleep_for = min(next_live - now, next_post - now)
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)

                now = time.monotonic()
                if now >= next_live:
                    next_live = now + self._jittered_interval(record.live_poll_seconds)
                    await self._do_live_check(record)
                if now >= next_post:
                    next_post = now + self._jittered_interval(record.post_poll_seconds)
                    await self._do_post_check(record)
        except asyncio.CancelledError:
            # Graceful stop: do not persist a partial checkpoint here. Any
            # download triggered this cycle is its own registry-tracked task
            # and drains independently.
            raise
        except Exception as exc:
            logger.exception(
                "watch loop crashed; the reconcile supervisor will retry "
                "it with backoff, or park it after repeated crashes",
                extra={"subscription_id": subscription_id},
            )
            crashed = exc
        finally:
            # Evict on clean exit (deleted/disabled/cancelled) only when
            # the registry slot is still us, mirroring the livestream
            # downloader's successor-safe cleanup. A crashed loop stays
            # registered so the next reconcile pass reaps it through
            # ``_note_crash`` (backoff/park policy) instead of
            # restarting it immediately with no alert.
            if (
                crashed is None
                and self._loops.get(subscription_id) is asyncio.current_task()
            ):
                self._loops.pop(subscription_id, None)
        if crashed is not None:
            return crashed
        return None

    async def _do_live_check(self, record: WatchSubscriptionRecord) -> None:
        """Check whether the user is live and, if so, start a recording.

        Reuses ``LivestreamService.download_stream`` which raises
        ``LivestreamError`` when the user is offline or already being
        recorded (deduped by ``room_id``); both are normal and swallowed.
        """
        try:
            await self.watch_store.update_subscription(
                record.subscription_id, last_live_check=self._now_iso()
            )
            await self.livestream_service.download_stream(
                url=f"https://www.douyin.com/user/{record.user_id}"
            )
            logger.info(
                "watch: livestream recording started",
                extra={
                    "subscription_id": record.subscription_id,
                    "user_id": record.user_id,
                },
            )
        except asyncio.CancelledError:
            raise
        except WatchSubscriptionNotFoundError:
            return
        except LivestreamError:
            return  # offline, no stream, or already recording -> skip
        except RuntimeError:
            # Usually the background registry closing during shutdown,
            # but a persistent non-shutdown RuntimeError would otherwise
            # loop forever with zero observability, so always log it.
            logger.warning(
                "watch live check failed",
                extra={"subscription_id": record.subscription_id},
                exc_info=True,
            )
            return
        except Exception:
            logger.warning(
                "watch live check failed",
                extra={"subscription_id": record.subscription_id},
                exc_info=True,
            )

    async def _do_post_check(self, record: WatchSubscriptionRecord) -> None:
        """Download the user's new posts and advance the checkpoint.

        The checkpoint is only advanced after a successful run, so an
        interrupted cycle re-downloads at most the most-recent window (which
        the f2 downloader overwrites idempotently) rather than skipping
        posts that were never fetched.
        """
        checkpoint = record.checkpoint or {}
        try:
            await self.watch_store.update_subscription(
                record.subscription_id, last_post_check=self._now_iso()
            )
            result = await self.post_service.download_new_posts(
                record.user_id,
                since_aweme_id=checkpoint.get("newest_aweme_id"),
                known_aweme_ids=set(checkpoint.get("recent_aweme_ids") or []),
                subscription_id=record.subscription_id,
            )
        except asyncio.CancelledError:
            raise
        except WatchSubscriptionNotFoundError:
            return
        except (UserNotFoundError, ServiceError):
            return  # upstream/profile failure -> keep checkpoint, retry later
        except RuntimeError:
            # Same observability rule as the live check: never swallow
            # silently, even though shutdown is the usual cause.
            logger.warning(
                "watch post check failed",
                extra={"subscription_id": record.subscription_id},
                exc_info=True,
            )
            return
        except Exception:
            logger.warning(
                "watch post check failed",
                extra={"subscription_id": record.subscription_id},
                exc_info=True,
            )
            return

        if result.failed_count or result.truncated:
            # Hold the checkpoint whenever the fetched window was incomplete:
            # either a post failed to download, or pagination hit the page cap
            # with more pages still available. The newest-first early-stop
            # would otherwise advance the boundary past a never-stored post and
            # skip it forever; instead the next cycle re-scans this window (f2
            # overwrites already-fetched posts idempotently) and retries. This
            # is the "advance only after a successful run" contract this
            # method's docstring promises.
            logger.warning(
                "watch: holding checkpoint after incomplete post run",
                extra={
                    "subscription_id": record.subscription_id,
                    "new_count": result.new_count,
                    "failed_count": result.failed_count,
                    "truncated": result.truncated,
                },
            )
            return

        if result.new_count <= 0:
            return

        merged = self._merge_recent(
            result.seen_aweme_ids, checkpoint.get("recent_aweme_ids") or []
        )
        with contextlib.suppress(WatchSubscriptionNotFoundError):
            await self.watch_store.update_subscription(
                record.subscription_id,
                checkpoint={
                    "newest_aweme_id": result.newest_aweme_id,
                    "recent_aweme_ids": merged,
                    "first_run_complete": True,
                },
            )
        logger.info(
            "watch: downloaded new posts",
            extra={
                "subscription_id": record.subscription_id,
                "new_count": result.new_count,
            },
        )

    async def _snapshot_baseline(self, user_id: str) -> list[str]:
        """Return the current first page of aweme_ids (newest first).

        Used to seed a non-backfill subscription's dedupe set so its first
        post check skips existing posts. Upstream failures propagate so a
        subscription that promises "new posts only" is not created on a
        broken baseline.
        """
        page = await self.post_service.get_user_posts(
            user_id, max_cursor=0, count=self.settings.watch.recent_id_cap
        )
        ids = [str(post.aweme_id) for post in page.posts if post.aweme_id]
        return ids[: self.settings.watch.recent_id_cap]

    def _merge_recent(self, new_ids: list[str], existing: list[str]) -> list[str]:
        """Merge newly downloaded ids ahead of existing ones, bounded by cap.

        Membership (not numeric ordering) is the dedupe oracle because
        Douyin aweme_ids are only roughly time-ordered; pinned or
        re-published posts can break a strict ``>`` comparison.
        """
        cap = self.settings.watch.recent_id_cap
        merged: list[str] = []
        seen: set[str] = set()
        for aweme_id in (*new_ids, *existing):
            if aweme_id and aweme_id not in seen:
                seen.add(aweme_id)
                merged.append(aweme_id)
            if len(merged) >= cap:
                break
        return merged

    def _jittered_interval(self, seconds: int) -> float:
        """Return the interval with +/-10% jitter to avoid lockstep polling."""
        return seconds * random.uniform(0.9, 1.1)

    @staticmethod
    def _now_iso() -> str:
        """Return the current UTC timestamp in ISO 8601 format."""
        return datetime.now(UTC).isoformat()
