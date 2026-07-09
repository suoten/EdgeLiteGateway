"""WebSocket连接管理器"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
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
        # FIXED-P2: 连接数检查与添加的原子锁，消除TOCTOU竞态窗口
        self._connect_lock = asyncio.Lock()
        # FIXED-P2: 心跳机制—定期ping检测死连接，防止连接池积累僵尸连接
        self._heartbeat_interval = 30  # 心跳间隔(秒)
        self._heartbeat_timeout = 60  # 心跳超时(秒)，超过此时间未响应则断开
        self._heartbeat_task: asyncio.Task | None = None
        # FIXED(严重): 连接元数据—存储每个 WebSocket 的用户身份（user_id, role, username），
        # 用于 broadcast 按角色过滤，防止低权限用户接收高权限频道消息
        self._conn_meta: dict[WebSocket, dict[str, Any]] = {}
        # FIXED(一般): pong 追踪—记录每连接最后 pong 时间，用于检测僵尸连接
        self._last_pong: dict[WebSocket, float] = {}
        # FIXED: per-connection send lock — ASGI send 不可重入，broadcast 与 heartbeat ping
        # 并发向同一 WebSocket 写入会导致帧字节交错损坏 [2026-06-29]
        self._send_locks: dict[WebSocket, asyncio.Lock] = {}
        # FIXED: 允许的 WebSocket Origin 白名单（None 表示不校验，开发模式用）[2026-06-29]
        self._allowed_origins: set[str] | None = None

    def set_allowed_origins(self, origins: list[str] | None) -> None:
        """FIXED: 设置 WebSocket Origin 白名单，防止 CSWSH 跨站 WebSocket 劫持 [2026-06-29]

        origins 为 None 或空列表时表示不校验（开发模式）。
        生产环境应传入与 CORS 一致的允许源列表。
        """
        self._allowed_origins = set(origins) if origins else None

    async def _send_with_lock(self, ws: WebSocket, send_fn: Callable, *args, **kwargs) -> None:
        """FIXED: 串行化 per-connection 发送，防止并发 send 导致帧损坏 [2026-06-29]"""
        lock = self._send_locks.get(ws)
        if lock is None:
            # 连接未注册（如 authenticate 阶段），直接发送无并发风险
            await send_fn(*args, **kwargs)
            return
        async with lock:
            await send_fn(*args, **kwargs)

    def validate_origin(self, websocket: WebSocket) -> bool:
        """FIXED: 校验 WebSocket Origin 头，防止 CSWSH 跨站 WebSocket 劫持 [2026-06-29]

        浏览器对任意 Origin 自动带 Cookie，若不校验 Origin，恶意页面可建 WS 直连
        完成认证并窃取数据。CORS 中间件对 WebSocket 无效（WS 不走 CORS 预检）。

        Returns:
            True 表示 Origin 合法（或未启用校验），False 表示应拒绝连接
        """
        if self._allowed_origins is None:
            return True  # 开发模式不校验
        try:
            origin = websocket.headers.get("origin", "") or websocket.headers.get("Origin", "")
        except Exception:
            origin = ""
        if not origin:
            # 非浏览器客户端（如 curl/Postman）无 Origin 头，允许通过（依赖 token 认证）
            return True
        return origin in self._allowed_origins

    async def connect(self, websocket: WebSocket, channel: str, token: str | None = None) -> bool:
        """建立WebSocket连接，验证Token

        FIXED-P2: 支持首帧认证模式—token为None时先accept，由调用方从首帧消息提取token后调用authenticate()
        """
        # FIXED: CSWSH 防护 — accept 前校验 Origin，恶意页面可携 Cookie 建 WS 直连窃取数据 [2026-06-29]
        if not self.validate_origin(websocket):
            try:
                await websocket.accept()
                await websocket.close(code=4003, reason="Origin not allowed")
            except Exception as e:
                logger.warning("WebSocket Origin reject accept/close failed: %s", e)
            logger.warning("WebSocket rejected: Origin not allowed, channel=%s", channel)
            return False
        if token:
            try:
                payload = verify_token(token, token_type="access")
            except Exception:
                # FIXED-P1: accept/close可能因客户端已断开而失败，需捕获异常防止未处理异常
                try:
                    await websocket.accept()
                    try:
                        await websocket.send_json({"type": "error", "code": 4001, "message": "Authentication failed: invalid or expired token"})
                    except Exception as e:
                        # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                        logger.warning("WebSocket发送认证错误消息失败: %s", e)
                    await websocket.close(code=4001, reason="Authentication failed")
                except Exception as e:
                    # FIXED-P1: 原问题-accept/close失败时异常未捕获，添加日志记录
                    logger.warning("WebSocket accept/close失败(认证失败路径): %s", e)
                return False

            # FIXED-P1: 原问题-早期连接数检查读取self._connections无锁保护，且accept/close无异常处理；
            # 移除冗余的早期检查（锁内检查是权威的），直接accept后在锁内检查并添加
            await websocket.accept()
            # FIXED-P2: 原子操作：连接数检查+添加在同一锁内，消除竞态窗口
            async with self._connect_lock:
                total = sum(len(conns) for conns in self._connections.values())
                if total >= self.max_connections:
                    try:
                        # FIXED-P1: close可能因客户端已断开而失败，需捕获异常
                        await websocket.close(code=1013, reason="Max connections reached")
                    except Exception as e:
                        logger.warning("WebSocket close失败(连接数超限): %s", e)
                    logger.warning("WebSocket max connections reached (%d), rejecting", self.max_connections)
                    return False
                if channel not in self._connections:
                    self._connections[channel] = set()
                self._connections[channel].add(websocket)
                # FIXED(严重): 绑定用户身份到连接，用于 broadcast 按角色过滤
                # FIXED(严重): 标记 authenticated=True，token 路径已通过 verify_token 认证
                self._conn_meta[websocket] = {
                    "user_id": payload.get("sub", ""),
                    "role": payload.get("role", ""),
                    "username": payload.get("username", ""),
                    "channel": channel,
                    "authenticated": True,
                }
                # FIXED: 注册 per-connection send lock，串行化 broadcast/heartbeat 发送 [2026-06-29]
                self._send_locks[websocket] = asyncio.Lock()
                self._last_pong[websocket] = time.monotonic()
            logger.info("WebSocket connected: channel=%s, user=%s", channel, payload.get("username", ""))
            return True
        else:
            # FIXED-P1: 原问题-token=None路径锁内检查连接数后释放锁再accept，竞态窗口可导致连接数超限；
            # 将accept移入锁内，连接数检查和accept在同一临界区
            # FIXED-P0: 原问题-accept后未将websocket加入_connections，若authenticate未调用则连接泄漏且计数不准；
            # accept后立即加入_connections，authenticate失败时由调用方调用disconnect清理
            async with self._connect_lock:
                total = sum(len(conns) for conns in self._connections.values())
                if total >= self.max_connections:
                    # FIXED-P1: accept/close添加异常捕获，防止客户端提前断开导致未处理异常
                    try:
                        await websocket.accept()
                        await websocket.close(code=1013, reason="Max connections reached")
                    except Exception as e:
                        logger.warning("WebSocket accept/close失败(连接数超限): %s", e)
                    logger.warning("WebSocket max connections reached (%d), rejecting", self.max_connections)
                    return False
                try:
                    await websocket.accept()
                except Exception as e:
                    # FIXED-P1: accept失败时不添加到_connections，避免追踪无效连接
                    logger.warning("WebSocket accept失败(token=None路径): %s", e)
                    return False
                if channel not in self._connections:
                    self._connections[channel] = set()
                self._connections[channel].add(websocket)
                # FIXED(严重): 标记 authenticated=False，防止未认证连接在 _recv_auth_token
                # 等待窗口内收到广播消息。broadcast 会跳过 authenticated!=True 的连接。
                # authenticate() 成功后会更新此标记为 True。
                self._conn_meta[websocket] = {
                    "user_id": "",
                    "role": "",
                    "username": "",
                    "channel": channel,
                    "authenticated": False,
                }
                # FIXED: 注册 per-connection send lock，串行化 broadcast/heartbeat 发送 [2026-06-29]
                self._send_locks[websocket] = asyncio.Lock()
                self._last_pong[websocket] = time.monotonic()
            # FIXED-P0: token=None正常路径应返回True，调用方才会在首帧认证后调用authenticate()
            return True

    async def authenticate(self, websocket: WebSocket, channel: str, token: str) -> bool:
        """首帧Token认证—connect(token=None)后调用

        FIXED-P0: connect(token=None)已将websocket加入_connections，authenticate仅验证token，
        失败时由调用方调用disconnect清理连接
        """
        try:
            payload = verify_token(token, token_type="access")
        except Exception:
            try:
                await websocket.send_json({"type": "error", "code": 4001, "message": "Authentication failed: invalid or expired token"})
            except Exception as e:
                # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                logger.warning("WebSocket发送认证错误消息失败: %s", e)
            try:
                await websocket.close(code=4001, reason="Authentication failed")
            except Exception:  # FIXED-P0: close可能已由框架执行，捕获异常防止double-close
                pass
            return False

        # FIXED-P0: 原问题-authenticate重复添加websocket到_connections（connect已添加）；
        # 改为仅验证token，不再重复添加。连接已在connect(token=None)中追踪。
        # LP-02: 认证成功后发送 auth ok 消息，前端依赖此消息将状态置为 connected
        # （Cookie 认证模式无首帧消息，必须由后端主动通知认证结果）
        # FIXED(严重): 绑定用户身份到连接，用于 broadcast 按角色过滤
        # FIXED(严重): 认证成功后设置 authenticated=True，使该连接可接收 broadcast 消息
        async with self._connect_lock:
            self._conn_meta[websocket] = {
                "user_id": payload.get("sub", ""),
                "role": payload.get("role", ""),
                "username": payload.get("username", ""),
                "channel": channel,
                "authenticated": True,
            }
            self._last_pong[websocket] = time.monotonic()
        try:
            await websocket.send_json({"type": "auth", "status": "ok"})
        except Exception as e:
            logger.warning("WebSocket发送auth ok消息失败: %s", e)
        logger.info("WebSocket authenticated (frame auth): channel=%s, user=%s", channel, payload.get("username", ""))
        return True

    async def disconnect(self, websocket: WebSocket, channel: str) -> None:
        """断开WebSocket连接"""
        async with self._connect_lock:  # FIXED-P1: disconnect与connect/authenticate互斥，防止并发修改_connections
            if channel in self._connections:
                self._connections[channel].discard(websocket)
                if not self._connections[channel]:
                    del self._connections[channel]
            # FIXED(严重): 清理连接元数据
            self._conn_meta.pop(websocket, None)
            self._last_pong.pop(websocket, None)  # FIXED(一般): 清理 pong 记录
            # FIXED: 清理 per-connection send lock，防止内存泄漏 [2026-06-29]
            self._send_locks.pop(websocket, None)
        logger.info("WebSocket disconnected: channel=%s", channel)

    def record_pong(self, websocket: WebSocket) -> None:
        """FIXED(一般): 记录客户端 pong 响应，用于心跳超时检测。"""
        # FIXED: 使用 monotonic 时钟，避免 NTP 时钟跳变导致误判 [2026-06-29]
        self._last_pong[websocket] = time.monotonic()

    async def broadcast(
        self,
        channel: str,
        data: dict[str, Any],
        filter_fn: Callable[[dict[str, Any]], bool] | None = None,
    ) -> None:
        """向频道内所有连接广播消息。

        FIXED(严重): 新增 filter_fn 参数，支持按连接的用户身份过滤消息。
        例如仅向 admin 角色广播：broadcast("alarm", data, lambda meta: meta.get("role") == "admin")
        若 filter_fn 为 None，则向频道内所有连接广播（向后兼容）。
        """
        async with self._connect_lock:  # FIXED-P1: 锁内复制connections快照，防止迭代期间disconnect修改
            connections = set(self._connections.get(channel, set()))
            # FIXED(严重): 同时复制元数据快照，用于过滤
            meta_snapshot = {
                ws: dict(self._conn_meta[ws])
                for ws in connections
                if ws in self._conn_meta
            }
        if not connections:
            return

        # FIXED(严重): 跳过未认证连接，防止 connect(token=None) 后 _recv_auth_token
        # 等待窗口内的未认证连接收到广播消息（认证前连接 _conn_meta.authenticated=False）
        connections = {
            ws for ws in connections
            if ws in meta_snapshot and meta_snapshot[ws].get("authenticated") is True
        }
        if not connections:
            return

        # FIXED(严重): 按用户身份过滤连接
        if filter_fn is not None:
            # FIXED(一般): 原问题-filter_fn调用未包裹try/except, 单个连接过滤抛异常会中断整个广播;
            # 修复-包裹try/except, 过滤失败时默认排除该连接, 不影响其他连接的广播
            filtered: set[WebSocket] = set()
            for ws in connections:
                if ws not in meta_snapshot:
                    continue
                try:
                    if filter_fn(meta_snapshot[ws]):
                        filtered.add(ws)
                except Exception as e:
                    logger.debug("broadcast filter_fn raised, excluding connection: %s", e)
                    continue
            connections = filtered
            if not connections:
                return

        message = json.dumps(data, ensure_ascii=False)
        disconnected: set[WebSocket] = set()

        async def _send_safe(ws: WebSocket) -> None:
            try:
                # FIXED: 使用 per-connection send lock 串行化发送，防止与 heartbeat ping 并发写入
                # 导致 ASGI send 帧字节交错损坏 [2026-06-29]
                # R6-S-05: 添加 per-send 超时(5s)，防止慢消费者阻塞整个广播批次
                await asyncio.wait_for(self._send_with_lock(ws, ws.send_text, message), timeout=5.0)
            except TimeoutError:
                # R6-S-05: 超时后关闭该慢消费者连接，避免后续广播继续阻塞
                logger.warning("WebSocket send timed out (5s), closing slow consumer: %s", ws)
                disconnected.add(ws)
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
        # FIXED-P2: 清理断开连接时加锁，与disconnect/connect互斥
        if disconnected:
            async with self._connect_lock:
                for ws in disconnected:
                    if channel in self._connections:
                        self._connections[channel].discard(ws)
                if channel in self._connections and not self._connections[channel]:
                    del self._connections[channel]

    # FIXED-P1: 此方法已弃用，返回近似值（不加锁），请使用 get_connection_count_async
    # Python内置DeprecationWarning在生产环境默认被忽略，改用logging.warning记录
    def get_connection_count(self, channel: str) -> int:
        """[已弃用] 获取频道连接数（近似值，同步方法无法加asyncio.Lock）

        请使用 get_connection_count_async(channel) 获取精确值。
        此方法将在未来版本中移除。
        """
        # FIXED-P1: 使用前发出弃用警告
        logger.warning(
            "get_connection_count() is deprecated, use get_connection_count_async() instead. "
            "Caller should migrate to async version for accurate connection counts."
        )
        return len(self._connections.get(channel, set()))

    async def get_connection_count_async(self, channel: str) -> int:
        """获取频道连接数（精确值，asyncio.Lock保护）"""
        async with self._connect_lock:  # FIXED-P1: 锁内读取，与connect/disconnect/authenticate互斥
            return len(self._connections.get(channel, set()))

    async def get_total_connection_count(self) -> int:
        """获取总连接数（精确值，asyncio.Lock保护）"""
        async with self._connect_lock:  # FIXED-P1: 锁内读取，与connect/disconnect/authenticate互斥
            return sum(len(conns) for conns in self._connections.values())

    # FIXED-P2: 心跳机制 - 定期ping所有连接，检测并清理死连接
    async def start_heartbeat(self) -> None:
        """启动心跳检测任务"""
        if self._heartbeat_task is not None and not self._heartbeat_task.done():
            return
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name="ws-heartbeat"
        )
        logger.info("WebSocket heartbeat started (interval=%ds)", self._heartbeat_interval)

    async def stop_heartbeat(self) -> None:
        """停止心跳检测任务"""
        if self._heartbeat_task is not None and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat_task
        self._heartbeat_task = None
        logger.info("WebSocket heartbeat stopped")

    async def close(self) -> None:
        """FIXED-P1: 原问题-ConnectionManager无close()方法，bootstrap.py的hasattr(c.ws_manager,'close')检查为False，
        导致应用关闭时WebSocket连接不被主动关闭、心跳任务不被取消，资源泄漏。
        统一关闭：停止心跳 → 关闭所有连接 → 清空连接表。"""
        await self.stop_heartbeat()
        async with self._connect_lock:
            closed = 0
            for _ch, conns in list(self._connections.items()):  # FIXED(P3): 原问题-B007循环变量ch未使用; 修复-改为_ch
                for ws in conns:
                    try:
                        await ws.close(code=1001, reason="Server shutting down")
                        closed += 1
                    except Exception as e:
                        logger.debug("WebSocket close failed during shutdown: %s", e)
                conns.clear()
            self._connections.clear()
            self._conn_meta.clear()  # FIXED(严重): 清理连接元数据
            self._last_pong.clear()  # FIXED(一般): 清理 pong 记录
            self._send_locks.clear()  # FIXED: 清理所有 send lock [2026-06-29]
        if closed:
            logger.info("WebSocket manager closed %d connections", closed)

    async def _heartbeat_loop(self) -> None:
        """心跳检测循环 - 定期ping所有连接，清理无响应的死连接"""
        while True:
            try:
                await asyncio.sleep(self._heartbeat_interval)
                # 获取所有连接的快照
                async with self._connect_lock:
                    all_connections: list[tuple[WebSocket, str]] = []
                    for ch, conns in self._connections.items():
                        for ws in conns:
                            all_connections.append((ws, ch))

                if not all_connections:
                    continue

                # 并发ping所有连接，检测死连接
                dead_connections: set[WebSocket] = set()
                # FIXED: 使用 monotonic 时钟，避免 NTP 时钟跳变误杀健康连接 [2026-06-29]
                now = time.monotonic()

                # FIXED(一般): 检查 pong 超时—连接超过 _heartbeat_timeout 未响应 pong 则视为僵尸连接
                for ws, _ in all_connections:
                    last_pong = self._last_pong.get(ws)
                    if last_pong is not None and (now - last_pong) > self._heartbeat_timeout:
                        logger.warning("WebSocket pong timeout, marking as dead: %s", ws)
                        dead_connections.add(ws)

                # FIXED(P1): 原问题-B023 循环变量捕获; 修复-使用默认参数绑定当前 dead_connections 的引用
                async def _ping_safe(ws: WebSocket, _dead=dead_connections) -> None:
                    try:
                        # 使用application层ping消息(兼容浏览器WebSocket API)
                        # FIXED: 使用 per-connection send lock 串行化发送，防止与 broadcast 并发写入 [2026-06-29]
                        # ping 时间戳用 time.time() 便于客户端计算延迟（非用于超时判断）
                        await self._send_with_lock(ws, ws.send_json, {"type": "ping", "timestamp": int(time.time())})
                    except Exception as e:
                        logger.debug("WebSocket ping failed: %s", e)
                        _dead.add(ws)

                # 分批ping，避免大量连接同时发送
                _PING_BATCH_SIZE = 50
                for i in range(0, len(all_connections), _PING_BATCH_SIZE):
                    batch = all_connections[i : i + _PING_BATCH_SIZE]
                    await asyncio.gather(*[_ping_safe(ws) for ws, _ in batch])

                # 清理死连接
                if dead_connections:
                    # R6-S-18: 锁内仅收集死连接并从 _connections/_conn_meta/_last_pong 移除，
                    # 锁外执行 await ws.close()，避免多个死连接串行关闭时 _connect_lock 被长时间持有，
                    # 从而阻塞 connect/disconnect/authenticate 等连接管理操作
                    async with self._connect_lock:
                        for ws in dead_connections:
                            for ch in list(self._connections.keys()):
                                self._connections[ch].discard(ws)
                                if not self._connections[ch]:
                                    del self._connections[ch]
                            self._conn_meta.pop(ws, None)  # FIXED(严重): 清理死连接元数据
                            self._last_pong.pop(ws, None)  # R6-S-18: 清理 pong 记录，避免内存泄漏
                            self._send_locks.pop(ws, None)  # FIXED: 清理 send lock [2026-06-29]
                    # 锁外关闭死连接，避免阻塞其他连接管理操作
                    for ws in dead_connections:
                        try:
                            await ws.close(code=1011, reason="Heartbeat timeout")
                        # FIXED-P1: 原问题-except Exception: pass 静默吞没异常，改为至少 logger.debug
                        except Exception as e:
                            logger.debug("[ws] dead connection close failed: %s", e)
                    logger.info("WebSocket heartbeat cleaned up %d dead connections", len(dead_connections))

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("WebSocket heartbeat loop error: %s", e)
