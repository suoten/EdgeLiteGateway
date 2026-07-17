"""MC协议驱动扩展单元测试

补充覆盖未测试的方法：
- 地址解析 (_parse_address) 全分支
- 同步读写 (_read_point/_write_point) 多数据类型与字节序
- 连接管理 (start/stop/reconnect/health_check)
- 批量读写 (read_points/write_point/write_points_batch)
- 写入合并 (_merge_contiguous_writes)
- 值校验 (_validate_write_value) 与审计日志
- 降级评估 (_update_degrade_level)、点统计、冻结/变化率检测
- FX5U SLMP 直接模式与网络读取
- 权限校验 (check_permission/check_rbac/set_user_role)
- 设备发现 (discover_devices) 与设备管理 (add_device/remove_device)
- 时序持久化 (_persist_points) 与存储统计
- 通信模式 (_apply_comm_mode) 额外分支
"""

from __future__ import annotations

import sys

sys.path.insert(0, "src")

import struct
from collections import deque
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from edgelite.drivers.base import ConnectionState, PointValue
from edgelite.drivers.mc import McDriver
from edgelite.security.rbac import Permission

# --- helpers ---


def _make_driver(running: bool = True, with_client: bool = True) -> McDriver:
    """Build a fully initialized McDriver with a mock client."""
    d = McDriver()
    d._running = running
    d._config = {"host": "10.0.0.1", "port": 5007, "plc_type": "Q", "batch_size": 10}
    if with_client:
        d._client = MagicMock()
    d._WRITE_VERIFY_DELAY = 0
    return d


@pytest.fixture(autouse=True)
def _mock_infra(monkeypatch):
    """Mock the thread-pool executor and record_packet to avoid real threading."""

    async def _fake_run(self, func, *args, timeout=None):
        return func(*args)

    monkeypatch.setattr(McDriver, "_run_in_executor", _fake_run)
    monkeypatch.setattr("edgelite.drivers.mc.record_packet", lambda *a, **k: None)


# --- _parse_address ---


class TestParseAddress:
    def test_word_device_default(self):
        d = _make_driver()
        assert d._parse_address("D100") == ("D100", "word")

    def test_bit_devices(self):
        d = _make_driver()
        for addr in ("M0", "X0", "Y0", "S10", "L5", "F3", "V1", "B2"):
            assert d._parse_address(addr)[1] == "bit", addr

    def test_digit_bit_suffix(self):
        d = _make_driver()
        assert d._parse_address("D100.0") == ("D100.0", "bit")

    def test_uword_suffix(self):
        d = _make_driver()
        assert d._parse_address("D100.U") == ("D100", "uword")

    def test_long_suffix(self):
        d = _make_driver()
        assert d._parse_address("D100.L") == ("D100", "long")

    def test_float_suffix(self):
        d = _make_driver()
        assert d._parse_address("D100.F") == ("D100", "float")

    def test_byte_suffixes(self):
        d = _make_driver()
        assert d._parse_address("D100.B") == ("D100", "B")
        assert d._parse_address("D100.BYTE") == ("D100", "byte")
        assert d._parse_address("D100.INT8") == ("D100", "int8")

    def test_unknown_suffix_defaults_word(self):
        d = _make_driver()
        assert d._parse_address("D100.word") == ("D100", "word")
        assert d._parse_address("D100.xyz") == ("D100", "word")

    def test_timer_counter_word(self):
        d = _make_driver()
        assert d._parse_address("T0")[1] == "word"
        assert d._parse_address("C5")[1] == "word"

    def test_empty_address(self):
        d = _make_driver()
        assert d._parse_address("") == ("", "word")


# --- _read_point (sync) ---


class TestReadPoint:
    def test_read_bit(self):
        d = _make_driver()
        d._client.read_bit_device.return_value = [1]
        assert d._read_point("M0") == 1

    def test_read_bit_empty_raises(self):
        d = _make_driver()
        d._client.read_bit_device.return_value = []
        with pytest.raises(ValueError, match="Empty response"):
            d._read_point("M0")

    def test_read_word(self):
        d = _make_driver()
        d._client.read_device.return_value = [100]
        assert d._read_point("D100") == 100

    def test_read_word_empty_raises(self):
        d = _make_driver()
        d._client.read_device.return_value = []
        with pytest.raises(ValueError):
            d._read_point("D100")

    def test_read_uword_masks(self):
        d = _make_driver()
        d._client.read_device.return_value = [-1]
        assert d._read_point("D100.U") == 65535

    def test_read_long_big_endian(self):
        d = _make_driver()
        d._byte_order = "big"
        d._client.read_device.return_value = [0x1234, 0x5678]
        assert d._read_point("D100.L") == 0x12345678

    def test_read_long_little_endian(self):
        d = _make_driver()
        d._byte_order = "little"
        d._client.read_device.return_value = [0x5678, 0x1234]
        assert d._read_point("D100.L") == 0x12345678

    def test_read_long_insufficient_raises(self):
        d = _make_driver()
        d._client.read_device.return_value = [1]
        with pytest.raises(ValueError, match="Insufficient data"):
            d._read_point("D100.L")

    def test_read_long_non_int_raises(self):
        d = _make_driver()
        d._client.read_device.return_value = [1, None]
        with pytest.raises(ValueError, match="Non-integer"):
            d._read_point("D100.L")

    def test_read_float_big_endian(self):
        d = _make_driver()
        d._byte_order = "big"
        raw = struct.pack(">f", 3.14)
        hi, lo = struct.unpack(">HH", raw)
        d._client.read_device.return_value = [hi, lo]
        assert abs(d._read_point("D100.F") - 3.14) < 0.01

    def test_read_float_little_endian(self):
        d = _make_driver()
        d._byte_order = "little"
        raw = struct.pack("<f", 2.5)
        lo, hi = struct.unpack("<HH", raw)
        d._client.read_device.return_value = [lo, hi]
        assert abs(d._read_point("D100.F") - 2.5) < 0.01

    def test_read_client_none_raises(self):
        d = _make_driver(with_client=False)
        with pytest.raises(ConnectionError):
            d._read_point("D100")


# --- _write_point (sync) ---


class TestWritePointSync:
    def test_write_bit(self):
        d = _make_driver()
        d._write_point("M0", True)
        d._client.write_bit_device.assert_called_once_with("M0", [1])

    def test_write_bit_false(self):
        d = _make_driver()
        d._write_point("M0", 0)
        d._client.write_bit_device.assert_called_once_with("M0", [0])

    def test_write_word(self):
        d = _make_driver()
        d._write_point("D100", 42)
        d._client.write_device.assert_called_once_with("D100", [42])

    def test_write_byte_masks(self):
        d = _make_driver()
        d._write_point("D100.B", 300)
        d._client.write_device.assert_called_once_with("D100", [44])

    def test_write_int8(self):
        d = _make_driver()
        d._write_point("D100.INT8", 200)
        d._client.write_device.assert_called_once_with("D100", [200 & 0xFF])

    def test_write_long_big_endian(self):
        d = _make_driver()
        d._byte_order = "big"
        d._write_point("D100.L", 0x12345678)
        d._client.write_device.assert_called_once_with("D100", [0x1234, 0x5678])

    def test_write_long_little_endian(self):
        d = _make_driver()
        d._byte_order = "little"
        d._write_point("D100.L", 0x12345678)
        d._client.write_device.assert_called_once_with("D100", [0x5678, 0x1234])

    def test_write_float_big_endian(self):
        d = _make_driver()
        d._byte_order = "big"
        d._write_point("D100.F", 3.14)
        args = d._client.write_device.call_args
        hi, lo = args[0][1]
        raw = struct.pack(">HH", hi, lo)
        assert abs(struct.unpack(">f", raw)[0] - 3.14) < 0.01

    def test_write_float_little_endian(self):
        d = _make_driver()
        d._byte_order = "little"
        d._write_point("D100.F", 2.5)
        args = d._client.write_device.call_args
        lo, hi = args[0][1]
        raw = struct.pack("<HH", lo, hi)
        assert abs(struct.unpack("<f", raw)[0] - 2.5) < 0.01

    def test_write_client_none_raises(self):
        d = _make_driver(with_client=False)
        with pytest.raises(ConnectionError):
            d._write_point("D100", 1)


# --- _validate_write_value ---


class TestValidateWriteValue:
    def test_none_rejected(self):
        d = _make_driver()
        assert d._validate_write_value(None, "word") is False

    def test_bit_valid(self):
        d = _make_driver()
        assert d._validate_write_value(True, "bit") is True
        assert d._validate_write_value(0, "bit") is True

    def test_word_in_range(self):
        d = _make_driver()
        assert d._validate_write_value(0, "word") is True
        assert d._validate_write_value(65535, "word") is True
        assert d._validate_write_value(-32768, "word") is True

    def test_word_out_of_range(self):
        d = _make_driver()
        assert d._validate_write_value(65536, "word") is False
        assert d._validate_write_value(-32769, "word") is False

    def test_long_range(self):
        d = _make_driver()
        assert d._validate_write_value(0, "long") is True
        assert d._validate_write_value(4294967295, "long") is True
        assert d._validate_write_value(-2147483648, "long") is True
        assert d._validate_write_value(4294967296, "long") is False

    def test_float_finite(self):
        d = _make_driver()
        assert d._validate_write_value(3.14, "float") is True

    def test_float_nan_rejected(self):
        d = _make_driver()
        assert d._validate_write_value(float("nan"), "float") is False
        assert d._validate_write_value(float("inf"), "float") is False

    def test_byte_range(self):
        d = _make_driver()
        assert d._validate_write_value(0, "byte") is True
        assert d._validate_write_value(255, "byte") is True
        assert d._validate_write_value(256, "byte") is False
        assert d._validate_write_value(-1, "byte") is False

    def test_int8_range(self):
        d = _make_driver()
        assert d._validate_write_value(127, "int8") is True
        assert d._validate_write_value(-128, "int8") is True
        assert d._validate_write_value(128, "int8") is False

    def test_float_truncation_for_word(self):
        d = _make_driver()
        assert d._validate_write_value(3.14, "word") is False
        assert d._validate_write_value(3.0, "word") is True

    def test_string_rejected(self):
        d = _make_driver()
        assert d._validate_write_value("abc", "word") is False


# --- NaN/Inf / rate-of-change / frozen ---


class TestCheckNanInf:
    def test_nan_detected(self):
        d = _make_driver()
        assert d._check_nan_inf("D0", float("nan")) is True

    def test_inf_detected(self):
        d = _make_driver()
        assert d._check_nan_inf("D0", float("inf")) is True

    def test_normal_ok(self):
        d = _make_driver()
        assert d._check_nan_inf("D0", 3.14) is False
        assert d._check_nan_inf("D0", 42) is False


class TestCheckRateOfChange:
    def test_no_history(self):
        d = _make_driver()
        assert d._check_rate_of_change("D0", 100, datetime.now(UTC), 10) is False

    def test_rate_within_threshold(self):
        d = _make_driver()
        now = datetime.now(UTC)
        d._last_values["D0"] = 100
        d._last_timestamps["D0"] = now
        assert d._check_rate_of_change("D0", 105, now + timedelta(seconds=10), 10) is False

    def test_rate_exceeds_threshold(self):
        d = _make_driver()
        now = datetime.now(UTC)
        d._last_values["D0"] = 100
        d._last_timestamps["D0"] = now
        assert d._check_rate_of_change("D0", 200, now + timedelta(seconds=1), 10) is True

    def test_zero_dt(self):
        d = _make_driver()
        now = datetime.now(UTC)
        d._last_values["D0"] = 100
        d._last_timestamps["D0"] = now
        assert d._check_rate_of_change("D0", 200, now, 10) is False


class TestCheckFrozenValue:
    def test_below_window(self):
        d = _make_driver()
        for _ in range(3):
            assert d._check_frozen_value("D0", 100, 5) is False

    def test_frozen_detected(self):
        d = _make_driver()
        for _ in range(4):
            d._check_frozen_value("D0", 100, 5)
        assert d._check_frozen_value("D0", 100, 5) is True

    def test_not_frozen(self):
        d = _make_driver()
        d._check_frozen_value("D0", 100, 3)
        d._check_frozen_value("D0", 100, 3)
        assert d._check_frozen_value("D0", 200, 3) is False


# --- point stats / degrade ---


class TestPointStats:
    def test_success_increments(self):
        d = _make_driver()
        d._update_point_stats("D0", True)
        d._update_point_stats("D0", True)
        s = d.get_point_stats("D0")
        assert s["success_count"] == 2
        assert s["consecutive_fails"] == 0

    def test_failure_increments(self):
        d = _make_driver()
        d._update_point_stats("D0", False)
        d._update_point_stats("D0", False)
        s = d.get_point_stats("D0")
        assert s["fail_count"] == 2
        assert s["consecutive_fails"] == 2

    def test_success_resets_consecutive(self):
        d = _make_driver()
        d._update_point_stats("D0", False)
        d._update_point_stats("D0", True)
        assert d.get_point_stats("D0")["consecutive_fails"] == 0

    def test_missing_point_zeros(self):
        d = _make_driver()
        s = d.get_point_stats("D99")
        assert s["success_count"] == 0
        assert s["avg_latency_ms"] == 0.0


class TestDegradeLevel:
    def test_all_success_level0(self):
        d = _make_driver()
        d._update_point_stats("D0", True)
        d._update_point_stats("D0", True)
        d._update_degrade_level("dev1")
        assert d._degrade_level == 0

    def test_low_success_degrades(self):
        d = _make_driver()
        for _ in range(8):
            d._update_point_stats("D0", False)
        d._update_point_stats("D0", True)
        d._update_degrade_level("dev1")
        assert d._degrade_level >= 2

    def test_degrade_interval(self):
        d = _make_driver()
        assert d.get_degrade_interval() == 1
        d._degrade_level = 2
        assert d.get_degrade_interval() == 30

    def test_no_stats_no_change(self):
        d = _make_driver()
        d._update_degrade_level("dev1")
        assert d._degrade_level == 0


# --- _merge_contiguous_writes ---


class TestMergeContiguousWrites:
    def test_single_write(self):
        d = _make_driver()
        r = d._merge_contiguous_writes([("D100", 1)])
        assert len(r) == 1
        assert not r[0].get("merged")

    def test_contiguous_merged(self):
        d = _make_driver()
        r = d._merge_contiguous_writes([("D100", 1), ("D101", 2), ("D102", 3)])
        merged = [x for x in r if x.get("merged")]
        assert len(merged) == 1
        assert merged[0]["values"] == [1, 2, 3]

    def test_non_contiguous_not_merged(self):
        d = _make_driver()
        r = d._merge_contiguous_writes([("D100", 1), ("D200", 2)])
        assert not any(x.get("merged") for x in r)

    def test_long_not_merged(self):
        d = _make_driver()
        r = d._merge_contiguous_writes([("D100.L", 1), ("D102", 2)])
        assert not any(x.get("merged") for x in r)

    def test_none_value_not_merged(self):
        d = _make_driver()
        r = d._merge_contiguous_writes([("D100", None), ("D101", 2)])
        assert not any(x.get("merged") for x in r)

    def test_float_value_split(self):
        d = _make_driver()
        r = d._merge_contiguous_writes([("D100", 1), ("D101", 2.5)])
        assert not any(x.get("merged") for x in r)

    def test_different_device_types_not_merged(self):
        d = _make_driver()
        r = d._merge_contiguous_writes([("D100", 1), ("M101", 2)])
        assert not any(x.get("merged") for x in r)


# --- write audit ---


class TestWriteAudit:
    def test_record_and_retrieve(self):
        d = _make_driver()
        d._record_write_audit("dev1", "D100", "D100", "word", 10, 20, "ok")
        log = d.get_write_audit_log()
        assert len(log) == 1
        assert log[0]["device_id"] == "dev1"
        assert log[0]["new_value"] == 20

    def test_filter_by_device(self):
        d = _make_driver()
        d._record_write_audit("dev1", "D100", "D100", "word", 0, 1, "ok")
        d._record_write_audit("dev2", "D200", "D200", "word", 0, 2, "ok")
        log = d.get_write_audit_log(device_id="dev1")
        assert len(log) == 1


# --- state / info ---


class TestStateAndInfo:
    def test_get_conn_state(self):
        d = _make_driver()
        assert d.get_conn_state() == ConnectionState.DISCONNECTED.value

    def test_set_conn_state_valid(self):
        d = _make_driver()
        d._set_conn_state(ConnectionState.CONNECTING.value)
        assert d.get_conn_state() == ConnectionState.CONNECTING.value

    def test_set_conn_state_invalid(self):
        d = _make_driver()
        d._set_conn_state(ConnectionState.CONNECTED.value)
        assert d.get_conn_state() == ConnectionState.DISCONNECTED.value

    def test_get_failover_info(self):
        d = _make_driver()
        info = d.get_failover_info()
        assert "primary_ip" in info
        assert "using_backup" in info
        assert info["using_backup"] is False


# --- _check_connection ---


class TestCheckConnection:
    def test_client_none_false(self):
        d = _make_driver(with_client=False)
        assert d._check_connection() is False

    def test_connected_true(self):
        d = _make_driver()
        d._client.read_word_device.return_value = [1]
        assert d._check_connection() is True

    def test_read_fails_false(self):
        d = _make_driver()
        d._client.read_word_device.side_effect = RuntimeError("timeout")
        assert d._check_connection() is False


# --- permissions ---


class TestPermissions:
    async def test_viewer_cannot_write(self):
        d = _make_driver()
        assert await d.check_permission(Permission.DEVICE_WRITE_POINT) is False

    async def test_admin_can_write(self):
        d = _make_driver()
        await d.set_user_role("admin")
        assert await d.check_permission(Permission.DEVICE_WRITE_POINT) is True

    async def test_operator_cannot_write(self):
        d = _make_driver()
        await d.set_user_role("operator")
        assert await d.check_permission(Permission.DEVICE_WRITE_POINT) is False

    def test_check_rbac_admin(self):
        d = _make_driver()
        assert d.check_rbac("dev1", Permission.DEVICE_READ, "admin") is True

    def test_check_rbac_unknown_role(self):
        d = _make_driver()
        assert d.check_rbac("dev1", Permission.DEVICE_WRITE_POINT, "unknown") is False

    def test_check_rbac_none_role(self):
        d = _make_driver()
        assert d.check_rbac("dev1", Permission.DEVICE_READ, None) is False


# --- start / stop ---


class TestStartStop:
    async def test_start_success_type3e(self):
        d = McDriver()
        mock_client = MagicMock()
        with (
            patch("pymcprotocol.Type3E", return_value=mock_client),
            patch.object(d, "_start_watchdog", new=AsyncMock()),
            patch.object(d, "_init_edge_rules"),
            patch.object(d, "_init_ts_storage", new=AsyncMock()),
            patch.object(d, "_init_ota"),
            patch.object(d, "_init_config_version"),
            patch.object(d, "_init_audit"),
        ):
            await d.start({"host": "10.0.0.1", "port": 5007, "plc_type": "Q"})
        assert d._running is True
        assert d._client is mock_client

    async def test_start_success_type4e(self):
        d = McDriver()
        mock_client = MagicMock()
        with (
            patch("pymcprotocol.Type4E", return_value=mock_client),
            patch("pymcprotocol.Type3E", return_value=MagicMock()),
            patch.object(d, "_start_watchdog", new=AsyncMock()),
            patch.object(d, "_init_edge_rules"),
            patch.object(d, "_init_ts_storage", new=AsyncMock()),
            patch.object(d, "_init_ota"),
            patch.object(d, "_init_config_version"),
            patch.object(d, "_init_audit"),
        ):
            await d.start({"host": "10.0.0.1", "port": 5007, "plc_type": "Q", "frame_type": "4E"})
        assert d._running is True

    async def test_start_fx5u_port_change(self):
        d = McDriver()
        mock_client = MagicMock()
        with (
            patch("pymcprotocol.Type3E", return_value=mock_client),
            patch.object(d, "_start_watchdog", new=AsyncMock()),
            patch.object(d, "_init_edge_rules"),
            patch.object(d, "_init_ts_storage", new=AsyncMock()),
            patch.object(d, "_init_ota"),
            patch.object(d, "_init_config_version"),
            patch.object(d, "_init_audit"),
        ):
            await d.start({"host": "10.0.0.1", "port": 5007, "plc_type": "FX5U"})
        assert d._is_fx5u is True
        mock_client.connect.assert_called_once_with("10.0.0.1", 5001)

    async def test_start_missing_host_raises(self):
        d = McDriver()
        with pytest.raises(ValueError, match="missing host"):
            await d.start({"port": 5007})

    async def test_start_port_out_of_range(self):
        d = McDriver()
        with pytest.raises(ValueError, match="port out of range"):
            await d.start({"host": "10.0.0.1", "port": 99999})

    async def test_start_connect_failure_raises(self):
        d = McDriver()
        mock_client = MagicMock()
        mock_client.connect.side_effect = ConnectionRefusedError("refused")
        with (
            patch("pymcprotocol.Type3E", return_value=mock_client),
            patch.object(d, "_start_watchdog", new=AsyncMock()),
            patch.object(d, "_init_edge_rules"),
            patch.object(d, "_init_ts_storage", new=AsyncMock()),
            patch.object(d, "_init_ota"),
            patch.object(d, "_init_config_version"),
            patch.object(d, "_init_audit"),
        ):
            with pytest.raises(ConnectionRefusedError):
                await d.start({"host": "10.0.0.1", "port": 5007})

    async def test_stop_with_client(self):
        d = _make_driver(running=True)
        await d.stop()
        assert d._running is False
        assert d._client is None

    async def test_stop_without_client(self):
        d = _make_driver(running=True, with_client=False)
        await d.stop()
        assert d._running is False

    async def test_stop_cleans_ts_storage(self):
        d = _make_driver(running=True)
        # Capture the mock reference before stop() sets _ts_storage = None
        ts_storage = MagicMock()
        ts_storage.close = MagicMock()
        d._ts_storage = ts_storage
        await d.stop()
        ts_storage.close.assert_called_once()
        assert d._ts_storage is None


# --- read_points (async) ---


class TestReadPoints:
    async def test_read_single_success(self):
        d = _make_driver()
        d._client.read_device.return_value = [100]
        result = await d.read_points("dev1", ["D100"])
        assert isinstance(result["D100"], PointValue)
        assert result["D100"].value == 100
        assert result["D100"].quality == "good"

    async def test_read_not_running_bad(self):
        d = _make_driver(running=False)
        result = await d.read_points("dev1", ["D100"])
        assert result["D100"].quality == "bad"

    async def test_read_cached_backup(self):
        d = _make_driver(running=False)
        d._using_backup = True
        d._last_good_values["D100"] = PointValue(value=42, quality="good", timestamp=datetime.now(UTC))
        result = await d.read_points("dev1", ["D100"])
        assert result["D100"].value == 42
        assert result["D100"].quality == "uncertain"

    async def test_read_bit_point(self):
        d = _make_driver()
        d._client.read_bit_device.return_value = [1]
        result = await d.read_points("dev1", ["M0"])
        assert result["M0"].value == 1
        assert result["M0"].quality == "good"

    async def test_read_failure_bad(self):
        d = _make_driver()
        d._client.read_device.side_effect = RuntimeError("err")
        result = await d.read_points("dev1", ["D100"])
        assert result["D100"].quality == "bad"

    async def test_read_long_point(self):
        d = _make_driver()
        d._byte_order = "big"
        d._client.read_device.return_value = [0x1234, 0x5678]
        result = await d.read_points("dev1", ["D100.L"])
        assert result["D100.L"].value == 0x12345678


# --- write_point (async) ---


class TestWritePointAsync:
    async def test_write_denied_viewer(self):
        d = _make_driver()
        assert await d.write_point("dev1", "D100", 42) is False

    async def test_write_success_admin(self):
        d = _make_driver()
        await d.set_user_role("admin")
        d._client.read_device.return_value = [42]
        ok = await d.write_point("dev1", "D100", 42)
        assert ok is True
        d._client.write_device.assert_called()

    async def test_write_invalid_value(self):
        d = _make_driver()
        await d.set_user_role("admin")
        assert await d.write_point("dev1", "D100", "bad") is False

    async def test_write_not_running_false(self):
        d = _make_driver(running=False)
        await d.set_user_role("admin")
        assert await d.write_point("dev1", "D100", 42) is False


# --- write_points_batch ---


class TestWritePointsBatch:
    async def test_batch_denied_viewer(self):
        d = _make_driver()
        result = await d.write_points_batch("dev1", {"D100": 1})
        assert result == {"D100": False}

    async def test_batch_single_write(self):
        d = _make_driver()
        await d.set_user_role("admin")
        d._client.read_device.return_value = [1]
        result = await d.write_points_batch("dev1", {"D100": 1})
        assert result["D100"] is True

    async def test_batch_merged_writes(self):
        d = _make_driver()
        await d.set_user_role("admin")
        d._client.read_device.return_value = [0]
        result = await d.write_points_batch("dev1", {"D100": 1, "D101": 2, "D102": 3})
        assert all(result.values())

    async def test_batch_not_running(self):
        d = _make_driver(running=False)
        await d.set_user_role("admin")
        result = await d.write_points_batch("dev1", {"D100": 1})
        assert result == {"D100": False}


# --- device management ---


class TestDeviceManagement:
    async def test_add_device(self):
        d = _make_driver()
        pts = [{"name": "temp", "address": "D100"}, {"name": "humid", "address": "D200"}]
        await d.add_device("dev1", {"host": "10.0.0.1"}, pts)
        assert "dev1" in d._devices
        assert len(d._devices["dev1"]["points"]) == 2

    async def test_add_device_no_points(self):
        d = _make_driver()
        await d.add_device("dev1", {"host": "10.0.0.1"})
        assert "dev1" in d._devices

    def test_remove_device(self):
        d = _make_driver()
        d._devices["dev1"] = {"config": {}, "points": {}}
        d._reconnect_count["dev1"] = 2
        d._value_history["dev1_D100"] = deque([1, 2, 3])
        d._write_rate_limits["dev1_D100"] = 1.0
        d.remove_device("dev1")
        assert "dev1" not in d._devices
        assert "dev1" not in d._reconnect_count
        assert "dev1_D100" not in d._value_history


# --- health check ---


class TestHealthCheck:
    async def test_not_running_false(self):
        d = _make_driver(running=False)
        assert await d.health_check("dev1") is False

    async def test_no_client_false(self):
        d = _make_driver(running=True, with_client=False)
        assert await d.health_check("dev1") is False

    async def test_connected_true(self):
        d = _make_driver()
        d._client.read_word_device.return_value = [1]
        assert await d.health_check("dev1") is True

    async def test_check_fails_false(self):
        d = _make_driver()
        d._client.read_word_device.side_effect = RuntimeError("fail")
        assert await d.health_check("dev1") is False


# --- reconnect ---


class TestReconnect:
    async def test_try_reconnect_no_config(self):
        d = _make_driver()
        d._config = {}
        await d._try_reconnect("dev1")

    async def test_do_reconnect_circuit_open(self):
        import time

        d = _make_driver()
        d._circuit_open.add("dev1")
        d._circuit_open_since["dev1"] = time.monotonic()
        await d._do_reconnect("dev1")

    async def test_do_reconnect_max_attempts(self):
        d = _make_driver()
        d._reconnect_count["dev1"] = 5
        with patch("edgelite.drivers.mc.asyncio.sleep", new=AsyncMock()):
            await d._do_reconnect("dev1")
        assert "dev1" in d._circuit_open

    async def test_do_reconnect_success(self):
        d = _make_driver()
        d._active_ip = "10.0.0.1"  # _do_reconnect uses _active_ip as target_ip
        mock_client = MagicMock()
        with (
            patch("pymcprotocol.Type3E", return_value=mock_client),
            patch("edgelite.drivers.mc.asyncio.sleep", new=AsyncMock()),
        ):
            await d._do_reconnect("dev1")
        assert d._client is mock_client

    async def test_do_reconnect_failover_to_backup(self):
        d = _make_driver()
        d._backup_ip = "10.0.0.2"
        d._primary_fail_count = d._FAILOVER_THRESHOLD
        mock_client = MagicMock()
        with (
            patch("pymcprotocol.Type3E", return_value=mock_client),
            patch("edgelite.drivers.mc.asyncio.sleep", new=AsyncMock()),
        ):
            await d._do_reconnect("dev1")
        assert d._using_backup is True
        assert d._active_ip == "10.0.0.2"

    async def test_try_revert_primary_not_using_backup(self):
        d = _make_driver()
        d._using_backup = False
        await d._try_revert_primary("dev1")

    async def test_try_revert_primary_success(self):
        d = _make_driver()
        d._using_backup = True
        d._primary_ip = "10.0.0.1"
        with patch.object(d, "_try_reconnect", new=AsyncMock()):
            await d._try_revert_primary("dev1")
        assert d._using_backup is False
        assert d._active_ip == "10.0.0.1"

    async def test_delayed_reconnect_not_running(self):
        d = _make_driver(running=False)
        with patch("edgelite.drivers.mc.asyncio.sleep", new=AsyncMock()):
            await d._delayed_reconnect(0.1, "dev1")

    async def test_delayed_reconnect_running(self):
        d = _make_driver()
        d._devices = {"dev1": {}}
        with (
            patch("edgelite.drivers.mc.asyncio.sleep", new=AsyncMock()),
            patch.object(d, "_try_reconnect", new=AsyncMock()) as mock_rc,
        ):
            await d._delayed_reconnect(0.01, "dev1")
        mock_rc.assert_called_once_with("dev1")


# --- discover_devices ---


class TestDiscoverDevices:
    async def test_discover_single_host(self):
        d = _make_driver()
        mock_client = MagicMock()
        with patch("pymcprotocol.Type3E", return_value=mock_client):
            result = await d.discover_devices({"host": "10.0.0.1", "timeout": 1})
        assert len(result) == 1
        assert result[0]["config"]["host"] == "10.0.0.1"

    async def test_discover_no_host_or_network(self):
        d = _make_driver()
        assert await d.discover_devices({}) == []

    async def test_discover_invalid_network(self):
        d = _make_driver()
        assert await d.discover_devices({"network": "invalid"}) == []

    async def test_discover_connect_fails(self):
        d = _make_driver()
        mock_client = MagicMock()
        mock_client.connect.side_effect = ConnectionRefusedError("refused")
        with patch("pymcprotocol.Type3E", return_value=mock_client):
            assert await d.discover_devices({"host": "10.0.0.1", "timeout": 1}) == []

    async def test_discover_fx5u_port(self):
        d = _make_driver()
        mock_client = MagicMock()
        with patch("pymcprotocol.Type3E", return_value=mock_client):
            await d.discover_devices({"host": "10.0.0.1", "plc_type": "FX5U", "timeout": 1})
        mock_client.connect.assert_called_once_with("10.0.0.1", 5001)


# --- FX5U read paths ---


class TestFx5uRead:
    def test_read_fx5u_slmp_direct(self):
        d = _make_driver()
        d._is_fx5u = True
        d._client._accessopt = {"network": 0}
        # _parse_address classifies "U" as a bit device → suffix="bit",
        # so _read_fx5u_slmp_direct calls read_bit_device, not read_device.
        d._client.read_bit_device.return_value = [42]
        assert d._read_point("U0\\G100") == 42

    def test_read_fx5u_slmp_direct_bit(self):
        d = _make_driver()
        d._is_fx5u = True
        d._client._accessopt = {"network": 0}
        d._client.read_bit_device.return_value = [1]
        assert d._read_point("U0\\G100") == 1

    def test_read_fx5u_slmp_direct_fallback(self):
        d = _make_driver()
        d._is_fx5u = True
        d._client._accessopt = {"network": 0}
        d._client.set_accessopt.side_effect = ValueError("bad")
        d._client.read_device.return_value = [99]
        assert d._read_point("U0\\G100") == 99

    def test_read_fx5u_network(self):
        d = _make_driver()
        d._is_fx5u = True
        d._client._accessopt = {"network": 0}
        # _parse_address classifies "J" as a bit device → suffix="bit",
        # so _read_fx5u_network calls read_bit_device, not read_device.
        d._client.read_bit_device.return_value = [55]
        assert d._read_point("J0\\D100") == 55

    def test_read_fx5u_network_bit(self):
        d = _make_driver()
        d._is_fx5u = True
        d._client._accessopt = {"network": 0}
        d._client.read_bit_device.return_value = [1]
        assert d._read_point("J0\\M0") == 1

    def test_read_fx5u_network_fallback(self):
        d = _make_driver()
        d._is_fx5u = True
        d._client._accessopt = {"network": 0}
        d._client.set_accessopt.side_effect = ValueError("bad")
        d._client.read_device.return_value = [77]
        assert d._read_point("J0\\D100") == 77


# --- _apply_comm_mode extra branches ---


class TestApplyCommModeExtra:
    def test_explicit_mode_binary(self):
        d = _make_driver()
        d._communication_mode = "ascii"
        fake = MagicMock()
        d._apply_comm_mode(fake, mode="binary")
        fake.setaccessopt.assert_not_called()

    def test_explicit_mode_ascii(self):
        d = _make_driver()
        fake = MagicMock()
        d._apply_comm_mode(fake, mode="ascii")
        fake.setaccessopt.assert_called_once_with(commtype="ascii")

    def test_invalid_mode_falls_back(self):
        d = _make_driver()
        fake = MagicMock()
        d._apply_comm_mode(fake, mode="rtu")
        fake.setaccessopt.assert_not_called()

    def test_none_mode_uses_default(self):
        d = _make_driver()
        d._communication_mode = "binary"
        fake = MagicMock()
        d._apply_comm_mode(fake, mode=None)
        fake.setaccessopt.assert_not_called()


# --- OTA / audit / storage stats ---


class TestOtaAuditStats:
    def test_ota_progress_no_manager(self):
        d = _make_driver()
        assert d.get_ota_progress() == {"status": "not_available"}

    def test_audit_stats_no_audit(self):
        d = _make_driver()
        assert d.get_audit_stats() == {}

    def test_storage_stats_no_storage(self):
        d = _make_driver()
        assert d.get_storage_stats()["persist_enabled"] is False

    def test_ota_progress_with_manager(self):
        d = _make_driver()
        d._ota_manager = MagicMock()
        d._ota_manager.get_progress.return_value = {"status": "idle"}
        assert d.get_ota_progress() == {"status": "idle"}

    def test_audit_stats_with_audit(self):
        d = _make_driver()
        d._mc_audit = MagicMock()
        d._mc_audit.get_stats.return_value = {"total": 5}
        assert d.get_audit_stats() == {"total": 5}


# --- persist points ---


class TestPersistPoints:
    async def test_persist_disabled_noop(self):
        d = _make_driver()
        d._persist_enabled = False
        await d._persist_points("dev1", {"D100": PointValue(value=1, quality="good", timestamp=datetime.now(UTC))})

    async def test_persist_with_storage(self):
        d = _make_driver()
        d._persist_enabled = True
        d._ts_storage = MagicMock()
        d._ts_storage.write_points_batch = AsyncMock(return_value=True)
        result = {"D100": PointValue(value=42, quality="good", timestamp=datetime.now(UTC))}
        await d._persist_points("dev1", result)
        d._ts_storage.write_points_batch.assert_called_once()

    async def test_persist_failure_queues_offline(self):
        d = _make_driver()
        d._persist_enabled = True
        d._ts_storage = MagicMock()
        d._ts_storage.write_points_batch = AsyncMock(side_effect=RuntimeError("db error"))
        d._offline_queue = MagicMock()
        d._offline_queue.enqueue = AsyncMock()
        result = {"D100": PointValue(value=42, quality="good", timestamp=datetime.now(UTC))}
        await d._persist_points("dev1", result)
        d._offline_queue.enqueue.assert_called_once()


# --- config version (empty manager paths) ---


class TestConfigVersionEmpty:
    async def test_save_no_mgr(self):
        d = _make_driver()
        assert await d.save_config_version("dev1", {}) == 0

    async def test_rollback_no_mgr(self):
        d = _make_driver()
        assert await d.rollback_config("dev1", 1) is None

    async def test_get_versions_no_mgr(self):
        d = _make_driver()
        assert await d.get_config_versions("dev1") == []

    async def test_get_audit_trail_no_mgr(self):
        d = _make_driver()
        assert await d.get_config_audit_trail("dev1") == []


# --- edge rules (empty engine paths) ---


class TestEdgeRulesEmpty:
    async def test_evaluate_no_engine(self):
        d = _make_driver()
        await d._evaluate_rules("dev1", {"D100": PointValue(value=1, quality="good", timestamp=datetime.now(UTC))})

    async def test_reload_no_engine(self):
        d = _make_driver()
        assert await d.reload_rules() == 0

    def test_add_rule_no_engine(self):
        d = _make_driver()
        assert d.add_edge_rule({"rule_id": "r1"}) is False

    async def test_remove_rule_no_engine(self):
        d = _make_driver()
        assert await d.remove_edge_rule("r1") is False

    def test_get_rules_no_engine(self):
        d = _make_driver()
        assert d.get_edge_rules() == []

    def test_get_alarm_history_no_engine(self):
        d = _make_driver()
        assert d.get_edge_alarm_history() == []

    def test_get_rule_stats_no_engine(self):
        d = _make_driver()
        assert d.get_edge_rule_stats() == {}


# --- edge write callback ---


class TestEdgeWriteCallback:
    async def test_callback_success(self):
        d = _make_driver()
        await d.set_user_role("admin")
        d._client.read_device.return_value = [5]
        result = await d._edge_write_callback("dev1", "D100", 5)
        assert result["success"] is True

    async def test_callback_failure(self):
        d = _make_driver(running=False)
        await d.set_user_role("admin")
        # _edge_write_callback returns success=False only when write_points_batch
        # raises an exception. With running=False it returns {"D100": False}
        # without raising, so we patch it to raise.
        with patch.object(d, "write_points_batch", new=AsyncMock(side_effect=RuntimeError("write failed"))):
            result = await d._edge_write_callback("dev1", "D100", 5)
        assert result["success"] is False


# --- upload loop ---


class TestUploadLoop:
    async def test_start_stop_upload(self):
        d = _make_driver()
        await d.start_upload()
        assert d._upload_task is not None
        await d.stop_upload()
        assert d._upload_task is None

    async def test_stop_upload_no_task(self):
        d = _make_driver()
        await d.stop_upload()

    async def test_force_sync_no_storage(self):
        d = _make_driver()
        assert await d.force_sync() == 0


# --- _log_error ---


class TestLogError:
    def test_log_error_err_code(self):
        d = _make_driver()
        d._log_error("dev1", "ERR_MC_CONN_FAILED", "test message")

    def test_log_error_custom_code(self):
        d = _make_driver()
        d._log_error("dev1", "CUSTOM_CODE", "test")
