"""告警管理API路由"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query

from edgelite.api.deps import AlarmServiceDep, CurrentUser, PaginationDep, require_permission, AuditServiceDep
from edgelite.api.error_codes import AlarmErrors
from edgelite.models.alarm import AlarmResponse
from edgelite.models.common import ApiResponse, PagedResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/alarms", tags=["Alarms"])


@router.get("/trend", response_model=ApiResponse)
async def get_alarm_trend(
    svc: AlarmServiceDep,
    user: CurrentUser = require_permission(Permission.ALARM_READ),
    hours: int = Query(24, ge=1, le=720),
):
    try:
        data = await svc.get_trend(hours)
        return ApiResponse(data=data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_alarm_trend failed: %s", e)
        raise HTTPException(status_code=500, detail=AlarmErrors.LIST_FAILED) from e


@router.get("", response_model=PagedResponse[AlarmResponse])
async def list_alarms(
    svc: AlarmServiceDep,
    user: CurrentUser = require_permission(Permission.ALARM_READ),
    pagination: PaginationDep = None,  # FIXED: 原问题-默认值None导致类型检查误判，但Python语法要求有默认值（前参有默认值）
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
        logger.error("list_alarms failed: %s", e)
        raise HTTPException(status_code=500, detail=AlarmErrors.LIST_FAILED) from e  # FIXED: 原问题-中文硬编码detail，改为error_code


@router.get("/{alarm_id}", response_model=ApiResponse[AlarmResponse])
async def get_alarm(
    alarm_id: str,
    svc: AlarmServiceDep,
    user: CurrentUser = require_permission(Permission.ALARM_READ),
):
    try:
        alarm = await svc.get_alarm(alarm_id)
        if alarm is None:
            raise HTTPException(status_code=404, detail=AlarmErrors.NOT_FOUND)  # FIXED: 原问题-中文硬编码detail，改为error_code
        return ApiResponse(data=alarm)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_alarm failed: %s", e)
        raise HTTPException(status_code=500, detail=AlarmErrors.GET_FAILED) from e  # FIXED: 原问题-中文硬编码detail，改为error_code


@router.put("/{alarm_id}/ack", response_model=ApiResponse[AlarmResponse])
async def ack_alarm(
    alarm_id: str,
    svc: AlarmServiceDep,
    user: CurrentUser = require_permission(Permission.ALARM_ACK),
    audit_svc: AuditServiceDep = None,
):
    try:
        alarm = await svc.ack_alarm(alarm_id, user["username"])
        if alarm is None:
            raise HTTPException(status_code=404, detail=AlarmErrors.NOT_FOUND)  # FIXED: 原问题-中文硬编码detail，改为error_code
        try:
            from edgelite.services.audit_service import AuditAction
            if audit_svc:
                await audit_svc.log(AuditAction.ALARM_ACK, user_id=user["user_id"], username=user["username"], resource_type="alarm", resource_id=alarm_id)
        except Exception:
            pass
        return ApiResponse(data=alarm)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("ack_alarm failed: %s", e)
        raise HTTPException(status_code=500, detail=AlarmErrors.ACK_FAILED) from e  # FIXED: 原问题-中文硬编码detail，改为error_code


@router.put("/{alarm_id}/recover", response_model=ApiResponse[AlarmResponse])
async def recover_alarm(
    alarm_id: str,
    svc: AlarmServiceDep,
    user: CurrentUser = require_permission(Permission.ALARM_ACK),
    audit_svc: AuditServiceDep = None,
):
    try:
        alarm = await svc.clear_alarm(alarm_id)
        if alarm is None:
            raise HTTPException(status_code=404, detail=AlarmErrors.NOT_FOUND)
        try:
            from edgelite.services.audit_service import AuditAction
            if audit_svc:
                await audit_svc.log(AuditAction.ALARM_ACK, user_id=user["user_id"], username=user["username"], resource_type="alarm", resource_id=alarm_id, details="recover")
        except Exception:
            pass
        return ApiResponse(data=alarm)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("recover_alarm failed: %s", e)
        raise HTTPException(status_code=500, detail=AlarmErrors.ACK_FAILED) from e
