"""Unit tests for the livestreams service.

This module tests the LiveStreamService functionality including:
- Room info retrieval
- Stream downloading
- Error handling
"""

import pytest
import asyncio
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

from dyvine.services.livestreams import (
    LiveStreamService,
    LivestreamError,
    UserNotFoundError,
    DownloadError
)

@pytest.fixture
def mock_webcast_fetcher():
    """Fixture for mocked WebCastIdFetcher."""
    with patch('dyvine.services.livestreams.WebCastIdFetcher') as mock:
        mock.get_all_webcast_id = AsyncMock(return_value=["test_webcast_id"])
        yield mock

@pytest.fixture
def mock_douyin_handler():
    """Fixture for mocked DouyinHandler."""
    mock = MagicMock()
    mock_live = MagicMock()
    mock_live._to_dict.return_value = {
        "room_id": "test123",
        "nickname_raw": "Test User",
        "live_title_raw": "Test Stream",
        "live_status": 2,
        "user_count": 1000,
        "flv_pull_url": {"HD1": "http://test.com/stream.flv"},
        "m3u8_pull_url": {"HD1": "http://test.com/stream.m3u8"}
    }
    mock.fetch_user_live_videos_by_room_id = AsyncMock(return_value=mock_live)
    return mock

@pytest.fixture
def mock_douyin_downloader():
    """Fixture for mocked DouyinDownloader."""
    mock = MagicMock()
    mock.create_stream_tasks = AsyncMock()
    return mock

@pytest.fixture
def livestream_service(mock_webcast_fetcher, mock_douyin_handler, mock_douyin_downloader):
    """Fixture for LiveStreamService with mocked dependencies."""
    with patch('dyvine.services.livestreams.DouyinHandler', return_value=mock_douyin_handler), \
         patch('dyvine.services.livestreams.DouyinDownloader', return_value=mock_douyin_downloader):
        service = LiveStreamService()
        yield service

@pytest.mark.asyncio
async def test_get_room_info_success(livestream_service):
    """Test successful room info retrieval."""
    room_id = "test123"
    mock_logger = MagicMock()
    
    result = await livestream_service.get_room_info(room_id, mock_logger)
    
    assert result["room_id"] == room_id
    assert result["live_status"] == 2
    assert "flv_pull_url" in result
    assert "m3u8_pull_url" in result

@pytest.mark.asyncio
async def test_get_room_info_not_found(livestream_service, mock_douyin_handler):
    """Test room info retrieval when room doesn't exist."""
    room_id = "nonexistent123"
    mock_logger = MagicMock()
    
    mock_douyin_handler.fetch_user_live_videos_by_room_id = AsyncMock(
        side_effect=ValueError("直播间不存在或已结束")
    )
    
    with pytest.raises(ValueError, match="直播间不存在或已结束"):
        await livestream_service.get_room_info(room_id, mock_logger)

@pytest.mark.asyncio
async def test_download_stream_success(livestream_service, test_data_dir):
    """Test successful stream download."""
    user_id = "test123"
    output_path = test_data_dir / "test_stream.flv"
    
    # Mock room info
    mock_room_info = {
        "room_id": user_id,
        "live_status": 2,
        "flv_pull_url": {"HD1": "http://test.com/stream.flv"},
        "m3u8_pull_url": {"HD1": "http://test.com/stream.m3u8"}
    }
    
    # Mock the get_room_info method
    livestream_service.get_room_info = AsyncMock(return_value=mock_room_info)
    
    status, path = await livestream_service.download_stream(user_id, str(output_path))
    
    # Wait for background task to complete
    await asyncio.sleep(0.1)
    
    assert status == "success"
    assert path == str(output_path)
    assert user_id not in livestream_service.active_downloads

@pytest.mark.asyncio
async def test_download_stream_already_downloading(livestream_service):
    """Test attempting to download an already downloading stream."""
    user_id = "test123"
    
    # Add user_id to active downloads
    livestream_service.active_downloads.add(user_id)
    
    status, message = await livestream_service.download_stream(user_id)
    
    assert status == "error"
    assert message == "Already downloading this stream"

@pytest.mark.asyncio
async def test_download_stream_not_live(livestream_service):
    """Test attempting to download when user is not live."""
    user_id = "test123"
    
    # Mock room info with non-live status
    mock_room_info = {
        "room_id": user_id,
        "live_status": 1,  # Not live
    }
    
    livestream_service.get_room_info = AsyncMock(return_value=mock_room_info)
    
    status, message = await livestream_service.download_stream(user_id)
    
    assert status == "error"
    assert message == "User is not currently streaming"

@pytest.mark.asyncio
async def test_download_stream_no_urls(livestream_service):
    """Test download attempt when no stream URLs are available."""
    user_id = "test123"
    
    # Mock room info with no stream URLs
    mock_room_info = {
        "room_id": user_id,
        "live_status": 2,
        "flv_pull_url": {}  # Empty URLs
    }
    
    livestream_service.get_room_info = AsyncMock(return_value=mock_room_info)
    
    status, message = await livestream_service.download_stream(user_id)
    
    assert status == "error"
    assert message == "No live stream available for this user"

@pytest.mark.asyncio
async def test_download_stream_with_url_extraction(livestream_service):
    """Test stream download with URL extraction."""
    url = "https://live.douyin.com/test123"
    expected_id = "test123"
    
    # Mock room info
    mock_room_info = {
        "room_id": expected_id,
        "live_status": 2,
        "flv_pull_url": {"HD1": "http://test.com/stream.flv"},
        "m3u8_pull_url": {"HD1": "http://test.com/stream.m3u8"}
    }
    
    livestream_service.get_room_info = AsyncMock(return_value=mock_room_info)
    
    status, _ = await livestream_service.download_stream(url)
    
    # Wait for background task to complete
    await asyncio.sleep(0.1)
    
    assert status == "success"
    assert expected_id not in livestream_service.active_downloads
