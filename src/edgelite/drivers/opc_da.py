"""OPC DA Client驱动 - 基于OpenOPC/OPCDA库，连接经典OPC DA Server

OPC DA (Data Access) 是Windows平台传统的工业数据访问标准，
大量老旧SCADA/DCS系统仍使用OPC DA。.NET/COM技术栈。
通过OpenOPC-Python3或OPCDA-Client库实现跨平台访问。

也支持通过OPC DA Gateway代理访问非Windows平台。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import json
import logging
import queue
import threading
import time
from collections import OrderedDict, deque
from datetime import UTC, datetime
from typing import Any

from edgelite.api.error_codes import OpcDaDriverErrors
from edgelite.drivers.base import DriverCapabilities, DriverPlugin, PointValue
from edgelite.packet_recorder import record_packet
from edgelite.services.i18n import t as _t

logger = logging.getLogger(__name__)

try:
    import pywintypes

    _COM_ERROR_TYPES = (pywintypes.com_error,)
except ImportError:
    _COM_ERROR_TYPES = ()

try:
    import pythoncom

    _PYTHONCOM_AVAILABLE = True
except ImportError:
    _PYTHONCOM_AVAILABLE = False

# ODA-MED-001: 使用 threading.local() 为每个线程存储独立的 COM 状态
_thread_local = threading.local()

# ODA-MED-001: 跟踪所有需要清理的线程
_active_com_threads: set = set()
_active_com_threads_lock = threading.Lock()

# ODA-MED-001: 线程清理函数映射（用于 atexit）
_thread_cleanup_funcs: dict = {}


def _thread_com_cleanup() -> None:
    """ODA-MED-001: 线程退出时的 COM 清理函数（通过 atexit 注册）"""
    if getattr(_thread_local, "com_initialized", False):
        try:
            pythoncom.CoUninitialize()
            logger.debug("[opc_da] ODA-MED-001: atexit CoUninitialize on thread %d", threading.current_thread().ident)
        except Exception as e:
            logger.warning("[opc_da] thread_com_cleanup failed: %s", e)  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
        _thread_local.com_initialized = False
        # FIXED-P2: 清理后从全局集合和字典中移除线程ID，防止无界增长
        tid = _thread_local.com_thread_id
        _thread_local.com_thread_id = None
        if tid is not None:
            with _active_com_threads_lock:
                _active_com_threads.discard(tid)
            _thread_cleanup_funcs.pop(tid, None)


def _register_thread_cleanup(thread_id: int) -> None:
    """ODA-MED-001: 为线程注册退出清理函数

    FIXED-P1: 移除atexit注册（atexit仅在主进程退出时执行，对工作线程不可靠），
    COM清理改为在executor shutdown前通过_run_in_com_thread主动调用CoUninitialize。
    """
    _thread_cleanup_funcs[thread_id] = True


_DCOM_ERROR_CODES = {
    0x80070005: "E_ACCESSDENIED - DCOM access denied",
    0x80070057: "E_INVALIDARG - Invalid argument",
    0x80004005: "E_FAIL - Unspecified failure",
    0x80010108: "RPC_E_DISCONNECTED - Object disconnected",
    0x800706BE: "RPC_S_CALL_FAILED - Remote procedure call failed",
    0x800706BA: "RPC_S_SERVER_UNAVAILABLE - RPC server unavailable",
    0x800706BF: "RPC_S_CALL_FAILED_DNE - Remote procedure call failed and did not execute",
    0x80080005: "CO_E_SERVEREXECFAILURE - Server execution failure",
    0x8000FFFF: "E_UNEXPECTED - Unexpected failure",
    0x80004002: "E_NOINTERFACE - No such interface supported",
    0x8001010E: "RPC_E_WRONG_THREAD - Wrong apartment thread",
    0x80040154: "REGDB_E_CLASSNOTREG - Class not registered",
    0x80040200: "CO_E_SERVERBUSY - Server busy",
}

_DCOM_NON_RETRYABLE = {"ACCESSDENIED", "CLASSNOTREG"}
_DCOM_BACKOFF_RETRY = {"SERVERBUSY", "SERVEREXECFAILURE"}

_OPC_ACCESS_READ = 1
_OPC_ACCESS_WRITE = 2
_OPC_ACCESS_READWRITE = 3

_VT_TYPE_MAP: dict[int, tuple[type, ...]] = {
    # FIXED-P4: VT_ARRAY类型（8192+基础类型）未在此映射中处理，read_points中单独处理
    2: (bool,),
    3: (int,),
    4: (int,),
    5: (float,),
    6: (float,),
    7: (str,),
    11: (bool,),
    16: (int,),
    17: (int,),
    19: (int,),
    21: (int,),
    22: (int,),
    23: (int,),
}

_VT_TYPE_NAMES: dict[int, str] = {
    0: "VT_EMPTY",
    1: "VT_NULL",
    2: "VT_I2",
    3: "VT_I4",
    4: "VT_R4",
    5: "VT_R8",
    6: "VT_CY",
    7: "VT_DATE",
    8: "VT_BSTR",
    11: "VT_BOOL",
    16: "VT_I1",
    17: "VT_UI1",
    18: "VT_UI2",
    19: "VT_UI4",
    20: "VT_I8",
    21: "VT_UI8",
    22: "VT_INT",
    23: "VT_UINT",
}

_OPC_QUALITY_HEX_MAP: dict[int, str] = {
    0xC0: "good",
    0xD8: "good",
    0x40: "uncertain",
    0x44: "uncertain",
    0x50: "uncertain",
    0x54: "uncertain",
    0x58: "uncertain",
    0x5C: "uncertain",
    0x60: "uncertain",
    0x64: "uncertain",
    0x68: "uncertain",
    0x6C: "uncertain",
    0x70: "uncertain",
    0x74: "uncertain",
    0x78: "uncertain",
    0x7C: "uncertain",
    0x00: "bad",
    0x04: "bad",
    0x08: "bad",
    0x0C: "bad",
    0x10: "bad",
    0x14: "bad",
    0x18: "bad",
    0x1C: "bad",
    0x20: "bad",
    0x24: "bad",
    0x28: "bad",
    0x2C: "bad",
    0x30: "bad",
    0x34: "bad",
    0x38: "bad",
    0x3C: "bad",
}

_OPC_QUALITY_STR_MAP: dict[str, str] = {
    "Good": "good",
    "GoodLocalOverride": "good",
    "Uncertain": "uncertain",
    "UncertainLastUsable": "uncertain",
    "UncertainSensorNotAccurate": "uncertain",
    "UncertainEngineeringUnitsExceeded": "uncertain",
    "UncertainSubNormal": "uncertain",
    "Bad": "bad",
    "BadNotConnected": "bad",
    "BadDeviceFailure": "bad",
    "BadSensorFailure": "bad",
    "BadLastKnownValue": "bad",
    "BadCommFailure": "bad",
    "BadOutOfService": "bad",
    "BadWaitingForInitialData": "bad",
    "ConfigError": "bad",
    "NotConnected": "bad",
    "Unknown": "bad",
    "WaitingForInitialData": "bad",
    "LastUsableValue": "uncertain",
    "SensorFailure": "bad",
    "LastUsableValue_CommFault": "uncertain",
    "SensorNotAccurate": "uncertain",
    "EngineeringUnitsExceeded": "uncertain",
    "SubNormal": "uncertain",
}


def _map_quality(opc_quality: Any) -> str:
    if opc_quality is None:
        return "bad"
    if isinstance(opc_quality, int):
        mapped = _OPC_QUALITY_HEX_MAP.get(opc_quality & 0xFC)
        if mapped:
            return mapped
        if (opc_quality & 0xC0) == 0xC0:
            return "good"
        if (opc_quality & 0xC0) == 0x40:
            return "uncertain"
        return "bad"
    s = str(opc_quality)
    if not s:
        return "bad"
    mapped = _OPC_QUALITY_STR_MAP.get(s)
    if mapped:
        return mapped
    lower = s.lower()
    if "good" in lower:
        return "good"
    if "uncertain" in lower:
        return "uncertain"
    return "bad"


class _PointState:
    __slots__ = ("last_value", "last_timestamp", "frozen_count")

    def __init__(self):
        self.last_value: Any = None
        self.last_timestamp: float = 0.0
        self.frozen_count: int = 0


class OpcDaComError(Exception):
    def __init__(self, hresult: int, context: str = ""):
        self.hresult = hresult
        hr_unsigned = hresult & 0xFFFFFFFF
        self.error_name = _DCOM_ERROR_CODES.get(hr_unsigned, f"UNKNOWN_0x{hr_unsigned:08X}")
        msg = self.error_name
        if context:
            msg = f"{context}: {msg}"
        super().__init__(msg)


class OpcDaDriver(DriverPlugin):
    """OPC DA Client驱动

    配置参数:
        server: OPC DA Server的ProgID (如"Matrikon.OPC.Simulation")
        host: OPC Server所在主机 (默认localhost)
        gateway: OPC网关地址 (可选，用于远程访问)
        connect_timeout: DCOM连接超时 (默认30s，DCOM较慢)
        username: DCOM远程认证用户名 (可选)
        password: DCOM远程认证密码 (可选)
        rate_of_change: 变化率检查配置 (可选)
        frozen_detect: 冻结值检测配置 (可选)
    """

    plugin_name = "opc_da"
    plugin_version = "1.4.0"
    supported_protocols = ("opc_da",)  # FIXED(P2): 原问题-可变默认值list; 修复-改为tuple
    # #[AUDIT-FIX] FATAL: 缺失 _required_dependencies 声明，导致 registry 无法预检 OpenOPC 依赖
    _required_dependencies: tuple[str, ...] = (
        "OpenOPC",
        "pywintypes",
        "pythoncom",
    )  # FIXED(P2): 原问题-可变默认值list; 修复-改为tuple
    config_schema = {
        "description": "OPC DA classic protocol (Windows COM), reads data from legacy OPC servers",
        "required": ["prog_id"],
        "properties": {"prog_id": {"type": "string", "description": "OPC DA server ProgID"}},
        "fields": [
            {
                "name": "prog_id",
                "type": "string",
                "label": "ProgID",
                "description": "OPC DA server ProgID, e.g. Matrikon.OPC.Simulation",
                "required": True,
            },
            {
                "name": "host",
                "type": "string",
                "label": "Host",
                "description": "OPC server host, leave empty for local machine",
                "default": "localhost",
            },
            {
                "name": "gateway",
                "type": "string",
                "label": "Gateway",
                "description": "OPC gateway address (optional, for remote access)",
                "default": "",
            },
            {
                "name": "connect_timeout",
                "type": "number",
                "label": "Connect Timeout (s)",
                "description": "DCOM connection timeout in seconds (default 30s, DCOM is slow)",
                "default": 30.0,
            },
            {
                "name": "username",
                "type": "string",
                "label": "DCOM Username",
                "description": "Username for remote OPC server DCOM authentication (optional)",
                "default": "",
            },
            {
                "name": "password",
                "type": "string",
                "label": "DCOM Password",
                "description": "Password for remote OPC server DCOM authentication (optional)",
                "default": "",
                "secret": True,
            },
            {
                "name": "use_groups",
                "type": "boolean",
                "label": "Use Groups",
                "description": "Use OPC groups for batch subscription",
                "default": True,
            },
            {
                "name": "update_rate",
                "type": "integer",
                "label": "Update Rate (ms)",
                "description": "OPC group update rate in milliseconds",
                "default": 1000,
            },
            {
                "name": "timeout",
                "type": "number",
                "label": "Timeout (s)",
                "description": "OPC DA communication timeout in seconds",
                "default": 5.0,
            },
            {
                "name": "dcom_call_timeout",
                "type": "number",
                "label": "DCOM Call Timeout (s)",
                "description": "Per-call DCOM timeout in seconds",
                "default": 10.0,
                "min": 1,
                "max": 120,
            },
            {
                "name": "deadband",
                "type": "number",
                "label": "Deadband (%)",
                "description": "Global deadband percentage (0-100). Point-level deadband overrides this",
                "default": 0.0,
                "min": 0,
                "max": 100,
            },
            {
                "name": "scaling",
                "type": "object",
                "label": "Scaling",
                "description": "Global linear scaling: {ratio: float, offset: float}. Point-level scaling overrides this",
                "default": None,
            },
            {
                "name": "clamp",
                "type": "object",
                "label": "Clamp",
                "description": "Global range clamp: {min: float, max: float}. Point-level clamp overrides this",
                "default": None,
            },
            {
                "name": "rate_of_change",
                "type": "object",
                "label": "Rate of Change",
                "description": "Rate of change check: {max_rate: float, unit: 's'|'min'}. Value change per unit time exceeds max_rate → bad quality",
                "default": None,
            },
            {
                "name": "frozen_detect",
                "type": "object",
                "label": "Frozen Detect",
                "description": "Frozen value detection: {consecutive: int, window_s: float}. Same value for N consecutive reads within window → bad quality",
                "default": None,
            },
            {
                "name": "watchdog_interval",
                "type": "number",
                "label": "Watchdog Interval (s)",
                "description": "Connection watchdog interval in seconds",
                "default": 15.0,
            },
        ],
    }

    experimental = True
    # #[AUDIT-FIX] W6: batch_read 改为 True，read_points 实际通过 self._client.read(points) 批量读取多个测点
    capabilities = DriverCapabilities(
        discover=True, read=True, write=True, subscribe=True, batch_read=True, batch_write=False
    )
    constraints = (
        {
            "type": "platform",
            "message": "OPC DA requires Windows with DCOM configured; use opc-da-gateway for cross-platform",
        },
        {
            "type": "platform",
            "message": "Cross-subnet/domain/DCOM permission issues are common; recommend opc-da-gateway proxy",
        },
    )  # FIXED(P2): 原问题-可变默认值list; 修复-改为tuple

    _MAX_RECONNECT_ATTEMPTS = 3
    _RECONNECT_BASE_DELAY = 1.0
    _RECONNECT_MAX_DELAY = 60.0
    _BUSY_RETRY_BASE_DELAY = 2.0
    _BUSY_RETRY_MAX_DELAY = 30.0
    _BUSY_RETRY_ATTEMPTS = 5

    def __init__(self):
        super().__init__()
        self._running = False
        self._client = None
        self._config: dict = {}
        self._lock = asyncio.Lock()
        self._reconnect_counts: dict[str, int] = {}  # FIXED-P0: 重连计数器改为设备级，避免多设备互相影响
        self._reconnect_delay: float = self._RECONNECT_BASE_DELAY
        self._connected: bool = False
        self._prog_id: str = ""
        self._subscription = None
        self._subscribed_items: set[str] = set()
        # CROSS-003: 使用 OrderedDict 限制 _latest_values 容量
        self._MAX_LATEST_VALUES = 10000
        self._latest_values: OrderedDict[str, Any] = OrderedDict()
        self._values_lock = asyncio.Lock()
        self._data_callback: Any = None
        self._devices: dict[str, dict] = {}
        self._device_configs: dict[str, dict] = {}
        # ODA-002: 设备级连接状态，避免单个设备错误影响整个驱动
        self._device_states: dict[str, str] = {}  # device_id -> "connected" | "disconnected" | "degraded"
        self._device_offline_since: dict[str, datetime] = {}  # ODA-002: 跟踪设备离线时间
        self._watchdog_task: asyncio.Task | None = None
        self._watchdog_interval: float = 15.0
        self._event_bus: Any = None
        self._last_good_values: dict[str, Any] = {}
        self._connect_timeout: float = 30.0
        self._dcom_username: str = ""
        self._dcom_password: str = ""
        self._server_list_cache: list[str] | None = None
        self._reconnect_lock = asyncio.Lock()  # FIXED-P1: 在__init__中初始化，避免hasattr竞态
        self._server_list_cache_host: str = ""
        self._point_states: dict[str, _PointState] = {}
        self._MAX_POINT_STATES = 10000  # FIXED-P2: _point_states容量上限
        self._global_rate_of_change: dict | None = None
        self._global_frozen_detect: dict | None = None
        self._item_properties_cache: dict[str, dict] = {}
        self._MAX_ITEM_PROPERTIES_CACHE = 5000  # FIXED-P2: _item_properties_cache容量上限
        self._audit_logger = logging.getLogger("edgelite.audit.opc_da")
        self._write_audit_log: deque = deque(maxlen=1000)
        self._quality_stream: deque = deque(maxlen=100)  # FIXED-P2: OPCDA-03 改为deque自动淘汰，防止内存增长
        self._QUALITY_STREAM_MAX = 100
        self._callback_queue: queue.Queue = queue.Queue(maxsize=10000)
        self._callback_processor_task: asyncio.Task | None = None
        self._dcom_call_timeout: float = 10.0
        self._com_executor: concurrent.futures.ThreadPoolExecutor | None = None

    def _ensure_com_initialized(self) -> bool:
        """ODA-MED-001: 使用 threading.local() 确保在当前线程中初始化COM

        每个线程独立维护自己的 COM 状态，避免跨线程状态污染。
        """
        if not _PYTHONCOM_AVAILABLE:
            return False
        try:
            current_tid = threading.current_thread().ident
            # ODA-MED-001: 使用 threading.local() 存储线程本地状态
            if (
                getattr(_thread_local, "com_initialized", False)
                and getattr(_thread_local, "com_thread_id", None) == current_tid
            ):
                return True

            # 在线程首次使用时注册清理函数
            if current_tid not in _active_com_threads:
                with _active_com_threads_lock:
                    _active_com_threads.add(current_tid)
                # ODA-MED-001: 注册线程退出清理（仅对工作线程有意义）
                _register_thread_cleanup(current_tid)

            pythoncom.CoInitialize()
            _thread_local.com_initialized = True
            _thread_local.com_thread_id = current_tid
            logger.debug("[opc_da] ODA-MED-001: CoInitialize on thread %d", current_tid)
            return True
        except Exception as e:
            logger.error("[opc_da] ODA-MED-001: CoInitialize failed: %s", e)
            return False

    async def _ensure_com_uninitialized(self) -> None:
        """ODA-MED-001: 使用 threading.local() 确保在正确的线程中释放COM
        FIXED-P2: 当前线程非COM初始化线程时，通过executor在正确线程执行CoUninitialize
        """
        if not _PYTHONCOM_AVAILABLE:
            return
        try:
            current_tid = threading.current_thread().ident
            if getattr(_thread_local, "com_thread_id", None) == current_tid and getattr(
                _thread_local, "com_initialized", False
            ):
                pythoncom.CoUninitialize()
                logger.debug("[opc_da] ODA-MED-001: CoUninitialize on thread %d", current_tid)
                _thread_local.com_initialized = False
                _thread_local.com_thread_id = None
                # FIXED-P2: 清理后从全局集合中移除
                with _active_com_threads_lock:
                    _active_com_threads.discard(current_tid)
                _thread_cleanup_funcs.pop(current_tid, None)
            elif self._com_executor is not None:
                # FIXED-P2: 使用await run_in_executor替代阻塞的submit().result()，避免阻塞事件循环
                try:
                    loop = asyncio.get_running_loop()
                    await asyncio.wait_for(
                        loop.run_in_executor(self._com_executor, _thread_com_cleanup),
                        timeout=5.0,
                    )
                except Exception as e:
                    logger.debug("[opc_da] ODA-MED-001: Deferred CoUninitialize via executor failed: %s", e)
        except Exception as e:
            logger.error("[opc_da] ODA-MED-001: CoUninitialize failed: %s", e)

    def _log_error(self, device_id: str, error_code: str, message: str) -> None:
        i18n_msg = _t(error_code) if error_code.startswith("ERR_") else error_code
        logger.error(
            "[opc_da] device=%s code=%s i18n=%s msg=%s",
            device_id,
            error_code,
            i18n_msg,
            message,
        )

    def _make_bad_result(self, points: list[str], now: datetime, error_code: str = "") -> dict[str, PointValue]:
        source = f"opc_da:{error_code}" if error_code else "opc_da"
        return {p: PointValue(value=None, timestamp=now, quality="bad", source=source) for p in points}

    def _is_non_retryable(self, error_class: str) -> bool:
        upper = error_class.upper()
        return any(k in upper for k in _DCOM_NON_RETRYABLE)

    def _is_backoff_retry(self, error_class: str) -> bool:
        upper = error_class.upper()
        return any(k in upper for k in _DCOM_BACKOFF_RETRY)

    def _set_device_state(self, device_id: str, state: str, reason: str = "") -> None:
        """ODA-002: 设置设备级状态"""
        old_state = self._device_states.get(device_id, "")
        self._device_states[device_id] = state
        if state == "disconnected":
            if device_id not in self._device_offline_since:
                self._device_offline_since[device_id] = datetime.now(UTC)
        elif state == "connected":
            self._device_offline_since.pop(device_id, None)
        if old_state != state:
            logger.debug("[opc_da] ODA-002: device=%s state=%s->%s reason=%s", device_id, old_state, state, reason)

    def _get_point_config(self, device_id: str, tag_name: str) -> dict:
        device = self._devices.get(device_id, {})
        points = device.get("points", {})
        return points.get(tag_name, {})

    def _get_effective_param(self, device_id: str, tag_name: str, param_name: str) -> Any:
        point_cfg = self._get_point_config(device_id, tag_name)
        point_val = point_cfg.get(param_name)
        if point_val is not None:
            return point_val
        return self._config.get(param_name)

    def _check_rate_of_change(self, tag_name: str, value: Any, now_ts: float, roc_cfg: dict | None) -> bool:
        if roc_cfg is None or value is None:
            return True
        if not isinstance(value, (int, float)):
            return True
        max_rate = roc_cfg.get("max_rate")
        if max_rate is None or max_rate <= 0:
            return True
        unit = roc_cfg.get("unit", "s")
        state = self._point_states.get(tag_name)
        if state is None or state.last_value is None or state.last_timestamp == 0.0:
            return True
        if not isinstance(state.last_value, (int, float)):
            return True
        dt = now_ts - state.last_timestamp
        if dt <= 0:
            return True
        if unit == "min":
            dt_minutes = dt / 60.0
            if dt_minutes <= 0:
                return True
            rate = abs(value - state.last_value) / dt_minutes
        else:
            rate = abs(value - state.last_value) / dt
        return rate <= max_rate

    def _check_frozen_value(self, tag_name: str, value: Any, frozen_cfg: dict | None) -> bool:
        if frozen_cfg is None or value is None:
            return True
        consecutive = frozen_cfg.get("consecutive", 3)
        if consecutive <= 0:
            return True
        state = self._point_states.get(tag_name)
        if state is None:
            return True
        if value == state.last_value:
            state.frozen_count += 1
        else:
            state.frozen_count = 0
        return not state.frozen_count >= consecutive

    def _get_or_create_point_state(self, tag_name: str) -> _PointState:
        state = self._point_states.get(tag_name)
        if state is None:
            # FIXED-P2: 容量超限时淘汰最旧条目，防止_point_states无界增长
            if len(self._point_states) >= self._MAX_POINT_STATES:
                self._point_states.pop(next(iter(self._point_states)), None)
            state = _PointState()
            self._point_states[tag_name] = state
        return state

    def _validate_write_type(self, value: Any, vt_type: int | None) -> bool:
        if vt_type is None:
            return True
        allowed_types = _VT_TYPE_MAP.get(vt_type)
        if allowed_types is None:
            return True
        if isinstance(value, allowed_types):
            return True
        if isinstance(value, (int, float)):
            numeric_types = set()
            for types in _VT_TYPE_MAP.values():
                numeric_types.update(types)
            if int in numeric_types and isinstance(value, int):
                if vt_type in (2, 3, 16, 17, 19, 21, 22, 23):
                    return True
            if float in numeric_types and isinstance(value, float):
                if vt_type in (4, 5, 6):
                    return True
            if isinstance(value, int) and vt_type in (4, 5, 6):
                return True
        return False

    async def _get_item_properties(self, item_id: str) -> dict:
        if item_id in self._item_properties_cache:
            return self._item_properties_cache[item_id]
        if not self._client:
            return {}
        try:
            props = await self._com_call(self._client.properties, item_id)
            result = {}
            if isinstance(props, (list, tuple)):
                for prop in props:
                    if isinstance(prop, (list, tuple)) and len(prop) >= 2:
                        result[prop[0]] = prop[1]
            # FIXED-P2: 容量超限时淘汰最旧条目
            if (
                len(self._item_properties_cache) >= self._MAX_ITEM_PROPERTIES_CACHE
                and item_id not in self._item_properties_cache
            ):
                self._item_properties_cache.pop(next(iter(self._item_properties_cache)), None)
            self._item_properties_cache[item_id] = result
            return result
        except Exception:
            return {}

    def _audit_write(
        self, device_id: str, point: str, item_id: str, data_type: str, old_value: Any, new_value: Any, result: str
    ) -> None:
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "user": self._dcom_username or "system",
            "device_id": device_id,
            "point_id": point,
            "item_id": item_id,
            "data_type": data_type,
            "old_value": str(old_value) if old_value is not None else None,
            "new_value": str(new_value) if new_value is not None else None,
            "result": result,
        }
        self._write_audit_log.append(entry)
        self._audit_logger.info(json.dumps(entry, ensure_ascii=False))

    def get_write_audit_log(self, device_id: str | None = None, limit: int = 100) -> list[dict]:
        entries = list(self._write_audit_log)
        if device_id:
            entries = [e for e in entries if e.get("device_id") == device_id]
        return entries[-limit:]

    def _record_quality_stream(
        self, device_id: str, tag_name: str, opc_quality: Any, mapped_quality: str, value: Any
    ) -> None:
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "device_id": device_id,
            "point_name": tag_name,
            "opc_quality": str(opc_quality) if opc_quality is not None else None,
            "quality": mapped_quality,
            "value": str(value) if value is not None else None,
        }
        self._quality_stream.append(
            entry
        )  # FIXED-P1: deque(maxlen=100)已自动淘汰，删除手动切片逻辑（切片会将deque腐蚀为list，丢失maxlen特性）

    def get_quality_stream(self, device_id: str | None = None, limit: int = 100) -> list[dict]:
        if device_id:
            entries = [e for e in self._quality_stream if e.get("device_id") == device_id]
        else:
            entries = list(self._quality_stream)
        return entries[-limit:]

    async def start(self, config: dict) -> None:
        import sys

        self._config = config
        self._event_bus = config.get("event_bus")
        self._watchdog_interval = float(config.get("watchdog_interval", 15.0))
        self._connect_timeout = float(config.get("connect_timeout", 30.0))
        self._dcom_call_timeout = float(config.get("dcom_call_timeout", config.get("timeout", 10.0)))
        self._dcom_username = config.get("username", "")
        self._dcom_password = config.get("password", "")
        self._global_rate_of_change = config.get("rate_of_change")
        self._global_frozen_detect = config.get("frozen_detect")

        if sys.platform != "win32":
            logger.warning("[opc_da] OPC DA requires Windows, current: %s. Use OPC gateway proxy.", sys.platform)

        try:
            import OpenOPC  # pyright: ignore[reportMissingImports]
        except ImportError:
            self._log_error("", OpcDaDriverErrors.IMPORT_ERROR, "OpenOPC-Python3 not installed")
            raise ImportError(
                "OpenOPC not installed. Run: pip install OpenOPC-Python3. "
                "Note: OPC DA requires Windows platform or OPC gateway proxy"
            ) from None

        self._config = config
        server = config.get("server", config.get("prog_id", ""))
        self._prog_id = server
        host = config.get("host", "localhost")
        gateway = config.get("gateway", "")

        if not server:
            raise ValueError("OPC DA driver config missing 'server' parameter (ProgID)")

        self._com_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="opc_da_com"
        )  # FIXED-P1: max_workers=1→2，防止单个挂起DCOM调用死锁整个executor

        try:

            def _init_com_and_create_client() -> Any:
                # ODA-001: 使用线程安全的方式初始化COM
                self._ensure_com_initialized()
                if gateway:
                    return OpenOPC.open_gateway(gateway, host=host)
                else:
                    return OpenOPC.client(host=host)

            loop = asyncio.get_running_loop()
            self._client = await loop.run_in_executor(self._com_executor, _init_com_and_create_client)

            if self._dcom_username and self._dcom_password:
                try:
                    self._client.set_username_password(self._dcom_username, self._dcom_password)
                except AttributeError:
                    logger.warning("[opc_da] DCOM identity impersonation not supported by this OpenOPC version")

            def _connect_server() -> None:
                self._client.connect(server)
                self._configure_dcom_security()

            await asyncio.wait_for(
                loop.run_in_executor(self._com_executor, _connect_server),
                timeout=self._connect_timeout,
            )
            self._running = True
            self._connected = True
            self._reconnect_counts = {}  # FIXED-P0: 重连计数器改为设备级，避免多设备互相影响
            self._reconnect_delay = self._RECONNECT_BASE_DELAY
            # ODA-002: 设置所有设备为已连接
            for device_id in self._devices:
                self._set_device_state(device_id, "connected", "start success")
            logger.info("OPC DA connected: %s@%s (timeout=%.1fs)", server, host, self._connect_timeout)

            self._server_list_cache = None
            self._item_properties_cache = {}

            use_groups = config.get("use_groups", True)
            if use_groups:
                await self._create_subscription()

            self._watchdog_task = asyncio.create_task(self._watchdog_loop())
            self._callback_processor_task = asyncio.create_task(self._process_callback_queue())

        except TimeoutError:
            self._log_error(
                self._prog_id,
                OpcDaDriverErrors.CONN_TIMEOUT,
                f"connect timeout after {self._connect_timeout}s to {server}@{host}",
            )
            self._connected = False
            # ODA-001: 清理COM状态
            await self._ensure_com_uninitialized()
            if self._com_executor:
                self._com_executor.shutdown(wait=False)
                self._com_executor = None
            raise TimeoutError(f"OPC DA connection timeout after {self._connect_timeout}s") from None
        except ImportError as e:
            self._log_error("", OpcDaDriverErrors.IMPORT_ERROR, str(e))
            self._connected = False
            # ODA-001: 清理COM状态
            await self._ensure_com_uninitialized()
            if self._com_executor:
                self._com_executor.shutdown(wait=False)
                self._com_executor = None
            raise
        except Exception as e:
            error_class = self._classify_dcom_error(e)
            dcom_code = self._dcom_class_to_error_code(error_class)
            self._log_error(self._prog_id, dcom_code, error_class)
            self._connected = False
            # ODA-001: 清理COM状态
            await self._ensure_com_uninitialized()
            if self._com_executor:
                self._com_executor.shutdown(wait=False)
                self._com_executor = None
            raise

    async def stop(self) -> None:
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watchdog_task
            self._watchdog_task = None
        if self._callback_processor_task and not self._callback_processor_task.done():
            self._callback_processor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._callback_processor_task
            self._callback_processor_task = None
        try:
            if self._subscription is not None:
                try:
                    if hasattr(self._subscription, "remove_all"):
                        await asyncio.to_thread(self._subscription.remove_all)
                except Exception as e:
                    logger.warning("[opc_da] stop failed: %s", e)  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                if hasattr(self._subscription, "_group"):
                    self._subscription._group = None
                self._subscription = None
            self._subscribed_items.clear()
            if self._client:
                try:
                    if hasattr(self._client, "_server") and self._client._server is not None:
                        self._client._server = None
                    if self._com_executor:
                        loop = asyncio.get_running_loop()
                        await loop.run_in_executor(self._com_executor, self._client.close)
                    else:
                        await asyncio.to_thread(self._client.close)
                except Exception as e:
                    logger.warning("[opc_da] close error: %s", e)
                self._client = None
            # ODA-001: 确保在同一线程中调用 CoUninitialize
            if _PYTHONCOM_AVAILABLE and self._com_executor:
                try:
                    await self._ensure_com_uninitialized()
                except Exception as e:
                    logger.warning("[opc_da] CoUninitialize error: %s", e)
        finally:
            self._running = False
            self._connected = False
            self._client = None
            # CROSS-004: 取消所有后台任务
            await self._cancel_background_tasks()
            # ODA-MED-001: 使用 wait=True 等待线程完成，超时后降级
            if self._com_executor:
                try:
                    # FIXED-P0: stop()添加超时保护，防止COM操作挂起导致永久阻塞
                    await asyncio.wait_for(
                        asyncio.get_running_loop().run_in_executor(None, self._com_executor.shutdown, True),
                        timeout=15.0,
                    )
                except TimeoutError:
                    logger.warning("[opc_da] executor shutdown timed out after 15s, forcing shutdown")
                    self._com_executor.shutdown(wait=False)
                except Exception as e:
                    logger.warning(
                        "[opc_da] ODA-MED-001: executor shutdown(wait=True) failed: %s, falling back to wait=False", e
                    )
                    self._com_executor.shutdown(wait=False)
                self._com_executor = None
            logger.info("OPC DA driver stopped")

    def _set_latest_value(self, tag_name: str, value: Any) -> None:
        """CROSS-003: 设置 latest_value，带容量淘汰机制"""
        if tag_name in self._latest_values:
            self._latest_values.move_to_end(tag_name)
        self._latest_values[tag_name] = value
        # FIXED-P2: 执行_latest_values容量淘汰，防止内存无限增长
        if len(self._latest_values) > self._MAX_LATEST_VALUES:
            # Remove oldest 20% of entries
            for _ in range(len(self._latest_values) // 5):
                self._latest_values.popitem(last=False)

    def _process_good_value(
        self, device_id: str, tag_name: str, value: Any, now: datetime, now_ts: float
    ) -> PointValue:
        scaling = self._get_effective_param(device_id, tag_name, "scaling")
        clamp = self._get_effective_param(device_id, tag_name, "clamp")
        deadband = self._get_effective_param(device_id, tag_name, "deadband")
        roc_cfg = self._get_effective_param(device_id, tag_name, "rate_of_change")
        frozen_cfg = self._get_effective_param(device_id, tag_name, "frozen_detect")

        if roc_cfg is None:
            roc_cfg = self._global_rate_of_change
        if frozen_cfg is None:
            frozen_cfg = self._global_frozen_detect

        value = self._apply_scaling(value, scaling)
        clamped, in_range = self._apply_clamp(value, clamp)
        if not in_range:
            self._log_error(device_id, OpcDaDriverErrors.VALUE_OUT_OF_RANGE, f"point={tag_name} value={value}")
            return PointValue(
                value=None, timestamp=now, quality="bad", source=f"opc_da:{OpcDaDriverErrors.VALUE_OUT_OF_RANGE}"
            )
        value = clamped

        if not self._check_rate_of_change(tag_name, value, now_ts, roc_cfg):
            self._log_error(device_id, OpcDaDriverErrors.RATE_OF_CHANGE_EXCEEDED, f"point={tag_name} value={value}")
            state = self._get_or_create_point_state(tag_name)
            state.last_value = value
            state.last_timestamp = now_ts
            return PointValue(
                value=None, timestamp=now, quality="bad", source=f"opc_da:{OpcDaDriverErrors.RATE_OF_CHANGE_EXCEEDED}"
            )

        if not self._check_frozen_value(tag_name, value, frozen_cfg):
            self._log_error(device_id, OpcDaDriverErrors.FROZEN_VALUE_DETECTED, f"point={tag_name} value={value}")
            return PointValue(
                value=None, timestamp=now, quality="bad", source=f"opc_da:{OpcDaDriverErrors.FROZEN_VALUE_DETECTED}"
            )

        last_val = self._last_good_values.get(tag_name)
        value = self._apply_deadband(value, last_val, deadband)
        self._last_good_values[tag_name] = value
        self._set_latest_value(tag_name, value)

        state = self._get_or_create_point_state(tag_name)
        state.last_value = value
        state.last_timestamp = now_ts

        # #[AUDIT-FIX] _record_read_success is async but _process_good_value is sync;
        # schedule as task (called from async read_points, so event loop is running)
        # FIXED(P1): 原问题-RUF006 create_task 返回值未保存，task 可能被 GC 回收;
        #            修复-保存到基类继承的 _background_tasks 集合
        task = asyncio.create_task(self._record_read_success(device_id))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return PointValue(value=value, timestamp=now, quality="good", source="opc_da")

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        now = datetime.now(UTC)
        now_ts = time.time()

        if not self._running or not self._client:
            self._log_error(device_id, OpcDaDriverErrors.OFFLINE_BAD_QUALITY, "driver not running or client None")
            self._record_read_failure(device_id)
            await self._try_reconnect(device_id)
            return self._make_bad_result(points, now, OpcDaDriverErrors.OFFLINE_BAD_QUALITY)

        record_packet("tx", "opc_da", device_id, f"READ:{','.join(points)}".encode())
        result: dict[str, PointValue] = {}

        async with self._lock:
            try:
                tags = await self._com_call(self._client.read, points)
                if isinstance(tags, list):
                    for item in tags:
                        if not isinstance(item, (list, tuple)) or len(item) < 2:
                            continue
                        tag_name = item[0] if isinstance(item[0], str) else str(item[0])
                        value = item[1]
                        # FIXED-P4: 处理VT_ARRAY类型（8192+基础类型）
                        if isinstance(value, (list, tuple)):
                            result[tag_name] = PointValue(
                                value=list(value), timestamp=now, quality="good", source="opc_da"
                            )
                            continue
                        opc_quality = item[2] if len(item) > 2 else "Unknown"
                        quality = _map_quality(opc_quality)
                        self._record_quality_stream(device_id, tag_name, opc_quality, quality, value)

                        if quality == "good":
                            result[tag_name] = self._process_good_value(device_id, tag_name, value, now, now_ts)
                        elif quality == "uncertain":
                            self._log_error(
                                device_id,
                                OpcDaDriverErrors.QUALITY_UNCERTAIN,
                                f"point={tag_name} opc_quality={opc_quality}",
                            )
                            result[tag_name] = PointValue(
                                value=None,
                                timestamp=now,
                                quality="uncertain",
                                source=f"opc_da:{OpcDaDriverErrors.QUALITY_UNCERTAIN}",
                            )
                            self._record_read_failure(device_id)
                        else:
                            self._log_error(
                                device_id, OpcDaDriverErrors.QUALITY_BAD, f"point={tag_name} opc_quality={opc_quality}"
                            )
                            result[tag_name] = PointValue(
                                value=None,
                                timestamp=now,
                                quality="bad",
                                source=f"opc_da:{OpcDaDriverErrors.QUALITY_BAD}",
                            )
                            self._record_read_failure(device_id)

                elif isinstance(tags, (list, tuple)) and len(tags) >= 2:
                    opc_quality = tags[2] if len(tags) > 2 else "Unknown"
                    quality = _map_quality(opc_quality)
                    tag_name = points[0]
                    item_value = tags[1]
                    # FIXED-P4: 处理VT_ARRAY类型（8192+基础类型）
                    if isinstance(item_value, (list, tuple)):
                        result[tag_name] = PointValue(
                            value=list(item_value), timestamp=now, quality="good", source="opc_da"
                        )
                    elif quality == "good":
                        result[tag_name] = self._process_good_value(device_id, tag_name, item_value, now, now_ts)
                    elif quality == "uncertain":
                        self._log_error(
                            device_id,
                            OpcDaDriverErrors.QUALITY_UNCERTAIN,
                            f"point={tag_name} opc_quality={opc_quality}",
                        )
                        result[tag_name] = PointValue(
                            value=None,
                            timestamp=now,
                            quality="uncertain",
                            source=f"opc_da:{OpcDaDriverErrors.QUALITY_UNCERTAIN}",
                        )
                        self._record_read_failure(device_id)
                    else:
                        self._log_error(
                            device_id, OpcDaDriverErrors.QUALITY_BAD, f"point={tag_name} opc_quality={opc_quality}"
                        )
                        result[tag_name] = PointValue(
                            value=None, timestamp=now, quality="bad", source=f"opc_da:{OpcDaDriverErrors.QUALITY_BAD}"
                        )
                        self._record_read_failure(device_id)

            except Exception as e:
                error_class = self._classify_dcom_error(e)
                dcom_code = self._dcom_class_to_error_code(error_class)
                self._log_error(device_id, dcom_code, error_class)
                if "DISCONNECTED" in error_class or "UNAVAILABLE" in error_class:
                    self._connected = False
                self._record_read_failure(device_id)
                return self._make_bad_result(points, now, dcom_code)

        record_packet("rx", "opc_da", device_id, f"READ_RESULT:{len(result)}".encode())

        for p in points:
            result.setdefault(p, PointValue(value=None, timestamp=now, quality="bad", source="opc_da"))
        return result

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        if not self._running or not self._client:
            await self._try_reconnect(device_id)
            return False

        item_id = point
        point_cfg = self._get_point_config(device_id, point)
        if point_cfg.get("address"):
            item_id = point_cfg["address"]

        props = await self._get_item_properties(item_id)
        access_rights = props.get("AccessRights", props.get(5))
        vt_type = props.get("VT", props.get("DataType", props.get(6)))
        data_type_name = _VT_TYPE_NAMES.get(vt_type, str(vt_type)) if vt_type is not None else "unknown"

        if access_rights is not None:
            try:
                ar = int(access_rights)
                if ar == _OPC_ACCESS_READ:
                    self._log_error(
                        device_id, OpcDaDriverErrors.WRITE_READ_ONLY, f"point={point} item={item_id} access_rights={ar}"
                    )
                    self._audit_write(device_id, point, item_id, data_type_name, None, value, "rejected_read_only")
                    return False
            except (ValueError, TypeError) as e:
                logger.warning("[opc_da] write_point failed: %s", e)  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录

        if vt_type is not None:
            try:
                vt_int = int(vt_type)
                if not self._validate_write_type(value, vt_int):
                    self._log_error(
                        device_id,
                        OpcDaDriverErrors.WRITE_TYPE_MISMATCH,
                        f"point={point} item={item_id} vt={_VT_TYPE_NAMES.get(vt_int, hex(vt_int))} value_type={type(value).__name__}",
                    )
                    self._audit_write(device_id, point, item_id, data_type_name, None, value, "rejected_type_mismatch")
                    return False
            except (ValueError, TypeError) as e:
                logger.warning("[opc_da] write_point failed: %s", e)  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录

        old_value = None
        try:
            read_result = await self._com_call(self._client.read, item_id)
            if isinstance(read_result, (list, tuple)):
                if isinstance(read_result[0], (list, tuple)):
                    old_value = read_result[0][1] if len(read_result[0]) > 1 else None
                elif len(read_result) > 1:
                    old_value = read_result[1]
        except Exception as e:
            logger.warning("[opc_da] operation failed: %s", e)  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录

        record_packet("tx", "opc_da", device_id, f"WRITE:{point}={value}".encode())
        try:
            async with self._lock:
                await self._com_call(self._client.write, (item_id, value))
            record_packet("rx", "opc_da", device_id, b"WRITE_RESULT:OK")
            self._audit_write(device_id, point, item_id, data_type_name, old_value, value, "ok")
            return True
        except Exception as e:
            error_class = self._classify_dcom_error(e)
            dcom_code = self._dcom_class_to_error_code(error_class)
            self._log_error(device_id, dcom_code, error_class)
            self._audit_write(device_id, point, item_id, data_type_name, old_value, value, f"error:{error_class}")
            if "DISCONNECTED" in error_class or "UNAVAILABLE" in error_class:
                self._connected = False
            return False

    async def discover_devices(self, config: dict) -> list[dict]:
        if not self._client:
            return []

        try:
            tree = await self._com_call(self._client.browse)
            items = []
            for branch in tree:
                items.append(
                    {
                        "name": branch,
                        "protocol": "opc_da",
                    }
                )
            return items
        except Exception as e:
            self._log_error("", OpcDaDriverErrors.BROWSE_FAILED, str(e))
            return []

    async def _create_subscription(self) -> None:
        if not self._client:
            return

        try:
            update_rate = self._config.get("update_rate", 1000)
            self._subscription = self._client.group(
                name="edgelite_group",
                update_rate=update_rate,
            )
            logger.info("OPC DA subscription group created")
        except Exception as e:
            self._log_error("", OpcDaDriverErrors.SUBSCRIPTION_FAILED, str(e))

    async def add_subscription(self, points: list[str]) -> None:
        if not self._subscription:
            return

        try:
            for point in points:
                if point not in self._subscribed_items:
                    self._subscription.add(point)
                    self._subscribed_items.add(point)
            logger.info("OPC DA subscription items added: %d", len(points))
        except Exception as e:
            self._log_error("", OpcDaDriverErrors.SUBSCRIPTION_FAILED, str(e))

    def on_data(self, callback) -> None:
        self._data_callback = callback

    def is_device_connected(self, device_id: str) -> bool:
        # ODA-002: 检查设备级状态，而非驱动级状态
        device_state = self._device_states.get(device_id, "")
        return device_state == "connected"

    def get_subscription_stats(self) -> dict:
        return {
            "subscription_active": self._subscription is not None,
            "subscribed_items": len(self._subscribed_items),
            "reconnect_count": sum(self._reconnect_counts.values()),  # FIXED-P0: 重连计数器改为设备级
        }

    async def list_servers(self, host: str = "localhost") -> list[str]:
        if self._server_list_cache is not None and self._server_list_cache_host == host:
            return list(self._server_list_cache)

        try:
            import OpenOPC  # pyright: ignore[reportMissingImports]

            def _list() -> list[str]:
                client = OpenOPC.client(host=host)
                return client.servers()

            servers = await self._com_call(_list)
            self._server_list_cache = list(servers)
            self._server_list_cache_host = host
            return list(self._server_list_cache)
        except ImportError:
            logger.warning("[opc_da] OpenOPC not installed, cannot list servers")
            return []
        except Exception as e:
            self._log_error("", OpcDaDriverErrors.BROWSE_FAILED, str(e))
            return []

    def invalidate_server_cache(self) -> None:
        self._server_list_cache = None
        self._server_list_cache_host = ""

    async def add_device(self, device_id: str, config: dict, points: list[dict] | None = None) -> None:
        if points is None:
            points = []
        self._devices[device_id] = {
            "config": config,
            "points": {p.get("name", p.get("address", "")): p for p in points if p.get("name") or p.get("address")},
        }
        self._device_configs[device_id] = config
        # ODA-002: 初始化设备状态（取决于驱动是否已连接）
        if self._running and self._connected:
            self._set_device_state(device_id, "connected", "device added")
        else:
            self._set_device_state(device_id, "disconnected", "device added while driver not running")
        if self._subscription and points:
            item_ids = [p.get("address", p.get("name", "")) for p in points if p.get("address") or p.get("name")]
            if item_ids:
                await self.add_subscription(item_ids)
        logger.info("[opc_da] device added: %s (%d points)", device_id, len(points))

    async def browse_server_items(self, item_path: str = "") -> list[dict]:
        if not self._client:
            return []

        try:

            def _browse() -> list:
                return self._client.browse(item_path)

            branches = await self._com_call(_browse)
            items = []
            for branch in branches:
                if isinstance(branch, (list, tuple)):
                    name = branch[0] if len(branch) > 0 else str(branch)
                    is_branch = bool(branch[1]) if len(branch) > 1 else True
                else:
                    name = str(branch)
                    is_branch = True
                items.append(
                    {
                        "name": name,
                        "is_branch": is_branch,
                        "path": item_path + "/" + name if item_path else name,
                        "protocol": "opc_da",
                    }
                )
            return items
        except Exception as e:
            self._log_error("", OpcDaDriverErrors.BROWSE_FAILED, str(e))
            return []

    async def remove_device(
        self, device_id: str
    ) -> None:  # FIXED-P1: 改为async，与onvif_driver/modbus_rtu/opcua等驱动签名保持一致
        self._health_stats.pop(device_id, None)
        self._offline_since.pop(device_id, None)
        # ODA-002: 清理设备状态
        self._device_states.pop(device_id, None)
        self._device_offline_since.pop(device_id, None)
        device_points = self._devices.get(device_id, {}).get("points", {})
        for tag_name in list(device_points.keys()):
            self._point_states.pop(tag_name, None)
            self._item_properties_cache.pop(tag_name, None)
            self._last_good_values.pop(tag_name, None)
            self._latest_values.pop(tag_name, None)
        self._devices.pop(device_id, None)
        self._device_configs.pop(device_id, None)
        logger.info("[opc_da] device removed: %s", device_id)

    def _is_connected(self) -> bool:
        return self._running and self._client is not None

    def _dcom_class_to_error_code(self, error_class: str) -> str:
        upper = error_class.upper()
        if "ACCESSDENIED" in upper:
            return OpcDaDriverErrors.DCOM_ACCESS_DENIED
        if "CLASSNOTREG" in upper:
            return OpcDaDriverErrors.DCOM_CLASS_NOT_REGISTERED
        if "SERVERBUSY" in upper:
            return OpcDaDriverErrors.DCOM_SERVER_BUSY
        if "UNAVAILABLE" in upper or "NOT_RUNNING" in upper:
            return OpcDaDriverErrors.DCOM_SERVER_UNAVAILABLE
        if "DISCONNECTED" in upper:
            return OpcDaDriverErrors.DCOM_DISCONNECTED
        if "CALL_FAILED" in upper or "TIMEOUT" in upper:
            return OpcDaDriverErrors.DCOM_CALL_FAILED
        if "IMPORT_ERROR" in upper:
            return OpcDaDriverErrors.IMPORT_ERROR
        return OpcDaDriverErrors.READ_FAILED

    def _classify_dcom_error(self, error: Exception) -> str:
        if _COM_ERROR_TYPES and isinstance(error, _COM_ERROR_TYPES):
            try:
                hr = error.args[0] if error.args else 0
                hr_unsigned = hr & 0xFFFFFFFF
                desc = _DCOM_ERROR_CODES.get(hr_unsigned, f"COM_ERROR_0x{hr_unsigned:08X}")
                return desc
            except (IndexError, AttributeError):
                return "COM_ERROR_UNKNOWN"

        if isinstance(error, OSError):
            if "WinError" in str(error) or "RPC" in str(error):
                return f"RPC_ERROR: {error}"
            return f"OS_ERROR: {error}"

        error_msg = str(error).lower()
        if "access denied" in error_msg or "permission" in error_msg:
            return "E_ACCESSDENIED - DCOM permission error"
        if "class not registered" in error_msg:
            return "REGDB_E_CLASSNOTREG - Class not registered"
        if "server busy" in error_msg:
            return "CO_E_SERVERBUSY - Server busy"
        if "server unavailable" in error_msg or "not running" in error_msg:
            return "RPC_S_SERVER_UNAVAILABLE - OPC server not running"
        if "timeout" in error_msg or "timed out" in error_msg:
            return "RPC_S_CALL_FAILED - DCOM timeout"
        if "disconnected" in error_msg:
            return "RPC_E_DISCONNECTED - OPC server disconnected"

        return f"UNKNOWN_ERROR: {type(error).__name__}"

    async def _com_call(self, func, *args, timeout: float | None = None) -> Any:
        if timeout is None:
            timeout = self._dcom_call_timeout
        if self._com_executor is None:
            return await asyncio.wait_for(asyncio.to_thread(func, *args), timeout=timeout)
        loop = asyncio.get_running_loop()
        return await asyncio.wait_for(loop.run_in_executor(self._com_executor, lambda: func(*args)), timeout=timeout)

    def _configure_dcom_security(self) -> None:
        if not _PYTHONCOM_AVAILABLE or not self._client:
            return
        try:
            server_obj = getattr(self._client, "_server", None)
            if server_obj is not None:
                pythoncom.CoSetProxyBlanket(
                    server_obj,
                    pythoncom.RPC_C_AUTHN_WINNT,
                    pythoncom.RPC_C_AUTHZ_NONE,
                    None,
                    pythoncom.RPC_C_AUTHN_LEVEL_PKT,
                    pythoncom.RPC_C_IMP_LEVEL_IMPERSONATE,
                    None,
                    pythoncom.EOAC_NONE,
                )
        except Exception as e:
            logger.warning("[opc_da] CoSetProxyBlanket skipped: %s", e, exc_info=True)

    def update_dcom_timeout(self, timeout: float) -> None:
        self._dcom_call_timeout = timeout
        if not _PYTHONCOM_AVAILABLE or not self._client:
            return
        try:
            server_obj = getattr(self._client, "_server", None)
            if server_obj is not None:
                pythoncom.CoSetProxyBlanket(
                    server_obj,
                    pythoncom.RPC_C_AUTHN_WINNT,
                    pythoncom.RPC_C_AUTHZ_NONE,
                    None,
                    pythoncom.RPC_C_AUTHN_LEVEL_PKT,
                    pythoncom.RPC_C_IMP_LEVEL_IMPERSONATE,
                    None,
                    pythoncom.EOAC_NONE,
                )
                logger.info("[opc_da] DCOM timeout updated to %.1fs and CoSetProxyBlanket reapplied", timeout)
        except Exception as e:
            logger.debug("[opc_da] update_dcom_timeout CoSetProxyBlanket skipped: %s", e)

    @staticmethod
    def _check_hresult(result: Any, context: str = "") -> Any:
        if isinstance(result, int) and result < 0:
            raise OpcDaComError(result, context)
        if _COM_ERROR_TYPES and isinstance(result, _COM_ERROR_TYPES):
            hr = result.args[0] if result.args else 0
            raise OpcDaComError(hr, context)
        return result

    def _on_subscription_data(self, device_id: str, data: dict) -> None:
        try:
            self._callback_queue.put_nowait((device_id, data))
        except queue.Full:
            logger.warning("[opc_da] callback queue full, dropping data for device=%s", device_id)

    async def _process_callback_queue(self) -> None:
        while self._running:
            try:
                while not self._callback_queue.empty():
                    try:
                        device_id, data = self._callback_queue.get_nowait()
                        if self._data_callback:
                            await self._data_callback(device_id, data)
                    except queue.Empty:
                        break
                await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("[opc_da] callback queue error: %s", e)

    async def _retry_on_busy(self, coro_factory, device_id: str, *args, **kwargs) -> Any:
        delay = self._BUSY_RETRY_BASE_DELAY
        for attempt in range(self._BUSY_RETRY_ATTEMPTS):
            try:
                return await coro_factory(*args, **kwargs)
            except Exception as e:
                error_class = self._classify_dcom_error(e)
                if self._is_backoff_retry(error_class) and attempt < self._BUSY_RETRY_ATTEMPTS - 1:
                    dcom_code = self._dcom_class_to_error_code(error_class)
                    self._log_error(
                        device_id,
                        dcom_code,
                        f"busy retry attempt {attempt + 1}/{self._BUSY_RETRY_ATTEMPTS} in {delay:.1f}s",
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, self._BUSY_RETRY_MAX_DELAY)
                else:
                    raise

    async def _watchdog_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._watchdog_interval)
                if not self._running:
                    break
                if self._client is not None:
                    try:
                        await self._com_call(self._client.ping)
                    except Exception as e:
                        error_class = self._classify_dcom_error(e)
                        if self._is_non_retryable(error_class):
                            # ODA-002: 不再设置 _running = False，只标记所有设备为离线
                            dcom_code = self._dcom_class_to_error_code(error_class)
                            self._log_error(
                                "watchdog", dcom_code, f"non-retryable error, marking devices offline: {error_class}"
                            )
                            for did in list(self._devices):  # FIXED-P1: 使用list()快照，防止迭代中dict被修改
                                self._set_device_state(did, "disconnected", f"non-retryable: {error_class}")
                            # 重置连接状态，但保持驱动运行以允许后续重连
                            self._connected = False
                            # 尝试重连
                            for did in list(self._devices):  # FIXED-P1: 使用list()快照，防止迭代中dict被修改
                                await self._try_reconnect(did)
                        elif "DISCONNECTED" in error_class or "UNAVAILABLE" in error_class:
                            dcom_code = self._dcom_class_to_error_code(error_class)
                            self._log_error("watchdog", dcom_code, error_class)
                            # ODA-002: 只标记设备为离线，不停止驱动
                            for did in list(self._devices):  # FIXED-P1: 使用list()快照，防止迭代中dict被修改
                                self._set_device_state(did, "disconnected", error_class)
                            await self._try_reconnect("watchdog")
                        elif self._is_backoff_retry(error_class):
                            dcom_code = self._dcom_class_to_error_code(error_class)
                            self._log_error("watchdog", dcom_code, f"server busy, will retry next cycle: {error_class}")
                        else:
                            logger.debug("[opc_da] Watchdog check error: %s", error_class)
                else:
                    logger.warning("[opc_da] watchdog: client is None, attempting reconnect")
                    for device_id in self._devices:
                        self._set_device_state(device_id, "disconnected", "client is None")
                        await self._try_reconnect(device_id)
                        if self._running:
                            break
            except asyncio.CancelledError:
                break
            except Exception as e:
                # CROSS-002: 分级处理异常，不静默吞没
                error_class = self._classify_dcom_error(e)
                if self._is_non_retryable(error_class):
                    # ODA-002: 不再设置 _running = False，只标记所有设备为离线
                    dcom_code = self._dcom_class_to_error_code(error_class)
                    self._log_error(
                        "watchdog", dcom_code, f"non-retryable error, marking devices offline: {error_class}"
                    )
                    for did in list(self._devices):  # FIXED-P1: 使用list()快照，防止迭代中dict被修改
                        self._set_device_state(did, "disconnected", f"non-retryable: {error_class}")
                    self._connected = False
                elif "DISCONNECTED" in error_class or "UNAVAILABLE" in error_class:
                    dcom_code = self._dcom_class_to_error_code(error_class)
                    self._log_error("watchdog", dcom_code, error_class)
                    # ODA-002: 只标记设备为离线，不停止驱动
                    for did in list(self._devices):  # FIXED-P1: 使用list()快照，防止迭代中dict被修改
                        self._set_device_state(did, "disconnected", error_class)
                    await self._try_reconnect("watchdog")
                else:
                    # CROSS-002: 使用分级异常处理
                    if not self._handle_watchdog_exception(e, "opc_da_watchdog"):
                        break

    async def _try_reconnect(self, device_id: str) -> None:
        if not self._config:
            return

        # FIXED-P0: 添加重连锁，防止并发重连导致COM client竞态关闭
        # FIXED-P1: 移除hasattr惰性创建和locked()预判，直接使用async with避免TOCTOU
        async with self._reconnect_lock:
            # FIXED-P0: 重连计数器改为设备级，避免多设备互相影响
            count = self._reconnect_counts.get(device_id, 0) + 1
            self._reconnect_counts[device_id] = count
            if count > self._MAX_RECONNECT_ATTEMPTS:
                self._log_error(device_id, OpcDaDriverErrors.RECONNECT_FAILED, f"abandoned after {count} attempts")
                self._connected = False
                self._reconnect_counts[device_id] = 0  # FIXED-P0: 重置计数器，允许后续watchdog触发重连
                self._reconnect_delay = self._RECONNECT_BASE_DELAY
                return

            delay = min(self._reconnect_delay, self._RECONNECT_MAX_DELAY)
            logger.warning("[opc_da] reconnect in %.1fs (attempt %d)", delay, count)
            await asyncio.sleep(delay)
            self._reconnect_delay = min(self._reconnect_delay * 2, self._RECONNECT_MAX_DELAY)

            server = self._config.get("server", self._config.get("prog_id", ""))
            host = self._config.get("host", "localhost")
            gateway = self._config.get("gateway", "")

            if self._client:
                try:
                    # FIXED-P0: 重连时COM操作在专用COM线程中执行，避免COM apartment不一致
                    if self._com_executor:
                        loop = asyncio.get_running_loop()
                        await loop.run_in_executor(self._com_executor, self._client.close)
                    else:
                        await asyncio.to_thread(self._client.close)
                except Exception as e:
                    logger.warning("[opc_da] close error: %s", e, exc_info=True)
                self._client = None

            try:
                import OpenOPC  # pyright: ignore[reportMissingImports]

                # FIXED-P0: 重连时COM操作在专用COM线程中执行，避免COM apartment不一致
                if self._com_executor:
                    loop = asyncio.get_running_loop()

                    def _init_com_and_create_client() -> Any:
                        self._ensure_com_initialized()
                        if gateway:
                            return OpenOPC.open_gateway(gateway, host=host)
                        else:
                            return OpenOPC.client(host=host)

                    self._client = await loop.run_in_executor(self._com_executor, _init_com_and_create_client)
                else:
                    # FIXED-P0: 无com_executor时使用asyncio.to_thread，禁止同步阻塞事件循环
                    if gateway:
                        self._client = await asyncio.to_thread(OpenOPC.open_gateway, gateway, host=host)
                    else:
                        self._client = await asyncio.to_thread(OpenOPC.client, host=host)

                if self._dcom_username and self._dcom_password:
                    try:
                        self._client.set_username_password(self._dcom_username, self._dcom_password)
                    except AttributeError:
                        logger.warning("[opc_da] DCOM identity impersonation not supported by this OpenOPC version")

                # FIXED-P0: 重连时COM操作在专用COM线程中执行，避免COM apartment不一致
                if self._com_executor:
                    await asyncio.wait_for(
                        loop.run_in_executor(self._com_executor, lambda: self._client.connect(server)),
                        timeout=self._connect_timeout,
                    )
                else:
                    await asyncio.wait_for(
                        asyncio.to_thread(self._client.connect, server),
                        timeout=self._connect_timeout,
                    )
                self._configure_dcom_security()
                self._running = True
                self._connected = True
                self._reconnect_counts[device_id] = 0  # FIXED-P0: 重连计数器改为设备级，避免多设备互相影响
                self._reconnect_delay = self._RECONNECT_BASE_DELAY
                # ODA-002: 设置所有设备为已连接
                for did in list(self._devices):  # FIXED-P1: 使用list()快照，防止迭代中dict被修改
                    self._set_device_state(did, "connected", "reconnect success")
                self._log_error(device_id, OpcDaDriverErrors.RECONNECT_OK, f"{server}@{host}")

                self._server_list_cache = None

                use_groups = self._config.get("use_groups", True)
                if use_groups:
                    await self._create_subscription()
                    if self._subscription and self._subscribed_items:
                        await self.add_subscription(list(self._subscribed_items))

            except TimeoutError:
                self._log_error(
                    device_id, OpcDaDriverErrors.CONN_TIMEOUT, f"reconnect timeout after {self._connect_timeout}s"
                )
            except Exception as e:
                error_class = self._classify_dcom_error(e)
                dcom_code = self._dcom_class_to_error_code(error_class)
                self._log_error(device_id, dcom_code, error_class)
                if self._is_non_retryable(error_class):
                    self._log_error(device_id, dcom_code, "non-retryable error, stopping reconnect")
                    self._connected = False
                    return

    async def health_check(self, device_id: str) -> bool:
        if not self._running or not self._client:
            return False
        try:
            await self._com_call(self._client.ping)
            return True
        except Exception:
            return False
