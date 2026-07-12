"""协议转换网关测试

覆盖模块：src/edgelite/engine/protocol_bridge.py
- MappingRule / ProtocolBridge 数据类
- ProtocolConverter: convert / reverse_convert 各种数据类型转换
- ProtocolBridgeManager: start/stop/add_bridge/remove_bridge/add_mapping_rule/
  remove_mapping_rule/update_source_data/_process_bridge/_sync_loop/
  register_transform_callback/get_bridges/get_bridge_stats
- ModbusToOpcUaConverter: add_mapping/update_modbus_data/read_node/get_all_nodes
- get_bridge_manager 全局单例

所有异步循环通过 patch asyncio.sleep 快速终止，不产生真实延迟。
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, "src")

from edgelite.engine.protocol_bridge import (
    MappingRule,
    ModbusToOpcUaConverter,
    ProtocolBridge,
    ProtocolBridgeManager,
    ProtocolConverter,
    get_bridge_manager,
)


# ── 辅助：快速终止 _sync_loop ──


@contextlib.contextmanager
def _fast_terminate(mgr: ProtocolBridgeManager):
    """将 asyncio.sleep 替换为立即返回并终止 _running，用于快速结束 _sync_loop。"""
    real_sleep = asyncio.sleep

    async def _sleep(delay, *args, **kwargs):
        mgr._running = False
        await real_sleep(0)

    with patch("asyncio.sleep", _sleep):
        yield


def _make_rule(
    rule_id="r1",
    source_device="dev1",
    source_point="pt1",
    target_device="tdev1",
    target_point="tpt1",
    data_type="passthrough",
    scale=1.0,
    offset=0.0,
    enabled=True,
):
    return MappingRule(
        rule_id=rule_id,
        source_protocol="modbus_tcp",
        target_protocol="opc_ua",
        source_device=source_device,
        source_point=source_point,
        target_device=target_device,
        target_point=target_point,
        data_type=data_type,
        scale=scale,
        offset=offset,
        enabled=enabled,
    )


def _make_bridge(bridge_id="b1", rules=None, enabled=True):
    return ProtocolBridge(
        bridge_id=bridge_id,
        source_protocol="modbus_tcp",
        target_protocol="opc_ua",
        mapping_rules=rules or [],
        enabled=enabled,
    )


# ── MappingRule 数据类 ──


class TestMappingRule:
    def test_defaults(self):
        r = MappingRule(
            rule_id="r1",
            source_protocol="modbus_tcp",
            target_protocol="opc_ua",
            source_device="d1",
            source_point="p1",
            target_device="td1",
            target_point="tp1",
        )
        assert r.data_type == "passthrough"
        assert r.scale == 1.0
        assert r.offset == 0.0
        assert r.enabled is True

    def test_custom_values(self):
        r = MappingRule(
            rule_id="r2",
            source_protocol="modbus_tcp",
            target_protocol="opc_ua",
            source_device="d1",
            source_point="p1",
            target_device="td1",
            target_point="tp1",
            data_type="scale",
            scale=0.1,
            offset=5.0,
            enabled=False,
        )
        assert r.data_type == "scale"
        assert r.scale == 0.1
        assert r.offset == 5.0
        assert r.enabled is False


# ── ProtocolBridge 数据类 ──


class TestProtocolBridge:
    def test_defaults(self):
        b = ProtocolBridge(
            bridge_id="b1",
            source_protocol="modbus_tcp",
            target_protocol="opc_ua",
        )
        assert b.source_config == {}
        assert b.target_config == {}
        assert b.mapping_rules == []
        assert b.enabled is True

    def test_with_rules(self):
        r1 = _make_rule()
        b = ProtocolBridge(
            bridge_id="b1",
            source_protocol="modbus_tcp",
            target_protocol="opc_ua",
            mapping_rules=[r1],
            enabled=False,
        )
        assert len(b.mapping_rules) == 1
        assert b.enabled is False


# ── ProtocolConverter.convert ──


class TestProtocolConverterConvert:
    def test_passthrough(self):
        assert ProtocolConverter.convert(42, "passthrough") == 42.0

    def test_passthrough_string(self):
        assert ProtocolConverter.convert("3.14", "passthrough") == 3.14

    def test_int16_to_float32_positive(self):
        assert ProtocolConverter.convert(100, "int16_to_float32") == 100.0

    def test_int16_to_float32_negative(self):
        """值 >= 32768 时减去 65536（补码转有符号）"""
        result = ProtocolConverter.convert(65536, "int16_to_float32")
        # 65536 - 65536 = 0
        assert result == 0.0

    def test_int16_to_float32_with_scale_offset(self):
        result = ProtocolConverter.convert(40000, "int16_to_float32", scale=0.1, offset=5.0)
        # 40000 - 65536 = -25536, -25536 * 0.1 + 5.0 = -2548.6
        assert result == pytest.approx(-2548.6)

    def test_uint16_to_float32(self):
        assert ProtocolConverter.convert(100, "uint16_to_float32", scale=2.0, offset=1.0) == 201.0

    def test_scale(self):
        assert ProtocolConverter.convert(10, "scale", scale=3.0, offset=2.0) == 32.0

    def test_offset(self):
        assert ProtocolConverter.convert(10, "offset", offset=5.0) == 15.0

    def test_invert(self):
        assert ProtocolConverter.convert(10, "invert") == -10.0

    def test_invert_negative(self):
        assert ProtocolConverter.convert(-5, "invert") == 5.0

    def test_bool_to_int_true(self):
        assert ProtocolConverter.convert(1, "bool_to_int") == 1

    def test_bool_to_int_false(self):
        assert ProtocolConverter.convert(0, "bool_to_int") == 0

    def test_bool_to_float_true(self):
        assert ProtocolConverter.convert(1, "bool_to_float") == 1.0

    def test_bool_to_float_false(self):
        assert ProtocolConverter.convert(0, "bool_to_float") == 0.0

    def test_percent_to_0_100(self):
        result = ProtocolConverter.convert(27648, "percent_to_0_100")
        assert result == pytest.approx(100.0)

    def test_percent_to_0_100_half(self):
        result = ProtocolConverter.convert(13824, "percent_to_0_100")
        assert result == pytest.approx(50.0)

    def test_percent_to_0_100_with_scale_offset(self):
        result = ProtocolConverter.convert(27648, "percent_to_0_100", scale=2.0, offset=1.0)
        assert result == pytest.approx(201.0)

    def test_temperature_pt100(self):
        result = ProtocolConverter.convert(32767, "temperature_pt100")
        assert result == pytest.approx(200.0)

    def test_temperature_pt100_zero(self):
        result = ProtocolConverter.convert(0, "temperature_pt100")
        assert result == pytest.approx(0.0)

    def test_unknown_type_falls_through_to_scale(self):
        assert ProtocolConverter.convert(10, "unknown_type", scale=2.0, offset=1.0) == 21.0

    def test_invalid_value_returns_none(self):
        assert ProtocolConverter.convert("not_a_number", "passthrough") is None

    def test_none_value_returns_none(self):
        assert ProtocolConverter.convert(None, "passthrough") is None

    def test_zero_division_returns_none(self):
        """float(None) 会抛 TypeError，应返回 None"""
        assert ProtocolConverter.convert(None, "scale") is None


# ── ProtocolConverter.reverse_convert ──


class TestProtocolConverterReverseConvert:
    def test_passthrough(self):
        assert ProtocolConverter.reverse_convert(42.7, "passthrough") == 42

    def test_int16_to_float32_positive(self):
        result = ProtocolConverter.reverse_convert(100.0, "int16_to_float32", scale=1.0, offset=0.0)
        assert result == 100

    def test_int16_to_float32_negative(self):
        """负值反向转换应加 65536"""
        result = ProtocolConverter.reverse_convert(-100.0, "int16_to_float32", scale=1.0, offset=0.0)
        assert result == (-100 + 65536) & 0xFFFF
        assert result == 65436

    def test_uint16_to_float32(self):
        result = ProtocolConverter.reverse_convert(201.0, "uint16_to_float32", scale=2.0, offset=1.0)
        assert result == 100

    def test_scale(self):
        result = ProtocolConverter.reverse_convert(32.0, "scale", scale=3.0, offset=2.0)
        assert result == 10

    def test_percent_to_0_27648(self):
        result = ProtocolConverter.reverse_convert(100.0, "percent_to_0_27648")
        assert result == 27648

    def test_unknown_type_falls_through(self):
        result = ProtocolConverter.reverse_convert(21.0, "unknown_type", scale=2.0, offset=1.0)
        assert result == 10

    def test_invalid_value_returns_zero(self):
        assert ProtocolConverter.reverse_convert("not_a_number", "passthrough") == 0

    def test_none_returns_zero(self):
        assert ProtocolConverter.reverse_convert(None, "passthrough") == 0


# ── ProtocolBridgeManager 初始化 ──


class TestBridgeManagerInit:
    def test_defaults(self):
        mgr = ProtocolBridgeManager()
        assert mgr._running is False
        assert mgr._bridges == {}
        assert mgr._source_data == {}
        assert mgr._task is None
        assert mgr._transform_callbacks == []
        assert mgr._processed_versions == {}
        assert isinstance(mgr._conversion, ProtocolConverter)


# ── ProtocolBridgeManager start/stop ──


class TestBridgeManagerStartStop:
    async def test_start_creates_task(self):
        mgr = ProtocolBridgeManager()
        with _fast_terminate(mgr):
            await mgr.start()
            assert mgr._running is True
            assert mgr._task is not None
            # Wait for task to complete (it will terminate due to _fast_terminate)
            with contextlib.suppress(asyncio.CancelledError):
                await mgr._task
        assert mgr._running is False

    async def test_stop_cancels_task(self):
        mgr = ProtocolBridgeManager()
        mgr._task = asyncio.create_task(asyncio.sleep(100))
        await mgr.stop()
        assert mgr._running is False
        assert mgr._task.done()

    async def test_stop_no_task_silent(self):
        mgr = ProtocolBridgeManager()
        await mgr.stop()
        assert mgr._running is False

    async def test_start_stop_cycle(self):
        mgr = ProtocolBridgeManager()
        with _fast_terminate(mgr):
            await mgr.start()
        await mgr.stop()
        assert mgr._running is False


# ── ProtocolBridgeManager bridge/rule 管理 ──


class TestBridgeManagerBridgeRule:
    def test_add_bridge(self):
        mgr = ProtocolBridgeManager()
        bridge = _make_bridge()
        mgr.add_bridge(bridge)
        assert "b1" in mgr._bridges

    def test_add_bridge_multiple(self):
        mgr = ProtocolBridgeManager()
        mgr.add_bridge(_make_bridge("b1"))
        mgr.add_bridge(_make_bridge("b2"))
        assert len(mgr._bridges) == 2

    def test_remove_bridge(self):
        mgr = ProtocolBridgeManager()
        mgr.add_bridge(_make_bridge("b1"))
        mgr.remove_bridge("b1")
        assert "b1" not in mgr._bridges

    def test_remove_bridge_not_exist_silent(self):
        mgr = ProtocolBridgeManager()
        mgr.remove_bridge("unknown")

    def test_add_mapping_rule(self):
        mgr = ProtocolBridgeManager()
        mgr.add_bridge(_make_bridge("b1"))
        rule = _make_rule()
        mgr.add_mapping_rule("b1", rule)
        assert len(mgr._bridges["b1"].mapping_rules) == 1

    def test_add_mapping_rule_bridge_not_found(self):
        mgr = ProtocolBridgeManager()
        mgr.add_mapping_rule("unknown", _make_rule())
        # No bridge, rule not added
        assert len(mgr._bridges) == 0

    def test_remove_mapping_rule(self):
        mgr = ProtocolBridgeManager()
        bridge = _make_bridge(rules=[_make_rule("r1"), _make_rule("r2")])
        mgr.add_bridge(bridge)
        mgr.remove_mapping_rule("b1", "r1")
        assert len(mgr._bridges["b1"].mapping_rules) == 1
        assert mgr._bridges["b1"].mapping_rules[0].rule_id == "r2"

    def test_remove_mapping_rule_bridge_not_found(self):
        mgr = ProtocolBridgeManager()
        mgr.remove_mapping_rule("unknown", "r1")  # 不应抛异常

    def test_remove_mapping_rule_not_found(self):
        mgr = ProtocolBridgeManager()
        bridge = _make_bridge(rules=[_make_rule("r1")])
        mgr.add_bridge(bridge)
        mgr.remove_mapping_rule("b1", "nonexistent")
        assert len(mgr._bridges["b1"].mapping_rules) == 1


# ── ProtocolBridgeManager update_source_data ──


class TestBridgeManagerUpdateData:
    async def test_update_creates_source_data(self):
        mgr = ProtocolBridgeManager()
        await mgr.update_source_data("d1", "p1", 42)
        assert "d1" in mgr._source_data
        assert mgr._source_data["d1"]["p1"]["value"] == 42
        assert mgr._source_data["d1"]["p1"]["quality"] == "good"

    async def test_update_with_quality(self):
        mgr = ProtocolBridgeManager()
        await mgr.update_source_data("d1", "p1", 42, quality="bad")
        assert mgr._source_data["d1"]["p1"]["quality"] == "bad"

    async def test_update_triggers_bridge_processing(self):
        mgr = ProtocolBridgeManager()
        rule = _make_rule(source_device="d1", source_point="p1")
        bridge = _make_bridge(rules=[rule])
        mgr.add_bridge(bridge)
        cb = MagicMock()
        mgr.register_transform_callback(cb)
        await mgr.update_source_data("d1", "p1", 42)
        cb.assert_called_once()
        result = cb.call_args.args[0]
        assert result["source_device"] == "d1"
        assert result["source_point"] == "p1"
        assert result["converted_value"] == 42.0
        assert result["quality"] == "good"

    async def test_update_disabled_bridge_skipped(self):
        mgr = ProtocolBridgeManager()
        rule = _make_rule(source_device="d1", source_point="p1")
        bridge = _make_bridge(rules=[rule], enabled=False)
        mgr.add_bridge(bridge)
        cb = MagicMock()
        mgr.register_transform_callback(cb)
        await mgr.update_source_data("d1", "p1", 42)
        cb.assert_not_called()

    async def test_update_disabled_rule_skipped(self):
        mgr = ProtocolBridgeManager()
        rule = _make_rule(source_device="d1", source_point="p1", enabled=False)
        bridge = _make_bridge(rules=[rule])
        mgr.add_bridge(bridge)
        cb = MagicMock()
        mgr.register_transform_callback(cb)
        await mgr.update_source_data("d1", "p1", 42)
        cb.assert_not_called()

    async def test_update_non_matching_rule_skipped(self):
        mgr = ProtocolBridgeManager()
        rule = _make_rule(source_device="other", source_point="p1")
        bridge = _make_bridge(rules=[rule])
        mgr.add_bridge(bridge)
        cb = MagicMock()
        mgr.register_transform_callback(cb)
        await mgr.update_source_data("d1", "p1", 42)
        cb.assert_not_called()

    async def test_update_async_callback(self):
        mgr = ProtocolBridgeManager()
        rule = _make_rule(source_device="d1", source_point="p1")
        bridge = _make_bridge(rules=[rule])
        mgr.add_bridge(bridge)
        cb = AsyncMock()
        mgr.register_transform_callback(cb)
        await mgr.update_source_data("d1", "p1", 42)
        cb.assert_awaited_once()

    async def test_update_callback_exception_silent(self):
        mgr = ProtocolBridgeManager()
        rule = _make_rule(source_device="d1", source_point="p1")
        bridge = _make_bridge(rules=[rule])
        mgr.add_bridge(bridge)
        cb = MagicMock(side_effect=RuntimeError("cb err"))
        mgr.register_transform_callback(cb)
        await mgr.update_source_data("d1", "p1", 42)  # 不应抛异常

    async def test_update_processed_versions_tracked(self):
        mgr = ProtocolBridgeManager()
        await mgr.update_source_data("d1", "p1", 42)
        assert "d1:p1" in mgr._processed_versions

    async def test_update_multiple_bridges_triggered(self):
        mgr = ProtocolBridgeManager()
        rule = _make_rule(source_device="d1", source_point="p1")
        mgr.add_bridge(_make_bridge("b1", rules=[rule]))
        mgr.add_bridge(_make_bridge("b2", rules=[rule]))
        cb = MagicMock()
        mgr.register_transform_callback(cb)
        await mgr.update_source_data("d1", "p1", 42)
        # Both bridges should process
        assert cb.call_count == 2

    async def test_update_conversion_failure_quality_bad(self):
        mgr = ProtocolBridgeManager()
        rule = _make_rule(source_device="d1", source_point="p1", data_type="passthrough")
        bridge = _make_bridge(rules=[rule])
        mgr.add_bridge(bridge)
        results = []
        mgr.register_transform_callback(lambda r: results.append(r))
        # Pass invalid value to trigger conversion failure
        await mgr.update_source_data("d1", "p1", "not_a_number")
        assert len(results) == 1
        assert results[0]["converted_value"] is None
        assert results[0]["quality"] == "bad"


# ── ProtocolBridgeManager _process_bridge ──


class TestProcessBridge:
    async def test_process_bridge_calls_callbacks(self):
        mgr = ProtocolBridgeManager()
        rule = _make_rule(source_device="d1", source_point="p1", data_type="scale", scale=2.0)
        bridge = _make_bridge(rules=[rule])
        cb = AsyncMock()
        mgr.register_transform_callback(cb)
        await mgr._process_bridge(bridge, "d1", "p1", 10)
        cb.assert_awaited_once()
        result = cb.call_args.args[0]
        assert result["converted_value"] == 20.0

    async def test_process_bridge_no_matching_rules(self):
        mgr = ProtocolBridgeManager()
        rule = _make_rule(source_device="other", source_point="other_pt")
        bridge = _make_bridge(rules=[rule])
        cb = AsyncMock()
        mgr.register_transform_callback(cb)
        await mgr._process_bridge(bridge, "d1", "p1", 10)
        cb.assert_not_awaited()

    async def test_process_bridge_exception_silent(self):
        mgr = ProtocolBridgeManager()
        rule = _make_rule(source_device="d1", source_point="p1")
        bridge = _make_bridge(rules=[rule])
        # Make conversion raise by using a bad conversion type setup
        with patch.object(mgr._conversion, "convert", side_effect=RuntimeError("conv err")):
            await mgr._process_bridge(bridge, "d1", "p1", 10)  # 不应抛异常


# ── ProtocolBridgeManager _sync_loop ──


class TestSyncLoop:
    async def test_sync_loop_processes_data(self):
        mgr = ProtocolBridgeManager()
        rule = _make_rule(source_device="d1", source_point="p1")
        bridge = _make_bridge(rules=[rule])
        mgr.add_bridge(bridge)
        # Pre-populate source data with a specific timestamp
        ts = time.monotonic()
        mgr._source_data["d1"] = {"p1": {"value": 42, "quality": "good", "timestamp": ts}}
        # _processed_versions should NOT have this version, so it gets processed
        # But update_source_data sets _processed_versions, so we need to clear it
        mgr._processed_versions.clear()

        cb = MagicMock()
        mgr.register_transform_callback(cb)

        with _fast_terminate(mgr):
            await mgr.start()
            with contextlib.suppress(asyncio.CancelledError):
                await mgr._task

        # The sync loop should have processed the data
        assert cb.call_count >= 1

    async def test_sync_loop_skips_already_processed(self):
        mgr = ProtocolBridgeManager()
        rule = _make_rule(source_device="d1", source_point="p1")
        bridge = _make_bridge(rules=[rule])
        mgr.add_bridge(bridge)
        ts = time.monotonic()
        mgr._source_data["d1"] = {"p1": {"value": 42, "quality": "good", "timestamp": ts}}
        # Mark as already processed with same timestamp
        mgr._processed_versions["d1:p1"] = ts

        cb = MagicMock()
        mgr.register_transform_callback(cb)

        with _fast_terminate(mgr):
            await mgr.start()
            with contextlib.suppress(asyncio.CancelledError):
                await mgr._task

        # Should not have been processed (same version)
        cb.assert_not_called()

    async def test_sync_loop_skips_disabled_bridge(self):
        mgr = ProtocolBridgeManager()
        rule = _make_rule(source_device="d1", source_point="p1")
        bridge = _make_bridge(rules=[rule], enabled=False)
        mgr.add_bridge(bridge)
        mgr._source_data["d1"] = {"p1": {"value": 42, "quality": "good", "timestamp": time.monotonic()}}
        mgr._processed_versions.clear()

        cb = MagicMock()
        mgr.register_transform_callback(cb)

        with _fast_terminate(mgr):
            await mgr.start()
            with contextlib.suppress(asyncio.CancelledError):
                await mgr._task

        cb.assert_not_called()

    async def test_sync_loop_exception_silent(self):
        mgr = ProtocolBridgeManager()
        # Cause an exception in the sync loop by having a bridge with bad data
        mgr.add_bridge(_make_bridge(rules=[_make_rule()]))
        mgr._source_data = None  # Will cause AttributeError

        with _fast_terminate(mgr):
            await mgr.start()
            with contextlib.suppress(asyncio.CancelledError):
                await mgr._task
        # Should not raise, loop exits gracefully

    async def test_sync_loop_no_source_value_skips(self):
        """规则引用的源数据不存在时应跳过"""
        mgr = ProtocolBridgeManager()
        rule = _make_rule(source_device="d1", source_point="p1")
        bridge = _make_bridge(rules=[rule])
        mgr.add_bridge(bridge)
        # No source data for d1
        mgr._processed_versions.clear()

        cb = MagicMock()
        mgr.register_transform_callback(cb)

        with _fast_terminate(mgr):
            await mgr.start()
            with contextlib.suppress(asyncio.CancelledError):
                await mgr._task

        cb.assert_not_called()


# ── ProtocolBridgeManager 查询方法 ──


class TestBridgeManagerQueries:
    def test_get_bridges_empty(self):
        mgr = ProtocolBridgeManager()
        assert mgr.get_bridges() == []

    def test_get_bridges(self):
        mgr = ProtocolBridgeManager()
        mgr.add_bridge(_make_bridge("b1", rules=[_make_rule()]))
        mgr.add_bridge(_make_bridge("b2"))
        bridges = mgr.get_bridges()
        assert len(bridges) == 2
        b1 = next(b for b in bridges if b["bridge_id"] == "b1")
        assert b1["source_protocol"] == "modbus_tcp"
        assert b1["target_protocol"] == "opc_ua"
        assert b1["enabled"] is True
        assert b1["rules_count"] == 1

    def test_get_bridge_stats(self):
        mgr = ProtocolBridgeManager()
        r1 = _make_rule("r1", enabled=True)
        r2 = _make_rule("r2", enabled=False)
        mgr.add_bridge(_make_bridge("b1", rules=[r1, r2]))
        stats = mgr.get_bridge_stats("b1")
        assert stats["bridge_id"] == "b1"
        assert stats["rules_count"] == 2
        assert stats["enabled_rules"] == 1

    def test_get_bridge_stats_not_found(self):
        mgr = ProtocolBridgeManager()
        assert mgr.get_bridge_stats("unknown") is None

    def test_register_transform_callback(self):
        mgr = ProtocolBridgeManager()
        def cb1(r):
            return r

        def cb2(r):
            return r
        mgr.register_transform_callback(cb1)
        mgr.register_transform_callback(cb2)
        assert len(mgr._transform_callbacks) == 2


# ── ModbusToOpcUaConverter ──


class TestModbusToOpcUaConverterInit:
    def test_defaults(self):
        conv = ModbusToOpcUaConverter()
        assert conv._server_url == "opc.tcp://localhost:4840"
        assert conv._node_map == {}
        assert conv._modbus_data == {}

    def test_custom_url(self):
        conv = ModbusToOpcUaConverter("opc.tcp://192.168.1.1:4840")
        assert conv._server_url == "opc.tcp://192.168.1.1:4840"


class TestModbusToOpcUaAddMapping:
    async def test_add_mapping(self):
        conv = ModbusToOpcUaConverter()
        await conv.add_mapping("ns=2;s=Temp", 100, register_count=2, data_type="float32")
        assert "ns=2;s=Temp" in conv._node_map
        m = conv._node_map["ns=2;s=Temp"]
        assert m["address"] == 100
        assert m["register_count"] == 2
        assert m["data_type"] == "float32"
        assert m["swap_bytes"] is False

    async def test_add_mapping_with_swap(self):
        conv = ModbusToOpcUaConverter()
        await conv.add_mapping("n1", 200, swap_bytes=True)
        assert conv._node_map["n1"]["swap_bytes"] is True

    async def test_add_mapping_defaults(self):
        conv = ModbusToOpcUaConverter()
        await conv.add_mapping("n1", 100)
        m = conv._node_map["n1"]
        assert m["register_count"] == 1
        assert m["data_type"] == "uint16"
        assert m["swap_bytes"] is False


class TestModbusToOpcUaUpdateData:
    async def test_update_modbus_data(self):
        conv = ModbusToOpcUaConverter()
        await conv.update_modbus_data(100, [1, 2, 3])
        assert conv._modbus_data[100] == [1, 2, 3]

    async def test_update_overwrites(self):
        conv = ModbusToOpcUaConverter()
        await conv.update_modbus_data(100, [1])
        await conv.update_modbus_data(100, [2])
        assert conv._modbus_data[100] == [2]


class TestModbusToOpcUaReadNode:
    async def test_read_node_no_mapping_returns_none(self):
        conv = ModbusToOpcUaConverter()
        assert await conv.read_node("unknown") is None

    async def test_read_node_no_data_returns_none(self):
        conv = ModbusToOpcUaConverter()
        await conv.add_mapping("n1", 100)
        assert await conv.read_node("n1") is None

    async def test_read_uint16(self):
        conv = ModbusToOpcUaConverter()
        await conv.add_mapping("n1", 100, data_type="uint16")
        await conv.update_modbus_data(100, [42])
        assert await conv.read_node("n1") == 42

    async def test_read_int16_positive(self):
        conv = ModbusToOpcUaConverter()
        await conv.add_mapping("n1", 100, data_type="int16")
        await conv.update_modbus_data(100, [100])
        assert await conv.read_node("n1") == 100

    async def test_read_int16_negative(self):
        conv = ModbusToOpcUaConverter()
        await conv.add_mapping("n1", 100, data_type="int16")
        await conv.update_modbus_data(100, [65536 - 100])
        result = await conv.read_node("n1")
        assert result == -100

    async def test_read_float32(self):
        conv = ModbusToOpcUaConverter()
        await conv.add_mapping("n1", 100, register_count=2, data_type="float32")
        # Encode 3.14 as IEEE 754 float (big-endian)
        raw = struct.pack(">f", 3.14)
        high = struct.unpack(">H", raw[0:2])[0]
        low = struct.unpack(">H", raw[2:4])[0]
        await conv.update_modbus_data(100, [high, low])
        result = await conv.read_node("n1")
        assert result == pytest.approx(3.14, rel=1e-5)

    async def test_read_float32_swapped(self):
        conv = ModbusToOpcUaConverter()
        await conv.add_mapping("n1", 100, register_count=2, data_type="float32", swap_bytes=True)
        raw = struct.pack(">f", 2.5)
        high = struct.unpack(">H", raw[0:2])[0]
        low = struct.unpack(">H", raw[2:4])[0]
        # Store in swapped order
        await conv.update_modbus_data(100, [low, high])
        result = await conv.read_node("n1")
        assert result == pytest.approx(2.5, rel=1e-5)

    async def test_read_float32_insufficient_registers(self):
        conv = ModbusToOpcUaConverter()
        await conv.add_mapping("n1", 100, register_count=2, data_type="float32")
        await conv.update_modbus_data(100, [42])
        assert await conv.read_node("n1") == 0.0

    async def test_read_unknown_type_fallback(self):
        conv = ModbusToOpcUaConverter()
        await conv.add_mapping("n1", 100, data_type="unknown")
        await conv.update_modbus_data(100, [42])
        assert await conv.read_node("n1") == 42

    async def test_read_empty_registers_returns_zero(self):
        conv = ModbusToOpcUaConverter()
        await conv.add_mapping("n1", 100, data_type="uint16")
        await conv.update_modbus_data(100, [])
        # Empty registers list → not registers → None
        assert await conv.read_node("n1") is None


class TestModbusToOpcUaGetAllNodes:
    def test_empty(self):
        conv = ModbusToOpcUaConverter()
        assert conv.get_all_nodes() == {}

    def test_multiple_nodes(self):
        conv = ModbusToOpcUaConverter()
        conv._node_map = {
            "n1": {"address": 100, "register_count": 1, "data_type": "uint16", "swap_bytes": False},
            "n2": {"address": 200, "register_count": 1, "data_type": "int16", "swap_bytes": False},
        }
        conv._modbus_data = {
            100: [42],
            200: [65536 - 10],  # -10 as int16
        }
        result = conv.get_all_nodes()
        assert result["n1"] == 42
        assert result["n2"] == -10

    def test_node_no_data_returns_zero(self):
        conv = ModbusToOpcUaConverter()
        conv._node_map = {
            "n1": {"address": 100, "register_count": 1, "data_type": "uint16", "swap_bytes": False},
        }
        conv._modbus_data = {}
        result = conv.get_all_nodes()
        assert result["n1"] == 0

    def test_float32_node(self):
        conv = ModbusToOpcUaConverter()
        raw = struct.pack(">f", 1.5)
        high = struct.unpack(">H", raw[0:2])[0]
        low = struct.unpack(">H", raw[2:4])[0]
        conv._node_map = {
            "n1": {"address": 100, "register_count": 2, "data_type": "float32", "swap_bytes": False},
        }
        conv._modbus_data = {100: [high, low]}
        result = conv.get_all_nodes()
        assert result["n1"] == pytest.approx(1.5)

    def test_float32_insufficient_registers(self):
        conv = ModbusToOpcUaConverter()
        conv._node_map = {
            "n1": {"address": 100, "register_count": 2, "data_type": "float32", "swap_bytes": False},
        }
        conv._modbus_data = {100: [42]}
        result = conv.get_all_nodes()
        assert result["n1"] == 0.0

    def test_node_none_address(self):
        """mapping 中 address 为 None 时返回 None"""
        conv = ModbusToOpcUaConverter()
        conv._node_map = {
            "n1": {"address": None, "data_type": "uint16", "swap_bytes": False},
        }
        result = conv.get_all_nodes()
        assert result["n1"] is None


# ── get_bridge_manager 全局单例 ──


class TestGetBridgeManager:
    def test_returns_instance(self):
        mgr = get_bridge_manager()
        assert isinstance(mgr, ProtocolBridgeManager)

    def test_returns_same_instance(self):
        mgr1 = get_bridge_manager()
        mgr2 = get_bridge_manager()
        assert mgr1 is mgr2


# ── 端到端：start + update + stop ──


class TestEndToEnd:
    async def test_start_update_stop(self):
        mgr = ProtocolBridgeManager()
        rule = _make_rule(source_device="d1", source_point="p1", data_type="scale", scale=2.0, offset=1.0)
        mgr.add_bridge(_make_bridge("b1", rules=[rule]))

        results = []
        mgr.register_transform_callback(lambda r: results.append(r))

        with _fast_terminate(mgr):
            await mgr.start()
            await mgr.update_source_data("d1", "p1", 10)
        await mgr.stop()

        assert len(results) == 1
        assert results[0]["converted_value"] == 21.0
        assert results[0]["target_device"] == "tdev1"
        assert results[0]["target_point"] == "tpt1"

    async def test_multiple_rules_different_points(self):
        mgr = ProtocolBridgeManager()
        r1 = _make_rule("r1", source_device="d1", source_point="p1", target_point="tp1")
        r2 = _make_rule("r2", source_device="d1", source_point="p2", target_point="tp2")
        mgr.add_bridge(_make_bridge("b1", rules=[r1, r2]))

        results = []
        mgr.register_transform_callback(lambda r: results.append(r))

        with _fast_terminate(mgr):
            await mgr.start()
            await mgr.update_source_data("d1", "p1", 10)
            await mgr.update_source_data("d1", "p2", 20)
        await mgr.stop()

        assert len(results) == 2
        target_points = {r["target_point"] for r in results}
        assert target_points == {"tp1", "tp2"}
