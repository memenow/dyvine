"""Service layer for managing Douyin post operations.

This module provides functionality for interacting with Douyin posts, including:
    - Fetching post details
    - Retrieving user posts
    - Downloading post content (videos and images)
    - Managing bulk download operations

Typical usage example:
    post_service = PostService(handler)
    post_detail = await post_service.get_post_detail(aweme_id)
    user_posts = await post_service.get_user_posts(sec_user_id)
"""

from typing import Dict, Optional, List
import logging
from pathlib import Path

from f2.apps.douyin.handler import DouyinHandler
from f2.apps.douyin.db import AsyncUserDB
from api.schemas.posts import PostType, DownloadStatus, BulkDownloadResponse

class PostService:
    """Service class for handling Douyin post operations.
    
    This class provides methods for interacting with Douyin posts, including
    fetching post details, user posts, and downloading post content.
    
    Attributes:
        handler: An instance of DouyinHandler for interacting with Douyin.
    """

    def __init__(self, handler: DouyinHandler) -> None:
        """Initialize the PostService.
        
        Args:
            handler: A DouyinHandler instance for Douyin operations.
        """
        self.handler = handler

    async def get_post_detail(self, aweme_id: str) -> Dict:
        """Fetches detailed information about a specific Douyin post.

        Args:
            aweme_id: The unique identifier of the Douyin post.

        Returns:
            A dictionary containing post details including:
                - aweme_id: Post identifier
                - desc: Post description
                - video/image info
                - author info
                - statistics

        Raises:
            ValueError: If the post cannot be found or is invalid.
        """
        post = await self.handler.fetch_one_video(aweme_id)
        return post._to_dict()

    async def get_user_posts(
        self, 
        sec_user_id: str, 
        max_cursor: int = 0, 
        count: int = 20
    ) -> Dict:
        """Retrieves a paginated list of posts from a specific user.

        Args:
            sec_user_id: The unique identifier of the Douyin user.
            max_cursor: Pagination cursor for fetching next page. Defaults to 0.
            count: Number of posts to fetch per page. Defaults to 20.

        Returns:
            A dictionary containing:
                - posts: List of post objects
                - has_more: Boolean indicating if more posts exist
                - max_cursor: Cursor for next page

        Raises:
            ValueError: If the user or posts cannot be fetched.
        """
        async for posts in self.handler.fetch_user_post_videos(
            sec_user_id=sec_user_id,
            max_cursor=max_cursor,
            page_counts=count
        ):
            return posts._to_dict()

    async def download_all_user_posts(
        self, 
        sec_user_id: str, 
        max_cursor: int = 0
    ) -> BulkDownloadResponse:
        """Download all posts from a specified user.
        
        This method will attempt to download all available posts (videos and images)
        from the specified user. It handles pagination automatically and provides
        detailed progress information.
        
        Args:
            sec_user_id: The unique identifier of the Douyin user.
            max_cursor: Starting point for pagination.
            
        Returns:
            BulkDownloadResponse: Object containing download results and statistics.
            
        Raises:
            ValueError: If the user cannot be found.
            Exception: If there are errors during the download process.
        """
        # Get user profile
        profile = await self.handler.fetch_user_profile(sec_user_id)
        if not profile:
            raise ValueError("User not found")
        
        logging.info(f"Found user {profile.nickname} with {profile.aweme_count} posts")

        download_stats = {
            PostType.VIDEO: 0,
            PostType.IMAGES: 0,
            PostType.MIXED: 0,
            PostType.UNKNOWN: 0
        }
        total_posts = profile.aweme_count or 0
        download_path = None
        
        async with AsyncUserDB("douyin_users.db") as db:
            user_path = await self.handler.get_or_add_user_data(
                self.handler.kwargs, 
                sec_user_id, 
                db
            )
            download_path = str(user_path)
            logging.info(f"Created download directory at {download_path}")

        async for posts in self.handler.fetch_user_post_videos(
            sec_user_id=sec_user_id,
            max_cursor=max_cursor,
            page_counts=20
        ):
            if not posts or not posts.has_aweme:
                continue
            
            await self._process_posts(posts._to_list(), download_stats, user_path)
            
            if not posts.has_more:
                break

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
            f"Mixed: {download_stats[PostType.MIXED]}) "
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

    async def _process_posts(
        self, 
        posts: List[Dict], 
        download_stats: Dict[PostType, int],
        user_path: Path
    ) -> None:
        """Process and download posts."""
        for post in posts:
            try:
                post_type = self._determine_post_type(post)
                if post_type == PostType.UNKNOWN:
                    continue

                await self._download_post_content(post, post_type, user_path)
                download_stats[post_type] += 1
                logging.info(f"Successfully downloaded {post_type} post")
                
            except Exception as e:
                logging.error(
                    f"Error processing post {post.get('aweme_id', 'unknown')}: {str(e)}"
                )

    def _determine_post_type(self, post: Dict) -> PostType:
        """Determine the type of a post."""
        has_images = False
        has_video = False
        
        # Check for images
        if post.get('aweme_type') == 2:
            images = post.get('images', [])
            if isinstance(images, list):
                for img in images:
                    if isinstance(img, dict) and img.get('url_list'):
                        has_images = any(
                            url and url.startswith('http') 
                            for url in img['url_list']
                        )
                        if has_images:
                            break
        
        # Check for video
        if post.get('aweme_type') == 0:
            video = post.get('video', {})
            play_addr = video.get('play_addr', {})
            video_urls = play_addr.get('url_list', [])
            has_video = any(
                url and url.startswith('http') 
                for url in video_urls
            )
        
        if has_images and has_video:
            return PostType.MIXED
        elif has_images:
            return PostType.IMAGES
        elif has_video:
            return PostType.VIDEO
        return PostType.UNKNOWN

    async def _download_post_content(
        self, 
        post: Dict, 
        post_type: PostType, 
        user_path: Path
    ) -> None:
        """Download the content of a post."""
        if PostType.IMAGES in (post_type, PostType.MIXED):
            image_data = {
                'aweme_id': str(post.get('aweme_id')),
                'desc': post.get('desc', ''),
                'image_urls': self._extract_image_urls(post),
                'create_time': post.get('create_time', ''),
            }
            await self.handler.downloader.create_image_download_tasks(
                self.handler.kwargs,
                image_data,
                user_path
            )
        
        if PostType.VIDEO in (post_type, PostType.MIXED):
            await self.handler.downloader.create_download_tasks(
                self.handler.kwargs,
                [post],
                user_path
            )

    def _extract_image_urls(self, post: Dict) -> List[str]:
        """Extract image URLs from a post."""
        image_urls = []
        images = post.get('images', [])
        if isinstance(images, list):
            for img in images:
                if isinstance(img, dict) and img.get('url_list'):
                    image_urls.extend([
                        url for url in img['url_list']
                        if url and url.startswith('http')
                    ])
        return image_urls