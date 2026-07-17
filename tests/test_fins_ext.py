"""FINS 协议驱动扩展测试 — 补充 test_fins.py 未覆盖的方法。"""

import asyncio
import struct
import sys
import threading
import time
from collections import OrderedDict, deque
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, "src")
from edgelite.api.error_codes import FinsDriverErrors
from edgelite.drivers.base import DriverCapabilities, DriverHealthStats, PointValue
from edgelite.drivers.fins import (
    FailoverInFlightTimeout,
    FinsConnState,
    FinsResponseError,
    FinsWriteError,
    OmronFinsDriver,
    PointHealthStats,
    WriteAuditEntry,
)


def _make_driver() -> OmronFinsDriver:
    d = OmronFinsDriver.__new__(OmronFinsDriver)
    for k, v in dict(
        _running=False,
        _health_stats={},
        _offline_since={},
        _device_configs={},
        _capabilities=DriverCapabilities(),
        _connection_statuses={},
        _reconnect_state={},
        _circuit_states={},
        _circuit_open_sinces={},
        _half_open_calls={},
        _failure_threshold=5,
        _recovery_timeout=30.0,
        _half_open_max_calls=3,
        _executor=None,
        _executor_max_workers=4,
        _executor_name_prefix="ft",
        _executor_futures=set(),
        _executor_futures_warn_threshold=64,
        _executor_shutting_down=False,
        _executor_shutdown_timeout=10.0,
        _background_tasks=set(),
        _delayed_reconnect_count=0,
        _permanent_offline=set(),
        _client=None,
        _config={},
        _socket_in_use=False,
        _reconnect_attempt=0,
        _reconnect_delay=1.0,
        _devices={},
        _watchdog_task=None,
        _watchdog_offline_count=0,
        _source_node=0,
        _dest_node=0,
        _network_no=0,
        _unit_no=0,
        _command_code="0101",
        _is_direct_mode=False,
        _plc_series="CJ",
        _MAX_LAST_VALUES=10000,
        _MAX_DICT_SIZE=10000,
        _conn_state=FinsConnState.DISCONNECTED,
        _primary_ip="",
        _primary_port=9600,
        _backup_ip="",
        _backup_port=0,
        _active_ip="",
        _active_port=9600,
        _using_backup=False,
        _primary_fail_count=0,
        _failover_probe_task=None,
        _point_configs={},
        _global_frozen_threshold=0,
        _global_rate_limit=0.0,
        _degraded=False,
        _degraded_interval_ms=0,
        _last_read_ts={},
        _frozen_counters={},
        _write_verify_enabled=False,
        _write_rate_limit_ms=500,
        _write_audit_enabled=True,
        _last_write_ts={},
        _quality_history={},
        _device_points={},
        _standby_client=None,
        _standby_task=None,
        _init_standby_task=None,
        _standby_ready=False,
        _standby_dest_node=0,
        _standby_network_no=0,
        _failover_ts=0.0,
        _delayed_reconnect_task=None,
        _bg_reconnect_task=None,
        _first_reconnect_time=0.0,
        _edge_rule_engine=None,
        _edge_trigger=None,
        _rule_store=None,
        _ts_store=None,
        _offline_sync=None,
        _network_online=True,
        _config_version_mgr=None,
        _audit=None,
        _ota_mgr=None,
        _current_user_role=None,
        _thread_pool=None,
        _thread_pool_failed=False,
        _thread_pool_created_at=0.0,
        _THREAD_POOL_MAX_AGE_SECONDS=3600.0,
        _is_udp=False,
        _udp_max_retries=3,
        _timeout=5.0,
        _max_response_size=65536,
        _data_callback=None,
    ).items():
        setattr(d, k, v)
    d._reconnect_lock = asyncio.Lock()
    d._circuit_lock = threading.Lock()
    d._executor_lock = asyncio.Lock()
    d._shutdown_requested = threading.Event()
    d._stats_lock = threading.RLock()
    d._conn_state_lock = threading.RLock()
    d._watchdog_exception_history = deque(maxlen=60)
    d._watchdog_history_lock = threading.Lock()
    d._lock = asyncio.Lock()
    d._client_lock = threading.RLock()
    d._async_client_lock = asyncio.Lock()
    d._in_flight_requests = 0
    d._in_flight_lock = threading.Lock()
    d._last_values = OrderedDict()
    d._point_stats = OrderedDict()
    d._audit_log = deque(maxlen=1000)
    d._thread_pool_lock = asyncio.Lock()
    return d


def _mksock(recv_data=None):
    s = MagicMock()
    s.fileno.return_value = 1
    s.settimeout = MagicMock()
    s.send = MagicMock()
    s.close = MagicMock()
    if recv_data is not None:
        full = b"FINS" + struct.pack(">I", len(recv_data)) + recv_data
        chunks = [full[i : i + 8] for i in range(0, len(full), 8)] or [b""]
        s.recv = MagicMock(side_effect=chunks)
    else:
        s.recv = MagicMock(return_value=b"")
    return s


def _mkclient(sock=None):
    c = MagicMock()
    c.fins_socket = sock or MagicMock()
    c.read = MagicMock(return_value=42)
    c.write = MagicMock(return_value=None)
    c.close = MagicMock()
    c.connect = MagicMock()
    return c


def _ok_resp(data=b"\x00\x00"):
    return bytes(10) + struct.pack(">H", 0) + bytes(2) + data


class TestParseAddress:
    @pytest.mark.parametrize(
        "addr,exp",
        [
            ("D100", ("d", 100, "w")),
            ("CIO200", ("c", 200, "w")),
            ("C300", ("c", 300, "w")),
            ("W400", ("w", 400, "w")),
            ("H500", ("h", 500, "w")),
            ("A600", ("h", 600, "w")),
            ("EM700", ("e", 700, "w")),
            ("VM800", ("v", 800, "w")),
            ("TK10", ("tk", 10, "w")),
            ("CS20", ("cs", 20, "w")),
            ("IR30", ("ir", 30, "w")),
            ("DR40", ("dr", 40, "w")),
            ("CF50", ("cf", 50, "w")),
        ],
    )
    def test_areas(self, addr, exp):
        assert _make_driver()._parse_address(addr) == exp

    @pytest.mark.parametrize(
        "addr,dt",
        [
            ("D100,b", "b"),
            ("D100,w", "w"),
            ("D100,i", "i"),
            ("D100,r", "r"),
            ("D100,ui", "ui"),
            ("D100,dw", "dw"),
            ("D100, b", "b"),
            (" D100 , w ", "w"),
        ],
    )
    def test_data_types(self, addr, dt):
        assert _make_driver()._parse_address(addr)[2] == dt

    def test_invalid_address(self):
        with pytest.raises(ValueError, match="无效的FINS地址"):
            _make_driver()._parse_address("X100")

    def test_invalid_offset(self):
        with pytest.raises(ValueError, match="无效的FINS地址偏移量"):
            _make_driver()._parse_address("Dabc")

    def test_offset_out_of_range(self):
        with pytest.raises(ValueError, match="超出范围"):
            _make_driver()._parse_address(f"D{0xFFFFFF + 1}")


class TestValidateWriteValue:
    @pytest.mark.parametrize(
        "val,dt,ok",
        [
            (0, "b", True),
            (1, "b", True),
            (True, "b", True),
            (2, "b", False),
            (0, "w", True),
            (65535, "w", True),
            (65536, "w", False),
            ("abc", "w", False),
            (30000, "ui", True),
            (4294967295, "dw", True),
            (4294967296, "dw", False),
            (-32768, "i", True),
            (32767, "i", True),
            (32768, "i", False),
            (3.14, "float", True),
            (float("nan"), "float", False),
            (float("inf"), "float", False),
            (2.718, "r", True),
            (float("nan"), "r", False),
            (42, "unknown", True),
        ],
    )
    def test_validate(self, val, dt, ok):
        result, _ = _make_driver()._validate_write_value(val, dt)
        assert result is ok


class TestGetFinsAreaCode:
    @pytest.mark.parametrize(
        "area,code",
        [
            ("d", 0x82),
            ("e", 0xA0),
            ("v", 0xA2),
            ("c", 0xB0),
            ("w", 0xB4),
            ("h", 0xB8),
            ("tk", 0x18),
            ("cs", 0x30),
            ("ir", 0xDC),
            ("dr", 0xBC),
            ("cf", 0x28),
        ],
    )
    def test_areas(self, area, code):
        assert _make_driver()._get_fins_area_code(area) == code

    def test_unknown(self):
        assert _make_driver()._get_fins_area_code("zzz") is None


class TestFilterNanInf:
    @pytest.mark.parametrize(
        "val,exp",
        [(3.14, 3.14), (float("nan"), None), (float("inf"), None), (float("-inf"), None), (42, 42), ("hello", "hello")],
    )
    def test_filter(self, val, exp):
        assert _make_driver()._filter_nan_inf(val, "D100") == exp


class TestDetectFrozenValue:
    def test_disabled(self):
        d = _make_driver()
        d._global_frozen_threshold = 0
        assert d._detect_frozen_value("D100", 42) is False

    def test_not_frozen(self):
        d = _make_driver()
        d._global_frozen_threshold = 3
        d._set_last_value("D100", 10)
        assert d._detect_frozen_value("D100", 20) is False

    def test_frozen_at_threshold(self):
        d = _make_driver()
        d._global_frozen_threshold = 3
        d._set_last_value("D100", 42)
        assert d._detect_frozen_value("D100", 42) is False
        assert d._detect_frozen_value("D100", 42) is False
        assert d._detect_frozen_value("D100", 42) is True

    def test_counter_reset_on_change(self):
        d = _make_driver()
        d._global_frozen_threshold = 3
        d._set_last_value("D100", 42)
        d._detect_frozen_value("D100", 42)
        d._detect_frozen_value("D100", 99)
        assert d._frozen_counters["D100"] == 0


class TestCheckRateOfChange:
    def test_disabled(self):
        d = _make_driver()
        d._global_rate_limit = 0
        assert d._check_rate_of_change("D100", 100) is True

    def test_first_read(self):
        d = _make_driver()
        d._global_rate_limit = 100
        assert d._check_rate_of_change("D100", 100) is True

    def test_exceeded(self):
        d = _make_driver()
        d._global_rate_limit = 10
        d._set_last_value("D100", 50)
        d._last_read_ts["D100"] = time.monotonic() - 0.1
        assert d._check_rate_of_change("D100", 100) is False


class TestCalcBackoffDelay:
    def test_base_delay(self):
        d = _make_driver()
        d._reconnect_attempt = 0
        delay = d._calc_backoff_delay()
        assert d._RECONNECT_BASE_DELAY <= delay <= d._RECONNECT_BASE_DELAY + d._JITTER_MAX_MS / 1000

    def test_capped(self):
        d = _make_driver()
        d._reconnect_attempt = 20
        assert d._calc_backoff_delay() <= d._RECONNECT_MAX_DELAY + d._JITTER_MAX_MS / 1000


class TestGetActiveEndpoint:
    def test_primary(self):
        d = _make_driver()
        d._primary_ip = "10.0.0.1"
        d._primary_port = 9600
        assert d._get_active_endpoint() == ("10.0.0.1", 9600)

    def test_backup(self):
        d = _make_driver()
        d._backup_ip = "10.0.0.2"
        d._backup_port = 9601
        d._using_backup = True
        assert d._get_active_endpoint() == ("10.0.0.2", 9601)


class TestPointConfig:
    def test_existing(self):
        d = _make_driver()
        d._point_configs = {"D100": {"deadband": 5}}
        assert d._get_point_config("D100") == {"deadband": 5}
        assert d._get_point_config("D999") == {}
        assert d._get_point_deadband("D100") == 5

    def test_global_fallback(self):
        d = _make_driver()
        d._config = {"deadband": 10}
        assert d._get_point_deadband("D100") == 10

    def test_scaling_clamp(self):
        d = _make_driver()
        d._point_configs = {"D100": {"scaling": {"ratio": 2}, "clamp": {"min": 0}}}
        assert d._get_point_scaling("D100") == {"ratio": 2}
        assert d._get_point_clamp("D100") == {"min": 0}

    def test_frozen_and_rate(self):
        d = _make_driver()
        d._global_frozen_threshold = 3
        d._global_rate_limit = 100
        assert d._get_point_frozen_threshold("D100") == 3
        assert d._get_point_rate_limit("D100") == 100
        d._point_configs = {"D100": {"frozen_threshold": 5, "rate_of_change_limit": 50}}
        assert d._get_point_frozen_threshold("D100") == 5
        assert d._get_point_rate_limit("D100") == 50


class TestSetLastValue:
    def test_lru_eviction(self):
        d = _make_driver()
        d._MAX_LAST_VALUES = 3
        for i in range(4):
            d._set_last_value(f"D{i}", i)
        assert "D0" not in d._last_values and "D3" in d._last_values

    def test_move_to_end_on_update(self):
        d = _make_driver()
        d._MAX_LAST_VALUES = 3
        d._set_last_value("D1", 1)
        d._set_last_value("D2", 2)
        d._set_last_value("D3", 3)
        d._set_last_value("D1", 10)
        d._set_last_value("D4", 4)
        assert "D1" in d._last_values and "D2" not in d._last_values


class TestPointHealthStats:
    def test_record_success(self):
        d = _make_driver()
        d._record_point_success("D100", 5.0)
        s = d._point_stats["D100"]
        assert s.success_count == 1 and s.consecutive_fails == 0 and s.avg_latency_ms == 5.0

    def test_record_failure(self):
        d = _make_driver()
        d._record_point_failure("D100")
        s = d._point_stats["D100"]
        assert s.fail_count == 1 and s.consecutive_fails == 1

    def test_success_resets_fails(self):
        d = _make_driver()
        d._record_point_failure("D100")
        d._record_point_failure("D100")
        d._record_point_success("D100", 1.0)
        assert d._point_stats["D100"].consecutive_fails == 0

    def test_lru_eviction(self):
        d = _make_driver()
        d._MAX_DICT_SIZE = 2
        d._record_point_success("D1", 1)
        d._record_point_success("D2", 2)
        d._record_point_success("D3", 3)
        assert "D1" not in d._point_stats and "D3" in d._point_stats

    def test_check_degradation_no_stats(self):
        d = _make_driver()
        d._check_degradation()
        assert d._degraded is False

    def test_check_degradation_degraded(self):
        d = _make_driver()
        for _ in range(8):
            d._record_point_failure("D100")
        for _ in range(2):
            d._record_point_success("D100", 1.0)
        d._check_degradation()
        assert d._degraded is True and d._degraded_interval_ms > 0

    def test_check_degradation_recovered(self):
        d = _make_driver()
        d._degraded = True
        for _ in range(9):
            d._record_point_success("D100", 1.0)
        d._record_point_failure("D100")
        d._check_degradation()
        assert d._degraded is False and d._degraded_interval_ms == 0

    def test_get_point_health(self):
        d = _make_driver()
        d._record_point_success("D100", 5.0)
        h = d.get_point_health("D100")
        assert h["success_count"] == 1 and h["success_rate"] == 1.0
        h2 = d.get_point_health("D999")
        assert h2["success_count"] == 0 and h2["success_rate"] == 1.0

    def test_get_point_stats(self):
        d = _make_driver()
        d._record_point_success("D100", 5.0)
        s = d.get_point_stats("dev1", "D100")
        assert s is not None and s["current_quality"] == "good"
        assert d.get_point_stats("dev1", "D999") is None


class TestCheckWriteRate:
    def test_disabled(self):
        d = _make_driver()
        d._write_rate_limit_ms = 0
        assert d._check_write_rate("D100") == (True, 0.0)

    def test_allowed(self):
        d = _make_driver()
        d._write_rate_limit_ms = 500
        d._last_write_ts["D100"] = 0.0
        assert d._check_write_rate("D100")[0] is True

    def test_rate_limited(self):
        d = _make_driver()
        d._write_rate_limit_ms = 500
        d._last_write_ts["D100"] = time.monotonic()
        ok, wait = d._check_write_rate("D100")
        assert ok is False and wait > 0


class TestAuditWrite:
    def test_disabled(self):
        d = _make_driver()
        d._write_audit_enabled = False
        d._audit_write("dev1", "D100", "d", 100, 1, 2, "ok")
        assert len(d._audit_log) == 0

    def test_enabled(self):
        d = _make_driver()
        d._audit_write("dev1", "D100", "d", 100, 1, 2, "ok")
        assert len(d._audit_log) == 1
        e = d._audit_log[0]
        assert e.device_id == "dev1" and e.result == "ok"

    def test_with_fins_code(self):
        d = _make_driver()
        d._audit_write("dev1", "D100", "d", 100, 1, 2, "fins_error", fins_code=0x1101)
        assert d._audit_log[0].fins_code == "0x1101"

    def test_get_audit_log(self):
        d = _make_driver()
        d._audit_write("dev1", "D100", "d", 100, 1, 2, "ok")
        d._audit_write("dev1", "D200", "d", 200, 3, 4, "error")
        log = d.get_audit_log()
        assert len(log) == 2 and log[0]["result"] == "ok"
        assert len(d.get_audit_log(limit=1)) == 1
        assert _make_driver().get_audit_log() == []


class TestMergeAdjacentWrites:
    def test_single(self):
        assert _make_driver()._merge_adjacent_writes([("D100", 1)]) == [("D100", 1, False)]

    def test_adjacent_merged(self):
        r = _make_driver()._merge_adjacent_writes([("D100", 1), ("D101", 2), ("D102", 3)])
        assert len(r) == 3 and all(x[2] for x in r)

    def test_non_adjacent(self):
        r = _make_driver()._merge_adjacent_writes([("D100", 1), ("D200", 2)])
        assert len(r) == 2 and all(not x[2] for x in r)

    def test_invalid_address(self):
        r = _make_driver()._merge_adjacent_writes([("X100", 1), ("D100", 2)])
        assert len(r) == 2 and r[0][2] is False


class TestStateManagement:
    def test_set_fins_state(self):
        d = _make_driver()
        d._set_fins_state(FinsConnState.CONNECTED, "dev1", "test")
        assert d._conn_state == FinsConnState.CONNECTED
        d._set_fins_state(FinsConnState.DEGRADED)
        assert d._conn_state == FinsConnState.DEGRADED

    def test_set_network_online(self):
        d = _make_driver()
        d._network_online = False
        d.set_network_online(True)
        assert d._network_online is True

    def test_set_network_online_with_sync(self):
        d = _make_driver()
        d._offline_sync = MagicMock()
        d.set_network_online(False)
        assert d._network_online is False
        d._offline_sync.set_online.assert_called_once_with(False)

    def test_set_user_role(self):
        d = _make_driver()
        d.set_user_role("admin")
        assert d._current_user_role == "admin"


class TestDeviceManagement:
    async def test_add_device(self):
        d = _make_driver()
        pts = [
            {"name": "temp", "address": "D100", "deadband": 1},
            {"name": "press", "address": "D200", "scaling": {"ratio": 2}},
        ]
        await d.add_device("dev1", {"host": "10.0.0.1"}, pts)
        assert "dev1" in d._devices
        assert d._point_configs["D100"] == {"deadband": 1}
        assert d._point_configs["D200"] == {"scaling": {"ratio": 2}}

    async def test_add_device_empty(self):
        d = _make_driver()
        await d.add_device("dev1", {"host": "10.0.0.1"})
        assert "dev1" in d._devices

    def test_remove_device(self):
        d = _make_driver()
        d._devices["dev1"] = {"config": {}, "points": {}}
        d._device_points["dev1"] = [{"name": "temp", "address": "D100"}]
        d._point_configs["D100"] = {"deadband": 1}
        d._health_stats["dev1"] = DriverHealthStats(device_id="dev1")
        d.remove_device("dev1")
        assert "dev1" not in d._devices and "D100" not in d._point_configs
        assert "dev1" not in d._health_stats

    async def test_health_check(self):
        d = _make_driver()
        d._running = False
        assert await d.health_check("dev1") is False
        d._running = True
        d._client = None
        assert await d.health_check("dev1") is False
        d._client = _mkclient()
        assert await d.health_check("dev1") is True
        c = MagicMock()
        c.fins_socket = None
        d._client = c
        assert await d.health_check("dev1") is False


class TestReadPoint:
    def test_normal_success(self):
        d = _make_driver()
        d._is_direct_mode = False
        c = _mkclient()
        c.read.return_value = 42
        d._client = c
        assert d._read_point("D100") == 42

    def test_normal_no_client(self):
        d = _make_driver()
        d._is_direct_mode = False
        d._client = None
        with pytest.raises(ConnectionError):
            d._read_point("D100")

    def test_direct_success(self):
        d = _make_driver()
        d._is_direct_mode = True
        d._dest_node = 1
        d._unit_no = 1
        d._source_node = 0
        d._client = _mkclient()
        d._fins_tcp_request = MagicMock(return_value=42)
        assert d._read_point("D100") == 42

    def test_direct_no_client(self):
        d = _make_driver()
        d._is_direct_mode = True
        d._dest_node = 1
        d._unit_no = 1
        d._source_node = 0
        d._client = None
        with pytest.raises(ConnectionError, match="not connected"):
            d._read_point("D100")

    def test_direct_fallback(self):
        d = _make_driver()
        d._is_direct_mode = True
        d._dest_node = 1
        d._unit_no = 1
        d._source_node = 0
        c = _mkclient()
        c.read.return_value = 99
        d._client = c
        d._fins_tcp_request = MagicMock(side_effect=RuntimeError("err"))
        assert d._read_point("D100") == 99


class TestReadPointAsync:
    async def test_success(self):
        d = _make_driver()
        d._config = {"host": "10.0.0.1"}
        with patch.object(d, "_run_in_thread", new_callable=AsyncMock, return_value=42):
            assert await d._read_point_async("D100") == 42

    async def test_fins_error(self):
        d = _make_driver()
        d._config = {"host": "10.0.0.1"}
        err = FinsResponseError(0x0101, FinsDriverErrors.FINS_ILLEGAL_AREA, "test")
        with patch.object(d, "_run_in_thread", new_callable=AsyncMock, side_effect=err):
            r = await d._read_point_async("D100")
        assert isinstance(r, PointValue) and r.quality == "bad"

    async def test_exception(self):
        d = _make_driver()
        d._config = {"host": "10.0.0.1"}
        with patch.object(d, "_run_in_thread", new_callable=AsyncMock, side_effect=RuntimeError("err")):
            r = await d._read_point_async("D100")
        assert isinstance(r, PointValue) and r.quality == "bad"


class TestReadPointsBatch:
    async def test_batch_success(self):
        d = _make_driver()
        with patch.object(d, "_read_point_async", new_callable=AsyncMock) as m:
            m.side_effect = [10, 20, 30]
            r = await d._read_points_batch(["D100", "D200", "D300"])
        assert r["D100"].value == 10 and r["D200"].value == 20

    async def test_batch_with_exception(self):
        d = _make_driver()
        with patch.object(d, "_read_point_async", new_callable=AsyncMock) as m:
            m.side_effect = [10, RuntimeError("err"), 30]
            r = await d._read_points_batch(["D100", "D200", "D300"])
        assert r["D100"].value == 10 and r["D200"].quality == "bad"

    async def test_sequential(self):
        d = _make_driver()
        with patch.object(d, "_read_point_async", new_callable=AsyncMock) as m:
            m.side_effect = [10, RuntimeError("err")]
            r = await d._read_points_sequential(["D100", "D200"])
        assert r["D100"].value == 10 and r["D200"].quality == "bad"

    async def test_fallback_success(self):
        d = _make_driver()
        with patch.object(d, "_read_points_batch", new_callable=AsyncMock) as m:
            m.return_value = {"D100": PointValue(value=42, quality="good", timestamp=datetime.now(UTC))}
            r = await d._read_points_batch_with_fallback(["D100"])
        assert r["D100"].value == 42

    async def test_fallback_split(self):
        d = _make_driver()
        d._BATCH_SPLIT_MIN = 2
        call_count = 0

        async def mock_batch(pts):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("batch failed")
            return {p: PointValue(value=1, quality="good", timestamp=datetime.now(UTC)) for p in pts}

        with patch.object(d, "_read_points_batch", side_effect=mock_batch):
            with patch.object(
                d,
                "_read_point_async",
                new_callable=AsyncMock,
                return_value=PointValue(value=5, quality="good", timestamp=datetime.now(UTC)),
            ):
                r = await d._read_points_batch_with_fallback(["D100", "D200", "D300", "D400"])
        assert len(r) == 4

    async def test_fallback_max_depth(self):
        d = _make_driver()
        d._BATCH_SPLIT_MIN = 2
        with patch.object(d, "_read_points_batch", new_callable=AsyncMock, side_effect=RuntimeError("fail")):
            with patch.object(d, "_read_point_async", new_callable=AsyncMock, side_effect=RuntimeError("fail")):
                r = await d._read_points_batch_with_fallback(["D100", "D200"], _max_depth=0)
        assert all(pv.quality == "bad" for pv in r.values())


class TestReadPoints:
    async def test_not_running(self):
        d = _make_driver()
        d._running = False
        d._client = None
        d._config = {"host": "10.0.0.1"}
        with patch.object(d, "_handle_connection_failure", new_callable=AsyncMock, return_value=False):
            r = await d.read_points("dev1", ["D100"])
        assert r["D100"].quality == "bad"

    async def test_success(self):
        d = _make_driver()
        d._running = True
        d._client = _mkclient()
        d._config = {"host": "10.0.0.1", "batch_size": 10}
        d._edge_rule_engine = None
        d._ts_store = None
        pv = PointValue(value=42, quality="good", timestamp=datetime.now(UTC), latency_ms=1.0)
        with patch.object(d, "_read_points_batch_with_fallback", new_callable=AsyncMock, return_value={"D100": pv}):
            with patch.object(d, "_record_read_success", new_callable=AsyncMock):
                r = await d.read_points("dev1", ["D100"])
        assert r["D100"].value == 42 and r["D100"].quality == "good"

    async def test_nan_filtering(self):
        d = _make_driver()
        d._running = True
        d._client = _mkclient()
        d._config = {"host": "10.0.0.1", "batch_size": 10}
        d._edge_rule_engine = None
        d._ts_store = None
        pv = PointValue(value=float("nan"), quality="good", timestamp=datetime.now(UTC))
        with patch.object(d, "_read_points_batch_with_fallback", new_callable=AsyncMock, return_value={"D100": pv}):
            with patch.object(d, "_record_read_failure"):
                r = await d.read_points("dev1", ["D100"])
        assert r["D100"].quality == "bad"


class TestWritePoint:
    async def test_not_running(self):
        d = _make_driver()
        d._running = False
        d._client = None
        with patch.object(d, "_handle_connection_failure", new_callable=AsyncMock, return_value=False):
            assert await d.write_point("dev1", "D100", 1) is False

    async def test_invalid_address(self):
        d = _make_driver()
        d._running = True
        d._client = _mkclient()
        assert await d.write_point("dev1", "X100", 1) is False
        assert d._audit_log[0].result == "rejected"

    async def test_invalid_value(self):
        d = _make_driver()
        d._running = True
        d._client = _mkclient()
        assert await d.write_point("dev1", "D100", 99999) is False
        assert d._audit_log[0].result == "rejected"

    async def test_rate_limited(self):
        d = _make_driver()
        d._running = True
        d._client = _mkclient()
        d._write_rate_limit_ms = 500
        d._last_write_ts["D100"] = time.monotonic()
        assert await d.write_point("dev1", "D100", 1) is False
        assert d._audit_log[0].result == "rate_limited"

    async def test_write_success_normal(self):
        d = _make_driver()
        d._running = True
        d._client = _mkclient()
        d._is_direct_mode = False
        d._write_verify_enabled = False
        with patch.object(d, "_run_in_thread", new_callable=AsyncMock):
            with patch.object(d, "_record_write_success"):
                assert await d.write_point("dev1", "D100", 42) is True
        assert d._audit_log[0].result == "ok"

    async def test_write_success_direct(self):
        d = _make_driver()
        d._running = True
        d._client = _mkclient()
        d._is_direct_mode = True
        d._dest_node = 1
        d._unit_no = 1
        d._source_node = 0
        d._write_verify_enabled = False
        with patch.object(d, "_fins_tcp_request", new_callable=AsyncMock):
            with patch.object(d, "_record_write_success"):
                assert await d.write_point("dev1", "D100", 42) is True

    async def test_write_timeout(self):
        d = _make_driver()
        d._running = True
        d._client = _mkclient()
        d._is_direct_mode = False
        d._write_verify_enabled = False
        with patch.object(d, "_run_in_thread", new_callable=AsyncMock, side_effect=TimeoutError()):
            with patch.object(d, "_record_write_failure"):
                assert await d.write_point("dev1", "D100", 42) is False
        assert d._audit_log[0].result == "timeout"

    async def test_write_fins_error(self):
        d = _make_driver()
        d._running = True
        d._client = _mkclient()
        d._is_direct_mode = False
        d._write_verify_enabled = False
        err = FinsWriteError(0x1101, FinsDriverErrors.WRITE_PROTECTED_AREA, "protected")
        with patch.object(d, "_run_in_thread", new_callable=AsyncMock, side_effect=err):
            with patch.object(d, "_record_write_failure"):
                assert await d.write_point("dev1", "D100", 42) is False
        assert d._audit_log[0].result == "fins_error"


class TestBatchWritePoints:
    async def test_not_running(self):
        d = _make_driver()
        d._running = False
        d._client = None
        with patch.object(d, "_handle_connection_failure", new_callable=AsyncMock, return_value=False):
            r = await d.batch_write_points("dev1", [("D100", 1), ("D200", 2)])
        assert r == {"D100": False, "D200": False}

    async def test_success(self):
        d = _make_driver()
        d._running = True
        d._client = _mkclient()
        d._is_direct_mode = False
        d._write_verify_enabled = False
        with patch.object(d, "_run_in_thread", new_callable=AsyncMock):
            with patch.object(d, "_record_write_success"):
                r = await d.batch_write_points("dev1", [("D100", 1), ("D200", 2)])
        assert r["D100"] is True and r["D200"] is True


class TestFinsTcpRequestInner:
    @pytest.mark.parametrize(
        "dt,data,exp",
        [
            ("w", struct.pack(">H", 12345), 12345),
            ("b", bytes([0x01]), 1),
            ("dw", struct.pack(">I", 100000), 100000),
            ("float", struct.pack(">f", 3.14), 3.14),
            ("i", struct.pack(">h", -100), -100),
        ],
    )
    def test_success(self, dt, data, exp):
        d = _make_driver()
        d._timeout = 5.0
        d._max_response_size = 65536
        d._primary_ip = "10.0.0.1"
        s = _mksock(_ok_resp(data))
        d._client = MagicMock()
        d._client.fins_socket = s
        result = d._fins_tcp_request_inner(bytes(12), dt)
        if dt == "float":
            assert abs(result - exp) < 0.001
        else:
            assert result == exp

    def test_no_socket(self):
        d = _make_driver()
        c = MagicMock()
        c.fins_socket = None
        d._client = c
        with pytest.raises(RuntimeError, match="socket not available"):
            d._fins_tcp_request_inner(b"\x00" * 12, "w")

    def test_invalid_header(self):
        d = _make_driver()
        d._timeout = 5.0
        d._max_response_size = 65536
        d._primary_ip = "10.0.0.1"
        s = MagicMock()
        s.settimeout = MagicMock()
        s.send = MagicMock()
        s.recv = MagicMock(side_effect=[b"XXXX" + struct.pack(">I", 12), bytes(12)])
        d._client = MagicMock()
        d._client.fins_socket = s
        with pytest.raises(RuntimeError):
            d._fins_tcp_request_inner(bytes(12), "w")

    def test_response_too_large(self):
        d = _make_driver()
        d._timeout = 5.0
        d._max_response_size = 100
        d._primary_ip = "10.0.0.1"
        s = MagicMock()
        s.settimeout = MagicMock()
        s.send = MagicMock()
        s.recv = MagicMock(side_effect=[b"FINS" + struct.pack(">I", 200)])
        d._client = MagicMock()
        d._client.fins_socket = s
        with pytest.raises(ValueError, match="too large"):
            d._fins_tcp_request_inner(bytes(12), "w")

    def test_fins_error_code(self):
        d = _make_driver()
        d._timeout = 5.0
        d._max_response_size = 65536
        d._primary_ip = "10.0.0.1"
        resp = bytes(10) + struct.pack(">H", 0x0101) + bytes(2)
        s = _mksock(resp)
        d._client = MagicMock()
        d._client.fins_socket = s
        with pytest.raises(FinsResponseError) as ei:
            d._fins_tcp_request_inner(bytes(12), "w")
        assert ei.value.fins_code == 0x0101

    def test_write_fins_error(self):
        d = _make_driver()
        d._timeout = 5.0
        d._max_response_size = 65536
        d._primary_ip = "10.0.0.1"
        resp = bytes(10) + struct.pack(">H", 0x1101) + bytes(2)
        s = _mksock(resp)
        d._client = MagicMock()
        d._client.fins_socket = s
        cmd = bytes(10) + bytes([0x01, 0x02]) + bytes(2)
        with pytest.raises(FinsWriteError) as ei:
            d._fins_tcp_request_inner(cmd, "w")
        assert ei.value.fins_code == 0x1101


class TestDoConnect:
    async def test_tcp(self):
        d = _make_driver()
        d._config = {"transport": "tcp", "timeout": 5}
        mc = MagicMock()
        mc.close = MagicMock()
        with patch("fins.tcp.TCPFinsConnection", return_value=mc):
            with patch.object(d, "_run_in_thread", new_callable=AsyncMock):
                d._wrap_udp_retransmission = MagicMock()
                await d._do_connect("10.0.0.1", 9600)
        assert d._client is mc

    async def test_udp(self):
        d = _make_driver()
        d._config = {"transport": "udp", "timeout": 5}
        mc = MagicMock()
        mc.close = MagicMock()
        with patch("fins.udp.UDPFinsConnection", return_value=mc):
            with patch.object(d, "_run_in_thread", new_callable=AsyncMock):
                d._wrap_udp_retransmission = MagicMock()
                await d._do_connect("10.0.0.1", 9600)
        assert d._client is mc
        d._wrap_udp_retransmission.assert_called_once_with(mc)

    async def test_failure_closes(self):
        d = _make_driver()
        d._config = {"transport": "tcp", "timeout": 5}
        mc = MagicMock()
        mc.close = MagicMock()
        with patch("fins.tcp.TCPFinsConnection", return_value=mc):
            with patch.object(d, "_run_in_thread", new_callable=AsyncMock, side_effect=ConnectionRefusedError):
                with pytest.raises(ConnectionRefusedError):
                    await d._do_connect("10.0.0.1", 9600)
        mc.close.assert_called_once()


class TestFinsNodeHandshakeSync:
    def test_no_client(self):
        assert _make_driver()._fins_node_handshake_sync(None) is False

    def test_no_socket(self):
        d = _make_driver()
        c = MagicMock()
        c.fins_socket = None
        assert d._fins_node_handshake_sync(c) is False

    def test_success(self):
        d = _make_driver()
        d._network_no = 0
        d._dest_node = 0
        d._unit_no = 0
        d._source_node = 0
        resp = bytes(6) + bytes([0x00, 0x01]) + bytes(2) + bytes([0x05, 0x01]) + bytes(4)
        s = MagicMock()
        s.settimeout = MagicMock()
        s.send = MagicMock()
        s.close = MagicMock()
        header = b"FINS" + struct.pack(">I", len(resp))
        s.recv = MagicMock(side_effect=[header, resp])
        c = MagicMock()
        c.fins_socket = s
        assert d._fins_node_handshake_sync(c) is True
        assert d._dest_node == 0x01

    def test_invalid_header(self):
        d = _make_driver()
        d._network_no = 0
        d._dest_node = 0
        d._unit_no = 0
        d._source_node = 0
        s = MagicMock()
        s.settimeout = MagicMock()
        s.send = MagicMock()
        s.close = MagicMock()
        s.recv = MagicMock(return_value=b"XXXX" + bytes(4))
        c = MagicMock()
        c.fins_socket = s
        assert d._fins_node_handshake_sync(c) is False

    def test_no_chunk(self):
        d = _make_driver()
        d._network_no = 0
        d._dest_node = 0
        d._unit_no = 0
        d._source_node = 0
        s = MagicMock()
        s.settimeout = MagicMock()
        s.send = MagicMock()
        s.close = MagicMock()
        s.recv = MagicMock(return_value=b"")
        c = MagicMock()
        c.fins_socket = s
        assert d._fins_node_handshake_sync(c) is False


class TestConnectWithHandshake:
    async def test_do_connect_fails(self):
        d = _make_driver()
        d._primary_ip = "10.0.0.1"
        d._primary_port = 9600
        with patch.object(d, "_do_connect", new_callable=AsyncMock, side_effect=ConnectionRefusedError):
            assert await d._connect_with_handshake("dev1") is False

    async def test_handshake_fails(self):
        d = _make_driver()
        d._primary_ip = "10.0.0.1"
        d._primary_port = 9600
        with patch.object(d, "_do_connect", new_callable=AsyncMock):
            with patch.object(d, "_fins_node_handshake", new_callable=AsyncMock, return_value=False):
                assert await d._connect_with_handshake("dev1") is False

    async def test_success(self):
        d = _make_driver()
        d._primary_ip = "10.0.0.1"
        d._primary_port = 9600
        with patch.object(d, "_do_connect", new_callable=AsyncMock):
            with patch.object(d, "_fins_node_handshake", new_callable=AsyncMock, return_value=True):
                assert await d._connect_with_handshake("dev1") is True
        assert d._running is True and d._active_ip == "10.0.0.1"


class TestStart:
    @pytest.mark.parametrize(
        "cfg,match",
        [
            ({"port": 0}, "port"),
            ({"network_no": 200}, "network_no"),
            ({"source_node": 300}, "source_node"),
            ({"dest_node": 300}, "dest_node"),
            ({"unit_no": 300}, "unit_no"),
        ],
    )
    async def test_invalid_config(self, cfg, match):
        with pytest.raises(ValueError, match=match):
            await _make_driver().start({"host": "10.0.0.1", **cfg})

    async def test_missing_host(self):
        with pytest.raises(ValueError, match="host"):
            await _make_driver().start({})

    async def test_connect_failure(self):
        d = _make_driver()
        with patch.object(d, "_connect_with_handshake", new_callable=AsyncMock, return_value=False):
            with pytest.raises(ConnectionError, match="FINS连接失败"):
                await d.start({"host": "10.0.0.1"})

    async def test_direct_mode_auto(self):
        d = _make_driver()
        with patch.object(d, "_connect_with_handshake", new_callable=AsyncMock, return_value=True):
            with patch.object(d, "_init_edge_rules"):
                with patch("edgelite.drivers.fins.FinsTsStore"):
                    await d.start({"host": "10.0.0.1", "direct_mode": True})
        assert d._is_direct_mode is True and d._dest_node == 1 and d._unit_no == 0x01

    async def test_start_with_backup(self):
        d = _make_driver()

        async def mock_connect(dev):
            d._running = True
            return True

        with patch.object(d, "_connect_with_handshake", side_effect=mock_connect):
            with patch.object(d, "_init_edge_rules"):
                with patch("edgelite.drivers.fins.FinsTsStore") as MTS:
                    ts = MagicMock()
                    ts.start = AsyncMock()
                    ts.stop = AsyncMock()
                    MTS.return_value = ts
                    with patch("edgelite.drivers.fins.FinsOfflineSyncManager") as MS:
                        s = MagicMock()
                        s.start = AsyncMock()
                        s.stop = AsyncMock()
                        MS.return_value = s
                        await d.start({"host": "10.0.0.1", "backup_host": "10.0.0.2"})
        assert d._backup_ip == "10.0.0.2" and d._watchdog_task is not None
        await d.stop()


class TestStop:
    async def test_cleans_up(self):
        d = _make_driver()
        d._running = True
        d._client = _mkclient()
        d._point_stats["D100"] = PointHealthStats()
        d._last_values["D100"] = 42
        d._frozen_counters["D100"] = 1
        d._audit_log.append(WriteAuditEntry("ts", "u", "d", "p", "d", "a", 0, 1, "ok"))
        d._devices["dev1"] = {}
        await d.stop()
        assert d._running is False and len(d._point_stats) == 0
        assert len(d._audit_log) == 0 and len(d._devices) == 0


class TestInitEnterprise:
    def test_init(self):
        d = _make_driver()
        d.init_enterprise()
        assert d._config_version_mgr is not None
        assert d._ota_mgr is not None and d._audit is not None


class TestWaitInFlight:
    async def test_no_pending(self):
        d = _make_driver()
        d._in_flight_requests = 0
        await d._wait_in_flight_requests(1.0)

    async def test_timeout(self):
        d = _make_driver()
        d._in_flight_requests = 5
        with pytest.raises(FailoverInFlightTimeout) as ei:
            await d._wait_in_flight_requests(0.1)
        assert ei.value.pending_count == 5


class TestEnterpriseFeatures:
    def test_check_rbac_invalid_perm(self):
        assert _make_driver().check_rbac("admin", "invalid:permission") is False

    @pytest.mark.parametrize(
        "method,args,exp",
        [
            ("ota_check_update", (MagicMock(),), False),
            ("ota_rollback", (), False),
            ("ota_get_progress", (), {}),
            ("ota_get_history", (), []),
            ("get_audit_recent", (), []),
            ("get_audit_by_device", ("dev1",), []),
            ("get_audit_by_action", ("write",), []),
            ("export_audit_csv", (), ""),
            ("get_audit_stats", (), {}),
            ("get_edge_rules", (), []),
        ],
    )
    def test_no_mgr(self, method, args, exp):
        assert getattr(_make_driver(), method)(*args) == exp

    @pytest.mark.parametrize(
        "method,args,exp",
        [
            ("save_config_version", ("dev1", {}), 0),
            ("get_config_current", ("dev1",), None),
            ("get_config_versions", ("dev1",), []),
            ("get_config_version_config", ("dev1", 1), None),
            ("rollback_config", ("dev1", 1), None),
            ("force_sync_offline", (), 0),
            ("hot_reload_rules", (), 0),
        ],
    )
    async def test_async_no_mgr(self, method, args, exp):
        assert await getattr(_make_driver(), method)(*args) == exp

    async def test_diff_config_no_mgr(self):
        assert await _make_driver().diff_config_versions("dev1", 1, 2) == {"changes": []}

    async def test_query_ts_no_store(self):
        assert await _make_driver().query_ts("dev1", "D100", 0) == []

    def test_get_persistence_stats(self):
        s = _make_driver().get_persistence_stats()
        assert s["network_online"] is True and s["ts_store"] == {}


class TestWritePointDirectMode:
    @pytest.mark.parametrize("val,dt", [(42, "w"), (1, "b"), (3.14, "float"), (100000, "dw"), (-100, "i")])
    async def test_write(self, val, dt):
        d = _make_driver()
        d._dest_node = 1
        d._unit_no = 1
        d._source_node = 0
        with patch.object(d, "_fins_tcp_request", new_callable=AsyncMock) as m:
            await d._write_point_direct_mode("d", 100, val, dt)
        m.assert_called_once()
        cmd = m.call_args[0][0]
        assert cmd[10] == 0x01 and cmd[11] == 0x02

    async def test_invalid_area(self):
        d = _make_driver()
        with pytest.raises(ValueError, match="不支持区域"):
            await d._write_point_direct_mode("zzz", 100, 42, "w")


class TestReadBackForVerify:
    async def test_success(self):
        d = _make_driver()
        with patch.object(d, "_read_point_async", new_callable=AsyncMock, return_value=42):
            assert await d._read_back_for_verify("D100") == 42

    async def test_exception(self):
        d = _make_driver()
        with patch.object(d, "_read_point_async", new_callable=AsyncMock, side_effect=RuntimeError("err")):
            assert await d._read_back_for_verify("D100") is None


class TestEdgeRuleMethods:
    async def test_evaluate_no_engine(self):
        await _make_driver()._evaluate_edge_rules("dev1", {"D100": 42})

    async def test_edge_write_no_running(self):
        d = _make_driver()
        d._running = False
        d._client = None
        with patch.object(d, "_handle_connection_failure", new_callable=AsyncMock, return_value=False):
            assert await d._edge_write_callback("dev1", "D100", 1) is False

    async def test_edge_mqtt(self):
        await _make_driver()._edge_mqtt_callback("topic", {"k": "v"}, 0, False)


class TestConfigSchema:
    def test_schema(self):
        s = OmronFinsDriver.config_schema
        assert "host" in s["required"]
        fields = {f["name"] for f in s["fields"]}
        for n in [
            "host",
            "port",
            "backup_host",
            "transport",
            "direct_mode",
            "plc_series",
            "deadband",
            "scaling",
            "clamp",
            "frozen_threshold",
            "rate_of_change_limit",
            "write_verify",
            "write_rate_limit_ms",
            "write_audit",
        ]:
            assert n in fields

    def test_plugin_info(self):
        assert OmronFinsDriver.plugin_name == "omron_fins"
        assert "fins" in OmronFinsDriver.supported_protocols
        assert OmronFinsDriver.capabilities.read is True
        assert OmronFinsDriver.capabilities.write is True
