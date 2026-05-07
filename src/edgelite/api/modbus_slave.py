"""内置Modbus Slave管理API路由"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from edgelite.models.common import ApiResponse
from edgelite.api.deps import CurrentUser, require_permission
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/modbus-slave", tags=["Modbus Slave"])


class ModbusSlaveConfigModel(BaseModel):
    host: str = "0.0.0.0"
    port: int = Field(default=502, ge=1, le=65535)
    holding_size: int = Field(default=100, ge=1)
    input_size: int = Field(default=100, ge=1)
    coil_size: int = Field(default=100, ge=1)
    discrete_size: int = Field(default=100, ge=1)


def _get_modbus_slave():
    try:
        from edgelite.app import _app_state
        return getattr(_app_state, "modbus_slave", None)
    except (ImportError, AttributeError) as e:
        logger.debug("Modbus Slave服务未加载: %s", e)
        return None
    except Exception as e:
        logger.warning("获取Modbus Slave服务异常: %s", e)
        return None


@router.get("/status", response_model=ApiResponse)
async def get_modbus_slave_status(
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    from edgelite.services.service_manager import get_service_manager
    mgr = get_service_manager()
    info = mgr.get_service_info("modbus_slave")

    return ApiResponse(data={
        "enabled": info.state.value != "disabled",
        "running": info.state.value == "running",
        "state": info.state.value,
        "host": info.current_config.get("host", "0.0.0.0"),
        "port": info.current_config.get("port", 502),
        "holding_size": info.current_config.get("holding_size", 100),
        "input_size": info.current_config.get("input_size", 100),
        "dependencies": [
            {"package": d.package, "installed": d.installed, "version": d.version}
            for d in info.dependencies
        ],
    })


@router.post("/start", response_model=ApiResponse)
async def start_modbus_slave(
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    from edgelite.services.service_manager import get_service_manager
    mgr = get_service_manager()
    result = await mgr.start_service("modbus_slave")
    if not result.get("success"):
        raise HTTPException(status_code=500, detail=result.get("error", "启动失败"))
    return ApiResponse(data=result)


@router.post("/stop", response_model=ApiResponse)
async def stop_modbus_slave(
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    from edgelite.services.service_manager import get_service_manager
    mgr = get_service_manager()
    result = await mgr.stop_service("modbus_slave")
    if not result.get("success"):
        raise HTTPException(status_code=500, detail=result.get("error", "停止失败"))
    return ApiResponse(data=result)


@router.put("/config", response_model=ApiResponse)
async def update_modbus_slave_config(
    config: ModbusSlaveConfigModel,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    from edgelite.services.service_manager import get_service_manager
    mgr = get_service_manager()
    result = await mgr.update_service_config("modbus_slave", config.model_dump())
    if not result.get("success"):
        raise HTTPException(status_code=500, detail=result.get("error", "配置更新失败"))
    return ApiResponse(data=result)
