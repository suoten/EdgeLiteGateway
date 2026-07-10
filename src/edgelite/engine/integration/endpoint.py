"""EdgeLite v1.0 联调集成端点"""

import asyncio
import json
import logging
import time
import uuid
from typing import Any

from edgelite.constants import _INTEGRATION_MAX_SESSIONS, _INTEGRATION_SESSION_TTL
from edgelite.engine.integration.dispatcher import MessageDispatcher

logger = logging.getLogger(__name__)


class IntegrationEndpoint:
    SESSION_TTL = _INTEGRATION_SESSION_TTL  # FIXED: 原问题-硬编码会话TTL，现引用constants.py

    def __init__(self, dispatcher: MessageDispatcher | None = None):
        self._dispatcher = dispatcher or MessageDispatcher()
        self._sessions: dict[str, dict[str, Any]] = {}
        self._connections: dict[str, Any] = {}
        self._max_sessions = _INTEGRATION_MAX_SESSIONS  # FIXED: 原问题-魔法数字，提取为命名常量
        # FIXED-P0: 添加锁保护_sessions和_connections的并发修改，防止handshake/register_connection竞态
        self._lock = asyncio.Lock()

    @property
    def dispatcher(self) -> MessageDispatcher:
        return self._dispatcher

    @property
    def session_count(self) -> int:
        return len(self._sessions)

    @property
    def has_connections(self) -> bool:
        return len(self._connections) > 0

    async def handle_handshake(self, request: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:  # FIXED-P0: 加锁保护_sessions并发修改
            await self._cleanup_expired_sessions()
            session_id = uuid.uuid4().hex[:16]
            self._sessions[session_id] = {
                "id": session_id,
                "version": request.get("version", "1.0"),
                "protocols": request.get("protocols", []),
                "capabilities": request.get("capabilities", []),
                "heartbeat_interval": request.get("heartbeat_interval", 30.0),
                "connected_at": time.time(),
                "last_activity": time.time(),
            }
        try:
            from edgelite.drivers.registry import get_driver_registry

            supported_protocols = get_driver_registry().get_all_protocol_keys()
        except Exception:
            supported_protocols = []

        response = {
            "type": "handshake_ack",
            "version": "1.0",
            "protocols": supported_protocols,
            "capabilities": [
                "push_device",
                "device_control",
                "delete_device",
                "backhaul",
                "alarm_forward",
            ],
            "session_id": session_id,
        }
        logger.info("Integration handshake complete, session: %s", session_id)
        return response

    async def register_connection(self, session_id: str, websocket: Any) -> None:
        async with self._lock:  # FIXED-P0: 加锁保护_connections并发修改
            if len(self._connections) >= self._max_sessions:
                # FIXED-P0: 原问题-min(self._sessions,...)在_sessions为空时抛ValueError。
                # 改为优先从_connections驱逐最旧连接，若_connections也为空则跳过。
                if not self._sessions:
                    # _sessions为空但_connections已满，从_connections中找最旧连接驱逐
                    if self._connections:
                        oldest_conn = next(iter(self._connections))
                        await self._unregister_connection_unlocked(oldest_conn)
                else:
                    oldest = min(self._sessions, key=lambda s: self._sessions[s].get("connected_at", 0))
                    await self._unregister_connection_unlocked(oldest)
            self._connections[session_id] = websocket
        # FIX: 新连接注册后，触发 BackhaulManager 刷新缓冲区，
        # 将断线期间积压的回传消息发送给新连接
        backhaul = getattr(self, "_backhaul", None)
        if backhaul and hasattr(backhaul, "flush_buffer"):
            try:
                flushed = await backhaul.flush_buffer()
                if flushed > 0:
                    logger.info("Flushed %d buffered messages to new integration connection %s", flushed, session_id)
            except Exception as e:
                logger.warning("Failed to flush backhaul buffer for new connection %s: %s", session_id, e)

    async def _unregister_connection_unlocked(self, session_id: str) -> None:
        """FIXED-P0: 锁内调用的unregister_connection内部方法，避免重复加锁"""
        ws = self._connections.pop(session_id, None)
        if ws:
            try:
                await ws.close()
            except Exception as e:
                logger.debug("Integration WebSocket close failed[%s]: %s", session_id, e)
        self._sessions.pop(session_id, None)

    async def unregister_connection(self, session_id: str) -> None:
        async with self._lock:  # FIXED-P0: 加锁保护
            await self._unregister_connection_unlocked(session_id)

    async def handle_message(self, session_id: str, raw_data: str) -> dict[str, Any] | None:
        try:
            message = json.loads(raw_data)
        except json.JSONDecodeError:
            return {"type": "error", "error": "Invalid JSON"}

        # FIXED: 原问题-客户端发送非字典JSON（如字符串/数字），message.get()报TypeError
        if not isinstance(message, dict):
            return {"type": "error", "error": "Message must be a JSON object"}

        # FIX: 每次收到消息时更新 session 的 last_activity，防止活跃连接被误清理
        # FIXED(严重): 原问题-session访问无锁，与handshake/unregister并发修改竞态;
        # 修复-在锁内读取并更新session
        async with self._lock:
            session = self._sessions.get(session_id)
            if session:
                session["last_activity"] = time.time()

        msg_type = message.get("type", "")
        payload = message.get("payload", {})
        response = await self._dispatcher.dispatch(msg_type, payload, session_id)

        if response is not None and msg_type in ("push_device", "delete_device", "device_control"):
            return {"type": f"{msg_type}_ack", "payload": response, "timestamp": time.time()}
        return response

    async def send_to_session(self, session_id: str, message: dict[str, Any]) -> bool:
        # FIXED(严重): _connections无锁访问保护，锁内取ws快照后锁外发送避免长时间持锁
        async with self._lock:
            ws = self._connections.get(session_id)
        if not ws:
            return False
        try:
            await ws.send(json.dumps(message))
            return True
        except Exception as e:  # FIXED: 原问题-WebSocket发送失败静默返回False，无日志
            logger.debug("Integration WebSocket send failed[%s]: %s", session_id, e)
            return False

    async def broadcast(self, message: dict[str, Any]) -> int:
        sent = 0
        # FIXED(严重): _connections无锁访问保护，锁内快照后锁外发送，避免与send_to_session死锁
        async with self._lock:
            session_ids = list(self._connections.keys())
        for session_id in session_ids:
            if await self.send_to_session(session_id, message):
                sent += 1
        return sent

    async def _cleanup_expired_sessions(self) -> None:
        # FIXED-P0: 调用方已持有_lock，此处不再重复加锁（RLock不支持asyncio.Lock重入）
        now = time.time()
        # 创建快照防止遍历期间修改
        expired = [
            sid
            for sid, info in list(self._sessions.items())
            if sid not in self._connections
            and now - info.get("last_activity", info.get("connected_at", 0))
            > self.SESSION_TTL  # FIXED-P2: 清理基于last_activity而非connected_at，活跃心跳的session不会被误清理
        ]
        for sid in expired:
            self._sessions.pop(sid, None)
            logger.info("Cleaning up expired session: %s", sid)
