"""Service module for handling user-related operations.

This module provides a UserService class that encapsulates the business logic
for various user-related operations in the Douyin application, including:

- Retrieving user information from Douyin
- Initiating asynchronous downloads of user content (posts, liked posts)
- Tracking the progress and status of download tasks

The UserService class follows the Singleton pattern to ensure a single instance
throughout the application. It interacts with the DouyinHandler from the f2
library to perform the necessary operations.

The module also defines custom exceptions related to user operations, such as
UserNotFoundError and DownloadError.
"""

import asyncio
import uuid
import re
from typing import Dict, Optional
import logging
from datetime import datetime
from pathlib import Path
from f2.apps.douyin.handler import DouyinHandler

from ..core.settings import settings
from ..schemas.users import UserResponse, DownloadResponse

def sanitize_filename(filename: str) -> str:
    """Remove emoji and special characters from a filename.

    This function removes emoji and other special characters from a filename
    to ensure it is safe for use across different filesystems.

    Args:
        filename (str): The original filename.

    Returns:
        str: The sanitized filename with special characters removed.
    """
    # Remove emoji and other special characters
    filename = re.sub(r'[^\x00-\x7F]+', '', filename)
    
    # Replace invalid filename characters with underscore
    filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
    
    # Remove multiple underscores
    filename = re.sub(r'_+', '_', filename)
    
    # Remove leading/trailing underscores and spaces
    filename = filename.strip('_ ')
    
    # Ensure filename isn't empty
    if not filename:
        filename = 'untitled'
        
    return filename

logger = logging.getLogger(__name__)

class UserServiceError(Exception):
    """Base exception class for errors related to the UserService.

    This exception serves as a general parent class for all custom exceptions
    defined within the UserService, allowing for more specific error handling
    and categorization.
    """
    pass

class UserNotFoundError(UserServiceError):
    """Exception raised when a requested user cannot be found.

    This exception indicates that a specific Douyin user, typically identified
    by their user ID, could not be retrieved or does not exist.
    """
    pass

class DownloadError(UserServiceError):
    """Exception raised when a download operation fails.

    This exception indicates that an attempt to download content associated with
    a Douyin user has failed. It may be due to network issues, invalid URLs,
    or other problems during the download process.
    """
    pass

class UserService:
    """Service class for handling user-related operations.

    This class encapsulates the business logic for various user-related
    operations in the Douyin application, such as retrieving user information,
    initiating content downloads, and tracking download progress. It follows the
    Singleton pattern to ensure a single instance throughout the application.
    """

    _instance = None
    _active_downloads: Dict[str, Dict] = {}

    def __new__(cls):
        """Ensure a single instance of the UserService class (Singleton pattern)."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        """Initialize the UserService instance (only once)."""
        # Skip initialization if already initialized
        if hasattr(self, '_initialized'):
            return
        self._initialized = True

    async def get_user_info(self, user_id: str) -> UserResponse:
        """Retrieve user information from Douyin.

        Args:
            user_id: The Douyin user ID.

        Returns:
            UserResponse: Object containing the user's information.

        Raises:
            UserNotFoundError: If the requested user cannot be found.
            UserServiceError: If an error occurs during the operation.
        """
        try:
            # Initialize f2 handler with settings
            handler_kwargs = {
                "url": f"https://www.douyin.com/user/{user_id}",
                "cookie": settings.douyin_cookie,
                "headers": {
                    "User-Agent": settings.douyin_user_agent,
                    "Referer": settings.douyin_referer,
                },
                "proxy": settings.douyin_proxy_http,
                "mode": "post"
            }
            # Initialize f2 handler with settings
            handler_kwargs = {
                "url": f"https://www.douyin.com/user/{user_id}",
                "cookie": settings.douyin_cookie,
                "headers": {
                    "User-Agent": settings.douyin_user_agent,
                    "Referer": settings.douyin_referer,
                },
                "proxy": settings.douyin_proxy_http,
                "mode": "post"
            }
            print(f"handler_kwargs: {handler_kwargs}")

            handler = DouyinHandler(handler_kwargs)
            print(f"Handler initialized: {handler}") # Added print statement
            user_data = await handler.fetch_user_profile(user_id)
            print(f"user_data: {user_data}")

            if not user_data.nickname:
                raise UserNotFoundError(f"User {user_id} not found")

            return UserResponse(
                user_id=user_id,
                nickname=user_data.nickname,
                avatar_url=user_data.avatar_url,
                signature=user_data.signature,
                following_count=user_data.following_count,
                follower_count=user_data.follower_count,
                total_favorited=user_data.total_favorited,
                is_living=bool(user_data.room_id),
                room_id=user_data.room_id
            )
        except Exception as e:
            print(f"Error in get_user_info: {type(e).__name__} - {str(e)}")
            logger.exception("Failed to get user info", extra={"user_id": user_id})
            raise UserServiceError(f"Failed to get user info: {str(e)}") from e

    async def start_download(
        self,
        user_id: str,
        include_posts: bool = True,
        include_likes: bool = False,
        max_items: Optional[int] = None,
    ) -> DownloadResponse:
        """Start an asynchronous download of user content from Douyin.

        Args:
            user_id: The Douyin user ID.
            include_posts: Whether to include the user's posts in the download.
            include_likes: Whether to include the user's liked posts in the download.
            max_items: Maximum number of items to download.

        Returns:
            DownloadResponse: Object containing details about the initiated download task.
        """
        task_id = str(uuid.uuid4())

        # Initialize download task
        self._active_downloads[task_id] = {
            "user_id": user_id,
            "status": "pending",
            "progress": 0.0,
            "start_time": datetime.now(),
            "include_posts": include_posts,
            "include_likes": include_likes,
            "max_items": max_items
        }

        # Start download task
        asyncio.create_task(
            self._process_download(task_id)
        )

        return DownloadResponse(
            task_id=task_id,
            status="pending",
            message="Download started",
            progress=0.0
        )

    async def get_download_status(self, task_id: str) -> DownloadResponse:
        """Get the status of a download task.

        Args:
            task_id: The unique identifier of the download task.

        Returns:
            DownloadResponse: Object containing the current status of the download task.

        Raises:
            DownloadError: If the specified download task is not found.
        """
        if task_id not in self._active_downloads:
            raise DownloadError(f"Download task {task_id} not found")

        task = self._active_downloads[task_id]
        response = DownloadResponse(
            task_id=task_id,
            status=task["status"],
            message=f"Download {task['status']}",
            progress=task["progress"],
            total_items=task.get("total_items"),
            downloaded_items=task.get("downloaded_items"),
            error=task.get("error")
        )
        return response

    async def _process_download(self, task_id: str) -> None:
        """Process a download task asynchronously.

        Args:
            task_id (str): The unique identifier of the download task.
        """
        task = self._active_downloads[task_id]
        
        try:
            # Update status to running
            task["status"] = "running"
            
            # Create downloads directory if it doesn't exist
            downloads_dir = Path("downloads")
            downloads_dir.mkdir(exist_ok=True)
            
            # Initialize f2 handler with settings
            handler_kwargs = {
                "url": f"https://www.douyin.com/user/{task['user_id']}",
                "cookie": settings.douyin_cookie,
                "headers": {
                    "User-Agent": settings.douyin_user_agent,
                    "Referer": settings.douyin_referer,
                },
                "proxy": settings.douyin_proxy_http,
                "download_path": str(downloads_dir),
                "max_counts": task["max_items"] if task["max_items"] else None,
                "download_favorite": task["include_likes"],
                "timeout": 5,
                "folderize": True,
                "mode": "post",
                "naming": "{create}_{desc}",
                "download_image": True,
                "filename_filter": sanitize_filename  # Add filename sanitization
            }
            
            handler = DouyinHandler(handler_kwargs)
            task["total_items"] = 0
            task["downloaded_items"] = 0
            
            # Get user profile to create user directory and verify post count
            user_data = await handler.fetch_user_profile(task["user_id"])
            if not user_data.nickname:
                raise UserNotFoundError(f"User {task['user_id']} not found")
                
            # Get total posts count from profile
            total_posts = user_data.aweme_count
            if total_posts == 0:
                logger.info(f"User {task['user_id']} has no posts")
                task["status"] = "completed"
                task["progress"] = 100.0
                return
                
            logger.info(f"User {user_data.nickname} has {total_posts} posts")
            task["total_items"] = total_posts
                
            # Create user directory
            user_dir = downloads_dir / user_data.nickname
            user_dir.mkdir(exist_ok=True)
            
            # Use the handler to download user posts
            max_cursor = 0
            has_more = True
            downloaded_count = 0
            
            while has_more and (task["max_items"] is None or downloaded_count < task["max_items"]):
                async for aweme_data in handler.fetch_user_post_videos(
                    task["user_id"],
                    min_cursor=0,
                    max_cursor=max_cursor,
                    page_counts=100,  # Increased page size
                    max_counts=None  # Don't limit per-page count
                ):
                    if not aweme_data.has_aweme:
                        has_more = False
                        break
                        
                    current_batch_size = len(aweme_data.aweme_id)
                    downloaded_count += current_batch_size
                    task["downloaded_items"] = downloaded_count
                    task["progress"] = (downloaded_count / total_posts) * 100
                    
                    logger.info(f"Downloaded {downloaded_count}/{total_posts} posts ({task['progress']:.1f}%)")
                    
                    # Create download tasks for the videos
                    await handler.downloader.create_download_tasks(
                        handler_kwargs,
                        aweme_data._to_list(),
                        user_dir
                    )
                    
                    # Update cursor for next page
                    if aweme_data.max_cursor != max_cursor:
                        max_cursor = aweme_data.max_cursor
                        has_more = aweme_data.has_more
                    else:
                        # If cursor didn't change but we haven't got all posts, increment it
                        if downloaded_count < total_posts:
                            max_cursor += 1
                            has_more = True
                        else:
                            has_more = False
                    
                    # If max_items is set and we've reached it, stop
                    if task["max_items"] and task["downloaded_items"] >= task["max_items"]:
                        has_more = False
                        break
                    
                    # Add delay between pages
                    await asyncio.sleep(handler_kwargs.get("timeout", 5))
                    
            # Verify download completion
            completion_percentage = (downloaded_count / total_posts) * 100
            
            if completion_percentage >= 100:
                # Consider anything >= 100% as complete success
                logger.info(
                    f"Successfully downloaded {downloaded_count} posts "
                    f"(100% complete)"
                )
                task["status"] = "completed"
                task["progress"] = 100.0
            else:
                # Less than 100% means we missed some posts
                logger.warning(
                    f"Only downloaded {downloaded_count}/{total_posts} posts "
                    f"({completion_percentage:.1f}%). Some posts may have been missed."
                )
                task["status"] = "partial"
                task["error"] = f"Only downloaded {downloaded_count} out of {total_posts} posts"
                task["progress"] = completion_percentage
            
        except Exception as e:
            logger.exception(
                "Download failed",
                extra={
                    "task_id": task_id,
                    "user_id": task["user_id"]
                }
            )
            task["status"] = "failed"
            task["error"] = str(e)
            
        finally:
            # Clean up after some time
            await asyncio.sleep(3600)  # Keep for 1 hour
            self._active_downloads.pop(task_id, None)
