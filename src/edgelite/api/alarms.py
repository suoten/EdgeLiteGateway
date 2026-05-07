"""告警管理API路由"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query

from edgelite.models.alarm import AlarmResponse
from edgelite.models.common import ApiResponse, PagedResponse
from edgelite.api.deps import CurrentUser, require_permission
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/alarms", tags=["告警管理"])


def _get_alarm_service():
    from edgelite.app import _app_state
    return _app_state.alarm_service


@router.get("", response_model=PagedResponse[AlarmResponse])
async def list_alarms(
    user: CurrentUser = require_permission(Permission.ALARM_READ),
    page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=1000),
    status: str | None = None, severity: str | None = None, device_id: str | None = None,
    search: str | None = None,
):
    try:
        svc = _get_alarm_service()
        alarms, total = await svc.list_alarms(page, size, status, severity, device_id, search)
        return PagedResponse(data=alarms, total=total, page=page, size=size)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("获取告警列表失败: %s", e)
        raise HTTPException(status_code=500, detail=f"获取告警列表失败: {e}")


@router.get("/{alarm_id}", response_model=ApiResponse[AlarmResponse])
async def get_alarm(alarm_id: str, user: CurrentUser = require_permission(Permission.ALARM_READ)):
    try:
        svc = _get_alarm_service()
        alarm = await svc.get_alarm(alarm_id)
        if alarm is None:
            raise HTTPException(status_code=404, detail="告警不存在")
        return ApiResponse(data=alarm)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("获取告警详情失败: %s", e)
        raise HTTPException(status_code=500, detail=f"获取告警详情失败: {e}")


@router.put("/{alarm_id}/ack", response_model=ApiResponse[AlarmResponse])
async def ack_alarm(alarm_id: str, user: CurrentUser = require_permission(Permission.ALARM_ACK)):
    try:
        svc = _get_alarm_service()
        alarm = await svc.ack_alarm(alarm_id, user["username"])
        if alarm is None:
            raise HTTPException(status_code=404, detail="告警不存在")
        return ApiResponse(data=alarm)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("确认告警失败: %s", e)
        raise HTTPException(status_code=500, detail=f"确认告警失败: {e}")
