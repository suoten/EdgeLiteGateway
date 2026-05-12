"""串口TCP透传桥接API路由"""

from __future__ import annotations

import logging
import platform

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from edgelite.api.deps import CurrentUser, SerialBridgeDep, require_permission
from edgelite.models.common import ApiResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

_DEFAULT_SERIAL_PORT = "COM1" if platform.system() == "Windows" else "/dev/ttyUSB0"

router = APIRouter(prefix="/api/v1/serial-bridge", tags=["串口透传"])


class DependencyInfo(BaseModel):
    package: str
    installed: bool
    version: str = ""


class SerialBridgeStatusResponse(BaseModel):
    enabled: bool = False
    running: bool = False
    state: str = "disabled"
    serial_port: str = _DEFAULT_SERIAL_PORT
    baud_rate: int = 9600
    tcp_port: int = 9000
    serial_rx_bytes: int = 0
    serial_tx_bytes: int = 0
    tcp_rx_bytes: int = 0
    tcp_tx_bytes: int = 0
    client_count: int = 0
    total_connections: int = 0
    start_time: str | None = None
    dependencies: list[DependencyInfo] = []


@router.get("/status", response_model=ApiResponse[SerialBridgeStatusResponse])
async def get_serial_bridge_status(
    bridge: SerialBridgeDep,
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    from edgelite.services.service_manager import get_service_manager

    try:
        mgr = get_service_manager()
        info = mgr.get_service_info("serial_bridge")
        # FIXED: get_service_info()可能返回None导致500
        if info is None:
            raise HTTPException(status_code=404, detail="串口桥接服务未注册")

        if bridge and hasattr(bridge, "get_status"):
            stats = bridge.get_status()
            return ApiResponse(
                data={
                    "enabled": info.state.value != "disabled",
                    "running": stats.running,
                    "state": info.state.value if not stats.running else "running",
                    "serial_port": info.current_config.get("serial_port", "/dev/ttyUSB0"),
                    "baud_rate": info.current_config.get("baud_rate", 9600),
                    "tcp_port": info.current_config.get("tcp_port", 9000),
                    "serial_rx_bytes": stats.serial_rx_bytes,
                    "serial_tx_bytes": stats.serial_tx_bytes,
                    "tcp_rx_bytes": stats.tcp_rx_bytes,
                    "tcp_tx_bytes": stats.tcp_tx_bytes,
                    "client_count": stats.client_count,
                    "total_connections": stats.total_connections,
                    "start_time": stats.start_time,
                    "dependencies": [
                        {"package": d.package, "installed": d.installed, "version": d.version}
                        for d in info.dependencies
                    ],
                }
            )

        return ApiResponse(
            data={
                "enabled": info.state.value != "disabled",
                "running": False,
                "state": info.state.value,
                "serial_port": info.current_config.get("serial_port", "/dev/ttyUSB0"),
                "baud_rate": info.current_config.get("baud_rate", 9600),
                "tcp_port": info.current_config.get("tcp_port", 9000),
                "dependencies": [
                    {"package": d.package, "installed": d.installed, "version": d.version}
                    for d in info.dependencies
                ],
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("获取失败: %s", e)
        raise HTTPException(status_code=500, detail="获取失败") from e


@router.post("/start", response_model=ApiResponse)
async def start_serial_bridge(
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    from edgelite.services.service_manager import get_service_manager

    try:
        mgr = get_service_manager()
        result = await mgr.start_service("serial_bridge")
        if not result.get("success"):
            error_msg = result.get("error", "启动失败")
            if "error_type" in result and result["error_type"] == "runtime":
                raise HTTPException(status_code=409, detail=error_msg)
            raise HTTPException(status_code=500, detail=error_msg)

        return ApiResponse(data=result)
    except HTTPException:
        raise
    except Exception as e:
        error_msg = str(e)
        if "could not open port" in error_msg or "No such file" in error_msg:
            raise HTTPException(
                status_code=409,
                detail="串口设备不存在或无法访问，请检查串口配置和设备连接",
            ) from e
        logger.error("启动失败: %s", e)
        raise HTTPException(status_code=500, detail="启动失败") from e


@router.post("/stop", response_model=ApiResponse)
async def stop_serial_bridge(
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    from edgelite.services.service_manager import get_service_manager

    try:
        mgr = get_service_manager()
        result = await mgr.stop_service("serial_bridge")
        if not result.get("success"):
            raise HTTPException(status_code=500, detail=result.get("error", "停止失败"))
        return ApiResponse(data=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("停止失败: %s", e)
        raise HTTPException(status_code=500, detail="停止失败") from e
