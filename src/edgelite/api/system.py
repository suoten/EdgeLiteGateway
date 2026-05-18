"""系统管理API路由"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Body, HTTPException

from edgelite.api.deps import CurrentUser, SystemServiceDep, require_permission
from edgelite.api.error_codes import SystemErrors
from edgelite.models.common import ApiResponse
from edgelite.models.system import SystemStatusResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/system", tags=["System"])


@router.get("/status", response_model=ApiResponse[SystemStatusResponse])
async def get_system_status(
    svc: SystemServiceDep,
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    try:
        status_data = await svc.get_status()
        return ApiResponse(data=status_data)
    except Exception as e:
        logger.error("get_system_status failed: %s", e)
        raise HTTPException(status_code=500, detail=SystemErrors.STATUS_FAILED) from e  # FIXED: 原问题-中文硬编码detail，改为error_code


@router.get("/backup", response_model=ApiResponse)
async def list_backups(
    svc: SystemServiceDep,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    try:
        backups = await svc.list_backups()
        return ApiResponse(data=backups)
    except Exception as e:
        logger.error("list_backups failed: %s", e)
        raise HTTPException(status_code=500, detail=SystemErrors.BACKUP_LIST_FAILED) from e  # FIXED: 原问题-中文硬编码detail，改为error_code


@router.post("/backup", response_model=ApiResponse, status_code=201)
async def create_backup(
    svc: SystemServiceDep,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    try:
        backup = await svc.create_backup()
        return ApiResponse(data=backup)
    except Exception as e:
        logger.error("create_backup failed: %s", e)
        raise HTTPException(status_code=500, detail=SystemErrors.BACKUP_CREATE_FAILED) from e  # FIXED: 原问题-中文硬编码detail，改为error_code


@router.post("/restore", response_model=ApiResponse)
async def restore_backup(
    svc: SystemServiceDep,
    backup_id: str = Body(..., embed=True),
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    if not re.match(r"^[a-zA-Z0-9_-]+$", backup_id):
        raise HTTPException(status_code=400, detail=SystemErrors.INVALID_BACKUP_ID)  # FIXED: 原问题-中文硬编码detail，改为error_code
    try:
        success = await svc.restore_backup(backup_id)
        if not success:
            raise HTTPException(status_code=404, detail=SystemErrors.BACKUP_NOT_FOUND)  # FIXED: 原问题-中文硬编码detail，改为error_code
        return ApiResponse()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("restore_backup failed: %s", e)
        raise HTTPException(status_code=500, detail=SystemErrors.RESTORE_FAILED) from e  # FIXED: 原问题-中文硬编码detail，改为error_code
