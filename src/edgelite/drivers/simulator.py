"""模拟器驱动 - 无需外部连接，纯内存实现"""

from __future__ import annotations

import asyncio
import collections
import hashlib
import logging
import math
import random
import time
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any

from edgelite.api.debug import record_packet
from edgelite.api.error_codes import SimulatorDriverErrors
from edgelite.drivers.base import DriverCapabilities, DriverPlugin, PointValue
from edgelite.services.i18n import t as _t

logger = logging.getLogger(__name__)

_FAULT_MODES = ("timeout", "disconnect", "data_error")

_WAVE_MODES = [
    "random",
    "sine",
    "square",
    "triangle",
    "sawtooth",
    "random_walk",
    "ramp",
    "step",
    "formula",
    "fixed",
]


class _WriteOverride:
    __slots__ = ("value", "expire_at", "audit")

    def __init__(self, value: float, expire_at: float, audit: dict):
        self.value = value
        self.expire_at = expire_at
        self.audit = audit


class SimulatorDriver(DriverPlugin):
    """模拟器驱动，生成模拟测点数据"""

    plugin_name = "simulator"
    plugin_version = "0.1.0"
    supported_protocols = ("simulator",)  # FIXED(P2): 原问题-可变默认值list; 修复-改为tuple
    _production_safe = False
    config_schema = {
        "description": "Simulator driver, generates simulated data without external connection",
        "required": [],
        "properties": {},
        "fields": [
            {
                "name": "update_interval",
                "type": "number",
                "label": "Update Interval (s)",
                "description": "Data update interval in seconds",
                "default": 1.0,
                "min": 0.1,
                "max": 3600,
            },
            {
                "name": "value_range_min",
                "type": "number",
                "label": "Min Value",
                "description": "Minimum simulated value",
                "default": 0.0,
            },
            {
                "name": "value_range_max",
                "type": "number",
                "label": "Max Value",
                "description": "Maximum simulated value",
                "default": 100.0,
            },
            {
                "name": "noise_amplitude",
                "type": "number",
                "label": "Noise Amplitude",
                "description": "Random noise amplitude overlaid on waveform",
                "default": 0.0,
                "min": 0,
            },
            {
                "name": "trend_drift",
                "type": "number",
                "label": "Trend Drift/s",
                "description": "Linear drift per second added to base waveform",
                "default": 0.0,
            },
            {
                "name": "timeout",
                "type": "number",
                "label": "Timeout (s)",
                "description": "Operation timeout in seconds",
                "default": 5.0,
                "min": 0,
                "max": 60,
            },
            {
                "name": "sim_mode",
                "type": "string",
                "label": "Simulation Mode",
                "description": "Default simulation mode for new points",
                "default": "random",
                "options": _WAVE_MODES,
            },
            {
                "name": "period",
                "type": "number",
                "label": "Period (s)",
                "description": "Wave period in seconds for periodic modes",
                "default": 60.0,
                "min": 1,
                "max": 3600,
            },
            {
                "name": "formula",
                "type": "string",
                "label": "Custom Formula",
                "description": "Custom formula using t (time), min, max, math functions",
                "default": "t",
            },
            {
                "name": "fault_simulation",
                "type": "string",
                "label": "Fault Simulation",
                "description": "Simulate device faults: none, timeout, disconnect, data_error, random",
                "default": "none",
                "options": ["none", "timeout", "disconnect", "data_error", "random"],
            },
            {
                "name": "fault_rate",
                "type": "number",
                "label": "Fault Rate (%)",
                "description": "Probability of fault occurrence (0-100%)",
                "default": 0,
                "min": 0,
                "max": 100,
            },
            {
                "name": "deadband",
                "type": "number",
                "label": "Deadband",
                "description": "Deadband filter threshold (0=disabled)",
                "default": 0,
                "min": 0,
            },
            {
                "name": "deadband_type",
                "type": "string",
                "label": "Deadband Type",
                "description": "Deadband type: absolute or percent",
                "default": "absolute",
                "options": ["absolute", "percent"],
            },
            {
                "name": "scaling_ratio",
                "type": "number",
                "label": "Scaling Ratio",
                "description": "Linear scaling ratio (1.0=disabled)",
                "default": 1.0,
            },
            {
                "name": "scaling_offset",
                "type": "number",
                "label": "Scaling Offset",
                "description": "Linear scaling offset (0.0=disabled)",
                "default": 0.0,
            },
            {
                "name": "clamp_min",
                "type": "number",
                "label": "Clamp Min",
                "description": "Minimum allowed value (empty=no limit)",
            },
            {
                "name": "clamp_max",
                "type": "number",
                "label": "Clamp Max",
                "description": "Maximum allowed value (empty=no limit)",
            },
            {
                "name": "rate_of_change_threshold",
                "type": "number",
                "label": "Rate of Change Threshold",
                "description": "Mark quality=uncertain when change exceeds this per second (0=disabled)",
                "default": 0,
                "min": 0,
            },
            {
                "name": "frozen_threshold",
                "type": "integer",
                "label": "Frozen Detection Count",
                "description": "Consecutive identical readings to detect frozen value (0=disabled)",
                "default": 0,
                "min": 0,
                "max": 1000,
            },
            {
                "name": "write_hold_seconds",
                "type": "number",
                "label": "Write Hold (s)",
                "description": "Seconds to hold written value before resuming waveform (0=hold forever)",
                "default": 10.0,
                "min": 0,
                "max": 3600,
            },
        ],
    }

    experimental = False
    capabilities = DriverCapabilities(
        discover=False, read=True, write=True, subscribe=False, batch_read=True, batch_write=False
    )
    constraints = ()  # FIXED(P2): 原问题-可变默认值list; 修复-改为tuple

    def __init__(self):
        super().__init__()
        self._running = False
        self._devices: dict[str, dict[str, dict]] = {}
        self._walk_state: dict[str, float] = {}
        self._phase_state: dict[str, float] = {}
        self._last_values: dict[str, Any] = {}
        self._last_timestamp: dict[str, float] = {}
        self._frozen_count: dict[str, int] = {}
        self._drift_accumulator: dict[str, float] = {}
        self._write_overrides: OrderedDict[str, _WriteOverride] = OrderedDict()  # FIXED-P2: 改为OrderedDict支持LRU淘汰
        self._write_audit_log: collections.deque = collections.deque(maxlen=1000)
        self._lock = asyncio.Lock()
        # _health_stats inherited from base class (dict[str, DriverHealthStats])  # FIXED-P2: 删除遮蔽基类的覆盖初始化
        self._auth_token: str | None = None

    async def start(self, config: dict) -> None:
        environment = config.get("environment", "")
        # #[AUDIT-FIX] WARNING: 生产环境检查大小写敏感，"Production"/"PRODUCTION" 会绕过保护
        if environment and environment.lower() == "production" and not self._production_safe:
            raise RuntimeError("SimulatorDriver is not production-safe and must not run in production environment")
        self._auth_token = config.get("auth_token")
        self._running = True
        logger.info("模拟器驱动启动")

    async def stop(self) -> None:
        self._running = False
        logger.info("模拟器驱动停止")
        # FIXED-P1: SIM-R01 调用基类stop()确保_shutdown_executor和_cancel_background_tasks执行
        await super().stop()

    async def add_device(self, device_id: str, config: dict, points: list[dict] | None = None) -> None:
        if points is None:
            points = []
        self._devices[device_id] = {}
        for pt in points:
            name = pt.get("name")
            if name is None:
                continue
            self._devices[device_id][name] = pt
            mid = (pt.get("min", 0) + pt.get("max", 100)) / 2
            key = f"{device_id}:{name}"
            self._walk_state[key] = mid
            self._phase_state[key] = random.uniform(0, 2 * math.pi)
            self._drift_accumulator[key] = 0.0

    async def remove_device(self, device_id: str) -> None:
        # FIXED-BugR4X: 原问题-remove_device遍历并修改多个共享字典未获取self._lock，与read_points/write_point并发竞态，修复-整个方法体放入self._lock
        async with self._lock:
            self._devices.pop(device_id, None)
            prefix = f"{device_id}:"
            for d in (
                self._walk_state,
                self._phase_state,
                self._last_values,
                self._last_timestamp,
                self._frozen_count,
                self._drift_accumulator,
                self._write_overrides,
            ):
                keys_to_remove = [k for k in d if k.startswith(prefix)]
                for k in keys_to_remove:
                    del d[k]

    def _log_error(self, device_id: str, error_code: str, message: str) -> None:
        i18n_msg = _t(error_code)
        logger.error(
            "[%s] device=%s code=%s i18n=%s msg=%s",
            self.plugin_name,
            device_id,
            error_code,
            i18n_msg,
            message,
        )

    def _resolve_fault_mode(self, device_id: str) -> str | None:
        device_config = self._devices.get(device_id, {})
        fault_mode = device_config.get("__fault_mode__", None)
        if fault_mode is None or fault_mode == "none":
            return None
        fault_rate = device_config.get("__fault_rate__", 0)
        if fault_rate <= 0:
            return None
        if random.uniform(0, 100) >= fault_rate:
            return None
        if fault_mode == "random":
            return random.choice(_FAULT_MODES)
        return fault_mode

    def _make_bad_result(self, points: list[str], now: datetime) -> dict[str, PointValue]:
        return {p: PointValue(value=None, timestamp=now, quality="bad", source="simulated") for p in points}

    def _check_write_override(self, key: str) -> float | None:
        override = self._write_overrides.get(key)
        if override is None:
            return None
        if override.expire_at > 0 and time.monotonic() >= override.expire_at:
            del self._write_overrides[key]
            return None
        return override.value

    def _record_write_audit(
        self, device_id: str, point: str, old_value: Any, new_value: Any, result: bool, user: str = ""
    ) -> None:
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "user": user,
            "device_id": device_id,
            "point_id": point,
            "old_value": old_value,
            "new_value": new_value,
            "result": "ok" if result else "failed",
        }
        self._write_audit_log.append(entry)

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        now = datetime.now(UTC)
        if device_id not in self._devices:
            return {}

        active_fault = self._resolve_fault_mode(device_id)

        if active_fault == "timeout":
            record_packet("tx", "simulator", device_id, f"Read: {points}")
            self._log_error(device_id, SimulatorDriverErrors.FAULT_TIMEOUT, "fault simulation: timeout")
            self._record_read_failure(device_id)
            for _ in range(35):
                await asyncio.sleep(1)
                if not self._running:
                    break
            record_packet("rx", "simulator", device_id, "Timeout")
            return self._make_bad_result(points, now)

        if active_fault == "disconnect":
            record_packet("tx", "simulator", device_id, f"Read: {points}")
            self._log_error(device_id, SimulatorDriverErrors.FAULT_DISCONNECT, "fault simulation: disconnect")
            self._record_read_failure(device_id)
            record_packet("rx", "simulator", device_id, "Disconnect")
            return self._make_bad_result(points, now)

        if active_fault == "data_error":
            record_packet("tx", "simulator", device_id, f"Read: {points}")
            self._log_error(device_id, SimulatorDriverErrors.FAULT_DATA_ERROR, "fault simulation: data error")
            self._record_read_failure(device_id)
            result = self._make_bad_result(points, now)
            record_packet("rx", "simulator", device_id, f"DataError: {list(result.keys())}")
            return result

        record_packet("tx", "simulator", device_id, f"Read: {points}")
        result = {}
        now_ts = time.monotonic()
        async with self._lock:
            device_points = self._devices[device_id]
            for point_name in points:
                pt_config = device_points.get(point_name)
                if pt_config is None:
                    continue

                key = f"{device_id}:{point_name}"

                override_val = self._check_write_override(key)
                if override_val is not None:
                    value = override_val
                else:
                    value = self._generate_value(device_id, point_name, pt_config)

                    noise_amplitude = pt_config.get("noise_amplitude", 0.0)
                    if noise_amplitude > 0:
                        value += random.gauss(0, noise_amplitude)

                    trend_drift = pt_config.get("trend_drift", 0.0)
                    if trend_drift != 0.0:
                        collect_interval = pt_config.get("collect_interval", 1.0)
                        self._drift_accumulator[key] = (
                            self._drift_accumulator.get(key, 0.0) + trend_drift * collect_interval
                        )
                        # FIXED-P4: 漂移累加器超过数据范围时重置，防止无限增长
                        acc = self._drift_accumulator[key]
                        max_range = pt_config.get("max", 100) - pt_config.get("min", 0)
                        if abs(acc) > max_range * 10:
                            self._drift_accumulator[key] = 0.0
                        value += self._drift_accumulator[key]

                quality = "good"

                rate_threshold = pt_config.get("rate_of_change_threshold", 0)
                if rate_threshold and rate_threshold > 0:
                    last_v = self._last_values.get(key)
                    last_ts = self._last_timestamp.get(key)
                    if (
                        last_v is not None
                        and last_ts is not None
                        and isinstance(value, (int, float))
                        and isinstance(last_v, (int, float))
                    ):
                        dt = now_ts - last_ts
                        if dt > 0:
                            roc = abs(value - last_v) / dt
                            if roc > rate_threshold:
                                quality = "uncertain"

                frozen_threshold = pt_config.get("frozen_threshold", 0)
                if frozen_threshold and frozen_threshold > 0:
                    last_v = self._last_values.get(key)
                    if last_v is not None and isinstance(value, (int, float)) and isinstance(last_v, (int, float)):
                        if abs(value - last_v) < 1e-9:
                            self._frozen_count[key] = self._frozen_count.get(key, 0) + 1
                            if self._frozen_count[key] >= frozen_threshold:
                                quality = "uncertain"
                        else:
                            self._frozen_count[key] = 0

                deadband = pt_config.get("deadband")
                if deadband is not None and deadband > 0:
                    db_type = pt_config.get("deadband_type", "absolute")
                    db_config = {"type": db_type, "threshold": deadband} if db_type == "percent" else deadband
                    last_val = self._last_values.get(key)
                    value = self._apply_deadband(value, last_val, db_config)

                scaling_ratio = pt_config.get("scaling_ratio")
                scaling_offset = pt_config.get("scaling_offset")
                if scaling_ratio is not None or scaling_offset is not None:
                    scaling = {
                        "ratio": scaling_ratio if scaling_ratio is not None else 1.0,
                        "offset": scaling_offset if scaling_offset is not None else 0.0,
                    }
                    value = self._apply_scaling(value, scaling)

                clamp_min = pt_config.get("clamp_min")
                clamp_max = pt_config.get("clamp_max")
                if clamp_min is not None or clamp_max is not None:
                    clamp = {}
                    if clamp_min is not None:
                        clamp["min"] = clamp_min
                    if clamp_max is not None:
                        clamp["max"] = clamp_max
                    clamped, in_range = self._apply_clamp(value, clamp)
                    if not in_range:
                        self._log_error(
                            device_id,
                            SimulatorDriverErrors.VALUE_OUT_OF_RANGE,
                            f"{point_name}: value {value} out of clamp {clamp}",
                        )
                        result[point_name] = PointValue(value=None, timestamp=now, quality="bad", source="simulated")
                        self._last_values[key] = None
                        self._last_timestamp[key] = now_ts
                        continue
                    value = clamped

                self._last_values[key] = value
                self._last_timestamp[key] = now_ts
                result[point_name] = PointValue(value=value, timestamp=now, quality=quality, source="simulated")

        await self._record_read_success(device_id)
        record_packet("rx", "simulator", device_id, f"Result: {list(result.keys())}")
        return result

    async def write_point(
        self, device_id: str, point: str, value: Any, user: str = "", auth_token: str | None = None
    ) -> bool:
        # SEC-FIX(修复2): 驱动层写入权限检查，保持与其他驱动一致；模拟器无角色锁时放行
        if hasattr(self, "check_permission"):
            from edgelite.security.rbac import Permission

            if not await self.check_permission(Permission.DEVICE_WRITE_POINT):
                logger.warning(
                    "[simulator] write denied: role=%s lacks device:write_point, device=%s point=%s",
                    getattr(self, "_current_user_role", "unknown"),
                    device_id,
                    point,
                )
                return False
        if self._auth_token is not None:
            if auth_token != self._auth_token:
                logger.warning("[simulator] write_point auth failed: device=%s point=%s", device_id, point)
                return False
        if value is None:
            logger.warning("[simulator] write_point rejected: value is None for device=%s point=%s", device_id, point)
            return False
        if isinstance(value, (int, float)) and (math.isnan(value) or math.isinf(value)):
            logger.warning(
                "[simulator] write_point rejected: invalid numeric value %s for device=%s point=%s",
                value,
                device_id,
                point,
            )
            return False
        record_packet("tx", "simulator", device_id, f"Write: {point}={value}")
        # FIXED-P1: 检查设备是否存在，防止KeyError（write_point未校验device_id）
        if device_id not in self._devices:
            logger.warning("[simulator] write_point rejected: device %s not found", device_id)
            return False
        if point == "__fault_mode__":
            # FIXED-BugR4X: 原问题-write_point修改_devices共享字典未加锁，与read_points并发竞态，修复-字典修改放入self._lock
            async with self._lock:
                self._devices[device_id]["__fault_mode__"] = value
            record_packet("rx", "simulator", device_id, f"FaultModeSet: {value}")
            return True
        if point == "__fault_rate__":
            # FIXED-BugR4X: 原问题-write_point修改_devices共享字典未加锁，与read_points并发竞态，修复-字典修改放入self._lock
            async with self._lock:
                self._devices[device_id]["__fault_rate__"] = float(value)
            record_packet("rx", "simulator", device_id, f"FaultRateSet: {value}")
            return True

        key = f"{device_id}:{point}"
        old_value = self._last_values.get(key)
        old_mode = self._devices.get(device_id, {}).get(point, {}).get("mode", "")

        try:
            float_val = float(value)
        except (ValueError, TypeError):
            self._record_write_audit(device_id, point, old_value, value, False, user)
            self._log_error(device_id, SimulatorDriverErrors.WRITE_FAILED, f"cannot convert {value} to float")
            return False

        device_points = self._devices.get(device_id, {})
        pt_config = device_points.get(point, {})
        write_hold = pt_config.get("write_hold_seconds", 10.0)

        expire_at = 0.0 if write_hold <= 0 else time.monotonic() + write_hold
        audit = {
            "timestamp": datetime.now(UTC).isoformat(),
            "user": user,
            "device_id": device_id,
            "point_id": point,
            "old_value": old_value,
            "old_waveform": old_mode,
            "new_value": float_val,
            "result": "ok",
        }
        # FIXED-BugR4X: 原问题-write_point修改_write_overrides/_walk_state/_last_values/_drift_accumulator共享字典未加锁，与read_points并发时OrderedDict竞态，修复-字典修改放入self._lock
        async with self._lock:
            self._write_overrides[key] = _WriteOverride(float_val, expire_at, audit)
            # FIXED-P2: OrderedDict LRU淘汰替代手动清理
            if key in self._write_overrides:
                self._write_overrides.move_to_end(key)
            while len(self._write_overrides) > 10000:
                self._write_overrides.popitem(last=False)
            self._walk_state[key] = float_val
            self._last_values[key] = float_val
            self._drift_accumulator[key] = 0.0

        self._record_write_audit(device_id, point, old_value, float_val, True, user)
        record_packet("rx", "simulator", device_id, f"WriteOK: {point}={value}")
        return True

    def get_write_audit_log(self, device_id: str | None = None, limit: int = 100) -> list[dict]:
        entries = list(self._write_audit_log)
        if device_id is None:
            return entries[-limit:]
        return [e for e in entries if e.get("device_id") == device_id][-limit:]

    def _advance_phase(self, key: str, collect_interval: float, period: float) -> float:
        phase = self._phase_state.get(key, 0.0)
        phase += 2 * math.pi * collect_interval / period
        self._phase_state[key] = phase
        return phase

    def _generate_value(self, device_id: str, point_name: str, config: dict) -> float:
        mode = config.get("mode", "random")
        min_val = config.get("min", 0.0)
        max_val = config.get("max", 100.0)
        # FIXED-P2: min > max 时自动交换并记录警告，防止波形反转
        if min_val > max_val:
            logger.warning("[simulator] %s:%s min(%.2f) > max(%.2f), swapping", device_id, point_name, min_val, max_val)
            min_val, max_val = max_val, min_val
        period = config.get("period", 60.0)
        collect_interval = config.get("collect_interval", 1.0)
        key = f"{device_id}:{point_name}"
        mid = (min_val + max_val) / 2
        amp = (max_val - min_val) / 2

        if mode == "fixed":
            return mid

        elif mode == "sine":
            phase = self._advance_phase(key, collect_interval, period)
            return mid + amp * math.sin(phase)

        elif mode == "square":
            phase = self._advance_phase(key, collect_interval, period)
            return max_val if math.sin(phase) >= 0 else min_val

        elif mode == "triangle":
            phase = self._advance_phase(key, collect_interval, period)
            t_norm = (phase % (2 * math.pi)) / (2 * math.pi)
            if t_norm < 0.25:
                return min_val + (max_val - min_val) * (t_norm / 0.25)
            elif t_norm < 0.75:
                return max_val - (max_val - min_val) * ((t_norm - 0.25) / 0.5)
            else:
                return min_val + (max_val - min_val) * ((t_norm - 0.75) / 0.25)

        elif mode == "sawtooth":
            phase = self._advance_phase(key, collect_interval, period)
            t_norm = (phase % (2 * math.pi)) / (2 * math.pi)
            return min_val + (max_val - min_val) * t_norm

        elif mode == "random_walk":
            current: float = self._walk_state.get(key, mid)
            step = (max_val - min_val) * 0.02
            current += random.gauss(0, step)
            current = max(min_val, min(max_val, current))
            self._walk_state[key] = current
            return current

        elif mode == "ramp":
            phase = self._phase_state.get(key, 0.0)
            phase += collect_interval / period
            if phase > 1.0:
                phase = 0.0
            self._phase_state[key] = phase
            return min_val + (max_val - min_val) * phase

        elif mode == "step":
            phase = self._phase_state.get(key, 0.0)
            phase += collect_interval / period
            if phase > 1.0:
                phase = 0.0
            self._phase_state[key] = phase
            return max_val if phase >= 0.5 else min_val

        elif mode == "formula":
            formula = config.get("formula", "t")
            t = time.monotonic()  # FIXED-P2: 原为 time.time() 返回 epoch 秒(约17亿)，用户写 sin(t) 频率极高；改为 monotonic 从0开始
            try:
                from edgelite.drivers.edge_triggers import (
                    _safe_eval_expr,  # FIXED-P2: eval→AST安全求值，防止属性链逃逸
                )

                result = _safe_eval_expr(formula, {"t": t, "min": min_val, "max": max_val, "pi": math.pi})
                return float(result)
            except Exception as e:
                self._log_error(device_id, SimulatorDriverErrors.FORMULA_EVAL_FAILED, f"{formula}: {e}")
                return mid

        else:
            return random.uniform(min_val, max_val)

    async def discover_devices(self, config: dict) -> list[dict]:
        return []

    async def health_check(self, device_id: str) -> bool:
        return self._running
