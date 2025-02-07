"""Service module for handling Douyin livestream operations.

This module provides functionality for:
- Downloading live streams from Douyin users
- Retrieving room information and stream URLs
- Managing download operations and status tracking
- Handling stream download lifecycle

The service implements robust error handling, logging, and resource management
for reliable livestream operations.

Typical usage example:
    service = LiveStreamService()
    status, path = await service.download_stream(user_id="123456")
    if status == "success":
        print(f"Stream downloading to: {path}")
"""

import os
from pathlib import Path
import asyncio
import httpx
import json
import logging
from datetime import datetime

# Custom exceptions
class LivestreamError(Exception):
    """Base exception for livestream-related errors."""
    pass

class UserNotFoundError(LivestreamError):
    """Exception raised when a user cannot be found."""
    pass

class DownloadError(LivestreamError):
    """Exception raised when a download operation fails."""
    pass

from f2.apps.douyin.dl import DouyinDownloader
from f2.apps.douyin.handler import DouyinHandler
from f2.apps.douyin.db import AsyncUserDB
from f2.apps.douyin.model import UserLive
from f2.apps.douyin.filter import UserLiveFilter
from f2.apps.douyin.crawler import DouyinCrawler
from f2.apps.douyin.utils import WebCastIdFetcher
from f2.utils.utils import extract_valid_urls
from f2.apps.douyin.algorithm.webcast_signature import DouyinWebcastSignature
from ..core.settings import settings
from ..core.logging import ContextLogger

logger = ContextLogger(__name__)

class LiveStreamService:
    """Service for handling live stream downloads from Douyin."""
    
    def __init__(self):
        """Initialize the LiveStreamService instance."""
        # Get current working directory
        cwd = Path.cwd()
        
        # Base directory for all data
        self.base_dir = cwd / "data" / "douyin"
        self.base_dir.mkdir(parents=True, exist_ok=True)
        
        # Downloads directory within base dir
        self.downloads_dir = self.base_dir / "downloads" / "livestreams"
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        
        # Logs directory
        self.logs_dir = self.base_dir / "logs" / "livestreams"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        
        # Database path
        self.db_path = self.base_dir / "douyin_users.db"
        
        # Create database directory if it doesn't exist
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Ensure all paths are absolute and exist
        self.base_dir = self.base_dir.absolute()
        self.downloads_dir = self.downloads_dir.absolute()
        self.logs_dir = self.logs_dir.absolute()
        self.db_path = self.db_path.absolute()
        
        # Double check directories exist
        os.makedirs(self.downloads_dir, exist_ok=True)
        os.makedirs(self.logs_dir, exist_ok=True)
        
        # Track active downloads
        self.active_downloads = set()
        
        # Initialize configuration
        self.config = {
            "cookie": settings.douyin_cookie,
            "headers": {
                "User-Agent": settings.douyin_user_agent,
                "Referer": settings.douyin_referer,
            },
            "proxies": {},
            "verify": True,
            "timeout": 30,
            "max_retries": 3,
            "mode": "live",
            "auto_cookie": True,
            "folderize": False,
            "naming": "{create}_{desc}",
            "base_dir": str(self.base_dir),
            "db_path": str(self.db_path),
            "downloads_dir": str(self.downloads_dir),
            "max_tasks": 5,
            "chunk_size": 1024 * 1024,  # 1MB chunks
            "retry_wait": 5,
            "download_timeout": 3600,  # 1 hour
        }
        
        # Add proxy configuration if provided
        if settings.douyin_proxy_http:
            self.config["proxies"]["http://"] = settings.douyin_proxy_http
        if settings.douyin_proxy_https:
            self.config["proxies"]["https://"] = settings.douyin_proxy_https
        
        # Add required crawler headers
        self.config["crawler_headers"] = self.config["headers"]
        
        # Initialize handler and downloader
        self.handler = DouyinHandler(self.config)
        self.downloader = DouyinDownloader(self.config)

    def setup_stream_logger(self, user_id: str) -> logging.Logger:
        """Set up a dedicated logger for a specific live stream operation.
        
        Creates a new logger instance with file-based logging for tracking
        stream-specific operations. Log files are stored in the logs directory
        with timestamps for easy tracking.
        
        Args:
            user_id: The user's Douyin ID to identify the stream logs
            
        Returns:
            logging.Logger: Configured logger instance for stream operations
            
        Note:
            Log files are named as: {user_id}_{timestamp}.log and stored in
            the configured logs directory.
        """
        # Create logger
        stream_logger = logging.getLogger(f"stream_{user_id}")
        stream_logger.setLevel(logging.DEBUG)
        
        # Create file handler
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_file = self.logs_dir / f"{user_id}_{timestamp}.log"
        handler = logging.FileHandler(log_file)
        handler.setLevel(logging.DEBUG)
        
        # Create formatter
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        handler.setFormatter(formatter)
        
        # Add handler to logger
        stream_logger.addHandler(handler)
        
        return stream_logger

    async def get_room_info(self, user_id: str, stream_logger: logging.Logger) -> dict:
        """Get live room information for a Douyin user.
        
        Retrieves detailed information about a user's live room including:
        - Room ID and status
        - Stream URLs (FLV and HLS)
        - User information
        - Stream statistics
        
        Args:
            user_id: The user's Douyin ID or room URL
            stream_logger: Logger instance for operation tracking
            
        Returns:
            dict: Room information including stream URLs and metadata
            
        Raises:
            ValueError: If room doesn't exist or stream has ended
        """
        try:
            # First check if input is a URL and extract user ID
            stream_logger.debug(f"Processing input: {user_id}")
            if user_id.startswith('http'):
                try:
                    base_url = user_id.split('?')[0]
                    extracted_id = base_url.rstrip('/').split('/')[-1]
                    stream_logger.debug(f"Extracted ID from URL: {extracted_id}")
                    user_id = extracted_id
                except Exception as e:
                    stream_logger.warning(f"Failed to extract ID from URL: {str(e)}")
            
            # Create URL for webcast ID extraction
            url = f"https://live.douyin.com/{user_id}"
            urls = [url]
            stream_logger.debug(f"Using URL: {url}")
            
            # Extract webcast ID
            webcast_ids = await WebCastIdFetcher.get_all_webcast_id(urls)
            
            if not webcast_ids:
                stream_logger.warning(f"Could not extract webcast ID from URL, using ID directly: {user_id}")
                webcast_id = user_id
            else:
                webcast_id = webcast_ids[0]
                stream_logger.debug(f"Successfully extracted webcast ID: {webcast_id}")
            
            # Try with extracted/direct ID
            try:
                live = await self.handler.fetch_user_live_videos_by_room_id(room_id=webcast_id)
                if live and live.room_id:
                    stream_logger.info(
                        f"Room ID: {live.room_id}, User: {live.nickname_raw}, Title: {live.live_title_raw}, Status: {live.live_status}, Viewers: {live.user_count}"
                    )
                    result = live._to_dict()
                    stream_logger.debug(f"Final room info from fetch_user_live_videos_by_room_id: {result}")
                    return result
            except Exception as e:
                stream_logger.warning(f"fetch_user_live_videos_by_room_id failed: {str(e)}, trying fetch_live")
            
            # Try with fetch_live as fallback
            params = UserLive(web_rid=webcast_id, room_id_str=webcast_id)
            async with DouyinCrawler(self.config) as crawler:
                response = await crawler.fetch_live(params)
                stream_logger.debug(f"Raw API response from fetch_live: {response}")
                
                if response.get("status_code") == 0:
                    live = UserLiveFilter(response)
                    if live and live.room_id:
                        stream_logger.info(
                            f"Room ID: {live.room_id}, User: {live.nickname_raw}, Title: {live.live_title_raw}, Status: {live.live_status}, Viewers: {live.user_count}"
                        )
                        result = live._to_dict()
                        stream_logger.debug(f"Final room info from fetch_live: {result}")
                        return result
                
            # If all attempts fail, raise error
            raise ValueError("直播间不存在或已结束")
                
        except Exception as e:
            error_msg = f"Failed to get room info: {str(e)}"
            stream_logger.error(error_msg)
            raise ValueError(error_msg)

    async def download_stream(self, user_id: str, output_path: str | None = None) -> tuple[str, str | None]:
        """Download a live stream from a Douyin user.
        
        Initiates an asynchronous download of a user's active livestream.
        The download runs in the background and can be monitored via
        get_download_status().
        
        Args:
            user_id: The user's Douyin ID or room URL
            output_path: Optional custom save location for the stream
            
        Returns:
            tuple[str, str | None]: Status ("success"/"error") and file path/error message
            
        Raises:
            ValueError: If user not found or not streaming
            DownloadError: If download initialization fails
        """
        # Extract user ID from URL if needed
        original_id = user_id
        if user_id.startswith('http'):
            try:
                base_url = user_id.split('?')[0]
                user_id = base_url.rstrip('/').split('/')[-1]
            except Exception as e:
                logger.warning(f"Failed to extract ID from URL: {str(e)}")
        
        # Check if already downloading
        if user_id in self.active_downloads:
            return "error", "Already downloading this stream"
        
        # Set up stream-specific logger
        stream_logger = self.setup_stream_logger(user_id)
        stream_logger.debug(f"Original input: {original_id}")
        stream_logger.debug(f"Using user ID: {user_id}")
        
        try:
            stream_logger.info(f"Starting stream download for user {user_id}")
            
            # Get room info
            room_info = await self.get_room_info(user_id, stream_logger)
            
            # Check if user is streaming
            if room_info["live_status"] != 2:
                stream_logger.info(f"User {user_id} is not currently streaming")
                return "error", "User is not currently streaming"
            
            # Get stream URLs from room info
            flv_urls = room_info.get("flv_pull_url", {})
            if not flv_urls:
                stream_logger.error(f"No FLV stream URLs found for user {user_id}")
                return "error", "No live stream available for this user"
            
            # Create stream data structure expected by downloader
            stream_data = {
                "flv_pull_url": flv_urls,
                "hls_pull_url_map": room_info.get("m3u8_pull_url", {}),
                "default_resolution": "FULL_HD1"
            }
            
            # Add stream data to room info
            room_info["stream_url"] = stream_data
            
            # Get current timestamp
            timestamp = int(asyncio.get_event_loop().time())
            
            # Set up download path
            if output_path:
                download_path = Path(output_path)
            else:
                download_path = self.downloads_dir / f"{user_id}_{timestamp}.flv"
            
            # Ensure the directory exists
            download_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Create webcast data
            webcast_data = room_info.copy()
            webcast_data["create_time"] = timestamp
            webcast_data["finish_time"] = 0
            
            stream_logger.info(f"Will save stream to: {download_path}")
            
            # Start the download with updated config
            download_config = self.config.copy()
            download_config.update({
                "output_dir": str(download_path.parent),
                "output_name": download_path.name
            })
            
            # Add to active downloads
            self.active_downloads.add(user_id)
            
            # Start download in background without waiting
            async def download_and_cleanup():
                try:
                    await self.downloader.create_stream_tasks(
                        kwargs=download_config,
                        webcast_datas=webcast_data,
                        user_path=str(download_path.parent)
                    )
                except Exception as e:
                    stream_logger.error(f"Download error: {str(e)}")
                finally:
                    # Remove from active downloads when done
                    self.active_downloads.remove(user_id)
                    stream_logger.info("Download task completed")
            
            asyncio.create_task(download_and_cleanup())
            
            # Return immediately with success
            stream_logger.info(f"Successfully started stream download to {download_path}")
            return "success", str(download_path)
            
        except Exception as e:
            stream_logger.error(f"Failed to download stream: {str(e)}", exc_info=True)
            # Remove from active downloads if error occurs
            if user_id in self.active_downloads:
                self.active_downloads.remove(user_id)
            return "error", str(e)

    async def get_download_status(self, operation_id: str) -> str:
        """Get the status of a download operation.
        
        Retrieves the current status of an active or completed download operation.
        
        Args:
            operation_id: Unique identifier for the download operation
            
        Returns:
            str: Path to the downloaded file if complete
            
        Raises:
            DownloadError: If operation not found or status check fails
        """
        # Implementation of download status tracking
        if operation_id not in self.active_downloads:
            raise DownloadError(f"Operation {operation_id} not found")
            
        # For now, we can only confirm if download is active
        # Future: Add progress tracking, file size, download speed etc.
        return f"Download in progress for {operation_id}"

livestream_service = LiveStreamService()
