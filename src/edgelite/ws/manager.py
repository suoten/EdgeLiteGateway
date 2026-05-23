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

    async def connect(self, websocket: WebSocket, channel: str, token: str | None = None) -> bool:
        """建立WebSocket连接，验证Token

        FIXED-P2: 支持首帧认证模式—token为None时先accept，由调用方从首帧消息提取token后调用authenticate()
        """
        if token:
            try:
                payload = verify_token(token, token_type="access")
            except Exception:
                await websocket.accept()
                try:
                    await websocket.send_json({"type": "error", "code": 4001, "message": "Authentication failed: invalid or expired token"})
                except Exception:
                    pass
                await websocket.close(code=4001, reason="Authentication failed")
                return False

            total = sum(len(conns) for conns in self._connections.values())
            if total >= self.max_connections:
                await websocket.accept()
                await websocket.close(code=1013, reason="Max connections reached")
                logger.warning("WebSocket max connections reached (%d), rejecting", self.max_connections)
                return False

            await websocket.accept()
            if channel not in self._connections:
                self._connections[channel] = set()
            self._connections[channel].add(websocket)
            logger.info("WebSocket connected: channel=%s, user=%s", channel, payload.get("username", ""))
            return True
        else:
            await websocket.accept()
            return False

    async def authenticate(self, websocket: WebSocket, channel: str, token: str) -> bool:
        """首帧Token认证—connect(token=None)后调用"""
        try:
            payload = verify_token(token, token_type="access")
        except Exception:
            try:
                await websocket.send_json({"type": "error", "code": 4001, "message": "Authentication failed: invalid or expired token"})
            except Exception:
                pass
            await websocket.close(code=4001, reason="Authentication failed")
            return False

        total = sum(len(conns) for conns in self._connections.values())
        if total >= self.max_connections:
            await websocket.close(code=1013, reason="Max connections reached")
            logger.warning("WebSocket max connections reached (%d), rejecting", self.max_connections)
            return False

        if channel not in self._connections:
            self._connections[channel] = set()
        self._connections[channel].add(websocket)
        logger.info("WebSocket connected (frame auth): channel=%s, user=%s", channel, payload.get("username", ""))
        return True

    async def disconnect(self, websocket: WebSocket, channel: str) -> None:
        """断开WebSocket连接"""
        if channel in self._connections:
            self._connections[channel].discard(websocket)
            if not self._connections[channel]:
                del self._connections[channel]
        logger.info("WebSocket disconnected: channel=%s", channel)

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
            except Exception as e:  # FIXED: 原问题-WebSocket发送失败静默添加到disconnected，无日志
                logger.debug("WebSocket send failed: %s", e)
                disconnected.add(ws)

        # FIXED-P2: 原asyncio.gather对全部连接并发发送，连接数>1000时压垮服务器，改为分批发送
        _BROADCAST_BATCH_SIZE = 50
        conn_list = list(connections)
        for i in range(0, len(conn_list), _BROADCAST_BATCH_SIZE):
            batch = conn_list[i : i + _BROADCAST_BATCH_SIZE]
            await asyncio.gather(*[_send_safe(ws) for ws in batch])

        for ws in disconnected:
            try:
                await ws.close()
            except Exception as e:
                logger.debug("WebSocket close failed: %s", e)
            self._connections.get(channel, set()).discard(ws)
        if disconnected and channel in self._connections and not self._connections[channel]:
            del self._connections[channel]

    def get_connection_count(self, channel: str) -> int:
        """获取频道连接数"""
        return len(self._connections.get(channel, set()))
