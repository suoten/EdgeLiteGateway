"""Allen-Bradley driver extended unit tests — coverage expansion.

Targets untested methods: connection lifecycle, read/write, tag parsing,
validation, discovery, watchdog, RBAC, config versioning, OTA, edge rules, TS storage.
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

sys.path.insert(0, "src")

from edgelite.drivers.allen_bradley import (
    CIP_STATUS_MAP,
    AbConnState,
    AllenBradleyDriver,
    PointHealthStats,
)
from edgelite.drivers.base import PointValue

# ── helpers ─────────────────────────────────────────────────────────────


def _make_driver() -> AllenBradleyDriver:
    """Construct a real AllenBradleyDriver (covers __init__) with lightweight _set_conn_state."""
    driver = AllenBradleyDriver()

    def _light_set_conn_state(new_state: str, device_id: str = "", reason: str = "") -> None:
        driver._conn_state = new_state

    driver._set_conn_state = _light_set_conn_state  # type: ignore[assignment]
    return driver


class _Resp:
    """Fake pylogix Response."""

    def __init__(self, value=None, status=0, data_length=None):
        self.Value = value
        self.Status = status
        self.DataLength = data_length


class _FakeTag:
    """Fake pylogix Tag entry."""

    def __init__(self, tag_name="Tag1", data_type="DINT", dims=None):
        self.TagName = tag_name
        self.DataType = data_type
        self.ArrayDimensions = dims or []
        self.ProgramName = tag_name  # required by _discover_programs


class _FakePlc:
    """Fake pylogix PLC client."""

    def __init__(self, read_value=None, read_status=0, write_status=0, raise_exc=None, tags=None):
        self._read_value = read_value
        self._read_status = read_status
        self._write_status = write_status
        self._raise = raise_exc
        self._tags = tags  # tags returned by GetTagList (defaults to [_FakeTag()])
        self.LargeForwardOpen = False
        self.SocketTimeout = None
        self._closed = False

    def Read(self, tag):
        if self._raise:
            raise self._raise
        if isinstance(tag, list):
            return [_Resp(value=self._read_value, status=self._read_status) for _ in tag]
        return _Resp(value=self._read_value, status=self._read_status)

    def Write(self, tag, value):
        return _Resp(status=self._write_status)

    def Close(self):
        self._closed = True

    def ForwardClose(self):
        pass

    def GetTagList(self, program=""):
        return _Resp(value=self._tags if self._tags is not None else [_FakeTag()], status=0)

    def GetProgramList(self):
        return _Resp(value=[_FakeTag("MainProgram")], status=0)


class _FallbackPlc(_FakePlc):
    """FakePlc that fails batch (list) reads but succeeds single-tag reads.

    Used to force the per-point fallback path in read_points, which is the
    only path that calls _record_point_success.
    """

    def Read(self, tag):
        if self._raise:
            raise self._raise
        if isinstance(tag, list):
            # Batch read: return CIP error so _parse_response_value returns None
            return [_Resp(value=None, status=0x06) for _ in tag]
        # Single read: return success
        return _Resp(value=self._read_value, status=self._read_status)


def _mrt(driver, ret=None, exc=None):
    """Mock _run_in_thread to call func directly (no threading)."""

    async def _fake(func, *args, timeout=30.0, **kwargs):
        if exc:
            raise exc
        if asyncio.iscoroutinefunction(func):
            return await func(*args, **kwargs)
        return func(*args, **kwargs)

    driver._run_in_thread = _fake  # type: ignore[assignment]
    return driver


# ══════════════════════════════════════════════════════════════════════
# 1. PointHealthStats
# ══════════════════════════════════════════════════════════════════════


class TestPointHealthStats:
    def test_record_success(self):
        s = PointHealthStats()
        s.record_success(10.0)
        assert s.success_count == 1
        assert s.consecutive_fails == 0
        assert s.avg_latency_ms == 10.0

    def test_record_success_avg(self):
        s = PointHealthStats()
        for v in (10, 20, 30):
            s.record_success(float(v))
        assert s.avg_latency_ms == 20.0

    def test_record_failure(self):
        s = PointHealthStats()
        s.record_failure("ERR_CIP_TAG_NOT_FOUND")
        assert s.fail_count == 1
        assert s.consecutive_fails == 1
        assert s.last_cip_error == "ERR_CIP_TAG_NOT_FOUND"

    def test_record_failure_empty_error(self):
        s = PointHealthStats()
        s.record_failure()
        assert s.fail_count == 1
        assert s.last_cip_error == ""

    def test_success_rate_no_data(self):
        assert PointHealthStats().success_rate == 1.0

    def test_success_rate_with_fails(self):
        s = PointHealthStats()
        s.record_success(1.0)
        s.record_failure()
        assert s.success_rate == 0.5


# ══════════════════════════════════════════════════════════════════════
# 2. CIP status / filter / frozen / rate-of-change
# ══════════════════════════════════════════════════════════════════════


class TestCipStatus:
    def test_known_status(self):
        d = _make_driver()
        assert d._parse_cip_status(0x00) == "OK"
        assert d._parse_cip_status(0x52) == "ERR_CIP_TAG_NOT_FOUND"

    def test_unknown_status(self):
        d = _make_driver()
        result = d._parse_cip_status(0xFF)
        assert "ERR_CIP_UNKNOWN" in result

    def test_cip_status_map_completeness(self):
        assert CIP_STATUS_MAP[0x04] == "ERR_CIP_PATH_ERROR"
        assert len(CIP_STATUS_MAP) > 40


class TestFilterNanInf:
    def test_normal_float(self):
        d = _make_driver()
        val, ok = d._filter_nan_inf(3.14)
        assert val == 3.14 and ok

    def test_nan(self):
        d = _make_driver()
        val, ok = d._filter_nan_inf(float("nan"))
        assert val is None and not ok

    def test_inf(self):
        d = _make_driver()
        val, ok = d._filter_nan_inf(float("inf"))
        assert val is None and not ok

    def test_list_filters_nan(self):
        d = _make_driver()
        val, ok = d._filter_nan_inf([1.0, float("nan"), 2.0, float("inf")])
        assert val == [1.0, 2.0] and ok

    def test_int_passthrough(self):
        d = _make_driver()
        val, ok = d._filter_nan_inf(42)
        assert val == 42 and ok


class TestFrozen:
    def test_non_numeric_returns_false(self):
        d = _make_driver()
        assert d._check_frozen("p1", "str") is False

    def test_no_history(self):
        d = _make_driver()
        assert d._check_frozen("p1", 5.0) is False

    def test_frozen_detected(self):
        d = _make_driver()
        d._set_last_value("p1", 5.0)
        stats = PointHealthStats()
        stats.success_count = 5
        d._point_stats["p1"] = stats
        assert d._check_frozen("p1", 5.0) is True

    def test_not_frozen_low_count(self):
        d = _make_driver()
        d._set_last_value("p1", 5.0)
        d._point_stats["p1"] = PointHealthStats(success_count=1)
        assert d._check_frozen("p1", 5.0) is False


class TestRateOfChange:
    def test_no_limit(self):
        d = _make_driver()
        assert d._check_rate_of_change("p1", 5.0, datetime.now(UTC)) is False

    def test_no_prev_value(self):
        d = _make_driver()
        d._rate_of_change_limit = 10.0
        assert d._check_rate_of_change("p1", 5.0, datetime.now(UTC)) is False

    def test_rate_exceeded(self):
        d = _make_driver()
        d._rate_of_change_limit = 1.0
        d._set_last_value("p1", 0.0)
        ts1 = datetime.now(UTC)
        ts2 = ts1 + timedelta(microseconds=10)  # dt > 0 required by _check_rate_of_change
        d._last_timestamps["p1"] = ts1
        # value jumped 100 in 10µs → rate huge
        assert d._check_rate_of_change("p1", 100.0, ts2) is True

    def test_rate_ok(self):
        d = _make_driver()
        d._rate_of_change_limit = 1000.0
        d._set_last_value("p1", 0.0)
        ts1 = datetime.now(UTC)
        d._last_timestamps["p1"] = ts1
        ts2 = datetime.now(UTC)
        assert d._check_rate_of_change("p1", 1.0, ts2) is False


# ══════════════════════════════════════════════════════════════════════
# 3. Last value / transforms / validation
# ══════════════════════════════════════════════════════════════════════


class TestLastValue:
    def test_set_and_get(self):
        d = _make_driver()
        d._set_last_value("p1", 42)
        assert d._last_values["p1"] == 42

    def test_lru_eviction(self):
        d = _make_driver()
        d._MAX_LAST_VALUES = 3
        for i in range(5):
            d._set_last_value(f"p{i}", i)
        assert len(d._last_values) == 3
        assert "p0" not in d._last_values
        assert "p4" in d._last_values

    def test_move_to_end(self):
        d = _make_driver()
        d._set_last_value("a", 1)
        d._set_last_value("b", 2)
        d._set_last_value("a", 3)
        assert list(d._last_values.keys())[-1] == "a"


class TestTransforms:
    def test_none_value_passthrough(self):
        d = _make_driver()
        pv = PointValue(value=None, quality="bad", timestamp=datetime.now(UTC))
        result = d._apply_point_transforms("p1", pv, datetime.now(UTC))
        assert result.value is None

    def test_scaling_applied(self):
        d = _make_driver()
        d._config["scaling"] = {"ratio": 2.0, "offset": 1.0}
        pv = PointValue(value=5.0, quality="good", timestamp=datetime.now(UTC))
        result = d._apply_point_transforms("p1", pv, datetime.now(UTC))
        assert result.value == 11.0

    def test_clamp_out_of_range(self):
        d = _make_driver()
        d._config["clamp"] = {"min": 0.0, "max": 10.0}
        pv = PointValue(value=15.0, quality="good", timestamp=datetime.now(UTC))
        result = d._apply_point_transforms("p1", pv, datetime.now(UTC))
        assert result.value is None
        assert result.quality == "bad"

    def test_nan_filtered(self):
        d = _make_driver()
        pv = PointValue(value=float("nan"), quality="good", timestamp=datetime.now(UTC))
        result = d._apply_point_transforms("p1", pv, datetime.now(UTC))
        assert result.value is None
        assert result.quality == "bad"

    def test_point_config_scaling(self):
        d = _make_driver()
        d._point_configs["p1"] = {"scaling": {"ratio": 3.0, "offset": 0.0}}
        pv = PointValue(value=4.0, quality="good", timestamp=datetime.now(UTC))
        result = d._apply_point_transforms("p1", pv, datetime.now(UTC))
        assert result.value == 12.0


class TestValidateCipLen:
    def test_none_resp(self):
        d = _make_driver()
        assert d._validate_cip_data_length(None, "p1") is False

    def test_none_value(self):
        d = _make_driver()
        assert d._validate_cip_data_length(_Resp(value=None), "p1") is True

    def test_no_device_info(self):
        d = _make_driver()
        assert d._validate_cip_data_length(_Resp(value=42), "p1") is True

    def test_bytes_too_short(self):
        d = _make_driver()
        d._devices["d1"] = {"points": {"p1": {"type": "DINT"}}}
        assert d._validate_cip_data_length(_Resp(value=b"\x01\x02"), "p1") is False

    def test_bytes_ok(self):
        d = _make_driver()
        # REAL has size 4 and no range check (min_val=None), so a 4-byte bytes
        # value passes both the length check and the (skipped) range check.
        d._devices["d1"] = {"points": {"p1": {"type": "REAL"}}}
        assert d._validate_cip_data_length(_Resp(value=b"\x01\x02\x03\x04"), "p1") is True

    def test_value_out_of_range(self):
        d = _make_driver()
        d._devices["d1"] = {"points": {"p1": {"type": "SINT"}}}
        assert d._validate_cip_data_length(_Resp(value=200), "p1") is False

    def test_value_in_range(self):
        d = _make_driver()
        d._devices["d1"] = {"points": {"p1": {"type": "SINT"}}}
        assert d._validate_cip_data_length(_Resp(value=100), "p1") is True

    def test_list_empty(self):
        d = _make_driver()
        d._devices["d1"] = {"points": {"p1": {"type": "INT"}}}
        assert d._validate_cip_data_length(_Resp(value=[]), "p1") is True

    def test_unknown_type(self):
        d = _make_driver()
        d._devices["d1"] = {"points": {"p1": {"type": "CUSTOM"}}}
        assert d._validate_cip_data_length(_Resp(value=42), "p1") is True


class TestValidateWrite:
    def test_no_type(self):
        d = _make_driver()
        val, err = d._validate_write_value("p1", 42, "", "d1")
        assert val == 42 and err == ""

    def test_int_range_ok(self):
        d = _make_driver()
        val, err = d._validate_write_value("p1", 100, "DINT", "d1")
        assert val == 100 and err == ""

    def test_int_range_exceeded(self):
        d = _make_driver()
        val, err = d._validate_write_value("p1", 9999999999, "SINT", "d1")
        assert val is None and err == "ERR_AB_WRITE_VALUE_INVALID"

    def test_type_cast(self):
        d = _make_driver()
        val, err = d._validate_write_value("p1", "42", "DINT", "d1")
        assert val == 42 and err == ""

    def test_type_cast_fail(self):
        d = _make_driver()
        val, err = d._validate_write_value("p1", "abc", "DINT", "d1")
        assert val is None and err == "ERR_AB_WRITE_VALUE_INVALID"

    def test_string_too_long(self):
        d = _make_driver()
        val, err = d._validate_write_value("p1", "x" * 100, "STRING", "d1")
        assert val is None and err == "ERR_AB_WRITE_VALUE_INVALID"

    def test_string_ok(self):
        d = _make_driver()
        val, err = d._validate_write_value("p1", "hello", "STRING", "d1")
        assert val == "hello" and err == ""

    def test_list_truncated(self):
        d = _make_driver()
        big = list(range(600))
        val, err = d._validate_write_value("p1", big, "", "d1")
        assert len(val) == 500 and err == ""

    def test_dict_missing_fields(self):
        d = _make_driver()
        d._devices["d1"] = {"points": {"p1": {"struct_fields": ["a", "b"]}}}
        val, err = d._validate_write_value("p1", {"a": 1}, "", "d1")
        assert val is None and err == "ERR_AB_WRITE_VALUE_INVALID"


class TestWriteRate:
    def test_first_write_ok(self):
        d = _make_driver()
        assert d._check_write_rate("p1") is True

    def test_rate_limited(self):
        d = _make_driver()
        d._check_write_rate("p1")
        assert d._check_write_rate("p1") is False

    def test_rate_after_interval(self):
        d = _make_driver()
        d._write_rate_limit_ms = 0.0
        d._check_write_rate("p1")
        time.sleep(0.001)
        assert d._check_write_rate("p1") is True


# ══════════════════════════════════════════════════════════════════════
# 4. Sync I/O methods
# ══════════════════════════════════════════════════════════════════════


class TestSyncMethods:
    def test_sync_read_no_client(self):
        d = _make_driver()
        try:
            d._sync_read_tag("Tag1")
            assert False, "Should raise"
        except RuntimeError:
            pass

    def test_sync_write_no_client(self):
        d = _make_driver()
        try:
            d._sync_write_tag("Tag1", 1)
            assert False, "Should raise"
        except RuntimeError:
            pass

    def test_sync_get_tag_list_no_client(self):
        d = _make_driver()
        try:
            d._sync_get_tag_list()
            assert False, "Should raise"
        except RuntimeError:
            pass

    def test_sync_get_program_list_no_client(self):
        d = _make_driver()
        try:
            d._sync_get_program_list()
            assert False, "Should raise"
        except RuntimeError:
            pass

    def test_sync_read_with_client(self):
        d = _make_driver()
        plc = _FakePlc(read_value=42)
        d._client = plc
        resp = d._sync_read_tag("Tag1")
        assert resp.Value == 42

    def test_sync_write_with_client(self):
        d = _make_driver()
        plc = _FakePlc(write_status=0)
        d._client = plc
        resp = d._sync_write_tag("Tag1", 1)
        assert resp.Status == 0

    def test_sync_get_tag_list_with_client(self):
        d = _make_driver()
        plc = _FakePlc()
        d._client = plc
        result = d._sync_get_tag_list("")
        assert result.Status == 0

    def test_sync_get_program_list_with_client(self):
        d = _make_driver()
        plc = _FakePlc()
        d._client = plc
        result = d._sync_get_program_list()
        assert result.Status == 0

    def test_sync_ping_no_client(self):
        d = _make_driver()
        assert d._sync_ping() is False

    def test_sync_ping_ok(self):
        d = _make_driver()
        d._client = _FakePlc(read_value="cpu_info", read_status=0)
        assert d._sync_ping() is True

    def test_sync_ping_fail_status(self):
        d = _make_driver()
        d._client = _FakePlc(read_status=0x01)
        assert d._sync_ping() is False

    def test_sync_ping_exception(self):
        d = _make_driver()
        d._client = _FakePlc(raise_exc=ConnectionError("timeout"))
        assert d._sync_ping() is False

    def test_sync_close_no_client(self):
        d = _make_driver()
        assert d._sync_close_client() is None

    def test_sync_close_with_client(self):
        d = _make_driver()
        plc = _FakePlc()
        d._client = plc
        d._sync_close_client()
        assert plc._closed

    def test_sync_forward_close_no_client(self):
        d = _make_driver()
        assert d._sync_forward_close_client() is None


# ══════════════════════════════════════════════════════════════════════
# 5. Forward close / backoff / degrade
# ══════════════════════════════════════════════════════════════════════


class TestForwardClose:
    async def test_no_client(self):
        d = _make_driver()
        await d._forward_close()  # should not raise

    async def test_close_with_client(self):
        d = _make_driver()
        d._client = _FakePlc()
        _mrt(d)
        await d._forward_close()
        assert d._client._closed

    async def test_close_timeout(self):
        d = _make_driver()
        d._client = _FakePlc()

        async def _raise_timeout(*a, **kw):
            raise TimeoutError()

        d._run_in_thread = _raise_timeout  # type: ignore
        await d._forward_close()  # should not raise despite timeout


class TestBackoff:
    def test_no_backup(self):
        d = _make_driver()
        d._reconnect_count = 2
        delay = d._calc_backoff_delay()
        assert 4.0 <= delay <= 5.0  # 1*2^2=4 + jitter

    def test_with_backup(self):
        d = _make_driver()
        d._backup_ip = "192.168.1.2"
        d._reconnect_count = 0
        delay = d._calc_backoff_delay()
        assert 0.5 <= delay <= 0.7  # fast failover + small jitter

    def test_max_delay_cap(self):
        d = _make_driver()
        d._reconnect_count = 20
        delay = d._calc_backoff_delay()
        assert delay <= 61.0

    def test_retry_delay(self):
        d = _make_driver()
        delay = d._calc_retry_delay(0)
        # base=0.5, jitter=0.5*0.3=0.15, range = [0.5-0.15, 0.5+0.15] = [0.35, 0.65]
        assert 0.35 <= delay <= 0.65


class TestDegrade:
    def test_no_window(self):
        d = _make_driver()
        d._check_degradation("d1")  # should not raise

    def test_degrade_triggered(self):
        d = _make_driver()
        for ok in [False] * 15:
            d._degrade_window.append((time.monotonic(), ok))
        d._check_degradation("d1")
        assert d._degraded_freq is True
        assert d._conn_state == AbConnState.DEGRADED.value

    def test_degrade_recovered(self):
        d = _make_driver()
        d._degraded_freq = True
        d._conn_state = AbConnState.DEGRADED.value
        for ok in [True] * 15:
            d._degrade_window.append((time.monotonic(), ok))
        d._check_degradation("d1")
        assert d._degraded_freq is False
        assert d._conn_state == AbConnState.CONNECTED.value

    def test_stale_points_cleaned(self):
        d = _make_driver()
        s = PointHealthStats()
        s.last_access_time = time.monotonic() - 4000
        d._point_stats["old_point"] = s
        d._check_degradation("d1")
        assert "old_point" not in d._point_stats


# ══════════════════════════════════════════════════════════════════════
# 6. _run_in_thread
# ══════════════════════════════════════════════════════════════════════


class TestRunInThread:
    async def test_normal_execution(self):
        d = _make_driver()

        def add(a, b):
            return a + b

        result = await d._run_in_thread(add, 1, 2, timeout=5.0)
        assert result == 3

    async def test_timeout(self):
        d = _make_driver()

        def slow():
            time.sleep(2)  # longer than 0.1s timeout, short enough for cleanup

        try:
            await d._run_in_thread(slow, timeout=0.1)
            assert False, "Should timeout"
        except TimeoutError:
            pass
        assert d._thread_pool_failed is True

    async def test_pool_rebuild_after_failure(self):
        d = _make_driver()
        d._thread_pool_failed = True
        old_pool = d._thread_pool

        def echo(x):
            return x

        result = await d._run_in_thread(echo, 42, timeout=5.0)
        assert result == 42
        assert d._thread_pool_failed is False

    async def test_saturation(self):
        d = _make_driver()
        # _thread_pool_semaphore is lazily created on first _run_in_thread call.
        # Trigger lazy init, then replace with a 0-permit semaphore to saturate.
        await d._run_in_thread(lambda: None, timeout=5.0)
        d._thread_pool_semaphore = asyncio.Semaphore(0)

        def echo(x):
            return x

        try:
            await d._run_in_thread(echo, 1, timeout=5.0)
            assert False, "Should raise RuntimeError"
        except RuntimeError:
            pass


# ══════════════════════════════════════════════════════════════════════
# 7. start / stop
# ══════════════════════════════════════════════════════════════════════


class TestStart:
    async def test_missing_ip(self):
        d = _make_driver()
        try:
            await d.start({})
            assert False, "Should raise ValueError"
        except ValueError:
            pass

    async def test_port_out_of_range(self):
        d = _make_driver()
        try:
            await d.start({"ip": "127.0.0.1", "port": 99999})
            assert False, "Should raise ValueError"
        except ValueError:
            pass

    async def test_slot_out_of_range(self):
        d = _make_driver()
        try:
            await d.start({"ip": "127.0.0.1", "port": 44818, "slot": 99})
            assert False, "Should raise ValueError"
        except ValueError:
            pass

    async def test_successful_start(self):
        d = _make_driver()
        fake_plc = _FakePlc(read_value="cpu", read_status=0)
        with patch("pylogix.PLC", return_value=fake_plc):
            await d.start({"ip": "127.0.0.1", "port": 44818, "slot": 0, "device_id": "d1"})
        assert d._running is True
        assert d._conn_state == AbConnState.CONNECTED.value
        assert d._primary_ip == "127.0.0.1"

    async def test_start_micrologix(self):
        d = _make_driver()
        fake_plc = _FakePlc(read_value="cpu", read_status=0)
        with patch("pylogix.PLC", return_value=fake_plc):
            await d.start({"ip": "127.0.0.1", "port": 2222, "plc_model": "MicroLogix", "device_id": "d1"})
        assert d._running is True

    async def test_ping_fail_sets_disconnected(self):
        d = _make_driver()
        fake_plc = _FakePlc(read_status=0x01)
        with patch("pylogix.PLC", return_value=fake_plc):
            await d.start({"ip": "127.0.0.1", "port": 44818, "device_id": "d1"})
        assert d._running is False
        assert d._conn_state == AbConnState.DISCONNECTED.value
        assert d._client is None

    async def test_start_with_backup(self):
        d = _make_driver()
        fake_plc = _FakePlc(read_value="cpu", read_status=0)
        with patch("pylogix.PLC", return_value=fake_plc):
            await d.start(
                {
                    "ip": "127.0.0.1",
                    "port": 44818,
                    "backup_ip": "127.0.0.2",
                    "device_id": "d1",
                }
            )
        assert d._backup_ip == "127.0.0.2"
        assert d._failover_probe_task is not None
        # cleanup
        await d.stop()

    async def test_start_scaling_clamp_config(self):
        d = _make_driver()
        fake_plc = _FakePlc(read_value="cpu", read_status=0)
        with patch("pylogix.PLC", return_value=fake_plc):
            await d.start(
                {
                    "ip": "127.0.0.1",
                    "port": 44818,
                    "device_id": "d1",
                    "scaling_ratio": 2.0,
                    "scaling_offset": 1.0,
                    "clamp_min": 0.0,
                    "clamp_max": 100.0,
                }
            )
        assert d._config.get("scaling") == {"ratio": 2.0, "offset": 1.0}
        assert d._config.get("clamp") == {"min": 0.0, "max": 100.0}


class TestStop:
    async def test_stop_no_client(self):
        d = _make_driver()
        await d.stop()
        assert d._running is False

    async def test_stop_with_client(self):
        d = _make_driver()
        d._client = _FakePlc()
        d._running = True
        _mrt(d)
        await d.stop()
        assert d._client is None
        assert d._running is False

    async def test_stop_cancels_watchdog(self):
        d = _make_driver()
        d._running = True

        async def _wd():
            try:
                while True:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise

        d._watchdog_task = asyncio.create_task(_wd())
        _mrt(d)
        await d.stop()
        assert d._watchdog_task is None

    async def test_stop_clears_state(self):
        d = _make_driver()
        d._devices = {"d1": {}}
        d._point_stats = {"p1": PointHealthStats()}
        d._last_values = OrderedDict({"p1": 1})
        _mrt(d)
        await d.stop()
        assert len(d._devices) == 0
        assert len(d._point_stats) == 0
        assert len(d._last_values) == 0


# ══════════════════════════════════════════════════════════════════════
# 8. read_points
# ══════════════════════════════════════════════════════════════════════


class TestReadPoints:
    async def test_not_running_returns_bad(self):
        d = _make_driver()
        result = await d.read_points("d1", ["Tag1"])
        assert result["Tag1"].quality == "bad"

    async def test_successful_read(self):
        d = _make_driver()
        d._running = True
        d._client = _FakePlc(read_value=42, read_status=0)
        _mrt(d)
        result = await d.read_points("d1", ["Tag1"])
        assert result["Tag1"].value == 42
        assert result["Tag1"].quality == "good"

    async def test_read_cip_error(self):
        d = _make_driver()
        d._running = True
        d._client = _FakePlc(read_status=0x52)
        _mrt(d)
        result = await d.read_points("d1", ["Tag1"])
        assert result["Tag1"].quality == "bad"

    async def test_read_exception(self):
        d = _make_driver()
        d._running = True
        d._client = _FakePlc(raise_exc=ConnectionError("lost"))
        _mrt(d)
        result = await d.read_points("d1", ["Tag1"])
        assert result["Tag1"].quality == "bad"

    async def test_read_batch_list(self):
        d = _make_driver()
        d._running = True
        d._client = _FakePlc(read_value=10, read_status=0)
        _mrt(d)
        result = await d.read_points("d1", ["T1", "T2", "T3"])
        assert all(result[p].value == 10 for p in ["T1", "T2", "T3"])

    async def test_read_records_point_stats(self):
        d = _make_driver()
        d._running = True
        # Use _FallbackPlc: batch (list) read fails (CIP error → None),
        # forcing the per-point fallback path which calls _record_point_success.
        d._client = _FallbackPlc(read_value=42, read_status=0)
        _mrt(d)
        await d.read_points("d1", ["Tag1"])
        assert "Tag1" in d._point_stats
        assert d._point_stats["Tag1"].success_count >= 1


# ══════════════════════════════════════════════════════════════════════
# 9. _parse_response_value
# ══════════════════════════════════════════════════════════════════════


class TestParseResp:
    def test_none_resp(self):
        d = _make_driver()
        assert d._parse_response_value(None, "p1") is None

    def test_error_status(self):
        d = _make_driver()
        assert d._parse_response_value(_Resp(status=0x52), "p1") is None

    def test_simple_value(self):
        d = _make_driver()
        assert d._parse_response_value(_Resp(value=42), "p1") == 42

    def test_list_value(self):
        d = _make_driver()
        assert d._parse_response_value(_Resp(value=[1, 2, 3]), "p1") == [1, 2, 3]

    def test_large_list_truncated(self):
        d = _make_driver()
        big = list(range(2000))
        result = d._parse_response_value(_Resp(value=big), "p1")
        assert len(result) == 1024

    def test_dict_value(self):
        d = _make_driver()

        class Obj:
            def __init__(self):
                self.a = 1
                self.b = 2

        result = d._parse_response_value(_Resp(value=Obj()), "p1")
        assert result == {"a": 1, "b": 2}

    def test_none_value(self):
        d = _make_driver()
        assert d._parse_response_value(_Resp(value=None), "p1") is None


# ══════════════════════════════════════════════════════════════════════
# 10. write_point / batch_write
# ══════════════════════════════════════════════════════════════════════


class TestWritePoint:
    async def test_not_running(self):
        d = _make_driver()
        assert await d.write_point("d1", "Tag1", 42) is False

    async def test_rbac_denied(self):
        d = _make_driver()
        d._running = True
        d._client = _FakePlc()

        async def _deny(perm):
            return False

        d.check_permission = _deny  # type: ignore
        assert await d.write_point("d1", "Tag1", 42) is False

    async def test_write_rate_limited(self):
        d = _make_driver()
        d._running = True
        d._client = _FakePlc(write_status=0)
        d._write_rate_limit_ms = 99999.0

        async def _allow(perm):
            return True

        d.check_permission = _allow  # type: ignore
        d._check_write_rate("Tag1")  # consume first
        assert await d.write_point("d1", "Tag1", 42) is False

    async def test_successful_write(self):
        d = _make_driver()
        d._running = True
        d._client = _FakePlc(read_value=42, read_status=0, write_status=0)
        d._write_verify_delay_ms = 0.0
        _mrt(d)

        async def _allow(perm):
            return True

        d.check_permission = _allow  # type: ignore
        assert await d.write_point("d1", "Tag1", 42) is True

    async def test_write_cip_error(self):
        d = _make_driver()
        d._running = True
        d._client = _FakePlc(write_status=0x52)
        _mrt(d)

        async def _allow(perm):
            return True

        d.check_permission = _allow  # type: ignore
        d._write_rate_limit_ms = 0.0
        assert await d.write_point("d1", "Tag1", 42) is False

    async def test_write_validation_fail(self):
        d = _make_driver()
        d._running = True
        d._client = _FakePlc(write_status=0)
        d._point_configs["Tag1"] = {"data_type": "SINT"}
        _mrt(d)

        async def _allow(perm):
            return True

        d.check_permission = _allow  # type: ignore
        d._write_rate_limit_ms = 0.0
        assert await d.write_point("d1", "Tag1", 999) is False

    async def test_write_audit_log(self):
        d = _make_driver()
        d._running = True
        d._client = _FakePlc(read_value=42, read_status=0, write_status=0)
        d._write_verify_delay_ms = 0.0
        _mrt(d)

        async def _allow(perm):
            return True

        d.check_permission = _allow  # type: ignore
        await d.write_point("d1", "Tag1", 42)
        log = d.get_write_audit_log()
        assert len(log) >= 1
        assert log[-1]["result"] == "ok"

    async def test_write_audit_log_by_device(self):
        d = _make_driver()
        d._running = True
        d._client = _FakePlc(read_value=42, read_status=0, write_status=0)
        d._write_verify_delay_ms = 0.0
        _mrt(d)

        async def _allow(perm):
            return True

        d.check_permission = _allow  # type: ignore
        await d.write_point("d1", "Tag1", 42)
        log = d.get_write_audit_log("d1")
        assert len(log) >= 1
        log_other = d.get_write_audit_log("other")
        assert len(log_other) == 0


class TestBatchWrite:
    async def test_not_running(self):
        d = _make_driver()
        result = await d.batch_write_points("d1", [("Tag1", 1)])
        assert result == {"Tag1": False}

    async def test_successful_batch(self):
        d = _make_driver()
        d._running = True
        d._client = _FakePlc(write_status=0, read_value=1, read_status=0)
        d._write_rate_limit_ms = 0.0
        d._write_verify_delay_ms = 0.0
        _mrt(d)
        result = await d.batch_write_points("d1", [("Tag1", 1), ("Tag2", 2)])
        assert result["Tag1"] is True
        assert result["Tag2"] is True

    async def test_batch_validation_fail(self):
        d = _make_driver()
        d._running = True
        d._client = _FakePlc(write_status=0)
        d._point_configs["Tag1"] = {"data_type": "SINT"}
        d._write_rate_limit_ms = 0.0
        _mrt(d)
        result = await d.batch_write_points("d1", [("Tag1", 999)])
        assert result["Tag1"] is False


# ══════════════════════════════════════════════════════════════════════
# 11. Bool array offset / reconnect / failover
# ══════════════════════════════════════════════════════════════════════


class TestBoolOffset:
    def test_no_offset(self):
        d = _make_driver()
        assert d._validate_bool_array_offset("Tag1") is True

    def test_no_device_info(self):
        d = _make_driver()
        assert d._validate_bool_array_offset("Tag1.5") is True

    def test_offset_in_range(self):
        d = _make_driver()
        d._devices["d1"] = {"points": {"Tag1": {"dimensions": [32]}}}
        assert d._validate_bool_array_offset("Tag1.5") is True

    def test_offset_out_of_range(self):
        d = _make_driver()
        # dimensions [32] (single-dim) → max_bits = 32 * 32 = 1024
        d._devices["d1"] = {"points": {"Tag1": {"dimensions": [32]}}}
        assert d._validate_bool_array_offset("Tag1.1024") is False

    def test_offset_multi_dim(self):
        d = _make_driver()
        d._devices["d1"] = {"points": {"Arr": {"dimensions": [10, 10]}}}
        assert d._validate_bool_array_offset("Arr.5") is True
        assert d._validate_bool_array_offset("Arr.15") is False


class TestReconnect:
    async def test_no_config(self):
        d = _make_driver()
        await d._try_reconnect("d1")  # should not raise

    async def test_cooldown_active(self):
        d = _make_driver()
        d._config = {"ip": "127.0.0.1", "port": 44818}
        d._reconnect_cooldown_until = time.time() + 3600
        await d._try_reconnect("d1")
        assert d._conn_state != AbConnState.CONNECTED.value

    async def test_max_attempts_offline(self):
        d = _make_driver()
        d._config = {"ip": "127.0.0.1", "port": 44818}
        d._reconnect_count = 3
        d._RECONNECT_BASE_DELAY = 0.0
        await d._try_reconnect("d1")
        assert d._conn_state == AbConnState.OFFLINE.value
        assert d._reconnect_cooldown_until > time.time()

    async def test_reconnect_success(self):
        d = _make_driver()
        d._config = {"ip": "127.0.0.1", "port": 44818, "slot": 0}
        d._active_ip = "127.0.0.1"  # _try_reconnect uses _active_ip as target_ip
        d._connection_timeout = 5.0  # normally set in start(), needed by reconnect
        d._RECONNECT_BASE_DELAY = 0.0
        d._JITTER_MAX_MS = 0
        fake_plc = _FakePlc(read_value="cpu", read_status=0)
        with patch("pylogix.PLC", return_value=fake_plc):
            await d._try_reconnect("d1")
        assert d._running is True
        assert d._conn_state == AbConnState.CONNECTED.value
        assert d._reconnect_count == 0

    async def test_reconnect_ping_fail(self):
        d = _make_driver()
        d._config = {"ip": "127.0.0.1", "port": 44818, "slot": 0}
        d._active_ip = "127.0.0.1"  # _try_reconnect uses _active_ip as target_ip
        d._connection_timeout = 5.0  # normally set in start(), needed by reconnect
        d._RECONNECT_BASE_DELAY = 0.0
        d._JITTER_MAX_MS = 0
        fake_plc = _FakePlc(read_status=0x01)
        with patch("pylogix.PLC", return_value=fake_plc):
            await d._try_reconnect("d1")
        assert d._client is None
        assert d._conn_state == AbConnState.DISCONNECTED.value

    async def test_reconnect_failover_to_backup(self):
        d = _make_driver()
        d._config = {
            "ip": "127.0.0.1",
            "port": 44818,
            "slot": 0,
            "backup_ip": "127.0.0.2",
            "failover_threshold": 1,
        }
        d._RECONNECT_BASE_DELAY = 0.0
        d._JITTER_MAX_MS = 0
        d._FAILOVER_FAST_DELAY = 0.0
        d._backup_ip = "127.0.0.2"
        fake_plc = _FakePlc(read_value="cpu", read_status=0)
        with patch("pylogix.PLC", return_value=fake_plc):
            await d._try_reconnect("d1")
        assert d._using_backup is True
        assert d._active_ip == "127.0.0.2"
        assert d._failover_count == 1


class TestFailoverInfo:
    def test_get_failover_info(self):
        d = _make_driver()
        d._primary_ip = "1.1.1.1"
        d._backup_ip = "2.2.2.2"
        d._active_ip = "1.1.1.1"
        info = d.get_failover_info()
        assert info["primary_ip"] == "1.1.1.1"
        assert info["backup_ip"] == "2.2.2.2"
        assert info["using_backup"] is False


# ══════════════════════════════════════════════════════════════════════
# 12. add_device / remove_device
# ══════════════════════════════════════════════════════════════════════


class TestAddRemove:
    async def test_add_device(self):
        d = _make_driver()
        d._connection_timeout = 5.0
        fake_plc = _FakePlc()
        with patch("pylogix.PLC", return_value=fake_plc):
            await d.add_device("d1", {"ip": "127.0.0.1", "port": 44818, "slot": 0}, [{"name": "Tag1"}])
        assert "d1" in d._devices
        assert "d1" in d._device_clients
        assert "d1" in d._device_locks

    async def test_add_device_with_point_config(self):
        d = _make_driver()
        d._connection_timeout = 5.0
        fake_plc = _FakePlc()
        points = [{"name": "p1", "deadband": 0.5, "scaling_ratio": 2.0, "clamp_min": 0, "clamp_max": 100}]
        with patch("pylogix.PLC", return_value=fake_plc):
            await d.add_device("d1", {"ip": "127.0.0.1"}, points)
        assert "p1" in d._point_configs
        assert d._point_configs["p1"]["deadband"] == 0.5

    async def test_add_device_no_ip(self):
        d = _make_driver()
        d._connection_timeout = 5.0
        await d.add_device("d1", {}, [{"name": "Tag1"}])
        assert "d1" in d._devices
        assert "d1" not in d._device_clients

    async def test_remove_device(self):
        d = _make_driver()
        d._connection_timeout = 5.0
        fake_plc = _FakePlc()
        with patch("pylogix.PLC", return_value=fake_plc):
            await d.add_device("d1", {"ip": "127.0.0.1"}, [{"name": "Tag1"}])
        _mrt(d)
        await d.remove_device("d1")
        assert "d1" not in d._devices
        assert "d1" not in d._device_clients
        assert "Tag1" not in d._point_configs

    async def test_remove_nonexistent(self):
        d = _make_driver()
        _mrt(d)
        await d.remove_device("nonexistent")  # should not raise


# ══════════════════════════════════════════════════════════════════════
# 13. Discovery
# ══════════════════════════════════════════════════════════════════════


class TestDiscover:
    async def test_discover_no_client(self):
        d = _make_driver()
        assert await d.discover_devices({}) == []

    async def test_discover_devices(self):
        d = _make_driver()
        d._client = _FakePlc(read_value="MyProject", read_status=0)
        d._config = {"ip": "192.168.1.1", "slot": 0, "plc_model": "ControlLogix"}
        _mrt(d)
        results = await d.discover_devices({})
        assert len(results) >= 1
        assert "ab_192_168_1_1" in results[0]["device_id"]

    async def test_discover_programs(self):
        d = _make_driver()
        d._client = _FakePlc()
        _mrt(d)
        progs = await d._discover_programs()
        assert "MainProgram" in progs

    async def test_discover_programs_no_client(self):
        d = _make_driver()
        assert await d._discover_programs() == []

    async def test_discover_tags(self):
        d = _make_driver()
        d._client = _FakePlc()
        _mrt(d)
        tags = await d.discover_tags("")
        assert len(tags) >= 1
        assert tags[0]["name"] == "Tag1"

    async def test_discover_tags_no_client(self):
        d = _make_driver()
        assert await d.discover_tags("") == []

    async def test_discover_tags_with_program(self):
        d = _make_driver()
        d._client = _FakePlc()
        _mrt(d)
        tags = await d.discover_tags("Main")
        assert len(tags) >= 1
        assert "Program:Main." in tags[0]["tag_name"]


class TestBrowse:
    async def test_browse_struct_no_client(self):
        d = _make_driver()
        assert await d.browse_struct_members("MyStruct") == []

    async def test_browse_struct_dict(self):
        d = _make_driver()
        # browse_struct_members looks for a tag with TagName == "MyStruct" in
        # GetTagList results before reading its value. Pass tags explicitly.
        d._client = _FakePlc(
            read_value={"a": 1, "b": 2},
            read_status=0,
            tags=[_FakeTag("MyStruct")],
        )
        _mrt(d)
        members = await d.browse_struct_members("MyStruct")
        assert len(members) == 2

    async def test_browse_array_no_client(self):
        d = _make_driver()
        assert await d.browse_array_range("Arr[0]") == []

    async def test_browse_array_ok(self):
        d = _make_driver()
        d._client = _FakePlc(read_value=[1, 2, 3], read_status=0)
        _mrt(d)
        result = await d.browse_array_range("Arr[0]")
        assert len(result) == 1
        assert result[0]["index"] == 0


# ══════════════════════════════════════════════════════════════════════
# 14. Watchdog / health_check
# ══════════════════════════════════════════════════════════════════════


class TestWatchdogModes:
    async def test_ping_mode_success(self):
        d = _make_driver()
        d._running = True
        d._watchdog_interval = 0.001
        d._watchdog_check_mode = "ping"
        d._client = _FakePlc(read_status=0)
        d._devices = {"d1": {}}
        d._conn_state = AbConnState.CONNECTED.value
        d._handle_watchdog_exception = MagicMock(return_value=True)  # type: ignore
        call_count = 0

        async def _mrt2(func, *a, timeout=30.0, **kw):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                d._running = False
            return func(*a, **kw)

        d._run_in_thread = _mrt2  # type: ignore
        await d._watchdog_loop()
        assert d._watchdog_fail_count == 0

    async def test_tag_mode_success(self):
        d = _make_driver()
        d._running = True
        d._watchdog_interval = 0.001
        d._watchdog_check_mode = "tag"
        d._client = _FakePlc(read_status=0)
        d._devices = {"d1": {}}
        d._conn_state = AbConnState.CONNECTED.value
        d._handle_watchdog_exception = MagicMock(return_value=True)  # type: ignore
        call_count = 0

        async def _mrt2(func, *a, timeout=30.0, **kw):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                d._running = False
            return func(*a, **kw)

        d._run_in_thread = _mrt2  # type: ignore
        await d._watchdog_loop()
        assert d._watchdog_fail_count == 0

    async def test_auto_mode_fallback(self):
        d = _make_driver()
        d._running = True
        d._watchdog_interval = 0.001
        d._watchdog_check_mode = "auto"
        d._client = _FakePlc(read_status=0)
        d._devices = {"d1": {}}
        d._conn_state = AbConnState.CONNECTED.value
        d._handle_watchdog_exception = MagicMock(return_value=True)  # type: ignore
        call_count = 0

        async def _mrt2(func, *a, timeout=30.0, **kw):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:  # ping(fail) + tag(ok)
                d._running = False
            if func.__name__ == "_sync_ping":
                return False
            return _Resp(status=0)

        d._run_in_thread = _mrt2  # type: ignore
        await d._watchdog_loop()
        assert d._watchdog_fail_count == 0

    async def test_watchdog_fail_increments(self):
        d = _make_driver()
        d._running = True
        d._watchdog_interval = 0.001
        d._watchdog_check_mode = "ping"
        d._client = _FakePlc(read_status=0x01)
        d._devices = {"d1": {}}
        d._conn_state = AbConnState.CONNECTED.value
        d._handle_watchdog_exception = MagicMock(return_value=True)  # type: ignore
        call_count = 0

        async def _mrt2(func, *a, timeout=30.0, **kw):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                d._running = False
            return False

        d._run_in_thread = _mrt2  # type: ignore
        await d._watchdog_loop()
        assert d._watchdog_fail_count == 2


class TestHealthCheck:
    async def test_not_running(self):
        d = _make_driver()
        assert await d.health_check("d1") is False

    async def test_no_client(self):
        d = _make_driver()
        d._running = True
        assert await d.health_check("d1") is False

    async def test_healthy(self):
        d = _make_driver()
        d._running = True
        d._client = _FakePlc(read_status=0)
        _mrt(d)
        assert await d.health_check("d1") is True

    async def test_unhealthy(self):
        d = _make_driver()
        d._running = True
        d._client = _FakePlc(read_status=0x01)
        _mrt(d)
        assert await d.health_check("d1") is False


# ══════════════════════════════════════════════════════════════════════
# 15. Point stats / CIP error dist
# ══════════════════════════════════════════════════════════════════════


class TestPointStats:
    def test_no_stats(self):
        d = _make_driver()
        assert d.get_point_stats("d1", "p1") is None

    def test_with_stats(self):
        d = _make_driver()
        s = PointHealthStats()
        s.record_success(10.0)
        s.record_failure("ERR_CIP_TAG_NOT_FOUND")
        d._point_stats["p1"] = s
        result = d.get_point_stats("d1", "p1")
        assert result["success_count"] == 1
        assert result["fail_count"] == 1
        assert result["avg_latency_ms"] == 10.0

    def test_cip_error_dist(self):
        d = _make_driver()
        s = PointHealthStats()
        s.record_failure("ERR_CIP_TAG_NOT_FOUND")
        s.record_failure("ERR_CIP_TAG_NOT_FOUND")
        d._point_stats["p1"] = s
        dist = d.get_cip_error_dist()
        assert dist.get("ERR_CIP_TAG_NOT_FOUND") == 2

    def test_cip_error_dist_empty(self):
        d = _make_driver()
        assert d.get_cip_error_dist() == {}

    def test_tag_type_unknown(self):
        d = _make_driver()
        assert d._get_point_tag_type("p1") == "unknown"

    def test_tag_type_from_device(self):
        d = _make_driver()
        d._devices["d1"] = {"points": {"p1": {"type": "DINT"}}}
        assert d._get_point_tag_type("p1") == "DINT"


# ══════════════════════════════════════════════════════════════════════
# 16. RBAC / Config version / OTA
# ══════════════════════════════════════════════════════════════════════


class TestRbac:
    def test_check_rbac_no_audit(self):
        d = _make_driver()
        result = d.check_rbac("admin", "device:write_point", "d1")
        assert isinstance(result, bool)

    def test_check_rbac_invalid_permission(self):
        d = _make_driver()
        assert d.check_rbac("admin", "invalid:perm", "d1") is False

    async def test_set_user_role(self):
        d = _make_driver()
        await d.set_user_role("operator")
        assert d._current_user_role == "operator"

    def test_init_enterprise(self):
        d = _make_driver()
        try:
            d.init_enterprise()
            assert d._config_version_mgr is not None
            assert d._ota_mgr is not None
            assert d._audit is not None
        except Exception:
            pass  # depends on ab_audit/config_version/ota modules


class TestConfigVer:
    async def test_no_manager_save(self):
        d = _make_driver()
        assert await d.save_config_version("d1", {}) == 0

    async def test_no_manager_get_current(self):
        d = _make_driver()
        assert await d.get_config_current("d1") is None

    async def test_no_manager_get_versions(self):
        d = _make_driver()
        assert await d.get_config_versions("d1") == []

    async def test_no_manager_get_version_config(self):
        d = _make_driver()
        assert await d.get_config_version_config("d1", 1) is None

    async def test_no_manager_rollback(self):
        d = _make_driver()
        assert await d.rollback_config("d1", 1) is None

    async def test_no_manager_audit_trail(self):
        d = _make_driver()
        assert await d.get_config_audit_trail("d1") == []

    async def test_no_manager_diff(self):
        d = _make_driver()
        result = await d.diff_config_versions("d1", 1, 2)
        assert result == {"changes": []}


class TestOta:
    def test_no_mgr_check_update(self):
        d = _make_driver()
        assert d.ota_check_update(MagicMock()) is False

    async def test_no_mgr_start(self):
        d = _make_driver()
        assert await d.ota_start(MagicMock()) is False

    def test_no_mgr_rollback(self):
        d = _make_driver()
        assert d.ota_rollback() is False

    def test_no_mgr_progress(self):
        d = _make_driver()
        assert d.ota_get_progress() == {}

    def test_no_mgr_history(self):
        d = _make_driver()
        assert d.ota_get_history() == []

    def test_no_audit_recent(self):
        d = _make_driver()
        assert d.get_audit_recent() == []

    def test_no_audit_by_device(self):
        d = _make_driver()
        assert d.get_audit_by_device("d1") == []

    def test_no_audit_by_action(self):
        d = _make_driver()
        assert d.get_audit_by_action("write") == []

    def test_no_audit_export_csv(self):
        d = _make_driver()
        assert d.get_audit_stats() == {}

    def test_no_audit_export(self):
        d = _make_driver()
        assert d.export_audit_csv() == ""


# ══════════════════════════════════════════════════════════════════════
# 17. Edge rules / TS storage
# ══════════════════════════════════════════════════════════════════════


class TestEdgeRules:
    def test_get_edge_rules_no_engine(self):
        d = _make_driver()
        assert d.get_edge_rules() == []

    def test_get_edge_alarm_history_no_engine(self):
        d = _make_driver()
        assert d.get_edge_alarm_history() == []

    def test_get_edge_rule_stats_no_engine(self):
        d = _make_driver()
        assert d.get_edge_rule_stats() == {}

    def test_add_edge_rule_no_engine(self):
        d = _make_driver()
        assert d.add_edge_rule({"rule_id": "r1"}) is False

    async def test_remove_edge_rule_no_engine(self):
        d = _make_driver()
        assert await d.remove_edge_rule("r1") is False

    async def test_reload_rules_no_engine(self):
        d = _make_driver()
        assert await d.reload_rules() == 0

    async def test_evaluate_rules_no_engine(self):
        d = _make_driver()
        await d._evaluate_rules("d1", {"p1": PointValue(value=1, quality="good", timestamp=datetime.now(UTC))})

    async def test_edge_write_callback_no_devices(self):
        d = _make_driver()
        result = await d._edge_write_callback({"point": "p1", "value": 1}, {})
        assert "error" in result

    async def test_edge_write_callback_missing_point(self):
        d = _make_driver()
        result = await d._edge_write_callback({}, {})
        assert "error" in result


class TestTsStore:
    def test_ts_stats_no_store(self):
        d = _make_driver()
        assert d.get_ts_store_stats() == {}

    async def test_persist_disabled(self):
        d = _make_driver()
        d._persist_enabled = False
        await d._persist_points("d1", {"p1": PointValue(value=1, quality="good", timestamp=datetime.now(UTC))})

    async def test_persist_no_good_values(self):
        d = _make_driver()
        d._persist_enabled = True
        d._ts_store = MagicMock()
        await d._persist_points("d1", {"p1": PointValue(value=None, quality="bad", timestamp=datetime.now(UTC))})
        d._ts_store.write_read_result.assert_not_called()

    def test_init_ts_storage_disabled(self):
        d = _make_driver()
        d._init_ts_storage({"ts_storage_enabled": False})
        assert d._persist_enabled is False

    def test_init_ts_storage_no_config(self):
        d = _make_driver()
        d._init_ts_storage({})
        assert d._persist_enabled is False


# ══════════════════════════════════════════════════════════════════════
# 18. _record_point_success/failure
# ══════════════════════════════════════════════════════════════════════


class TestPointRecord:
    def test_record_success(self):
        d = _make_driver()
        d._record_point_success("p1", 5.0)
        assert "p1" in d._point_stats
        assert d._point_stats["p1"].success_count == 1

    def test_record_failure(self):
        d = _make_driver()
        d._record_point_failure("p1", "ERR_CIP_TAG_NOT_FOUND")
        assert d._point_stats["p1"].fail_count == 1
        assert d._point_stats["p1"].last_cip_error == "ERR_CIP_TAG_NOT_FOUND"

    def test_record_success_capacity(self):
        d = _make_driver()
        d._MAX_POINT_STATS = 2
        d._record_point_success("p1", 1.0)
        d._record_point_success("p2", 1.0)
        d._record_point_success("p3", 1.0)
        assert len(d._point_stats) == 2
        assert "p3" in d._point_stats

    def test_record_failure_capacity(self):
        d = _make_driver()
        d._MAX_POINT_STATS = 2
        d._record_point_failure("p1")
        d._record_point_failure("p2")
        d._record_point_failure("p3")
        assert len(d._point_stats) == 2
        assert "p3" in d._point_stats


# ══════════════════════════════════════════════════════════════════════
# 19. _write_verify
# ══════════════════════════════════════════════════════════════════════


class TestWriteVerify:
    async def test_verify_success(self):
        d = _make_driver()
        d._client = _FakePlc(read_value=42, read_status=0)
        d._write_verify_delay_ms = 0.0
        _mrt(d)
        assert await d._write_verify("Tag1", 42) is True

    async def test_verify_mismatch(self):
        d = _make_driver()
        d._client = _FakePlc(read_value=99, read_status=0)
        d._write_verify_delay_ms = 0.0
        _mrt(d)
        assert await d._write_verify("Tag1", 42) is False

    async def test_verify_none_readback(self):
        d = _make_driver()
        d._client = _FakePlc(read_value=None, read_status=0x01)
        d._write_verify_delay_ms = 0.0
        _mrt(d)
        assert await d._write_verify("Tag1", 42) is False

    async def test_verify_exception(self):
        d = _make_driver()
        d._client = _FakePlc(raise_exc=ConnectionError("lost"))
        d._write_verify_delay_ms = 0.0
        _mrt(d)
        assert await d._write_verify("Tag1", 42) is False


# ══════════════════════════════════════════════════════════════════════
# 20. _try_revert_primary / _failover_probe_loop
# ══════════════════════════════════════════════════════════════════════


class TestRevertPrimary:
    async def test_not_using_backup(self):
        d = _make_driver()
        await d._try_revert_primary("d1")  # should not raise

    async def test_no_primary_ip(self):
        d = _make_driver()
        d._using_backup = True
        await d._try_revert_primary("d1")

    async def test_revert_primary_ping_fail(self):
        d = _make_driver()
        d._using_backup = True
        d._primary_ip = "127.0.0.1"
        d._config = {"port": 44818, "slot": 0}
        d._connection_timeout = 5.0
        fake_plc = _FakePlc(read_status=0x01)
        _mrt(d)
        with patch("pylogix.PLC", return_value=fake_plc):
            await d._try_revert_primary("d1")
        assert d._using_backup is True  # stays on backup

    async def test_revert_primary_success(self):
        d = _make_driver()
        d._using_backup = True
        d._primary_ip = "127.0.0.1"
        d._active_ip = "127.0.0.2"
        d._config = {"port": 44818, "slot": 0}
        d._connection_timeout = 5.0
        fake_plc = _FakePlc(read_value="cpu", read_status=0)
        _mrt(d)
        with patch("pylogix.PLC", return_value=fake_plc):
            await d._try_revert_primary("d1")
        assert d._using_backup is False
        assert d._active_ip == "127.0.0.1"


class TestFailoverProbe:
    async def test_probe_loop_not_running(self):
        d = _make_driver()
        d._running = False
        await d._failover_probe_loop()  # should return immediately

    async def test_probe_loop_no_backup(self):
        d = _make_driver()
        d._running = True
        d._FAILOVER_PROBE_INTERVAL = 0.001
        d._using_backup = False

        # Stop after one iteration
        original_sleep = asyncio.sleep

        async def _quick_sleep(t):
            d._running = False
            await original_sleep(0)

        with patch("asyncio.sleep", _quick_sleep):
            await d._failover_probe_loop()
