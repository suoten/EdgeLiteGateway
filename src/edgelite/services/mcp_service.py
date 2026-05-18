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
                "description": "获取所有设备列表",
                "inputSchema": {"type": "object", "properties": {}, "required": []},
            },
            "get_device_status": {
                "name": "get_device_status",
                "description": "获取指定设备的运行状态",
                "inputSchema": {
                    "type": "object",
                    "properties": {"device_id": {"type": "string", "description": "设备ID"}},
                    "required": ["device_id"],
                },
            },
            "read_device_points": {
                "name": "read_device_points",
                "description": "读取设备测点当前值",
                "inputSchema": {
                    "type": "object",
                    "properties": {"device_id": {"type": "string", "description": "设备ID"}},
                    "required": ["device_id"],
                },
            },
            "write_device_point": {
                "name": "write_device_point",
                "description": "写入设备测点值",
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
                "description": "获取当前活跃告警列表",
                "inputSchema": {
                    "type": "object",
                    "properties": {"severity": {"type": "string", "description": "告警级别过滤"}},
                    "required": [],
                },
            },
            "get_system_status": {
                "name": "get_system_status",
                "description": "获取系统运行状态",
                "inputSchema": {"type": "object", "properties": {}, "required": []},
            },
            "list_rules": {
                "name": "list_rules",
                "description": "获取所有规则列表",
                "inputSchema": {"type": "object", "properties": {}, "required": []},
            },
        }

    def _register_resources(self):
        self._resources = {
            "devices": {
                "uri": "edgelite://devices",
                "name": "设备列表",
                "description": "所有已注册设备的概要信息",
                "mimeType": "application/json",
            },
            "alarms/active": {
                "uri": "edgelite://alarms/active",
                "name": "活跃告警",
                "description": "当前未确认的告警列表",
                "mimeType": "application/json",
            },
            "system/status": {
                "uri": "edgelite://system/status",
                "name": "系统状态",
                "description": "系统运行状态概要",
                "mimeType": "application/json",
            },
        }

    def _register_prompts(self):
        self._prompts = {
            "analyze_device": {
                "name": "analyze_device",
                "description": "分析设备运行状态和异常",
                "arguments": [
                    {"name": "device_id", "description": "要分析的设备ID", "required": True}
                ],
            },
            "alarm_summary": {
                "name": "alarm_summary",
                "description": "生成告警摘要报告",
                "arguments": [
                    {"name": "severity", "description": "告警级别过滤", "required": False}
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
                    errors.append(f"缺少必填参数: {field_name}")
            for key in args:
                if key not in properties:
                    errors.append(f"未知参数: {key}")
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
            with open(self._STORE_FILE, "w", encoding="utf-8") as f:
                json.dump({"keys": self._keys, "enabled": self._enabled}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("保存MCP密钥文件失败: %s", e)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def list_keys(self) -> list[dict[str, Any]]:
        return [
            {"id": k, "name": v["name"], "scopes": v["scopes"], "created_at": v.get("created_at", "")}
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
                return {"id": key_id, "name": key_data["name"], "scopes": key_data["scopes"]}
        return None
