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
                "description": "Open-source IoT platform",  # FIXED-P3: 中文硬编码description
                "module": "edgelite.platform.iotsharp",
                "class": "IoTSharpHandler",
                "schema": {
                    "fields": [
                        {"name": "broker", "type": "string", "label": "MQTT Broker Address", "default": "localhost", "required": True},  # FIXED-P3: 中文label
                        {"name": "port", "type": "integer", "label": "MQTT Port", "default": 1883, "required": True},
                        {"name": "username", "type": "string", "label": "MQTT Username"},
                        {"name": "password", "type": "string", "label": "MQTT Password", "secret": True},
                    ]
                },
            },
            "thingsboard": {
                "label": "ThingsBoard",
                "description": "Open-source IoT platform",  # FIXED-P3: 中文硬编码description
                "module": "edgelite.platform.thingsboard",
                "class": "ThingsBoardHandler",
                "schema": {
                    "fields": [
                        {"name": "broker", "type": "string", "label": "MQTT Broker Address", "default": "localhost", "required": True},  # FIXED-P3: 中文label
                        {"name": "port", "type": "integer", "label": "MQTT Port", "default": 1883, "required": True},
                        {"name": "token", "type": "string", "label": "Gateway Access Token", "required": True},
                        {"name": "password", "type": "string", "label": "MQTT Password", "secret": True},
                    ]
                },
            },
            "huawei_iotda": {
                "label": "Huawei IoTDA",  # FIXED-P3: 中文硬编码label→英文
                "description": "Huawei Cloud Device Access Service",  # FIXED-P3: 中文硬编码description→英文
                "module": "edgelite.platform.huawei_iotda",
                "class": "HuaweiIoTDAHandler",
                "schema": {
                    "fields": [
                        {"name": "broker", "type": "string", "label": "MQTT Broker", "required": True},
                        {"name": "port", "type": "integer", "label": "Port", "default": 8883},  # FIXED-P3: 中文label→英文
                        {"name": "device_id", "type": "string", "label": "Device ID", "required": True},  # FIXED-P3: 中文label→英文
                        {"name": "secret", "type": "string", "label": "Device Secret", "secret": True, "required": True},  # FIXED-P3: 中文label→英文
                    ]
                },
            },
            "thingscloud": {
                "label": "ThingsCloud",
                "description": "ThingsCloud IoT Platform",  # FIXED-P3: 中文硬编码description→英文
                "module": "edgelite.platform.thingscloud",
                "class": "ThingsCloudHandler",
                "schema": {
                    "fields": [
                        {"name": "broker", "type": "string", "label": "MQTT Broker", "required": True},
                        {"name": "port", "type": "integer", "label": "Port", "default": 1883},  # FIXED-P3: 中文label→英文
                        {"name": "access_key", "type": "string", "label": "Access Key", "required": True},
                        {"name": "access_secret", "type": "string", "label": "Access Secret", "secret": True, "required": True},
                    ]
                },
            },
            "thingspanel": {
                "label": "ThingsPanel",
                "description": "ThingsPanel Open-Source IoT Platform",  # FIXED-P3: 中文硬编码description→英文
                "module": "edgelite.platform.thingspanel",
                "class": "ThingsPanelHandler",
                "schema": {
                    "fields": [
                        {"name": "broker", "type": "string", "label": "MQTT Broker", "required": True},
                        {"name": "port", "type": "integer", "label": "Port", "default": 1883},  # FIXED-P3: 中文label→英文
                        {"name": "username", "type": "string", "label": "Username"},  # FIXED-P3: 中文label→英文
                        {"name": "password", "type": "string", "label": "Password", "secret": True},  # FIXED-P3: 中文label→英文
                        {"name": "device_token", "type": "string", "label": "Device Token", "required": True},  # FIXED-P3: 中文label→英文
                    ]
                },
            },
            "custom": {
                "label": "Custom Platform",  # FIXED-P3: 中文硬编码label→英文
                "description": "MQTT/HTTP Custom Integration",  # FIXED-P3: 中文硬编码description→英文
                "module": "edgelite.platform.custom_mqtt",
                "class": "CustomMqttHandler",
                "schema": {
                    "fields": [
                        {"name": "broker", "type": "string", "label": "MQTT Broker", "required": True},
                        {"name": "port", "type": "integer", "label": "Port", "default": 1883},  # FIXED-P3: 中文label→英文
                        {"name": "username", "type": "string", "label": "Username"},  # FIXED-P3: 中文label→英文
                        {"name": "password", "type": "string", "label": "Password", "secret": True},  # FIXED-P3: 中文label→英文
                        {"name": "topic_prefix", "type": "string", "label": "Topic Prefix", "default": "edgelite"},  # FIXED-P3: 中文label→英文
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
        try:  # FIXED: 原问题-handler.connect(config)无try-except保护
            await handler.connect(config)
        except Exception as e:
            logger.error("Platform connect failed: %s - %s", platform_name, e)
            raise RuntimeError(f"Platform connect failed: {platform_name} - {e}") from e
        self._handlers[platform_name] = handler
        return {"status": "connected"}

    async def disconnect(self, platform_name: str) -> dict[str, Any]:
        handler = self._handlers.get(platform_name)
        if not handler:
            raise KeyError(f"Platform {platform_name} not connected")  # FIXED: 原问题-中文硬编码错误消息
        try:  # FIXED: 原问题-handler.disconnect()无try-except保护
            await handler.disconnect()
        except Exception as e:
            logger.error("Platform disconnect failed: %s - %s", platform_name, e)
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

    async def test_connection(self, platform_name: str, config: dict) -> dict[str, Any]:
        registry = _ensure_registry()
        entry = registry.get(platform_name)
        if not entry:
            raise ValueError(f"Unsupported platform: {platform_name}")
        missing = self.validate_config(platform_name, config)
        if missing:
            raise ValueError(f"Missing required config: {', '.join(missing)}")
        module = importlib.import_module(entry["module"])
        handler_cls = getattr(module, entry["class"])
        handler = handler_cls()
        try:
            await handler.connect(config)
            connected = getattr(handler, "_connected", False)
            await handler.disconnect()
            return {"success": connected, "message": "Connection test successful" if connected else "Connection test failed"}
        except Exception as e:
            return {"success": False, "message": f"Connection test failed: {e}"}
