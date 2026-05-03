"""OTA升级管理API路由"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from edgelite.models.common import ApiResponse
from edgelite.api.deps import CurrentUser, require_permission
from edgelite.security.rbac import Permission

router = APIRouter(prefix="/api/v1/ota", tags=["OTA升级"])

_ota_lock = asyncio.Lock()


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
    if _ota_lock.locked():
        raise HTTPException(status_code=409, detail="OTA升级正在进行中，请勿重复提交")
    async with _ota_lock:
        mgr = _get_ota_manager()
        if not mgr:
            raise HTTPException(status_code=503, detail="OTA升级服务未启用")
        try:
            update_info = await mgr.check_update()
            if not update_info:
                raise HTTPException(status_code=404, detail="没有可用更新")
            version = update_info.get("version", "")
            download_url = update_info.get("download_url", "")
            if not download_url:
                raise HTTPException(status_code=500, detail="更新信息中缺少下载地址")
            update_file = await mgr.download_update(version, download_url)
            if not update_file:
                raise HTTPException(status_code=500, detail="下载更新包失败")
            result = await mgr.apply_update(update_file)
            return ApiResponse(data={"success": result})
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))


@router.post("/rollback", response_model=ApiResponse)
async def rollback_update(
    version: str = Query(default="", description="回滚目标版本号"),
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
        result = await asyncio.to_thread(mgr.list_backups)
        return ApiResponse(data={"backups": result})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
