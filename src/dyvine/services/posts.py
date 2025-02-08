"""Service layer for managing Douyin post operations.

This module provides a service class (PostService) that encapsulates the
high-level business logic for handling Douyin post-related operations,
including:

- Fetching detailed information about a specific post
- Retrieving a paginated list of posts from a user
- Downloading post content (videos, images, live streams, collections, stories)
- Managing bulk download operations for all posts of a user

The PostService class implements proper error handling, comprehensive logging,
and follows dependency injection patterns. It interacts with the DouyinHandler
and DouyinDownloader classes from the f2 library to perform the necessary
operations.

The module also defines custom exceptions related to post operations, such as
PostNotFoundError, UserNotFoundError, and DownloadError.
"""

from typing import Dict, Optional, List, Any
from pathlib import Path
import asyncio
import logging
from datetime import datetime

from f2.apps.douyin.handler import DouyinHandler
from f2.apps.douyin.db import AsyncUserDB

from ..core.logging import ContextLogger
from ..schemas.posts import (
    PostType,
    DownloadStatus,
    BulkDownloadResponse,
    PostDetail
)

logger = ContextLogger(logging.getLogger(__name__))

class PostServiceError(Exception):
    """Base exception class for errors related to the PostService.

    This exception serves as a general parent class for all custom exceptions
    defined within the PostService, allowing for more specific error handling
    and categorization.
    """
    pass

class PostNotFoundError(PostServiceError):
    """Exception raised when a requested post cannot be found.

    This exception indicates that a specific Douyin post, typically identified
    by its aweme_id, could not be retrieved or does not exist.
    """
    pass

class UserNotFoundError(PostServiceError):
    """Exception raised when a requested user cannot be found.

    This exception indicates that a specific Douyin user, typically identified
    by their sec_user_id, could not be retrieved or does not exist.
    """
    pass

class DownloadError(PostServiceError):
    """Exception raised when a content download operation fails.

    This exception indicates that an attempt to download content (video, images,
    etc.) associated with a Douyin post has failed. It may be due to network
    issues, invalid URLs, or other problems during the download process.
    """
    pass

class PostService:
    """Service class for handling Douyin post operations.

    This class encapsulates the business logic for various post-related
    operations, such as fetching post details, retrieving user posts,
    downloading post content, and managing bulk downloads. It maintains the
    following principles:

    - Proper error handling through custom exceptions
    - Comprehensive logging for tracking operations
    - Clean separation of concerns by delegating specific tasks to helper
      methods
    - Type safety through type annotations
    """

    def __init__(self, handler: DouyinHandler) -> None:
        """Initialize the PostService instance.

        Args:
            handler: Configured DouyinHandler instance for Douyin operations.
        """
        self.handler = handler
        logger.info("PostService initialized", extra={"handler_config": handler.kwargs})

    async def get_post_detail(self, aweme_id: str) -> PostDetail:
        """Fetch detailed information about a specific Douyin post.

        Args:
            aweme_id: Unique identifier of the post.

        Returns:
            PostDetail: Object containing detailed information about the post.

        Raises:
            PostNotFoundError: If the requested post cannot be found.
            PostServiceError: If an error occurs during the operation.
        """
        try:
            logger.info("Fetching post detail", extra={"aweme_id": aweme_id})
            post = await self.handler.fetch_one_video(aweme_id)

            if not post:
                raise PostNotFoundError(f"Post not found: {aweme_id}")

            post_data = post._to_dict()
            
            # Parse create_time string into a Unix timestamp (integer)
            create_time_str = post_data.get("create_time")
            try:
                create_time_dt = datetime.strptime(
                    create_time_str,
                    "%Y-%m-%d %H-%M-%S"
                )
                create_time = int(create_time_dt.timestamp())
            except (ValueError, TypeError):
                create_time = 0

            return PostDetail(
                aweme_id=post_data["aweme_id"],
                desc=post_data.get("desc", ""),
                create_time=create_time,
                post_type=self._determine_post_type(post_data),
                video_info=self._extract_video_info(post_data),
                images=self._extract_image_info(post_data),
                statistics=post_data.get("statistics", {})
            )

        except PostNotFoundError:
            raise
        except Exception as e:
            logger.exception(
                "Error fetching post detail",
                extra={"aweme_id": aweme_id, "error": str(e)}
            )
            raise PostServiceError(f"Failed to fetch post: {str(e)}") from e

    async def get_user_posts(
        self,
        sec_user_id: str,
        max_cursor: int = 0,
        count: int = 20,
    ) -> List[PostDetail]:
        """Retrieve a paginated list of posts from a Douyin user.

        Args:
            sec_user_id: Unique identifier of the user.
            max_cursor: Pagination cursor for fetching the next batch of posts.
            count: Number of posts to fetch per page.

        Returns:
            List[PostDetail]: List of PostDetail objects representing the user's posts.

        Raises:
            UserNotFoundError: If the requested user cannot be found.
            PostServiceError: If an error occurs during the operation.
        """
        try:
            logger.info(
                "Fetching user posts",
                extra={
                    "sec_user_id": sec_user_id,
                    "max_cursor": max_cursor,
                    "count": count
                }
            )

            posts_iterator = self.handler.fetch_user_post_videos(
                sec_user_id=sec_user_id,
                max_cursor=max_cursor,
                page_counts=count
            )

            try:
                posts_filter = await posts_iterator.__anext__()
            except StopAsyncIteration:
                logger.warning("No posts found", extra={"sec_user_id": sec_user_id})
                return []

            posts_data = posts_filter._to_dict()
            if not posts_data:
                raise UserNotFoundError(f"User not found: {sec_user_id}")

            return [
                PostDetail(
                    aweme_id=post["aweme_id"],
                    desc=post.get("desc", ""),
                    create_time=post.get("create_time", 0),
                    post_type=self._determine_post_type(post),
                    video_info=self._extract_video_info(post),
                    images=self._extract_image_info(post),
                    statistics=post.get("statistics", {})
                )
                for post in posts_data.get("aweme_list", [])
            ]

        except UserNotFoundError:
            raise
        except Exception as e:
            logger.exception(
                "Error fetching user posts",
                extra={"sec_user_id": sec_user_id, "error": str(e)}
            )
            raise PostServiceError(f"Failed to fetch user posts: {str(e)}") from e

    async def download_all_user_posts(
        self,
        sec_user_id: str,
        max_cursor: int = 0,
    ) -> BulkDownloadResponse:
        """Download all available posts from a Douyin user.

        Args:
            sec_user_id: Unique identifier of the user.
            max_cursor: Starting pagination cursor for fetching posts.

        Returns:
            BulkDownloadResponse: Object containing the results of the bulk download operation.

        Raises:
            UserNotFoundError: If the requested user cannot be found.
            DownloadError: If the download operation fails.
            PostServiceError: If an error occurs during the operation.
        """
        download_stats = {post_type: 0 for post_type in PostType}
        download_path = None
        total_posts = 0
        
        try:
            # Get user profile
            logger.info("Fetching user profile", extra={"sec_user_id": sec_user_id})
            profile = await self.handler.fetch_user_profile(sec_user_id)
            if not profile:
                raise UserNotFoundError(f"User not found: {sec_user_id}")
            
            total_posts = profile.aweme_count or 0
            logger.info(
                "User profile fetched",
                extra={
                    "sec_user_id": sec_user_id,
                    "nickname": profile.nickname,
                    "total_posts": total_posts
                }
            )

            # Set up user directory
            async with AsyncUserDB("douyin_users.db") as db:
                user_path = await self.handler.get_or_add_user_data(
                    self.handler.kwargs,
                    sec_user_id,
                    db
                )
                download_path = str(user_path)
                logger.info(
                    "Download directory created",
                    extra={"download_path": download_path}
                )

            current_cursor = max_cursor
            while True:
                try:
                    posts = await self._fetch_posts_batch(
                        sec_user_id,
                        current_cursor
                    )
                    if not posts:
                        break

                    await self._process_posts_batch(
                        posts,
                        download_stats,
                        user_path
                    )

                    # Handle pagination
                    has_more = posts.get("has_more", False)
                    next_cursor = posts.get("max_cursor", 0)
                    
                    if not has_more or not next_cursor:
                        break
                        
                    if next_cursor == current_cursor:
                        next_cursor = current_cursor + 1
                        
                    current_cursor = next_cursor
                    logger.info(
                        "Moving to next page",
                        extra={"cursor": current_cursor}
                    )

                except Exception as batch_error:
                    logger.error(
                        "Error processing batch",
                        extra={"error": str(batch_error)}
                    )
                    continue

        except UserNotFoundError:
            raise
        except Exception as e:
            logger.exception(
                "Error in download process",
                extra={"sec_user_id": sec_user_id, "error": str(e)}
            )
            raise DownloadError(f"Download failed: {str(e)}") from e

        return self._create_download_response(
            sec_user_id,
            download_path,
            total_posts,
            download_stats
        )

    async def _fetch_posts_batch(
        self,
        sec_user_id: str,
        cursor: int,
    ) -> Dict[str, Any]:
        """Fetch a batch of posts from a user.

        Args:
            sec_user_id (str): Unique identifier of the user.
            cursor (int): Pagination cursor for fetching the next batch of
                posts.

        Returns:
            Dict[str, Any]: Dictionary containing the fetched posts and
                pagination information. Returns an empty dictionary if no
                posts are found for the given user and cursor.
        """
        logger.info(
            "Fetching posts batch",
            extra={"sec_user_id": sec_user_id, "cursor": cursor}
        )
        
        posts_iterator = self.handler.fetch_user_post_videos(
            sec_user_id=sec_user_id,
            max_cursor=cursor,
            page_counts=20
        )
        
        try:
            posts_filter = await posts_iterator.__anext__()
            return posts_filter._to_dict()
        except StopAsyncIteration:
            return {}

    async def _process_posts_batch(
        self,
        posts: Dict[str, Any],
        download_stats: Dict[PostType, int],
        user_path: Path,
    ) -> None:
        """Process and download a batch of posts.

        Args:
            posts (Dict[str, Any]): Dictionary containing the batch of posts.
            download_stats (Dict[PostType, int]): Dictionary for tracking the
                download statistics for each post type.
            user_path (Path): Path to the user's directory for saving
                downloaded content.
        """
        post_list = posts.get("aweme_list", [])
        logger.info(
            "Processing posts batch",
            extra={"post_count": len(post_list)}
        )
        
        for post in post_list:
            try:
                post_type = self._determine_post_type(post)
                await self._download_post_content(post, post_type, user_path)
                download_stats[post_type] += 1
                
            except Exception as e:
                logger.error(
                    "Error processing post",
                    extra={
                        "aweme_id": post.get("aweme_id"),
                        "error": str(e)
                    }
                )

    def _determine_post_type(self, post: Dict[str, Any]) -> PostType:
        """Determine the type of a Douyin post.

        Args:
            post (Dict[str, Any]): Dictionary containing the post data.

        Returns:
            PostType: Enum representing the type of the post (e.g., video,
                image, live, collection, story).
        """
        try:
            aweme_type = int(post.get("aweme_type", post.get("type", -1)))
            
            # Special post types
            if aweme_type == 1:
                return PostType.LIVE
            elif aweme_type == 3:
                return PostType.COLLECTION
            elif aweme_type == 4:
                return PostType.STORY
                
            # Check for images and videos
            has_images = bool(post.get("images"))
            has_video = bool(
                post.get("video_play_addr") or
                post.get("video", {}).get("play_addr")
            )
            
            if has_images and has_video:
                return PostType.MIXED
            elif has_images:
                return PostType.IMAGES
            elif has_video:
                return PostType.VIDEO
                
            return PostType.UNKNOWN
            
        except (ValueError, TypeError):
            return PostType.UNKNOWN

    async def _download_post_content(
        self,
        post: Dict[str, Any],
        post_type: PostType,
        user_path: Path,
    ) -> None:
        """Download the content of a Douyin post.

        Args:
            post (Dict[str, Any]): Dictionary containing the post data.
            post_type (PostType): Type of the post.
            user_path (Path): Path to the user's directory for saving
                downloaded content.
        """
        logger.info(
            "Downloading post content",
            extra={
                "aweme_id": post.get("aweme_id"),
                "post_type": post_type
            }
        )
        
        try:
            if post_type == PostType.LIVE:
                await self.handler.downloader.create_live_download_tasks(
                    self.handler.kwargs,
                    [post],
                    user_path
                )
            elif post_type == PostType.COLLECTION:
                await self.handler.downloader.create_collection_download_tasks(
                    self.handler.kwargs,
                    [post],
                    user_path
                )
            elif post_type == PostType.STORY:
                await self.handler.downloader.create_story_download_tasks(
                    self.handler.kwargs,
                    [post],
                    user_path
                )
            else:
                if PostType.IMAGES in (post_type, PostType.MIXED):
                    await self._download_images(post, user_path)
                if PostType.VIDEO in (post_type, PostType.MIXED):
                    await self._download_video(post, user_path)
                    
        except Exception as e:
            logger.error(
                "Error downloading content",
                extra={
                    "aweme_id": post.get("aweme_id"),
                    "post_type": post_type,
                    "error": str(e)
                }
            )
            raise

    async def _download_images(
        self,
        post: Dict[str, Any],
        user_path: Path,
    ) -> None:
        """Download images from a Douyin post.

        Args:
            post (Dict[str, Any]): Dictionary containing the post data.
            user_path (Path): Path to the user's directory for saving
                downloaded images.
        """
        image_data = {
            "aweme_id": str(post.get("aweme_id")),
            "desc": post.get("desc", ""),
            "image_urls": self._extract_image_urls(post),
            "create_time": post.get("create_time", ""),
        }
        await self.handler.downloader.create_image_download_tasks(
            self.handler.kwargs,
            image_data,
            user_path
        )

    async def _download_video(
        self,
        post: Dict[str, Any],
        user_path: Path,
    ) -> None:
        """Download the video from a Douyin post.

        Args:
            post (Dict[str, Any]): Dictionary containing the post data.
            user_path (Path): Path to the user's directory for saving the
                downloaded video.
        """
        await self.handler.downloader.create_download_tasks(
            self.handler.kwargs,
            [post],
            user_path
        )

    def _extract_image_urls(self, post: Dict[str, Any]) -> List[str]:
        """Extract image URLs from a Douyin post.

        Args:
            post (Dict[str, Any]): Dictionary containing the post data.

        Returns:
            List[str]: List of image URLs extracted from the post.
        """
        image_urls = []
        images = post.get("images", [])
        if isinstance(images, list):
            for img in images:
                if isinstance(img, dict) and img.get("url_list"):
                    image_urls.extend([
                        url for url in img["url_list"]
                        if url and url.startswith("http")
                    ])
        return image_urls

    def _extract_video_info(self, post: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Extract video information from a Douyin post.

        Args:
            post (Dict[str, Any]): Dictionary containing the post data.

        Returns:
            Optional[Dict[str, Any]]: Dictionary containing video information
                (play URL, duration, aspect ratio, width, height), or None if
                no video information is available.
        """
        video = post.get("video", {})
        play_addr = video.get("play_addr", {})
        if play_addr and play_addr.get("url_list"):
            return {
                "play_addr": play_addr["url_list"][0],
                "duration": video.get("duration", 0),
                "ratio": video.get("ratio", ""),
                "width": play_addr.get("width", 0),
                "height": play_addr.get("height", 0)
            }
        return None

    def _extract_image_info(self, post: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
        """Extract image information from a Douyin post.

        Args:
            post (Dict[str, Any]): Dictionary containing the post data.

        Returns:
            Optional[List[Dict[str, Any]]]: List of dictionaries containing
                image information (URL, width, height), or None if no image
                information is available.
        """
        images = post.get("images", [])
        if not images:
            return None
            
        image_info = []
        for img in images:
            if isinstance(img, dict) and img.get("url_list"):
                image_info.append({
                    "url": img["url_list"][0],
                    "width": img.get("width", 0),
                    "height": img.get("height", 0)
                })
        return image_info if image_info else None

    def _create_download_response(
        self,
        sec_user_id: str,
        download_path: str,
        total_posts: int,
        download_stats: Dict[PostType, int],
    ) -> BulkDownloadResponse:
        """Create the response object for a bulk download operation.

        Args:
            sec_user_id (str): Unique identifier of the user.
            download_path (str): Path to the directory where the content was
                downloaded.
            total_posts (int): Total number of posts for the user.
            download_stats (Dict[PostType, int]): Dictionary containing the
                download statistics for each post type.

        Returns:
            BulkDownloadResponse: Object containing the results of the bulk
                download operation.
        """
        total_downloaded = sum(download_stats.values())
        
        status = (
            DownloadStatus.SUCCESS if total_downloaded == total_posts
            else DownloadStatus.PARTIAL_SUCCESS if total_downloaded > 0
            else DownloadStatus.FAILED
        )
        
        message = (
            f"Downloaded {total_downloaded} out of {total_posts} posts. "
            f"(Videos: {download_stats[PostType.VIDEO]}, "
            f"Images: {download_stats[PostType.IMAGES]}, "
            f"Mixed: {download_stats[PostType.MIXED]}, "
            f"Lives: {download_stats[PostType.LIVE]}, "
            f"Collections: {download_stats[PostType.COLLECTION]}, "
            f"Stories: {download_stats[PostType.STORY]}) "
            f"Files saved to {download_path}"
        )
        
        return BulkDownloadResponse(
            sec_user_id=sec_user_id,
            download_path=download_path,
            total_posts=total_posts,
            downloaded_count=download_stats,
            total_downloaded=total_downloaded,
            status=status,
            message=message
        )
