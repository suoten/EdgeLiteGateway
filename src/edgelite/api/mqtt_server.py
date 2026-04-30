"""内置MQTT Server管理API路由"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from edgelite.models.common import ApiResponse
from edgelite.api.deps import CurrentUser, require_permission
from edgelite.security.rbac import Permission

router = APIRouter(prefix="/api/v1/mqtt-server", tags=["MQTT Server"])


class MqttServerConfigModel(BaseModel):
    host: str = "0.0.0.0"
    port: int = 1883
    ws_port: int = 8083
    username: str = ""
    password: str = ""


def _get_mqtt_server():
    try:
        from edgelite.app import _app_state
        return getattr(_app_state, "mqtt_server", None)
    except Exception:
        return None


@router.get("/status", response_model=ApiResponse)
async def get_mqtt_server_status(
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    srv = _get_mqtt_server()
    if not srv:
        raise HTTPException(status_code=501, detail="MQTT Server服务未启用")
    return ApiResponse(data={
        "running": srv.is_running if hasattr(srv, "is_running") else False,
        "host": getattr(srv, "_host", "0.0.0.0"),
        "port": getattr(srv, "_port", 1883),
        "ws_port": getattr(srv, "_ws_port", 8083),
        "connections": getattr(srv, "_connections", 0),
    })


@router.post("/start", response_model=ApiResponse)
async def start_mqtt_server(
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    srv = _get_mqtt_server()
    if not srv:
        raise HTTPException(status_code=501, detail="MQTT Server服务未启用")
    try:
        if hasattr(srv, "is_running") and srv.is_running:
            return ApiResponse(data={"status": "already_running"})
        await srv.start()
        return ApiResponse(data={"status": "started"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/stop", response_model=ApiResponse)
async def stop_mqtt_server(
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    srv = _get_mqtt_server()
    if not srv:
        raise HTTPException(status_code=501, detail="MQTT Server服务未启用")
    try:
        await srv.stop()
        return ApiResponse(data={"status": "stopped"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/config", response_model=ApiResponse)
async def update_mqtt_server_config(
    config: MqttServerConfigModel,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    srv = _get_mqtt_server()
    if not srv:
        raise HTTPException(status_code=501, detail="MQTT Server服务未启用")
    try:
        if hasattr(srv, "update_config"):
            await srv.update_config(config.model_dump())
        return ApiResponse(data={"status": "config_updated"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
