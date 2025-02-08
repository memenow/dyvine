"""Unit tests for the livestreams service."""

import pytest
import asyncio
import httpx
from pathlib import Path
from unittest.mock import Mock, patch, AsyncMock, MagicMock
from src.dyvine.services.livestreams import LivestreamService, LivestreamError

@pytest.fixture
def mock_room_info():
    """Mock room info data."""
    return {
        "room_id": "test123",
        "live_status": 2,
        "user": {
            "id_str": "user123",
            "nickname": "Test User"
        },
        "title": "Test Stream",
        "flv_pull_url": {"HD1": "http://test.com/stream.flv"}
    }

@pytest.fixture
def mock_response(mock_room_info):
    """Mock httpx.Response."""
    mock = MagicMock()
    mock.json = AsyncMock(return_value={'data': mock_room_info})
    return mock

@pytest.fixture
def mock_downloader(mock_room_info, mock_response):
    """Fixture for mocked DouyinDownloader."""
    with patch('f2.apps.douyin.dl.DouyinDownloader') as mock_class:
        mock = mock_class.return_value
        mock.create_stream_tasks = AsyncMock()
        mock.get_fetch_data = AsyncMock(return_value=mock_response)
        yield mock_class

@pytest.fixture
def livestream_service(mock_downloader):
    """Fixture for LivestreamService."""
    return LivestreamService()

@pytest.mark.asyncio
async def test_get_room_info_success(mock_downloader, mock_room_info):
    """Test successful room info retrieval."""
    service = LivestreamService()
    room_id = "test123"
    
    result = await service.get_room_info(room_id)
    
    assert result == mock_room_info
    expected_url = f"https://live.douyin.com/webcast/room/web/enter/?room_id={room_id}"
    mock_downloader.return_value.get_fetch_data.assert_called_once_with(expected_url)

@pytest.mark.asyncio
async def test_get_room_info_failure(mock_downloader, mock_response):
    """Test room info retrieval failure."""
    service = LivestreamService()
    mock_response.json = AsyncMock(return_value={})
    
    with pytest.raises(ValueError, match="Could not get room info"):
        await service.get_room_info("test123")

@pytest.mark.asyncio
async def test_download_stream_creates_directory(mock_downloader, mock_room_info, tmp_path):
    """Test that download_stream creates output directory if it doesn't exist."""
    service = LivestreamService()
    output_path = tmp_path / "downloads" / "test_stream.flv"
    
    # Create a mock task that simulates file creation
    async def create_file_after_delay():
        await asyncio.sleep(0.1)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"test data")
        return None
    
    mock_downloader.return_value.create_stream_tasks.return_value = asyncio.create_task(
        create_file_after_delay()
    )
    
    status, _ = await service.download_stream(
        "https://live.douyin.com/123456",
        str(output_path)
    )
    
    assert output_path.parent.exists()
    assert status == "success"

@pytest.mark.asyncio
async def test_download_stream_already_downloading(mock_downloader):
    """Test attempting to download an already downloading stream."""
    service = LivestreamService()
    room_id = "test123"
    
    # Add room_id to active downloads
    service.active_downloads.add(room_id)
    
    status, message = await service.download_stream(room_id)
    
    assert status == "error"
    assert message == "Already downloading this stream"

@pytest.mark.asyncio
async def test_download_stream_not_live(mock_downloader, mock_response):
    """Test attempting to download when user is not live."""
    service = LivestreamService()
    
    # Mock room info with non-live status
    mock_response.json = AsyncMock(return_value={'data': {
        "room_id": "test123",
        "live_status": 1  # Not live
    }})
    
    status, message = await service.download_stream("test123")
    
    assert status == "error"
    assert message == "User is not currently streaming"

@pytest.mark.asyncio
async def test_download_stream_handles_download_error(mock_downloader, tmp_path):
    """Test that download errors are handled properly."""
    service = LivestreamService()
    output_path = tmp_path / "test_stream.flv"
    
    # Make download task raise an error
    mock_downloader.return_value.create_stream_tasks.side_effect = Exception("Download failed")
    
    status, message = await service.download_stream(
        "https://live.douyin.com/123456",
        str(output_path)
    )
    
    assert status == "error"
    assert "Download failed" in message
    assert "123456" not in service.active_downloads
