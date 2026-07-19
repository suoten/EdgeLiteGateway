"""OTA升级管理API路由"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Query

from edgelite.api.deps import CurrentUser, OtaManagerDep, require_permission
from edgelite.api.error_codes import OtaErrors
from edgelite.models.common import ApiResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/ota", tags=["OTA"])

_ota_lock = asyncio.Lock()


@router.get("/check", response_model=ApiResponse)
async def check_update(
    mgr: OtaManagerDep,
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    if not mgr:
        raise HTTPException(
            status_code=503, detail=OtaErrors.NOT_ENABLED
        )  # FIXED: 原问题-硬编码错误码字符串，改为集中管理
    try:
        result = await mgr.check_update()
        return ApiResponse(data=result)
    except Exception:
        logger.exception("OTA check_update failed")
        raise HTTPException(
            status_code=500, detail=OtaErrors.CHECK_FAILED
        ) from None  # FIXED: 原问题-硬编码错误码字符串，改为集中管理


@router.post("/apply", response_model=ApiResponse)
async def apply_update(
    mgr: OtaManagerDep,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    if _ota_lock.locked():
        raise HTTPException(
            status_code=409, detail=OtaErrors.IN_PROGRESS
        )  # FIXED: 原问题-硬编码错误码字符串，改为集中管理
    try:
        async with _ota_lock:
            if not mgr:
                raise HTTPException(
                    status_code=503, detail=OtaErrors.NOT_ENABLED
                )  # FIXED: 原问题-硬编码错误码字符串，改为集中管理
            try:
                update_info = await mgr.check_update()
                if not update_info:
                    raise HTTPException(
                        status_code=404, detail=OtaErrors.NO_UPDATE
                    )  # FIXED: 原问题-硬编码错误码字符串，改为集中管理
                version = update_info.get("version", "")
                download_url = update_info.get("download_url", "")
                if not download_url:
                    raise HTTPException(
                        status_code=500, detail=OtaErrors.NO_DOWNLOAD_URL
                    )  # FIXED: 原问题-硬编码错误码字符串，改为集中管理
                update_file = await mgr.download_update(version, download_url)
                if not update_file:
                    raise HTTPException(
                        status_code=500, detail=OtaErrors.DOWNLOAD_FAILED
                    )  # FIXED: 原问题-硬编码错误码字符串，改为集中管理
                result = await mgr.apply_update(update_file)
                return ApiResponse(data={"success": result})
            except HTTPException:
                raise
            except Exception:
                logger.exception("OTA apply_update failed")
                raise HTTPException(
                    status_code=500, detail=OtaErrors.APPLY_FAILED
                ) from None  # FIXED: 原问题-硬编码错误码字符串，改为集中管理
    except HTTPException:
        raise
    except Exception as e:
        logger.error("OTA apply failed: %s", e)
        raise HTTPException(
            status_code=500, detail=OtaErrors.APPLY_FAILED
        ) from e  # FIXED: 原问题-硬编码错误码字符串，改为集中管理


@router.post("/rollback", response_model=ApiResponse)
async def rollback_update(
    mgr: OtaManagerDep,
    version: str = Query(default="", description="Target version for rollback"),
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    if not mgr:
        raise HTTPException(
            status_code=503, detail=OtaErrors.NOT_ENABLED
        )  # FIXED: 原问题-硬编码错误码字符串，改为集中管理
    try:
        result = await mgr.rollback(version if version else None)
        return ApiResponse(data=result)
    except Exception:
        logger.exception("OTA rollback failed")
        raise HTTPException(
            status_code=500, detail=OtaErrors.ROLLBACK_FAILED
        ) from None  # FIXED: 原问题-硬编码错误码字符串，改为集中管理


@router.get("/backups", response_model=ApiResponse)
async def list_ota_backups(
    mgr: OtaManagerDep,
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    if not mgr:
        raise HTTPException(
            status_code=503, detail=OtaErrors.NOT_ENABLED
        )  # FIXED: 原问题-硬编码错误码字符串，改为集中管理
    try:
        result = await asyncio.to_thread(mgr.list_backups)
        return ApiResponse(data={"backups": result})
    except Exception:
        logger.exception("OTA list_backups failed")
        raise HTTPException(
            status_code=500, detail=OtaErrors.LIST_BACKUPS_FAILED
        ) from None  # FIXED: 原问题-硬编码错误码字符串，改为集中管理


# --- OTA 状态与取消端点 ---


@router.get("/status", response_model=ApiResponse)
async def get_ota_status(
    mgr: OtaManagerDep,
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    """返回当前升级状态"""
    try:
        if not mgr:
            return ApiResponse(
                data={
                    "enabled": False,
                    "in_progress": False,
                    "state": "not_configured",
                }
            )
        # 探测 OTA manager 实际暴露的状态属性
        in_progress = bool(getattr(mgr, "in_progress", False) or getattr(mgr, "_in_progress", False))
        state = getattr(mgr, "state", None) or getattr(mgr, "_state", None)
        if state is None:
            state = "in_progress" if in_progress else "idle"
        current_version = getattr(mgr, "current_version", None) or getattr(mgr, "_current_version", None)
        target_version = getattr(mgr, "target_version", None) or getattr(mgr, "_target_version", None)
        progress = getattr(mgr, "progress", None)
        try:
            last_error = getattr(mgr, "last_error", None) or getattr(mgr, "_last_error", None)
        except Exception:
            last_error = None
        return ApiResponse(
            data={
                "enabled": True,
                "in_progress": in_progress,
                "state": state,
                "current_version": current_version,
                "target_version": target_version,
                "progress": progress,
                "last_error": str(last_error) if last_error else None,
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("OTA status failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.post("/cancel", response_model=ApiResponse)
async def cancel_ota(
    mgr: OtaManagerDep,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    """取消进行中的升级"""
    try:
        if not mgr:
            raise HTTPException(status_code=503, detail=OtaErrors.NOT_ENABLED)
        # 优先调用 cancel 方法
        cancel_method = getattr(mgr, "cancel", None)
        if callable(cancel_method):
            import asyncio as _asyncio

            if _asyncio.iscoroutinefunction(cancel_method):
                result = await cancel_method()
            else:
                result = await asyncio.to_thread(cancel_method)
        else:
            # 兼容：标记 in_progress = False
            if hasattr(mgr, "in_progress"):
                mgr.in_progress = False
            elif hasattr(mgr, "_in_progress"):
                mgr._in_progress = False
            result = True
        return ApiResponse(
            data={
                "cancelled": bool(result),
                "in_progress": bool(
                    getattr(mgr, "in_progress", False) or getattr(mgr, "_in_progress", False)
                ),
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("OTA cancel failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None
