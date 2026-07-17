"""OPC UA Server 驱动单元测试

覆盖 src/edgelite/drivers/opcua_server.py：
- OpcUaNode 数据类（to_dict / 字段默认值）
- OpcUaSubscription 数据类
- OpcUaServerDriver 类元数据（plugin_name / config_schema）

设计要点：
- 数据类 to_dict 验证序列化格式（timestamp → isoformat）
- 类元数据验证驱动注册信息
"""

from __future__ import annotations

from datetime import UTC, datetime

from edgelite.drivers.opcua_server import OpcUaNode, OpcUaServerDriver, OpcUaSubscription

# ── OpcUaNode 数据类 ──


class TestOpcUaNode:
    def test_to_dict_contains_all_fields(self):
        ts = datetime.now(UTC)
        node = OpcUaNode(
            node_id="ns=2;s=Temperature",
            display_name="Temperature",
            data_type="Double",
            value=42.5,
            quality="good",
            timestamp=ts,
            writable=True,
            description="Room temperature sensor",
        )
        d = node.to_dict()
        assert d["node_id"] == "ns=2;s=Temperature"
        assert d["display_name"] == "Temperature"
        assert d["data_type"] == "Double"
        assert d["value"] == 42.5
        assert d["quality"] == "good"
        assert d["timestamp"] == ts.isoformat()
        assert d["writable"] is True
        assert d["description"] == "Room temperature sensor"

    def test_defaults(self):
        node = OpcUaNode(node_id="n1", display_name="Node 1")
        assert node.data_type == "Float"
        assert node.value is None
        assert node.quality == "good"
        assert node.timestamp is None
        assert node.writable is False
        assert node.description == ""

    def test_to_dict_timestamp_none(self):
        node = OpcUaNode(node_id="n1", display_name="Node 1", timestamp=None)
        d = node.to_dict()
        assert d["timestamp"] is None

    def test_to_dict_timestamp_isoformat(self):
        ts = datetime(2025, 1, 15, 10, 30, 0, tzinfo=UTC)
        node = OpcUaNode(node_id="n1", display_name="Node 1", timestamp=ts)
        d = node.to_dict()
        assert d["timestamp"] == "2025-01-15T10:30:00+00:00"


# ── OpcUaSubscription 数据类 ──


class TestOpcUaSubscription:
    def test_defaults(self):
        sub = OpcUaSubscription(subscription_id="sub1", node_ids=["n1", "n2"])
        assert sub.subscription_id == "sub1"
        assert sub.node_ids == ["n1", "n2"]
        assert sub.callback is None
        assert sub.sampling_interval == 500.0
        assert sub.publishing_interval == 1000.0
        assert sub.max_notifications_per_publish == 1000

    def test_custom_values(self):
        sub = OpcUaSubscription(
            subscription_id="sub2",
            node_ids=["n3"],
            sampling_interval=100.0,
            publishing_interval=500.0,
            max_notifications_per_publish=100,
        )
        assert sub.sampling_interval == 100.0
        assert sub.publishing_interval == 500.0
        assert sub.max_notifications_per_publish == 100


# ── OpcUaServerDriver 类元数据 ──


class TestOpcUaServerDriverMetadata:
    def test_plugin_name(self):
        assert OpcUaServerDriver.plugin_name == "opcua_server"

    def test_plugin_version(self):
        assert OpcUaServerDriver.plugin_version == "1.0.0"

    def test_supported_protocols(self):
        assert "opcua_server" in OpcUaServerDriver.supported_protocols

    def test_config_schema_exists(self):
        assert hasattr(OpcUaServerDriver, "config_schema")
        assert "description" in OpcUaServerDriver.config_schema
