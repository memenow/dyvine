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
    RateLimitError,
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
    ) -> None:
        """Initialize the watch service from injected dependencies."""
        self.settings = settings
        self.watch_store = watch_store
        self.livestream_service = livestream_service
        self.post_service = post_service
        self._task_registry = task_registry
        self._loops: dict[str, asyncio.Task[Any]] = {}
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

            count = await self.watch_store.count_subscriptions()
            if count >= watch_cfg.max_subscriptions:
                raise RateLimitError(
                    "Watch subscription limit reached "
                    f"({watch_cfg.max_subscriptions}); delete a subscription first"
                )

            checkpoint: dict[str, Any] = {
                "newest_aweme_id": baseline[0] if baseline else None,
                "recent_aweme_ids": baseline,
                "first_run_complete": not backfill,
            }
            try:
                record = await self.watch_store.create_subscription(
                    user_id=user_id,
                    live_poll_seconds=live,
                    post_poll_seconds=post,
                    checkpoint=checkpoint,
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
        """
        records = await self.watch_store.list_subscriptions(enabled_only=True)
        for record in records:
            self._start_loop(record)
        if records:
            logger.info("resumed watch subscriptions", extra={"count": len(records)})
        return len(records)

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
        """Spawn the watcher loop for a subscription if not already running."""
        existing = self._loops.get(record.subscription_id)
        if existing is not None and not existing.done():
            return
        task = spawn_or_fallback(
            self._task_registry,
            self._watch_loop(record.subscription_id),
            name=f"watch-{record.subscription_id}",
        )
        self._loops[record.subscription_id] = task

    async def _cancel_loop(self, subscription_id: str) -> None:
        """Cancel and await a subscription's watcher loop if present."""
        task = self._loops.pop(subscription_id, None)
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _watch_loop(self, subscription_id: str) -> None:
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
        """
        next_live = time.monotonic()
        next_post = time.monotonic()
        try:
            while True:
                try:
                    record = await self.watch_store.get_subscription(subscription_id)
                except WatchSubscriptionNotFoundError:
                    return  # deleted out from under us
                if not record.enabled:
                    return

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
        except Exception:
            logger.exception(
                "watch loop crashed; it will be resumed on next restart",
                extra={"subscription_id": subscription_id},
            )
        finally:
            # Self-evict only if the registry slot is still us, mirroring the
            # livestream downloader's successor-safe cleanup.
            if self._loops.get(subscription_id) is asyncio.current_task():
                self._loops.pop(subscription_id, None)

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
            return  # background registry closed during shutdown
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
