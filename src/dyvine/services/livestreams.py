import asyncio
import logging
import os
import signal
import urllib.parse
from pathlib import Path
from typing import Dict, Optional, Set, Tuple

import httpx
from f2.apps.douyin.dl import DouyinDownloader, Live

from ..core.logging import ContextLogger
from ..core.settings import settings
from .users import UserNotFoundError as UserServiceNotFoundError
from .users import UserService

logger = ContextLogger(logging.getLogger(__name__))

class LivestreamError(Exception):
    """Base exception for livestream-related errors.

    This exception serves as a general parent class for all custom exceptions
    related to livestream operations, providing a way to catch all livestream
    errors in a single except block if needed.
    """
    pass

class UserNotFoundError(LivestreamError):
    """Exception raised when a user is not found.

    This exception indicates that a user, typically identified by their user ID
    or room ID, could not be found or does not exist. It inherits from
    LivestreamError, allowing it to be caught as a general livestream error or
    specifically as a user-not-found error.
    """
    pass

class DownloadError(LivestreamError):
    """Exception raised when a download fails.

    This exception indicates that an error occurred during the download process
    of a livestream. It could be due to various reasons, such as network issues,
    invalid stream URLs, or problems with the download configuration. It
    inherits from LivestreamError, allowing it to be caught as a general
    livestream error or specifically as a download error.
    """
    pass

class LivestreamService:
    """Service class for managing Douyin livestream operations.

    This class provides methods for:
    - Retrieving information about a livestream room.
    - Downloading livestreams and saving them as video files.
    - Monitoring the status of a livestream and merging downloaded segments.
    - Handling active downloads and preventing duplicate downloads.

    It interacts with the DouyinDownloader and UserService classes to perform
    the necessary operations.
    """

    def __init__(self) -> None:
        """Initialize the livestream service using global settings.

        Initializes the service with settings from the environment,
        configures the Douyin downloader, and sets up data structures
        for managing active downloads and download processes.
        """
        self.settings = settings
        self.config = self._build_douyin_config()
        self.downloader = DouyinDownloader(self.config)
        self.active_downloads: Set[str] = set()
        self.download_processes: Dict[str, asyncio.subprocess.Process] = {}
        self.user_service = UserService()

    def _build_douyin_config(self) -> Dict:
        """Build Douyin downloader configuration from settings.

        Returns:
            Dict: Containing Douyin downloader configuration.
        """
        config = {
            'cookie': self.settings.douyin_cookie,
            'headers': {
                'authority': 'live.douyin.com',
                'accept': 'application/json, text/plain, */*',
                'accept-language': 'zh-CN,zh;q=0.9,en;q=0.8',
                'cache-control': 'no-cache',
                'cookie': self.settings.douyin_cookie,
                'origin': 'https://live.douyin.com',
                'pragma': 'no-cache',
                'referer': 'https://live.douyin.com/',
                'sec-ch-ua': '"Not A(Brand";v="99", "Google Chrome";v="121", "Chromium";v="121"',
                'sec-ch-ua-mobile': '?0',
                'sec-ch-ua-platform': '"macOS"',
                'sec-fetch-dest': 'empty',
                'sec-fetch-mode': 'cors',
                'sec-fetch-site': 'same-origin',
                'user-agent': self.settings.douyin_user_agent,
                'x-secsdk-csrf-token': '000100000001d40084dca1e2d5f6e2f8c2e4d7e2d3c2e6e09fab5dcae72468976d4d15139b417e8c4527b6eb2ff0',
                'x-use-ppe': '1'
            },
            'proxies': self.settings.douyin_proxies,
            'verify': True,
            'timeout': 30,  # Default timeout
            'naming': '{room_id}_{nickname}',  # Include room_id and nickname in filename
            'mode': 'live',
            'auto_cookie': True,
            'folderize': False,
        }
        return config

    async def get_room_info(self, room_id: str, logger: logging.Logger = logger) -> Dict:
        """Get room information for a given room ID.

        Args:
            room_id (str): The room ID to get info for.
            logger (logging.Logger): Logger instance to use.

        Returns:
            Dict: Containing room information.

        Raises:
            ValueError: If room doesn't exist or stream ended.
        """
        try:
            # Get room info using downloader's get_fetch_data method
            # Use Douyin's room info API
            url = f"https://live.douyin.com/webcast/room/web/enter/"
            params = {
                "aid": "6383",
                "app_name": "douyin_web",
                "live_id": "1",
                "device_platform": "web",
                "enter_from": "web_live",
                "web_rid": room_id,
                "room_id_str": room_id,
                "enter_source": "room",
                "browser_language": "zh-CN",
                "browser_platform": "Win32",
                "browser_name": "Chrome",
                "browser_version": "121.0.0.0",
                "cookie_enabled": "true",
                "screen_width": "1920",
                "screen_height": "1080",
                "update_version_code": "1.3.0",
                "identity": "audience"
            }
            # Only include proxies if they are configured
            proxies = None
            if any(self.config['proxies'].values()):
                proxies = self.config['proxies']

            async with httpx.AsyncClient(
                headers=self.config["headers"],
                params=params,
                proxies=proxies,
                timeout=30,
            ) as client:
                response = await client.get(url)
                logger.info(f"API Response type: {type(response)}")
                logger.info(f"API Response status code: {response.status_code}")
                logger.info(f"API Response headers: {response.headers}")
                logger.info(f"API Response content: {response.text}")

                response_data = response.json()
                logger.info(f"Parsed response data: {response_data}")
                if (
                    not response_data
                    or "data" not in response_data
                    or "data" not in response_data["data"]
                    or not response_data["data"]["data"]
                ):
                    raise ValueError("Could not get room info")
                room_data = response_data["data"]["data"][0]
                return room_data
        except Exception as e:
            logger.error(f"Error getting room info: {str(e)}")
            raise

    async def merge_ts_files(self, output_dir: Path, room_id: str) -> None:
        """Merge downloaded ts files into a single mp4 file.

        Args:
            output_dir (Path): Directory containing ts files.
            room_id (str): Room ID for filename prefix.
        """
        try:
            # Get list of ts files for this room
            ts_files = sorted([f for f in output_dir.glob(f"{room_id}__*.ts")])
            if not ts_files:
                logger.warning(f"No ts files found for room {room_id}")
                return

            # Create concat file
            concat_file = output_dir / f"{room_id}_concat.txt"
            with open(concat_file, 'w') as f:
                for ts_file in ts_files:
                    f.write(f"file '{ts_file.name}'\n")

            # Merge files using ffmpeg
            output_file = output_dir / f"{room_id}_merged.mp4"
            merge_command = (
                f'ffmpeg -f concat -safe 0 -i "{concat_file}" -c copy "{output_file}"'
            )

            process = await asyncio.create_subprocess_shell(
                merge_command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await process.wait()

            # Clean up
            concat_file.unlink()

            # Delete individual ts files
            for ts_file in ts_files:
                ts_file.unlink()

            logger.info(f"Successfully merged ts files for room {room_id}")

        except Exception as e:
            logger.error(f"Error merging ts files: {str(e)}")

    async def monitor_room_status(self, room_id: str, output_dir: Path) -> None:
        """Monitor room status and merge ts files when stream ends.

        Args:
            room_id (str): Room ID to monitor.
            output_dir (Path): Directory containing ts files.
        """
        try:
            while True:
                try:
                    room_info = await self.get_room_info(room_id)
                    if room_info.get('status') != 2:  # Not live anymore
                        logger.info(f"Stream ended for room {room_id}")
                        # Kill ffmpeg process
                        if room_id in self.download_processes:
                            process = self.download_processes[room_id]
                            try:
                                process.terminate()
                                await process.wait()
                            except ProcessLookupError:
                                pass
                            del self.download_processes[room_id]

                        # Merge ts files
                        await self.merge_ts_files(output_dir, room_id)
                        break

                    await asyncio.sleep(30)  # Check every 30 seconds

                except Exception as e:
                    logger.error(f"Error checking room status: {str(e)}")
                    await asyncio.sleep(30)  # Wait before retrying

        except Exception as e:
            logger.error(f"Error in monitor_room_status: {str(e)}")
        finally:
            if room_id in self.active_downloads:
                self.active_downloads.remove(room_id)

    async def download_stream(self, url: str, output_path: Optional[str] = None) -> Tuple[str, str]:
        """Download a Douyin livestream.

        Args:
            url (str): The URL or room ID of the livestream.
            output_path (Optional[str]): Optional path where to save the stream.

        Returns:
            Tuple[str, str]: (status: str, message: str).

        Raises:
            LivestreamError: If download fails.
        """
        try:
            # Extract ID from URL if needed
            id_str = url.split('/')[-1] if '/' in url else url

            # Try to parse as room ID first (numeric)
            try:
                room_id = str(int(id_str))  # Will fail if not numeric
            except ValueError:
                # If not numeric, treat as user ID and get room ID from profile
                try:
                    user_info = await self.user_service.get_user_info(id_str)
                    if not user_info.is_living or not user_info.room_id:
                        return "error", "User is not currently streaming"
                    room_id = str(user_info.room_id)
                except UserServiceNotFoundError:
                    return "error", f"User {id_str} not found"
                except Exception as e:
                    return "error", f"Failed to get user info: {str(e)}"

            # Check if already downloading
            if room_id in self.active_downloads:
                return "error", "Already downloading this stream"

            # Get room info
            try:
                room_info = await self.get_room_info(room_id)
            except ValueError as e:
                return "error", str(e)

            # Check if user is live (status 2 = live)
            status_code = room_info.get('status')
            if status_code != 2:
                return "error", f"User is not currently streaming (status code: {status_code})"

            # Get stream URL
            stream_url = room_info.get('stream_url', {})
            hls_pull_url_map = stream_url.get('hls_pull_url_map', {})
            if not hls_pull_url_map:
                logger.warning(f"No stream URLs found for room ID: {room_id}")
                return "error", "No live stream available for this user"

            # Get highest quality stream URL (FULL_HD1)
            stream_urls = hls_pull_url_map.get('FULL_HD1')
            if not stream_urls:
                # Fallback to HD1 if FULL_HD1 not available
                stream_urls = hls_pull_url_map.get('HD1')
                if not stream_urls:
                    return "error", "No suitable quality stream found"

            # Set up output directory
            output_dir = Path("data/douyin/downloads/livestreams")
            output_dir.mkdir(parents=True, exist_ok=True)

            # Add to active downloads
            self.active_downloads.add(room_id)

            try:
                # Create output filename
                output_filename = f"{room_id}"
                output_path = output_dir / output_filename

                # Use ffmpeg to download and save as ts files
                command = (
                    f'ffmpeg -i "{stream_urls}" -c copy -f segment -segment_time 10 '
                    f'-segment_format mpegts "{output_path}__%03d.ts"'
                )

                # Start download process
                process = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )

                # Store process for later cleanup
                self.download_processes[room_id] = process

                # Start monitoring room status
                asyncio.create_task(self.monitor_room_status(room_id, output_dir))

                # Return pending status with expected output directory
                return "pending", str(output_dir)

            except Exception as e:
                logger.error(f"Download task error: {str(e)}")
                raise DownloadError(f"Failed to start download: {str(e)}")
            finally:
                if room_id in self.active_downloads:
                    self.active_downloads.remove(room_id)

        except Exception as e:
            logger.error(f"Error downloading stream: {str(e)}")
            return "error", str(e)

# Create service instance after class definition
livestream_service = LivestreamService()
