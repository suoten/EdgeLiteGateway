"""视频接入API路由"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from edgelite.models.common import ApiResponse
from edgelite.api.deps import CurrentUser, require_permission
from edgelite.security.rbac import Permission

router = APIRouter(prefix="/api/v1/video", tags=["视频接入"])


def _get_video_service():
    from edgelite.app import _app_state
    return _app_state.video_service


@router.get("/{device_id}/stream", response_model=ApiResponse)
async def get_stream_url(
    device_id: str,
    channel_id: str = "1",
    user: CurrentUser = require_permission(Permission.VIDEO_READ),
):
    svc = _get_video_service()
    url = await svc.get_stream_url(device_id, channel_id)
    if not url:
        raise HTTPException(status_code=503, detail="视频流地址获取失败")
    return ApiResponse(data={"url": url, "device_id": device_id, "channel_id": channel_id})


@router.post("/{device_id}/ptz", response_model=ApiResponse)
async def ptz_control(
    device_id: str,
    action: str,
    channel_id: str = "1",
    user: CurrentUser = require_permission(Permission.VIDEO_CONTROL),
):
    svc = _get_video_service()
    success = await svc.ptz_control(device_id, channel_id, action)
    if not success:
        raise HTTPException(status_code=400, detail="云台控制失败")
    return ApiResponse()


@router.post("/webhook", response_model=ApiResponse)
async def video_webhook(event_data: dict):
    """接收PyGBSentry Webhook回调"""
    svc = _get_video_service()
    await svc.handle_webhook(event_data)
    return ApiResponse()
