"""Configuration and process outcomes for weekly Dyvine delivery."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True, slots=True)
class WeeklyConfig:
    """Require explicit routing, timezone, and original group reuse."""

    timezone: str
    owner_open_id: str
    download_root: Path
    first_auto_date: date | None = None
    cutover_round: str | None = None

    @classmethod
    def from_environment(cls) -> WeeklyConfig:
        """Load Hermes's env before building the first database connection."""
        from dotenv import load_dotenv

        load_dotenv(Path.home() / ".hermes" / ".env", override=False)
        timezone = os.environ.get("DYVINE_WEEKLY_TIMEZONE", "").strip()
        owner_open_id = os.environ.get("DYVINE_WEEKLY_OWNER_OPEN_ID", "").strip()
        first_auto_text = os.environ.get("DYVINE_WEEKLY_FIRST_AUTO_DATE", "").strip()
        cutover_round = os.environ.get("DYVINE_WEEKLY_CUTOVER_ROUND", "").strip()
        group_policy_text = os.environ.get("DYVINE_WEEKLY_GROUP_POLICY", "").strip()
        if not timezone:
            raise ValueError("DYVINE_WEEKLY_TIMEZONE is required")
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as error:
            raise ValueError("DYVINE_WEEKLY_TIMEZONE is invalid") from error
        if not owner_open_id.startswith("ou_"):
            raise ValueError("DYVINE_WEEKLY_OWNER_OPEN_ID must be a Feishu open_id")
        try:
            first_auto_date = date.fromisoformat(first_auto_text)
        except ValueError as error:
            raise ValueError(
                "DYVINE_WEEKLY_FIRST_AUTO_DATE must be YYYY-MM-DD"
            ) from error
        if first_auto_date.weekday() != 6:
            raise ValueError("DYVINE_WEEKLY_FIRST_AUTO_DATE must be Sunday")
        if not cutover_round:
            raise ValueError("DYVINE_WEEKLY_CUTOVER_ROUND is required")
        if group_policy_text != "reuse_existing":
            raise ValueError("DYVINE_WEEKLY_GROUP_POLICY must be reuse_existing")
        from dyvine.core.settings import settings

        return cls(
            timezone=timezone,
            owner_open_id=owner_open_id,
            download_root=Path(settings.douyin.download_root).expanduser(),
            first_auto_date=first_auto_date,
            cutover_round=cutover_round,
        )


@dataclass(frozen=True, slots=True)
class WeeklyOutcome:
    """Small process result; only dry-run prints it to stdout."""

    status: str
    round: str | None = None
    key: str | None = None
    files: int = 0
    note: str | None = None
    processed: int = 0
