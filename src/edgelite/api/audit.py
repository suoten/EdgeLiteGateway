"""审计日志API路由"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query

from edgelite.api.deps import AuditServiceDep, CurrentUser, PaginationDep, require_permission
from edgelite.api.error_codes import AuditErrors
from edgelite.models.common import ApiResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/audit", tags=["Audit"])


@router.get("/logs", response_model=ApiResponse)
async def query_audit_logs(
    svc: AuditServiceDep,
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
    pagination: PaginationDep = None,  # FIXED: 原问题-默认值None导致类型检查误判，但Python语法要求有默认值（前参有默认值）
    user_id: str | None = None,
    action: str | None = None,
    resource_type: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
):
    if not svc:
        # FIXED: 原问题-中文硬编码detail，改为error_code
        raise HTTPException(status_code=501, detail=AuditErrors.NOT_ENABLED)

    from edgelite.services.audit_service import AuditAction

    action_enum = None
    if action:
        try:
            action_enum = AuditAction(action)
        except ValueError:
            # FIXED: 原问题-中文硬编码detail，改为error_code
            raise HTTPException(status_code=400, detail=AuditErrors.INVALID_ACTION) from None

    try:
        st = datetime.fromisoformat(start_time) if start_time else None
    except ValueError:
        # FIXED: 原问题-中文硬编码detail，改为error_code
        raise HTTPException(status_code=400, detail=AuditErrors.INVALID_TIME_FORMAT) from None
    try:
        et = datetime.fromisoformat(end_time) if end_time else None
    except ValueError:
        # FIXED: 原问题-中文硬编码detail，改为error_code
        raise HTTPException(status_code=400, detail=AuditErrors.INVALID_TIME_FORMAT) from None

    try:
        logs, total = await svc.query(
            user_id=user_id,
            action=action_enum,
            resource_type=resource_type,
            start_time=st,
            end_time=et,
            page=pagination.page,
            size=pagination.size,
        )
        return ApiResponse(data={"logs": logs, "total": total})
    except Exception as e:
        logger.error("query_audit_logs failed: %s", e)
        raise HTTPException(status_code=500, detail=AuditErrors.LIST_FAILED) from e


@router.get("/integrity", response_model=ApiResponse)
async def verify_integrity(
    svc: AuditServiceDep,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    try:
        if not svc:
            # FIXED: 原问题-中文硬编码detail，改为error_code
            raise HTTPException(status_code=501, detail=AuditErrors.NOT_ENABLED)

        result = await svc.verify_integrity()
        return ApiResponse(data=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("verify_integrity failed: %s", e)
        raise HTTPException(status_code=500, detail=AuditErrors.INTEGRITY_FAILED) from e


@router.get("/export/csv", response_model=ApiResponse)
async def export_audit_csv(
    svc: AuditServiceDep,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
    start_time: str | None = None,
    end_time: str | None = None,
):
    if not svc:
        # FIXED: 原问题-中文硬编码detail，改为error_code
        raise HTTPException(status_code=501, detail=AuditErrors.NOT_ENABLED)

    try:
        st = datetime.fromisoformat(start_time) if start_time else None
    except ValueError:
        # FIXED: 原问题-中文硬编码detail，改为error_code
        raise HTTPException(status_code=400, detail=AuditErrors.INVALID_TIME_FORMAT) from None
    try:
        et = datetime.fromisoformat(end_time) if end_time else None
    except ValueError:
        # FIXED: 原问题-中文硬编码detail，改为error_code
        raise HTTPException(status_code=400, detail=AuditErrors.INVALID_TIME_FORMAT) from None

    try:
        csv_content = await svc.export_csv(start_time=st, end_time=et)
        return ApiResponse(data={"content": csv_content})
    except Exception as e:
        logger.error("export_audit_csv failed: %s", e)
        raise HTTPException(status_code=500, detail=AuditErrors.EXPORT_FAILED) from e


@router.post("/cleanup", response_model=ApiResponse)
async def cleanup_audit_logs(
    svc: AuditServiceDep,
    retention_days: int = Query(90, ge=1),
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    try:
        if not svc:
            # FIXED: 原问题-中文硬编码detail，改为error_code
            raise HTTPException(status_code=501, detail=AuditErrors.NOT_ENABLED)

        deleted = await svc.cleanup(retention_days=retention_days)
        return ApiResponse(data={"deleted": deleted})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("cleanup_audit_logs failed: %s", e)
        raise HTTPException(status_code=500, detail=AuditErrors.CLEANUP_FAILED) from e
