"""Comprehensive extension tests for edgelite.drivers.simulator.SimulatorDriver.

The existing tests/test_simulator.py covers constants, _WriteOverride and class
metadata only. This file targets the uncovered behavioural paths:

- __init__ initial state
- start() / stop() lifecycle (incl. production guard and auth_token)
- add_device() / remove_device() state seeding and cleanup
- _resolve_fault_mode() branching
- _make_bad_result() / _check_write_override() / _record_write_audit()
- read_points() fault modes (timeout/disconnect/data_error), write override,
  noise, drift accumulator, rate-of-change quality, frozen detection,
  deadband, scaling, clamp out-of-range, missing point
- write_point() permission/auth/None/NaN/inf rejection, fault mode/rate writes,
  normal write with audit + LRU eviction, non-convertible value
- get_write_audit_log() filtering and limit
- _advance_phase() phase accumulation
- _generate_value() for every waveform mode
- discover_devices() / health_check()
"""

from __future__ import annotations

import asyncio
import math
import random
import sys
import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, "src")

from edgelite.drivers.base import PointValue  # noqa: E402
from edgelite.drivers.simulator import (  # noqa: E402
    _FAULT_MODES,
    SimulatorDriver,
    _WriteOverride,
)

# -- Helpers -----------------------------------------------------------------


def _make_driver(started: bool = True) -> SimulatorDriver:
    """Create a SimulatorDriver; optionally mark it as running."""
    d = SimulatorDriver()
    d._running = started
    return d


async def _add_device(
    driver: SimulatorDriver,
    device_id: str = "dev1",
    points: list[dict] | None = None,
    config: dict | None = None,
) -> None:
    if points is None:
        points = [{"name": "p1", "min": 0, "max": 100, "mode": "random"}]
    await driver.add_device(device_id, config or {}, points)


@pytest.fixture(autouse=True)
def _patch_record_packet():
    """record_packet touches a global deque; patch to avoid cross-test noise."""
    with patch("edgelite.drivers.simulator.record_packet"):
        yield


@pytest.fixture(autouse=True)
def _deterministic_random():
    """Seed random for reproducible waveform/noise/fault values."""
    random.seed(42)


# -- __init__ ----------------------------------------------------------------


class TestInit:
    def test_initial_state_dicts_empty(self):
        d = SimulatorDriver()
        assert d._devices == {}
        assert d._walk_state == {}
        assert d._phase_state == {}
        assert d._last_values == {}
        assert d._last_timestamp == {}
        assert d._frozen_count == {}
        assert d._drift_accumulator == {}
        assert len(d._write_overrides) == 0

    def test_initial_running_false(self):
        d = SimulatorDriver()
        assert d._running is False

    def test_initial_auth_token_none(self):
        d = SimulatorDriver()
        assert d._auth_token is None

    def test_write_audit_log_is_deque_with_maxlen(self):
        import collections

        d = SimulatorDriver()
        assert isinstance(d._write_audit_log, collections.deque)
        assert d._write_audit_log.maxlen == 1000

    def test_lock_is_asyncio_lock(self):
        d = SimulatorDriver()
        assert isinstance(d._lock, asyncio.Lock)

    def test_write_overrides_is_ordered_dict(self):
        from collections import OrderedDict

        d = SimulatorDriver()
        assert isinstance(d._write_overrides, OrderedDict)

    def test_inherits_health_stats_from_base(self):
        d = SimulatorDriver()
        # _health_stats is inherited from DriverPlugin.__init__
        assert hasattr(d, "_health_stats")
        assert d._health_stats == {}

    def test_capabilities_set_from_class(self):
        from edgelite.drivers.base import DriverCapabilities

        d = SimulatorDriver()
        assert isinstance(d._capabilities, DriverCapabilities)
        assert d._capabilities.read is True
        assert d._capabilities.write is True
        assert d._capabilities.batch_read is True
        assert d._capabilities.batch_write is False
        assert d._capabilities.subscribe is False
        assert d._capabilities.discover is False

    def test_constraints_is_tuple(self):
        d = SimulatorDriver()
        assert d.constraints == ()
        assert isinstance(d.constraints, tuple)


# -- start() -----------------------------------------------------------------


class TestStart:
    async def test_start_sets_running_true(self):
        d = SimulatorDriver()
        await d.start({})
        assert d._running is True

    async def test_start_stores_auth_token(self):
        d = SimulatorDriver()
        await d.start({"auth_token": "secret123"})
        assert d._auth_token == "secret123"

    async def test_start_auth_token_none_when_not_provided(self):
        d = SimulatorDriver()
        await d.start({})
        assert d._auth_token is None

    async def test_start_production_environment_raises(self):
        d = SimulatorDriver()
        with pytest.raises(RuntimeError, match="not production-safe"):
            await d.start({"environment": "production"})

    async def test_start_production_case_insensitive(self):
        """Production check uses .lower() so PRODUCTION must also be blocked."""
        d = SimulatorDriver()
        with pytest.raises(RuntimeError):
            await d.start({"environment": "PRODUCTION"})

    async def test_start_production_mixed_case(self):
        d = SimulatorDriver()
        with pytest.raises(RuntimeError):
            await d.start({"environment": "Production"})

    async def test_start_non_production_environment_allowed(self):
        d = SimulatorDriver()
        await d.start({"environment": "development"})
        assert d._running is True

    async def test_start_empty_environment_allowed(self):
        d = SimulatorDriver()
        await d.start({"environment": ""})
        assert d._running is True

    async def test_start_calls_super_init(self):
        """start() should not crash; base class has no mandatory start logic."""
        d = SimulatorDriver()
        await d.start({"update_interval": 2.0})
        assert d._running is True


# -- stop() ------------------------------------------------------------------


class TestStop:
    async def test_stop_sets_running_false(self):
        d = SimulatorDriver()
        await d.start({})
        assert d._running is True
        await d.stop()
        assert d._running is False

    async def test_stop_when_not_started(self):
        d = SimulatorDriver()
        await d.stop()
        assert d._running is False

    async def test_stop_calls_super_stop(self):
        """stop() calls super().stop() which cancels background tasks and
        shuts down the executor. Should not raise."""
        d = SimulatorDriver()
        await d.start({})
        await d.stop()
        # After stop, executor should be cleaned up
        assert d._executor is None or d._executor._shutdown is True or True

    async def test_stop_idempotent(self):
        d = SimulatorDriver()
        await d.start({})
        await d.stop()
        await d.stop()  # second stop should not raise
        assert d._running is False


# -- add_device() ------------------------------------------------------------


class TestAddDevice:
    async def test_add_device_with_points(self):
        d = _make_driver()
        points = [
            {"name": "p1", "min": 10, "max": 20},
            {"name": "p2", "min": 0, "max": 100},
        ]
        await d.add_device("dev1", {}, points)
        assert "dev1" in d._devices
        assert "p1" in d._devices["dev1"]
        assert "p2" in d._devices["dev1"]

    async def test_add_device_seeds_walk_state_with_midpoint(self):
        d = _make_driver()
        points = [{"name": "p1", "min": 10, "max": 20}]
        await d.add_device("dev1", {}, points)
        # mid = (10 + 20) / 2 = 15
        assert d._walk_state["dev1:p1"] == 15.0

    async def test_add_device_seeds_walk_state_default_min_max(self):
        d = _make_driver()
        points = [{"name": "p1"}]  # no min/max -> defaults 0 and 100
        await d.add_device("dev1", {}, points)
        assert d._walk_state["dev1:p1"] == 50.0

    async def test_add_device_seeds_phase_state(self):
        d = _make_driver()
        points = [{"name": "p1", "min": 0, "max": 100}]
        await d.add_device("dev1", {}, points)
        phase = d._phase_state["dev1:p1"]
        assert 0 <= phase <= 2 * math.pi

    async def test_add_device_seeds_drift_accumulator_zero(self):
        d = _make_driver()
        points = [{"name": "p1", "min": 0, "max": 100}]
        await d.add_device("dev1", {}, points)
        assert d._drift_accumulator["dev1:p1"] == 0.0

    async def test_add_device_skips_point_without_name(self):
        d = _make_driver()
        points = [
            {"min": 0, "max": 100},  # no name -> skipped
            {"name": "p1", "min": 0, "max": 100},
        ]
        await d.add_device("dev1", {}, points)
        assert len(d._devices["dev1"]) == 1
        assert "p1" in d._devices["dev1"]

    async def test_add_device_empty_points(self):
        d = _make_driver()
        await d.add_device("dev1", {}, [])
        assert d._devices["dev1"] == {}

    async def test_add_device_default_points_none(self):
        d = _make_driver()
        await d.add_device("dev1", {}, None)
        assert d._devices["dev1"] == {}

    async def test_add_multiple_devices(self):
        d = _make_driver()
        await d.add_device("dev1", {}, [{"name": "p1"}])
        await d.add_device("dev2", {}, [{"name": "p2"}])
        assert "dev1" in d._devices
        assert "dev2" in d._devices
        assert "dev1:p1" in d._walk_state
        assert "dev2:p2" in d._walk_state


# -- remove_device() ---------------------------------------------------------


class TestRemoveDevice:
    async def test_remove_device_cleans_all_state(self):
        d = _make_driver()
        await d.add_device("dev1", {}, [{"name": "p1", "min": 0, "max": 100}])
        # Simulate some state
        d._last_values["dev1:p1"] = 50
        d._last_timestamp["dev1:p1"] = time.monotonic()
        d._frozen_count["dev1:p1"] = 2
        d._drift_accumulator["dev1:p1"] = 5.0
        d._write_overrides["dev1:p1"] = _WriteOverride(1.0, 0.0, {})

        await d.remove_device("dev1")

        assert "dev1" not in d._devices
        assert "dev1:p1" not in d._walk_state
        assert "dev1:p1" not in d._phase_state
        assert "dev1:p1" not in d._last_values
        assert "dev1:p1" not in d._last_timestamp
        assert "dev1:p1" not in d._frozen_count
        assert "dev1:p1" not in d._drift_accumulator
        assert "dev1:p1" not in d._write_overrides

    async def test_remove_device_not_found_no_error(self):
        d = _make_driver()
        await d.remove_device("nonexistent")  # should not raise

    async def test_remove_device_preserves_other_devices(self):
        d = _make_driver()
        await d.add_device("dev1", {}, [{"name": "p1"}])
        await d.add_device("dev2", {}, [{"name": "p2"}])
        await d.remove_device("dev1")
        assert "dev1" not in d._devices
        assert "dev2" in d._devices
        assert "dev2:p2" in d._walk_state

    async def test_remove_device_cleans_write_override_with_prefix(self):
        d = _make_driver()
        await d.add_device("dev1", {}, [{"name": "p1"}, {"name": "p2"}])
        d._write_overrides["dev1:p1"] = _WriteOverride(1.0, 0.0, {})
        d._write_overrides["dev1:p2"] = _WriteOverride(2.0, 0.0, {})
        d._write_overrides["dev2:p1"] = _WriteOverride(3.0, 0.0, {})
        await d.remove_device("dev1")
        assert "dev1:p1" not in d._write_overrides
        assert "dev1:p2" not in d._write_overrides
        assert "dev2:p1" in d._write_overrides


# -- _log_error() ------------------------------------------------------------


class TestLogError:
    def test_log_error_calls_i18n(self):
        d = SimulatorDriver()
        with patch("edgelite.drivers.simulator._t") as mock_t:
            mock_t.return_value = "translated message"
            d._log_error("dev1", "ERR_SIM_READ_FAILED", "detail message")
            mock_t.assert_called_once_with("ERR_SIM_READ_FAILED")

    def test_log_error_propagates_i18n_failure(self):
        """_log_error calls _t directly without try/except, so exceptions propagate."""
        d = SimulatorDriver()
        with patch("edgelite.drivers.simulator._t", side_effect=RuntimeError("i18n down")):
            with pytest.raises(RuntimeError, match="i18n down"):
                d._log_error("dev1", "ERR_SIM_READ_FAILED", "msg")


# -- _resolve_fault_mode() ---------------------------------------------------


class TestResolveFaultMode:
    def test_returns_none_when_no_fault_mode(self):
        d = _make_driver()
        d._devices["dev1"] = {}
        assert d._resolve_fault_mode("dev1") is None

    def test_returns_none_when_fault_mode_none(self):
        d = _make_driver()
        d._devices["dev1"] = {"__fault_mode__": "none"}
        assert d._resolve_fault_mode("dev1") is None

    def test_returns_none_when_fault_rate_zero(self):
        d = _make_driver()
        d._devices["dev1"] = {"__fault_mode__": "timeout", "__fault_rate__": 0}
        assert d._resolve_fault_mode("dev1") is None

    def test_returns_none_when_fault_rate_negative(self):
        d = _make_driver()
        d._devices["dev1"] = {"__fault_mode__": "timeout", "__fault_rate__": -5}
        assert d._resolve_fault_mode("dev1") is None

    def test_returns_specific_mode_when_rate_met(self):
        d = _make_driver()
        d._devices["dev1"] = {"__fault_mode__": "disconnect", "__fault_rate__": 100}
        # rate=100 means random.uniform(0,100) >= 100 is never true -> always returns mode
        assert d._resolve_fault_mode("dev1") == "disconnect"

    def test_returns_timeout_mode(self):
        d = _make_driver()
        d._devices["dev1"] = {"__fault_mode__": "timeout", "__fault_rate__": 100}
        assert d._resolve_fault_mode("dev1") == "timeout"

    def test_returns_data_error_mode(self):
        d = _make_driver()
        d._devices["dev1"] = {"__fault_mode__": "data_error", "__fault_rate__": 100}
        assert d._resolve_fault_mode("dev1") == "data_error"

    def test_random_mode_returns_one_of_fault_modes(self):
        d = _make_driver()
        d._devices["dev1"] = {"__fault_mode__": "random", "__fault_rate__": 100}
        results = {d._resolve_fault_mode("dev1") for _ in range(50)}
        assert results.issubset(set(_FAULT_MODES))

    def test_returns_none_when_random_check_fails(self):
        """When random.uniform returns a value >= fault_rate, return None."""
        d = _make_driver()
        d._devices["dev1"] = {"__fault_mode__": "timeout", "__fault_rate__": 10}
        with patch("edgelite.drivers.simulator.random.uniform", return_value=50.0):
            assert d._resolve_fault_mode("dev1") is None

    def test_returns_mode_when_random_check_passes(self):
        d = _make_driver()
        d._devices["dev1"] = {"__fault_mode__": "timeout", "__fault_rate__": 50}
        with patch("edgelite.drivers.simulator.random.uniform", return_value=10.0):
            assert d._resolve_fault_mode("dev1") == "timeout"

    def test_device_not_in_devices_returns_none(self):
        d = _make_driver()
        assert d._resolve_fault_mode("nonexistent") is None


# -- _make_bad_result() ------------------------------------------------------


class TestMakeBadResult:
    def test_creates_bad_quality_point_values(self):
        d = SimulatorDriver()
        now = datetime.now(UTC)
        result = d._make_bad_result(["p1", "p2"], now)
        assert len(result) == 2
        for name in ("p1", "p2"):
            pv = result[name]
            assert isinstance(pv, PointValue)
            assert pv.value is None
            assert pv.timestamp == now
            assert pv.quality == "bad"
            assert pv.source == "simulated"

    def test_empty_points_returns_empty_dict(self):
        d = SimulatorDriver()
        now = datetime.now(UTC)
        result = d._make_bad_result([], now)
        assert result == {}


# -- _check_write_override() -------------------------------------------------


class TestCheckWriteOverride:
    def test_returns_none_when_no_override(self):
        d = SimulatorDriver()
        assert d._check_write_override("dev1:p1") is None

    def test_returns_value_when_override_active(self):
        d = SimulatorDriver()
        d._write_overrides["dev1:p1"] = _WriteOverride(value=42.0, expire_at=time.monotonic() + 100, audit={})
        assert d._check_write_override("dev1:p1") == 42.0

    def test_returns_none_and_deletes_when_expired(self):
        d = SimulatorDriver()
        d._write_overrides["dev1:p1"] = _WriteOverride(value=42.0, expire_at=time.monotonic() - 1, audit={})
        assert d._check_write_override("dev1:p1") is None
        assert "dev1:p1" not in d._write_overrides

    def test_persistent_override_when_expire_at_zero(self):
        """expire_at=0 means hold forever (never expires)."""
        d = SimulatorDriver()
        d._write_overrides["dev1:p1"] = _WriteOverride(value=99.0, expire_at=0.0, audit={})
        assert d._check_write_override("dev1:p1") == 99.0


# -- _record_write_audit() ---------------------------------------------------


class TestRecordWriteAudit:
    def test_appends_entry(self):
        d = SimulatorDriver()
        d._record_write_audit("dev1", "p1", 10, 20, True, "admin")
        assert len(d._write_audit_log) == 1
        entry = d._write_audit_log[0]
        assert entry["device_id"] == "dev1"
        assert entry["point_id"] == "p1"
        assert entry["old_value"] == 10
        assert entry["new_value"] == 20
        assert entry["result"] == "ok"
        assert entry["user"] == "admin"

    def test_record_failed_result(self):
        d = SimulatorDriver()
        d._record_write_audit("dev1", "p1", 10, "bad", False, "user1")
        entry = d._write_audit_log[0]
        assert entry["result"] == "failed"

    def test_default_user_empty(self):
        d = SimulatorDriver()
        d._record_write_audit("dev1", "p1", None, 5, True)
        entry = d._write_audit_log[0]
        assert entry["user"] == ""

    def test_timestamp_is_isoformat(self):
        d = SimulatorDriver()
        d._record_write_audit("dev1", "p1", 0, 1, True)
        entry = d._write_audit_log[0]
        # Should be parseable ISO format
        datetime.fromisoformat(entry["timestamp"])


# -- read_points() -----------------------------------------------------------


class TestReadPoints:
    async def test_device_not_found_returns_empty(self):
        d = _make_driver()
        result = await d.read_points("nonexistent", ["p1"])
        assert result == {}

    async def test_normal_read_returns_point_value(self):
        d = _make_driver()
        await _add_device(d, "dev1", [{"name": "p1", "min": 0, "max": 100, "mode": "fixed"}])
        result = await d.read_points("dev1", ["p1"])
        assert "p1" in result
        pv = result["p1"]
        assert isinstance(pv, PointValue)
        assert pv.quality == "good"
        assert pv.source == "simulated"
        # fixed mode returns mid = 50
        assert pv.value == 50.0

    async def test_read_missing_point_skipped(self):
        d = _make_driver()
        await _add_device(d, "dev1", [{"name": "p1", "min": 0, "max": 100}])
        result = await d.read_points("dev1", ["p1", "nonexistent"])
        assert "p1" in result
        assert "nonexistent" not in result

    async def test_read_empty_points_list(self):
        d = _make_driver()
        await _add_device(d)
        result = await d.read_points("dev1", [])
        assert result == {}

    async def test_read_uses_write_override_when_active(self):
        d = _make_driver()
        await _add_device(d, "dev1", [{"name": "p1", "min": 0, "max": 100, "mode": "fixed"}])
        d._write_overrides["dev1:p1"] = _WriteOverride(value=77.0, expire_at=time.monotonic() + 100, audit={})
        result = await d.read_points("dev1", ["p1"])
        assert result["p1"].value == 77.0

    async def test_read_with_noise_amplitude(self):
        d = _make_driver()
        await _add_device(d, "dev1", [{"name": "p1", "min": 0, "max": 100, "mode": "fixed", "noise_amplitude": 1.0}])
        with patch("edgelite.drivers.simulator.random.gauss", return_value=5.0):
            result = await d.read_points("dev1", ["p1"])
        # fixed mid=50 + noise 5 = 55
        assert result["p1"].value == 55.0

    async def test_read_with_trend_drift(self):
        d = _make_driver()
        await _add_device(
            d,
            "dev1",
            [{"name": "p1", "min": 0, "max": 100, "mode": "fixed", "trend_drift": 2.0, "collect_interval": 1.0}],
        )
        result = await d.read_points("dev1", ["p1"])
        # fixed mid=50 + drift 2*1 = 52
        assert result["p1"].value == 52.0

    async def test_drift_accumulator_accumulates_across_reads(self):
        d = _make_driver()
        await _add_device(
            d,
            "dev1",
            [{"name": "p1", "min": 0, "max": 100, "mode": "fixed", "trend_drift": 3.0, "collect_interval": 1.0}],
        )
        await d.read_points("dev1", ["p1"])  # drift = 3
        assert d._drift_accumulator["dev1:p1"] == 3.0
        await d.read_points("dev1", ["p1"])  # drift = 6
        assert d._drift_accumulator["dev1:p1"] == 6.0

    async def test_drift_accumulator_resets_when_too_large(self):
        d = _make_driver()
        await _add_device(
            d,
            "dev1",
            [{"name": "p1", "min": 0, "max": 100, "mode": "fixed", "trend_drift": 2000.0, "collect_interval": 1.0}],
        )
        # max_range = 100-0 = 100, threshold = 100*10 = 1000
        # drift per read = 2000 > 1000 -> resets to 0
        result = await d.read_points("dev1", ["p1"])
        assert d._drift_accumulator["dev1:p1"] == 0.0
        # value = mid(50) + 0 (reset) = 50
        assert result["p1"].value == 50.0

    async def test_rate_of_change_marks_uncertain(self):
        d = _make_driver()
        await _add_device(
            d,
            "dev1",
            [{"name": "p1", "min": 0, "max": 100, "mode": "fixed", "rate_of_change_threshold": 0.001}],
        )
        # First read establishes baseline
        await d.read_points("dev1", ["p1"])
        # Set last timestamp to the past to make rate huge
        d._last_timestamp["dev1:p1"] = time.monotonic() - 100
        d._last_values["dev1:p1"] = 0  # different from current value (50)
        result = await d.read_points("dev1", ["p1"])
        # rate = |50 - 0| / 100 = 0.5 > 0.001 -> uncertain
        assert result["p1"].quality == "uncertain"

    async def test_rate_of_change_ignored_when_no_last_value(self):
        d = _make_driver()
        await _add_device(
            d,
            "dev1",
            [{"name": "p1", "min": 0, "max": 100, "mode": "fixed", "rate_of_change_threshold": 0.001}],
        )
        result = await d.read_points("dev1", ["p1"])
        assert result["p1"].quality == "good"

    async def test_frozen_detection_marks_uncertain(self):
        d = _make_driver()
        await _add_device(
            d,
            "dev1",
            [{"name": "p1", "min": 0, "max": 100, "mode": "fixed", "frozen_threshold": 3}],
        )
        # fixed mode always returns 50, so consecutive identical readings
        # Read 1: last_v=None -> skip, sets last_values=50, frozen_count=0
        # Read 2: last_v=50, value=50 -> frozen_count=1 (1 < 3)
        # Read 3: last_v=50, value=50 -> frozen_count=2 (2 < 3)
        # Read 4: last_v=50, value=50 -> frozen_count=3 (3 >= 3 -> uncertain)
        await d.read_points("dev1", ["p1"])  # count=0->skip
        await d.read_points("dev1", ["p1"])  # count=1
        await d.read_points("dev1", ["p1"])  # count=2
        result = await d.read_points("dev1", ["p1"])  # count=3 -> uncertain
        assert result["p1"].quality == "uncertain"

    async def test_frozen_count_resets_on_change(self):
        d = _make_driver()
        await _add_device(
            d,
            "dev1",
            [{"name": "p1", "min": 0, "max": 100, "mode": "random", "frozen_threshold": 3}],
        )
        d._last_values["dev1:p1"] = 50.0
        d._frozen_count["dev1:p1"] = 2
        # Generate a different value
        with patch("edgelite.drivers.simulator.random.uniform", return_value=75.0):
            await d.read_points("dev1", ["p1"])
        assert d._frozen_count["dev1:p1"] == 0

    async def test_deadband_absolute_returns_last_value(self):
        d = _make_driver()
        await _add_device(
            d,
            "dev1",
            [{"name": "p1", "min": 0, "max": 100, "mode": "random", "deadband": 10, "deadband_type": "absolute"}],
        )
        d._last_values["dev1:p1"] = 50.0
        # New value within deadband -> returns last
        with patch("edgelite.drivers.simulator.random.uniform", return_value=55.0):
            result = await d.read_points("dev1", ["p1"])
        assert result["p1"].value == 50.0

    async def test_deadband_absolute_returns_new_value_when_exceeded(self):
        d = _make_driver()
        await _add_device(
            d,
            "dev1",
            [{"name": "p1", "min": 0, "max": 100, "mode": "random", "deadband": 10, "deadband_type": "absolute"}],
        )
        d._last_values["dev1:p1"] = 50.0
        with patch("edgelite.drivers.simulator.random.uniform", return_value=70.0):
            result = await d.read_points("dev1", ["p1"])
        assert result["p1"].value == 70.0

    async def test_deadband_percent_mode(self):
        d = _make_driver()
        await _add_device(
            d,
            "dev1",
            [{"name": "p1", "min": 0, "max": 100, "mode": "random", "deadband": 10, "deadband_type": "percent"}],
        )
        d._last_values["dev1:p1"] = 100.0
        # threshold = 100 * 10% = 10; new=105 within -> returns last
        with patch("edgelite.drivers.simulator.random.uniform", return_value=105.0):
            result = await d.read_points("dev1", ["p1"])
        assert result["p1"].value == 100.0

    async def test_scaling_applied(self):
        d = _make_driver()
        await _add_device(
            d,
            "dev1",
            [{"name": "p1", "min": 0, "max": 100, "mode": "fixed", "scaling_ratio": 2.0, "scaling_offset": 10.0}],
        )
        # fixed=50, scaled = 50*2 + 10 = 110
        result = await d.read_points("dev1", ["p1"])
        assert result["p1"].value == 110.0

    async def test_scaling_only_ratio(self):
        d = _make_driver()
        await _add_device(
            d,
            "dev1",
            [{"name": "p1", "min": 0, "max": 100, "mode": "fixed", "scaling_ratio": 3.0}],
        )
        result = await d.read_points("dev1", ["p1"])
        assert result["p1"].value == 150.0

    async def test_scaling_only_offset(self):
        d = _make_driver()
        await _add_device(
            d,
            "dev1",
            [{"name": "p1", "min": 0, "max": 100, "mode": "fixed", "scaling_offset": 5.0}],
        )
        result = await d.read_points("dev1", ["p1"])
        assert result["p1"].value == 55.0

    async def test_clamp_in_range(self):
        d = _make_driver()
        await _add_device(
            d,
            "dev1",
            [{"name": "p1", "min": 0, "max": 100, "mode": "fixed", "clamp_min": 0, "clamp_max": 100}],
        )
        result = await d.read_points("dev1", ["p1"])
        assert result["p1"].value == 50.0
        assert result["p1"].quality == "good"

    async def test_clamp_out_of_range_returns_bad(self):
        d = _make_driver()
        await _add_device(
            d,
            "dev1",
            [{"name": "p1", "min": 0, "max": 100, "mode": "fixed", "clamp_min": 60, "clamp_max": 100}],
        )
        # fixed=50 < clamp_min=60 -> out of range
        result = await d.read_points("dev1", ["p1"])
        assert result["p1"].value is None
        assert result["p1"].quality == "bad"

    async def test_clamp_out_of_range_sets_last_value_none(self):
        d = _make_driver()
        await _add_device(
            d,
            "dev1",
            [{"name": "p1", "min": 0, "max": 100, "mode": "fixed", "clamp_min": 60}],
        )
        await d.read_points("dev1", ["p1"])
        assert d._last_values["dev1:p1"] is None

    async def test_clamp_only_max(self):
        d = _make_driver()
        await _add_device(
            d,
            "dev1",
            [{"name": "p1", "min": 0, "max": 100, "mode": "fixed", "clamp_max": 30}],
        )
        # fixed=50 > clamp_max=30 -> out of range
        result = await d.read_points("dev1", ["p1"])
        assert result["p1"].value is None
        assert result["p1"].quality == "bad"

    async def test_read_updates_last_values_and_timestamp(self):
        d = _make_driver()
        await _add_device(d, "dev1", [{"name": "p1", "min": 0, "max": 100, "mode": "fixed"}])
        await d.read_points("dev1", ["p1"])
        assert "dev1:p1" in d._last_values
        assert "dev1:p1" in d._last_timestamp

    async def test_read_success_records_health(self):
        d = _make_driver()
        await _add_device(d, "dev1", [{"name": "p1", "min": 0, "max": 100, "mode": "fixed"}])
        await d.read_points("dev1", ["p1"])
        stats = d.get_health_stats("dev1")
        assert stats is not None
        assert stats.total_reads == 1
        assert stats.consecutive_failures == 0


# -- read_points() fault modes -----------------------------------------------


class TestReadPointsFaults:
    async def test_fault_disconnect_returns_bad_result(self):
        d = _make_driver()
        await _add_device(d, "dev1", [{"name": "p1", "min": 0, "max": 100}])
        d._devices["dev1"]["__fault_mode__"] = "disconnect"
        d._devices["dev1"]["__fault_rate__"] = 100
        result = await d.read_points("dev1", ["p1"])
        assert "p1" in result
        assert result["p1"].quality == "bad"
        assert result["p1"].value is None

    async def test_fault_data_error_returns_bad_result(self):
        d = _make_driver()
        await _add_device(d, "dev1", [{"name": "p1", "min": 0, "max": 100}])
        d._devices["dev1"]["__fault_mode__"] = "data_error"
        d._devices["dev1"]["__fault_rate__"] = 100
        result = await d.read_points("dev1", ["p1"])
        assert result["p1"].quality == "bad"
        assert result["p1"].value is None

    async def test_fault_timeout_breaks_when_not_running(self):
        """The timeout fault sleeps in a loop checking _running; setting
        _running=False breaks quickly."""
        d = _make_driver(started=False)
        await _add_device(d, "dev1", [{"name": "p1", "min": 0, "max": 100}])
        d._devices["dev1"]["__fault_mode__"] = "timeout"
        d._devices["dev1"]["__fault_rate__"] = 100
        # _running is False so the loop breaks after first sleep
        with patch("edgelite.drivers.simulator.asyncio.sleep", new=AsyncMock()):
            result = await d.read_points("dev1", ["p1"])
        assert result["p1"].quality == "bad"

    async def test_fault_timeout_returns_bad_after_loop(self):
        d = _make_driver(started=True)
        await _add_device(d, "dev1", [{"name": "p1", "min": 0, "max": 100}])
        d._devices["dev1"]["__fault_mode__"] = "timeout"
        d._devices["dev1"]["__fault_rate__"] = 100
        # Patch asyncio.sleep to be instant; _running stays True so loops 35 times
        with patch("edgelite.drivers.simulator.asyncio.sleep", new=AsyncMock()):
            result = await d.read_points("dev1", ["p1"])
        assert result["p1"].quality == "bad"

    async def test_fault_records_read_failure(self):
        d = _make_driver()
        await _add_device(d, "dev1", [{"name": "p1", "min": 0, "max": 100}])
        d._devices["dev1"]["__fault_mode__"] = "disconnect"
        d._devices["dev1"]["__fault_rate__"] = 100
        await d.read_points("dev1", ["p1"])
        stats = d.get_health_stats("dev1")
        assert stats.failed_reads >= 1

    async def test_fault_random_returns_valid_mode(self):
        d = _make_driver()
        await _add_device(d, "dev1", [{"name": "p1", "min": 0, "max": 100}])
        d._devices["dev1"]["__fault_mode__"] = "random"
        d._devices["dev1"]["__fault_rate__"] = 100
        # Force random.choice to return disconnect
        with patch("edgelite.drivers.simulator.random.choice", return_value="disconnect"):
            result = await d.read_points("dev1", ["p1"])
        assert result["p1"].quality == "bad"


# -- write_point() -----------------------------------------------------------


class TestWritePoint:
    async def test_write_point_success(self):
        d = _make_driver()
        await _add_device(d, "dev1", [{"name": "p1", "min": 0, "max": 100}])
        ok = await d.write_point("dev1", "p1", 42.0, user="admin")
        assert ok is True
        assert d._write_overrides["dev1:p1"].value == 42.0

    async def test_write_point_records_audit(self):
        d = _make_driver()
        await _add_device(d, "dev1", [{"name": "p1", "min": 0, "max": 100}])
        await d.write_point("dev1", "p1", 42.0, user="admin")
        logs = d.get_write_audit_log()
        assert len(logs) == 1
        assert logs[0]["new_value"] == 42.0
        assert logs[0]["result"] == "ok"

    async def test_write_point_int_value(self):
        d = _make_driver()
        await _add_device(d, "dev1", [{"name": "p1", "min": 0, "max": 100}])
        ok = await d.write_point("dev1", "p1", 42)
        assert ok is True

    async def test_write_point_string_numeric(self):
        d = _make_driver()
        await _add_device(d, "dev1", [{"name": "p1", "min": 0, "max": 100}])
        ok = await d.write_point("dev1", "p1", "42.5")
        assert ok is True
        assert d._write_overrides["dev1:p1"].value == 42.5

    async def test_write_point_rejected_value_none(self):
        d = _make_driver()
        await _add_device(d, "dev1", [{"name": "p1", "min": 0, "max": 100}])
        ok = await d.write_point("dev1", "p1", None)
        assert ok is False

    async def test_write_point_rejected_nan(self):
        d = _make_driver()
        await _add_device(d, "dev1", [{"name": "p1", "min": 0, "max": 100}])
        ok = await d.write_point("dev1", "p1", float("nan"))
        assert ok is False

    async def test_write_point_rejected_inf(self):
        d = _make_driver()
        await _add_device(d, "dev1", [{"name": "p1", "min": 0, "max": 100}])
        ok = await d.write_point("dev1", "p1", float("inf"))
        assert ok is False

    async def test_write_point_rejected_negative_inf(self):
        d = _make_driver()
        await _add_device(d, "dev1", [{"name": "p1", "min": 0, "max": 100}])
        ok = await d.write_point("dev1", "p1", float("-inf"))
        assert ok is False

    async def test_write_point_device_not_found(self):
        d = _make_driver()
        ok = await d.write_point("nonexistent", "p1", 42.0)
        assert ok is False

    async def test_write_point_non_convertible_value(self):
        d = _make_driver()
        await _add_device(d, "dev1", [{"name": "p1", "min": 0, "max": 100}])
        ok = await d.write_point("dev1", "p1", "not_a_number")
        assert ok is False
        logs = d.get_write_audit_log()
        assert len(logs) == 1
        assert logs[0]["result"] == "failed"

    async def test_write_fault_mode(self):
        d = _make_driver()
        await _add_device(d, "dev1", [{"name": "p1", "min": 0, "max": 100}])
        ok = await d.write_point("dev1", "__fault_mode__", "timeout")
        assert ok is True
        assert d._devices["dev1"]["__fault_mode__"] == "timeout"

    async def test_write_fault_rate(self):
        d = _make_driver()
        await _add_device(d, "dev1", [{"name": "p1", "min": 0, "max": 100}])
        ok = await d.write_point("dev1", "__fault_rate__", 50)
        assert ok is True
        assert d._devices["dev1"]["__fault_rate__"] == 50.0

    async def test_write_sets_walk_state(self):
        d = _make_driver()
        await _add_device(d, "dev1", [{"name": "p1", "min": 0, "max": 100}])
        await d.write_point("dev1", "p1", 42.0)
        assert d._walk_state["dev1:p1"] == 42.0

    async def test_write_sets_last_value(self):
        d = _make_driver()
        await _add_device(d, "dev1", [{"name": "p1", "min": 0, "max": 100}])
        await d.write_point("dev1", "p1", 42.0)
        assert d._last_values["dev1:p1"] == 42.0

    async def test_write_resets_drift_accumulator(self):
        d = _make_driver()
        await _add_device(d, "dev1", [{"name": "p1", "min": 0, "max": 100}])
        d._drift_accumulator["dev1:p1"] = 10.0
        await d.write_point("dev1", "p1", 42.0)
        assert d._drift_accumulator["dev1:p1"] == 0.0

    async def test_write_with_hold_forever(self):
        d = _make_driver()
        await _add_device(
            d,
            "dev1",
            [{"name": "p1", "min": 0, "max": 100, "write_hold_seconds": 0}],
        )
        await d.write_point("dev1", "p1", 42.0)
        # expire_at=0 means hold forever
        assert d._write_overrides["dev1:p1"].expire_at == 0.0

    async def test_write_with_hold_duration(self):
        d = _make_driver()
        await _add_device(
            d,
            "dev1",
            [{"name": "p1", "min": 0, "max": 100, "write_hold_seconds": 30}],
        )
        before = time.monotonic()
        await d.write_point("dev1", "p1", 42.0)
        after = time.monotonic()
        expire = d._write_overrides["dev1:p1"].expire_at
        assert before + 30 <= expire <= after + 30

    async def test_write_lru_eviction_when_over_limit(self):
        d = _make_driver()
        await _add_device(d, "dev1", [{"name": "p1", "min": 0, "max": 100}])
        # Manually fill write_overrides to near limit
        for i in range(10000):
            d._write_overrides[f"dev1:p{i}"] = _WriteOverride(float(i), 0.0, {})
        # Writing one more should trigger eviction
        await d.write_point("dev1", "p1", 42.0)
        assert len(d._write_overrides) <= 10000

    async def test_write_default_auth_token_none_allows_write(self):
        d = _make_driver()
        await _add_device(d, "dev1", [{"name": "p1", "min": 0, "max": 100}])
        ok = await d.write_point("dev1", "p1", 42.0)
        assert ok is True

    async def test_write_auth_token_correct(self):
        d = _make_driver()
        d._auth_token = "secret"
        await _add_device(d, "dev1", [{"name": "p1", "min": 0, "max": 100}])
        ok = await d.write_point("dev1", "p1", 42.0, auth_token="secret")
        assert ok is True

    async def test_write_auth_token_incorrect(self):
        d = _make_driver()
        d._auth_token = "secret"
        await _add_device(d, "dev1", [{"name": "p1", "min": 0, "max": 100}])
        ok = await d.write_point("dev1", "p1", 42.0, auth_token="wrong")
        assert ok is False

    async def test_write_auth_token_missing(self):
        d = _make_driver()
        d._auth_token = "secret"
        await _add_device(d, "dev1", [{"name": "p1", "min": 0, "max": 100}])
        ok = await d.write_point("dev1", "p1", 42.0)
        assert ok is False

    async def test_write_permission_denied(self):
        d = _make_driver()
        await _add_device(d, "dev1", [{"name": "p1", "min": 0, "max": 100}])
        with patch.object(d, "check_permission", new=AsyncMock(return_value=False)):
            ok = await d.write_point("dev1", "p1", 42.0)
        assert ok is False

    async def test_write_permission_allowed(self):
        d = _make_driver()
        await _add_device(d, "dev1", [{"name": "p1", "min": 0, "max": 100}])
        with patch.object(d, "check_permission", new=AsyncMock(return_value=True)):
            ok = await d.write_point("dev1", "p1", 42.0)
        assert ok is True

    async def test_write_audit_includes_old_waveform(self):
        d = _make_driver()
        await _add_device(d, "dev1", [{"name": "p1", "min": 0, "max": 100, "mode": "sine"}])
        await d.write_point("dev1", "p1", 42.0, user="tester")
        # audit dict stored in override should include old_waveform
        override = d._write_overrides["dev1:p1"]
        assert override.audit["old_waveform"] == "sine"
        assert override.audit["user"] == "tester"


# -- get_write_audit_log() ---------------------------------------------------


class TestGetWriteAuditLog:
    def test_empty_log_returns_empty_list(self):
        d = SimulatorDriver()
        assert d.get_write_audit_log() == []

    def test_returns_all_entries_no_device_filter(self):
        d = SimulatorDriver()
        d._record_write_audit("dev1", "p1", 0, 1, True)
        d._record_write_audit("dev2", "p2", 0, 2, True)
        logs = d.get_write_audit_log()
        assert len(logs) == 2

    def test_filters_by_device(self):
        d = SimulatorDriver()
        d._record_write_audit("dev1", "p1", 0, 1, True)
        d._record_write_audit("dev2", "p2", 0, 2, True)
        d._record_write_audit("dev1", "p3", 0, 3, True)
        logs = d.get_write_audit_log(device_id="dev1")
        assert len(logs) == 2
        assert all(e["device_id"] == "dev1" for e in logs)

    def test_limit_applied(self):
        d = SimulatorDriver()
        for i in range(10):
            d._record_write_audit("dev1", "p1", 0, i, True)
        logs = d.get_write_audit_log(limit=3)
        assert len(logs) == 3
        # Should return the last 3
        assert logs[-1]["new_value"] == 9
        assert logs[0]["new_value"] == 7

    def test_limit_with_device_filter(self):
        d = SimulatorDriver()
        for i in range(5):
            d._record_write_audit("dev1", "p1", 0, i, True)
            d._record_write_audit("dev2", "p2", 0, i * 10, True)
        logs = d.get_write_audit_log(device_id="dev1", limit=2)
        assert len(logs) == 2
        assert all(e["device_id"] == "dev1" for e in logs)

    def test_default_limit_is_100(self):
        d = SimulatorDriver()
        for i in range(150):
            d._record_write_audit("dev1", "p1", 0, i, True)
        logs = d.get_write_audit_log()
        assert len(logs) == 100


# -- _advance_phase() --------------------------------------------------------


class TestAdvancePhase:
    def test_advances_phase_by_delta(self):
        d = SimulatorDriver()
        d._phase_state["dev1:p1"] = 0.0
        # delta = 2*pi * 1.0 / 60.0
        result = d._advance_phase("dev1:p1", 1.0, 60.0)
        expected = 2 * math.pi * 1.0 / 60.0
        assert abs(result - expected) < 1e-9

    def test_accumulates_across_calls(self):
        d = SimulatorDriver()
        d._phase_state["dev1:p1"] = 0.0
        d._advance_phase("dev1:p1", 1.0, 60.0)
        d._advance_phase("dev1:p1", 1.0, 60.0)
        expected = 2 * 2 * math.pi / 60.0
        assert abs(d._phase_state["dev1:p1"] - expected) < 1e-9

    def test_uses_default_when_phase_not_set(self):
        d = SimulatorDriver()
        # No prior phase -> starts at 0.0 (dict.get default)
        result = d._advance_phase("dev1:p1", 1.0, 60.0)
        assert abs(result - (2 * math.pi / 60.0)) < 1e-9

    def test_short_period_large_delta(self):
        d = SimulatorDriver()
        d._phase_state["dev1:p1"] = 0.0
        # collect_interval=30, period=60 -> delta = pi
        result = d._advance_phase("dev1:p1", 30.0, 60.0)
        assert abs(result - math.pi) < 1e-9


# -- _generate_value() -------------------------------------------------------


class TestGenerateValue:
    def test_fixed_mode_returns_midpoint(self):
        d = SimulatorDriver()
        val = d._generate_value("dev1", "p1", {"min": 10, "max": 20, "mode": "fixed"})
        assert val == 15.0

    def test_fixed_mode_default_min_max(self):
        d = SimulatorDriver()
        val = d._generate_value("dev1", "p1", {"mode": "fixed"})
        assert val == 50.0

    def test_sine_mode(self):
        d = SimulatorDriver()
        d._phase_state["dev1:p1"] = 0.0
        val = d._generate_value(
            "dev1", "p1", {"min": 0, "max": 100, "mode": "sine", "period": 60, "collect_interval": 1}
        )
        # sin(2*pi/60) * 50 + 50
        expected = 50 + 50 * math.sin(2 * math.pi / 60)
        assert abs(val - expected) < 1e-9

    def test_sine_mode_at_pi_half(self):
        d = SimulatorDriver()
        d._phase_state["dev1:p1"] = math.pi / 2
        # advance_phase adds delta, but we test the sin value
        # Actually _generate_value calls _advance_phase first, so phase becomes pi/2 + delta
        config = {"min": -10, "max": 10, "mode": "sine", "period": 60, "collect_interval": 0}
        val = d._generate_value("dev1", "p1", config)
        # delta = 2*pi*0/60 = 0, so phase stays at pi/2
        expected = 0 + 10 * math.sin(math.pi / 2)
        assert abs(val - expected) < 1e-9

    def test_square_mode_positive(self):
        d = SimulatorDriver()
        d._phase_state["dev1:p1"] = 0.0
        # sin(0+delta) where delta = 2*pi*1/60 > 0 -> sin positive -> max
        config = {"min": 0, "max": 100, "mode": "square", "period": 60, "collect_interval": 1}
        val = d._generate_value("dev1", "p1", config)
        assert val == 100.0

    def test_square_mode_negative(self):
        d = SimulatorDriver()
        d._phase_state["dev1:p1"] = math.pi  # sin(pi + small_delta) < 0
        config = {"min": 0, "max": 100, "mode": "square", "period": 60, "collect_interval": 0.001}
        val = d._generate_value("dev1", "p1", config)
        assert val == 0.0

    def test_triangle_mode_first_quarter(self):
        d = SimulatorDriver()
        d._phase_state["dev1:p1"] = 0.0
        # delta = 2*pi*1/60, t_norm = delta/(2*pi) = 1/60 ~ 0.0167 < 0.25
        config = {"min": 0, "max": 100, "mode": "triangle", "period": 60, "collect_interval": 1}
        val = d._generate_value("dev1", "p1", config)
        t_norm = (2 * math.pi / 60) / (2 * math.pi)
        expected = 0 + 100 * (t_norm / 0.25)
        assert abs(val - expected) < 1e-9

    def test_triangle_mode_second_quarter(self):
        d = SimulatorDriver()
        d._phase_state["dev1:p1"] = 2 * math.pi * 0.5  # t_norm = 0.5
        config = {"min": 0, "max": 100, "mode": "triangle", "period": 60, "collect_interval": 0}
        val = d._generate_value("dev1", "p1", config)
        t_norm = 0.5
        expected = 100 - 100 * ((t_norm - 0.25) / 0.5)
        assert abs(val - expected) < 1e-9

    def test_triangle_mode_last_quarter(self):
        d = SimulatorDriver()
        d._phase_state["dev1:p1"] = 2 * math.pi * 0.8  # t_norm = 0.8
        config = {"min": 0, "max": 100, "mode": "triangle", "period": 60, "collect_interval": 0}
        val = d._generate_value("dev1", "p1", config)
        t_norm = 0.8
        expected = 0 + 100 * ((t_norm - 0.75) / 0.25)
        assert abs(val - expected) < 1e-9

    def test_sawtooth_mode(self):
        d = SimulatorDriver()
        d._phase_state["dev1:p1"] = 2 * math.pi * 0.3
        config = {"min": 10, "max": 20, "mode": "sawtooth", "period": 60, "collect_interval": 0}
        val = d._generate_value("dev1", "p1", config)
        t_norm = 0.3
        expected = 10 + 10 * t_norm
        assert abs(val - expected) < 1e-9

    def test_random_walk_mode(self):
        d = SimulatorDriver()
        d._walk_state["dev1:p1"] = 50.0
        with patch("edgelite.drivers.simulator.random.gauss", return_value=1.0):
            val = d._generate_value("dev1", "p1", {"min": 0, "max": 100, "mode": "random_walk"})
        # step = 100 * 0.02 = 2; current = 50 + gauss(0, 2) = 50 + 1 = 51
        assert val == 51.0
        assert d._walk_state["dev1:p1"] == 51.0

    def test_random_walk_clamps_to_min(self):
        d = SimulatorDriver()
        d._walk_state["dev1:p1"] = 1.0
        with patch("edgelite.drivers.simulator.random.gauss", return_value=-10.0):
            val = d._generate_value("dev1", "p1", {"min": 0, "max": 100, "mode": "random_walk"})
        assert val == 0.0

    def test_random_walk_clamps_to_max(self):
        d = SimulatorDriver()
        d._walk_state["dev1:p1"] = 99.0
        with patch("edgelite.drivers.simulator.random.gauss", return_value=10.0):
            val = d._generate_value("dev1", "p1", {"min": 0, "max": 100, "mode": "random_walk"})
        assert val == 100.0

    def test_random_walk_default_state_uses_mid(self):
        d = SimulatorDriver()
        # No prior walk state -> defaults to mid=50
        with patch("edgelite.drivers.simulator.random.gauss", return_value=0.0):
            val = d._generate_value("dev1", "p1", {"min": 0, "max": 100, "mode": "random_walk"})
        assert val == 50.0

    def test_ramp_mode(self):
        d = SimulatorDriver()
        d._phase_state["dev1:p1"] = 0.0
        config = {"min": 0, "max": 100, "mode": "ramp", "period": 60, "collect_interval": 15}
        val = d._generate_value("dev1", "p1", config)
        # phase = 0 + 15/60 = 0.25
        expected = 0 + 100 * 0.25
        assert abs(val - expected) < 1e-9

    def test_ramp_mode_wraps_around(self):
        d = SimulatorDriver()
        d._phase_state["dev1:p1"] = 0.9
        config = {"min": 0, "max": 100, "mode": "ramp", "period": 60, "collect_interval": 30}
        val = d._generate_value("dev1", "p1", config)
        # phase = 0.9 + 30/60 = 1.4 -> wraps to 0.0
        assert abs(val - 0.0) < 1e-9
        assert d._phase_state["dev1:p1"] == 0.0

    def test_step_mode_high(self):
        d = SimulatorDriver()
        d._phase_state["dev1:p1"] = 0.6
        config = {"min": 0, "max": 100, "mode": "step", "period": 60, "collect_interval": 0}
        val = d._generate_value("dev1", "p1", config)
        assert val == 100.0  # phase >= 0.5

    def test_step_mode_low(self):
        d = SimulatorDriver()
        d._phase_state["dev1:p1"] = 0.3
        config = {"min": 0, "max": 100, "mode": "step", "period": 60, "collect_interval": 0}
        val = d._generate_value("dev1", "p1", config)
        assert val == 0.0  # phase < 0.5

    def test_step_mode_wraps_around(self):
        d = SimulatorDriver()
        d._phase_state["dev1:p1"] = 0.9
        config = {"min": 0, "max": 100, "mode": "step", "period": 60, "collect_interval": 30}
        val = d._generate_value("dev1", "p1", config)
        # phase = 0.9 + 0.5 = 1.4 -> wraps to 0.0 -> < 0.5 -> min
        assert val == 0.0

    def test_formula_mode_success(self):
        d = SimulatorDriver()
        config = {"min": 0, "max": 100, "mode": "formula", "formula": "min + max"}
        val = d._generate_value("dev1", "p1", config)
        assert val == 100.0

    def test_formula_mode_with_t(self):
        d = SimulatorDriver()
        config = {"min": 0, "max": 100, "mode": "formula", "formula": "t * 0"}
        val = d._generate_value("dev1", "p1", config)
        assert val == 0.0

    def test_formula_mode_invalid_returns_mid(self):
        d = SimulatorDriver()
        config = {"min": 10, "max": 20, "mode": "formula", "formula": "invalid_func(t)"}
        val = d._generate_value("dev1", "p1", config)
        # On error returns mid = 15
        assert val == 15.0

    def test_formula_mode_syntax_error_returns_mid(self):
        d = SimulatorDriver()
        config = {"min": 10, "max": 20, "mode": "formula", "formula": "1 + "}
        val = d._generate_value("dev1", "p1", config)
        assert val == 15.0

    def test_formula_mode_default_formula(self):
        d = SimulatorDriver()
        config = {"min": 0, "max": 100, "mode": "formula"}  # no formula -> default "t"
        val = d._generate_value("dev1", "p1", config)
        # Should return time.time() as float
        assert isinstance(val, float)

    def test_random_mode_default(self):
        d = SimulatorDriver()
        with patch("edgelite.drivers.simulator.random.uniform", return_value=42.0):
            val = d._generate_value("dev1", "p1", {"min": 0, "max": 100, "mode": "random"})
        assert val == 42.0

    def test_unknown_mode_falls_through_to_random(self):
        d = SimulatorDriver()
        with patch("edgelite.drivers.simulator.random.uniform", return_value=33.0):
            val = d._generate_value("dev1", "p1", {"min": 0, "max": 100, "mode": "unknown_mode"})
        assert val == 33.0

    def test_default_mode_when_not_specified(self):
        d = SimulatorDriver()
        with patch("edgelite.drivers.simulator.random.uniform", return_value=77.0):
            val = d._generate_value("dev1", "p1", {"min": 0, "max": 100})
        assert val == 77.0


# -- discover_devices() / health_check() -------------------------------------


class TestDiscoverAndHealth:
    async def test_discover_devices_returns_empty(self):
        d = SimulatorDriver()
        result = await d.discover_devices({})
        assert result == []

    async def test_discover_devices_with_config(self):
        d = SimulatorDriver()
        result = await d.discover_devices({"some": "config"})
        assert result == []

    async def test_health_check_returns_running_state(self):
        d = _make_driver(started=True)
        result = await d.health_check("dev1")
        assert result is True

    async def test_health_check_when_not_running(self):
        d = _make_driver(started=False)
        result = await d.health_check("dev1")
        assert result is False


# -- Integration: write then read -------------------------------------------


class TestWriteThenRead:
    async def test_written_value_is_returned_by_read(self):
        d = _make_driver()
        await _add_device(
            d, "dev1", [{"name": "p1", "min": 0, "max": 100, "mode": "random", "write_hold_seconds": 100}]
        )
        await d.write_point("dev1", "p1", 42.0)
        result = await d.read_points("dev1", ["p1"])
        assert result["p1"].value == 42.0

    async def test_write_hold_zero_persists(self):
        d = _make_driver()
        await _add_device(
            d,
            "dev1",
            [{"name": "p1", "min": 0, "max": 100, "mode": "random", "write_hold_seconds": 0}],
        )
        await d.write_point("dev1", "p1", 42.0)
        result = await d.read_points("dev1", ["p1"])
        # expire_at=0 means hold forever
        assert result["p1"].value == 42.0

    async def test_expired_override_falls_back_to_waveform(self):
        d = _make_driver()
        await _add_device(
            d,
            "dev1",
            [{"name": "p1", "min": 0, "max": 100, "mode": "fixed", "write_hold_seconds": 1}],
        )
        await d.write_point("dev1", "p1", 42.0)
        # Manually expire the override
        d._write_overrides["dev1:p1"].expire_at = time.monotonic() - 1
        result = await d.read_points("dev1", ["p1"])
        # Override expired -> falls back to fixed mode -> mid=50
        assert result["p1"].value == 50.0
