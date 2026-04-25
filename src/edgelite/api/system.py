"""系统管理API路由"""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException

from edgelite.models.common import ApiResponse
from edgelite.api.deps import CurrentUser, require_permission
from edgelite.security.rbac import Permission

router = APIRouter(prefix="/api/v1/system", tags=["系统管理"])


def _get_system_service():
    from edgelite.app import _app_state
    return _app_state.system_service


@router.get("/status", response_model=ApiResponse)
async def get_system_status(user: CurrentUser = require_permission(Permission.SYSTEM_READ)):
    svc = _get_system_service()
    status_data = await svc.get_status()
    return ApiResponse(data=status_data)


@router.get("/backup", response_model=ApiResponse)
async def list_backups(user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE)):
    svc = _get_system_service()
    backups = await svc.list_backups()
    return ApiResponse(data=backups)


@router.post("/backup", response_model=ApiResponse, status_code=201)
async def create_backup(user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE)):
    svc = _get_system_service()
    backup = await svc.create_backup()
    return ApiResponse(data=backup)


@router.post("/restore", response_model=ApiResponse)
async def restore_backup(backup_id: str, user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE)):
    if not re.match(r'^[a-zA-Z0-9_-]+$', backup_id):
        raise HTTPException(status_code=400, detail="无效的备份ID")
    svc = _get_system_service()
    success = await svc.restore_backup(backup_id)
    if not success:
        raise HTTPException(status_code=404, detail="备份不存在")
    return ApiResponse()
