"""Tests for ProfileService over the fake repository."""

from __future__ import annotations

import pytest
from fake_repos import FakeProfileRepository

from dyvine.core.exceptions import UserProfileNotFoundError
from dyvine.services.profiles import ProfileService


async def test_upsert_and_get_round_trip() -> None:
    """Snapshots upsert fully and patch partially."""
    svc = ProfileService(profiles=FakeProfileRepository())
    created = await svc.upsert_profile(
        sec_user_id="s1", nickname="n1", follower_count=5
    )
    assert created.nickname == "n1"
    patched = await svc.upsert_profile(sec_user_id="s1", city="Hangzhou")
    assert patched.city == "Hangzhou"
    assert patched.nickname == "n1"
    assert (await svc.get_profile("s1")) == patched
    with pytest.raises(UserProfileNotFoundError):
        await svc.get_profile("ghost")
