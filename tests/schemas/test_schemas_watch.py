"""Tests for watch-mode request/response schema validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from dyvine.schemas.watch import (
    WatchSubscriptionCreate,
    WatchSubscriptionList,
    WatchSubscriptionResponse,
)


def test_create_accepts_minimal_payload() -> None:
    """A bare user_id is valid; interval overrides default to None."""
    model = WatchSubscriptionCreate(user_id="user01")
    assert model.user_id == "user01"
    assert model.live_poll_seconds is None
    assert model.post_poll_seconds is None
    assert model.backfill_on_create is None


def test_create_accepts_in_range_intervals() -> None:
    """Interval overrides within bounds are accepted."""
    model = WatchSubscriptionCreate(
        user_id="user01",
        live_poll_seconds=60,
        post_poll_seconds=86400,
        backfill_on_create=True,
    )
    assert model.live_poll_seconds == 60
    assert model.post_poll_seconds == 86400
    assert model.backfill_on_create is True


@pytest.mark.parametrize("bad_user_id", ["u1", "", "has spaces", "bad/slash"])
def test_create_rejects_invalid_user_id(bad_user_id: str) -> None:
    """user_id must match the Douyin identifier pattern."""
    with pytest.raises(ValidationError):
        WatchSubscriptionCreate(user_id=bad_user_id)


@pytest.mark.parametrize("seconds", [59, 3601])
def test_create_rejects_out_of_range_live_interval(seconds: int) -> None:
    """live_poll_seconds outside 60..3600 is rejected."""
    with pytest.raises(ValidationError):
        WatchSubscriptionCreate(user_id="user01", live_poll_seconds=seconds)


@pytest.mark.parametrize("seconds", [299, 86401])
def test_create_rejects_out_of_range_post_interval(seconds: int) -> None:
    """post_poll_seconds outside 300..86400 is rejected."""
    with pytest.raises(ValidationError):
        WatchSubscriptionCreate(user_id="user01", post_poll_seconds=seconds)


def test_response_round_trips_fields() -> None:
    """The response model carries every surfaced subscription field."""
    response = WatchSubscriptionResponse(
        subscription_id="sub-12345",
        user_id="user01",
        enabled=True,
        live_poll_seconds=300,
        post_poll_seconds=2700,
        last_live_check=None,
        last_post_check=None,
        newest_aweme_id="7423",
        created_at="2026-06-22T00:00:00+00:00",
        updated_at="2026-06-22T00:00:00+00:00",
    )
    assert response.newest_aweme_id == "7423"
    assert response.enabled is True


def test_list_envelope_defaults_to_empty() -> None:
    """The list envelope reports an explicit total."""
    envelope = WatchSubscriptionList(total=0)
    assert envelope.subscriptions == []
    assert envelope.total == 0
