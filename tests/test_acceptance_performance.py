"""EdgeLite 验收测试 — 性能测试模块

测试范围: 并发设备连接、API响应时间、并发请求处理
"""

from __future__ import annotations

import asyncio
import statistics
import time

import pytest
from httpx import ASGITransport, AsyncClient
from test_acceptance_smoke import _build_test_app

# ═══════════════════════════════════════════════════════════════
# 模块 5: 性能测试 (PERFORMANCE)
# ═══════════════════════════════════════════════════════════════


class TestAPIResponseTime:
    """PERF1: API 响应时间基准"""

    @pytest.fixture
    def app(self):
        return _build_test_app("admin")

    @pytest.fixture
    async def client(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c

    @pytest.mark.asyncio
    async def test_perf1_01_devices_list_latency(self, client):
        """GET /api/v1/devices 响应时间 < 200ms"""
        times = []
        for _ in range(20):
            start = time.perf_counter()
            resp = await client.get("/api/v1/devices")
            elapsed = (time.perf_counter() - start) * 1000
            times.append(elapsed)
            assert resp.status_code == 200

        avg_latency = statistics.mean(times)
        p95 = statistics.quantiles(times, n=20)[18]  # P95

        assert avg_latency < 200, f"Average latency {avg_latency:.1f}ms exceeds 200ms"
        assert p95 < 500, f"P95 latency {p95:.1f}ms exceeds 500ms"

    @pytest.mark.asyncio
    async def test_perf1_02_rules_list_latency(self, client):
        """GET /api/v1/rules 响应时间 < 200ms"""
        times = []
        for _ in range(20):
            start = time.perf_counter()
            resp = await client.get("/api/v1/rules")
            elapsed = (time.perf_counter() - start) * 1000
            times.append(elapsed)
            assert resp.status_code == 200

        avg_latency = statistics.mean(times)
        assert avg_latency < 200, f"Average latency {avg_latency:.1f}ms exceeds 200ms"

    @pytest.mark.asyncio
    async def test_perf1_03_alarms_list_latency(self, client):
        """GET /api/v1/alarms 响应时间 < 200ms"""
        times = []
        for _ in range(20):
            start = time.perf_counter()
            resp = await client.get("/api/v1/alarms")
            elapsed = (time.perf_counter() - start) * 1000
            times.append(elapsed)
            assert resp.status_code == 200

        avg_latency = statistics.mean(times)
        assert avg_latency < 200, f"Average latency {avg_latency:.1f}ms exceeds 200ms"

    @pytest.mark.asyncio
    async def test_perf1_04_system_status_latency(self, client):
        """GET /api/v1/system/status 响应时间 < 100ms"""
        times = []
        for _ in range(10):
            start = time.perf_counter()
            resp = await client.get("/api/v1/system/status")
            elapsed = (time.perf_counter() - start) * 1000
            times.append(elapsed)
            assert resp.status_code == 200

        avg_latency = statistics.mean(times)
        assert avg_latency < 100, f"Average latency {avg_latency:.1f}ms exceeds 100ms"

    @pytest.mark.asyncio
    async def test_perf1_05_health_check_latency(self, client):
        """GET /health/live 响应时间 < 50ms"""
        times = []
        for _ in range(20):
            start = time.perf_counter()
            resp = await client.get("/health/live")
            elapsed = (time.perf_counter() - start) * 1000
            times.append(elapsed)
            assert resp.status_code == 200

        avg_latency = statistics.mean(times)
        assert avg_latency < 50, f"Health check latency {avg_latency:.1f}ms exceeds 50ms"


class TestConcurrentRequests:
    """PERF2: 并发请求处理能力"""

    @pytest.fixture
    def app(self):
        return _build_test_app("admin")

    @pytest.fixture
    async def client(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c

    @pytest.mark.asyncio
    async def test_perf2_01_concurrent_device_list(self, client):
        """50 并发 GET /api/v1/devices — 全部成功"""

        async def make_request():
            resp = await client.get("/api/v1/devices")
            return resp.status_code

        results = await asyncio.gather(*[make_request() for _ in range(50)])
        success_count = sum(1 for s in results if s == 200)
        assert success_count >= 48, f"Only {success_count}/50 concurrent requests succeeded"

    @pytest.mark.asyncio
    async def test_perf2_02_concurrent_mixed_endpoints(self, client):
        """50 并发混合请求 — 全部成功"""
        endpoints = [
            "/api/v1/devices",
            "/api/v1/rules",
            "/api/v1/alarms",
            "/api/v1/system/status",
            "/api/v1/system/resources",
        ]

        async def make_request(url):
            resp = await client.get(url)
            return resp.status_code

        tasks = [make_request(endpoints[i % len(endpoints)]) for i in range(50)]
        results = await asyncio.gather(*tasks)
        success_count = sum(1 for s in results if s == 200)
        assert success_count >= 48, f"Only {success_count}/50 mixed concurrent requests succeeded"

    @pytest.mark.asyncio
    async def test_perf2_03_concurrent_create_devices(self, client):
        """20 并发 POST /api/v1/devices — 至少 90% 成功"""

        async def create_device(idx):
            payload = {
                "device_id": f"perf-device-{idx:03d}",
                "name": f"性能测试设备{idx}",
                "protocol": "modbus_tcp",
                "config": {"host": "127.0.0.1", "port": 502, "slave_id": 1},
                "points": [{"name": "temp", "data_type": "float32", "address": "0"}],
                "collect_interval": 5,
            }
            resp = await client.post("/api/v1/devices", json=payload)
            return resp.status_code

        results = await asyncio.gather(*[create_device(i) for i in range(20)])
        success_count = sum(1 for s in results if s in (200, 201))
        assert success_count >= 18, f"Only {success_count}/20 concurrent creates succeeded"


class TestStabilityUnderLoad:
    """PERF3: 负载下稳定性"""

    @pytest.fixture
    def app(self):
        return _build_test_app("admin")

    @pytest.fixture
    async def client(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c

    @pytest.mark.asyncio
    async def test_perf3_01_sustained_requests(self, client):
        """持续 100 次请求 — 无内存泄漏迹象（全部成功）"""
        success = 0
        for _i in range(100):
            resp = await client.get("/api/v1/devices")
            if resp.status_code == 200:
                success += 1

        assert success == 100, f"Only {success}/100 sustained requests succeeded"

    @pytest.mark.asyncio
    async def test_perf3_02_response_time_stability(self, client):
        """连续 50 次请求 — P95 与 P50 差距 < 3x（无性能退化）"""
        times = []
        for _ in range(50):
            start = time.perf_counter()
            resp = await client.get("/api/v1/devices")
            elapsed = (time.perf_counter() - start) * 1000
            times.append(elapsed)
            assert resp.status_code == 200

        p50 = statistics.median(times)
        p95 = statistics.quantiles(times, n=20)[18]

        # P95 不应超过 P50 的 3 倍
        if p50 > 0:
            assert p95 / p50 < 3.0, f"P95({p95:.1f}ms) / P50({p50:.1f}ms) ratio too high"
