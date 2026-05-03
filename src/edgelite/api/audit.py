"""审计日志API路由"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query

from edgelite.models.common import ApiResponse
from edgelite.api.deps import CurrentUser, require_permission
from edgelite.security.rbac import Permission

router = APIRouter(prefix="/api/v1/audit", tags=["审计日志"])


def _get_audit_service():
    from edgelite.app import _app_state
    return getattr(_app_state, "audit_service", None)


@router.get("/logs", response_model=ApiResponse)
async def query_audit_logs(
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=1000),
    user_id: str | None = None,
    action: str | None = None,
    resource_type: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
):
    svc = _get_audit_service()
    if not svc:
        raise HTTPException(status_code=501, detail="审计日志服务未启用")

    from edgelite.services.audit_service import AuditAction
    action_enum = None
    if action:
        try:
            action_enum = AuditAction(action)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"无效的action: {action}")

    try:
        st = datetime.fromisoformat(start_time) if start_time else None
    except ValueError:
        raise HTTPException(status_code=400, detail=f"无效的start_time格式: {start_time}")
    try:
        et = datetime.fromisoformat(end_time) if end_time else None
    except ValueError:
        raise HTTPException(status_code=400, detail=f"无效的end_time格式: {end_time}")

    logs, total = await svc.query(
        user_id=user_id, action=action_enum, resource_type=resource_type,
        start_time=st, end_time=et, page=page, size=size,
    )
    return ApiResponse(data={"logs": logs, "total": total})


@router.get("/integrity", response_model=ApiResponse)
async def verify_integrity(
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    svc = _get_audit_service()
    if not svc:
        raise HTTPException(status_code=501, detail="审计日志服务未启用")

    result = await svc.verify_integrity()
    return ApiResponse(data=result)


@router.get("/export/csv", response_model=ApiResponse)
async def export_audit_csv(
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
    start_time: str | None = None,
    end_time: str | None = None,
):
    svc = _get_audit_service()
    if not svc:
        raise HTTPException(status_code=501, detail="审计日志服务未启用")

    try:
        st = datetime.fromisoformat(start_time) if start_time else None
    except ValueError:
        raise HTTPException(status_code=400, detail=f"无效的start_time格式: {start_time}")
    try:
        et = datetime.fromisoformat(end_time) if end_time else None
    except ValueError:
        raise HTTPException(status_code=400, detail=f"无效的end_time格式: {end_time}")

    csv_content = await svc.export_csv(start_time=st, end_time=et)
    return ApiResponse(data={"content": csv_content})


@router.post("/cleanup", response_model=ApiResponse)
async def cleanup_audit_logs(
    retention_days: int = Query(90, ge=1),
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    svc = _get_audit_service()
    if not svc:
        raise HTTPException(status_code=501, detail="审计日志服务未启用")

    deleted = await svc.cleanup(retention_days=retention_days)
    return ApiResponse(data={"deleted": deleted})
