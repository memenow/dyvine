"""Posts router for handling post-related endpoints."""
from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import APIKeyHeader
from typing import Dict

from f2.apps.douyin.handler import DouyinHandler
from api.core.config import get_douyin_handler
from api.core.security import verify_api_key
from api.schemas.posts import BulkDownloadResponse
from api.services.posts import PostService

router = APIRouter()
api_key_header = APIKeyHeader(name="X-API-Key")

def get_post_service(
    handler: DouyinHandler = Depends(get_douyin_handler)
) -> PostService:
    """Dependency to get PostService instance."""
    return PostService(handler)

@router.get("/posts/{aweme_id}")
async def get_post_detail(
    aweme_id: str,
    api_key: str = Depends(api_key_header),
    post_service: PostService = Depends(get_post_service)
):
    """Get post details by aweme_id."""
    if not verify_api_key(api_key):
        raise HTTPException(status_code=401, detail="Invalid API key")
        
    try:
        return await post_service.get_post_detail(aweme_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.get("/users/{sec_user_id}/posts")
async def get_user_posts(
    sec_user_id: str,
    max_cursor: int = 0,
    count: int = 20,
    api_key: str = Depends(api_key_header),
    post_service: PostService = Depends(get_post_service)
):
    """Get user's posts."""
    if not verify_api_key(api_key):
        raise HTTPException(status_code=401, detail="Invalid API key")
        
    try:
        return await post_service.get_user_posts(
            sec_user_id=sec_user_id,
            max_cursor=max_cursor,
            count=count
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/users/{sec_user_id}/download-all", response_model=BulkDownloadResponse)
async def download_all_user_posts(
    sec_user_id: str,
    max_cursor: int = 0,
    api_key: str = Depends(api_key_header),
    post_service: PostService = Depends(get_post_service)
):
    """Download all posts (videos and images) from a user."""
    if not verify_api_key(api_key):
        raise HTTPException(status_code=401, detail="Invalid API key")
    
    try:
        return await post_service.download_all_user_posts(
            sec_user_id=sec_user_id,
            max_cursor=max_cursor
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Unexpected error during bulk download: {str(e)}"
        )
