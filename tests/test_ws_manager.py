"""WebSocket 连接管理器单元测试。

覆盖 src/edgelite/ws/manager.py：连接认证（Origin/token/首帧）、连接数限制、
断开清理、广播（全量/按角色过滤/未认证跳过/发送失败清理）、心跳检测、
连接计数、close 统一关闭。
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from edgelite.ws.manager import ConnectionManager


class MockWebSocket:
    """模拟 FastAPI WebSocket，支持 accept/close/send_json/send_text/headers。"""

    def __init__(self, headers: dict | None = None, origin: str = ""):
        self._headers = headers or {}
        if origin:
            self._headers["origin"] = origin
        self.accepted = False
        self.closed = False
        self.close_code = None
        self.close_reason = None
        self.sent_messages: list = []

    @property
    def headers(self):
        return self._headers

    async def accept(self):
        self.accepted = True

    async def close(self, code: int = 1000, reason: str = ""):
        self.closed = True
        self.close_code = code
        self.close_reason = reason

    async def send_json(self, data):
        self.sent_messages.append(("json", data))

    async def send_text(self, text):
        self.sent_messages.append(("text", text))


# ─── Origin 校验 ───


def test_validate_origin_none_whitelist_allows_all():
    """未设置 Origin 白名单时全部放行。"""
    mgr = ConnectionManager()
    ws = MockWebSocket(origin="http://evil.com")
    assert mgr.validate_origin(ws) is True


def test_validate_origin_in_whitelist():
    """Origin 在白名单中放行。"""
    mgr = ConnectionManager()
    mgr.set_allowed_origins(["http://localhost:3000", "https://app.example.com"])
    ws = MockWebSocket(origin="http://localhost:3000")
    assert mgr.validate_origin(ws) is True


def test_validate_origin_not_in_whitelist():
    """Origin 不在白名单中拒绝。"""
    mgr = ConnectionManager()
    mgr.set_allowed_origins(["http://localhost:3000"])
    ws = MockWebSocket(origin="http://evil.com")
    assert mgr.validate_origin(ws) is False


def test_validate_origin_no_origin_header_allows_non_browser():
    """无 Origin 头（非浏览器客户端）放行。"""
    mgr = ConnectionManager()
    mgr.set_allowed_origins(["http://localhost:3000"])
    ws = MockWebSocket(headers={})  # 无 origin
    assert mgr.validate_origin(ws) is True


def test_validate_origin_empty_whitelist_allows_all():
    """空白名单列表等同于不校验。"""
    mgr = ConnectionManager()
    mgr.set_allowed_origins([])
    ws = MockWebSocket(origin="http://evil.com")
    assert mgr.validate_origin(ws) is True


# ─── connect (token 路径) ───


@pytest.mark.asyncio
async def test_connect_valid_token():
    """有效 token 连接成功。"""
    mgr = ConnectionManager(max_connections=10)
    ws = MockWebSocket()
    payload = {"sub": "user1", "role": "admin", "username": "admin"}
    with patch("edgelite.ws.manager.verify_token", return_value=payload):
        result = await mgr.connect(ws, "realtime", token="valid-token")
    assert result is True
    assert ws.accepted is True
    # 连接应被追踪
    count = await mgr.get_connection_count_async("realtime")
    assert count == 1
    # 元数据应包含用户身份
    meta = mgr._conn_meta[ws]
    assert meta["user_id"] == "user1"
    assert meta["role"] == "admin"
    assert meta["authenticated"] is True


@pytest.mark.asyncio
async def test_connect_invalid_token_rejected():
    """无效 token 连接被拒绝。"""
    mgr = ConnectionManager(max_connections=10)
    ws = MockWebSocket()
    with patch("edgelite.ws.manager.verify_token", side_effect=Exception("invalid")):
        result = await mgr.connect(ws, "realtime", token="bad-token")
    assert result is False
    assert ws.closed is True
    assert ws.close_code == 4001
    count = await mgr.get_connection_count_async("realtime")
    assert count == 0


@pytest.mark.asyncio
async def test_connect_origin_rejected():
    """Origin 不在白名单中连接被拒绝。"""
    mgr = ConnectionManager(max_connections=10)
    mgr.set_allowed_origins(["http://localhost:3000"])
    ws = MockWebSocket(origin="http://evil.com")
    result = await mgr.connect(ws, "realtime", token="valid-token")
    assert result is False
    assert ws.closed is True
    assert ws.close_code == 4003


@pytest.mark.asyncio
async def test_connect_max_connections_reached():
    """达到最大连接数时拒绝新连接。"""
    mgr = ConnectionManager(max_connections=1)
    ws1 = MockWebSocket()
    ws2 = MockWebSocket()
    payload = {"sub": "user1", "role": "admin", "username": "admin"}
    with patch("edgelite.ws.manager.verify_token", return_value=payload):
        assert await mgr.connect(ws1, "ch", token="t1") is True
        assert await mgr.connect(ws2, "ch", token="t2") is False
    assert ws2.close_code == 1013


# ─── connect (token=None 路径 — 首帧认证) ───


@pytest.mark.asyncio
async def test_connect_no_token_accepts_pending_auth():
    """token=None 时 accept 并标记 authenticated=False，等待首帧认证。"""
    mgr = ConnectionManager(max_connections=10)
    ws = MockWebSocket()
    result = await mgr.connect(ws, "realtime", token=None)
    assert result is True
    assert ws.accepted is True
    meta = mgr._conn_meta[ws]
    assert meta["authenticated"] is False


@pytest.mark.asyncio
async def test_authenticate_success_sets_authenticated():
    """authenticate 成功后标记 authenticated=True 并绑定用户身份。"""
    mgr = ConnectionManager(max_connections=10)
    ws = MockWebSocket()
    await mgr.connect(ws, "realtime", token=None)
    payload = {"sub": "user1", "role": "operator", "username": "op1"}
    with patch("edgelite.ws.manager.verify_token", return_value=payload):
        result = await mgr.authenticate(ws, "realtime", "valid-token")
    assert result is True
    meta = mgr._conn_meta[ws]
    assert meta["authenticated"] is True
    assert meta["role"] == "operator"
    # 应发送 auth ok 消息
    assert any(msg[0] == "json" and msg[1].get("status") == "ok" for msg in ws.sent_messages)


@pytest.mark.asyncio
async def test_authenticate_failure_returns_false():
    """authenticate 失败返回 False 并关闭连接。"""
    mgr = ConnectionManager(max_connections=10)
    ws = MockWebSocket()
    await mgr.connect(ws, "realtime", token=None)
    with patch("edgelite.ws.manager.verify_token", side_effect=Exception("invalid")):
        result = await mgr.authenticate(ws, "realtime", "bad-token")
    assert result is False
    assert ws.closed is True
    assert ws.close_code == 4001


# ─── disconnect ───


@pytest.mark.asyncio
async def test_disconnect_removes_connection():
    """disconnect 从连接表中移除连接并清理元数据。"""
    mgr = ConnectionManager(max_connections=10)
    ws = MockWebSocket()
    payload = {"sub": "user1", "role": "admin", "username": "admin"}
    with patch("edgelite.ws.manager.verify_token", return_value=payload):
        await mgr.connect(ws, "realtime", token="valid-token")
    assert await mgr.get_connection_count_async("realtime") == 1
    await mgr.disconnect(ws, "realtime")
    assert await mgr.get_connection_count_async("realtime") == 0
    assert ws not in mgr._conn_meta
    assert ws not in mgr._send_locks
    assert ws not in mgr._last_pong


@pytest.mark.asyncio
async def test_disconnect_nonexistent_channel_no_error():
    """disconnect 不存在的频道不抛错。"""
    mgr = ConnectionManager()
    ws = MockWebSocket()
    await mgr.disconnect(ws, "nonexistent")


# ─── broadcast ───


@pytest.mark.asyncio
async def test_broadcast_to_all_authenticated():
    """broadcast 向频道内所有已认证连接发送消息。"""
    mgr = ConnectionManager(max_connections=10)
    ws1, ws2 = MockWebSocket(), MockWebSocket()
    payload = {"sub": "u1", "role": "admin", "username": "a1"}
    with patch("edgelite.ws.manager.verify_token", return_value=payload):
        await mgr.connect(ws1, "alarm", token="t1")
        await mgr.connect(ws2, "alarm", token="t2")
    await mgr.broadcast("alarm", {"type": "alert", "msg": "fire"})
    assert any(msg[0] == "text" for msg in ws1.sent_messages)
    assert any(msg[0] == "text" for msg in ws2.sent_messages)


@pytest.mark.asyncio
async def test_broadcast_skips_unauthenticated():
    """broadcast 跳过未认证连接（authenticated=False）。"""
    mgr = ConnectionManager(max_connections=10)
    ws_authed = MockWebSocket()
    ws_pending = MockWebSocket()
    payload = {"sub": "u1", "role": "admin", "username": "a1"}
    with patch("edgelite.ws.manager.verify_token", return_value=payload):
        await mgr.connect(ws_authed, "alarm", token="t1")
    await mgr.connect(ws_pending, "alarm", token=None)  # 未认证
    await mgr.broadcast("alarm", {"type": "alert"})
    assert len(ws_authed.sent_messages) > 0
    assert len(ws_pending.sent_messages) == 0


@pytest.mark.asyncio
async def test_broadcast_with_role_filter():
    """broadcast filter_fn 按角色过滤连接。"""
    mgr = ConnectionManager(max_connections=10)
    ws_admin = MockWebSocket()
    ws_viewer = MockWebSocket()
    with patch(
        "edgelite.ws.manager.verify_token",
        side_effect=[
            {"sub": "u1", "role": "admin", "username": "a"},
            {"sub": "u2", "role": "viewer", "username": "v"},
        ],
    ):
        await mgr.connect(ws_admin, "ch", token="t1")
        await mgr.connect(ws_viewer, "ch", token="t2")
    await mgr.broadcast("ch", {"type": "secret"}, filter_fn=lambda meta: meta.get("role") == "admin")
    assert len(ws_admin.sent_messages) > 0
    assert len(ws_viewer.sent_messages) == 0


@pytest.mark.asyncio
async def test_broadcast_empty_channel_no_error():
    """broadcast 空频道不抛错。"""
    mgr = ConnectionManager()
    await mgr.broadcast("nonexistent", {"type": "test"})


@pytest.mark.asyncio
async def test_broadcast_send_failure_disconnects():
    """broadcast 发送失败的连接被关闭并清理。"""
    mgr = ConnectionManager(max_connections=10)
    ws_good = MockWebSocket()
    ws_bad = MockWebSocket()

    async def fail_send(text):
        raise RuntimeError("connection lost")

    ws_bad.send_text = fail_send
    payload = {"sub": "u1", "role": "admin", "username": "a"}
    with patch("edgelite.ws.manager.verify_token", return_value=payload):
        await mgr.connect(ws_good, "ch", token="t1")
        await mgr.connect(ws_bad, "ch", token="t2")
    await mgr.broadcast("ch", {"type": "test"})
    assert ws_bad.closed is True
    assert await mgr.get_connection_count_async("ch") == 1


# ─── 连接计数 ───


@pytest.mark.asyncio
async def test_get_total_connection_count():
    """get_total_connection_count 返回所有频道总连接数。"""
    mgr = ConnectionManager(max_connections=10)
    payload = {"sub": "u1", "role": "admin", "username": "a"}
    with patch("edgelite.ws.manager.verify_token", return_value=payload):
        await mgr.connect(MockWebSocket(), "ch1", token="t1")
        await mgr.connect(MockWebSocket(), "ch2", token="t2")
        await mgr.connect(MockWebSocket(), "ch1", token="t3")
    total = await mgr.get_total_connection_count()
    assert total == 3


# ─── record_pong ───


def test_record_pong_updates_timestamp():
    """record_pong 更新连接的最后 pong 时间。"""
    mgr = ConnectionManager()
    ws = MockWebSocket()
    mgr._last_pong[ws] = 0.0
    mgr.record_pong(ws)
    assert mgr._last_pong[ws] > 0.0


# ─── close ───


@pytest.mark.asyncio
async def test_close_disconnects_all_and_stops_heartbeat():
    """close 关闭所有连接、清理连接表、停止心跳。"""
    mgr = ConnectionManager(max_connections=10)
    ws1, ws2 = MockWebSocket(), MockWebSocket()
    payload = {"sub": "u1", "role": "admin", "username": "a"}
    with patch("edgelite.ws.manager.verify_token", return_value=payload):
        await mgr.connect(ws1, "ch1", token="t1")
        await mgr.connect(ws2, "ch2", token="t2")
    await mgr.close()
    assert ws1.closed is True
    assert ws2.closed is True
    assert await mgr.get_total_connection_count() == 0


@pytest.mark.asyncio
async def test_close_no_connections_no_error():
    """close 无连接时不抛错。"""
    mgr = ConnectionManager()
    await mgr.close()


# ─── set_allowed_origins ───


def test_set_allowed_origins_none():
    """set_allowed_origins(None) 禁用校验。"""
    mgr = ConnectionManager()
    mgr.set_allowed_origins(None)
    assert mgr._allowed_origins is None


def test_set_allowed_origins_list():
    """set_allowed_origins(list) 设置白名单。"""
    mgr = ConnectionManager()
    mgr.set_allowed_origins(["http://a.com", "http://b.com"])
    assert mgr._allowed_origins == {"http://a.com", "http://b.com"}


# ─── get_connection_count (deprecated sync) ───


@pytest.mark.asyncio
async def test_get_connection_count_deprecated():
    """get_connection_count (同步) 返回近似值。"""
    mgr = ConnectionManager(max_connections=10)
    ws = MockWebSocket()
    payload = {"sub": "u1", "role": "admin", "username": "a"}
    with patch("edgelite.ws.manager.verify_token", return_value=payload):
        await mgr.connect(ws, "ch", token="t1")
    count = mgr.get_connection_count("ch")
    assert count == 1


# ─── 心跳 ───


@pytest.mark.asyncio
async def test_start_stop_heartbeat():
    """start_heartbeat / stop_heartbeat 正常启动和停止。"""
    mgr = ConnectionManager()
    mgr._heartbeat_interval = 0.01  # 加速测试
    await mgr.start_heartbeat()
    assert mgr._heartbeat_task is not None
    assert not mgr._heartbeat_task.done()
    await mgr.stop_heartbeat()
    assert mgr._heartbeat_task is None


@pytest.mark.asyncio
async def test_start_heartbeat_idempotent():
    """重复 start_heartbeat 不创建多个任务。"""
    mgr = ConnectionManager()
    mgr._heartbeat_interval = 0.01
    await mgr.start_heartbeat()
    task1 = mgr._heartbeat_task
    await mgr.start_heartbeat()
    assert mgr._heartbeat_task is task1
    await mgr.stop_heartbeat()


@pytest.mark.asyncio
async def test_stop_heartbeat_no_task_no_error():
    """stop_heartbeat 无任务时不抛错。"""
    mgr = ConnectionManager()
    await mgr.stop_heartbeat()
    assert mgr._heartbeat_task is None
