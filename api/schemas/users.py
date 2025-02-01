"""Schema definitions for Douyin user data models.

This module defines Pydantic models for:
    - User profiles
    - Following relationships
    - User statistics

These models ensure data validation and provide serialization/deserialization.
"""

from pydantic import BaseModel
from typing import Optional, List

class UserProfile(BaseModel):
    """User profile data model.
    
    Attributes:
        sec_user_id: Unique identifier for the user.
        nickname: User's display name.
        signature: User's profile bio/description.
        following_count: Number of users being followed.
        follower_count: Number of followers.
        total_favorited: Total number of likes received.
        aweme_count: Total number of posts.
        avatar_url: URL to user's profile picture.
    """
    sec_user_id: str
    nickname: str
    signature: Optional[str]
    following_count: int  
    follower_count: int
    total_favorited: int
    aweme_count: int
    avatar_url: Optional[str]
    
    class Config:
        from_attributes = True

class UserFollowing(BaseModel):
    """User following relationship model.
    
    Attributes:
        sec_user_id: Unique identifier for the followed user.
        nickname: Display name of the followed user.
        signature: Profile bio of the followed user.
        avatar_url: Profile picture URL of the followed user.
    """
    sec_user_id: str
    nickname: str
    signature: Optional[str]
    avatar_url: Optional[str]
    
    class Config:
        from_attributes = True
