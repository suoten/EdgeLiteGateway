"""OTA升级管理API路由"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query

from edgelite.api.deps import OtaManagerDep, require_permission
from edgelite.api.error_codes import CommonErrors, OtaErrors
from edgelite.models.common import ApiResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/ota", tags=["OTA"])

_ota_lock = asyncio.Lock()

# FIXED(安全-P1): version 来自 OTA 服务器 JSON 并被拼入下载文件路径（update-{version}.zip），
# 必须做字符白名单，防 `../` 路径遍历
_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


def _validate_version(version: str) -> str:
    """校验版本号字符白名单（仅 [A-Za-z0-9._-]），非法返回 400"""
    if not version or not _VERSION_PATTERN.fullmatch(version):
        raise HTTPException(status_code=400, detail=OtaErrors.INVALID_VERSION)
    return version


def _validate_download_url(download_url: str) -> str:
    """FIXED(安全-P1): 强制 HTTPS 下载地址，防中间人替换固件包。

    http:// 仅在环境变量 EDGELITE_ALLOW_INSECURE_TLS=1（受控测试环境显式放行）时允许。
    """
    parsed = urlparse(download_url)
    if parsed.scheme != "https":
        allow_insecure = os.environ.get("EDGELITE_ALLOW_INSECURE_TLS", "").strip() == "1"
        if parsed.scheme == "http" and allow_insecure:
            logger.warning("OTA download_url uses insecure http:// (EDGELITE_ALLOW_INSECURE_TLS=1): %s", download_url)
        else:
            raise HTTPException(status_code=400, detail=CommonErrors.VALIDATION_FAILED)
    return download_url


@router.get("/check", response_model=ApiResponse)
async def check_update(
    mgr: OtaManagerDep,
    user: dict = Depends(require_permission(Permission.SYSTEM_READ)),
):
    if not mgr:
        # FIXED: OTA 未启用时返回 200 + not_configured 状态，避免 5xx ERROR 日志
        return ApiResponse(data={"enabled": False, "update_available": False})
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
    user: dict = Depends(require_permission(Permission.SYSTEM_MANAGE)),
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
                version = _validate_version(update_info.get("version", ""))
                download_url = update_info.get("download_url", "")
                if not download_url:
                    raise HTTPException(
                        status_code=500, detail=OtaErrors.NO_DOWNLOAD_URL
                    )  # FIXED: 原问题-硬编码错误码字符串，改为集中管理
                download_url = _validate_download_url(download_url)
                # FIXED(安全-P1): 完整性链——必须携带 OTA 服务器下发的 sha256 传给
                # apply_update 做下载后哈希校验；服务器未提供时拒绝应用（fail-closed），
                # 防止退化为"无完整性校验直接解压覆盖应用目录"的危险路径
                expected_sha256 = str(update_info.get("sha256") or update_info.get("expected_sha256") or "").strip()
                if not expected_sha256:
                    raise HTTPException(status_code=400, detail=CommonErrors.VALIDATION_FAILED)
                update_file = await mgr.download_update(version, download_url)
                if not update_file:
                    raise HTTPException(
                        status_code=500, detail=OtaErrors.DOWNLOAD_FAILED
                    )  # FIXED: 原问题-硬编码错误码字符串，改为集中管理
                result = await mgr.apply_update(update_file, expected_sha256=expected_sha256)
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
    user: dict = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    if not mgr:
        raise HTTPException(
            status_code=503, detail=OtaErrors.NOT_ENABLED
        )  # FIXED: 原问题-硬编码错误码字符串，改为集中管理
    # rollback 的 version 同样拼入备份路径，做相同的字符白名单校验
    if version:
        _validate_version(version)
    try:
        result = await mgr.rollback(version if version else None)
        return ApiResponse(data=result)
    except HTTPException:
        raise
    except Exception:
        logger.exception("OTA rollback failed")
        raise HTTPException(
            status_code=500, detail=OtaErrors.ROLLBACK_FAILED
        ) from None  # FIXED: 原问题-硬编码错误码字符串，改为集中管理


@router.get("/backups", response_model=ApiResponse)
async def list_ota_backups(
    mgr: OtaManagerDep,
    user: dict = Depends(require_permission(Permission.SYSTEM_READ)),
):
    if not mgr:
        # FIXED: OTA 未启用时返回 200 + 空列表，与 /status 端点行为一致，避免 5xx ERROR 日志
        return ApiResponse(data={"backups": [], "enabled": False})
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
    user: dict = Depends(require_permission(Permission.SYSTEM_READ)),
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
    user: dict = Depends(require_permission(Permission.SYSTEM_MANAGE)),
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
                "in_progress": bool(getattr(mgr, "in_progress", False) or getattr(mgr, "_in_progress", False)),
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("OTA cancel failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None
