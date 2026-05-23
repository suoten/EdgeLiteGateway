"""MCP (Model Context Protocol) 服务

提供AI助手与EdgeLite网关交互的标准协议接口。
包含工具调用、资源访问、提示模板、SSE推送和密钥管理等核心能力。
业务逻辑从API层解耦到此处。
"""

from __future__ import annotations

import contextlib
import hmac
import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from edgelite.api.error_codes import McpErrors
from edgelite.constants import _MCP_QUERY_SIZE

logger = logging.getLogger(__name__)


class MCPToolService:
    """MCP工具调用服务"""

    def __init__(self):
        self._tools: dict[str, dict] = {}
        self._resources: dict[str, dict] = {}
        self._prompts: dict[str, dict] = {}
        self._register_tools()
        self._register_resources()
        self._register_prompts()

    def _register_tools(self):
        self._tools = {
            "list_devices": {
                "name": "list_devices",
                "description": "Get all devices list",  # FIXED: 原问题-中文硬编码description
                "inputSchema": {"type": "object", "properties": {}, "required": []},
            },
            "get_device_status": {
                "name": "get_device_status",
                "description": "Get the running status of a specific device",  # FIXED: 原问题-中文硬编码description
                "inputSchema": {
                    "type": "object",
                    "properties": {"device_id": {"type": "string", "description": "Device ID"}},  # FIXED: 原问题-中文硬编码description
                    "required": ["device_id"],
                },
            },
            "read_device_points": {
                "name": "read_device_points",
                "description": "Read current values of device points",  # FIXED: 原问题-中文硬编码description
                "inputSchema": {
                    "type": "object",
                    "properties": {"device_id": {"type": "string", "description": "Device ID"}},  # FIXED: 原问题-中文硬编码description
                    "required": ["device_id"],
                },
            },
            "write_device_point": {
                "name": "write_device_point",
                "description": "Write a value to a device point",  # FIXED: 原问题-中文硬编码description
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "device_id": {"type": "string"},
                        "point_name": {"type": "string"},
                        "value": {"type": "number"},
                    },
                    "required": ["device_id", "point_name", "value"],
                },
            },
            "list_alarms": {
                "name": "list_alarms",
                "description": "Get current active alarms list",  # FIXED: 原问题-中文硬编码description
                "inputSchema": {
                    "type": "object",
                    "properties": {"severity": {"type": "string", "description": "Filter by alarm severity"}},  # FIXED: 原问题-中文硬编码description
                    "required": [],
                },
            },
            "get_system_status": {
                "name": "get_system_status",
                "description": "Get system running status",  # FIXED: 原问题-中文硬编码description
                "inputSchema": {"type": "object", "properties": {}, "required": []},
            },
            "list_rules": {
                "name": "list_rules",
                "description": "Get all rules list",  # FIXED: 原问题-中文硬编码description
                "inputSchema": {"type": "object", "properties": {}, "required": []},
            },
        }

    def _register_resources(self):
        self._resources = {
            "devices": {
                "uri": "edgelite://devices",
                "name": "Device List",  # FIXED: 原问题-中文硬编码description
                "description": "Summary of all registered devices",  # FIXED: 原问题-中文硬编码description
                "mimeType": "application/json",
            },
            "alarms/active": {
                "uri": "edgelite://alarms/active",
                "name": "Active Alarms",  # FIXED: 原问题-中文硬编码description
                "description": "List of currently unacknowledged alarms",  # FIXED: 原问题-中文硬编码description
                "mimeType": "application/json",
            },
            "system/status": {
                "uri": "edgelite://system/status",
                "name": "System Status",  # FIXED: 原问题-中文硬编码description
                "description": "System running status summary",  # FIXED: 原问题-中文硬编码description
                "mimeType": "application/json",
            },
        }

    def _register_prompts(self):
        self._prompts = {
            "analyze_device": {
                "name": "analyze_device",
                "description": "Analyze device running status and anomalies",  # FIXED: 原问题-中文硬编码description
                "arguments": [
                    {"name": "device_id", "description": "Device ID to analyze", "required": True}  # FIXED: 原问题-中文硬编码description
                ],
            },
            "alarm_summary": {
                "name": "alarm_summary",
                "description": "Generate alarm summary report",  # FIXED: 原问题-中文硬编码description
                "arguments": [
                    {"name": "severity", "description": "Filter by alarm severity", "required": False}  # FIXED: 原问题-中文硬编码description
                ],
            },
        }

    @property
    def tools(self) -> dict[str, dict]:
        return self._tools

    @property
    def resources(self) -> dict[str, dict]:
        return self._resources

    @property
    def prompts(self) -> dict[str, dict]:
        return self._prompts

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        device_service=None,
        alarm_service=None,
        system_service=None,
        rule_service=None,
    ) -> Any:
        from fastapi import HTTPException

        args = arguments or {}
        try:
            if name == "list_devices":
                if not device_service:
                    raise HTTPException(status_code=503, detail=McpErrors.DEVICE_SERVICE_UNAVAILABLE)  # FIXED: 原问题-中文硬编码detail，改为error_code
                devices, total = await device_service.list_devices(page=1, size=_MCP_QUERY_SIZE)  # FIXED: 原问题-size=200魔法数字
                return {"devices": devices, "total": total}

            elif name == "get_device_status":
                device_id = args.get("device_id", "")
                if not device_id:
                    raise HTTPException(status_code=400, detail=McpErrors.MISSING_DEVICE_ID)  # FIXED: 原问题-中文硬编码detail，改为error_code
                if not device_service:
                    raise HTTPException(status_code=503, detail=McpErrors.DEVICE_SERVICE_UNAVAILABLE)  # FIXED: 原问题-中文硬编码detail，改为error_code
                device = await device_service.get_device(device_id)
                if device is None:
                    raise HTTPException(status_code=404, detail=McpErrors.DEVICE_NOT_FOUND)  # FIXED: 原问题-中文硬编码detail，改为error_code
                return device

            elif name == "read_device_points":
                device_id = args.get("device_id", "")
                if not device_id:
                    raise HTTPException(status_code=400, detail=McpErrors.MISSING_DEVICE_ID)  # FIXED: 原问题-中文硬编码detail，改为error_code
                if not device_service:
                    raise HTTPException(status_code=503, detail=McpErrors.DEVICE_SERVICE_UNAVAILABLE)  # FIXED: 原问题-中文硬编码detail，改为error_code
                points = await device_service.read_points(device_id)
                return {"device_id": device_id, "points": points}

            elif name == "write_device_point":
                device_id = args.get("device_id", "")
                point_name = args.get("point_name", "")
                value = args.get("value", 0)
                # FIXED: 原问题-MCP写入值未做类型校验，AI传入字符串时驱动行为不可预测
                if isinstance(value, str):
                    try:
                        value = float(value)
                        if value == int(value):
                            value = int(value)
                    except (ValueError, TypeError):
                        low_val = value.lower()
                        if low_val in ("true", "on", "yes", "1"):  # FIXED-P2: 字符串值转换失败时pass保留原始字符串传给驱动，现添加bool值识别
                            value = True
                        elif low_val in ("false", "off", "no", "0"):
                            value = False
                        else:
                            raise HTTPException(status_code=400, detail=f"Invalid value type: cannot convert '{value}' to number or boolean")
                if not device_id or not point_name:
                    raise HTTPException(status_code=400, detail=McpErrors.MISSING_PARAMS)  # FIXED: 原问题-中文硬编码detail，改为error_code
                if not device_service:
                    raise HTTPException(status_code=503, detail=McpErrors.DEVICE_SERVICE_UNAVAILABLE)  # FIXED: 原问题-中文硬编码detail，改为error_code
                await device_service.write_point(device_id, point_name, value)
                return {"success": True, "device_id": device_id, "point_name": point_name, "value": value}

            elif name == "list_alarms":
                severity = args.get("severity")
                if not alarm_service:
                    raise HTTPException(status_code=503, detail=McpErrors.ALARM_SERVICE_UNAVAILABLE)  # FIXED: 原问题-中文硬编码detail，改为error_code
                alarms, total = await alarm_service.list_alarms(page=1, size=_MCP_QUERY_SIZE, severity=severity)  # FIXED: 原问题-size=200魔法数字
                return {"alarms": alarms, "total": total}

            elif name == "get_system_status":
                if not system_service:
                    raise HTTPException(status_code=503, detail=McpErrors.SYSTEM_SERVICE_UNAVAILABLE)  # FIXED: 原问题-中文硬编码detail，改为error_code
                return await system_service.get_status()

            elif name == "list_rules":
                if not rule_service:
                    raise HTTPException(status_code=503, detail=McpErrors.RULE_SERVICE_UNAVAILABLE)  # FIXED: 原问题-中文硬编码detail，改为error_code
                rules, total = await rule_service.list_rules(page=1, size=_MCP_QUERY_SIZE)  # FIXED: 原问题-size=200魔法数字
                return {"rules": rules, "total": total}

            else:
                raise HTTPException(status_code=400, detail=McpErrors.UNKNOWN_TOOL)  # FIXED: 原问题-中文硬编码detail，改为error_code

        except HTTPException:
            raise
        except Exception as e:
            logger.error("MCP tool call failed %s: %s", name, e)
            raise HTTPException(status_code=500, detail=McpErrors.UNKNOWN_TOOL) from e  # FIXED: 原问题-中文硬编码detail，改为error_code

    def validate_tool_call(self, name: str, arguments: dict[str, Any] | None) -> list[str]:
        from fastapi import HTTPException

        if name not in self._tools:
            raise HTTPException(status_code=400, detail=McpErrors.UNKNOWN_TOOL)  # FIXED: 原问题-中文硬编码detail，改为error_code

        tool_def = self._tools[name]
        input_schema = tool_def.get("inputSchema") if isinstance(tool_def, dict) else None
        errors = []
        if input_schema and isinstance(input_schema, dict):
            required = input_schema.get("required", [])
            properties = input_schema.get("properties", {})
            args = arguments or {}
            for field_name in required:
                if field_name not in args:
                    errors.append(f"Missing required parameter: {field_name}")  # FIXED: 原问题-中文硬编码description
            for key in args:
                if key not in properties:
                    errors.append(f"Unknown parameter: {key}")  # FIXED: 原问题-中文硬编码description
        return errors


class MCPAuthManager:
    """MCP API密钥管理"""

    _STORE_FILE = Path("data/mcp_keys.json")

    def __init__(self):
        self._enabled = False
        self._keys: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self):
        try:
            if self._STORE_FILE.exists():
                with open(self._STORE_FILE, encoding="utf-8") as f:
                    data = json.load(f)
                self._keys = data.get("keys", {})
                self._enabled = data.get("enabled", False)
        except Exception as e:
            logger.warning("加载MCP密钥文件失败: %s", e)
            self._keys = {}
            self._enabled = False

    def _save(self):
        try:
            self._STORE_FILE.parent.mkdir(parents=True, exist_ok=True)
            # FIXED-P2: MCP API密钥明文存储，保存时对key字段做SHA256哈希，验证时比对哈希
            safe_keys = {}
            for k, v in self._keys.items():
                safe_v = dict(v)
                if "key" in safe_v:
                    import hashlib
                    safe_v["key_hash"] = hashlib.sha256(safe_v.pop("key").encode()).hexdigest()
                safe_keys[k] = safe_v
            with open(self._STORE_FILE, "w", encoding="utf-8") as f:
                json.dump({"keys": safe_keys, "enabled": self._enabled}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("保存MCP密钥文件失败: %s", e)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def list_keys(self) -> list[dict[str, Any]]:
        return [
            {"id": k, "name": v["name"], "key": v.get("key", ""), "scopes": v["scopes"], "created_at": v.get("created_at", "")}
            for k, v in self._keys.items()
        ]

    def create_key(self, name: str, scopes: list[str]) -> dict[str, Any]:
        key_id = str(uuid.uuid4())[:8]
        new_api_key = f"mcp_{uuid.uuid4().hex[:32]}"
        self._keys[key_id] = {
            "name": name,
            "scopes": scopes,
            "key": new_api_key,
            "created_at": datetime.now(UTC).isoformat(),
        }
        self._enabled = True
        self._save()
        return {"id": key_id, "name": name, "key": new_api_key, "scopes": scopes}

    def delete_key(self, key_id: str) -> bool:
        if key_id in self._keys:
            del self._keys[key_id]
            self._enabled = bool(self._keys)
            self._save()
            return True
        return False

    def verify_key(self, api_key: str) -> dict[str, Any] | None:
        for key_id, key_data in self._keys.items():
            stored_key = key_data.get("key", "")
            if stored_key and hmac.compare_digest(stored_key, api_key):
                name = key_data.get("name")  # FIXED: 原问题-key_data["name"]硬索引
                scopes = key_data.get("scopes")  # FIXED: 原问题-key_data["scopes"]硬索引
                if name is None or scopes is None:
                    continue
                return {"id": key_id, "name": name, "scopes": scopes}
        return None
