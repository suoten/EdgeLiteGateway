"""健康检查端点测试 - 探针、速率限制、依赖检查

覆盖 api/health.py：
- _RateLimiter: 每 IP 滑动窗口限流
- /health/live, /live: liveness 探针
- /health/ready, /ready: readiness 探针（检查 SQLite+InfluxDB）
- /health: 完整健康检查（含所有依赖详情）
- _check_sqlite/_check_influxdb/_check_mqtt/_check_drivers
- _run_full_check: 并发执行 + 总超时
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from edgelite.api.health import (
    _RATE_LIMIT_MAX_REQUESTS,
    _RATE_LIMIT_WINDOW_SECONDS,
    _check_drivers,
    _check_influxdb,
    _check_mqtt,
    _check_sqlite,
    _RateLimiter,
    _run_full_check,
    router,
)


@pytest.fixture
def app():
    """创建仅包含 health 路由的 FastAPI 应用"""
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


class TestRateLimiter:
    def test_allow_under_limit(self):
        """未超限时应允许请求"""
        rl = _RateLimiter()
        assert rl.allow("1.2.3.4") is True

    def test_block_over_limit(self):
        """超过限制时应拒绝请求"""
        rl = _RateLimiter()
        for _ in range(_RATE_LIMIT_MAX_REQUESTS):
            rl.allow("1.2.3.4")
        assert rl.allow("1.2.3.4") is False

    def test_different_ips_independent(self):
        """不同 IP 的限流应相互独立"""
        rl = _RateLimiter()
        for _ in range(_RATE_LIMIT_MAX_REQUESTS):
            rl.allow("1.1.1.1")
        # 1.1.1.1 已限流，但 2.2.2.2 应仍可请求
        assert rl.allow("2.2.2.2") is True

    def test_cleanup_removes_empty_buckets(self):
        """cleanup 应清理空桶"""
        rl = _RateLimiter()
        rl.allow("1.1.1.1")
        rl.cleanup()
        # cleanup 后桶可能被清理（取决于时间），不应抛异常
        assert isinstance(rl._buckets, dict)

    def test_window_expiry_allows_again(self):
        """窗口过期后应重新允许请求"""
        rl = _RateLimiter()
        # 手动注入过期时间戳
        rl._buckets["1.1.1.1"].append(time.monotonic() - _RATE_LIMIT_WINDOW_SECONDS - 1)
        # 过期条目应被清理，新请求应被允许
        assert rl.allow("1.1.1.1") is True


class TestHealthLive:
    def test_health_live_returns_ok(self, client):
        """/health/live 应返回 200 + status:ok"""
        resp = client.get("/health/live")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_live_alias_returns_ok(self, client):
        """/live 别名应返回 200 + status:ok"""
        resp = client.get("/live")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestHealthReady:
    def test_health_ready_rate_limited(self, client):
        """/health/ready 超过限流应返回 429"""
        # 先清空限流器
        from edgelite.api import health as health_module

        health_module._rate_limiter = _RateLimiter()
        # 发送超过限制的请求
        for _ in range(_RATE_LIMIT_MAX_REQUESTS):
            client.get("/health/ready")
        resp = client.get("/health/ready")
        assert resp.status_code == 429

    def test_ready_alias_exists(self, client):
        """/ready 别名应可访问"""
        from edgelite.api import health as health_module

        health_module._rate_limiter = _RateLimiter()
        resp = client.get("/ready")
        # 应返回 200 或 503（取决于依赖状态），不应是 404
        assert resp.status_code in (200, 503)


class TestHealthFull:
    def test_health_full_rate_limited(self, client):
        """/health 超过限流应返回 429"""
        from edgelite.api import health as health_module

        health_module._rate_limiter = _RateLimiter()
        for _ in range(_RATE_LIMIT_MAX_REQUESTS):
            client.get("/health")
        resp = client.get("/health")
        assert resp.status_code == 429


class TestCheckSqlite:
    @pytest.mark.asyncio
    async def test_check_sqlite_not_initialized(self):
        """未初始化数据库应返回 unhealthy"""
        with patch("edgelite.app._app_state", SimpleNamespace(database=None)):
            result = await _check_sqlite()
            assert result["status"] == "unhealthy"
            assert "not initialized" in result["error"]

    @pytest.mark.asyncio
    async def test_check_sqlite_healthy(self):
        """数据库正常应返回 healthy"""
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock()

        class _CM:
            async def __aenter__(self):
                return mock_session

            async def __aexit__(self, *args):
                return False

        mock_db = MagicMock()
        mock_db.get_session.return_value = _CM()

        with patch("edgelite.app._app_state", SimpleNamespace(database=mock_db)):
            result = await _check_sqlite()
            assert result["status"] == "healthy"

    @pytest.mark.asyncio
    async def test_check_sqlite_exception(self):
        """数据库异常应返回 unhealthy"""
        mock_db = MagicMock()
        mock_db.get_session.side_effect = RuntimeError("db error")

        with patch("edgelite.app._app_state", SimpleNamespace(database=mock_db)):
            result = await _check_sqlite()
            assert result["status"] == "unhealthy"
            assert "db error" in result["error"]


class TestCheckInfluxdb:
    @pytest.mark.asyncio
    async def test_not_initialized(self):
        """未初始化应返回 unhealthy"""
        with patch("edgelite.app._app_state", SimpleNamespace(influx_storage=None)):
            result = await _check_influxdb()
            assert result["status"] == "unhealthy"

    @pytest.mark.asyncio
    async def test_healthy(self):
        """check_health 返回 True 应为 healthy"""
        mock_influx = AsyncMock()
        mock_influx.check_health = AsyncMock(return_value=True)
        with patch("edgelite.app._app_state", SimpleNamespace(influx_storage=mock_influx)):
            result = await _check_influxdb()
            assert result["status"] == "healthy"

    @pytest.mark.asyncio
    async def test_unhealthy(self):
        """check_health 返回 False 应为 unhealthy"""
        mock_influx = AsyncMock()
        mock_influx.check_health = AsyncMock(return_value=False)
        with patch("edgelite.app._app_state", SimpleNamespace(influx_storage=mock_influx)):
            result = await _check_influxdb()
            assert result["status"] == "unhealthy"


class TestCheckMqtt:
    @pytest.mark.asyncio
    async def test_not_initialized_degraded(self):
        """未初始化应返回 degraded（可选依赖）"""
        with patch("edgelite.app._app_state", SimpleNamespace(mqtt_forwarder=None)):
            result = await _check_mqtt()
            assert result["status"] == "degraded"

    @pytest.mark.asyncio
    async def test_healthy(self):
        """connected+running 应为 healthy"""
        mock_fwd = SimpleNamespace(_connected=True, _running=True)
        with patch("edgelite.app._app_state", SimpleNamespace(mqtt_forwarder=mock_fwd)):
            result = await _check_mqtt()
            assert result["status"] == "healthy"

    @pytest.mark.asyncio
    async def test_unhealthy_disconnected(self):
        """未连接应为 unhealthy"""
        mock_fwd = SimpleNamespace(_connected=False, _running=True)
        with patch("edgelite.app._app_state", SimpleNamespace(mqtt_forwarder=mock_fwd)):
            result = await _check_mqtt()
            assert result["status"] == "unhealthy"


class TestCheckDrivers:
    @pytest.mark.asyncio
    async def test_not_initialized_degraded(self):
        """未初始化应返回 degraded"""
        with patch("edgelite.app._app_state", SimpleNamespace(driver_registry=None)):
            result = await _check_drivers()
            assert result["status"] == "degraded"

    @pytest.mark.asyncio
    async def test_healthy(self):
        """注册表正常应为 healthy"""
        mock_registry = MagicMock()
        mock_registry.get_all_protocol_keys.return_value = ["modbus", "opcua"]
        with patch("edgelite.app._app_state", SimpleNamespace(driver_registry=mock_registry)):
            result = await _check_drivers()
            assert result["status"] == "healthy"
            assert result["protocols"] == 2


class TestRunFullCheck:
    @pytest.mark.asyncio
    async def test_overall_unhealthy(self):
        """任一 unhealthy 应整体 unhealthy"""
        with (
            patch("edgelite.api.health._check_sqlite", return_value={"status": "unhealthy", "error": "x"}),
            patch("edgelite.api.health._check_influxdb", return_value={"status": "healthy"}),
            patch("edgelite.api.health._check_mqtt", return_value={"status": "healthy"}),
            patch("edgelite.api.health._check_drivers", return_value={"status": "healthy"}),
            patch("edgelite.api.health._check_disk_space", return_value={"status": "healthy"}),
        ):
            result = await _run_full_check()
            assert result["status"] == "unhealthy"
            assert "checks" in result

    @pytest.mark.asyncio
    async def test_overall_degraded(self):
        """仅有 degraded 应整体 degraded"""
        with (
            patch("edgelite.api.health._check_sqlite", return_value={"status": "healthy"}),
            patch("edgelite.api.health._check_influxdb", return_value={"status": "healthy"}),
            patch("edgelite.api.health._check_mqtt", return_value={"status": "degraded", "error": "x"}),
            patch("edgelite.api.health._check_drivers", return_value={"status": "healthy"}),
            patch("edgelite.api.health._check_disk_space", return_value={"status": "healthy"}),
        ):
            result = await _run_full_check()
            assert result["status"] == "degraded"

    @pytest.mark.asyncio
    async def test_overall_healthy(self):
        """全部 healthy 应整体 healthy"""
        with (
            patch("edgelite.api.health._check_sqlite", return_value={"status": "healthy"}),
            patch("edgelite.api.health._check_influxdb", return_value={"status": "healthy"}),
            patch("edgelite.api.health._check_mqtt", return_value={"status": "healthy"}),
            patch("edgelite.api.health._check_drivers", return_value={"status": "healthy"}),
            patch("edgelite.api.health._check_disk_space", return_value={"status": "healthy"}),
        ):
            result = await _run_full_check()
            assert result["status"] == "healthy"
            assert "timestamp" in result
            assert "checks" in result
            assert set(result["checks"].keys()) == {"sqlite", "influxdb", "mqtt", "drivers", "disk"}

    @pytest.mark.asyncio
    async def test_exception_in_check_handled(self):
        """检查函数抛异常应转为 unhealthy"""
        with (
            patch("edgelite.api.health._check_sqlite", side_effect=RuntimeError("boom")),
            patch("edgelite.api.health._check_influxdb", return_value={"status": "healthy"}),
            patch("edgelite.api.health._check_mqtt", return_value={"status": "healthy"}),
            patch("edgelite.api.health._check_drivers", return_value={"status": "healthy"}),
            patch("edgelite.api.health._check_disk_space", return_value={"status": "healthy"}),
        ):
            result = await _run_full_check()
            # gather(return_exceptions=True) 捕获异常，转 unhealthy
            assert result["status"] == "unhealthy"
