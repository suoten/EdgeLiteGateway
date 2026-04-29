"""串口TCP透传桥接API路由"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from edgelite.models.common import ApiResponse
from edgelite.api.deps import CurrentUser, require_permission
from edgelite.security.rbac import Permission

router = APIRouter(prefix="/api/v1/serial-bridge", tags=["串口透传"])


_serial_bridge = None


def _get_serial_bridge():
    global _serial_bridge
    return _serial_bridge


@router.get("/status", response_model=ApiResponse)
async def get_serial_bridge_status(
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    bridge = _get_serial_bridge()
    if bridge and hasattr(bridge, "get_status"):
        stats = bridge.get_status()
        return ApiResponse(data={
            "running": stats.running,
            "serial_rx_bytes": stats.serial_rx_bytes,
            "serial_tx_bytes": stats.serial_tx_bytes,
            "tcp_rx_bytes": stats.tcp_rx_bytes,
            "tcp_tx_bytes": stats.tcp_tx_bytes,
            "client_count": stats.client_count,
            "total_connections": stats.total_connections,
            "start_time": stats.start_time,
        })

    from edgelite.config import get_config
    config = get_config()
    sb_config = getattr(config, "serial_bridge", None)

    return ApiResponse(data={
        "running": False,
        "enabled": getattr(sb_config, "enabled", False) if sb_config else False,
        "serial_port": getattr(sb_config, "serial_port", "/dev/ttyUSB0") if sb_config else "/dev/ttyUSB0",
        "baud_rate": getattr(sb_config, "baud_rate", 9600) if sb_config else 9600,
        "tcp_port": getattr(sb_config, "tcp_port", 9000) if sb_config else 9000,
    })


@router.post("/start", response_model=ApiResponse)
async def start_serial_bridge(
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    global _serial_bridge
    from edgelite.config import get_config
    config = get_config()
    sb_config = getattr(config, "serial_bridge", None)

    if not sb_config or not getattr(sb_config, "enabled", False):
        raise HTTPException(status_code=400, detail="串口透传未在配置中启用")

    if _serial_bridge and getattr(_serial_bridge, "_running", False):
        return ApiResponse(data={"status": "already_running"})

    try:
        from edgelite.engine.serial_bridge import SerialTcpBridge
        _serial_bridge = SerialTcpBridge()
        await _serial_bridge.start({
            "serial_port": getattr(sb_config, "serial_port", "/dev/ttyUSB0"),
            "baudrate": getattr(sb_config, "baud_rate", 9600),
            "tcp_port": getattr(sb_config, "tcp_port", 9000),
            "allowed_ips": getattr(sb_config, "ip_whitelist", []),
        })
        return ApiResponse(data={"status": "started"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"启动失败: {e}")


@router.post("/stop", response_model=ApiResponse)
async def stop_serial_bridge(
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    global _serial_bridge
    if not _serial_bridge or not getattr(_serial_bridge, "_running", False):
        return ApiResponse(data={"status": "not_running"})

    try:
        await _serial_bridge.stop()
        _serial_bridge = None
        return ApiResponse(data={"status": "stopped"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"停止失败: {e}")
