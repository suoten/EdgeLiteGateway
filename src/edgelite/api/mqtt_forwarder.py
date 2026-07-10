"""MQTT北向转发离线缓存API"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from edgelite.api.deps import require_permission
from edgelite.api.error_codes import ServiceErrors
from edgelite.models.common import ApiResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/mqtt-forwarder", tags=["MQTT Forwarder"])


@router.get("/offline-queue/status")
async def get_offline_queue_status(
    request: Request,
    _user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """获取MQTT离线缓存队列状态"""
    # FIXED-P1: 原问题-get_offline_queue_status()调用无异常保护，内部错误会导致500无友好提示
    try:
        forwarder = getattr(request.app.state, "mqtt_forwarder", None)
        if not forwarder:
            return ApiResponse(
                data={
                    "enabled": False,
                    "pending_count": 0,
                    "sent_count": 0,
                    "oldest_timestamp": None,
                    "db_size_bytes": 0,
                }
            )
        return ApiResponse(data=forwarder.get_offline_queue_status())
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_offline_queue_status failed: %s", e)
        raise HTTPException(status_code=500, detail=ServiceErrors.STATUS_FAILED) from e
