"""EdgeLite v1.0 联调集成端点"""

import json
import logging
import time
import uuid
from typing import Any

from edgelite.engine.integration.dispatcher import MessageDispatcher

logger = logging.getLogger(__name__)


class IntegrationEndpoint:
    def __init__(self, dispatcher: MessageDispatcher | None = None):
        self._dispatcher = dispatcher or MessageDispatcher()
        self._sessions: dict[str, dict[str, Any]] = {}
        self._connections: dict[str, Any] = {}
        self._max_sessions = 10

    @property
    def dispatcher(self) -> MessageDispatcher:
        return self._dispatcher

    @property
    def session_count(self) -> int:
        return len(self._sessions)

    async def handle_handshake(self, request: dict[str, Any]) -> dict[str, Any]:
        session_id = uuid.uuid4().hex[:16]
        self._sessions[session_id] = {
            "id": session_id,
            "version": request.get("version", "1.0"),
            "protocols": request.get("protocols", []),
            "capabilities": request.get("capabilities", []),
            "heartbeat_interval": request.get("heartbeat_interval", 30.0),
            "connected_at": time.time(),
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
            "capabilities": ["push_device", "device_control", "delete_device", "backhaul", "alarm_forward"],
            "session_id": session_id,
        }
        logger.info("联调握手完成, session: %s", session_id)
        return response

    async def register_connection(self, session_id: str, websocket: Any) -> None:
        if len(self._connections) >= self._max_sessions:
            oldest = min(self._sessions, key=lambda s: self._sessions[s].get("connected_at", 0))
            await self.unregister_connection(oldest)
        self._connections[session_id] = websocket

    async def unregister_connection(self, session_id: str) -> None:
        ws = self._connections.pop(session_id, None)
        if ws:
            try:
                await ws.close()
            except Exception:
                pass
        self._sessions.pop(session_id, None)

    async def handle_message(self, session_id: str, raw_data: str) -> dict[str, Any] | None:
        try:
            message = json.loads(raw_data)
        except json.JSONDecodeError:
            return {"type": "error", "error": "Invalid JSON"}

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
        except Exception:
            return False

    async def broadcast(self, message: dict[str, Any]) -> int:
        sent = 0
        for session_id in list(self._connections.keys()):
            if await self.send_to_session(session_id, message):
                sent += 1
        return sent
