"""Watch-mode FastAPI router.

Endpoints under ``/api/v1/watch`` manage long-lived watch subscriptions
that auto-download a Douyin user's new posts and record their livestreams:

- ``POST /watch`` — create a subscription (idempotent on ``user_id``: 201
  for a new subscription, 200 when one already exists).
- ``GET /watch`` — list every subscription.
- ``GET /watch/{subscription_id}`` — fetch one subscription.
- ``DELETE /watch/{subscription_id}`` — stop a subscription's watcher loop
  and delete its record. A livestream recording already in progress runs in
  its own background task and continues to completion; deletion only stops
  future checks.

``WatchSubscriptionNotFoundError`` (a ``NotFoundError``) maps to 404 and the
``max_subscriptions`` guardrail raises ``RateLimitError`` (429) through the
shared ``handle_errors`` decorator, so no per-handler error mapping is
needed here.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Response, status

from ..core.decorators import handle_errors
from ..core.dependencies import get_watch_service, require_api_key
from ..core.logging import ContextLogger
from ..schemas.watch import (
    WatchSubscriptionCreate,
    WatchSubscriptionList,
    WatchSubscriptionResponse,
)
from ..services.watch import WatchService

router = APIRouter(
    prefix="/watch",
    tags=["watch"],
    dependencies=[Depends(require_api_key)],
)
logger = ContextLogger(__name__)

_SUBSCRIPTION_ID_PATTERN = r"^[A-Za-z0-9_\-]{8,128}$"


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=WatchSubscriptionResponse,
    responses={
        200: {"description": "A subscription for this user already exists"},
        401: {"description": "Missing or invalid API key"},
        422: {"description": "Invalid user_id or poll interval"},
        429: {"description": "Watch subscription limit reached"},
    },
)
@handle_errors(logger=logger)
async def create_watch_subscription(
    request: WatchSubscriptionCreate,
    service: Annotated[WatchService, Depends(get_watch_service)],
    response: Response,
) -> WatchSubscriptionResponse:
    """Create a watch subscription for a user (idempotent on user_id)."""
    logger.info(
        "Processing create_watch_subscription request",
        extra={"user_id": request.user_id},
    )
    async with logger.track_time("create_watch_subscription"):
        record, created = await service.create_subscription(
            user_id=request.user_id,
            live_poll_seconds=request.live_poll_seconds,
            post_poll_seconds=request.post_poll_seconds,
            backfill_on_create=request.backfill_on_create,
        )
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return service.to_response(record)


@router.get(
    "",
    response_model=WatchSubscriptionList,
    responses={401: {"description": "Missing or invalid API key"}},
)
@handle_errors(logger=logger)
async def list_watch_subscriptions(
    service: Annotated[WatchService, Depends(get_watch_service)],
) -> WatchSubscriptionList:
    """List all watch subscriptions."""
    logger.info("Processing list_watch_subscriptions request")
    async with logger.track_time("list_watch_subscriptions"):
        records = await service.list_subscriptions()
    items = [service.to_response(record) for record in records]
    return WatchSubscriptionList(subscriptions=items, total=len(items))


@router.get(
    "/{subscription_id}",
    response_model=WatchSubscriptionResponse,
    responses={
        401: {"description": "Missing or invalid API key"},
        404: {"description": "Subscription not found"},
    },
)
@handle_errors(logger=logger)
async def get_watch_subscription(
    service: Annotated[WatchService, Depends(get_watch_service)],
    subscription_id: str = Path(
        ...,
        pattern=_SUBSCRIPTION_ID_PATTERN,
        description="The unique identifier of the watch subscription",
    ),
) -> WatchSubscriptionResponse:
    """Get a single watch subscription by id."""
    logger.info(
        "Processing get_watch_subscription request",
        extra={"subscription_id": subscription_id},
    )
    async with logger.track_time("get_watch_subscription"):
        record = await service.get_subscription(subscription_id)
    return service.to_response(record)


@router.delete(
    "/{subscription_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        401: {"description": "Missing or invalid API key"},
        404: {"description": "Subscription not found"},
    },
)
@handle_errors(logger=logger)
async def delete_watch_subscription(
    service: Annotated[WatchService, Depends(get_watch_service)],
    subscription_id: str = Path(
        ...,
        pattern=_SUBSCRIPTION_ID_PATTERN,
        description="The unique identifier of the watch subscription",
    ),
) -> None:
    """Delete a watch subscription and stop its watcher loop."""
    logger.info(
        "Processing delete_watch_subscription request",
        extra={"subscription_id": subscription_id},
    )
    async with logger.track_time("delete_watch_subscription"):
        await service.delete_subscription(subscription_id)
