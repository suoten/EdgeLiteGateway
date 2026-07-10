"""多网关级联管理器 - 基于mDNS(zeroconf)实现邻居发现与拓扑构建"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import logging
import os
import secrets
import socket
import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from edgelite.constants import (
    _CASCADE_HOP_LIMIT,
    _CASCADE_TOKEN_ENV,
    _CASCADE_TOKEN_HASH_LEN,
    _CASCADE_TOKEN_TTL,
)

logger = logging.getLogger(__name__)

_MDNS_SERVICE_TYPE = "_edgelite._tcp.local."


class TopologyStatus(StrEnum):
    """拓扑节点角色"""

    STANDALONE = "standalone"
    PARENT = "parent"
    CHILD = "child"
    PEER = "peer"


@dataclass
class NeighborInfo:
    """邻居网关信息"""

    neighbor_id: str
    host: str
    port: int
    role: str = "peer"
    properties: dict[str, str] = field(default_factory=dict)
    last_seen: float = field(default_factory=time.time)


@dataclass
class CascadeTopology:
    """级联拓扑数据"""

    local_id: str
    status: TopologyStatus = TopologyStatus.STANDALONE
    parent_id: str | None = None
    children: list[str] = field(default_factory=list)
    peers: list[NeighborInfo] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)


class CascadeManager:
    """多网关级联管理器，使用zeroconf进行mDNS服务发现"""

    def __init__(
        self,
        local_id: str | None = None,
        service_name: str = "EdgeLite",
        parent_host: str | None = None,
        parent_port: int | None = None,
        service_port: int | None = None,
        cascade_token: str | None = None,
        allowed_neighbors: set[str] | None = None,
    ):
        self._local_id = local_id or socket.gethostname()
        self._service_name = service_name
        self._parent_host = parent_host
        self._parent_port = parent_port
        # R5-S-01 修复(严重): 原实现硬编码 http:// 与父节点通信，明文传输 HMAC 签名
        # 和业务数据，中间人可窃听/篡改。修复-支持通过 parent_scheme 配置 HTTPS。
        self._parent_scheme = "http"
        self._service_port = service_port  # FIXED: P0-4 mDNS服务端口硬编码改为可配置
        # FIXED(编号6/7): 级联认证 Token，优先使用参数，其次从环境变量读取
        # 用于 HMAC 签名父节点转发请求 + mDNS 邻居身份校验
        self._cascade_token = cascade_token or os.environ.get(_CASCADE_TOKEN_ENV, "")
        # FIXED(编号7): 邻居白名单(neighbor_id 集合)，为空时仅校验 token_hash 一致性
        self._allowed_neighbors = allowed_neighbors if allowed_neighbors is not None else None
        self._running = False
        self._zeroconf: Any = None
        self._registration: Any = None
        self._browser: Any = None
        self._neighbors: dict[str, NeighborInfo] = {}
        # 并发安全: 使用 RLock 保护 _neighbors 的跨线程读写
        # zeroconf 回调线程修改，asyncio 主线程迭代，无锁会导致 RuntimeError/数据不一致
        self._neighbors_lock = threading.RLock()
        self._topology = CascadeTopology(local_id=self._local_id)
        self._discover_task: asyncio.Task | None = None
        self._http_session: Any = None  # BUG-017: 复用HTTP会话
        # 并发安全: 使用 asyncio.Lock 保护 _http_session 的懒创建，防止并发创建多个 ClientSession 导致旧 session 泄漏
        self._http_session_lock = asyncio.Lock()
        # 修复P1-9: verify_cascade_request 中 _seen_nonces 的"检查→添加"为读-改-写操作，
        # 并发请求可同时通过 nonce 唯一性检查而绕过重放防护，使用 threading.Lock 保护
        # （verify_cascade_request 为同步方法，不能用 asyncio.Lock）
        self._seen_nonces: set[str] = set()
        self._nonce_lock = threading.Lock()

    @property
    def topology(self) -> CascadeTopology:
        """获取当前拓扑数据"""
        return self._topology

    @property
    def neighbors(self) -> list[NeighborInfo]:
        """获取邻居列表"""
        return list(self._neighbors.values())

    async def start(self) -> None:
        """启动级联管理器，注册mDNS服务并开始邻居发现。"""
        if self._running:
            return

        self._running = True

        try:
            from zeroconf import ServiceBrowser, ServiceInfo, Zeroconf

            self._zeroconf = Zeroconf()

            from edgelite.config import get_config

            app_config = get_config()
            # FIXED: P0-4 mDNS服务端口从配置读取，默认8080
            service_port = self._service_port or getattr(app_config.server, "port", 8080)

            info = ServiceInfo(
                _MDNS_SERVICE_TYPE,
                f"{self._service_name}.{_MDNS_SERVICE_TYPE}",
                addresses=[socket.inet_aton(self._get_local_ip())],
                port=service_port,
                properties={
                    "local_id": self._local_id,
                    # FIXED(编号7): 携带 node_id 与 token_hash 供邻居身份校验
                    "node_id": self._local_id,
                    "token_hash": self._compute_token_hash(),
                    "version": "1.0",
                },
            )
            await self._zeroconf.async_register_service(info)
            self._registration = info

            self._browser = ServiceBrowser(self._zeroconf, _MDNS_SERVICE_TYPE, self._on_service_state_change)

            if self._parent_host:
                self._topology.status = TopologyStatus.CHILD
                self._topology.parent_id = f"{self._parent_host}:{self._parent_port or 8080}"
                self._discover_task = asyncio.create_task(
                    self._maintain_parent_connection(), name="cascade-parent-maintain"
                )

            logger.info("CascadeManager started: local_id=%s role=%s", self._local_id, self._topology.status)

        except ImportError:
            logger.warning("zeroconf library not installed, cascade discovery unavailable")  # FIXED-P3: 中文日志→英文
            # FIXED-P2: 原实现ImportError时仍设_running=True，导致管理器认为已启动但实际无法工作
            self._running = False
        except Exception as e:
            logger.error("CascadeManager start failed: %s", e)  # FIXED-P3: 中文日志→英文

    async def stop(self) -> None:
        """停止级联管理器，注销mDNS服务。"""
        self._running = False

        if self._discover_task and not self._discover_task.done():
            self._discover_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._discover_task

        # BUG-017: 关闭复用的HTTP会话
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
            self._http_session = None

        if self._zeroconf:
            try:
                if self._registration:
                    await self._zeroconf.async_unregister_service(self._registration)
                await self._zeroconf.async_close()
            except Exception as e:
                logger.debug("Zeroconf close exception: %s", e)  # FIXED-P3: 中文日志→英文

        self._zeroconf = None
        self._registration = None
        self._browser = None
        logger.info("CascadeManager stopped")

    def _on_service_state_change(self, zeroconf: Any, service_type: str, name: str, state_change: Any) -> None:
        """mDNS服务状态变更回调。"""
        try:
            from zeroconf import ServiceStateChange

            if state_change == ServiceStateChange.Added:
                info = zeroconf.get_service_info(service_type, name)
                if info:
                    neighbor_id = info.properties.get(b"local_id", b"").decode() or name
                    host = socket.inet_ntoa(info.addresses[0]) if info.addresses else ""
                    port = info.port
                    props = {k.decode(): v.decode() for k, v in info.properties.items()}

                    # FIXED(编号7): 校验邻居身份 - token_hash 必须与本节点一致(共享密钥)
                    # 若配置了白名单，还需校验 neighbor_id 是否在白名单中
                    neighbor_token_hash = props.get("token_hash", "")
                    if not self._verify_neighbor(neighbor_id, neighbor_token_hash):
                        logger.warning(
                            "Neighbor %s rejected: token_hash mismatch or not in whitelist",
                            neighbor_id,
                        )
                        return

                    # 并发安全: 加锁保护 _neighbors 写操作
                    with self._neighbors_lock:
                        self._neighbors[neighbor_id] = NeighborInfo(
                            neighbor_id=neighbor_id,
                            host=host,
                            port=port,
                            role="peer",
                            properties=props,
                        )
                    self._rebuild_topology()
                    logger.info(
                        "Neighbor discovered: id=%s host=%s:%d", neighbor_id, host, port
                    )  # FIXED-P3: 中文日志→英文

            elif state_change == ServiceStateChange.Removed:
                # 并发安全: 加锁保护 _neighbors 删除操作，使用 list 快照迭代
                removed_nid: str | None = None
                with self._neighbors_lock:
                    for nid in list(self._neighbors.keys()):
                        if name.startswith(nid + "."):  # FIXED-P2: 精确前缀匹配，避免子字符串误删（如 gw-1 匹配 gw-10）
                            del self._neighbors[nid]
                            removed_nid = nid
                            break
                if removed_nid:
                    self._rebuild_topology()
                    logger.info("Neighbor offline: id=%s", removed_nid)  # FIXED-P3: 中文日志→英文
        except Exception as e:
            logger.error("mDNS service state callback exception: %s", e)  # FIXED-P3: 中文日志→英文

    def _rebuild_topology(self) -> None:
        """根据邻居信息重建拓扑。"""
        # 并发安全: 加锁取快照后操作，避免迭代期间字典被修改
        with self._neighbors_lock:
            neighbors_snapshot = list(self._neighbors.values())
        self._topology.peers = neighbors_snapshot
        if self._topology.parent_id:
            # FIXED-P2: 原实现parent_id格式为"host:port"，而_neighbors的key是neighbor_id，格式不匹配导致永远找不到父节点
            # 改为检查父节点的host:port是否匹配任何邻居的host:port
            parent_found = any(f"{n.host}:{n.port}" == self._topology.parent_id for n in neighbors_snapshot)
            if not parent_found:
                logger.debug("Parent node %s not found in neighbors", self._topology.parent_id)
            self._topology.children = [n.neighbor_id for n in neighbors_snapshot if n.role == "child"]
        self._topology.updated_at = time.time()

    async def discover_neighbors(self, timeout: float = 5.0) -> list[NeighborInfo]:
        """主动发现邻居网关。

        Args:
            timeout: 发现等待超时(秒)。

        Returns:
            当前已发现的邻居列表。
        """
        if not self._zeroconf:
            # 并发安全: 加锁返回快照
            with self._neighbors_lock:
                return list(self._neighbors.values())

        await asyncio.sleep(min(timeout, 5.0))
        # 并发安全: 加锁返回快照
        with self._neighbors_lock:
            return list(self._neighbors.values())

    def build_topology(self) -> CascadeTopology:
        """构建并返回当前级联拓扑。

        Returns:
            CascadeTopology: 包含本节点角色、父/子/邻居信息的拓扑数据。
        """
        self._rebuild_topology()
        return self._topology

    async def forward_to_parent(self, data: dict[str, Any]) -> bool:
        """将数据转发至父节点网关。

        Args:
            data: 待转发的数据字典。

        Returns:
            转发是否成功。
        """
        if not self._parent_host or not self._parent_port:
            logger.warning("Parent node not configured, cannot forward data")  # FIXED-P3: 中文日志→英文
            return False

        # FIXED(编号8): 拓扑环检测 - 携带 hop_count，每跳递增，超限丢弃
        forward_data = dict(data)
        current_hop = forward_data.get("_cascade_hop_count", 0)
        if not isinstance(current_hop, int):
            current_hop = 0
        forward_data["_cascade_hop_count"] = current_hop + 1
        if forward_data["_cascade_hop_count"] > _CASCADE_HOP_LIMIT:
            logger.error(
                "Cascade hop count exceeded limit %d, dropping forward (possible topology loop)",
                _CASCADE_HOP_LIMIT,
            )
            return False

        try:
            import aiohttp

            # BUG-017: 复用HTTP会话，避免每次调用创建新ClientSession
            # 修复P1-10: 原实现虽定义了 _http_session_lock 但未使用，并发 forward_to_parent
            # 调用会各自创建 ClientSession 导致旧 session 泄漏。现用锁包裹懒创建逻辑
            if self._http_session is None or self._http_session.closed:
                async with self._http_session_lock:
                    # 双重检查：持锁后可能已被其他协程创建
                    if self._http_session is None or self._http_session.closed:
                        # R6-S-01 修复(严重): 原 ClientSession() 无默认超时，DNS 解析或 TCP 握手
                        # 阶段可能长时间挂起。修复-会话级兜底超时 total=10s, connect=5s。
                        self._http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10, connect=5))
            url = f"{self._parent_scheme}://{self._parent_host}:{self._parent_port}/api/v1/integration/cascade/forward"
            # FIXED(编号6): 序列化请求体用于 HMAC 签名，发送原始字符串确保签名一致
            import json as _json

            body_str = _json.dumps(forward_data, ensure_ascii=False, default=str)
            headers = self._build_cascade_headers(body_str.encode("utf-8"))
            headers["Content-Type"] = "application/json"
            async with self._http_session.post(
                url,
                data=body_str,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                return resp.status == 200
        except ImportError:
            logger.warning("aiohttp library not installed, data forwarding unavailable")  # FIXED-P3: 中文日志→英文
            return False
        except Exception as e:
            logger.error("Forward data to parent node failed: %s", e)  # FIXED-P3: 中文日志→英文
            return False

    async def update_config(self, config: dict[str, Any]) -> None:
        """更新级联配置。

        Args:
            config: 包含parent_host/parent_port/role等字段的配置字典。
        """
        if "parent_host" in config:
            self._parent_host = config["parent_host"]
        if "parent_port" in config:
            self._parent_port = config["parent_port"]
        # R5-S-01: 支持 HTTPS 级联通信，生产环境应配置 parent_scheme=https
        if "parent_scheme" in config:
            scheme = str(config["parent_scheme"]).lower()
            if scheme in ("http", "https"):
                self._parent_scheme = scheme
            else:
                logger.warning("Invalid parent_scheme '%s', keeping '%s'", scheme, self._parent_scheme)
        if "role" in config:
            role = config["role"]
            if role in (TopologyStatus.PARENT, TopologyStatus.CHILD, TopologyStatus.PEER, TopologyStatus.STANDALONE):
                self._topology.status = TopologyStatus(role)
        if self._parent_host:
            self._topology.parent_id = f"{self._parent_host}:{self._parent_port or 8080}"
            self._topology.status = TopologyStatus.CHILD
        self._rebuild_topology()
        logger.info(
            "Cascade config updated: parent=%s:%s role=%s", self._parent_host, self._parent_port, self._topology.status
        )  # FIXED-P3: 中文日志→英文

    async def remove_neighbor(self, neighbor_id: str) -> bool:
        """移除指定邻居。

        Args:
            neighbor_id: 邻居网关ID。

        Returns:
            是否成功移除。
        """
        # 并发安全: 加锁保护 _neighbors 的检查与删除
        with self._neighbors_lock:
            if neighbor_id in self._neighbors:
                del self._neighbors[neighbor_id]
            else:
                return False
        self._rebuild_topology()
        logger.info("Neighbor removed: id=%s", neighbor_id)  # FIXED-P3: 中文日志→英文
        return True

    async def _maintain_parent_connection(self) -> None:
        """维持与父节点的连接（心跳检测）。"""
        while self._running:
            try:
                await asyncio.sleep(30)
                if self._parent_host:
                    success = await self.forward_to_parent({"type": "heartbeat", "local_id": self._local_id})
                    if not success:
                        logger.debug(
                            "Parent heartbeat failed: %s:%s", self._parent_host, self._parent_port
                        )  # FIXED-P3: 中文日志→英文
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Parent connection maintenance exception: %s", e)  # FIXED-P3: 中文日志→英文

    # ── FIXED(编号6/7/8): 级联认证与拓扑环检测方法 ──

    def _compute_token_hash(self) -> str:
        """计算本节点级联 Token 的哈希(截取前N位)，用于 mDNS 广播身份标识。

        未配置 Token 时返回空字符串(向后兼容，但无法通过严格校验)。
        """
        if not self._cascade_token:
            return ""
        return hashlib.sha256(self._cascade_token.encode("utf-8")).hexdigest()[:_CASCADE_TOKEN_HASH_LEN]

    def _verify_neighbor(self, neighbor_id: str, neighbor_token_hash: str) -> bool:
        """校验邻居身份：token_hash 一致性 + 白名单(若配置)。

        Args:
            neighbor_id: 邻居节点 ID。
            neighbor_token_hash: 邻居广播的 token_hash。

        Returns:
            是否通过校验。
        """
        # 若本节点未配置 Token，跳过校验(向后兼容，记录告警)
        if not self._cascade_token:
            logger.warning("Cascade token not configured, neighbor verification skipped for %s", neighbor_id)
            return True
        expected_hash = self._compute_token_hash()
        if not neighbor_token_hash or neighbor_token_hash != expected_hash:
            return False
        # 若配置了白名单，还需校验 neighbor_id
        return not (self._allowed_neighbors is not None and neighbor_id not in self._allowed_neighbors)

    def _build_cascade_headers(self, body: bytes) -> dict[str, str]:
        """构建级联转发认证头：HMAC-SHA256 签名 + 时间戳 + nonce。

        Args:
            body: 请求体原始字节，签名基于 timestamp + nonce + body 计算。

        Returns:
            包含认证头的字典；未配置 Token 时返回空字典(向后兼容)。
        """
        if not self._cascade_token:
            return {}
        timestamp = str(int(time.time()))
        nonce = secrets.token_hex(16)
        message = f"{timestamp}{nonce}".encode() + body
        signature = hmac.new(self._cascade_token.encode("utf-8"), message, hashlib.sha256).hexdigest()
        return {
            "X-Cascade-Token": signature,
            "X-Cascade-Timestamp": timestamp,
            "X-Cascade-Nonce": nonce,
        }

    def verify_cascade_request(self, headers: dict[str, str], body: bytes) -> tuple[bool, str]:
        """校验级联转发请求的 HMAC 签名、时间戳有效性(接收端调用)。

        Args:
            headers: HTTP 请求头字典(键不区分大小写)。
            body: 请求体原始字节。

        Returns:
            (是否通过, 失败原因)。
        """
        if not self._cascade_token:
            # 未配置 Token 时拒绝所有转发请求(严格模式)，防止未认证节点冒充
            return False, "cascade token not configured on receiver"
        # 大小写不敏感取头
        lower_headers = {k.lower(): v for k, v in headers.items()}
        signature = lower_headers.get("x-cascade-token", "")
        timestamp_str = lower_headers.get("x-cascade-timestamp", "")
        nonce = lower_headers.get("x-cascade-nonce", "")
        if not signature or not timestamp_str or not nonce:
            return False, "missing cascade auth headers"
        # 时间戳有效性校验，防止重放攻击
        try:
            ts = int(timestamp_str)
        except ValueError:
            return False, "invalid timestamp format"
        if abs(time.time() - ts) > _CASCADE_TOKEN_TTL:
            return False, "timestamp out of allowed window"
        # 重放防护: nonce 去重(简单内存集合，进程级)
        # 修复P1-9: 用 threading.Lock 保护 _seen_nonces 的"检查→添加"读-改-写操作，
        # 避免并发请求同时通过 nonce 唯一性检查而绕过重放防护
        with self._nonce_lock:
            if nonce in self._seen_nonces:
                return False, "nonce replay detected"
            self._seen_nonces.add(nonce)
            # 防止 nonce 集合无限增长
            if len(self._seen_nonces) > 10000:
                self._seen_nonces.clear()
                self._seen_nonces.add(nonce)
        # 重算签名并比对(常量时间比较)
        message = f"{timestamp_str}{nonce}".encode() + body
        expected = hmac.new(self._cascade_token.encode("utf-8"), message, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return False, "signature mismatch"
        return True, ""

    @staticmethod
    def check_hop_count(data: dict[str, Any]) -> tuple[bool, int]:
        """检查转发数据的 hop_count，防止拓扑环(接收端调用)。

        Args:
            data: 转发请求体字典。

        Returns:
            (是否允许, 当前 hop_count)。
        """
        hop = data.get("_cascade_hop_count", 0)
        if not isinstance(hop, int) or hop < 0:
            hop = 0
        if hop > _CASCADE_HOP_LIMIT:
            logger.error(
                "Cascade hop count %d exceeded limit %d, dropping (possible topology loop)",
                hop,
                _CASCADE_HOP_LIMIT,
            )
            return False, hop
        return True, hop

    @staticmethod
    def _get_local_ip() -> str:
        """获取本机局域网IP地址。"""
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            return ip
        except Exception:
            return "127.0.0.1"
        finally:
            # FIXED-P2: 原实现s.connect()异常时socket未关闭导致fd泄漏，使用finally确保关闭
            if s is not None:
                with contextlib.suppress(OSError):
                    s.close()
