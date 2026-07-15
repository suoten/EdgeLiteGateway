"""聚合健康检查端点 — 供 Docker / K8s 探针使用。

FIXED: 重建丢失的 health.py 模块 (untracked 文件 stash 恢复失败导致丢失) [2026-06-30]

设计原则（R11-API-01/R11-API-03）:
- 无需认证：K8s/Docker 探针无法携带 JWT，强制认证会导致探针失败触发重启循环
- 轻量级 liveness：/health/live 仅返回 {"status":"ok"}，不检查依赖，避免抖动重启
- 5s 总超时：/health 完整检查 DB+InfluxDB+MQTT+Drivers，使用 asyncio.wait_for 限制总时长
- 速率限制：防止探针被滥用进行 DDoS（原 system.py 版本缺少速率限制）
- HTTP 语义：200=健康，503=不健康（探针根据状态码判断）

端点清单:
- GET /health/live  — liveness 探针（Docker healthcheck 使用）
- GET /health/ready — readiness 探针（K8s readinessProbe 使用）
- GET /health       — 完整健康检查（含依赖详情，5s 超时）
- GET /live         — /health/live 别名（K8s 约定）
- GET /ready        — /health/ready 别名（K8s 约定）
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import defaultdict, deque
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Health"])

# ── 常量 ──
_HEALTH_CHECK_TIMEOUT_SECONDS = 5  # 完整健康检查总超时（秒）
_RATE_LIMIT_WINDOW_SECONDS = 10  # 速率限制时间窗口
_RATE_LIMIT_MAX_REQUESTS = 60  # 每个 IP 在窗口内最大请求数
_DISK_SPACE_CRITICAL_PERCENT = 90  # 磁盘使用率告警阈值（%）
_DISK_SPACE_CRITICAL_FREE_BYTES = 1 * 1024 * 1024 * 1024  # 磁盘可用空间最低阈值（1GB）

# psutil 可选依赖
try:
    import psutil
except ImportError:
    psutil = None


# ── 简单内存速率限制器（token bucket per IP）──
# FIXED: 原 system.py 版本缺少速率限制，探针端点可被滥用进行 DDoS
class _RateLimiter:
    """每 IP 滑动窗口速率限制器（线程不安全，仅在 asyncio 单线程事件循环中使用）。"""

    def __init__(self) -> None:
        self._buckets: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, client_ip: str) -> bool:
        now = time.monotonic()
        bucket = self._buckets[client_ip]
        # 清理过期条目
        while bucket and bucket[0] < now - _RATE_LIMIT_WINDOW_SECONDS:
            bucket.popleft()
        if len(bucket) >= _RATE_LIMIT_MAX_REQUESTS:
            return False
        bucket.append(now)
        return True

    def cleanup(self) -> None:
        """定期清理空桶，防止内存无限增长。"""
        now = time.monotonic()
        empty_keys = [
            ip
            for ip, bucket in self._buckets.items()
            if not bucket or bucket[-1] < now - _RATE_LIMIT_WINDOW_SECONDS * 6
        ]
        for ip in empty_keys:
            self._buckets.pop(ip, None)


_rate_limiter = _RateLimiter()
_last_cleanup = 0.0


def _check_rate_limit(request: Request) -> bool:
    """检查速率限制。返回 True 表示允许，False 表示被限流。"""
    global _last_cleanup
    now = time.monotonic()
    # 每 60 秒清理一次空桶
    if now - _last_cleanup > 60:
        _rate_limiter.cleanup()
        _last_cleanup = now
    client_ip = request.client.host if request.client else "unknown"
    return _rate_limiter.allow(client_ip)


# ── 依赖检查函数 ──
async def _check_sqlite() -> dict[str, Any]:
    """检查 SQLite 主库可用性。"""
    try:
        from edgelite.app import _app_state

        db = getattr(_app_state, "database", None)
        if db is None:
            return {"status": "unhealthy", "error": "database not initialized"}
        async with db.get_session() as session:
            from sqlalchemy import text

            await session.execute(text("SELECT 1"))
        return {"status": "healthy"}
    except Exception as e:
        return {"status": "unhealthy", "error": str(e)}


async def _check_influxdb() -> dict[str, Any]:
    """检查 InfluxDB 可用性。"""
    try:
        from edgelite.app import _app_state

        influx = getattr(_app_state, "influx_storage", None)
        if influx is None:
            return {"status": "unhealthy", "error": "influx_storage not initialized"}
        # check_health 返回 bool
        healthy = await influx.check_health()
        return {"status": "healthy" if healthy else "unhealthy"}
    except Exception as e:
        return {"status": "unhealthy", "error": str(e)}


async def _check_mqtt() -> dict[str, Any]:
    """检查 MQTT forwarder 连接状态。"""
    try:
        from edgelite.app import _app_state

        forwarder = getattr(_app_state, "mqtt_forwarder", None)
        if forwarder is None:
            # MQTT forwarder 是可选依赖，未初始化视为 degraded 而非 unhealthy
            return {"status": "degraded", "error": "mqtt_forwarder not initialized"}
        connected = getattr(forwarder, "_connected", False)
        running = getattr(forwarder, "_running", False)
        if connected and running:
            return {"status": "healthy"}
        return {"status": "unhealthy", "error": f"connected={connected}, running={running}"}
    except Exception as e:
        return {"status": "unhealthy", "error": str(e)}


async def _check_drivers() -> dict[str, Any]:
    """检查驱动注册表状态。"""
    try:
        from edgelite.app import _app_state

        registry = getattr(_app_state, "driver_registry", None)
        if registry is None:
            return {"status": "degraded", "error": "driver_registry not initialized"}
        protocols = registry.get_all_protocol_keys() if hasattr(registry, "get_all_protocol_keys") else []
        return {"status": "healthy", "protocols": len(protocols)}
    except Exception as e:
        return {"status": "unhealthy", "error": str(e)}


def _check_disk_space() -> dict[str, Any]:
    """检查磁盘空间是否充足。

    当磁盘使用率超过 90% 或可用空间低于 1GB 时返回 unhealthy。
    readiness 探针据此返回 503，防止 K8s 向磁盘不足的节点调度流量。
    """
    if psutil is None:
        return {"status": "degraded", "error": "psutil not available"}
    try:
        disk_path = "C:\\" if os.name == "nt" else "/"
        usage = psutil.disk_usage(disk_path)
        result = {
            "status": "healthy",
            "disk_total": usage.total,
            "disk_used": usage.used,
            "disk_free": usage.free,
            "disk_percent": usage.percent,
        }
        if usage.percent >= _DISK_SPACE_CRITICAL_PERCENT:
            result["status"] = "unhealthy"
            result["error"] = f"disk usage {usage.percent:.1f}% exceeds threshold {_DISK_SPACE_CRITICAL_PERCENT}%"
        elif usage.free < _DISK_SPACE_CRITICAL_FREE_BYTES:
            result["status"] = "unhealthy"
            result["error"] = f"disk free space {usage.free / 1024 / 1024:.0f}MB below 1GB threshold"
        return result
    except Exception as e:
        return {"status": "unhealthy", "error": str(e)}


async def _run_full_check() -> dict[str, Any]:
    """运行完整健康检查（并发执行所有依赖检查，受总超时保护）。"""
    try:
        results = await asyncio.wait_for(
            asyncio.gather(
                _check_sqlite(),
                _check_influxdb(),
                _check_mqtt(),
                _check_drivers(),
                return_exceptions=True,
            ),
            timeout=_HEALTH_CHECK_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        return {
            "status": "unhealthy",
            "error": f"health check timed out after {_HEALTH_CHECK_TIMEOUT_SECONDS}s",
        }

    sqlite_r, influx_r, mqtt_r, drivers_r = results
    # asyncio.gather(return_exceptions=True) 可能返回 Exception 实例
    if isinstance(sqlite_r, Exception):
        sqlite_r = {"status": "unhealthy", "error": str(sqlite_r)}
    if isinstance(influx_r, Exception):
        influx_r = {"status": "unhealthy", "error": str(influx_r)}
    if isinstance(mqtt_r, Exception):
        mqtt_r = {"status": "unhealthy", "error": str(mqtt_r)}
    if isinstance(drivers_r, Exception):
        drivers_r = {"status": "unhealthy", "error": str(drivers_r)}

    # 磁盘空间检查（同步、快速）
    disk_r = _check_disk_space()

    # 综合状态判定：任一 unhealthy 即整体 unhealthy；仅有 degraded 则整体 degraded
    statuses = [sqlite_r["status"], influx_r["status"], mqtt_r["status"], drivers_r["status"], disk_r["status"]]
    if "unhealthy" in statuses:
        overall = "unhealthy"
    elif "degraded" in statuses:
        overall = "degraded"
    else:
        overall = "healthy"

    return {
        "status": overall,
        "timestamp": time.time(),
        "checks": {
            "sqlite": sqlite_r,
            "influxdb": influx_r,
            "mqtt": mqtt_r,
            "drivers": drivers_r,
            "disk": disk_r,
        },
    }


# ── 路由端点 ──


@router.get("/health/live", include_in_schema=False)
async def health_live() -> JSONResponse:
    """Liveness 探针 — 仅返回 ok，不检查依赖。

    Docker healthcheck 使用此端点。返回 200 表示进程存活。
    故意不检查依赖，避免依赖抖动（如 InfluxDB 重启）触发容器重启循环。
    """
    return JSONResponse(status_code=200, content={"status": "ok"})


@router.get("/live", include_in_schema=False)
async def health_live_alias() -> JSONResponse:
    """K8s 约定的 /live 别名，转发到 /health/live 逻辑。"""
    return await health_live()


@router.get("/health/ready", include_in_schema=False)
async def health_ready(request: Request) -> JSONResponse:
    """Readiness 探针 — 检查核心依赖（SQLite + InfluxDB），决定是否接收流量。

    K8s readinessProbe 使用此端点。返回 200 表示可以接收流量，503 表示不可接收。
    仅检查核心依赖（SQLite+InfluxDB），不检查 MQTT/Drivers（它们是可选/异步的）。
    """
    if not _check_rate_limit(request):
        return JSONResponse(status_code=429, content={"status": "rate_limited"})

    try:
        sqlite_r, influx_r, disk_r = await asyncio.wait_for(
            asyncio.gather(_check_sqlite(), _check_influxdb(), return_exceptions=True),
            timeout=_HEALTH_CHECK_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "error": "readiness check timed out"},
        )

    if isinstance(sqlite_r, Exception):
        sqlite_r = {"status": "unhealthy", "error": str(sqlite_r)}
    if isinstance(influx_r, Exception):
        influx_r = {"status": "unhealthy", "error": str(influx_r)}

    # 磁盘空间检查（同步、快速，不纳入 async gather）
    disk_r = _check_disk_space()

    ready = sqlite_r["status"] == "healthy" and influx_r["status"] == "healthy" and disk_r["status"] != "unhealthy"
    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "status": "ready" if ready else "not_ready",
            "checks": {"sqlite": sqlite_r, "influxdb": influx_r, "disk": disk_r},
        },
    )


@router.get("/ready", include_in_schema=False)
async def health_ready_alias(request: Request) -> JSONResponse:
    """K8s 约定的 /ready 别名，转发到 /health/ready 逻辑。"""
    return await health_ready(request)


@router.get("/health", include_in_schema=False)
async def health_full(request: Request) -> JSONResponse:
    """完整健康检查 — 检查所有依赖并返回详细状态。

    包含 SQLite+InfluxDB+MQTT+Drivers 详情，受 5s 总超时保护。
    返回 200 表示健康，503 表示不健康或降级。
    """
    if not _check_rate_limit(request):
        return JSONResponse(status_code=429, content={"status": "rate_limited"})

    result = await _run_full_check()
    status_code = 200 if result["status"] == "healthy" else 503
    return JSONResponse(status_code=status_code, content=result)
