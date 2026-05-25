"""EdgeLite v1.0 联调集成端点"""

import json
import logging
import time
import uuid
from typing import Any

from edgelite.constants import _INTEGRATION_SESSION_TTL, _INTEGRATION_MAX_SESSIONS
from edgelite.engine.integration.dispatcher import MessageDispatcher

logger = logging.getLogger(__name__)


class IntegrationEndpoint:
    SESSION_TTL = _INTEGRATION_SESSION_TTL  # FIXED: 原问题-硬编码会话TTL，现引用constants.py

    def __init__(self, dispatcher: MessageDispatcher | None = None):
        self._dispatcher = dispatcher or MessageDispatcher()
        self._sessions: dict[str, dict[str, Any]] = {}
        self._connections: dict[str, Any] = {}
        self._max_sessions = _INTEGRATION_MAX_SESSIONS  # FIXED: 原问题-魔法数字，提取为命名常量

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

            supported_protocols = get_driver_registry().get_supported_protocols()
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
        if len(self._connections) >= self._max_sessions:
            oldest = min(self._sessions, key=lambda s: self._sessions[s].get("connected_at", 0))
            await self.unregister_connection(oldest)
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

    async def unregister_connection(self, session_id: str) -> None:
        ws = self._connections.pop(session_id, None)
        if ws:
            try:
                await ws.close()
            except Exception as e:
                logger.debug("Integration WebSocket close failed[%s]: %s", session_id, e)
        self._sessions.pop(session_id, None)

    async def handle_message(self, session_id: str, raw_data: str) -> dict[str, Any] | None:
        try:
            message = json.loads(raw_data)
        except json.JSONDecodeError:
            return {"type": "error", "error": "Invalid JSON"}

        # FIXED: 原问题-客户端发送非字典JSON（如字符串/数字），message.get()报TypeError
        if not isinstance(message, dict):
            return {"type": "error", "error": "Message must be a JSON object"}

        # FIX: 每次收到消息时更新 session 的 last_activity，防止活跃连接被误清理
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
        for session_id in list(self._connections.keys()):
            if await self.send_to_session(session_id, message):
                sent += 1
        return sent

    async def _cleanup_expired_sessions(self) -> None:
        now = time.time()
        expired = [
            sid
            for sid, info in self._sessions.items()
            if sid not in self._connections and now - info.get("last_activity", info.get("connected_at", 0)) > self.SESSION_TTL  # FIXED-P2: 清理基于last_activity而非connected_at，活跃心跳的session不会被误清理
        ]
        for sid in expired:
            self._sessions.pop(sid, None)
            logger.info("Cleaning up expired session: %s", sid)
