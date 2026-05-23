"""内置Modbus Slave管理API路由"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from edgelite.api.deps import CurrentUser, ModbusSlaveDep, require_permission
from edgelite.api.error_codes import ServiceErrors
from edgelite.models.common import ApiResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/modbus-slave", tags=["Modbus Slave"])


class ModbusSlaveConfigModel(BaseModel):
    host: str = "0.0.0.0"
    port: int = Field(default=502, ge=1, le=65535)
    holding_size: int = Field(default=1000, ge=1)
    input_size: int = Field(default=1000, ge=1)
    coil_size: int = Field(default=1000, ge=1)
    discrete_size: int = Field(default=1000, ge=1)


@router.get("/status", response_model=ApiResponse)
async def get_modbus_slave_status(
    _slave: ModbusSlaveDep,
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    from edgelite.services.service_manager import get_service_manager

    try:
        mgr = get_service_manager()
        info = mgr.get_service_info("modbus_slave")
        # FIXED: get_service_info()可能返回None导致500
        if info is None:
            raise HTTPException(status_code=404, detail=ServiceErrors.NOT_REGISTERED)

        # FIXED: 原问题-info.current_config可能为None时直接调用.get()崩溃
        _cfg = info.current_config or {}
        return ApiResponse(
            data={
                "enabled": info.state.value != "disabled",
                "running": info.state.value == "running",
                "state": info.state.value,
                "host": _cfg.get("host", "0.0.0.0"),
                "port": _cfg.get("port", 502),
                "holding_size": _cfg.get("holding_size", 100),
                "input_size": _cfg.get("input_size", 100),
                "dependencies": [
                    {"package": d.package, "installed": d.installed, "version": d.version}
                    for d in info.dependencies
                ],
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Get modbus slave status failed: %s", e)  # FIXED-P3: 中文日志→英文
        raise HTTPException(status_code=500, detail=ServiceErrors.STATUS_FAILED) from e


@router.post("/start", response_model=ApiResponse)
async def start_modbus_slave(
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    from edgelite.services.service_manager import get_service_manager

    try:
        mgr = get_service_manager()
        result = await mgr.start_service("modbus_slave")
        if not result.get("success"):
            # FIXED: 原问题-中文硬编码detail
            raise HTTPException(status_code=500, detail=result.get("error", ServiceErrors.START_FAILED))
        return ApiResponse(data=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("start_modbus_slave failed: %s", e)
        raise HTTPException(status_code=500, detail=ServiceErrors.START_FAILED) from e


@router.post("/stop", response_model=ApiResponse)
async def stop_modbus_slave(
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    from edgelite.services.service_manager import get_service_manager

    try:
        mgr = get_service_manager()
        result = await mgr.stop_service("modbus_slave")
        if not result.get("success"):
            # FIXED: 原问题-中文硬编码detail
            raise HTTPException(status_code=500, detail=result.get("error", ServiceErrors.STOP_FAILED))
        return ApiResponse(data=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("stop_modbus_slave failed: %s", e)
        raise HTTPException(status_code=500, detail=ServiceErrors.STOP_FAILED) from e


@router.put("/config", response_model=ApiResponse)
async def update_modbus_slave_config(
    config: ModbusSlaveConfigModel,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    from edgelite.services.service_manager import get_service_manager

    try:
        mgr = get_service_manager()
        result = await mgr.update_service_config("modbus_slave", config.model_dump())
        if not result.get("success"):
            # FIXED: 原问题-中文硬编码detail
            raise HTTPException(status_code=500, detail=result.get("error", ServiceErrors.CONFIG_UPDATE_FAILED))
        return ApiResponse(data=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("update_modbus_slave_config failed: %s", e)
        raise HTTPException(status_code=500, detail=ServiceErrors.CONFIG_UPDATE_FAILED) from e
