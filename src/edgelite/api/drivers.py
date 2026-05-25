"""驱动配置管理API路由"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from edgelite.api.deps import (
    CurrentUser,
    DriverRegistryDep,
    PluginManagerDep,
    require_permission,
)
from edgelite.api.error_codes import DriverErrors
from edgelite.models.common import ApiResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/drivers", tags=["Drivers"])


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
            raise HTTPException(status_code=501, detail=DriverErrors.REGISTRY_NOT_INIT)  # FIXED: 原问题-中文硬编码detail，改为error_code

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
        logger.error("Failed to list drivers: %s", e)  # FIXED: 原问题-中文硬编码日志
        raise HTTPException(status_code=500, detail=DriverErrors.LIST_FAILED) from e  # FIXED: 原问题-中文硬编码detail，改为error_code


@router.get("/protocols", response_model=ApiResponse[DriverProtocolsResponse])
async def list_protocols(
    registry: DriverRegistryDep,
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    try:
        if not registry:
            raise HTTPException(status_code=501, detail=DriverErrors.REGISTRY_NOT_INIT)  # FIXED: 原问题-中文硬编码detail，改为error_code

        protocols = registry.get_supported_protocols()
        return ApiResponse(data={"protocols": protocols})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to list protocols: %s", e)  # FIXED: 原问题-中文硬编码日志
        raise HTTPException(status_code=500, detail=DriverErrors.LIST_FAILED) from e  # FIXED: 原问题-中文硬编码detail，改为error_code


@router.get("/{driver_name}/config-schema", response_model=ApiResponse[DriverConfigSchemaResponse])
async def get_driver_config_schema(
    driver_name: str,
    registry: DriverRegistryDep,
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    try:
        if not registry:
            raise HTTPException(status_code=501, detail=DriverErrors.REGISTRY_NOT_INIT)  # FIXED: 原问题-中文硬编码detail，改为error_code

        driver_cls = registry.get_driver_class(driver_name)
        if not driver_cls:
            raise HTTPException(status_code=404, detail=DriverErrors.NOT_FOUND)  # FIXED: 原问题-中文硬编码detail，改为error_code

        schema = getattr(driver_cls, "config_schema", None)
        if not schema:
            schema = {
                "fields": [
                    {"name": "host", "type": "string", "label": "Host", "default": "localhost", "required": True},
                    {"name": "port", "type": "integer", "label": "Port", "default": 0},
                ]
            }

        return ApiResponse(data={"driver_name": driver_name, "config_schema": schema})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to get driver config schema: %s", e)  # FIXED: 原问题-中文硬编码日志
        raise HTTPException(status_code=500, detail=DriverErrors.GET_FAILED) from e  # FIXED: 原问题-中文硬编码detail，改为error_code


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
        logger.error("Failed to list all drivers: %s", e)  # FIXED: 原问题-中文硬编码日志
        raise HTTPException(status_code=500, detail=DriverErrors.LIST_FAILED) from e  # FIXED: 原问题-中文硬编码detail，改为error_code


class DriverDiscoverRequest(BaseModel):
    config: dict = {}


_DISCOVER_TIMEOUT = 60.0


@router.post("/{driver_name}/discover", response_model=ApiResponse[DriverDiscoverResponse])
async def discover_devices(
    driver_name: str,
    registry: DriverRegistryDep,
    req: DriverDiscoverRequest = None,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    if not registry:
        raise HTTPException(status_code=501, detail=DriverErrors.REGISTRY_NOT_INIT)

    driver_cls = registry.get_driver_class(driver_name)
    if not driver_cls:
        raise HTTPException(status_code=404, detail=DriverErrors.NOT_FOUND)

    try:
        driver = driver_cls()
        driver_config = req.config if req else {}
        await driver.start(driver_config)
    except Exception as e:
        logger.error("Driver %s start failed for discover: %s", driver_name, e)
        raise HTTPException(
            status_code=503,
            detail={"message": DriverErrors.START_FAILED, "detail": str(e), "hint": f"Driver {driver_name} failed to start, check configuration and connectivity"},
        ) from e  # FIXED: 原问题-驱动启动失败直接抛500，改为503+友好提示

    try:
        devices = await asyncio.wait_for(
            driver.discover_devices(driver_config),
            timeout=_DISCOVER_TIMEOUT,
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail={"message": DriverErrors.DISCOVER_FAILED, "hint": f"Discovery timed out ({_DISCOVER_TIMEOUT}s), target may be unreachable or no devices responded"},
        ) from None  # FIXED: 原问题-驱动扫描无超时保护，前端15s超时后看到网络错误而非业务提示
    except NotImplementedError:
        raise HTTPException(
            status_code=501,
            detail={"message": DriverErrors.DISCOVER_FAILED, "hint": f"Driver {driver_name} does not support device discovery"},
        ) from None  # FIXED: 原问题-驱动不支持discover时抛500，改为501+明确提示
    except ConnectionRefusedError as e:
        raise HTTPException(
            status_code=503,
            detail={"message": DriverErrors.DISCOVER_FAILED, "hint": f"Connection refused, target {driver_name} service may not be running"},
        ) from e  # FIXED: 原问题-连接被拒时返回500，改为503+友好提示
    except OSError as e:
        raise HTTPException(
            status_code=503,
            detail={"message": DriverErrors.DISCOVER_FAILED, "hint": f"Network error: {e}, check target address and connectivity"},
        ) from e  # FIXED: 原问题-网络错误返回500，改为503+友好提示
    except Exception as e:
        err_msg = str(e).lower()
        if "timeout" in err_msg or "timed out" in err_msg:
            raise HTTPException(
                status_code=504,
                detail={"message": DriverErrors.DISCOVER_FAILED, "hint": f"Discovery timed out, target may be unreachable"},
            ) from e
        if "refused" in err_msg or "connection" in err_msg:
            raise HTTPException(
                status_code=503,
                detail={"message": DriverErrors.DISCOVER_FAILED, "hint": f"Cannot connect to target, check if {driver_name} service is running and reachable"},
            ) from e
        raise HTTPException(
            status_code=500,
            detail={"message": DriverErrors.DISCOVER_FAILED, "detail": str(e)},
        ) from e
    finally:
        try:
            await driver.stop()
        except Exception as e:
            logger.warning("Failed to stop driver: %s", e)

    return ApiResponse(data={"devices": devices})
