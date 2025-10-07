import asyncio
import json
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlparse

from f2.apps.douyin.dl import DouyinDownloader  # type: ignore
from f2.apps.douyin.utils import WebCastIdFetcher  # type: ignore
from f2.exceptions.api_exceptions import APIResponseError  # type: ignore

from ..core.dependencies import get_service_container
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
        base_config = self._build_douyin_config()
        self.downloader_config = {
            "cookie": self.settings.douyin_cookie,
            "headers": base_config["headers"],
            "proxies": base_config["proxies"],
        }
        self.download_jobs: dict[str, asyncio.Task[Any]] = {}
        self.user_service = UserService()
        self.douyin_handler = get_service_container().douyin_handler
        # Disable optional Bark notifications to avoid network noise in service usage.
        setattr(self.douyin_handler, "enable_bark", False)

    async def _load_live_filter(
        self,
        *,
        webcast_id: str | None = None,
    ) -> Any:
        """Load live metadata via the f2 Douyin handler."""
        if not webcast_id:
            raise ValueError("webcast_id is required to resolve livestream metadata")

        # Primary attempt: direct webcast id lookup
        try:
            live_filter = await self.douyin_handler.fetch_user_live_videos(webcast_id)
            if live_filter:
                return live_filter
        except Exception as error:
            logger.debug("fetch_user_live_videos failed for %s: %s", webcast_id, error)

        # Fallback: convert long room ids to webcast ids via WebCastIdFetcher
        try:
            converted_id = await WebCastIdFetcher.get_webcast_id(
                f"https://live.douyin.com/{webcast_id}"
            )
            if converted_id and converted_id != webcast_id:
                converted_filter = await self.douyin_handler.fetch_user_live_videos(
                    converted_id
                )
                if converted_filter:
                    return converted_filter
        except APIResponseError as error:
            logger.debug("WebCastIdFetcher could not convert %s: %s", webcast_id, error)
        except Exception as error:
            logger.debug(
                "Unexpected error converting webcast id %s: %s", webcast_id, error
            )

        # Final fallback: query by room id directly
        try:
            return await self.douyin_handler.fetch_user_live_videos_by_room_id(
                webcast_id
            )
        except Exception as error:
            logger.debug(
                "fetch_user_live_videos_by_room_id failed for %s: %s",
                webcast_id,
                error,
            )
            return None

    @staticmethod
    def _extract_stream_map(live_filter: Any) -> Dict[str, str]:
        """Extract HLS stream map from the f2 live filter."""
        if hasattr(live_filter, "m3u8_pull_url"):
            stream_map = getattr(live_filter, "m3u8_pull_url") or {}
            if isinstance(stream_map, dict):
                return stream_map
        if hasattr(live_filter, "hls_pull_url"):
            stream_map = getattr(live_filter, "hls_pull_url") or {}
            if isinstance(stream_map, dict):
                return stream_map
        return {}

    @staticmethod
    def _select_stream_url(stream_map: Dict[str, str]) -> str | None:
        """Choose the preferred stream URL from the available variants."""
        preferred_order = ("FULL_HD1", "HD1", "SD1", "SD2")
        for key in preferred_order:
            value = stream_map.get(key)
            if isinstance(value, str) and value:
                return value
        for value in stream_map.values():
            if isinstance(value, str) and value:
                return value
        return None

    @staticmethod
    def _stream_map_from_room_data(
        room_data: str | None,
    ) -> tuple[dict[str, str], int | None, dict[str, str]]:
        """Parse room_data JSON into HLS and FLV stream maps plus status."""
        if not room_data:
            return {}, None, {}

        try:
            payload = json.loads(room_data)
        except json.JSONDecodeError:
            logger.debug("Invalid room_data payload")
            return {}, None, {}

        stream_map: dict[str, str] = {}
        flv_map: dict[str, str] = {}
        status = payload.get("status")
        stream_url = payload.get("stream_url") or {}

        live_core = stream_url.get("live_core_sdk_data") or {}
        pull_data = live_core.get("pull_data") or {}
        quality_map: dict[str, Any] = {}

        stream_data_raw = pull_data.get("stream_data")
        if isinstance(stream_data_raw, str):
            try:
                stream_data_payload = json.loads(stream_data_raw)
                quality_map = stream_data_payload.get("data") or {}
            except json.JSONDecodeError:
                logger.debug("Invalid stream_data payload in room_data")
        elif isinstance(pull_data.get("data"), dict):
            quality_map = pull_data.get("data") or {}

        if isinstance(quality_map, dict):
            for quality_name, quality_payload in quality_map.items():
                if not isinstance(quality_payload, dict):
                    continue
                main_payload = quality_payload.get("main")
                if not isinstance(main_payload, dict):
                    continue

                hls_candidate = (
                    main_payload.get("hls")
                    or main_payload.get("ll_hls")
                    or main_payload.get("http_ts")
                )
                if isinstance(hls_candidate, str) and hls_candidate:
                    stream_map[quality_name.upper()] = hls_candidate

                flv_candidate = main_payload.get("flv")
                if isinstance(flv_candidate, str) and flv_candidate:
                    flv_map.setdefault(quality_name.upper(), flv_candidate)

        raw_flv_map = stream_url.get("flv_pull_url") or {}
        if isinstance(raw_flv_map, dict):
            for quality_name, candidate_url in raw_flv_map.items():
                quality_key = quality_name.upper()
                if isinstance(candidate_url, str) and candidate_url:
                    flv_map.setdefault(quality_key, candidate_url)

        return stream_map, status, flv_map

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

    async def get_room_info(self, webcast_id: str) -> dict[str, Any]:
        """Resolve livestream metadata via f2 for monitoring and downloads."""
        try:
            live_filter = await self._load_live_filter(webcast_id=webcast_id)
            if not live_filter:
                raise ValueError("Unable to resolve livestream metadata")
            room_info = live_filter._to_dict()
            room_info["stream_map"] = self._extract_stream_map(live_filter)
            room_info["status"] = getattr(live_filter, "live_status", 0)
            room_info.setdefault("room_id", getattr(live_filter, "room_id", webcast_id))
            room_info.setdefault("webcast_id", webcast_id)
            return room_info
        except Exception as error:
            logger.error(f"Error getting room info via f2: {error}")
            raise

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
            normalized_url = url.strip()
            if not normalized_url:
                return "error", "Livestream URL is required"

            webcast_id: str | None = None
            room_id: str | None = None
            profile_room_info: dict[str, Any] | None = None

            if normalized_url.isdigit():
                webcast_id = normalized_url

            parsed = urlparse(
                normalized_url
                if "://" in normalized_url
                else f"https://{normalized_url}"
            )
            path = parsed.path or ""
            host = parsed.netloc.lower()
            last_segment = path.rstrip("/").split("/")[-1] if path else ""
            last_segment = last_segment.split("?")[0].split("#")[0]

            if not webcast_id and last_segment.isdigit():
                webcast_id = last_segment

            if not webcast_id:
                try:
                    webcast_id = await WebCastIdFetcher.get_webcast_id(normalized_url)
                except APIResponseError as error:
                    logger.debug(
                        "WebCastIdFetcher could not resolve webcast id: %s", error
                    )
                except Exception as error:
                    logger.debug(
                        "Unexpected error resolving webcast id from %s: %s",
                        normalized_url,
                        error,
                    )

            if not webcast_id and "douyin.com" in host and path.startswith("/user/"):
                try:
                    user_id = last_segment or (
                        path.rstrip("/").split("/")[-1] if path else ""
                    )
                    if not user_id:
                        return "error", "User identifier missing in profile URL"
                    profile = await self.user_service.get_user_info(user_id)
                    if not profile.is_living or not profile.room_id:
                        return "error", "User is not currently livestreaming"
                    webcast_id = str(profile.room_id)
                    logger.info(
                        "Derived webcast id %s from user profile %s",
                        webcast_id,
                        user_id,
                    )
                    (
                        stream_map,
                        status,
                        flv_map,
                    ) = self._stream_map_from_room_data(
                        getattr(profile, "room_data", None)
                    )
                    profile_room_info = {
                        "status": (
                            status if status is not None else (2 if stream_map else 0)
                        ),
                        "stream_map": stream_map,
                        "flv_pull_url": flv_map,
                        "room_id": str(profile.room_id),
                        "webcast_id": webcast_id,
                    }
                except Exception as error:
                    return (
                        "error",
                        f"Failed to resolve webcast id from profile: {error}",
                    )

            if not webcast_id:
                return "error", "Unable to resolve webcast id from provided URL"

            if webcast_id in self.download_jobs:
                return "error", "Already downloading this stream"

            live_filter = await self._load_live_filter(webcast_id=webcast_id)
            if not live_filter and not profile_room_info:
                return "error", "Unable to fetch livestream metadata"

            room_info = (
                live_filter._to_dict() if live_filter else profile_room_info or {}
            )
            room_id = str(room_info.get("room_id", webcast_id) or webcast_id)

            status_code = room_info.get("status")
            if status_code != 2:
                return "error", (
                    f"User is not currently streaming (status code: {status_code})"
                )

            stream_map: dict[str, str] = {}
            flv_map: dict[str, str] = {}
            if profile_room_info and profile_room_info.get("stream_map"):
                stream_map = profile_room_info["stream_map"]
            if profile_room_info and profile_room_info.get("flv_pull_url"):
                flv_map = profile_room_info["flv_pull_url"]
            if not stream_map and live_filter:
                stream_map = getattr(live_filter, "m3u8_pull_url", {}) or {}
            if not flv_map and live_filter:
                flv_map = getattr(live_filter, "flv_pull_url", {}) or {}
            if not stream_map:
                logger.warning(f"No stream URLs found for webcast ID: {webcast_id}")
                return "error", "No live stream available for this user"

            if not self._select_stream_url(stream_map):
                return "error", "No suitable quality stream found"

            if output_path:
                output_dir = Path(output_path)
            else:
                output_dir = Path("data/douyin/downloads/livestreams")
            output_dir.mkdir(parents=True, exist_ok=True)

            webcast_payload = {
                "room_id": room_id,
                "live_title": room_info.get("live_title_raw")
                or room_info.get("live_title")
                or "",
                "user_id": room_info.get("user_id") or "",
                "nickname": room_info.get("nickname_raw")
                or room_info.get("nickname")
                or "",
                "m3u8_pull_url": stream_map,
                "flv_pull_url": flv_map or room_info.get("flv_pull_url") or {},
            }

            download_kwargs = {
                **self.downloader_config,
                "cookie": self.settings.douyin_cookie,
                "folderize": False,
                "naming": "{aweme_id}",
                "mode": "live",
            }
            download_kwargs["headers"] = {
                **download_kwargs["headers"],
                "cookie": self.settings.douyin_cookie,
            }

            target_file = output_dir / f"{room_id}_live.flv"

            job = asyncio.create_task(
                self._run_stream_download(
                    room_id, download_kwargs, webcast_payload, output_dir
                )
            )
            self.download_jobs[room_id] = job
            return "pending", str(target_file)

        except Exception as e:
            logger.error(f"Error downloading stream: {str(e)}")
            return "error", str(e)

    async def _run_stream_download(
        self,
        job_id: str,
        download_kwargs: dict[str, Any],
        webcast_payload: dict[str, Any],
        output_dir: Path,
    ) -> None:
        """Run the f2 livestream downloader in the background."""
        try:
            async with DouyinDownloader(download_kwargs) as downloader:
                await downloader.create_stream_tasks(
                    download_kwargs, webcast_payload, output_dir
                )
        except Exception as error:
            logger.error(
                "Livestream download failed",
                extra={"job_id": job_id, "error": str(error)},
            )
        finally:
            self.download_jobs.pop(job_id, None)

    async def get_download_status(self, operation_id: str) -> str:
        """Get the status of a download operation.

        Args:
            operation_id (str): The operation ID (room_id).

        Returns:
            str: The path to the downloaded file if complete, otherwise a status
                message.
        """
        output_dir = Path("data/douyin/downloads/livestreams")
        output_file = output_dir / f"{operation_id}_live.flv"

        if output_file.exists():
            return str(output_file)

        if operation_id in self.download_jobs:
            return "Download in progress."

        raise NotImplementedError("Operation not found or status unknown.")


# Create service instance after class definition
livestream_service = LivestreamService()
