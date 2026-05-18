"""平台配置管理API路由"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from edgelite.api.deps import CurrentUser, PlatformHandlersDep, require_permission
from edgelite.api.error_codes import PlatformErrors
from edgelite.models.common import ApiResponse
from edgelite.security.rbac import Permission
from edgelite.services.platform_service import PlatformService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/platforms", tags=["Platforms"])


class PlatformConnectRequest(BaseModel):
    config: dict


def _get_service(handlers: dict) -> PlatformService:
    return PlatformService(handlers)


@router.get("/list", response_model=ApiResponse)
async def list_platforms(
    handlers: PlatformHandlersDep,
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    try:
        svc = _get_service(handlers)
        platforms = svc.list_platforms()
        supported = svc.list_supported()
        return ApiResponse(data={"platforms": platforms, "supported": supported})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to list platforms: %s", e)  # FIXED: 原问题-中文硬编码日志
        raise HTTPException(status_code=500, detail=PlatformErrors.CONNECT_FAILED) from e  # FIXED: 原问题-中文硬编码detail，改为error_code


@router.get("/config-schema/{platform_name}", response_model=ApiResponse)
async def get_platform_config_schema(
    platform_name: str,
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    try:
        svc = PlatformService()
        schema = svc.get_config_schema(platform_name)
        if not schema:
            raise HTTPException(status_code=404, detail=PlatformErrors.CONFIG_SCHEMA_NOT_FOUND)  # FIXED: 原问题-中文硬编码detail，改为error_code
        return ApiResponse(data={"platform_name": platform_name, "config_schema": schema})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to get config schema: %s", e)  # FIXED: 原问题-中文硬编码日志
        raise HTTPException(status_code=500, detail=PlatformErrors.CONNECT_FAILED) from e  # FIXED: 原问题-中文硬编码detail，改为error_code


@router.post("/connect/{platform_name}", response_model=ApiResponse)
async def connect_platform(
    platform_name: str,
    req: PlatformConnectRequest,
    handlers: PlatformHandlersDep,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    if not req.config:
        raise HTTPException(status_code=400, detail=PlatformErrors.MISSING_CONFIG)  # FIXED: 原问题-中文硬编码detail，改为error_code

    try:
        svc = _get_service(handlers)
        result = await svc.connect(platform_name, req.config)
        return ApiResponse(data=result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=PlatformErrors.CONNECT_FAILED) from e  # FIXED: 原问题-中文硬编码detail，改为error_code


@router.post("/disconnect/{platform_name}", response_model=ApiResponse)
async def disconnect_platform(
    platform_name: str,
    handlers: PlatformHandlersDep,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    try:
        svc = _get_service(handlers)
        result = await svc.disconnect(platform_name)
        return ApiResponse(data=result)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=PlatformErrors.DISCONNECT_FAILED) from e  # FIXED: 原问题-中文硬编码detail，改为error_code


@router.get("/status/{platform_name}", response_model=ApiResponse)
async def get_platform_status(
    platform_name: str,
    handlers: PlatformHandlersDep,
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    try:
        svc = _get_service(handlers)
        status = svc.get_status(platform_name)
        return ApiResponse(data=status)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to get platform status: %s", e)  # FIXED: 原问题-中文硬编码日志
        raise HTTPException(status_code=500, detail=PlatformErrors.CONNECT_FAILED) from e  # FIXED: 原问题-中文硬编码detail，改为error_code
