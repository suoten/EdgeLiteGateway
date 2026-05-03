"""平台配置管理API路由"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from edgelite.models.common import ApiResponse
from edgelite.api.deps import CurrentUser, require_permission
from edgelite.security.rbac import Permission

router = APIRouter(prefix="/api/v1/platforms", tags=["平台配置"])

_PLATFORM_REGISTRY: dict[str, dict] = {}


def _ensure_registry():
    if _PLATFORM_REGISTRY:
        return
    _PLATFORM_REGISTRY.update({
        "iotsharp": {
            "label": "IoTSharp",
            "description": "开源IoT平台",
            "module": "edgelite.platform.iotsharp",
            "class": "IoTSharpHandler",
            "schema": {
                "fields": [
                    {"name": "broker", "type": "string", "label": "MQTT Broker地址", "default": "localhost", "required": True},
                    {"name": "port", "type": "integer", "label": "MQTT端口", "default": 1883, "required": True},
                    {"name": "username", "type": "string", "label": "MQTT用户名"},
                    {"name": "password", "type": "string", "label": "MQTT密码", "secret": True},
                ]
            },
        },
        "thingsboard": {
            "label": "ThingsBoard",
            "description": "开源IoT平台",
            "module": "edgelite.platform.thingsboard",
            "class": "ThingsBoardHandler",
            "schema": {
                "fields": [
                    {"name": "broker", "type": "string", "label": "MQTT Broker地址", "default": "localhost", "required": True},
                    {"name": "port", "type": "integer", "label": "MQTT端口", "default": 1883, "required": True},
                    {"name": "token", "type": "string", "label": "网关AccessToken", "required": True},
                    {"name": "password", "type": "string", "label": "MQTT密码", "secret": True},
                ]
            },
        },
        "huawei_iotda": {
            "label": "华为云IoTDA",
            "description": "华为云设备接入服务",
            "module": "edgelite.platform.huawei_iotda",
            "class": "HuaweiIoTDAHandler",
            "schema": {
                "fields": [
                    {"name": "broker", "type": "string", "label": "MQTT Broker", "required": True},
                    {"name": "port", "type": "integer", "label": "端口", "default": 8883},
                    {"name": "device_id", "type": "string", "label": "设备ID", "required": True},
                    {"name": "secret", "type": "string", "label": "设备密钥", "secret": True, "required": True},
                ]
            },
        },
        "thingscloud": {
            "label": "ThingsCloud",
            "description": "ThingsCloud物联网平台",
            "module": "edgelite.platform.thingscloud",
            "class": "ThingsCloudHandler",
            "schema": {
                "fields": [
                    {"name": "broker", "type": "string", "label": "MQTT Broker", "required": True},
                    {"name": "port", "type": "integer", "label": "端口", "default": 1883},
                    {"name": "access_key", "type": "string", "label": "Access Key", "required": True},
                    {"name": "access_secret", "type": "string", "label": "Access Secret", "secret": True, "required": True},
                ]
            },
        },
        "thingspanel": {
            "label": "ThingsPanel",
            "description": "ThingsPanel开源物联网平台",
            "module": "edgelite.platform.thingspanel",
            "class": "ThingsPanelHandler",
            "schema": {
                "fields": [
                    {"name": "broker", "type": "string", "label": "MQTT Broker", "required": True},
                    {"name": "port", "type": "integer", "label": "端口", "default": 1883},
                    {"name": "username", "type": "string", "label": "用户名"},
                    {"name": "password", "type": "string", "label": "密码", "secret": True},
                    {"name": "device_token", "type": "string", "label": "设备Token", "required": True},
                ]
            },
        },
        "custom": {
            "label": "自定义平台",
            "description": "MQTT/HTTP自定义对接",
            "module": "edgelite.platform.custom_mqtt",
            "class": "CustomMqttHandler",
            "schema": {
                "fields": [
                    {"name": "broker", "type": "string", "label": "MQTT Broker", "required": True},
                    {"name": "port", "type": "integer", "label": "端口", "default": 1883},
                    {"name": "username", "type": "string", "label": "用户名"},
                    {"name": "password", "type": "string", "label": "密码", "secret": True},
                    {"name": "topic_prefix", "type": "string", "label": "Topic前缀", "default": "edgelite"},
                ]
            },
        },
    })


def _get_platform_handlers():
    from edgelite.app import _app_state
    return getattr(_app_state, "platform_handlers", {})


@router.get("/list", response_model=ApiResponse)
async def list_platforms(
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    _ensure_registry()
    handlers = _get_platform_handlers()
    platforms = []
    for name, handler in handlers.items():
        platforms.append({
            "name": getattr(handler, "platform_name", name),
            "version": getattr(handler, "platform_version", "1.0.0"),
            "connected": getattr(handler, "_connected", False),
        })

    all_supported = [
        {"name": k, "label": v["label"], "description": v["description"]}
        for k, v in _PLATFORM_REGISTRY.items()
    ]

    return ApiResponse(data={"platforms": platforms, "supported": all_supported})


@router.get("/config-schema/{platform_name}", response_model=ApiResponse)
async def get_platform_config_schema(
    platform_name: str,
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    _ensure_registry()
    entry = _PLATFORM_REGISTRY.get(platform_name)
    if not entry:
        raise HTTPException(status_code=404, detail=f"平台 {platform_name} 配置模板不存在")

    return ApiResponse(data={"platform_name": platform_name, "schema": entry["schema"]})


@router.post("/connect/{platform_name}", response_model=ApiResponse)
async def connect_platform(
    platform_name: str,
    config: dict,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    _ensure_registry()
    from edgelite.app import _app_state
    import importlib

    handlers = getattr(_app_state, "platform_handlers", {})
    if platform_name in handlers:
        return ApiResponse(data={"status": "already_connected"})

    entry = _PLATFORM_REGISTRY.get(platform_name)
    if not entry:
        raise HTTPException(status_code=400, detail=f"不支持的平台: {platform_name}")

    try:
        module = importlib.import_module(entry["module"])
        handler_cls = getattr(module, entry["class"])
        handler = handler_cls()
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
