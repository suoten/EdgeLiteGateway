"""平台配置管理API路由"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from edgelite.models.common import ApiResponse
from edgelite.api.deps import CurrentUser, require_permission
from edgelite.security.rbac import Permission

router = APIRouter(prefix="/api/v1/platforms", tags=["平台配置"])


def _get_platform_handlers():
    from edgelite.app import _app_state
    return getattr(_app_state, "platform_handlers", {})


@router.get("/list", response_model=ApiResponse)
async def list_platforms(
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    handlers = _get_platform_handlers()
    platforms = []
    for name, handler in handlers.items():
        platforms.append({
            "name": getattr(handler, "platform_name", name),
            "version": getattr(handler, "platform_version", "1.0.0"),
            "connected": getattr(handler, "_connected", False),
        })

    all_supported = [
        {"name": "iotsharp", "label": "IoTSharp", "description": "开源IoT平台"},
        {"name": "thingsboard", "label": "ThingsBoard", "description": "开源IoT平台"},
        {"name": "huawei_iotda", "label": "华为云IoTDA", "description": "华为云设备接入服务"},
        {"name": "thingscloud", "label": "ThingsCloud", "description": "ThingsCloud物联网平台"},
        {"name": "custom", "label": "自定义平台", "description": "MQTT/HTTP自定义对接"},
    ]

    return ApiResponse(data={"platforms": platforms, "supported": all_supported})


@router.get("/config-schema/{platform_name}", response_model=ApiResponse)
async def get_platform_config_schema(
    platform_name: str,
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    schemas = {
        "iotsharp": {
            "fields": [
                {"name": "host", "type": "string", "label": "IoTSharp地址", "default": "http://localhost:8080", "required": True},
                {"name": "api_key", "type": "string", "label": "API Key", "required": True},
                {"name": "device_id", "type": "string", "label": "设备ID"},
            ]
        },
        "thingsboard": {
            "fields": [
                {"name": "host", "type": "string", "label": "ThingsBoard地址", "default": "http://localhost:8080", "required": True},
                {"name": "access_token", "type": "string", "label": "设备Token", "required": True},
                {"name": "device_id", "type": "string", "label": "设备ID"},
            ]
        },
        "huawei_iotda": {
            "fields": [
                {"name": "broker", "type": "string", "label": "MQTT Broker", "required": True},
                {"name": "port", "type": "integer", "label": "端口", "default": 8883},
                {"name": "device_id", "type": "string", "label": "设备ID", "required": True},
                {"name": "secret", "type": "string", "label": "设备密钥", "secret": True, "required": True},
            ]
        },
        "thingscloud": {
            "fields": [
                {"name": "broker", "type": "string", "label": "MQTT Broker", "required": True},
                {"name": "port", "type": "integer", "label": "端口", "default": 1883},
                {"name": "username", "type": "string", "label": "Access Key", "required": True},
                {"name": "password", "type": "string", "label": "Access Secret", "secret": True, "required": True},
            ]
        },
        "custom": {
            "fields": [
                {"name": "broker", "type": "string", "label": "MQTT Broker", "required": True},
                {"name": "port", "type": "integer", "label": "端口", "default": 1883},
                {"name": "username", "type": "string", "label": "用户名"},
                {"name": "password", "type": "string", "label": "密码", "secret": True},
                {"name": "topic_prefix", "type": "string", "label": "Topic前缀", "default": "edgelite"},
            ]
        },
    }

    schema = schemas.get(platform_name)
    if not schema:
        raise HTTPException(status_code=404, detail=f"平台 {platform_name} 配置模板不存在")

    return ApiResponse(data={"platform_name": platform_name, "schema": schema})


@router.post("/connect/{platform_name}", response_model=ApiResponse)
async def connect_platform(
    platform_name: str,
    config: dict,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    from edgelite.app import _app_state

    handlers = getattr(_app_state, "platform_handlers", {})
    if platform_name in handlers:
        return ApiResponse(data={"status": "already_connected"})

    try:
        if platform_name == "iotsharp":
            from edgelite.platform.iotsharp import IoTSharpHandler
            handler = IoTSharpHandler()
        elif platform_name == "thingsboard":
            from edgelite.platform.thingsboard import ThingsBoardHandler
            handler = ThingsBoardHandler()
        elif platform_name == "huawei_iotda":
            from edgelite.platform.huawei_iotda import HuaweiIoTDAHandler
            handler = HuaweiIoTDAHandler()
        elif platform_name == "thingscloud":
            from edgelite.platform.thingscloud import ThingsCloudHandler
            handler = ThingsCloudHandler()
        else:
            raise HTTPException(status_code=400, detail=f"不支持的平台: {platform_name}")

        await handler.connect(config)
        _app_state.platform_handlers[platform_name] = handler
        return ApiResponse(data={"status": "connected"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"平台连接失败: {e}")


@router.post("/disconnect/{platform_name}", response_model=ApiResponse)
async def disconnect_platform(
    platform_name: str,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    from edgelite.app import _app_state

    handlers = getattr(_app_state, "platform_handlers", {})
    handler = handlers.get(platform_name)
    if not handler:
        raise HTTPException(status_code=404, detail=f"平台 {platform_name} 未连接")

    try:
        await handler.disconnect()
        handlers.pop(platform_name, None)
        return ApiResponse(data={"status": "disconnected"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"断开失败: {e}")


@router.get("/status/{platform_name}", response_model=ApiResponse)
async def get_platform_status(
    platform_name: str,
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    handlers = _get_platform_handlers()
    handler = handlers.get(platform_name)
    if not handler:
        return ApiResponse(data={"connected": False})

    return ApiResponse(data={
        "connected": getattr(handler, "_connected", False),
        "name": getattr(handler, "platform_name", platform_name),
        "version": getattr(handler, "platform_version", "1.0.0"),
    })
