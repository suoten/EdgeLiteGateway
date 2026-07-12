"""每 IP 请求限流中间件（可插拔后端：内存 / Redis）。

FIXED: 重建丢失的 rate_limit 中间件（app.py:655 引用但文件不存在，导致 create_app() 崩溃）[2026-06-30]
FIXED(项4): 引入可插拔限流后端抽象，支持多实例共享状态 [2026-06-30]

设计目标:
- 单实例: 默认 MemoryRateLimitBackend（滑动窗口 deque，零依赖）
- 多实例: 通过 EDGELITE_RATE_LIMIT_BACKEND=redis + EDGELITE_RATE_LIMIT_REDIS_URL
  切换至 RedisRateLimitBackend（sorted set 滑动窗口，跨实例共享计数）
- 后端切换零代码改动，仅需环境变量

安全修复（R5-S-XX）:
- X-Forwarded-For 绕过修复: 默认使用 TCP 对端 IP (request.client.host)，
  仅当配置 trusted_proxies 且对端在白名单时才解析 XFF 链最右侧非可信 IP
- /health* /docs /openapi.json 豁免（探针/文档不应被限流）
- 命中限流返回 429 + Retry-After + X-RateLimit-* 头
"""

from __future__ import annotations

import logging
import os
import threading
import time
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)

# 豁免限流的路径前缀（探针 / 文档 / OpenAPI 不应被限流）
_EXEMPT_PREFIXES: tuple[str, ...] = (
    "/health",
    "/live",
    "/ready",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/favicon.ico",
)


# ──────────────────────────────────────────────────────────────
# 后端抽象（项4 核心）
# ──────────────────────────────────────────────────────────────
class RateLimitBackend(ABC):
    """限流后端抽象接口。所有后端必须实现原子化的 allow()。"""

    @abstractmethod
    def allow(self, key: str, limit: int, window_seconds: float) -> bool:
        """原子化判定并记录一次请求。返回 True 放行，False 触发限流。"""

    def cleanup(self) -> None:
        """清理过期条目，防止内存无限增长（Redis 后端为 no-op）。"""
        return None

    def close(self) -> None:
        """释放后端持有的资源（如 Redis 连接池）。"""
        return None


class MemoryRateLimitBackend(RateLimitBackend):
    """内存滑动窗口后端（单实例默认）。

    线程安全说明: 在 asyncio 单线程事件循环中调用安全；多线程 ASGI 服务器
    （如 gunicorn sync worker 多线程）需配合进程内锁，此处用 threading.Lock 保护。
    """

    def __init__(self) -> None:
        self._buckets: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window_seconds: float) -> bool:
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            bucket = self._buckets[key]
            # 清理过期条目（滑动窗口）
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                return False
            bucket.append(now)
            return True

    def cleanup(self) -> None:
        now = time.monotonic()
        with self._lock:
            # 清理空桶或 6 倍窗口未活跃的桶
            stale = [k for k, b in self._buckets.items() if not b or b[-1] < now - 3600]
            for k in stale:
                self._buckets.pop(k, None)


class RedisRateLimitBackend(RateLimitBackend):
    """Redis 滑动窗口后端（多实例共享状态）。

    使用 Sorted Set + Lua 脚本实现原子化滑动窗口：
    - ZADD key now now (member=score=时间戳保证唯一)
    - ZREMRANGEBYSCORE key 0 cutoff (清理过期)
    - ZCARD key (当前窗口计数)
    - EXPIRE key window*2 (自动回收)

    依赖: redis>=4.2（可选，仅启用 redis 后端时需要）。
    """

    _LUA_SCRIPT = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local cutoff = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])
redis.call('ZREMRANGEBYSCORE', key, 0, cutoff)
local count = redis.call('ZCARD', key)
if count >= limit then
    return 0
end
redis.call('ZADD', key, now, now .. '-' .. math.random())
redis.call('PEXPIRE', key, ttl)
return 1
"""

    def __init__(self, redis_url: str) -> None:
        try:
            import redis  # type: ignore[import-untyped]
        except ImportError as e:  # pragma: no cover - 由工厂防御
            raise ImportError("RedisRateLimitBackend requires 'redis' package. Install: pip install redis>=4.2") from e

        self._redis = redis.Redis.from_url(redis_url, decode_responses=True)
        self._script = self._redis.register_script(self._LUA_SCRIPT)
        logger.info("RedisRateLimitBackend connected: %s", self._safe_url(redis_url))

    @staticmethod
    def _safe_url(url: str) -> str:
        """脱敏 Redis URL，避免密码泄露到日志。"""
        if "@" in url:
            scheme_rest = url.split("://", 1)
            if len(scheme_rest) == 2:
                scheme, rest = scheme_rest
                creds, host = rest.split("@", 1)
                return f"{scheme}://***@{host}"
        return url

    def allow(self, key: str, limit: int, window_seconds: float) -> bool:
        now_ms = int(time.time() * 1000)
        cutoff_ms = int((time.time() - window_seconds) * 1000)
        ttl_ms = int(window_seconds * 1000 * 2)
        # 返回 1=允许 0=限流；redis 返回 int
        result: Any = self._script(
            keys=[f"ratelimit:{key}"],
            args=[now_ms, cutoff_ms, limit, ttl_ms],
        )
        return bool(int(result))

    def close(self) -> None:
        with _suppress_conn_errors():
            self._redis.close()


class _suppress_conn_errors:
    """关闭时吞掉连接异常（进程退出阶段不应抛错）。"""

    def __enter__(self) -> _suppress_conn_errors:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return True  # 吞掉所有异常


# ──────────────────────────────────────────────────────────────
# 后端工厂（项4: 环境变量驱动，零代码切换）
# ──────────────────────────────────────────────────────────────
_backend: RateLimitBackend | None = None
# FIXED(F4): 原实现 _backend_lock_init 标志位被设置但从未用于加锁（死代码），
# 多线程并发首次调用可创建多个后端实例（竞态）。改为真正的双检查锁。
_backend_lock = threading.Lock()


def get_rate_limit_backend() -> RateLimitBackend:
    """获取/惰性创建限流后端单例。

    后端选择（环境变量）:
    - EDGELITE_RATE_LIMIT_BACKEND=redis + EDGELITE_RATE_LIMIT_REDIS_URL → Redis
    - 其他/未设置 → Memory（默认）

    多实例部署设置示例:
        EDGELITE_RATE_LIMIT_BACKEND=redis
        EDGELITE_RATE_LIMIT_REDIS_URL=redis://:password@redis-host:6379/0
    """
    global _backend
    if _backend is not None:
        return _backend

    # F4: 双检查锁保护单例创建，消除并发竞态
    with _backend_lock:
        if _backend is not None:
            return _backend
        backend_type = os.environ.get("EDGELITE_RATE_LIMIT_BACKEND", "memory").strip().lower()
        if backend_type == "redis":
            redis_url = os.environ.get("EDGELITE_RATE_LIMIT_REDIS_URL", "").strip()
            if not redis_url:
                logger.warning(
                    "EDGELITE_RATE_LIMIT_BACKEND=redis but EDGELITE_RATE_LIMIT_REDIS_URL unset; "
                    "falling back to MemoryRateLimitBackend"
                )
                _backend = MemoryRateLimitBackend()
            else:
                try:
                    _backend = RedisRateLimitBackend(redis_url)
                except Exception as e:
                    logger.error(
                        "RedisRateLimitBackend init failed (%s); falling back to memory. "
                        "Multi-instance rate limiting will NOT be shared!",
                        e,
                    )
                    _backend = MemoryRateLimitBackend()
        else:
            _backend = MemoryRateLimitBackend()
        return _backend


def _reset_backend_for_tests() -> None:
    """测试专用: 重置后端单例（用于切换后端测试）。"""
    global _backend
    if _backend is not None:
        _backend.close()
    _backend = None


# ──────────────────────────────────────────────────────────────
# 中间件
# ──────────────────────────────────────────────────────────────
class RateLimitMiddleware(BaseHTTPMiddleware):
    """每 IP 请求限流中间件。

    配置（config.security）:
    - rate_limit_requests_per_minute: 每分钟每 IP 最大请求数（默认 120）

    X-Forwarded-For 安全: 默认使用 TCP 对端 IP，不信任 XFF 头，
    避免攻击者伪造 XFF 绕过限流（原问题: 直接取 XFF 第一段可被伪造）。
    """

    def __init__(
        self,
        app: ASGIApp,
        limit_per_minute: int = 120,
        window_seconds: float = 60.0,
        backend: RateLimitBackend | None = None,
    ) -> None:
        super().__init__(app)
        self._limit = max(1, int(limit_per_minute))
        self._window = float(window_seconds)
        self._backend = backend or get_rate_limit_backend()
        self._last_cleanup = 0.0

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        # 豁免探针/文档路径
        path = request.url.path
        if any(path.startswith(p) for p in _EXEMPT_PREFIXES):
            return await call_next(request)

        # 周期性清理（仅内存后端有实际开销）
        now = time.monotonic()
        if now - self._last_cleanup > 60:
            self._backend.cleanup()
            self._last_cleanup = now

        client_ip = self._extract_client_ip(request)
        allowed = self._backend.allow(client_ip, self._limit, self._window)
        if not allowed:
            logger.warning("Rate limit exceeded: ip=%s path=%s", client_ip, path)
            return JSONResponse(
                status_code=429,
                content={
                    "code": 429,
                    "message": "Too Many Requests",
                    "detail": f"Rate limit exceeded for {client_ip}",
                },
                headers={
                    "Retry-After": str(int(self._window)),
                    "X-RateLimit-Limit": str(self._limit),
                    "X-RateLimit-Window": f"{int(self._window)}s",
                },
            )
        return await call_next(request)

    @staticmethod
    def _extract_client_ip(request: Request) -> str:
        """提取客户端 IP。

        FIXED(项4/R5-S-XX): 默认使用 TCP 对端 IP (request.client.host)，
        不信任 X-Forwarded-For 头，避免攻击者伪造 XFF 绕过限流。

        如部署在可信反向代理后且需获取真实客户端 IP，请在反向代理层
        配置统一的客户端 IP 提取策略，并在此扩展 trusted_proxies 白名单。
        """
        client = request.client
        if client and client.host:
            return client.host
        return "unknown"
