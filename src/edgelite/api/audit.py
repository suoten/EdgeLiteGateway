"""审计日志API路由"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request

from edgelite.api.deps import AuditServiceDep, PaginationDep, require_permission
from edgelite.api.error_codes import AuditErrors, AuthzErrors, CommonErrors
from edgelite.models.common import ApiResponse, PagedResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/audit", tags=["Audit"])


@router.get("/logs", response_model=PagedResponse)
async def query_audit_logs(
    svc: AuditServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
    pagination: PaginationDep = None,  # FIXED: 原问题-默认值None导致类型检查误判，但Python语法要求有默认值（前参有默认值）  # noqa: E501
    user_id: str | None = None,
    action: str | None = None,
    resource_type: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
):
    if not svc:
        # FIXED: 原问题-中文硬编码detail，改为error_code
        raise HTTPException(status_code=501, detail=AuditErrors.NOT_ENABLED)

    # FIXED(一般): 原问题-user_id 查询参数未做归属校验，任何认证用户可查询任意其他 user_id 的审计日志;
    # 修复-非 admin 用户强制 user_id 为自身，admin 可查询任意 user_id
    if user["role"] != "admin":
        if user_id is not None and user_id != user["user_id"]:
            raise HTTPException(status_code=403, detail=AuthzErrors.RESOURCE_OWNERSHIP_DENIED)
        user_id = user["user_id"]

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
        # FIXED-P1: 原问题-分页接口返回ApiResponse而非PagedResponse，与devices/rules/alarms/users不一致
        return PagedResponse(data=logs, total=total, page=pagination.page, size=pagination.size)
    except Exception as e:
        logger.error("query_audit_logs failed: %s", e)
        raise HTTPException(status_code=500, detail=AuditErrors.LIST_FAILED) from e


@router.get("/integrity", response_model=ApiResponse)
async def verify_integrity(
    svc: AuditServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
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
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
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
    request: Request,
    svc: AuditServiceDep,
    audit_svc: AuditServiceDep,
    retention_days: int = Query(90, ge=1, le=3650),  # FIXED-P2: 原问题-retention_days无上限，可传超大值删除全部审计日志
    confirm: bool = Body(..., embed=True),
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    # 第三轮审计修复: 高风险操作二次确认
    if not confirm:
        # R11-API-27: 硬编码中文错误信息替换为 error_code 引用
        raise HTTPException(status_code=400, detail=CommonErrors.CONFIRM_REQUIRED)
    # 第三轮审计修复: 记录审计日志清理操作本身
    from edgelite.api.auth import _get_client_ip
    from edgelite.services.audit_service import AuditAction

    client_ip = _get_client_ip(request)
    user_agent = request.headers.get("user-agent", "")
    try:
        if not svc:
            # FIXED: 原问题-中文硬编码detail，改为error_code
            raise HTTPException(status_code=501, detail=AuditErrors.NOT_ENABLED)

        deleted = await svc.cleanup(retention_days=retention_days)
        try:
            await audit_svc.log(
                AuditAction.LOG_CLEAR,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="audit_logs",
                ip_address=client_ip,
                user_agent=user_agent,
                after_value={"deleted": deleted, "retention_days": retention_days},
            )
        except Exception as e:
            logger.warning("Audit log failed: %s", e)
        return ApiResponse(data={"deleted": deleted})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("cleanup_audit_logs failed: %s", e)
        try:
            await audit_svc.log(
                AuditAction.LOG_CLEAR,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="audit_logs",
                ip_address=client_ip,
                user_agent=user_agent,
                status="failed",
                error_message=str(e),
            )
        except Exception as audit_e:
            logger.warning("Audit log failed: %s", audit_e)
        raise HTTPException(status_code=500, detail=AuditErrors.CLEANUP_FAILED) from e
