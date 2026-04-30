"""内置Modbus Slave管理API路由"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from edgelite.models.common import ApiResponse
from edgelite.api.deps import CurrentUser, require_permission
from edgelite.security.rbac import Permission

router = APIRouter(prefix="/api/v1/modbus-slave", tags=["Modbus Slave"])


class ModbusSlaveConfigModel(BaseModel):
    host: str = "0.0.0.0"
    port: int = 502
    holding_size: int = 100
    input_size: int = 100
    coil_size: int = 100
    discrete_size: int = 100


def _get_modbus_slave():
    try:
        from edgelite.app import _app_state
        return getattr(_app_state, "modbus_slave", None)
    except Exception:
        return None


@router.get("/status", response_model=ApiResponse)
async def get_modbus_slave_status(
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    srv = _get_modbus_slave()
    if not srv:
        raise HTTPException(status_code=501, detail="Modbus Slave服务未启用")
    return ApiResponse(data={
        "running": srv.is_running if hasattr(srv, "is_running") else False,
        "host": getattr(srv, "_host", "0.0.0.0"),
        "port": getattr(srv, "_port", 502),
        "holding_size": getattr(srv, "_holding_size", 100),
        "input_size": getattr(srv, "_input_size", 100),
    })


@router.post("/start", response_model=ApiResponse)
async def start_modbus_slave(
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    srv = _get_modbus_slave()
    if not srv:
        raise HTTPException(status_code=501, detail="Modbus Slave服务未启用")
    try:
        if hasattr(srv, "is_running") and srv.is_running:
            return ApiResponse(data={"status": "already_running"})
        await srv.start()
        return ApiResponse(data={"status": "started"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/stop", response_model=ApiResponse)
async def stop_modbus_slave(
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    srv = _get_modbus_slave()
    if not srv:
        raise HTTPException(status_code=501, detail="Modbus Slave服务未启用")
    try:
        await srv.stop()
        return ApiResponse(data={"status": "stopped"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/config", response_model=ApiResponse)
async def update_modbus_slave_config(
    config: ModbusSlaveConfigModel,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    srv = _get_modbus_slave()
    if not srv:
        raise HTTPException(status_code=501, detail="Modbus Slave服务未启用")
    try:
        if hasattr(srv, "update_config"):
            await srv.update_config(config.model_dump())
        return ApiResponse(data={"status": "config_updated"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
