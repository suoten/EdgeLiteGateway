"""内置MQTT Server管理API路由"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from edgelite.api.deps import CurrentUser, require_permission
from edgelite.models.common import ApiResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/mqtt-server", tags=["MQTT Server"])


class MqttServerConfigModel(BaseModel):
    host: str = "0.0.0.0"
    port: int = Field(default=1883, ge=1, le=65535)
    ws_port: int = Field(default=8083, ge=1, le=65535)
    username: str = ""
    password: str = ""


def _get_mqtt_server():
    try:
        from edgelite.app import _app_state

        return getattr(_app_state, "mqtt_server", None)
    except (ImportError, AttributeError) as e:
        logger.debug("MQTT Server服务未加载: %s", e)
        return None
    except Exception as e:
        logger.warning("获取MQTT Server服务异常: %s", e)
        return None


@router.get("/status", response_model=ApiResponse)
async def get_mqtt_server_status(
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    from edgelite.services.service_manager import get_service_manager

    try:
        mgr = get_service_manager()
        info = mgr.get_service_info("mqtt_server")

        mqtt_server = _get_mqtt_server()
        connections = 0
        if mqtt_server and hasattr(mqtt_server, "get_client_count"):
            try:
                connections = mqtt_server.get_client_count()
            except Exception as e:
                logger.warning("获取MQTT客户端数量失败: %s", e)

        return ApiResponse(
            data={
                "enabled": info.state.value != "disabled",
                "running": info.state.value == "running",
                "state": info.state.value,
                "host": info.current_config.get("host", "0.0.0.0"),
                "port": info.current_config.get("port", 1883),
                "ws_port": info.current_config.get("ws_port", 8083),
                "connections": connections,
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
async def start_mqtt_server(
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    from edgelite.services.service_manager import get_service_manager

    try:
        mgr = get_service_manager()
        result = await mgr.start_service("mqtt_server")
        if not result.get("success"):
            raise HTTPException(status_code=500, detail=result.get("error", "启动失败"))
        return ApiResponse(data=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("启动失败: %s", e)
        raise HTTPException(status_code=500, detail="启动失败") from e


@router.post("/stop", response_model=ApiResponse)
async def stop_mqtt_server(
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    from edgelite.services.service_manager import get_service_manager

    try:
        mgr = get_service_manager()
        result = await mgr.stop_service("mqtt_server")
        if not result.get("success"):
            raise HTTPException(status_code=500, detail=result.get("error", "停止失败"))
        return ApiResponse(data=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("停止失败: %s", e)
        raise HTTPException(status_code=500, detail="停止失败") from e


@router.put("/config", response_model=ApiResponse)
async def update_mqtt_server_config(
    config: MqttServerConfigModel,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    from edgelite.services.service_manager import get_service_manager

    try:
        mgr = get_service_manager()
        result = await mgr.update_service_config("mqtt_server", config.model_dump())
        if not result.get("success"):
            raise HTTPException(status_code=500, detail=result.get("error", "配置更新失败"))
        return ApiResponse(data=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("更新失败: %s", e)
        raise HTTPException(status_code=500, detail="更新失败") from e
