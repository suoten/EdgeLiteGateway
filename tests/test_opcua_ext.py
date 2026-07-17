"""OPC UA driver extended coverage tests.

This file complements tests/test_opcua.py (which only covers quality mapping
helpers) by exercising the OpcUaDriver class methods and the _SubHandler:

- OpcUaPointHealthStats (record_success / record_failure / success_rate)
- _bad_pv helper
- _calc_backoff bounds and growth
- _resolve_complex_type_with_fallback for scalars/bytes/list/dict/objects/depth
- OpcUaDriver state machine (_set_state / _get_state), point health, stale data,
  NaN/Inf, frozen value, rate of change, write rate limit, nested capacity,
  audit write log, write type validation, array bounds, write value closeness,
  cert paths failover, endpoint failover, failover info, collection mode,
  certificate status, point health stats, security policy map, edge rule
  delegation, data persistence delegation, enterprise/audit delegation,
  config version delegation, OTA delegation, RBAC, read_points offline path,
  write_point rejection paths, health_check, discover_devices SSRF guard,
  browse empty client, create_subscription_batch offline path.
- _SubHandler datachange_notification queueing and cancel.

All external I/O (asyncua, cryptography, audit/ts_store/ota/config_version
helpers, event_bus, record_packet) is mocked.
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, "src")

from edgelite.drivers.base import PointValue  # noqa: E402
from edgelite.drivers.opcua import (  # noqa: E402
    _BACKOFF_BASE,
    _FROZEN_COUNT_THRESHOLD,
    _MAX_ARRAY_LENGTH,
    CollectionMode,
    OpcUaConnectionState,
    OpcUaDriver,
    OpcUaPointHealthStats,
    _bad_pv,
    _calc_backoff,
    _resolve_complex_type_with_fallback,
    _SubHandler,
)

# ════════════════════════════════════════════════════════════════════════
# 1. OpcUaPointHealthStats dataclass
# ════════════════════════════════════════════════════════════════════════


class TestPointHealthStats:
    def test_defaults(self):
        ph = OpcUaPointHealthStats()
        assert ph.success_count == 0
        assert ph.fail_count == 0
        assert ph.consecutive_fails == 0
        assert ph.last_value is None
        assert ph.last_timestamp == 0.0
        assert ph.last_publish_at == 0.0
        assert ph.same_value_count == 0
        assert ph.subscription_count == 0

    def test_success_rate_empty_returns_one(self):
        assert OpcUaPointHealthStats().success_rate == 1.0

    def test_success_rate_with_counts(self):
        ph = OpcUaPointHealthStats(success_count=3, fail_count=1)
        assert ph.success_rate == 0.75

    def test_record_success_increments_and_resets_fails(self):
        ph = OpcUaPointHealthStats(fail_count=2, consecutive_fails=2)
        before = time.monotonic()
        ph.record_success()
        assert ph.success_count == 1
        assert ph.consecutive_fails == 0
        assert ph.last_publish_at >= before

    def test_record_failure_increments(self):
        ph = OpcUaPointHealthStats()
        ph.record_failure()
        ph.record_failure()
        assert ph.fail_count == 2
        assert ph.consecutive_fails == 2


# ════════════════════════════════════════════════════════════════════════
# 2. _bad_pv helper
# ════════════════════════════════════════════════════════════════════════


class TestBadPv:
    def test_returns_bad_quality(self):
        pv = _bad_pv("ERR_X")
        assert pv.value is None
        assert pv.quality == "bad"
        assert pv.source == "opcua:ERR_X"
        assert isinstance(pv.timestamp, datetime)


# ════════════════════════════════════════════════════════════════════════
# 3. _calc_backoff
# ════════════════════════════════════════════════════════════════════════


class TestCalcBackoff:
    def test_first_fail_uses_base(self):
        # fail_count=1 -> base*2^0 = base, plus jitter in [0,5)
        with patch("edgelite.drivers.opcua.random.uniform", return_value=0.0):
            delay = _calc_backoff(1)
        assert delay == _BACKOFF_BASE

    def test_grows_with_fail_count(self):
        with patch("edgelite.drivers.opcua.random.uniform", return_value=0.0):
            d1 = _calc_backoff(1)
            d5 = _calc_backoff(5)
        assert d5 > d1

    def test_capped_at_300(self):
        # huge fail_count -> base*2^7=640 capped to BACKOFF_MAX=600, +jitter capped at 300
        with patch("edgelite.drivers.opcua.random.uniform", return_value=0.0):
            delay = _calc_backoff(100)
        assert delay <= 300.0

    def test_includes_jitter(self):
        with patch("edgelite.drivers.opcua.random.uniform", return_value=2.5):
            delay = _calc_backoff(1)
        assert delay == _BACKOFF_BASE + 2.5


# ════════════════════════════════════════════════════════════════════════
# 4. _resolve_complex_type_with_fallback
# ════════════════════════════════════════════════════════════════════════


class TestResolveComplexType:
    def test_scalar_int(self):
        v, q = _resolve_complex_type_with_fallback(42)
        assert v == 42 and q == "good"

    def test_scalar_float(self):
        v, q = _resolve_complex_type_with_fallback(3.14)
        assert v == 3.14 and q == "good"

    def test_scalar_str(self):
        v, q = _resolve_complex_type_with_fallback("hello")
        assert v == "hello" and q == "good"

    def test_scalar_bool(self):
        v, q = _resolve_complex_type_with_fallback(True)
        assert v is True and q == "good"

    def test_bytes_returns_hex_uncertain(self):
        v, q = _resolve_complex_type_with_fallback(b"\xde\xad")
        assert v == "dead" and q == "uncertain"

    def test_bytearray_returns_hex_uncertain(self):
        v, q = _resolve_complex_type_with_fallback(bytearray(b"\x01\x02"))
        assert v == "0102" and q == "uncertain"

    def test_list_short(self):
        v, q = _resolve_complex_type_with_fallback([1, 2, 3])
        assert v == [1, 2, 3] and q == "good"

    def test_list_with_uncertain_element(self):
        v, q = _resolve_complex_type_with_fallback([1, b"\xab"])
        assert v == [1, "ab"] and q == "uncertain"

    def test_list_truncated_over_max(self):
        big = list(range(_MAX_ARRAY_LENGTH + 50))
        v, q = _resolve_complex_type_with_fallback(big)
        assert len(v) == _MAX_ARRAY_LENGTH
        assert q == "uncertain"

    def test_tuple(self):
        v, q = _resolve_complex_type_with_fallback((1, 2))
        assert v == [1, 2] and q == "good"

    def test_dict(self):
        v, q = _resolve_complex_type_with_fallback({"a": 1, "b": b"\x00"})
        assert v == {"a": 1, "b": "00"} and q == "uncertain"

    def test_object_with_dict(self):
        class Obj:
            def __init__(self):
                self.x = 1
                self.y = b"\xff"

        v, q = _resolve_complex_type_with_fallback(Obj())
        assert v == {"x": 1, "y": "ff"} and q == "uncertain"

    def test_object_with_private_attrs_skipped(self):
        class Obj:
            def __init__(self):
                self._private = 1
                self.public = 2

        v, q = _resolve_complex_type_with_fallback(Obj())
        assert v == {"public": 2} and q == "good"

    def test_depth_limit_returns_bad(self):
        # Nesting beyond 10 -> depth limit hit, innermost returns str + bad,
        # propagates up as list with worst_q="bad".
        nested = 1
        for _ in range(15):
            nested = [nested]
        v, q = _resolve_complex_type_with_fallback(nested)
        assert q == "bad"

    def test_object_empty_dict_falls_back_to_hex(self):
        class HexLike:
            # No instance attributes -> __dict__ is empty -> hex() fallback path
            def hex(self):
                return "abcd"

        obj = HexLike()
        v, q = _resolve_complex_type_with_fallback(obj)
        assert v == "abcd" and q == "uncertain"

    def test_object_empty_dict_no_hex_returns_str_bad(self):
        class Plain:
            pass

        obj = Plain()
        v, q = _resolve_complex_type_with_fallback(obj)
        assert q == "bad"
        assert isinstance(v, str)


# ════════════════════════════════════════════════════════════════════════
# 5. OpcUaDriver state machine and helpers
# ════════════════════════════════════════════════════════════════════════


@pytest.fixture
def driver():
    d = OpcUaDriver()
    d._running = True
    yield d
    # cleanup any stray tasks
    for t in list(d._background_tasks):
        if not t.done():
            t.cancel()


class TestDriverStateAndHelpers:
    def test_get_state_default_disconnected(self, driver):
        assert driver._get_state("dev1") == OpcUaConnectionState.DISCONNECTED

    def test_set_state_and_get_state(self, driver):
        driver._set_state("dev1", OpcUaConnectionState.CONNECTING)
        assert driver._get_state("dev1") == OpcUaConnectionState.CONNECTING

    def test_get_connection_state_returns_string(self, driver):
        driver._set_state("dev1", OpcUaConnectionState.CONNECTED)
        assert driver.get_connection_state("dev1") == "connected"

    def test_get_point_config_found(self, driver):
        driver._device_points["dev1"] = [{"name": "p1", "address": "ns=2;s=P1"}]
        cfg = driver._get_point_config("dev1", "p1")
        assert cfg["address"] == "ns=2;s=P1"

    def test_get_point_config_missing(self, driver):
        driver._device_points["dev1"] = []
        assert driver._get_point_config("dev1", "missing") == {}

    def test_get_effective_point_param_point_level(self, driver):
        driver._device_points["dev1"] = [{"name": "p1", "frozen_count": 5}]
        assert driver._get_effective_point_param("dev1", "p1", "frozen_count") == 5

    def test_get_effective_point_param_device_level(self, driver):
        driver._device_configs["dev1"] = {"frozen_count": 7}
        assert driver._get_effective_point_param("dev1", "p1", "frozen_count") == 7

    def test_get_point_health_creates_entry(self, driver):
        ph = driver._get_point_health("dev1", "p1")
        assert isinstance(ph, OpcUaPointHealthStats)

    def test_check_nan_inf_float(self, driver):
        assert driver._check_nan_inf(float("nan")) is True
        assert driver._check_nan_inf(float("inf")) is True
        assert driver._check_nan_inf(1.0) is False

    def test_check_nan_inf_non_float(self, driver):
        assert driver._check_nan_inf(1) is False
        assert driver._check_nan_inf("x") is False

    def test_check_frozen_value_non_numeric(self, driver):
        assert driver._check_frozen_value("dev1", "p1", "abc") is False

    def test_check_frozen_value_threshold(self, driver):
        # _check_frozen_value reads ph.last_value (set externally by _apply_point_preprocess)
        # and increments same_value_count when value == last_value. The method does NOT
        # update last_value itself, so we set it once before the loop.
        ph = driver._get_point_health("dev1", "p1")
        ph.last_value = 5
        # Calls 1..(threshold-1): same_value_count goes 1..9, all below threshold
        for _ in range(_FROZEN_COUNT_THRESHOLD - 1):
            assert driver._check_frozen_value("dev1", "p1", 5) is False
        # Call threshold: same_value_count == threshold -> frozen
        assert driver._check_frozen_value("dev1", "p1", 5) is True

    def test_check_frozen_value_custom_threshold(self, driver):
        driver._device_points["dev1"] = [{"name": "p1", "frozen_count": 2}]
        ph = driver._get_point_health("dev1", "p1")
        ph.last_value = 9
        assert driver._check_frozen_value("dev1", "p1", 9) is False  # count=1
        assert driver._check_frozen_value("dev1", "p1", 9) is True  # count=2 >= 2

    def test_check_frozen_value_resets_on_change(self, driver):
        ph = driver._get_point_health("dev1", "p1")
        ph.last_value = 5
        driver._check_frozen_value("dev1", "p1", 5)  # count=1
        # different value -> count resets to 0
        ph.last_value = 6
        assert driver._check_frozen_value("dev1", "p1", 6) is False

    def test_check_rate_of_change_no_threshold(self, driver):
        assert driver._check_rate_of_change("dev1", "p1", 100) is False

    def test_check_rate_of_change_non_numeric(self, driver):
        driver._device_configs["dev1"] = {"rate_of_change": 1.0}
        assert driver._check_rate_of_change("dev1", "p1", "x") is False

    def test_check_rate_of_change_first_value(self, driver):
        driver._device_configs["dev1"] = {"rate_of_change": 1.0}
        assert driver._check_rate_of_change("dev1", "p1", 100) is False

    def test_check_rate_of_change_exceeds(self, driver):
        driver._device_configs["dev1"] = {"rate_of_change": 0.001}
        ph = driver._get_point_health("dev1", "p1")
        ph.last_value = 0.0
        ph.last_timestamp = time.monotonic() - 1.0
        # huge change over 1s -> roc huge > 0.001
        assert driver._check_rate_of_change("dev1", "p1", 1000.0) is True

    def test_check_stale_data_no_publish(self, driver):
        assert driver._check_stale_data("dev1", "p1") is False

    def test_check_stale_data_stale(self, driver):
        ph = driver._get_point_health("dev1", "p1")
        ph.last_publish_at = time.monotonic() - 100
        driver._device_configs["dev1"] = {"subscription_interval": 500}
        assert driver._check_stale_data("dev1", "p1") is True


class TestWriteHelpers:
    def test_get_write_type_strategy_point_level(self, driver):
        driver._device_points["dev1"] = [{"name": "p1", "write_type_strategy": "truncate"}]
        assert driver._get_write_type_strategy("dev1", "p1") == "truncate"

    def test_get_write_type_strategy_default_reject(self, driver):
        assert driver._get_write_type_strategy("dev1", "p1") == "reject"

    def test_check_write_rate_limit_first_allowed(self, driver):
        assert driver._check_write_rate_limit("dev1", "p1") is True

    def test_check_write_rate_limit_blocked(self, driver):
        driver._record_write_time("dev1", "p1")
        # immediately after write -> blocked
        assert driver._check_write_rate_limit("dev1", "p1") is False

    def test_record_write_time_updates(self, driver):
        before = time.monotonic()
        driver._record_write_time("dev1", "p1")
        assert driver._write_rate_limits["dev1"]["p1"] >= before

    def test_enforce_nested_capacity_evicts(self, driver):
        nested = {"d": {f"p{i}": i for i in range(5)}}
        driver._enforce_nested_capacity(nested, 2)
        assert sum(len(v) for v in nested.values()) <= 2

    def test_enforce_nested_capacity_noop_when_under(self, driver):
        nested = {"d": {"p1": 1}}
        driver._enforce_nested_capacity(nested, 10)
        assert nested == {"d": {"p1": 1}}

    def test_audit_write_appends_entry(self, driver):
        driver._audit_write("dev1", "p1", "ns=2;s=P1", "Int32", 1, 2, "ok")
        assert len(driver._write_audit_log) == 1
        entry = driver._write_audit_log[0]
        assert entry["device_id"] == "dev1"
        assert entry["point_id"] == "p1"
        assert entry["result"] == "ok"

    def test_get_write_audit_log_filtered(self, driver):
        driver._audit_write("dev1", "p1", "n1", "Int32", 1, 2, "ok")
        driver._audit_write("dev2", "p2", "n2", "Int32", 1, 2, "ok")
        result = driver.get_write_audit_log(device_id="dev1")
        assert len(result) == 1
        assert result[0]["device_id"] == "dev1"

    def test_get_write_audit_log_all(self, driver):
        driver._audit_write("dev1", "p1", "n1", "Int32", 1, 2, "ok")
        driver._audit_write("dev2", "p2", "n2", "Int32", 1, 2, "ok")
        result = driver.get_write_audit_log()
        assert len(result) == 2

    def test_get_write_audit_log_limit(self, driver):
        for i in range(5):
            driver._audit_write("dev1", f"p{i}", f"n{i}", "Int32", 1, 2, "ok")
        result = driver.get_write_audit_log(limit=2)
        assert len(result) == 2

    def test_check_array_bounds_non_list(self, driver):
        v, ok = driver._check_array_bounds("dev1", "p1", "n1", 42)
        assert ok is True and v == 42

    def test_check_array_bounds_within_limit(self, driver):
        v, ok = driver._check_array_bounds("dev1", "p1", "n1", [1, 2, 3])
        assert ok is True and v == [1, 2, 3]

    def test_check_array_bounds_truncate(self, driver):
        driver._device_points["dev1"] = [{"name": "p1", "write_type_strategy": "truncate"}]
        big = list(range(_MAX_ARRAY_LENGTH + 5))
        v, ok = driver._check_array_bounds("dev1", "p1", "n1", big)
        assert ok is True
        assert len(v) == _MAX_ARRAY_LENGTH

    def test_check_array_bounds_reject(self, driver):
        big = list(range(_MAX_ARRAY_LENGTH + 5))
        v, ok = driver._check_array_bounds("dev1", "p1", "n1", big)
        assert ok is False
        assert v == big

    def test_is_write_value_close_float_close(self, driver):
        assert driver._is_write_value_close(1.0000001, 1.0, "Double") is True

    def test_is_write_value_close_float_far(self, driver):
        assert driver._is_write_value_close(2.0, 1.0, "Double") is False

    def test_is_write_value_close_int_equal(self, driver):
        assert driver._is_write_value_close(5, 5, "Int32") is True

    def test_is_write_value_close_list_diff_len(self, driver):
        assert driver._is_write_value_close([1, 2], [1], "Int32") is False

    def test_is_write_value_close_list_equal(self, driver):
        assert driver._is_write_value_close([1, 2], [1, 2], "Int32") is True


class TestValidateWriteType:
    async def test_boolean_bool_ok(self, driver):
        client = MagicMock()
        with patch.object(driver, "_read_node_data_type", return_value="Boolean"):
            v, ok = await driver._validate_write_type(client, "dev1", "p1", "n1", True)
        assert ok is True and v is True

    async def test_boolean_int_rejected_default(self, driver):
        # default strategy is reject -> int to Boolean fails
        client = MagicMock()
        with patch.object(driver, "_read_node_data_type", return_value="Boolean"):
            v, ok = await driver._validate_write_type(client, "dev1", "p1", "n1", 1)
        assert ok is False

    async def test_boolean_truncate_int(self, driver):
        driver._device_points["dev1"] = [{"name": "p1", "write_type_strategy": "truncate"}]
        client = MagicMock()
        with patch.object(driver, "_read_node_data_type", return_value="Boolean"):
            v, ok = await driver._validate_write_type(client, "dev1", "p1", "n1", 1)
        assert ok is True
        assert v is True

    async def test_int_type_int_ok(self, driver):
        client = MagicMock()
        with patch.object(driver, "_read_node_data_type", return_value="Int32"):
            v, ok = await driver._validate_write_type(client, "dev1", "p1", "n1", 42)
        assert ok is True and v == 42

    async def test_int_type_bool_rejected(self, driver):
        client = MagicMock()
        with patch.object(driver, "_read_node_data_type", return_value="Int32"):
            v, ok = await driver._validate_write_type(client, "dev1", "p1", "n1", True)
        assert ok is False

    async def test_int_type_float_rejected(self, driver):
        client = MagicMock()
        with patch.object(driver, "_read_node_data_type", return_value="Int32"):
            v, ok = await driver._validate_write_type(client, "dev1", "p1", "n1", 3.14)
        assert ok is False

    async def test_int_type_float_truncate(self, driver):
        driver._device_points["dev1"] = [{"name": "p1", "write_type_strategy": "truncate"}]
        client = MagicMock()
        with patch.object(driver, "_read_node_data_type", return_value="Int32"):
            v, ok = await driver._validate_write_type(client, "dev1", "p1", "n1", 3.14)
        assert ok is True and v == 3

    async def test_float_type_int_ok(self, driver):
        client = MagicMock()
        with patch.object(driver, "_read_node_data_type", return_value="Double"):
            v, ok = await driver._validate_write_type(client, "dev1", "p1", "n1", 42)
        assert ok is True and v == 42.0

    async def test_float_type_bool_rejected(self, driver):
        client = MagicMock()
        with patch.object(driver, "_read_node_data_type", return_value="Double"):
            v, ok = await driver._validate_write_type(client, "dev1", "p1", "n1", True)
        assert ok is False

    async def test_float_type_str_rejected(self, driver):
        client = MagicMock()
        with patch.object(driver, "_read_node_data_type", return_value="Double"):
            v, ok = await driver._validate_write_type(client, "dev1", "p1", "n1", "x")
        assert ok is False

    async def test_string_type_str_ok(self, driver):
        client = MagicMock()
        with patch.object(driver, "_read_node_data_type", return_value="String"):
            v, ok = await driver._validate_write_type(client, "dev1", "p1", "n1", "hello")
        assert ok is True and v == "hello"

    async def test_string_type_int_truncate(self, driver):
        driver._device_points["dev1"] = [{"name": "p1", "write_type_strategy": "truncate"}]
        client = MagicMock()
        with patch.object(driver, "_read_node_data_type", return_value="String"):
            v, ok = await driver._validate_write_type(client, "dev1", "p1", "n1", 42)
        assert ok is True and v == "42"

    async def test_string_type_int_rejected(self, driver):
        client = MagicMock()
        with patch.object(driver, "_read_node_data_type", return_value="String"):
            v, ok = await driver._validate_write_type(client, "dev1", "p1", "n1", 42)
        assert ok is False

    async def test_bytestring_bytes_ok(self, driver):
        client = MagicMock()
        with patch.object(driver, "_read_node_data_type", return_value="ByteString"):
            v, ok = await driver._validate_write_type(client, "dev1", "p1", "n1", b"\x01\x02")
        assert ok is True and v == b"\x01\x02"

    async def test_bytestring_str_rejected(self, driver):
        client = MagicMock()
        with patch.object(driver, "_read_node_data_type", return_value="ByteString"):
            v, ok = await driver._validate_write_type(client, "dev1", "p1", "n1", "x")
        assert ok is False

    async def test_datetime_str_ok(self, driver):
        client = MagicMock()
        with patch.object(driver, "_read_node_data_type", return_value="DateTime"):
            v, ok = await driver._validate_write_type(client, "dev1", "p1", "n1", "2025-01-01")
        assert ok is True

    async def test_datetime_int_rejected(self, driver):
        client = MagicMock()
        with patch.object(driver, "_read_node_data_type", return_value="DateTime"):
            v, ok = await driver._validate_write_type(client, "dev1", "p1", "n1", 42)
        assert ok is False

    async def test_guid_str_ok(self, driver):
        client = MagicMock()
        with patch.object(driver, "_read_node_data_type", return_value="Guid"):
            v, ok = await driver._validate_write_type(client, "dev1", "p1", "n1", "abc")
        assert ok is True

    async def test_guid_int_rejected(self, driver):
        client = MagicMock()
        with patch.object(driver, "_read_node_data_type", return_value="Guid"):
            v, ok = await driver._validate_write_type(client, "dev1", "p1", "n1", 42)
        assert ok is False

    async def test_unknown_type_rejects(self, driver):
        client = MagicMock()
        with patch.object(driver, "_read_node_data_type", return_value="Unknown"):
            v, ok = await driver._validate_write_type(client, "dev1", "p1", "n1", 42)
        assert ok is False

    async def test_unhandled_type_rejects(self, driver):
        client = MagicMock()
        with patch.object(driver, "_read_node_data_type", return_value="SomeWeirdType"):
            v, ok = await driver._validate_write_type(client, "dev1", "p1", "n1", 42)
        assert ok is False


class TestReadNodeDataType:
    async def test_cached_returns_immediately(self, driver):
        driver._node_data_types["dev1"] = {"n1": "Int32"}
        client = MagicMock()
        result = await driver._read_node_data_type(client, "dev1", "n1")
        assert result == "Int32"
        client.get_node.assert_not_called()

    async def test_builtin_type_id(self, driver):
        client = MagicMock()
        node = MagicMock()
        dt_node_id = MagicMock()
        dt_node_id.Identifier = 6  # Int32
        node.read_data_type = AsyncMock(return_value=dt_node_id)
        client.get_node.return_value = node
        result = await driver._read_node_data_type(client, "dev1", "n1")
        assert result == "Int32"
        assert driver._node_data_types["dev1"]["n1"] == "Int32"

    async def test_non_builtin_identifier_browses_name(self, driver):
        client = MagicMock()
        node = MagicMock()
        dt_node_id = MagicMock()
        dt_node_id.Identifier = "ns=2;s=CustomType"  # not int
        node.read_data_type = AsyncMock(return_value=dt_node_id)
        # Then code does client.get_node(dt_node_id) and read_browse_name
        dt_node = MagicMock()
        browse_name = MagicMock()
        browse_name.Name = "CustomType"
        dt_node.read_browse_name = AsyncMock(return_value=browse_name)
        client.get_node.side_effect = [node, dt_node]
        result = await driver._read_node_data_type(client, "dev1", "n1")
        assert result == "CustomType"

    async def test_exception_returns_unknown(self, driver):
        client = MagicMock()
        node = MagicMock()
        node.read_data_type = AsyncMock(side_effect=RuntimeError("boom"))
        client.get_node.return_value = node
        result = await driver._read_node_data_type(client, "dev1", "n1")
        assert result == "Unknown"


# ════════════════════════════════════════════════════════════════════════
# 6. Failover and cert path helpers
# ════════════════════════════════════════════════════════════════════════


class TestFailoverHelpers:
    def test_get_active_endpoint_default(self, driver):
        driver._device_configs["dev1"] = {"endpoint": "opc.tcp://primary:4840"}
        assert driver._get_active_endpoint("dev1") == "opc.tcp://primary:4840"

    def test_get_active_endpoint_server_url_fallback(self, driver):
        driver._device_configs["dev1"] = {"server_url": "opc.tcp://srv:4840"}
        assert driver._get_active_endpoint("dev1") == "opc.tcp://srv:4840"

    def test_get_active_endpoint_default_localhost(self, driver):
        # empty config
        driver._device_configs["dev1"] = {}
        assert driver._get_active_endpoint("dev1") == "opc.tcp://localhost:4840"

    def test_get_active_endpoint_uses_active(self, driver):
        driver._device_configs["dev1"] = {"endpoint": "opc.tcp://primary:4840"}
        driver._active_endpoints["dev1"] = "opc.tcp://backup:4840"
        assert driver._get_active_endpoint("dev1") == "opc.tcp://backup:4840"

    def test_get_backup_endpoint_none(self, driver):
        driver._device_configs["dev1"] = {}
        assert driver._get_backup_endpoint("dev1") is None

    def test_get_backup_endpoint_present(self, driver):
        driver._device_configs["dev1"] = {"backup_endpoint": "opc.tcp://backup:4840"}
        assert driver._get_backup_endpoint("dev1") == "opc.tcp://backup:4840"

    def test_is_using_backup_false_when_no_active(self, driver):
        driver._device_configs["dev1"] = {"endpoint": "opc.tcp://primary:4840"}
        assert driver._is_using_backup("dev1") is False

    def test_is_using_backup_true(self, driver):
        driver._device_configs["dev1"] = {"endpoint": "opc.tcp://primary:4840"}
        driver._active_endpoints["dev1"] = "opc.tcp://backup:4840"
        assert driver._is_using_backup("dev1") is True

    def test_switch_to_backup_no_backup(self, driver):
        driver._device_configs["dev1"] = {}
        assert driver._switch_to_backup("dev1") is False

    def test_switch_to_backup_success(self, driver):
        driver._device_configs["dev1"] = {"backup_endpoint": "opc.tcp://backup:4840"}
        assert driver._switch_to_backup("dev1") is True
        assert driver._active_endpoints["dev1"] == "opc.tcp://backup:4840"

    def test_revert_to_primary_success(self, driver):
        driver._device_configs["dev1"] = {"endpoint": "opc.tcp://primary:4840"}
        driver._active_endpoints["dev1"] = "opc.tcp://backup:4840"
        assert driver._revert_to_primary("dev1") is True
        assert driver._active_endpoints["dev1"] == "opc.tcp://primary:4840"

    def test_revert_to_primary_no_primary(self, driver):
        driver._device_configs["dev1"] = {}
        assert driver._revert_to_primary("dev1") is False

    async def test_fast_failover_no_backup(self, driver):
        driver._device_configs["dev1"] = {}
        result = await driver._fast_failover("dev1")
        assert result is False

    async def test_fast_failover_success(self, driver):
        driver._device_configs["dev1"] = {
            "endpoint": "opc.tcp://primary:4840",
            "backup_endpoint": "opc.tcp://backup:4840",
        }
        driver._active_endpoints["dev1"] = "opc.tcp://primary:4840"
        result = await driver._fast_failover("dev1")
        assert result is True
        assert driver._active_endpoints["dev1"] == "opc.tcp://backup:4840"
        assert driver._failover_at["dev1"] > 0
        assert "dev1" in driver._session_state  # persisted

    def test_get_failover_info(self, driver):
        driver._device_configs["dev1"] = {
            "endpoint": "opc.tcp://primary:4840",
            "backup_endpoint": "opc.tcp://backup:4840",
        }
        driver._active_endpoints["dev1"] = "opc.tcp://backup:4840"
        driver._failover_at["dev1"] = time.monotonic() - 2.0
        info = driver.get_failover_info("dev1")
        assert info["using_backup"] is True
        assert info["current_endpoint"] == "opc.tcp://backup:4840"
        assert info["primary_endpoint"] == "opc.tcp://primary:4840"
        assert info["backup_endpoint"] == "opc.tcp://backup:4840"
        assert info["failover_elapsed_s"] is not None
        assert info["within_sla"] is True

    def test_get_failover_info_no_failover(self, driver):
        driver._device_configs["dev1"] = {"endpoint": "opc.tcp://primary:4840"}
        info = driver.get_failover_info("dev1")
        assert info["using_backup"] is False
        assert info["failover_elapsed_s"] is None


class TestCertPathHelpers:
    def test_get_effective_cert_paths_from_config(self, driver):
        driver._device_configs["dev1"] = {
            "client_cert_path": "/c.pem",
            "client_key_path": "/k.pem",
            "ca_cert_path": "/ca.pem",
        }
        cert, key, ca = driver._get_effective_cert_paths("dev1")
        assert cert == "/c.pem" and key == "/k.pem" and ca == "/ca.pem"

    def test_get_effective_cert_paths_from_backup(self, driver):
        driver._device_configs["dev1"] = {"client_cert_path": "/orig.pem"}
        driver._backup_cert_paths["dev1"] = {
            "client_cert_path": "/bk.pem",
            "client_key_path": "/bk.pem",
            "ca_cert_path": "/bkca.pem",
        }
        cert, key, ca = driver._get_effective_cert_paths("dev1")
        assert cert == "/bk.pem"

    def test_switch_to_backup_certs_no_backup(self, driver):
        driver._device_configs["dev1"] = {}
        assert driver._switch_to_backup_certs("dev1") is False

    def test_switch_to_backup_certs_success(self, driver):
        driver._device_configs["dev1"] = {
            "client_cert_path": "/orig.pem",
            "client_key_path": "/origk.pem",
            "backup_client_cert_path": "/bk.pem",
            "backup_client_key_path": "/bkk.pem",
            "backup_ca_cert_path": "/bkca.pem",
        }
        assert driver._switch_to_backup_certs("dev1") is True
        assert "dev1" in driver._backup_cert_paths
        assert "dev1" in driver._original_cert_paths

    def test_revert_to_primary_certs_no_primary(self, driver):
        driver._device_configs["dev1"] = {}
        assert driver._revert_to_primary_certs("dev1") is False

    def test_revert_to_primary_certs_success(self, driver):
        driver._device_configs["dev1"] = {"client_cert_path": "/orig.pem"}
        driver._backup_cert_paths["dev1"] = {"client_cert_path": "/bk.pem"}
        assert driver._revert_to_primary_certs("dev1") is True
        assert "dev1" not in driver._backup_cert_paths


# ════════════════════════════════════════════════════════════════════════
# 7. Session state persistence
# ════════════════════════════════════════════════════════════════════════


class TestSessionState:
    def test_persist_session_state_no_subscription(self, driver):
        driver._device_configs["dev1"] = {"endpoint": "opc.tcp://e:4840"}
        driver._device_points["dev1"] = [{"name": "p1", "address": "n1"}]
        driver._persist_session_state("dev1")
        state = driver._session_state["dev1"]
        assert state["endpoint"] == "opc.tcp://e:4840"
        assert state["subscription_id"] is None
        assert state["point_addresses"] == ["n1"]
        assert state["point_names"] == ["p1"]

    def test_persist_session_state_with_subscription(self, driver):
        sub = MagicMock()
        sub.subscription_id = 99
        mi = MagicMock()
        mi.client_handle = 5
        sub.monitored_items_map = {"ns=2;s=P1": mi}
        driver._subscriptions["dev1"] = sub
        driver._device_configs["dev1"] = {"endpoint": "opc.tcp://e:4840"}
        driver._device_points["dev1"] = [{"name": "p1", "address": "ns=2;s=P1"}]
        driver._persist_session_state("dev1")
        state = driver._session_state["dev1"]
        assert state["subscription_id"] == 99
        assert len(state["monitored_items"]) == 1

    def test_restore_session_state_none(self, driver):
        assert driver._restore_session_state("dev1") is None

    def test_restore_session_state_present(self, driver):
        driver._session_state["dev1"] = {"endpoint": "opc.tcp://e:4840"}
        assert driver._restore_session_state("dev1") == {"endpoint": "opc.tcp://e:4840"}


# ════════════════════════════════════════════════════════════════════════
# 8. Collection mode, certificate status, point health stats
# ════════════════════════════════════════════════════════════════════════


class TestMetadataHelpers:
    def test_get_collection_mode_default(self, driver):
        assert driver.get_collection_mode("dev1") == "subscription"

    def test_get_collection_mode_polling(self, driver):
        driver._collection_modes["dev1"] = CollectionMode.POLLING
        assert driver.get_collection_mode("dev1") == "polling"

    def test_get_certificate_status_empty(self, driver):
        assert driver.get_certificate_status() == {}

    def test_get_certificate_status_with_entry(self, driver):
        driver._certificate_status["dev1"] = {"status": "valid"}
        result = driver.get_certificate_status()
        assert result == {"dev1": {"status": "valid"}}

    def test_get_point_health_stats_empty(self, driver):
        assert driver.get_point_health_stats("dev1") == {}

    def test_get_point_health_stats_with_data(self, driver):
        ph = driver._get_point_health("dev1", "p1")
        ph.record_success()
        ph.record_success()
        stats = driver.get_point_health_stats("dev1")
        assert "p1" in stats
        assert stats["p1"]["success_count"] == 2
        assert stats["p1"]["success_rate"] == 1.0

    def test_get_security_policy_map_import_error(self, driver):
        # When asyncua.crypto.security_policies is not importable, returns
        # only the "None" -> None mapping.
        with patch.dict(sys.modules, {"asyncua.crypto.security_policies": None}):
            result = driver._get_security_policy_map()
        assert result == {"None": None}

    def test_get_security_policy_map_with_policies(self, driver):
        # When asyncua is importable, returns the full policy map.
        fake_sp = MagicMock()
        fake_sp.Basic128Rsa15 = "p1"
        fake_sp.Basic256 = "p2"
        fake_sp.Basic256Sha256 = "p3"
        fake_module = MagicMock()
        fake_module.SecurityPolicy = fake_sp
        with patch.dict(sys.modules, {"asyncua.crypto.security_policies": fake_module}):
            result = driver._get_security_policy_map()
        assert result["None"] is None
        assert result["Basic128Rsa15"] == "p1"
        assert result["Basic256"] == "p2"
        assert result["Basic256Sha256"] == "p3"

    async def test_check_rbac_invalid_permission(self, driver):
        assert driver.check_rbac("admin", "not_a_perm") is False

    async def test_check_rbac_admin_granted(self, driver):
        # admin has all permissions
        assert driver.check_rbac("admin", "device:read") is True

    async def test_check_rbac_viewer_denied_write(self, driver):
        assert driver.check_rbac("viewer", "device:delete") is False

    async def test_check_rbac_unknown_role_denied(self, driver):
        assert driver.check_rbac("unknown_role", "device:read") is False


# ════════════════════════════════════════════════════════════════════════
# 9. Read points offline path and health check
# ════════════════════════════════════════════════════════════════════════


class TestReadPointsOffline:
    async def test_read_points_no_client_returns_bad(self, driver):
        # No client registered -> all points bad quality
        driver._device_points["dev1"] = [{"name": "p1", "address": "n1"}]
        result = await driver.read_points("dev1", ["p1"])
        assert "p1" in result
        pv = result["p1"]
        assert isinstance(pv, PointValue)
        assert pv.quality == "bad"

    async def test_read_points_undefined_point(self, driver):
        client = MagicMock()
        driver._clients["dev1"] = client
        driver._device_points["dev1"] = []
        # read_points accesses _session_locks and _session_rebuilding dicts
        driver._session_locks["dev1"] = asyncio.Lock()
        driver._session_rebuilding["dev1"] = asyncio.Event()
        driver._session_rebuild_skip["dev1"] = False
        result = await driver.read_points("dev1", ["unknown"])
        assert result["unknown"].quality == "bad"


class TestHealthCheck:
    async def test_health_check_no_client(self, driver):
        assert await driver.health_check("dev1") is False

    async def test_health_check_client_ok(self, driver):
        client = MagicMock()
        client.nodes.server.read_browse_name = AsyncMock(return_value=MagicMock())
        driver._clients["dev1"] = client
        assert await driver.health_check("dev1") is True

    async def test_health_check_client_exception(self, driver):
        client = MagicMock()
        client.nodes.server.read_browse_name = AsyncMock(side_effect=RuntimeError("fail"))
        driver._clients["dev1"] = client
        assert await driver.health_check("dev1") is False


# ════════════════════════════════════════════════════════════════════════
# 10. Write point rejection paths
# ════════════════════════════════════════════════════════════════════════


class TestWritePointRejections:
    async def test_write_point_no_client(self, driver):
        result = await driver.write_point("dev1", "p1", 42)
        assert result is False

    async def test_write_point_undefined_point(self, driver):
        driver._clients["dev1"] = MagicMock()
        driver._device_points["dev1"] = []
        result = await driver.write_point("dev1", "p1", 42)
        assert result is False

    async def test_write_point_rate_limited(self, driver):
        driver._clients["dev1"] = MagicMock()
        driver._device_points["dev1"] = [{"name": "p1", "address": "n1"}]
        driver._record_write_time("dev1", "p1")  # mark just written
        result = await driver.write_point("dev1", "p1", 42)
        assert result is False

    async def test_write_point_type_validation_fails(self, driver):
        client = MagicMock()
        driver._clients["dev1"] = client
        driver._device_points["dev1"] = [{"name": "p1", "address": "n1"}]
        with patch.object(driver, "_validate_write_type", return_value=(42, False)):
            result = await driver.write_point("dev1", "p1", 42)
        assert result is False

    async def test_write_point_array_bounds_fails(self, driver):
        client = MagicMock()
        driver._clients["dev1"] = client
        driver._device_points["dev1"] = [{"name": "p1", "address": "n1"}]
        big = list(range(_MAX_ARRAY_LENGTH + 5))
        with patch.object(driver, "_validate_write_type", return_value=(big, True)):
            result = await driver.write_point("dev1", "p1", big)
        assert result is False


# ════════════════════════════════════════════════════════════════════════
# 11. Discover / browse
# ════════════════════════════════════════════════════════════════════════


class TestDiscoverBrowse:
    async def test_discover_no_endpoint(self, driver):
        result = await driver.discover_devices({})
        assert result == []

    async def test_discover_invalid_protocol(self, driver):
        result = await driver.discover_devices({"endpoint": "http://evil:4840"})
        assert result == []

    async def test_discover_asyncua_import_error(self, driver):
        with patch.dict(sys.modules, {"asyncua": None}):
            result = await driver.discover_devices({"endpoint": "opc.tcp://localhost:4840"})
        assert result == []

    async def test_browse_no_client(self, driver):
        result = await driver.browse("dev1")
        assert result == []


# ════════════════════════════════════════════════════════════════════════
# 12. create_subscription_batch offline path
# ════════════════════════════════════════════════════════════════════════


class TestSubscriptionBatchOffline:
    async def test_create_subscription_batch_no_client(self, driver):
        result = await driver.create_subscription_batch("dev1", ["ns=2;s=P1"])
        assert result["success"] is False
        assert result["subscribed"] == 0
        assert result["failed"] == 1
        assert "client_not_connected" in result["errors"]


# ════════════════════════════════════════════════════════════════════════
# 13. Edge rule / data persistence / enterprise delegation (no init)
# ════════════════════════════════════════════════════════════════════════


class TestDelegationNoInit:
    def test_get_edge_rules_empty(self, driver):
        assert driver.get_edge_rules() == []

    def test_get_edge_alarm_history_empty(self, driver):
        assert driver.get_edge_alarm_history() == []

    def test_get_edge_rule_stats_empty(self, driver):
        assert driver.get_edge_rule_stats() == {}

    async def test_evaluate_point_rules_no_engine(self, driver):
        assert await driver.evaluate_point_rules("dev1", "p1", 1.0) == []

    async def test_remove_edge_rule_no_engine(self, driver):
        assert await driver.remove_edge_rule("r1") is None

    async def test_update_edge_rule_no_engine(self, driver):
        assert await driver.update_edge_rule("r1", {}) is False

    async def test_hot_reload_rules_no_engine(self, driver):
        assert await driver.hot_reload_rules() == 0

    def test_get_ts_store_stats_empty(self, driver):
        assert driver.get_ts_store_stats() == {}

    def test_get_offline_sync_stats_empty(self, driver):
        assert driver.get_offline_sync_stats() == {}

    async def test_query_ts_no_store(self, driver):
        assert await driver.query_ts("dev1", "p1", 0) == []

    async def test_query_ts_latest_no_store(self, driver):
        assert await driver.query_ts_latest("dev1", ["p1"]) == {}

    async def test_force_offline_sync_no_manager(self, driver):
        assert await driver.force_offline_sync() == 0

    async def test_save_config_version_no_mgr(self, driver):
        assert await driver.save_config_version("dev1", {}) == 0

    async def test_get_config_current_no_mgr(self, driver):
        assert await driver.get_config_current("dev1") is None

    async def test_get_config_versions_no_mgr(self, driver):
        assert await driver.get_config_versions("dev1") == []

    async def test_get_config_version_config_no_mgr(self, driver):
        assert await driver.get_config_version_config("dev1", 1) is None

    async def test_rollback_config_no_mgr(self, driver):
        assert await driver.rollback_config("dev1", 1) is None

    async def test_get_config_audit_trail_no_mgr(self, driver):
        assert await driver.get_config_audit_trail("dev1") == []

    def test_diff_config_versions_no_mgr(self, driver):
        assert driver.diff_config_versions("dev1", 1, 2) is None

    async def test_ota_check_update_no_mgr(self, driver):
        result = await driver.ota_check_update({})
        assert result["update_available"] is False

    async def test_ota_start_no_mgr(self, driver):
        result = await driver.ota_start({})
        assert result["ok"] is False

    async def test_ota_rollback_no_mgr(self, driver):
        result = await driver.ota_rollback()
        assert result["ok"] is False

    def test_ota_get_progress_no_mgr(self, driver):
        assert driver.ota_get_progress() == {"status": "unavailable"}

    def test_ota_get_history_no_mgr(self, driver):
        assert driver.ota_get_history() == []

    def test_get_audit_recent_no_audit(self, driver):
        assert driver.get_audit_recent() == []

    def test_get_audit_by_device_no_audit(self, driver):
        assert driver.get_audit_by_device("dev1") == []

    def test_get_audit_by_action_no_audit(self, driver):
        assert driver.get_audit_by_action("x") == []

    def test_export_audit_csv_no_audit(self, driver):
        assert driver.export_audit_csv() == ""

    def test_get_audit_stats_no_audit(self, driver):
        assert driver.get_audit_stats() == {}


# ════════════════════════════════════════════════════════════════════════
# 14. on_data / set_upload_callback / set_offline_sync_online (no-op paths)
# ════════════════════════════════════════════════════════════════════════


class TestCallbacksNoInit:
    def test_on_data_sets_callback(self, driver):
        def cb(**kw):
            pass

        driver.on_data(cb)
        assert driver._data_callback is cb

    def test_set_offline_sync_online_no_manager(self, driver):
        # should not raise
        driver.set_offline_sync_online(True)

    def test_set_upload_callback_no_manager(self, driver):
        # should not raise
        driver.set_upload_callback(lambda x: None)


# ════════════════════════════════════════════════════════════════════════
# 15. init_data_persistence guard
# ════════════════════════════════════════════════════════════════════════


class TestInitDataPersistence:
    async def test_init_data_persistence_rejects_double_init(self, driver):
        driver._ts_store = MagicMock()
        with pytest.raises(RuntimeError, match="already initialized"):
            await driver.init_data_persistence()


# ════════════════════════════════════════════════════════════════════════
# 16. _SubHandler
# ════════════════════════════════════════════════════════════════════════


class TestSubHandler:
    def _make_handler(self):
        # _SubHandler.__init__ calls asyncio.get_running_loop(), so this must
        # be called from within an async test (pytest-asyncio provides a loop).
        latest_values = {}
        values_lock = asyncio.Lock()
        handler = _SubHandler(
            device_id="dev1",
            latest_values=latest_values,
            data_callback=None,
            values_lock=values_lock,
            event_bus=None,
            subscription_lock=None,
            driver=None,
        )
        return handler

    async def test_datachange_notification_queues(self):
        handler = self._make_handler()
        node = MagicMock()
        node.nodeid.to_string.return_value = "ns=2;s=P1"
        data = MagicMock()
        data.monitored_item = MagicMock()
        data.monitored_item.Value = MagicMock()
        data.monitored_item.Value.StatusCode = 0
        handler.datachange_notification(node, 42, data)
        assert not handler._notify_queue.empty()
        node_id, val, quality = handler._notify_queue.get_nowait()
        assert node_id == "ns=2;s=P1"
        assert val == 42
        assert quality == "good"

    async def test_datachange_notification_data_none(self):
        handler = self._make_handler()
        node = MagicMock()
        node.nodeid.to_string.return_value = "ns=2;s=P1"
        handler.datachange_notification(node, 42, None)
        assert not handler._notify_queue.empty()
        _, _, quality = handler._notify_queue.get_nowait()
        assert quality == "good"

    async def test_datachange_notification_value_attr(self):
        handler = self._make_handler()
        node = MagicMock()
        node.nodeid.to_string.return_value = "ns=2;s=P1"
        data = MagicMock(spec=["Value"])
        data.Value = MagicMock(spec=["StatusCode"])
        data.Value.StatusCode = 0
        handler.datachange_notification(node, 42, data)
        _, _, quality = handler._notify_queue.get_nowait()
        assert quality == "good"

    async def test_datachange_notification_status_code_attr(self):
        handler = self._make_handler()
        node = MagicMock()
        node.nodeid.to_string.return_value = "ns=2;s=P1"
        data = MagicMock(spec=["status_code"])
        data.status_code = 0x80210000  # BadTimeout
        handler.datachange_notification(node, 42, data)
        _, _, quality = handler._notify_queue.get_nowait()
        assert quality == "bad"

    async def test_datachange_notification_exception_falls_to_bad(self):
        handler = self._make_handler()
        node = MagicMock()
        node.nodeid.to_string.return_value = "ns=2;s=P1"
        data = MagicMock()
        # Make accessing monitored_item raise
        type(data).monitored_item = property(lambda self: (_ for _ in ()).throw(RuntimeError("x")))
        handler.datachange_notification(node, 42, data)
        # quality should be "bad" due to exception
        _, _, quality = handler._notify_queue.get_nowait()
        assert quality == "bad"

    async def test_cancel_stops_task_and_clears_queue(self):
        handler = self._make_handler()
        node = MagicMock()
        node.nodeid.to_string.return_value = "ns=2;s=P1"
        handler.datachange_notification(node, 42, None)
        assert not handler._notify_queue.empty()
        handler.cancel()
        assert handler._cancelled is True
        assert handler._notify_queue.empty()


# ════════════════════════════════════════════════════════════════════════
# 17. _apply_point_preprocess
# ════════════════════════════════════════════════════════════════════════


class TestApplyPointPreprocess:
    async def test_bad_quality_returns_bad(self, driver):
        pv = await driver._apply_point_preprocess("dev1", "p1", 42, "bad")
        assert pv.quality == "bad"
        assert pv.value is None

    async def test_nan_value_returns_bad(self, driver):
        pv = await driver._apply_point_preprocess("dev1", "p1", float("nan"), "good")
        assert pv.quality == "bad"
        assert pv.value is None

    async def test_inf_value_returns_bad(self, driver):
        pv = await driver._apply_point_preprocess("dev1", "p1", float("inf"), "good")
        assert pv.quality == "bad"

    async def test_good_value_returns_good(self, driver):
        pv = await driver._apply_point_preprocess("dev1", "p1", 42, "good")
        assert pv.quality == "good"
        assert pv.value == 42

    async def test_scaling_applied(self, driver):
        driver._device_points["dev1"] = [{"name": "p1", "scaling": {"ratio": 2.0, "offset": 1.0}}]
        pv = await driver._apply_point_preprocess("dev1", "p1", 10, "good")
        assert pv.value == 21.0  # 10*2 + 1

    async def test_clamp_out_of_range(self, driver):
        driver._device_points["dev1"] = [{"name": "p1", "clamp": {"min": 0, "max": 100}}]
        pv = await driver._apply_point_preprocess("dev1", "p1", 200, "good")
        assert pv.quality == "bad"
        assert pv.value is None

    async def test_clamp_in_range(self, driver):
        driver._device_points["dev1"] = [{"name": "p1", "clamp": {"min": 0, "max": 100}}]
        pv = await driver._apply_point_preprocess("dev1", "p1", 50, "good")
        assert pv.quality == "good"
        assert pv.value == 50

    async def test_stale_data_returns_uncertain(self, driver):
        ph = driver._get_point_health("dev1", "p1")
        ph.last_publish_at = time.monotonic() - 100
        driver._device_configs["dev1"] = {"subscription_interval": 500}
        pv = await driver._apply_point_preprocess("dev1", "p1", 42, "good")
        assert pv.quality == "uncertain"
        assert pv.value is None


# ════════════════════════════════════════════════════════════════════════
# 18. _mark_all_subscription_points_bad
# ════════════════════════════════════════════════════════════════════════


class TestMarkAllSubscriptionPointsBad:
    async def test_marks_all_points(self, driver):
        driver._device_points["dev1"] = [
            {"name": "p1", "address": "n1"},
            {"name": "p2", "address": "n2"},
        ]
        await driver._mark_all_subscription_points_bad("dev1")
        assert "p1" in driver._latest_values.get("dev1", {})
        assert driver._latest_values["dev1"]["p1"].quality == "bad"
        assert driver._latest_values["dev1"]["p2"].quality == "bad"

    async def test_marks_no_points(self, driver):
        driver._device_points["dev1"] = []
        await driver._mark_all_subscription_points_bad("dev1")
        # no crash, empty result
        assert driver._latest_values.get("dev1", {}) == {}


# ════════════════════════════════════════════════════════════════════════
# 19. _drain_rebuild_queue
# ════════════════════════════════════════════════════════════════════════


class TestDrainRebuildQueue:
    async def test_no_queue(self, driver):
        # should not raise
        await driver._drain_rebuild_queue("dev1")

    async def test_drains_queue(self, driver):
        q = asyncio.Queue(maxsize=10)
        for i in range(3):
            q.put_nowait(("read", [f"p{i}"]))
        driver._rebuild_wait_queue["dev1"] = q
        await driver._drain_rebuild_queue("dev1")
        assert q.empty()


# ════════════════════════════════════════════════════════════════════════
# 20. _check_cert_expiry
# ════════════════════════════════════════════════════════════════════════


class TestCheckCertExpiry:
    def test_empty_path_returns_true(self, driver):
        assert driver._check_cert_expiry("", "Client") is True

    def test_import_error_returns_false(self, driver):
        with patch.dict(sys.modules, {"cryptography": None, "cryptography.x509": None}):
            result = driver._check_cert_expiry("/nonexistent.pem", "Client", device_id="dev1")
        assert result is False

    def test_file_not_found_returns_false(self, driver):
        # cryptography installed but file missing -> exception -> False
        result = driver._check_cert_expiry("/nonexistent/path/cert.pem", "Client", device_id="dev1")
        assert result is False


# ════════════════════════════════════════════════════════════════════════
# 21. init_enterprise / init_edge_rules
# ════════════════════════════════════════════════════════════════════════


class TestInitEnterprise:
    def test_init_enterprise_creates_managers(self, driver):
        driver.init_enterprise()
        assert driver._config_version_mgr is not None
        assert driver._ota_mgr is not None
        assert driver._audit is not None

    def test_init_edge_rules_creates_engine(self, driver):
        driver.init_edge_rules()
        assert driver._rule_engine is not None
        assert driver._trigger_executor is not None
        assert driver._rule_store is not None


# ════════════════════════════════════════════════════════════════════════
# 22. _probe_primary
# ════════════════════════════════════════════════════════════════════════


class TestProbePrimary:
    async def test_no_primary_endpoint(self, driver):
        driver._device_configs["dev1"] = {}
        assert await driver._probe_primary("dev1") is False

    async def test_probe_success(self, driver):
        driver._device_configs["dev1"] = {"endpoint": "opc.tcp://primary:4840"}
        fake_client = MagicMock()
        fake_client.connect = AsyncMock()
        fake_client.disconnect = AsyncMock()
        # Client is imported inside _probe_primary via `from asyncua import Client`
        with patch("asyncua.Client", return_value=fake_client):
            result = await driver._probe_primary("dev1")
        assert result is True

    async def test_probe_failure(self, driver):
        driver._device_configs["dev1"] = {"endpoint": "opc.tcp://primary:4840"}
        fake_client = MagicMock()
        fake_client.connect = AsyncMock(side_effect=RuntimeError("conn refused"))
        fake_client.disconnect = AsyncMock()
        with patch("asyncua.Client", return_value=fake_client):
            result = await driver._probe_primary("dev1")
        assert result is False


# ════════════════════════════════════════════════════════════════════════
# 23. _validate_security_config
# ════════════════════════════════════════════════════════════════════════


class TestValidateSecurityConfig:
    async def test_invalid_combo_returns_false(self, driver):
        # The invalid combo path sleeps in a loop checking _stopping/_running.
        # Patch asyncio.sleep to avoid the 60s hang; also set _running=False so
        # the loop breaks immediately on the first iteration.
        driver._running = False
        driver._device_configs["dev1"] = {}
        driver._device_points["dev1"] = []
        # security_mode != None but policy == None -> invalid
        result = await driver._validate_security_config("dev1", {}, "SignAndEncrypt", "None")
        assert result is False

    async def test_valid_none_mode(self, driver):
        result = await driver._validate_security_config("dev1", {}, "None", "None")
        assert result is True

    async def test_valid_combo_with_certs(self, driver):
        config = {
            "client_cert_path": "/c.pem",
            "client_key_path": "/k.pem",
        }
        result = await driver._validate_security_config("dev1", config, "SignAndEncrypt", "Basic256Sha256")
        assert result is True

    async def test_valid_combo_without_certs_warns_but_proceeds(self, driver):
        # Missing certs but combo valid -> warns, returns True
        result = await driver._validate_security_config("dev1", {}, "SignAndEncrypt", "Basic256Sha256")
        assert result is True
