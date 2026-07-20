"""MCP (Model Context Protocol) API路由

提供AI助手与EdgeLite网关交互的标准协议接口。
业务逻辑委托给 mcp_service.py。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from edgelite.api.deps import (
    AlarmServiceDep,
    AuditServiceDep,
    DeviceServiceDep,
    EventBusDep,
    RuleServiceDep,
    SystemServiceDep,
    require_permission,
)
from edgelite.api.error_codes import AuthzErrors, McpErrors
from edgelite.models.common import ApiResponse
from edgelite.security.rbac import Permission
from edgelite.services.mcp_service import MCPAuthManager, MCPToolService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/mcp", tags=["MCP"])

_mcp_tools = MCPToolService()
_mcp_auth = MCPAuthManager()

# R11-API-05: SSE 短期 ticket 机制
# 原问题-JWT token 通过 URL Query 参数传递，会被代理日志/浏览器历史记录泄露
# 修复-新增 /sse-ticket 端点（需认证）签发 30 秒有效的一次性 ticket，SSE 端点改为消费 ticket
_sse_tickets: dict[str, float] = {}  # ticket -> 过期时间戳
_sse_tickets_lock = asyncio.Lock()
_SSE_TICKET_TTL = 30.0  # ticket 有效期 30 秒


async def _consume_sse_ticket(ticket: str) -> bool:
    """校验并消费（一次性使用）SSE ticket，有效返回 True。

    R11-API-05: 同时清理过期 ticket，防止内存无限增长。
    """
    now = time.time()
    async with _sse_tickets_lock:
        # 清理过期 ticket
        expired = [k for k, exp in _sse_tickets.items() if exp <= now]
        for k in expired:
            _sse_tickets.pop(k, None)
        exp = _sse_tickets.pop(ticket, None)
        if exp is None:
            return False
        return exp > now


class ToolCallRequest(BaseModel):
    name: str
    arguments: dict[str, Any] | None = None


@router.get("/tools", response_model=ApiResponse)
async def list_tools(_user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ))):
    try:
        return ApiResponse(data={"tools": list(_mcp_tools.tools.values())})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("list_tools failed: %s", e)
        raise HTTPException(
            status_code=500, detail=McpErrors.LIST_FAILED
        ) from e  # FIXED: 原问题-硬编码错误码字符串，改为集中管理


@router.post("/call", response_model=ApiResponse)
async def call_tool(
    req: ToolCallRequest,
    device_service: DeviceServiceDep,
    alarm_service: AlarmServiceDep,
    system_service: SystemServiceDep,
    rule_service: RuleServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
    audit_svc: AuditServiceDep = None,  # SEC-FIX: MCP 写入需记录审计日志
):
    try:
        errors = _mcp_tools.validate_tool_call(req.name, req.arguments)
        if errors:
            raise HTTPException(status_code=400, detail=McpErrors.MISSING_PARAMS)
        result = await _mcp_tools.call_tool(
            req.name,
            req.arguments or {},
            device_service=device_service,
            alarm_service=alarm_service,
            system_service=system_service,
            rule_service=rule_service,
            user=user,  # SEC-FIX: 传递 user 上下文用于写保护校验与审计
            audit_svc=audit_svc,
        )
        return ApiResponse(data=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("call_tool failed: %s", e)
        raise HTTPException(
            status_code=500, detail=McpErrors.CALL_FAILED
        ) from e  # FIXED: 原问题-硬编码错误码字符串，改为集中管理


@router.get("/resources", response_model=ApiResponse)
async def list_resources(_user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ))):
    try:
        return ApiResponse(data={"resources": list(_mcp_tools.resources.values())})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("list_resources failed: %s", e)
        raise HTTPException(
            status_code=500, detail=McpErrors.LIST_FAILED
        ) from e  # FIXED: 原问题-硬编码错误码字符串，改为集中管理


@router.get("/prompts", response_model=ApiResponse)
async def list_prompts(_user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ))):
    try:
        return ApiResponse(data={"prompts": list(_mcp_tools.prompts.values())})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("list_prompts failed: %s", e)
        raise HTTPException(
            status_code=500, detail=McpErrors.LIST_FAILED
        ) from e  # FIXED: 原问题-硬编码错误码字符串，改为集中管理


@router.get("/auth-keys", response_model=ApiResponse)
async def list_auth_keys(user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE))):
    try:
        return ApiResponse(data={"keys": _mcp_auth.list_keys(), "enabled": _mcp_auth.enabled})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("list_auth_keys failed: %s", e)
        raise HTTPException(
            status_code=500, detail=McpErrors.LIST_FAILED
        ) from e  # FIXED: 原问题-硬编码错误码字符串，改为集中管理


class CreateKeyRequest(BaseModel):
    # FIXED-P3: 原问题-name无长度限制，可传超长字符串; 修复-限制最长128字符
    name: str = Field(max_length=128)
    # FIXED-P3: 原问题-scopes无长度限制; 修复-限制最多50个scope
    scopes: list[str] = Field(default=[], max_length=50)


@router.post("/auth-keys", response_model=ApiResponse)
async def create_auth_key(
    req: CreateKeyRequest, user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE))
):
    try:
        result = _mcp_auth.create_key(req.name, req.scopes)
        return ApiResponse(data=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("create_auth_key failed: %s", e)
        raise HTTPException(
            status_code=500, detail=McpErrors.CREATE_KEY_FAILED
        ) from e  # FIXED: 原问题-硬编码错误码字符串，改为集中管理


@router.delete("/auth-keys/{key_id}", response_model=ApiResponse)
async def delete_auth_key(key_id: str, user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE))):
    if _mcp_auth.delete_key(key_id):
        return ApiResponse(data={"deleted": True, "key_id": key_id})
    raise HTTPException(
        status_code=404, detail=McpErrors.KEY_NOT_FOUND
    )  # FIXED: 原问题-硬编码错误码字符串，改为集中管理


@router.get("/sse-ticket", response_model=ApiResponse)
async def create_sse_ticket(
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    # R11-API-05: 签发短期一次性 SSE ticket，替代直接通过 Query 传递 JWT token
    # ticket 有效期 30 秒，一次性使用，避免 token 被代理日志/浏览器历史泄露
    ticket = secrets.token_urlsafe(32)
    now = time.time()
    async with _sse_tickets_lock:
        _sse_tickets[ticket] = now + _SSE_TICKET_TTL
    return ApiResponse(data={"ticket": ticket, "expires_in": int(_SSE_TICKET_TTL)})


@router.get("/sse")
async def mcp_sse(
    event_bus: EventBusDep,
    request: Request = None,  # type: ignore[assignment]
    ticket: str | None = Query(None),  # R11-API-05: 改为接收短期 ticket，避免 JWT 通过 URL 泄露
):
    # R11-API-05: 优先校验一次性 ticket；保留 Authorization header 作为备选认证方式
    user = None

    # 路径一：短期 ticket 认证（推荐）
    if ticket:
        if not await _consume_sse_ticket(ticket):
            from fastapi import status as _st

            raise HTTPException(status_code=_st.HTTP_401_UNAUTHORIZED, detail=AuthzErrors.NOT_AUTHENTICATED)
        # ticket 有效即放行（ticket 由已认证用户通过 /sse-ticket 换取）
        user = {"user_id": "sse-ticket", "username": "sse-ticket", "role": "system"}

    # 路径二：Authorization header 备选认证（向后兼容）
    if user is None:
        auth_header = request.headers.get("Authorization", "") if request else ""
        if auth_header.startswith("Bearer "):
            from jwt import PyJWTError as JWTError

            from edgelite.security.jwt import verify_token

            try:
                bearer_token = auth_header[7:]
                payload = verify_token(bearer_token, token_type="access")
                # FIXED-P1: 添加Token撤销检查和用户禁用检查，与deps.py:get_current_user一致
                jti = payload.get("jti", "")
                if jti:
                    from edgelite.security.token_revocation import is_token_revoked

                    if is_token_revoked(jti):
                        raise JWTError("Token revoked")
                username = payload.get("username", "")
                container = request.app.state
                from edgelite.storage.sqlite_repo import UserRepo

                async with container.database.get_session() as session:
                    repo = UserRepo(session, container.database.write_lock)
                    user = await repo.get_by_username(username)
                if user is None or not user.get("enabled"):  # FIXED-P2: 合并None和禁用检查
                    user = None
            except JWTError as e:  # FIXED-P2: 移除冗余的Exception捕获，JWTError已覆盖token相关错误
                logger.warning("MCP SSE token verification failed: %s", e)
            except Exception as e:  # FIXED-P2: 仅捕获非JWT的其他异常（如DB访问异常）
                logger.warning("MCP SSE token verification unexpected error: %s", e)

    if user is None:
        from fastapi import status as _st

        raise HTTPException(status_code=_st.HTTP_401_UNAUTHORIZED, detail=AuthzErrors.NOT_AUTHENTICATED)

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
                            logger.debug(
                                "MCP alarm event queue full, dropping"
                            )  # FIXED-P1: 原问题-队列满静默丢弃无日志

                    def _on_device_event(event):
                        try:
                            data = {
                                "type": "device",
                                "device_id": getattr(event, "device_id", ""),
                                "new_status": getattr(event, "new_status", ""),
                            }
                            queue.put_nowait(json.dumps(data, default=str, ensure_ascii=False))
                        except _asyncio.QueueFull:
                            logger.debug("MCP device event queue full, dropping")  # FIXED-P1

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
                            logger.debug("MCP point event queue full, dropping")  # FIXED-P1

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
        raise HTTPException(
            status_code=500, detail=McpErrors.SSE_FAILED
        ) from e  # FIXED: 原问题-硬编码错误码字符串，改为集中管理
