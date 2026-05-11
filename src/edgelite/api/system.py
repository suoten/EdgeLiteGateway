"""系统管理API路由"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Body, HTTPException

from edgelite.api.deps import CurrentUser, SystemServiceDep, require_permission
from edgelite.models.common import ApiResponse
from edgelite.models.system import SystemStatusResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/system", tags=["系统管理"])


@router.get("/status", response_model=ApiResponse[SystemStatusResponse])
async def get_system_status(
    svc: SystemServiceDep,
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    try:
        status_data = await svc.get_status()
        return ApiResponse(data=status_data)
    except Exception as e:
        logger.error("获取系统状态失败: %s", e)
        raise HTTPException(status_code=500, detail="获取系统状态失败") from e


@router.get("/backup", response_model=ApiResponse)
async def list_backups(
    svc: SystemServiceDep,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    try:
        backups = await svc.list_backups()
        return ApiResponse(data=backups)
    except Exception as e:
        logger.error("获取备份列表失败: %s", e)
        raise HTTPException(status_code=500, detail="获取备份列表失败") from e


@router.post("/backup", response_model=ApiResponse, status_code=201)
async def create_backup(
    svc: SystemServiceDep,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    try:
        backup = await svc.create_backup()
        return ApiResponse(data=backup)
    except Exception as e:
        error_msg = str(e)
        if "malformed" in error_msg.lower() or "database disk image" in error_msg.lower():
            logger.error("数据库损坏，创建备份失败: %s", e)
            raise HTTPException(
                status_code=503,
                detail="数据库文件损坏，无法创建备份。请执行数据库修复或删除损坏的数据库文件后重启系统。",
            ) from e
        logger.error("创建备份失败: %s", e)
        raise HTTPException(status_code=500, detail="创建备份失败") from e


@router.post("/restore", response_model=ApiResponse)
async def restore_backup(
    svc: SystemServiceDep,
    backup_id: str = Body(..., embed=True),
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    if not re.match(r"^[a-zA-Z0-9_-]+$", backup_id):
        raise HTTPException(status_code=400, detail="无效的备份ID")
    try:
        success = await svc.restore_backup(backup_id)
        if not success:
            raise HTTPException(status_code=404, detail="备份不存在")
        return ApiResponse()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("恢复失败: %s", e)
        raise HTTPException(status_code=500, detail="恢复失败") from e
