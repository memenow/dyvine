"""Pytest configuration shared across all test modules.

Adds ``src/`` to ``sys.path`` so that imports like ``from dyvine.core import ...``
resolve to the local source tree rather than an installed package. This ensures a
single canonical import path (``dyvine.*``) — using the project root instead would
expose a second path (``src.dyvine.*``), causing double-registration errors in
modules with side effects at import time (e.g. Prometheus metrics in storage.py).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Flag the test runtime as "debug" before any ``dyvine.core.settings`` import
# so the production-only validator in ``SecuritySettings.validate_not_default``
# does not reject the default ``change-me-in-production`` sentinel values that
# pydantic-settings supplies when no real env vars are present. Production
# deployments set ``API_DEBUG=false`` explicitly; leaving this unset in tests
# used to silently skip the validator, which was the bug fixed alongside this
# conftest change.
os.environ.setdefault("API_DEBUG", "true")

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


@pytest.fixture(autouse=True)
def reset_singletons(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Reset cached state and isolate operation storage between tests."""
    from dyvine.core.dependencies import get_service_container
    from dyvine.core.settings import settings

    monkeypatch.setattr(
        settings.api,
        "operation_db_path",
        str(tmp_path / "operations.db"),
    )
    get_service_container.cache_clear()
    yield
    get_service_container.cache_clear()


@pytest.fixture
def mock_douyin_handler() -> MagicMock:
    """Return a MagicMock mimicking DouyinHandler's async interface."""
    handler = MagicMock()
    handler.kwargs = {"mode": "all", "max_tasks": 3}
    handler.fetch_one_video = AsyncMock(return_value=None)
    handler.fetch_user_profile = AsyncMock(return_value=None)
    handler.fetch_user_post_videos = MagicMock(return_value=AsyncMock())
    handler.fetch_user_live_videos = AsyncMock(return_value=None)
    handler.fetch_user_live_videos_by_room_id = AsyncMock(return_value=None)
    handler.get_or_add_user_data = AsyncMock(return_value=Path("/tmp/test"))
    handler.downloader = MagicMock()
    handler.downloader.create_download_tasks = AsyncMock()
    handler.downloader.create_stream_tasks = AsyncMock()
    handler.enable_bark = False
    return handler


@pytest.fixture
def storage_service_no_init():
    """Create R2StorageService without calling __init__ (avoids boto3)."""
    from dyvine.services.storage import R2StorageService

    service = object.__new__(R2StorageService)
    service._executor = None  # type: ignore[attr-defined]
    return service
