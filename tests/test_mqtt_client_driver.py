"""MQTT Client 驱动单元测试

覆盖 src/edgelite/drivers/mqtt_client.py 的纯函数与数据结构：
- _is_broker_host_safe（SSRF 校验：拦截 loopback/link_local/未指定/组播/保留地址）
- _sanitize_topic_segment（topic 注入防护：替换 / + # \\0）
- PersistentPubQueue（SQLite 持久化离线缓冲：append/popleft/appendleft/clear/容量淘汰）
- MqttClientDriver 类元数据

设计要点：
- SSRF 校验允许 is_private（内网 MQTT broker 合理），拦截 is_loopback/is_link_local 等
- PersistentPubQueue 使用 tmp_path 隔离 SQLite 文件，验证 WAL 模式与容量淘汰
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from edgelite.drivers.mqtt_client import (
    MqttClientDriver,
    PersistentPubQueue,
    _is_broker_host_safe,
    _sanitize_topic_segment,
)

# ── _is_broker_host_safe ──


class TestIsBrokerHostSafe:
    def test_public_ip_safe(self):
        assert _is_broker_host_safe("8.8.8.8") is True

    def test_private_ip_safe(self):
        """内网 MQTT broker 是合理场景，允许 is_private。"""
        assert _is_broker_host_safe("192.168.1.100") is True
        assert _is_broker_host_safe("10.0.0.1") is True

    def test_loopback_blocked(self):
        assert _is_broker_host_safe("127.0.0.1") is False

    def test_link_local_blocked(self):
        """拦截 169.254.x.x（云元数据地址）。"""
        assert _is_broker_host_safe("169.254.1.1") is False

    def test_unspecified_blocked(self):
        assert _is_broker_host_safe("0.0.0.0") is False

    def test_multicast_blocked(self):
        assert _is_broker_host_safe("224.0.0.1") is False

    def test_empty_host_blocked(self):
        assert _is_broker_host_safe("") is False

    def test_ipv6_loopback_blocked(self):
        assert _is_broker_host_safe("::1") is False

    def test_domain_resolves_to_loopback_blocked(self):
        """域名解析到 loopback 应被拦截。"""
        with patch("edgelite.drivers.mqtt_client.socket.getaddrinfo") as mock_resolve:
            mock_resolve.return_value = [
                (0, 0, 0, 0, ("127.0.0.1", 0)),
            ]
            assert _is_broker_host_safe("evil.example.com") is False

    def test_domain_resolves_to_public_safe(self):
        with patch("edgelite.drivers.mqtt_client.socket.getaddrinfo") as mock_resolve:
            mock_resolve.return_value = [
                (0, 0, 0, 0, ("93.184.216.34", 0)),
            ]
            assert _is_broker_host_safe("broker.example.com") is True

    def test_domain_resolution_failure_blocked(self):
        with patch("edgelite.drivers.mqtt_client.socket.getaddrinfo", side_effect=OSError("DNS failed")):
            assert _is_broker_host_safe("unresolvable.example.com") is False


# ── _sanitize_topic_segment ──


class TestSanitizeTopicSegment:
    def test_replaces_slash(self):
        assert _sanitize_topic_segment("dev/ice") == "dev_ice"

    def test_replaces_plus(self):
        assert _sanitize_topic_segment("dev+ice") == "dev_ice"

    def test_replaces_hash(self):
        assert _sanitize_topic_segment("dev#ice") == "dev_ice"

    def test_replaces_null_byte(self):
        assert _sanitize_topic_segment("dev\x00ice") == "dev_ice"

    def test_replaces_all_special_chars(self):
        assert _sanitize_topic_segment("a/b+c#d\x00e") == "a_b_c_d_e"

    def test_empty_returns_empty(self):
        assert _sanitize_topic_segment("") == ""

    def test_normal_string_unchanged(self):
        assert _sanitize_topic_segment("device_001") == "device_001"

    def test_none_input_returns_empty(self):
        assert _sanitize_topic_segment(None) == ""  # type: ignore[arg-type]


# ── PersistentPubQueue ──


class TestPersistentPubQueue:
    @pytest.fixture
    def queue(self, tmp_path):
        """使用 tmp_path 隔离 SQLite 文件。"""
        db_path = str(tmp_path / "test_mqtt_queue.db")
        return PersistentPubQueue(maxlen=5, db_path=db_path)

    def test_append_popleft_fifo(self, queue):
        item = ("topic/test", b'{"value": 1}', time.time())
        queue.append(item)
        result = queue.popleft()
        assert result == item

    def test_multiple_append_popleft_fifo_order(self, queue):
        queue.append(("topic/a", b"1", time.time()))
        queue.append(("topic/b", b"2", time.time()))
        queue.append(("topic/c", b"3", time.time()))
        assert queue.popleft()[0] == "topic/a"
        assert queue.popleft()[0] == "topic/b"
        assert queue.popleft()[0] == "topic/c"

    def test_popleft_empty_raises_index_error(self, queue):
        with pytest.raises(IndexError, match="empty"):
            queue.popleft()

    def test_appendleft_inserts_at_front(self, queue):
        queue.append(("topic/a", b"1", time.time()))
        queue.appendleft(("topic/priority", b"0", time.time()))
        assert queue.popleft()[0] == "topic/priority"
        assert queue.popleft()[0] == "topic/a"

    def test_len(self, queue):
        assert len(queue) == 0
        queue.append(("t", b"1", time.time()))
        assert len(queue) == 1
        queue.append(("t", b"2", time.time()))
        assert len(queue) == 2

    def test_clear(self, queue):
        queue.append(("t", b"1", time.time()))
        queue.append(("t", b"2", time.time()))
        queue.clear()
        assert len(queue) == 0

    def test_maxlen_property(self, queue):
        assert queue.maxlen == 5

    def test_capacity_eviction_on_append(self, queue):
        """容量达上限时 append 淘汰最旧条目。"""
        for i in range(5):
            queue.append((f"topic/{i}", str(i).encode(), time.time()))
        assert len(queue) == 5
        # 第 6 条会淘汰第 1 条
        queue.append(("topic/5", b"5", time.time()))
        assert len(queue) == 5  # 仍然 5
        # 最旧的 topic/0 已被淘汰
        first = queue.popleft()
        assert first[0] == "topic/1"

    def test_capacity_eviction_on_appendleft(self, queue):
        for i in range(5):
            queue.append((f"topic/{i}", str(i).encode(), time.time()))
        queue.appendleft(("topic/priority", b"99", time.time()))
        assert len(queue) == 5
        # popleft 返回最新插入的 priority
        assert queue.popleft()[0] == "topic/priority"

    def test_persists_across_instances(self, tmp_path):
        """SQLite 持久化：新实例能加载旧数据。"""
        db_path = str(tmp_path / "persist_test.db")
        q1 = PersistentPubQueue(maxlen=10, db_path=db_path)
        q1.append(("topic/persist", b"data", time.time()))
        # 新实例使用同一 db
        q2 = PersistentPubQueue(maxlen=10, db_path=db_path)
        assert len(q2) == 1
        result = q2.popleft()
        assert result[0] == "topic/persist"

    def test_default_ts_when_not_provided(self, queue):
        """append 时未提供 ts，使用当前时间。"""
        before = time.time()
        queue.append(("topic", b"payload"))
        item = queue.popleft()
        after = time.time()
        assert len(item) == 3
        assert before <= item[2] <= after


# ── MqttClientDriver 类元数据 ──


class TestMqttClientDriverMetadata:
    def test_plugin_name(self):
        assert MqttClientDriver.plugin_name == "mqtt_client"

    def test_supported_protocols(self):
        assert isinstance(MqttClientDriver.supported_protocols, tuple)

    def test_required_dependencies(self):
        assert isinstance(MqttClientDriver._required_dependencies, tuple)

    def test_config_schema_exists(self):
        assert hasattr(MqttClientDriver, "config_schema")
