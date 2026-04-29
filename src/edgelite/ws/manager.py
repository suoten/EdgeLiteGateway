"""WebSocket连接管理器"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import WebSocket

from edgelite.security.jwt import verify_token

logger = logging.getLogger(__name__)


class ConnectionManager:
    """WebSocket连接管理器"""

    def __init__(self, max_connections: int = 100):
        # channel -> set of WebSocket connections
        self._connections: dict[str, set[WebSocket]] = {}
        self.max_connections = max_connections

    async def connect(self, websocket: WebSocket, channel: str, token: str) -> bool:
        """建立WebSocket连接，验证Token"""
        try:
            payload = verify_token(token, token_type="access")
        except Exception:
            await websocket.accept()
            await websocket.close(code=4001, reason="Token无效")
            return False

        total = sum(len(conns) for conns in self._connections.values())
        if total >= self.max_connections:
            await websocket.accept()
            await websocket.close(code=1013, reason="连接数已达上限")
            logger.warning("WebSocket连接数已达上限(%d)，拒绝新连接", self.max_connections)
            return False

        await websocket.accept()
        if channel not in self._connections:
            self._connections[channel] = set()
        self._connections[channel].add(websocket)
        logger.info("WebSocket连接: channel=%s, user=%s", channel, payload.get("username", ""))
        return True

    async def disconnect(self, websocket: WebSocket, channel: str) -> None:
        """断开WebSocket连接"""
        if channel in self._connections:
            self._connections[channel].discard(websocket)
            if not self._connections[channel]:
                del self._connections[channel]
        logger.info("WebSocket断开: channel=%s", channel)

    async def broadcast(self, channel: str, data: dict[str, Any]) -> None:
        """向频道内所有连接广播消息"""
        connections = self._connections.get(channel, set())
        if not connections:
            return

        message = json.dumps(data, ensure_ascii=False)
        disconnected: set[WebSocket] = set()

        async def _send_safe(ws: WebSocket) -> None:
            try:
                await ws.send_text(message)
            except Exception:
                disconnected.add(ws)

        await asyncio.gather(*[_send_safe(ws) for ws in connections])

        for ws in disconnected:
            try:
                await ws.close()
            except Exception:
                pass
            self._connections.get(channel, set()).discard(ws)
        if disconnected and channel in self._connections and not self._connections[channel]:
            del self._connections[channel]

    def get_connection_count(self, channel: str) -> int:
        """获取频道连接数"""
        return len(self._connections.get(channel, set()))
