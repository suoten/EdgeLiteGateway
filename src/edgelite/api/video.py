"""视频接入API路由"""

from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Header, Depends
from pydantic import BaseModel, Field

from edgelite.models.common import ApiResponse
from edgelite.api.deps import CurrentUser, require_permission
from edgelite.security.rbac import Permission

router = APIRouter(prefix="/api/v1/video", tags=["视频接入"])

_PTZ_ACTIONS = Literal["up", "down", "left", "right", "up_left", "up_right", "down_left", "down_right", "zoom_in", "zoom_out", "focus_in", "focus_out", "stop"]


class VideoWebhookEvent(BaseModel):
    event_type: str = Field(default="", description="事件类型")
    device_id: str = Field(default="", description="设备ID")
    timestamp: Optional[str] = Field(default=None, description="事件时间戳")

    model_config = {"extra": "allow"}


def _get_video_service():
    from edgelite.app import _app_state
    return _app_state.video_service


def _verify_webhook_key(x_api_key: str = Header(default="")) -> None:
    import hmac
    from edgelite.app import _app_state
    config = _app_state.config
    if config and getattr(config, 'server', None) and getattr(config.server, 'webhook_api_key', None):
        if not x_api_key or not hmac.compare_digest(x_api_key, config.server.webhook_api_key):
            raise HTTPException(status_code=401, detail="Invalid API Key")
    else:
        raise HTTPException(status_code=401, detail="API Key not configured")


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
    action: _PTZ_ACTIONS,
    channel_id: str = "1",
    user: CurrentUser = require_permission(Permission.VIDEO_CONTROL),
):
    svc = _get_video_service()
    success = await svc.ptz_control(device_id, channel_id, action)
    if not success:
        raise HTTPException(status_code=400, detail="云台控制失败")
    return ApiResponse()


@router.post("/webhook", response_model=ApiResponse)
async def video_webhook(event: VideoWebhookEvent, auth: None = Depends(_verify_webhook_key)):
    """接收PyGBSentry Webhook回调"""
    svc = _get_video_service()
    await svc.handle_webhook(event.model_dump())
    return ApiResponse()
