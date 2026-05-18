"""MCP (Model Context Protocol) API路由

提供AI助手与EdgeLite网关交互的标准协议接口。
业务逻辑委托给 mcp_service.py。
"""

from __future__ import annotations

import contextlib
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from edgelite.api.deps import (
    AlarmServiceDep,
    CurrentUser,
    DeviceServiceDep,
    EventBusDep,
    RuleServiceDep,
    SystemServiceDep,
    get_current_user,
    require_permission,
)
from edgelite.api.error_codes import McpErrors
from edgelite.models.common import ApiResponse
from edgelite.security.rbac import Permission
from edgelite.services.mcp_service import MCPAuthManager, MCPToolService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/mcp", tags=["MCP"])

_mcp_tools = MCPToolService()
_mcp_auth = MCPAuthManager()


class ToolCallRequest(BaseModel):
    name: str
    arguments: dict[str, Any] | None = None


@router.get("/tools", response_model=ApiResponse)
async def list_tools(_user=Depends(get_current_user)):
    try:
        return ApiResponse(data={"tools": list(_mcp_tools.tools.values())})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("list_tools failed: %s", e)
        raise HTTPException(status_code=500, detail=McpErrors.LIST_FAILED) from e  # FIXED: 原问题-硬编码错误码字符串，改为集中管理


@router.post("/call", response_model=ApiResponse)
async def call_tool(
    req: ToolCallRequest,
    device_service: DeviceServiceDep,
    alarm_service: AlarmServiceDep,
    system_service: SystemServiceDep,
    rule_service: RuleServiceDep,
    _user=Depends(get_current_user),
):
    try:
        errors = _mcp_tools.validate_tool_call(req.name, req.arguments)
        if errors:
            raise HTTPException(status_code=400, detail="; ".join(errors))
        result = await _mcp_tools.call_tool(
            req.name,
            req.arguments or {},
            device_service=device_service,
            alarm_service=alarm_service,
            system_service=system_service,
            rule_service=rule_service,
        )
        return ApiResponse(data=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("call_tool failed: %s", e)
        raise HTTPException(status_code=500, detail=McpErrors.CALL_FAILED) from e  # FIXED: 原问题-硬编码错误码字符串，改为集中管理


@router.get("/resources", response_model=ApiResponse)
async def list_resources(_user=Depends(get_current_user)):
    try:
        return ApiResponse(data={"resources": list(_mcp_tools.resources.values())})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("list_resources failed: %s", e)
        raise HTTPException(status_code=500, detail=McpErrors.LIST_FAILED) from e  # FIXED: 原问题-硬编码错误码字符串，改为集中管理


@router.get("/prompts", response_model=ApiResponse)
async def list_prompts(_user=Depends(get_current_user)):
    try:
        return ApiResponse(data={"prompts": list(_mcp_tools.prompts.values())})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("list_prompts failed: %s", e)
        raise HTTPException(status_code=500, detail=McpErrors.LIST_FAILED) from e  # FIXED: 原问题-硬编码错误码字符串，改为集中管理


@router.get("/auth-keys", response_model=ApiResponse)
async def list_auth_keys(user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE)):
    try:
        return ApiResponse(data={"keys": _mcp_auth.list_keys(), "enabled": _mcp_auth.enabled})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("list_auth_keys failed: %s", e)
        raise HTTPException(status_code=500, detail=McpErrors.LIST_FAILED) from e  # FIXED: 原问题-硬编码错误码字符串，改为集中管理


class CreateKeyRequest(BaseModel):
    name: str
    scopes: list[str] = []


@router.post("/auth-keys", response_model=ApiResponse)
async def create_auth_key(
    req: CreateKeyRequest, user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE)
):
    try:
        result = _mcp_auth.create_key(req.name, req.scopes)
        return ApiResponse(data=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("create_auth_key failed: %s", e)
        raise HTTPException(status_code=500, detail=McpErrors.CREATE_KEY_FAILED) from e  # FIXED: 原问题-硬编码错误码字符串，改为集中管理


@router.delete("/auth-keys/{key_id}", response_model=ApiResponse)
async def delete_auth_key(
    key_id: str, user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE)
):
    if _mcp_auth.delete_key(key_id):
        return ApiResponse(data={"deleted": True, "key_id": key_id})
    raise HTTPException(status_code=404, detail=McpErrors.KEY_NOT_FOUND)  # FIXED: 原问题-硬编码错误码字符串，改为集中管理


@router.get("/sse")
async def mcp_sse(
    event_bus: EventBusDep,
    token: str | None = Query(None),
    request: Request = None,  # FIXED: 原问题-request参数类型与默认值矛盾; FastAPI自动注入Request,但Python语法要求有默认值参数后不能跟无默认值参数
):
    # FIXED: 原问题-EventSource不支持自定义Header，token查询参数声明但未用于认证，SSE连接必定401
    user = None
    if token:
        from edgelite.security.jwt import verify_token
        from jose import JWTError
        try:
            payload = verify_token(token, token_type="access")
            username = payload.get("username", "")
            container = request.app.state
            from edgelite.storage.sqlite_repo import UserRepo
            async with container.database.get_session() as session:
                repo = UserRepo(session, container.database.write_lock)
                user = await repo.get_by_username(username)
        except (JWTError, Exception) as e:
            logger.warning("MCP SSE token验证失败: %s", e)
    if user is None:
        from fastapi import status as _st
        raise HTTPException(status_code=_st.HTTP_401_UNAUTHORIZED, detail="Invalid or missing token")

    try:
        import asyncio as _asyncio
        import time as _time

        from fastapi.responses import StreamingResponse as _StreamingResp

        async def event_generator():
            yield 'event: connected\ndata: {"server": "edgelite-mcp", "version": "1.0"}\n\n'
            queue: _asyncio.Queue = _asyncio.Queue(maxsize=100)
            _handler_types = []
            try:
                if event_bus:

                    def _on_alarm_event(event):
                        try:
                            data = {
                                "type": "alarm",
                                "alarm_id": getattr(event, "alarm_id", ""),
                                "device_id": getattr(event, "device_id", ""),
                                "severity": getattr(event, "severity", ""),
                                "action": getattr(event, "action", ""),
                            }
                            queue.put_nowait(json.dumps(data, default=str, ensure_ascii=False))
                        except _asyncio.QueueFull:
                            pass

                    def _on_device_event(event):
                        try:
                            data = {
                                "type": "device",
                                "device_id": getattr(event, "device_id", ""),
                                "new_status": getattr(event, "new_status", ""),
                            }
                            queue.put_nowait(json.dumps(data, default=str, ensure_ascii=False))
                        except _asyncio.QueueFull:
                            pass

                    def _on_point_event(event):
                        try:
                            data = {
                                "type": "realtime",
                                "device_id": getattr(event, "device_id", ""),
                                "point_name": getattr(event, "point_name", ""),
                                "value": getattr(event, "value", ""),
                            }
                            queue.put_nowait(json.dumps(data, default=str, ensure_ascii=False))
                        except _asyncio.QueueFull:
                            pass

                    event_bus.register_handler("AlarmEvent", _on_alarm_event)
                    _handler_types.append(("AlarmEvent", _on_alarm_event))
                    event_bus.register_handler("DeviceStatusEvent", _on_device_event)
                    _handler_types.append(("DeviceStatusEvent", _on_device_event))
                    event_bus.register_handler("PointUpdateEvent", _on_point_event)
                    _handler_types.append(("PointUpdateEvent", _on_point_event))

                while True:
                    try:
                        message = await _asyncio.wait_for(queue.get(), timeout=30)
                        yield f"event: message\ndata: {message}\n\n"
                    except TimeoutError:
                        yield f'event: ping\ndata: {{"timestamp": {_time.time()}}}\n\n'
            except _asyncio.CancelledError:
                pass
            finally:
                if event_bus:
                    for event_type, handler in _handler_types:
                        with contextlib.suppress(Exception):
                            event_bus.unregister_handler(event_type, handler)

        return _StreamingResp(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("mcp_sse failed: %s", e)
        raise HTTPException(status_code=500, detail=McpErrors.SSE_FAILED) from e  # FIXED: 原问题-硬编码错误码字符串，改为集中管理
