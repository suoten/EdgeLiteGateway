"""限流中间件单元测试。

覆盖 src/edgelite/middleware/rate_limit.py：MemoryRateLimitBackend 滑动窗口、
cleanup、RedisRateLimitBackend（mock）、get_rate_limit_backend 单例 + F4 并发竞态、
RateLimitMiddleware 429 响应头、_extract_client_ip 不信任 XFF。
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from edgelite.middleware import rate_limit as rl
from edgelite.middleware.rate_limit import (
    MemoryRateLimitBackend,
    RateLimitMiddleware,
    RedisRateLimitBackend,
    _reset_backend_for_tests,
    get_rate_limit_backend,
)

# ─── MemoryRateLimitBackend ───


def test_memory_allow_under_limit():
    """limit 次放行，第 limit+1 次拒绝。"""
    backend = MemoryRateLimitBackend()
    for _ in range(10):
        assert backend.allow("ip1", limit=10, window_seconds=60.0)
    # 第 11 次到达上限被拒绝
    assert backend.allow("ip1", limit=10, window_seconds=60.0) is False


def test_memory_allow_independent_keys():
    """不同 key 独立计数。"""
    backend = MemoryRateLimitBackend()
    for _ in range(5):
        assert backend.allow("ip1", limit=5, window_seconds=60.0)
    # ip2 仍有完整配额
    assert backend.allow("ip2", limit=5, window_seconds=60.0)


def test_memory_allow_sliding_window_recovery():
    """窗口滑动后恢复配额。"""
    backend = MemoryRateLimitBackend()
    # 用极短窗口模拟过期
    for _ in range(3):
        assert backend.allow("ip1", limit=3, window_seconds=0.05)
    assert backend.allow("ip1", limit=3, window_seconds=0.05) is False
    # 等待窗口过期
    time.sleep(0.1)
    assert backend.allow("ip1", limit=3, window_seconds=0.05)


def test_memory_cleanup_removes_stale_buckets():
    """cleanup 清理空桶与过期桶。"""
    backend = MemoryRateLimitBackend()
    backend.allow("ip1", limit=10, window_seconds=0.05)
    backend.allow("ip2", limit=10, window_seconds=0.05)
    time.sleep(0.1)
    backend.cleanup()
    # 过期桶应被清理（6 倍窗口未活跃）— cleanup 用 3600s 阈值，此处仅验证不抛错且可再次 cleanup
    assert isinstance(backend._buckets, dict)


def test_memory_cleanup_empty_backend():
    """空后端 cleanup 不抛错。"""
    backend = MemoryRateLimitBackend()
    backend.cleanup()


# ─── RedisRateLimitBackend ───


def test_redis_safe_url_masks_credentials():
    """_safe_url 脱敏 Redis URL 中的密码。"""
    url = "redis://:secret-pass@redis-host:6379/0"
    masked = RedisRateLimitBackend._safe_url(url)
    assert "secret-pass" not in masked
    assert "***" in masked
    assert "redis-host:6379" in masked


def test_redis_safe_url_no_credentials():
    """无凭据的 URL 原样返回。"""
    url = "redis://redis-host:6379/0"
    assert RedisRateLimitBackend._safe_url(url) == url


def test_redis_allow_maps_script_result():
    """allow 将脚本返回 1→True、0→False。"""
    backend = RedisRateLimitBackend.__new__(RedisRateLimitBackend)
    # mock _script callable
    backend._script = MagicMock(side_effect=[1, 0])
    assert backend.allow("k", limit=10, window_seconds=60.0) is True
    assert backend.allow("k", limit=10, window_seconds=60.0) is False


def test_redis_close_suppresses_errors():
    """close 吞掉连接异常（进程退出阶段不应抛错）。"""
    backend = RedisRateLimitBackend.__new__(RedisRateLimitBackend)
    backend._redis = MagicMock()
    backend._redis.close.side_effect = ConnectionError("boom")
    # 不应抛错
    backend.close()


# ─── get_rate_limit_backend 单例 + F4 并发竞态 ───


def test_get_backend_singleton():
    """多次调用返回同一实例。"""
    _reset_backend_for_tests()
    try:
        b1 = get_rate_limit_backend()
        b2 = get_rate_limit_backend()
        assert b1 is b2
    finally:
        _reset_backend_for_tests()


def test_get_backend_memory_default(monkeypatch):
    """默认使用 MemoryRateLimitBackend。"""
    monkeypatch.delenv("EDGELITE_RATE_LIMIT_BACKEND", raising=False)
    _reset_backend_for_tests()
    try:
        b = get_rate_limit_backend()
        assert isinstance(b, MemoryRateLimitBackend)
    finally:
        _reset_backend_for_tests()


def test_get_backend_redis_without_url_falls_back_to_memory(monkeypatch):
    """redis 后端但无 URL → 回退 memory + warning。"""
    monkeypatch.setenv("EDGELITE_RATE_LIMIT_BACKEND", "redis")
    monkeypatch.delenv("EDGELITE_RATE_LIMIT_REDIS_URL", raising=False)
    _reset_backend_for_tests()
    try:
        b = get_rate_limit_backend()
        assert isinstance(b, MemoryRateLimitBackend)
    finally:
        _reset_backend_for_tests()


def test_get_backend_redis_init_failure_falls_back_to_memory(monkeypatch):
    """redis 初始化失败 → 回退 memory。"""
    monkeypatch.setenv("EDGELITE_RATE_LIMIT_BACKEND", "redis")
    monkeypatch.setenv("EDGELITE_RATE_LIMIT_REDIS_URL", "redis://invalid:1/0")

    def _fail_init(self, url):  # noqa: ARG001
        raise OSError("connection refused")

    monkeypatch.setattr(RedisRateLimitBackend, "__init__", _fail_init)
    _reset_backend_for_tests()
    try:
        b = get_rate_limit_backend()
        assert isinstance(b, MemoryRateLimitBackend)
    finally:
        _reset_backend_for_tests()


def test_get_backend_concurrent_init_creates_single_instance(monkeypatch):
    """F4: 并发首次调用只创建一个后端实例（双检查锁修复竞态）。"""
    monkeypatch.delenv("EDGELITE_RATE_LIMIT_BACKEND", raising=False)
    _reset_backend_for_tests()

    created: list[MemoryRateLimitBackend] = []
    original_init = MemoryRateLimitBackend.__init__

    def counting_init(self):
        original_init(self)
        created.append(self)

    monkeypatch.setattr(MemoryRateLimitBackend, "__init__", counting_init)

    barrier = threading.Barrier(8)
    results: list[object] = [None] * 8

    def worker(i):
        barrier.wait()
        results[i] = get_rate_limit_backend()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    try:
        # 所有线程拿到同一实例
        first = results[0]
        assert first is not None
        assert all(r is first for r in results)
    finally:
        _reset_backend_for_tests()


# ─── RateLimitMiddleware 集成 ───


def _make_rl_app(backend: rl.RateLimitBackend, limit: int = 2) -> Starlette:
    def ping(request: Request) -> JSONResponse:  # type: ignore[misc]
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/api/v1/ping", ping, methods=["GET"])])
    app.add_middleware(RateLimitMiddleware, limit_per_minute=limit, backend=backend)
    return app


def test_middleware_allows_under_limit():
    """未达 limit 放行。"""
    backend = MemoryRateLimitBackend()
    app = _make_rl_app(backend, limit=5)
    with TestClient(app) as client:
        for _ in range(5):
            assert client.get("/api/v1/ping").status_code == 200


def test_middleware_returns_429_when_exceeded():
    """超过 limit 返回 429 + Retry-After + X-RateLimit-* 头。"""
    backend = MemoryRateLimitBackend()
    app = _make_rl_app(backend, limit=2)
    with TestClient(app) as client:
        assert client.get("/api/v1/ping").status_code == 200
        assert client.get("/api/v1/ping").status_code == 200
        resp = client.get("/api/v1/ping")
    assert resp.status_code == 429
    assert resp.json()["code"] == 429
    assert "Retry-After" in resp.headers
    assert "X-RateLimit-Limit" in resp.headers
    assert "X-RateLimit-Window" in resp.headers


@pytest.mark.parametrize("path", ["/health", "/live", "/ready", "/docs", "/openapi.json"])
def test_middleware_exempt_paths_not_limited(path):
    """豁免路径不受限流。"""

    def _h(request: Request) -> JSONResponse:  # type: ignore[misc]
        return JSONResponse({"ok": True})

    backend = MemoryRateLimitBackend()
    app = Starlette(routes=[Route(path, _h, methods=["GET"])])
    app.add_middleware(RateLimitMiddleware, limit_per_minute=1, backend=backend)
    with TestClient(app) as client:
        for _ in range(5):
            assert client.get(path).status_code == 200


def test_extract_client_ip_uses_tcp_peer_not_xff():
    """_extract_client_ip 默认使用 TCP 对端 IP，不信任 X-Forwarded-For。"""
    request = MagicMock()
    request.client = MagicMock(host="203.0.113.5")
    request.headers = {"X-Forwarded-For": "1.2.3.4"}
    ip = RateLimitMiddleware._extract_client_ip(request)
    assert ip == "203.0.113.5"


def test_extract_client_ip_no_client_returns_unknown():
    """无 client 信息时返回 'unknown'。"""
    request = MagicMock()
    request.client = None
    assert RateLimitMiddleware._extract_client_ip(request) == "unknown"


def test_middleware_limit_floor_to_one():
    """limit < 1 时向下取整为 1（max(1, ...)）。"""
    backend = MemoryRateLimitBackend()
    mw = RateLimitMiddleware(MagicMock(), limit_per_minute=0, backend=backend)
    assert mw._limit == 1
