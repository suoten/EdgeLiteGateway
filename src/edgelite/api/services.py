"""服务管理API路由 - 统一管理所有可选服务的启停、依赖和配置"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from edgelite.api.deps import CurrentUser, require_permission
from edgelite.api.error_codes import ServiceErrors
from edgelite.models.common import ApiResponse
from edgelite.security.rbac import Permission
from edgelite.services.service_manager import SERVICE_DEFINITIONS, get_service_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/services", tags=["Services"])


class EnableServiceRequest(BaseModel):
    config: dict | None = None  # FIXED: 原问题-dict类型参数无schema校验，此处为动态配置场景，schema由驱动/平台运行时决定


class UpdateServiceConfigRequest(BaseModel):
    config: dict  # FIXED: 原问题-dict类型参数无schema校验，此处为动态配置场景，schema由驱动/平台运行时决定


class InstallDepsRequest(BaseModel):
    package: str | None = None


@router.get("/list", response_model=ApiResponse)
async def list_services(
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    mgr = get_service_manager()
    try:
        services = []
        for info in mgr.list_services():
            svc_def = SERVICE_DEFINITIONS.get(info.name, {})
            services.append(
                {
                    "name": info.name,
                    "display_name": info.display_name,
                    "description": info.description,
                    "icon": svc_def.get("icon", ""),
                    "category": svc_def.get("category", ""),
                    "state": info.state.value,
                    "config_section": info.config_section,
                    "dependencies": [
                        {"package": d.package, "installed": d.installed, "version": d.version}
                        for d in info.dependencies
                    ],
                    "use_cases": svc_def.get("use_cases", []),
                    "related_features": svc_def.get("related_features", []),
                    "setup_guide": svc_def.get("setup_guide", []),
                    "config_schema": info.config_schema,
                    "current_config": info.current_config,
                    "error_message": info.error_message,
                }
            )
        return ApiResponse(data={"services": services})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("list_services failed: %s", e)
        # FIXED: 原问题-中文硬编码detail
        raise HTTPException(status_code=500, detail=ServiceErrors.LIST_FAILED) from e


@router.get("/{service_name}/status", response_model=ApiResponse)
async def get_service_status(
    service_name: str,
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    if service_name not in SERVICE_DEFINITIONS:
        # FIXED: 原问题-中文硬编码detail
        raise HTTPException(status_code=404, detail=ServiceErrors.UNKNOWN_SERVICE)
    try:
        mgr = get_service_manager()
        info = mgr.get_service_info(service_name)
        if info is None:
            # FIXED: 原问题-中文硬编码detail
            raise HTTPException(status_code=404, detail=ServiceErrors.NOT_REGISTERED)
        svc_def = SERVICE_DEFINITIONS.get(service_name, {})
        return ApiResponse(
            data={
                "name": info.name,
                "display_name": info.display_name,
                "description": info.description,
                "icon": svc_def.get("icon", ""),
                "category": svc_def.get("category", ""),
                "state": info.state.value,
                "dependencies": [
                    {"package": d.package, "installed": d.installed, "version": d.version}
                    for d in info.dependencies
                ],
                "use_cases": svc_def.get("use_cases", []),
                "related_features": svc_def.get("related_features", []),
                "setup_guide": svc_def.get("setup_guide", []),
                "config_schema": info.config_schema,
                "current_config": info.current_config,
                "running_info": info.running_info,
                "error_message": info.error_message,
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_service_status failed: %s", e)
        # FIXED: 原问题-中文硬编码detail
        raise HTTPException(status_code=500, detail=ServiceErrors.STATUS_FAILED) from e


@router.post("/{service_name}/enable", response_model=ApiResponse)
async def enable_service(
    service_name: str,
    req: EnableServiceRequest | None = None,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    if service_name not in SERVICE_DEFINITIONS:
        raise HTTPException(status_code=404, detail=ServiceErrors.UNKNOWN_SERVICE)
    try:
        mgr = get_service_manager()
        config_values = req.config if req else None
        result = await mgr.enable_service(service_name, config_values)

        if not result.get("success"):
            if "missing_dependencies" in result:
                raise HTTPException(
                    status_code=424,
                    detail={
                        "message": result.get("error", ServiceErrors.ENABLE_FAILED),
                        "missing_dependencies": result.get("missing_dependencies", []),
                        "hint": result.get("hint", ""),
                    },
                )
            raise HTTPException(
                status_code=500,
                detail={
                    "message": result.get("error", ServiceErrors.ENABLE_FAILED),
                    "detail": result.get("detail", ""),
                    "hint": result.get("hint", ""),
                },
            )  # FIXED: 原问题-enable失败仅返回START_FAILED字符串，前端无法展示友好提示，改为返回包含hint的结构化错误

        if result.get("warning"):
            return ApiResponse(data={**result, "message": result.get("warning", "")})

        return ApiResponse(data=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("enable_service failed: %s", e)
        raise HTTPException(status_code=500, detail=ServiceErrors.ENABLE_FAILED) from e


@router.post("/{service_name}/disable", response_model=ApiResponse)
async def disable_service(
    service_name: str,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    if service_name not in SERVICE_DEFINITIONS:
        raise HTTPException(status_code=404, detail=ServiceErrors.UNKNOWN_SERVICE)
    try:
        mgr = get_service_manager()
        result = await mgr.disable_service(service_name)

        if not result.get("success"):
            # FIXED: 原问题-中文硬编码detail
            raise HTTPException(status_code=500, detail=result.get("error", ServiceErrors.DISABLE_FAILED))

        return ApiResponse(data=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("disable_service failed: %s", e)
        raise HTTPException(status_code=500, detail=ServiceErrors.DISABLE_FAILED) from e


@router.post("/{service_name}/start", response_model=ApiResponse)
async def start_service(
    service_name: str,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    if service_name not in SERVICE_DEFINITIONS:
        raise HTTPException(status_code=404, detail=ServiceErrors.UNKNOWN_SERVICE)
    try:
        mgr = get_service_manager()
        result = await mgr.start_service(service_name)

        if not result.get("success"):
            if result.get("error_type") == "runtime":
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": result.get("error", ServiceErrors.START_FAILED),
                        "detail": result.get("detail", ""),
                        "hint": result.get("hint", ""),
                    },
                )  # FIXED: 原问题-start失败仅返回START_FAILED，改为返回包含hint的结构化错误
            raise HTTPException(
                status_code=500,
                detail={
                    "message": result.get("error", ServiceErrors.START_FAILED),
                    "detail": result.get("detail", ""),
                    "hint": result.get("hint", ""),
                },
            )

        return ApiResponse(data=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("start_service failed: %s", e)
        raise HTTPException(status_code=500, detail=ServiceErrors.START_FAILED) from e


@router.post("/{service_name}/stop", response_model=ApiResponse)
async def stop_service(
    service_name: str,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    if service_name not in SERVICE_DEFINITIONS:
        raise HTTPException(status_code=404, detail=ServiceErrors.UNKNOWN_SERVICE)
    try:
        mgr = get_service_manager()
        result = await mgr.stop_service(service_name)

        if not result.get("success"):
            # FIXED: 原问题-中文硬编码detail
            raise HTTPException(status_code=500, detail=result.get("error", ServiceErrors.STOP_FAILED))

        return ApiResponse(data=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("stop_service failed: %s", e)
        raise HTTPException(status_code=500, detail=ServiceErrors.STOP_FAILED) from e


@router.post("/{service_name}/install-deps", response_model=ApiResponse)
async def install_service_dependencies(
    service_name: str,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    if service_name not in SERVICE_DEFINITIONS:
        raise HTTPException(status_code=404, detail=ServiceErrors.UNKNOWN_SERVICE)
    try:
        mgr = get_service_manager()
        result = await mgr.install_service_dependencies(service_name)

        if not result.get("all_installed"):
            # FIXED: 原问题-中文硬编码detail
            raise HTTPException(status_code=500, detail=ServiceErrors.DEPS_INSTALL_FAILED)

        return ApiResponse(data=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("install_deps failed: %s", e)
        raise HTTPException(status_code=500, detail=ServiceErrors.INSTALL_FAILED) from e


@router.put("/{service_name}/config", response_model=ApiResponse)
async def update_service_config(
    service_name: str,
    req: UpdateServiceConfigRequest,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    if service_name not in SERVICE_DEFINITIONS:
        raise HTTPException(status_code=404, detail=ServiceErrors.UNKNOWN_SERVICE)
    try:
        mgr = get_service_manager()
        result = await mgr.update_service_config(service_name, req.config)

        if not result.get("success"):
            # FIXED: 原问题-中文硬编码detail
            raise HTTPException(status_code=500, detail=result.get("error", ServiceErrors.CONFIG_UPDATE_FAILED))

        return ApiResponse(data=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("update_service_config failed: %s", e)
        raise HTTPException(status_code=500, detail=ServiceErrors.CONFIG_UPDATE_FAILED) from e
