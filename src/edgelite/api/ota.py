"""OTA升级管理API路由"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Query

from edgelite.models.common import ApiResponse
from edgelite.api.deps import CurrentUser, require_permission
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/ota", tags=["OTA升级"])

_ota_lock = asyncio.Lock()


def _get_ota_manager():
    try:
        from edgelite.app import _app_state
        return getattr(_app_state, "ota_manager", None)
    except (ImportError, AttributeError) as e:
        logger.debug("OTA管理器未加载: %s", e)
        return None
    except Exception as e:
        logger.warning("获取OTA管理器异常: %s", e)
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
        logger.exception("OTA检查更新失败")
        raise HTTPException(status_code=500, detail="检查更新失败，请稍后重试")


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
            logger.exception("OTA应用更新失败")
            raise HTTPException(status_code=500, detail="应用更新失败，请稍后重试")


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
        logger.exception("OTA回滚失败")
        raise HTTPException(status_code=500, detail="回滚失败，请稍后重试")


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
        logger.exception("OTA获取备份列表失败")
        raise HTTPException(status_code=500, detail="获取备份列表失败")
