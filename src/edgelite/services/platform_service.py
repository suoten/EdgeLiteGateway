"""平台管理服务

负责平台注册表的维护、平台连接/断开、配置schema查询等业务逻辑。
API层仅做参数校验和响应格式化，核心逻辑全部在此服务中。
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import re
from typing import Any

from edgelite.platform.base import PlatformHandler

logger = logging.getLogger(__name__)

_PLATFORM_REGISTRY: dict[str, dict] = {}

# broker 地址格式：域名、IP、或 mqtt:// 开头的URI
_BROKER_PATTERN = re.compile(
    r"^(mqtt[s]?://)?"        # 可选 mqtt:// 或 mqtts:// 前缀
    r"([a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.)*"  # 域名部分
    r"[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?"        # 顶级域
    r"$|"
    r"^(\d{1,3}\.){3}\d{1,3}$"  # IPv4 地址
)


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
                        {"name": "broker", "type": "string", "label": "MQTT Broker Address", "placeholder": "e.g. iotsharp.example.com", "required": True},
                        {"name": "port", "type": "integer", "label": "MQTT Port", "default": 1883, "required": True},
                        {"name": "username", "type": "string", "label": "MQTT Username"},
                        {"name": "password", "type": "string", "label": "MQTT Password", "secret": True},
                    ]
                },
            },
            "thingsboard": {
                "label": "ThingsBoard",
                "description": "Open-source IoT platform",
                "module": "edgelite.platform.thingsboard",
                "class": "ThingsBoardHandler",
                "schema": {
                    "fields": [
                        {"name": "broker", "type": "string", "label": "MQTT Broker Address", "placeholder": "e.g. thingsboard.example.com", "required": True},
                        {"name": "port", "type": "integer", "label": "MQTT Port", "default": 1883, "required": True},
                        {"name": "token", "type": "string", "label": "Gateway Access Token", "required": True},
                        {"name": "password", "type": "string", "label": "MQTT Password", "secret": True},
                    ]
                },
            },
            "huawei_iotda": {
                "label": "Huawei IoTDA",
                "description": "Huawei Cloud Device Access Service",
                "module": "edgelite.platform.huawei_iotda",
                "class": "HuaweiIoTDAHandler",
                "schema": {
                    "fields": [
                        {"name": "broker", "type": "string", "label": "MQTT Broker", "placeholder": "e.g. a123b456c.iot-mqtts.cn-north-4.myhuaweicloud.com", "required": True},
                        {"name": "port", "type": "integer", "label": "Port", "default": 8883},
                        {"name": "device_id", "type": "string", "label": "Device ID", "required": True},
                        {"name": "secret", "type": "string", "label": "Device Secret", "secret": True, "required": True},
                    ]
                },
            },
            "thingscloud": {
                "label": "ThingsCloud",
                "description": "ThingsCloud IoT Platform",
                "module": "edgelite.platform.thingscloud",
                "class": "ThingsCloudHandler",
                "schema": {
                    "fields": [
                        {"name": "broker", "type": "string", "label": "MQTT Broker", "placeholder": "e.g. mqtt.thingscloud.cn", "required": True},
                        {"name": "port", "type": "integer", "label": "Port", "default": 1883},  # FIXED-P3: 中文label→英文
                        {"name": "access_key", "type": "string", "label": "Access Key", "required": True},
                        {"name": "access_secret", "type": "string", "label": "Access Secret", "secret": True, "required": True},
                    ]
                },
            },
            "thingspanel": {
                "label": "ThingsPanel",
                "description": "ThingsPanel Open-Source IoT Platform",
                "module": "edgelite.platform.thingspanel",
                "class": "ThingsPanelHandler",
                "schema": {
                    "fields": [
                        {"name": "broker", "type": "string", "label": "MQTT Broker", "placeholder": "e.g. mqtt.thingspanel.cn", "required": True},
                        {"name": "port", "type": "integer", "label": "Port", "default": 1883},
                        {"name": "username", "type": "string", "label": "Username"},
                        {"name": "password", "type": "string", "label": "Password", "secret": True},
                        {"name": "device_token", "type": "string", "label": "Device Token", "required": True},
                    ]
                },
            },
            "custom": {
                "label": "Custom Platform",
                "description": "MQTT/HTTP Custom Integration",
                "module": "edgelite.platform.custom_mqtt",
                "class": "CustomMqttHandler",
                "schema": {
                    "fields": [
                        {"name": "broker", "type": "string", "label": "MQTT Broker", "placeholder": "e.g. 192.168.1.100 or mqtt.example.com", "required": True},
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
        """验证平台配置，返回错误码列表（空列表表示验证通过）"""
        registry = _ensure_registry()
        entry = registry.get(platform_name)
        if not entry:
            return ["ERR_PLATFORM_VALIDATION_UNSUPPORTED"]

        errors: list[str] = []
        schema_fields = entry.get("schema", {}).get("fields", [])

        # 1. 必填字段检查
        for f in schema_fields:
            if f.get("required"):
                name = f["name"]
                val = config.get(name)
                if not val and val != 0:
                    errors.append(f"ERR_PLATFORM_VALIDATION_REQUIRED:{name}")

        # 2. broker 地址格式验证
        broker_val = config.get("broker")
        if broker_val:
            clean_broker = re.sub(r"^mqtt[s]?://", "", str(broker_val))
            if not re.match(r"^[a-zA-Z0-9]", clean_broker):
                errors.append("ERR_PLATFORM_VALIDATION_BROKER_FORMAT")

        # 3. 端口范围验证
        port_val = config.get("port")
        if port_val is not None:
            try:
                port_int = int(port_val)
                if port_int < 1 or port_int > 65535:
                    errors.append("ERR_PLATFORM_VALIDATION_PORT_RANGE")
            except (ValueError, TypeError):
                errors.append("ERR_PLATFORM_VALIDATION_PORT_NUMBER")

        # 4. 字符串长度限制
        for f in schema_fields:
            name = f["name"]
            val = config.get(name)
            if isinstance(val, str) and len(val) > 256:
                errors.append(f"ERR_PLATFORM_VALIDATION_TOO_LONG:{name}")

        return errors

    async def connect(self, platform_name: str, config: dict) -> dict[str, Any]:
        if platform_name in self._handlers:
            return {"status": "already_connected"}

        registry = _ensure_registry()
        entry = registry.get(platform_name)
        if not entry:
            raise ValueError(f"Unsupported platform: {platform_name}")

        errors = self.validate_config(platform_name, config)
        if errors:
            raise ValueError(";".join(errors))

        module = importlib.import_module(entry["module"])
        handler_cls = getattr(module, entry["class"])
        handler = handler_cls()
        try:
            await handler.connect(config)
        except Exception as e:
            logger.error("Platform connect failed: %s - %s", platform_name, e)
            raise RuntimeError(f"Platform connect failed: {platform_name} - {e}") from e
        self._handlers[platform_name] = handler
        # connect()是异步后台连接，_connected此时可能为False
        # 返回connecting状态，让前端通过status API轮询
        return {"status": "connecting"}

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
        """测试平台连接：创建临时handler，等待MQTT连接建立，然后断开"""
        registry = _ensure_registry()
        entry = registry.get(platform_name)
        if not entry:
            raise ValueError(f"Unsupported platform: {platform_name}")
        errors = self.validate_config(platform_name, config)
        if errors:
            raise ValueError(";".join(errors))
        module = importlib.import_module(entry["module"])
        handler_cls = getattr(module, entry["class"])
        handler = handler_cls()
        try:
            await handler.connect(config)
            # 等待连接建立（最多10秒），因为connect()是异步后台连接
            max_wait = 10.0
            interval = 0.5
            elapsed = 0.0
            while elapsed < max_wait:
                if getattr(handler, "_connected", False):
                    break
                await asyncio.sleep(interval)
                elapsed += interval

            connected = getattr(handler, "_connected", False)
            await handler.disconnect()
            if connected:
                return {"success": True, "message": "Connection test successful"}
            else:
                return {
                    "success": False,
                    "message": f"Connection timed out after {max_wait:.0f}s - broker may be unreachable or credentials invalid",
                }
        except ImportError as e:
            return {"success": False, "message": f"Missing dependency: {e}"}
        except Exception as e:
            return {"success": False, "message": f"Connection test failed: {e}"}
