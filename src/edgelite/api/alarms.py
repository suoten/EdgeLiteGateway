"""告警管理API路由"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from edgelite.models.alarm import AlarmResponse
from edgelite.models.common import ApiResponse, PagedResponse
from edgelite.api.deps import CurrentUser, require_permission
from edgelite.security.rbac import Permission

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
    svc = _get_alarm_service()
    alarms, total = await svc.list_alarms(page, size, status, severity, device_id)
    if search:
        search_lower = search.lower()
        alarms = [a for a in alarms if search_lower in a.get("device_id", "").lower() or search_lower in a.get("rule_id", "").lower()]
    return PagedResponse(data=alarms, total=total, page=page, size=size)


@router.get("/{alarm_id}", response_model=ApiResponse[AlarmResponse])
async def get_alarm(alarm_id: str, user: CurrentUser = require_permission(Permission.ALARM_READ)):
    svc = _get_alarm_service()
    alarm = await svc.get_alarm(alarm_id)
    if alarm is None:
        raise HTTPException(status_code=404, detail="告警不存在")
    return ApiResponse(data=alarm)


@router.put("/{alarm_id}/ack", response_model=ApiResponse[AlarmResponse])
async def ack_alarm(alarm_id: str, user: CurrentUser = require_permission(Permission.ALARM_ACK)):
    svc = _get_alarm_service()
    alarm = await svc.ack_alarm(alarm_id, user["username"])
    if alarm is None:
        raise HTTPException(status_code=404, detail="告警不存在")
    return ApiResponse(data=alarm)
