"""平台配置管理API路由"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from edgelite.api.deps import CurrentUser, PlatformHandlersDep, require_permission
from edgelite.models.common import ApiResponse
from edgelite.security.rbac import Permission
from edgelite.services.platform_service import PlatformService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/platforms", tags=["平台配置"])


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
        logger.error("获取列表失败: %s", e)
        raise HTTPException(status_code=500, detail="获取列表失败") from e


@router.get("/config-schema/{platform_name}", response_model=ApiResponse)
async def get_platform_config_schema(
    platform_name: str,
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    try:
        svc = PlatformService()
        schema = svc.get_config_schema(platform_name)
        if not schema:
            raise HTTPException(status_code=404, detail=f"平台 {platform_name} 配置模板不存在")
        return ApiResponse(data={"platform_name": platform_name, "config_schema": schema})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("获取失败: %s", e)
        raise HTTPException(status_code=500, detail="获取失败") from e


@router.post("/connect/{platform_name}", response_model=ApiResponse)
async def connect_platform(
    platform_name: str,
    req: PlatformConnectRequest,
    handlers: PlatformHandlersDep,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    if not req.config:
        raise HTTPException(status_code=400, detail="平台配置不能为空")

    try:
        svc = _get_service(handlers)
        result = await svc.connect(platform_name, req.config)
        return ApiResponse(data=result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail="平台连接失败") from e


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
        raise HTTPException(status_code=500, detail="断开失败") from e


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
        logger.error("获取失败: %s", e)
        raise HTTPException(status_code=500, detail="获取失败") from e
