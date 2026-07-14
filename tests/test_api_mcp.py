"""MCP (Model Context Protocol) API 路由测试

覆盖 src/edgelite/api/mcp.py：
- _consume_sse_ticket: SSE 一次性 ticket 校验
- GET /api/v1/mcp/tools: 列出 MCP 工具
- POST /api/v1/mcp/call: 调用工具（含参数校验/异常分支）
- GET /api/v1/mcp/resources: 列出资源
- GET /api/v1/mcp/prompts: 列出提示
- GET /api/v1/mcp/auth-keys: 列出 API 密钥
- POST /api/v1/mcp/auth-keys: 创建 API 密钥
- DELETE /api/v1/mcp/auth-keys/{key_id}: 删除 API 密钥
- GET /api/v1/mcp/sse-ticket: 签发 SSE ticket
- GET /api/v1/mcp/sse: SSE 推送（ticket/header 认证、事件循环）
"""

from __future__ import annotations

import asyncio
import sys
import time
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

sys.path.insert(0, "src")

from httpx import ASGITransport, AsyncClient

from edgelite.api import mcp as mcp_module
from edgelite.api.error_codes import AuthzErrors, McpErrors
from edgelite.api.mcp import _consume_sse_ticket, _sse_tickets, _sse_tickets_lock, router


# -- helpers --


def _services_for_mcp() -> dict:
    """构建 MCP 端点所需 app.state 服务字典"""
    from conftest import make_mock_audit_service

    return {
        "device_service": AsyncMock(),
        "alarm_service": AsyncMock(),
        "system_service": AsyncMock(),
        "rule_service": AsyncMock(),
        "audit_service": make_mock_audit_service(),
        "event_bus": MagicMock(),
        "database": MagicMock(),
    }


@pytest.fixture
async def client():
    from conftest import make_app

    app = make_app(router, role="admin", services=_services_for_mcp())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c, app


@pytest.fixture(autouse=True)
def _clear_sse_tickets():
    """每个测试前清空全局 SSE ticket 字典，避免跨测试污染"""
    _sse_tickets.clear()
    yield
    _sse_tickets.clear()


# -- _consume_sse_ticket --


class TestConsumeSseTicket:
    async def test_invalid_ticket_returns_false(self):
        result = await _consume_sse_ticket("nonexistent")
        assert result is False

    async def test_valid_ticket_returns_true_and_consumed(self):
        ticket = "valid-ticket"
        now = time.time()
        _sse_tickets[ticket] = now + 30.0
        result = await _consume_sse_ticket(ticket)
        assert result is True
        # 一次性使用：第二次应失败
        result2 = await _consume_sse_ticket(ticket)
        assert result2 is False

    async def test_expired_ticket_returns_false_and_cleaned(self):
        ticket = "expired-ticket"
        _sse_tickets[ticket] = time.time() - 1.0
        result = await _consume_sse_ticket(ticket)
        assert result is False
        # 过期 ticket 应被清理
        assert ticket not in _sse_tickets

    async def test_expired_tickets_cleaned_during_validation(self):
        """校验时同时清理其他过期 ticket"""
        _sse_tickets["valid"] = time.time() + 30.0
        _sse_tickets["expired-1"] = time.time() - 1.0
        _sse_tickets["expired-2"] = time.time() - 2.0
        await _consume_sse_ticket("valid")
        assert "expired-1" not in _sse_tickets
        assert "expired-2" not in _sse_tickets
        assert "valid" not in _sse_tickets  # valid 被消费


# -- GET /api/v1/mcp/tools --


class TestListTools:
    async def test_returns_tools_list(self, client):
        c, _ = client
        resp = await c.get("/api/v1/mcp/tools")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "tools" in data
        assert isinstance(data["tools"], list)
        assert len(data["tools"]) > 0

    async def test_returns_500_on_exception(self, client):
        from edgelite.services.mcp_service import MCPToolService

        c, _ = client
        with patch.object(MCPToolService, "tools", new_callable=PropertyMock) as mock_tools:
            mock_tools.side_effect = RuntimeError("boom")
            resp = await c.get("/api/v1/mcp/tools")
        assert resp.status_code == 500
        assert resp.json()["detail"] == McpErrors.LIST_FAILED


# -- GET /api/v1/mcp/resources --


class TestListResources:
    async def test_returns_resources_list(self, client):
        c, _ = client
        resp = await c.get("/api/v1/mcp/resources")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "resources" in data
        assert isinstance(data["resources"], list)

    async def test_returns_500_on_exception(self, client):
        from edgelite.services.mcp_service import MCPToolService

        c, _ = client
        with patch.object(MCPToolService, "resources", new_callable=PropertyMock) as mock_res:
            mock_res.side_effect = RuntimeError("boom")
            resp = await c.get("/api/v1/mcp/resources")
        assert resp.status_code == 500
        assert resp.json()["detail"] == McpErrors.LIST_FAILED


# -- GET /api/v1/mcp/prompts --


class TestListPrompts:
    async def test_returns_prompts_list(self, client):
        c, _ = client
        resp = await c.get("/api/v1/mcp/prompts")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "prompts" in data
        assert isinstance(data["prompts"], list)

    async def test_returns_500_on_exception(self, client):
        from edgelite.services.mcp_service import MCPToolService

        c, _ = client
        with patch.object(MCPToolService, "prompts", new_callable=PropertyMock) as mock_prompts:
            mock_prompts.side_effect = RuntimeError("boom")
            resp = await c.get("/api/v1/mcp/prompts")
        assert resp.status_code == 500
        assert resp.json()["detail"] == McpErrors.LIST_FAILED


# -- POST /api/v1/mcp/call --


class TestCallTool:
    async def test_call_tool_success(self, client):
        c, _ = client
        with patch.object(mcp_module._mcp_tools, "call_tool", new=AsyncMock(return_value={"ok": True})):
            resp = await c.post(
                "/api/v1/mcp/call",
                json={"name": "list_devices", "arguments": {}},
            )
        assert resp.status_code == 200
        assert resp.json()["data"] == {"ok": True}

    async def test_call_tool_with_none_arguments(self, client):
        """arguments 为 None 时应正常调用"""
        c, _ = client
        mock_call = AsyncMock(return_value={"ok": True})
        with patch.object(mcp_module._mcp_tools, "call_tool", new=mock_call):
            resp = await c.post(
                "/api/v1/mcp/call",
                json={"name": "list_devices", "arguments": None},
            )
        assert resp.status_code == 200
        # 验证调用时 arguments 被转为 {}
        _, kwargs = mock_call.call_args
        assert kwargs.get("arguments", {}) == {}

    async def test_call_tool_validation_errors_returns_400(self, client):
        """validate_tool_call 返回错误时应返回 400 MISSING_PARAMS"""
        c, _ = client
        with patch.object(mcp_module._mcp_tools, "validate_tool_call", return_value=["Missing required parameter: device_id"]):
            resp = await c.post(
                "/api/v1/mcp/call",
                json={"name": "get_device_status", "arguments": {}},
            )
        assert resp.status_code == 400
        assert resp.json()["detail"] == McpErrors.MISSING_PARAMS

    async def test_call_tool_unknown_tool_returns_400(self, client):
        """未知工具时 validate_tool_call 抛 HTTPException(400)"""
        c, _ = client
        resp = await c.post(
            "/api/v1/mcp/call",
            json={"name": "nonexistent_tool", "arguments": {}},
        )
        # validate_tool_call 内部抛 HTTPException(400, UNKNOWN_TOOL)
        assert resp.status_code == 400
        assert resp.json()["detail"] == McpErrors.UNKNOWN_TOOL

    async def test_call_tool_raises_http_exception_passthrough(self, client):
        """call_tool 抛 HTTPException 应直接透传"""
        from fastapi import HTTPException

        c, _ = client
        mock_call = AsyncMock(side_effect=HTTPException(status_code=404, detail="custom"))
        with patch.object(mcp_module._mcp_tools, "call_tool", new=mock_call):
            resp = await c.post(
                "/api/v1/mcp/call",
                json={"name": "list_devices", "arguments": {}},
            )
        assert resp.status_code == 404
        assert resp.json()["detail"] == "custom"

    async def test_call_tool_unexpected_exception_returns_500(self, client):
        """call_tool 抛非 HTTP 异常应返回 500 CALL_FAILED"""
        c, _ = client
        mock_call = AsyncMock(side_effect=RuntimeError("boom"))
        with patch.object(mcp_module._mcp_tools, "call_tool", new=mock_call):
            resp = await c.post(
                "/api/v1/mcp/call",
                json={"name": "list_devices", "arguments": {}},
            )
        assert resp.status_code == 500
        assert resp.json()["detail"] == McpErrors.CALL_FAILED


# -- GET /api/v1/mcp/auth-keys --


class TestListAuthKeys:
    async def test_returns_keys_and_enabled(self, client):
        from edgelite.services.mcp_service import MCPAuthManager

        c, _ = client
        with (
            patch.object(mcp_module._mcp_auth, "list_keys", return_value=[{"id": "k1", "name": "key1"}]),
            patch.object(MCPAuthManager, "enabled", new_callable=PropertyMock, return_value=True),
        ):
            resp = await c.get("/api/v1/mcp/auth-keys")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["keys"] == [{"id": "k1", "name": "key1"}]
        assert data["enabled"] is True

    async def test_returns_500_on_exception(self, client):
        c, _ = client
        with patch.object(mcp_module._mcp_auth, "list_keys", side_effect=RuntimeError("boom")):
            resp = await c.get("/api/v1/mcp/auth-keys")
        assert resp.status_code == 500
        assert resp.json()["detail"] == McpErrors.LIST_FAILED


# -- POST /api/v1/mcp/auth-keys --


class TestCreateAuthKey:
    async def test_create_key_success(self, client):
        c, _ = client
        expected = {"id": "abc123", "key": "mcp_xxx", "name": "test-key"}
        with patch.object(mcp_module._mcp_auth, "create_key", return_value=expected):
            resp = await c.post(
                "/api/v1/mcp/auth-keys",
                json={"name": "test-key", "scopes": ["read"]},
            )
        assert resp.status_code == 200
        assert resp.json()["data"] == expected

    async def test_create_key_returns_500_on_exception(self, client):
        c, _ = client
        with patch.object(mcp_module._mcp_auth, "create_key", side_effect=RuntimeError("boom")):
            resp = await c.post(
                "/api/v1/mcp/auth-keys",
                json={"name": "test-key", "scopes": []},
            )
        assert resp.status_code == 500
        assert resp.json()["detail"] == McpErrors.CREATE_KEY_FAILED

    async def test_create_key_name_too_long_rejected(self, client):
        """name 超过 128 字符应被 Pydantic 校验拒绝（422）"""
        c, _ = client
        resp = await c.post(
            "/api/v1/mcp/auth-keys",
            json={"name": "a" * 129, "scopes": []},
        )
        assert resp.status_code == 422

    async def test_create_key_scopes_too_many_rejected(self, client):
        """scopes 超过 50 个应被 Pydantic 校验拒绝（422）"""
        c, _ = client
        resp = await c.post(
            "/api/v1/mcp/auth-keys",
            json={"name": "k", "scopes": ["s" + str(i) for i in range(51)]},
        )
        assert resp.status_code == 422


# -- DELETE /api/v1/mcp/auth-keys/{key_id} --


class TestDeleteAuthKey:
    async def test_delete_existing_key_returns_200(self, client):
        c, _ = client
        with patch.object(mcp_module._mcp_auth, "delete_key", return_value=True):
            resp = await c.delete("/api/v1/mcp/auth-keys/abc123")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["deleted"] is True
        assert data["key_id"] == "abc123"

    async def test_delete_nonexistent_key_returns_404(self, client):
        c, _ = client
        with patch.object(mcp_module._mcp_auth, "delete_key", return_value=False):
            resp = await c.delete("/api/v1/mcp/auth-keys/nonexistent")
        assert resp.status_code == 404
        assert resp.json()["detail"] == McpErrors.KEY_NOT_FOUND


# -- GET /api/v1/mcp/sse-ticket --


class TestCreateSseTicket:
    async def test_returns_ticket_with_expiry(self, client):
        c, _ = client
        resp = await c.get("/api/v1/mcp/sse-ticket")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "ticket" in data
        assert isinstance(data["ticket"], str)
        assert data["expires_in"] == 30

    async def test_ticket_stored_in_global_dict(self, client):
        c, _ = client
        resp = await c.get("/api/v1/mcp/sse-ticket")
        ticket = resp.json()["data"]["ticket"]
        assert ticket in _sse_tickets
        # 过期时间应为未来
        assert _sse_tickets[ticket] > time.time()


# -- GET /api/v1/mcp/sse --


class TestMcpSse:
    async def test_no_auth_returns_401(self, client):
        """无 ticket 且无 Authorization header 应返回 401"""
        c, _ = client
        resp = await c.get("/api/v1/mcp/sse")
        assert resp.status_code == 401
        assert resp.json()["detail"] == AuthzErrors.NOT_AUTHENTICATED

    async def test_invalid_ticket_returns_401(self, client):
        """无效 ticket 应返回 401"""
        c, _ = client
        resp = await c.get("/api/v1/mcp/sse?ticket=invalid")
        assert resp.status_code == 401
        assert resp.json()["detail"] == AuthzErrors.NOT_AUTHENTICATED

    async def test_valid_ticket_starts_stream(self, client):
        """有效 ticket 应建立 SSE 流并消费 ticket；直接调用 mcp_sse 避免 ASGITransport 流阻塞"""
        from edgelite.api.mcp import mcp_sse

        c, _ = client
        ticket_resp = await c.get("/api/v1/mcp/sse-ticket")
        ticket = ticket_resp.json()["data"]["ticket"]

        # 直接调用端点函数（ASGITransport 不支持无限流式响应）
        response = await mcp_sse(event_bus=None, request=None, ticket=ticket)
        assert response.status_code == 200
        assert response.media_type == "text/event-stream"
        # ticket 应被消费（一次性）
        assert ticket not in _sse_tickets
        # 清理生成器
        await response.body_iterator.aclose()

    async def test_bearer_token_invalid_returns_401(self, client):
        """Authorization Bearer token 无效应返回 401"""
        c, _ = client
        resp = await c.get("/api/v1/mcp/sse", headers={"Authorization": "Bearer invalid-token"})
        assert resp.status_code == 401
        assert resp.json()["detail"] == AuthzErrors.NOT_AUTHENTICATED

    async def test_bearer_token_revoked_returns_401(self, client):
        """已撤销的 token 应返回 401"""
        c, _ = client
        with (
            patch("edgelite.security.jwt.verify_token", return_value={"jti": "jti1", "username": "u"}),
            patch("edgelite.security.token_revocation.is_token_revoked", return_value=True),
        ):
            resp = await c.get("/api/v1/mcp/sse", headers={"Authorization": "Bearer fake-token"})
        assert resp.status_code == 401
        assert resp.json()["detail"] == AuthzErrors.NOT_AUTHENTICATED

    async def test_bearer_token_user_not_found_returns_401(self, client):
        """token 有效但用户不存在应返回 401"""
        c, _ = client

        async def fake_get_by_username(username):
            return None

        mock_session = AsyncMock()
        mock_repo = MagicMock()
        mock_repo.get_by_username = AsyncMock(return_value=None)

        mock_db = MagicMock()

        class _CM:
            async def __aenter__(self):
                return mock_session

            async def __aexit__(self, *args):
                return False

        mock_db.get_session.return_value = _CM()

        c, app = client
        app.state.database = mock_db

        with (
            patch("edgelite.security.jwt.verify_token", return_value={"jti": "jti1", "username": "ghost"}),
            patch("edgelite.security.token_revocation.is_token_revoked", return_value=False),
            patch("edgelite.storage.sqlite_repo.UserRepo", return_value=mock_repo),
        ):
            resp = await c.get("/api/v1/mcp/sse", headers={"Authorization": "Bearer fake-token"})
        assert resp.status_code == 401
        assert resp.json()["detail"] == AuthzErrors.NOT_AUTHENTICATED

    async def test_bearer_token_disabled_user_returns_401(self, client):
        """token 有效但用户被禁用应返回 401"""
        c, app = client
        mock_repo = MagicMock()
        mock_repo.get_by_username = AsyncMock(return_value={"enabled": False})

        mock_session = AsyncMock()

        class _CM:
            async def __aenter__(self):
                return mock_session

            async def __aexit__(self, *args):
                return False

        mock_db = MagicMock()
        mock_db.get_session.return_value = _CM()
        app.state.database = mock_db

        with (
            patch("edgelite.security.jwt.verify_token", return_value={"jti": "jti1", "username": "disabled"}),
            patch("edgelite.security.token_revocation.is_token_revoked", return_value=False),
            patch("edgelite.storage.sqlite_repo.UserRepo", return_value=mock_repo),
        ):
            resp = await c.get("/api/v1/mcp/sse", headers={"Authorization": "Bearer fake-token"})
        assert resp.status_code == 401

    async def test_bearer_token_db_exception_returns_401(self, client):
        """token 有效但 DB 异常应返回 401（降级为未认证）"""
        c, app = client
        mock_db = MagicMock()
        mock_db.get_session.side_effect = RuntimeError("db down")
        app.state.database = mock_db

        with (
            patch("edgelite.security.jwt.verify_token", return_value={"jti": "jti1", "username": "u"}),
            patch("edgelite.security.token_revocation.is_token_revoked", return_value=False),
        ):
            resp = await c.get("/api/v1/mcp/sse", headers={"Authorization": "Bearer fake-token"})
        assert resp.status_code == 401

class TestMcpSseStream:
    """SSE 流内容测试 - 验证 connected 事件、headers、event_bus 处理器注册

    直接调用 mcp_sse 端点函数，因为 ASGITransport 不支持无限流式响应
    （会阻塞等待生成器结束，导致超时）。
    """

    async def test_stream_emits_connected_event(self):
        """SSE 流应首先发送 connected 事件"""
        from edgelite.api.mcp import mcp_sse

        # 先签发 ticket
        ticket = "test-connected-ticket"
        _sse_tickets[ticket] = time.time() + 30.0

        response = await mcp_sse(event_bus=None, request=None, ticket=ticket)
        assert response.status_code == 200

        # 迭代生成器获取首个 chunk
        generator = response.body_iterator
        first_chunk = await generator.__anext__()
        assert "event: connected" in first_chunk
        assert "edgelite-mcp" in first_chunk
        await generator.aclose()

    async def test_stream_headers_correct(self):
        """SSE 响应应包含正确的 headers"""
        from edgelite.api.mcp import mcp_sse

        ticket = "test-headers-ticket"
        _sse_tickets[ticket] = time.time() + 30.0

        response = await mcp_sse(event_bus=None, request=None, ticket=ticket)
        assert response.media_type == "text/event-stream"
        headers = dict(response.headers)
        assert headers.get("cache-control") == "no-cache"
        assert headers.get("connection") == "keep-alive"
        assert headers.get("x-accel-buffering") == "no"
        await response.body_iterator.aclose()

    async def test_stream_with_event_bus_registers_handlers(self):
        """有 event_bus 时应注册并注销事件处理器"""
        from edgelite.api.mcp import mcp_sse

        mock_bus = MagicMock()
        mock_bus.register_handler = MagicMock()
        mock_bus.unregister_handler = MagicMock()

        ticket = "test-bus-ticket"
        _sse_tickets[ticket] = time.time() + 30.0

        response = await mcp_sse(event_bus=mock_bus, request=None, ticket=ticket)
        generator = response.body_iterator

        # 首个 yield 是 connected 事件
        first = await generator.__anext__()
        assert "connected" in first

        # 再次迭代触发 handler 注册（生成器从首个 yield 后继续执行）
        # 使用 wait_for + 短超时让生成器运行到 queue.get() 阻塞处
        # 此时 handler 已注册完成
        try:
            await asyncio.wait_for(generator.__anext__(), timeout=0.1)
        except (asyncio.TimeoutError, StopAsyncIteration):
            pass

        # 应注册 3 个事件处理器
        assert mock_bus.register_handler.call_count == 3

        # 关闭生成器触发 finally 块注销 handler
        await generator.aclose()

        # 应注销 3 个事件处理器
        assert mock_bus.unregister_handler.call_count == 3
