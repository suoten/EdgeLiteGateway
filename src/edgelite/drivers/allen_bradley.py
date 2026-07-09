"""Allen-Bradley PLC驱动 - 基于pylogix库，支持ControlLogix/CompactLogix/MicroLogix"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import random
import threading
import time
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum, StrEnum
from typing import Any

from edgelite.api.debug import record_packet
from edgelite.drivers.base import ConnectionState, DriverCapabilities, DriverPlugin, PointValue
from edgelite.engine.event_bus import PointUpdateEvent
from edgelite.services.i18n import t as _t

logger = logging.getLogger(__name__)


class AbConnState(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CIP_NEGOTIATING = "cip_negotiating"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    OFFLINE = "offline"


@dataclass
class PointHealthStats:
    success_count: int = 0
    fail_count: int = 0
    avg_latency_ms: float = 0.0
    consecutive_fails: int = 0
    last_access_time: float = 0.0
    # #[AUDIT-FIX] W9: 新增 last_cip_error 字段，用于 CIP 错误分布统计
    last_cip_error: str = ""
    _latency_samples: deque = field(default_factory=lambda: deque(maxlen=20))  # FIXED-P2: 改为deque避免列表切片内存分配

    def record_success(self, latency_ms: float) -> None:
        self.success_count += 1
        self.consecutive_fails = 0
        self.last_access_time = time.monotonic()
        self._latency_samples.append(latency_ms)
        self.avg_latency_ms = sum(self._latency_samples) / len(self._latency_samples)

    def record_failure(self, cip_error: str = "") -> None:
        self.fail_count += 1
        self.consecutive_fails += 1
        self.last_access_time = time.monotonic()
        # #[AUDIT-FIX] W9: 记录最近一次 CIP 错误码，空字符串表示非 CIP 错误
        if cip_error:
            self.last_cip_error = cip_error

    @property
    def success_rate(self) -> float:
        total = self.success_count + self.fail_count
        return self.success_count / total if total > 0 else 1.0


CIP_STATUS_MAP: dict[int, str] = {
    0x00: "OK",
    0x01: "ERR_CIP_INVALID_ADDRESS",
    0x02: "ERR_CIP_INVALID_PARAMETER",
    0x04: "ERR_CIP_PATH_ERROR",
    0x05: "ERR_CIP_PATH_DEST_UNKNOWN",
    0x06: "ERR_CIP_PARTIAL_TRANSFER",
    0x08: "ERR_CIP_SERVICE_NOT_SUPPORTED",
    0x09: "ERR_CIP_INVALID_DATA",
    0x0A: "ERR_CIP_ATTRIBUTE_NOT_SUPPORTED",
    0x0B: "ERR_CIP_INVALID_ATTRIBUTE_VALUE",
    0x0E: "ERR_CIP_ATTRIBUTE_LIST_ERROR",
    0x13: "ERR_CIP_INSUFFICIENT_DATA",
    0x14: "ERR_CIP_ATTRIBUTE_NOT_SETTABLE",
    0x15: "ERR_CIP_PRIVILEGE_VIOLATION",
    0x16: "ERR_CIP_DEVICE_STATE_CONFLICT",
    0x17: "ERR_CIP_RESOURCE_UNAVAILABLE",
    0x18: "ERR_CIP_SERVICE_FRAG_NOT_SUPPORTED",
    0x1A: "ERR_CIP_KEY_FAILURE",
    0x1B: "ERR_CIP_PATH_PORT_INVALID",
    0x1C: "ERR_CIP_PATH_PORT_NOT_AVAILABLE",
    0x1D: "ERR_CIP_INVALID_MSG_TIMEOUT",
    0x1E: "ERR_CIP_INVALID_MSG_PRIORITY",
    0x20: "ERR_CIP_INVALID_CONNECTION",
    0x21: "ERR_CIP_TARGET_NOT_CONNECTED",
    0x22: "ERR_CIP_INVALID_CONNECTION_SERVICE",
    0x23: "ERR_CIP_NETWORK_CONNECTION_LOST",
    0x24: "ERR_CIP_UNSUPPORTED_REVISION",
    0x28: "ERR_CIP_SEQUENCE_OVERFLOW",
    0x29: "ERR_CIP_NO_MORE_CONNECTIONS",
    0x32: "ERR_CIP_TARGET_CONFLICT",
    0x33: "ERR_CIP_CONNECTION_IN_USE",
    0x34: "ERR_CIP_TRANSPORT_CLASS_NOT_SUPPORTED",
    0x36: "ERR_CIP_INVALID_CONNECTION_SIZE",
    0x3A: "ERR_CIP_INVALID_APPLICATION_TAG",
    0x3B: "ERR_CIP_INVALID_SEGMENT_TYPE",
    0x3C: "ERR_CIP_CONNECTION_TIMED_OUT",
    0x3D: "ERR_CIP_UNCONNECTED_RESPONSE_TIMEOUT",
    0x3E: "ERR_CIP_INVALID_PRODUCER_CONSUMER_SIZE",
    0x3F: "ERR_CIP_INVALID_VENDOR_ID",
    0x40: "ERR_CIP_INVALID_PRODUCT_CODE",
    0x41: "ERR_CIP_INVALID_DEVICE_TYPE",
    0x42: "ERR_CIP_INVALID_REVISION",
    0x43: "ERR_CIP_INVALID_STATUS",
    0x44: "ERR_CIP_INVALID_SERIAL_NUMBER",
    0x46: "ERR_CIP_INVALID_ASSEMBLY_ID",
    0x47: "ERR_CIP_INVALID_ATTRIBUTE_DIRECTION",
    0x48: "ERR_CIP_INVALID_ATTRIBUTE_DATA",
    0x49: "ERR_CIP_INVALID_ATTRIBUTE_LIST",
    0x4A: "ERR_CIP_INVALID_COMBINATION",
    0x4B: "ERR_CIP_INVALID_EKEY",
    0x4C: "ERR_CIP_INVALID_OWNER",
    0x4D: "ERR_CIP_INVALID_PRIORITY",
    0x4E: "ERR_CIP_INVALID_TIME_VALUE",
    0x4F: "ERR_CIP_INVALID_PROCESSING_OPTION",
    0x50: "ERR_CIP_INVALID_ROUTE",
    0x51: "ERR_CIP_INVALID_TAG_TYPE",
    0x52: "ERR_CIP_TAG_NOT_FOUND",
}


class AllenBradleyDriver(DriverPlugin):
    """Allen-Bradley PLC驱动 (罗克韦尔自动化)

    配置参数:
        ip: PLC IP地址
        port: 端口号 (默认44818 for CIP, 2222 for PCCC)
        slot: ControlLogix槽号 (默认0, CompactLogix默认0)
        micrologix: 是否为MicroLogix/PCCC协议 (默认False)
    """

    plugin_name = "allen_bradley"
    plugin_version = "1.0.0"
    supported_protocols = ("allen_bradley", "ab", "ab_cip", "ab_pccc")  # FIXED(P2): 原问题-可变默认值list; 修复-改为tuple
    _required_dependencies: tuple[str, ...] = ("pylogix",)  # FIXED(P2): 原问题-可变默认值list; 修复-改为tuple
    config_schema = {
        "description": "Allen-Bradley PLC protocol (pylogix), supports ControlLogix/CompactLogix",
        "required": ["ip"],
        "properties": {"ip": {"type": "string", "description": "AB PLC IP", "format": "ipv4"}, "port": {"type": "integer", "description": "CIP/PCCC port", "minimum": 1, "maximum": 65535}},
        "fields": [
            {"name": "ip", "type": "string", "label": "IP Address", "description": "AB PLC IP address", "default": "", "required": True},  # FIXED-P1: 默认IP改为空，避免暴露内网网段
            {"name": "port", "type": "integer", "label": "Port", "description": "CIP port (default 44818), PCCC/MicroLogix uses 2222", "default": 44818, "min": 1, "max": 65535},
            {"name": "slot", "type": "integer", "label": "Slot", "description": "CPU slot position, ControlLogix default 0, CompactLogix default 0", "default": 0, "min": 0, "max": 31},
            {"name": "timeout", "type": "number", "default": 5, "description": "Connection timeout in seconds"},
            {"name": "connection_timeout", "type": "number", "label": "CIP Connection Timeout (s)", "description": "CIP session connection timeout in seconds (default 5.0)", "default": 5.0},
            {"name": "connection_type", "type": "string", "label": "Connection Type", "description": "CIP (ControlLogix/CompactLogix) or PCCC (MicroLogix/SLC)", "default": "CIP", "options": ["CIP", "PCCC"]},
            {"name": "plc_model", "type": "string", "label": "PLC Model", "description": "PLC model type", "default": "ControlLogix", "options": ["MicroLogix", "ControlLogix", "CompactLogix"]},
            {"name": "large_forward_open", "type": "boolean", "label": "Large Forward Open", "description": "Enable Large Forward Open for connections >448 bytes (ControlLogix/CompactLogix)", "default": False},
            {"name": "cip_username", "type": "string", "label": "CIP Username", "description": "CIP Security username (for firmware 21+)", "default": ""},
            {"name": "cip_password", "type": "string", "label": "CIP Password", "description": "CIP Security password", "default": "", "secret": True},
            {"name": "default_tag", "type": "string", "label": "Default Tag/Address", "description": "Default tag for health check, '@cpu' reads controller info, e.g. 'Program:Main.TagName' or 'N7:0'", "default": "@cpu"},
            {"name": "watchdog_interval", "type": "number", "label": "Watchdog Interval (s)", "description": "Connection watchdog interval in seconds", "default": 10.0},
            {"name": "watchdog_check_mode", "type": "string", "label": "Watchdog Check Mode", "description": "Health check mode: ping=CIP identity (no tag), tag=specific tag, auto=ping then fallback tag", "default": "auto", "options": ["ping", "tag", "auto"]},
            {"name": "backup_ip", "type": "string", "label": "Backup IP", "description": "Backup PLC IP for redundancy failover", "default": ""},
            {"name": "backup_port", "type": "integer", "label": "Backup Port", "description": "Backup PLC port (default same as primary)", "default": None},
            {"name": "failover_threshold", "type": "integer", "label": "Failover Threshold", "description": "Primary failure count before switching to backup", "default": 3, "min": 1, "max": 10},
            {"name": "auto_revert", "type": "boolean", "label": "Auto Revert", "description": "Auto revert to primary IP when recovered", "default": True},
            {"name": "deadband", "type": "number", "label": "Deadband", "description": "Deadband threshold (absolute or percent), suppress unchanged values", "default": None},
            {"name": "scaling_ratio", "type": "number", "label": "Scaling Ratio", "description": "Linear scaling ratio (value * ratio + offset)", "default": 1.0},
            {"name": "scaling_offset", "type": "number", "label": "Scaling Offset", "description": "Linear scaling offset", "default": 0.0},
            {"name": "clamp_min", "type": "number", "label": "Clamp Min", "description": "Minimum valid value, values below are marked bad quality", "default": None},
            {"name": "clamp_max", "type": "number", "label": "Clamp Max", "description": "Maximum valid value, values above are marked bad quality", "default": None},
        ],
    }

    experimental = False
    capabilities = DriverCapabilities(discover=True, read=True, write=True, subscribe=False, batch_read=True, batch_write=True)
    constraints = ({"type": "feature_gap", "message": "Program Tags vs Controller Tags distinction not fully supported"}, {"type": "feature_gap", "message": "UDT/Array/Structure types have limited support"})  # FIXED(P2): 原问题-可变默认值list; 修复-改为tuple

    _MAX_RECONNECT_ATTEMPTS = 3
    _RECONNECT_BASE_DELAY = 1.0
    _RECONNECT_MAX_DELAY = 60.0
    _JITTER_MAX_MS = 1000
    _RETRY_BASE_DELAY = 0.5
    _RETRY_JITTER_FACTOR = 0.3
    _FAILOVER_THRESHOLD = 3
    _FAILOVER_FAST_DELAY = 0.5
    _FAILOVER_PROBE_INTERVAL = 30.0
    _READ_TIMEOUT = 30  # FIXED-P1: 定义读取超时常量，防止AttributeError
    _DEFAULT_TAG = "@cpu"
    # AB-MED-001: watchdog 健康检查模式
    _WATCHDOG_CHECK_MODE_TAG = "tag"       # 读取特定 tag
    _WATCHDOG_CHECK_MODE_PING = "ping"     # 使用 CIP Identity 查询（不依赖 tag）
    _WATCHDOG_CHECK_MODE_AUTO = "auto"     # 优先 ping，失败则回退 tag

    def __init__(self):
        super().__init__()
        self._client = None
        self._config: dict = {}
        self._lock = asyncio.Lock()
        self._sync_lock = threading.RLock()  # FIXED-P1: 改为可重入锁，防止嵌套调用死锁
        self._ab_conn_state_lock = threading.Lock()  # #[AUDIT-FIX] renamed from _conn_state_lock to avoid clobbering base class asyncio.Lock (caused TypeError: '_thread.lock' does not support async context manager)
        self._reconnect_lock = asyncio.Lock()
        self._device_clients_lock = asyncio.Lock()
        self._device_clients: dict[str, Any] = {}
        self._device_locks: dict[str, asyncio.Lock] = {}
        self._reconnect_count: int = 0
        self._reconnect_cooldown_until: float = 0.0  # FIXED-P1: 重连冷却期时间戳，达到上限后1小时内不再重试
        self._reconnect_delay: float = self._RECONNECT_BASE_DELAY
        self._devices: dict[str, dict] = {}
        self._device_configs: dict[str, dict] = {}
        self._watchdog_task: asyncio.Task | None = None
        self._watchdog_interval: float = 10.0
        self._watchdog_check_mode: str = self._WATCHDOG_CHECK_MODE_AUTO  # AB-MED-001
        self._event_bus: Any = None
        self._cip_security_enabled: bool = False
        # _offline_since inherited from base class (dict[str, datetime])  # FIXED-P2: 删除遮蔽基类的覆盖初始化
        # CROSS-003: 使用 OrderedDict 限制 _last_values 容量
        self._MAX_LAST_VALUES = 10000
        self._last_values: OrderedDict[str, Any] = OrderedDict()
        self._last_timestamps: dict[str, datetime] = {}
        self._conn_state: str = AbConnState.DISCONNECTED.value
        self._primary_ip: str = ""
        self._backup_ip: str = ""
        self._active_ip: str = ""
        self._using_backup: bool = False
        self._primary_fail_count: int = 0
        self._failover_count: int = 0
        self._failover_start_mono: float = 0.0
        self._last_failover_time: str = ""
        self._last_failover_duration_ms: float = 0.0
        self._failover_probe_task: asyncio.Task | None = None
        self._failover_mode_lock = asyncio.Lock()  # FIXED-P1: 模式切换(CAS/EIP failover/revert)锁保护，防止并发切换导致状态不一致
        self._auto_revert: bool = True
        self._large_forward_open_auto: bool = False
        self._point_stats: dict[str, PointHealthStats] = {}
        self._MAX_POINT_STATS = 10000  # FIXED-P2: _point_stats容量上限
        self._degraded_freq: bool = False
        self._degraded_batch_size: int = 0
        self._degrade_window: deque = deque(maxlen=200)
        self._frozen_check_window: int = 3
        self._rate_of_change_limit: float | None = None
        self._point_configs: dict[str, dict] = {}
        self._device_points: dict[str, list] = {}  # FIXED-P1: 设备级测点列表，remove_device依赖此字典清理测点相关数据
        self._write_rate_limit_ms: float = 500.0
        self._last_write_time: dict[str, float] = {}
        self._last_reconnect_attempt: dict[str, float] = {}  # FIXED-P0: 重连速率限制，记录上次重连时间
        self._write_verify_delay_ms: float = 100.0
        self._write_audit_log: deque = deque(maxlen=1000)
        self._rule_engine = None
        self._trigger_executor = None
        self._rule_store = None
        self._ts_store = None
        self._offline_sync = None
        self._persist_enabled: bool = False
        self._browse_timeout: float = 30.0
        self._config_version_mgr = None
        self._audit = None
        self._ota_mgr = None
        self._current_user_role: str = "viewer"
        self._role_lock = asyncio.Lock()  # FIXED-P0: 角色读写锁，防止多协程并发写入 _current_user_role 导致权限竞态（对齐 modbus_tcp/modbus_rtu/mc 驱动）
        # AB-001: 独立线程池，避免阻塞其他驱动的 to_thread 调用
        self._thread_pool: ThreadPoolExecutor | None = None
        self._thread_pool_failed: bool = False  # FIXED-P1: 线程池卡死标志，触发重建
        self._thread_pool_lock: asyncio.Lock = asyncio.Lock()  # FIXED-P1: 线程池重建+submit原子性保护锁
        # FIXED-P0: 线程池饱和检测，防止饥饿导致驱动永久卡死
        self._thread_pool_semaphore: asyncio.Semaphore | None = None

    async def _run_in_thread(self, func, *args, timeout: float = 30.0, **kwargs):  # FIXED-P1: 添加超时参数，防止同步调用永久阻塞
        """AB-001: 在独立线程池中执行同步函数，避免阻塞全局线程池"""
        async with self._thread_pool_lock:  # FIXED-P1: 线程池重建+submit原子性保护
            if self._thread_pool_failed:
                old_pool = self._thread_pool
                self._thread_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ab-worker-")
                self._thread_pool_failed = False
                # FIXED-P0: 信号量泄漏修复——重建线程池时不创建新信号量，复用原信号量。
                # 原因：旧信号量上已acquire的协程在finally中会release self._thread_pool_semaphore，
                # 若此处创建新信号量，旧协程会release新信号量（从未acquire），导致新信号量计数错乱（超过max_value）。
                # 信号量仅作为并发限流器，与线程池生命周期解耦，复用安全。
                if old_pool is not None:
                    try:
                        old_pool.shutdown(wait=False, cancel_futures=True)
                    except Exception as e:
                        logger.warning("[ab] run_in_thread failed: %s", e)  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                logger.warning("[ab] code=THREAD_POOL_REBUILT msg=AB thread pool rebuilt after timeout")
            if self._thread_pool is None:
                self._thread_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ab-worker-")
                # FIXED-P0: 线程池饱和检测，防止饥饿导致驱动永久卡死
                self._thread_pool_semaphore = asyncio.Semaphore(4)
            # FIXED-P0: 线程池饱和检测，防止饥饿导致驱动永久卡死
            if self._thread_pool_semaphore is None:
                self._thread_pool_semaphore = asyncio.Semaphore(4)
            acquired = False
            try:
                try:
                    acquired = await asyncio.wait_for(self._thread_pool_semaphore.acquire(), timeout=1.0)
                except TimeoutError as exc:  # FIXED(P3): 原问题-B904 异常链丢失; 修复-添加 from exc
                    raise RuntimeError("Thread pool saturated, cannot submit new task") from exc
                future = self._thread_pool.submit(func, *args, **kwargs)
            except Exception:
                if acquired and self._thread_pool_semaphore is not None:
                    self._thread_pool_semaphore.release()
                raise
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), timeout=timeout)
        except TimeoutError:
            self._thread_pool_failed = True  # FIXED-P1: 超时后标记线程池可能有卡死线程，下次调用时重建
            raise
        finally:
            if acquired and self._thread_pool_semaphore is not None:
                self._thread_pool_semaphore.release()

    def _log_error(self, device_id: str, error_code: str, message: str) -> None:
        i18n_msg = _t(error_code) if error_code.startswith("ERR_") else error_code
        logger.error("[ab] device=%s code=%s i18n=%s msg=%s", device_id, error_code, i18n_msg, message)

    def _set_last_value(self, point: str, value: Any) -> None:
        """CROSS-003: 设置 last_value，带 LRU 淘汰机制"""
        if point in self._last_values:
            self._last_values.move_to_end(point)
        self._last_values[point] = value
        # 超过容量时淘汰最旧条目
        while len(self._last_values) > self._MAX_LAST_VALUES:
            self._last_values.pop(next(iter(self._last_values)))

    def _sync_read_tag(self, tag):
        with self._sync_lock:
            if self._client is None:  # FIXED-P0: _client为None时抛异常而非AttributeError
                raise RuntimeError("AB client not connected")
            return self._client.Read(tag)

    def _sync_write_tag(self, tag, value):
        with self._sync_lock:
            if self._client is None:  # FIXED-P0: _client为None时抛异常而非AttributeError
                raise RuntimeError("AB client not connected")
            return self._client.Write(tag, value)

    def _sync_get_tag_list(self, program=""):
        with self._sync_lock:
            if self._client is None:  # FIXED-P0: _client为None时抛异常而非AttributeError
                raise RuntimeError("AB client not connected")
            return self._client.GetTagList(program)

    def _sync_get_program_list(self):
        with self._sync_lock:
            if self._client is None:  # FIXED-P0: _client为None时抛异常而非AttributeError
                raise RuntimeError("AB client not connected")
            return self._client.GetProgramList()

    def _sync_ping(self) -> bool:
        """AB-MED-001: 通过 CIP Identity 查询检测 PLC 连接（不依赖特定 tag）

        Returns:
            True if PLC responds, False otherwise
        """
        with self._sync_lock:  # FIXED-P2: 与_sync_read_tag/_sync_write_tag一致，保护非线程安全的client
            if self._client is None:  # FIXED-P2: _client为None时直接返回False，避免AttributeError掩盖其他问题
                return False
            try:
                # 方法1: 尝试读取 @cpu 特殊标签（CIP Identity）
                resp = self._client.Read("@cpu")
                if resp.Status == 0:
                    return True
            except AttributeError:
                return False  # client is None, not a connection issue  # FIXED-P2: 区分AttributeError和其他异常，避免None client误触发重连
            except Exception as e:
                logger.debug("[ab] ping failed: %s", e)
                return False

            # 方法2: 尝试使用 GetDeviceProperties (pylogix 的 CIP identity)
            try:
                if hasattr(self._client, 'GetDeviceProperties'):
                    props = self._client.GetDeviceProperties()  # FIXED-P1: GetDeviceProperties是方法而非属性，需方法调用
                    if props and hasattr(props, 'Value') and props.Value is not None:
                        return True
            except AttributeError:
                return False  # client is None, not a connection issue  # FIXED-P2: 区分AttributeError和其他异常，避免None client误触发重连
            except Exception as e:
                logger.debug("[ab] ping failed: %s", e)
                return False

            return False

    def _sync_close_client(self):
        with self._sync_lock:
            if self._client is None:  # FIXED-P0: _client为None时安全返回
                return None
            return self._client.Close()

    def _sync_forward_close_client(self):
        with self._sync_lock:
            if self._client is None:  # FIXED-P0: _client为None时安全返回
                return None
            return self._client.ForwardClose()

    async def _forward_close(self) -> None:
        if not self._client:
            return
        try:
            if hasattr(self._client, 'ForwardClose'):
                try:
                    await asyncio.wait_for(self._run_in_thread(self._sync_forward_close_client), timeout=5.0)
                except TimeoutError:
                    logger.debug("[ab] ForwardClose timed out")
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.debug("[ab] ForwardClose error: %s", e)
            try:
                await asyncio.wait_for(self._run_in_thread(self._sync_close_client), timeout=5.0)
            except TimeoutError:
                logger.warning("[ab] CIP close timeout (5s)")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("[ab] CIP close failed: %s", e)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("[ab] code=FORWARD_CLOSE_FAILED ForwardClose failed: %s", e)

    def _set_conn_state(self, new_state: str, device_id: str = "", reason: str = "") -> None:
        with self._ab_conn_state_lock:  # #[AUDIT-FIX] use AB-specific lock (base class _conn_state_lock is asyncio.Lock)
            valid_transitions = {
                AbConnState.DISCONNECTED.value: {AbConnState.CONNECTING.value},
                AbConnState.CONNECTING.value: {
                    AbConnState.CIP_NEGOTIATING.value,
                    AbConnState.DISCONNECTED.value,
                    AbConnState.OFFLINE.value,
                },
                AbConnState.CIP_NEGOTIATING.value: {
                    AbConnState.CONNECTED.value,
                    AbConnState.DISCONNECTED.value,
                    AbConnState.OFFLINE.value,
                },
                AbConnState.CONNECTED.value: {
                    AbConnState.DEGRADED.value,
                    AbConnState.DISCONNECTED.value,
                    AbConnState.CONNECTING.value,
                },
                AbConnState.DEGRADED.value: {
                    AbConnState.CONNECTED.value,
                    AbConnState.OFFLINE.value,
                    AbConnState.CONNECTING.value,
                },
                AbConnState.OFFLINE.value: {
                    AbConnState.CONNECTING.value,
                    AbConnState.DISCONNECTED.value,
                },
            }
            allowed = valid_transitions.get(self._conn_state, set())
            if new_state in allowed or new_state == self._conn_state:
                old = self._conn_state
                self._conn_state = new_state
                if old != new_state:
                    logger.info("[ab] state: %s -> %s reason=%s", old, new_state, reason)
                if device_id:
                    base_state = {
                        AbConnState.CIP_NEGOTIATING.value: ConnectionState.CIP_NEGOTIATING.value,
                    }.get(new_state, new_state)
                    try:
                        loop = asyncio.get_running_loop()
                        task = loop.create_task(self._set_connection_state(device_id, base_state, reason))
                        self._background_tasks.add(task)
                        task.add_done_callback(self._background_tasks.discard)
                    except RuntimeError as e:
                        logger.debug("[ab] operation failed: %s", e)  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
            else:
                logger.warning("[ab] state transition blocked: %s -> %s", self._conn_state, new_state)

    def _calc_backoff_delay(self) -> float:
        has_backup = bool(self._backup_ip) and not self._using_backup
        if has_backup:
            delay = self._FAILOVER_FAST_DELAY
        else:
            delay = min(self._RECONNECT_BASE_DELAY * (2 ** self._reconnect_count), self._RECONNECT_MAX_DELAY)
        jitter_ms = random.randint(0, 200 if has_backup else self._JITTER_MAX_MS)
        return delay + jitter_ms / 1000.0

    def _calc_retry_delay(self, attempt: int) -> float:
        base = self._RETRY_BASE_DELAY * (2 ** attempt)
        jitter = base * self._RETRY_JITTER_FACTOR
        return base + random.uniform(-jitter, jitter)

    def _parse_cip_status(self, status: int) -> str:
        return CIP_STATUS_MAP.get(status, f"ERR_CIP_UNKNOWN_0x{status:02X}")

    def _record_point_success(self, point: str, latency_ms: float) -> None:
        # FIXED-P2: 容量超限时淘汰最旧条目
        if len(self._point_stats) >= self._MAX_POINT_STATS and point not in self._point_stats:
            self._point_stats.pop(next(iter(self._point_stats)), None)
        stats = self._point_stats.setdefault(point, PointHealthStats())
        stats.record_success(latency_ms)
        self._degrade_window.append((time.monotonic(), True))

    def _record_point_failure(self, point: str, cip_error: str = "") -> None:
        # FIXED-P2: 容量超限时淘汰最旧条目
        if len(self._point_stats) >= self._MAX_POINT_STATS and point not in self._point_stats:
            self._point_stats.pop(next(iter(self._point_stats)), None)
        stats = self._point_stats.setdefault(point, PointHealthStats())
        stats.record_failure(cip_error)  # #[AUDIT-FIX] W9: 传递 cip_error 用于错误分布统计
        self._degrade_window.append((time.monotonic(), False))

    def _check_degradation(self, device_id: str) -> None:
        now = time.monotonic()
        stale_points = [p for p, s in self._point_stats.items() if s.last_access_time > 0 and (now - s.last_access_time) > 3600]
        for p in stale_points:
            del self._point_stats[p]
        if not self._degrade_window:
            return
        window_success = sum(1 for _, ok in self._degrade_window if ok)
        total = len(self._degrade_window)
        if total < 10:
            return
        rate = window_success / total
        if rate < 0.8 and not self._degraded_freq:
            self._degraded_freq = True
            self._set_conn_state(AbConnState.DEGRADED.value, device_id, f"success_rate={rate:.1%}")
            self._log_error(device_id, "ERR_AB_DEGRADE_ACTIVE", f"success_rate={rate:.1%}")
        elif rate >= 0.9 and self._degraded_freq:
            self._degraded_freq = False
            if self._conn_state == AbConnState.DEGRADED.value:
                self._set_conn_state(AbConnState.CONNECTED.value, device_id, f"success_rate={rate:.1%}")
            self._log_error(device_id, "ERR_AB_DEGRADE_RECOVERED", f"success_rate={rate:.1%}")

    def _filter_nan_inf(self, value: Any) -> tuple[Any, bool]:
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None, False
        if isinstance(value, list):
            filtered = []
            for v in value:
                if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                    continue
                filtered.append(v)
            return filtered, True
        return value, True

    def _check_frozen(self, point: str, value: Any) -> bool:
        if not isinstance(value, (int, float)):
            return False
        history = self._last_values.get(point)
        if history is None:
            return False
        if isinstance(history, (int, float)) and abs(value - history) < 1e-9:
            stats = self._point_stats.get(point)
            if stats and stats.success_count > self._frozen_check_window:
                return True
        return False

    def _check_rate_of_change(self, point: str, value: Any, timestamp: datetime | None) -> bool:
        if self._rate_of_change_limit is None or not isinstance(value, (int, float)):
            return False
        prev = self._last_values.get(point)
        if prev is None or not isinstance(prev, (int, float)) or timestamp is None:
            return False
        prev_ts = self._last_timestamps.get(point)
        if prev_ts is None:
            return False
        dt = (timestamp - prev_ts).total_seconds()
        if dt <= 0:
            return False
        rate = abs(value - prev) / dt
        return rate > self._rate_of_change_limit

    def _get_point_config(self, point: str) -> dict:
        return self._point_configs.get(point, {})

    def _apply_point_transforms(self, point: str, pv: PointValue, now: datetime) -> PointValue:
        if pv.value is None or pv.quality == "bad":
            return pv
        pcfg = self._get_point_config(point)
        val = pv.value
        val, ok = self._filter_nan_inf(val)
        if not ok or val is None:
            self._log_error("", "ERR_AB_NAN_INF", f"point={point}")
            return PointValue(value=None, quality="bad", timestamp=pv.timestamp or now)
        if self._check_frozen(point, val):
            self._log_error("", "ERR_AB_FROZEN_VALUE", f"point={point}")
        if self._check_rate_of_change(point, val, pv.timestamp):
            self._log_error("", "ERR_AB_RATE_OF_CHANGE", f"point={point}")
        scaling = pcfg.get("scaling") or self._config.get("scaling")
        if scaling:
            val = self._apply_scaling(val, scaling)
        clamp = pcfg.get("clamp") or self._config.get("clamp")
        if clamp:
            val, in_range = self._apply_clamp(val, clamp)
            if not in_range:
                self._log_error("", "ERR_AB_VALUE_OUT_OF_RANGE", f"point={point}")
                return PointValue(value=None, quality="bad", timestamp=pv.timestamp or now)
        deadband = pcfg.get("deadband") or self._config.get("deadband")
        if deadband:
            last_val = self._last_values.get(point)
            val = self._apply_deadband(val, last_val, deadband)
        # CROSS-003: LRU 缓存更新
        self._set_last_value(point, val)
        if pv.timestamp:
            self._last_timestamps[point] = pv.timestamp
        return PointValue(value=val, quality=pv.quality, timestamp=pv.timestamp or now)

    _AB_TYPE_RANGES: dict[str, tuple] = {
        "BOOL": (0, 1, int),
        "SINT": (-128, 127, int),
        "USINT": (0, 255, int),
        "INT": (-32768, 32767, int),
        "UINT": (0, 65535, int),
        "DINT": (-2147483648, 2147483647, int),
        "UDINT": (0, 4294967295, int),
        "LINT": (-9223372036854775808, 9223372036854775807, int),
        "ULINT": (0, 18446744073709551615, int),
        "REAL": (None, None, float),
        "LREAL": (None, None, float),
    }

    # FIXED(严重): 已知但未在_AB_TYPE_RANGES中定义范围的AB类型，用于区分"已知类型"与"完全未知类型"
    _AB_KNOWN_NON_RANGE_TYPES: set[str] = {
        "STRING", "TIMER", "COUNTER", "UDT", "BYTE", "WORD", "DWORD", "LWORD",
    }

    # FIXED(严重): AB PLC字符串默认最大长度82字符
    _AB_STRING_MAX_LEN = 82

    _CIP_TYPE_SIZES: dict[str, int] = {
        "BOOL": 1,
        "SINT": 1,
        "USINT": 1,
        "BYTE": 1,
        "INT": 2,
        "UINT": 2,
        "WORD": 2,
        "DINT": 4,
        "UDINT": 4,
        "DWORD": 4,
        "LINT": 8,
        "ULINT": 8,
        "LWORD": 8,
        "REAL": 4,
        "LREAL": 8,
    }

    def _validate_cip_data_length(self, resp: Any, point: str = "") -> bool:
        if resp is None:
            return False
        value = getattr(resp, 'Value', None)
        if value is None:
            return True
        data_type = ""
        for dev_info in list(self._devices.values()):  # FIXED-P1: 使用list()快照，防止迭代中dict被修改
            pts = dev_info.get("points", {})
            pinfo = pts.get(point, {})
            if pinfo:
                data_type = pinfo.get("type", pinfo.get("data_type", "")).upper()
                break
        if not data_type:
            return True
        base_type = data_type.rstrip("[]")
        expected_size = self._CIP_TYPE_SIZES.get(base_type)
        if expected_size is None:
            return True
        if isinstance(value, bytes):
            if len(value) < expected_size:
                self._log_error("", "ERR_AB_DATA_LENGTH_MISMATCH", f"point={point} expected>={expected_size} got={len(value)}")
                return False
        if isinstance(value, list):
            if len(value) == 0:
                return True
            elem_size = expected_size
            total_expected = len(value) * elem_size
            raw_size = getattr(resp, 'DataLength', None)
            if raw_size is not None and raw_size < total_expected:
                self._log_error("", "ERR_AB_DATA_LENGTH_MISMATCH", f"point={point} array expected>={total_expected} got={raw_size}")
                return False
        range_info = self._AB_TYPE_RANGES.get(base_type)
        if range_info:
            min_val, max_val, expected_type = range_info
            if min_val is not None and max_val is not None:
                try:
                    if isinstance(value, list):
                        for v in value:
                            casted = expected_type(v)
                            if casted < min_val or casted > max_val:
                                self._log_error("", "ERR_AB_DATA_LENGTH_MISMATCH", f"point={point} value={casted} out of range [{min_val},{max_val}]")
                                return False
                    else:
                        casted = expected_type(value)
                        if casted < min_val or casted > max_val:
                            self._log_error("", "ERR_AB_DATA_LENGTH_MISMATCH", f"point={point} value={casted} out of range [{min_val},{max_val}]")
                            return False
                except (ValueError, TypeError):
                    self._log_error("", "ERR_AB_DATA_LENGTH_MISMATCH", f"point={point} type mismatch expected={expected_type.__name__}")
                    return False
        return True

    def _validate_write_value(self, point: str, value: Any, data_type: str = "", device_id: str = "") -> tuple[Any, str]:
        if data_type:
            dt_upper = data_type.upper()
            range_info = self._AB_TYPE_RANGES.get(dt_upper)
            if range_info:
                min_val, max_val, expected_type = range_info
                if not isinstance(value, expected_type):
                    try:
                        value = expected_type(value)
                    except (ValueError, TypeError):
                        return None, "ERR_AB_WRITE_VALUE_INVALID"
                if min_val is not None and max_val is not None:
                    if value < min_val or value > max_val:
                        return None, "ERR_AB_WRITE_VALUE_INVALID"
            elif dt_upper == "STRING":
                # FIXED(严重): STRING类型添加长度校验(AB PLC字符串默认最大82字符)，防止超长字符串导致PLC内存越界
                if not isinstance(value, str):
                    try:
                        value = str(value)
                    except (ValueError, TypeError):
                        return None, "ERR_AB_WRITE_VALUE_INVALID"
                if len(value) > self._AB_STRING_MAX_LEN:
                    self._log_error("", "ERR_AB_WRITE_VALUE_INVALID", f"point={point} string length={len(value)} exceeds max={self._AB_STRING_MAX_LEN}")
                    return None, "ERR_AB_WRITE_VALUE_INVALID"
            else:
                # FIXED(严重): 对未在_AB_TYPE_RANGES中的类型(TIMER/COUNTER/UDT/BYTE/WORD等)至少校验基本数据类型匹配
                base_dt = dt_upper.rstrip("[]")
                if not isinstance(value, (int, float, bool, str, list, dict)):
                    return None, "ERR_AB_WRITE_VALUE_INVALID"
                # FIXED(严重): 完全不认识的类型记录WARNING但仍放行，避免阻断合法操作
                if base_dt not in self._AB_KNOWN_NON_RANGE_TYPES:
                    logger.warning("[ab] unknown data type '%s' for point=%s, value passed through without range validation", data_type, point)
        if isinstance(value, list):
            max_array_len = 500
            if len(value) > max_array_len:
                value = value[:max_array_len]
        if isinstance(value, dict):
            dev_info = self._devices.get(device_id)
            if dev_info:
                points_map = dev_info.get("points", {})
                pinfo = points_map.get(point, {})
                expected_fields = pinfo.get("struct_fields")
                if expected_fields and isinstance(expected_fields, list):
                    missing = [f for f in expected_fields if f not in value]
                    if missing:
                        return None, "ERR_AB_WRITE_VALUE_INVALID"
        return value, ""

    def _check_write_rate(self, point: str) -> bool:
        now = time.monotonic()
        last = self._last_write_time.get(point, 0.0)
        if (now - last) * 1000 < self._write_rate_limit_ms:
            return False
        self._last_write_time[point] = now
        return True

    async def _write_verify(self, point: str, written_value: Any) -> bool:
        await asyncio.sleep(self._write_verify_delay_ms / 1000.0)
        try:
            resp = await asyncio.wait_for(self._run_in_thread(self._sync_read_tag, point), timeout=10.0)
            read_back = self._parse_response_value(resp, point)
            if read_back is None:
                return False
            if isinstance(written_value, (int, float)) and isinstance(read_back, (int, float)):
                return abs(written_value - read_back) < 1e-6
            return read_back == written_value
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("[ab] write verify failed: %s", e)  # FIXED-P1: 原问题-写入验证异常返回False无日志
            return False

    def _record_write_audit(self, device_id: str, point: str, old_value: Any, new_value: Any, result: bool, error_code: str = "") -> None:
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "device_id": device_id,
            "point_id": point,
            "tag_name": point,
            "old_value": old_value,
            "new_value": new_value,
            "result": "ok" if result else "failed",
            "error_code": error_code,
        }
        self._write_audit_log.append(entry)

    def get_write_audit_log(self, device_id: str = "", limit: int = 100) -> list[dict]:
        entries = list(self._write_audit_log)
        if not device_id:
            return entries[-limit:]
        return [e for e in entries if e.get("device_id") == device_id][-limit:]

    async def start(self, config: dict) -> None:
        try:
            from pylogix import PLC
        except ImportError:
            raise ImportError("pylogix not installed, run: pip install pylogix") from None

        self._config = config
        self._event_bus = config.get("event_bus")
        self._watchdog_interval = float(config.get("watchdog_interval", 10.0))
        self._watchdog_check_mode = config.get("watchdog_check_mode", "auto")  # AB-MED-001
        self._connection_timeout = float(config.get("connection_timeout", 5.0))
        self._browse_timeout = float(config.get("browse_timeout", 30.0))
        window_size = int(config.get("degradation_window_size", 200))
        if window_size != self._degrade_window.maxlen:
            self._degrade_window = deque(self._degrade_window, maxlen=window_size)
        if not config.get("default_tag"):
            self._config["default_tag"] = self._DEFAULT_TAG
        scaling_ratio = config.get("scaling_ratio")
        scaling_offset = config.get("scaling_offset")
        if scaling_ratio is not None or scaling_offset is not None:
            self._config["scaling"] = {"ratio": float(scaling_ratio or 1.0), "offset": float(scaling_offset or 0.0)}
        clamp_min = config.get("clamp_min")
        clamp_max = config.get("clamp_max")
        if clamp_min is not None or clamp_max is not None:
            self._config["clamp"] = {}
            if clamp_min is not None:
                self._config["clamp"]["min"] = float(clamp_min)
            if clamp_max is not None:
                self._config["clamp"]["max"] = float(clamp_max)

        ip = config.get("ip") or config.get("host", "")
        port = int(config.get("port", 44818))
        slot = int(config.get("slot", 0))
        connection_type = config.get("connection_type", "CIP")
        micrologix = connection_type == "PCCC" or config.get("micrologix", False)

        plc_model = config.get("plc_model", "ControlLogix")
        if plc_model == "MicroLogix":
            micrologix = True
            port = int(config.get("port", 2222))

        if not ip:
            raise ValueError("AB driver config missing 'ip' parameter")

        if not (1 <= port <= 65535):
            raise ValueError(f"AB driver port out of range [1-65535], got: {port}")
        if not (0 <= slot <= 31):
            raise ValueError(f"AB driver slot out of range [0-31], got: {slot}")

        self._primary_ip = ip
        self._backup_ip = config.get("backup_ip", "")
        self._active_ip = ip
        self._using_backup = False
        self._primary_fail_count = 0
        self._failover_count = 0
        self._failover_start_mono = 0.0
        self._auto_revert = bool(config.get("auto_revert", True))

        device_id = config.get("device_id", "")
        self._set_conn_state(AbConnState.CONNECTING.value, device_id, f"{ip}:{port}")

        tls_enabled = bool(config.get("tls_enabled", False))
        if tls_enabled:
            test_sock = None  # FIXED-P1: 确保TLS测试socket在异常路径也被关闭
            try:
                import socket as _socket
                import ssl as _ssl
                test_sock = _socket.create_connection((ip, port), timeout=self._connection_timeout)
                ctx = _ssl.create_default_context()
                ca_cert = config.get("ca_cert", "")
                if ca_cert:
                    ctx.load_verify_locations(ca_cert)
                client_cert = config.get("client_cert", "")
                client_key = config.get("client_key", "")
                if client_cert and client_key:
                    ctx.load_cert_chain(client_cert, client_key)
                test_sock = ctx.wrap_socket(test_sock, server_hostname=ip)
                test_sock.close()
                test_sock = None
            except asyncio.CancelledError:
                if test_sock is not None:
                    try:
                        test_sock.close()
                    except Exception as e:
                        logger.warning("[ab] operation failed: %s", e)  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                raise
            except Exception:
                if test_sock is not None:  # FIXED-P1: wrap_socket异常时关闭原始socket，防止连接泄漏
                    try:
                        test_sock.close()
                    except Exception as e:
                        logger.warning("[ab] operation failed: %s", e)  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                logger.warning("AB driver: TLS connection failed for %s, falling back to non-encrypted connection", device_id)

        try:
            new_client = PLC(ip=ip, port=port, slot=slot)
            self._client = new_client  # FIXED-P0: 先赋值到局部变量再赋值到self，构造异常时self._client保持原值
            if hasattr(self._client, 'SocketTimeout'):
                self._client.SocketTimeout = self._connection_timeout

            self._cip_security_enabled = False
            cip_user = config.get("cip_username", "")
            cip_pass = config.get("cip_password", "")
            if cip_user and cip_pass and hasattr(self._client, "set_cip_security"):
                try:
                    self._client.set_cip_security(username=cip_user, password=cip_pass)
                    self._cip_security_enabled = True
                    logger.info("[ab] code=CIP_SECURITY_ENABLED user=%s", cip_user)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self._log_error(device_id, "ERR_AB_CIP_SECURITY_FAILED", str(e))

            self._set_conn_state(AbConnState.CIP_NEGOTIATING.value, device_id, "LargeForwardOpen negotiation")

            large_forward_open = config.get("large_forward_open", False)
            self._large_forward_open_auto = False
            if large_forward_open and hasattr(self._client, 'LargeForwardOpen'):
                self._client.LargeForwardOpen = True
            elif not micrologix and hasattr(self._client, 'LargeForwardOpen'):
                try:
                    self._client.LargeForwardOpen = True
                    self._large_forward_open_auto = True
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._client.LargeForwardOpen = False

            # FIXED-P2: 将_running=True移到ping验证成功之后，避免ping失败时驱动进入"运行中但断开"的矛盾状态
            self._reconnect_count = 0
            self._reconnect_delay = self._RECONNECT_BASE_DELAY
            self._init_edge_rules()
            self._init_ts_storage(config)
            # FIXED-P1: PLC()惰性连接，标记CONNECTED前验证连接是否真正可用
            ping_ok = await asyncio.to_thread(self._sync_ping)
            if not ping_ok:
                self._set_conn_state(AbConnState.DISCONNECTED.value, device_id, "ping verification failed after PLC() construction")
                logger.warning("[ab] code=PING_FAILED ip=%s slot=%d, connection verification failed, marking DISCONNECTED", ip, slot)
                # FIXED-P1: 原问题-isolated client创建后未关闭，ping验证失败时self._client仍持有CIP连接
                #           修复-在finally块中关闭isolated client，防止CIP连接泄漏
                try:
                    await self._forward_close()
                except Exception as e:
                    logger.debug("[ab] close failed after ping verification failure: %s", e)
                self._client = None
            else:
                self._running = True
                self._set_conn_state(AbConnState.CONNECTED.value, device_id, ip)
                # FIXED-P2: watchdog和failover探测仅在ping成功后启动
                if self._watchdog_task is None or self._watchdog_task.done():
                    self._watchdog_task = asyncio.create_task(self._watchdog_loop())
                if self._backup_ip and self._auto_revert and (self._failover_probe_task is None or self._failover_probe_task.done()):
                    self._failover_probe_task = asyncio.create_task(self._failover_probe_loop())
                logger.info(
                    "[ab] code=CONNECTED ip=%s slot=%d micrologix=%s plc_model=%s "
                    "cip_security=%s large_fwd_open=%s(auto=%s) backup=%s",
                    ip, slot, micrologix, plc_model, self._cip_security_enabled,
                    large_forward_open, self._large_forward_open_auto, self._backup_ip or "none",
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._log_error(device_id, "ERR_AB_CONN_FAILED", f"ip={ip} error={e}")
            self._set_conn_state(AbConnState.DISCONNECTED.value, device_id, str(e))
            raise


    async def stop(self) -> None:
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watchdog_task
            self._watchdog_task = None
        if self._failover_probe_task and not self._failover_probe_task.done():
            self._failover_probe_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._failover_probe_task
            self._failover_probe_task = None
        if self._config_version_mgr:
            await self._config_version_mgr.stop()  # FIXED-P1: close()已改为async stop()，需await并在_db_lock保护下关闭
            self._config_version_mgr = None
        self._audit = None
        self._ota_mgr = None
        # FIXED-P0: 原问题-stop() 方法完全遗漏 _trigger_executor 和 _rule_store 的清理
        # 第1801行创建的 EdgeTriggerExecutor 实例在驱动停止时被遗忘，
        # 其 SQLite 连接和后台任务（_upload_task/_pulse_tasks）泄漏
        if self._trigger_executor:
            try:
                await self._trigger_executor.stop()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("[ab] code=STOP_TRIGGER_FAILED trigger_executor.stop failed: %s", e)
            self._trigger_executor = None
        if self._rule_store:
            self._rule_store.stop()
            self._rule_store = None
        if self._offline_sync:
            try:
                await self._offline_sync.stop()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("[ab] code=STOP_OFFLINE_SYNC_FAILED offline_sync.stop failed: %s", e)
        if self._ts_store:
            try:
                await self._ts_store.stop()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("[ab] code=STOP_TS_STORE_FAILED ts_store.stop failed: %s", e)
        try:
            if self._client:
                try:
                    await self._forward_close()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning("[ab] code=STOP_FORWARD_CLOSE_FAILED ForwardClose failed during stop: %s", e)
        finally:
            self._running = False
            # CROSS-004: 取消所有后台任务
            await self._cancel_background_tasks()
            # FIXED-P0: 先关闭设备客户端（线程池仍可用），再关闭线程池，防止线程池泄漏
            async with self._device_clients_lock:
                for device_id, dev_client in list(self._device_clients.items()):
                    try:
                        await asyncio.wait_for(self._run_in_thread(dev_client.Close), timeout=5.0)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.warning("[ab] code=STOP_DEVICE_CLIENT_FAILED device=%s Close failed: %s", device_id, e)
                self._device_clients.clear()
            # CROSS-001: 关闭独立线程池，与fins.py一致使用非阻塞shutdown
            if self._thread_pool:
                try:  # FIXED-P1: shutdown改为非阻塞+取消futures，防止worker卡死时stop永久阻塞
                    self._thread_pool.shutdown(wait=False, cancel_futures=True)
                except Exception as e:
                    logger.warning("[ab] code=EXECUTOR_SHUTDOWN_ERROR msg=Error during executor shutdown: %s", e)
                self._thread_pool = None
            self._client = None
            for did in list(self._devices.keys()):
                self._set_conn_state(AbConnState.DISCONNECTED.value, did, "driver stopped")
            # FIXED-P2: 清理内部状态字典，防止stop后restart使用过期数据
            self._devices.clear()
            self._device_configs.clear()
            self._point_stats.clear()
            self._last_values.clear()
            self._last_timestamps.clear()
            self._last_write_time.clear()  # FIXED-P0: 修复属性名错误(_write_rate_limits→_last_write_time)
            self._last_reconnect_attempt.clear()  # FIXED-P0: 清理重连速率限制状态
            logger.info("[ab] code=STOPPED")

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        quality = self.get_connection_quality(device_id)
        if quality < 60:
            # FIXED-P0: 重连速率限制，距上次重连不到30秒则跳过
            now_mono = time.monotonic()
            last_attempt = self._last_reconnect_attempt.get(device_id, 0.0)
            if now_mono - last_attempt >= 30.0:
                self._last_reconnect_attempt[device_id] = now_mono
                logger.warning("[ab] device=%s code=QUALITY_LOW quality=%.1f, attempting reconnect", device_id, quality)
                await self._try_reconnect(device_id)
                new_quality = self.get_connection_quality(device_id)
                if new_quality > 80:
                    logger.info("[ab] device=%s code=QUALITY_RECOVERED quality=%.1f->%.1f", device_id, quality, new_quality)
                else:
                    await self._set_connection_state(device_id, ConnectionState.DEGRADED.value, "Connection quality below threshold after reconnect")
                    logger.warning("[ab] device=%s code=QUALITY_DEGRADED quality=%.1f after reconnect", device_id, new_quality)
            else:
                await self._set_connection_state(device_id, ConnectionState.DEGRADED.value, "Connection quality below threshold, reconnect rate limited")

        if not self._running or not self._client:
            # FIXED-P0: 重连速率限制，距上次重连不到30秒则跳过
            now_mono = time.monotonic()
            last_attempt = self._last_reconnect_attempt.get(device_id, 0.0)
            if now_mono - last_attempt >= 30.0:
                self._last_reconnect_attempt[device_id] = now_mono
                await self._try_reconnect(device_id)
            if not self._running or not self._client:
                now = datetime.now(UTC)
                return {p: PointValue(value=None, quality="bad", timestamp=now) for p in points}

        result: dict[str, Any] = {}
        record_packet("tx", "ab", device_id, f"CIP Read: {points}")

        batch_size = len(points)
        if self._degraded_freq and batch_size > 1:
            batch_size = max(batch_size // 2, 1)
            self._log_error(device_id, "ERR_AB_BATCH_RETRY", f"degraded batch_size={batch_size}")

        async def _do_batch_read(tag_list: list[str]) -> dict[str, Any]:
            batch_result: dict[str, Any] = {}
            # FIXED-P0: 缩小锁粒度，仅在获取client引用时持锁，I/O操作在锁外执行
            # 之前整个I/O操作持锁，一个慢速设备可阻塞全部设备读写
            async with self._device_clients_lock:
                client = self._device_clients.get(device_id, self._client)
                lock = self._device_locks.get(device_id, self._lock)
            # FIXED-P0: 获取引用后检查client是否已被stop()置None，防止使用已释放的client
            if client is None:
                self._log_error(device_id, "ERR_AB_CLIENT_DISPOSED", "client reference invalidated during lock release")
                for point in tag_list:
                    batch_result[point] = None
                return batch_result
            try:
                async with lock:
                    response = await self._run_in_thread(client.Read, tag_list)
                    if isinstance(response, list):
                        for i, point in enumerate(tag_list):
                            if i < len(response):
                                batch_result[point] = self._parse_response_value(response[i], point)
                            else:
                                batch_result[point] = None
                    else:
                        if tag_list:
                            batch_result[tag_list[0]] = self._parse_response_value(response, tag_list[0])
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._log_error(device_id, "ERR_AB_DEVICE_ISOLATED_READ", f"isolated client error: {e}")
                self._record_read_failure(device_id)
                for point in tag_list:
                    self._record_point_failure(point)
                if self._client:
                    try:  # FIXED-P2: 回退到共享client读取添加超时保护，防止阻塞
                        response = await asyncio.wait_for(
                            self._run_in_thread(self._sync_read_tag, tag_list),
                            timeout=self._READ_TIMEOUT,
                        )
                    except TimeoutError:
                        self._log_error(device_id, "ERR_AB_FALLBACK_READ_TIMEOUT", f"fallback read timeout ({self._READ_TIMEOUT}s)")
                        for point in tag_list:
                            batch_result[point] = None
                        return batch_result
                    if isinstance(response, list):
                        for i, point in enumerate(tag_list):
                            if i < len(response):
                                batch_result[point] = self._parse_response_value(response[i], point)
                            else:
                                batch_result[point] = None
                    else:
                        if tag_list:
                            batch_result[tag_list[0]] = self._parse_response_value(response, tag_list[0])
            return batch_result

        _cip_conn_failed = False
        # FIXED-P1: 重试循环增加总超时预算(60秒)，防止3次重试总阻塞90+秒
        _retry_deadline = time.monotonic() + 60.0
        if batch_size >= len(points) and len(points) > 0:
            for attempt in range(3):
                if time.monotonic() > _retry_deadline:
                    self._log_error(device_id, "ERR_AB_RETRY_BUDGET_EXCEEDED", "retry budget exceeded 60s")
                    break
                try:
                    t0 = time.monotonic()
                    _remaining = max(1.0, _retry_deadline - time.monotonic())
                    batch_result = await asyncio.wait_for(_do_batch_read(points), timeout=min(30.0, _remaining))
                    result.update(batch_result)
                    for pv in batch_result.values():
                        if pv is None:
                            _cip_conn_failed = True
                            break
                    break
                except TimeoutError:
                    self._log_error(device_id, "ERR_AB_READ_TIMEOUT", f"attempt={attempt + 1}/3")
                    # FIXED-P2: 原问题-TimeoutError() 创建异常实例但未 raise，是无效死代码
                    # 删除该死代码，避免误导（如需重新抛出应使用 raise）
                    if attempt < 2:
                        await asyncio.sleep(self._calc_retry_delay(attempt))
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self._log_error(device_id, "ERR_AB_READ_BATCH_FAILED", f"error={e} attempt={attempt + 1}/3")
                    if attempt == 0 and len(points) > 1:
                        batch_size = max(len(points) // 2, 1)
                        self._log_error(device_id, "ERR_AB_BATCH_RETRY", f"reducing to batch_size={batch_size}")
                    if attempt < 2:
                        await asyncio.sleep(self._calc_retry_delay(attempt))
        else:
            chunks = [points[i:i + batch_size] for i in range(0, len(points), batch_size)]
            for chunk in chunks:
                try:
                    t0 = time.monotonic()
                    batch_result = await asyncio.wait_for(_do_batch_read(chunk), timeout=30.0)
                    result.update(batch_result)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self._log_error(device_id, "ERR_AB_READ_BATCH_FAILED", f"chunk error={e}")
                    for p in chunk:
                        result[p] = None

        failed_points = [p for p in points if result.get(p) is None]
        if failed_points:
            self._log_error(device_id, "ERR_AB_READ_FAILED", f"fallback to per-point for {len(failed_points)} tags")
            for point_addr in failed_points:
                if not self._validate_bool_array_offset(point_addr):
                    result[point_addr] = None
                    self._record_point_failure(point_addr)
                    continue
                try:
                    t0 = time.monotonic()
                    # FIXED-P2: 检查self._client是否为None，防止并发stop()后fallback读取崩溃
                    current_client = self._client
                    if current_client is None:
                        result[point_addr] = None
                        self._record_point_failure(point_addr)
                        continue
                    resp = await self._run_in_thread(self._sync_read_tag, point_addr)
                    latency_ms = (time.monotonic() - t0) * 1000
                    parsed = self._parse_response_value(resp, point_addr)
                    if parsed is not None:
                        result[point_addr] = parsed
                        self._record_point_success(point_addr, latency_ms)
                    else:
                        result[point_addr] = None
                        self._record_point_failure(point_addr)
                except asyncio.CancelledError:
                    raise
                except Exception as e2:
                    self._log_error(device_id, "ERR_AB_READ_FAILED", f"point={point_addr} error={e2}")
                    result[point_addr] = None
                    self._record_point_failure(point_addr)

        now = datetime.now(UTC)
        for p in points:
            if p not in result or result[p] is None:
                result[p] = PointValue(value=None, quality="bad", timestamp=now)
                self._record_point_failure(p)
            elif not isinstance(result[p], PointValue):
                quality = "uncertain" if isinstance(result[p], dict) else "good"
                result[p] = PointValue(value=result[p], quality=quality, timestamp=now)

        for p in points:
            pv = result[p]
            if isinstance(pv, PointValue) and pv.quality != "bad":
                result[p] = self._apply_point_transforms(p, pv, now)

        self._check_degradation(device_id)

        if _cip_conn_failed:
            async with self._device_clients_lock:
                client = self._device_clients.get(device_id, self._client)
            if client:
                try:
                    probe_resp = await asyncio.wait_for(self._run_in_thread(client.Read, self._config.get("default_tag", self._DEFAULT_TAG)), timeout=10.0)
                    probe_status = getattr(probe_resp, 'Status', None)
                    if probe_status == 0x01:
                        logger.warning("[ab] code=CIP_CONN_FAILURE device=%s status=0x01, triggering reconnect", device_id)
                        await self._try_reconnect(device_id)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning("[ab] code=PROBE_CHECK_FAILED device=%s probe check failed: %s", device_id, e)

        await self._persist_points(device_id, result)
        await self._evaluate_rules(device_id, result)

        record_packet("rx", "ab", device_id, f"CIP Read Response: {result}")
        if any(isinstance(v, PointValue) and v.quality == "bad" for v in result.values()):
            self._record_read_failure(device_id)
        else:
            await self._record_read_success(device_id)  # #[AUDIT-FIX] _record_read_success is async, must await (was no-op coroutine)
        return result

    def _parse_response_value(self, resp: Any, point: str = "") -> Any:
        if resp is None:
            return None
        status = getattr(resp, 'Status', None) or getattr(resp, 'status', None) or 0
        if status != 0:
            cip_err = self._parse_cip_status(status)
            if point:
                self._record_point_failure(point, cip_err)  # #[AUDIT-FIX] W9: 传入 cip_err 用于错误分布统计
                self._log_error("", cip_err, f"point={point} cip_status=0x{status:02X}")
            return None
        if not self._validate_cip_data_length(resp, point):
            if point:
                self._record_point_failure(point)
            return None
        value = getattr(resp, 'Value', None)
        if value is None:  # FIXED-P1: 使用is None而非or，避免0值被跳过
            value = getattr(resp, 'value', None)
        if value is None:
            return None
        try:
            if hasattr(value, '_dict'):
                try:
                    return dict(value._dict)
                except Exception:
                    partial = {}
                    for k in dir(value):
                        if k.startswith('_'):
                            continue
                        try:
                            partial[k] = getattr(value, k)
                        except Exception:
                            continue
                    return partial if partial else None
            if hasattr(value, '__dict__') and not isinstance(value, (int, float, str, bool, list, bytes)):
                try:
                    return {k: v for k, v in value.__dict__.items() if not k.startswith('_')}
                except Exception:
                    partial = {}
                    for k in dir(value):
                        if k.startswith('_'):
                            continue
                        try:
                            partial[k] = getattr(value, k)
                        except Exception:
                            continue
                    return partial if partial else None
            if hasattr(value, 'tobytes'):
                try:
                    return value.tobytes().hex()
                except Exception:
                    if point:
                        self._log_error("", "ERR_AB_DECODE_FAILED", f"point={point} PIL Image decode failed")
                    return None
            if isinstance(value, list):
                _MAX_ARRAY_LENGTH = 1024  # FIXED-P2: 大列表长度限制
                if len(value) > _MAX_ARRAY_LENGTH:
                    logger.warning("[allen_bradley] 数组长度%d超过上限%d，已截断", len(value), _MAX_ARRAY_LENGTH)
                    # FIXED-P2: 返回截断后的原始列表而非PointValue，避免调用方包装为嵌套PointValue
                    return value[:_MAX_ARRAY_LENGTH]
                return value
            return value
        except Exception:
            if point:
                self._log_error("", "ERR_AB_DECODE_FAILED", f"point={point} parse exception")
            return None

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        # SEC-FIX(修复2): 驱动层写入权限检查，防止内部服务绕过 API 层鉴权直接写入
        if hasattr(self, "check_permission"):
            from edgelite.security.rbac import Permission
            if not await self.check_permission(Permission.DEVICE_WRITE_POINT):
                self._log_error(device_id, "ERR_AB_RBAC_DENIED", f"role={self._current_user_role} lacks device:write_point, point={point}")
                return False
        if not self._running or not self._client:
            now_mono = time.monotonic()  # FIXED-P0: write路径重连速率限制
            last_attempt = self._last_reconnect_attempt.get(device_id, 0.0)
            if now_mono - last_attempt >= 30.0:
                self._last_reconnect_attempt[device_id] = now_mono
                await self._try_reconnect(device_id)
            if not self._running or not self._client:
                return False

        if not self._check_write_rate(point):
            self._log_error(device_id, "ERR_AB_WRITE_RATE_LIMITED", f"point={point}")
            self._record_write_audit(device_id, point, None, value, False, "ERR_AB_WRITE_RATE_LIMITED")
            return False

        pcfg = self._get_point_config(point)
        data_type = pcfg.get("data_type", "")
        validated, err = self._validate_write_value(point, value, data_type, device_id)
        if err:
            self._log_error(device_id, err, f"point={point} value={value} type={data_type}")
            self._record_write_audit(device_id, point, None, value, False, err)
            self._record_write_failure(device_id)
            return False
        value = validated

        old_value = None
        async with self._device_clients_lock:
            write_client = self._device_clients.get(device_id, self._client)
        write_lock = self._device_locks.get(device_id, self._lock)
        try:
            resp = await asyncio.wait_for(self._run_in_thread(write_client.Read, point), timeout=10.0)
            old_value = self._parse_response_value(resp, point)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("[ab] device=%s point=%s failed to read old value for audit: %s", device_id, point, e)

        for attempt in range(3):
            try:
                record_packet("tx", "ab", device_id, f"CIP Write: {point}={value}")
                async with self._device_clients_lock:
                    current_write_client = self._device_clients.get(device_id, self._client)
                try:
                    async with write_lock:
                        response = await asyncio.wait_for(self._run_in_thread(current_write_client.Write, point, value), timeout=30.0)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self._log_error(device_id, "ERR_AB_DEVICE_ISOLATED_WRITE", f"isolated client error: {e}")
                    async with self._device_clients_lock:
                        fallback_client = self._client
                    if fallback_client:
                        response = await asyncio.wait_for(self._run_in_thread(self._sync_write_tag, point, value), timeout=30.0)
                    else:
                        raise
                status = getattr(response, 'Status', 1)
                if status == 0:
                    verified = await self._write_verify(point, value)
                    if not verified:
                        self._log_error(device_id, "ERR_AB_WRITE_VERIFY_FAILED", f"point={point}")
                        self._record_write_audit(device_id, point, old_value, value, False, "ERR_AB_WRITE_VERIFY_FAILED")
                        self._record_write_failure(device_id)
                        return False
                    self._record_write_success(device_id)
                    self._record_write_audit(device_id, point, old_value, value, True)
                    record_packet("rx", "ab", device_id, "CIP Write Response: OK")
                    return True
                else:
                    cip_err = self._parse_cip_status(status)
                    self._log_error(device_id, "ERR_AB_WRITE_FAILED", f"point={point} cip={cip_err} status=0x{status:02X}")
                    self._record_write_audit(device_id, point, old_value, value, False, cip_err)
                    self._record_write_failure(device_id)
                    record_packet("rx", "ab", device_id, f"CIP Write Response: Status=0x{status:02X}")
                    return False
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._log_error(device_id, "ERR_AB_WRITE_FAILED", f"point={point} error={e} attempt={attempt + 1}/3")
                if attempt < 2:
                    await asyncio.sleep(self._calc_retry_delay(attempt))
                else:
                    self._record_write_audit(device_id, point, old_value, value, False, "ERR_AB_WRITE_FAILED")
                    self._record_write_failure(device_id)
                    return False
        return False

    async def batch_write_points(self, device_id, writes):
        if not self._running or not self._client:
            now_mono = time.monotonic()  # FIXED-P0: write路径重连速率限制
            last_attempt = self._last_reconnect_attempt.get(device_id, 0.0)
            if now_mono - last_attempt >= 30.0:
                self._last_reconnect_attempt[device_id] = now_mono
                await self._try_reconnect(device_id)
            if not self._running or not self._client:
                return {point: False for point, _ in writes}

        validated_writes = []
        result = {}
        for point, value in writes:
            if not self._check_write_rate(point):
                self._log_error(device_id, "ERR_AB_WRITE_RATE_LIMITED", f"point={point}")
                self._record_write_audit(device_id, point, None, value, False, "ERR_AB_WRITE_RATE_LIMITED")
                result[point] = False
                continue
            pcfg = self._get_point_config(point)
            data_type = pcfg.get("data_type", "")
            v, err = self._validate_write_value(point, value, data_type, device_id)
            if err:
                self._log_error(device_id, err, f"point={point} value={value}")
                self._record_write_audit(device_id, point, None, value, False, err)
                result[point] = False
                continue
            validated_writes.append((point, v))

        if not validated_writes:
            return result

        tags = [p for p, _ in validated_writes]
        values_list = [v for _, v in validated_writes]

        try:
            record_packet("tx", "ab", device_id, f"CIP BatchWrite: {len(tags)} tags")
            async with self._device_clients_lock:
                batch_write_client = self._client
            if not batch_write_client:
                return {p: False for p in tags}
            response = await self._run_in_thread(self._sync_write_tag, tags, values_list)
            if isinstance(response, list):
                for i, point in enumerate(tags):
                    if i < len(response):
                        item = response[i]
                        status = getattr(item, 'Status', 1)
                        ok = status == 0
                        result[point] = ok
                        if ok:
                            self._record_write_success(device_id)
                            self._record_write_audit(device_id, point, None, values_list[i], True)
                        else:
                            cip_err = self._parse_cip_status(status)
                            self._record_write_failure(device_id)
                            self._record_write_audit(device_id, point, None, values_list[i], False, cip_err)
                    else:
                        result[point] = False
            else:
                ok = getattr(response, 'Status', 1) == 0
                for point in tags:
                    result[point] = ok
                if ok:
                    self._record_write_success(device_id)
                else:
                    self._record_write_failure(device_id)
            record_packet("rx", "ab", device_id, f"CIP BatchWrite Response: {result}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._log_error(device_id, "ERR_AB_WRITE_FAILED", f"batch error={e}, fallback per-point")

            async def _do_write(point, value):
                for attempt in range(3):
                    try:
                        async with self._device_clients_lock:
                            write_ref = self._client
                        if not write_ref:
                            return False
                        resp = await self._run_in_thread(self._sync_write_tag, point, value)
                        status = getattr(resp, 'Status', 1)
                        if status == 0:
                            verified = await self._write_verify(point, value)
                            return verified
                        cip_err = self._parse_cip_status(status)
                        self._record_write_audit(device_id, point, None, value, False, cip_err)
                        return False
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.warning("[ab] operation failed: %s", e, exc_info=True)
                        if attempt < 2:
                            await asyncio.sleep(self._calc_retry_delay(attempt))
                return False

            tasks = [_do_write(p, v) for p, v in validated_writes]
            results_list = await asyncio.gather(*tasks, return_exceptions=True)
            for i, r in enumerate(results_list):
                point = tags[i]
                result[point] = False if isinstance(r, Exception) else bool(r)

        return result

    def _validate_bool_array_offset(self, tag: str) -> bool:
        import re
        match = re.search(r'\.(\d+)$', tag)
        if not match:
            return True
        bit_offset = int(match.group(1))
        base_tag = tag[:match.start()]
        for dev_info in list(self._devices.values()):  # FIXED-P1: 使用list()快照，防止迭代中dict被修改
            pts = dev_info.get("points", {})
            pinfo = pts.get(tag, pts.get(base_tag, {}))
            if pinfo:
                dims = pinfo.get("dimensions", [])
                if dims:
                    max_bits = dims[0] * 32 if len(dims) == 1 else dims[0]
                    if bit_offset >= max_bits:
                        self._log_error("", "ERR_AB_BOOL_OFFSET_OUT_OF_BOUNDS", f"tag={tag} offset={bit_offset} max={max_bits}")
                        return False
                break
        return True

    async def _try_reconnect(self, device_id: str) -> None:
        if not self._config:
            return
        if time.time() < self._reconnect_cooldown_until:  # FIXED-P1: 冷却期内跳过重连
            return
        async with self._reconnect_lock:
            self._reconnect_count += 1
            if self._reconnect_count > self._MAX_RECONNECT_ATTEMPTS:
                self._log_error(device_id, "ERR_AB_RECONNECT_FAILED", f"abandoned after {self._reconnect_count} attempts")
                self._set_conn_state(AbConnState.OFFLINE.value, device_id, "max reconnect attempts reached")
                self._reconnect_cooldown_until = time.time() + 3600  # FIXED-P1: 达到上限后设置1小时冷却期，而非重置计数器允许立即重试
                self._reconnect_count = 0
                return

            has_backup = bool(self._backup_ip) and not self._using_backup
            if has_backup and self._failover_start_mono == 0.0:
                self._failover_start_mono = time.monotonic()

            delay = self._calc_backoff_delay()
            self._set_conn_state(AbConnState.CONNECTING.value, device_id, f"reconnect attempt={self._reconnect_count}")
            logger.warning("[ab] code=CONNECTION_LOST device=%s delay=%.3fs attempt=%d backup=%s", device_id, delay, self._reconnect_count, has_backup)

        # FIXED-P1: 将sleep移到锁外执行，避免持锁期间阻塞其他协程
        await asyncio.sleep(delay)

        # FIXED-P2: 移除locked()检查，直接获取锁，在锁内检查是否已有其他重连成功
        # 之前：locked()检查与锁获取之间存在TOCTOU窗口，两个协程可能同时执行重连
        # 之后：直接获取锁，锁内检查连接状态，若已连接则跳过重连
        async with self._reconnect_lock:
            # 锁内检查：若其他协程已成功重连，则跳过
            if self._client is not None and self._conn_state.get(device_id) == AbConnState.CONNECTED.value:
                logger.debug("[ab] Another reconnect already succeeded for %s, skipping", device_id)
                return
            target_ip = self._active_ip
            target_port = int(self._config.get("port", 44818))

            if has_backup:
                self._primary_fail_count += 1
                threshold = int(self._config.get("failover_threshold", self._FAILOVER_THRESHOLD))
                if self._primary_fail_count >= threshold:
                    target_ip = self._backup_ip
                    bp = self._config.get("backup_port")
                    target_port = int(bp) if bp else int(self._config.get("port", 44818))
                    self._active_ip = self._backup_ip
                    self._using_backup = True
                    self._failover_count += 1
                    failover_dur = (time.monotonic() - self._failover_start_mono) * 1000
                    self._failover_start_mono = 0.0
                    self._last_failover_duration_ms = failover_dur
                    self._last_failover_time = datetime.now(UTC).isoformat()
                    self._log_error(device_id, "ERR_AB_FAILOVER_TRIGGERED", f"primary->{self._backup_ip} dur={failover_dur:.0f}ms")
                    if self._audit:
                        self._audit.log_failover(device_id, self._primary_ip, self._backup_ip)
                    if failover_dur > 3000:
                        self._log_error(device_id, "ERR_AB_FAILOVER_FAST", f"failover took {failover_dur:.0f}ms > 3000ms target")

            slot = int(self._config.get("slot", 0))
            if not target_ip:
                return
            if self._client:
                try:
                    await self._forward_close()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.debug("[ab] operation failed: %s", e)
            try:
                from pylogix import PLC
                tls_enabled = bool(self._config.get("tls_enabled", False))
                if tls_enabled:
                    test_sock = None  # FIXED-P1: 确保TLS测试socket在异常路径也被关闭
                    try:
                        import socket as _socket
                        import ssl as _ssl
                        test_sock = _socket.create_connection((target_ip, target_port), timeout=self._connection_timeout)
                        ctx = _ssl.create_default_context()
                        ca_cert = self._config.get("ca_cert", "")
                        if ca_cert:
                            ctx.load_verify_locations(ca_cert)
                        client_cert = self._config.get("client_cert", "")
                        client_key = self._config.get("client_key", "")
                        if client_cert and client_key:
                            ctx.load_cert_chain(client_cert, client_key)
                        test_sock = ctx.wrap_socket(test_sock, server_hostname=target_ip)
                        test_sock.close()
                        test_sock = None
                    except asyncio.CancelledError:
                        if test_sock is not None:
                            try:
                                test_sock.close()
                            except Exception as e:
                                logger.warning("[ab] operation failed: %s", e)  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                        raise
                    except Exception:
                        if test_sock is not None:  # FIXED-P1: wrap_socket异常时关闭原始socket
                            try:
                                test_sock.close()
                            except Exception as e:
                                logger.warning("[ab] operation failed: %s", e)  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                        logger.warning("AB driver: TLS connection failed for %s, falling back to non-encrypted connection", device_id)
                self._set_conn_state(AbConnState.CIP_NEGOTIATING.value, device_id, f"connecting to {target_ip}")
                with self._sync_lock:  # FIXED-P1: client赋值需在_sync_lock保护下，与读取方法一致
                    self._client = PLC(ip=target_ip, port=target_port, slot=slot)
                if hasattr(self._client, 'SocketTimeout'):
                    self._client.SocketTimeout = self._connection_timeout
                large_forward_open = self._config.get("large_forward_open", False)
                if large_forward_open and hasattr(self._client, 'LargeForwardOpen'):
                    self._client.LargeForwardOpen = True
                elif not self._using_backup and hasattr(self._client, 'LargeForwardOpen'):
                    try:
                        self._client.LargeForwardOpen = True
                        self._large_forward_open_auto = True
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        self._client.LargeForwardOpen = False
                cip_user = self._config.get("cip_username", "")
                cip_pass = self._config.get("cip_password", "")
                if cip_user and cip_pass and hasattr(self._client, "set_cip_security"):
                    try:
                        self._client.set_cip_security(username=cip_user, password=cip_pass)
                        self._cip_security_enabled = True
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        self._log_error(device_id, "ERR_AB_CIP_SECURITY_FAILED", f"reconnect error={e}")
                # FIXED-P1: 重连后验证连接可用性，PLC()是惰性连接，ping失败则不标记CONNECTED
                ping_ok = await asyncio.to_thread(self._sync_ping)
                if not ping_ok:
                    self._log_error(device_id, "ERR_AB_RECONNECT_PING_FAILED", f"ping verification failed after reconnect to {target_ip}")
                    self._set_conn_state(AbConnState.DISCONNECTED.value, device_id, "ping verification failed after reconnect")
                    # FIXED-P1: 原问题-isolated client创建后未关闭，ping验证失败时self._client仍持有CIP连接
                    #           修复-在finally块中关闭isolated client，防止CIP连接泄漏
                    try:
                        await self._forward_close()
                    except Exception as e:
                        logger.debug("[ab] close failed after ping verification failure: %s", e)
                    self._client = None
                    return
                self._running = True
                self._reconnect_count = 0
                self._reconnect_cooldown_until = 0.0  # FIXED-P2: 重连成功后重置冷却期，允许后续自动重连
                self._reconnect_delay = self._RECONNECT_BASE_DELAY
                self._set_conn_state(AbConnState.CONNECTED.value, device_id, target_ip)
                async with self._device_clients_lock:
                    for did, old_dev_client in list(self._device_clients.items()):
                        try:
                            await asyncio.wait_for(self._run_in_thread(old_dev_client.Close), timeout=5.0)
                        except asyncio.CancelledError:
                            raise
                        except Exception as e:
                            logger.warning("[ab] code=RECONNECT_CLEANUP_FAILED device=%s Close failed: %s", did, e)
                        dev_config = self._device_configs.get(did, {})
                        dev_ip = dev_config.get("ip", target_ip)
                        dev_port = int(dev_config.get("port", target_port))
                        dev_slot = int(dev_config.get("slot", slot))
                        try:
                            new_dev_client = PLC(ip=dev_ip, port=dev_port, slot=dev_slot)
                            if hasattr(new_dev_client, 'SocketTimeout'):
                                new_dev_client.SocketTimeout = self._connection_timeout
                            self._device_clients[did] = new_dev_client
                        except asyncio.CancelledError:
                            raise
                        except Exception as e:
                            logger.warning("[ab] operation failed: %s", e, exc_info=True)
                            self._device_clients.pop(did, None)
                logger.info("[ab] code=RECONNECTED ip=%s:%d slot=%d using_backup=%s", target_ip, target_port, slot, self._using_backup)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._log_error(device_id, "ERR_AB_RECONNECT_FAILED", f"ip={target_ip} error={e}")
                if self._reconnect_count >= self._MAX_RECONNECT_ATTEMPTS:
                    self._set_conn_state(AbConnState.OFFLINE.value, device_id, str(e))
                else:
                    self._set_conn_state(AbConnState.DISCONNECTED.value, device_id, str(e))

    async def _try_revert_primary(self, device_id: str) -> None:  # FIXED-P1: 改为async，创建新连接到主IP验证后再替换
        if not self._using_backup or not self._primary_ip:
            return
        # FIXED-P1: 原问题-模式切换(CAS/EIP failover/revert)无锁保护，_try_revert_primary与_try_reconnect并发执行时
        #           _using_backup/_active_ip/_client等状态可能不一致；修复-添加_failover_mode_lock保护整个切换操作
        await self._failover_mode_lock.acquire()
        try:
            from pylogix import PLC
            primary_port = int(self._config.get("port", 44818))
            primary_slot = int(self._config.get("slot", 0))
            new_client = PLC(ip=self._primary_ip, port=primary_port, slot=primary_slot)
            if hasattr(new_client, 'SocketTimeout'):
                new_client.SocketTimeout = self._connection_timeout
            # 验证新连接可用性
            ping_ok = False
            try:
                resp = await self._run_in_thread(new_client.Read, "@cpu", timeout=5.0)  # FIXED-P2: 同步Read移入线程池，避免阻塞事件循环
                ping_ok = (getattr(resp, 'Status', None) == 0)
            except Exception:
                ping_ok = False
            if not ping_ok:
                # 新连接失败，关闭新client，保持备用连接不变
                try:
                    await self._run_in_thread(new_client.Close)  # FIXED-P1: Close()移入线程池，避免同步CIP ForwardClose阻塞事件循环
                except Exception as e:
                    logger.warning("[ab] try_revert_primary failed: %s", e)  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                self._log_error(device_id, "ERR_AB_FAILOVER_REVERT_FAILED", f"primary {self._primary_ip} ping failed, staying on backup")
                return
            # FIXED-P1: client替换使用compare-and-swap模式，防止_try_revert_primary与_try_reconnect并发替换导致连接泄漏
            with self._sync_lock:
                old_client = self._client
                self._client = new_client
            # 关闭旧备用连接
            if old_client:
                try:
                    await asyncio.wait_for(self._run_in_thread(old_client.Close), timeout=5.0)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.debug("[ab] operation failed: %s", e)
            self._active_ip = self._primary_ip
            self._using_backup = False
            self._primary_fail_count = 0
            self._failover_start_mono = 0.0
            self._log_error(device_id, "ERR_AB_FAILOVER_REVERT", f"backup->{self._primary_ip}")
            if self._audit:
                self._audit.log_failover(device_id, self._backup_ip, self._primary_ip)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._log_error(device_id, "ERR_AB_FAILOVER_REVERT_FAILED", f"revert to primary failed: {e}")
        finally:
            self._failover_mode_lock.release()

    async def _failover_probe_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._FAILOVER_PROBE_INTERVAL)
                if not self._running or not self._using_backup or not self._primary_ip:
                    continue
                probe_client = None  # FIXED-P0: 初始化probe_client，防止CancelledError时引用未定义变量
                try:
                    from pylogix import PLC
                    probe_port = int(self._config.get("port", 44818))
                    probe_slot = int(self._config.get("slot", 0))
                    probe_client = PLC(ip=self._primary_ip, port=probe_port, slot=probe_slot)
                    try:
                        default_tag = self._config.get("default_tag", self._DEFAULT_TAG)
                        resp = await asyncio.wait_for(self._run_in_thread(probe_client.Read, default_tag), timeout=10.0)
                        if getattr(resp, 'Status', None) == 0:
                            await self._try_revert_primary(next(iter(self._devices), ""))  # FIXED-P1: _try_revert_primary改为async，需await
                    finally:
                        try:
                            await asyncio.wait_for(self._run_in_thread(probe_client.Close), timeout=5.0)
                        except asyncio.CancelledError:
                            raise
                        except Exception as e:
                            logger.warning("[ab] code=PROBE_CLOSE_FAILED Close failed: %s", e)
                except asyncio.CancelledError:
                    if probe_client is not None:  # FIXED-P2: CancelledError时也关闭probe_client，防止CIP连接泄漏
                        try:
                            await asyncio.wait_for(self._run_in_thread(probe_client.Close), timeout=5.0)  # FIXED-P1: 同步Close改为异步执行，避免阻塞事件循环
                        except Exception as e:
                            logger.warning("[ab] failover_probe_loop failed: %s", e)  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                    raise
                except Exception as e:
                    logger.warning("[ab] code=PROBE_READ_FAILED probe read failed: %s", e)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("[ab] failover probe error: %s", e, exc_info=True)

    def get_failover_info(self) -> dict:
        return {
            "primary_ip": self._primary_ip,
            "backup_ip": self._backup_ip,
            "active_ip": self._active_ip,
            "using_backup": self._using_backup,
            "primary_fail_count": self._primary_fail_count,
            "failover_count": self._failover_count,
            "last_failover_time": self._last_failover_time,
            "last_failover_duration_ms": self._last_failover_duration_ms,
        }

    def get_point_stats(self, device_id: str, point_name: str) -> dict | None:
        stats = self._point_stats.get(point_name)
        if not stats:
            return None
        return {
            "success_count": stats.success_count,
            "fail_count": stats.fail_count,
            "avg_latency_ms": stats.avg_latency_ms,
            "consecutive_fails": stats.consecutive_fails,
            "total": stats.success_count + stats.fail_count,
            "success": stats.success_count,
            "quality": "bad" if stats.consecutive_fails > 0 else "good",
            "tag_type": self._get_point_tag_type(point_name),
        }

    def _get_point_tag_type(self, point_name: str) -> str:
        for dev_info in list(self._devices.values()):  # FIXED-P1: 使用list()快照，防止迭代中dict被修改
            pts = dev_info.get("points", {})
            pinfo = pts.get(point_name, {})
            if pinfo:
                return pinfo.get("type", pinfo.get("data_type", "unknown"))
        return "unknown"

    def get_cip_error_dist(self) -> dict[str, int]:
        # #[AUDIT-FIX] W9: 实现 CIP 错误分布统计，按错误码聚合所有测点的失败次数
        dist: dict[str, int] = {}
        for stats in self._point_stats.values():
            if stats.last_cip_error and stats.fail_count > 0:
                dist[stats.last_cip_error] = dist.get(stats.last_cip_error, 0) + stats.fail_count
        return dist

    def _init_edge_rules(self) -> None:
        try:
            from edgelite.drivers.edge_rule_engine import ModbusEdgeRuleEngine
            from edgelite.drivers.edge_triggers import EdgeTriggerExecutor
            from edgelite.drivers.rule_store import RuleStore
            try:
                from edgelite.engine.event_bus import EventBus
                event_bus = EventBus()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("[ab] operation failed: %s", e, exc_info=True)
                event_bus = None
            self._rule_store = RuleStore()
            self._rule_engine = ModbusEdgeRuleEngine(event_bus=event_bus)
            self._trigger_executor = EdgeTriggerExecutor(
                device_write_callback=self._edge_write_callback,
            )
            self._rule_engine.set_on_action_callback(self._trigger_executor.execute)
            rules = self._rule_store.load_rules()
            for r in rules:
                self._rule_engine.add_rule(r)
            logger.info("[ab] edge rules loaded: %d rules", len(rules))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._log_error("", "ERR_AB_RULE_ENGINE_INIT_FAILED", str(e))
            self._rule_engine = None
            self._trigger_executor = None
            self._rule_store = None

    async def _edge_write_callback(self, action: dict, context: dict) -> dict:
        target_device = action.get("device_id", "")
        target_point = action.get("point", "")
        value = action.get("value")
        if not target_point or value is None:
            return {"type": "write_point", "error": "missing point or value"}
        if not target_device:
            if not self._devices:
                logger.error("[ab] No devices available for edge write callback")
                return {"type": "write_point", "error": "no devices available"}
            target_device = next(iter(self._devices), "")  # FIXED-P1: 空dict安全取值，避免IndexError
        try:
            ok = await self.write_point(target_device, target_point, value)
            return {"type": "write_point", "point": target_point, "value": value, "success": ok}
        except asyncio.CancelledError:
            raise
        except Exception as e:
            return {"type": "write_point", "point": target_point, "value": value, "error": str(e)}

    async def _evaluate_rules(self, device_id: str, result: dict[str, Any]) -> None:
        if not self._rule_engine:
            return
        for point_name, pv in result.items():
            if not isinstance(pv, PointValue) or pv.quality != "good":
                continue
            if not isinstance(pv.value, (int, float)):
                continue
            try:
                await self._rule_engine.evaluate_point(device_id, point_name, pv.value, pv.quality)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("[ab] rule eval failed: %s %s - %s", device_id, point_name, e)

    async def reload_rules(self) -> int:
        if not self._rule_engine or not self._rule_store:
            return 0
        existing = {r.rule_id for r in self._rule_engine._rules.values()}
        for rid in existing:
            await self._rule_engine.remove_rule(rid)
        rules = self._rule_store.load_rules()
        for r in rules:
            self._rule_engine.add_rule(r)
        logger.info("[ab] rules reloaded: %d rules", len(rules))
        return len(rules)

    def add_edge_rule(self, rule_dict: dict) -> bool:
        if not self._rule_engine or not self._rule_store:
            return False
        from edgelite.drivers.edge_rule_engine import EdgeRule, EdgeRuleOperator, EdgeRuleType
        try:
            rule = EdgeRule(
                rule_id=rule_dict["rule_id"],
                device_id=rule_dict.get("device_id", ""),
                point_name=rule_dict.get("point_name", ""),
                rule_type=EdgeRuleType(rule_dict.get("rule_type", "threshold")),
                operator=EdgeRuleOperator(rule_dict.get("operator", ">")),
                threshold=float(rule_dict.get("threshold", 0)),
                severity=rule_dict.get("severity", "major"),
                enabled=rule_dict.get("enabled", True),
                cooldown_ms=float(rule_dict.get("cooldown_ms", 5000)),
                duration_ms=float(rule_dict.get("duration_ms", 0)),
                deadband=float(rule_dict.get("deadband", 0)),
                actions=rule_dict.get("actions", []),
            )
            self._rule_store.save_rule(rule)
            self._rule_engine.add_rule(rule)
            logger.info("[ab] edge rule added: %s", rule.rule_id)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._log_error("", "ERR_AB_RULE_ADD_FAILED", str(e))
            return False

    async def remove_edge_rule(self, rule_id: str) -> bool:
        if not self._rule_engine or not self._rule_store:
            return False
        await self._rule_engine.remove_rule(rule_id)
        self._rule_store.delete_rule(rule_id)
        return True

    def get_edge_rules(self) -> list[dict]:
        if not self._rule_engine:
            return []
        return self._rule_engine.get_all_rules()

    def get_edge_alarm_history(self, limit: int = 100) -> list[dict]:
        if not self._rule_engine:
            return []
        return self._rule_engine.get_alarm_history(limit=limit)

    def get_edge_rule_stats(self) -> dict:
        if not self._rule_engine:
            return {}
        return self._rule_engine.get_stats()

    def _init_ts_storage(self, config: dict) -> None:
        ts_config = config.get("ts_storage", {})
        if not ts_config:
            ts_config = {
                "enabled": config.get("ts_storage_enabled", False),
                "influx_url": config.get("ts_influx_url", ""),
                "influx_org": config.get("ts_influx_org", "edgelite"),
                "influx_bucket": config.get("ts_influx_bucket", "edgelite"),
                "influx_token": config.get("ts_influx_token", ""),
            }
        if not ts_config.get("enabled", False):
            self._persist_enabled = False
            return
        try:
            from edgelite.drivers.ab_ts_store import AbOfflineSyncManager, AbTsStore
            ts_retention = int(config.get("ts_retention_days", 7))
            self._ts_store = AbTsStore(retention_days=ts_retention)
            self._offline_sync = AbOfflineSyncManager(
                ts_store=self._ts_store,
                sync_interval=float(config.get("offline_sync_interval", 30.0)),
                batch_size=int(config.get("offline_batch_size", 1000)),
                compress=config.get("offline_compress", "gzip"),
            )
            self._persist_enabled = True
            logger.info("[ab] ts storage initialized, retention=%ddays", ts_retention)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._log_error("", "ERR_AB_TS_STORAGE_INIT_FAILED", str(e))
            self._persist_enabled = False
            self._ts_store = None
            self._offline_sync = None

    async def _persist_points(self, device_id: str, result: dict[str, Any]) -> None:
        if not self._persist_enabled or not self._ts_store:
            return
        has_good = any(isinstance(v, PointValue) and v.quality == "good" for v in result.values())
        if not has_good:
            return
        try:
            await self._ts_store.write_read_result(device_id, result)
            if not self._offline_sync:
                return
            if self._conn_state in (AbConnState.OFFLINE.value, AbConnState.DISCONNECTED.value):
                for point_name, pv in result.items():
                    if not isinstance(pv, PointValue) or pv.quality != "good":
                        continue
                    await self._offline_sync.enqueue(device_id, point_name, pv.value, pv.quality)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._log_error(device_id, "ERR_AB_TS_PERSIST_FAILED", str(e))

    def get_ts_store_stats(self) -> dict:
        if not self._ts_store:
            return {}
        stats = self._ts_store.get_stats()
        if self._offline_sync:
            stats["offline_sync"] = self._offline_sync.get_stats()
        return stats

    async def add_device(self, device_id: str, config: dict, points: list[dict] | None = None) -> None:
        self._device_configs[device_id] = config
        self._devices[device_id] = {
            "config": config,
            "points": {p.get("name", p.get("address", "")): p for p in (points or []) if p.get("name") or p.get("address")},
        }
        self._device_points[device_id] = points or []  # FIXED-P1: 存储设备测点列表，供remove_device清理使用
        self._device_locks.setdefault(device_id, asyncio.Lock())
        try:
            from pylogix import PLC
            dev_ip = config.get("ip", self._config.get("ip", ""))
            dev_port = int(config.get("port", self._config.get("port", 44818)))
            dev_slot = int(config.get("slot", self._config.get("slot", 0)))
            if dev_ip:
                async with self._device_clients_lock:
                    old_client = self._device_clients.get(device_id)  # FIXED-P2: 关闭旧客户端防止连接泄漏
                    if old_client is not None:
                        try:
                            await self._run_in_thread(old_client.Close)  # FIXED-P2: 同步Close改为线程池执行，防止阻塞事件循环
                        except Exception as e:
                            logger.warning("[ab] add_device failed: %s", e)  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                dev_client = PLC(ip=dev_ip, port=dev_port, slot=dev_slot)
                if hasattr(dev_client, 'SocketTimeout'):
                    dev_client.SocketTimeout = self._connection_timeout if hasattr(self, '_connection_timeout') else 5.0
                try:  # FIXED-P0: 客户端注册异常时关闭新客户端防止泄漏
                    async with self._device_clients_lock:
                        self._device_clients[device_id] = dev_client
                except Exception:
                    try:
                        dev_client.Close()
                    except Exception as e:
                        logger.warning("[ab] add_device failed: %s", e)  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                    raise
                logger.info("[ab] device=%s isolated CIP session created: %s:%d slot=%d", device_id, dev_ip, dev_port, dev_slot)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("[ab] device=%s isolated session failed, will use shared client: %s", device_id, e)
        if points:
            for p in points:
                pname = p.get("name", p.get("address", ""))
                if not pname:
                    continue
                pcfg = {}
                if "deadband" in p and p["deadband"] is not None:
                    pcfg["deadband"] = p["deadband"]
                if "scaling_ratio" in p or "scaling_offset" in p:
                    pcfg["scaling"] = {"ratio": float(p.get("scaling_ratio", 1.0)), "offset": float(p.get("scaling_offset", 0.0))}
                if "clamp_min" in p or "clamp_max" in p:
                    clamp = {}
                    if p.get("clamp_min") is not None:
                        clamp["min"] = float(p["clamp_min"])
                    if p.get("clamp_max") is not None:
                        clamp["max"] = float(p["clamp_max"])
                    pcfg["clamp"] = clamp
                if pcfg:
                    self._point_configs[pname] = pcfg
        logger.info("[ab] device added: %s (%d points)", device_id, len(points) if points else 0)

    async def discover_devices(self, config: dict) -> list[dict]:
        """AB PLC发现 - 获取控制器信息和所有程序

        Returns:
            [{"device_id", "name", "ip", "slot", "model", "cip_security", "programs": [str], ...}]
        """
        if not self._client:
            return []

        ip = self._config.get("ip", "unknown")
        slot = int(self._config.get("slot", 0))
        plc_model = self._config.get("plc_model", "ControlLogix")
        programs: list[str] = []

        try:
            # 读取项目名称
            response = await self._run_in_thread(self._sync_read_tag, "ProgramName")
            project_name = response.Value if response.Status == 0 else "Unknown"
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("[ab] operation failed: %s", e)
            project_name = "Unknown"

        # 发现所有程序标签
        try:
            programs = await self._discover_programs()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("[ab] code=PROGRAM_DISCOVERY_FAILED error=%s", e)

        results = [
            {
                "device_id": f"ab_{ip.replace('.', '_')}",
                "name": f"AB PLC ({project_name})",
                "ip": ip,
                "slot": slot,
                "model": plc_model,
                "protocol": "allen_bradley",
                "cip_security": self._cip_security_enabled,
                "programs": programs,
            }
        ]

        # 如果是ControlLogix，添加每个程序作为子设备
        if plc_model == "ControlLogix" and programs:
            for prog in programs:
                results.append({
                    "device_id": f"ab_{ip.replace('.', '_')}_{prog}",
                    "name": f"AB Program ({prog})",
                    "ip": ip,
                    "slot": slot,
                    "model": "Program",
                    "program_name": prog,
                    "protocol": "allen_bradley",
                    "cip_security": self._cip_security_enabled,
                })

        return results

    async def _discover_programs(self) -> list[str]:
        """发现PLC中所有程序名"""
        if not self._client:
            return []

        try:
            # GetTagList with empty program returns controller tags
            # Call GetProgramList if available
            if hasattr(self._client, 'GetProgramList'):
                response = await self._run_in_thread(self._sync_get_program_list)
                if response and response.Value:
                    return [p.ProgramName for p in response.Value if hasattr(p, 'ProgramName')]
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("[ab] operation failed: %s", e)

        common_programs = ["MainProgram", "Main", "Program_1", "Program_2"]
        found: list[str] = []
        for prog in common_programs:
            try:
                tag_path = f"Program:{prog}.ProgramName"
                resp = await self._run_in_thread(self._sync_read_tag, tag_path)
                if resp.Status == 0:
                    found.append(prog)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("[ab] operation failed: %s", e)
        return found

    async def discover_tags(self, program: str = "", device_id: str = "") -> list[dict]:
        """发现PLC标签（AB特有功能）

        Args:
            program: 程序名，空字符串表示控制器标签，非空如 "Main" 表示 Program:Main 下的标签
            device_id: 设备ID（可选，用于路由到正确的PLC）

        Returns:
            [{"name": str, "tag_name": str, "type": str, "dimensions": list[int],
              "is_array": bool, "is_struct": bool, "member_count": int, "path": str}]
        """
        if not self._client:
            return []

        if not program:
            tag_list_result = await asyncio.wait_for(
                self._run_in_thread(self._sync_get_tag_list, ""),
                timeout=self._browse_timeout,
            )
        else:
            tag_list_result = await asyncio.wait_for(
                self._run_in_thread(self._sync_get_tag_list, program),
                timeout=self._browse_timeout,
            )

        tags: list[dict] = []
        try:
            raw_tags = tag_list_result.Value if tag_list_result else []
            for tag in raw_tags:
                if tag is None:
                    continue
                tag_name = getattr(tag, 'TagName', str(tag))
                data_type = getattr(tag, 'DataType', 'unknown')
                array_dims_raw = getattr(tag, 'ArrayDimensions', None)
                dimensions: list[int] = []
                if array_dims_raw:
                    if isinstance(array_dims_raw, (list, tuple)):
                        dimensions = [int(d) for d in array_dims_raw if d is not None and d > 0]
                    else:
                        dimensions = [int(array_dims_raw)]
                is_array = len(dimensions) > 0
                is_struct = data_type.lower() not in (
                    'bool', 'sint', 'int', 'dint', 'lint',
                    'usint', 'uint', 'udint', 'ulint',
                    'real', 'lreal', 'string',
                ) and not is_array
                prefix = f"Program:{program}." if program else ""
                tags.append({
                    "name": tag_name,
                    "tag_name": prefix + tag_name,
                    "type": data_type,
                    "dimensions": dimensions,
                    "is_array": is_array,
                    "is_struct": is_struct,
                    "member_count": 0,  # Set to 0 for controller tags; for program tags see browse_struct_members
                    "path": prefix + tag_name,
                    "program": program or "<controller>",
                    "device_id": device_id,
                })
        except TimeoutError:
            logger.warning("[ab] code=TAG_DISCOVERY_TIMEOUT program=%s timeout=%ss", program, self._browse_timeout)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("[ab] code=TAG_DISCOVERY_FAILED program=%s error=%s", program, e)

        return tags

    async def browse_struct_members(self, struct_tag: str) -> list[dict]:
        """浏览结构体标签的成员

        Args:
            struct_tag: 结构体标签名，如 "Program:Main.MyStruct" 或 "MyStruct"

        Returns:
            [{"name": str, "type": str, "offset": int, "bit_length": int}]
        """
        if not self._client:
            return []

        try:
            program = ""
            tag_name = struct_tag
            if ":" in struct_tag:
                parts = struct_tag.split(":", 1)
                program = parts[0].replace("Program:", "")
                tag_name = parts[1]

            raw_list = await asyncio.wait_for(
                self._run_in_thread(self._sync_get_tag_list, program) if program else self._run_in_thread(self._sync_get_tag_list, ""),
                timeout=self._browse_timeout,
            )
            raw_tags = raw_list.Value if raw_list else []

            # 找到目标结构体标签
            target = None
            for tag in raw_tags:
                if getattr(tag, 'TagName', '') == tag_name:
                    target = tag
                    break

            if not target:
                return []

            # 读取结构体成员（通过读取标签，让pylogix展开结构）
            member_tag = struct_tag if ":" in struct_tag else tag_name
            resp = await asyncio.wait_for(
                self._run_in_thread(self._sync_read_tag, member_tag),
                timeout=self._browse_timeout,
            )
            if resp.Status != 0 or resp.Value is None:
                return []

            value = resp.Value
            members: list[dict] = []

            if hasattr(value, '_dict'):
                for idx, (k, v) in enumerate(value._dict.items()):
                    members.append({
                        "name": k,
                        "type": type(v).__name__,
                        "offset": idx * 4,
                        "bit_length": 0,
                        "value": v,
                    })
            elif hasattr(value, '__dict__'):
                for idx, (k, v) in enumerate(value.__dict__.items()):
                    if not k.startswith('_'):
                        members.append({
                            "name": k,
                            "type": type(v).__name__,
                            "offset": idx * 4,
                            "bit_length": 0,
                            "value": v,
                        })
            elif isinstance(value, dict):
                for idx, (k, v) in enumerate(value.items()):
                    members.append({
                        "name": k,
                        "type": type(v).__name__,
                        "offset": idx * 4,
                        "bit_length": 0,
                        "value": v,
                    })

            return members
        except TimeoutError:
            logger.warning("[ab] code=STRUCT_BROWSE_TIMEOUT tag=%s timeout=%ss", struct_tag, self._browse_timeout)
            return []
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("[ab] code=STRUCT_BROWSE_FAILED tag=%s error=%s", struct_tag, e)
            return []

    async def browse_array_range(self, array_tag: str, dim: int = 0) -> list[dict]:
        """浏览数组标签的有效索引范围

        Args:
            array_tag: 数组标签名，如 "TagName[0]" 或 "Program:Main.Arr[0]"
            dim: 维度索引 (0=第一维)

        Returns:
            [{"index": int, "value": Any}]
        """
        if not self._client:
            return []

        results: list[dict] = []
        try:
            resp = await asyncio.wait_for(
                self._run_in_thread(self._sync_read_tag, array_tag),
                timeout=self._browse_timeout,
            )
            if resp.Status == 0 and resp.Value is not None:
                # 返回单个值表示请求成功
                results.append({"index": 0, "value": resp.Value})
        except TimeoutError:
            logger.warning("[ab] code=ARRAY_BROWSE_TIMEOUT tag=%s timeout=%ss", array_tag, self._browse_timeout)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("[ab] code=ARRAY_BROWSE_FAILED tag=%s error=%s", array_tag, e)

        return results

    async def remove_device(self, device_id: str) -> None:
        async with self._device_clients_lock:
            dev_client = self._device_clients.pop(device_id, None)
        if dev_client:
            try:
                await asyncio.wait_for(self._run_in_thread(dev_client.Close), timeout=5.0)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("[ab] operation failed: %s", e)
        self._device_locks.pop(device_id, None)
        self._devices.pop(device_id, None)
        self._device_configs.pop(device_id, None)
        with self._stats_lock:  # FIXED-P2: 健康统计pop纳入_stats_lock，与写入路径锁保护一致
            self._health_stats.pop(device_id, None)
            self._offline_since.pop(device_id, None)
        # FIXED-P1: 使用_device_points获取该设备的测点名来清理，之前用device_id前缀匹配永远不命中
        device_points = self._device_points.pop(device_id, [])  # FIXED-P1: pop并清理_device_points，防止内存泄漏
        # FIXED-P2: 键推导逻辑需与add_device一致（name优先，fallback到address），否则无name的测点配置不会被清理
        point_keys = {pt.get("name", pt.get("address", "")) for pt in device_points if pt.get("name") or pt.get("address")}
        for pk in point_keys:
            self._point_configs.pop(pk, None)
        keys_to_del_ts = [k for k in self._last_timestamps if k in point_keys]
        for k in keys_to_del_ts:
            del self._last_timestamps[k]
        keys_to_del_wt = [k for k in self._last_write_time if k in point_keys]
        for k in keys_to_del_wt:
            del self._last_write_time[k]
        # FIXED-P2: 清理_point_stats中与该设备测点相关的条目，防止内存泄漏
        keys_to_del_ps = [k for k in self._point_stats if k in point_keys]
        for k in keys_to_del_ps:
            self._point_stats.pop(k, None)
        logger.info("[ab] device removed: %s", device_id)

    async def _watchdog_loop(self) -> None:
        """AB-MED-001: 支持多种健康检查模式的 watchdog 循环

        Modes:
            ping: 使用 CIP Identity 查询（不依赖特定 tag）
            tag:  读取特定 tag（传统方式）
            auto: 优先 ping，失败则回退 tag
        """
        while self._running:
            try:
                await asyncio.sleep(self._watchdog_interval)
                if not self._running:
                    break
                if not self._client:
                    continue

                # AB-MED-001: 根据配置选择检查模式
                connected = False
                check_mode = self._watchdog_check_mode

                if check_mode == "ping":
                    # 模式1: 仅使用 ping（CIP Identity）
                    try:
                        connected = await self._run_in_thread(self._sync_ping)
                    except Exception:
                        connected = False

                elif check_mode == "tag":
                    # 模式2: 仅使用 tag
                    default_tag = self._config.get("default_tag", self._DEFAULT_TAG)
                    try:
                        resp = await self._run_in_thread(self._sync_read_tag, default_tag)
                        status = getattr(resp, 'Status', None)
                        connected = (status == 0)
                    except Exception:
                        connected = False

                else:  # auto 模式
                    # 模式3: 优先 ping，失败则回退 tag
                    try:
                        connected = await self._run_in_thread(self._sync_ping)
                    except Exception:
                        connected = False

                    if not connected:
                        # ping 失败，尝试 tag
                        default_tag = self._config.get("default_tag", self._DEFAULT_TAG)
                        try:
                            resp = await self._run_in_thread(self._sync_read_tag, default_tag)
                            status = getattr(resp, 'Status', None)
                            connected = (status == 0)
                        except Exception:
                            connected = False

                if connected:
                    for did in list(self._devices.keys()):
                        self._offline_since.pop(did, None)
                        if self._conn_state != AbConnState.CONNECTED.value:
                            self._set_conn_state(AbConnState.CONNECTED.value, did, "watchdog ok")
                else:
                    for did in list(self._devices.keys()):
                        self._offline_since[did] = datetime.now(UTC)
                    # FIXED-SIM108: 使用三元表达式简化
                    did = next(iter(self._devices), "") if self._devices else ""  # FIXED-P0: 空字典时安全获取第一个key
                    if self._conn_state == AbConnState.CONNECTED.value:
                        self._set_conn_state(AbConnState.DEGRADED.value, did, "watchdog check failed")

            except asyncio.CancelledError:
                break
            except Exception as e:
                # CROSS-002: 分级处理异常，不静默吞没
                if not self._handle_watchdog_exception(e, "ab_watchdog"):
                    break

    async def health_check(self, device_id: str) -> bool:
        if not self._running or not self._client:
            return False
        try:
            tag = self._config.get("default_tag", self._DEFAULT_TAG)
            resp = await self._run_in_thread(self._sync_read_tag, tag)
            status = getattr(resp, 'Status', 1)
            return status == 0
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    def init_enterprise(self, audit_service=None) -> None:
        from edgelite.drivers.ab_audit import AbAudit
        from edgelite.drivers.ab_config_version import AbConfigVersionManager
        from edgelite.drivers.ab_ota import AbOtaManager
        self._config_version_mgr = AbConfigVersionManager()
        self._ota_mgr = AbOtaManager()
        self._audit = AbAudit(audit_service)
        logger.info("[ab] enterprise ops initialized: config_version, rbac, audit, ota")

    def check_rbac(self, role: str, permission: str, device_id: str = "") -> bool:
        try:
            from edgelite.security.rbac import Permission, has_permission
            perm = Permission(permission)
        except (ImportError, ValueError):
            return False
        granted = has_permission(role, perm)
        if self._audit:
            self._audit.log_rbac_check(device_id, permission, role, granted)
        if not granted:
            self._log_error(device_id, "ERR_AB_RBAC_DENIED", f"role={role} permission={permission}")
        return granted

    async def set_user_role(self, role: str) -> None:  # FIXED-P0: 改为async并使用_role_lock，防止多协程并发写入 _current_user_role 导致权限竞态（对齐 modbus_tcp/modbus_rtu/mc 驱动）
        async with self._role_lock:
            self._current_user_role = role

    async def save_config_version(self, device_id: str, config: dict, change_summary: str = "", operator: str = "system") -> int:
        if not self._config_version_mgr:
            return 0
        if not self.check_rbac(operator, "config:edit", device_id):
            self._log_error(device_id, "ERR_AB_CONFIG_CHANGE_DENIED", f"operator={operator}")
            return 0
        version = await self._config_version_mgr.save_version(device_id, config, change_summary, operator)
        if self._audit:
            self._audit.log_config_version(device_id, "save", to_version=version, operator=operator)
        self._log_error(device_id, "ERR_AB_CONFIG_VERSION_SAVED", f"version={version} operator={operator}")
        return version

    async def get_config_current(self, device_id: str) -> dict | None:
        if not self._config_version_mgr:
            return None
        return await self._config_version_mgr.get_current(device_id)

    async def get_config_versions(self, device_id: str, limit: int = 50) -> list[dict]:
        if not self._config_version_mgr:
            return []
        return await self._config_version_mgr.get_versions(device_id, limit)

    async def get_config_version_config(self, device_id: str, version: int) -> dict | None:
        if not self._config_version_mgr:
            return None
        return await self._config_version_mgr.get_version_config(device_id, version)

    async def rollback_config(self, device_id: str, target_version: int, operator: str = "system") -> dict | None:
        if not self._config_version_mgr:
            return None
        if not self.check_rbac(operator, "config_version:edit", device_id):
            self._log_error(device_id, "ERR_AB_CONFIG_CHANGE_DENIED", f"rollback denied for operator={operator}")
            return None
        config = await self._config_version_mgr.rollback(device_id, target_version, operator)
        if config and self._audit:
            self._audit.log_config_version(device_id, "rollback", from_version=0, to_version=target_version, operator=operator)
        self._log_error(device_id, "ERR_AB_CONFIG_VERSION_ROLLBACK", f"to_version={target_version} operator={operator}")
        return config

    async def get_config_audit_trail(self, device_id: str, limit: int = 100) -> list[dict]:
        if not self._config_version_mgr:
            return []
        return await self._config_version_mgr.get_audit_trail(device_id, limit)

    async def diff_config_versions(self, device_id: str, version_a: int, version_b: int) -> dict:  # FIXED-P1: 改为async并await，与FINS驱动一致
        if not self._config_version_mgr:
            return {"changes": []}
        return await self._config_version_mgr.diff_versions(device_id, version_a, version_b)

    def ota_check_update(self, package) -> bool:
        if not self._ota_mgr:
            return False
        result = self._ota_mgr.check_update(package)
        self._log_error("global", "ERR_AB_OTA_CHECK", f"available={result} target={package.version}")
        return result

    async def ota_start(self, package, config_snapshot: dict | None = None) -> bool:
        if not self._ota_mgr:
            return False
        if not self.check_rbac(self._current_user_role, "ota:manage"):
            self._log_error("global", "ERR_AB_OTA_FAILED", "RBAC denied for OTA")
            return False
        if self._audit:
            self._audit.log_ota("global", "ota_start", package.version, self._current_user_role)
        self._log_error("global", "ERR_AB_OTA_STARTED", f"target={package.version}")
        result = self._ota_mgr.start_ota(package, config_snapshot or self._config)
        if result:
            self._log_error("global", "ERR_AB_OTA_COMPLETED", f"version={package.version}")
            if self._audit:
                self._audit.log_ota("global", "ota_completed", package.version, self._current_user_role)
        else:
            self._log_error("global", "ERR_AB_OTA_FAILED", f"version={package.version}")
            if self._audit:
                self._audit.log_ota("global", "ota_failed", package.version, self._current_user_role)
        return result

    def ota_rollback(self) -> bool:
        if not self._ota_mgr:
            return False
        result = self._ota_mgr.rollback_ota()
        self._log_error("global", "ERR_AB_OTA_ROLLBACK", f"success={result}")
        if self._audit:
            self._audit.log_ota("global", "ota_rollback", "", self._current_user_role)
        return result

    def ota_get_progress(self) -> dict:
        if not self._ota_mgr:
            return {}
        return self._ota_mgr.get_progress()

    def ota_get_history(self, limit: int = 50) -> list[dict]:
        if not self._ota_mgr:
            return []
        return self._ota_mgr.get_history(limit)

    def get_audit_recent(self, limit: int = 100) -> list[dict]:
        if not self._audit:
            return []
        return self._audit.get_recent(limit)

    def get_audit_by_device(self, device_id: str, limit: int = 100) -> list[dict]:
        if not self._audit:
            return []
        return self._audit.get_by_device(device_id, limit)

    def get_audit_by_action(self, action: str, limit: int = 100) -> list[dict]:
        if not self._audit:
            return []
        return self._audit.get_by_action(action, limit)

    def export_audit_csv(self, start_time: str = "", end_time: str = "") -> str:
        if not self._audit:
            return ""
        return self._audit.export_csv(start_time, end_time)

    def get_audit_stats(self) -> dict:
        if not self._audit:
            return {}
        return self._audit.get_stats()
