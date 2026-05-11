"""视频接入API路由"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from edgelite.api.deps import ConfigDep, CurrentUser, VideoServiceDep, require_permission
from edgelite.models.common import ApiResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/video", tags=["视频接入"])

_PTZ_ACTIONS = Literal[
    "up",
    "down",
    "left",
    "right",
    "up_left",
    "up_right",
    "down_left",
    "down_right",
    "zoom_in",
    "zoom_out",
    "focus_in",
    "focus_out",
    "stop",
]


class VideoWebhookEvent(BaseModel):
    event_type: str = Field(default="", description="事件类型")
    device_id: str = Field(default="", description="设备ID")
    timestamp: str | None = Field(default=None, description="事件时间戳")

    model_config = {"extra": "allow"}


def _verify_webhook_key(config=Depends(ConfigDep), x_api_key: str = Header(default="")) -> None:
    import hmac
    if (
        config
        and getattr(config, "server", None)
        and getattr(config.server, "webhook_api_key", None)
    ):
        if not x_api_key or not hmac.compare_digest(x_api_key, config.server.webhook_api_key):
            raise HTTPException(status_code=401, detail="Invalid API Key")
    else:
        raise HTTPException(status_code=401, detail="API Key not configured")


@router.get("/{device_id}/stream", response_model=ApiResponse)
async def get_stream_url(
    device_id: str,
    svc: VideoServiceDep,
    channel_id: str = "1",
    user: CurrentUser = require_permission(Permission.VIDEO_READ),
):
    try:
        url = await svc.get_stream_url(device_id, channel_id)
        if not url:
            raise HTTPException(status_code=503, detail="视频流地址获取失败")
        return ApiResponse(data={"url": url, "device_id": device_id, "channel_id": channel_id})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("获取失败: %s", e)
        raise HTTPException(status_code=500, detail="获取失败") from e


@router.post("/{device_id}/ptz", response_model=ApiResponse)
async def ptz_control(
    device_id: str,
    action: _PTZ_ACTIONS,
    svc: VideoServiceDep,
    channel_id: str = "1",
    user: CurrentUser = require_permission(Permission.VIDEO_CONTROL),
):
    try:
        success = await svc.ptz_control(device_id, channel_id, action)
        if not success:
            raise HTTPException(status_code=400, detail="云台控制失败")
        return ApiResponse()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("云台控制失败: %s", e)
        raise HTTPException(status_code=500, detail="云台控制失败") from e


@router.post("/webhook", response_model=ApiResponse)
async def video_webhook(
    event: VideoWebhookEvent,
    svc: VideoServiceDep,
    auth: None = Depends(_verify_webhook_key),
):
    try:
        await svc.handle_webhook(event.model_dump())
        return ApiResponse()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("操作失败: %s", e)
        raise HTTPException(status_code=500, detail="操作失败") from e
