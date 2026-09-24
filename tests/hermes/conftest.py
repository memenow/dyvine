"""Fixtures shared by the Hermes plugin tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dyvine_hermes import weekly as weekly_module


@pytest.fixture(autouse=True)
def roomy_download_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Admit every weekly pair unless a test narrows the disk itself.

    The runner's disk budget reads the host's free space; without this stub
    runner tests would pass or fail with the machine that runs them.
    """
    usage = SimpleNamespace(total=2**50, used=0, free=2**50)
    monkeypatch.setattr(weekly_module.shutil, "disk_usage", lambda _path: usage)
