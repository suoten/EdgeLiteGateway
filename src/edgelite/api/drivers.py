"""驱动配置管理API路由"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from edgelite.api.deps import (
    CurrentUser,
    DriverRegistryDep,
    PluginManagerDep,
    require_permission,
)
from edgelite.models.common import ApiResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/drivers", tags=["驱动配置"])


class DriverInfo(BaseModel):
    name: str
    version: str = "1.0.0"
    protocols: list[str] = []
    description: str = ""


class DriverListResponse(BaseModel):
    drivers: list[DriverInfo]
    total: int


class DriverProtocolsResponse(BaseModel):
    protocols: list[str]


class DriverConfigSchemaResponse(BaseModel):
    driver_name: str
    config_schema: dict


class DriverStatusInfo(BaseModel):
    name: str
    class_: str = Field(alias="class")
    module: str
    custom: bool | None = None
    loaded: bool | None = None
    error: str | None = None

    model_config = {"populate_by_name": True}


class DriverDiscoverResponse(BaseModel):
    devices: list[dict]


@router.get("/list", response_model=ApiResponse[DriverListResponse])
async def list_drivers(
    registry: DriverRegistryDep,
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    try:
        if not registry:
            raise HTTPException(status_code=501, detail="驱动注册表未初始化")

        drivers = []
        for name, driver_cls in registry.items():
            instance = driver_cls() if isinstance(driver_cls, type) else driver_cls
            drivers.append(
                {
                    "name": getattr(instance, "plugin_name", name),
                    "version": getattr(instance, "plugin_version", "1.0.0"),
                    "protocols": getattr(instance, "supported_protocols", []),
                    "description": getattr(instance, "__doc__", "") or "",
                }
            )

        return ApiResponse(data={"drivers": drivers, "total": len(drivers)})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("获取列表失败: %s", e)
        raise HTTPException(status_code=500, detail="获取列表失败") from e


@router.get("/protocols", response_model=ApiResponse[DriverProtocolsResponse])
async def list_protocols(
    registry: DriverRegistryDep,
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    try:
        if not registry:
            raise HTTPException(status_code=501, detail="驱动注册表未初始化")

        protocols = registry.get_supported_protocols()
        return ApiResponse(data={"protocols": protocols})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("获取列表失败: %s", e)
        raise HTTPException(status_code=500, detail="获取列表失败") from e


@router.get("/{driver_name}/config-schema", response_model=ApiResponse[DriverConfigSchemaResponse])
async def get_driver_config_schema(
    driver_name: str,
    registry: DriverRegistryDep,
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    try:
        if not registry:
            raise HTTPException(status_code=501, detail="驱动注册表未初始化")

        driver_cls = registry.get_driver_class(driver_name)
        if not driver_cls:
            raise HTTPException(status_code=404, detail=f"驱动 {driver_name} 不存在")

        schema = getattr(driver_cls, "config_schema", None)
        if not schema:
            schema = {
                "fields": [
                    {"name": "host", "type": "string", "label": "主机地址", "default": "localhost", "required": True},
                    {"name": "port", "type": "integer", "label": "端口", "default": 0},
                ]
            }

        return ApiResponse(data={"driver_name": driver_name, "config_schema": schema})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("获取失败: %s", e)
        raise HTTPException(status_code=500, detail="获取失败") from e


@router.get("", response_model=ApiResponse[list[DriverStatusInfo]])
async def list_all_drivers(
    registry: DriverRegistryDep,
    plugin_manager: PluginManagerDep,
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    try:
        drivers_info = []
        if registry:
            for name, cls in registry.items():
                drivers_info.append(
                    {
                        "name": name,
                        "class": cls.__name__,
                        "module": cls.__module__,
                    }
                )
        if plugin_manager:
            for info in plugin_manager.list_plugins():
                drivers_info.append(
                    {
                        "name": info.name,
                        "class": info.class_name,
                        "module": info.module_path,
                        "custom": info.is_custom,
                        "loaded": info.is_loaded,
                        "error": info.error,
                    }
                )
        return ApiResponse(data=drivers_info)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("获取列表失败: %s", e)
        raise HTTPException(status_code=500, detail="获取列表失败") from e


class DriverDiscoverRequest(BaseModel):
    config: dict = {}


@router.post("/{driver_name}/discover", response_model=ApiResponse[DriverDiscoverResponse])
async def discover_devices(
    driver_name: str,
    registry: DriverRegistryDep,
    req: DriverDiscoverRequest = None,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    if not registry:
        raise HTTPException(status_code=501, detail="驱动注册表未初始化")

    driver_cls = registry.get_driver_class(driver_name)
    if not driver_cls:
        raise HTTPException(status_code=404, detail=f"驱动 {driver_name} 不存在")

    try:
        driver = driver_cls()
        driver_config = req.config if req else {}
        await driver.start(driver_config)
        devices = await driver.discover_devices(driver_config)
        try:
            await driver.stop()
        except Exception as e:
            logger.warning("驱动停止失败: %s", e)
        return ApiResponse(data={"devices": devices})
    except Exception as e:
        raise HTTPException(status_code=500, detail="设备发现失败") from e
