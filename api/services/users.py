"""User service layer for handling business logic."""
from typing import Dict, List

from f2.apps.douyin.handler import DouyinHandler
from api.schemas.users import UserProfile, UserFollowing

class UserService:
    def __init__(self, handler: DouyinHandler):
        self.handler = handler

    async def get_user_profile(self, sec_user_id: str) -> Dict:
        """Get user profile information."""
        profile = await self.handler.fetch_user_profile(sec_user_id)
        return profile._to_dict()

    async def get_user_following(
        self, 
        sec_user_id: str, 
        offset: int = 0, 
        count: int = 20
    ) -> List[Dict]:
        """Get user's following list."""
        following = await self.handler.fetch_user_following(
            sec_user_id=sec_user_id,
            offset=offset,
            count=count
        )
        return following._to_list() 