"""Modbus RTU驱动 - 基于pymodbus串口实现"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import random
import struct
import sys
import threading
import time
from collections import OrderedDict, deque
from datetime import UTC, datetime
from typing import Any

try:
    import pymodbus
    from pymodbus.client import AsyncModbusSerialClient, AsyncModbusTcpClient

    _PYMODBUS_AVAILABLE = True
except ImportError:
    pymodbus = None
    AsyncModbusSerialClient = None
    AsyncModbusTcpClient = None
    _PYMODBUS_AVAILABLE = False

try:
    from pymodbus.exceptions import ModbusException
except ImportError:
    ModbusException = Exception

from edgelite.api.debug import record_packet
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
from edgelite.drivers.modbus_config_version import ModbusConfigVersion
from edgelite.drivers.rule_store import RuleStore
from edgelite.security.rbac import Permission, has_permission
from edgelite.storage.offline_queue import OfflineQueue
from edgelite.storage.ring_buffer import RingBuffer
from edgelite.storage.sqlite_ts import SqliteTimeSeriesStorage

if _PYMODBUS_AVAILABLE:
    _PYMODBUS_MAJOR = int(getattr(pymodbus, "__version__", "2.0.0").split(".")[0])
    _PYMODBUS_MINOR = int(getattr(pymodbus, "__version__", "2.0.0").split(".")[1]) if _PYMODBUS_MAJOR >= 3 else 0
    _PYMODBUS_37_PLUS = _PYMODBUS_MAJOR > 3 or (_PYMODBUS_MAJOR == 3 and _PYMODBUS_MINOR >= 7)
else:
    _PYMODBUS_MAJOR = 3
    _PYMODBUS_MINOR = 0
    _PYMODBUS_37_PLUS = False

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT = 5
READ_TIMEOUT = 30
WRITE_TIMEOUT = 10
_RETRY_TOTAL_TIMEOUT = 10  # FIXED-P2: 重试总超时上限(秒)，防止批量重试长时间阻塞采集线程

# FIXED: pymodbus 3.7+ 不再接受 slave/unit 作为关键字参数，需使用 client.slave_id
_SLAVE_KWARG_NAME: str | None = None

# Modbus异常码映射
_MODBUS_EXCEPTION_CODES = {
    0x81: "Illegal Function (0x01)",
    0x82: "Illegal Data Address (0x02)",
    0x83: "Illegal Data Value (0x03)",
    0x84: "Server Device Failure (0x04)",
    0x85: "Acknowledge (0x05)",
    0x86: "Server Device Busy (0x06)",
    0x87: "Negative Acknowledge (0x07)",
    0x88: "Memory Parity Error (0x08)",
    0x8A: "Gateway Path Unavailable (0x0A)",
    0x8B: "Gateway Target Device Failed (0x0B)",
    0xAB: "Extended Exception (0x2B)",
}

_ERROR_I18N = {
    "CONN_FAILED": {"zh": "连接失败", "en": "Connection failed"},
    "CRC_ERROR": {"zh": "CRC校验失败，触发重连", "en": "CRC check failure, triggering reconnect"},
    "READ_TIMEOUT": {"zh": "读取超时", "en": "Read timeout"},
    "WRITE_TIMEOUT": {"zh": "写入超时", "en": "Write timeout"},
    "WRITE_ERROR": {"zh": "写入错误", "en": "Write error"},
    "PORT_LOCKED": {"zh": "串口被占用", "en": "Serial port is locked"},
    "SERIAL_LOCK_TIMEOUT": {"zh": "串口锁等待超时", "en": "Serial lock wait timeout"},
    "RECONNECT_OK": {"zh": "重连成功", "en": "Reconnected"},
    "CONFIG_INVALID": {"zh": "配置无效", "en": "Invalid configuration"},
    "HEALTH_CHECK_FAILED": {"zh": "健康检查失败", "en": "Health check failed"},
    "PORT_DISAPPEARED": {"zh": "串口消失", "en": "Serial port disappeared"},
    "PORT_REAPPEARED": {"zh": "串口恢复", "en": "Serial port recovered"},
    "BACKOFF_RETRY": {"zh": "退避重试", "en": "Backoff retry"},
    "RATE_OF_CHANGE": {"zh": "变化率超限", "en": "Rate of change exceeded"},
    "VALUE_FROZEN": {"zh": "值冻结", "en": "Value frozen"},
    "VALUE_INVALID": {"zh": "值无效(NaN/Inf)", "en": "Invalid value (NaN/Inf)"},
    "DEGRADED_FREQ": {"zh": "采集频率降级", "en": "Polling frequency degraded"},
    "RECOVERED_FREQ": {"zh": "采集频率恢复", "en": "Polling frequency recovered"},
    "WRITE_CLAMP_REJECT": {"zh": "写值超范围拒绝", "en": "Write value out of clamp range rejected"},
    "WRITE_RATE_LIMIT": {"zh": "写速率限制", "en": "Write rate limited"},
    "WRITE_VERIFY_MISMATCH": {"zh": "写后回读不一致", "en": "Write verify mismatch"},
    "WRITE_BATCH_MERGE": {"zh": "批量写合并", "en": "Batch write merged"},
    "FAILOVER_PORT": {"zh": "串口切换", "en": "Port failover"},
    "FAILOVER_TCP": {"zh": "网关切换", "en": "TCP gateway failover"},
    "FAILOVER_BACK": {"zh": "切回主通道", "en": "Failback to primary"},
    "EDGE_ALARM_FIRE": {"zh": "边缘报警触发", "en": "Edge alarm fired"},
    "EDGE_ALARM_RECOVER": {"zh": "边缘报警恢复", "en": "Edge alarm recovered"},
    "EDGE_RULE_LOAD": {"zh": "规则加载", "en": "Rule loaded"},
    "EDGE_RULE_RELOAD": {"zh": "规则热加载", "en": "Rule hot-reload"},
    "EDGE_RULE_ROLLBACK": {"zh": "规则回滚", "en": "Rule rollback"},
    "EDGE_TRIGGER_EXEC": {"zh": "触发器执行", "en": "Trigger executed"},
    "PERSIST_LOCAL_WRITE": {"zh": "本地持久化写入", "en": "Local persistence write"},
    "PERSIST_OFFLINE_ENQUEUE": {"zh": "离线队列入队", "en": "Offline queue enqueue"},
    "PERSIST_SYNC_BATCH": {"zh": "增量同步批量上传", "en": "Incremental sync batch upload"},
    "PERSIST_SYNC_COMPLETE": {"zh": "同步完成", "en": "Sync completed"},
    "PERSIST_NETWORK_DOWN": {"zh": "网络断开，切换本地存储", "en": "Network down, switching to local storage"},
    "PERSIST_NETWORK_UP": {"zh": "网络恢复，开始续传", "en": "Network recovered, starting resync"},
    "CONFIG_VERSION_SAVE": {"zh": "配置版本保存", "en": "Config version saved"},
    "CONFIG_VERSION_ROLLBACK": {"zh": "配置版本回滚", "en": "Config version rollback"},
    "CONFIG_CHANGE_DENIED": {"zh": "配置变更权限拒绝", "en": "Config change permission denied"},
    "AUDIT_CONFIG_CHANGE": {"zh": "审计-配置变更", "en": "Audit - Config change"},
    "AUDIT_WRITE_POINT": {"zh": "审计-写点操作", "en": "Audit - Write point"},
    "AUDIT_FAILOVER": {"zh": "审计-故障切换", "en": "Audit - Failover"},
}

_READ_FAIL_I18N = {"zh": "Modbus RTU 读取失败", "en": "Modbus RTU read failed"}


def _detect_slave_kwarg_name() -> str | None:
    """根据pymodbus版本号检测正确的slave参数名。
    pymodbus 2.x: slave
    pymodbus 3.0~3.6: unit
    pymodbus 3.7+: slave (per-call传递，与modbus_tcp.py保持一致)
    """
    if _PYMODBUS_MAJOR < 3:
        return "slave"
    if _PYMODBUS_MAJOR == 3 and _PYMODBUS_MINOR < 7:
        return "unit"
    return "slave"  # FIXED-P0: pymodbus 3.7+使用slave参数per-call传递，修复多设备串口slave_id丢失


def _slave_kwarg(slave_id: int) -> dict:
    """返回正确的 Modbus 设备 ID 参数，所有版本均 per-call 传递"""  # FIXED-P0: 与modbus_tcp.py一致，3.7+也per-call传递slave
    if not 1 <= slave_id <= 247:
        raise ValueError(f"Modbus config invalid: slave_id must be 1-247, got {slave_id}")
    global _SLAVE_KWARG_NAME
    if _SLAVE_KWARG_NAME is None:
        _SLAVE_KWARG_NAME = _detect_slave_kwarg_name()
    if _SLAVE_KWARG_NAME is None:
        return {}  # FIXED-P0: 理论上不再触发，3.7+返回"slave"
    return {_SLAVE_KWARG_NAME: slave_id}


def _set_client_slave_id(client: Any, slave_id: int) -> None:
    """为 pymodbus 3.7+ 设置 client.slave_id"""
    if _SLAVE_KWARG_NAME is None and hasattr(client, "slave_id"):
        client.slave_id = slave_id


def _read_kwargs(count: int, slave_id: int) -> dict:
    kwargs = _slave_kwarg(slave_id)
    kwargs["count"] = count  # FIXED: 始终传count
    return kwargs


REGISTER_TYPES = {
    "coil": (0, 1),
    "discrete": (1, 1),
    "holding": (3, 2),
    "input": (4, 2),
}

DATA_TYPE_REGS = {
    "bool": 1,
    "int16": 1,
    "uint16": 1,
    "int32": 2,
    "uint32": 2,
    "float32": 2,
    "float64": 4,
    "string": 1,
}

# 字节序→(寄存器打包格式, 浮点/整数解包格式)
_BYTE_ORDER_FMT = {
    "ABCD": (">", ">"),   # Big-Endian (默认)
    "BADC": ("<", ">"),   # Big-Endian Byte Swap
    "CDAB": (">", "<"),   # Little-Endian Word Swap
    "DCBA": ("<", "<"),   # Little-Endian (完全反转)
}


def _parse_modbus_exception(result: Any) -> str | None:
    """解析Modbus错误响应中的异常码，返回异常码描述或None"""
    try:
        raw = getattr(result, "raw", None) or getattr(result, "value", None)
        if raw and isinstance(raw, (bytes, list)):
            data = raw if isinstance(raw, bytes) else bytes(raw)
            if len(data) >= 2:
                # FIXED-P1: 字典键为异常码|0x80(如0x82=Illegal Data Address)，data[1]为异常码(如0x02)，始终用data[1]|0x80
                exc_code = data[1] | 0x80
                return _MODBUS_EXCEPTION_CODES.get(exc_code)
        err_str = str(result)
        for code, desc in _MODBUS_EXCEPTION_CODES.items():
            if hex(code) in err_str or desc.split("(")[0].strip() in err_str:
                return desc
    except Exception as e:
        logger.debug("[modbus_rtu] Exception code parse failed: %s", e)  # MRTU-LOW-001: 删除冗余pass
    return None


class _CRCReconnectNeeded(Exception):
    """FIXED-P0: CRC错误时不在持有串口锁的情况下触发重连，通过异常让 read_points 在释放锁后处理重连"""
    def __init__(self, message: str, partial_result: dict | None = None):
        super().__init__(message)
        self.partial_result = partial_result or {}


class ModbusRtuDriver(DriverPlugin):

    plugin_name = "modbus_rtu"
    plugin_version = "1.0.0"
    supported_protocols = ("modbus_rtu", "modbus-rtu")  # FIXED(P2): 原问题-可变默认值list; 修复-改为tuple
    _required_dependencies: tuple[str, ...] = ("pymodbus", "serial")  # FIXED(P2): 原问题-可变默认值list; 修复-改为tuple
    _MAX_RECONNECT_ATTEMPTS = 10  # FIXED-P1: 从3提升到10，工业现场网络抖动常见
    _BACKOFF_BASE = 1.0
    _BACKOFF_MAX = 60.0
    _JITTER_MAX = 1.0
    _PORT_MONITOR_INTERVAL = 3.0
    _DEGRADE_LEVELS = (1.0, 5.0, 30.0, 60.0)  # FIXED(P2): 原问题-可变默认值list; 修复-改为tuple
    _DEGRADE_THRESHOLD = 0.8
    _RECOVER_THRESHOLD = 0.95
    _FROZEN_DEFAULT_COUNT = 10
    _POINT_STATS_WINDOW = 100
    _MAX_POINT_STATS = 10000  # FIXED-P1: _point_stats字典容量上限
    _WRITE_VERIFY_DELAY = 0.05
    _WRITE_RATE_LIMIT_INTERVAL = 1.0
    _FAILOVER_FAIL_THRESHOLD = 3
    _FAILOVER_CHECK_INTERVAL = 30
    _FAILOVER_STABLE_THRESHOLD = 3  # FIXED-P1: failback前需连续N次探测成功才切换，防止主链路抖动

    config_schema = {
        "description": "Modbus RTU serial protocol for RS485/RS232 bus devices",
        "required": ["port", "baudrate", "unit_id"],
        "properties": {"port": {"type": "string", "description": "Serial device path"}, "baudrate": {"type": "integer", "description": "Communication baud rate", "minimum": 300, "maximum": 115200}, "unit_id": {"type": "integer", "description": "Device slave address (Unit ID)", "minimum": 1, "maximum": 247}},
        "fields": [
            {"name": "port", "type": "string", "label": "Serial Port", "description": "Serial device path, e.g. COM3 or /dev/ttyUSB0", "default": "/dev/ttyUSB0", "required": True},
            {"name": "baudrate", "type": "integer", "label": "Baud Rate", "description": "Communication baud rate", "default": 9600, "required": True},
            {"name": "parity", "type": "string", "label": "Parity", "description": "Parity check: N/E/O", "default": "N"},
            {"name": "stopbits", "type": "integer", "label": "Stop Bits", "description": "Stop bits: 1 or 2", "default": 1},
            {"name": "bytesize", "type": "integer", "label": "Data Bits", "description": "Data bits: 7 or 8", "default": 8},
            {"name": "unit_id", "type": "integer", "label": "Slave ID", "description": "Device slave address (Unit ID)", "default": 1, "required": True},
            {"name": "timeout", "type": "number", "label": "Timeout (s)", "description": "Connection and read timeout", "default": 3.0},
            {"name": "byte_order", "type": "string", "label": "Byte Order", "description": "Multi-register byte order: ABCD(Big-Endian), BADC, CDAB, DCBA(Little-Endian)", "default": "ABCD", "options": ["ABCD", "BADC", "CDAB", "DCBA"]},
            {"name": "reconnect_interval", "type": "number", "label": "Reconnect Interval (s)", "description": "Seconds between reconnection attempts", "default": 10.0},
            {"name": "max_reconnect_attempts", "type": "integer", "label": "Max Reconnect Attempts", "description": "Maximum consecutive reconnection attempts (default 3)", "default": 3},
            {"name": "batch_read_size", "type": "integer", "label": "Batch Read Size", "description": "Maximum registers per read request (1-125)", "default": 125, "min": 1, "max": 125},
            {"name": "function_code", "type": "string", "label": "Function Code", "description": "Default Modbus function code", "default": "03", "options": ["01", "02", "03", "04", "05", "06", "15", "16"]},
            {"name": "rs485_mode", "type": "boolean", "label": "RS485 Mode", "description": "Enable RS485 half-duplex mode (RTS/CTS control)", "default": False},
            {"name": "rs485_rts_on_send", "type": "boolean", "label": "RS485 RTS On Send", "description": "RTS signal active during send in RS485 mode", "default": True},
            {"name": "rs485_rts_on_recv", "type": "boolean", "label": "RS485 RTS On Receive", "description": "RTS signal active during receive in RS485 mode", "default": False},
            {"name": "rs485_delay_before_send", "type": "integer", "label": "RS485 Delay Before Send (ms)", "description": "Delay in ms before sending in RS485 mode", "default": 0},
            {"name": "rs485_delay_after_send", "type": "integer", "label": "RS485 Delay After Send (ms)", "description": "Delay in ms after sending in RS485 mode", "default": 0},
            {"name": "deadband", "type": "number", "label": "Deadband", "description": "Minimum change threshold for value reporting (0 = disabled)", "default": 0},
            {"name": "scaling", "type": "object", "label": "Scaling", "description": "Linear scaling config: {ratio: 1.0, offset: 0.0}"},
            {"name": "clamp", "type": "object", "label": "Clamp Range", "description": "Valid value range: {min: null, max: null}. Out-of-range values are marked bad"},
            {"name": "language", "type": "string", "label": "Log Language", "description": "Log message language: zh or en", "default": "zh", "options": ["zh", "en"]},
            {"name": "backup_port", "type": "string", "label": "Backup Serial Port", "description": "Backup serial port path for redundancy (e.g. /dev/ttyUSB1 or COM4)"},
            {"name": "tcp_gateway", "type": "object", "label": "TCP-RTU Gateway", "description": "Serial server / TCP-RTU gateway config: {host, port, backup_host}"},
        ],
    }

    experimental = False
    capabilities = DriverCapabilities(discover=False, read=True, write=True, subscribe=False, batch_read=True, batch_write=True)
    constraints = ({"type": "protocol_note", "message": "RTU bus: only one master per bus; shared serial port may conflict"},)  # FIXED(P2): 原问题-可变默认值list; 修复-改为tuple

    def __init__(self):
        super().__init__()
        self._clients: dict[str, Any] = {}
        self._connected: dict[str, bool] = {}
        self._device_points: dict[str, list[dict]] = {}
        self._retry_count: dict[str, int] = {}
        self._retry_lock = asyncio.Lock()
        self._read_fail_tracker: OrderedDict[tuple[str, str], tuple[float, float]] = OrderedDict()  # FIXED-P1: 改用OrderedDict支持LRU淘汰
        self._MAX_TRACKER_ENTRIES = 20000  # FIXED-P2: _read_fail_tracker容量上限
        # _offline_since inherited from base class (dict[str, datetime])  # FIXED-P2: 删除遮蔽基类的覆盖初始化
        self._watchdog_tasks: dict[str, asyncio.Task] = {}  # FIXED-P0: 每设备独立watchdog，避免单设备阻塞所有设备健康检查
        self._watchdog_fail_count: dict[str, int] = {}
        # FIXED-P3: 重连中设备标记集合，防止watchdog跨await重复触发reconnect
        self._reconnecting: set[str] = set()
        self._serial_locks: dict[str, asyncio.Lock] = {}
        self._lock_versions: dict[str, int] = {}  # FIXED-P2: 锁版本号，用于检测锁替换
        # MRTU-001: 锁持有者追踪，检测死锁
        self._lock_holders: dict[str, asyncio.Task] = {}  # {port_path: holder_task}
        self._device_port_map: dict[str, str] = {}
        self._SERIAL_LOCK_TIMEOUT = 5.0
        self._serial_lock_max_retries = 3
        self._serial_lock_retry_base_delay = 0.1
        # CROSS-003: _last_values 使用两层结构，限制设备级容量
        self._MAX_POINTS_PER_DEVICE = 1000
        self._last_values: dict[str, dict[str, Any]] = {}
        self._health_check_fail_count: dict[str, int] = {}
        self._lang: str = "zh"
        self._port_backoff: dict[str, dict] = {}
        self._port_available: dict[str, bool] = {}
        self._port_monitor_task: asyncio.Task | None = None
        self._turnaround_delay: dict[str, float] = {}
        self._port_reconnect_locks: dict[str, asyncio.Lock] = {}
        self._point_stats: dict[tuple[str, str], dict] = {}
        self._degrade_level: dict[str, int] = {}
        self._point_timestamps: dict[tuple[str, str], float] = {}
        self._frozen_counts: dict[tuple[str, str], int] = {}
        self._last_raw_values: dict[str, dict[str, Any]] = {}
        self._write_last_time: dict[tuple[str, str], float] = {}
        self._write_audit_log: deque[dict] = deque(maxlen=1000)
        self._audit_lock: threading.Lock = threading.Lock()  # FIXED-P4: 保护_write_audit_log并发读写
        self._active_port: dict[str, str] = {}
        self._port_fail_count: dict[str, int] = {}
        self._failback_task: asyncio.Task | None = None
        self._failback_stable_count: dict[str, int] = {}  # FIXED-P1: failback稳定计数，连续N次探测成功才切换回主链路
        self._edge_engine: ModbusEdgeRuleEngine | None = None
        self._edge_trigger: EdgeTriggerExecutor | None = None
        self._rule_store: RuleStore | None = None
        self._mqtt_publish_callback = None
        self._sqlite_ts: SqliteTimeSeriesStorage | None = None
        self._offline_queue: OfflineQueue | None = None
        self._ring_buffer: RingBuffer | None = None
        self._persist_enabled: bool = False
        self._network_online: bool = True
        self._sync_task: asyncio.Task | None = None
        self._sync_interval: float = 30.0
        self._sync_batch_size: int = 500
        self._upload_callback = None
        self._persist_stats: dict = {"local_writes": 0, "offline_enqueues": 0, "sync_batches": 0, "sync_records": 0}
        self._config_version: ModbusConfigVersion | None = None
        self._audit: ModbusAudit | None = None
        self._current_user_role: str = "viewer"  # FIXED-P2: 默认角色从admin改为viewer，遵循最小权限原则
        self._role_lock = asyncio.Lock()

    _LOG_INTERVAL = 60.0
    _SERIAL_LOCK_DEFAULT_TIMEOUT = 5.0
    _SERIAL_LOCK_RETRY_BASE_DELAY = 0.1
    _SERIAL_LOCK_MAX_RETRIES = 3
    _SERIAL_DEADLOCK_RESET = True  # MRTU-MED-001: 死锁检测后是否 reset 串口

    class SerialLockContext:
        """串口锁上下文管理器（MRTU-001）：确保锁被正确释放"""

        def __init__(self, driver: ModbusRtuDriver, device_id: str):
            self._driver = driver
            self._device_id = device_id
            self._port_path: str | None = None
            self._lock: asyncio.Lock | None = None
            self._holder_task: asyncio.Task | None = None
            self._acquired_version: int = 0  # FIXED-P2: 锁获取时的版本号，用于释放时校验

        async def __aenter__(self) -> bool:
            """获取锁，成功返回 True"""
            port_path = self._driver._device_port_map.get(self._device_id)
            if not port_path:
                return False

            lock = self._driver._get_serial_lock(port_path)
            max_retries = getattr(self._driver, '_serial_lock_max_retries', self._driver._SERIAL_LOCK_MAX_RETRIES)
            base_delay = getattr(self._driver, '_serial_lock_retry_base_delay', self._driver._SERIAL_LOCK_RETRY_BASE_DELAY)
            timeout = getattr(self._driver, '_SERIAL_LOCK_TIMEOUT', self._driver._SERIAL_LOCK_DEFAULT_TIMEOUT)
            deadlock_reset = getattr(self._driver, '_SERIAL_DEADLOCK_RESET', self._driver._SERIAL_DEADLOCK_RESET)
            # FIXED-BugR4X: 原问题-deadlock_recovered仅在holder.done()块内赋值，第430行使用时可能未定义，修复-循环前初始化为False
            deadlock_recovered = False

            for attempt in range(max_retries + 1):
                try:
                    await asyncio.wait_for(lock.acquire(), timeout=timeout)
                    self._port_path = port_path
                    self._lock = lock
                    self._holder_task = asyncio.current_task()
                    self._acquired_version = self._driver._lock_versions.get(port_path, 0)  # FIXED-P2: 记录获取锁时的版本号
                    # 记录锁持有者
                    self._driver._lock_holders[port_path] = self._holder_task
                    if attempt > 0:
                        logger.debug("[modbus_rtu] device=%s acquired serial lock after %d retries",
                                   self._device_id, attempt)
                    return True
                except TimeoutError:
                    # MRTU-001: 检查锁持有者是否已消失
                    holder = self._driver._lock_holders.get(port_path)
                    if holder is not None and holder.done():
                        # MRTU-MED-001: 检测到疑似死锁，尝试 reset 串口
                        if deadlock_reset:
                            logger.warning("[modbus_rtu] device=%s code=DEADLOCK_DETECTED "
                                        "msg=Stale lock holder detected, attempting serial reset",
                                        self._device_id)
                            reset_ok = await self._driver._reset_device_port(self._device_id)
                            if not reset_ok:
                                logger.error("[modbus_rtu] device=%s code=DEADLOCK_RESET_FAILED "
                                           "msg=Serial reset failed, refusing to force lock acquisition",
                                           self._device_id)
                                return False
                        else:
                            logger.warning("[modbus_rtu] device=%s detected stale lock holder, clearing holder record only",
                                          self._device_id)
                        # FIXED-P0: 死锁恢复后强制释放旧锁并替换为新Lock实例，否则后续acquire()仍会超时导致设备永久不可访问
                        self._driver._lock_holders.pop(port_path, None)
                        try:
                            lock.release()
                        except RuntimeError as e:
                            logger.debug("[modbus_rtu] operation failed: %s", e)
                        new_lock = asyncio.Lock()
                        self._driver._serial_locks[port_path] = new_lock
                        # FIXED-P2: 递增锁版本号，使旧锁持有者感知锁已变更
                        # 之前：替换锁后其他协程仍持有旧Lock引用，RS485总线并发发送帧冲突
                        # 之后：通过版本号机制，旧锁持有者释放后检查版本号，版本不匹配则放弃操作
                        self._driver._lock_versions[port_path] = self._driver._lock_versions.get(port_path, 0) + 1
                        lock = new_lock
                        deadlock_recovered = True

                    if attempt < max_retries:
                        delay = base_delay * (2 ** attempt)
                        logger.debug(
                            "[modbus_rtu] device=%s serial lock timeout (attempt %d/%d), retrying in %.1fs (%s)",
                            self._device_id, attempt + 1, max_retries + 1, delay, port_path
                        )
                        await asyncio.sleep(delay)
                    else:
                        # FIXED: 死锁恢复后创建新锁，最后一次attempt也应尝试获取新锁，避免新锁闲置却返回失败
                        if deadlock_recovered:
                            try:
                                await asyncio.wait_for(lock.acquire(), timeout=timeout)
                                self._port_path = port_path
                                self._lock = lock
                                self._holder_task = asyncio.current_task()
                                self._acquired_version = self._driver._lock_versions.get(port_path, 0)
                                self._driver._lock_holders[port_path] = self._holder_task
                                logger.debug("[modbus_rtu] device=%s acquired serial lock after deadlock recovery",
                                           self._device_id)
                                return True
                            except TimeoutError:
                                pass
                        self._driver._log_error(self._device_id, "SERIAL_LOCK_TIMEOUT",
                            f"Serial port lock timeout after {max_retries + 1} attempts ({port_path}, {timeout}s each)")
                        logger.warning(
                            "[modbus_rtu] device=%s code=SERIAL_LOCK_CONTENTION "
                            "msg=Serial lock contention frequent on %s. Consider: 1) reducing polling intervals, "
                            "2) increasing serial_lock_timeout, 3) using fewer devices per port",
                            self._device_id, port_path
                        )
                        return False
                except BaseException:
                    # FIXED-P1: 原问题-串口死锁时未释放锁，lock.acquire()成功后若发生非TimeoutError异常
                    #           (如CancelledError)，self._lock未赋值导致__aexit__无法释放锁，串口永久不可访问
                    #           修复-使用try-except确保serial lock在异常时也被释放
                    try:
                        lock.release()
                    # FIXED-P1: 原问题-except Exception: pass 静默吞没异常，改为至少 logger.debug
                    except Exception as e:
                        logger.debug("[modbus_rtu] serial lock.release() failed: %s", e)
                    raise
            return False

        async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
            """释放锁"""
            if self._lock is not None and self._port_path is not None:
                current_task = asyncio.current_task()
                if current_task == self._holder_task:
                    self._driver._lock_holders.pop(self._port_path, None)
                    # FIXED-P2: 释放前检查锁版本号，若版本不匹配说明锁已被替换，跳过释放
                    current_version = self._driver._lock_versions.get(self._port_path, 0)
                    if current_version == self._acquired_version:
                        self._lock.release()
                    else:
                        logger.warning("[modbus_rtu] device=%s lock version mismatch on release, skipping (acquired=%d, current=%d)",
                                    self._device_id, self._acquired_version, current_version)
                    logger.debug("[modbus_rtu] device=%s released serial lock", self._device_id)
                else:
                    self._driver._lock_holders.pop(self._port_path, None)
                    logger.warning("[modbus_rtu] device=%s serial lock not released: task mismatch (holder=%s, current=%s)",
                                self._device_id, self._holder_task, current_task)

    def _get_serial_lock(self, port_path: str) -> asyncio.Lock:
        """Get or create an asyncio.Lock for a specific serial port path"""
        if port_path not in self._serial_locks:
            self._serial_locks[port_path] = asyncio.Lock()
        return self._serial_locks[port_path]

    def _acquire_serial_context(self, device_id: str) -> SerialLockContext:
        """获取串口锁上下文管理器（MRTU-001）"""
        return self.SerialLockContext(self, device_id)

    async def _acquire_serial_lock(self, device_id: str) -> asyncio.Lock | None:
        """Acquire serial port lock for the device, with timeout and backoff retry.
        Returns the lock if acquired, None if all retries timed out."""
        port_path = self._device_port_map.get(device_id)
        if not port_path:
            return None
        lock = self._get_serial_lock(port_path)
        max_retries = self._serial_lock_max_retries if hasattr(self, '_serial_lock_max_retries') else self._SERIAL_LOCK_MAX_RETRIES
        base_delay = self._serial_lock_retry_base_delay if hasattr(self, '_serial_lock_retry_base_delay') else self._SERIAL_LOCK_RETRY_BASE_DELAY
        timeout = self._SERIAL_LOCK_TIMEOUT if hasattr(self, '_SERIAL_LOCK_TIMEOUT') else self._SERIAL_LOCK_DEFAULT_TIMEOUT

        for attempt in range(max_retries + 1):
            try:
                await asyncio.wait_for(lock.acquire(), timeout=timeout)
                if attempt > 0:
                    logger.debug("[modbus_rtu] device=%s acquired serial lock after %d retries", device_id, attempt)
                return lock
            except TimeoutError:
                if attempt < max_retries:
                    delay = base_delay * (2 ** attempt)
                    logger.debug(
                        "[modbus_rtu] device=%s serial lock timeout (attempt %d/%d), retrying in %.1fs (%s)",
                        device_id, attempt + 1, max_retries + 1, delay, port_path
                    )
                    await asyncio.sleep(delay)
                else:
                    self._log_error(device_id, "SERIAL_LOCK_TIMEOUT",
                        f"Serial port lock timeout after {max_retries + 1} attempts ({port_path}, {timeout}s each)")
                    logger.warning(
                        "[modbus_rtu] device=%s code=SERIAL_LOCK_CONTENTION "
                        "msg=Serial lock contention frequent on %s. Consider: 1) reducing polling intervals, "
                        "2) increasing serial_lock_timeout, 3) using fewer devices per port",
                        device_id, port_path
                    )
                    return None
        return None

    async def start(self, config: dict) -> None:
        if not _PYMODBUS_AVAILABLE:
            logger.warning("[modbus_rtu] pymodbus未安装，Modbus RTU驱动无法正常工作")
        self._running = True
        self._init_edge_engine()
        self._init_persistence()
        self._init_config_version()
        self._init_audit()
        for device_id in list(self._device_configs.keys()):
            if device_id not in self._clients:
                await self._connect_device(device_id)
        if self._port_monitor_task is None or self._port_monitor_task.done():
            self._port_monitor_task = asyncio.ensure_future(self._port_monitor_loop())
        logger.info("Modbus RTU驱动启动")

    async def stop(self) -> None:
        self._running = False
        tasks_to_wait: list[asyncio.Task] = []

        # FIXED-P0: 取消所有每设备独立watchdog任务
        for _device_id, wd_task in list(self._watchdog_tasks.items()):  # FIXED(P3): 原问题-B007循环变量device_id未使用; 修复-改为_device_id
            if not wd_task.done():
                wd_task.cancel()
                tasks_to_wait.append(wd_task)
        self._watchdog_tasks.clear()

        # MRTU-004: 收集所有后台任务
        if self._port_monitor_task and not self._port_monitor_task.done():
            self._port_monitor_task.cancel()
            tasks_to_wait.append(self._port_monitor_task)
        if self._failback_task and not self._failback_task.done():
            self._failback_task.cancel()
            tasks_to_wait.append(self._failback_task)
        if self._sync_task and not self._sync_task.done():
            self._sync_task.cancel()
            tasks_to_wait.append(self._sync_task)
        # CROSS-004: 添加后台任务到等待列表
        for task in list(self._background_tasks):
            if not task.done():
                task.cancel()
                tasks_to_wait.append(task)

        if tasks_to_wait:
            # 第一轮等待：最多5秒
            done, pending = await asyncio.wait(
                tasks_to_wait, timeout=5.0, return_when=asyncio.ALL_COMPLETED
            )
            for task in done:
                exc = task.exception()
                if exc is not None and not isinstance(exc, asyncio.CancelledError):
                    logger.debug("[modbus_rtu] background task ended with exception: %s", exc)

            # MRTU-004: 等待被 force cancel 的任务完成（最多3秒）
            if pending:
                logger.warning("[modbus_rtu] code=TASK_FORCE_CANCEL msg=%d background task(s) did not terminate, forcing cancel", len(pending))
                for task in pending:
                    task.cancel()

                # 使用 asyncio.gather 统一等待，设置超时
                try:
                    await asyncio.wait(pending, timeout=3.0)
                except Exception as e:
                    logger.error("[modbus_rtu] code=TASK_WAIT_ERROR msg=Error waiting for cancelled tasks: %s", e)

        self._port_monitor_task = None
        self._failback_task = None
        self._sync_task = None

        if self._edge_trigger:
            await self._edge_trigger.stop()  # #[AUDIT-FIX] stop() is async, must await (was no-op coroutine)
            self._edge_trigger = None
        if self._rule_store:
            self._rule_store.stop()
            self._rule_store = None
        if self._sqlite_ts:
            await self._sqlite_ts.stop()
            self._sqlite_ts = None
        if self._offline_queue:
            await self._offline_queue.close()
            self._offline_queue = None
        self._ring_buffer = None
        self._config_version = None
        self._audit = None

        for device_id, client in list(self._clients.items()):
            try:
                if client.connected:
                    client.close()
                logger.info("Modbus RTU连接关闭: %s", device_id)
            except asyncio.CancelledError:
                # FIXED-P1: 不直接raise，继续关闭剩余客户端，防止串口资源泄漏
                logger.warning("Modbus RTU client close cancelled [%s], continuing cleanup", device_id)
            except Exception as e:
                logger.warning("Modbus RTU client close failed [%s]: %s", device_id, e)

        self._clients.clear()
        self._connected.clear()
        self._device_configs.clear()
        self._device_points.clear()
        self._retry_count.clear()
        with self._stats_lock:  # FIXED-P0: _health_stats/_offline_since清理加锁保护，与基类读写路径一致
            self._health_stats.clear()
            self._offline_since.clear()
        self._lock_holders.clear()  # MRTU-001: 清理锁持有者追踪
        self._serial_locks.clear()
        self._device_port_map.clear()
        # R5-G-03: 清理锁版本号字典，防止stop后字典残留导致内存泄漏
        self._lock_versions.clear()
        self._point_stats.clear()
        self._degrade_level.clear()
        self._point_timestamps.clear()
        self._frozen_counts.clear()
        self._last_raw_values.clear()
        self._write_last_time.clear()
        self._write_audit_log.clear()
        self._active_port.clear()
        self._port_fail_count.clear()
        self._read_fail_tracker.clear()
        self._port_reconnect_locks.clear()
        self._port_backoff.clear()
        self._port_available.clear()
        self._turnaround_delay.clear()
        logger.info("Modbus RTU驱动停止")
        # FIXED-P1: MRTU-R01 调用基类stop()确保_shutdown_executor和_cancel_background_tasks执行
        await super().stop()

    async def add_device(self, device_id: str, config: dict, points: list[dict]) -> None:
        unit_id = config.get("unit_id", 1)
        if not (1 <= unit_id <= 247):
            logger.error("[modbus_rtu] device=%s code=CONFIG_INVALID unit_id=%d out of range [1-247]", device_id, unit_id)
            raise ValueError(f"Modbus RTU config invalid: unit_id must be 1-247, got {unit_id}")
        baudrate = config.get("baudrate", 9600)
        if not (300 <= baudrate <= 115200):
            raise ValueError(f"Modbus RTU config invalid: baudrate must be 300-115200, got {baudrate}")
        stopbits = config.get("stopbits", 1)
        if stopbits not in (1, 2):
            raise ValueError(f"Modbus RTU config invalid: stopbits must be 1 or 2, got {stopbits}")
        bytesize = config.get("bytesize", 8)
        if bytesize not in (7, 8):
            raise ValueError(f"Modbus RTU config invalid: bytesize must be 7 or 8, got {bytesize}")
        parity = config.get("parity", "N")
        if parity not in ("N", "E", "O"):
            raise ValueError(f"Modbus RTU config invalid: parity must be N/E/O, got {parity}")

        self._device_configs[device_id] = config
        self._device_points[device_id] = points
        self._connected[device_id] = False
        self._lang = config.get("language", "zh")
        # FIXED: TCP网关设备应使用host而非串口路径作为_active_port，避免_connect_device取到串口路径
        tcp_gateway = config.get("tcp_gateway")
        if tcp_gateway and isinstance(tcp_gateway, dict):
            port_path = tcp_gateway.get("host", "")
        else:
            port_path = config.get("port", "/dev/ttyUSB0")  # FIXED-P0: port_path未定义导致NameError
        self._device_port_map[device_id] = port_path
        self._active_port[device_id] = port_path
        self._get_serial_lock(port_path)
        self._port_reconnect_locks.setdefault(port_path, asyncio.Lock())
        self._port_available.setdefault(port_path, True)
        self._turnaround_delay[port_path] = self._calc_turnaround_delay(config)

        if not _PYMODBUS_AVAILABLE:
            self._log_error(device_id, "CONN_FAILED", "pymodbus not installed, cannot create serial connection")
            return

        if self._running:
            await self._connect_device(device_id)

        if self._config_version:
            self._config_version.snapshot_device_config(device_id, config)
        if self._audit:
            await self._audit.log_config_change(device_id, list(config.keys()), {}, config)

    async def remove_device(self, device_id: str) -> None:
        self._stop_watchdog(device_id)
        client = self._clients.pop(device_id, None)
        if client:
            try:
                if client.connected:
                    client.close()
            except Exception as e:
                logger.debug("Modbus RTU客户端关闭失败[%s]: %s", device_id, e)
        self._connected.pop(device_id, None)
        self._device_configs.pop(device_id, None)
        self._device_points.pop(device_id, None)
        self._retry_count.pop(device_id, None)
        # FIXED-P1: _health_stats/_offline_since操作加_stats_lock，与_record_read_success/failure竞态保护一致
        with self._stats_lock:
            self._health_stats.pop(device_id, None)
            self._offline_since.pop(device_id, None)
        self._watchdog_fail_count.pop(device_id, None)
        removed_port = self._device_port_map.pop(device_id, None)  # MRTU-005: 保留移除的端口用于检查
        self._last_values.pop(device_id, None)
        self._health_check_fail_count.pop(device_id, None)
        self._degrade_level.pop(device_id, None)
        self._last_raw_values.pop(device_id, None)
        for key in [k for k in self._point_stats if k[0] == device_id]:
            self._point_stats.pop(key, None)
        for key in [k for k in self._point_timestamps if k[0] == device_id]:
            self._point_timestamps.pop(key, None)
        for key in [k for k in self._frozen_counts if k[0] == device_id]:
            self._frozen_counts.pop(key, None)
        for key in [k for k in self._write_last_time if k[0] == device_id]:
            self._write_last_time.pop(key, None)
        # FIXED-P1: 清理_read_fail_tracker中该设备的条目，避免字典无限增长
        for key in [k for k in self._read_fail_tracker if k[0] == device_id]:
            self._read_fail_tracker.pop(key, None)
        self._active_port.pop(device_id, None)
        self._port_fail_count.pop(device_id, None)
        self._failback_stable_count.pop(device_id, None)  # FIXED-P1: 清理failback稳定计数

        # MRTU-005: 如果该串口路径下无其他设备，清理串口锁和锁持有者追踪
        if removed_port:
            still_used = any(port == removed_port for port in self._device_port_map.values())
            if not still_used:
                self._serial_locks.pop(removed_port, None)
                self._lock_holders.pop(removed_port, None)
                logger.debug("[modbus_rtu] code=SERIAL_LOCK_CLEANUP msg=Cleaned up lock for unused port: %s", removed_port)

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        client = await self._ensure_connected(device_id)
        if client is None:
            now = datetime.now(UTC)
            return {p: PointValue(value=None, quality="bad", timestamp=now) for p in points}

        # MRTU-001: 使用上下文管理器确保锁正确释放
        try:
            async with self._acquire_serial_context(device_id) as acquired:
                if not acquired:
                    now = datetime.now(UTC)
                    return {p: PointValue(value=None, quality="bad", timestamp=now) for p in points}

                result = await self._read_points_inner(device_id, client, points)
                await self._apply_turnaround_delay(device_id)
                return result
        except _CRCReconnectNeeded as exc:
            # FIXED-P0: CRC错误时不在持有串口锁的情况下触发重连，锁已由上下文管理器释放
            logger.warning("[modbus_rtu] device=%s CRC error detected, reconnecting after lock release", device_id)
            old_client = self._clients.get(device_id)
            if old_client:
                try:
                    old_client.close()
                except Exception as _close_err:  # FIXED-P1
                    logger.debug("[modbus_rtu] close error: %s", _close_err)
            await self._try_reconnect(device_id)
            # 使用已有的部分结果，对缺失的测点填充bad quality
            partial = exc.partial_result
            now = datetime.now(UTC)
            return {p: partial.get(p, PointValue(value=None, quality="bad", timestamp=now)) for p in points}

    async def _read_points_inner(self, device_id: str, client: Any, points: list[str]) -> dict[str, Any]:
        """Internal read implementation (called with serial lock held)"""

        config = self._device_configs.get(device_id, {})
        slave_id = config.get("unit_id", 1)
        byte_order = config.get("byte_order", "ABCD")
        device_points = self._device_points.get(device_id, [])

        # 构建测点定义映射
        pt_map: dict[str, dict] = {}
        for point_name in points:
            pt_def = next((p for p in device_points if p.get("name") == point_name), None)
            if pt_def is not None:
                pt_map[point_name] = pt_def

        if not pt_map:
            return {}

        # 分离 coil/discrete（位操作）和 holding/input（寄存器操作）
        bit_points: dict[str, dict] = {}
        reg_points: dict[str, dict] = {}
        for name, pt_def in pt_map.items():
            reg_type = pt_def.get("register_type", "holding")
            if reg_type in ("coil", "discrete"):
                bit_points[name] = pt_def
            else:
                reg_points[name] = pt_def

        result: dict[str, Any] = {}

        # 位类型测点逐个读取
        for point_name, pt_def in bit_points.items():
            try:  # FIXED-P2: 合并三个重复的CancelledError捕获块为一个外层try/except
                try:
                    value = await asyncio.wait_for(
                        self._read_single_point(client, slave_id, pt_def, byte_order, device_id),
                        timeout=READ_TIMEOUT,
                    )
                    result[point_name] = value
                    self._read_fail_tracker.pop((device_id, point_name), None)
                    await self._record_read_success(device_id)  # #[AUDIT-FIX] _record_read_success is async, must await (was no-op coroutine)
                except ModbusException as e:
                    self._record_read_failure(device_id)
                    self._log_throttled(device_id, point_name, e)
                    result[point_name] = PointValue(value=None, quality="bad", timestamp=datetime.now(UTC))
                except _CRCReconnectNeeded:  # FIXED-P0: CRC错误在锁外重连
                    raise
                except TimeoutError:  # FIXED-P1: 兼容Python<3.11
                    self._record_read_failure(device_id)
                    self._log_error(device_id, "READ_TIMEOUT", f"Read timeout ({READ_TIMEOUT}s) for {point_name}")
                    result[point_name] = PointValue(value=None, quality="bad", timestamp=datetime.now(UTC))
                except Exception as e:
                    self._record_read_failure(device_id)
                    self._log_error(device_id, "READ_ERROR", f"Read failed for {point_name}: {e}", exc_info=True)
                    result[point_name] = PointValue(value=None, quality="bad", timestamp=datetime.now(UTC))
            except asyncio.CancelledError:
                raise

        # 寄存器类型测点批量合并读取
        if reg_points:
            try:
                batch_result = await asyncio.wait_for(
                    self._batch_read_points(client, slave_id, reg_points, byte_order, device_id),
                    timeout=READ_TIMEOUT,
                )
            except TimeoutError:  # FIXED-P1: 兼容Python<3.11
                self._record_read_failure(device_id)
                self._log_error(device_id, "READ_TIMEOUT", f"Batch read timeout ({READ_TIMEOUT}s)")
                batch_result = {name: PointValue(value=None, quality="bad", timestamp=datetime.now(UTC)) for name in reg_points}
            for point_name, value in batch_result.items():
                result[point_name] = value
                self._read_fail_tracker.pop((device_id, point_name), None)
            if batch_result:
                await self._record_read_success(device_id)  # #[AUDIT-FIX] _record_read_success is async, must await (was no-op coroutine)
            else:
                self._record_read_failure(device_id)

        now = datetime.now(UTC)
        for point_name in list(result.keys()):
            val = result[point_name]
            if isinstance(val, PointValue):
                if val.timestamp is None:
                    result[point_name] = PointValue(
                        value=val.value, quality=val.quality,
                        timestamp=now, source=val.source, latency_ms=val.latency_ms,
                    )
                self._record_point_stat(device_id, point_name, val.quality == "good")
                continue
            pt_def = pt_map.get(point_name, {})
            processed = self._apply_point_processing(
                device_id, point_name, val, pt_def, config,
            )
            if isinstance(processed, PointValue):
                result[point_name] = processed
                self._record_point_stat(device_id, point_name, processed.quality == "good")
            else:
                final_val = self._apply_quality_checks(device_id, point_name, processed, pt_def, config)
                result[point_name] = final_val
                self._record_point_stat(device_id, point_name, final_val.quality == "good")

        self._evaluate_degradation(device_id)

        if self._edge_engine:
            await self._evaluate_edge_rules(device_id, result)

        if self._persist_enabled:
            await self._persist_read_result(device_id, result)

        return result

    async def _batch_read_points(
        self, client: Any, slave_id: int,
        point_defs: dict[str, dict], byte_order: str, device_id: str,
    ) -> dict[str, Any]:
        """批量合并读取寄存器测点，自动分包（>125寄存器自动拆分）。

        将连续/相邻地址的测点合并为一次读取请求，每段不超过125个寄存器，
        超过则拆分为多个子段并发执行。
        """
        # 按地址排序
        sorted_points = sorted(point_defs.items(), key=lambda x: int(x[1].get("address", 0)))

        # 合并连续/相邻地址为读取段
        MAX_REGS = 125
        segments: list[tuple[int, int, list[tuple[str, dict]]]] = []
        seg_start: int | None = None
        seg_end: int = 0
        seg_items: list[tuple[str, dict]] = []
        seg_reg_type: str | None = None  # FIXED-P0: 跟踪当前段寄存器类型，防止input/holding混段读取

        for name, pt_def in sorted_points:
            addr = int(pt_def.get("address", 0))
            data_type = pt_def.get("data_type", "float32")
            n_regs = DATA_TYPE_REGS.get(data_type, 1)
            pt_reg_type = pt_def.get("register_type", "holding")

            if seg_start is None:
                seg_start = addr
                seg_end = addr + n_regs
                seg_items = [(name, pt_def)]
                seg_reg_type = pt_reg_type
            elif addr <= seg_end and (addr + n_regs - seg_start) <= MAX_REGS and pt_reg_type == seg_reg_type:
                seg_end = max(seg_end, addr + n_regs)
                seg_items.append((name, pt_def))
            else:
                segments.append((seg_start, seg_end - seg_start, seg_items))
                seg_start = addr
                seg_end = addr + n_regs
                seg_items = [(name, pt_def)]
                seg_reg_type = pt_reg_type

        if seg_start is not None:
            segments.append((seg_start, seg_end - seg_start, seg_items))

        # 对超过125寄存器的段进行拆分
        sub_segments: list[tuple[int, int, list[tuple[str, dict]]]] = []
        for start, count, items in segments:
            if count <= MAX_REGS:
                sub_segments.append((start, count, items))
            else:
                sub_start = None
                sub_end = 0
                sub_items: list[tuple[str, dict]] = []
                for name, pt_def in items:
                    addr = int(pt_def.get("address", 0))
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
        crc_error_detected = False

        async def _read_segment(
            start_addr: int, count: int, items: list[tuple[str, dict]],
        ) -> None:
            nonlocal crc_error_detected
            _set_client_slave_id(client, slave_id)
            try:
                read_result = await client.read_holding_registers(
                    start_addr, **_read_kwargs(count, slave_id)
                )
                if read_result.isError():
                    err_str = str(read_result)
                    # 检测CRC错误
                    if "crc" in err_str.lower() or "checksum" in err_str.lower():
                        crc_error_detected = True
                    # 解析Modbus异常码
                    exc_desc = _parse_modbus_exception(read_result)
                    if exc_desc:
                        logger.warning("[modbus_rtu] device=%s Modbus exception: %s", device_id, exc_desc)
                        # 0x84/0x86: 服务端故障/忙，可重试
                        if exc_desc.startswith("Server Device Failure") or exc_desc.startswith("Server Device Busy"):
                            # FIXED-P2: 添加重试总超时限制，防止批量重试长时间阻塞采集线程
                            retry_deadline = asyncio.get_running_loop().time() + _RETRY_TOTAL_TIMEOUT
                            for retry in range(2):
                                if asyncio.get_running_loop().time() >= retry_deadline:
                                    break
                                delay = min(1.0 * (2 ** retry), 30.0)
                                await asyncio.sleep(delay)
                                # FIXED-Bug5: 单次 I/O 超时不超过剩余预算，防止总耗时超过 _RETRY_TOTAL_TIMEOUT
                                _remaining_budget = max(1.0, retry_deadline - asyncio.get_running_loop().time())
                                try:
                                    retry_result = await asyncio.wait_for(  # FIXED-P2: 重试添加超时保护
                                        client.read_holding_registers(start_addr, **_read_kwargs(count, slave_id)),
                                        timeout=_remaining_budget,
                                    )
                                except TimeoutError:
                                    # FIXED-P2: 重试超时后继续下一次重试，与TCP驱动行为一致
                                    continue
                                if not retry_result.isError():
                                    registers = retry_result.registers
                                    for name, pt_def in items:
                                        addr = int(pt_def.get("address", 0))
                                        data_type = pt_def.get("data_type", "float32")
                                        n_regs = DATA_TYPE_REGS.get(data_type, 1)
                                        offset = addr - start_addr
                                        if offset < 0 or offset + n_regs > len(registers):  # FIXED-P2: 增加offset<0检查，防止负索引返回错误数据
                                            failed_points[name] = ModbusException("Insufficient registers in batch")
                                            continue
                                        pt_regs = registers[offset:offset + n_regs]
                                        try:
                                            value = self._decode_point_value(pt_regs, data_type, byte_order)
                                            result[name] = value
                                        except Exception as e:
                                            logger.warning("[modbus_rtu] Decode point value failed: %s.%s - %s", device_id, name, e)
                                            failed_points[name] = e
                                    return
                    for name, _ in items:
                        failed_points[name] = ModbusException(f"Batch read error: {read_result}")
                    return
                registers = read_result.registers
                for name, pt_def in items:
                    addr = int(pt_def.get("address", 0))
                    data_type = pt_def.get("data_type", "float32")
                    n_regs = DATA_TYPE_REGS.get(data_type, 1)
                    offset = addr - start_addr
                    if offset < 0 or offset + n_regs > len(registers):  # FIXED-P2: 添加offset负值检查
                        failed_points[name] = ModbusException("Insufficient registers in batch")
                        continue
                    pt_regs = registers[offset:offset + n_regs]
                    try:
                        value = self._decode_point_value(pt_regs, data_type, byte_order)
                        result[name] = value
                    except Exception as e:
                        logger.warning("[modbus_rtu] Decode point value failed: %s.%s - %s", device_id, name, e)
                        failed_points[name] = e
            except asyncio.CancelledError:  # FIXED-P1: Python 3.8下CancelledError继承自Exception，必须先捕获再向上传播
                raise
            except Exception as e:
                logger.warning("[modbus_rtu] Batch read error: %s.%s - %s", device_id, items[0][0] if items else "unknown", e)
                err_str = str(e)
                if "crc" in err_str.lower() or "checksum" in err_str.lower():
                    crc_error_detected = True
                for name, _ in items:
                    failed_points[name] = e

        # 判断每个测点的 register_type，input 类型需要单独读取
        holding_segments: list[tuple[int, int, list[tuple[str, dict]]]] = []
        input_items: list[tuple[str, dict]] = []

        for start, count, items in sub_segments:
            all_input = all(pt_def.get("register_type", "holding") == "input" for _, pt_def in items)
            if all_input:
                input_items.extend(items)
            else:
                holding_segments.append((start, count, items))

        if holding_segments:
            for s, c, i in holding_segments:
                await _read_segment(s, c, i)

        # 逐个读取 input 类型测点
        for name, pt_def in input_items:
            try:
                value = await self._read_single_point(client, slave_id, pt_def, byte_order, device_id)
                result[name] = value
            except _CRCReconnectNeeded:  # FIXED-P0: CRC错误在锁外重连
                raise
            except Exception as e:
                logger.warning("[modbus_rtu] Input point read failed: %s.%s - %s", device_id, name, e)
                failed_points[name] = e

        # FIXED-P0: CRC错误时不在持有串口锁的情况下触发重连
        if crc_error_detected:
            self._log_error(device_id, "CRC_ERROR", "CRC check failure detected, triggering reconnect")
            self._connected[device_id] = False
            # 先记录失败的测点到result中
            for name, err in failed_points.items():
                self._log_throttled(device_id, name, err)
                result[name] = PointValue(value=None, quality="bad", timestamp=datetime.now(UTC))
            # FIXED-P0: CRC错误后所有已有值可能来自损坏帧，全部标记为bad quality
            for name in list(result.keys()):
                pv = result[name]
                if pv.quality != "bad":
                    result[name] = PointValue(value=pv.value, quality="bad", timestamp=pv.timestamp)
            raise _CRCReconnectNeeded(f"CRC error on device {device_id}", partial_result=result)

        # 记录失败的测点
        for name, err in failed_points.items():
            self._log_throttled(device_id, name, err)
            result[name] = PointValue(value=None, quality="bad", timestamp=datetime.now(UTC))

        return result

    def _apply_point_processing(self, device_id: str, point_name: str, value: Any, pt_def: dict, config: dict) -> Any:
        scaling = pt_def.get("scaling") or config.get("scaling")
        clamp = pt_def.get("clamp") or config.get("clamp")
        deadband = pt_def.get("deadband", config.get("deadband"))

        if scaling is not None:
            value = self._apply_scaling(value, scaling)

        if clamp is not None:
            value, valid = self._apply_clamp(value, clamp)
            if not valid:
                return PointValue(value=value, quality="bad", timestamp=datetime.now(UTC))

        if deadband is not None:
            effective = False
            if isinstance(deadband, dict):
                threshold = deadband.get("threshold", 0)
                effective = threshold > 0
            elif isinstance(deadband, (int, float)):
                effective = deadband > 0
            if effective:
                last_values = self._last_values.setdefault(device_id, {})
                last_value = last_values.get(point_name)
                value = self._apply_deadband(value, last_value, deadband)
                last_values[point_name] = value
                # CROSS-003: 限制每个设备的测点数量
                while len(last_values) > self._MAX_POINTS_PER_DEVICE:
                    last_values.pop(next(iter(last_values)))

        return value

    def _decode_point_value(self, registers: list[int], data_type: str, byte_order: str) -> Any:
        """从寄存器列表解码单个测点值"""
        if data_type == "bool":
            if not registers:
                raise ModbusException("Empty registers for bool type")
            return bool(registers[0])
        elif data_type == "int16":
            if not registers:
                raise ModbusException("Empty registers for int16 type")
            val = registers[0]
            return val if val < 32768 else val - 65536
        elif data_type == "uint16":
            if not registers:
                raise ModbusException("Empty registers for uint16 type")
            return registers[0]
        elif data_type == "int32":
            return self._decode_registers(registers, byte_order, "i", 2)
        elif data_type == "uint32":
            return self._decode_registers(registers, byte_order, "I", 2)
        elif data_type == "float32":
            return self._decode_registers(registers, byte_order, "f", 2)
        elif data_type == "float64":
            return self._decode_registers(registers, byte_order, "d", 4)
        elif data_type == "string":  # FIXED-P1: 增加string类型处理，多寄存器字符串不再被截断
            raw_bytes = b''.join(struct.pack('>H', r) for r in registers)
            try:
                decoded = raw_bytes.decode('utf-8', errors='strict').rstrip('\x00')  # FIXED-P2: 先用strict解码，与TCP驱动一致
            except UnicodeDecodeError as e:
                logger.warning("[modbus_rtu] string解码失败: %s", e)
                decoded = raw_bytes.decode('utf-8', errors='ignore').rstrip('\x00')
            # FIXED-P1: 字符串长度限制256字节，与TCP驱动一致
            if len(decoded.encode('utf-8')) > 256:
                decoded = decoded.encode('utf-8')[:256].decode('utf-8', errors='ignore')
                logger.warning("[modbus_rtu] string解码超256字节，已截断: len=%d", len(raw_bytes))
            return decoded
        else:
            return registers[0] if registers else None  # FIXED-P2: 未知数据类型返回None而非0，与TCP驱动行为一致

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        if not await self.check_permission(Permission.DEVICE_WRITE_POINT):  # FIXED-P0: 写入操作添加权限检查，viewer角色无法写入
            self._log_error(device_id, "WRITE_DENIED", f"role={self._current_user_role} lacks device:write")
            return False
        config = self._device_configs.get(device_id, {})
        write_verify = config.get("write_verify", True)
        device_points = self._device_points.get(device_id, [])
        pt_def = next((p for p in device_points if p.get("name") == point), None)
        if pt_def is None:
            return False

        # FIXED-P0: NaN/Inf值写入前拒绝，防止异常浮点值绕过clamp写入设备寄存器
        if isinstance(value, float) and (value != value or value == float('inf') or value == float('-inf')):
            self._log_error(device_id, "WRITE_NAN_INF", f"{point}: value is NaN/Inf, rejected")
            return False
        clamp = pt_def.get("clamp") or config.get("clamp")
        if clamp is not None and isinstance(value, (int, float)):
            _, valid = self._apply_clamp(value, clamp)
            if not valid:
                self._log_error(device_id, "WRITE_CLAMP_REJECT", f"{point}: value={value} out of clamp range")
                self._record_write_audit(device_id, point, None, value, "rejected", "out of clamp range")
                return False

        key = (device_id, point)
        now_mono = time.monotonic()
        last_write_time = self._write_last_time.get(key, 0.0)
        if now_mono - last_write_time < self._WRITE_RATE_LIMIT_INTERVAL:
            self._log_error(device_id, "WRITE_RATE_LIMIT", f"{point}: interval < {self._WRITE_RATE_LIMIT_INTERVAL}s")
            self._record_write_audit(device_id, point, None, value, "rejected", "rate limited")
            return False

        client = await self._ensure_connected(device_id)
        if client is None:
            return False

        # MRTU-001: 使用上下文管理器确保锁正确释放
        async with self._acquire_serial_context(device_id) as acquired:
            if not acquired:
                return False

            old_value = await self._read_point_raw(device_id, client, pt_def, config) if write_verify else None
            result = await self._write_point_inner(device_id, client, point, value)
            await self._apply_turnaround_delay(device_id)
            if result:
                if write_verify:
                    await asyncio.sleep(self._WRITE_VERIFY_DELAY)
                    verify_value = await self._read_point_raw(device_id, client, pt_def, config)
                    await self._apply_turnaround_delay(device_id)
                    if verify_value is not None and not self._verify_write_value(value, verify_value, pt_def):
                        self._log_error(device_id, "WRITE_VERIFY_MISMATCH", f"{point}: written={value}, readback={verify_value}")
                        self._record_write_audit(device_id, point, old_value, value, "mismatch", f"readback={verify_value}")
                        self._write_last_time[key] = now_mono
                        return False
                self._record_write_audit(device_id, point, old_value, value, "ok", "")
            else:
                self._record_write_audit(device_id, point, old_value, value, "failed", "write error")
            self._write_last_time[key] = now_mono
            return result

    async def _write_point_inner(self, device_id: str, client: Any, point: str, value: Any) -> bool:
        """Internal write implementation (called with serial lock held)"""

        config = self._device_configs.get(device_id, {})
        slave_id = config.get("unit_id", 1)
        byte_order = config.get("byte_order", "ABCD")
        device_points = self._device_points.get(device_id, [])

        pt_def = next((p for p in device_points if p.get("name") == point), None)
        if pt_def is None:
            return False

        address = int(pt_def.get("address", 0))
        # 协议边界校验: Modbus 寄存器地址有效范围为 0-65535
        if not 0 <= address <= 65535:
            raise ValueError(f"Modbus 寄存器地址超出有效范围 0-65535: {address}")
        data_type = pt_def.get("data_type", "float32")

        _set_client_slave_id(client, slave_id)

        # 构建写入数据用于record_packet
        tx_data = f"FC=write addr={address} slave={slave_id} type={data_type} val={value}"
        record_packet("tx", self.plugin_name, device_id, tx_data)

        async def _do_write():
            if data_type == "bool":
                await client.write_coil(address, bool(value), **_slave_kwarg(slave_id))
            elif data_type == "int16" or data_type == "uint16":
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
            await asyncio.wait_for(_do_write(), timeout=WRITE_TIMEOUT)
            rx_data = f"FC=write addr={address} slave={slave_id} OK"
            record_packet("rx", self.plugin_name, device_id, rx_data)
            self._record_write_success(device_id)
            return True
        except TimeoutError:  # FIXED-P1: 兼容Python<3.11
            self._record_write_failure(device_id)
            self._log_error(device_id, "WRITE_TIMEOUT", f"{point}: write timeout ({WRITE_TIMEOUT}s)")
            return False
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._record_write_failure(device_id)
            self._log_error(device_id, "WRITE_ERROR", f"{point}: {e}", exc_info=True)
            return False

    def is_device_connected(self, device_id: str) -> bool:
        """检查设备是否已连接"""
        return self._connected.get(device_id, False)

    async def _read_single_point(
        self, client: Any, slave_id: int, pt_def: dict,
        byte_order: str = "ABCD", device_id: str = "",
    ) -> Any:
        """读取单个测点"""
        address = int(pt_def.get("address", 0))
        # 协议边界校验: Modbus 寄存器地址有效范围为 0-65535
        if not 0 <= address <= 65535:
            raise ValueError(f"Modbus 寄存器地址超出有效范围 0-65535: {address}")
        data_type = pt_def.get("data_type", "float32")
        reg_type = pt_def.get("register_type", "holding")
        reg_count = DATA_TYPE_REGS.get(data_type, 1)

        _set_client_slave_id(client, slave_id)

        # 记录发送包
        tx_data = f"FC=read addr={address} slave={slave_id} type={reg_type} count={reg_count}"
        record_packet("tx", self.plugin_name, device_id, tx_data)

        if reg_type == "coil":
            result = await asyncio.wait_for(  # FIXED-P1: 初始读取添加超时保护，与重试路径一致
                client.read_coils(address, **_read_kwargs(reg_count, slave_id)),
                timeout=10.0,
            )
            if result.isError():
                exc_desc = _parse_modbus_exception(result)
                if exc_desc:
                    logger.warning("[modbus_rtu] device=%s Modbus exception: %s", device_id, exc_desc)
                raise ModbusException(f"Read error: {result}")
            rx_data = f"FC=read addr={address} bits={result.bits[:reg_count]}"
            record_packet("rx", self.plugin_name, device_id, rx_data)
            if not result.bits:
                raise ValueError("coil读取结果bits为空")  # FIXED-P2: coil读取结果bits边界检查
            return bool(result.bits[0])
        elif reg_type == "discrete":
            result = await asyncio.wait_for(  # FIXED-P1: 初始读取添加超时保护，与重试路径一致
                client.read_discrete_inputs(address, **_read_kwargs(reg_count, slave_id)),
                timeout=10.0,
            )
            if result.isError():
                exc_desc = _parse_modbus_exception(result)
                if exc_desc:
                    logger.warning("[modbus_rtu] device=%s Modbus exception: %s", device_id, exc_desc)
                raise ModbusException(f"Read error: {result}")
            rx_data = f"FC=read addr={address} bits={result.bits[:reg_count]}"
            record_packet("rx", self.plugin_name, device_id, rx_data)
            if not result.bits:
                raise ValueError("discrete读取结果bits为空")  # FIXED-P2: discrete读取结果bits边界检查
            return bool(result.bits[0])
        elif reg_type == "input":
            result = await asyncio.wait_for(  # FIXED-P1: 初始读取添加超时保护，与重试路径一致
                client.read_input_registers(address, **_read_kwargs(reg_count, slave_id)),
                timeout=10.0,
            )
        else:
            result = await asyncio.wait_for(  # FIXED-P1: 初始读取添加超时保护，与重试路径一致
                client.read_holding_registers(
                    address, **_read_kwargs(reg_count, slave_id)
                ),
                timeout=10.0,
            )

        if result.isError():
            err_str = str(result)
            # 解析Modbus异常码
            exc_desc = _parse_modbus_exception(result)
            if exc_desc:
                logger.warning("[modbus_rtu] device=%s Modbus exception: %s", device_id, exc_desc)
                # 0x84/0x86: 服务端故障/忙，可重试
                if exc_desc.startswith("Server Device Failure") or exc_desc.startswith("Server Device Busy"):
                    # FIXED-P2: 添加重试总超时限制，防止批量重试长时间阻塞采集线程
                    retry_deadline = asyncio.get_running_loop().time() + _RETRY_TOTAL_TIMEOUT
                    for retry in range(2):
                        if asyncio.get_running_loop().time() >= retry_deadline:
                            break
                        delay = min(1.0 * (2 ** retry), 30.0)
                        await asyncio.sleep(delay)
                        # FIXED-Bug5: 单次 I/O 超时不超过剩余预算，防止总耗时超过 _RETRY_TOTAL_TIMEOUT
                        _remaining_budget = max(1.0, retry_deadline - asyncio.get_running_loop().time())
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
                            # FIXED-P2: 重试超时后继续下一次重试，与TCP驱动行为一致
                            continue
                        if not retry_result.isError():
                            rx_data = f"FC=read addr={address} regs={retry_result.registers}"
                            record_packet("rx", self.plugin_name, device_id, rx_data)
                            registers = retry_result.registers
                            return self._decode_point_value(registers, data_type, byte_order)
            # 检测CRC错误，触发重连
            if device_id and ("crc" in err_str.lower() or "checksum" in err_str.lower()):
                self._log_error(device_id, "CRC_ERROR", f"CRC check failure: {err_str}")
                # FIXED-P1: CRC错误前标记连接断开，与_batch_read_points行为一致，防止其他协程获取已关闭client
                self._connected[device_id] = False
                # FIXED-P0: 抛出异常而非直接调用_try_reconnect，避免在持有串口锁时重连导致死锁
                raise _CRCReconnectNeeded(device_id)
            raise ModbusException(f"Read error: {result}")

        registers = result.registers
        rx_data = f"FC=read addr={address} regs={registers}"
        record_packet("rx", self.plugin_name, device_id, rx_data)

        if data_type == "bool":
            if len(registers) < 1:
                raise ModbusException("Insufficient registers for bool")
            return bool(registers[0])
        elif data_type == "int16":
            if len(registers) < 1:
                raise ModbusException("Insufficient registers for int16")
            val = registers[0]
            return val if val < 32768 else val - 65536
        elif data_type == "uint16":
            if len(registers) < 1:
                raise ModbusException("Insufficient registers for uint16")
            return registers[0]
        elif data_type == "int32":
            return self._decode_registers(registers, byte_order, "i", 2)
        elif data_type == "uint32":
            return self._decode_registers(registers, byte_order, "I", 2)
        elif data_type == "float32":
            return self._decode_registers(registers, byte_order, "f", 2)
        elif data_type == "float64":
            return self._decode_registers(registers, byte_order, "d", 4)
        else:
            if len(registers) < 1:
                raise ModbusException("Insufficient registers")
            return registers[0]

    @staticmethod
    def _decode_registers(registers: list[int], byte_order: str, fmt_char: str, n_regs: int) -> Any:
        """根据字节序将寄存器列表解码为指定类型"""
        if len(registers) < n_regs:
            raise ModbusException(f"Insufficient registers: need {n_regs}, got {len(registers)}")
        reg_pack, val_unpack = _BYTE_ORDER_FMT.get(byte_order)
        # FIXED-P2: 非法byte_order抛出异常而非静默降级，与TCP驱动一致
        if reg_pack is None:
            raise ModbusException(f"Invalid byte_order: {byte_order}, must be one of {list(_BYTE_ORDER_FMT.keys())}")
        raw = struct.pack(f"{reg_pack}{'H' * n_regs}", *registers[:n_regs])
        return struct.unpack(f"{val_unpack}{fmt_char}", raw)[0]

    @staticmethod
    def _encode_value(value: Any, byte_order: str, fmt_char: str, n_regs: int) -> list[int]:
        """根据字节序将值编码为寄存器列表"""
        reg_pack, val_unpack = _BYTE_ORDER_FMT.get(byte_order)
        # FIXED-P2: 非法byte_order抛出异常而非静默降级，与TCP驱动一致
        if reg_pack is None:
            raise ModbusException(f"Invalid byte_order: {byte_order}, must be one of {list(_BYTE_ORDER_FMT.keys())}")
        raw = struct.pack(f"{val_unpack}{fmt_char}", value)
        return list(struct.unpack(f"{reg_pack}{'H' * n_regs}", raw))

    def _log_error(self, device_id: str, error_code: str, message: str = "") -> None:
        i18n_msg = _ERROR_I18N.get(error_code, {}).get(self._lang, "")
        display_msg = f"{i18n_msg} - {message}" if i18n_msg and message else (i18n_msg or message)
        logger.error(
            "[%s] device=%s code=%s msg=%s",
            self.plugin_name, device_id, error_code, display_msg,
        )

    def _log_throttled(self, device_id: str, point_name: str, error: Exception) -> None:
        key = (device_id, point_name)
        now = time.monotonic()
        # FIXED-P1: 容量超限时淘汰最旧条目，防止无界增长；OrderedDict保证FIFO淘汰
        if len(self._read_fail_tracker) >= self._MAX_TRACKER_ENTRIES and key not in self._read_fail_tracker:
            self._read_fail_tracker.popitem(last=False)
        first_time, last_log = self._read_fail_tracker.get(key, (now, 0.0))
        level = logging.WARNING if now - first_time < 5.0 else logging.DEBUG
        if now - last_log >= self._LOG_INTERVAL:
            prefix = _READ_FAIL_I18N.get(self._lang, "Modbus RTU read failed")
            logger.log(level, "%s: %s.%s - %s", prefix, device_id, point_name, error)
            self._read_fail_tracker[key] = (first_time, now)
        else:
            self._read_fail_tracker[key] = (first_time, last_log)
        # FIXED-P1: 更新后移到末尾，实现LRU淘汰而非FIFO
        self._read_fail_tracker.move_to_end(key)

    async def _connect_device(self, device_id: str) -> bool:
        config = self._device_configs.get(device_id, {})
        if not config:
            return False

        self._connected[device_id] = False
        await self._set_connection_state(device_id, ConnectionState.CONNECTING.value, "connecting")

        if not _PYMODBUS_AVAILABLE:
            self._log_error(device_id, "CONN_FAILED", "pymodbus not installed, cannot create serial connection")
            await self._set_connection_state(device_id, ConnectionState.DISCONNECTED.value, "pymodbus not installed")
            return False

        active_port = self._active_port.get(device_id, config.get("port", "/dev/ttyUSB0"))
        baudrate = config.get("baudrate", 9600)
        parity = config.get("parity", "N")
        stopbits = config.get("stopbits", 1)
        bytesize = config.get("bytesize", 8)

        old_client = self._clients.get(device_id)
        if old_client:
            try:
                if old_client.connected:
                    old_client.close()
            except Exception as e:
                logger.warning("Modbus RTU old client close failed [%s]: %s", device_id, e)
            # FIXED-P0: MR-01 移除对old_client的二次关闭，避免double-close

        tcp_gateway = config.get("tcp_gateway")
        if tcp_gateway and isinstance(tcp_gateway, dict):
            active_host = self._active_port.get(device_id, tcp_gateway.get("host", ""))
            client = AsyncModbusTcpClient(
                host=active_host,
                port=tcp_gateway.get("port", 502),
                timeout=CONNECT_TIMEOUT,
            )
        else:
            # FIXED-P1: 原问题-Windows平台串口被另一进程占用时，pymodbus抛出的异常信息不明确，
            #           难以快速定位为占用问题；且占用检测发生在connect()之后浪费时间
            #           修复-连接前用serial.Serial预检串口占用状态，失败时给出明确PORT_LOCKED错误提示并提前返回
            if sys.platform == "win32":
                try:
                    import serial as _pyserial
                    _probe = _pyserial.Serial(active_port, baudrate=baudrate, timeout=0.1)
                    _probe.close()
                except Exception as _port_err:
                    self._log_error(device_id, "PORT_LOCKED", f"Serial port {active_port} is already in use or unavailable on Windows: {_port_err}")
                    await self._set_connection_state(device_id, ConnectionState.DISCONNECTED.value, "serial port locked")
                    return False
            client = AsyncModbusSerialClient(
                port=active_port,
                baudrate=baudrate,
                parity=parity,
                stopbits=stopbits,
                bytesize=bytesize,
                timeout=CONNECT_TIMEOUT,
            )
            # FIXED-P0: 传递RS485配置参数，确保半双工RTS/CTS控制生效
            if config.get("rs485_mode", False):
                try:
                    client.comm_params.rs485_mode = True
                    client.comm_params.rs485_rts_on_send = config.get("rs485_rts_on_send", True)
                    client.comm_params.rs485_rts_on_recv = config.get("rs485_rts_on_recv", False)
                    client.comm_params.rs485_delay_before_send = config.get("rs485_delay_before_send", 0) / 1000.0
                    client.comm_params.rs485_delay_after_send = config.get("rs485_delay_after_send", 0) / 1000.0
                except Exception as e:
                    logger.warning("[modbus_rtu] operation failed: %s", e)  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
        self._clients[device_id] = client

        try:
            try:
                connected = await asyncio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT)
            except TimeoutError:
                connected = False
                self._log_error(device_id, "CONN_FAILED", f"connect timeout ({CONNECT_TIMEOUT}s) to {active_port}@{baudrate}")
            if connected:
                logger.info("Modbus RTU连接成功: %s (%s@%d)", device_id, active_port, baudrate)
                self._connected[device_id] = True
                self._retry_count[device_id] = 0
                self._health_check_fail_count[device_id] = 0
                self._port_backoff.pop(active_port, None)
                self._device_port_map[device_id] = active_port
                await self._set_connection_state(device_id, ConnectionState.CONNECTED.value)
                self._start_watchdog(device_id)
                return True
            else:
                self._log_error(device_id, "CONN_FAILED", f"connect failed to {active_port}@{baudrate}")
                await self._set_connection_state(device_id, ConnectionState.DISCONNECTED.value, "connect failed")
                try:
                    client.close()
                except Exception as _close_err:  # FIXED-P1
                    logger.debug("[modbus_rtu] close error: %s", _close_err)
                self._clients.pop(device_id, None)  # FIXED-P1: 连接失败时清理client，防止串口泄漏
                return False
        except Exception as e:
            err_msg = str(e)
            if "Permission denied" in err_msg or "Device or resource busy" in err_msg:
                self._log_error(device_id, "PORT_LOCKED", f"Serial port {active_port} is already in use by another process")
            else:
                self._log_error(device_id, "CONN_FAILED", str(e))
            await self._set_connection_state(device_id, ConnectionState.DISCONNECTED.value, str(e))
            try:
                client.close()
            except Exception as _close_err:  # FIXED-P1
                logger.debug("[modbus_rtu] close error: %s", _close_err)
            self._clients.pop(device_id, None)  # FIXED-P1: 连接异常时清理client，防止串口泄漏
            return False

    async def _ensure_connected(self, device_id: str) -> Any | None:
        client = self._clients.get(device_id)

        if client is not None and self._connected.get(device_id, False) and client.connected:
            return client

        if client is not None and not client.connected:
            self._connected[device_id] = False

        status = self._connection_statuses.get(device_id)
        if status and status.state == ConnectionState.OFFLINE.value:
            port_path = self._device_port_map.get(device_id, "")
            if not self._port_available.get(port_path, True):
                return None
            self._port_backoff.pop(port_path, None)

        if not self._connected.get(device_id, False):
            await self._try_reconnect(device_id)

        client = self._clients.get(device_id)
        if client is not None and self._connected.get(device_id, False) and client.connected:
            return client

        return None

    async def _try_reconnect(self, device_id: str) -> None:
        config = self._device_configs.get(device_id, {})
        if not config:
            return

        port_path = self._active_port.get(device_id, config.get("port", "/dev/ttyUSB0"))
        max_attempts = config.get("max_reconnect_attempts", self._MAX_RECONNECT_ATTEMPTS)

        await self._set_connection_state(device_id, ConnectionState.CONNECTING.value, "reconnect in progress")

        port_lock = self._port_reconnect_locks.setdefault(port_path, asyncio.Lock())
        exceeded = False
        should_failover = False

        # FIXED-P2: 将整个重连逻辑放在单次port_lock获取中，消除TOCTOU窗口
        # 之前：backoff检查与连接操作分两次获取port_lock，中间sleep期间状态可能变化
        # 之后：单次port_lock获取内完成backoff检查、sleep、连接操作；exceeded路径因需调用
        #       _try_failover（会获取同一port_lock）故放在锁外处理
        async with port_lock:
            port_state = self._port_backoff.setdefault(port_path, {"attempt": 0, "last_attempt_time": 0.0})
            attempt = port_state["attempt"]

            if attempt >= max_attempts:
                fail_count = self._port_fail_count.get(device_id, 0) + 1
                self._port_fail_count[device_id] = fail_count
                if fail_count >= self._FAILOVER_FAIL_THRESHOLD:
                    should_failover = True
                exceeded = True
            else:
                delay = min(self._BACKOFF_BASE * (2 ** attempt), self._BACKOFF_MAX)
                jitter = random.uniform(0, self._JITTER_MAX)
                total_delay = delay + jitter

                now = time.monotonic()
                elapsed = now - port_state["last_attempt_time"]
                if elapsed < total_delay:
                    wait_time = total_delay - elapsed
                    self._log_error(device_id, "BACKOFF_RETRY", f"attempt={attempt + 1}, delay={wait_time:.2f}s, port={port_path}")
                    await asyncio.sleep(wait_time)

                port_state["attempt"] = attempt + 1
                port_state["last_attempt_time"] = time.monotonic()

                stats = self._health_stats.get(device_id)
                if stats:
                    stats.total_reconnects += 1

                client = self._clients.get(device_id)
                # FIXED-P0: 检查是否已被其他协程成功重连，避免关闭已恢复的连接
                if client and self._connected.get(device_id, False) and client.connected:
                    return
                if client:
                    try:
                        client.close()
                    except Exception as e:
                        logger.debug("Modbus RTU reconnect close failed [%s]: %s", device_id, e)

                self._connected[device_id] = False

                if not _PYMODBUS_AVAILABLE:
                    return

                active_port = self._active_port.get(device_id, config.get("port", "/dev/ttyUSB0"))
                tcp_gateway = config.get("tcp_gateway")

                if tcp_gateway and isinstance(tcp_gateway, dict):
                    active_host = self._active_port.get(device_id, tcp_gateway.get("host", ""))
                    new_client = AsyncModbusTcpClient(
                        host=active_host,
                        port=tcp_gateway.get("port", 502),
                        timeout=config.get("timeout", 3.0),
                    )
                else:
                    new_client = AsyncModbusSerialClient(
                        port=active_port,
                        baudrate=config.get("baudrate", 9600),
                        parity=config.get("parity", "N"),
                        stopbits=config.get("stopbits", 1),
                        bytesize=config.get("bytesize", 8),
                        timeout=config.get("timeout", 3.0),
                    )
                    # FIXED-P0: 重连时也传递RS485配置参数
                    if config.get("rs485_mode", False):
                        try:
                            new_client.comm_params.rs485_mode = True
                            new_client.comm_params.rs485_rts_on_send = config.get("rs485_rts_on_send", True)
                            new_client.comm_params.rs485_rts_on_recv = config.get("rs485_rts_on_recv", False)
                            new_client.comm_params.rs485_delay_before_send = config.get("rs485_delay_before_send", 0) / 1000.0
                            new_client.comm_params.rs485_delay_after_send = config.get("rs485_delay_after_send", 0) / 1000.0
                        except Exception as e:
                            logger.warning("[modbus_rtu] operation failed: %s", e)  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                try:
                    try:
                        connected = await asyncio.wait_for(new_client.connect(), timeout=config.get("timeout", 3.0))
                    except TimeoutError:
                        connected = False
                        self._log_error(device_id, "CONN_FAILED", f"reconnect timeout to {active_port}")
                    if connected:
                        self._clients[device_id] = new_client
                        self._connected[device_id] = True
                        self._retry_count[device_id] = 0
                        self._watchdog_fail_count[device_id] = 0
                        self._health_check_fail_count[device_id] = 0
                        self._port_fail_count[device_id] = 0
                        self._port_backoff.pop(active_port, None)
                        self._device_port_map[device_id] = active_port
                        await self._set_connection_state(device_id, ConnectionState.CONNECTED.value)
                        self._log_error(device_id, "RECONNECT_OK", f"reconnected to {active_port}@{config.get('baudrate')}")
                    else:
                        try:
                            new_client.close()
                        except Exception as _close_err:  # FIXED-P1
                            logger.debug("[modbus_rtu] close error: %s", _close_err)
                        self._connected[device_id] = False
                        await self._set_connection_state(device_id, ConnectionState.DISCONNECTED.value, "connect failed")
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    try:
                        new_client.close()
                    except Exception as _close_err:  # FIXED-P1
                        logger.debug("[modbus_rtu] close error: %s", _close_err)
                    self._connected[device_id] = False
                    err_msg = str(e)
                    if "Permission denied" in err_msg or "Device or resource busy" in err_msg:
                        self._log_error(device_id, "PORT_LOCKED", f"Serial port {active_port} is already in use")
                    else:
                        self._log_error(device_id, "CONN_FAILED", str(e))
                    await self._set_connection_state(device_id, ConnectionState.DISCONNECTED.value, str(e))
                return

        if exceeded:
            if should_failover:
                switched = await self._try_failover(device_id)
                if switched:
                    return
            self._log_error(device_id, "CONN_FAILED", f"exceeded max reconnect attempts ({max_attempts})")
            await self._set_connection_state(device_id, ConnectionState.OFFLINE.value, "max reconnect attempts reached")
            return

    def _start_watchdog(self, device_id: str) -> None:
        """启动设备连接看门狗（每设备独立任务）"""  # FIXED-P0: 每设备独立watchdog，避免单设备阻塞所有设备健康检查
        self._watchdog_fail_count[device_id] = 0
        self._health_check_fail_count[device_id] = 0
        old_task = self._watchdog_tasks.get(device_id)
        if old_task is None or old_task.done():
            self._watchdog_tasks[device_id] = asyncio.ensure_future(self._watchdog_loop(device_id))

    def _stop_watchdog(self, device_id: str) -> None:
        """停止设备连接看门狗"""  # FIXED-P0: 每设备独立watchdog，避免单设备阻塞所有设备健康检查
        self._watchdog_fail_count.pop(device_id, None)
        self._health_check_fail_count.pop(device_id, None)
        task = self._watchdog_tasks.pop(device_id, None)
        if task and not task.done():
            task.cancel()

    async def _watchdog_loop(self, device_id: str) -> None:  # FIXED-P0: 每设备独立watchdog循环
        while self._running:
            await asyncio.sleep(10)
            if not self._running:
                break
            try:
                if not self._connected.get(device_id, False):
                    client = self._clients.get(device_id)
                    if client is not None and client.connected:
                        self._connected[device_id] = True
                        self._watchdog_fail_count[device_id] = 0
                        self._health_check_fail_count[device_id] = 0
                        # FIXED-P1: 设备已连接时清理对应端口的退避计数器，避免端口恢复后设备永久离线
                        active_port = self._active_port.get(device_id) or self._device_port_map.get(device_id)
                        if active_port and active_port in self._port_backoff:
                            self._port_backoff.pop(active_port, None)
                        await self._set_connection_state(device_id, ConnectionState.CONNECTED.value)
                    else:
                        self._watchdog_fail_count[device_id] = self._watchdog_fail_count.get(device_id, 0) + 1
                        if self._watchdog_fail_count[device_id] >= 3:
                            # FIXED-P3: 触发reconnect前重置fail count并检查_reconnecting标记
                            # 之前：跨await访问_watchdog_fail_count，reconnect期间可能重复触发
                            # 之后：重置fail count，添加_reconnecting标记防止重复触发
                            if device_id not in self._reconnecting:
                                logger.warning("[modbus_rtu] device=%s disconnected for >30s, triggering reconnect", device_id)
                                self._watchdog_fail_count[device_id] = 0
                                self._reconnecting.add(device_id)
                                try:
                                    await self._try_reconnect(device_id)
                                finally:
                                    self._reconnecting.discard(device_id)
                else:
                    health_ok = await self._active_health_check(device_id)
                    if not health_ok:
                        self._health_check_fail_count[device_id] = self._health_check_fail_count.get(device_id, 0) + 1
                        if self._health_check_fail_count[device_id] >= 3:
                            self._log_error(device_id, "HEALTH_CHECK_FAILED", "3 consecutive failures, marking offline")
                            self._connected[device_id] = False
                            self._health_check_fail_count[device_id] = 0
                            await self._set_connection_state(device_id, ConnectionState.OFFLINE.value, "health check failed 3 times")
                        elif self._health_check_fail_count[device_id] >= 1:
                            await self._set_connection_state(device_id, ConnectionState.DEGRADED.value, "health check failing")
                    else:
                        self._health_check_fail_count[device_id] = 0
                        status = self._connection_statuses.get(device_id)
                        if status and status.state == ConnectionState.DEGRADED.value:
                            await self._set_connection_state(device_id, ConnectionState.CONNECTED.value, "health check recovered")
                    self._watchdog_fail_count[device_id] = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("[modbus_rtu] Watchdog loop exception for device=%s: %s", device_id, e)
                if not self._handle_watchdog_exception(e, "modbus_rtu_watchdog"):
                    break

    async def _active_health_check(self, device_id: str) -> bool:
        client = self._clients.get(device_id)
        if client is None or not client.connected:
            return False
        config = self._device_configs.get(device_id, {})
        slave_id = config.get("unit_id", 1)
        try:
            async with self.SerialLockContext(self, device_id) as acquired:  # FIXED-P1: 使用SerialLockContext替代手动acquire/release，确保死锁检测和超时保护
                if not acquired:
                    return False
                _set_client_slave_id(client, slave_id)
                result = await asyncio.wait_for(
                    client.read_holding_registers(0, **_read_kwargs(1, slave_id)),
                    timeout=1.0,
                )
                await self._apply_turnaround_delay(device_id)
                return bool(not result.isError())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # FIXED-P1
            logger.debug("[modbus_rtu] health check error: %s", exc)
            return False

    @staticmethod
    def _calc_turnaround_delay(config: dict) -> float:
        baudrate = config.get("baudrate", 9600)
        if baudrate <= 0:
            return 0.0
        databits = config.get("bytesize", 8)
        parity = config.get("parity", "N")
        stopbits = config.get("stopbits", 1)
        parity_bits = 0 if parity == "N" else 1
        char_bits = 1 + databits + parity_bits + stopbits
        delay_ms = 3.5 * char_bits / baudrate * 1000
        return delay_ms / 1000.0

    async def _apply_turnaround_delay(self, device_id: str) -> None:
        port_path = self._device_port_map.get(device_id)
        if port_path and port_path in self._turnaround_delay:
            delay = self._turnaround_delay[port_path]
            if delay > 0:
                await asyncio.sleep(delay)

    async def _port_monitor_loop(self) -> None:
        _CLEANUP_COUNTER_RESET = 60  # 每 60 次（约1小时）执行一次清理

        cleanup_counter = 0
        while self._running:
            await asyncio.sleep(self._PORT_MONITOR_INTERVAL)
            if not self._running:
                break
            try:
                unique_ports = set(self._device_port_map.values())
                for port_path in unique_ports:
                    was_available = self._port_available.get(port_path, True)
                    is_available = await asyncio.to_thread(self._check_port_available, port_path)
                    if was_available and not is_available:
                        await self._mark_port_offline(port_path)
                    elif not was_available and is_available:
                        await self._try_port_recovery(port_path)
                    self._port_available[port_path] = is_available

                # FIXED-P1: 对超过300秒未更新的退避条目进行attempt减半衰减，避免设备永久离线
                now_mono = time.monotonic()
                for bp_port, bp_state in list(self._port_backoff.items()):
                    last_time = bp_state.get("last_attempt_time", 0.0)
                    if now_mono - last_time > 300 and bp_state.get("attempt", 0) > 0:
                        old_attempt = bp_state["attempt"]
                        bp_state["attempt"] = max(1, old_attempt // 2)
                        logger.debug("[modbus_rtu] code=BACKOFF_DECAY msg=Port %s backoff attempt decayed: %d -> %d", bp_port, old_attempt, bp_state["attempt"])

                # MRTU-005: 定期清理无设备引用的串口锁
                cleanup_counter += 1
                if cleanup_counter >= _CLEANUP_COUNTER_RESET:
                    cleanup_counter = 0
                    self._cleanup_orphaned_locks()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("[modbus_rtu] port monitor loop error: %s", e)

    def _cleanup_orphaned_locks(self) -> None:
        """MRTU-005: 清理无设备引用的串口锁"""
        active_ports = set(self._device_port_map.values())
        orphaned = [p for p in self._serial_locks if p not in active_ports]
        for port_path in orphaned:
            self._serial_locks.pop(port_path, None)
            self._lock_holders.pop(port_path, None)
            logger.debug("[modbus_rtu] code=ORPHANED_LOCK_CLEANUP msg=Cleaned up orphaned lock for port: %s", port_path)

    async def _reset_device_port(self, device_id: str) -> bool:
        """MRTU-MED-001: 重置设备关联的串口连接状态

        当检测到死锁时，关闭并重新打开串口连接，确保数据帧不会交错。

        Returns:
            True 如果 reset 成功，False 如果 reset 失败
        """
        port_path = self._device_port_map.get(device_id)
        if not port_path:
            return False

        try:
            # 关闭该设备关联的串口上的所有客户端
            devices_on_port = [did for did, port in self._device_port_map.items() if port == port_path]
            for did in devices_on_port:
                client = self._clients.get(did)
                if client:
                    try:
                        client.close()
                    except Exception as _close_err:  # FIXED-P1
                        logger.debug("[modbus_rtu] close error: %s", _close_err)
                    self._clients.pop(did, None)
                    self._connected[did] = False

            # 等待一小段时间确保底层资源释放
            await asyncio.sleep(0.5)

            # FIXED-P0: 对同端口设备串行重连，避免并发争抢串口
            for did in devices_on_port:
                try:
                    await self._try_reconnect(did)
                except Exception as e:  # FIXED-P2: 重连异常不再静默吞没，记录warning日志
                    logger.warning("[modbus_rtu] Reconnect failed for device=%s during port reset: %s", did, e)

            # 检查触发设备的重连是否成功
            client = self._clients.get(device_id)
            if client and self._connected.get(device_id, False) and client.connected:
                logger.info("[modbus_rtu] device=%s code=DEADLOCK_RESET_OK msg=Serial reset and reconnect successful", device_id)
                return True
            else:
                logger.error("[modbus_rtu] device=%s code=DEADLOCK_RECONNECT_FAILED msg=Serial reset succeeded but reconnect failed", device_id)
                return False

        except Exception as e:
            logger.error("[modbus_rtu] device=%s code=DEADLOCK_RESET_ERROR msg=Serial reset failed: %s", device_id, e)
            return False

    @staticmethod
    def _check_port_available(port_path: str) -> bool:
        if sys.platform in ("linux", "darwin"):
            return os.path.exists(port_path)
        try:
            from serial.tools.list_ports import comports
            available = [p.device for p in comports()]
            return port_path in available
        except Exception:
            logger.debug("[modbus_rtu] failed to list serial ports for availability check: %s", port_path)
            return False  # FIXED-P1

    async def _mark_port_offline(self, port_path: str) -> None:
        port_lock = self._port_reconnect_locks.setdefault(port_path, asyncio.Lock())
        async with port_lock:
            for device_id, port in list(self._device_port_map.items()):
                if port == port_path:
                    self._connected[device_id] = False
                    await self._set_connection_state(device_id, ConnectionState.DISCONNECTED.value, "serial port disappeared")
                    self._log_error(device_id, "PORT_DISAPPEARED", f"Serial port {port_path} disappeared")
                    client = self._clients.get(device_id)
                    if client:
                        try:
                            client.close()
                        except Exception as _close_err:  # FIXED-P1
                            logger.debug("[modbus_rtu] close error: %s", _close_err)
                    config = self._device_configs.get(device_id, {})
                    backup_port = config.get("backup_port")
                    if backup_port and self._active_port.get(device_id) != backup_port:
                        self._port_fail_count[device_id] = self._FAILOVER_FAIL_THRESHOLD
                        self._active_port[device_id] = backup_port
                        self._device_port_map[device_id] = backup_port
                        self._get_serial_lock(backup_port)
                        self._port_reconnect_locks.setdefault(backup_port, asyncio.Lock())
                        self._port_available.setdefault(backup_port, True)
                        self._turnaround_delay[backup_port] = self._calc_turnaround_delay(config)
                        self._log_error(device_id, "FAILOVER_PORT", f"Switching from {port_path} to {backup_port}")

    async def _try_port_recovery(self, port_path: str) -> None:
        self._port_backoff.pop(port_path, None)
        primary_ports = set()
        for device_id, config in self._device_configs.items():
            primary_port = config.get("port", "/dev/ttyUSB0")
            if primary_port == port_path:
                primary_ports.add(device_id)

        for device_id in primary_ports:
            active = self._active_port.get(device_id, "")
            config = self._device_configs.get(device_id, {})
            primary_port = config.get("port", "/dev/ttyUSB0")
            if active != primary_port:
                self._log_error(device_id, "FAILOVER_BACK", f"Primary port {primary_port} recovered, switching back")
                port_lock = self._port_reconnect_locks.setdefault(primary_port, asyncio.Lock())
                async with port_lock:
                    self._active_port[device_id] = primary_port
                    self._port_fail_count[device_id] = 0
                    self._device_port_map[device_id] = primary_port
                    self._get_serial_lock(primary_port)
                    self._port_reconnect_locks.setdefault(primary_port, asyncio.Lock())
                    self._port_available[primary_port] = True
                    self._turnaround_delay[primary_port] = self._calc_turnaround_delay(config)
                await self._connect_device(device_id)
            else:
                self._log_error(device_id, "PORT_REAPPEARED", f"Serial port {port_path} recovered, reconnecting")
                await self._connect_device(device_id)

    async def _try_failover(self, device_id: str) -> bool:
        config = self._device_configs.get(device_id, {})
        if not config:
            return False

        current_port = self._active_port.get(device_id, config.get("port", "/dev/ttyUSB0"))
        backup_port = config.get("backup_port")
        tcp_gateway = config.get("tcp_gateway")

        if tcp_gateway and isinstance(tcp_gateway, dict):
            current_host = self._active_port.get(device_id, tcp_gateway.get("host", ""))
            backup_host = tcp_gateway.get("backup_host")
            if backup_host and current_host != backup_host:
                self._log_error(device_id, "FAILOVER_TCP", f"Switching from {current_host} to {backup_host}")
                port_lock = self._port_reconnect_locks.setdefault(current_host, asyncio.Lock())
                async with port_lock:
                    self._active_port[device_id] = backup_host
                    self._port_fail_count[device_id] = 0
                    self._port_backoff.pop(backup_host, None)
                await self._connect_device(device_id)
                if self._connected.get(device_id, False):
                    self._start_failback_monitor(device_id)
                    return True
                return False

        if backup_port and current_port != backup_port:
            self._log_error(device_id, "FAILOVER_PORT", f"Switching from {current_port} to {backup_port}")
            port_lock = self._port_reconnect_locks.setdefault(current_port, asyncio.Lock())
            async with port_lock:
                self._active_port[device_id] = backup_port
                self._device_port_map[device_id] = backup_port
                self._port_fail_count[device_id] = 0
                self._port_backoff.pop(backup_port, None)
                self._get_serial_lock(backup_port)
                self._port_reconnect_locks.setdefault(backup_port, asyncio.Lock())
                self._port_available.setdefault(backup_port, True)
                self._turnaround_delay[backup_port] = self._calc_turnaround_delay(config)
            await self._connect_device(device_id)
            if self._connected.get(device_id, False):
                self._start_failback_monitor(device_id)
                return True
            # FIXED-P1: 备用串口连接失败返回False，与TCP网关切换逻辑一致，防止_try_reconnect误认为切换成功
            return False

        return False

    def _start_failback_monitor(self, device_id: str) -> None:
        if self._failback_task is None or self._failback_task.done():
            self._failback_task = asyncio.ensure_future(self._failback_loop())

    async def _failback_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._FAILOVER_CHECK_INTERVAL)
            if not self._running:
                break
            for device_id in list(self._active_port.keys()):
                config = self._device_configs.get(device_id, {})
                if not config:
                    continue
                primary_port = config.get("port", "/dev/ttyUSB0")
                active_port = self._active_port.get(device_id, primary_port)
                if active_port == primary_port:
                    # FIXED-P1: 已在主链路上，重置稳定计数
                    self._failback_stable_count.pop(device_id, None)
                    continue
                if not self._connected.get(device_id, False):
                    continue
                if await asyncio.to_thread(self._check_port_available, primary_port):
                    # FIXED-P1: 原问题-failback切换无抖动，主链路恢复后立即failback可能导致抖动
                    #           修复-添加stable_count计数，连续N次探测成功后才切换
                    count = self._failback_stable_count.get(device_id, 0) + 1
                    self._failback_stable_count[device_id] = count
                    if count < self._FAILOVER_STABLE_THRESHOLD:
                        logger.debug("[modbus_rtu] device=%s primary port %s stable count=%d/%d",
                                     device_id, primary_port, count, self._FAILOVER_STABLE_THRESHOLD)
                        continue
                    self._failback_stable_count.pop(device_id, None)
                    self._log_error(device_id, "FAILOVER_BACK", f"Primary port {primary_port} recovered (stable={count}), switching back")
                    port_lock = self._port_reconnect_locks.setdefault(primary_port, asyncio.Lock())
                    async with port_lock:
                        self._active_port[device_id] = primary_port
                        self._device_port_map[device_id] = primary_port
                        self._port_fail_count[device_id] = 0
                        self._port_available[primary_port] = True
                        self._get_serial_lock(primary_port)
                        self._port_reconnect_locks.setdefault(primary_port, asyncio.Lock())
                        self._turnaround_delay[primary_port] = self._calc_turnaround_delay(config)
                    await self._connect_device(device_id)
                else:
                    # FIXED-P1: 探测失败时重置稳定计数，防止间歇性可用导致误切换
                    self._failback_stable_count.pop(device_id, None)

    def _record_point_stat(self, device_id: str, point_name: str, success: bool) -> None:
        key = (device_id, point_name)
        # FIXED-P3: 先检查容量再添加，淘汰最旧的1个条目
        # 之前：setdefault先创建条目，容量检查在之后，新条目可能被立即淘汰
        # 之后：容量达上限时先淘汰最旧条目，再添加新条目
        if key not in self._point_stats and len(self._point_stats) >= self._MAX_POINT_STATS:
            oldest_key = next(iter(self._point_stats))
            self._point_stats.pop(oldest_key, None)
        stats = self._point_stats.setdefault(key, {
            "success_count": 0, "fail_count": 0,
            "latency_samples": [], "consecutive_fails": 0,
        })
        if success:
            stats["success_count"] += 1
            stats["consecutive_fails"] = 0
        else:
            stats["fail_count"] += 1
            stats["consecutive_fails"] += 1
        if len(stats["latency_samples"]) > self._POINT_STATS_WINDOW:
            stats["latency_samples"] = stats["latency_samples"][-self._POINT_STATS_WINDOW:]

    def get_point_stats(self, device_id: str, point_name: str) -> dict | None:
        key = (device_id, point_name)
        stats = self._point_stats.get(key)
        if stats is None:
            return None
        total = stats["success_count"] + stats["fail_count"]
        avg_latency = 0.0
        if stats["latency_samples"]:
            avg_latency = sum(stats["latency_samples"]) / len(stats["latency_samples"])
        return {
            "success_count": stats["success_count"],
            "fail_count": stats["fail_count"],
            "avg_latency_ms": avg_latency,
            "consecutive_fails": stats["consecutive_fails"],
            "success_rate": stats["success_count"] / total if total > 0 else 1.0,
        }

    def _apply_quality_checks(self, device_id: str, point_name: str, value: Any, pt_def: dict, config: dict) -> Any:
        now = datetime.now(UTC)
        quality = "good"

        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            self._log_error(device_id, "VALUE_INVALID", f"{point_name}={value}")
            return PointValue(value=None, quality="bad", timestamp=now)

        key = (device_id, point_name)
        prev_raw = self._last_raw_values.setdefault(device_id, {}).get(point_name)
        now_mono = time.monotonic()

        roc_threshold = pt_def.get("roc_threshold") or config.get("roc_threshold")
        if roc_threshold is not None and isinstance(value, (int, float)):
            prev_ts = self._point_timestamps.get(key)
            if prev_ts is not None and prev_raw is not None and isinstance(prev_raw, (int, float)):
                dt = now_mono - prev_ts
                if dt > 0:
                    rate = abs(value - prev_raw) / dt
                    if rate > roc_threshold:
                        quality = "uncertain"
                        self._log_error(device_id, "RATE_OF_CHANGE", f"{point_name}: {rate:.2f}/s > {roc_threshold}")
            self._point_timestamps[key] = now_mono

        frozen_count = pt_def.get("frozen_count") or config.get("frozen_count") or self._FROZEN_DEFAULT_COUNT
        if isinstance(value, (int, float)):
            if prev_raw is not None and value == prev_raw:
                count = self._frozen_counts.get(key, 0) + 1
                self._frozen_counts[key] = count
                if count >= frozen_count:
                    quality = "uncertain"
                    self._log_error(device_id, "VALUE_FROZEN", f"{point_name}: same value x{count}")
            else:
                self._frozen_counts[key] = 0

        raw_values = self._last_raw_values.setdefault(device_id, {})
        raw_values[point_name] = value
        # FIXED-P1: _last_raw_values每设备容量限制，与_last_values一致
        while len(raw_values) > self._MAX_POINTS_PER_DEVICE:
            raw_values.pop(next(iter(raw_values)))

        if quality != "good":
            return PointValue(value=value, quality=quality, timestamp=now)
        return PointValue(value=value, quality="good", timestamp=now)

    def _evaluate_degradation(self, device_id: str) -> None:
        device_point_keys = [k for k in self._point_stats if k[0] == device_id]
        if not device_point_keys:
            return
        total_success = 0
        total_count = 0
        for key in device_point_keys:
            stats = self._point_stats[key]
            total_success += stats["success_count"]
            total_count += stats["success_count"] + stats["fail_count"]
        if total_count == 0:
            return
        success_rate = total_success / total_count
        current_level = self._degrade_level.get(device_id, 0)

        if success_rate < self._DEGRADE_THRESHOLD and current_level < len(self._DEGRADE_LEVELS) - 1:
            new_level = current_level + 1
            self._degrade_level[device_id] = new_level
            new_interval = self._DEGRADE_LEVELS[new_level]
            self._log_error(device_id, "DEGRADED_FREQ", f"rate={success_rate:.2f}, level={new_level}, interval={new_interval}s")
        elif success_rate > self._RECOVER_THRESHOLD and current_level > 0:
            new_level = current_level - 1
            self._degrade_level[device_id] = new_level
            new_interval = self._DEGRADE_LEVELS[new_level]
            self._log_error(device_id, "RECOVERED_FREQ", f"rate={success_rate:.2f}, level={new_level}, interval={new_interval}s")

    def get_polling_interval(self, device_id: str) -> float:
        level = self._degrade_level.get(device_id, 0)
        return self._DEGRADE_LEVELS[level]

    async def write_points_batch(self, device_id: str, points: dict[str, Any]) -> dict[str, bool]:
        # FIXED-P1: 方法名与基类 write_points_batch 对齐
        if not await self.check_permission(Permission.DEVICE_WRITE_POINT):  # FIXED-P0: 批量写入同样需要权限检查，防止viewer角色绕过
            self._log_error(device_id, "WRITE_DENIED", f"role={self._current_user_role} lacks device:write")
            return {p: False for p in points}
        writes = points
        client = await self._ensure_connected(device_id)
        if client is None:
            return {p: False for p in writes}

        config = self._device_configs.get(device_id, {})
        device_points = self._device_points.get(device_id, [])
        byte_order = config.get("byte_order", "ABCD")
        slave_id = config.get("unit_id", 1)

        pt_defs = {}
        for point in writes:
            pt_def = next((p for p in device_points if p.get("name") == point), None)
            if pt_def is None:
                continue
            value = writes[point]
            # FIXED-P0: 批量写入NaN/Inf拒绝，防止异常浮点值绕过clamp写入设备寄存器
            if isinstance(value, float) and (value != value or value == float('inf') or value == float('-inf')):
                self._log_error(device_id, "WRITE_NAN_INF", f"{point}: batch write value is NaN/Inf, rejected")
                continue
            clamp = pt_def.get("clamp") or config.get("clamp")
            if clamp is not None and isinstance(value, (int, float)):
                _, valid = self._apply_clamp(value, clamp)
                if not valid:
                    self._log_error(device_id, "WRITE_CLAMP_REJECT", f"{point}: value={value} out of clamp range")
                    self._record_write_audit(device_id, point, None, value, "rejected", "out of clamp range")
                    continue
            pt_defs[point] = pt_def

        # MRTU-001: 使用上下文管理器确保锁正确释放
        async with self._acquire_serial_context(device_id) as acquired:
            if not acquired:
                return {p: False for p in writes}

            results = await self._batch_write_inner(device_id, client, pt_defs, writes, config, byte_order, slave_id)
            await self._apply_turnaround_delay(device_id)
            return results

    async def _batch_write_inner(
        self, device_id: str, client: Any, pt_defs: dict[str, dict],
        writes: dict[str, Any], config: dict, byte_order: str, slave_id: int,
    ) -> dict[str, bool]:
        results = {}
        holding_writes: list[tuple[str, dict, Any]] = []
        coil_writes: list[tuple[str, dict, Any]] = []

        for point, pt_def in pt_defs.items():
            value = writes[point]
            reg_type = pt_def.get("register_type", "holding")
            if reg_type == "coil" or pt_def.get("data_type") == "bool":
                coil_writes.append((point, pt_def, value))
            else:
                holding_writes.append((point, pt_def, value))

        for point, pt_def, value in coil_writes:
            ok = await self._write_single_point(device_id, client, point, pt_def, value, config, byte_order, slave_id)
            results[point] = ok

        if holding_writes:
            merged = self._merge_adjacent_writes(holding_writes, byte_order)
            for group in merged:
                if len(group) == 1:
                    point, pt_def, value, regs = group[0]
                    ok = await self._write_single_point(device_id, client, point, pt_def, value, config, byte_order, slave_id)
                    results[point] = ok
                else:
                    start_addr = min(min(g[3].keys()) for g in group)
                    all_regs: dict[int, int] = {}
                    for _, _, _, regs in group:
                        all_regs.update(regs)
                    end_addr = max(all_regs.keys()) + 1
                    reg_values = [all_regs.get(a, 0) for a in range(start_addr, end_addr)]

                    _set_client_slave_id(client, slave_id)
                    try:
                        await asyncio.wait_for(
                            client.write_registers(start_addr, reg_values, **_slave_kwarg(slave_id)),
                            timeout=WRITE_TIMEOUT,
                        )
                        merged_names = ",".join(g[0] for g in group)
                        self._log_error(device_id, "WRITE_BATCH_MERGE", f"FC16: addr={start_addr}, count={len(reg_values)}, points={merged_names}")
                        for point, _pt_def, value, _ in group:  # FIXED(P3): 原问题-B007循环变量pt_def未使用; 修复-改为_pt_def
                            results[point] = True
                            self._record_write_audit(device_id, point, None, value, "ok", "batch FC16")
                            self._record_write_success(device_id)
                    except Exception as e:
                        for point, _pt_def, value, _ in group:  # FIXED(P3): 原问题-B007循环变量pt_def未使用; 修复-改为_pt_def
                            results[point] = False
                            self._record_write_audit(device_id, point, None, value, "failed", str(e))
                            self._record_write_failure(device_id)
                        self._log_error(device_id, "WRITE_ERROR", f"batch FC16: {e}", exc_info=True)

        for point in writes:
            if point not in results:
                results[point] = False

        return results

    def _merge_adjacent_writes(
        self, holding_writes: list[tuple[str, dict, Any]], byte_order: str,
    ) -> list[list[tuple[str, dict, Any, dict[int, int]]]]:
        encoded = []
        for point, pt_def, value in holding_writes:
            address = int(pt_def.get("address", 0))
            data_type = pt_def.get("data_type", "uint16")
            regs = self._encode_write_value(value, data_type, byte_order)
            reg_map = {address + i: r for i, r in enumerate(regs)}
            encoded.append((point, pt_def, value, reg_map))

        encoded.sort(key=lambda x: min(x[3].keys()))

        groups: list[list[tuple[str, dict, Any, dict[int, int]]]] = []
        current_group: list[tuple[str, dict, Any, dict[int, int]]] = []
        current_max_addr = -999

        for item in encoded:
            item_min = min(item[3].keys())
            item_max = max(item[3].keys())
            if not current_group or item_min <= current_max_addr + 1:
                current_group.append(item)
                current_max_addr = max(current_max_addr, item_max)
            else:
                groups.append(current_group)
                current_group = [item]
                current_max_addr = item_max

        if current_group:
            groups.append(current_group)

        return groups

    def _encode_write_value(self, value: Any, data_type: str, byte_order: str) -> list[int]:
        if data_type == "bool":
            return [1 if value else 0]
        elif data_type == "int16":
            v = int(value)
            return [v & 0xFFFF]
        elif data_type == "uint16":
            return [int(value) & 0xFFFF]
        elif data_type == "int32":
            return self._encode_value(int(value), byte_order, "i", 2)
        elif data_type == "uint32":
            return self._encode_value(int(value), byte_order, "I", 2)
        elif data_type == "float32":
            return self._encode_value(float(value), byte_order, "f", 2)
        elif data_type == "float64":
            return self._encode_value(float(value), byte_order, "d", 4)
        return [int(value) & 0xFFFF]

    async def _write_single_point(
        self, device_id: str, client: Any, point: str, pt_def: dict,
        value: Any, config: dict, byte_order: str, slave_id: int,
    ) -> bool:
        address = int(pt_def.get("address", 0))
        data_type = pt_def.get("data_type", "uint16")

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
            await asyncio.wait_for(_do_write(), timeout=WRITE_TIMEOUT)
            rx_data = f"FC=write addr={address} slave={slave_id} OK"
            record_packet("rx", self.plugin_name, device_id, rx_data)
            self._record_write_success(device_id)
            return True
        except TimeoutError:  # FIXED-P1: 兼容Python<3.11
            self._record_write_failure(device_id)
            self._log_error(device_id, "WRITE_TIMEOUT", f"{point}: write timeout ({WRITE_TIMEOUT}s)")
            return False
        except Exception as e:
            self._record_write_failure(device_id)
            self._log_error(device_id, "WRITE_ERROR", f"{point}: {e}", exc_info=True)
            return False

    async def _read_point_raw(self, device_id: str, client: Any, pt_def: dict, config: dict) -> Any:
        try:
            slave_id = config.get("unit_id", 1)
            byte_order = config.get("byte_order", "ABCD")
            address = int(pt_def.get("address", 0))
            data_type = pt_def.get("data_type", "uint16")
            reg_type = pt_def.get("register_type", "holding")
            reg_count = DATA_TYPE_REGS.get(data_type, 1)

            _set_client_slave_id(client, slave_id)

            if reg_type == "coil":
                result = await asyncio.wait_for(
                    client.read_coils(address, **_read_kwargs(reg_count, slave_id)),
                    timeout=3.0,
                )
                if not result.isError():
                    if not result.bits:  # FIXED-P1: 检查bits非空，防止IndexError导致写入验证被静默跳过
                        return None
                    return bool(result.bits[0])
            elif reg_type == "input":
                result = await asyncio.wait_for(
                    client.read_input_registers(address, **_read_kwargs(reg_count, slave_id)),
                    timeout=3.0,
                )
            else:
                result = await asyncio.wait_for(
                    client.read_holding_registers(address, **_read_kwargs(reg_count, slave_id)),
                    timeout=3.0,
                )

            if not result.isError() and reg_type != "coil":
                registers = result.registers
                return self._decode_point_value(registers, data_type, byte_order)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("[modbus_rtu] read_point_raw failed: %s.%s - %s", device_id, pt_def.get('name', ''), e)
        return None

    @staticmethod
    def _verify_write_value(written: Any, readback: Any, pt_def: dict) -> bool:
        tolerance = pt_def.get("write_tolerance", 0.01)
        if isinstance(written, float) and isinstance(readback, float):
            if abs(written) > 1e-10:
                return abs(readback - written) / abs(written) <= tolerance
            return abs(readback - written) <= tolerance
        if isinstance(written, int) and isinstance(readback, (int, float)):
            if isinstance(readback, float):
                return abs(readback - written) <= tolerance
            return readback == written
        if isinstance(written, bool) and isinstance(readback, bool):
            return written == readback
        return written == readback

    def _record_write_audit(
        self, device_id: str, point: str, old_value: Any,
        new_value: Any, result: str, error_msg: str,
    ) -> None:
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "device_id": device_id,
            "point_id": point,
            "old_value": old_value,
            "new_value": new_value,
            "result": result,
            "error_msg": error_msg,
        }
        with self._audit_lock:  # FIXED-P4: 加锁保护并发append
            self._write_audit_log.append(entry)

    def get_write_audit_log(self, device_id: str | None = None, limit: int = 100) -> list[dict]:
        with self._audit_lock:  # FIXED-P4: 加锁保护并发读取/遍历
            if device_id:
                entries = [e for e in self._write_audit_log if e["device_id"] == device_id]
            else:
                entries = list(self._write_audit_log)
        return entries[-limit:]

    def _init_edge_engine(self) -> None:
        if self._edge_engine is not None:
            return
        self._rule_store = RuleStore()
        self._edge_engine = ModbusEdgeRuleEngine()
        self._edge_trigger = EdgeTriggerExecutor(
            device_write_callback=self._edge_write_callback,
            mqtt_publish_callback=self._edge_mqtt_callback,
        )
        self._edge_engine.set_on_action_callback(self._edge_trigger.execute)
        rules = self._rule_store.load_rules()
        for rule in rules:
            self._edge_engine.add_rule(rule)
        self._log_error("", "EDGE_RULE_LOAD", f"loaded {len(rules)} rules from store")

    async def _edge_write_callback(self, device_id: str, point: str, value: Any) -> bool:
        try:
            return await self.write_point(device_id, point, value)
        except Exception as e:
            logger.error("[modbus_rtu] edge write callback failed: %s.%s=%s - %s", device_id, point, value, e)
            return False

    async def _edge_mqtt_callback(self, topic: str, payload: dict, qos: int = 0, retain: bool = False) -> None:
        if self._mqtt_publish_callback:
            try:
                await self._mqtt_publish_callback(topic, payload, qos=qos, retain=retain)
            except Exception as e:
                logger.error("[modbus_rtu] edge MQTT callback failed: %s", e)
        else:
            logger.debug("[modbus_rtu] edge MQTT: topic=%s payload=%s (no callback)", topic, payload)

    def set_mqtt_publish_callback(self, callback) -> None:
        self._mqtt_publish_callback = callback

    async def _evaluate_edge_rules(self, device_id: str, result: dict[str, Any]) -> None:
        if not self._edge_engine:
            return
        for point_name, pv in result.items():
            if not isinstance(pv, PointValue):
                continue
            if pv.value is None or not isinstance(pv.value, (int, float)):
                continue
            try:
                alarm_records = await self._edge_engine.evaluate_point(
                    device_id, point_name, float(pv.value), pv.quality,
                )
                for record in alarm_records:
                    action_label = "EDGE_ALARM_FIRE" if record.action == "firing" else "EDGE_ALARM_RECOVER"
                    self._log_error(
                        device_id, action_label,
                        f"{point_name}={record.trigger_value:.2f} {record.action} (rule={record.rule_id}, threshold={record.threshold:.2f}, latency={record.latency_ms:.1f}ms)",
                    )
            except Exception as e:
                logger.error("[modbus_rtu] edge rule eval failed: %s.%s - %s", device_id, point_name, e)

    def add_edge_rule(self, rule_dict: dict) -> EdgeRule | None:
        if not self._edge_engine or not self._rule_store:
            return None
        try:
            rule = EdgeRule(
                rule_id=rule_dict["rule_id"],
                device_id=rule_dict["device_id"],
                point_name=rule_dict["point_name"],
                rule_type=EdgeRuleType(rule_dict.get("rule_type", "threshold")),
                operator=EdgeRuleOperator(rule_dict.get("operator", ">")),
                threshold=float(rule_dict["threshold"]),
                severity=rule_dict.get("severity", "major"),
                enabled=rule_dict.get("enabled", True),
                cooldown_ms=float(rule_dict.get("cooldown_ms", 5000.0)),
                duration_ms=float(rule_dict.get("duration_ms", 0.0)),
                deadband=float(rule_dict.get("deadband", 0.0)),
                actions=rule_dict.get("actions", []),
            )
            self._edge_engine.add_rule(rule)
            self._rule_store.save_rule(rule)
            self._log_error(rule.device_id, "EDGE_RULE_LOAD", f"rule={rule.rule_id} op={rule.operator.value} threshold={rule.threshold}")
            return rule
        except Exception as e:
            logger.error("[modbus_rtu] add edge rule failed: %s", e)
            return None

    async def remove_edge_rule(self, rule_id: str) -> bool:
        if not self._edge_engine or not self._rule_store:
            return False
        rule = await self._edge_engine.remove_rule(rule_id)
        if rule:
            self._rule_store.delete_rule(rule_id)
            return True
        return False

    async def update_edge_rule(self, rule_id: str, updates: dict) -> bool:
        if not self._edge_engine or not self._rule_store:
            return False
        ok = await self._edge_engine.update_rule(rule_id, updates)
        if ok:
            rule = self._edge_engine.get_rule(rule_id)
            if rule:
                self._rule_store.save_rule(rule)
            self._log_error("", "EDGE_RULE_RELOAD", f"rule={rule_id} updated")
        return ok

    def get_edge_rules(self, device_id: str | None = None) -> list[dict]:
        if not self._edge_engine:
            return []
        if device_id:
            return [r.to_dict() for r in self._edge_engine.get_rules_for_device(device_id)]
        return self._edge_engine.get_all_rules()

    async def rollback_edge_rule(self, rule_id: str, target_version: int) -> EdgeRule | None:
        if not self._rule_store or not self._edge_engine:
            return None
        rule = self._rule_store.rollback(rule_id, target_version)
        if rule:
            await self._edge_engine.remove_rule(rule_id)
            self._edge_engine.add_rule(rule)
            self._log_error(rule.device_id, "EDGE_RULE_ROLLBACK", f"rule={rule_id} to v{target_version}")
        return rule

    def get_edge_rule_versions(self, rule_id: str) -> list[dict]:
        if not self._rule_store:
            return []
        return self._rule_store.get_versions(rule_id)

    def get_edge_alarm_history(self, limit: int = 100) -> list[dict]:
        if not self._edge_engine:
            return []
        return self._edge_engine.get_alarm_history(limit)

    def get_edge_active_alarms(self) -> list[dict]:
        if not self._edge_engine:
            return []
        return self._edge_engine.get_active_alarms()

    def get_edge_stats(self) -> dict:
        if not self._edge_engine:
            return {}
        stats = self._edge_engine.get_stats()
        if self._edge_trigger:
            stats["trigger"] = self._edge_trigger.get_stats()
        return stats

    async def reload_edge_rules(self) -> int:
        if not self._rule_store or not self._edge_engine:
            return 0
        for rule_id in list(self._edge_engine._rules.keys()):
            await self._edge_engine.remove_rule(rule_id)
        rules = self._rule_store.load_rules()
        for rule in rules:
            self._edge_engine.add_rule(rule)
        self._log_error("", "EDGE_RULE_RELOAD", f"reloaded {len(rules)} rules")
        return len(rules)

    def _init_persistence(self) -> None:
        if self._persist_enabled:
            return
        self._sqlite_ts = SqliteTimeSeriesStorage()
        self._offline_queue = OfflineQueue()
        self._ring_buffer = RingBuffer(capacity=100000, compress=True)
        self._persist_enabled = True
        try:
            asyncio.get_running_loop()
            self._sync_task = asyncio.ensure_future(self._sync_loop())
        except RuntimeError:
            self._persist_enabled = False  # FIXED-P2: 无事件循环时禁用持久化，防止_sync_task缺失导致写入失败
            pass
        self._log_error("", "PERSIST_LOCAL_WRITE", "persistence initialized")

    async def _persist_read_result(self, device_id: str, result: dict[str, Any]) -> None:
        if not self._sqlite_ts:
            return
        ts_ns = time.time_ns()
        for point_name, pv in result.items():
            if not isinstance(pv, PointValue):
                continue
            if pv.value is None:
                continue
            try:
                await self._sqlite_ts.write_point(
                    measurement="modbus_rtu",
                    device_id=device_id,
                    point_name=point_name,
                    value=pv.value,
                    quality=pv.quality,
                    timestamp_ns=ts_ns,
                )
                self._persist_stats["local_writes"] += 1
            except Exception as e:
                logger.error("[modbus_rtu] local persist write failed: %s.%s - %s", device_id, point_name, e)

        if not self._network_online:
            await self._enqueue_offline(device_id, result, ts_ns)

    async def _enqueue_offline(self, device_id: str, result: dict[str, Any], ts_ns: int) -> None:
        if not self._offline_queue:
            return
        for point_name, pv in result.items():
            if not isinstance(pv, PointValue):
                continue
            if pv.value is None:
                continue
            try:
                await self._offline_queue.enqueue(
                    topic=f"edgelite/data/{device_id}/{point_name}",
                    payload={
                        "device_id": device_id,
                        "point_name": point_name,
                        "value": pv.value,
                        "quality": pv.quality,
                        "timestamp_ns": ts_ns,
                    },
                )
                self._persist_stats["offline_enqueues"] += 1
            except Exception as e:
                logger.error("[modbus_rtu] offline enqueue failed: %s.%s - %s", device_id, point_name, e)

        if self._ring_buffer:
            try:
                await self._ring_buffer.put({
                    "device_id": device_id,
                    "points": {
                        pn: {"value": pv.value, "quality": pv.quality}
                        for pn, pv in result.items()
                        if isinstance(pv, PointValue) and pv.value is not None
                    },
                    "timestamp_ns": ts_ns,
                })
            except Exception as e:
                logger.error("[modbus_rtu] ring buffer put failed: %s", e)

    def set_network_status(self, online: bool) -> None:
        was_online = self._network_online
        self._network_online = online
        if was_online and not online:
            self._log_error("", "PERSIST_NETWORK_DOWN", "switched to offline mode")
        elif not was_online and online:
            self._log_error("", "PERSIST_NETWORK_UP", "switched to online mode, starting resync")
            if self._sync_task is None or self._sync_task.done():
                try:
                    asyncio.get_running_loop()
                    self._sync_task = asyncio.ensure_future(self._sync_loop())
                except RuntimeError as e:
                    logger.debug("[modbus_rtu] set_network_status failed: %s", e)  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录

    def set_upload_callback(self, callback) -> None:
        self._upload_callback = callback

    async def _sync_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._sync_interval)
                if self._network_online:
                    await self._sync_batch()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("[modbus_rtu] sync loop error: %s", e)

    async def _sync_batch(self) -> None:
        if not self._offline_queue or not self._upload_callback:
            return
        batch = await self._offline_queue.dequeue_batch(self._sync_batch_size)
        if not batch:
            return
        self._persist_stats["sync_batches"] += 1
        success_ids = []
        for item in batch:
            try:
                ok = await self._upload_callback(item["topic"], item["payload"])
                if ok:
                    success_ids.append(item["id"])
                    self._persist_stats["sync_records"] += 1
                else:
                    await self._offline_queue.increment_retry([item["id"]], "upload returned False")
            except Exception as e:
                await self._offline_queue.increment_retry([item["id"]], str(e))
        if success_ids:
            await self._offline_queue.acknowledge(success_ids)
            self._log_error("", "PERSIST_SYNC_BATCH", f"synced {len(success_ids)} records")

    async def force_sync_all(self) -> int:
        if not self._offline_queue or not self._upload_callback:
            return 0
        return await self._offline_queue.flush(self._upload_callback)

    async def sync_sqlite_to_upload(self, limit: int = 1000) -> int:
        if not self._sqlite_ts or not self._upload_callback:
            return 0
        records = await self._sqlite_ts.get_unsynced_records(limit)
        if not records:
            return 0
        synced = 0
        max_id = 0
        for rec in records:
            try:
                ok = await self._upload_callback(
                    f"edgelite/data/{rec['device_id']}/{rec['point_name']}",
                    rec,
                )
                if ok:
                    max_id = max(max_id, rec["id"])
                    synced += 1
            except Exception as e:
                logger.error("[modbus_rtu] sqlite sync record failed: %s", e)
        if max_id > 0:
            await self._sqlite_ts.sync_completed(max_id)
            self._log_error("", "PERSIST_SYNC_COMPLETE", f"synced {synced} records from SQLite (max_id={max_id})")
        return synced

    def get_persist_stats(self) -> dict:
        stats = dict(self._persist_stats)
        stats["network_online"] = self._network_online
        stats["persist_enabled"] = self._persist_enabled
        if self._sqlite_ts:
            try:
                asyncio.get_running_loop()
                stats["sqlite"] = "running"
            except RuntimeError:
                stats["sqlite"] = "unavailable"
        if self._offline_queue:
            try:
                asyncio.get_running_loop()
                stats["offline_queue"] = "running"
            except RuntimeError:
                stats["offline_queue"] = "unavailable"
        if self._ring_buffer:
            stats["ring_buffer"] = self._ring_buffer.get_stats()
        return stats

    def _init_config_version(self) -> None:
        if self._config_version is not None:
            return
        self._config_version = ModbusConfigVersion()
        for device_id, config in self._device_configs.items():
            self._config_version.snapshot_device_config(device_id, config, operator="system")

    def _init_audit(self) -> None:
        if self._audit is not None:
            return
        self._audit = ModbusAudit()

    async def set_user_role(self, role: str) -> None:
        async with self._role_lock:
            self._current_user_role = role

    async def check_permission(self, permission: Permission) -> bool:
        async with self._role_lock:
            return has_permission(self._current_user_role, permission)

    async def update_device_config(self, device_id: str, updates: dict, operator: str = "system") -> bool:
        if not await self.check_permission(Permission.CONFIG_EDIT):
            self._log_error(device_id, "CONFIG_CHANGE_DENIED", f"role={self._current_user_role} lacks config:edit")
            return False
        old_config = dict(self._device_configs.get(device_id, {}))
        if not old_config:
            return False
        new_config = dict(old_config)
        new_config.update(updates)
        self._device_configs[device_id] = new_config
        if self._config_version:
            changed_keys = self._config_version._deep_diff_keys(old_config, new_config)
            version = self._config_version.snapshot_device_config(device_id, new_config, operator=operator)
            self._log_error(device_id, "CONFIG_VERSION_SAVE", f"v={version} keys={changed_keys} op={operator}")
        if self._audit:
            await self._audit.log_config_change(
                device_id,
                list(updates.keys()),
                old_config,
                new_config,
                operator=operator,
            )
        return True

    async def rollback_device_config(self, device_id: str, target_version: int, operator: str = "system") -> dict | None:
        if not await self.check_permission(Permission.CONFIG_EDIT):
            self._log_error(device_id, "CONFIG_CHANGE_DENIED", f"role={self._current_user_role} lacks config:edit")
            return None
        if not self._config_version:
            return None
        config = self._config_version.rollback(target_version)
        if config is None:
            return None
        if isinstance(config, dict) and "device_id" in config and "config" in config:
            rollback_config = config["config"]
            rollback_device_id = config["device_id"]
        elif isinstance(config, dict):
            rollback_config = config
            rollback_device_id = device_id
        else:
            return None
        old_config = dict(self._device_configs.get(rollback_device_id, {}))
        self._device_configs[rollback_device_id] = rollback_config
        self._log_error(rollback_device_id, "CONFIG_VERSION_ROLLBACK", f"v={target_version} op={operator}")
        if self._audit:
            await self._audit.log_config_change(
                rollback_device_id,
                [f"rollback_to_v{target_version}"],
                old_config,
                rollback_config,
                operator=operator,
            )
        return rollback_config

    def list_config_versions(self, limit: int = 20, offset: int = 0) -> list[dict]:
        if not self._config_version:
            return []
        return self._config_version.list_versions(limit, offset)

    def get_config_version(self, version: int) -> dict | None:
        if not self._config_version:
            return None
        return self._config_version.get_version(version)

    def diff_config_versions(self, v1: int, v2: int) -> dict:
        if not self._config_version:
            return {}
        return self._config_version.diff_versions(v1, v2)

    def export_config_json(self) -> str:
        if not self._config_version:
            return "{}"
        return self._config_version.export_json()

    async def import_config_json(self, data: str, operator: str = "system") -> bool:
        if not await self.check_permission(Permission.CONFIG_EDIT):
            return False
        if not self._config_version:
            return False
        return self._config_version.import_json(data)

    def verify_config_integrity(self, version: int) -> bool:
        if not self._config_version:
            return False
        return self._config_version.verify_integrity(version)

    def get_audit_trail(self, device_id: str | None = None, action: str | None = None, limit: int = 100) -> list[dict]:
        if not self._audit:
            return []
        if device_id:
            return self._audit.get_by_device(device_id, limit)
        if action:
            return self._audit.get_by_action(action, limit)
        return self._audit.get_recent(limit)

    def get_audit_stats(self) -> dict:
        if not self._audit:
            return {}
        return self._audit.get_stats()

    def export_audit_csv(self) -> str:
        if not self._audit:
            return ""
        return self._audit.export_csv()

    async def audit_write_point(self, device_id: str, point_name: str, value: Any, operator: str = "system", status: str = "success") -> None:
        if self._audit:
            await self._audit.log_write(device_id, point_name, value, operator=operator, status=status)

    async def audit_failover(self, device_id: str, from_host: str, to_host: str) -> None:
        if self._audit:
            await self._audit.log_failover(device_id, from_host, to_host)
