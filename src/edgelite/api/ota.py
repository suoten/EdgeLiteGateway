"""OTA升级管理API路由"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Query

from edgelite.api.deps import CurrentUser, OtaManagerDep, require_permission
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
        raise HTTPException(status_code=503, detail="ERR_OTA_NOT_ENABLED")
    try:
        result = await mgr.check_update()
        return ApiResponse(data=result)
    except Exception:
        logger.exception("OTA check_update failed")
        raise HTTPException(status_code=500, detail="ERR_OTA_CHECK_FAILED") from None


@router.post("/apply", response_model=ApiResponse)
async def apply_update(
    mgr: OtaManagerDep,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    if _ota_lock.locked():
        raise HTTPException(status_code=409, detail="ERR_OTA_IN_PROGRESS")
    try:
        async with _ota_lock:
            if not mgr:
                raise HTTPException(status_code=503, detail="ERR_OTA_NOT_ENABLED")
            try:
                update_info = await mgr.check_update()
                if not update_info:
                    raise HTTPException(status_code=404, detail="ERR_OTA_NO_UPDATE")
                version = update_info.get("version", "")
                download_url = update_info.get("download_url", "")
                if not download_url:
                    raise HTTPException(status_code=500, detail="ERR_OTA_NO_DOWNLOAD_URL")
                update_file = await mgr.download_update(version, download_url)
                if not update_file:
                    raise HTTPException(status_code=500, detail="ERR_OTA_DOWNLOAD_FAILED")
                result = await mgr.apply_update(update_file)
                return ApiResponse(data={"success": result})
            except HTTPException:
                raise
            except Exception:
                logger.exception("OTA apply_update failed")
                raise HTTPException(status_code=500, detail="ERR_OTA_APPLY_FAILED") from None
    except HTTPException:
        raise
    except Exception as e:
        logger.error("OTA apply failed: %s", e)
        raise HTTPException(status_code=500, detail="ERR_OTA_APPLY_FAILED") from e


@router.post("/rollback", response_model=ApiResponse)
async def rollback_update(
    mgr: OtaManagerDep,
    version: str = Query(default="", description="Target version for rollback"),
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    if not mgr:
        raise HTTPException(status_code=503, detail="ERR_OTA_NOT_ENABLED")
    try:
        result = await mgr.rollback(version if version else None)
        return ApiResponse(data=result)
    except Exception:
        logger.exception("OTA rollback failed")
        raise HTTPException(status_code=500, detail="ERR_OTA_ROLLBACK_FAILED") from None


@router.get("/backups", response_model=ApiResponse)
async def list_ota_backups(
    mgr: OtaManagerDep,
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    if not mgr:
        raise HTTPException(status_code=503, detail="ERR_OTA_NOT_ENABLED")
    try:
        result = await asyncio.to_thread(mgr.list_backups)
        return ApiResponse(data={"backups": result})
    except Exception:
        logger.exception("OTA list_backups failed")
        raise HTTPException(status_code=500, detail="ERR_OTA_LIST_BACKUPS_FAILED") from None
