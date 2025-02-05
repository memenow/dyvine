"""User-related API endpoints.

This module handles routing for user profile and relationship operations.
"""
from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import APIKeyHeader
from typing import List

from f2.apps.douyin.handler import DouyinHandler
from api.core.config import get_douyin_handler
from api.core.security import verify_api_key
from api.schemas.users import UserProfile, UserFollowing
from api.services.users import UserService

router = APIRouter()
api_key_header = APIKeyHeader(name="X-API-Key")

def get_user_service(
    handler: DouyinHandler = Depends(get_douyin_handler)
) -> UserService:
    """Dependency to get UserService instance."""
    return UserService(handler)

@router.get("/users/{sec_user_id}/profile", response_model=UserProfile)
async def get_user_profile(
    sec_user_id: str,
    api_key: str = Depends(api_key_header),
    user_service: UserService = Depends(get_user_service)
):
    """Get a user's profile information.
    
    Args:
        sec_user_id: Target user's identifier.
        api_key: API authentication key.
        user_service: Injected user service instance.
        
    Returns:
        UserProfile: User's profile information.
        
    Raises:
        HTTPException: If authentication fails or user not found.
    """
    if not verify_api_key(api_key):
        raise HTTPException(status_code=401, detail="Invalid API key")
        
    try:
        return await user_service.get_user_profile(sec_user_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.get("/users/{sec_user_id}/following", response_model=List[UserFollowing])
async def get_user_following(
    sec_user_id: str,
    offset: int = 0,
    count: int = 20,
    api_key: str = Depends(api_key_header),
    user_service: UserService = Depends(get_user_service)
):
    """Get user's following list."""
    if not verify_api_key(api_key):
        raise HTTPException(status_code=401, detail="Invalid API key")
        
    try:
        return await user_service.get_user_following(
            sec_user_id=sec_user_id,
            offset=offset,
            count=count
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
