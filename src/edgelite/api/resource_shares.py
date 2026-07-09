"""资源共享 API 路由（设备/规则等资源在用户间共享）。

FIXED: 重建丢失的 api/resource_shares 模块（app.py:176 作为必需路由引用但文件不存在，
导致 create_app() 崩溃）[2026-06-30]

底层: edgelite.storage.sqlite_repo.ResourceShareRepo
权限: 共享/取消共享需 SYSTEM_MANAGE；查询需 SYSTEM_READ
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from edgelite.api.deps import DatabaseDep
from edgelite.api.error_codes import CommonErrors
from edgelite.models.common import ApiResponse
from edgelite.security.rbac import Permission, require_permission
from edgelite.storage.sqlite_repo import ResourceShareRepo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/resource-shares", tags=["ResourceShares"])

_VALID_RESOURCE_TYPES = {"device", "rule", "alarm", "variable"}
_VALID_PERMISSION_LEVELS = {"read", "write", "admin"}


class ShareRequest(BaseModel):
    resource_type: str = Field(..., description="资源类型: device/rule/alarm/variable")
    resource_id: str = Field(..., min_length=1, max_length=128)
    shared_with_user_id: str = Field(..., min_length=1, max_length=128)
    permission_level: str = Field(default="read", description="read/write/admin")


class UnshareRequest(BaseModel):
    resource_type: str
    resource_id: str
    shared_with_user_id: str


class AccessCheckRequest(BaseModel):
    resource_type: str
    resource_id: str
    permission_level: str = "read"


def _validate_resource_type(rt: str) -> None:
    if rt not in _VALID_RESOURCE_TYPES:
        raise HTTPException(status_code=400, detail=CommonErrors.VALIDATION_FAILED)


def _validate_permission_level(level: str) -> None:
    if level not in _VALID_PERMISSION_LEVELS:
        raise HTTPException(status_code=400, detail=CommonErrors.VALIDATION_FAILED)


def _repo(database) -> ResourceShareRepo:
    return ResourceShareRepo(database, database.write_lock)


@router.post("", response_model=ApiResponse)
async def share_resource(
    req: ShareRequest,
    database: DatabaseDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    """共享一个资源给指定用户（已存在则更新权限级别）。"""
    _validate_resource_type(req.resource_type)
    _validate_permission_level(req.permission_level)
    try:
        repo = _repo(database)
        result = await repo.share_resource(
            resource_type=req.resource_type,
            resource_id=req.resource_id,
            shared_with_user_id=req.shared_with_user_id,
            permission_level=req.permission_level,
            shared_by_user_id=user["user_id"],
        )
        return ApiResponse(data=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("share_resource failed: %s", e)
        raise HTTPException(status_code=500, detail=CommonErrors.INTERNAL_ERROR) from e


@router.delete("", response_model=ApiResponse)
async def unshare_resource(
    req: UnshareRequest,
    database: DatabaseDep,
    _user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    """取消资源共享。"""
    _validate_resource_type(req.resource_type)
    try:
        repo = _repo(database)
        deleted = await repo.unshare_resource(
            resource_type=req.resource_type,
            resource_id=req.resource_id,
            shared_with_user_id=req.shared_with_user_id,
        )
        return ApiResponse(data={"deleted": deleted})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("unshare_resource failed: %s", e)
        raise HTTPException(status_code=500, detail=CommonErrors.INTERNAL_ERROR) from e


@router.get("", response_model=ApiResponse)
async def list_my_shares(
    database: DatabaseDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
    resource_type: str | None = Query(None, description="按资源类型过滤"),
    page: int = Query(default=1, ge=1, description="页码（从 1 开始）"),
    size: int = Query(default=50, ge=1, le=200, alias="page_size", description="每页数量"),
):
    """列出共享给当前用户的资源。"""
    if resource_type is not None:
        _validate_resource_type(resource_type)
    try:
        repo = _repo(database)
        # FIXED: [P0 无分页] 原 list_shared_with_user 无 LIMIT，全量加载有 OOM 风险 [2026-06-30]
        shares, total = await repo.list_shared_with_user(
            user["user_id"], resource_type, page=page, size=size
        )
        return ApiResponse(
            data={
                "shares": shares,
                "count": len(shares),
                "total": total,
                "page": page,
                "size": size,
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("list_my_shares failed: %s", e)
        raise HTTPException(status_code=500, detail=CommonErrors.INTERNAL_ERROR) from e


@router.get("/resource/{resource_type}/{resource_id}", response_model=ApiResponse)
async def list_shares_for_resource(
    resource_type: str,
    resource_id: str,
    database: DatabaseDep,
    _user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
    page: int = Query(default=1, ge=1, description="页码（从 1 开始）"),
    size: int = Query(default=50, ge=1, le=200, alias="page_size", description="每页数量"),
):
    """列出指定资源的所有共享记录。"""
    _validate_resource_type(resource_type)
    try:
        repo = _repo(database)
        # FIXED: [P0 无分页] 原 list_shares_for_resource 无 LIMIT，全量加载有 OOM 风险 [2026-06-30]
        shares, total = await repo.list_shares_for_resource(
            resource_type, resource_id, page=page, size=size
        )
        return ApiResponse(
            data={
                "shares": shares,
                "count": len(shares),
                "total": total,
                "page": page,
                "size": size,
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("list_shares_for_resource failed: %s", e)
        raise HTTPException(status_code=500, detail=CommonErrors.INTERNAL_ERROR) from e


@router.post("/check", response_model=ApiResponse)
async def check_access(
    req: AccessCheckRequest,
    database: DatabaseDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """检查当前用户对某资源的访问权限。"""
    _validate_resource_type(req.resource_type)
    _validate_permission_level(req.permission_level)
    try:
        repo = _repo(database)
        has_access = await repo.check_user_has_access(
            resource_type=req.resource_type,
            resource_id=req.resource_id,
            user_id=user["user_id"],
            permission_level=req.permission_level,
        )
        return ApiResponse(data={"has_access": has_access})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("check_access failed: %s", e)
        raise HTTPException(status_code=500, detail=CommonErrors.INTERNAL_ERROR) from e
