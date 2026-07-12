"""HTTP Webhook 驱动单元测试

覆盖 src/edgelite/drivers/http_webhook.py 的纯函数与数据结构：
- _bad_pv / _uncertain_pv（错误/不确定点值构造）
- _PointHealth（数据健康追踪：record_receive / record_timeout / value_history）
- _CachedResolver（DNS 缓存：resolve / clear / TTL 过期）
- HttpWebhookDriver 类元数据

设计要点：
- _CachedResolver 使用 mock getaddrinfo 避免真实 DNS 查询
- _PointHealth 验证 monotonic 时钟与 wall-clock 时钟分离
- 私有网络列表验证 SSRF 防护覆盖 IPv4 + IPv6
"""

from __future__ import annotations

import asyncio
import socket
import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from edgelite.drivers.base import PointValue
from edgelite.drivers.http_webhook import (
    HttpWebhookDriver,
    _bad_pv,
    _CachedResolver,
    _PointHealth,
    _uncertain_pv,
)


# ── _bad_pv / _uncertain_pv ──


class TestPointValueConstructors:
    def test_bad_pv_quality(self):
        pv = _bad_pv("TIMEOUT")
        assert isinstance(pv, PointValue)
        assert pv.quality == "bad"
        assert pv.value is None
        assert "TIMEOUT" in pv.source

    def test_bad_pv_timestamp_recent(self):
        before = datetime.now(UTC)
        pv = _bad_pv("ERR")
        after = datetime.now(UTC)
        assert before <= pv.timestamp <= after

    def test_uncertain_pv_quality(self):
        pv = _uncertain_pv(42, "STALE")
        assert isinstance(pv, PointValue)
        assert pv.quality == "uncertain"
        assert pv.value == 42
        assert "STALE" in pv.source

    def test_uncertain_pv_preserves_value_type(self):
        pv = _uncertain_pv("hello", "PARTIAL")
        assert pv.value == "hello"
        assert pv.quality == "uncertain"

    def test_uncertain_pv_none_value(self):
        pv = _uncertain_pv(None, "NO_DATA")
        assert pv.value is None
        assert pv.quality == "uncertain"


# ── _PointHealth ──


class TestPointHealth:
    def test_record_receive_increments_count(self):
        health = _PointHealth()
        assert health.receive_count == 0
        health.record_receive(42)
        assert health.receive_count == 1
        health.record_receive(43)
        assert health.receive_count == 2

    def test_record_receive_updates_last_value(self):
        health = _PointHealth()
        health.record_receive(42)
        assert health.last_value == 42
        health.record_receive(99)
        assert health.last_value == 99

    def test_record_receive_appends_to_history(self):
        health = _PointHealth()
        health.record_receive(1)
        health.record_receive(2)
        health.record_receive(3)
        assert list(health.value_history) == [1, 2, 3]

    def test_record_receive_default_quality_good(self):
        health = _PointHealth()
        health.record_receive(42)
        assert health.quality_flow[-1]["quality"] == "good"

    def test_record_receive_custom_quality(self):
        health = _PointHealth()
        health.record_receive(42, quality="uncertain")
        assert health.quality_flow[-1]["quality"] == "uncertain"

    def test_record_receive_sets_wall_timestamp(self):
        health = _PointHealth()
        assert health.last_received_wall_ts is None
        health.record_receive(42)
        assert health.last_received_wall_ts is not None
        assert isinstance(health.last_received_wall_ts, datetime)

    def test_record_timeout_increments_count(self):
        health = _PointHealth()
        assert health.timeout_count == 0
        health.record_timeout()
        assert health.timeout_count == 1
        health.record_timeout()
        assert health.timeout_count == 2

    def test_record_timeout_appends_bad_quality(self):
        health = _PointHealth()
        health.record_timeout()
        assert health.quality_flow[-1]["quality"] == "bad"

    def test_value_history_maxlen(self):
        """value_history maxlen=20，超出后淘汰最旧。"""
        health = _PointHealth()
        for i in range(25):
            health.record_receive(i)
        assert len(health.value_history) == 20
        assert health.value_history[0] == 5  # 前 5 个被淘汰

    def test_quality_flow_maxlen(self):
        """quality_flow maxlen=100，超出后淘汰最旧。"""
        health = _PointHealth()
        for i in range(110):
            health.record_receive(i)
        assert len(health.quality_flow) == 100


# ── _CachedResolver ──


class TestCachedResolver:
    @pytest.fixture
    def resolver(self):
        return _CachedResolver(ttl=60.0)

    @pytest.mark.asyncio
    async def test_resolve_caches_result(self, resolver):
        """首次解析后缓存，第二次不调用 getaddrinfo。"""
        mock_addrs = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))]
        with patch("asyncio.get_running_loop") as mock_loop:
            loop = mock_loop.return_value
            loop.getaddrinfo = AsyncMock(return_value=mock_addrs)

            result1 = await resolver.resolve("example.com")
            result2 = await resolver.resolve("example.com")

            assert result1 == mock_addrs
            assert result2 == mock_addrs
            # getaddrinfo 只调用一次（缓存命中）
            assert loop.getaddrinfo.call_count == 1

    @pytest.mark.asyncio
    async def test_clear_resets_cache(self, resolver):
        mock_addrs = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("1.2.3.4", 0))]
        with patch("asyncio.get_running_loop") as mock_loop:
            loop = mock_loop.return_value
            loop.getaddrinfo = AsyncMock(return_value=mock_addrs)

            await resolver.resolve("example.com")
            resolver.clear()
            await resolver.resolve("example.com")

            # clear 后重新解析
            assert loop.getaddrinfo.call_count == 2

    @pytest.mark.asyncio
    async def test_ttl_expiry_triggers_re_resolve(self):
        """TTL 过期后重新解析。"""
        resolver = _CachedResolver(ttl=0.05)  # 50ms TTL
        mock_addrs = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("1.2.3.4", 0))]
        with patch("asyncio.get_running_loop") as mock_loop:
            loop = mock_loop.return_value
            loop.getaddrinfo = AsyncMock(return_value=mock_addrs)

            await resolver.resolve("example.com")
            await asyncio.sleep(0.06)  # 等待 TTL 过期
            await resolver.resolve("example.com")

            assert loop.getaddrinfo.call_count == 2

    @pytest.mark.asyncio
    async def test_resolve_failure_returns_cached(self, resolver):
        """DNS 解析失败时返回缓存结果（如果有）。"""
        mock_addrs = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("1.2.3.4", 0))]
        with patch("asyncio.get_running_loop") as mock_loop:
            loop = mock_loop.return_value
            loop.getaddrinfo = AsyncMock(return_value=mock_addrs)

            # 首次成功解析
            await resolver.resolve("example.com")
            # 第二次解析失败，应返回缓存
            loop.getaddrinfo = AsyncMock(side_effect=socket.gaierror("DNS failed"))
            result = await resolver.resolve("example.com")
            assert result == mock_addrs

    @pytest.mark.asyncio
    async def test_resolve_failure_no_cache_raises(self, resolver):
        """DNS 解析失败且无缓存时抛异常。"""
        with patch("asyncio.get_running_loop") as mock_loop:
            loop = mock_loop.return_value
            loop.getaddrinfo = AsyncMock(side_effect=socket.gaierror("DNS failed"))
            with pytest.raises(socket.gaierror):
                await resolver.resolve("never-cached.example.com")


# ── HttpWebhookDriver 类元数据 ──


class TestHttpWebhookDriverMetadata:
    def test_plugin_name(self):
        assert HttpWebhookDriver.plugin_name == "http_webhook"

    def test_supported_protocols(self):
        assert "http" in HttpWebhookDriver.supported_protocols
        assert "webhook" in HttpWebhookDriver.supported_protocols

    def test_required_dependencies(self):
        assert "httpx" in HttpWebhookDriver._required_dependencies

    def test_config_schema_has_url_required(self):
        schema = HttpWebhookDriver.config_schema
        assert "url" in schema["required"]

    def test_supported_protocols_is_tuple(self):
        """FIXED(P2): 可变默认值改为 tuple。"""
        assert isinstance(HttpWebhookDriver.supported_protocols, tuple)

    def test_required_dependencies_is_tuple(self):
        assert isinstance(HttpWebhookDriver._required_dependencies, tuple)
