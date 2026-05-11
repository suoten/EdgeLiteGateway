"""审计日志API路由"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query

from edgelite.api.deps import AuditServiceDep, CurrentUser, require_permission
from edgelite.models.common import ApiResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/audit", tags=["审计日志"])


@router.get("/logs", response_model=ApiResponse)
async def query_audit_logs(
    svc: AuditServiceDep,
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=1000),
    user_id: str | None = None,
    action: str | None = None,
    resource_type: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
):
    if not svc:
        raise HTTPException(status_code=501, detail="审计日志服务未启用")

    from edgelite.services.audit_service import AuditAction

    action_enum = None
    if action:
        try:
            action_enum = AuditAction(action)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"无效的action: {action}") from None

    try:
        st = datetime.fromisoformat(start_time) if start_time else None
    except ValueError:
        raise HTTPException(status_code=400, detail=f"无效的start_time格式: {start_time}") from None
    try:
        et = datetime.fromisoformat(end_time) if end_time else None
    except ValueError:
        raise HTTPException(status_code=400, detail=f"无效的end_time格式: {end_time}") from None

    logs, total = await svc.query(
        user_id=user_id,
        action=action_enum,
        resource_type=resource_type,
        start_time=st,
        end_time=et,
        page=page,
        size=size,
    )
    return ApiResponse(data={"logs": logs, "total": total})


@router.get("/integrity", response_model=ApiResponse)
async def verify_integrity(
    svc: AuditServiceDep,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    try:
        if not svc:
            raise HTTPException(status_code=501, detail="审计日志服务未启用")

        result = await svc.verify_integrity()
        return ApiResponse(data=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("验证失败: %s", e)
        raise HTTPException(status_code=500, detail="验证失败") from e


@router.get("/export/csv", response_model=ApiResponse)
async def export_audit_csv(
    svc: AuditServiceDep,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
    start_time: str | None = None,
    end_time: str | None = None,
):
    if not svc:
        raise HTTPException(status_code=501, detail="审计日志服务未启用")

    try:
        st = datetime.fromisoformat(start_time) if start_time else None
    except ValueError:
        raise HTTPException(status_code=400, detail=f"无效的start_time格式: {start_time}") from None
    try:
        et = datetime.fromisoformat(end_time) if end_time else None
    except ValueError:
        raise HTTPException(status_code=400, detail=f"无效的end_time格式: {end_time}") from None

    csv_content = await svc.export_csv(start_time=st, end_time=et)
    return ApiResponse(data={"content": csv_content})


@router.post("/cleanup", response_model=ApiResponse)
async def cleanup_audit_logs(
    svc: AuditServiceDep,
    retention_days: int = Query(90, ge=1),
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    try:
        if not svc:
            raise HTTPException(status_code=501, detail="审计日志服务未启用")

        deleted = await svc.cleanup(retention_days=retention_days)
        return ApiResponse(data={"deleted": deleted})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("清理失败: %s", e)
        raise HTTPException(status_code=500, detail="清理失败") from e
