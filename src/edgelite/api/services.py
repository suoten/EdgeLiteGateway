"""服务管理API路由 - 统一管理所有可选服务的启停、依赖和配置"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from edgelite.api.deps import CurrentUser, require_permission
from edgelite.models.common import ApiResponse
from edgelite.security.rbac import Permission
from edgelite.services.service_manager import SERVICE_DEFINITIONS, get_service_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/services", tags=["服务管理"])


class EnableServiceRequest(BaseModel):
    config: dict | None = None


class UpdateServiceConfigRequest(BaseModel):
    config: dict


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
        logger.error("获取列表失败: %s", e)
        raise HTTPException(status_code=500, detail="获取列表失败") from e


@router.get("/{service_name}/status", response_model=ApiResponse)
async def get_service_status(
    service_name: str,
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    if service_name not in SERVICE_DEFINITIONS:
        raise HTTPException(status_code=404, detail=f"未知服务: {service_name}")
    try:
        mgr = get_service_manager()
        info = mgr.get_service_info(service_name)
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
        logger.error("获取失败: %s", e)
        raise HTTPException(status_code=500, detail="获取失败") from e


@router.post("/{service_name}/enable", response_model=ApiResponse)
async def enable_service(
    service_name: str,
    req: EnableServiceRequest | None = None,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    if service_name not in SERVICE_DEFINITIONS:
        raise HTTPException(status_code=404, detail=f"未知服务: {service_name}")
    try:
        mgr = get_service_manager()
        config_values = req.config if req else None
        result = await mgr.enable_service(service_name, config_values)

        if not result.get("success"):
            if "missing_dependencies" in result:
                raise HTTPException(
                    status_code=424,
                    detail={
                        "message": result.get("error", "缺少依赖"),
                        "missing_dependencies": result.get("missing_dependencies", []),
                        "hint": result.get("hint", ""),
                    },
                )
            raise HTTPException(status_code=500, detail=result.get("error", "启用失败"))

        if result.get("warning"):
            return ApiResponse(data={**result, "message": result.get("warning", "")})

        return ApiResponse(data=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("启用失败: %s", e)
        raise HTTPException(status_code=500, detail="启用失败") from e


@router.post("/{service_name}/disable", response_model=ApiResponse)
async def disable_service(
    service_name: str,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    if service_name not in SERVICE_DEFINITIONS:
        raise HTTPException(status_code=404, detail=f"未知服务: {service_name}")
    try:
        mgr = get_service_manager()
        result = await mgr.disable_service(service_name)

        if not result.get("success"):
            raise HTTPException(status_code=500, detail=result.get("error", "停用失败"))

        return ApiResponse(data=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("禁用失败: %s", e)
        raise HTTPException(status_code=500, detail="禁用失败") from e


@router.post("/{service_name}/start", response_model=ApiResponse)
async def start_service(
    service_name: str,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    if service_name not in SERVICE_DEFINITIONS:
        raise HTTPException(status_code=404, detail=f"未知服务: {service_name}")
    try:
        mgr = get_service_manager()
        result = await mgr.start_service(service_name)

        if not result.get("success"):
            if result.get("error_type") == "runtime":
                raise HTTPException(status_code=409, detail=result.get("error", "启动失败"))
            raise HTTPException(status_code=500, detail=result.get("error", "启动失败"))

        return ApiResponse(data=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("启动失败: %s", e)
        raise HTTPException(status_code=500, detail="启动失败") from e


@router.post("/{service_name}/stop", response_model=ApiResponse)
async def stop_service(
    service_name: str,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    if service_name not in SERVICE_DEFINITIONS:
        raise HTTPException(status_code=404, detail=f"未知服务: {service_name}")
    try:
        mgr = get_service_manager()
        result = await mgr.stop_service(service_name)

        if not result.get("success"):
            raise HTTPException(status_code=500, detail=result.get("error", "停止失败"))

        return ApiResponse(data=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("停止失败: %s", e)
        raise HTTPException(status_code=500, detail="停止失败") from e


@router.post("/{service_name}/install-deps", response_model=ApiResponse)
async def install_service_dependencies(
    service_name: str,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    if service_name not in SERVICE_DEFINITIONS:
        raise HTTPException(status_code=404, detail=f"未知服务: {service_name}")
    try:
        mgr = get_service_manager()
        result = await mgr.install_service_dependencies(service_name)

        if not result.get("all_installed"):
            failed = [r for r in result.get("results", []) if not r.get("success")]
            raise HTTPException(
                status_code=500,
                detail=f"依赖安装失败: {', '.join(r.get('package', '未知') for r in failed)}",
            )

        return ApiResponse(data=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("安装失败: %s", e)
        raise HTTPException(status_code=500, detail="安装失败") from e


@router.put("/{service_name}/config", response_model=ApiResponse)
async def update_service_config(
    service_name: str,
    req: UpdateServiceConfigRequest,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    if service_name not in SERVICE_DEFINITIONS:
        raise HTTPException(status_code=404, detail=f"未知服务: {service_name}")
    try:
        mgr = get_service_manager()
        result = await mgr.update_service_config(service_name, req.config)

        if not result.get("success"):
            raise HTTPException(status_code=500, detail=result.get("error", "配置更新失败"))

        return ApiResponse(data=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("更新失败: %s", e)
        raise HTTPException(status_code=500, detail="更新失败") from e
