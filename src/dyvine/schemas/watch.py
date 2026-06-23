"""Pydantic models for the watch-mode router.

``WatchSubscriptionCreate`` is the request body for ``POST /watch``;
``WatchSubscriptionResponse`` and ``WatchSubscriptionList`` are the read
models. Interval overrides are optional: when omitted the service fills in
the configured ``DOUYIN_WATCH_*`` defaults. The bounds mirror
``WatchSettings`` so the schema rejects out-of-range values before they
reach the service.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# Matches the alphabet Douyin emits for sec_user_id values, mirroring the
# livestream schema/router so a watch target validates the same way a
# livestream download target does.
_USER_ID_PATTERN = r"^[A-Za-z0-9_\-]{6,128}$"


class WatchSubscriptionCreate(BaseModel):
    """Request body for creating a watch subscription."""

    user_id: str = Field(
        ...,
        pattern=_USER_ID_PATTERN,
        description=(
            "The Douyin user identifier (sec_user_id) to watch. Restricted "
            "to the alphabet Douyin emits to prevent injection into "
            "generated upstream URLs."
        ),
    )
    live_poll_seconds: int | None = Field(
        None,
        ge=60,
        le=3600,
        description=(
            "Seconds between live-status checks. Omit to use the configured "
            "DOUYIN_WATCH_LIVE_POLL_SECONDS default."
        ),
    )
    post_poll_seconds: int | None = Field(
        None,
        ge=300,
        le=86400,
        description=(
            "Seconds between new-post checks. Omit to use the configured "
            "DOUYIN_WATCH_POST_POLL_SECONDS default."
        ),
    )
    backfill_on_create: bool | None = Field(
        None,
        description=(
            "Download the user's existing posts once on creation instead of "
            "only fetching posts published afterwards. Omit to use the "
            "configured DOUYIN_WATCH_BACKFILL_ON_CREATE default."
        ),
    )


class WatchSubscriptionResponse(BaseModel):
    """Public representation of a watch subscription."""

    subscription_id: str = Field(..., description="Unique subscription identifier")
    user_id: str = Field(..., description="Watched Douyin user identifier")
    enabled: bool = Field(..., description="Whether the watcher loop is active")
    live_poll_seconds: int = Field(
        ..., description="Seconds between live-status checks"
    )
    post_poll_seconds: int = Field(..., description="Seconds between new-post checks")
    last_live_check: str | None = Field(
        None, description="ISO 8601 timestamp of the last live-status check"
    )
    last_post_check: str | None = Field(
        None, description="ISO 8601 timestamp of the last new-post check"
    )
    newest_aweme_id: str | None = Field(
        None,
        description="Most recent downloaded aweme_id, surfaced from the checkpoint",
    )
    created_at: str = Field(..., description="ISO 8601 creation timestamp")
    updated_at: str = Field(..., description="ISO 8601 last update timestamp")


class WatchSubscriptionList(BaseModel):
    """Envelope for listing watch subscriptions."""

    subscriptions: list[WatchSubscriptionResponse] = Field(
        default_factory=list, description="All watch subscriptions"
    )
    total: int = Field(..., description="Total number of subscriptions")
