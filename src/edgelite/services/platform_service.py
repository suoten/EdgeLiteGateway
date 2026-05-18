"""平台管理服务

负责平台注册表的维护、平台连接/断开、配置schema查询等业务逻辑。
API层仅做参数校验和响应格式化，核心逻辑全部在此服务中。
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

from edgelite.platform.base import PlatformHandler

logger = logging.getLogger(__name__)

_PLATFORM_REGISTRY: dict[str, dict] = {}


def _ensure_registry() -> dict[str, dict]:
    if _PLATFORM_REGISTRY:
        return _PLATFORM_REGISTRY
    _PLATFORM_REGISTRY.update(
        {
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
        }
    )
    return _PLATFORM_REGISTRY


class PlatformService:
    """平台管理服务"""

    def __init__(self, handlers: dict[str, PlatformHandler] | None = None):
        self._handlers: dict[str, PlatformHandler] = handlers if handlers is not None else {}

    @property
    def handlers(self) -> dict[str, PlatformHandler]:
        return self._handlers

    def list_platforms(self) -> list[dict[str, Any]]:
        return [
            {
                "name": getattr(h, "platform_name", name),
                "version": getattr(h, "platform_version", "1.0.0"),
                "connected": getattr(h, "_connected", False),
            }
            for name, h in self._handlers.items()
        ]

    def list_supported(self) -> list[dict[str, str]]:
        registry = _ensure_registry()
        return [
            {"name": k, "label": v["label"], "description": v["description"]}
            for k, v in registry.items()
        ]

    def get_config_schema(self, platform_name: str) -> dict | None:
        registry = _ensure_registry()
        entry = registry.get(platform_name)
        if not entry:
            return None
        return entry["schema"]

    def validate_config(self, platform_name: str, config: dict) -> list[str]:
        registry = _ensure_registry()
        entry = registry.get(platform_name)
        if not entry:
            return [f"Unsupported platform: {platform_name}"]  # FIXED: 原问题-中文硬编码错误消息
        schema_fields = entry.get("schema", {}).get("fields", [])
        required_fields = [f["name"] for f in schema_fields if f.get("required")]
        missing = [f for f in required_fields if f not in config or not config[f]]
        return missing

    async def connect(self, platform_name: str, config: dict) -> dict[str, Any]:
        if platform_name in self._handlers:
            return {"status": "already_connected"}

        registry = _ensure_registry()
        entry = registry.get(platform_name)
        if not entry:
            raise ValueError(f"Unsupported platform: {platform_name}")  # FIXED: 原问题-中文硬编码错误消息

        missing = self.validate_config(platform_name, config)
        if missing:
            raise ValueError(f"Missing required config: {', '.join(missing)}")  # FIXED: 原问题-中文硬编码错误消息

        module = importlib.import_module(entry["module"])
        handler_cls = getattr(module, entry["class"])
        handler = handler_cls()
        await handler.connect(config)
        self._handlers[platform_name] = handler
        return {"status": "connected"}

    async def disconnect(self, platform_name: str) -> dict[str, Any]:
        handler = self._handlers.get(platform_name)
        if not handler:
            raise KeyError(f"Platform {platform_name} not connected")  # FIXED: 原问题-中文硬编码错误消息
        await handler.disconnect()
        self._handlers.pop(platform_name, None)
        return {"status": "disconnected"}

    def get_status(self, platform_name: str) -> dict[str, Any]:
        handler = self._handlers.get(platform_name)
        if not handler:
            return {"connected": False}
        return {
            "connected": getattr(handler, "_connected", False),
            "name": getattr(handler, "platform_name", platform_name),
            "version": getattr(handler, "platform_version", "1.0.0"),
        }
