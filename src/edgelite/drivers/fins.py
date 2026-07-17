"""欧姆龙FINS协议驱动 - 基于fins库(TCPFinsConnection)，支持CJ/CP/NJ系列PLC

支持：
- Omron FINS协议 TCP/UDP通信
- CJ/CP/NJ系列PLC
- 批量读取优化 - 减少通信开销
- D/CIO/W/H多区域支持
- 指数退避+随机抖动重连
- 双IP链路冗余（主+备）
- FINS节点地址握手
- 连接状态机
- 测点级健康统计与退化检测
- FINS错误码结构化解析
- 批量读取失败自动降级
- NaN/Inf过滤、冻结值检测、变化率检查
- 测点级deadband/scaling/clamp参数
- 写前回读验证、写速率限制、写值范围校验
- 写操作审计日志、FINS写入错误解析
- 批量写优化：同一区域相邻地址合并
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import random
import secrets
import socket
import struct
import threading
import time
from collections import OrderedDict, defaultdict, deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum, StrEnum
from typing import Any

from edgelite.api.error_codes import FinsDriverErrors
from edgelite.drivers.base import ConnectionState, DriverCapabilities, DriverPlugin, PointValue
from edgelite.drivers.edge_rule_engine import (  # FIXED-P0: 导入规则引擎类，修复NameError
    EdgeRule,
    ModbusEdgeRuleEngine,
)
from edgelite.drivers.edge_triggers import EdgeTriggerExecutor  # FIXED-P0: 导入触发器执行器
from edgelite.drivers.fins_audit import FinsAudit
from edgelite.drivers.fins_config_version import FinsConfigVersionManager
from edgelite.drivers.fins_ota import FinsOtaManager, OtaPackage
from edgelite.drivers.fins_ts_store import FinsOfflineSyncManager, FinsTsStore
from edgelite.drivers.rule_store import RuleStore  # FIXED-P0: 导入规则存储
from edgelite.packet_recorder import record_packet
from edgelite.services.i18n import t as i18n_t


# FIXED-P1: Windows上已关闭socket的fileno()抛OSError而非返回负数
def _is_socket_closed(sock) -> bool:
    try:
        return sock.fileno() < 0
    except OSError:
        return True


logger = logging.getLogger(__name__)

_AREA_MAP = {
    "D": "d",
    "DM": "d",
    "CIO": "c",
    "C": "c",
    "W": "w",
    "WR": "w",
    "H": "h",
    "HR": "h",
    "A": "h",
    "AR": "h",
    "EM": "e",
    "E": "e",
    "TK": "tk",
    "CS": "cs",
    "TS": "ts",
    "TC": "tc",
    "CC": "cc",
    "IR": "ir",
    "DR": "dr",
    "CF": "cf",
    "VM": "vm",
}

_ICF_ROUTED = 0x80
_ICF_DIRECT = 0x00

_DTYPE_MAP = {
    1: "b",
    16: "w",
    32: "r",
}

_FINS_COMMAND_CODES = {
    "0101": "Memory Area Read",
    "0102": "Memory Area Write",
    "0103": "Memory Area Fill",
    "0104": "Memory Area Read Multiple",
}

_FINS_RESPONSE_CODE_MAP: dict[int, tuple[str, str]] = {
    0x0101: (FinsDriverErrors.FINS_ILLEGAL_AREA, "Illegal memory area"),
    0x0102: (FinsDriverErrors.FINS_ILLEGAL_ADDRESS, "Illegal memory address offset"),
    0x0103: (FinsDriverErrors.FINS_ILLEGAL_AREA, "Illegal memory area range"),
    0x0201: (FinsDriverErrors.FINS_ILLEGAL_ADDRESS, "Illegal address offset"),
    0x0202: (FinsDriverErrors.FINS_ILLEGAL_DATA, "Illegal data length"),
    0x0301: (FinsDriverErrors.FINS_ILLEGAL_DATA, "Illegal data type"),
    0x0302: (FinsDriverErrors.FINS_ILLEGAL_DATA, "Illegal data count"),
    0x0401: (FinsDriverErrors.READ_FAILED, "FINS command not supported"),
    0x0402: (FinsDriverErrors.READ_FAILED, "FINS command not executable"),
    0x0501: (FinsDriverErrors.READ_FAILED, "FINS routing table error"),
    0x0502: (FinsDriverErrors.READ_FAILED, "FINS destination not reachable"),
}

_FINS_WRITE_RESPONSE_CODE_MAP: dict[int, tuple[str, str]] = {
    0x0101: (FinsDriverErrors.WRITE_PROTECTED_AREA, "Write to protected/illegal memory area"),
    0x0102: (FinsDriverErrors.FINS_ILLEGAL_ADDRESS, "Write to illegal address offset"),
    0x0103: (FinsDriverErrors.WRITE_PROTECTED_AREA, "Write to illegal memory area range"),
    0x0201: (FinsDriverErrors.FINS_ILLEGAL_ADDRESS, "Write address out of range"),
    0x0202: (FinsDriverErrors.FINS_ILLEGAL_DATA, "Write data length mismatch"),
    0x0301: (FinsDriverErrors.FINS_ILLEGAL_DATA, "Write data type not supported"),
    0x0302: (FinsDriverErrors.FINS_ILLEGAL_DATA, "Write data count invalid"),
    0x0401: (FinsDriverErrors.WRITE_REJECTED, "Write command not supported"),
    0x0402: (FinsDriverErrors.WRITE_REJECTED, "Write command not executable"),
    0x0501: (FinsDriverErrors.WRITE_REJECTED, "Write routing error"),
    0x0502: (FinsDriverErrors.WRITE_REJECTED, "Write destination not reachable"),
    0x1101: (FinsDriverErrors.WRITE_PROTECTED_AREA, "Write protected - CPU in RUN mode"),
    0x1102: (FinsDriverErrors.WRITE_PROTECTED_AREA, "Write protected - write prohibited"),
    0x1103: (FinsDriverErrors.WRITE_PROTECTED_AREA, "Write protected - cannot write to program area"),
}


class FinsConnState(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    NODE_INITIALIZING = "node_initializing"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    OFFLINE = "offline"


class FinsResponseError(Exception):
    def __init__(self, fins_code: int, error_code: str, message: str):
        super().__init__(message)
        self.fins_code = fins_code
        self.error_code = error_code


class FinsWriteError(FinsResponseError):
    pass


class FailoverInFlightTimeout(Exception):
    """Raised when in-flight requests do not complete within the timeout during failover."""

    def __init__(self, pending_count: int, timeout: float):
        self.pending_count = pending_count
        self.timeout = timeout
        super().__init__(f"{pending_count} in-flight requests did not complete within {timeout}s timeout")


@dataclass
class PointHealthStats:
    success_count: int = 0
    fail_count: int = 0
    consecutive_fails: int = 0
    _latency_samples: deque = field(default_factory=lambda: deque(maxlen=100))
    avg_latency_ms: float = 0.0
    last_good_value: Any = None
    last_good_ts: float = 0.0
    frozen_count: int = 0

    def record_success(self, latency_ms: float) -> None:
        self.success_count += 1
        self.consecutive_fails = 0
        self._latency_samples.append(latency_ms)
        if self._latency_samples:
            self.avg_latency_ms = sum(self._latency_samples) / len(self._latency_samples)

    def record_failure(self) -> None:
        self.fail_count += 1
        self.consecutive_fails += 1

    @property
    def success_rate(self) -> float:
        total = self.success_count + self.fail_count
        if total == 0:
            return 1.0
        return self.success_count / total


@dataclass
class WriteAuditEntry:
    timestamp: str
    user: str
    device_id: str
    point_id: str
    area_code: str
    address: str
    old_value: Any
    new_value: Any
    result: str
    error_code: str = ""
    fins_code: str = ""
    verify_ok: bool | None = None


class OmronFinsDriver(DriverPlugin):
    plugin_name = "omron_fins"
    plugin_version = "2.8.0"
    supported_protocols = ("omron_fins", "fins")  # FIXED(P2): 原问题-可变默认值list; 修复-改为tuple
    _required_dependencies: tuple[str, ...] = ("fins",)  # FIXED(P2): 原问题-可变默认值list; 修复-改为tuple
    config_schema = {
        "description": "Omron FINS protocol, supports CJ/CS/CJ2/CP/NJ/NX series PLC. For CS/CJ2 direct mode, set direct_mode=True",
        "required": ["host"],
        "properties": {
            "host": {"type": "string", "description": "PLC IP address", "format": "ipv4"},
            "port": {"type": "integer", "description": "FINS TCP port", "minimum": 1, "maximum": 65535},
        },
        "fields": [
            {
                "name": "host",
                "type": "string",
                "label": "IP Address",
                "description": "PLC IP address",
                "default": "",
                "required": True,
            },  # FIXED-P1: 默认IP改为空
            {
                "name": "port",
                "type": "integer",
                "label": "Port",
                "description": "FINS TCP port, default 9600",
                "default": 9600,
            },
            {
                "name": "backup_host",
                "type": "string",
                "label": "Backup IP",
                "description": "Backup PLC IP for link redundancy. Auto-switch after 3 primary failures",
                "default": "",
            },
            {
                "name": "backup_port",
                "type": "integer",
                "label": "Backup Port",
                "description": "Backup PLC FINS port (default: same as port)",
                "default": 0,
            },
            {
                "name": "transport",
                "type": "string",
                "label": "Transport",
                "description": "Transport mode: tcp or udp",
                "default": "tcp",
                "options": ["tcp", "udp"],
            },
            {
                "name": "udp_retries",
                "type": "integer",
                "label": "UDP Retries",
                "description": "UDP retransmission count on packet loss (0=disabled, default 3)",
                "default": 3,
                "min": 0,
                "max": 10,
            },
            {
                "name": "batch_size",
                "type": "integer",
                "label": "Batch Size",
                "description": "Number of points to read in parallel (default 10)",
                "default": 10,
            },
            {"name": "timeout", "type": "number", "default": 5, "description": "Connection timeout in seconds"},
            {
                "name": "source_node",
                "type": "integer",
                "label": "Source Node",
                "description": "Local FINS node number (default 0)",
                "default": 0,
                "min": 0,
                "max": 255,
            },
            {
                "name": "dest_node",
                "type": "integer",
                "label": "Destination Node",
                "description": "Target FINS node number (default 0=auto, or 1 for CS/CJ2 direct mode)",
                "default": 0,
                "min": 0,
                "max": 255,
            },
            {
                "name": "network_no",
                "type": "integer",
                "label": "Network No.",
                "description": "FINS network number: 0=local/direct (CS/CJ2), 1-127 for network",
                "default": 0,
                "min": 0,
                "max": 255,
            },
            {
                "name": "unit_no",
                "type": "integer",
                "label": "Unit No.",
                "description": "FINS unit number: 0x00=CPU, 0x01=built-in Ethernet, 0xFF=broadcast",
                "default": 0,
                "min": 0,
                "max": 255,
            },
            {
                "name": "command_code",
                "type": "string",
                "label": "Command Code",
                "description": "FINS command code for read operations",
                "default": "0101",
                "options": ["0101", "0102", "0103", "0104"],
            },
            {
                "name": "direct_mode",
                "type": "boolean",
                "label": "Direct Mode (CS/CJ2)",
                "description": "Enable FINS direct mode for CS/CJ2 series. Uses network_no=0, dest_node=1, unit_no=0x01 (built-in Ethernet port). No router required",
                "default": False,
            },
            {
                "name": "plc_series",
                "type": "string",
                "label": "PLC Series",
                "description": "Omron PLC series: CJ (default), CS, CJ2, CP, NJ, NX",
                "default": "CJ",
                "options": ["CJ", "CS", "CJ2", "CP", "NJ", "NX", "auto"],
            },
            {
                "name": "deadband",
                "type": "object",
                "label": "Deadband",
                "description": "Deadband filter: {type: absolute|percent, threshold: number}. Skip reporting if change < threshold",
                "default": None,
            },
            {
                "name": "scaling",
                "type": "object",
                "label": "Scaling",
                "description": "Linear scaling: {ratio: number, offset: number}. Applied as value*ratio+offset",
                "default": None,
            },
            {
                "name": "clamp",
                "type": "object",
                "label": "Clamp",
                "description": "Range clamp: {min: number, max: number}. Values outside range are marked bad quality",
                "default": None,
            },
            {
                "name": "frozen_threshold",
                "type": "integer",
                "label": "Frozen Threshold",
                "description": "Number of consecutive identical values before marking frozen (0=disabled)",
                "default": 0,
            },
            {
                "name": "rate_of_change_limit",
                "type": "number",
                "label": "Rate of Change Limit",
                "description": "Max allowed change per second between reads (0=disabled)",
                "default": 0,
            },
            {
                "name": "points_config",
                "type": "object",
                "label": "Points Config",
                "description": "Per-point config: {address: {deadband, scaling, clamp, frozen_threshold, rate_of_change_limit}}",
                "default": None,
            },
            {
                "name": "write_verify",
                "type": "boolean",
                "label": "Write Verify",
                "description": "Enable write-back verification: write->delay 50ms->read back->compare",
                "default": False,
            },
            {
                "name": "write_rate_limit_ms",
                "type": "integer",
                "label": "Write Rate Limit (ms)",
                "description": "Minimum interval between writes to the same register (default 500ms, 0=disabled)",
                "default": 500,
            },
            {
                "name": "write_audit",
                "type": "boolean",
                "label": "Write Audit",
                "description": "Enable write operation audit logging",
                "default": True,
            },
            {
                "name": "fins_max_response_size",
                "type": "integer",
                "label": "Max Response Size",
                "description": "Maximum FINS response size in bytes to prevent memory exhaustion (default 65536=64KB)",
                "default": 65536,
                "min": 1024,
                "max": 1048576,
            },
        ],
    }

    experimental = False
    capabilities = DriverCapabilities(
        discover=True, read=True, write=True, subscribe=False, batch_read=True, batch_write=True
    )
    constraints = (
        {
            "type": "protocol_note",
            "message": "UDP mode may experience packet loss; source/dest node and network number must match remote configuration",
        },
    )  # FIXED(P2): 原问题-可变默认值list; 修复-改为tuple

    _RECONNECT_BASE_DELAY = 1.0
    _RECONNECT_MAX_DELAY = 60.0
    _RECONNECT_MAX_ATTEMPTS = 10
    _JITTER_MAX_MS = 1000
    _FAILOVER_THRESHOLD = 3
    _FAILOVER_PROBE_INTERVAL = 30
    _READ_TIMEOUT = 30
    _WRITE_TIMEOUT = 10
    # FINS-MED-001: 自适应 watchdog 间隔配置
    _WATCHDOG_INTERVAL_ONLINE = 10  # 在线时间隔
    _WATCHDOG_INTERVAL_OFFLINE_FAST = 5  # 首次离线快速检测
    _WATCHDOG_INTERVAL_OFFLINE_MAX = 60  # 离线最大间隔
    _DEGRADATION_SUCCESS_RATE = 0.8
    _FROZEN_WINDOW = 10
    _BATCH_SPLIT_MIN = 2
    _WRITE_VERIFY_DELAY_MS = 50
    _WRITE_RATE_LIMIT_MS = 500
    _WRITE_MERGE_MAX_GAP = 1
    _STANDBY_KEEPALIVE_INTERVAL = 10
    _FAILOVER_FAST_TIMEOUT = 3.0
    _DEFAULT_MAX_FINS_RESPONSE_SIZE = 65536  # FINS-004: 默认 64KB
    MAX_FINS_RESPONSE_SIZE = _DEFAULT_MAX_FINS_RESPONSE_SIZE
    # FINS-P1: UDP 重传默认配置 — UDP 丢包时应用层重传，工业现场丢包率 0.1%-1%
    _DEFAULT_UDP_RETRIES = 3  # 默认重传 3 次 (总尝试 4 次)
    _UDP_BACKOFF_BASE = 0.01  # 首次重传退避 10ms
    _UDP_BACKOFF_CAP = 0.20  # 退避上限 200ms

    def __init__(self):
        super().__init__()  # FIXED-P0: 基类属性未初始化
        self._running = False
        self._delayed_reconnect_count: int = 0  # FIXED-P2: 在__init__中初始化，而非hasattr惰性创建
        self._permanent_offline: set[str] = set()  # FIXED-P2: 永久离线设备集合，防止延迟重连无限循环
        self._client = None
        self._config: dict = {}
        self._lock = asyncio.Lock()
        self._client_lock = threading.RLock()  # FIXED-P1: 改为可重入锁，防止嵌套调用死锁
        self._async_client_lock = asyncio.Lock()
        self._in_flight_requests: int = 0
        self._in_flight_lock = threading.Lock()
        # FIXED-P0: 标记socket是否正被读取路径使用，failover/reconnect路径据此决定是否强制中断
        self._socket_in_use: bool = False
        self._reconnect_attempt: int = 0
        # FIXED-P2: 重连串行化锁，防止并发重连导致_reconnect_attempt自增竞态
        self._reconnect_lock: asyncio.Lock = asyncio.Lock()
        self._reconnect_delay: float = self._RECONNECT_BASE_DELAY
        self._devices: dict[str, dict] = {}
        self._watchdog_task: asyncio.Task | None = None
        # FINS-MED-001: 自适应 watchdog 状态
        self._watchdog_offline_count: int = 0  # 连续离线次数
        # _health_stats inherited from base class (dict[str, DriverHealthStats])  # FIXED-P2: 删除遮蔽基类的覆盖初始化
        self._source_node: int = 0
        self._dest_node: int = 0
        self._network_no: int = 0
        self._unit_no: int = 0
        self._command_code: str = "0101"
        self._is_direct_mode: bool = False
        self._plc_series: str = "CJ"
        # CROSS-003: 使用 OrderedDict 限制 _last_values 容量
        self._MAX_LAST_VALUES = 10000
        self._MAX_DICT_SIZE = 10000  # FIXED-P0: 字典容量上限，防止无界增长
        self._last_values: OrderedDict[str, Any] = OrderedDict()
        self._conn_state: FinsConnState = FinsConnState.DISCONNECTED
        self._primary_ip: str = ""
        self._primary_port: int = 9600
        self._backup_ip: str = ""
        self._backup_port: int = 0
        self._active_ip: str = ""
        self._active_port: int = 9600
        self._using_backup: bool = False
        self._primary_fail_count: int = 0
        self._failover_probe_task: asyncio.Task | None = None
        self._point_stats: OrderedDict[str, PointHealthStats] = OrderedDict()  # FIXED-P0: 改用OrderedDict支持LRU淘汰
        self._point_configs: dict[str, dict] = {}
        self._global_frozen_threshold: int = 0
        self._global_rate_limit: float = 0.0
        self._degraded: bool = False
        self._degraded_interval_ms: int = 0
        self._last_read_ts: dict[str, float] = {}
        self._frozen_counters: dict[str, int] = {}
        self._write_verify_enabled: bool = False
        self._write_rate_limit_ms: int = self._WRITE_RATE_LIMIT_MS
        self._write_audit_enabled: bool = True
        self._last_write_ts: dict[str, float] = {}
        self._audit_log: deque = deque(maxlen=1000)
        self._quality_history: dict[str, deque] = {}
        self._device_points: dict[str, list[dict]] = {}
        self._standby_client = None
        self._standby_task: asyncio.Task | None = None
        self._init_standby_task: asyncio.Task | None = None
        self._standby_ready: bool = False
        self._standby_dest_node: int = 0
        self._standby_network_no: int = 0
        self._failover_ts: float = 0.0
        self._delayed_reconnect_task: asyncio.Task | None = None
        # FIXED-P1: 后台重连任务引用，read/write路径非阻塞调度重连，避免阻塞采集循环
        self._bg_reconnect_task: asyncio.Task | None = None
        self._first_reconnect_time: float = 0.0  # FIXED-P1: 首次重连时间戳，用于限制全局最大重连时间窗口
        self._edge_rule_engine: ModbusEdgeRuleEngine | None = None
        self._edge_trigger: EdgeTriggerExecutor | None = None
        self._rule_store: RuleStore | None = None
        self._ts_store: FinsTsStore | None = None
        self._offline_sync: FinsOfflineSyncManager | None = None
        self._network_online: bool = True
        self._config_version_mgr: FinsConfigVersionManager | None = None
        self._audit: FinsAudit | None = None
        self._ota_mgr: FinsOtaManager | None = None
        self._current_user_role: str = "viewer"
        # FINS-001: 独立线程池，避免阻塞其他驱动的 to_thread 调用
        self._thread_pool: ThreadPoolExecutor | None = None
        self._thread_pool_failed: bool = False  # FIXED-P0: 线程池卡死标志，触发重建
        self._thread_pool_lock: asyncio.Lock = asyncio.Lock()  # FIXED-P0: 线程池重建+submit原子性保护锁
        # FIXED-P2: 线程池创建时间戳，用于定期重建防止僵尸线程累积
        self._thread_pool_created_at: float = 0.0
        self._THREAD_POOL_MAX_AGE_SECONDS: float = 3600.0  # 线程池最大存活1小时
        # FINS-P1: UDP 重传状态 — start() 中从 config 读取 udp_retries 覆盖默认值
        self._is_udp: bool = False
        self._udp_max_retries: int = self._DEFAULT_UDP_RETRIES

    async def _run_in_thread(self, func: Callable, *args, timeout: float = 10.0, **kwargs):
        """FINS-001: 在独立线程池中执行同步函数，避免阻塞全局线程池
        FIXED-P1: FINS-01 增加超时保护，防止fins库同步调用永久阻塞协程
        FIXED-P0: 超时后标记线程池failed并重建，避免卡死线程耗尽线程池
        FIXED-P0: 超时后显式关闭 client.fins_socket 中断阻塞 recv，
                 缩短 socket settimeout 至 1.0s，防止 future.cancel() 无法终止已运行的同步 I/O
        """
        async with self._thread_pool_lock:  # FIXED-P0: 线程池重建+submit原子性保护，防止并发重建
            # FIXED-P2: 线程池超过最大存活时间时主动重建，防止僵尸线程累积
            _now = time.monotonic()
            if (
                self._thread_pool is not None
                and self._thread_pool_created_at > 0
                and _now - self._thread_pool_created_at > self._THREAD_POOL_MAX_AGE_SECONDS
            ):
                self._thread_pool_failed = True
                logger.info("[fins] code=THREAD_POOL_AGED msg=Thread pool exceeded max age, scheduling rebuild")
            if self._thread_pool_failed:
                old_pool = self._thread_pool
                self._thread_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="fins-worker-")
                self._thread_pool_failed = False
                self._thread_pool_created_at = time.monotonic()
                if old_pool is not None:
                    try:
                        old_pool.shutdown(wait=False, cancel_futures=True)
                    except Exception as e:
                        logger.warning(
                            "[fins] run_in_thread failed: %s", e
                        )  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                logger.warning("[fins] code=THREAD_POOL_REBUILT msg=FINS thread pool rebuilt after timeout")
            if self._thread_pool is None:
                self._thread_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="fins-worker-")
                self._thread_pool_created_at = time.monotonic()
            future = self._thread_pool.submit(func, *args, **kwargs)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), timeout=timeout)
        except TimeoutError:
            self._thread_pool_failed = True  # FIXED-P0: 标记线程池可能有卡死线程，下次调用时重建
            future.cancel()
            # FIXED-P0: future.cancel() 无法中断已运行的同步 sock.recv，显式关闭 socket 强制中断阻塞 I/O
            # 之前：超时后仅 future.cancel()，卡死线程仍占用 max_workers=4 线程池槽位，多设备并发握手失败时耗尽
            # 之后：关闭 client.fins_socket 触发阻塞 recv 抛出 OSError，使卡死线程退出
            try:
                c = self._client
                if c is not None and hasattr(c, "fins_socket") and c.fins_socket is not None:
                    await asyncio.wait_for(
                        asyncio.to_thread(c.fins_socket.close),
                        timeout=1.0,
                    )
                    logger.debug(
                        "[fins] code=SOCKET_CLOSED_AFTER_TIMEOUT msg=Closed fins_socket to interrupt blocking recv"
                    )
            except TimeoutError:
                logger.warning("[fins] code=SOCKET_CLOSE_TIMEOUT msg=fins_socket.close timed out after 1.0s")
            except Exception as e:
                logger.debug("[fins] fins_socket.close after timeout failed: %s", e)
            raise

    def _log_error(self, error_code: str, device_id: str, detail: str) -> None:
        msg = i18n_t(error_code)
        if detail:
            logger.error("[fins] device=%s code=%s msg=%s detail=%s", device_id, error_code, msg, detail)
        else:
            logger.error("[fins] device=%s code=%s msg=%s", device_id, error_code, msg)

    def _set_fins_state(self, state: FinsConnState, device_id: str = "", reason: str = "") -> None:
        with self._client_lock:  # FIXED-P0: _conn_state赋值加锁保护，防止watchdog与重连并发读写竞态
            old = self._conn_state
            self._conn_state = state
        if old != state:
            logger.info("[fins] state=%s->%s device=%s reason=%s", old.value, state.value, device_id, reason)
        base_state = {
            FinsConnState.DISCONNECTED: ConnectionState.DISCONNECTED.value,
            FinsConnState.CONNECTING: ConnectionState.CONNECTING.value,
            FinsConnState.NODE_INITIALIZING: ConnectionState.CONNECTING.value,
            FinsConnState.CONNECTED: ConnectionState.CONNECTED.value,
            FinsConnState.DEGRADED: ConnectionState.DEGRADED.value,
            FinsConnState.OFFLINE: ConnectionState.OFFLINE.value,
        }.get(state, ConnectionState.DISCONNECTED.value)
        if device_id:
            try:
                loop = asyncio.get_running_loop()
                task = loop.create_task(self._set_connection_state(device_id, base_state, reason))
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
            except RuntimeError:
                # FIXED-P1: 从线程池调用时get_running_loop()失败，使用call_soon_threadsafe调度到主事件循环
                try:
                    loop = asyncio.get_event_loop()
                    loop.call_soon_threadsafe(lambda: self._schedule_state_update(device_id, base_state, reason))
                except Exception as e:
                    logger.warning(
                        "[fins] set_fins_state failed: %s", e
                    )  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录

    def _schedule_state_update(
        self, device_id: str, base_state: str, reason: str
    ) -> None:  # FIXED-P1: 线程安全的状态更新调度辅助方法
        try:
            loop = asyncio.get_event_loop()
            task = loop.create_task(self._set_connection_state(device_id, base_state, reason))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        except Exception as e:
            logger.warning(
                "[fins] schedule_state_update failed: %s", e
            )  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录

    def _set_last_value(self, point: str, value: Any) -> None:
        """CROSS-003: 设置 last_value，带 LRU 淘汰机制"""
        if point in self._last_values:
            self._last_values.move_to_end(point)
        self._last_values[point] = value
        # 超过容量时淘汰最旧条目
        while len(self._last_values) > self._MAX_LAST_VALUES:
            self._last_values.pop(next(iter(self._last_values)))

    def _calc_backoff_delay(self) -> float:
        delay = min(self._RECONNECT_BASE_DELAY * (2**self._reconnect_attempt), self._RECONNECT_MAX_DELAY)
        jitter = random.uniform(0, self._JITTER_MAX_MS) / 1000.0
        return delay + jitter

    def _get_active_endpoint(self) -> tuple[str, int]:
        if self._using_backup and self._backup_ip:
            return self._backup_ip, self._backup_port or self._primary_port
        return self._primary_ip, self._primary_port

    def _get_point_config(self, address: str) -> dict:
        return self._point_configs.get(address, {})

    def _get_point_deadband(self, address: str) -> Any:
        pc = self._get_point_config(address)
        if "deadband" in pc:
            return pc["deadband"]
        return self._config.get("deadband")

    def _get_point_scaling(self, address: str) -> Any:
        pc = self._get_point_config(address)
        if "scaling" in pc:
            return pc["scaling"]
        return self._config.get("scaling")

    def _get_point_clamp(self, address: str) -> Any:
        pc = self._get_point_config(address)
        if "clamp" in pc:
            return pc["clamp"]
        return self._config.get("clamp")

    def _get_point_frozen_threshold(self, address: str) -> int:
        pc = self._get_point_config(address)
        if "frozen_threshold" in pc:
            return pc["frozen_threshold"]
        return self._global_frozen_threshold

    def _get_point_rate_limit(self, address: str) -> float:
        pc = self._get_point_config(address)
        if "rate_of_change_limit" in pc:
            return pc["rate_of_change_limit"]
        return self._global_rate_limit

    def _filter_nan_inf(self, value: Any, address: str) -> Any:
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            self._log_error(FinsDriverErrors.FINS_NAN_INF, address, f"value={value}")
            return None
        return value

    def _detect_frozen_value(self, address: str, value: Any) -> bool:
        threshold = self._get_point_frozen_threshold(address)
        if threshold <= 0:
            return False
        last = self._last_values.get(address)
        if last is not None and value == last:
            if (
                len(self._frozen_counters) >= self._MAX_DICT_SIZE and address not in self._frozen_counters
            ):  # FIXED-P0: 字典无界增长，淘汰最旧条目
                self._frozen_counters.pop(next(iter(self._frozen_counters)), None)
            self._frozen_counters[address] = self._frozen_counters.get(address, 0) + 1
            if self._frozen_counters[address] >= threshold:
                self._log_error(FinsDriverErrors.FINS_FROZEN_VALUE, address, f"count={self._frozen_counters[address]}")
                return True
        else:
            self._frozen_counters[address] = 0
        return False

    def _check_rate_of_change(self, address: str, value: Any) -> bool:
        rate_limit = self._get_point_rate_limit(address)
        if rate_limit <= 0:
            return True
        if not isinstance(value, (int, float)):
            return True
        last = self._last_values.get(address)
        if last is None or not isinstance(last, (int, float)):
            return True
        last_ts = self._last_read_ts.get(address, 0)
        now = time.monotonic()  # FIXED-P1: 使用monotonic时钟，防止NTP时钟回拨导致速率限制失效
        dt = now - last_ts
        if dt <= 0:
            return True
        rate = abs(value - last) / dt
        if rate > rate_limit:
            self._log_error(FinsDriverErrors.FINS_RATE_OF_CHANGE, address, f"rate={rate:.2f}/s limit={rate_limit}/s")
            return False
        return True

    def _record_point_success(self, address: str, latency_ms: float) -> None:
        stats = self._point_stats.get(address)
        if stats is None:
            if len(self._point_stats) >= self._MAX_DICT_SIZE:  # FIXED-P0: LRU淘汰最旧条目
                self._point_stats.popitem(last=False)
            stats = PointHealthStats()
            self._point_stats[address] = stats
        else:
            self._point_stats.move_to_end(address)  # FIXED-P0: LRU访问时移到末尾
        stats.record_success(latency_ms)

    def _record_point_failure(self, address: str) -> None:
        stats = self._point_stats.get(address)
        if stats is None:
            if len(self._point_stats) >= self._MAX_DICT_SIZE:  # FIXED-P0: LRU淘汰最旧条目
                self._point_stats.popitem(last=False)
            stats = PointHealthStats()
            self._point_stats[address] = stats
        else:
            self._point_stats.move_to_end(address)  # FIXED-P0: LRU访问时移到末尾
        stats.record_failure()

    def _check_degradation(self) -> None:
        if not self._point_stats:
            return
        total_success = sum(s.success_count for s in self._point_stats.values())
        total_fail = sum(s.fail_count for s in self._point_stats.values())
        total = total_success + total_fail
        if total < 10:
            return
        rate = total_success / total
        was_degraded = self._degraded
        self._degraded = rate < self._DEGRADATION_SUCCESS_RATE
        if self._degraded and not was_degraded:
            self._degraded_interval_ms = int(2000 * (1.0 - rate))
            self._log_error(
                FinsDriverErrors.FINS_DEGRADED_FREQ,
                "global",
                f"rate={rate:.2%} interval_ms={self._degraded_interval_ms}",
            )
        elif not self._degraded and was_degraded:
            self._degraded_interval_ms = 0

    def get_point_health(self, address: str) -> dict:
        stats = self._point_stats.get(address)
        if stats is None:
            return {
                "success_count": 0,
                "fail_count": 0,
                "avg_latency_ms": 0.0,
                "consecutive_fails": 0,
                "success_rate": 1.0,
            }
        return {
            "success_count": stats.success_count,
            "fail_count": stats.fail_count,
            "avg_latency_ms": stats.avg_latency_ms,
            "consecutive_fails": stats.consecutive_fails,
            "success_rate": stats.success_rate,
        }

    def get_point_stats(self, device_id: str, point_name: str) -> dict | None:
        stats = self._point_stats.get(point_name)
        if stats is None:
            return None
        q_hist = list(self._quality_history.get(point_name, deque(maxlen=100)))
        current_q = "bad" if stats.consecutive_fails > 3 else ("uncertain" if stats.consecutive_fails > 0 else "good")
        last_success = stats.last_good_ts
        return {
            "success_count": stats.success_count,
            "fail_count": stats.fail_count,
            "avg_latency_ms": stats.avg_latency_ms,
            "consecutive_fails": stats.consecutive_fails,
            "success_rate": stats.success_rate,
            "quality_history": q_hist,
            "current_quality": current_q,
            "last_success_at": datetime.fromtimestamp(last_success, UTC).isoformat() if last_success else None,
        }

    def get_write_audit_log(self, device_id: str, limit: int = 1000) -> list[dict]:
        entries = list(self._audit_log)[-limit:]
        return [e for e in entries if e.get("device_id") == device_id or not device_id]

    def _trim_dict(self, d: dict, key: str) -> None:  # FIXED-P0: 字典无界增长，淘汰最旧条目
        if len(d) >= self._MAX_DICT_SIZE and key not in d:
            d.pop(next(iter(d)), None)

    def _record_quality(self, address: str, quality: str) -> None:
        if address not in self._quality_history:
            if len(self._quality_history) >= self._MAX_DICT_SIZE:  # FIXED-P0: 字典无界增长，淘汰最旧条目
                self._quality_history.pop(next(iter(self._quality_history)), None)
            self._quality_history[address] = deque(maxlen=100)
        self._quality_history[address].append(quality)

    def _validate_write_value(self, value: Any, data_type: str) -> tuple[bool, str]:
        if data_type == "b":
            if value not in (0, 1, True, False):
                return False, f"bit value must be 0 or 1, got {value}"
            return True, ""
        if data_type == "w":
            try:
                iv = int(value)
            except (ValueError, TypeError):
                return False, f"word value must be integer, got {value}"
            if not (0 <= iv <= 65535):
                return False, f"word value out of range [0-65535], got {iv}"
            return True, ""
        if data_type == "ui":
            try:
                iv = int(value)
            except (ValueError, TypeError):
                return False, f"unsigned int value must be integer, got {value}"
            if not (0 <= iv <= 65535):
                return False, f"unsigned int value out of range [0-65535], got {iv}"
            return True, ""
        if data_type == "dw":
            try:
                iv = int(value)
            except (ValueError, TypeError):
                return False, f"dword value must be integer, got {value}"
            if not (0 <= iv <= 4294967295):
                return False, f"dword value out of range [0-4294967295], got {iv}"
            return True, ""
        if data_type == "i":
            try:
                iv = int(value)
            except (ValueError, TypeError):
                return False, f"signed int value must be integer, got {value}"
            if not (-32768 <= iv <= 32767):
                return False, f"signed int value out of range [-32768-32767], got {iv}"
            return True, ""
        if data_type == "float":
            try:
                fv = float(value)
            except (ValueError, TypeError):
                return False, f"float value must be number, got {value}"
            if math.isnan(fv) or math.isinf(fv):
                return False, f"float value must not be NaN/Inf, got {fv}"
            return True, ""
        if data_type == "r":
            try:
                fv = float(value)
            except (ValueError, TypeError):
                return False, f"real value must be number, got {value}"
            if math.isnan(fv) or math.isinf(fv):
                return False, f"real value must not be NaN/Inf, got {fv}"
            return True, ""
        return True, ""

    def _check_write_rate(self, point: str) -> tuple[bool, float]:
        if self._write_rate_limit_ms <= 0:
            return True, 0.0
        now = time.monotonic()  # FIXED-P1: 使用monotonic时钟，防止NTP时钟回拨导致速率限制失效
        last_ts = self._last_write_ts.get(point, 0)
        elapsed_ms = (now - last_ts) * 1000
        if elapsed_ms < self._write_rate_limit_ms:
            return False, self._write_rate_limit_ms - elapsed_ms
        return True, 0.0

    def _audit_write(
        self,
        device_id: str,
        point: str,
        area: str,
        offset: int,
        old_value: Any,
        new_value: Any,
        result: str,
        error_code: str = "",
        fins_code: int = 0,
        verify_ok: bool | None = None,
    ) -> None:
        if not self._write_audit_enabled:
            return
        entry = WriteAuditEntry(
            timestamp=datetime.now(UTC).isoformat(),
            user="system",
            device_id=device_id,
            point_id=point,
            area_code=area,
            address=f"{area}:{offset}",
            old_value=old_value,
            new_value=new_value,
            result=result,
            error_code=error_code,
            fins_code=f"0x{fins_code:04X}" if fins_code else "",
            verify_ok=verify_ok,
        )
        self._audit_log.append(entry)
        logger.info(
            "[fins-audit] ts=%s device=%s point=%s area=%s addr=%s old=%s new=%s result=%s err=%s fins=%s verify=%s",
            entry.timestamp,
            entry.device_id,
            entry.point_id,
            entry.area_code,
            entry.address,
            entry.old_value,
            entry.new_value,
            entry.result,
            entry.error_code,
            entry.fins_code,
            entry.verify_ok,
        )

    def get_audit_log(self, limit: int = 100) -> list[dict]:
        entries = list(self._audit_log)[-limit:]
        return [
            {
                "timestamp": e.timestamp,
                "user": e.user,
                "device_id": e.device_id,
                "point_id": e.point_id,
                "area_code": e.area_code,
                "address": e.address,
                "old_value": e.old_value,
                "new_value": e.new_value,
                "result": e.result,
                "error_code": e.error_code,
                "fins_code": e.fins_code,
                "verify_ok": e.verify_ok,
            }
            for e in entries
        ]

    def _merge_adjacent_writes(self, writes: list[tuple[str, Any]]) -> list[tuple[str, Any, bool]]:
        if len(writes) <= 1:
            return [(w[0], w[1], False) for w in writes]

        parsed = []
        for point, value in writes:
            try:
                area, offset, data_type = self._parse_address(point)
                parsed.append((point, value, area, offset, data_type))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(
                    "[fins] address parse failed for point %s: %s", point, e
                )  # FIXED-P1: 原问题-地址解析异常填充无效值无日志
                parsed.append((point, value, "", -1, ""))

        merged = []
        i = 0
        while i < len(parsed):
            curr = parsed[i]
            if curr[3] < 0:
                merged.append((curr[0], curr[1], False))
                i += 1
                continue

            j = i + 1
            while j < len(parsed):
                nxt = parsed[j]
                if (
                    nxt[2] == curr[2]
                    and nxt[3] == parsed[j - 1][3] + 1
                    and nxt[4] == curr[4]
                    and nxt[4] in ("w", "ui", "i")
                ):
                    j += 1
                else:
                    break

            if j - i > 1:
                self._log_error(FinsDriverErrors.WRITE_MERGE, curr[0], f"merged {j - i} writes in area={curr[2]}")
                for k in range(i, j):
                    merged.append((parsed[k][0], parsed[k][1], True))
            else:
                merged.append((curr[0], curr[1], False))
            i = j

        return merged

    def _wrap_udp_retransmission(self, client) -> None:
        """FINS-P1: 为 UDP FINS 连接的 execute_fins_command_frame 注入应用层重传逻辑。

        背景: UDPFinsConnection.execute_fins_command_frame 单次 sendto + recvfrom，
              UDP 丢包时 recvfrom 抛 socket.timeout 直接失败。工业现场 UDP 丢包率
              可达 0.1%-1%，导致 FINS 读写在 UDP 模式下频繁失败。

        行为:
          - socket.timeout (丢包) 时按指数退避重传: 10ms, 20ms, 40ms ... 上限 200ms
          - 最多重传 _udp_max_retries 次 (默认 3 次，总尝试 4 次)
          - 非 timeout 的 OSError (连接重置/拒绝等不可恢复错误) 不重传，立即抛出
          - udp_retries=0 时不包装，保留原始单次行为
          - 幂等: 重复包装已包装的方法安全 (通过 _fins_retrans_wrapped 标记跳过)
        """
        max_retries = getattr(self, "_udp_max_retries", self._DEFAULT_UDP_RETRIES)
        # udp_retries=0 → 禁用重传，保留原始方法 (不替换)
        if max_retries <= 0:
            return

        original = client.execute_fins_command_frame
        # 幂等保护: 已包装过则不再重复包装，避免嵌套导致重试次数指数放大
        if getattr(original, "_fins_retrans_wrapped", False):
            return

        driver = self

        def wrapped_execute(fins_command_frame: bytes):
            # 总尝试 1 + max_retries 次; 首次不 sleep, 之后每次重传前指数退避
            last_timeout: socket.timeout | None = None
            for attempt in range(max_retries + 1):
                try:
                    return original(fins_command_frame)
                except TimeoutError as exc:
                    # socket.timeout (Python 3.10+ 即 TimeoutError) → 丢包，可重传
                    last_timeout = exc
                    if attempt < max_retries:
                        backoff = min(
                            driver._UDP_BACKOFF_BASE * (2**attempt),
                            driver._UDP_BACKOFF_CAP,
                        )
                        time.sleep(backoff)
                        continue
                    # 重传耗尽 → 抛出最后一次超时
                    raise
                except OSError:
                    # 非 timeout 的 OSError (连接重置/拒绝等) → 不可恢复，立即抛出
                    raise
            # 逻辑上不可达 (循环必 return 或 raise); 保险起见抛出最后一次超时
            if last_timeout is not None:
                raise last_timeout
            raise RuntimeError("UDP retransmission loop exited unexpectedly")

        wrapped_execute._fins_retrans_wrapped = True  # type: ignore[attr-defined]
        client.execute_fins_command_frame = wrapped_execute

    async def _do_connect(self, ip: str, port: int) -> None:
        transport = self._config.get("transport", "tcp").lower()
        if transport == "udp":
            from fins.udp import UDPFinsConnection

            new_client = UDPFinsConnection()
        else:
            from fins.tcp import TCPFinsConnection

            new_client = TCPFinsConnection()
        try:
            await asyncio.wait_for(
                self._run_in_thread(new_client.connect, ip, port),
                timeout=self._config.get("timeout", 5),
            )
        except Exception:
            try:  # FIXED-P2: 连接失败时显式关闭new_client，防止socket泄漏
                new_client.close()
            except Exception as e:
                logger.warning("[fins] do_connect failed: %s", e)  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
            raise
        # FINS-P1: UDP 模式注入应用层重传 (丢包自动重传)
        if transport == "udp":
            self._wrap_udp_retransmission(new_client)
        with self._client_lock:  # FIXED-P0: 替换client时与读取互斥
            # FIXED-P1: 并发_do_connect时，若已有更新的client则关闭当前new_client，防止socket泄漏
            old_client = self._client
            if old_client is not None and old_client is not new_client:
                try:
                    old_client.close()
                # FIXED-P1: 原问题-except Exception: pass 静默吞没异常，改为至少 logger.debug
                except Exception as e:
                    logger.debug("[fins] old_client.close() failed: %s", e)
            self._client = new_client

    async def _fins_node_handshake(self, client=None, update_shared_state: bool = True) -> bool:
        # FIXED-P2: 将同步socket操作移入线程池执行，避免阻塞事件循环最长10秒
        return await self._run_in_thread(
            self._fins_node_handshake_sync,
            client,
            update_shared_state,
            timeout=15.0,
        )

    def _fins_node_handshake_sync(self, client=None, update_shared_state: bool = True) -> bool:
        c = client or self._client
        if not c or not hasattr(c, "fins_socket") or not c.fins_socket:
            return False
        try:
            sock = c.fins_socket
            # FIXED-P0: 在handshake socket上显式settimeout(1.0)，缩短单次 send/recv 超时
            # 原问题：sock.settimeout(2.0) 使单个 recv 可阻塞 2 秒，多设备并发握手失败时
            #         max_workers=4 线程池被耗尽，future.cancel() 无法中断已运行的同步 recv
            # 修复：单个 send/recv 超时设为 1 秒，总体超时 10 秒允许最多 10 次重试；
            #       超时后 _run_in_thread 显式关闭 fins_socket 中断阻塞 recv
            sock.settimeout(1.0)
            hs_deadline = time.monotonic() + 10.0  # FIXED-P0: 握手recv总体超时10秒
            sid = secrets.randbits(8)  # FIXED-P1: 使用不可预测的随机数生成会话ID，防止会话劫持
            fins_frame = bytes(
                [
                    0x80,
                    0x00,
                    0x02,
                    self._network_no & 0xFF,
                    self._dest_node & 0xFF,
                    self._unit_no & 0xFF,
                    0x00,
                    self._source_node & 0xFF,
                    0x00,
                    sid & 0xFF,
                    0x05,
                    0x01,
                ]
            )
            tcp_frame = b"FINS" + struct.pack(">I", len(fins_frame)) + fins_frame
            sock.send(tcp_frame)
            header = b""
            while len(header) < 8:
                if time.monotonic() > hs_deadline:  # FIXED-P0: 握手header读取总体超时
                    try:
                        sock.close()
                    except Exception as e:
                        logger.debug(
                            "[fins] handshake header timeout, sock.close failed: %s", e
                        )  # FIXED-P2: 原问题-异常被静默吞没
                    return False
                chunk = sock.recv(8 - len(header))
                if not chunk:
                    try:
                        sock.close()  # FIXED-P2: 握手失败时关闭socket，避免连接泄漏
                    except Exception as e:
                        logger.debug("[fins] handshake no chunk, sock.close failed: %s", e)  # FIXED-P2
                    return False
                header += chunk
            if header[:4] != b"FINS":
                try:
                    sock.close()  # FIXED-P2: 握手失败时关闭socket
                except Exception as e:
                    logger.debug("[fins] handshake invalid header, sock.close failed: %s", e)  # FIXED-P2
                return False
            data_len = struct.unpack(">I", header[4:8])[0]
            # FINS-004: 检查响应大小上限
            if data_len > self._max_response_size:
                logger.error(
                    "[fins] code=RESPONSE_TOO_LARGE device=%s size=%d max=%d, closing connection",
                    self._primary_ip,
                    data_len,
                    self._max_response_size,
                )
                try:
                    sock.close()
                except Exception as e:
                    logger.warning("[fins] operation failed: %s", e)  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                raise ValueError(f"FINS handshake response too large: {data_len} > {self._max_response_size}")
            response = b""
            while len(response) < data_len:
                if time.monotonic() > hs_deadline:  # FIXED-P0: 握手response读取总体超时
                    try:
                        sock.close()
                    except Exception as e:
                        logger.debug("[fins] handshake response timeout, sock.close failed: %s", e)  # FIXED-P2
                    return False
                chunk = sock.recv(data_len - len(response))
                if not chunk:
                    break
                response += chunk
            if len(response) < 12:
                try:
                    sock.close()
                except Exception as e:
                    logger.debug("[fins] handshake response too short, sock.close failed: %s", e)  # FIXED-P2
                return False
            if len(response) < data_len:  # FIXED-P2: 响应不完整时拒绝处理，防止解析错误数据
                logger.warning(
                    "[fins] handshake response incomplete: got %d bytes, expected %d", len(response), data_len
                )
                try:
                    sock.close()
                except Exception as e:
                    logger.debug("[fins] handshake response incomplete, sock.close failed: %s", e)  # FIXED-P2
                return False
            mrc = response[10]
            src = response[11]
            if mrc == 0x05 and src == 0x01:
                if len(response) >= 16:
                    peer_dest_node = response[7]
                    peer_network_no = response[6]
                    if update_shared_state:
                        # FIXED-P2: 节点信息写入改为无锁原子赋值（Python属性赋值是原子的）
                        # 之前：with self._client_lock 与 _fins_tcp_request_inner 锁竞争可导致握手超时
                        self._dest_node = peer_dest_node
                        self._network_no = peer_network_no
                    else:
                        # FIXED-P2: 备用状态写入改为无锁原子赋值
                        self._standby_dest_node = peer_dest_node
                        self._standby_network_no = peer_network_no
                return True
            return False
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("[fins] node handshake error: %s", e)
            try:
                if c and hasattr(c, "fins_socket") and c.fins_socket:
                    c.fins_socket.close()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("[fins] operation failed: %s", e)
            # FIXED-P2: 握手失败后重置self._client，防止后续使用已关闭socket
            if client is None and self._client is c:
                with self._client_lock:  # FIXED-P0: 替换client时与读取互斥
                    if self._client is c:
                        self._client = None
            return False

    async def _connect_with_handshake(self, device_id: str) -> bool:
        ip, port = self._get_active_endpoint()
        self._set_fins_state(FinsConnState.CONNECTING, device_id, f"{ip}:{port}")
        try:
            await self._do_connect(ip, port)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._log_error(FinsDriverErrors.CONN_FAILED, ip, str(e))
            return False

        self._set_fins_state(FinsConnState.NODE_INITIALIZING, device_id, ip)
        handshake_ok = await self._fins_node_handshake()
        if not handshake_ok:
            self._log_error(FinsDriverErrors.NODE_INIT_FAILED, ip, "")
            self._set_fins_state(FinsConnState.OFFLINE, device_id, "node handshake failed")
            # FIXED-P0: 握手失败时关闭并置空client，防止后续使用不可用的client
            with self._client_lock:
                if self._client:
                    try:
                        if hasattr(self._client, "fins_socket") and self._client.fins_socket:
                            self._client.fins_socket.close()
                    except Exception as e:
                        logger.warning(
                            "[fins] connect_with_handshake failed: %s", e
                        )  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                    self._client = None
            return False

        self._log_error(FinsDriverErrors.NODE_INIT_OK, ip, f"dest_node={self._dest_node}")
        self._permanent_offline.discard(device_id)  # FIXED-P2: 连接成功时从永久离线集合中移除设备
        self._active_ip = ip
        self._active_port = port
        self._running = True
        self._reconnect_attempt = 0
        self._reconnect_delay = self._RECONNECT_BASE_DELAY
        self._delayed_reconnect_count = 0  # FIXED-P1: 移除hasattr检查，直接赋值（已在__init__中初始化）
        self._first_reconnect_time = 0.0  # FIXED-P1: 重连成功时重置首次重连时间戳
        self._set_fins_state(FinsConnState.CONNECTED, device_id, ip)
        return True

    async def start(self, config: dict) -> None:
        try:
            from fins.tcp import TCPFinsConnection
        except ImportError:
            raise ImportError("fins未安装，请执行: pip install fins") from None

        self._config = config
        self._primary_ip = config.get("host", "") or config.get("ip", "")
        self._primary_port = int(config.get("port", 9600))
        self._backup_ip = config.get("backup_host", "")
        self._backup_port = int(config.get("backup_port", 0))

        if not self._primary_ip:
            raise ValueError("FINS驱动配置缺少host参数")
        if not (1 <= self._primary_port <= 65535):
            raise ValueError(f"FINS驱动port超出范围[1-65535]，当前: {self._primary_port}")

        source_node = int(config.get("source_node", 0))
        dest_node = int(config.get("dest_node", 0))
        network_no = int(config.get("network_no", 0))
        unit_no = int(config.get("unit_no", 0))
        # FIXED-P1: 校验 FINS 地址参数范围，防止非法值导致协议层异常
        # network_no: 0=local/direct, 1-127 for network (FINS spec: 0-127)
        if not (0 <= network_no <= 127):
            raise ValueError(f"FINS network_no out of range [0-127], got {network_no}")
        # source_node/dest_node: 0-255 (FINS node address is 1 byte)
        if not (0 <= source_node <= 255):
            raise ValueError(f"FINS source_node out of range [0-255], got {source_node}")
        if not (0 <= dest_node <= 255):
            raise ValueError(f"FINS dest_node out of range [0-255], got {dest_node}")
        # unit_no: 0-255 (FINS unit address is 1 byte; 0x00=CPU, 0xFF=broadcast)
        if not (0 <= unit_no <= 255):
            raise ValueError(f"FINS unit_no out of range [0-255], got {unit_no}")
        self._source_node = source_node
        self._dest_node = dest_node
        self._network_no = network_no
        self._unit_no = unit_no
        self._command_code = str(config.get("command_code", "0101"))
        self._plc_series = str(config.get("plc_series", "CJ")).upper()

        direct_mode = config.get("direct_mode", False)
        self._is_direct_mode = direct_mode

        if self._is_direct_mode or self._plc_series in ("CS", "CJ", "CJ2", "CP"):
            self._is_direct_mode = True
            if network_no == 0 and dest_node == 0:
                self._dest_node = 1
                self._unit_no = 0x01
                logger.info(
                    "[fins] device=%s code=DIRECT_MODE msg=FINS直接模式已启用 "
                    "(network_no=0, dest_node=1, unit_no=0x01, series=%s)",
                    self._primary_ip,
                    self._plc_series,
                )

        self._using_backup = False
        self._active_ip = self._primary_ip
        self._active_port = self._primary_port
        self._timeout = self._config.get("timeout", 5.0)  # FIXED-P1: 初始化_timeout，避免hasattr回退硬编码
        # FINS-P1: UDP 重传配置 — transport=udp 时启用应用层重传
        transport_cfg = str(config.get("transport", "tcp")).lower()
        self._is_udp = transport_cfg == "udp"
        udp_retries_cfg = int(config.get("udp_retries", self._DEFAULT_UDP_RETRIES))
        # 限制到 schema 范围 [0, 10]，防止非法值导致重传次数过大
        self._udp_max_retries = max(0, min(udp_retries_cfg, 10))

        self._global_frozen_threshold = int(config.get("frozen_threshold", 0))
        self._global_rate_limit = float(config.get("rate_of_change_limit", 0))

        points_config = config.get("points_config")
        if isinstance(points_config, dict):
            self._point_configs = points_config

        self._write_verify_enabled = bool(config.get("write_verify", False))
        self._write_rate_limit_ms = int(config.get("write_rate_limit_ms", self._WRITE_RATE_LIMIT_MS))
        self._write_audit_enabled = bool(config.get("write_audit", True))
        # FINS-004: 从配置读取最大响应大小限制
        self._max_response_size = int(config.get("fins_max_response_size", self._DEFAULT_MAX_FINS_RESPONSE_SIZE))

        device_id = self._primary_ip
        ok = await self._connect_with_handshake(device_id)
        if not ok and self._backup_ip:
            self._primary_fail_count += 1
            if self._primary_fail_count >= self._FAILOVER_THRESHOLD:
                self._using_backup = True
                self._log_error(
                    FinsDriverErrors.FAILOVER_TRIGGERED,
                    device_id,
                    f"primary failed {self._primary_fail_count}x, switching to backup {self._backup_ip}",
                )
                ok = await self._connect_with_handshake(device_id)
                if not ok:
                    self._log_error(FinsDriverErrors.FAILOVER_NO_BACKUP, self._backup_ip, "")
                    raise ConnectionError(f"FINS连接失败: primary={self._primary_ip}, backup={self._backup_ip}")
        elif not ok:
            raise ConnectionError(f"FINS连接失败: {self._primary_ip}:{self._primary_port}")

        if self._running:
            try:
                self._watchdog_task = asyncio.create_task(self._watchdog_loop())
                if self._backup_ip:
                    self._failover_probe_task = asyncio.create_task(self._failover_probe_loop())
                    self._standby_task = asyncio.create_task(self._standby_keepalive_loop())
                    self._init_standby_task = asyncio.create_task(self._init_standby(self._primary_ip))
                self._init_edge_rules(device_id)
                ts_retention = int(self._config.get("ts_retention_days", 7))
                self._ts_store = FinsTsStore(retention_days=ts_retention)
                await self._ts_store.start()
                self._offline_sync = FinsOfflineSyncManager(
                    ts_store=self._ts_store,
                    sync_interval=float(self._config.get("offline_sync_interval", 30.0)),
                    batch_size=int(self._config.get("offline_batch_size", 1000)),
                    compress=self._config.get("offline_compress", "gzip"),
                )
                await self._offline_sync.start()
            except asyncio.CancelledError:
                await self.stop()
                raise
            except Exception:
                await self.stop()
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
        if self._standby_task and not self._standby_task.done():
            self._standby_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._standby_task
            self._standby_task = None
        if self._init_standby_task and not self._init_standby_task.done():
            self._init_standby_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._init_standby_task
            self._init_standby_task = None
        if self._delayed_reconnect_task and not self._delayed_reconnect_task.done():
            self._delayed_reconnect_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._delayed_reconnect_task
            self._delayed_reconnect_task = None
        # FIXED-P1: 取消后台重连任务，防止stop后继续重连
        if self._bg_reconnect_task and not self._bg_reconnect_task.done():
            self._bg_reconnect_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._bg_reconnect_task
            self._bg_reconnect_task = None
        # FIXED-P3: standby_client关闭前确保keepalive loop已完全退出，避免竞态
        # _standby_task已在上方取消并await，此处让出事件循环确保其清理回调完成
        await asyncio.sleep(0)
        if self._standby_client:
            try:
                if self._standby_client.fins_socket is not None:  # FIXED-P0: fins_socket为None时跳过close
                    await asyncio.wait_for(asyncio.to_thread(self._standby_client.fins_socket.close), timeout=5.0)
            except TimeoutError:
                logger.warning("[fins] TCP writer close timeout (5s)")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("[fins] operation failed: %s", e)
            self._standby_client = None
            self._standby_ready = False
        if self._config_version_mgr:
            await self._config_version_mgr.stop()  # FIXED-P0: stop()已改为async，需要await
            self._config_version_mgr = None
        self._audit = None
        self._ota_mgr = None
        if self._offline_sync:
            await self._offline_sync.stop()
            self._offline_sync = None
        if self._ts_store:
            await self._ts_store.stop()
            self._ts_store = None
        self._edge_rule_engine = None
        # FIXED-P0: 原问题-_edge_trigger（EdgeTriggerExecutor 实例）直接置 None，从未调用 stop()
        # 导致触发器内部 SQLite 连接（_db_conn）不关闭，后台上传任务（_upload_task）和脉冲任务（_pulse_tasks）不取消
        # 对比 s7.py/modbus_tcp.py/modbus_rtu.py 均在 stop() 中正确调用 await self._edge_trigger.stop()
        if self._edge_trigger:
            await self._edge_trigger.stop()
            self._edge_trigger = None
        self._rule_store = None
        try:
            if self._client:
                try:
                    if self._client.fins_socket is not None:  # FIXED-P0: fins_socket为None时跳过close
                        await asyncio.wait_for(asyncio.to_thread(self._client.fins_socket.close), timeout=5.0)
                except TimeoutError:
                    logger.warning("[fins] TCP writer close timeout (5s)")
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning("[fins] FINS驱动断开异常: %s", e)
        finally:
            self._running = False
            # CROSS-004: 取消所有后台任务
            await self._cancel_background_tasks()
            # FIXED-P1: FINS-03 线程池shutdown改为非阻塞，防止fins线程卡死时stop()永久等待
            if self._thread_pool:
                try:
                    self._thread_pool.shutdown(wait=False, cancel_futures=True)
                except Exception as e:
                    logger.warning("[fins] code=EXECUTOR_SHUTDOWN_ERROR msg=Error during executor shutdown: %s", e)
                self._thread_pool = None
            self._client = None
            self._point_stats.clear()
            self._last_values.clear()
            self._frozen_counters.clear()
            self._last_read_ts.clear()
            self._last_write_ts.clear()
            self._audit_log.clear()
            self._quality_history.clear()
            self._point_configs.clear()
            self._degraded = False
            self._degraded_interval_ms = 0
            self._offline_since.clear()
            self._health_stats.clear()
            self._permanent_offline.clear()  # FIXED-P1: stop()中清理_permanent_offline，防止restart后设备永远不重试
            self._device_points.clear()
            self._devices.clear()
            self._set_fins_state(FinsConnState.DISCONNECTED, self._active_ip, "driver stopped")
            logger.info("FINS驱动已停止")

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        quality = self.get_connection_quality(device_id)
        if quality < 60:
            self._log_error(FinsDriverErrors.CONN_LOST, device_id, f"quality={quality:.1f}")
            try:
                recovered = await self._handle_connection_failure(device_id)
            except FailoverInFlightTimeout:  # FIXED-P0: 捕获FailoverInFlightTimeout，返回降级值
                recovered = False
            if recovered:
                new_quality = self.get_connection_quality(device_id)
                self._log_error(FinsDriverErrors.CONN_RECOVERED, device_id, f"{quality:.1f}->{new_quality:.1f}")
            else:
                self._set_fins_state(
                    FinsConnState.DEGRADED, device_id, "Connection quality below threshold after reconnect"
                )

        if not self._running or not self._client:
            try:
                recovered = await self._handle_connection_failure(device_id)
            except FailoverInFlightTimeout:  # FIXED-P0: 捕获FailoverInFlightTimeout，返回降级值
                recovered = False
            if not recovered:
                now = datetime.now(UTC)
                return {p: PointValue(value=None, quality="bad", timestamp=now) for p in points}

        if self._degraded and self._degraded_interval_ms > 0:
            await asyncio.sleep(self._degraded_interval_ms / 1000.0)

        batch_size = self._config.get("batch_size", 10)
        result = {}

        for i in range(0, len(points), batch_size):
            batch = points[i : i + batch_size]
            try:
                batch_result = await asyncio.wait_for(
                    self._read_points_batch_with_fallback(batch),
                    timeout=self._READ_TIMEOUT,
                )
            except TimeoutError:  # FIXED-P1: 兼容Python<3.11
                self._record_read_failure(device_id)
                self._log_error(FinsDriverErrors.READ_TIMEOUT, device_id, f"{self._READ_TIMEOUT}s")
                now = datetime.now(UTC)
                batch_result = {p: PointValue(value=None, quality="bad", timestamp=now) for p in batch}
            result.update(batch_result)

        now_utc = datetime.now(UTC)
        has_good = False
        for p, pv in result.items():
            if isinstance(pv, PointValue):
                if pv.quality == "good" and pv.value is not None:
                    val = pv.value
                    val = self._filter_nan_inf(val, p)
                    if val is None:
                        result[p] = PointValue(value=None, quality="bad", timestamp=pv.timestamp)
                        self._record_point_failure(p)
                        self._record_quality(p, "bad")
                        continue
                    if self._detect_frozen_value(p, val):
                        result[p] = PointValue(value=None, quality="bad", timestamp=pv.timestamp)
                        self._record_point_failure(p)
                        self._record_quality(p, "bad")
                        continue
                    if not self._check_rate_of_change(p, val):
                        result[p] = PointValue(value=None, quality="uncertain", timestamp=pv.timestamp)
                        self._record_point_failure(p)
                        self._record_quality(p, "uncertain")
                        continue
                    has_good = True
                    point_deadband = self._get_point_deadband(p)
                    point_scaling = self._get_point_scaling(p)
                    point_clamp = self._get_point_clamp(p)
                    val = self._apply_scaling(val, point_scaling)
                    val = self._apply_deadband(val, self._last_values.get(p), point_deadband)
                    clamped, clamped_ok = self._apply_clamp(val, point_clamp)
                    if not clamped_ok:
                        result[p] = PointValue(value=None, quality="bad", timestamp=pv.timestamp)
                        self._log_error(FinsDriverErrors.VALUE_OUT_OF_RANGE, p, f"value={val}")
                        self._record_point_failure(p)
                        self._record_quality(p, "bad")
                    else:
                        self._set_last_value(p, clamped)
                        self._trim_dict(self._last_read_ts, p)  # FIXED-P0: 字典无界增长
                        self._last_read_ts[p] = time.monotonic()  # FIXED-P1: 使用monotonic时钟
                        result[p] = PointValue(
                            value=clamped, quality="good", timestamp=pv.timestamp, latency_ms=pv.latency_ms
                        )
                        self._record_point_success(p, pv.latency_ms)
                        self._record_quality(p, "good")
                elif pv.quality == "bad":
                    self._record_point_failure(p)
                    self._record_quality(p, "bad")
            elif pv is not None:
                val = pv
                val = self._filter_nan_inf(val, p)
                if val is None:
                    result[p] = PointValue(value=None, quality="bad", timestamp=now_utc)
                    self._record_point_failure(p)
                    self._record_quality(p, "bad")
                    continue
                if self._detect_frozen_value(p, val):
                    result[p] = PointValue(value=None, quality="bad", timestamp=now_utc)
                    self._record_point_failure(p)
                    self._record_quality(p, "bad")
                    continue
                if not self._check_rate_of_change(p, val):
                    result[p] = PointValue(value=None, quality="uncertain", timestamp=now_utc)
                    self._record_point_failure(p)
                    self._record_quality(p, "uncertain")
                    continue
                has_good = True
                point_deadband = self._get_point_deadband(p)
                point_scaling = self._get_point_scaling(p)
                point_clamp = self._get_point_clamp(p)
                val = self._apply_scaling(val, point_scaling)
                val = self._apply_deadband(val, self._last_values.get(p), point_deadband)
                clamped, clamped_ok = self._apply_clamp(val, point_clamp)
                if not clamped_ok:
                    result[p] = PointValue(value=None, quality="bad", timestamp=now_utc)
                    self._log_error(FinsDriverErrors.VALUE_OUT_OF_RANGE, p, f"value={val}")
                    self._record_point_failure(p)
                    self._record_quality(p, "bad")
                else:
                    self._set_last_value(p, clamped)
                    self._trim_dict(self._last_read_ts, p)  # FIXED-P0: 字典无界增长
                    self._last_read_ts[p] = time.monotonic()  # FIXED-P1: 使用monotonic时钟
                    result[p] = PointValue(value=clamped, quality="good", timestamp=now_utc)
                    self._record_point_success(p, 0.0)
                    self._record_quality(p, "good")

        self._check_degradation()
        if has_good:
            await self._record_read_success(
                device_id
            )  # #[AUDIT-FIX] _record_read_success is async, must await (was no-op coroutine)
            if not self._using_backup:
                self._primary_fail_count = 0
        else:
            self._record_read_failure(device_id)
            if not self._using_backup and self._backup_ip:
                self._primary_fail_count += 1
                if self._primary_fail_count >= self._FAILOVER_THRESHOLD:
                    if await self._activate_standby(device_id) or await self._fast_failover(device_id):
                        pass
        await self._evaluate_edge_rules(device_id, result)
        if self._ts_store and has_good:
            try:
                written = await self._ts_store.write_read_result(device_id, result)
                if written > 0:
                    self._log_error(FinsDriverErrors.TS_STORE_WRITE, device_id, f"wrote {written} points to local TS")
                if not self._network_online and self._offline_sync:
                    for point_name, value in result.items():
                        pv = value
                        quality = "good"
                        if isinstance(value, PointValue):
                            pv = value.value
                            quality = value.quality
                        if pv is not None and quality == "good":
                            await self._offline_sync.enqueue(device_id, point_name, pv, quality)
                    self._log_error(FinsDriverErrors.TS_STORE_OFFLINE, device_id, "enqueued to offline queue")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("[fins] persist failed: %s", e)
        return result

    async def _read_points_batch_with_fallback(
        self, points: list[str], _depth: int = 0, _max_depth: int = 5, _deadline: float = 0.0
    ) -> dict[str, Any]:
        # FIXED-P2: 递归拆分总超时预算(30秒)，防止设备离线时递归拆分长时间阻塞
        # 之前：默认120秒过长，设备离线时阻塞采集循环
        if _deadline == 0.0:
            _deadline = time.monotonic() + 30.0
        if time.monotonic() > _deadline:
            now = datetime.now(UTC)
            return {p: PointValue(value=None, quality="bad", timestamp=now) for p in points}
        try:
            return await self._read_points_batch(points)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("[fins] batch read failed, splitting: %s", e)
            self._log_error(FinsDriverErrors.FINS_BATCH_SPLIT, "batch", str(e))

        if _depth >= _max_depth:  # FIXED-P2: 超过最大递归深度时停止拆分，直接返回失败
            now = datetime.now(UTC)
            return {p: PointValue(value=None, quality="bad", timestamp=now) for p in points}

        if len(points) <= self._BATCH_SPLIT_MIN:
            return await self._read_points_sequential(points)

        mid = len(points) // 2
        left = await self._read_points_batch_with_fallback(points[:mid], _depth + 1, _max_depth, _deadline)
        right = await self._read_points_batch_with_fallback(points[mid:], _depth + 1, _max_depth, _deadline)
        left.update(right)
        return left

    async def _read_points_sequential(self, points: list[str]) -> dict[str, Any]:
        result = {}
        for p in points:
            try:
                r = await self._read_point_async(p)
                now = datetime.now(UTC)
                if isinstance(r, Exception) or r is None:
                    result[p] = PointValue(value=None, quality="bad", timestamp=now)
                elif isinstance(r, PointValue):
                    result[p] = r
                else:
                    result[p] = PointValue(value=r, quality="good", timestamp=now)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                now = datetime.now(UTC)
                result[p] = PointValue(value=None, quality="bad", timestamp=now)
                self._log_error(FinsDriverErrors.READ_FAILED, p, str(e))
        return result

    async def _read_points_batch(self, points: list[str]) -> dict[str, Any]:
        tasks = [self._read_point_async(p) for p in points]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        now = datetime.now(UTC)
        out = {}
        for p, r in zip(
            points, results, strict=True
        ):  # FIXED(P2): 原问题-B905 zip无strict; 修复-添加strict=True(points与results等长)
            if isinstance(r, Exception) or r is None:
                out[p] = PointValue(value=None, quality="bad", timestamp=now)
            elif isinstance(r, PointValue):
                out[p] = r
            else:
                out[p] = PointValue(value=r, quality="good", timestamp=now)
        return out

    async def _read_point_async(self, address: str) -> Any:
        t0 = time.monotonic()
        try:
            device_id = self._config.get("host", "unknown")
            record_packet("tx", "fins", device_id, f"FINS Read: {address}")
            result = await self._run_in_thread(self._read_point, address)
            latency = (time.monotonic() - t0) * 1000
            record_packet("rx", "fins", device_id, f"FINS Read Response: {address} = {result}")
            if isinstance(result, PointValue):
                result.latency_ms = latency
            return result
        except FinsResponseError as e:
            now = datetime.now(UTC)
            self._log_error(e.error_code, address, f"fins_code=0x{e.fins_code:04X}")
            return PointValue(value=None, quality="bad", timestamp=now)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            now = datetime.now(UTC)
            self._log_error(FinsDriverErrors.READ_FAILED, address, str(e))
            return PointValue(value=None, quality="bad", timestamp=now)

    _FINS_MAX_OFFSET = 0xFFFFFF  # FIXED-P2: FINS偏移量最大值(24位)，防止溢出截断写入非预期区域

    def _parse_address(self, address: str) -> tuple[str, int, str]:
        addr_upper = address.upper()
        data_type = "w"

        def _safe_int(s: str) -> int:
            try:
                val = int(s)
            except ValueError as exc:  # FIXED(P3): 原问题-B904 异常链丢失; 修复-添加 from exc
                raise ValueError(f"无效的FINS地址偏移量: '{s}' 在地址 '{address}' 中") from exc
            if not (0 <= val <= self._FINS_MAX_OFFSET):  # FIXED-P2: 校验偏移量范围，防止24位截断
                raise ValueError(f"FINS地址偏移量超出范围[0-{self._FINS_MAX_OFFSET}]: {val} 在地址 '{address}' 中")
            return val

        if "," in address:
            parts = address.split(",")
            addr_part = parts[0].strip()
            if len(parts) > 1:
                dt = parts[1].strip().lower()
                if dt in ("b", "w", "i", "r", "ui", "dw", "str"):
                    data_type = dt
            addr_upper = addr_part.upper()
            address = addr_part

        if addr_upper.startswith("EM"):
            offset = _safe_int(addr_upper[2:])
            return ("e", offset, data_type)
        elif addr_upper.startswith("VM"):
            offset = _safe_int(addr_upper[2:])
            return ("v", offset, data_type)
        elif addr_upper.startswith("TK"):
            offset = _safe_int(addr_upper[2:])
            return ("tk", offset, data_type)
        elif addr_upper.startswith("CS"):
            offset = _safe_int(addr_upper[2:])
            return ("cs", offset, data_type)
        elif addr_upper.startswith("IR"):
            offset = _safe_int(addr_upper[2:])
            return ("ir", offset, data_type)
        elif addr_upper.startswith("DR"):
            offset = _safe_int(addr_upper[2:])
            return ("dr", offset, data_type)
        elif addr_upper.startswith("CF"):
            offset = _safe_int(addr_upper[2:])
            return ("cf", offset, data_type)

        if addr_upper.startswith("CIO"):
            offset = _safe_int(addr_upper[3:])
            return ("c", offset, data_type)
        elif addr_upper.startswith("C"):
            offset = _safe_int(addr_upper[1:])
            return ("c", offset, data_type)
        elif addr_upper.startswith("D"):
            offset = _safe_int(addr_upper[1:])
            return ("d", offset, data_type)
        elif addr_upper.startswith("W"):
            offset = _safe_int(addr_upper[1:])
            return ("w", offset, data_type)
        elif addr_upper.startswith("H") or addr_upper.startswith("A"):
            offset = _safe_int(addr_upper[1:])
            return ("h", offset, data_type)

        raise ValueError(f"无效的FINS地址: {address}")

    def _read_point(self, address: str) -> Any:
        area, offset, data_type = self._parse_address(address)

        if self._is_direct_mode:
            with self._client_lock:  # FIXED-P0: 快照client引用
                client = self._client
            if client is None:
                raise ConnectionError("FINS client is not connected")
            return self._read_point_direct_mode(area, offset, data_type)

        with self._client_lock:  # FIXED-P0: 仅获取client引用时持锁，与直接模式一致，I/O在锁外执行
            client = self._client
        if client is None:
            raise ConnectionError("FINS client is not connected")
        return client.read(area, offset, data_type=data_type, number_of_values=1)

    def _read_point_direct_mode(self, area: str, offset: int, data_type: str) -> Any:
        try:
            area_code = self._get_fins_area_code(area)
            if area_code is None:
                raise ValueError(f"FINS直接模式不支持区域: {area}")

            offset_bytes = bytes(
                [
                    (offset >> 16) & 0xFF,
                    (offset >> 8) & 0xFF,
                    offset & 0xFF,
                ]
            )

            word_count = 1 if data_type in ("b", "w", "i", "ui") else 2

            fins_command = (
                bytes(
                    [
                        _ICF_DIRECT,
                        0x00,
                        0x02,
                        0x00,
                        self._dest_node,
                        self._unit_no,
                        0x00,
                        self._source_node,
                        0x00,
                        0x00,
                        0x01,
                        0x01,
                        area_code,
                    ]
                )
                + offset_bytes
                + struct.pack(">H", word_count)
            )

            return self._fins_tcp_request(fins_command, data_type)
        except FinsResponseError:
            raise
        except Exception as e:
            self._log_error(FinsDriverErrors.DIRECT_MODE_FALLBACK, f"{area}:{offset}", str(e))
            try:
                with self._client_lock:  # FIXED-P0: 快照client引用，与failover互斥
                    client = self._client
                if client is None:
                    return PointValue(value=None, quality="bad", timestamp=datetime.now(UTC))
                return client.read(area, offset, data_type=data_type, number_of_values=1)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("[fins] single point read failed: %s", e)  # FIXED-P1: 原问题-单点读取异常返回bad质量无日志
                return PointValue(value=None, quality="bad", timestamp=datetime.now(UTC))

    async def _wait_in_flight_requests(self, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._in_flight_lock:
                if self._in_flight_requests <= 0:
                    return
            await asyncio.sleep(0.05)
        with self._in_flight_lock:
            count = self._in_flight_requests
        if count > 0:
            logger.error(
                "[fins] code=IN_FLIGHT_TIMEOUT msg=%d in-flight requests still pending after %.1fs timeout",
                count,
                timeout,
            )
            raise FailoverInFlightTimeout(pending_count=count, timeout=timeout)

    async def _fins_tcp_request(self, fins_command: bytes, data_type: str = "w") -> Any:
        # CROSS-001: 使用独立线程池而非默认 executor
        return await self._run_in_thread(self._fins_tcp_request_sync, fins_command, data_type)

    def _fins_tcp_request_sync(self, fins_command: bytes, data_type: str = "w") -> Any:
        with self._in_flight_lock:
            self._in_flight_requests += 1
        try:
            return self._fins_tcp_request_inner(fins_command, data_type)
        finally:
            with self._in_flight_lock:
                self._in_flight_requests -= 1

    def _fins_tcp_request_inner(self, fins_command: bytes, data_type: str = "w") -> Any:
        # FIXED-P0: 缩小锁范围仅保护socket引用获取，I/O在锁外执行
        # 之前：整个send+recv在_client_lock内完成，阻塞watchdog/failover最长10秒
        # 之后：锁内仅获取socket引用并设置_socket_in_use，I/O在锁外执行；
        #       failover/reconnect路径检查_socket_in_use，若为True则用shutdown强制中断
        with self._client_lock:
            sock = self._client.fins_socket if self._client else None
            if not sock:
                raise RuntimeError("FINS socket not available")
            self._socket_in_use = True
        tcp_frame = b"FINS" + struct.pack(">I", len(fins_command)) + fins_command

        try:
            sock.settimeout(self._timeout or 5.0)
            sock.send(tcp_frame)

            recv_deadline = time.monotonic() + 10.0
            header = bytearray()
            while len(header) < 8:
                if time.monotonic() > recv_deadline:
                    raise RuntimeError("FINS header read total timeout (10s)")
                chunk = sock.recv(8 - len(header))
                if not chunk:
                    raise RuntimeError("FINS connection closed during header read")
                header.extend(chunk)
            if header[:4] != b"FINS":
                raise RuntimeError(f"Invalid FINS TCP response header: {header[:4]}")
            data_len = struct.unpack(">I", header[4:8])[0]
            if data_len < 12:
                raise RuntimeError(f"FINS response too short: data_len={data_len}")
            if data_len > self._max_response_size:
                logger.error(
                    "[fins] device=%s code=RESPONSE_TOO_LARGE size=%d max=%d, closing connection",
                    self._primary_ip,
                    data_len,
                    self._max_response_size,
                )
                try:
                    sock.close()
                except Exception as e:
                    logger.warning("[fins] fins_tcp_request_inner failed: %s", e)
                raise ValueError(f"FINS response too large: {data_len} > {self._max_response_size}")

            response = bytearray()
            while len(response) < data_len:
                if time.monotonic() > recv_deadline:
                    raise RuntimeError("FINS response read total timeout (10s)")
                chunk = sock.recv(data_len - len(response))
                if not chunk:
                    break
                response.extend(chunk)

            if len(response) < 14:
                raise RuntimeError(f"FINS response too short: {len(response)}")

            if len(response) < data_len:
                raise RuntimeError(f"FINS response incomplete: expected {data_len} bytes, got {len(response)}")

            err_code = struct.unpack(">H", response[10:12])[0] if len(response) >= 12 else 0
            if err_code != 0:
                is_write = len(fins_command) > 10 and fins_command[10] == 0x01 and fins_command[11] == 0x02
                code_map = _FINS_WRITE_RESPONSE_CODE_MAP if is_write else _FINS_RESPONSE_CODE_MAP
                mapped = code_map.get(err_code)
                if mapped:
                    exc_class = FinsWriteError if is_write else FinsResponseError
                    raise exc_class(err_code, mapped[0], mapped[1])
                exc_class = FinsWriteError if is_write else FinsResponseError
                default_code = FinsDriverErrors.WRITE_FAILED if is_write else FinsDriverErrors.READ_FAILED
                raise exc_class(err_code, default_code, f"FINS error 0x{err_code:04X}")

            data_start = 14
            if len(response) <= data_start:
                raise ValueError(f"FINS response data truncated: length {len(response)} <= {data_start}")

            data_bytes = response[data_start:]

            if data_type == "b":
                if not data_bytes:
                    raise RuntimeError(
                        f"FINS response data truncated: expected >=1 byte for bit, got {len(data_bytes)}"
                    )
                return data_bytes[0] & 0x01
            elif data_type == "dw":
                if len(data_bytes) >= 4:
                    return struct.unpack(">I", data_bytes[:4])[0]
                raise RuntimeError(f"FINS response data truncated: expected 4 bytes for dword, got {len(data_bytes)}")
            elif data_type == "float":
                if len(data_bytes) >= 4:
                    return struct.unpack(">f", data_bytes[:4])[0]
                raise RuntimeError(f"FINS response data truncated: expected 4 bytes for float, got {len(data_bytes)}")
            elif data_type == "r":
                if len(data_bytes) >= 4:
                    return struct.unpack(">f", data_bytes[:4])[0]
                raise RuntimeError(f"FINS response data truncated: expected 4 bytes for REAL, got {len(data_bytes)}")
            elif data_type == "i":
                if len(data_bytes) >= 2:
                    return struct.unpack(">h", data_bytes[:2])[0]
                raise RuntimeError(
                    f"FINS response data truncated: expected 2 bytes for signed int, got {len(data_bytes)}"
                )
            else:
                if len(data_bytes) >= 2:
                    return struct.unpack(">H", data_bytes[:2])[0]
                raise RuntimeError(f"FINS response data truncated: expected >=2 bytes for word, got {len(data_bytes)}")

        except (FinsResponseError, FinsWriteError):
            raise
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._log_error(FinsDriverErrors.READ_FAILED, "fins_tcp", str(e))
            raise
        finally:
            # FIXED-P0: 在锁内清除_socket_in_use标记，与failover/reconnect路径互斥
            with self._client_lock:
                self._socket_in_use = False

    def _get_fins_area_code(self, area: str) -> int | None:
        area_codes = {
            "d": 0x82,
            "e": 0xA0,
            "v": 0xA2,
            "c": 0xB0,
            "w": 0xB4,
            "h": 0xB8,
            "tk": 0x18,
            "cs": 0x30,
            "ir": 0xDC,
            "dr": 0xBC,
            "cf": 0x28,
        }
        return area_codes.get(area)

    async def _write_point_direct_mode(self, area: str, offset: int, value: Any, data_type: str) -> None:
        area_code = self._get_fins_area_code(area)
        if area_code is None:
            raise ValueError(f"FINS直接模式不支持区域: {area}")

        if data_type == "b":
            data_bytes = bytes([0x01 if value else 0x00])
        elif data_type in ("dw", "long"):
            data_bytes = struct.pack(">I", int(value))
        elif data_type == "float" or data_type == "r":
            data_bytes = struct.pack(">f", float(value))
        elif data_type == "i":  # FIXED-P1: 有符号整数使用">h"编码，不截断
            data_bytes = struct.pack(">h", int(value))
        else:
            data_bytes = struct.pack(">H", int(value) & 0xFFFF)

        offset_bytes = bytes(
            [
                (offset >> 16) & 0xFF,
                (offset >> 8) & 0xFF,
                offset & 0xFF,
            ]
        )

        fins_command = (
            bytes(
                [
                    _ICF_DIRECT,
                    0x00,
                    0x02,
                    0x00,
                    self._dest_node,
                    self._unit_no,
                    0x00,
                    self._source_node,
                    0x00,
                    0x00,
                    0x01,
                    0x02,
                    area_code,
                ]
            )
            + offset_bytes
            + struct.pack(">H", len(data_bytes))
            + data_bytes
        )

        await self._fins_tcp_request(fins_command, "w")  # FIXED-P0: 补充缺失的await，否则异步写入永不执行

    async def _read_back_for_verify(self, point: str) -> Any:
        try:
            return await asyncio.wait_for(
                self._read_point_async(point),
                timeout=5,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("[fins] operation failed: %s", e)
            return None

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        # SEC-FIX(修复2): 驱动层写入权限检查，防止内部服务绕过 API 层鉴权直接写入
        if hasattr(self, "check_permission"):
            from edgelite.security.rbac import Permission

            if not await self.check_permission(Permission.DEVICE_WRITE_POINT):
                self._log_error(
                    FinsDriverErrors.RBAC_DENIED, device_id, f"role={self._current_user_role} lacks device:write_point"
                )
                return False
        if not self._running or not self._client:
            recovered = await self._handle_connection_failure(device_id)
            if not recovered:
                return False

        try:
            area, offset, data_type = self._parse_address(point)
        except ValueError as e:
            self._log_error(FinsDriverErrors.WRITE_FAILED, point, str(e))
            self._audit_write(device_id, point, "", 0, None, value, "rejected", error_code="INVALID_ADDRESS")
            return False

        valid, reason = self._validate_write_value(value, data_type)
        if not valid:
            self._log_error(FinsDriverErrors.WRITE_VALUE_OUT_OF_RANGE, point, reason)
            self._audit_write(
                device_id,
                point,
                area,
                offset,
                None,
                value,
                "rejected",
                error_code=FinsDriverErrors.WRITE_VALUE_OUT_OF_RANGE,
            )
            return False

        allowed, wait_ms = self._check_write_rate(point)
        if not allowed:
            self._log_error(FinsDriverErrors.WRITE_RATE_LIMITED, point, f"wait={wait_ms:.0f}ms")
            self._audit_write(
                device_id,
                point,
                area,
                offset,
                None,
                value,
                "rate_limited",
                error_code=FinsDriverErrors.WRITE_RATE_LIMITED,
            )
            return False

        old_value = None
        if self._write_verify_enabled:
            try:
                old_pv = await self._read_back_for_verify(point)
                if isinstance(old_pv, PointValue) and old_pv.value is not None:
                    old_value = old_pv.value
                elif not isinstance(old_pv, PointValue) and old_pv is not None:
                    old_value = old_pv
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("[fins] operation failed: %s", e)

        fins_code = 0
        error_code = ""
        try:
            async with self._lock:
                with self._client_lock:  # FIXED-P0: 快照client引用，与failover互斥
                    write_client = self._client
                if write_client is None:
                    return False
                record_packet("tx", "fins", device_id, f"FINS Write: {point} = {value}")
                if self._is_direct_mode:
                    await asyncio.wait_for(
                        self._write_point_direct_mode(area, offset, value, data_type),
                        timeout=self._WRITE_TIMEOUT,
                    )
                else:
                    await asyncio.wait_for(
                        self._run_in_thread(write_client.write, value, area, offset, data_type),
                        timeout=self._WRITE_TIMEOUT,
                    )
                record_packet("rx", "fins", device_id, f"FINS Write Response: {point}")

            self._trim_dict(self._last_write_ts, point)  # FIXED-P0: 字典无界增长
            self._last_write_ts[point] = time.monotonic()  # FIXED-P1: 使用monotonic时钟
            self._record_write_success(device_id)

            verify_ok = None
            if self._write_verify_enabled:
                await asyncio.sleep(self._WRITE_VERIFY_DELAY_MS / 1000.0)
                read_back = await self._read_back_for_verify(point)
                read_val = None
                if isinstance(read_back, PointValue) and read_back.value is not None:
                    read_val = read_back.value
                elif not isinstance(read_back, PointValue) and read_back is not None:
                    read_val = read_back

                if read_val is not None:
                    if data_type == "float":
                        verify_ok = abs(read_val - float(value)) < 0.001
                    elif data_type == "b":
                        verify_ok = (read_val & 0x01) == (1 if value else 0)
                    else:
                        verify_ok = read_val == value

                    if not verify_ok:
                        self._log_error(FinsDriverErrors.WRITE_VERIFY_FAILED, point, f"expected={value} got={read_val}")
                        self._audit_write(
                            device_id,
                            point,
                            area,
                            offset,
                            old_value,
                            value,
                            "verify_failed",
                            error_code=FinsDriverErrors.WRITE_VERIFY_FAILED,
                            verify_ok=False,
                        )
                        return False

            self._audit_write(device_id, point, area, offset, old_value, value, "ok", verify_ok=verify_ok)
            return True

        except FinsWriteError as e:
            fins_code = e.fins_code
            error_code = e.error_code
            self._record_write_failure(device_id)
            self._log_error(error_code, point, f"fins_code=0x{fins_code:04X}")
            self._audit_write(
                device_id,
                point,
                area,
                offset,
                old_value,
                value,
                "fins_error",
                error_code=error_code,
                fins_code=fins_code,
            )
            return False
        except TimeoutError:  # FIXED-P1: 兼容Python<3.11
            self._record_write_failure(device_id)
            self._log_error(FinsDriverErrors.WRITE_TIMEOUT, point, f"{self._WRITE_TIMEOUT}s")
            self._audit_write(
                device_id, point, area, offset, old_value, value, "timeout", error_code=FinsDriverErrors.WRITE_TIMEOUT
            )
            return False
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._record_write_failure(device_id)
            self._log_error(FinsDriverErrors.WRITE_FAILED, device_id, f"{point}: {e}")
            self._audit_write(
                device_id, point, area, offset, old_value, value, "error", error_code=FinsDriverErrors.WRITE_FAILED
            )
            return False

    async def batch_write_points(self, device_id, writes):
        if not self._running or not self._client:
            recovered = await self._handle_connection_failure(device_id)
            if not recovered:
                return {point: False for point, _ in writes}

        merged = self._merge_adjacent_writes(writes)

        async def _do_write(point, value, _merged):
            return await self.write_point(device_id, point, value)

        tasks = [_do_write(p, v, m) for p, v, m in merged]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return {merged[i][0]: False if isinstance(r, Exception) else bool(r) for i, r in enumerate(results)}

    async def _fast_failover(self, device_id: str) -> bool:
        if not self._backup_ip:
            return False
        t0 = time.monotonic()
        self._log_error(FinsDriverErrors.FAILOVER_FAST, device_id, f"initiating fast failover to {self._backup_ip}")
        await self._wait_in_flight_requests(5.0)
        async with self._async_client_lock:
            with self._client_lock:  # FIXED-P0: 替换client时与读取互斥
                if self._client:
                    # FIXED-P0: 若socket正被读取路径使用，用shutdown强制中断阻塞recv，避免close等待
                    if self._socket_in_use and self._client.fins_socket is not None:
                        try:
                            import socket as _socket_mod

                            self._client.fins_socket.shutdown(_socket_mod.SHUT_RDWR)
                        except Exception as e:
                            logger.debug("[fins] failover shutdown failed: %s", e)
                    try:
                        if self._client.fins_socket is not None:  # FIXED-P1: fins_socket为None时跳过close，与stop()一致
                            await asyncio.wait_for(asyncio.to_thread(self._client.fins_socket.close), timeout=5.0)
                    except TimeoutError:
                        logger.warning("[fins] TCP writer close timeout (5s)")
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.debug("[fins] operation failed: %s", e)
                    self._client = None
        self._using_backup = True
        ok = await self._connect_with_handshake(device_id)
        elapsed = time.monotonic() - t0
        if ok:
            self._failover_ts = time.monotonic()  # FIXED-P1: 使用monotonic时钟
            self._primary_fail_count = 0
            self._reconnect_attempt = 0
            self._log_error(FinsDriverErrors.FAILOVER_FAST, device_id, f"failover completed in {elapsed:.2f}s")
            if self._audit:
                self._audit.log_failover(device_id, self._primary_ip, self._backup_ip)
        else:
            self._log_error(
                FinsDriverErrors.FAILOVER_NO_BACKUP, self._backup_ip, f"failover took {elapsed:.2f}s but failed"
            )
        return ok

    async def _init_standby(self, device_id: str) -> None:
        if not self._backup_ip or self._standby_client is not None:
            return
        try:
            backup_port = self._backup_port or self._primary_port
            transport = self._config.get("transport", "tcp").lower()
            if transport == "udp":
                from fins.udp import UDPFinsConnection

                standby = UDPFinsConnection()
            else:
                from fins.tcp import TCPFinsConnection

                standby = TCPFinsConnection()
            await asyncio.wait_for(
                self._run_in_thread(standby.connect, self._backup_ip, backup_port),
                timeout=self._config.get("timeout", 5),
            )
            # FINS-P1: UDP 模式为备用连接注入应用层重传
            if transport == "udp":
                self._wrap_udp_retransmission(standby)
            handshake_ok = await self._fins_node_handshake(standby, update_shared_state=False)
            if handshake_ok:
                self._standby_client = standby
                self._standby_ready = True
                self._log_error(FinsDriverErrors.STANDBY_READY, self._backup_ip, "")
            else:
                try:
                    await asyncio.wait_for(asyncio.to_thread(standby.fins_socket.close), timeout=5.0)
                except TimeoutError:
                    logger.warning("[fins] TCP writer close timeout (5s)")
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.debug("[fins] operation failed: %s", e)
                self._standby_client = None
                self._standby_ready = False
                self._log_error(FinsDriverErrors.STANDBY_FAILED, self._backup_ip, "handshake failed")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._log_error(FinsDriverErrors.STANDBY_FAILED, self._backup_ip, str(e))
            self._standby_client = None
            self._standby_ready = False

    async def _standby_keepalive_loop(self) -> None:
        _standby_init_fail_count = 0  # FIXED-P2: 限制备用连接初始化失败次数，防止反复触发失败初始化
        while self._running:
            await asyncio.sleep(self._STANDBY_KEEPALIVE_INTERVAL)
            if not self._running:
                break
            if not self._using_backup and self._standby_client is None and self._backup_ip:
                try:
                    await self._init_standby(self._config.get("host", "unknown"))
                    _standby_init_fail_count = 0  # FIXED-P2: 初始化成功则重置计数
                except Exception:
                    _standby_init_fail_count += 1
                    if _standby_init_fail_count >= 5:  # FIXED-P2: 连续5次初始化失败后暂停尝试
                        logger.warning(
                            "[fins] standby init failed %d times, pausing attempts", _standby_init_fail_count
                        )
                        _standby_init_fail_count = 0
                continue
            if self._standby_client is not None:
                try:
                    connected = False
                    try:
                        connected = (
                            hasattr(self._standby_client, "fins_socket")
                            and self._standby_client.fins_socket is not None
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.debug("[fins] operation failed: %s", e)
                    if not connected:
                        self._standby_ready = False
                        try:
                            if (
                                hasattr(self._standby_client, "fins_socket")
                                and self._standby_client.fins_socket is not None
                            ):
                                await asyncio.wait_for(
                                    asyncio.to_thread(self._standby_client.fins_socket.close), timeout=5.0
                                )
                        except TimeoutError:
                            logger.warning("[fins] TCP writer close timeout (5s)")
                        except asyncio.CancelledError:
                            raise
                        except Exception as e:
                            logger.debug("[fins] operation failed: %s", e)
                        self._standby_client = None
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.debug("[fins] operation failed: %s", e)
                    self._standby_ready = False
                    self._standby_client = None

    async def _activate_standby(self, device_id: str) -> bool:
        if not self._standby_ready or self._standby_client is None:
            return False
        t0 = time.monotonic()
        await self._wait_in_flight_requests(5.0)
        async with self._async_client_lock:
            with self._client_lock:  # FIXED-P0: 替换client时与读取互斥
                if self._client:
                    # FIXED-P0: 若socket正被读取路径使用，用shutdown强制中断阻塞recv，避免close等待
                    if self._socket_in_use and self._client.fins_socket is not None:
                        try:
                            import socket as _socket_mod

                            self._client.fins_socket.shutdown(_socket_mod.SHUT_RDWR)
                        except Exception as e:
                            logger.debug("[fins] standby takeover shutdown failed: %s", e)
                    try:
                        if self._client.fins_socket is not None:  # FIXED-P1: fins_socket为None时跳过close，与stop()一致
                            await asyncio.wait_for(asyncio.to_thread(self._client.fins_socket.close), timeout=5.0)
                    except TimeoutError:
                        logger.warning("[fins] TCP writer close timeout (5s)")
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.debug("[fins] operation failed: %s", e)
                self._client = self._standby_client
                self._standby_client = None
        self._standby_ready = False
        self._using_backup = True
        self._active_ip = self._backup_ip
        self._active_port = self._backup_port or self._primary_port
        self._primary_fail_count = 0
        self._reconnect_attempt = 0
        self._running = True
        self._failover_ts = time.monotonic()
        elapsed = time.monotonic() - t0
        self._log_error(FinsDriverErrors.STANDBY_TAKEOVER, device_id, f"standby takeover in {elapsed:.3f}s")
        self._set_fins_state(FinsConnState.CONNECTED, device_id, f"standby takeover -> {self._backup_ip}")
        return True

    async def _handle_connection_failure(self, device_id: str) -> bool:
        if not self._network_online:
            return False
        if not self._using_backup and self._backup_ip:
            if self._primary_fail_count >= self._FAILOVER_THRESHOLD:
                if await self._activate_standby(device_id):
                    self.set_network_online(True)
                    return True
                if await self._fast_failover(device_id):
                    self.set_network_online(True)
                    return True
        # FIXED-P1: 非阻塞调度重连，避免阻塞采集循环最长61秒
        # 之前：await self._try_reconnect(device_id) 同步等待重连完成（含sleep最长60s）
        # 之后：后台调度重连任务，立即返回False（bad quality），采集循环继续
        self._schedule_background_reconnect(device_id)
        return False

    def _schedule_background_reconnect(self, device_id: str) -> None:
        # FIXED-P1: 后台调度重连，避免并发重连任务
        if self._bg_reconnect_task is not None and not self._bg_reconnect_task.done():
            return  # 已有后台重连任务在运行
        if self._delayed_reconnect_task is not None and not self._delayed_reconnect_task.done():
            return  # 延迟重连任务已在运行
        self._bg_reconnect_task = asyncio.create_task(self._try_reconnect(device_id))

    def _init_edge_rules(self, device_id: str) -> None:
        try:
            event_bus = None
            try:
                from edgelite.engine.event_bus import EventBus

                event_bus = EventBus()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("[fins] operation failed: %s", e)
            self._edge_rule_engine = ModbusEdgeRuleEngine(event_bus=event_bus)
            self._edge_trigger = EdgeTriggerExecutor(
                device_write_callback=self._edge_write_callback,
                mqtt_publish_callback=self._edge_mqtt_callback,
            )
            self._rule_store = RuleStore()
            self._edge_rule_engine.set_on_action_callback(self._edge_trigger.execute)
            rules = self._rule_store.load_rules()
            for rule in rules:
                self._edge_rule_engine.add_rule(rule)
            logger.info("[fins] edge rule engine initialized: %d rules loaded for device=%s", len(rules), device_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._log_error(FinsDriverErrors.EDGE_RULE_ERROR, device_id, f"init failed: {e}")
            self._edge_rule_engine = None
            self._edge_trigger = None
            self._rule_store = None

    async def _edge_write_callback(self, device_id: str, point: str, value: Any) -> bool:
        try:
            return await self.write_point(device_id, point, value)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._log_error(FinsDriverErrors.EDGE_TRIGGER_FAILED, device_id, f"edge write {point}={value}: {e}")
            return False

    async def _edge_mqtt_callback(self, topic: str, payload: dict, qos: int = 0, retain: bool = False) -> None:
        try:
            import json as _json

            logger.info("[fins-edge] mqtt publish topic=%s payload=%s", topic, _json.dumps(payload, default=str))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("[fins-edge] mqtt callback error: %s", e)

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
            try:
                records = await self._edge_rule_engine.evaluate_point(device_id, point_name, float(pv), quality)
                for record in records:
                    if record.action == "firing":
                        self._log_error(
                            FinsDriverErrors.EDGE_RULE_FIRED,
                            device_id,
                            f"rule={record.rule_id} point={record.point_name} value={record.trigger_value:.2f} threshold={record.threshold:.2f} latency={record.latency_ms:.1f}ms",
                        )
                    elif record.action == "recovered":
                        self._log_error(
                            FinsDriverErrors.EDGE_RULE_RECOVERED,
                            device_id,
                            f"rule={record.rule_id} point={record.point_name}",
                        )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._log_error(FinsDriverErrors.EDGE_RULE_ERROR, device_id, f"evaluate {point_name}: {e}")

    async def hot_reload_rules(self) -> int:
        if not self._edge_rule_engine or not self._rule_store:
            return 0
        old_rules = self._edge_rule_engine.get_all_rules()
        for r in old_rules:
            await self._edge_rule_engine.remove_rule(r["rule_id"])
        new_rules = self._rule_store.load_rules()
        for rule in new_rules:
            self._edge_rule_engine.add_rule(rule)
        self._log_error(
            FinsDriverErrors.RULE_HOT_RELOADED, "global", f"reloaded {len(new_rules)} rules (was {len(old_rules)})"
        )
        return len(new_rules)

    async def rollback_edge_rule(self, rule_id: str, target_version: int) -> EdgeRule | None:
        if not self._rule_store or not self._edge_rule_engine:
            return None
        rule = self._rule_store.rollback(rule_id, target_version)
        if rule:
            await self._edge_rule_engine.remove_rule(rule_id)
            self._edge_rule_engine.add_rule(rule)
            self._log_error(FinsDriverErrors.RULE_ROLLBACK_OK, rule.device_id, f"rule={rule_id} to v{target_version}")
        return rule

    def add_edge_rule(self, rule: EdgeRule) -> None:
        if not self._edge_rule_engine:
            return
        self._edge_rule_engine.add_rule(rule)
        if self._rule_store:
            self._rule_store.save_rule(rule)

    async def remove_edge_rule(self, rule_id: str) -> EdgeRule | None:
        if not self._edge_rule_engine:
            return None
        rule = await self._edge_rule_engine.remove_rule(rule_id)
        if rule and self._rule_store:
            self._rule_store.delete_rule(rule_id)
        return rule

    def get_edge_rules(self, device_id: str = "") -> list[dict]:
        if not self._edge_rule_engine:
            return []
        if device_id:
            return [r.to_dict() for r in self._edge_rule_engine.get_rules_for_device(device_id)]
        return self._edge_rule_engine.get_all_rules()

    def get_edge_alarm_history(self, limit: int = 100) -> list[dict]:
        if not self._edge_rule_engine:
            return []
        records = self._edge_rule_engine._alarm_history[-limit:]
        return [
            {
                "alarm_id": r.alarm_id,
                "rule_id": r.rule_id,
                "device_id": r.device_id,
                "point_name": r.point_name,
                "severity": r.severity,
                "action": r.action,
                "trigger_value": r.trigger_value,
                "threshold": r.threshold,
                "timestamp": r.timestamp,
                "latency_ms": r.latency_ms,
            }
            for r in records
        ]

    def set_network_online(self, online: bool) -> None:
        was_offline = not self._network_online
        self._network_online = online
        if self._offline_sync:
            self._offline_sync.set_online(online)
        if online and was_offline:
            self._log_error(FinsDriverErrors.TS_SYNC_RESTORED, "global", "network restored, starting offline sync")

    async def force_sync_offline(self) -> int:
        if not self._offline_sync:
            return 0
        try:
            count = await self._offline_sync.force_sync()
            if count > 0:
                self._log_error(FinsDriverErrors.TS_COMPRESS_UPLOAD, "global", f"force synced {count} records")
            return count
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._log_error(FinsDriverErrors.TS_SYNC_FAILED, "global", str(e))
            return 0

    async def query_ts(
        self,
        device_id: str,
        point_name: str,
        start_time,
        end_time=None,
        quality: str | None = None,
        aggregate: str | None = None,
        window_seconds: int | None = None,
        limit: int = 10000,
    ) -> list[dict]:
        if not self._ts_store:
            return []
        return await self._ts_store.query(
            device_id, point_name, start_time, end_time, quality, aggregate, window_seconds, limit
        )

    async def query_ts_latest(self, device_id: str, point_names: list[str]) -> dict[str, dict]:
        if not self._ts_store:
            return {}
        return await self._ts_store.query_latest(device_id, point_names)

    def get_persistence_stats(self) -> dict:
        ts_stats = self._ts_store.get_stats() if self._ts_store else {}
        sync_stats = self._offline_sync.get_stats() if self._offline_sync else {}
        return {
            "ts_store": ts_stats,
            "offline_sync": sync_stats,
            "network_online": self._network_online,
        }

    def init_enterprise(self, audit_service=None) -> None:
        self._config_version_mgr = FinsConfigVersionManager()
        self._ota_mgr = FinsOtaManager()
        self._audit = FinsAudit(audit_service)
        logger.info("[fins] enterprise ops initialized: config_version, rbac, audit, ota")

    def check_rbac(self, role: str, permission: str, device_id: str = "") -> bool:
        try:
            from edgelite.security.rbac import (  # FIXED-P0: 补充缺失的导入，修复NameError
                Permission,
                has_permission,
            )
        except ImportError:
            return False  # FIXED-P0: 安全模块不可用时默认拒绝(fail-closed)
        try:
            perm = Permission(permission)
        except ValueError:
            return False
        granted = has_permission(role, perm)
        if self._audit:
            self._audit.log_rbac_check(device_id, permission, role, granted)
        if not granted:
            self._log_error(FinsDriverErrors.RBAC_DENIED, device_id, f"role={role} permission={permission}")
        return granted

    def set_user_role(self, role: str) -> None:
        self._current_user_role = role

    async def save_config_version(
        self, device_id: str, config: dict, change_summary: str = "", operator: str = "system"
    ) -> int:
        if not self._config_version_mgr:
            return 0
        if not self.check_rbac(operator, "config:edit", device_id):
            self._log_error(FinsDriverErrors.CONFIG_CHANGE_DENIED, device_id, f"operator={operator}")
            return 0
        version = await self._config_version_mgr.save_version(device_id, config, change_summary, operator)
        if self._audit:
            self._audit.log_config_version(device_id, "save", to_version=version, operator=operator)
        self._log_error(FinsDriverErrors.CONFIG_VERSION_SAVED, device_id, f"version={version} operator={operator}")
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
            self._log_error(
                FinsDriverErrors.CONFIG_CHANGE_DENIED, device_id, f"rollback denied for operator={operator}"
            )
            return None
        config = await self._config_version_mgr.rollback(device_id, target_version, operator)
        if config and self._audit:
            self._audit.log_config_version(
                device_id, "rollback", from_version=0, to_version=target_version, operator=operator
            )
        self._log_error(
            FinsDriverErrors.CONFIG_VERSION_ROLLBACK, device_id, f"to_version={target_version} operator={operator}"
        )
        return config

    async def get_config_audit_trail(self, device_id: str, limit: int = 100) -> list[dict]:
        if not self._config_version_mgr:
            return []
        return await self._config_version_mgr.get_audit_trail(device_id, limit)

    async def diff_config_versions(
        self, device_id: str, version_a: int, version_b: int
    ) -> dict:  # FIXED-P0: 改为async并await，原同步调用async方法返回协程对象而非dict
        if not self._config_version_mgr:
            return {"changes": []}
        return await self._config_version_mgr.diff_versions(device_id, version_a, version_b)

    def ota_check_update(self, package: OtaPackage) -> bool:
        if not self._ota_mgr:
            return False
        result = self._ota_mgr.check_update(package)
        self._log_error(FinsDriverErrors.OTA_CHECK, "global", f"available={result} target={package.version}")
        return result

    async def ota_start(self, package: OtaPackage, config_snapshot: dict | None = None) -> bool:
        if not self._ota_mgr:
            return False
        if not self.check_rbac(self._current_user_role, "ota:manage"):
            self._log_error(FinsDriverErrors.OTA_FAILED, "global", "RBAC denied for OTA")
            return False
        if self._audit:
            self._audit.log_ota("global", "ota_start", package.version, self._current_user_role)
        self._log_error(FinsDriverErrors.OTA_STARTED, "global", f"target={package.version}")
        result = self._ota_mgr.start_ota(package, config_snapshot or self._config)
        if result:
            self._log_error(FinsDriverErrors.OTA_COMPLETED, "global", f"version={package.version}")
            if self._audit:
                self._audit.log_ota("global", "ota_completed", package.version, self._current_user_role)
        else:
            self._log_error(FinsDriverErrors.OTA_FAILED, "global", f"version={package.version}")
            if self._audit:
                self._audit.log_ota("global", "ota_failed", package.version, self._current_user_role)
        return result

    def ota_rollback(self) -> bool:
        if not self._ota_mgr:
            return False
        result = self._ota_mgr.rollback_ota()
        self._log_error(FinsDriverErrors.OTA_ROLLBACK, "global", f"success={result}")
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

    async def _try_reconnect(self, device_id: str) -> None:
        if device_id in self._permanent_offline:  # FIXED-P2: 永久离线设备跳过重连，防止无限循环
            return
        # FIXED-P1: 重连前检查设备是否已被移除，若已移除则取消重连
        # 原问题：设备被remove_device移除后，正在调度中的_try_reconnect仍会执行，
        #         回退到self._config全局默认配置进行重连，连接到已不管理的设备
        # 修复：检查device_id是否仍在self._devices中，若已移除则直接返回
        if device_id not in self._devices:
            logger.info("[fins] device=%s code=RECONNECT_CANCELLED msg=Device removed, skipping reconnect", device_id)
            return
        if not self._config:
            return
        # FIXED-P2: 使用_reconnect_lock串行化重连，防止并发重连导致_reconnect_attempt自增竞态
        async with self._reconnect_lock:
            self._reconnect_attempt += 1
            if self._reconnect_attempt > self._RECONNECT_MAX_ATTEMPTS:
                self._log_error(FinsDriverErrors.RECONNECT_FAILED, device_id, f"attempts={self._reconnect_attempt}")
                self._set_fins_state(FinsConnState.OFFLINE, device_id, "max reconnect attempts reached")
                # FIXED-P3: _delayed_reconnect_task创建检查与实际创建非原子，此处假设单事件循环线程下无竞态
                if self._delayed_reconnect_task is None or self._delayed_reconnect_task.done():
                    self._delayed_reconnect_task = asyncio.create_task(self._delayed_reconnect(60, device_id))
                return

            if not self._using_backup and self._backup_ip and self._primary_fail_count >= self._FAILOVER_THRESHOLD:
                if await self._activate_standby(device_id):
                    return
                if await self._fast_failover(device_id):
                    return

            delay = self._calc_backoff_delay()
            self._log_error(
                FinsDriverErrors.CONN_LOST, device_id, f"reconnect in {delay:.2f}s (attempt {self._reconnect_attempt})"
            )
            await asyncio.sleep(delay)

            with self._client_lock:  # FIXED-P0: 替换client时与读取互斥
                old_client = self._client
                self._client = None
                # FIXED-P0: 若socket正被读取路径使用，用shutdown强制中断阻塞recv，避免close等待
                if old_client is not None and self._socket_in_use and old_client.fins_socket is not None:
                    try:
                        import socket as _socket_mod

                        old_client.fins_socket.shutdown(_socket_mod.SHUT_RDWR)
                    except Exception as e:
                        logger.debug("[fins] reconnect shutdown failed: %s", e)
            if old_client:
                try:
                    if old_client.fins_socket is not None:  # FIXED-P1: fins_socket为None时跳过close，与stop()一致
                        await asyncio.wait_for(asyncio.to_thread(old_client.fins_socket.close), timeout=5.0)
                except TimeoutError:
                    logger.warning("[fins] TCP writer close timeout (5s)")
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.debug("[fins] close error: %s", e)

            if not self._using_backup:
                ok = await self._connect_with_handshake(device_id)
                if ok:
                    self._primary_fail_count = 0
                    return
                self._primary_fail_count += 1
                if self._primary_fail_count >= self._FAILOVER_THRESHOLD and self._backup_ip:
                    self._using_backup = True
                    self._log_error(
                        FinsDriverErrors.FAILOVER_TRIGGERED,
                        device_id,
                        f"primary failed {self._primary_fail_count}x, switching to backup {self._backup_ip}",
                    )
                    ok = await self._connect_with_handshake(device_id)
                    if ok:
                        return
                    self._log_error(FinsDriverErrors.FAILOVER_NO_BACKUP, self._backup_ip, "")
            else:
                ok = await self._connect_with_handshake(device_id)
                if ok:
                    return
                if self._backup_ip:
                    self._log_error(FinsDriverErrors.FAILOVER_NO_BACKUP, self._backup_ip, "")

            self._set_fins_state(FinsConnState.OFFLINE, device_id, "reconnect failed")

    _MAX_DELAYED_RECONNECTS = 10  # FIXED-P2: 延迟重连最大次数，防止设备永久离线时无限循环

    async def _delayed_reconnect(self, delay: float, device_id: str) -> None:
        if device_id in self._permanent_offline:  # FIXED-P2: 永久离线设备跳过延迟重连，防止无限循环
            return
        # FIXED-P1: 延迟重连前检查设备是否已被移除，若已移除则取消重连
        # 原问题：设备被remove_device移除后，已调度的_delayed_reconnect仍会在sleep后执行_try_reconnect
        # 修复：检查device_id是否仍在self._devices中，若已移除则直接返回
        if device_id not in self._devices:
            logger.info(
                "[fins] device=%s code=DELAYED_RECONNECT_CANCELLED msg=Device removed, skipping delayed reconnect",
                device_id,
            )
            return

        if self._first_reconnect_time == 0.0:
            self._first_reconnect_time = time.monotonic()  # FIXED-P1: 记录首次重连时间戳
        elif time.monotonic() - self._first_reconnect_time > 86400:  # FIXED-P1: 超过24小时停止重连
            self._log_error(
                FinsDriverErrors.CONN_LOST,
                device_id,
                "delayed reconnect exceeded 24h window, marking device permanently offline",
            )
            self._permanent_offline.add(device_id)  # FIXED-P2: 24小时窗口到期后加入永久离线集合，防止watchdog触发新周期
            self._set_fins_state(FinsConnState.OFFLINE, device_id, "permanently offline after 24h reconnect window")
            self._delayed_reconnect_count = 0
            self._first_reconnect_time = 0.0
            return
        self._delayed_reconnect_count += 1
        if self._delayed_reconnect_count > self._MAX_DELAYED_RECONNECTS:
            self._log_error(
                FinsDriverErrors.CONN_LOST,
                device_id,
                f"delayed reconnect reached max {self._MAX_DELAYED_RECONNECTS} attempts, marking permanently offline",
            )
            self._permanent_offline.add(device_id)  # FIXED-P2: 达上限后加入永久离线集合，而非重置计数器继续循环
            self._delayed_reconnect_count = 0
            return
        self._log_error(
            FinsDriverErrors.CONN_LOST,
            device_id,
            f"delayed reconnect in {delay:.0f}s (attempt {self._delayed_reconnect_count}/{self._MAX_DELAYED_RECONNECTS})",
        )
        await asyncio.sleep(delay)
        self._reconnect_attempt = 0
        self._reconnect_delay = self._RECONNECT_BASE_DELAY
        await self._try_reconnect(device_id)

    async def _failover_probe_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._FAILOVER_PROBE_INTERVAL)
            if not self._running:
                break
            if not self._using_backup:
                continue
            probe_client = None
            try:
                transport = self._config.get("transport", "tcp").lower()
                if transport == "udp":
                    from fins.udp import UDPFinsConnection

                    probe_client = UDPFinsConnection()
                else:
                    from fins.tcp import TCPFinsConnection

                    probe_client = TCPFinsConnection()
                await asyncio.wait_for(
                    self._run_in_thread(probe_client.connect, self._primary_ip, self._primary_port),
                    timeout=5,
                )
                try:
                    if hasattr(probe_client, "fins_socket") and probe_client.fins_socket is not None:
                        probe_client.fins_socket.close()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.debug("[fins] operation failed: %s", e)
                probe_client = None

                self._log_error(FinsDriverErrors.FAILOVER_REVERT, self._primary_ip, "primary recovered, reverting")
                device_id = self._config.get("host", "unknown")
                # FIXED-P1: CAS模式 - 保存旧client，仅在连接+握手全部成功后才替换
                with self._client_lock:
                    saved_client = self._client
                    self._client = None  # 临时清空，让_connect_with_handshake使用新连接
                ok = await self._connect_with_handshake(device_id)
                if ok:
                    if saved_client:
                        try:
                            await asyncio.wait_for(asyncio.to_thread(saved_client.fins_socket.close), timeout=5.0)
                        except TimeoutError:
                            logger.warning("[fins] TCP writer close timeout (5s)")
                        except asyncio.CancelledError:
                            raise
                        except Exception as e:
                            logger.debug("[fins] operation failed: %s", e)
                    with self._client_lock:
                        self._primary_fail_count = 0
                        self._using_backup = False
                    self._standby_client = None
                    self._standby_ready = False
                    self._init_standby_task = asyncio.create_task(self._init_standby(device_id))
                else:
                    # FIXED-P1: 新连接失败，恢复旧client，避免并发读取全部失败
                    with self._client_lock:
                        new_client = self._client
                        self._client = saved_client
                    if new_client and new_client is not saved_client:
                        try:
                            await asyncio.wait_for(asyncio.to_thread(new_client.fins_socket.close), timeout=5.0)
                        except TimeoutError:
                            logger.warning("[fins] TCP writer close timeout (5s)")
                        except asyncio.CancelledError:
                            raise
                        except Exception as e:
                            logger.debug("[fins] operation failed: %s", e)
                    self._primary_fail_count = self._FAILOVER_THRESHOLD
                    self._log_error(
                        FinsDriverErrors.FAILOVER_NO_BACKUP,
                        self._primary_ip,
                        "primary reconnect failed after probe, staying on backup",
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                # FIXED-P0: 异常路径恢复saved_client，防止self._client为None导致设备完全断连
                with self._client_lock:
                    if self._client is None and saved_client is not None:
                        self._client = saved_client
                logger.debug("[fins] primary probe failed, staying on backup")
            finally:
                if probe_client is not None:
                    try:
                        if hasattr(probe_client, "fins_socket") and probe_client.fins_socket:
                            probe_client.fins_socket.close()
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.debug("[fins] operation failed: %s", e)
                    probe_client = None

    async def _watchdog_loop(self) -> None:
        """FINS-MED-001: 自适应 watchdog 循环

        在线时：间隔 10 秒
        首次离线时：间隔 5 秒（快速检测恢复）
        持续离线时：间隔渐进增长至 60 秒
        恢复在线后：重置为 10 秒
        """
        import time

        while self._running:
            # FINS-MED-001: 计算自适应间隔
            if self._watchdog_offline_count == 0:
                interval = self._WATCHDOG_INTERVAL_ONLINE
            elif self._watchdog_offline_count == 1:
                interval = self._WATCHDOG_INTERVAL_OFFLINE_FAST
            else:
                # 渐进增长：5s * 2^(count-2)，上限 60s
                interval = min(5 * (2 ** (self._watchdog_offline_count - 2)), self._WATCHDOG_INTERVAL_OFFLINE_MAX)

            await asyncio.sleep(interval)
            if not self._running:
                break
            try:
                with self._client_lock:  # FIXED-P0: 快照client引用，与failover互斥
                    wd_client = self._client
                connected = (
                    wd_client is not None and hasattr(wd_client, "fins_socket") and wd_client.fins_socket is not None
                )
                if connected:
                    if self._watchdog_offline_count > 0:
                        logger.info(
                            "[fins] watchdog: device recovered, resetting offline count from %d",
                            self._watchdog_offline_count,
                        )
                    self._watchdog_offline_count = 0  # FINS-MED-001: 重置离线计数
                    self._offline_since.clear()
                    if self._conn_state == FinsConnState.DEGRADED:
                        self._set_fins_state(FinsConnState.CONNECTED, self._active_ip, "watchdog recovered")
                else:
                    self._watchdog_offline_count += 1  # FINS-MED-001: 增加离线计数
                    now = datetime.now(UTC)  # FIXED-P0: _offline_since统一使用datetime类型
                    device_id = self._config.get("host", "unknown")
                    if device_id not in self._offline_since:
                        self._offline_since[device_id] = now
                        logger.warning(
                            "[fins] watchdog: device offline (count=%d, interval=%.0fs), %s",
                            self._watchdog_offline_count,
                            interval,
                            device_id,
                        )
                    elif (
                        now - self._offline_since[device_id]
                    ).total_seconds() > 30:  # FIXED-P0: _offline_since统一使用datetime类型
                        logger.warning(
                            "[fins] watchdog: connection offline >30s (count=%d), triggering reconnect for %s",
                            self._watchdog_offline_count,
                            device_id,
                        )
                        await self._try_reconnect(device_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # CROSS-002: 分级处理异常，不静默吞没
                if not self._handle_watchdog_exception(e, "fins_watchdog"):
                    break

    async def add_device(self, device_id: str, config: dict, points: list[dict] | None = None) -> None:
        if points is None:
            points = []
        self._devices[device_id] = {
            "config": config,
            "points": {p.get("name", p.get("address", "")): p for p in points if p.get("name") or p.get("address")},
        }
        self._device_points[device_id] = points
        for p in points:
            addr = p.get("address", "")
            pc = {}
            if "deadband" in p:
                pc["deadband"] = p["deadband"]
            if "scaling" in p:
                pc["scaling"] = p["scaling"]
            if "clamp" in p:
                pc["clamp"] = p["clamp"]
            if "frozen_threshold" in p:
                pc["frozen_threshold"] = p["frozen_threshold"]
            if "rate_of_change_limit" in p:
                pc["rate_of_change_limit"] = p["rate_of_change_limit"]
            if pc and addr:
                self._point_configs[addr] = pc
        logger.info("FINS设备已添加: %s (%d测点)", device_id, len(points))

    async def discover_devices(self, config: dict) -> list[dict]:
        import socket

        broadcast = config.get("broadcast", "255.255.255.255")
        port = int(config.get("port", 9600))
        timeout = float(config.get("timeout", 3.0))
        source_node = int(config.get("source_node", 0))

        discovered = []
        sock = None

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.settimeout(timeout)

            fins_search_frame = bytes(
                [
                    0x80,
                    0x00,
                    0x02,
                    0x00,
                    0x00,
                    0x00,
                    0x00,
                    source_node,
                    0x00,
                    0x00,
                    0x05,
                    0x01,
                ]
            )

            await self._run_in_thread(sock.sendto, fins_search_frame, (broadcast, port))

            deadline = asyncio.get_running_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    data, addr = await asyncio.wait_for(
                        self._run_in_thread(sock.recvfrom, 4096),
                        timeout=remaining,
                    )
                except TimeoutError:
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.debug("[fins] discover recv error: %s", e)
                    break

                if len(data) < 14:
                    continue

                try:
                    remote_node = data[7]
                    remote_network = data[6]
                    mrc = data[10]
                    src = data[11]

                    if mrc != 0x05 or src != 0x01:
                        continue

                    controller_name = ""
                    if len(data) > 14:
                        try:
                            controller_name = data[14:].decode("ascii", errors="replace").strip("\x00")
                        except asyncio.CancelledError:
                            raise
                        except Exception as e:
                            logger.debug("[fins] controller name decode error: %s", e)
                    device_info = {
                        "device_id": f"fins_{addr[0].replace('.', '_')}",
                        "name": f"Omron PLC ({addr[0]})" + (f" - {controller_name}" if controller_name else ""),
                        "protocol": "fins",
                        "config": {
                            "host": addr[0],
                            "port": port,
                            "transport": "udp",
                        },
                        "points": [],
                        "details": {
                            "fins_node": remote_node,
                            "fins_network": remote_network,
                            "controller_name": controller_name,
                        },
                    }
                    if not any(d["device_id"] == device_info["device_id"] for d in discovered):
                        discovered.append(device_info)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.debug("FINS发现: 解析响应失败 - %s", e)
                    continue

        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._log_error(FinsDriverErrors.DISCOVER_FAILED, "broadcast", str(e))
        finally:
            if sock:
                try:
                    sock.close()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.debug("[fins] socket close error: %s", e)

        logger.info("FINS设备发现完成: 发现%d台设备", len(discovered))
        return discovered

    def remove_device(self, device_id: str) -> None:
        with self._stats_lock:  # FIXED-P2: 健康统计pop纳入_stats_lock，与写入路径锁保护一致
            self._health_stats.pop(device_id, None)
            self._offline_since.pop(device_id, None)
        self._permanent_offline.discard(device_id)
        # FIXED-P2: 清理设备相关的点配置和点列表，防止内存泄漏
        device_pts = self._device_points.pop(device_id, [])
        # FIXED-P2: _point_configs键是地址字符串不是device_id，需按地址逐个清理
        point_addrs = set()
        for pt in device_pts:
            addr = pt.get("address", "")
            if addr:
                self._point_configs.pop(addr, None)
                point_addrs.add(addr)
        # FIXED-P2: 清理_quality_history、_frozen_counters、_last_read_ts、_last_write_ts中与该设备测点相关的条目
        for addr in point_addrs:
            self._quality_history.pop(addr, None)
            self._frozen_counters.pop(addr, None)
            self._last_read_ts.pop(addr, None)
            self._last_write_ts.pop(addr, None)
        self._devices.pop(device_id, None)
        logger.info("FINS device removed: %s", device_id)

    async def health_check(self, device_id: str) -> bool:
        if not self._running:
            return False
        with self._client_lock:  # FIXED-P0: 快照client引用，与failover互斥
            hc_client = self._client
        if not hc_client:
            return False
        try:
            connected = (
                hc_client is not None and hasattr(hc_client, "fins_socket") and hc_client.fins_socket is not None
            )
            return connected
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("[fins] health_check failed: %s", e)  # FIXED-P1: 原问题-健康检查异常返回False无日志
            return False
