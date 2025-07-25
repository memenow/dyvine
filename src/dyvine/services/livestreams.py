import asyncio
from pathlib import Path
from typing import Any

import httpx
from f2.apps.douyin.dl import DouyinDownloader  # type: ignore

from ..core.exceptions import (
    DownloadError,
    ServiceError,
)
from ..core.logging import ContextLogger
from ..core.settings import settings
from .users import UserService

logger = ContextLogger(__name__)

# Alias for backward compatibility
LivestreamError = ServiceError


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
        self.active_downloads: set[str] = set()
        self.download_processes: dict[str, asyncio.subprocess.Process] = {}
        self.user_service = UserService()

    def _build_douyin_config(self) -> dict:
        """Build Douyin downloader configuration from settings.

        Returns:
            Dict: Containing Douyin downloader configuration.
        """
        config = {
            "cookie": self.settings.douyin_cookie,
            "headers": {
                "authority": "live.douyin.com",
                "accept": "application/json, text/plain, */*",
                "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
                "cache-control": "no-cache",
                "cookie": self.settings.douyin_cookie,
                "origin": "https://live.douyin.com",
                "pragma": "no-cache",
                "referer": "https://live.douyin.com/",
                "sec-ch-ua": (
                    '"Not A(Brand";v="99", "Google Chrome";v="121", "Chromium";v="121"'
                ),
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"macOS"',
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
                "user-agent": self.settings.douyin_user_agent,
                "x-secsdk-csrf-token": (
                    "000100000001d40084dca1e2d5f6e2f8c2e4d7e2d3c2e6e09fab5dcae72468976d4d15139b417e8c4527b6eb2ff0"
                ),
                "x-use-ppe": "1",
            },
            "proxies": self.settings.douyin_proxies,
            "verify": True,
            "timeout": 30,  # Default timeout
            "naming": "{room_id}_{nickname}",
            "mode": "live",
            "auto_cookie": True,
            "folderize": False,
        }
        return config

    async def get_room_info(
        self, room_id: str, logger: ContextLogger = logger
    ) -> dict[str, Any]:
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
            # Use correct Douyin live API endpoint
            url = "https://webcast.amemv.com/webcast/room/reflow/info/"

            # Correct parameters format
            params = {
                "type_id": "0",
                "live_id": "1",
                "room_id": room_id,
                "app_id": "1128",
            }

            # Set appropriate headers with cookie
            headers = {
                "authority": "webcast.amemv.com",
                "user-agent": (
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 10_3_1 like Mac OS X) "
                    "AppleWebKit/603.1.30 (KHTML, like Gecko) Version/10.0 "
                    "Mobile/14E304 Safari/602.1"
                ),
                "cookie": (
                    "_tea_utm_cache_1128={"
                    "%22utm_source%22:%22copy%22,"
                    "%22utm_medium%22:%22android%22,"
                    "%22utm_campaign%22:%22client_share%22}"
                ),
            }

            # Only include proxies if they are configured
            proxies = None
            if any(self.config["proxies"].values()):
                proxies = self.config["proxies"]

            async with httpx.AsyncClient(
                headers=headers,
                proxies=proxies,
                timeout=30,
            ) as client:
                response = await client.get(url, params=params)
                logger.info(f"API Response status code: {response.status_code}")
                logger.info(f"API Response content: {response.text[:500]}...")

                response_data = response.json()
                logger.info(f"Parsed response data keys: {list(response_data.keys())}")

                if (
                    not response_data
                    or "data" not in response_data
                    or "room" not in response_data["data"]
                    or not response_data["data"]["room"]
                ):
                    raise ValueError("Could not get room info")

                room_data: dict[str, Any] = response_data["data"]["room"]

                # Check if stream is live (status=2 means live)
                status = room_data.get("status", 0)
                logger.info(f"Room status: {status}")

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
            ts_files = sorted(output_dir.glob(f"{room_id}__*.ts"))
            if not ts_files:
                logger.warning(f"No ts files found for room {room_id}")
                return

            # Create concat file
            concat_file = output_dir / f"{room_id}_concat.txt"
            with open(concat_file, "w") as f:
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
                stderr=asyncio.subprocess.PIPE,
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
                    if room_info.get("status") != 2:  # Not live anymore
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

    async def download_stream(
        self, url: str, output_path: str | None = None
    ) -> tuple[str, str]:
        """Download a Douyin livestream.

        Args:
            url (str): The livestream room URL (e.g., https://live.douyin.com/123456789).
            output_path (Optional[str]): Optional path where to save the stream.

        Returns:
            Tuple[str, str]: (status: str, message: str).

        Raises:
            LivestreamError: If download fails.
        """
        try:
            # Extract room ID from livestream URL
            room_id = None

            # Handle direct room ID
            if url.isdigit():
                room_id = url
            # Handle livestream URL format: https://live.douyin.com/123456789
            elif "live.douyin.com/" in url:
                parts = url.rstrip("/").split("/")
                if parts and parts[-1].isdigit():
                    room_id = parts[-1]
                else:
                    return (
                        "error",
                        "Invalid livestream URL format. Expected: https://live.douyin.com/[room_id]",
                    )
            # Handle webcast.amemv.com URL format
            elif "webcast.amemv.com" in url and "/webcast/reflow/" in url:
                import re

                match = re.search(r"/webcast/reflow/(\d+)", url)
                if match:
                    room_id = match.group(1)
                else:
                    return "error", (
                        "Invalid webcast URL format. Could not extract room ID"
                    )
            # Handle user profile URL
            # - try to get room ID from user info
            elif "douyin.com/user/" in url:
                try:
                    # Extract user ID from URL
                    user_id = url.split("/")[-1] if "/" in url else url
                    user_info = await self.user_service.get_user_info(user_id)
                    if not user_info.is_living or not user_info.room_id:
                        return "error", (
                            "User is not currently streaming or room ID not available"
                        )
                    room_id = str(user_info.room_id)
                    logger.info(
                        f"Extracted room_id {room_id} from user profile {user_id}"
                    )
                except Exception as e:
                    return "error", f"Failed to get room ID from user profile: {str(e)}"
            else:
                return "error", (
                    "Invalid URL format. Expected livestream URL "
                    "(https://live.douyin.com/[room_id]) or user profile URL"
                )

            if not room_id:
                return "error", "Could not extract room ID from URL"

            # Check if already downloading
            if room_id in self.active_downloads:
                return "error", "Already downloading this stream"

            # Get room info
            try:
                room_info = await self.get_room_info(room_id)
            except ValueError as e:
                return "error", str(e)

            # Check if user is live (status 2 = live)
            status_code = room_info.get("status")
            if status_code != 2:
                return "error", (
                    f"User is not currently streaming (status code: {status_code})"
                )

            # Get stream URL
            stream_url = room_info.get("stream_url", {})
            hls_pull_url_map = stream_url.get("hls_pull_url_map", {})
            if not hls_pull_url_map:
                logger.warning(f"No stream URLs found for room ID: {room_id}")
                return "error", "No live stream available for this user"

            # Get highest quality stream URL (FULL_HD1)
            stream_urls = hls_pull_url_map.get("FULL_HD1")
            if not stream_urls:
                # Fallback to HD1 if FULL_HD1 not available
                stream_urls = hls_pull_url_map.get("HD1")
                if not stream_urls:
                    return "error", "No suitable quality stream found"

            # Set up output directory
            if output_path:
                output_dir = Path(output_path)
            else:
                output_dir = Path("data/douyin/downloads/livestreams")
            output_dir.mkdir(parents=True, exist_ok=True)

            # Add to active downloads
            self.active_downloads.add(room_id)

            try:
                # Create output filename
                output_filename = f"{room_id}"
                final_output_path = output_dir / output_filename

                # Use ffmpeg to download and save as ts files
                command = (
                    f'ffmpeg -i "{stream_urls}" -c copy -f segment -segment_time 10 '
                    f'-segment_format mpegts "{final_output_path}__%03d.ts"'
                )

                # Start download process
                process = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

                # Store process for later cleanup
                self.download_processes[room_id] = process

                # Start monitoring room status
                asyncio.create_task(self.monitor_room_status(room_id, output_dir))

                # Return pending status with expected output directory
                return "pending", str(output_dir)

            except Exception as e:
                logger.error(f"Download task error: {str(e)}")
                raise DownloadError(f"Failed to start download: {str(e)}") from e
            finally:
                if room_id in self.active_downloads:
                    self.active_downloads.remove(room_id)

        except Exception as e:
            logger.error(f"Error downloading stream: {str(e)}")
            return "error", str(e)

    async def get_download_status(self, operation_id: str) -> str:
        """Get the status of a download operation.

        Args:
            operation_id (str): The operation ID (room_id).

        Returns:
            str: The path to the downloaded file if complete, otherwise a status
                message.
        """
        # operation_id is the room_id
        output_dir = Path("data/douyin/downloads/livestreams")
        output_file = output_dir / f"{operation_id}_merged.mp4"

        if output_file.exists():
            return str(output_file)

        if operation_id in self.active_downloads:
            return "Download in progress."

        # Check for ts files to see if it's merging
        ts_files = list(output_dir.glob(f"{operation_id}__*.ts"))
        if ts_files:
            return "Merging downloaded files."

        raise NotImplementedError("Operation not found or status unknown.")


# Create service instance after class definition
livestream_service = LivestreamService()
