"""MCP (Model Context Protocol) 服务端API路由

提供AI助手与EdgeLite网关交互的标准协议接口。
包含工具调用、资源访问、提示模板等核心能力。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from edgelite.models.common import ApiResponse
from edgelite.api.deps import CurrentUser, get_current_user, require_permission
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/mcp", tags=["MCP协议"])


class ToolCallRequest(BaseModel):
    name: str
    arguments: dict[str, Any] | None = None


class MCPServer:
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
                "inputSchema": {"type": "object", "properties": {"device_id": {"type": "string", "description": "设备ID"}}, "required": ["device_id"]},
            },
            "read_device_points": {
                "name": "read_device_points",
                "description": "读取设备测点当前值",
                "inputSchema": {"type": "object", "properties": {"device_id": {"type": "string", "description": "设备ID"}}, "required": ["device_id"]},
            },
            "write_device_point": {
                "name": "write_device_point",
                "description": "写入设备测点值",
                "inputSchema": {"type": "object", "properties": {"device_id": {"type": "string"}, "point_name": {"type": "string"}, "value": {"type": "number"}}, "required": ["device_id", "point_name", "value"]},
            },
            "list_alarms": {
                "name": "list_alarms",
                "description": "获取当前活跃告警列表",
                "inputSchema": {"type": "object", "properties": {"severity": {"type": "string", "description": "告警级别过滤"}}, "required": []},
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
            "devices": {"uri": "edgelite://devices", "name": "设备列表", "description": "所有已注册设备的概要信息", "mimeType": "application/json"},
            "alarms/active": {"uri": "edgelite://alarms/active", "name": "活跃告警", "description": "当前未确认的告警列表", "mimeType": "application/json"},
            "system/status": {"uri": "edgelite://system/status", "name": "系统状态", "description": "系统运行状态概要", "mimeType": "application/json"},
        }

    def _register_prompts(self):
        self._prompts = {
            "analyze_device": {
                "name": "analyze_device",
                "description": "分析设备运行状态和异常",
                "arguments": [{"name": "device_id", "description": "要分析的设备ID", "required": True}],
            },
            "alarm_summary": {
                "name": "alarm_summary",
                "description": "生成告警摘要报告",
                "arguments": [{"name": "severity", "description": "告警级别过滤", "required": False}],
            },
        }

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        args = arguments or {}
        try:
            from edgelite.app import _app_state

            if name == "list_devices":
                svc = getattr(_app_state, "device_service", None)
                if svc:
                    devices, total = await svc.list_devices(page=1, size=200)
                    return {"devices": devices, "total": total}
                raise HTTPException(status_code=503, detail="设备服务不可用")

            elif name == "get_device_status":
                device_id = args.get("device_id", "")
                if not device_id:
                    raise HTTPException(status_code=400, detail="缺少参数: device_id")
                svc = getattr(_app_state, "device_service", None)
                if svc:
                    device = await svc.get_device(device_id)
                    if device is None:
                        raise HTTPException(status_code=404, detail=f"设备 {device_id} 不存在")
                    return device
                raise HTTPException(status_code=503, detail="设备服务不可用")

            elif name == "read_device_points":
                device_id = args.get("device_id", "")
                if not device_id:
                    raise HTTPException(status_code=400, detail="缺少参数: device_id")
                svc = getattr(_app_state, "device_service", None)
                if svc:
                    points = await svc.read_points(device_id)
                    return {"device_id": device_id, "points": points}
                raise HTTPException(status_code=503, detail="设备服务不可用")

            elif name == "write_device_point":
                device_id = args.get("device_id", "")
                point_name = args.get("point_name", "")
                value = args.get("value", 0)
                if not device_id or not point_name:
                    raise HTTPException(status_code=400, detail="缺少参数: device_id 或 point_name")
                svc = getattr(_app_state, "device_service", None)
                if svc:
                    await svc.write_point(device_id, point_name, value)
                    return {"success": True, "device_id": device_id, "point_name": point_name, "value": value}
                raise HTTPException(status_code=503, detail="设备服务不可用")

            elif name == "list_alarms":
                severity = args.get("severity")
                svc = getattr(_app_state, "alarm_service", None)
                if svc:
                    alarms, total = await svc.list_alarms(page=1, size=200, severity=severity)
                    return {"alarms": alarms, "total": total}
                raise HTTPException(status_code=503, detail="告警服务不可用")

            elif name == "get_system_status":
                svc = getattr(_app_state, "system_service", None)
                if svc:
                    status = await svc.get_status()
                    return status
                raise HTTPException(status_code=503, detail="系统服务不可用")

            elif name == "list_rules":
                svc = getattr(_app_state, "rule_service", None)
                if svc:
                    rules, total = await svc.list_rules(page=1, size=200)
                    return {"rules": rules, "total": total}
                raise HTTPException(status_code=503, detail="规则服务不可用")

            else:
                raise HTTPException(status_code=400, detail=f"未知工具: {name}")

        except HTTPException:
            raise
        except Exception as e:
            logger.error("MCP工具调用异常 %s: %s", name, e)
            raise HTTPException(status_code=500, detail=f"工具调用失败: {str(e)}")


_mcp_server = MCPServer()


@router.get("/tools", response_model=ApiResponse)
async def list_tools(_user=Depends(get_current_user)):
    try:
        return ApiResponse(data={"tools": list(_mcp_server._tools.values())})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("获取列表失败: %s", e)
        raise HTTPException(status_code=500, detail="获取列表失败")


@router.post("/call", response_model=ApiResponse)
async def call_tool(req: ToolCallRequest, _user=Depends(get_current_user)):
    if req.name not in _mcp_server._tools:
        raise HTTPException(status_code=400, detail=f"未知工具: {req.name}")
    try:
        tool_def = _mcp_server._tools[req.name]
        input_schema = tool_def.get("inputSchema") if isinstance(tool_def, dict) else getattr(tool_def, "input_schema", None)
        if input_schema and isinstance(input_schema, dict):
            required = input_schema.get("required", [])
            properties = input_schema.get("properties", {})
            if req.arguments is None:
                req.arguments = {}
            for field_name in required:
                if field_name not in req.arguments:
                    raise HTTPException(status_code=400, detail=f"缺少必填参数: {field_name}")
            for key in req.arguments:
                if key not in properties:
                    raise HTTPException(status_code=400, detail=f"未知参数: {key}")
        result = await _mcp_server.call_tool(req.name, req.arguments or {})
        return ApiResponse(data=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("操作失败: %s", e)
        raise HTTPException(status_code=500, detail="操作失败")


@router.get("/resources", response_model=ApiResponse)
async def list_resources(_user=Depends(get_current_user)):
    try:
        return ApiResponse(data={"resources": list(_mcp_server._resources.values())})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("获取列表失败: %s", e)
        raise HTTPException(status_code=500, detail="获取列表失败")


@router.get("/prompts", response_model=ApiResponse)
async def list_prompts(_user=Depends(get_current_user)):
    try:
        return ApiResponse(data={"prompts": list(_mcp_server._prompts.values())})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("获取列表失败: %s", e)
        raise HTTPException(status_code=500, detail="获取列表失败")


class MCPAuthManager:
    _STORE_FILE = Path("data/mcp_keys.json")

    def __init__(self):
        self._enabled = False
        self._keys: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self):
        try:
            if self._STORE_FILE.exists():
                with open(self._STORE_FILE, "r", encoding="utf-8") as f:
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

    def list_keys(self) -> list[dict[str, Any]]:
        return [{"id": k, "name": v["name"], "scopes": v["scopes"], "created_at": v.get("created_at", "")} for k, v in self._keys.items()]

    def create_key(self, name: str, scopes: list[str]) -> dict[str, Any]:
        import uuid
        from datetime import datetime, timezone
        key_id = str(uuid.uuid4())[:8]
        new_api_key = f"mcp_{uuid.uuid4().hex[:32]}"
        self._keys[key_id] = {"name": name, "scopes": scopes, "key": new_api_key, "created_at": datetime.now(timezone.utc).isoformat()}
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


_mcp_auth = MCPAuthManager()


@router.get("/auth-keys", response_model=ApiResponse)
async def list_auth_keys(_user=Depends(get_current_user)):
    try:
        return ApiResponse(data={"keys": _mcp_auth.list_keys(), "enabled": _mcp_auth._enabled})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("获取列表失败: %s", e)
        raise HTTPException(status_code=500, detail="获取列表失败")


class CreateKeyRequest(BaseModel):
    name: str
    scopes: list[str] = []


@router.post("/auth-keys", response_model=ApiResponse)
async def create_auth_key(req: CreateKeyRequest, user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE)):
    result = _mcp_auth.create_key(req.name, req.scopes)
    try:
        return ApiResponse(data=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("创建失败: %s", e)
        raise HTTPException(status_code=500, detail="创建失败")


@router.delete("/auth-keys/{key_id}", response_model=ApiResponse)
async def delete_auth_key(key_id: str, user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE)):
    if _mcp_auth.delete_key(key_id):
        return ApiResponse(data={"deleted": True, "key_id": key_id})
    try:
        raise HTTPException(status_code=404, detail=f"密钥 {key_id} 不存在")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("删除失败: %s", e)
        raise HTTPException(status_code=500, detail="删除失败")


@router.get("/sse")
async def mcp_sse(_user=Depends(get_current_user)):
    """MCP SSE传输端点 - 供AI助手通过EventSource连接"""
    try:
        import asyncio as _asyncio
        from fastapi.responses import StreamingResponse as _SR

        async def event_generator():
            yield "event: connected\ndata: {\"server\": \"edgelite-mcp\", \"version\": \"1.0\"}\n\n"
            queue: _asyncio.Queue = _asyncio.Queue(maxsize=100)
            try:
                while True:
                    try:
                        message = await _asyncio.wait_for(queue.get(), timeout=30)
                        yield f"event: message\ndata: {message}\n\n"
                    except _asyncio.TimeoutError:
                        import time as _time
                        yield f"event: ping\ndata: {{\"timestamp\": {_time.time()}}}\n\n"
            except _asyncio.CancelledError:
                pass

        return _SR(
            event_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("SSE连接失败: %s", e)
        raise HTTPException(status_code=500, detail="SSE连接失败")
