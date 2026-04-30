"""OTA升级管理API路由"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, UploadFile, File
from pydantic import BaseModel

from edgelite.models.common import ApiResponse
from edgelite.api.deps import CurrentUser, require_permission
from edgelite.security.rbac import Permission

router = APIRouter(prefix="/api/v1/ota", tags=["OTA升级"])


def _get_ota_manager():
    try:
        from edgelite.app import _app_state
        return getattr(_app_state, "ota_manager", None)
    except Exception:
        return None


@router.get("/check", response_model=ApiResponse)
async def check_update(
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    mgr = _get_ota_manager()
    if not mgr:
        raise HTTPException(status_code=503, detail="OTA升级服务未启用")
    try:
        result = await mgr.check_update()
        return ApiResponse(data=result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/apply", response_model=ApiResponse)
async def apply_update(
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    mgr = _get_ota_manager()
    if not mgr:
        raise HTTPException(status_code=503, detail="OTA升级服务未启用")
    try:
        result = await mgr.apply_update()
        return ApiResponse(data=result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/rollback", response_model=ApiResponse)
async def rollback_update(
    version: str = "",
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    mgr = _get_ota_manager()
    if not mgr:
        raise HTTPException(status_code=503, detail="OTA升级服务未启用")
    try:
        result = await mgr.rollback(version if version else None)
        return ApiResponse(data=result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/backups", response_model=ApiResponse)
async def list_ota_backups(
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    mgr = _get_ota_manager()
    if not mgr:
        raise HTTPException(status_code=503, detail="OTA升级服务未启用")
    try:
        result = await mgr.list_backups()
        return ApiResponse(data={"backups": result})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
