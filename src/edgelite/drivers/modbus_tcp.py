"""Modbus TCP驱动 - 基于pymodbus实现"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import random
import struct
import time
from collections import OrderedDict, deque
from typing import Any

import pymodbus
from pymodbus.client import AsyncModbusTcpClient

try:
    from pymodbus.exceptions import ModbusException
except ImportError:
    ModbusException = Exception

from datetime import UTC, datetime, timedelta

from edgelite.api.debug import record_packet
from edgelite.api.error_codes import ModbusDriverErrors
from edgelite.constants import _DEVICE_CONNECT_TIMEOUT
from edgelite.drivers.base import ConnectionState, DriverCapabilities, DriverPlugin, PointValue
from edgelite.drivers.edge_rule_engine import (
    EdgeRule,
    EdgeRuleOperator,
    EdgeRuleType,
    ModbusEdgeRuleEngine,
)
from edgelite.drivers.edge_triggers import EdgeTriggerExecutor
from edgelite.drivers.modbus_audit import ModbusAudit, ModbusAuditAction

# FIXED-P2 (Task #17): 共享常量与函数统一从 modbus_base 导入，消除 TCP/RTU 重复定义，
# 确保 `modbus_tcp.X is modbus_base.X` 同一性检查通过。
from edgelite.drivers.modbus_base import (
    _BYTE_ORDER_FMT,
    _MODBUS_EXCEPTION_CODES,
    DATA_TYPE_REGS,
    REGISTER_TYPES,
    _detect_slave_kwarg_name,
    _parse_modbus_exception,
    _set_client_slave_id,
)
from edgelite.drivers.modbus_base import (
    _read_kwargs as _read_kwargs_base,
)
from edgelite.drivers.modbus_base import (
    _slave_kwarg as _slave_kwarg_base,
)
from edgelite.drivers.modbus_config_version import ModbusConfigVersion
from edgelite.drivers.modbus_ts_store import ModbusTsStore
from edgelite.drivers.offline_sync import OfflineSyncManager
from edgelite.drivers.redundancy import LinkRedundancyManager, LinkRole, RedundancyConfig
from edgelite.drivers.rule_store import RuleStore
from edgelite.engine.event_bus import DeviceStatusEvent, EventBus, PointUpdateEvent
from edgelite.security.rbac import Permission, has_permission  # FIXED-P1: 写入权限检查所需
from edgelite.services.i18n import t as _t

_PYMODBUS_MAJOR = int(getattr(pymodbus, "__version__", "2.0.0").split(".")[0])
_PYMODBUS_MINOR = int(getattr(pymodbus, "__version__", "2.0.0").split(".")[1]) if _PYMODBUS_MAJOR >= 3 else 0
_PYMODBUS_37_PLUS = _PYMODBUS_MAJOR > 3 or (_PYMODBUS_MAJOR == 3 and _PYMODBUS_MINOR >= 7)

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT = 5
READ_TIMEOUT = 30
WRITE_TIMEOUT = 10

# MTCP-LOW-001: watchdog 断连检测阈值（默认 30 秒）
_WATCHDOG_DISCONNECT_THRESHOLD = 30  # 失败次数阈值，乘以检测间隔(10s)即为实际时间

# MTCP-007: 单点读取重试配置：10秒总超时，指数退避1s, 2s, 4s）
_SINGLE_POINT_RETRY_TIMEOUT = 10
_SINGLE_POINT_RETRY_DELAYS = (1, 2, 4)

# MTCP-009: _last_values 最大容量（LRU 淘汰）
_MAX_LAST_VALUES = 10000

# _SLAVE_KWARG_NAME / _detect_slave_kwarg_name / _set_client_slave_id 由 modbus_base 维护，
# TCP 不再持有本地副本（FIXED-P2 Task #17），消除 TCP/RTU 间重复定义与版本检测漂移。


def _slave_kwarg(slave_id: int) -> dict:
    """返回正确的 Modbus 设备 ID 参数（TCP 模式：允许 slave_id=0 广播写）

    所有 pymodbus 版本均 per-call 传递 slave 参数，避免共享连接 slave_id 竞态。
    TCP 模式允许 slave_id=0（广播地址），其余值需 1-247。
    """
    return _slave_kwarg_base(slave_id, allow_broadcast=True)


def _read_kwargs(count: int, slave_id: int) -> dict:
    """返回正确的读取方法关键字参数（TCP 模式：允许 slave_id=0 广播读）"""
    return _read_kwargs_base(count, slave_id, allow_broadcast=True)


_EXCEPTION_ERROR_CODE_MAP: dict[type, str] = {
    ModbusException: ModbusDriverErrors.READ_EXCEPTION,
    TimeoutError: ModbusDriverErrors.READ_TIMEOUT,
}


def _resolve_error_code(exc: Exception) -> str:
    for exc_type, code in _EXCEPTION_ERROR_CODE_MAP.items():
        if isinstance(exc, exc_type):
            return code
    return ModbusDriverErrors.READ_FAILED


def _bad_pv(error_code: str) -> PointValue:
    return PointValue(value=None, quality="bad", timestamp=datetime.now(UTC), source=f"error:{error_code}")


class ModbusTcpDriver(DriverPlugin):
    """Modbus TCP协议驱动"""

    plugin_name = "modbus_tcp"
    plugin_version = "0.1.0"
    supported_protocols = ("modbus_tcp", "modbus-tcp")  # FIXED(P2): 原问题-可变默认值list; 修复-改为tuple
    _required_dependencies: tuple[str, ...] = ("pymodbus",)  # FIXED(P2): 原问题-可变默认值list; 修复-改为tuple
    _MAX_RECONNECT_ATTEMPTS = 10  # FIXED-P1: 重试提升到10，工业现场网络抖动常见
    _MAX_QUALITY_HISTORY = 100
    _MAX_LATENCY_HISTORY = 1000

    config_schema = {
        "description": "Modbus TCP industrial standard protocol for reading/writing PLC/instrument coils and registers",
        "required": ["host", "port", "slave_id"],
        "properties": {
            "host": {"type": "string", "description": "PLC/gateway IP address", "format": "ipv4"},
            "host_backup": {"type": "string", "description": "Backup IP address for link redundancy", "format": "ipv4"},
            "port": {"type": "integer", "description": "Modbus TCP port", "minimum": 1, "maximum": 65535},
            "slave_id": {
                "type": "integer",
                "description": "Device slave address (Unit ID)",
                "minimum": 1,
                "maximum": 247,
            },
            "timeout": {
                "type": "number",
                "description": "Connection and read timeout",
                "minimum": 0.1,
                "maximum": 60,
                "default": 3.0,
            },
            "byte_order": {
                "type": "string",
                "description": "Multi-register byte order",
                "enum": ["ABCD", "BADC", "CDAB", "DCBA"],
                "default": "ABCD",
            },
            "reconnect_interval": {
                "type": "number",
                "description": "Seconds between reconnection attempts",
                "minimum": 1,
                "maximum": 300,
                "default": 10.0,
            },
            "max_reconnect_attempts": {
                "type": "integer",
                "description": "Maximum consecutive reconnection attempts",
                "minimum": 1,
                "maximum": 100,
                "default": 10,
            },
            "batch_read_size": {
                "type": "integer",
                "description": "Maximum registers per read request",
                "minimum": 1,
                "maximum": 125,
                "default": 125,
            },
            "function_code": {
                "type": "string",
                "description": "Default Modbus function code",
                "enum": ["01", "02", "03", "04", "05", "06", "15", "16"],
                "default": "03",
            },
            "broadcast": {
                "type": "boolean",
                "description": "Allow writing to slave_id=0 (broadcast)",
                "default": False,
            },
            "deadband": {"type": "number", "description": "Deadband filter threshold", "minimum": 0},
            "deadband_type": {
                "type": "string",
                "description": "Deadband type: absolute or percent",
                "enum": ["absolute", "percent"],
            },
            "scaling": {
                "type": "object",
                "description": "Linear scaling transformation",
                "properties": {
                    "ratio": {"type": "number", "default": 1.0},
                    "offset": {"type": "number", "default": 0.0},
                },
            },
            "clamp": {
                "type": "object",
                "description": "Value range validation",
                "properties": {"min": {"type": "number"}, "max": {"type": "number"}},
            },
            "max_retry_interval": {
                "type": "integer",
                "description": "Maximum retry backoff interval in seconds",
                "minimum": 1,
                "maximum": 300,
                "default": 60,
            },
            "jitter_enable": {
                "type": "boolean",
                "description": "Enable jitter on retry backoff to prevent thundering herd",
                "default": True,
            },
            "rate_of_change_threshold": {
                "type": "number",
                "description": "Rate of change threshold for data credibility check",
                "minimum": 0,
            },
            "frozen_threshold": {
                "type": "integer",
                "description": "Consecutive identical readings to detect frozen value",
                "minimum": 1,
                "maximum": 1000,
                "default": 10,
            },
            "write_verify": {"type": "boolean", "description": "Enable read-verify-write", "default": False},
            "write_rate_limit": {
                "type": "number",
                "description": "Minimum interval between writes to same register (seconds)",
                "minimum": 0.1,
                "maximum": 60,
                "default": 1.0,
            },
            "write_audit": {"type": "boolean", "description": "Enable write operation audit logging", "default": True},
        },
        "fields": [
            {
                "name": "host",
                "type": "string",
                "label": "IP Address",
                "description": "PLC or gateway IP address",
                "default": "",
                "required": True,
            },  # FIXED-P1: 默认IP改为空
            {
                "name": "host_backup",
                "type": "string",
                "label": "Backup IP",
                "description": "Backup IP for link redundancy, auto-switch after 3 primary failures",
                "default": None,
            },
            {
                "name": "port",
                "type": "integer",
                "label": "Port",
                "description": "Modbus TCP port, default 502",
                "default": 502,
                "required": True,
            },
            {
                "name": "slave_id",
                "type": "integer",
                "label": "Slave ID",
                "description": "Device slave address (Unit ID), usually 1",
                "default": 1,
                "required": True,
            },
            {
                "name": "timeout",
                "type": "number",
                "label": "Timeout (s)",
                "description": "Connection and read timeout",
                "default": 3.0,
            },
            {
                "name": "byte_order",
                "type": "string",
                "label": "Byte Order",
                "description": "Multi-register byte order: ABCD(Big-Endian), BADC, CDAB, DCBA(Little-Endian)",
                "default": "ABCD",
                "options": ["ABCD", "BADC", "CDAB", "DCBA"],
            },
            {
                "name": "reconnect_interval",
                "type": "number",
                "label": "Reconnect Interval (s)",
                "description": "Seconds between reconnection attempts",
                "default": 10.0,
            },
            {
                "name": "max_reconnect_attempts",
                "type": "integer",
                "label": "Max Reconnect Attempts",
                "description": "Maximum consecutive reconnection attempts (default 10)",
                "default": 10,
            },
            {
                "name": "max_retry_interval",
                "type": "integer",
                "label": "Max Retry Interval (s)",
                "description": "Maximum retry backoff interval in seconds (1-300)",
                "default": 60,
                "min": 1,
                "max": 300,
            },
            {
                "name": "jitter_enable",
                "type": "boolean",
                "label": "Enable Jitter",
                "description": "Enable jitter on retry backoff to prevent thundering herd",
                "default": True,
            },
            {
                "name": "batch_read_size",
                "type": "integer",
                "label": "Batch Read Size",
                "description": "Maximum registers per read request (1-125)",
                "default": 125,
                "min": 1,
                "max": 125,
            },
            {
                "name": "function_code",
                "type": "string",
                "label": "Function Code",
                "description": "Default Modbus function code",
                "default": "03",
                "options": ["01", "02", "03", "04", "05", "06", "15", "16"],
            },
            {
                "name": "broadcast",
                "type": "boolean",
                "label": "Enable Broadcast Write",
                "description": "Allow writing to slave_id=0 (broadcast address). Note: broadcast writes have no response and cannot be verified",
                "default": False,
            },
            {
                "name": "rate_of_change_threshold",
                "type": "number",
                "label": "Rate of Change Threshold",
                "description": "Rate of change threshold for data credibility, mark quality=uncertain when exceeded",
                "default": None,
            },
            {
                "name": "frozen_threshold",
                "type": "integer",
                "label": "Frozen Detection Count",
                "description": "Consecutive identical readings to detect frozen value (1-1000)",
                "default": 10,
                "min": 1,
                "max": 1000,
            },
            {
                "name": "watchdog_threshold",
                "type": "integer",
                "label": "Watchdog Threshold",
                "description": "Watchdog disconnect detection threshold in seconds (detection_interval * fail_count >= threshold), default 30s",
                "default": 30,
                "min": 10,
                "max": 300,
            },
            {
                "name": "write_verify",
                "type": "boolean",
                "label": "Write Verify",
                "description": "Enable read-verify-write: read back after write and compare",
                "default": False,
            },
            {
                "name": "write_rate_limit",
                "type": "number",
                "label": "Write Rate Limit (s)",
                "description": "Minimum interval between writes to same register (seconds)",
                "default": 1.0,
            },
            {
                "name": "write_audit",
                "type": "boolean",
                "label": "Write Audit",
                "description": "Enable write operation audit logging",
                "default": True,
            },
            {
                "name": "deadband",
                "type": "number",
                "label": "Deadband",
                "description": "Deadband filter threshold, suppress updates when change < deadband",
                "default": None,
            },
            {
                "name": "scaling",
                "type": "object",
                "label": "Scaling",
                "description": "Linear scaling: y = x * ratio + offset",
                "default": None,
                "fields": [
                    {
                        "name": "ratio",
                        "type": "number",
                        "label": "Ratio",
                        "description": "Scaling ratio (multiplier)",
                        "default": 1.0,
                    },
                    {
                        "name": "offset",
                        "type": "number",
                        "label": "Offset",
                        "description": "Scaling offset (addend)",
                        "default": 0.0,
                    },
                ],
            },
            {
                "name": "clamp",
                "type": "object",
                "label": "Clamp",
                "description": "Value range validation, mark quality=bad when out of range",
                "default": None,
                "fields": [
                    {"name": "min", "type": "number", "label": "Min", "description": "Minimum allowed value"},
                    {"name": "max", "type": "number", "label": "Max", "description": "Maximum allowed value"},
                ],
            },
        ],
    }

    experimental = False
    capabilities = DriverCapabilities(
        discover=True, read=True, write=True, subscribe=False, batch_read=True, batch_write=True
    )
    constraints = ()  # FIXED(P2): 原问题-可变默认值list; 修复-改为tuple

    def __init__(self):
        super().__init__()  # FIXED-P0: 基类属性未初始化
        self._running = False
        self._clients: dict[str, AsyncModbusTcpClient] = {}
        self._connection_pool: dict[str, tuple[AsyncModbusTcpClient, int]] = {}
        self._pool_lock = asyncio.Lock()  # FIXED-P2: 连接池引用计数操作加锁防竞态
        self._stale_clients: dict[AsyncModbusTcpClient, float] = {}  # MTCP-002/MTCP-MED-002: 过期client标记及添加时间
        self._STALE_CLIENT_TIMEOUT = 300.0  # MTCP-MED-002: stale client 超过5分钟强制关闭
        self._stale_cleanup_task: asyncio.Task | None = None  # FIXED-P1: MTCP-01 stale client定期清理任务
        # MTCP-MED-001: 连接租用机制，防止use-after-close
        self._leased_clients: set[AsyncModbusTcpClient] = set()  # 当前正在使用的client
        self._lease_lock = asyncio.Lock()  # 保护租用操作
        self._device_pool_key: dict[str, str] = {}
        self._device_configs: dict[str, dict] = {}
        self._device_points: dict[str, list[dict]] = {}
        self._retry_count: dict[str, int] = {}
        self._retry_lock = asyncio.Lock()
        self._read_fail_tracker: OrderedDict[tuple[str, str], tuple[float, float]] = (
            OrderedDict()
        )  # FIXED-P1: 改为OrderedDict支持LRU淘汰
        self._MAX_TRACKER_ENTRIES = 20000  # FIXED-P1: 容量上限，与RTU驱动一致
        self._circuit_open: set[str] = set()  # FIXED-P1: 熔断状态设备集合，阻止无限递归重连
        # FIXED-P2: 删除遮蔽基类_health_stats的初始化（super().__init__()已正确初始化为dict[str, DriverHealthStats]）
        # _offline_since inherited from base class (dict[str, datetime])  # FIXED-P2
        self._watchdog_fail_count: dict[str, int] = {}
        self._device_watchdog_tasks: dict[str, asyncio.Task] = {}  # MTCP-003: 每个设备独立的watchdog task
        self._reconnect_tasks: dict[str, asyncio.Task] = {}  # FIXED-P0: 非阻塞重连任务追踪
        # MTCP-007: 单点读取缓存和后台重试
        self._single_point_cache: OrderedDict[tuple[str, str], Any] = (
            OrderedDict()
        )  # FIXED-P2: 改为OrderedDict支持LRU淘汰
        self._SINGLE_POINT_CACHE_MAX = 10000  # FIXED-P2: 单点缓存容量上限
        self._point_retry_tasks: dict[tuple[str, str], asyncio.Task] = {}  # {(device_id, point_name): retry_task}
        # MTCP-009: 使用 OrderedDict 实现 LRU，超过容量时淘汰最旧条目
        self._last_values: OrderedDict[tuple[str, str], float] = OrderedDict()
        self._pool_backoff: dict[str, int] = {}
        self._conn_state: dict[str, str] = {}
        self._primary_fail_count: dict[str, int] = {}
        self._active_host: dict[str, str] = {}
        self._point_health: OrderedDict[tuple[str, str], dict] = OrderedDict()  # FIXED-P1: 使用OrderedDict实现LRU淘汰
        self._point_quality_history: dict[tuple[str, str], deque[str]] = {}
        self._device_latency_history: dict[str, deque[tuple[float, float]]] = {}
        self._degrade_level: dict[str, int] = {}
        self._frozen_count: dict[tuple[str, str], int] = {}
        self._last_timestamp: dict[tuple[str, str], float] = {}
        self._last_write_time: dict[tuple[str, str], float] = {}
        self._device_point_keys: dict[str, set[str]] = {}
        self._write_audit_log: deque[dict] = deque(maxlen=1000)
        self._event_bus: EventBus | None = None
        self._redundancy_mgr: LinkRedundancyManager | None = None
        self._edge_rule_engine: ModbusEdgeRuleEngine | None = None
        self._edge_trigger: EdgeTriggerExecutor | None = None
        self._rule_store: RuleStore | None = None
        self._ts_store: ModbusTsStore | None = None
        self._offline_sync: OfflineSyncManager | None = None
        self._config_version: ModbusConfigVersion | None = None
        self._audit: ModbusAudit | None = None
        self._current_user_role: str = "viewer"  # FIXED-P2: 默认角色改为viewer，遵循最小权限原则
        self._role_lock = asyncio.Lock()  # FIXED-P1: 角色读写锁

    async def start(self, config: dict) -> None:
        """启动驱动（config为全局配置，实际连接在add_device时建立）"""
        self._running = True
        self._event_bus = config.get("_event_bus") if isinstance(config.get("_event_bus"), EventBus) else None
        self._redundancy_mgr = LinkRedundancyManager(event_bus=self._event_bus)
        self._edge_rule_engine = ModbusEdgeRuleEngine(event_bus=self._event_bus)
        self._edge_trigger = EdgeTriggerExecutor(
            device_write_callback=self._async_write_point,
        )
        self._edge_rule_engine.set_on_action_callback(self._edge_trigger.execute)
        self._rule_store = RuleStore()
        for rule in self._rule_store.load_rules():
            self._edge_rule_engine.add_rule(rule)
        ts_retention = config.get("ts_retention_days", 7)
        self._ts_store = ModbusTsStore(retention_days=ts_retention)
        await self._ts_store.start()
        self._offline_sync = OfflineSyncManager(
            ts_store=self._ts_store,
            sync_interval=config.get("offline_sync_interval", 30.0),
            batch_size=config.get("offline_sync_batch", 1000),
            compress=config.get("offline_compress", "gzip"),
        )
        await self._offline_sync.start()
        self._config_version = ModbusConfigVersion()
        self._audit = ModbusAudit()
        # FIXED-P1: MTCP-01 启动stale client定期清理任务
        self._stale_cleanup_task = asyncio.create_task(
            self._stale_client_cleanup_loop(), name="modbus-tcp-stale-cleanup"
        )
        logger.info("Modbus TCP驱动启动")

    async def stop(self) -> None:
        """停止驱动，关闭所有连接"""
        try:
            async with self._pool_lock:
                for pool_key, (client, _ref_count) in list(self._connection_pool.items()):
                    try:
                        if client.connected:
                            # FIXED-P2: stop()不调用_can_close_client，直接关闭避免锁反转
                            client.close()
                            logger.info("Modbus pool connection closed: %s", pool_key)
                    except Exception as e:
                        logger.warning("Modbus pool connection close failed: %s - %s", pool_key, e)
        finally:
            # FIXED-P1: MTCP-01 取消stale client清理任务
            if self._stale_cleanup_task and not self._stale_cleanup_task.done():
                self._stale_cleanup_task.cancel()
            self._stale_cleanup_task = None
            # MTCP-003: 取消所有设备的watchdog tasks
            for task in list(self._device_watchdog_tasks.values()):
                if not task.done():
                    task.cancel()
            self._device_watchdog_tasks.clear()
            self._watchdog_fail_count.clear()
            # FIXED-P0: 取消所有重连任务，防止stop后仍有后台重连任务运行
            for _did, _rtask in list(self._reconnect_tasks.items()):
                if _rtask and not _rtask.done():
                    _rtask.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await _rtask
            self._reconnect_tasks.clear()
            # MTCP-007: 取消所有单点重试任务
            for task in list(self._point_retry_tasks.values()):
                if not task.done():
                    task.cancel()
            self._point_retry_tasks.clear()
            # CROSS-004: 取消所有后台任务
            await self._cancel_background_tasks()
            self._single_point_cache.clear()
            self._running = False
            self._clients.clear()
            self._connection_pool.clear()
            self._stale_clients.clear()
            self._device_pool_key.clear()
            self._pool_backoff.clear()
            self._conn_state.clear()
            self._primary_fail_count.clear()
            self._active_host.clear()
            self._point_health.clear()
            self._point_quality_history.clear()
            self._device_latency_history.clear()
            self._degrade_level.clear()
            self._frozen_count.clear()
            self._last_timestamp.clear()
            self._last_write_time.clear()
            self._device_point_keys.clear()
            self._write_audit_log.clear()
            if self._redundancy_mgr:
                self._redundancy_mgr.stop()
                self._redundancy_mgr = None
            if self._edge_trigger:
                await self._edge_trigger.stop()  # #[AUDIT-FIX] stop() is async, must await (was no-op coroutine)
                self._edge_trigger = None
            if self._rule_store:
                self._rule_store.stop()
                self._rule_store = None
            if self._offline_sync:
                await self._offline_sync.stop()
                self._offline_sync = None
            if self._ts_store:
                await self._ts_store.stop()
                self._ts_store = None
            self._edge_rule_engine = None
            self._device_configs.clear()  # FIXED-P2: stop()清理状态残留
            self._device_points.clear()
            self._retry_count.clear()
            with self._stats_lock:  # FIXED-P0: _health_stats/_offline_since清理加锁保护，与基类读写路径一致
                self._health_stats.clear()
                self._offline_since.clear()
            self._watchdog_fail_count.clear()
            logger.info("Modbus TCP驱动停止")
            # FIXED-P1: MTCP-R01 调用基类stop()确保_shutdown_executor和_cancel_background_tasks执行
            await super().stop()

    async def add_device(self, device_id: str, config: dict, points: list[dict]) -> None:
        """添加设备并建立连接（相同host:port共享连接池"""
        slave_id = config.get("slave_id", 1)
        broadcast_enabled = config.get("broadcast", False)
        if not broadcast_enabled and not (1 <= slave_id <= 247):
            logger.error(
                "[modbus_tcp] device=%s code=%s slave_id=%d out of range [1-247]",
                device_id,
                ModbusDriverErrors.CONFIG_INVALID,
                slave_id,
            )
            raise ValueError(f"Modbus TCP config invalid: slave_id must be 1-247, got {slave_id}")
        if broadcast_enabled and slave_id == 0:
            logger.warning("[modbus_tcp] device=%s code=BROADCAST_ENABLED slave_id=0 broadcast mode", device_id)

        self._device_configs[device_id] = config
        self._device_points[device_id] = points
        point_names = {p.get("name") for p in points if p.get("name")}
        self._device_point_keys[device_id] = point_names
        if self._config_version:
            self._config_version.snapshot_device_config(device_id, config)
        if self._audit:
            await self._audit.log_config_change(device_id, list(config.keys()), {}, config)

        host = config.get("host", "127.0.0.1")
        port = config.get("port", 502)
        timeout = config.get("timeout", 5.0)
        pool_key = f"{host}:{port}"
        self._active_host[device_id] = host
        self._primary_fail_count[device_id] = 0
        self._conn_state.setdefault(device_id, ConnectionState.DISCONNECTED.value)

        if self._redundancy_mgr:
            backup_host = config.get("host_backup", "")
            redundancy_cfg = RedundancyConfig(
                primary_host=host,
                primary_port=port,
                backup_host=backup_host,
                backup_port=config.get("port_backup", port),
                failover_threshold=config.get("failover_threshold", 3),
                recovery_probe_interval=config.get("recovery_probe_interval", 30.0),
                auto_revert=config.get("auto_revert", True),
            )
            self._redundancy_mgr.register_device(device_id, redundancy_cfg)

        async with self._pool_lock:
            if pool_key in self._connection_pool:
                client, ref_count = self._connection_pool[pool_key]
                # FIXED-Bug1: 复用前校验 client 连通性，防止绑定到已断开的 client
                if client.connected:
                    self._connection_pool[pool_key] = (client, ref_count + 1)
                    self._clients[device_id] = client
                    self._device_pool_key[device_id] = pool_key
                    logger.info(
                        "Modbus TCP reused pooled connection: %s (%s:%d, ref_count=%d)",
                        device_id,
                        host,
                        port,
                        ref_count + 1,
                    )
                    self._retry_count[device_id] = 0
                    self._start_watchdog(device_id)
                    return
                # 池中 client 已断开，移除池引用并走新建连接分支
                self._connection_pool.pop(pool_key, None)

        # FIXED-P2: 连接创建和connect()在锁外执行，避免阻塞其他设备的pool操作
        client = AsyncModbusTcpClient(
            host=host,
            port=port,
            timeout=config.get("timeout", CONNECT_TIMEOUT),
        )

        connect_error = None
        try:
            connected = await asyncio.wait_for(client.connect(), timeout=timeout)
        except TimeoutError:
            connected = False
            connect_error = f"connect timeout ({timeout}s)"
        except Exception as e:
            connected = False
            connect_error = str(e)
            logger.warning(
                "[modbus_tcp] connection failed: device=%s host=%s port=%d error=%s",
                device_id,
                host,
                port,
                e,
                exc_info=True,
            )

        # FIXED-P0: 修复连接池TOCTOU竞态和引用计数虚高 — 二次检查：连接创建期间另一个协程可能已添加同pool_key
        # 仅在连接成功时递增引用计数，避免连接失败导致引用计数虚高
        reused_existing = False
        async with self._pool_lock:
            if pool_key in self._connection_pool:
                existing_client, ref_count = self._connection_pool[pool_key]
                if connected:
                    self._connection_pool[pool_key] = (existing_client, ref_count + 1)
                    self._clients[device_id] = existing_client
                    self._device_pool_key[device_id] = pool_key
                    reused_existing = True
                else:
                    # FIXED-P2: 连接失败时不绑定到已有连接，防止设备绑定到已断开的client
                    # 之前：连接失败时仍将设备绑定到existing_client，但未递增引用计数
                    # 之后：连接失败时不绑定，让设备保持未连接状态等待重连
                    self._device_pool_key[device_id] = pool_key
                    reused_existing = True
                try:
                    client.close()
                except Exception as e:
                    logger.warning("[modbus_tcp] operation failed: %s", e)
            else:
                self._clients[device_id] = client
                self._connection_pool[pool_key] = (client, 1)
                self._device_pool_key[device_id] = pool_key

        if connected:
            logger.info("Modbus TCP connected: %s (%s:%d)", device_id, host, port)
            self._retry_count[device_id] = 0
            self._circuit_open.discard(device_id)  # FIXED-P1: 首次连接成功，解除熔断
            self._pool_backoff[pool_key] = 0
            self._transition_state(device_id, ConnectionState.CONNECTED.value, f"connected to {host}:{port}")
            self._start_watchdog(device_id)
        else:
            self._transition_state(device_id, ConnectionState.DISCONNECTED.value, f"connect failed to {host}:{port}")
            # FIXED-P0: 区分"复用已有连接但设备连接失败"和"新建连接失败"，前者不应关闭池中健康连接
            if reused_existing:
                # 复用已有连接分支：仅从_clients移除，不关闭池中连接（连接本身是健康的）
                async with self._pool_lock:
                    self._clients.pop(device_id, None)
                    self._device_pool_key.pop(device_id, None)
            else:
                # 新建连接分支：从pool中移除并关闭client
                async with self._pool_lock:
                    failed_client = self._clients.pop(device_id, None)
                    pk = self._device_pool_key.pop(device_id, None)
                    if pk and pk in self._connection_pool:
                        c, ref = self._connection_pool[pk]
                        if c is failed_client:
                            if ref > 1:
                                self._connection_pool[pk] = (c, ref - 1)
                            else:
                                self._connection_pool.pop(pk)
                        else:
                            if ref > 1:
                                self._connection_pool[pk] = (c, ref - 1)
                            else:
                                self._connection_pool.pop(pk)
                if failed_client:
                    try:
                        failed_client.close()
                    except Exception as e:
                        logger.warning(
                            "[modbus_tcp] operation failed: %s", e
                        )  # FIXED-P2: 原问题：异常被静默吞没，添加日志记录
            if connect_error:
                self._log_error(device_id, ModbusDriverErrors.CONN_FAILED, connect_error)
            else:
                self._log_error(device_id, ModbusDriverErrors.CONN_FAILED, f"connect failed to {host}:{port}")

    async def remove_device(self, device_id: str) -> None:
        """移除设备（引用计数-1，最后一个设备移除时才关闭连接）"""
        self._stop_watchdog(device_id)
        self._clients.pop(
            device_id, None
        )  # FIXED-P3: 原问题-client赋值后未使用(ruff F841); 修复-移除无用变量绑定，后续使用连接池中的pooled_client
        pool_key = self._device_pool_key.pop(device_id, None)

        # FIXED-P2: 与add_device() 一致，在_pool_lock 保护下操作引用计数
        async with self._pool_lock:
            if pool_key and pool_key in self._connection_pool:
                pooled_client, ref_count = self._connection_pool[pool_key]
                if ref_count <= 1:
                    # MTCP-MED-001: 检查client 是否正在被使用
                    # FIXED-P0: 修复连接池TOCTOU竞态和引用计数虚高 — connected检查和close()在锁内，并加try-except防已关闭
                    if pooled_client:
                        try:
                            if pooled_client.connected:
                                # FIXED-P1: 在_pool_lock内直接检查_leased_clients而非调用_can_close_client）
                                # 避免嵌套获取_lease_lock导致与_stale_client_cleanup_loop的ABBA死锁
                                if pooled_client not in self._leased_clients:
                                    pooled_client.close()
                                    logger.info(
                                        "Modbus TCP pool connection closed: %s (pool_key=%s)", device_id, pool_key
                                    )
                                else:
                                    # client 正在被使用，延迟关闭
                                    # MTCP-MED-002: 记录stale client的添加时间
                                    self._stale_clients[pooled_client] = time.monotonic()
                                    logger.warning(
                                        "Modbus TCP: client still in use, marked stale for deferred close: %s",
                                        device_id,
                                    )
                        except Exception:
                            logger.debug("Modbus TCP: pooled_client already closed or invalid: %s", device_id)
                    self._stale_clients.pop(pooled_client, None)  # MTCP-002/MTCP-MED-002: 清理过期标记
                    del self._connection_pool[pool_key]
                else:
                    self._connection_pool[pool_key] = (pooled_client, ref_count - 1)
                    # MTCP-002: 如果该client被标记为过期且这是最后一次引用，关闭）
                    if pooled_client in self._stale_clients and ref_count - 1 == 0:
                        # FIXED-P0: 修复连接池TOCTOU竞态和引用计数虚高 — 加try-except防已关闭
                        try:
                            if pooled_client.connected:
                                pooled_client.close()
                        except Exception:
                            logger.debug("Modbus TCP: stale client already closed: %s", device_id)
                        self._stale_clients.pop(pooled_client, None)
                        logger.info("Modbus TCP stale client closed after last reference released: %s", device_id)
                    logger.info(
                        "Modbus TCP pool connection kept: %s (pool_key=%s, ref_count=%d)",
                        device_id,
                        pool_key,
                        ref_count - 1,
                    )

        self._device_configs.pop(device_id, None)
        self._device_points.pop(device_id, None)
        self._retry_count.pop(device_id, None)
        self._circuit_open.discard(device_id)  # FIXED-P1: 清理熔断状态
        # FIXED-P1: _health_stats/_offline_since操作加_stats_lock，与_record_read_success/failure竞态保护一致
        with self._stats_lock:
            self._health_stats.pop(device_id, None)
            self._offline_since.pop(device_id, None)
        self._watchdog_fail_count.pop(device_id, None)
        self._conn_state.pop(device_id, None)
        self._primary_fail_count.pop(device_id, None)
        self._active_host.pop(device_id, None)
        if self._redundancy_mgr:
            self._redundancy_mgr.unregister_device(device_id)
        self._degrade_level.pop(device_id, None)
        point_names = self._device_point_keys.pop(device_id, set())
        # FIXED-P1: 清理_read_fail_tracker中该设备的相关条目
        keys_to_remove = [k for k in self._read_fail_tracker if k[0] == device_id]
        for k in keys_to_remove:
            self._read_fail_tracker.pop(k, None)
        for point_name in point_names:
            key = (device_id, point_name)
            self._last_values.pop(key, None)
            self._point_health.pop(key, None)
            self._frozen_count.pop(key, None)
            self._last_timestamp.pop(key, None)
            self._last_write_time.pop(key, None)
            self._point_quality_history.pop(key, None)
        self._device_latency_history.pop(device_id, None)

    # MTCP-MED-001: 连接租用机制，防止use-after-close

    async def _lease_client(self, client: AsyncModbusTcpClient) -> bool:
        """租用连接：标记client 为正在使用，不允许被关闭

        Returns:
            True 如果租用成功，False 如果 client 已被关闭
        """
        async with self._lease_lock:
            if not client.connected:
                return False
            self._leased_clients.add(client)
            return True

    async def _release_client(self, client: AsyncModbusTcpClient) -> None:
        """释放租用：标记client 不再被使用"""
        async with self._lease_lock:
            self._leased_clients.discard(client)

    def _is_client_leased(self, client: AsyncModbusTcpClient) -> bool:
        """检查client 是否正在被使用"""
        return client in self._leased_clients

    async def _can_close_client(self, client: AsyncModbusTcpClient) -> bool:
        """检查是否可以安全关闭client（无活跃租用）"""
        async with self._lease_lock:
            return client not in self._leased_clients

    async def _stale_client_cleanup_loop(self) -> None:
        """FIXED-P1: MTCP-01 定期清理超时的stale client，防止连接泄漏"""
        while self._running:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                return
            try:
                now = time.monotonic()
                expired = [c for c, t in self._stale_clients.items() if now - t > self._STALE_CLIENT_TIMEOUT]
                for client in expired:
                    async with self._lease_lock:  # FIXED-P1: 在lease_lock内完成关闭操作，防止关闭期间被另一协程租用
                        if client in self._leased_clients:
                            continue
                        try:
                            client.close()
                            logger.info(
                                "[modbus_tcp] stale client force-closed after %.0fs timeout", self._STALE_CLIENT_TIMEOUT
                            )
                        except Exception as e:
                            logger.warning(
                                "[modbus_tcp] stale_client_cleanup_loop failed: %s", e
                            )  # FIXED-P2: 原问题：异常被静默吞没，添加日志记录
                        self._stale_clients.pop(client, None)
                        # FIXED-P1: 从_clients中移除已关闭的stale client引用，移入lease_lock内与read_points的get+lease原子互斥
                        # 之前：_clients.pop在锁外执行，read_points可能在get与lease之间获取到已关闭client
                        stale_device_ids = [did for did, c in self._clients.items() if c is client]
                        for did in stale_device_ids:
                            self._clients.pop(did, None)
                        # FIXED-Bug3: 将 pool 引用移除移入 _lease_lock 内，消除两锁之间的 TOCTOU 窗口
                        # 配合 Bug1 修复中 add_device 检查 client.connected，杜绝 use-after-close
                        expired_keys = [k for k, (c, _) in self._connection_pool.items() if c is client]
                        for k in expired_keys:
                            self._connection_pool.pop(k, None)
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning("[modbus_tcp] stale client cleanup iteration failed: %s", e)

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        """读取测点值（支持批量合并读取和自动分包）"""
        # FIXED-P1: 在_lease_lock内获取client引用并租用，防止_stale_client_cleanup_loop在get与lease之间关闭client
        # 之前：client = self._clients.get(device_id) 无锁获取，cleanup_loop可能在get与_lease_client之间关闭client
        # 之后：get+lease在同一个_lease_lock内原子完成，cleanup_loop的close也在_lease_lock内，互斥
        async with self._lease_lock:
            client = self._clients.get(device_id)
            if client is None or not client.connected:
                client = None
            else:
                self._leased_clients.add(client)
        if client is None:
            state = self._conn_state.get(device_id, ConnectionState.DISCONNECTED.value)
            if state != ConnectionState.CONNECTING.value:
                self._transition_state(device_id, ConnectionState.DISCONNECTED.value, "client not connected")
            # FIXED-P0: 非阻塞调度重连，避免单个设备离线阻塞整个采集循环
            # 之前：await _try_reconnect()阻塞调用方，_retry_lock被长时间持有时所有读取协程阻塞
            # 之后：create_task非阻塞调度重连，读取立即返回bad quality
            if not self._reconnect_tasks.get(device_id) or self._reconnect_tasks[device_id].done():
                self._reconnect_tasks[device_id] = asyncio.create_task(self._try_reconnect(device_id))
            return {name: _bad_pv(ModbusDriverErrors.CONN_FAILED) for name in points}

        try:
            config = self._device_configs.get(device_id, {})
            slave_id = config.get("slave_id", 1)
            byte_order = config.get("byte_order", "ABCD")
            read_timeout = config.get("timeout", READ_TIMEOUT)
            device_points = self._device_points.get(device_id, [])

            pt_map: dict[str, dict] = {}
            for point_name in points:
                pt_def = next((p for p in device_points if p.get("name") == point_name), None)
                if pt_def is not None:
                    pt_map[point_name] = pt_def

            if not pt_map:
                return {}

            bit_points: dict[str, dict] = {}
            reg_points: dict[str, dict] = {}
            for name, pt_def in pt_map.items():
                reg_type = pt_def.get("register_type", "holding")
                if reg_type in ("coil", "discrete"):
                    bit_points[name] = pt_def
                else:
                    reg_points[name] = pt_def

            result: dict[str, Any] = {}

            def _check_and_reconnect() -> tuple[bool, AsyncModbusTcpClient | None]:
                """检查连通性，如断开则触发重连，返回(是否需要跳过, 当前client)"""
                current_client = self._clients.get(device_id)
                if current_client is None or not current_client.connected:
                    state = self._conn_state.get(device_id, ConnectionState.DISCONNECTED.value)
                    if state != ConnectionState.CONNECTING.value:
                        self._transition_state(
                            device_id, ConnectionState.DISCONNECTED.value, "connection lost before read"
                        )
                    # FIXED-Bug2: 去重检查，避免重复创建重连任务（与 read_points 开头行为一致）
                    existing = self._reconnect_tasks.get(device_id)
                    if existing is None or existing.done():
                        task = asyncio.ensure_future(self._try_reconnect(device_id))

                        def _log_reconnect_exc(t):
                            if not t.cancelled():
                                exc = t.exception()
                                if exc:
                                    logger.warning("[modbus_tcp] reconnect task failed: %s", exc)

                        task.add_done_callback(_log_reconnect_exc)
                        self._reconnect_tasks[device_id] = task
                        # FIXED-P1: 将重连Task纳入_background_tasks管理，stop()时可取消，防止孤立Task创建泄漏连接
                        self._background_tasks.add(task)
                        task.add_done_callback(self._background_tasks.discard)
                    return True, current_client
                return False, current_client

            for point_name, pt_def in bit_points.items():
                skip, current_client = _check_and_reconnect()
                # FIXED-P0: MT-03 重连后client引用可能变更，需释放旧租用并重新租用新client
                if not skip and current_client is not client:
                    await self._release_client(client)
                    client = current_client
                    if not await self._lease_client(client):
                        self._transition_state(
                            device_id, ConnectionState.DISCONNECTED.value, "client closed during re-lease"
                        )
                        await self._try_reconnect(device_id)
                        skip = True
                if skip:
                    result[point_name] = _bad_pv(ModbusDriverErrors.CONN_FAILED)
                    self._record_read_failure(device_id)
                    self._record_point_failure(device_id, point_name)
                    continue

                t0 = time.monotonic()
                try:
                    value = await asyncio.wait_for(
                        self._read_single_point(current_client, slave_id, pt_def, byte_order, device_id, point_name),
                        timeout=read_timeout,
                    )
                    value = self._apply_pipeline(value, pt_def, device_id, point_name)
                    result[point_name] = value
                    self._read_fail_tracker.pop((device_id, point_name), None)
                    await self._record_read_success(
                        device_id
                    )  # #[AUDIT-FIX] _record_read_success is async, must await (was no-op coroutine)
                    latency = (time.monotonic() - t0) * 1000
                    if not (isinstance(value, PointValue) and value.quality == "bad"):
                        self._record_point_success(device_id, point_name, latency)
                    else:
                        self._record_point_failure(device_id, point_name)
                except ModbusException as e:
                    self._record_read_failure(device_id)
                    self._record_point_failure(device_id, point_name)
                    cached = self._single_point_cache.get((device_id, point_name))
                    if cached is not None:
                        result[point_name] = PointValue(
                            value=cached, quality="uncertain", timestamp=datetime.now(UTC), source="cached"
                        )
                        logger.debug(
                            "[modbus_tcp] device=%s point=%s returning cached value (ModbusException)",
                            device_id,
                            point_name,
                        )
                    else:
                        self._log_throttled(device_id, point_name, e, ModbusDriverErrors.READ_EXCEPTION)
                        result[point_name] = _bad_pv(ModbusDriverErrors.READ_EXCEPTION)
                except TimeoutError:  # FIXED-P1: 兼容Python<3.11
                    self._record_read_failure(device_id)
                    self._record_point_failure(device_id, point_name)
                    cached = self._single_point_cache.get((device_id, point_name))
                    if cached is not None:
                        result[point_name] = PointValue(
                            value=cached, quality="uncertain", timestamp=datetime.now(UTC), source="cached"
                        )
                        logger.debug(
                            "[modbus_tcp] device=%s point=%s returning cached value (TimeoutError)",
                            device_id,
                            point_name,
                        )
                    else:
                        self._log_error(
                            device_id,
                            ModbusDriverErrors.READ_TIMEOUT,
                            f"Read timeout ({read_timeout}s) for {point_name}",
                        )
                        result[point_name] = _bad_pv(ModbusDriverErrors.READ_TIMEOUT)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self._record_read_failure(device_id)
                    self._record_point_failure(device_id, point_name)
                    logger.warning(
                        "[modbus_tcp] single point read failed: device=%s point=%s error=%s",
                        device_id,
                        point_name,
                        e,
                        exc_info=True,
                    )
                    cached = self._single_point_cache.get((device_id, point_name))
                    if cached is not None:
                        result[point_name] = PointValue(
                            value=cached, quality="uncertain", timestamp=datetime.now(UTC), source="cached"
                        )
                        logger.debug(
                            "[modbus_tcp] device=%s point=%s returning cached value (Exception)", device_id, point_name
                        )
                    else:
                        self._log_throttled(device_id, point_name, e, ModbusDriverErrors.READ_FAILED)
                        result[point_name] = _bad_pv(ModbusDriverErrors.READ_FAILED)

            if reg_points:
                skip, current_client = _check_and_reconnect()
                # FIXED-P0: MT-03 重连后client引用可能变更，需释放旧租用并重新租用新client
                if not skip and current_client is not client:
                    await self._release_client(client)
                    client = current_client
                    if not await self._lease_client(client):
                        self._transition_state(
                            device_id, ConnectionState.DISCONNECTED.value, "client closed during re-lease"
                        )
                        await self._try_reconnect(device_id)
                        skip = True
                if skip:
                    for name in reg_points:
                        if name not in result:
                            result[name] = _bad_pv(ModbusDriverErrors.CONN_FAILED)
                            self._record_read_failure(device_id)
                            self._record_point_failure(device_id, name)
                else:
                    try:
                        batch_result = await asyncio.wait_for(
                            self._batch_read_points(current_client, slave_id, reg_points, byte_order, device_id),
                            timeout=read_timeout,
                        )
                    except TimeoutError:  # FIXED-P1: 兼容Python<3.11
                        self._record_read_failure(device_id)
                        self._log_error(
                            device_id, ModbusDriverErrors.READ_TIMEOUT, f"Batch read timeout ({read_timeout}s)"
                        )
                        batch_result = {name: _bad_pv(ModbusDriverErrors.READ_TIMEOUT) for name in reg_points}
                    except Exception as e:
                        # FIXED-P1: 原问题-非超时异常(如ModbusException/ConnectionException)未被捕获，
                        # batch_result未定义导致UnboundLocalError或整批测点结果丢失
                        self._record_read_failure(device_id)
                        self._log_error(device_id, ModbusDriverErrors.CONN_FAILED, f"Batch read error: {e}")
                        batch_result = {name: _bad_pv(ModbusDriverErrors.CONN_FAILED) for name in reg_points}
                    t0 = time.monotonic()
                    for point_name, value in batch_result.items():
                        if isinstance(value, PointValue) and value.quality == "bad":
                            result[point_name] = value
                            self._record_point_failure(device_id, point_name)
                        else:
                            pt_def = reg_points.get(point_name, {})
                            value = self._apply_pipeline(value, pt_def, device_id, point_name)
                            result[point_name] = value
                            latency = (time.monotonic() - t0) * 1000
                            if not (isinstance(value, PointValue) and value.quality == "bad"):
                                self._record_point_success(device_id, point_name, latency)
                            else:
                                self._record_point_failure(device_id, point_name)
                        self._read_fail_tracker.pop((device_id, point_name), None)
                    if batch_result:
                        await self._record_read_success(
                            device_id
                        )  # #[AUDIT-FIX] _record_read_success is async, must await (was no-op coroutine)
                    else:
                        self._record_read_failure(device_id)

            self._check_degradation(device_id)

            self._publish_point_updates(device_id, result)
            await self._evaluate_edge_rules(device_id, result)
            if self._ts_store:
                await self._ts_store.write_read_result(device_id, result)

            return result
        finally:
            # MTCP-MED-001: 释放 client 租用
            await self._release_client(client)

    async def _batch_read_points(
        self,
        client: AsyncModbusTcpClient,
        slave_id: int,
        point_defs: dict[str, dict],
        byte_order: str,
        device_id: str,
    ) -> dict[str, Any]:
        """批量合并读取寄存器测点，自动分包（125寄存器自动拆分）

        将连续相邻地址的测点合并为一次读取请求，每段不超过125个寄存器）
        超过则拆分为多个子段并发执行）
        """
        # 按地址排序
        sorted_points = sorted(point_defs.items(), key=lambda x: int(x[1].get("address", 0)))

        # 合并连续/相邻地址为读取段
        MAX_REGS = 125
        segments: list[tuple[int, int, list[tuple[str, dict]]]] = []  # (start_addr, count, [(name, pt_def)])
        seg_start: int | None = None
        seg_end: int = 0
        seg_items: list[tuple[str, dict]] = []

        for name, pt_def in sorted_points:
            addr = int(pt_def.get("address", 0))
            # 协议边界校验: Modbus 寄存器地址有效范围为 0-65535
            if not 0 <= addr <= 65535:
                raise ValueError(f"Modbus 寄存器地址超出有效范围 0-65535: {addr}")
            data_type = pt_def.get("data_type", "float32")
            n_regs = DATA_TYPE_REGS.get(data_type, 1)

            if seg_start is None:
                seg_start = addr
                seg_end = addr + n_regs
                seg_items = [(name, pt_def)]
            elif addr <= seg_end and (addr + n_regs - seg_start) <= MAX_REGS:
                # 可以合并到当前段
                seg_end = max(seg_end, addr + n_regs)
                seg_items.append((name, pt_def))
            else:
                # 保存当前段，开始新段
                segments.append((seg_start, seg_end - seg_start, seg_items))
                seg_start = addr
                seg_end = addr + n_regs
                seg_items = [(name, pt_def)]

        if seg_start is not None:
            segments.append((seg_start, seg_end - seg_start, seg_items))

        # 对超过125寄存器的段进行拆分
        sub_segments: list[tuple[int, int, list[tuple[str, dict]]]] = []
        for start, count, items in segments:
            if count <= MAX_REGS:
                sub_segments.append((start, count, items))
            else:
                # 按测点拆分为多个子段
                sub_start = None
                sub_end = 0
                sub_items: list[tuple[str, dict]] = []
                for name, pt_def in items:
                    addr = int(pt_def.get("address", 0))
                    # 协议边界校验: Modbus 寄存器地址有效范围为 0-65535
                    if not 0 <= addr <= 65535:
                        raise ValueError(f"Modbus 寄存器地址超出有效范围 0-65535: {addr}")
                    data_type = pt_def.get("data_type", "float32")
                    n_regs = DATA_TYPE_REGS.get(data_type, 1)
                    if sub_start is None:
                        sub_start = addr
                        sub_end = addr + n_regs
                        sub_items = [(name, pt_def)]
                    elif (addr + n_regs - sub_start) <= MAX_REGS:
                        sub_end = max(sub_end, addr + n_regs)
                        sub_items.append((name, pt_def))
                    else:
                        sub_segments.append((sub_start, sub_end - sub_start, sub_items))
                        sub_start = addr
                        sub_end = addr + n_regs
                        sub_items = [(name, pt_def)]
                if sub_start is not None:
                    sub_segments.append((sub_start, sub_end - sub_start, sub_items))

        # 并发执行所有子段读取
        result: dict[str, Any] = {}
        failed_points: dict[str, Exception] = {}

        async def _read_segment(
            start_addr: int,
            count: int,
            items: list[tuple[str, dict]],
        ) -> None:
            _set_client_slave_id(client, slave_id)
            try:
                read_result = await client.read_holding_registers(start_addr, **_read_kwargs(count, slave_id))
                if read_result.isError():
                    # 解析Modbus异常码
                    exc_desc = _parse_modbus_exception(read_result)
                    if exc_desc:
                        logger.warning("[modbus_tcp] device=%s Modbus exception: %s", device_id, exc_desc)
                        # 0x84/0x86: 服务端故障/忙，可重试
                        if exc_desc.startswith("Server Device Failure") or exc_desc.startswith("Server Device Busy"):
                            # MTCP-007: 总超时10秒，指数退避1s, 2s, 4s
                            retry_start = time.monotonic()
                            for _idx, delay in enumerate(
                                _SINGLE_POINT_RETRY_DELAYS
                            ):  # FIXED(P3): 原问题-B007循环变量idx未使用; 修复-改为_idx
                                if time.monotonic() - retry_start > _SINGLE_POINT_RETRY_TIMEOUT:
                                    logger.warning(
                                        "[modbus_tcp] device=%s retry loop exceeded %ds, aborting",
                                        device_id,
                                        _SINGLE_POINT_RETRY_TIMEOUT,
                                    )
                                    break
                                await asyncio.sleep(delay)
                                # FIXED-Bug4: 单次 I/O 超时不超过剩余预算，与 _read_single_point 行为一致
                                _remaining_budget = max(
                                    1.0, _SINGLE_POINT_RETRY_TIMEOUT - (time.monotonic() - retry_start)
                                )
                                try:
                                    retry_result = await asyncio.wait_for(
                                        client.read_holding_registers(start_addr, **_read_kwargs(count, slave_id)),
                                        timeout=_remaining_budget,
                                    )
                                except TimeoutError:
                                    continue
                                if not retry_result.isError():
                                    registers = retry_result.registers
                                    for name, pt_def in items:
                                        addr = int(pt_def.get("address", 0))
                                        # 协议边界校验: Modbus 寄存器地址有效范围为 0-65535
                                        if not 0 <= addr <= 65535:
                                            raise ValueError(f"Modbus 寄存器地址超出有效范围 0-65535: {addr}")
                                        data_type = pt_def.get("data_type", "float32")
                                        n_regs = DATA_TYPE_REGS.get(data_type, 1)
                                        offset = addr - start_addr
                                        if offset < 0 or offset + n_regs > len(
                                            registers
                                        ):  # FIXED-P2: 增加offset<0检查，防止负索引返回错误数据
                                            failed_points[name] = ModbusException("Insufficient registers in batch")
                                            continue
                                        pt_regs = registers[offset : offset + n_regs]
                                        try:
                                            value = self._decode_point_value(pt_regs, data_type, byte_order)
                                            result[name] = value
                                        except Exception as e:
                                            logger.warning(
                                                "[modbus_tcp] point decode failed: device=%s point=%s error=%s",
                                                device_id,
                                                name,
                                                e,
                                            )
                                            failed_points[name] = e
                                    return
                    for name, _ in items:
                        failed_points[name] = ModbusException(f"批量读取错误: {read_result}")
                    return
                registers = read_result.registers
                for name, pt_def in items:
                    addr = int(pt_def.get("address", 0))
                    # 协议边界校验: Modbus 寄存器地址有效范围为 0-65535
                    if not 0 <= addr <= 65535:
                        raise ValueError(f"Modbus 寄存器地址超出有效范围 0-65535: {addr}")
                    data_type = pt_def.get("data_type", "float32")
                    n_regs = DATA_TYPE_REGS.get(data_type, 1)
                    offset = addr - start_addr
                    if offset < 0 or offset + n_regs > len(registers):  # FIXED-P2: 添加offset负值检查
                        failed_points[name] = ModbusException("Insufficient registers in batch")
                        continue
                    pt_regs = registers[offset : offset + n_regs]
                    try:
                        value = self._decode_point_value(pt_regs, data_type, byte_order)
                        result[name] = value
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.warning(
                            "[modbus_tcp] point decode failed: device=%s point=%s error=%s", device_id, name, e
                        )
                        failed_points[name] = e
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(
                    "[modbus_tcp] batch read segment failed: device=%s error=%s", device_id, e, exc_info=True
                )
                for name, _ in items:
                    failed_points[name] = e

        # 判断每个测点的register_type，input类型需要单独读取
        holding_segments: list[tuple[int, int, list[tuple[str, dict]]]] = []
        input_items: list[tuple[str, dict]] = []

        for start, count, items in sub_segments:
            # 检查是否全部为 input 类型
            all_input = all(pt_def.get("register_type", "holding") == "input" for _, pt_def in items)
            if all_input:
                input_items.extend(items)
            else:
                holding_segments.append((start, count, items))

        # 串行读取holding区  # FIXED-P2: 并发读取共享client存在事务ID冲突风险，改为串行
        if holding_segments:
            tasks = [_read_segment(s, c, i) for s, c, i in holding_segments]
            for task in tasks:
                await task

        # 逐个读取input类型测点（通常较少）
        for name, pt_def in input_items:
            try:
                value = await self._read_single_point(client, slave_id, pt_def, byte_order, device_id, name)
                result[name] = value
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(
                    "[modbus_tcp] input register read failed: device=%s point=%s error=%s", device_id, name, e
                )
                failed_points[name] = e

        # 记录失败的测点
        for name, err in failed_points.items():
            error_code = _resolve_error_code(err)
            self._log_throttled(device_id, name, err, error_code)
            result[name] = _bad_pv(error_code)

        return result

    def _decode_point_value(self, registers: list[int], data_type: str, byte_order: str) -> Any:
        """从寄存器列表解码单个测点值"""
        if not registers:
            raise ModbusException("Empty registers: communication error, no data received")
        if data_type == "bool":
            return bool(registers[0])
        elif data_type == "int16":
            val = registers[0]
            return val if val < 32768 else val - 65536
        elif data_type == "uint16":
            return registers[0]
        elif data_type == "int32":
            return self._decode_registers(registers, byte_order, "i", 2)
        elif data_type == "uint32":
            return self._decode_registers(registers, byte_order, "I", 2)
        elif data_type == "float32":
            return self._decode_registers(registers, byte_order, "f", 2)
        elif data_type == "float64":
            return self._decode_registers(registers, byte_order, "d", 4)
        elif data_type == "string":
            raw_bytes = b"".join(struct.pack(">H", r) for r in registers)
            try:
                decoded = raw_bytes.decode("utf-8", errors="strict").rstrip("\x00")
            except UnicodeDecodeError as e:
                logger.warning("[modbus_tcp] string解码失败: %s", e)  # FIXED-P2: string解码使用strict并记录warning
                decoded = raw_bytes.decode("utf-8", errors="ignore").rstrip("\x00")
            # FIXED-P2: string解码无长度限制，超出256字节截断并标记uncertain
            if len(decoded.encode("utf-8")) > 256:
                decoded = decoded.encode("utf-8")[:256].decode("utf-8", errors="ignore")
                logger.warning("[modbus_tcp] string解码超256字节，已截断: len=%d", len(raw_bytes))
            return decoded  # FIXED-P0: 字符串截断时返回原始值而非PointValue，防止调用方双重包装导致PointValue(value=PointValue(...), quality="good")
        else:
            return registers[0]

    _READ_LOG_INTERVAL = 60.0

    def _log_throttled(
        self, device_id: str, point_name: str, error: Exception, error_code: str = ModbusDriverErrors.READ_FAILED
    ) -> None:
        key = (device_id, point_name)
        now = time.monotonic()
        # FIXED-P1: 容量超限时淘汰最旧条目，与RTU驱动一致
        if len(self._read_fail_tracker) >= self._MAX_TRACKER_ENTRIES and key not in self._read_fail_tracker:
            self._read_fail_tracker.pop(next(iter(self._read_fail_tracker)), None)
        first_time, last_log = self._read_fail_tracker.get(key, (now, 0.0))
        level = logging.WARNING if now - first_time < 5.0 else logging.DEBUG
        if now - last_log >= self._READ_LOG_INTERVAL:
            logging.getLogger(__name__).log(
                level,
                "[%s] device=%s code=%s point=%s msg=%s",
                self.plugin_name,
                device_id,
                error_code,
                point_name,
                error,
            )
            self._read_fail_tracker[key] = (first_time, now)
        else:
            self._read_fail_tracker[key] = (first_time, last_log)

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        if not await self.check_permission(
            Permission.DEVICE_WRITE_POINT
        ):  # FIXED-P1: 写入操作添加权限检查，与RTU驱动一致
            self._log_error(device_id, "WRITE_DENIED", f"role={self._current_user_role} lacks device:write")
            return False
        # FIXED-TOCTOU (并发安全 #1): 原子 get+lease，在 _lease_lock 内完成 client 获取与租用，
        # 防止 _stale_client_cleanup_loop 在 get 与 _lease_client 之间关闭/移除 client。
        # 之前: client = self._clients.get(device_id) (无锁) → _lease_client (加锁)，
        #       cleanup_loop 可在两步之间 close(client) 并从 _clients 移除，导致 use-after-close。
        # 之后: get+check+lease 在同一个 _lease_lock 内原子完成，与 cleanup_loop 的 close 互斥。
        async with self._lease_lock:
            client = self._clients.get(device_id)
            if client is None or not client.connected:
                client = None
            else:
                self._leased_clients.add(client)
        if client is None:
            self._transition_state(device_id, ConnectionState.DISCONNECTED.value, "client not connected")
            return False

        try:
            config = self._device_configs.get(device_id, {})
            slave_id = config.get("slave_id", 1)
            byte_order = config.get("byte_order", "ABCD")
            write_timeout = config.get("timeout", WRITE_TIMEOUT)
            write_rate_limit = config.get("write_rate_limit", self._WRITE_RATE_LIMIT_DEFAULT)
            write_verify = config.get("write_verify", False)
            device_points = self._device_points.get(device_id, [])

            pt_def = next((p for p in device_points if p.get("name") == point), None)
            if pt_def is None:
                return False

            address = int(pt_def.get("address", 0))
            data_type = pt_def.get("data_type", "float32")
            clamp = pt_def.get("clamp", config.get("clamp"))
            old_value = self._last_values.get((device_id, point))

            if not self._check_write_value_range(value, clamp):
                self._log_error(
                    device_id,
                    ModbusDriverErrors.VALUE_OUT_OF_RANGE,
                    f"{point}: write value {value} out of range {clamp}",
                )
                self._audit_write(device_id, point, old_value, value, "rejected", "value out of clamp range")
                return False

            # FIXED-P1: 检查NaN/Inf，防止异常值写入设备（NaN比较始终为False可绕过clamp检查）
            if isinstance(value, float) and (value != value or value == float("inf") or value == float("-inf")):
                self._log_error(
                    device_id,
                    ModbusDriverErrors.VALUE_OUT_OF_RANGE,
                    f"{point}: write value {value} is NaN/Inf, rejected",
                )
                self._audit_write(device_id, point, old_value, value, "rejected", "NaN/Inf value")
                return False

            if not self._check_write_rate_limit(device_id, point, write_rate_limit):
                self._log_error(
                    device_id,
                    ModbusDriverErrors.WRITE_FAILED,
                    f"{point}: write rate limited (min interval {write_rate_limit}s)",
                )
                self._audit_write(device_id, point, old_value, value, "rejected", "write rate limited")
                return False

            # FIXED-BCAST (并发安全 #2): 广播路径仅由 slave_id==0 决定，broadcast 配置仅作为"允许广播"开关。
            # 之前: `if slave_id == 0 or broadcast_enabled` 会在 broadcast_enabled=True 时
            #       把 slave_id=1-247 的单播错误地走广播路径 (slave_id=0)，写入总线上所有设备。
            # 之后:
            #   - slave_id=0 + broadcast_enabled=True  → 广播写入 (slave_id=0)
            #   - slave_id=0 + broadcast_enabled=False → 拒绝写入 (BCAST_NOT_ENABLED)
            #   - slave_id=1-247 (任意 broadcast_enabled) → 正常单播写入
            broadcast_enabled = config.get("broadcast", False)
            if slave_id == 0:
                if not broadcast_enabled:
                    self._log_error(
                        device_id,
                        ModbusDriverErrors.BCAST_NOT_ENABLED,
                        f"{point}: slave_id=0 but broadcast not enabled in config",
                    )
                    self._audit_write(device_id, point, old_value, value, "rejected", "broadcast not enabled")
                    return False
                try:
                    ok = await asyncio.wait_for(  # FIXED-P0: 广播写入添加超时保护，与普通写入一致，防止TCP挂起时client租用无法释放
                        self._broadcast_write(client, address, data_type, value, byte_order, device_id, point),
                        timeout=write_timeout,
                    )
                except TimeoutError:
                    ok = False
                    self._log_error(
                        device_id,
                        ModbusDriverErrors.WRITE_TIMEOUT,
                        f"{point}: broadcast write timeout ({write_timeout}s)",
                    )
                self._audit_write(device_id, point, old_value, value, "ok" if ok else "failed")
                return ok

            _set_client_slave_id(client, slave_id)

            tx_data = f"FC=write addr={address} slave={slave_id} type={data_type} val={value}"
            record_packet("tx", self.plugin_name, device_id, tx_data)

            async def _do_write():
                if data_type == "bool":
                    await client.write_coil(address, bool(value), **_slave_kwarg(slave_id))
                elif data_type in ("int16", "uint16"):
                    await client.write_register(address, int(value) & 0xFFFF, **_slave_kwarg(slave_id))
                elif data_type == "int32":
                    regs = self._encode_value(int(value), byte_order, "i", 2)
                    await client.write_registers(address, regs, **_slave_kwarg(slave_id))
                elif data_type == "uint32":
                    regs = self._encode_value(int(value), byte_order, "I", 2)
                    await client.write_registers(address, regs, **_slave_kwarg(slave_id))
                elif data_type == "float32":
                    regs = self._encode_value(float(value), byte_order, "f", 2)
                    await client.write_registers(address, regs, **_slave_kwarg(slave_id))
                elif data_type == "float64":
                    regs = self._encode_value(float(value), byte_order, "d", 4)
                    await client.write_registers(address, regs, **_slave_kwarg(slave_id))
                else:
                    await client.write_register(address, int(value) & 0xFFFF, **_slave_kwarg(slave_id))

            try:
                await asyncio.wait_for(_do_write(), timeout=write_timeout)
                rx_data = f"FC=write addr={address} slave={slave_id} OK"
                record_packet("rx", self.plugin_name, device_id, rx_data)

                if write_verify:
                    verified = await self._read_verify_write(
                        client, slave_id, address, data_type, byte_order, value, device_id
                    )
                    if not verified:
                        self._record_write_failure(device_id)
                        self._log_error(
                            device_id,
                            ModbusDriverErrors.WRITE_FAILED,
                            f"{point}: write verify failed (read-back mismatch)",
                        )
                        self._audit_write(device_id, point, old_value, value, "verify_failed", "read-back mismatch")
                        return False

                self._record_write_success(device_id)
                self._audit_write(device_id, point, old_value, value, "ok")
                if self._audit:
                    await self._audit.log_write(device_id, point, value)
                return True
            except TimeoutError:  # FIXED-P1: 兼容Python<3.11
                self._record_write_failure(device_id)
                self._log_error(
                    device_id, ModbusDriverErrors.WRITE_TIMEOUT, f"{point}: write timeout ({write_timeout}s)"
                )
                self._audit_write(device_id, point, old_value, value, "failed", "timeout")
                return False
            except Exception as e:
                self._record_write_failure(device_id)
                logger.warning(
                    "[modbus_tcp] write failed: device=%s point=%s error=%s", device_id, point, e, exc_info=True
                )
                self._log_error(device_id, ModbusDriverErrors.WRITE_FAILED, f"{point}: {e}")
                self._audit_write(device_id, point, old_value, value, "failed", str(e))
                return False
        finally:
            # MTCP-MED-001: 释放 client 租用
            await self._release_client(client)

    async def write_points_batch(self, device_id: str, points: dict[str, Any]) -> dict[str, bool]:
        if not await self.check_permission(Permission.DEVICE_WRITE_POINT):  # FIXED-P1: 批量写入同样需要权限检查
            self._log_error(device_id, "WRITE_DENIED", f"role={self._current_user_role} lacks device:write")
            return {name: False for name in points}
        # FIXED-TOCTOU (并发安全 #1): 原子 get+lease，与 write_point 一致，
        # 防止 _stale_client_cleanup_loop 在 get 与 lease 之间关闭/移除 client。
        async with self._lease_lock:
            client = self._clients.get(device_id)
            if client is None or not client.connected:
                client = None
            else:
                self._leased_clients.add(client)
        if client is None:
            self._transition_state(device_id, ConnectionState.DISCONNECTED.value, "client not connected")
            return {name: False for name in points}

        try:
            config = self._device_configs.get(device_id, {})
            slave_id = config.get("slave_id", 1)
            byte_order = config.get("byte_order", "ABCD")
            write_timeout = config.get("timeout", WRITE_TIMEOUT)
            write_rate_limit = config.get("write_rate_limit", self._WRITE_RATE_LIMIT_DEFAULT)
            write_verify = config.get("write_verify", False)
            device_points_list = self._device_points.get(device_id, [])

            pt_defs: dict[str, dict] = {}
            for pt in device_points_list:
                pt_defs[pt.get("name", "")] = pt

            single_writes: dict[str, Any] = {}
            batch_candidates: list[tuple[str, int, str, Any]] = []

            for point_name, value in points.items():
                # FIXED-P2: 批量写入前检查NaN/Inf，防止异常浮点值写入设备
                if isinstance(value, float) and (value != value or value == float("inf") or value == float("-inf")):
                    self._log_error(
                        device_id,
                        ModbusDriverErrors.VALUE_OUT_OF_RANGE,
                        f"{point_name}: batch write value {value} is NaN/Inf",
                    )
                    self._audit_write(
                        device_id,
                        point_name,
                        self._last_values.get((device_id, point_name)),
                        value,
                        "rejected",
                        "NaN/Inf value",
                    )
                    continue
                pt_def = pt_defs.get(point_name)
                if pt_def is None:
                    single_writes[point_name] = value
                    continue
                clamp = pt_def.get("clamp", config.get("clamp"))
                if not self._check_write_value_range(value, clamp):
                    self._log_error(
                        device_id,
                        ModbusDriverErrors.VALUE_OUT_OF_RANGE,
                        f"{point_name}: batch write value {value} out of range",
                    )
                    self._audit_write(
                        device_id,
                        point_name,
                        self._last_values.get((device_id, point_name)),
                        value,
                        "rejected",
                        "value out of clamp range",
                    )
                    continue
                if not self._check_write_rate_limit(device_id, point_name, write_rate_limit):
                    self._audit_write(
                        device_id,
                        point_name,
                        self._last_values.get((device_id, point_name)),
                        value,
                        "rejected",
                        "write rate limited",
                    )
                    continue
                data_type = pt_def.get("data_type", "float32")
                address = int(pt_def.get("address", 0))
                if data_type == "bool":
                    single_writes[point_name] = value
                else:
                    batch_candidates.append((point_name, address, data_type, value))

            results: dict[str, bool] = {}

            for point_name, value in single_writes.items():
                # MTCP-MED-001: 这里复用 client 租用，但 write_point 会先尝试租用
                # 由于当前协程已经持有租用，write_point 中的租用检查会失败
                # 所以需要直接执行单点写入
                ok = await self._write_single_point_no_lease(
                    client,
                    device_id,
                    point_name,
                    value,
                    config,
                    pt_defs,
                    write_timeout,
                    write_verify,
                    slave_id,
                    byte_order,
                )
                results[point_name] = ok

            if not batch_candidates:
                return results

            batch_candidates.sort(key=lambda x: x[1])

            segments: list[list[tuple[str, int, str, Any]]] = []
            current_seg: list[tuple[str, int, str, Any]] = []

            for item in batch_candidates:
                _, addr, dtype, _ = item
                n_regs = DATA_TYPE_REGS.get(dtype, 1)
                if not current_seg:
                    current_seg = [item]
                else:
                    last_addr = current_seg[-1][1]
                    last_dtype = current_seg[-1][2]
                    last_n_regs = DATA_TYPE_REGS.get(last_dtype, 1)
                    expected_next = last_addr + last_n_regs
                    total_regs = (addr + n_regs) - current_seg[0][1]
                    if addr == expected_next and total_regs <= 125:
                        current_seg.append(item)
                    else:
                        segments.append(current_seg)
                        current_seg = [item]
            if current_seg:
                segments.append(current_seg)

            _set_client_slave_id(client, slave_id)

            for seg in segments:
                start_addr = seg[0][1]
                all_regs: list[int] = []
                point_map: dict[str, Any] = {}
                for point_name, addr, dtype, val in seg:
                    n_regs = DATA_TYPE_REGS.get(dtype, 1)
                    if dtype == "int16" or dtype == "uint16":
                        regs = [int(val) & 0xFFFF]
                    elif dtype == "int32":
                        regs = self._encode_value(int(val), byte_order, "i", 2)
                    elif dtype == "uint32":
                        regs = self._encode_value(int(val), byte_order, "I", 2)
                    elif dtype == "float32":
                        regs = self._encode_value(float(val), byte_order, "f", 2)
                    elif dtype == "float64":
                        regs = self._encode_value(float(val), byte_order, "d", 4)
                    else:
                        regs = [int(val) & 0xFFFF]
                    offset = addr - start_addr
                    while len(all_regs) < offset:
                        all_regs.append(0)
                    for i, r in enumerate(regs):
                        idx = offset + i
                        while len(all_regs) <= idx:
                            all_regs.append(0)
                        all_regs[idx] = r
                    point_map[point_name] = val

                try:
                    await asyncio.wait_for(
                        client.write_registers(start_addr, all_regs, **_slave_kwarg(slave_id)),
                        timeout=write_timeout,
                    )
                    record_packet("tx", self.plugin_name, device_id, f"FC=16 addr={start_addr} regs={len(all_regs)}")
                    for point_name, val in point_map.items():
                        old_val = self._last_values.get((device_id, point_name))
                        if write_verify:
                            pt_def = pt_defs.get(point_name, {})
                            addr_v = int(pt_def.get("address", 0))
                            dtype_v = pt_def.get("data_type", "float32")
                            verified = await self._read_verify_write(
                                client, slave_id, addr_v, dtype_v, byte_order, val, device_id
                            )
                            if not verified:
                                results[point_name] = False
                                self._audit_write(
                                    device_id, point_name, old_val, val, "verify_failed", "read-back mismatch"
                                )
                                continue
                        results[point_name] = True
                        self._record_write_success(device_id)
                        self._audit_write(device_id, point_name, old_val, val, "ok")
                except Exception as e:
                    logger.warning("[modbus_tcp] batch write failed: device=%s error=%s", device_id, e, exc_info=True)
                    for point_name in point_map:
                        results[point_name] = False
                        self._record_write_failure(device_id)
                        self._log_error(device_id, ModbusDriverErrors.WRITE_FAILED, f"batch write: {e}")
                        self._audit_write(
                            device_id,
                            point_name,
                            self._last_values.get((device_id, point_name)),
                            point_map[point_name],
                            "failed",
                            str(e),
                        )

            return results
        finally:
            # MTCP-MED-001: 释放 client 租用
            await self._release_client(client)

    async def _write_single_point_no_lease(
        self,
        client: AsyncModbusTcpClient,
        device_id: str,
        point: str,
        value: Any,
        config: dict,
        pt_defs: dict,
        write_timeout: float,
        write_verify: bool,
        slave_id: int,
        byte_order: str,
    ) -> bool:
        """单点写入（不申请租用，调用方已持有租用）"""
        pt_def = pt_defs.get(point)
        if pt_def is None:
            return False

        address = int(pt_def.get("address", 0))
        data_type = pt_def.get("data_type", "float32")
        clamp = pt_def.get("clamp", config.get("clamp"))
        old_value = self._last_values.get((device_id, point))

        if not self._check_write_value_range(value, clamp):
            self._log_error(
                device_id, ModbusDriverErrors.VALUE_OUT_OF_RANGE, f"{point}: write value {value} out of range {clamp}"
            )
            self._audit_write(device_id, point, old_value, value, "rejected", "value out of clamp range")
            return False

        _set_client_slave_id(client, slave_id)

        async def _do_write():
            if data_type == "bool":
                await client.write_coil(address, bool(value), **_slave_kwarg(slave_id))
            elif data_type in ("int16", "uint16"):
                await client.write_register(address, int(value) & 0xFFFF, **_slave_kwarg(slave_id))
            elif data_type == "int32":
                regs = self._encode_value(int(value), byte_order, "i", 2)
                await client.write_registers(address, regs, **_slave_kwarg(slave_id))
            elif data_type == "uint32":
                regs = self._encode_value(int(value), byte_order, "I", 2)
                await client.write_registers(address, regs, **_slave_kwarg(slave_id))
            elif data_type == "float32":
                regs = self._encode_value(float(value), byte_order, "f", 2)
                await client.write_registers(address, regs, **_slave_kwarg(slave_id))
            elif data_type == "float64":
                regs = self._encode_value(float(value), byte_order, "d", 4)
                await client.write_registers(address, regs, **_slave_kwarg(slave_id))
            else:
                await client.write_register(address, int(value) & 0xFFFF, **_slave_kwarg(slave_id))

        try:
            await asyncio.wait_for(_do_write(), timeout=write_timeout)
            if write_verify:
                verified = await self._read_verify_write(
                    client, slave_id, address, data_type, byte_order, value, device_id
                )
                if not verified:
                    self._record_write_failure(device_id)
                    self._log_error(device_id, ModbusDriverErrors.WRITE_FAILED, f"{point}: write verify failed")
                    self._audit_write(device_id, point, old_value, value, "verify_failed", "read-back mismatch")
                    return False
            self._record_write_success(device_id)
            self._audit_write(device_id, point, old_value, value, "ok")
            return True
        except TimeoutError:  # FIXED-P1: 兼容Python<3.11
            self._record_write_failure(device_id)
            self._log_error(device_id, ModbusDriverErrors.WRITE_TIMEOUT, f"{point}: write timeout")
            self._audit_write(device_id, point, old_value, value, "failed", "timeout")
            return False
        except Exception as e:
            self._record_write_failure(device_id)
            logger.warning(
                "[modbus_tcp] single point write failed: device=%s point=%s error=%s",
                device_id,
                point,
                e,
                exc_info=True,
            )
            self._log_error(device_id, ModbusDriverErrors.WRITE_FAILED, f"{point}: {e}")
            self._audit_write(device_id, point, old_value, value, "failed", str(e))
            return False

    async def _broadcast_write(
        self,
        client: AsyncModbusTcpClient,
        address: int,
        data_type: str,
        value: Any,
        byte_order: str,
        device_id: str,
        point: str,
    ) -> bool:
        """执行广播写入（slave_id=0），无响应确认

        Modbus 协议规定广播地址（slave_id=0）写入时不返回响应）
        此方法仅发送写入请求，无法验证写入是否成功）

        Args:
            client: Modbus TCP客户端
            address: 寄存器地址
            data_type: 数据类型
            value: 写入值
            byte_order: 字节序
            device_id: 设备ID（用于日志）
            point: 测点名（用于日志）

        Returns:
            始终返回True（因无响应确认），记录WARNING 日志提醒用户
        """
        logger.warning(
            "[modbus_tcp] device=%s code=BROADCAST_WRITE point=%s addr=%d type=%s val=%s "
            "msg=Broadcast write (slave_id=0) sent, no response expected. "
            "Success cannot be guaranteed - use with caution.",
            device_id,
            point,
            address,
            data_type,
            value,
        )
        record_packet("tx", self.plugin_name, device_id, f"BCAST addr={address} type={data_type} val={value}")
        try:
            if data_type == "bool":
                await client.write_coil(address, bool(value), **_slave_kwarg(0))
            elif data_type == "int16" or data_type == "uint16":
                await client.write_register(address, int(value) & 0xFFFF, **_slave_kwarg(0))
            elif data_type == "int32":
                regs = self._encode_value(int(value), byte_order, "i", 2)
                await client.write_registers(address, regs, **_slave_kwarg(0))
            elif data_type == "uint32":
                regs = self._encode_value(int(value), byte_order, "I", 2)
                await client.write_registers(address, regs, **_slave_kwarg(0))
            elif data_type == "float32":
                regs = self._encode_value(float(value), byte_order, "f", 2)
                await client.write_registers(address, regs, **_slave_kwarg(0))
            elif data_type == "float64":
                regs = self._encode_value(float(value), byte_order, "d", 4)
                await client.write_registers(address, regs, **_slave_kwarg(0))
            else:
                await client.write_register(address, int(value) & 0xFFFF, **_slave_kwarg(0))
            # 广播写入无法确认，假设成功
            return True
        except Exception as e:
            logger.warning(
                "[modbus_tcp] broadcast write failed: device=%s point=%s error=%s", device_id, point, e, exc_info=True
            )
            self._log_error(device_id, ModbusDriverErrors.BCAST_WRITE_FAILED, f"{point}: {e}")
            return False

    def is_device_connected(self, device_id: str) -> bool:
        """检查设备是否已连接"""
        client = self._clients.get(device_id)
        return client is not None and client.connected

    async def discover_devices(self, config: dict) -> list[dict]:
        """扫描指定IP或IP段内的Modbus设备"""
        host = config.get("host", "127.0.0.1")
        port = config.get("port", 502)
        # FIXED: 默认只扫描从站ID 1，避免扫描247个无效地址
        slave_ids = config.get("slave_ids", [1])
        # 限制从站ID范围，防止恶意/误配置扫描全网段
        if not isinstance(slave_ids, list) or len(slave_ids) > 247:
            slave_ids = [1]
        slave_ids = [sid for sid in slave_ids if isinstance(sid, int) and 0 < sid <= 247]
        if not slave_ids:
            slave_ids = [1]
        max_concurrency = int(config.get("max_concurrency", 32))
        if max_concurrency > 256:  # FIXED-P2: 限制最大并发数，防止资源耗尽
            max_concurrency = 256

        hosts = self._expand_hosts(host)

        discovered = []
        sem = asyncio.Semaphore(max_concurrency)

        async def _probe(h: str, slave_id: int) -> dict | None:
            async with sem:
                client = AsyncModbusTcpClient(host=h, port=port, timeout=_DEVICE_CONNECT_TIMEOUT)
                try:
                    # FIXED-P1: 探测操作添加wait_for超时保护，防止底层阻塞导致探测任务永不完成
                    return await asyncio.wait_for(
                        _probe_inner(client, h, slave_id, port),
                        timeout=_DEVICE_CONNECT_TIMEOUT + 5.0,
                    )
                except TimeoutError:
                    logger.debug("[modbus_tcp] probe timeout: %s:%d slave=%d", h, port, slave_id)
                except Exception as e:
                    logger.debug("[modbus_tcp] error: %s", e)
                finally:
                    client.close()
            return None

        async def _probe_inner(client, h: str, slave_id: int, port: int) -> dict | None:
            connected = await client.connect()
            if connected:
                _set_client_slave_id(client, slave_id)
                result = await client.read_holding_registers(0, **_read_kwargs(1, slave_id))
                if not result.isError():
                    return {
                        "host": h,
                        "port": port,
                        "slave_id": slave_id,
                        "protocol": "modbus_tcp",
                        "name": f"modbus-{h.split('.')[-1]}-{slave_id}",
                    }
            return None

        tasks = [_probe(h, sid) for h in hosts for sid in slave_ids]
        # FIXED-P2: 降低最大探测Task数从4096到256，防止网络不可达时OOM
        # 之前：最多4096个Task同时创建，网络不可达时所有Task等待超时消耗大量内存
        # 之后：最多256个Task，分批执行避免同时创建大量Task
        _MAX_DISCOVER_TASKS = 256
        if len(tasks) > _MAX_DISCOVER_TASKS:
            logger.warning(
                "[modbus_tcp] discover too many probes (%d), truncating to %d", len(tasks), _MAX_DISCOVER_TASKS
            )
            tasks = tasks[:_MAX_DISCOVER_TASKS]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                logger.debug("[modbus_tcp] discover probe unexpected error: %s", r)
            elif isinstance(r, dict):
                discovered.append(r)

        return discovered

    _MAX_EXPAND_HOSTS = 1024  # FIXED-P2: IP展开最大数量，防止/8等大网段导致资源耗尽

    @staticmethod
    def _expand_hosts(host: str) -> list[str]:
        """将IP或网段展开为IP列表，支持x.x.x.x/x和x.x.x.*格式"""
        if "/" in host:
            try:
                import ipaddress

                network = ipaddress.ip_network(host, strict=False)
                hosts = [str(ip) for ip in network.hosts()]
                if len(hosts) > ModbusTcpDriver._MAX_EXPAND_HOSTS:  # FIXED-P2: 限制展开IP数量
                    hosts = hosts[: ModbusTcpDriver._MAX_EXPAND_HOSTS]
                return hosts
            except ValueError:
                return [host.split("/")[0]]
        if "*" in host:
            prefix = host.rsplit(".", 1)[0]
            return [f"{prefix}.{i}" for i in range(1, 255)]
        return [host]

    @staticmethod
    def _decode_registers(registers: list[int], byte_order: str, fmt_char: str, n_regs: int) -> Any:
        """根据字节序将寄存器列表解码为指定类型

        Args:
            registers: 原始寄存器值列表
            byte_order: 字节序ABCD/BADC/CDAB/DCBA
            fmt_char: struct 格式字符，如 'f'(float32), 'd'(float64), 'i'(int32), 'I'(uint32)
            n_regs: 需要的寄存器数量
        Returns:
            解码后的值
        """
        if len(registers) < n_regs:
            raise ModbusException(f"Insufficient registers: need {n_regs}, got {len(registers)}")
        # FIXED-P1: 非法byte_order抛ValueError而非静默降级，防止小端设备数据静默损坏
        if byte_order not in _BYTE_ORDER_FMT:
            raise ValueError(f"Invalid byte_order '{byte_order}', must be one of {list(_BYTE_ORDER_FMT.keys())}")
        reg_pack, val_unpack = _BYTE_ORDER_FMT[byte_order]
        raw = struct.pack(f"{reg_pack}{'H' * n_regs}", *registers[:n_regs])
        return struct.unpack(f"{val_unpack}{fmt_char}", raw)[0]

    @staticmethod
    def _encode_value(value: Any, byte_order: str, fmt_char: str, n_regs: int) -> list[int]:
        """根据字节序将值编码为寄存器列表

        Args:
            value: 要编码的值
            byte_order: 字节序ABCD/BADC/CDAB/DCBA
            fmt_char: struct 格式字符
            n_regs: 需要的寄存器数量
        Returns:
            寄存器值列表
        """
        # FIXED-P1: 非法byte_order抛ValueError而非静默降级
        if byte_order not in _BYTE_ORDER_FMT:
            raise ValueError(f"Invalid byte_order '{byte_order}', must be one of {list(_BYTE_ORDER_FMT.keys())}")
        reg_pack, val_unpack = _BYTE_ORDER_FMT[byte_order]
        raw = struct.pack(
            f"{val_unpack}{fmt_char}", value
        )  # FIXED-P0: 缺少此行导致raw未定义，int32/uint32/float32/float64写入必定NameError
        return list(struct.unpack(f"{reg_pack}{'H' * n_regs}", raw))

    _DEGRADE_INTERVALS = (1, 5, 30, 60)  # FIXED(P2): 原问题-可变默认值list; 修复-改为tuple
    _DEGRADE_SUCCESS_THRESHOLD = 0.95
    _DEGRADE_FAIL_THRESHOLD = 0.80
    _FROZEN_THRESHOLD = 10
    _HEALTH_WINDOW = 20

    def _record_point_success(self, device_id: str, point_name: str, latency_ms: float) -> None:
        key = (device_id, point_name)
        h = self._point_health.setdefault(
            key, {"success": 0, "fail": 0, "total": 0, "consecutive_fails": 0, "latencies": []}
        )
        self._point_health.move_to_end(key)  # FIXED-P1: LRU淘汰-访问时移到末尾
        if len(self._point_health) > 15000:  # FIXED-P2: LRU淘汰阈值调整为1.5万(15000)，减少热路径淘汰频率
            for _ in range(2000):
                self._point_health.popitem(last=False)
        h["success"] += 1
        h["total"] += 1
        h["consecutive_fails"] = 0
        h["latencies"].append(latency_ms)
        if len(h["latencies"]) > 100:
            h["latencies"] = h["latencies"][-100:]
        h["last_success_at"] = datetime.now(UTC).isoformat()
        qh = self._point_quality_history.setdefault(key, deque(maxlen=self._MAX_QUALITY_HISTORY))
        qh.append("good")
        stats = self._health_stats.get(device_id)
        if stats and hasattr(stats, "_record_latency"):
            stats._record_latency(latency_ms)
        lh = self._device_latency_history.setdefault(device_id, deque(maxlen=self._MAX_LATENCY_HISTORY))
        lh.append((time.monotonic(), latency_ms))

    def _record_point_failure(self, device_id: str, point_name: str) -> None:
        key = (device_id, point_name)
        h = self._point_health.setdefault(
            key, {"success": 0, "fail": 0, "total": 0, "consecutive_fails": 0, "latencies": []}
        )
        self._point_health.move_to_end(key)  # FIXED-P1: LRU淘汰-访问时移到末尾
        if len(self._point_health) > 15000:  # FIXED-P2: LRU淘汰阈值调整为1.5万(15000)，减少热路径淘汰频率
            for _ in range(2000):
                self._point_health.popitem(last=False)
        h["fail"] += 1
        h["total"] += 1
        h["consecutive_fails"] += 1
        qh = self._point_quality_history.setdefault(key, deque(maxlen=self._MAX_QUALITY_HISTORY))
        qh.append("bad")

    def _get_point_success_rate(self, device_id: str, point_name: str) -> float:
        key = (device_id, point_name)
        h = self._point_health.get(key)
        if not h or h["total"] == 0:
            return 1.0
        self._point_health.move_to_end(key)  # FIXED-P1: LRU淘汰-访问时移到末尾
        window = h["total"] if h["total"] <= self._HEALTH_WINDOW else self._HEALTH_WINDOW
        recent_success = min(h["success"], window)
        return recent_success / window if window > 0 else 1.0

    def _get_point_avg_latency(self, device_id: str, point_name: str) -> float:
        key = (device_id, point_name)
        h = self._point_health.get(key)
        if not h or not h["latencies"]:
            return 0.0
        self._point_health.move_to_end(key)  # FIXED-P1: LRU淘汰-访问时移到末尾
        recent = h["latencies"][-self._HEALTH_WINDOW :]
        return sum(recent) / len(recent)

    def _check_degradation(self, device_id: str) -> int:
        device_points = self._device_points.get(device_id, [])
        if not device_points:
            return 0
        total_rate = 0.0
        count = 0
        for pt in device_points:
            pn = pt.get("name", "")
            rate = self._get_point_success_rate(device_id, pn)
            total_rate += rate
            count += 1
        avg_rate = total_rate / count if count > 0 else 1.0
        current_level = self._degrade_level.get(device_id, 0)
        if avg_rate < self._DEGRADE_FAIL_THRESHOLD and current_level < len(self._DEGRADE_INTERVALS) - 1:
            new_level = current_level + 1
            self._degrade_level[device_id] = new_level
            logger.warning(
                "[modbus_tcp] device=%s degraded: level=%d interval=%ds avg_rate=%.2f",
                device_id,
                new_level,
                self._DEGRADE_INTERVALS[new_level],
                avg_rate,
            )
            return new_level
        if avg_rate > self._DEGRADE_SUCCESS_THRESHOLD and current_level > 0:
            new_level = current_level - 1
            self._degrade_level[device_id] = new_level
            logger.info(
                "[modbus_tcp] device=%s recovered: level=%d interval=%ds avg_rate=%.2f",
                device_id,
                new_level,
                self._DEGRADE_INTERVALS[new_level],
                avg_rate,
            )
            return new_level
        return current_level

    def _calc_degraded_interval(self, device_id: str) -> float:
        level = self._degrade_level.get(device_id, 0)
        return float(self._DEGRADE_INTERVALS[level])

    def _check_nan_inf(self, value: Any) -> tuple[Any, bool]:
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None, False
        return value, True

    def _set_last_value(self, device_id: str, point_name: str, value: float) -> None:
        """设置 last_value，带 LRU 淘汰机制（MTCP-009）"""
        key = (device_id, point_name)
        if key in self._last_values:
            # 移到末尾（最近使用）
            self._last_values.move_to_end(key)
        self._last_values[key] = value
        # 超过容量时淘汰最旧条目
        while len(self._last_values) > _MAX_LAST_VALUES:
            oldest_key = next(iter(self._last_values))
            self._last_values.pop(oldest_key)
            logger.debug("[modbus_tcp] LRU evicted oldest key: %s", oldest_key)

    def _check_rate_of_change(
        self, value: Any, last_value: Any, last_ts: float, threshold: float | None
    ) -> tuple[Any, str]:
        if threshold is None or last_value is None or value is None:
            return value, "good"
        if not isinstance(value, (int, float)) or not isinstance(last_value, (int, float)):
            return value, "good"
        now = time.monotonic()
        dt = now - last_ts
        if dt <= 0:
            return value, "good"
        rate = abs(value - last_value) / dt
        if rate > threshold:
            return value, "uncertain"
        return value, "good"

    def _check_frozen_value(
        self, device_id: str, point_name: str, value: Any, threshold: int | None
    ) -> tuple[Any, str]:
        if threshold is None or threshold <= 0:
            return value, "good"
        key = (device_id, point_name)
        last = self._last_values.get(key)
        if (
            value is not None
            and last is not None
            and isinstance(value, (int, float))
            and isinstance(last, (int, float))
        ):
            if abs(value - last) < 1e-9:
                count = self._frozen_count.get(key, 0) + 1
                self._frozen_count[key] = count
                if count >= threshold:
                    return value, "uncertain"
            else:
                self._frozen_count[key] = 0
        else:
            self._frozen_count[key] = 0
        return value, "good"

    def _apply_pipeline(self, value: Any, pt_def: dict, device_id: str, point_name: str) -> Any:
        # FIXED-P1: 非数值类型也包装为PointValue，统一返回类型
        if not isinstance(value, (int, float)):
            return PointValue(value=value, quality="good", timestamp=datetime.now(UTC), source="device")

        value, valid = self._check_nan_inf(value)
        if not valid:
            self._record_point_failure(device_id, point_name)
            return PointValue(value=None, quality="bad", timestamp=datetime.now(UTC), source="error:ERR_MODBUS_NAN_INF")

        config = self._device_configs.get(device_id, {})
        deadband = pt_def.get("deadband", config.get("deadband"))
        scaling = pt_def.get("scaling", config.get("scaling"))
        clamp = pt_def.get("clamp", config.get("clamp"))
        roc_threshold = pt_def.get("rate_of_change_threshold", config.get("rate_of_change_threshold"))
        frozen_threshold = pt_def.get("frozen_threshold", config.get("frozen_threshold", self._FROZEN_THRESHOLD))

        key = (device_id, point_name)
        last_value = self._last_values.get(key)
        last_ts = self._last_timestamp.get(key, 0.0)
        now_mono = time.monotonic()

        value, roc_quality = self._check_rate_of_change(value, last_value, last_ts, roc_threshold)

        value = self._apply_deadband(value, last_value, deadband)
        value = self._apply_scaling(value, scaling)
        value, in_range = self._apply_clamp(value, clamp)
        if not in_range:
            self._record_point_failure(device_id, point_name)
            self._log_error(
                device_id, ModbusDriverErrors.VALUE_OUT_OF_RANGE, f"{point_name}: value out of clamp range {clamp}"
            )
            return _bad_pv(ModbusDriverErrors.VALUE_OUT_OF_RANGE)

        value, frozen_quality = self._check_frozen_value(device_id, point_name, value, frozen_threshold)

        if isinstance(value, (int, float)):
            self._set_last_value(device_id, point_name, value)  # MTCP-009: 使用 LRU 方法
        self._last_timestamp[key] = now_mono

        final_quality = "good"
        if roc_quality == "uncertain" or frozen_quality == "uncertain":
            final_quality = "uncertain"

        if final_quality == "uncertain":
            key = (device_id, point_name)
            qh = self._point_quality_history.setdefault(key, deque(maxlen=self._MAX_QUALITY_HISTORY))
            qh.append("uncertain")
            return PointValue(value=value, quality="uncertain", timestamp=datetime.now(UTC), source="device")
        # FIXED-P1: 统一返回PointValue，与bad/uncertain路径保持类型一致
        return PointValue(value=value, quality="good", timestamp=datetime.now(UTC), source="device")

    _BACKOFF_MAX_DELAY = 60.0
    _PRIMARY_FAIL_THRESHOLD = 3
    _VALID_TRANSITIONS: dict[str, set[str]] = {
        ConnectionState.DISCONNECTED.value: {ConnectionState.CONNECTING.value},
        ConnectionState.CONNECTING.value: {
            ConnectionState.CONNECTED.value,
            ConnectionState.DISCONNECTED.value,
            ConnectionState.OFFLINE.value,
        },
        ConnectionState.CONNECTED.value: {ConnectionState.DISCONNECTED.value, ConnectionState.DEGRADED.value},
        ConnectionState.DEGRADED.value: {
            ConnectionState.CONNECTED.value,
            ConnectionState.DISCONNECTED.value,
            ConnectionState.OFFLINE.value,
        },
        ConnectionState.OFFLINE.value: {ConnectionState.CONNECTING.value},
    }

    def _transition_state(self, device_id: str, target: str, reason: str = "") -> None:
        current = self._conn_state.get(device_id, ConnectionState.DISCONNECTED.value)
        allowed = self._VALID_TRANSITIONS.get(current, set())
        if target in allowed:
            self._conn_state[device_id] = target
            try:
                loop = asyncio.get_running_loop()
                task = loop.create_task(self._set_connection_state(device_id, target, reason))
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
            except RuntimeError as e:
                logger.debug(
                    "[modbus_tcp] transition_state failed: %s", e
                )  # FIXED-P2: 原问题：异常被静默吞没，添加日志记录
            logger.info("[modbus_tcp] device=%s state: %s->%s reason=%s", device_id, current, target, reason)
        elif target == current:
            return
        else:
            self._conn_state[device_id] = target
            try:
                loop = asyncio.get_running_loop()
                task = loop.create_task(self._set_connection_state(device_id, target, reason))
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
            except RuntimeError as e:
                logger.debug(
                    "[modbus_tcp] transition_state failed: %s", e
                )  # FIXED-P2: 原问题：异常被静默吞没，添加日志记录
            logger.warning(
                "[modbus_tcp] device=%s state: %s->%s (forced) reason=%s", device_id, current, target, reason
            )
        if current != target and self._event_bus:
            event = DeviceStatusEvent(
                device_id=device_id,
                old_status=current,
                new_status=target,
            )
            try:
                task = asyncio.get_running_loop().create_task(self._event_bus.publish(event))
                # FIXED-P2: 事件发布任务纳入后台任务管理，stop()时能正确取消
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
            except RuntimeError as e:
                logger.debug(
                    "[modbus_tcp] transition_state failed: %s", e
                )  # FIXED-P2: 原问题：异常被静默吞没，添加日志记录

    def _calc_backoff_delay(self, pool_key: str, device_id: str = "") -> float:
        config = self._device_configs.get(device_id, {})
        max_delay = float(config.get("max_retry_interval", self._BACKOFF_MAX_DELAY))
        count = min(
            self._pool_backoff.get(pool_key, 0), 20
        )  # FIXED-P1: 限制指数退避上限为2^20，防止count过大导致2**count溢出或延迟过大
        delay = min(1.0 * (2**count), max_delay)
        if config.get("jitter_enable", True):
            jitter = random.uniform(0, 1.0)
            return delay + jitter
        return delay

    def _resolve_active_host(self, device_id: str) -> str:
        if self._redundancy_mgr:
            return self._redundancy_mgr.get_active_host(device_id)
        config = self._device_configs.get(device_id, {})
        primary = config.get("host", "127.0.0.1")
        backup = config.get("host_backup")
        active = self._active_host.get(device_id, primary)
        if active != primary and active != backup:
            self._active_host[device_id] = primary
            active = primary
        if active == primary:
            fail_count = self._primary_fail_count.get(device_id, 0)
            if fail_count >= self._PRIMARY_FAIL_THRESHOLD and backup:
                self._active_host[device_id] = backup
                logger.warning(
                    "[modbus_tcp] device=%s primary failed %d times, switching to backup %s",
                    device_id,
                    fail_count,
                    backup,
                )
                return backup
        return active

    _WRITE_RATE_LIMIT_DEFAULT = 1.0
    _WRITE_VERIFY_DELAY_MS = 50

    def _check_write_rate_limit(self, device_id: str, point_name: str, min_interval: float) -> bool:
        key = (device_id, point_name)
        now = time.monotonic()
        last = self._last_write_time.get(key, 0.0)
        if now - last < min_interval:
            return False
        self._last_write_time[key] = now
        return True

    def _check_write_value_range(self, value: Any, clamp: dict | None) -> bool:
        # FIXED(严重): NaN与任何数比较都返回False，会绕过min/max校验导致NaN写入设备；
        # Inf同样会绕过校验，需一并拒绝
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return False
        if clamp is None or not isinstance(value, (int, float)):
            return True
        min_val = clamp.get("min")
        max_val = clamp.get("max")
        if min_val is not None and value < min_val:
            return False
        return not (max_val is not None and value > max_val)

    def _audit_write(
        self,
        device_id: str,
        point_name: str,
        old_value: Any,
        new_value: Any,
        result: str,
        error_msg: str = "",
        user: str = "",
    ) -> None:
        config = self._device_configs.get(device_id, {})
        if not config.get("write_audit", True):
            return
        # SEC-FIX-V04: 若未显式传入 user，优先从 contextvars 读取（协程级隔离），
        # 再 fallback 到驱动实例属性 _current_write_user
        # FIXED(严重): 原问题-_current_write_user 是实例属性，多协程并发写入同一设备时
        # 会互相覆盖，导致审计日志记录错误用户；改用 contextvars 隔离协程上下文
        if not user:
            try:
                from edgelite.services.device_service import _current_write_user_var

                user = _current_write_user_var.get("")
            except Exception as ctx_err:
                logger.debug("contextvars user lookup failed: %s", ctx_err)
        if not user:
            user = getattr(self, "_current_write_user", "")
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "user": user,
            "device_id": device_id,
            "point_id": point_name,
            "old_value": old_value,
            "new_value": new_value,
            "result": result,
            "error_msg": error_msg,
        }
        self._write_audit_log.append(entry)

    async def _read_verify_write(
        self,
        client: AsyncModbusTcpClient,
        slave_id: int,
        address: int,
        data_type: str,
        byte_order: str,
        expected_value: Any,
        device_id: str,
    ) -> bool:
        await asyncio.sleep(self._WRITE_VERIFY_DELAY_MS / 1000.0)
        _set_client_slave_id(client, slave_id)
        n_regs = DATA_TYPE_REGS.get(data_type, 1)
        reg_type = "coil" if data_type == "bool" else "holding"
        try:
            if reg_type == "coil":
                result = (
                    await asyncio.wait_for(  # FIXED-P0: 写后回读验证添加超时保护，防止TCP挂起时持有client租用无法释放
                        client.read_coils(address, **_read_kwargs(n_regs, slave_id)),
                        timeout=5.0,
                    )
                )
                if result.isError():
                    return False
                if not result.bits:  # FIXED-P1: 检查bits非空，防止IndexError导致写入验证被静默跳过
                    return False
                read_value = bool(result.bits[0])
                return read_value == bool(expected_value)
            else:
                result = await asyncio.wait_for(  # FIXED-P0: 写后回读验证添加超时保护
                    client.read_holding_registers(address, **_read_kwargs(n_regs, slave_id)),
                    timeout=5.0,
                )
                if result.isError():
                    return False
                read_value = self._decode_point_value(result.registers, data_type, byte_order)
                if isinstance(expected_value, float) and isinstance(read_value, float):
                    return abs(read_value - expected_value) < 1e-6
                return read_value == expected_value
        except Exception as e:
            logger.warning("[modbus_tcp] write verify failed: %s", e)  # FIXED-P1: 原问题：写入验证异常返回False无日志
            return False

    def get_write_audit_log(self, device_id: str | None = None, limit: int = 100) -> list[dict]:
        if device_id is None:
            return list(self._write_audit_log)[-limit:]
        return [e for e in self._write_audit_log if e["device_id"] == device_id][-limit:]

    def _log_error(self, device_id: str, error_code: str, message: str, exc_info: bool = False) -> None:
        i18n_msg = _t(error_code)
        # CROSS-004: 添加 exc_info=True 以包含异常堆栈，便于生产环境故障排查
        logger.error(
            "[%s] device=%s code=%s i18n=%s msg=%s",
            self.plugin_name,
            device_id,
            error_code,
            i18n_msg,
            message,
            exc_info=exc_info,
        )

    def _cache_point_value(self, cache_key: tuple[str, str], value: Any) -> None:  # FIXED-P2: LRU缓存写入辅助方法
        self._single_point_cache[cache_key] = value
        self._single_point_cache.move_to_end(cache_key)
        while len(self._single_point_cache) > self._SINGLE_POINT_CACHE_MAX:
            self._single_point_cache.popitem(last=False)

    async def _read_single_point(
        self,
        client: AsyncModbusTcpClient,
        slave_id: int,
        pt_def: dict,
        byte_order: str = "ABCD",
        device_id: str = "",
        point_name: str = "",
    ) -> Any:
        """读取单个测点（MTCP-007: 添加缓存和快速失败机制）"""
        address = int(pt_def.get("address", 0))
        data_type = pt_def.get("data_type", "float32")
        reg_type = pt_def.get("register_type", "holding")
        reg_count = DATA_TYPE_REGS.get(data_type, 1)
        cache_key = (device_id, point_name) if point_name else None

        _set_client_slave_id(client, slave_id)

        # 记录发送包
        tx_data = f"FC=read addr={address} slave={slave_id} type={reg_type} count={reg_count}"
        record_packet("tx", self.plugin_name, device_id, tx_data)

        if reg_type == "coil":
            result = await asyncio.wait_for(  # FIXED-P2: 初始读取添加超时保护，防止TCP连接挂起导致采集线程无限阻塞
                client.read_coils(address, **_read_kwargs(reg_count, slave_id)),
                timeout=10.0,
            )
            if result.isError():
                exc_desc = _parse_modbus_exception(result)
                if exc_desc:
                    logger.warning("[modbus_tcp] device=%s Modbus exception: %s", device_id, exc_desc)
                raise ModbusException(f"读取错误: {result}")
            rx_data = f"FC=read addr={address} bits={result.bits[:reg_count]}"
            record_packet("rx", self.plugin_name, device_id, rx_data)
            if not result.bits:
                raise ValueError("coil读取结果bits为空")  # FIXED-P2: coil读取结果bits边界检查
            value = bool(result.bits[0])
            # MTCP-007: 缓存成功值
            if cache_key:
                self._cache_point_value(cache_key, value)  # FIXED-P2: 使用LRU缓存写入
            return value
        elif reg_type == "discrete":
            result = await asyncio.wait_for(  # FIXED-P2: 初始读取添加超时保护
                client.read_discrete_inputs(address, **_read_kwargs(reg_count, slave_id)),
                timeout=10.0,
            )
            if result.isError():
                exc_desc = _parse_modbus_exception(result)
                if exc_desc:
                    logger.warning("[modbus_tcp] device=%s Modbus exception: %s", device_id, exc_desc)
                raise ModbusException(f"读取错误: {result}")
            rx_data = f"FC=read addr={address} bits={result.bits[:reg_count]}"
            record_packet("rx", self.plugin_name, device_id, rx_data)
            if not result.bits:
                raise ValueError("discrete读取结果bits为空")  # FIXED-P2: discrete读取结果bits边界检查
            value = bool(result.bits[0])
            # MTCP-007: 缓存成功值
            if cache_key:
                self._cache_point_value(cache_key, value)  # FIXED-P2: 使用LRU缓存写入
            return value
        elif reg_type == "input":
            result = await asyncio.wait_for(  # FIXED-P2: 初始读取添加超时保护
                client.read_input_registers(address, **_read_kwargs(reg_count, slave_id)),
                timeout=10.0,
            )
        else:
            result = await asyncio.wait_for(  # FIXED-P2: 初始读取添加超时保护
                client.read_holding_registers(address, **_read_kwargs(reg_count, slave_id)),
                timeout=10.0,
            )

        if result.isError():
            # 解析Modbus异常码
            exc_desc = _parse_modbus_exception(result)
            if exc_desc:
                logger.warning("[modbus_tcp] device=%s Modbus exception: %s", device_id, exc_desc)
                # 0x84/0x86: 服务端故障/忙，可重试
                if exc_desc.startswith("Server Device Failure") or exc_desc.startswith("Server Device Busy"):
                    # MTCP-007: 总超时10秒，指数退避1s, 2s, 4s
                    retry_start = time.monotonic()
                    for _idx, delay in enumerate(
                        _SINGLE_POINT_RETRY_DELAYS
                    ):  # FIXED(P3): 原问题-B007循环变量idx未使用; 修复-改为_idx
                        if time.monotonic() - retry_start > _SINGLE_POINT_RETRY_TIMEOUT:
                            logger.warning(
                                "[modbus_tcp] device=%s retry loop exceeded %ds, aborting",
                                device_id,
                                _SINGLE_POINT_RETRY_TIMEOUT,
                            )
                            break
                        await asyncio.sleep(delay)
                        # FIXED-P1: 单次I/O超时不超过剩余预算，防止总耗时远超SINGLE_POINT_RETRY_TIMEOUT
                        _remaining_budget = max(1.0, _SINGLE_POINT_RETRY_TIMEOUT - (time.monotonic() - retry_start))
                        try:
                            if reg_type == "input":
                                retry_result = await asyncio.wait_for(
                                    client.read_input_registers(address, **_read_kwargs(reg_count, slave_id)),
                                    timeout=_remaining_budget,
                                )
                            else:
                                retry_result = await asyncio.wait_for(
                                    client.read_holding_registers(address, **_read_kwargs(reg_count, slave_id)),
                                    timeout=_remaining_budget,
                                )
                        except TimeoutError:
                            continue
                        if not retry_result.isError():
                            rx_data = f"FC=read addr={address} regs={retry_result.registers}"
                            record_packet("rx", self.plugin_name, device_id, rx_data)
                            registers = retry_result.registers
                            # MTCP-007: 缓存成功读取的值用于后续返回
                            decoded = self._decode_point_value(registers, data_type, byte_order)
                            if cache_key:
                                self._cache_point_value(cache_key, decoded)  # FIXED-P2: 使用LRU缓存写入
                            return decoded
            raise ModbusException(f"读取错误: {result}")

        registers = result.registers
        rx_data = f"FC=read addr={address} regs={registers}"
        record_packet("rx", self.plugin_name, device_id, rx_data)

        if data_type == "bool":
            if len(registers) < 1:
                raise ModbusException("Insufficient registers for bool")
            value = bool(registers[0])
        elif data_type == "int16":
            if len(registers) < 1:
                raise ModbusException("Insufficient registers for int16")
            val = registers[0]
            value = val if val < 32768 else val - 65536
        elif data_type == "uint16":
            if len(registers) < 1:
                raise ModbusException("Insufficient registers for uint16")
            value = registers[0]
        elif data_type == "int32":
            value = self._decode_registers(registers, byte_order, "i", 2)
        elif data_type == "uint32":
            value = self._decode_registers(registers, byte_order, "I", 2)
        elif data_type == "float32":
            value = self._decode_registers(registers, byte_order, "f", 2)
        elif data_type == "float64":
            value = self._decode_registers(registers, byte_order, "d", 4)
        elif data_type == "string":  # FIXED-P0: string类型使用_decode_point_value解码，之前落入else返回整数
            value = self._decode_point_value(registers, data_type, byte_order)
        else:
            if len(registers) < 1:
                raise ModbusException("Insufficient registers")
            value = registers[0]

        # MTCP-007: 缓存成功值
        if cache_key:
            self._cache_point_value(cache_key, value)  # FIXED-P2: 使用LRU缓存写入
        return value

    async def _try_reconnect(self, device_id: str) -> None:
        # FIXED-P1: 熔断检查，若设备已熔断则跳过重连，等watchdog探测恢复
        if device_id in self._circuit_open:
            return
        config = self._device_configs.get(device_id, {})
        max_attempts = config.get("max_reconnect_attempts", self._MAX_RECONNECT_ATTEMPTS)

        self._transition_state(device_id, ConnectionState.DISCONNECTED.value, "reconnect in progress")

        async with self._retry_lock:
            count = self._retry_count.get(device_id, 0)
            if count >= max_attempts:
                self._transition_state(
                    device_id, ConnectionState.OFFLINE.value, f"exceeded max reconnect attempts ({max_attempts})"
                )
                self._log_error(
                    device_id, ModbusDriverErrors.CONN_FAILED, f"exceeded max reconnect attempts ({max_attempts})"
                )
                self._circuit_open.add(device_id)  # FIXED-P1: 熔断，不再递归调度_delayed_reconnect
                self._retry_count[device_id] = 0
                logger.warning("[modbus_tcp] circuit open for device=%s, watchdog will probe recovery", device_id)
                return
            self._retry_count[device_id] = count + 1

            stats = self._health_stats.get(device_id)
            if stats:
                stats.total_reconnects += 1

            pool_key = self._device_pool_key.get(device_id, "")
            delay = self._calc_backoff_delay(pool_key, device_id)
            self._pool_backoff[pool_key] = self._pool_backoff.get(pool_key, 0) + 1

        await asyncio.sleep(delay)

        # FIXED-P0: sleep后重新获取_retry_lock进行二次检查，防止并发重连竞态导致重复连接
        async with self._retry_lock:
            if self._conn_state.get(device_id) == ConnectionState.CONNECTED.value:
                return
            if not config:
                return

            self._transition_state(device_id, ConnectionState.CONNECTING.value, "attempting reconnect")

            host = self._resolve_active_host(device_id)
            port = config.get("port", 502)
            timeout = config.get("timeout", 5.0)

            new_client = AsyncModbusTcpClient(host=host, port=port, timeout=timeout)

        # FIXED-P0: MT-02 将网络I/O移到_retry_lock锁外，避免阻塞其他设备重连
        try:
            connected = await asyncio.wait_for(new_client.connect(), timeout=timeout)
        except asyncio.CancelledError:
            # FIXED-P1: CancelledError时也必须close新client，防止TCP socket泄漏
            try:
                new_client.close()
            except Exception as e:
                logger.warning("[modbus_tcp] operation failed: %s", e)  # FIXED-P2: 原问题：异常被静默吞没，添加日志记录
            raise
        except Exception as e:
            try:
                new_client.close()
            except Exception as e2:
                logger.debug("Modbus TCP reconnect close failed: %s", e2)
            self._primary_fail_count[device_id] = self._primary_fail_count.get(device_id, 0) + 1
            if self._redundancy_mgr:
                self._redundancy_mgr.record_failure(device_id)
            self._transition_state(device_id, ConnectionState.DISCONNECTED.value, str(e))
            self._log_error(device_id, ModbusDriverErrors.CONN_FAILED, str(e), exc_info=True)
            return

        async with self._retry_lock:
            if connected:
                # 重新检查状态，防止连接期间另一重连已成功
                if self._conn_state.get(device_id) == ConnectionState.CONNECTED.value:
                    try:
                        new_client.close()
                    except Exception as e:
                        logger.warning(
                            "[modbus_tcp] operation failed: %s", e
                        )  # FIXED-P2: 原问题：异常被静默吞没，添加日志记录
                    return
                new_pool_key = f"{host}:{port}"
                async with self._pool_lock:
                    old_pool_key = self._device_pool_key.get(device_id)
                    if old_pool_key and old_pool_key in self._connection_pool:
                        old_client, old_ref = self._connection_pool[old_pool_key]
                        if old_ref <= 1:
                            del self._connection_pool[old_pool_key]
                            if old_client.connected:
                                # FIXED-P1: 直接检查_leased_clients替代_can_close_client，避免_pool_lock内嵌套获取_lease_lock
                                if old_client not in self._leased_clients:
                                    old_client.close()
                                else:
                                    self._stale_clients[old_client] = time.monotonic()
                        else:
                            # MTCP-002: 引用计数>0时，标记client为过期，等最后一个引用释放时关闭
                            # MTCP-MED-002: 记录stale client的添加时间
                            self._stale_clients[old_client] = time.monotonic()
                            self._connection_pool[old_pool_key] = (old_client, old_ref - 1)

                    # FIXED-P0: 检查pool中是否已有同一pool_key的新client（其他设备刚重连成功），避免竞态震荡
                    existing_client, existing_ref = self._connection_pool.get(new_pool_key, (None, 0))
                    if existing_client is not None and existing_client is not new_client and existing_client.connected:
                        # 其他设备已重连成功且client仍可用，复用该client而非替换
                        self._clients[device_id] = existing_client
                        self._connection_pool[new_pool_key] = (existing_client, existing_ref + 1)
                        self._device_pool_key[device_id] = new_pool_key
                        try:
                            new_client.close()
                        except Exception as e:
                            logger.warning(
                                "[modbus_tcp] operation failed: %s", e
                            )  # FIXED-P2: 原问题：异常被静默吞没，添加日志记录
                    else:
                        self._clients[device_id] = new_client
                        new_ref = existing_ref + 1
                        self._connection_pool[new_pool_key] = (new_client, new_ref)
                        self._device_pool_key[device_id] = new_pool_key

                self._retry_count[device_id] = 0
                self._circuit_open.discard(device_id)  # FIXED-P1: 重连成功，解除熔断
                self._pool_backoff[new_pool_key] = 0
                self._primary_fail_count[device_id] = 0
                self._active_host[device_id] = host
                self._watchdog_fail_count[device_id] = 0
                if self._redundancy_mgr:
                    self._redundancy_mgr.record_success(device_id)
                self._transition_state(device_id, ConnectionState.CONNECTED.value, f"reconnected to {host}:{port}")
                self._log_error(device_id, ModbusDriverErrors.RECONNECT_OK, f"reconnected to {host}:{port}")
            else:
                try:
                    new_client.close()
                except Exception as e:
                    logger.debug("Modbus TCP reconnect close failed: %s", e)
                self._primary_fail_count[device_id] = self._primary_fail_count.get(device_id, 0) + 1
                if self._redundancy_mgr:
                    self._redundancy_mgr.record_failure(device_id)
                self._transition_state(
                    device_id, ConnectionState.DISCONNECTED.value, f"connect failed to {host}:{port}"
                )
                self._log_error(device_id, ModbusDriverErrors.CONN_FAILED, f"connect failed to {host}:{port}")

    def _start_watchdog(self, device_id: str) -> None:
        """启动设备连接看门狗（MTCP-003: 每个设备独立的watchdog task）"""
        self._watchdog_fail_count[device_id] = 0
        existing_task = self._device_watchdog_tasks.get(device_id)
        if existing_task is None or existing_task.done():
            self._device_watchdog_tasks[device_id] = asyncio.ensure_future(self._watchdog_loop(device_id))

    def _stop_watchdog(self, device_id: str) -> None:
        """停止设备连接看门狗（MTCP-003: 只取消指定设备的task）"""
        self._watchdog_fail_count.pop(device_id, None)
        task = self._device_watchdog_tasks.pop(device_id, None)
        if task and not task.done():
            task.cancel()

    async def _cleanup_stale_clients(self) -> None:
        """MTCP-MED-002: 清理过期的stale clients，释放socket 资源

        检查所有stale clients，如果超过_STALE_CLIENT_TIMEOUT 秒仍未被释放，
        则强制关闭并从集合中移除。
        """
        if not self._stale_clients:
            return

        now = time.monotonic()
        stale_to_close: list[AsyncModbusTcpClient] = []

        async with self._lease_lock:
            for client, added_at in list(self._stale_clients.items()):
                # 检查是否超时
                if now - added_at >= self._STALE_CLIENT_TIMEOUT:
                    stale_to_close.append(client)

        if stale_to_close:
            closed_count = 0
            async with self._lease_lock:  # FIXED-P0: 在lease_lock内完成租用检查和关闭，与_stale_client_cleanup_loop一致，防止关闭期间被另一协程租用
                for client in stale_to_close:
                    try:
                        if client in self._leased_clients:
                            continue
                        if client.connected:
                            client.close()
                            closed_count += 1
                    except Exception as e:
                        logger.debug("[modbus_tcp] stale client close error: %s", e)
                    self._stale_clients.pop(client, None)
            # FIXED-P1: 与_stale_client_cleanup_loop保持一致，关闭stale client后同步清理连接池引用
            if closed_count > 0:
                async with self._pool_lock:
                    expired_keys = [k for k, (c, _) in self._connection_pool.items() if c in stale_to_close]
                    for k in expired_keys:
                        self._connection_pool.pop(k, None)
                logger.info(
                    "[modbus_tcp] MTCP-MED-002: cleaned up %d stale clients (timeout=%.0fs)",
                    closed_count,
                    self._STALE_CLIENT_TIMEOUT,
                )

    async def _watchdog_loop(self, device_id: str) -> None:
        """设备连接看门狗循环（MTCP-003: 每个设备独立的watchdog loop）

        MTCP-LOW-001: 支持可配置的断连检测阈值
        """
        _WATCHDOG_INTERVAL = 10  # 检测间隔（秒）

        while self._running:
            await asyncio.sleep(_WATCHDOG_INTERVAL)
            if not self._running:
                break
            # MTCP-MED-002: 每次循环检查并清理过期的stale clients
            await self._cleanup_stale_clients()
            # 检查该task是否已被移除（设备已停止）
            if device_id not in self._device_watchdog_tasks:
                break
            try:
                # MTCP-LOW-001: 从设备配置读取阈值
                device_config = self._device_configs.get(device_id, {})
                threshold_seconds = device_config.get("watchdog_threshold", _WATCHDOG_DISCONNECT_THRESHOLD)
                fail_count_limit = max(1, round(threshold_seconds / _WATCHDOG_INTERVAL))

                client = self._clients.get(device_id)
                try:
                    is_connected = client is not None and client.connected
                except Exception:
                    is_connected = False
                if not is_connected:
                    self._watchdog_fail_count[device_id] = self._watchdog_fail_count.get(device_id, 0) + 1
                    state = self._conn_state.get(device_id, ConnectionState.DISCONNECTED.value)
                    if state == ConnectionState.CONNECTED.value:
                        self._transition_state(
                            device_id, ConnectionState.DISCONNECTED.value, "watchdog detected disconnect"
                        )
                    if self._watchdog_fail_count[device_id] >= fail_count_limit:  # MTCP-LOW-001: 使用配置的阈值
                        logger.warning(
                            "[modbus_tcp] device=%s disconnected for >%ds, triggering reconnect",
                            device_id,
                            threshold_seconds,
                        )
                        self._watchdog_fail_count[device_id] = 0
                        # FIXED-P2: 非阻塞调度重连，避免watchdog阻塞
                        _rtask = self._reconnect_tasks.get(device_id)
                        if not _rtask or _rtask.done():
                            self._reconnect_tasks[device_id] = asyncio.create_task(self._try_reconnect(device_id))
                else:
                    self._watchdog_fail_count[device_id] = 0
                    # FIXED-P1: 心跳成功时解除熔断，允许后续重连
                    self._circuit_open.discard(device_id)
                    state = self._conn_state.get(device_id, ConnectionState.DISCONNECTED.value)
                    if state != ConnectionState.CONNECTED.value:
                        self._transition_state(device_id, ConnectionState.CONNECTED.value, "watchdog detected recovery")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # CROSS-002: 分级处理异常，不静默吞没
                logger.warning("[modbus_tcp] watchdog loop exception: device=%s error=%s", device_id, e, exc_info=True)
                if not self._handle_watchdog_exception(e, f"modbus_tcp_watchdog_{device_id}"):
                    break

    async def check_permission(self, permission: Permission) -> bool:  # FIXED-P1: 权限检查方法，与RTU驱动一致
        async with self._role_lock:
            return has_permission(self._current_user_role, permission)

    async def set_user_role(
        self, role: str
    ) -> None:  # FIXED-P0: 改为async并使用_role_lock，与RTU驱动和check_permission一致
        async with self._role_lock:
            self._current_user_role = role

    async def health_check(self, device_id: str) -> bool:
        """Modbus TCP健康检查：尝试读取单个寄存器验证连接"""
        client = self._clients.get(device_id)
        if client is None or not client.connected:
            return False
        if not await self._lease_client(
            client
        ):  # FIXED-P0: 添加租用保护，防止health_check期间client被另一协程关闭导致use-after-close
            return False
        try:
            config = self._device_configs.get(device_id, {})
            slave_id = config.get("slave_id", 1)
            _set_client_slave_id(client, slave_id)
            result = await asyncio.wait_for(
                client.read_holding_registers(0, **_read_kwargs(1, slave_id)),
                timeout=3.0,
            )
            return not result.isError()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(
                "[modbus_tcp] health_check failed: %s", e
            )  # FIXED-P2: 原问题-日志消息误写为"write_point failed"，实际是health_check方法
            return False
        finally:
            await self._release_client(client)

    def get_redundancy_status(self, device_id: str) -> dict | None:
        if not self._redundancy_mgr:
            return None
        return self._redundancy_mgr.get_status(device_id)

    async def probe_primary_link(self, device_id: str) -> bool:
        if not self._redundancy_mgr:
            return False
        config = self._device_configs.get(device_id, {})
        if not config:
            return False
        primary_host = config.get("host", "127.0.0.1")
        port = config.get("port", 502)
        timeout = config.get("timeout", 5.0)
        slave_id = config.get("slave_id", 1)
        probe_client = AsyncModbusTcpClient(host=primary_host, port=port, timeout=timeout)
        try:
            connected = await asyncio.wait_for(
                probe_client.connect(), timeout=timeout
            )  # FIXED-P0: 探测连接添加超时保护
            if connected:
                _set_client_slave_id(probe_client, slave_id)
                result = await asyncio.wait_for(  # FIXED-P0: 探测读取添加超时保护
                    probe_client.read_holding_registers(0, **_read_kwargs(1, slave_id)),
                    timeout=5.0,
                )
                if not result.isError():
                    self._redundancy_mgr.mark_primary_healthy(device_id)
                    return True
            return False
        except Exception as e:
            logger.warning("[modbus_tcp] probe_primary_link failed: %s", e)  # FIXED-P1: 原问题：探测异常返回False无日志
            return False
        finally:
            try:
                probe_client.close()
            except Exception as e:
                logger.warning(
                    "[modbus_tcp] probe_primary_link failed: %s", e
                )  # FIXED-P2: 原问题：异常被静默吞没，添加日志记录

    def _publish_point_updates(self, device_id: str, result: dict[str, Any]) -> None:
        if not self._event_bus:
            return
        for point_name, value in result.items():
            pv = value
            quality = "good"
            if isinstance(value, PointValue):
                pv = value.value
                quality = value.quality
            if pv is None:
                continue
            event = PointUpdateEvent(
                device_id=device_id,
                point_name=point_name,
                value=float(pv) if isinstance(pv, (int, float)) else None,
                quality=quality,
            )
            try:
                task = asyncio.get_running_loop().create_task(self._event_bus.publish(event))
                # FIXED-P2: 事件发布任务纳入后台任务管理，stop()时能正确取消
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
            except RuntimeError as e:
                logger.debug(
                    "[modbus_tcp] publish_point_updates failed: %s", e
                )  # FIXED-P2: 原问题：异常被静默吞没，添加日志记录

    async def _evaluate_edge_rules(self, device_id: str, result: dict[str, Any]) -> None:
        if not self._edge_rule_engine:
            return
        for point_name, value in result.items():
            pv = value
            quality = "good"
            if isinstance(value, PointValue):
                pv = value.value
                quality = value.quality
            if pv is None or not isinstance(pv, (int, float)):
                continue
            await self._edge_rule_engine.evaluate_point(device_id, point_name, float(pv), quality)

    async def _async_write_point(self, device_id: str, point: str, value: Any) -> bool:
        return await self.write_point(device_id, point, value)

    def add_edge_rule(self, rule: EdgeRule) -> None:
        if self._edge_rule_engine:
            self._edge_rule_engine.add_rule(rule)
        if self._rule_store:
            self._rule_store.save_rule(rule)

    async def remove_edge_rule(self, rule_id: str) -> EdgeRule | None:
        rule = None
        if self._edge_rule_engine:
            rule = await self._edge_rule_engine.remove_rule(rule_id)
        if self._rule_store:
            self._rule_store.delete_rule(rule_id)
        return rule

    async def update_edge_rule(self, rule_id: str, updates: dict) -> bool:
        ok = False
        if self._edge_rule_engine:
            ok = await self._edge_rule_engine.update_rule(rule_id, updates)
        if ok and self._rule_store:
            rule = self._edge_rule_engine.get_rule(rule_id)
            if rule:
                self._rule_store.save_rule(rule)
        return ok

    def get_edge_rules(self) -> list[dict]:
        if self._edge_rule_engine:
            return self._edge_rule_engine.get_all_rules()
        return []

    def get_edge_rule_status(self) -> dict:
        result = {}
        if self._edge_rule_engine:
            result["rule_engine"] = self._edge_rule_engine.get_stats()
            result["active_alarms"] = self._edge_rule_engine.get_active_alarms()
        if self._edge_trigger:
            result["trigger"] = self._edge_trigger.get_stats()
        return result

    async def rollback_edge_rule(self, rule_id: str, version: int) -> EdgeRule | None:
        if not self._rule_store:
            return None
        rule = self._rule_store.rollback(rule_id, version)
        if rule and self._edge_rule_engine:
            # FIXED-P0: remove_rule是协程，必须await（与remove_edge_rule/update_edge_rule一致）
            await self._edge_rule_engine.remove_rule(rule_id)
            self._edge_rule_engine.add_rule(rule)
        return rule

    async def query_ts(
        self,
        device_id: str,
        point_name: str,
        start_time: datetime,
        end_time: datetime | None = None,
        quality: str | None = None,
        aggregate: str | None = None,
        window_seconds: int | None = None,
        limit: int = 10000,
    ) -> list[dict]:
        if not self._ts_store:
            return []
        return await self._ts_store.query(
            device_id,
            point_name,
            start_time,
            end_time,
            quality=quality,
            aggregate=aggregate,
            window_seconds=window_seconds,
            limit=limit,
        )

    async def query_ts_latest(self, device_id: str, point_names: list[str]) -> dict[str, dict]:
        if not self._ts_store:
            return {}
        return await self._ts_store.query_latest(device_id, point_names)

    async def query_ts_by_quality(
        self,
        device_id: str,
        point_name: str,
        start_time: datetime,
        end_time: datetime | None = None,
        quality: str = "bad",
        limit: int = 10000,
    ) -> list[dict]:
        if not self._ts_store:
            return []
        return await self._ts_store.query_by_quality(
            device_id,
            point_name,
            start_time,
            end_time,
            quality=quality,
            limit=limit,
        )

    def set_offline_sync_online(self, online: bool) -> None:
        if self._offline_sync:
            self._offline_sync.set_online(online)

    def set_offline_upload_callback(self, callback) -> None:
        if self._offline_sync:
            self._offline_sync.set_upload_callback(callback)

    async def force_offline_sync(self) -> int:
        if not self._offline_sync:
            return 0
        return await self._offline_sync.force_sync()

    def get_ts_stats(self) -> dict:
        result = {}
        if self._ts_store:
            result["ts_store"] = self._ts_store.get_stats()
        if self._offline_sync:
            result["offline_sync"] = self._offline_sync.get_stats()
        return result

    def list_config_versions(self, limit: int = 20, offset: int = 0) -> list[dict]:
        if not self._config_version:
            return []
        return self._config_version.list_versions(limit, offset)

    def get_config_version(self, version: int) -> dict | None:
        if not self._config_version:
            return None
        return self._config_version.get_version(version)

    def rollback_config_version(self, version: int) -> dict | None:
        if not self._config_version:
            return None
        return self._config_version.rollback(version)

    def diff_config_versions(self, v1: int, v2: int) -> dict:
        if not self._config_version:
            return {}
        return self._config_version.diff_versions(v1, v2)

    def export_config_json(self) -> str:
        if not self._config_version:
            return "{}"
        return self._config_version.export_json()

    def export_config_yaml(self) -> str:
        if not self._config_version:
            return ""
        return self._config_version.export_yaml()

    def import_config_json(self, data: str) -> bool:
        if not self._config_version:
            return False
        return self._config_version.import_json(data)

    def get_audit_recent(self, limit: int = 100) -> list[dict]:
        if not self._audit:
            return []
        return self._audit.get_recent(limit)

    def get_audit_by_device(self, device_id: str, limit: int = 100) -> list[dict]:
        if not self._audit:
            return []
        return self._audit.get_by_device(device_id, limit)

    def export_audit_csv(self, start_time: datetime | None = None, end_time: datetime | None = None) -> str:
        if not self._audit:
            return ""
        return self._audit.export_csv(start_time, end_time)

    def get_point_stats(self, device_id: str, point_name: str) -> dict | None:
        key = (device_id, point_name)
        h = self._point_health.get(key)
        if h is None:
            return None
        self._point_health.move_to_end(key)  # FIXED-P1: LRU淘汰-访问时移到末尾
        total = h.get("total", 0)
        success_count = h.get("success", 0)
        fail_count = h.get("fail", 0)
        success_rate = success_count / total if total > 0 else 1.0
        latencies = h.get("latencies", [])
        avg_latency_ms = sum(latencies) / len(latencies) if latencies else 0.0
        quality_history = list(self._point_quality_history.get(key, []))
        current_quality = quality_history[-1] if quality_history else "good"
        last_success_at = h.get("last_success_at")
        return {
            "success_count": success_count,
            "fail_count": fail_count,
            "total": total,
            "consecutive_fails": h.get("consecutive_fails", 0),
            "success_rate": round(success_rate, 4),
            "avg_latency_ms": round(avg_latency_ms, 2),
            "quality_history": quality_history,
            "current_quality": current_quality,
            "last_success_at": last_success_at,
        }

    def get_latency_history(self, device_id: str, hours: int = 1) -> list[dict]:
        lh = self._device_latency_history.get(device_id, [])
        if not lh:
            return []
        now = time.monotonic()
        cutoff = now - hours * 3600
        result = []
        for ts, val in lh:
            if ts >= cutoff:
                offset = round(now - ts, 1)
                result.append({"time": offset, "value": round(val, 2)})
        return result

    def get_reconnect_history(self, device_id: str, hours: int = 24) -> list[dict]:
        if not self._audit:
            return []
        records = self._audit.get_by_action(ModbusAuditAction.RECONNECT, limit=5000)
        device_records = [r for r in records if r.get("device_id") == device_id]
        now = datetime.now(UTC)
        cutoff = now.timestamp() - hours * 3600
        hourly: dict[str, int] = {}
        for r in device_records:
            ts_str = r.get("timestamp", "")
            try:
                ts = datetime.fromisoformat(ts_str).timestamp()
            except (ValueError, TypeError):
                continue
            if ts < cutoff:
                continue
            try:
                dt = datetime.fromisoformat(ts_str)
                hour_key = dt.strftime("%H:00")
            except (ValueError, TypeError):
                continue
            hourly[hour_key] = hourly.get(hour_key, 0) + 1
        result = []
        for i in range(hours):
            hour_dt = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=i)
            hour_key = hour_dt.strftime("%H:00")
            result.append({"hour": hour_key, "count": hourly.get(hour_key, 0)})
        result.reverse()
        return result
