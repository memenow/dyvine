"""Tests for watch-mode router endpoints.

Handlers are invoked directly (as in the livestream router tests) so the
``@handle_errors`` decorator's exception-to-HTTP mapping is exercised
without standing up a full TestClient.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, Response

from dyvine.core.exceptions import RateLimitError, WatchSubscriptionNotFoundError
from dyvine.schemas.watch import WatchSubscriptionCreate, WatchSubscriptionResponse


def _response(subscription_id: str = "sub-12345") -> WatchSubscriptionResponse:
    """Build a representative response model."""
    return WatchSubscriptionResponse(
        subscription_id=subscription_id,
        user_id="user01",
        enabled=True,
        live_poll_seconds=300,
        post_poll_seconds=2700,
        last_live_check=None,
        last_post_check=None,
        newest_aweme_id=None,
        created_at="2026-06-22T00:00:00+00:00",
        updated_at="2026-06-22T00:00:00+00:00",
    )


@pytest.fixture
def mock_watch_service() -> MagicMock:
    """A WatchService double with async CRUD methods."""
    svc = MagicMock()
    svc.create_subscription = AsyncMock()
    svc.get_subscription = AsyncMock()
    svc.list_subscriptions = AsyncMock()
    svc.delete_subscription = AsyncMock()
    svc.to_response = MagicMock(return_value=_response())
    return svc


async def test_create_returns_201_for_new(mock_watch_service: MagicMock) -> None:
    """Creating a new subscription sets HTTP 201."""
    from dyvine.routers.watch import create_watch_subscription

    mock_watch_service.create_subscription.return_value = (MagicMock(), True)
    response = Response()
    result = await create_watch_subscription(
        request=WatchSubscriptionCreate(user_id="user01"),
        service=mock_watch_service,
        response=response,
    )
    assert response.status_code == 201
    assert result.subscription_id == "sub-12345"


async def test_create_returns_200_for_existing(mock_watch_service: MagicMock) -> None:
    """Re-creating an existing subscription sets HTTP 200 (idempotent)."""
    from dyvine.routers.watch import create_watch_subscription

    mock_watch_service.create_subscription.return_value = (MagicMock(), False)
    response = Response()
    await create_watch_subscription(
        request=WatchSubscriptionCreate(user_id="user01"),
        service=mock_watch_service,
        response=response,
    )
    assert response.status_code == 200


async def test_create_over_limit_returns_429(mock_watch_service: MagicMock) -> None:
    """The subscription cap surfaces as HTTP 429."""
    from dyvine.routers.watch import create_watch_subscription

    mock_watch_service.create_subscription.side_effect = RateLimitError("limit")
    with pytest.raises(HTTPException) as exc_info:
        await create_watch_subscription(
            request=WatchSubscriptionCreate(user_id="user01"),
            service=mock_watch_service,
            response=Response(),
        )
    assert exc_info.value.status_code == 429


async def test_list_returns_total(mock_watch_service: MagicMock) -> None:
    """Listing reports the number of subscriptions."""
    from dyvine.routers.watch import list_watch_subscriptions

    mock_watch_service.list_subscriptions.return_value = [MagicMock(), MagicMock()]
    mock_watch_service.to_response.side_effect = [_response("a"), _response("b")]
    result = await list_watch_subscriptions(service=mock_watch_service)
    assert result.total == 2
    assert len(result.subscriptions) == 2


async def test_get_unknown_returns_404(mock_watch_service: MagicMock) -> None:
    """Fetching an unknown subscription returns HTTP 404."""
    from dyvine.routers.watch import get_watch_subscription

    mock_watch_service.get_subscription.side_effect = WatchSubscriptionNotFoundError(
        "nf"
    )
    with pytest.raises(HTTPException) as exc_info:
        await get_watch_subscription(
            service=mock_watch_service, subscription_id="sub-99999"
        )
    assert exc_info.value.status_code == 404


async def test_delete_success_returns_none(mock_watch_service: MagicMock) -> None:
    """A successful delete returns None (HTTP 204 at the framework layer)."""
    from dyvine.routers.watch import delete_watch_subscription

    mock_watch_service.delete_subscription.return_value = None
    result = await delete_watch_subscription(
        service=mock_watch_service, subscription_id="sub-12345"
    )
    assert result is None


async def test_delete_unknown_returns_404(mock_watch_service: MagicMock) -> None:
    """Deleting an unknown subscription returns HTTP 404."""
    from dyvine.routers.watch import delete_watch_subscription

    mock_watch_service.delete_subscription.side_effect = WatchSubscriptionNotFoundError(
        "nf"
    )
    with pytest.raises(HTTPException) as exc_info:
        await delete_watch_subscription(
            service=mock_watch_service, subscription_id="sub-99999"
        )
    assert exc_info.value.status_code == 404
