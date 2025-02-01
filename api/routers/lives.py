from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import APIKeyHeader

from f2.apps.douyin.handler import DouyinHandler
from api.core.config import get_douyin_handler
from api.core.security import verify_api_key

router = APIRouter()
api_key_header = APIKeyHeader(name="X-API-Key")

@router.get("/lives/{webcast_id}")
async def get_live_info(
    webcast_id: str,
    api_key: str = Depends(api_key_header),
    handler: DouyinHandler = Depends(get_douyin_handler)
):
    """Get live streaming information"""
    if not verify_api_key(api_key):
        raise HTTPException(status_code=401, detail="Invalid API key")
        
    try:
        live = await handler.fetch_user_live_videos(webcast_id)
        return live._to_dict()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
