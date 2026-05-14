"""告警管理API路由"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from edgelite.api.deps import AlarmServiceDep, CurrentUser, PaginationDep, require_permission
from edgelite.models.alarm import AlarmResponse
from edgelite.models.common import ApiResponse, PagedResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/alarms", tags=["Alarms"])


@router.get("", response_model=PagedResponse[AlarmResponse])
async def list_alarms(
    svc: AlarmServiceDep,
    user: CurrentUser = require_permission(Permission.ALARM_READ),
    pagination: PaginationDep = None,  # FIXED: 原问题-硬编码分页参数，未使用公共PaginationParams模型
    status: str | None = None,
    severity: str | None = None,
    device_id: str | None = None,
    search: str | None = None,
):
    try:
        alarms, total = await svc.list_alarms(pagination.page, pagination.size, status, severity, device_id, search)
        return PagedResponse(data=alarms, total=total, page=pagination.page, size=pagination.size)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("获取告警列表失败: %s", e)
        raise HTTPException(status_code=500, detail="获取告警列表失败") from e


@router.get("/{alarm_id}", response_model=ApiResponse[AlarmResponse])
async def get_alarm(
    alarm_id: str,
    svc: AlarmServiceDep,
    user: CurrentUser = require_permission(Permission.ALARM_READ),
):
    try:
        alarm = await svc.get_alarm(alarm_id)
        if alarm is None:
            raise HTTPException(status_code=404, detail="告警不存在")
        return ApiResponse(data=alarm)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("获取告警详情失败: %s", e)
        raise HTTPException(status_code=500, detail="获取告警详情失败") from e


@router.put("/{alarm_id}/ack", response_model=ApiResponse[AlarmResponse])
async def ack_alarm(
    alarm_id: str,
    svc: AlarmServiceDep,
    user: CurrentUser = require_permission(Permission.ALARM_ACK),
):
    try:
        alarm = await svc.ack_alarm(alarm_id, user["username"])
        if alarm is None:
            raise HTTPException(status_code=404, detail="告警不存在")
        return ApiResponse(data=alarm)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("确认告警失败: %s", e)
        raise HTTPException(status_code=500, detail="确认告警失败") from e
