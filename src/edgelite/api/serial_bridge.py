"""串口TCP透传桥接API路由"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from edgelite.models.common import ApiResponse
from edgelite.api.deps import CurrentUser, require_permission
from edgelite.security.rbac import Permission

router = APIRouter(prefix="/api/v1/serial-bridge", tags=["串口透传"])


def _get_serial_bridge():
    try:
        from edgelite.app import _app_state
        return getattr(_app_state, "serial_bridge", None)
    except Exception:
        return None


@router.get("/status", response_model=ApiResponse)
async def get_serial_bridge_status(
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    from edgelite.services.service_manager import get_service_manager
    mgr = get_service_manager()
    info = mgr.get_service_info("serial_bridge")

    bridge = _get_serial_bridge()
    if bridge and hasattr(bridge, "get_status"):
        stats = bridge.get_status()
        return ApiResponse(data={
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
        })

    return ApiResponse(data={
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
    })


@router.post("/start", response_model=ApiResponse)
async def start_serial_bridge(
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    from edgelite.services.service_manager import get_service_manager
    mgr = get_service_manager()
    result = await mgr.start_service("serial_bridge")
    if not result.get("success"):
        raise HTTPException(status_code=500, detail=result.get("error", "启动失败"))

    return ApiResponse(data=result)


@router.post("/stop", response_model=ApiResponse)
async def stop_serial_bridge(
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    from edgelite.services.service_manager import get_service_manager
    mgr = get_service_manager()
    result = await mgr.stop_service("serial_bridge")
    if not result.get("success"):
        raise HTTPException(status_code=500, detail=result.get("error", "停止失败"))

    return ApiResponse(data=result)
