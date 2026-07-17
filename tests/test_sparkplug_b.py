"""Sparkplug B 协议驱动单元测试

覆盖 src/edgelite/drivers/sparkplug_b.py：
- 模块级辅助函数：_map_datatype / _set_metric_value / _get_metric_value
- SparkplugBDriver：初始化、bdSeq 持久化、topic 构建、payload 编解码、
  设备增删、测点读写、连接循环、Birth/Death/Data 发布、命令处理、
  批量发布、设备发现

设计要点：
- sparkplugb 库未安装（_pb2=None），构造 fake _pb2 模块以覆盖编解码逻辑
- aiomqtt 与网络均被 mock，不发起真实连接
- 所有 asyncio.sleep 被加速，测试快速完成
- 资源（connect_task/batch_task）在测试末尾正确关闭
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, "src")

from edgelite.drivers import sparkplug_b as spb_mod
from edgelite.drivers.sparkplug_b import (
    _SPB_DATATYPE_BOOLEAN,
    _SPB_DATATYPE_DATETIME,
    _SPB_DATATYPE_DOUBLE,
    _SPB_DATATYPE_FLOAT,
    _SPB_DATATYPE_INT32,
    _SPB_DATATYPE_INT64,
    _SPB_DATATYPE_STRING,
    _SPB_DATATYPE_TEXT,
    _SPB_DATATYPE_UINT32,
    _SPB_DATATYPE_UINT64,
    SparkplugBDriver,
    _get_metric_value,
    _map_datatype,
    _set_metric_value,
)

# ── Fake sparkplugb_pb2 实现 ──────────────────────────────────────────


class _FakeMeta:
    def __init__(self):
        self.content: dict = {}


class _FakeMetric:
    """模拟 protobuf Metric 消息对象。"""

    def __init__(self):
        self.name = ""
        self.datatype = 0
        self.boolean_value = False
        self.int_value = 0
        self.long_value = 0
        self.float_value = 0.0
        self.double_value = 0.0
        self.string_value = ""
        self.alias = 0
        self.timestamp = 0
        self.is_historical = False
        self.is_transient = False
        self.metadata = _FakeMeta()
        self._which_oneof: str | None = None
        self._has_fields: set[str] = set()

    def WhichOneof(self, field_name: str) -> str | None:
        return self._which_oneof

    def HasField(self, name: str) -> bool:
        return name in self._has_fields


class _FakeMetricsList:
    """模拟 RepeatedScalarContainer，支持 add() 与迭代。"""

    def __init__(self):
        self._items: list[_FakeMetric] = []

    def add(self) -> _FakeMetric:
        m = _FakeMetric()
        self._items.append(m)
        return m

    def __iter__(self):
        return iter(self._items)

    def __len__(self):
        return len(self._items)


class _FakePayload:
    """模拟 protobuf Payload 消息对象。"""

    # 类级解码数据，供 ParseFromString 后迭代使用
    _decode_metrics: list[dict] = []

    def __init__(self):
        self.timestamp = 0
        self.seq = 0
        self.metrics = _FakeMetricsList()

    def SerializeToString(self) -> bytes:
        return b"FAKE_PAYLOAD_BYTES"

    def ParseFromString(self, data: bytes) -> None:
        # 根据类级配置填充 metrics，模拟反序列化
        for dm in _FakePayload._decode_metrics:
            m = self.metrics.add()
            m.name = dm.get("name", "")
            m.datatype = dm.get("datatype", 0)
            which = dm.get("which")
            m._which_oneof = which
            if which is not None:
                setattr(m, which, dm.get("value"))
            m._has_fields = set(dm.get("has_fields", []))
            m.alias = dm.get("alias", 0)
            m.timestamp = dm.get("timestamp", 0)
            m.is_historical = dm.get("is_historical", False)
            m.is_transient = dm.get("is_transient", False)


def _make_fake_pb2():
    """构造 fake sparkplugb_pb2 模块。"""
    mod = MagicMock()
    mod.Payload = _FakePayload
    return mod


# ── 辅助函数测试 ──


class TestMapDatatype:
    def test_bool(self):
        assert _map_datatype(True) == _SPB_DATATYPE_BOOLEAN
        assert _map_datatype(False) == _SPB_DATATYPE_BOOLEAN

    def test_positive_int_small(self):
        assert _map_datatype(100) == _SPB_DATATYPE_UINT32

    def test_positive_int_large(self):
        assert _map_datatype(5000000000) == _SPB_DATATYPE_UINT64

    def test_negative_int_small(self):
        assert _map_datatype(-100) == _SPB_DATATYPE_INT32

    def test_negative_int_large(self):
        assert _map_datatype(-3000000000) == _SPB_DATATYPE_INT64

    def test_float(self):
        assert _map_datatype(3.14) == _SPB_DATATYPE_DOUBLE

    def test_string(self):
        assert _map_datatype("hello") == _SPB_DATATYPE_STRING

    def test_unsupported_type(self):
        assert _map_datatype([1, 2]) is None
        assert _map_datatype(None) is None
        assert _map_datatype({"a": 1}) is None


class TestSetMetricValue:
    def test_boolean(self):
        m = _FakeMetric()
        _set_metric_value(m, _SPB_DATATYPE_BOOLEAN, True)
        assert m.boolean_value is True

    def test_int_types(self):
        m = _FakeMetric()
        _set_metric_value(m, _SPB_DATATYPE_INT32, 42)
        assert m.int_value == 42

    def test_long_types(self):
        m = _FakeMetric()
        _set_metric_value(m, _SPB_DATATYPE_INT64, 9999999999)
        assert m.long_value == 9999999999

    def test_datetime_type(self):
        m = _FakeMetric()
        _set_metric_value(m, _SPB_DATATYPE_DATETIME, 1700000000)
        assert m.long_value == 1700000000

    def test_float_type(self):
        m = _FakeMetric()
        _set_metric_value(m, _SPB_DATATYPE_FLOAT, 1.5)
        assert m.float_value == 1.5

    def test_double_type(self):
        m = _FakeMetric()
        _set_metric_value(m, _SPB_DATATYPE_DOUBLE, 2.5)
        assert m.double_value == 2.5

    def test_string_type(self):
        m = _FakeMetric()
        _set_metric_value(m, _SPB_DATATYPE_STRING, "text")
        assert m.string_value == "text"

    def test_text_type(self):
        m = _FakeMetric()
        _set_metric_value(m, _SPB_DATATYPE_TEXT, "long text")
        assert m.string_value == "long text"

    def test_unknown_type_falls_back_to_string(self):
        m = _FakeMetric()
        _set_metric_value(m, 999, "fallback")
        assert m.string_value == "fallback"


class TestGetMetricValue:
    def test_none_when_no_value_set(self):
        m = _FakeMetric()
        m._which_oneof = None
        assert _get_metric_value(m) is None

    def test_returns_value_field(self):
        m = _FakeMetric()
        m._which_oneof = "double_value"
        m.double_value = 12.5
        assert _get_metric_value(m) == 12.5

    def test_returns_string_field(self):
        m = _FakeMetric()
        m._which_oneof = "string_value"
        m.string_value = "abc"
        assert _get_metric_value(m) == "abc"


# ── 驱动元数据 ──


class TestDriverMetadata:
    def test_plugin_name(self):
        assert SparkplugBDriver.plugin_name == "sparkplug_b"

    def test_plugin_version(self):
        assert SparkplugBDriver.plugin_version == "1.1.0"

    def test_supported_protocols(self):
        assert "sparkplug_b" in SparkplugBDriver.supported_protocols

    def test_config_schema_has_fields(self):
        names = {f["name"] for f in SparkplugBDriver.config_schema["fields"]}
        assert "group_id" in names
        assert "edge_node_id" in names
        assert "mqtt_broker" in names


# ── 驱动初始化与序列号 ──


class TestDriverInit:
    def test_initial_state(self):
        drv = SparkplugBDriver()
        assert drv._running is False
        assert drv._client is None
        assert drv._seq_num == 0
        assert drv._bd_seq == 0
        assert drv._data_callback is None

    def test_next_seq_wraps_at_256(self):
        drv = SparkplugBDriver()
        drv._seq_num = 255
        assert drv._next_seq() == 255
        assert drv._seq_num == 0
        assert drv._next_seq() == 0
        assert drv._seq_num == 1

    def test_next_bd_seq_increments_and_wraps(self):
        drv = SparkplugBDriver()
        drv._bd_seq = 255
        # _save_bd_seq 无文件路径时为空操作
        assert drv._next_bd_seq() == 255
        assert drv._bd_seq == 0

    def test_build_topic_without_device(self):
        drv = SparkplugBDriver()
        drv._group_id = "g1"
        drv._edge_node_id = "n1"
        assert drv._build_topic("NBIRTH") == "spBv1.0/g1/NBIRTH/n1"

    def test_build_topic_with_device(self):
        drv = SparkplugBDriver()
        drv._group_id = "g1"
        drv._edge_node_id = "n1"
        assert drv._build_topic("DBIRTH", "d1") == "spBv1.0/g1/DBIRTH/n1/d1"


# ── bdSeq 持久化 ──


class TestBdSeqPersistence:
    def test_load_bd_seq_reads_file(self, tmp_path):
        drv = SparkplugBDriver()
        f = tmp_path / "bd.json"
        f.write_text('{"bd_seq": 5}', encoding="utf-8")
        drv._bd_seq_file = str(f)
        drv._load_bd_seq()
        assert drv._bd_seq == 5

    def test_load_bd_seq_no_file_path(self):
        drv = SparkplugBDriver()
        drv._bd_seq_file = ""
        drv._load_bd_seq()  # 不应抛异常
        assert drv._bd_seq == 0

    def test_load_bd_seq_missing_file_silent(self, tmp_path):
        drv = SparkplugBDriver()
        drv._bd_seq_file = str(tmp_path / "missing.json")
        drv._load_bd_seq()
        assert drv._bd_seq == 0

    def test_load_bd_seq_corrupt_file_silent(self, tmp_path):
        drv = SparkplugBDriver()
        f = tmp_path / "bd.json"
        f.write_text("not json", encoding="utf-8")
        drv._bd_seq_file = str(f)
        drv._load_bd_seq()
        assert drv._bd_seq == 0

    def test_save_bd_seq_writes_file(self, tmp_path):
        drv = SparkplugBDriver()
        f = tmp_path / "sub" / "bd.json"
        drv._bd_seq_file = str(f)
        drv._bd_seq = 7
        drv._save_bd_seq()
        assert f.exists()
        import json

        assert json.loads(f.read_text(encoding="utf-8"))["bd_seq"] == 7

    def test_save_bd_seq_no_file_path(self):
        drv = SparkplugBDriver()
        drv._bd_seq_file = ""
        drv._save_bd_seq()  # 不应抛异常


# ── Payload 编解码 ──


class TestEncodePayload:
    def test_returns_none_when_pb2_missing(self):
        drv = SparkplugBDriver()
        with patch.object(spb_mod, "_pb2", None):
            assert drv._encode_payload([{"name": "x", "value": 1}]) is None

    def test_encodes_basic_metrics(self):
        drv = SparkplugBDriver()
        with patch.object(spb_mod, "_pb2", _make_fake_pb2()):
            payload = drv._encode_payload([{"name": "t1", "value": 42}, {"name": "s1", "value": "hi"}], seq=3)
        assert payload == b"FAKE_PAYLOAD_BYTES"

    def test_skips_unsupported_type(self):
        drv = SparkplugBDriver()
        with patch.object(spb_mod, "_pb2", _make_fake_pb2()):
            payload = drv._encode_payload([{"name": "ok", "value": 1}, {"name": "bad", "value": [1, 2]}], seq=0)
        assert payload == b"FAKE_PAYLOAD_BYTES"

    def test_encodes_with_explicit_datatype(self):
        drv = SparkplugBDriver()
        with patch.object(spb_mod, "_pb2", _make_fake_pb2()):
            payload = drv._encode_payload([{"name": "n", "value": 0, "datatype": _SPB_DATATYPE_BOOLEAN}], seq=1)
        assert payload is not None

    def test_encodes_with_alias_and_timestamp(self):
        drv = SparkplugBDriver()
        with patch.object(spb_mod, "_pb2", _make_fake_pb2()):
            payload = drv._encode_payload(
                [
                    {
                        "name": "n",
                        "value": 1,
                        "alias": 10,
                        "timestamp": 12345,
                        "is_historical": True,
                        "is_transient": True,
                        "metadata": {"unit": "C"},
                    }
                ],
                seq=2,
            )
        assert payload is not None

    def test_encode_exception_returns_none(self):
        drv = SparkplugBDriver()
        # 用 MagicMock 让 Payload() 构造抛异常
        fake = MagicMock()
        fake.Payload = MagicMock(side_effect=RuntimeError("boom"))
        with patch.object(spb_mod, "_pb2", fake):
            assert drv._encode_payload([{"name": "x", "value": 1}]) is None


class TestDecodePayload:
    def teardown_method(self):
        _FakePayload._decode_metrics = []

    def test_returns_none_when_pb2_missing(self):
        drv = SparkplugBDriver()
        with patch.object(spb_mod, "_pb2", None):
            assert drv._decode_payload(b"data") is None

    def test_decodes_metrics(self):
        _FakePayload._decode_metrics = [
            {
                "name": "t1",
                "datatype": _SPB_DATATYPE_DOUBLE,
                "which": "double_value",
                "value": 3.14,
                "has_fields": ["timestamp", "alias"],
                "alias": 5,
                "timestamp": 100,
                "is_historical": True,
            }
        ]
        drv = SparkplugBDriver()
        with patch.object(spb_mod, "_pb2", _make_fake_pb2()):
            result = drv._decode_payload(b"data")
        assert result is not None
        assert len(result) == 1
        assert result[0]["name"] == "t1"
        assert result[0]["value"] == 3.14
        assert result[0]["datatype"] == _SPB_DATATYPE_DOUBLE
        assert result[0]["alias"] == 5
        assert result[0]["timestamp"] == 100
        assert result[0]["is_historical"] is True

    def test_decode_exception_returns_none(self):
        drv = SparkplugBDriver()
        # 让 ParseFromString 抛异常
        fake = MagicMock()
        payload_inst = MagicMock()
        payload_inst.ParseFromString.side_effect = RuntimeError("boom")
        fake.Payload = MagicMock(return_value=payload_inst)
        with patch.object(spb_mod, "_pb2", fake):
            assert drv._decode_payload(b"data") is None


# ── 配置夹具 ──


def _make_sp_config():
    return SimpleNamespace(
        group_id="g1",
        edge_node_id="n1",
        birth_debounce_ms=1000,
        mqtt_broker="localhost",
        mqtt_port=1883,
        mqtt_username="",
        mqtt_password="",
        tls_enabled=False,
        tls_ca_cert="",
        tls_client_cert="",
        tls_client_key="",
    )


@pytest.fixture
def mock_config(monkeypatch):
    cfg = SimpleNamespace(sparkplug_b=_make_sp_config())
    monkeypatch.setattr("edgelite.drivers.sparkplug_b.get_config", lambda: cfg)
    return cfg


@pytest.fixture
def driver():
    drv = SparkplugBDriver()
    yield drv
    # 清理：确保无残留任务
    drv._running = False
    for t in (drv._connect_task, drv._batch_task):
        if t and not t.done():
            t.cancel()


# ── start / stop ──


class TestStartStop:
    async def test_start_raises_without_config_section(self, monkeypatch):
        monkeypatch.setattr("edgelite.drivers.sparkplug_b.get_config", lambda: SimpleNamespace())
        drv = SparkplugBDriver()
        with pytest.raises(ValueError, match="configuration section not found"):
            await drv.start({})
        assert drv._running is False

    async def test_start_creates_connect_task(self, mock_config, driver):
        # _load_bd_seq 仅读取，文件不存在时静默跳过，无副作用
        await driver.start({"group_id": "g1", "edge_node_id": "n1"})
        try:
            assert driver._running is True
            assert driver._connect_task is not None
            assert driver._group_id == "g1"
            assert driver._edge_node_id == "n1"
        finally:
            # 取消 connect_task 避免真实连接
            driver._connect_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await driver._connect_task
            driver._connect_task = None

    async def test_start_uses_config_overrides(self, mock_config, driver, monkeypatch, tmp_path):
        await driver.start({"group_id": "grp", "edge_node_id": "node", "birth_debounce_ms": 500})
        try:
            assert driver._group_id == "grp"
            assert driver._edge_node_id == "node"
            assert driver._birth_debounce_ms == 500
        finally:
            driver._connect_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await driver._connect_task
            driver._connect_task = None

    async def test_stop_cleans_state(self, driver):
        driver._running = True
        driver._nbirth_published = True
        driver._dbirth_published.add("d1")
        driver._client = None  # 无 client，stop 跳过发布
        await driver.stop()
        assert driver._running is False
        assert driver._nbirth_published is False
        assert len(driver._dbirth_published) == 0

    async def test_stop_publishes_ddeath_for_published_devices(self, driver):
        driver._running = True
        driver._dbirth_published.add("d1")
        driver._client = AsyncMock()
        with patch.object(spb_mod, "_pb2", _make_fake_pb2()):
            await driver.stop()
        assert "d1" not in driver._dbirth_published

    async def test_stop_cancels_connect_task(self, driver):
        driver._running = True
        driver._connect_task = asyncio.create_task(asyncio.sleep(100))
        await driver.stop()
        assert driver._connect_task.done()

    async def test_stop_client_disconnect_exception_silent(self, driver):
        driver._running = True
        mock_client = AsyncMock()
        mock_client.__aexit__ = AsyncMock(side_effect=RuntimeError("disconnect fail"))
        driver._client = mock_client
        await driver.stop()  # 不应抛异常
        assert driver._client is None


# ── 设备增删 ──


class TestDeviceManagement:
    async def test_add_device_stores_metadata(self, driver):
        await driver.add_device("d1", {"name": "dev1"}, [{"name": "p1"}, {"address": "p2"}])
        assert "d1" in driver._device_metadata
        assert "p1" in driver._device_points["d1"]
        assert "p2" in driver._device_points["d1"]
        assert driver._latest_values["d1"] == {}

    async def test_add_device_publishes_dbirth_when_connected(self, driver):
        driver._client = AsyncMock()
        driver._nbirth_published = True
        with patch.object(spb_mod, "_pb2", _make_fake_pb2()):
            await driver.add_device("d1", {}, [{"name": "p1"}])
        assert "d1" in driver._dbirth_published

    async def test_add_device_no_points(self, driver):
        await driver.add_device("d1", {})
        assert "d1" in driver._device_metadata

    async def test_remove_device_publishes_ddeath_when_published(self, driver):
        driver._client = AsyncMock()
        driver._dbirth_published.add("d1")
        driver._device_metadata["d1"] = {}
        driver._device_points["d1"] = {}
        driver._latest_values["d1"] = {}
        with patch.object(spb_mod, "_pb2", _make_fake_pb2()):
            await driver.remove_device("d1")
        assert "d1" not in driver._device_metadata
        assert "d1" not in driver._dbirth_published

    async def test_remove_device_not_published_no_ddeath(self, driver):
        driver._device_metadata["d1"] = {}
        await driver.remove_device("d1")
        assert "d1" not in driver._device_metadata


# ── 测点读写 ──


class TestReadPoints:
    async def test_read_returns_cached_values(self, driver):
        driver._latest_values["d1"] = {"p1": 42, "p2": None}
        result = await driver.read_points("d1", ["p1", "p2", "p3"])
        assert result == {"p1": 42}

    async def test_read_empty_device(self, driver):
        result = await driver.read_points("unknown", ["p1"])
        assert result == {}


class TestWritePoint:
    async def test_write_returns_false_without_client(self, driver):
        assert await driver.write_point("d1", "p1", 10) is False

    async def test_write_success(self, driver):
        driver._client = AsyncMock()
        with patch.object(spb_mod, "_pb2", _make_fake_pb2()):
            ok = await driver.write_point("d1", "p1", 10)
        assert ok is True
        assert driver._latest_values["d1"]["p1"] == 10
        driver._client.publish.assert_called_once()

    async def test_write_encode_failure_returns_false(self, driver):
        driver._client = AsyncMock()
        with patch.object(spb_mod, "_pb2", None):
            ok = await driver.write_point("d1", "p1", 10)
        assert ok is False

    async def test_write_publish_exception_returns_false(self, driver):
        driver._client = AsyncMock()
        driver._client.publish = AsyncMock(side_effect=RuntimeError("publish fail"))
        with patch.object(spb_mod, "_pb2", _make_fake_pb2()):
            ok = await driver.write_point("d1", "p1", 10)
        assert ok is False


class TestOnData:
    def test_on_data_registers_callback(self, driver):
        def cb(*a):
            pass

        driver.on_data(cb)
        assert driver._data_callback is cb


# ── 发布方法 ──


class TestPublishMethods:
    async def test_publish_nbirth_no_client(self, driver):
        await driver._publish_nbirth()  # 不应抛异常

    async def test_publish_nbirth_success(self, driver):
        driver._client = AsyncMock()
        driver._group_id = "g1"
        driver._edge_node_id = "n1"
        driver._device_metadata["d1"] = {}
        with patch.object(spb_mod, "_pb2", _make_fake_pb2()):
            await driver._publish_nbirth()
        assert driver._nbirth_published is True
        driver._client.publish.assert_called_once()

    async def test_publish_nbirth_publish_exception(self, driver):
        driver._client = AsyncMock()
        driver._client.publish = AsyncMock(side_effect=RuntimeError("fail"))
        with patch.object(spb_mod, "_pb2", _make_fake_pb2()):
            await driver._publish_nbirth()
        assert driver._nbirth_published is False

    async def test_publish_dbirth_no_client(self, driver):
        await driver._publish_dbirth("d1")

    async def test_publish_dbirth_with_points(self, driver):
        driver._client = AsyncMock()
        driver._group_id = "g1"
        driver._edge_node_id = "n1"
        driver._device_points["d1"] = {"p1": 10}
        driver._device_metadata["d1"] = {
            "points": {"p1": {"datatype": _SPB_DATATYPE_INT32, "alias": 1, "metadata": {"u": "C"}}}
        }
        with patch.object(spb_mod, "_pb2", _make_fake_pb2()):
            await driver._publish_dbirth("d1")
        assert "d1" in driver._dbirth_published

    async def test_publish_dbirth_no_points_adds_online(self, driver):
        driver._client = AsyncMock()
        driver._group_id = "g1"
        driver._edge_node_id = "n1"
        driver._device_points["d1"] = {}
        driver._device_metadata["d1"] = {}
        with patch.object(spb_mod, "_pb2", _make_fake_pb2()):
            await driver._publish_dbirth("d1")
        assert "d1" in driver._dbirth_published

    async def test_publish_ddeath_no_client(self, driver):
        await driver._publish_ddeath("d1")

    async def test_publish_ddeath_success(self, driver):
        driver._client = AsyncMock()
        driver._group_id = "g1"
        driver._edge_node_id = "n1"
        driver._dbirth_published.add("d1")
        with patch.object(spb_mod, "_pb2", _make_fake_pb2()):
            await driver._publish_ddeath("d1")
        assert "d1" not in driver._dbirth_published

    async def test_publish_ddeath_publish_exception(self, driver):
        driver._client = AsyncMock()
        driver._client.publish = AsyncMock(side_effect=RuntimeError("fail"))
        driver._dbirth_published.add("d1")
        with patch.object(spb_mod, "_pb2", _make_fake_pb2()):
            await driver._publish_ddeath("d1")
        # 失败时仍保留在已发布集合
        assert "d1" in driver._dbirth_published

    async def test_publish_ndata_no_client(self, driver):
        await driver._publish_ndata([{"name": "x", "value": 1}])

    async def test_publish_ndata_not_published(self, driver):
        driver._client = AsyncMock()
        driver._nbirth_published = False
        await driver._publish_ndata([{"name": "x", "value": 1}])
        driver._client.publish.assert_not_called()

    async def test_publish_ndata_success(self, driver):
        driver._client = AsyncMock()
        driver._nbirth_published = True
        with patch.object(spb_mod, "_pb2", _make_fake_pb2()):
            await driver._publish_ndata([{"name": "x", "value": 1}])
        driver._client.publish.assert_called_once()

    async def test_publish_ddata_no_client(self, driver):
        await driver._publish_ddata("d1", [{"name": "x", "value": 1}])

    async def test_publish_ddata_publishes_dbirth_first(self, driver):
        driver._client = AsyncMock()
        driver._group_id = "g1"
        driver._edge_node_id = "n1"
        driver._device_points["d1"] = {"p1": 0}
        driver._device_metadata["d1"] = {}
        with patch.object(spb_mod, "_pb2", _make_fake_pb2()):
            await driver._publish_ddata("d1", [{"name": "p1", "value": 5}])
        assert "d1" in driver._dbirth_published

    async def test_publish_ddata_publish_exception(self, driver):
        driver._client = AsyncMock()
        driver._client.publish = AsyncMock(side_effect=RuntimeError("fail"))
        driver._group_id = "g1"
        driver._edge_node_id = "n1"
        driver._device_points["d1"] = {}
        driver._device_metadata["d1"] = {}
        with patch.object(spb_mod, "_pb2", _make_fake_pb2()):
            await driver._publish_ddata("d1", [{"name": "p1", "value": 5}])


# ── 消息处理 ──


class TestHandleMessage:
    async def test_undecodable_payload_returns(self, driver):
        msg = SimpleNamespace(topic="spBv1.0/g1/NCMD/n1", payload=b"bad")
        with patch.object(spb_mod, "_pb2", None):
            await driver._handle_message(msg)  # 不应抛异常

    async def test_invalid_topic_returns(self, driver):
        msg = SimpleNamespace(topic="invalid/topic", payload=b"data")
        with patch.object(spb_mod, "_pb2", _make_fake_pb2()):
            await driver._handle_message(msg)

    async def test_short_topic_returns(self, driver):
        msg = SimpleNamespace(topic="spBv1.0/g1", payload=b"data")
        with patch.object(spb_mod, "_pb2", _make_fake_pb2()):
            await driver._handle_message(msg)

    async def test_handles_ncmd_rebirth(self, driver):
        driver._client = AsyncMock()
        driver._group_id = "g1"
        driver._edge_node_id = "n1"
        _FakePayload._decode_metrics = [
            {"name": "Node Control/Rebirth", "datatype": _SPB_DATATYPE_BOOLEAN, "which": "boolean_value", "value": True}
        ]
        # 源码使用 msg_type = parts[3]，故 NCMD 须位于第 4 段
        msg = SimpleNamespace(topic="spBv1.0/g1/node/NCMD", payload=b"data")
        with patch.object(spb_mod, "_pb2", _make_fake_pb2()):
            await driver._handle_message(msg)
        assert driver._nbirth_published is True
        _FakePayload._decode_metrics = []

    async def test_handles_dcmd_write(self, driver):
        driver._client = AsyncMock()
        driver._group_id = "g1"
        driver._edge_node_id = "n1"
        driver._enable_cmd_response = False
        _FakePayload._decode_metrics = [
            {"name": "p1", "datatype": _SPB_DATATYPE_INT32, "which": "int_value", "value": 42}
        ]
        # 源码使用 msg_type = parts[3]，device_id = parts[4]
        msg = SimpleNamespace(topic="spBv1.0/g1/node/DCMD/d1", payload=b"data")
        with patch.object(spb_mod, "_pb2", _make_fake_pb2()):
            await driver._handle_message(msg)
        assert driver._latest_values.get("d1", {}).get("p1") == 42
        _FakePayload._decode_metrics = []

    async def test_handles_dcmd_with_callback(self, driver):
        driver._client = AsyncMock()
        driver._group_id = "g1"
        driver._edge_node_id = "n1"
        driver._enable_cmd_response = False
        driver._data_callback = AsyncMock()
        _FakePayload._decode_metrics = [
            {"name": "p1", "datatype": _SPB_DATATYPE_INT32, "which": "int_value", "value": 42}
        ]
        msg = SimpleNamespace(topic="spBv1.0/g1/node/DCMD/d1", payload=b"data")
        with patch.object(spb_mod, "_pb2", _make_fake_pb2()):
            await driver._handle_message(msg)
        driver._data_callback.assert_called_once()
        _FakePayload._decode_metrics = []

    async def test_handles_unknown_msg_type(self, driver):
        _FakePayload._decode_metrics = []
        msg = SimpleNamespace(topic="spBv1.0/g1/node/UNKOWN", payload=b"data")
        with patch.object(spb_mod, "_pb2", _make_fake_pb2()):
            await driver._handle_message(msg)  # 不应抛异常
        _FakePayload._decode_metrics = []

    async def test_handle_message_exception_silent(self, driver):
        msg = SimpleNamespace(topic="spBv1.0/g1/NCMD/n1", payload=b"data")
        # 让 _decode_payload 抛异常
        with patch.object(driver, "_decode_payload", side_effect=RuntimeError("boom")):
            await driver._handle_message(msg)


class TestHandleNcmd:
    async def test_rebirth_republishes_all(self, driver):
        driver._client = AsyncMock()
        driver._group_id = "g1"
        driver._edge_node_id = "n1"
        driver._device_metadata["d1"] = {}
        driver._device_points["d1"] = {"p1": 0}
        with patch.object(spb_mod, "_pb2", _make_fake_pb2()):
            await driver._handle_ncmd([{"name": "Node Control/Rebirth", "value": True}])
        assert driver._nbirth_published is True
        assert "d1" in driver._dbirth_published

    async def test_non_rebirth_command_ignored(self, driver):
        driver._client = AsyncMock()
        await driver._handle_ncmd([{"name": "Node Control/NextSeq", "value": 5}])
        driver._client.publish.assert_not_called()


class TestHandleDcmd:
    async def test_dcmd_write_success_with_response(self, driver):
        driver._client = AsyncMock()
        driver._group_id = "g1"
        driver._edge_node_id = "n1"
        driver._enable_cmd_response = True
        with patch.object(spb_mod, "_pb2", _make_fake_pb2()):
            await driver._handle_dcmd("d1", [{"name": "p1", "value": 42}])
        assert driver._latest_values["d1"]["p1"] == 42

    async def test_dcmd_skips_none_value(self, driver):
        driver._client = AsyncMock()
        await driver._handle_dcmd("d1", [{"name": "p1", "value": None}])
        driver._client.publish.assert_not_called()

    async def test_send_cmd_response_no_client(self, driver):
        await driver._send_cmd_response("d1", "p1", 42, True)  # 不应抛异常

    async def test_send_cmd_response_success(self, driver):
        driver._client = AsyncMock()
        driver._group_id = "g1"
        driver._edge_node_id = "n1"
        with patch.object(spb_mod, "_pb2", _make_fake_pb2()):
            await driver._send_cmd_response("d1", "p1", 42, True)
        driver._client.publish.assert_called_once()

    async def test_send_cmd_response_publish_exception(self, driver):
        driver._client = AsyncMock()
        driver._client.publish = AsyncMock(side_effect=RuntimeError("fail"))
        with patch.object(spb_mod, "_pb2", _make_fake_pb2()):
            await driver._send_cmd_response("d1", "p1", 42, False)


# ── handle_point_update 与批量发布 ──


class TestHandlePointUpdate:
    async def test_not_running_returns(self, driver):
        driver._running = False
        await driver.handle_point_update(SimpleNamespace(device_id="d1", point_name="p1", value=1))
        assert driver._latest_values == {}

    async def test_missing_device_id_returns(self, driver):
        driver._running = True
        await driver.handle_point_update(SimpleNamespace(device_id="", point_name="p1", value=1))
        assert driver._latest_values == {}

    async def test_missing_point_name_returns(self, driver):
        driver._running = True
        await driver.handle_point_update(SimpleNamespace(device_id="d1", point_name="", value=1))
        assert driver._latest_values == {}

    async def test_updates_value_and_starts_batch(self, driver):
        driver._running = True
        await driver.handle_point_update(SimpleNamespace(device_id="d1", point_name="p1", value=42))
        assert driver._latest_values["d1"]["p1"] == 42
        assert "d1" in driver._pending_metrics
        assert driver._batch_task is not None
        # 清理 batch task
        driver._batch_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await driver._batch_task


class TestBatchPublishLoop:
    async def test_publishes_pending_metrics(self, driver):
        driver._running = True
        driver._client = AsyncMock()
        driver._group_id = "g1"
        driver._edge_node_id = "n1"
        driver._device_points["d1"] = {}
        driver._device_metadata["d1"] = {}
        driver._pending_metrics["d1"] = [{"name": "p1", "value": 42}]
        driver._batch_interval_ms = 1  # 极短间隔

        with patch.object(spb_mod, "_pb2", _make_fake_pb2()):
            task = asyncio.create_task(driver._batch_publish_loop())
            # 等待循环处理完毕（pending 清空后循环自动退出）
            await asyncio.wait_for(task, timeout=3)
        assert len(driver._pending_metrics) == 0
        driver._running = False

    async def test_batch_loop_exception_silent(self, driver):
        driver._running = True
        driver._client = AsyncMock()
        driver._group_id = "g1"
        driver._edge_node_id = "n1"
        driver._device_points["d1"] = {}
        driver._device_metadata["d1"] = {}
        driver._pending_metrics["d1"] = [{"name": "p1", "value": 42}]
        driver._batch_interval_ms = 1

        # _publish_ddata 抛异常，循环应捕获并退出（pending 已清空）
        with patch.object(driver, "_publish_ddata", side_effect=RuntimeError("boom")):
            task = asyncio.create_task(driver._batch_publish_loop())
            await asyncio.wait_for(task, timeout=3)
        # 异常被捕获，循环正常退出
        assert len(driver._pending_metrics) == 0
        driver._running = False


# ── discover_devices ──


class TestDiscoverDevices:
    async def test_returns_empty_when_aiomqtt_missing(self, driver):
        with patch.dict(sys.modules, {"aiomqtt": None}):
            result = await driver.discover_devices({})
        assert result == []

    async def test_returns_empty_when_pb2_missing(self, driver):
        with patch.object(spb_mod, "_pb2", None):
            result = await driver.discover_devices({})
        assert result == []

    async def test_discover_parses_dbirth(self, driver):
        _FakePayload._decode_metrics = [
            {"name": "tag1", "datatype": _SPB_DATATYPE_DOUBLE, "which": "double_value", "value": 1.0}
        ]
        # 构造 mock aiomqtt.Client 异步上下文管理器
        mock_msg = SimpleNamespace(
            topic="spBv1.0/grp/DBIRTH/node/dev1",
            payload=b"data",
        )

        class _FakeMessages:
            def __init__(self, msgs):
                self._msgs = list(msgs)

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self._msgs:
                    return self._msgs.pop(0)
                raise StopAsyncIteration

        mock_client = AsyncMock()
        mock_client.subscribe = AsyncMock()
        mock_client.messages = _FakeMessages([mock_msg])
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_client)
        cm.__aexit__ = AsyncMock(return_value=None)

        fake_aiomqtt = MagicMock()
        fake_aiomqtt.Client = MagicMock(return_value=cm)

        with patch.dict(sys.modules, {"aiomqtt": fake_aiomqtt}), patch.object(spb_mod, "_pb2", _make_fake_pb2()):
            result = await driver.discover_devices({"timeout": 0.1})

        assert len(result) == 1
        assert result[0]["device_id"] == "spb_grp_node_dev1"
        assert result[0]["protocol"] == "sparkplug_b"
        assert len(result[0]["points"]) == 1
        _FakePayload._decode_metrics = []

    async def test_discover_with_group_filter(self, driver):
        _FakePayload._decode_metrics = []
        mock_client = AsyncMock()
        mock_client.subscribe = AsyncMock()

        class _EmptyMessages:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

        mock_client.messages = _EmptyMessages()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_client)
        cm.__aexit__ = AsyncMock(return_value=None)

        fake_aiomqtt = MagicMock()
        fake_aiomqtt.Client = MagicMock(return_value=cm)

        with patch.dict(sys.modules, {"aiomqtt": fake_aiomqtt}), patch.object(spb_mod, "_pb2", _make_fake_pb2()):
            result = await driver.discover_devices({"group_id": "grp", "timeout": 0.1})
        assert result == []

    async def test_discover_connection_failure_returns_empty(self, driver):
        fake_aiomqtt = MagicMock()
        fake_aiomqtt.Client = MagicMock(side_effect=ConnectionRefusedError("refused"))
        with patch.dict(sys.modules, {"aiomqtt": fake_aiomqtt}), patch.object(spb_mod, "_pb2", _make_fake_pb2()):
            result = await driver.discover_devices({"timeout": 0.1})
        assert result == []


# ── 连接循环 ──


class TestConnectLoop:
    async def test_connect_loop_import_error_retries(self, driver, mock_config):
        driver._running = True

        # 模拟 ImportError 后快速终止
        original_sleep = asyncio.sleep

        async def _fast_sleep(delay, *a, **kw):
            driver._running = False
            await original_sleep(0)

        with patch("asyncio.sleep", new=_fast_sleep), patch.dict(sys.modules, {"aiomqtt": None}):
            await asyncio.wait_for(driver._connect_loop(), timeout=3)
        assert driver._client is None

    async def test_connect_loop_exception_retries(self, driver, mock_config):
        driver._running = True
        original_sleep = asyncio.sleep

        async def _fast_sleep(delay, *a, **kw):
            driver._running = False
            await original_sleep(0)

        # aiomqtt.Client 抛异常
        fake_aiomqtt = MagicMock()
        fake_aiomqtt.Client = MagicMock(side_effect=RuntimeError("conn fail"))
        fake_aiomqtt.Will = MagicMock()

        with (
            patch("asyncio.sleep", new=_fast_sleep),
            patch.dict(sys.modules, {"aiomqtt": fake_aiomqtt}),
            patch.object(spb_mod, "_pb2", _make_fake_pb2()),
        ):
            await asyncio.wait_for(driver._connect_loop(), timeout=3)
        assert driver._client is None

    async def test_connect_loop_success_and_message(self, driver, mock_config):
        driver._running = True
        driver._group_id = "g1"
        driver._edge_node_id = "n1"

        # 构造 mock client 与 messages
        mock_client = AsyncMock()
        mock_client.subscribe = AsyncMock()
        mock_client.publish = AsyncMock()

        class _Msgs:
            def __init__(self):
                self._count = 0

            def __aiter__(self):
                return self

            async def __anext__(self):
                # 第一条消息后停止
                if self._count > 0:
                    driver._running = False
                    raise StopAsyncIteration
                self._count += 1
                return SimpleNamespace(topic="spBv1.0/g1/NCMD/n1", payload=b"data")

        mock_client.messages = _Msgs()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_client)
        cm.__aexit__ = AsyncMock(return_value=None)

        fake_aiomqtt = MagicMock()
        fake_aiomqtt.Client = MagicMock(return_value=cm)
        fake_aiomqtt.Will = MagicMock()

        _FakePayload._decode_metrics = []
        with patch.dict(sys.modules, {"aiomqtt": fake_aiomqtt}), patch.object(spb_mod, "_pb2", _make_fake_pb2()):
            await asyncio.wait_for(driver._connect_loop(), timeout=5)
        _FakePayload._decode_metrics = []
        assert driver._nbirth_published is True

    async def test_connect_loop_tls_enabled(self, driver, mock_config):
        driver._running = True
        driver._group_id = "g1"
        driver._edge_node_id = "n1"
        mock_config.sparkplug_b.tls_enabled = True
        mock_config.sparkplug_b.tls_ca_cert = ""
        mock_config.sparkplug_b.tls_client_cert = ""
        mock_config.sparkplug_b.tls_client_key = ""

        mock_client = AsyncMock()
        mock_client.subscribe = AsyncMock()
        mock_client.publish = AsyncMock()

        class _Msgs:
            def __aiter__(self):
                return self

            async def __anext__(self):
                driver._running = False
                raise StopAsyncIteration

        mock_client.messages = _Msgs()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_client)
        cm.__aexit__ = AsyncMock(return_value=None)

        fake_aiomqtt = MagicMock()
        fake_aiomqtt.Client = MagicMock(return_value=cm)
        fake_aiomqtt.Will = MagicMock()

        _FakePayload._decode_metrics = []
        with patch.dict(sys.modules, {"aiomqtt": fake_aiomqtt}), patch.object(spb_mod, "_pb2", _make_fake_pb2()):
            await asyncio.wait_for(driver._connect_loop(), timeout=5)
        _FakePayload._decode_metrics = []
