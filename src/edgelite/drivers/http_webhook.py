"""HTTP/Webhook接入驱动 - 接收外部HTTP推送数据"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json as _json
import logging
import math
import socket
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from edgelite.constants import _HTTP_TIMEOUT
from edgelite.drivers.base import DriverCapabilities, DriverPlugin, PointValue
from edgelite.services.i18n import t as _t

logger = logging.getLogger(__name__)

_DNS_CACHE: dict[str, tuple[str, float]] = {}
_DNS_CACHE_TTL = 60.0  # HW-003: 降低 TTL 防止 DNS Rebinding 攻击
_MAX_DNS_CACHE_TTL = 60.0  # HW-003: 最大允许 TTL
_MAX_PAYLOAD_SIZE = 65536
_WRITE_RATE_LIMIT_MS = 100
_RETRY_BACKOFF_MAX = 60.0

_PRIVATE_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),  # FIXED-P1: IPv6链路本地地址
]


def _bad_pv(error_code: str) -> PointValue:
    return PointValue(value=None, quality="bad", timestamp=datetime.now(UTC), source=f"http_webhook:{error_code}")


def _uncertain_pv(value: Any, error_code: str) -> PointValue:
    return PointValue(
        value=value, quality="uncertain", timestamp=datetime.now(UTC), source=f"http_webhook:{error_code}"
    )


@dataclass
class _PointHealth:
    last_received_at: float = 0.0
    receive_count: int = 0
    timeout_count: int = 0
    last_value: Any = None
    last_timestamp: float = 0.0
    value_history: deque = field(default_factory=lambda: deque(maxlen=20))
    quality_flow: deque = field(default_factory=lambda: deque(maxlen=100))
    # #[AUDIT-FIX] FATAL: 新增 wall-clock 时间戳用于显示，monotonic 时间不可用于 datetime.fromtimestamp
    last_received_wall_ts: datetime | None = None

    def record_receive(self, value: Any, quality: str = "good") -> None:
        now = time.monotonic()  # FIXED-P1: 使用monotonic时钟，防止NTP时钟回拨导致变化率检查失效
        self.last_received_at = now
        self.receive_count += 1
        self.last_value = value
        self.last_timestamp = now
        # #[AUDIT-FIX] FATAL: 记录 wall-clock 时间戳用于显示，monotonic 时间不可用于 fromtimestamp
        self.last_received_wall_ts = datetime.now(UTC)
        self.value_history.append(value)
        self.quality_flow.append({"ts": now, "quality": quality})

    def record_timeout(self) -> None:
        self.timeout_count += 1
        self.quality_flow.append({"ts": time.time(), "quality": "bad"})


@dataclass
class _WriteAuditEntry:
    timestamp: str
    device_id: str
    url: str
    method: str
    payload: str
    result: str
    status_code: int | None
    attempts: int
    user: str = ""


class _CachedResolver:
    def __init__(self, ttl: float = _DNS_CACHE_TTL):
        self._ttl = ttl
        self._cache: dict[str, list[tuple[int, int, int, int, str]]] = {}
        self._timestamps: dict[str, float] = {}
        self._lock = asyncio.Lock()  # FIXED-P2: 添加异步锁保护并发访问

    async def resolve(self, host: str, port: int = 0) -> list[tuple[int, int, int, int, str]]:
        now = time.monotonic()  # FIXED-P1: 使用monotonic时钟
        async with self._lock:  # FIXED-P2: 锁内读取缓存快照
            cached = self._cache.get(host)
            ts = self._timestamps.get(host, 0.0)
            if cached is not None and (now - ts) < self._ttl:
                return cached
        loop = asyncio.get_running_loop()
        try:
            addrs = await loop.getaddrinfo(host, port, family=socket.AF_INET, type=socket.SOCK_STREAM)
        except socket.gaierror:
            if cached is not None:
                return cached
            raise
        result = list(addrs)
        async with self._lock:  # FIXED-P2: 锁内写入缓存
            self._cache[host] = result
            self._timestamps[host] = now
        return result

    def clear(self) -> None:
        self._cache.clear()
        self._timestamps.clear()


class HttpWebhookDriver(DriverPlugin):
    """HTTP Webhook驱动，设备通过HTTP POST推送数据到EdgeLite"""

    plugin_name = "http_webhook"
    plugin_version = "0.1.0"
    supported_protocols = ("http", "webhook")  # FIXED(P2): 原问题-可变默认值list; 修复-改为tuple
    # #[AUDIT-FIX] WARNING: 缺失 _required_dependencies 声明，registry 无法预检 httpx 依赖
    _required_dependencies: tuple[str, ...] = ("httpx",)  # FIXED(P2): 原问题-可变默认值list; 修复-改为tuple
    config_schema = {
        "description": "HTTP/Webhook driver, receives data pushed from external devices via HTTP POST",
        "required": ["url"],
        "properties": {"url": {"type": "string", "description": "Webhook URL", "format": "url"}},
        "fields": [
            {
                "name": "url",
                "type": "string",
                "label": "Webhook URL",
                "description": "URL for receiving webhook data",
                "default": "",
            },
            {
                "name": "push_url",
                "type": "string",
                "label": "Push URL",
                "description": "URL to push data to device",
                "default": "",
            },
            {
                "name": "method",
                "type": "string",
                "label": "HTTP Method",
                "description": "HTTP request method",
                "default": "POST",
                "options": ["GET", "POST", "PUT", "DELETE", "PATCH"],
            },
            {
                "name": "interval",
                "type": "number",
                "label": "Poll Interval (s)",
                "description": "Polling interval in seconds",
                "default": 5.0,
            },
            {
                "name": "timeout",
                "type": "number",
                "label": "Timeout (s)",
                "description": "HTTP request timeout in seconds",
                "default": 10.0,
            },
            {
                "name": "connect_timeout",
                "type": "number",
                "label": "Connect Timeout (s)",
                "description": "Connection timeout in seconds",
                "default": 5.0,
                "min": 1,
                "max": 30,
            },
            {
                "name": "read_timeout",
                "type": "number",
                "label": "Read Timeout (s)",
                "description": "Read timeout in seconds",
                "default": 30.0,
                "min": 1,
                "max": 120,
            },
            {
                "name": "write_timeout",
                "type": "number",
                "label": "Write Timeout (s)",
                "description": "Write timeout in seconds",
                "default": 10.0,
                "min": 1,
                "max": 60,
            },
            {
                "name": "pool_max_connections",
                "type": "integer",
                "label": "Max Connections",
                "description": "Max total connections in pool",
                "default": 20,
                "min": 1,
                "max": 200,
            },
            {
                "name": "pool_max_keepalive",
                "type": "integer",
                "label": "Max Keepalive",
                "description": "Max keepalive connections in pool",
                "default": 10,
                "min": 1,
                "max": 100,
            },
            {
                "name": "health_check_timeout",
                "type": "number",
                "label": "Health Check Timeout (s)",
                "description": "HEAD request timeout for health check",
                "default": 5.0,
                "min": 1,
                "max": 30,
            },
            {
                "name": "health_response_threshold",
                "type": "number",
                "label": "Health Response Threshold (s)",
                "description": "Alert if HEAD response exceeds this duration",
                "default": 3.0,
                "min": 0.5,
                "max": 30,
            },
            {
                "name": "dns_cache_ttl",
                "type": "number",
                "label": "DNS Cache TTL (s)",
                "description": "DNS resolution cache time-to-live in seconds (max 60s to prevent DNS rebinding)",
                "default": 60.0,
                "min": 0,
                "max": 60,
            },
            {
                "name": "rate_of_change",
                "type": "number",
                "label": "Rate of Change",
                "description": "Max allowed value change rate per second (0=disabled)",
                "default": 0,
            },
            {
                "name": "frozen_count",
                "type": "integer",
                "label": "Frozen Count",
                "description": "Consecutive identical values to detect frozen (0=disabled)",
                "default": 0,
                "min": 0,
                "max": 100,
            },
            {
                "name": "max_payload_size",
                "type": "integer",
                "label": "Max Payload Size (bytes)",
                "description": "Maximum write payload size in bytes",
                "default": 65536,
                "min": 1024,
                "max": 1048576,
            },
            {
                "name": "write_rate_limit_ms",
                "type": "integer",
                "label": "Write Rate Limit (ms)",
                "description": "Minimum interval between writes to same URL in ms",
                "default": 100,
                "min": 0,
                "max": 10000,
            },
            {
                "name": "auth_type",
                "type": "string",
                "label": "Authentication",
                "description": "Authentication method: None, Basic, Bearer, OAuth2",
                "default": "None",
                "options": ["None", "Basic", "Bearer", "OAuth2"],
            },
            {
                "name": "auth_token",
                "type": "string",
                "label": "Auth Token",
                "description": "Authentication token or credentials",
                "default": "",
                "secret": True,
            },
            {
                "name": "headers",
                "type": "string",
                "label": "Custom Headers",
                "description": "Custom HTTP headers as JSON string",
                "default": "{}",
            },
            {
                "name": "body_template",
                "type": "string",
                "label": "Body Template",
                "description": "Request body template",
                "default": "",
            },
            {
                "name": "body_type",
                "type": "string",
                "label": "Body Type",
                "description": "Request body format",
                "default": "json",
                "options": ["json", "xml", "form", "raw"],
            },
            {
                "name": "max_retries",
                "type": "integer",
                "label": "Max Retries",
                "description": "Maximum number of retries on 5xx failure",
                "default": 3,
                "min": 0,
                "max": 10,
            },
            {
                "name": "retry_backoff",
                "type": "number",
                "label": "Retry Backoff (s)",
                "description": "Base retry delay in seconds (doubles each retry, max 60s)",
                "default": 1.0,
                "min": 0.1,
                "max": 30,
            },
            {
                "name": "allowed_hosts",
                "type": "string",
                "label": "Allowed Hosts",
                "description": "Comma-separated hostnames allowed to bypass private IP check",
                "default": "",
            },
            {
                "name": "deadband",
                "type": "string",
                "label": "Deadband",
                "description": "Deadband filter: number for absolute, or JSON {type,threshold} for percent",
                "default": "",
            },
            {
                "name": "scaling",
                "type": "string",
                "label": "Scaling",
                "description": "Linear scaling as JSON {ratio,offset}",
                "default": "",
            },
            {
                "name": "clamp",
                "type": "string",
                "label": "Clamp",
                "description": "Value clamp as JSON {min,max}. Out-of-range values marked bad quality",
                "default": "",
            },
            {
                "name": "ssl_verify",
                "type": "boolean",
                "label": "SSL Verify",
                "description": "Verify SSL/TLS certificates (default true, set false for self-signed certs in dev only)",
                "default": True,
            },
        ],
    }

    experimental = False
    capabilities = DriverCapabilities(
        discover=False, read=True, write=True, subscribe=False, batch_read=False, batch_write=False
    )
    constraints = (
        {
            "type": "field_effectiveness",
            "message": "body_template field: variable substitution support varies; verify with your payload format",
        },
    )  # FIXED(P2): 原问题-可变默认值list; 修复-改为tuple

    _DEFAULT_POOL_MAX = 20
    _DEFAULT_POOL_KEEPALIVE = 10
    _DEFAULT_HEALTH_TIMEOUT = 5.0
    _DEFAULT_HEALTH_THRESHOLD = 3.0
    _DEFAULT_DNS_TTL = 60.0  # #[AUDIT-FIX] W5: 从 300.0 调整为 60.0，与 _MAX_DNS_CACHE_TTL 一致，防止 DNS Rebinding
    _FROZEN_VALUE_WINDOW = 20

    def __init__(self):
        super().__init__()
        self._device_points: dict[str, list[dict]] = {}
        # CROSS-003: _latest_values 使用两层结构，设备级用普通 dict，测点级限制容量
        self._latest_values: dict[str, dict[str, Any]] = {}
        self._MAX_POINTS_PER_DEVICE = 1000  # CROSS-003: 每个设备最大测点数
        self._last_receive: dict[str, float] = {}
        # #[AUDIT-FIX] FATAL: 新增 wall-clock 时间戳字典，monotonic 时间不可用于 datetime.fromtimestamp
        self._last_receive_wall_ts: dict[str, datetime] = {}
        self._http_client: Any = None
        self._lock = asyncio.Lock()
        self._last_emitted: dict[str, dict[str, Any]] = {}
        self._dns_resolver: _CachedResolver | None = None
        self._health_latency: dict[str, float] = {}
        self._driver_config: dict = {}
        self._point_health: dict[str, dict[str, _PointHealth]] = {}
        self._last_write_time: dict[str, float] = {}
        self._write_audit_log: deque[_WriteAuditEntry] = deque(maxlen=1000)
        self._http_status_counter: dict[str, dict[int, int]] = {}
        self._dns_cleanup_task: asyncio.Task | None = None
        self._pinned_dns: dict[
            str, tuple[str, str, float]
        ] = {}  # HW-003: 固定 DNS 解析结果防止 DNS Rebinding  # FIXED-P1: 类型注解修正为3-tuple(safe_url, custom_host_header, resolved_time)，与实际存储一致
        self._MAX_PINNED_DNS = 1000  # FIXED-P2: _pinned_dns容量上限

    def _build_client(self, config: dict) -> Any:
        import httpx

        connect_timeout = float(config.get("connect_timeout", 5.0))
        read_timeout = float(config.get("read_timeout", 30.0))
        write_timeout = float(config.get("write_timeout", 10.0))
        timeout_cfg = httpx.Timeout(read_timeout, connect=connect_timeout, write=write_timeout)

        pool_max = int(config.get("pool_max_connections", self._DEFAULT_POOL_MAX))
        pool_keepalive = int(config.get("pool_max_keepalive", self._DEFAULT_POOL_KEEPALIVE))
        limits = httpx.Limits(max_connections=pool_max, max_keepalive_connections=pool_keepalive)

        dns_ttl = float(config.get("dns_cache_ttl", self._DEFAULT_DNS_TTL))
        # #[AUDIT-FIX] W5: 强制 DNS TTL 上限校验，防止配置绕过 DNS Rebinding 防护
        if dns_ttl > _MAX_DNS_CACHE_TTL:
            logger.warning(
                "[http_webhook] dns_cache_ttl=%.1fs exceeds max %.1fs (DNS rebinding protection), clamped to max",
                dns_ttl,
                _MAX_DNS_CACHE_TTL,
            )
            dns_ttl = _MAX_DNS_CACHE_TTL
        elif dns_ttl < 0:
            logger.warning("[http_webhook] dns_cache_ttl=%.1fs is negative, disabled DNS cache", dns_ttl)
            dns_ttl = 0.0
        resolver = _CachedResolver(ttl=dns_ttl) if dns_ttl > 0 else None
        self._dns_resolver = resolver

        client_kwargs: dict[str, Any] = {
            "timeout": timeout_cfg,
            "limits": limits,
            # HW-003: 禁用重定向，防止 DNS Rebinding 绕过 SSRF 防护
            "follow_redirects": False,
            # FIXED-P2: 添加 ssl_verify 配置，默认 True 验证证书，false 用于开发环境自签名证书
            "verify": bool(config.get("ssl_verify", True)),
        }

        if resolver is not None:
            with contextlib.suppress(TypeError):
                client_kwargs["transport"] = httpx.AsyncHTTPTransport(resolver=resolver.resolve)

        auth_type = config.get("auth_type", "None")
        if auth_type == "Bearer":
            client_kwargs["headers"] = {"Authorization": f"Bearer {config.get('auth_token', '')}"}
        elif auth_type == "Basic":
            token = config.get("auth_token", "")
            if ":" in token:
                user, pwd = token.split(":", 1)
                client_kwargs["auth"] = (user, pwd)

        return httpx.AsyncClient(**client_kwargs)

    async def start(self, config: dict) -> None:
        self._driver_config = config
        try:
            self._http_client = self._build_client(config)
        except ImportError:
            self._http_client = None
        self._running = True
        self._dns_cleanup_task = asyncio.create_task(self._dns_cache_cleanup_loop())
        logger.info("HTTP Webhook driver started")

    async def _dns_cache_cleanup_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(3600)
                if self._dns_resolver:
                    self._dns_resolver.clear()
                    logger.debug("[http_webhook] DNS cache cleared")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("[http_webhook] DNS cache cleanup error: %s", e)

    async def stop(self) -> None:
        if self._dns_cleanup_task and not self._dns_cleanup_task.done():
            self._dns_cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._dns_cleanup_task
            self._dns_cleanup_task = None
        try:
            if self._http_client:
                try:
                    await self._http_client.aclose()
                except Exception as e:
                    logger.debug("[http_webhook] error: %s", e)
        finally:
            self._running = False
            self._http_client = None
            # CROSS-004: 取消所有后台任务
            await self._cancel_background_tasks()
            if self._dns_resolver:
                self._dns_resolver.clear()
                self._dns_resolver = None
            # FIXED-P0: 状态清理不应嵌套在 if self._dns_resolver 内，DNS初始化失败时清理被跳过
            self._device_configs.clear()
            self._device_points.clear()
            self._latest_values.clear()
            self._last_receive.clear()
            # #[AUDIT-FIX] FATAL: 清理 wall-clock 时间戳字典
            self._last_receive_wall_ts.clear()
            self._last_emitted.clear()
            self._health_latency.clear()
            self._point_health.clear()
            self._last_write_time.clear()
            self._http_status_counter.clear()
            # HW-003: 清理固定 DNS 缓存
            self._pinned_dns.clear()
            logger.info("HTTP Webhook driver stopped")

    async def add_device(self, device_id: str, config: dict, points: list[dict]) -> None:
        self._device_configs[device_id] = config
        self._device_points[device_id] = points
        self._latest_values[device_id] = {}
        self._last_receive[device_id] = 0
        self._last_emitted[device_id] = {}
        self._point_health[device_id] = {}
        logger.info("HTTP Webhook device registered: %s", device_id)

    async def remove_device(self, device_id: str) -> None:
        self._device_configs.pop(device_id, None)
        self._device_points.pop(device_id, None)
        self._latest_values.pop(device_id, None)
        self._last_receive.pop(device_id, None)
        self._last_emitted.pop(device_id, None)
        # #[AUDIT-FIX] FATAL: 清理 wall-clock 时间戳，避免内存泄漏
        self._last_receive_wall_ts.pop(device_id, None)
        self._health_latency.pop(device_id, None)
        self._point_health.pop(device_id, None)
        keys_to_remove = [k for k in self._last_write_time if k.startswith(f"{device_id}:")]
        for k in keys_to_remove:
            del self._last_write_time[k]
        self._http_status_counter.pop(device_id, None)

    _MAX_POINT_HEALTH_PER_DEVICE = 5000  # FIXED-P2: 每设备测点健康条目上限

    def _get_point_health(self, device_id: str, point_name: str) -> _PointHealth:
        dev_health = self._point_health.setdefault(device_id, {})
        if point_name not in dev_health:
            # FIXED-P2: 容量超限时淘汰最旧条目
            if len(dev_health) >= self._MAX_POINT_HEALTH_PER_DEVICE:
                dev_health.pop(next(iter(dev_health)), None)
            dev_health[point_name] = _PointHealth()
        return dev_health[point_name]

    def get_point_health(self, device_id: str, point_name: str) -> dict[str, Any]:
        ph = self._point_health.get(device_id, {}).get(point_name)
        if ph is None:
            return {"last_received_at": 0.0, "receive_count": 0, "timeout_count": 0}
        return {
            "last_received_at": ph.last_received_at,
            "receive_count": ph.receive_count,
            "timeout_count": ph.timeout_count,
        }

    def get_write_audit_log(self, device_id: str | None = None, limit: int = 100) -> list[dict]:
        entries = list(self._write_audit_log)
        if device_id:
            entries = [e for e in entries if e.device_id == device_id]
        return [
            {
                "timestamp": e.timestamp,
                "user": e.user,
                "device_id": e.device_id,
                "url": e.url,
                "method": e.method,
                "payload": e.payload,
                "result": e.result,
                "status_code": e.status_code,
                "attempts": e.attempts,
            }
            for e in entries[-limit:]
        ]

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        async with self._lock:
            values = self._latest_values.get(device_id, {})
            last_recv = self._last_receive.get(device_id, 0)
            timed_out = (
                last_recv > 0 and (time.monotonic() - last_recv) >= self._OFFLINE_TIMEOUT
            )  # FIXED-P1: 使用monotonic防止时钟回拨误判

            result: dict[str, Any] = {}
            for p in points:
                if timed_out:
                    result[p] = _bad_pv("ERR_WEBHOOK_OFFLINE_BAD_QUALITY")
                    self._get_point_health(device_id, p).record_timeout()
                elif p not in values:
                    result[p] = _bad_pv("ERR_WEBHOOK_READ_TIMEOUT")
                    self._get_point_health(device_id, p).record_timeout()
                else:
                    result[p] = values[p]

            has_good = any(not isinstance(v, PointValue) or v.quality != "bad" for v in result.values())
            if has_good:
                await self._record_read_success(
                    device_id
                )  # #[AUDIT-FIX] _record_read_success is async, must await (was no-op coroutine)
            else:
                self._record_read_failure(device_id)

            return result

    def _validate_write_payload(
        self, device_id: str, point: str, value: Any, payload: dict | None, max_size: int
    ) -> str | None:
        if payload is not None:
            try:
                payload_bytes = _json.dumps(payload).encode("utf-8")
                if len(payload_bytes) > max_size:
                    return f"payload_size={len(payload_bytes)} max={max_size}"
            except (TypeError, ValueError) as e:
                return f"payload_serialize_error={e}"
        if not point or not isinstance(point, str):
            return "point_name_invalid"
        if value is None:
            return "value_is_none"
        return None

    async def _enforce_write_rate_limit(self, device_id: str, url: str, rate_limit_ms: int) -> None:
        key = f"{device_id}:{url}"
        now = time.monotonic()
        last = self._last_write_time.get(key, 0.0)
        min_interval = rate_limit_ms / 1000.0
        elapsed = now - last
        if elapsed < min_interval:
            await asyncio.sleep(min_interval - elapsed)
        self._last_write_time[key] = time.monotonic()

    def _record_write_audit(
        self, device_id: str, url: str, method: str, payload: str, result: str, status_code: int | None, attempts: int
    ) -> None:
        entry = _WriteAuditEntry(
            timestamp=datetime.now(UTC).isoformat(),
            device_id=device_id,
            url=url,
            method=method,
            payload=payload[:512],
            result=result,
            status_code=status_code,
            attempts=attempts,
        )
        self._write_audit_log.append(entry)

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        # FIXED-P0: 缩小锁范围，HTTP请求在锁外执行
        config = self._device_configs.get(device_id, {})
        push_url = config.get("push_url")

        if not push_url:
            self._log_error(device_id, "ERR_WEBHOOK_CONFIG_INVALID", "push_url not configured")
            self._record_write_audit(device_id, "", "N/A", f"point={point} value={value}", "rejected_no_url", None, 0)
            return False

        method = config.get("method", "POST").upper()
        max_payload_size = int(config.get("max_payload_size", _MAX_PAYLOAD_SIZE))
        rate_limit_ms = int(config.get("write_rate_limit_ms", _WRITE_RATE_LIMIT_MS))

        body = {"point": point, "value": value}
        validation_error = self._validate_write_payload(device_id, point, value, body, max_payload_size)
        if validation_error:
            self._log_error(device_id, "ERR_WEBHOOK_WRITE_PAYLOAD_INVALID", validation_error)
            self._record_write_audit(
                device_id, push_url, method, _json.dumps(body), f"rejected:{validation_error}", None, 0
            )
            return False

        # FIXED-P0: 速率限制sleep在锁外执行，不再阻塞其他操作
        await self._enforce_write_rate_limit(device_id, push_url, rate_limit_ms)

        # FIXED-P0: URL验证(DNS解析)在锁外执行
        url_error = await self._validate_webhook_url(push_url, config, device_id)
        if url_error:
            self._log_error(device_id, "ERR_WEBHOOK_URL_BLOCKED", url_error)
            self._record_write_audit(device_id, push_url, method, _json.dumps(body), f"rejected:{url_error}", None, 0)
            return False

        try:
            import httpx

            if not self._http_client:
                # FIXED-P0: 重建前关闭旧实例连接池，防止连接池泄漏
                old_client = self._http_client
                self._http_client = self._build_client(config)
                if old_client is not None:
                    with contextlib.suppress(Exception):
                        await old_client.aclose()

            custom_headers = {}
            with contextlib.suppress(Exception):
                custom_headers = _json.loads(config.get("headers", "{}"))

            max_retries = int(config.get("max_retries", 3))
            retry_backoff = float(config.get("retry_backoff", 1.0))
            connect_timeout = float(config.get("connect_timeout", 5.0))
            read_timeout = float(config.get("read_timeout", 30.0))
            write_timeout = float(config.get("write_timeout", 10.0))
            req_timeout = httpx.Timeout(read_timeout, connect=connect_timeout, read=read_timeout, write=write_timeout)

            resp = None
            last_status = None
            attempts = 0

            for attempt in range(max_retries + 1):
                attempts += 1
                try:
                    # HW-003: 在每次请求前检查 DNS 是否需要重新验证
                    dns_error = await self._revalidate_dns(push_url)
                    if dns_error:
                        self._log_error(device_id, "ERR_WEBHOOK_DNS_REBINDING_BLOCKED", dns_error)
                        self._record_write_audit(
                            device_id, push_url, method, _json.dumps(body), f"rejected:{dns_error}", None, attempts
                        )
                        return False

                    pinned = self._pinned_dns.get(push_url)
                    req_url = pinned[0] if pinned else push_url
                    if pinned:
                        custom_headers["Host"] = pinned[1]
                    # FIXED-P1: stop()/write并发时client可能被置None，使用前检查
                    http_client = self._http_client
                    if http_client is None:
                        self._log_error(device_id, "ERR_WEBHOOK_SHUTTING_DOWN", "HTTP client disposed during write")
                        return False
                    if method == "GET":
                        resp = await http_client.get(
                            req_url,
                            params={"point": point, "value": value},
                            headers=custom_headers,
                            timeout=req_timeout,
                        )
                    elif method == "DELETE":
                        resp = await http_client.delete(
                            req_url,
                            params={"point": point, "value": value},
                            headers=custom_headers,
                            timeout=req_timeout,
                        )
                    elif method == "PUT":
                        resp = await http_client.put(req_url, json=body, headers=custom_headers, timeout=req_timeout)
                    elif method == "PATCH":
                        resp = await http_client.patch(req_url, json=body, headers=custom_headers, timeout=req_timeout)
                    else:
                        resp = await http_client.post(req_url, json=body, headers=custom_headers, timeout=req_timeout)

                    last_status = resp.status_code
                    # FIXED-P0: 缩小锁范围，仅保护共享数据结构更新
                    async with self._lock:
                        dev_counter = self._http_status_counter.setdefault(device_id, {})
                        dev_counter[last_status] = dev_counter.get(last_status, 0) + 1

                    try:
                        from edgelite.packet_recorder import record_packet

                        record_packet("tx", "http_webhook", device_id, f"{method} {push_url}")
                    except ImportError:
                        pass

                    if 400 <= last_status < 500:
                        self._log_error(device_id, "ERR_WEBHOOK_WRITE_CLIENT_ERROR", f"status={last_status}")
                        break

                    if last_status < 500:
                        break

                    if attempt < max_retries:
                        import random

                        delay = min(retry_backoff * (2**attempt) + random.uniform(0, 0.5), _RETRY_BACKOFF_MAX)
                        self._log_error(
                            device_id,
                            "ERR_WEBHOOK_WRITE_RETRY",
                            f"attempt={attempt + 1}/{max_retries} status={last_status} delay={delay:.2f}s",
                        )
                        await asyncio.sleep(delay)
                except Exception as e:
                    last_status = None
                    if attempt < max_retries:
                        import random

                        delay = min(retry_backoff * (2**attempt) + random.uniform(0, 0.5), _RETRY_BACKOFF_MAX)
                        self._log_error(
                            device_id,
                            "ERR_WEBHOOK_WRITE_RETRY",
                            f"attempt={attempt + 1}/{max_retries} error={e} delay={delay:.2f}s",
                        )
                        await asyncio.sleep(delay)
                    else:
                        raise

            ok = resp is not None and last_status == 200
            result_str = "success" if ok else f"failed:status={last_status}"

            # FIXED-P0: 缩小锁范围，仅保护共享数据结构更新
            async with self._lock:
                if ok:
                    self._record_write_success(device_id)
                else:
                    self._record_write_failure(device_id)
                    if last_status and last_status >= 500:
                        self._log_error(
                            device_id, "ERR_WEBHOOK_WRITE_SERVER_ERROR", f"status={last_status} attempts={attempts}"
                        )

            self._record_write_audit(device_id, push_url, method, _json.dumps(body), result_str, last_status, attempts)
            return ok
        except Exception as e:
            self._record_write_failure(device_id)
            self._log_error(device_id, "ERR_WEBHOOK_WRITE_FAILED", str(e))
            self._record_write_audit(
                device_id,
                push_url,
                method,
                _json.dumps(body),
                f"failed:exception={e}",
                None,
                attempts if attempts else 1,
            )
            return False

    def on_data(self, callback: Callable) -> None:
        self._data_callback = callback

    async def receive_data(self, device_id: str, data: dict[str, Any]) -> None:
        if device_id not in self._device_configs:
            self._log_error(device_id, "ERR_WEBHOOK_DEVICE_NOT_REGISTERED", device_id)
            return

        parsed = self._parse_payload(data, device_id)
        processed = self._transform_data(device_id, parsed)
        data_to_emit = None
        async with self._lock:
            try:
                # CROSS-003: 添加 LRU 限制
                point_values = self._latest_values.setdefault(device_id, {})
                point_values.update(processed)
                # 限制每个设备的测点数量
                while len(point_values) > self._MAX_POINTS_PER_DEVICE:
                    point_values.pop(next(iter(point_values)))
                self._last_receive[device_id] = time.monotonic()  # FIXED-P1: 使用monotonic与读取端一致
                # #[AUDIT-FIX] FATAL: 同步记录 wall-clock 时间戳用于显示，monotonic 不能用于 datetime.fromtimestamp
                self._last_receive_wall_ts[device_id] = datetime.now(UTC)

                try:
                    from edgelite.packet_recorder import record_packet

                    record_packet("rx", "http_webhook", device_id, f"RECEIVE {device_id}")
                except ImportError:
                    pass

                if self._data_callback:
                    data_to_emit = (device_id, processed)
            except Exception as e:
                self._log_error(device_id, "ERR_WEBHOOK_DATA_PROCESS_FAILED", str(e))
        # FIXED-P1: 回调在锁外执行，避免回调中获取同一把锁导致死锁
        if data_to_emit:
            await self._data_callback(*data_to_emit)

    def _parse_payload(self, data: dict[str, Any], device_id: str = "") -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, raw_value in data.items():
            if isinstance(raw_value, str):
                try:
                    parsed = _json.loads(raw_value)
                    if isinstance(parsed, dict):
                        result.update(parsed)
                        continue
                    result[key] = parsed
                except (_json.JSONDecodeError, ValueError):
                    if device_id:
                        self._log_error(
                            device_id, "ERR_WEBHOOK_PAYLOAD_PARSE_FAILED", f"point={key} raw={raw_value[:80]}"
                        )
                    result[key] = _uncertain_pv(raw_value, "ERR_WEBHOOK_PAYLOAD_PARSE_FAILED")
            else:
                result[key] = raw_value
        return result

    def _transform_data(self, device_id: str, data: dict) -> dict[str, Any]:
        points = self._device_points.get(device_id, [])
        point_names = {p.get("name") for p in points if p.get("name") is not None}
        point_map = {p.get("name"): p for p in points if p.get("name") is not None}

        config = self._device_configs.get(device_id, {})
        global_deadband = self._parse_optional_json(config.get("deadband"))
        global_scaling = self._parse_optional_json(config.get("scaling"))
        global_clamp = self._parse_optional_json(config.get("clamp"))
        global_roc = float(config.get("rate_of_change", 0))
        global_frozen = int(config.get("frozen_count", 0))

        result = {}
        for key, value in data.items():
            if key not in point_names and point_names:
                continue

            if isinstance(value, PointValue):
                ph = self._get_point_health(device_id, key)
                ph.record_receive(value.value, quality=value.quality)
                result[key] = value
                continue

            point_def = point_map.get(key)
            ph = self._get_point_health(device_id, key)

            if point_def:
                value = self._cast_value(value, point_def.get("data_type", "float32"))

            if isinstance(value, (int, float)):
                pt_scaling = point_def.get("scaling") if point_def else None
                scaling = pt_scaling if pt_scaling else global_scaling
                if scaling:
                    value = self._apply_scaling(value, scaling)

                pt_deadband = point_def.get("deadband") if point_def else None
                deadband = pt_deadband if pt_deadband else global_deadband
                if deadband:
                    last_emitted = self._last_emitted.get(device_id, {}).get(key)
                    value = self._apply_deadband(value, last_emitted, deadband)

                pt_clamp = point_def.get("clamp") if point_def else None
                clamp = pt_clamp if pt_clamp else global_clamp
                if clamp:
                    clamped, ok = self._apply_clamp(value, clamp)
                    if not ok:
                        self._log_error(device_id, "ERR_WEBHOOK_VALUE_OUT_OF_RANGE", f"point={key} value={value}")
                        continue
                    value = clamped

                pt_roc = point_def.get("rate_of_change") if point_def else None
                roc_threshold = pt_roc if pt_roc else global_roc
                if roc_threshold and roc_threshold > 0:
                    if self._check_rate_of_change(device_id, key, value, roc_threshold):
                        self._log_error(
                            device_id,
                            "ERR_WEBHOOK_RATE_OF_CHANGE",
                            f"point={key} value={value} threshold={roc_threshold}",
                        )

                pt_frozen = point_def.get("frozen_count") if point_def else None
                frozen_threshold = pt_frozen if pt_frozen else global_frozen
                if frozen_threshold and frozen_threshold > 0:
                    if self._check_frozen_value(device_id, key, value, frozen_threshold):
                        self._log_error(
                            device_id, "ERR_WEBHOOK_FROZEN_VALUE", f"point={key} value={value} count={frozen_threshold}"
                        )

                if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
                    self._log_error(device_id, "ERR_WEBHOOK_NAN_INF", f"point={key} value={value}")
                    continue

            ph.record_receive(value)
            result[key] = value
            self._last_emitted.setdefault(device_id, {})[key] = value

        return result

    def _check_rate_of_change(self, device_id: str, point: str, value: float, threshold: float) -> bool:
        ph = self._get_point_health(device_id, point)
        if ph.last_value is None or ph.last_timestamp <= 0:
            return False
        dt = time.monotonic() - ph.last_timestamp  # FIXED-P1: 使用monotonic时钟
        if dt <= 0:
            return False
        rate = abs(value - ph.last_value) / dt
        return rate > threshold

    def _check_frozen_value(self, device_id: str, point: str, value: float, window: int) -> bool:
        ph = self._get_point_health(device_id, point)
        if len(ph.value_history) >= window and window > 0:
            if all(abs(v - value) < 1e-9 for v in list(ph.value_history)[-window:]):
                return True
        return False

    @staticmethod
    def _cast_value(value: Any, data_type: str) -> Any:
        try:
            if data_type in ("int16", "int32", "uint16", "uint32", "int", "integer"):
                return int(float(value))
            elif data_type in ("float32", "float64", "float", "double"):
                return float(value)
            elif data_type in ("bool", "boolean"):
                if isinstance(value, str):
                    return value.lower() in ("true", "1", "yes")
                return bool(value)
            return value
        except (ValueError, TypeError):
            return value

    @staticmethod
    def _parse_optional_json(value: Any) -> Any:
        if value is None or value == "" or value == "{}":
            return None
        if isinstance(value, str):
            try:
                parsed = _json.loads(value)
                return parsed if parsed else None
            except (ValueError, TypeError):
                try:
                    return float(value)
                except (ValueError, TypeError):
                    return None
        return value if value else None

    def _log_error(self, device_id: str, error_code: str, message: str) -> None:
        i18n_msg = _t(error_code) if error_code.startswith("ERR_") else error_code
        logger.error("[http_webhook] device=%s code=%s i18n=%s msg=%s", device_id, error_code, i18n_msg, message)

    @staticmethod
    def _is_private_ip(ip_str: str) -> bool:
        try:
            ip = ipaddress.ip_address(ip_str)
            return any(ip in network for network in _PRIVATE_NETWORKS)
        except ValueError:
            return False

    async def _validate_webhook_url(self, url: str, config: dict, device_id: str) -> str | None:
        from urllib.parse import urlparse as _urlparse

        parsed = _urlparse(url)
        if not parsed.hostname:
            return "invalid_url_no_host"
        allowed_hosts = config.get("allowed_hosts", "")
        if isinstance(allowed_hosts, str):
            allowed_hosts = [h.strip() for h in allowed_hosts.split(",") if h.strip()]
        if parsed.hostname in allowed_hosts:
            return None
        resolved_ip = None
        resolved_time = time.monotonic()  # FIXED-P1: 使用monotonic时钟，防止系统时间回拨导致DNS缓存TTL失效
        try:
            if self._dns_resolver:
                addrs = await self._dns_resolver.resolve(parsed.hostname)
                for addr_info in addrs:
                    ip_str = addr_info[4][0]
                    if self._is_private_ip(ip_str):
                        return f"private_ip_blocked:{ip_str}"
                    if resolved_ip is None:
                        resolved_ip = ip_str
            else:
                # FIXED-P0: 将同步DNS解析移入线程池，防止阻塞事件循环
                # 之前：socket.getaddrinfo()同步调用，DNS慢时阻塞整个事件循环
                # 之后：使用asyncio.to_thread()异步执行DNS解析
                def _sync_resolve(hostname):
                    results = []
                    for family in (socket.AF_INET, socket.AF_INET6):
                        try:
                            resolved = socket.getaddrinfo(hostname, None, family)
                            results.extend(resolved)
                        except (socket.gaierror, OSError):
                            continue
                    return results

                resolved = await asyncio.to_thread(_sync_resolve, parsed.hostname)
                for addr_info in resolved:
                    ip_str = addr_info[4][0]
                    if self._is_private_ip(ip_str):
                        return f"private_ip_blocked:{ip_str}"
                    if resolved_ip is None:
                        resolved_ip = ip_str
        except (socket.gaierror, OSError):
            return "dns_resolution_failed"
        if resolved_ip and parsed.hostname != resolved_ip:
            # FIXED-P1: 使用urllib.parse重建URL而非字符串替换，避免误替换query/path中的同名子串
            from urllib.parse import urlunparse

            safe_url = urlunparse(
                (
                    parsed.scheme,
                    f"{resolved_ip}:{parsed.port}" if parsed.port else resolved_ip,
                    parsed.path,
                    parsed.params,
                    parsed.query,
                    parsed.fragment,
                )
            )
            custom_host_header = parsed.hostname
            # HW-003: 固定 DNS 解析结果，设置时间戳用于 TTL 检查
            # FIXED-P2: 容量超限时淘汰最旧条目
            if len(self._pinned_dns) >= self._MAX_PINNED_DNS and url not in self._pinned_dns:
                self._pinned_dns.pop(next(iter(self._pinned_dns)), None)
            self._pinned_dns[url] = (safe_url, custom_host_header, resolved_time)
        return None

    async def _revalidate_dns(self, url: str) -> str | None:
        """HW-003: 在请求前重新验证 DNS 解析结果，防止 DNS Rebinding 攻击

        检查固定 DNS 是否过期（超过 _MAX_DNS_CACHE_TTL 秒）
        如果过期，重新解析并验证 IP
        """
        from urllib.parse import urlparse as _urlparse

        pinned = self._pinned_dns.get(url)
        if not pinned:
            return None

        validated_url, custom_host_header, resolved_time = pinned
        current_time = time.monotonic()  # FIXED-P1: 使用monotonic时钟防止系统时间回拨导致TTL检查失效

        # 检查 TTL 是否过期
        if (current_time - resolved_time) > _MAX_DNS_CACHE_TTL:
            logger.debug("[http_webhook] HW-003: DNS cache expired for %s, revalidating", url)
            # TTL 过期，需要重新验证
            parsed = _urlparse(url)
            if not parsed.hostname:
                return "invalid_url_no_host"

            try:
                # 重新解析 DNS
                if self._dns_resolver:
                    addrs = await self._dns_resolver.resolve(parsed.hostname)
                else:
                    # FIXED-P1: 同时检查IPv4和IPv6，防止IPv6绕过SSRF防护
                    # FIXED-P1: DNS回退路径使用asyncio.to_thread，防止阻塞事件循环
                    addrs = []
                    for family in (socket.AF_INET, socket.AF_INET6):
                        try:
                            family_addrs = await asyncio.to_thread(socket.getaddrinfo, parsed.hostname, None, family)
                            addrs.extend(family_addrs)
                        except (socket.gaierror, OSError):
                            continue

                current_ip = None
                for addr_info in addrs:
                    ip_str = addr_info[4][0]
                    if self._is_private_ip(ip_str):
                        return f"dns_rebinding_blocked:{ip_str}"
                    if current_ip is None:
                        current_ip = ip_str

                if current_ip and parsed.hostname != current_ip:
                    # FIXED-P1: 使用urllib.parse重建URL而非字符串替换，避免误替换query/path中的同名子串
                    from urllib.parse import urlunparse as _urlunparse

                    new_validated_url = _urlunparse(
                        (
                            parsed.scheme,
                            f"{current_ip}:{parsed.port}" if parsed.port else current_ip,
                            parsed.path,
                            parsed.params,
                            parsed.query,
                            parsed.fragment,
                        )
                    )
                    # 更新固定 DNS
                    self._pinned_dns[url] = (new_validated_url, parsed.hostname, current_time)
                    logger.debug("[http_webhook] HW-003: DNS revalidated for %s, new IP=%s", url, current_ip)
                    return None
                elif current_ip is None:
                    return "dns_resolution_failed"
                else:
                    # IP 未变化，更新时间戳
                    self._pinned_dns[url] = (validated_url, custom_host_header, current_time)
                    return None
            except (socket.gaierror, OSError):
                return "dns_resolution_failed"

        return None

    def get_last_receive_time(self, device_id: str) -> float:
        return self._last_receive.get(device_id, 0)

    def get_health_latency(self, device_id: str) -> float:
        return self._health_latency.get(device_id, 0.0)

    _OFFLINE_TIMEOUT = 60

    def is_device_connected(self, device_id: str) -> bool:
        if device_id not in self._device_configs:
            return False
        last = self._last_receive.get(device_id, 0)
        if last == 0:
            return True
        return (time.monotonic() - last) < self._OFFLINE_TIMEOUT  # FIXED-P1: 使用monotonic

    async def health_check(self, device_id: str) -> bool:
        if not self._running:
            return False
        config = self._device_configs.get(device_id, {})
        push_url = config.get("push_url")
        if not push_url:
            return self.is_device_connected(device_id)
        if not self._http_client:
            return self.is_device_connected(device_id)
        url_error = await self._validate_webhook_url(push_url, config, device_id)
        if url_error:
            self._log_error(device_id, "ERR_WEBHOOK_URL_BLOCKED", url_error)
            return False

        # HW-003: 在健康检查前重新验证 DNS
        dns_error = await self._revalidate_dns(push_url)
        if dns_error:
            self._log_error(device_id, "ERR_WEBHOOK_DNS_REBINDING_BLOCKED", dns_error)
            return False

        health_timeout = float(
            config.get(
                "health_check_timeout", self._driver_config.get("health_check_timeout", self._DEFAULT_HEALTH_TIMEOUT)
            )
        )
        response_threshold = float(
            config.get(
                "health_response_threshold",
                self._driver_config.get("health_response_threshold", self._DEFAULT_HEALTH_THRESHOLD),
            )
        )

        try:
            t0 = time.monotonic()
            pinned = self._pinned_dns.get(push_url)
            req_url = pinned[0] if pinned else push_url
            req_headers = {}
            if pinned:
                req_headers["Host"] = pinned[1]
            resp = await self._http_client.head(req_url, headers=req_headers, timeout=health_timeout)
            elapsed = time.monotonic() - t0
            self._health_latency[device_id] = elapsed

            if elapsed > response_threshold:
                self._log_error(
                    device_id, "ERR_WEBHOOK_HEALTH_SLOW", f"response={elapsed:.3f}s threshold={response_threshold:.1f}s"
                )

            if resp.status_code >= 500:
                self._log_error(device_id, "ERR_WEBHOOK_HEALTH_SERVER_ERROR", f"status={resp.status_code}")
                return False

            return True
        except TimeoutError:
            self._log_error(device_id, "ERR_WEBHOOK_HEALTH_TIMEOUT", f"timeout={health_timeout}s")
            self._record_read_failure(device_id)
            return False
        except Exception as e:
            self._log_error(device_id, "ERR_WEBHOOK_HEALTH_FAILED", str(e))
            return False

    def get_point_stats(self, device_id: str, point_name: str) -> dict[str, Any] | None:
        ph = self._point_health.get(device_id, {}).get(point_name)
        if ph is None:
            return None
        current_quality = "good"
        device_offline = not self.is_device_connected(device_id)
        if device_offline or ph.timeout_count > 0 and ph.receive_count == 0:
            current_quality = "bad"
        elif ph.receive_count > 0:
            last_recv_age = time.monotonic() - ph.last_received_at  # FIXED-P1: 使用monotonic
            if last_recv_age >= self._OFFLINE_TIMEOUT:
                current_quality = "bad"
            elif ph.timeout_count > ph.receive_count // 2:
                current_quality = "uncertain"
        return {
            "success_count": ph.receive_count,
            "fail_count": ph.timeout_count,
            "avg_latency_ms": 0,
            "consecutive_fails": ph.timeout_count,
            "success_rate": ph.receive_count / max(ph.receive_count + ph.timeout_count, 1),
            "quality_history": list(ph.quality_flow)[-100:],
            "current_quality": current_quality,
            # #[AUDIT-FIX] FATAL: 使用 wall-clock 时间戳替代 monotonic，monotonic 不能用于 fromtimestamp
            "last_success_at": ph.last_received_wall_ts.isoformat() if ph.last_received_wall_ts else None,
            "last_received_at": ph.last_received_at,
            "receive_count": ph.receive_count,
            "timeout_count": ph.timeout_count,
            "last_value": ph.last_value,
        }

    def get_health_stats(self, device_id: str) -> dict[str, Any] | None:
        base = self._health_stats.get(device_id)
        connected = self.is_device_connected(device_id)
        latency = self._health_latency.get(
            device_id, 0.0
        )  # FIXED-P3: 原问题-last_recv赋值后未使用(ruff F841); 修复-移除无用赋值，last_receive_time已从_last_receive_wall_ts获取
        status_dist = self._http_status_counter.get(device_id, {})

        online_rate = 1.0
        if base:
            total = base.total_reads
            failed = base.failed_reads
            if total > 0:
                online_rate = 1.0 - (failed / total)

        return {
            "total_reads": base.total_reads if base else 0,
            "failed_reads": base.failed_reads if base else 0,
            "total_writes": base.total_writes if base else 0,
            "failed_writes": base.failed_writes if base else 0,
            "total_reconnects": base.total_reconnects if base else 0,
            "avg_latency_ms": latency * 1000,
            "online_rate": online_rate,
            "state": "connected" if connected else "disconnected",
            # #[AUDIT-FIX] FATAL: 使用 wall-clock 时间戳替代 monotonic，monotonic 不可用于 fromtimestamp
            "last_receive_time": self._last_receive_wall_ts.get(device_id).isoformat()
            if device_id in self._last_receive_wall_ts
            else None,
            "health_latency_s": latency,
            "http_status_distribution": dict(status_dist),
        }
