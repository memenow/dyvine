"""Test configuration and fixtures for the Dyvine package.

This module provides common fixtures and configurations used across all test modules.
It includes:
- Environment setup
- Mock services
- Test data
- Common utilities
"""

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.dyvine.core.settings import Settings, get_settings
from src.dyvine.main import app
from src.dyvine.services.livestreams import LivestreamService

# Test data directory
TEST_DATA_DIR = Path(__file__).parent / "data"
TEST_DATA_DIR.mkdir(exist_ok=True)

@pytest.fixture
def test_settings():
    """Fixture for test settings."""
    return Settings(
        debug=True,
        project_name="Dyvine Test API",
        version="test",
        prefix="/api/v1",
        secret_key="test-secret-key",
        api_key="test-api-key",
        douyin_cookie="test-cookie",
        douyin_user_agent="test-user-agent",
        douyin_referer="https://www.douyin.com/"
    )

@pytest.fixture
def test_client(test_settings):
    """Fixture for FastAPI test client."""
    def get_test_settings():
        return test_settings

    app.dependency_overrides[get_settings] = get_test_settings
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()

@pytest.fixture
def mock_livestream_service():
    """Fixture for mocked LiveStreamService."""
    service = MagicMock(spec=LivestreamService)
    
    # Mock successful download
    async def mock_download(*args, **kwargs):
        return "success", "/path/to/downloaded/stream.flv"
    service.download_stream.side_effect = mock_download
    
    # Mock room info
    async def mock_room_info(*args, **kwargs):
        return {
            "room_id": "test123",
            "nickname_raw": "Test User",
            "live_title_raw": "Test Stream",
            "live_status": 2,
            "user_count": 1000,
            "flv_pull_url": {"HD1": "http://test.com/stream.flv"},
            "m3u8_pull_url": {"HD1": "http://test.com/stream.m3u8"}
        }
    service.get_room_info.side_effect = mock_room_info
    
    return service

@pytest.fixture
def test_data_dir():
    """Fixture for test data directory."""
    return TEST_DATA_DIR

@pytest.fixture
def cleanup_test_files():
    """Fixture to clean up test files after tests."""
    yield
    # Clean up test files
    for file in TEST_DATA_DIR.glob("*"):
        if file.is_file():
            file.unlink()

@pytest.fixture
def mock_response():
    """Fixture for mocked HTTP response."""
    class MockResponse:
        def __init__(self, status_code=200, json_data=None):
            self.status_code = status_code
            self.json_data = json_data or {}

        async def json(self):
            return self.json_data

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    return MockResponse
