"""视频接入API路由"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from edgelite.api.deps import (
    ConfigDep,
    DeviceServiceDep,
    VideoServiceDep,
    require_permission,
)
from edgelite.api.error_codes import VideoErrors
from edgelite.models.common import ApiResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/video", tags=["Video"])

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

    if config and getattr(config, "server", None) and getattr(config.server, "webhook_api_key", None):
        if not x_api_key or not hmac.compare_digest(x_api_key, config.server.webhook_api_key):
            raise HTTPException(status_code=401, detail=VideoErrors.API_KEY_INVALID)
    else:
        raise HTTPException(status_code=401, detail=VideoErrors.API_KEY_NOT_CONFIGURED)


@router.get("/{device_id}/stream", response_model=ApiResponse)
async def get_stream_url(
    device_id: str,
    svc: VideoServiceDep,
    device_svc: DeviceServiceDep,
    channel_id: str = Query(default="1", max_length=64),
    user: dict[str, str] = Depends(require_permission(Permission.VIDEO_READ)),
):
    try:
        # FIXED(严重): 原问题-未检查设备访问权限，任何有 VIDEO_READ 权限的用户可获取
        # 任意设备的视频流 URL（IDOR 越权）。视频流 URL 通常包含认证凭证，泄露后可被未授权观看
        # 修复：复用 devices.py 的设备归属校验逻辑
        from edgelite.api.devices import _check_device_owner

        await _check_device_owner(device_svc, device_id, user)
        url = await svc.get_stream_url(device_id, channel_id)
        if not url:
            raise HTTPException(status_code=503, detail=VideoErrors.STREAM_NOT_AVAILABLE)
        return ApiResponse(data={"url": url, "device_id": device_id, "channel_id": channel_id})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_stream_url failed: %s", e, exc_info=True)
        raise HTTPException(status_code=503, detail=VideoErrors.STREAM_NOT_AVAILABLE) from e


@router.post("/{device_id}/ptz", response_model=ApiResponse)
async def ptz_control(
    device_id: str,
    action: _PTZ_ACTIONS,
    svc: VideoServiceDep,
    device_svc: DeviceServiceDep,
    channel_id: str = Query(default="1", max_length=64),
    user: dict[str, str] = Depends(require_permission(Permission.VIDEO_CONTROL)),
):
    try:
        # FIXED(严重): 原问题-未检查设备访问权限，任何有 VIDEO_CONTROL 权限的用户可控制
        # 任意设备的 PTZ（IDOR 越权）。PTZ 控制是物理操作，越权控制可能影响监控覆盖范围
        # 修复：复用 devices.py 的设备归属校验逻辑
        from edgelite.api.devices import _check_device_owner

        await _check_device_owner(device_svc, device_id, user)
        success = await svc.ptz_control(device_id, channel_id, action)
        if not success:
            raise HTTPException(status_code=400, detail=VideoErrors.PTZ_FAILED)
        return ApiResponse()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("ptz_control failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=VideoErrors.PTZ_FAILED) from e


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
        logger.error("video_webhook failed: %s", e)
        raise HTTPException(status_code=500, detail=VideoErrors.WEBHOOK_FAILED) from e
