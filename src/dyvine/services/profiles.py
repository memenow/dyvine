"""Cached Douyin profile snapshots over ``user_profiles``.

:class:`ProfileService` is a thin typed seam over
:class:`ProfileRepository`: tools upsert snapshots after a successful
profile fetch and read them back without touching Douyin.
"""

from __future__ import annotations

from typing import Any

from ..db.protocols import ProfileRepository
from ..db.records import UserProfileRecord


class ProfileService:
    """Upsert and read cached profile snapshots."""

    def __init__(self, *, profiles: ProfileRepository) -> None:
        """Bind the service to its repository protocol."""
        self._profiles = profiles

    async def upsert_profile(
        self, *, sec_user_id: str, **fields: Any
    ) -> UserProfileRecord:
        """Insert or patch the snapshot row and return it."""
        return await self._profiles.upsert_profile(sec_user_id=sec_user_id, **fields)

    async def get_profile(self, sec_user_id: str) -> UserProfileRecord:
        """Fetch one snapshot by id."""
        return await self._profiles.get_profile(sec_user_id)
