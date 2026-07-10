"""西门子S7协议驱动 - 基于snap7库，支持S7-200/300/400/1200/1500"""

from __future__ import annotations

import asyncio
import collections
import concurrent.futures
import contextlib
import json
import logging
import math
import os
import random
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from edgelite.api.debug import record_packet
from edgelite.api.error_codes import S7DriverErrors
from edgelite.drivers.base import (
    ConnectionState,
    DriverCapabilities,
    DriverPlugin,
    LRUCache,
    PointValue,
)
from edgelite.drivers.edge_rule_engine import EdgeRule, ModbusEdgeRuleEngine
from edgelite.drivers.edge_triggers import EdgeTriggerExecutor
from edgelite.drivers.redundancy import LinkRedundancyManager, LinkRole, RedundancyConfig
from edgelite.drivers.rule_store import RuleStore
from edgelite.drivers.s7_config_version import S7ConfigVersionManager
from edgelite.drivers.s7_ota import S7OtaManager
from edgelite.drivers.s7_ts_store import (  # FIXED-P0: 导入S7TsStore和S7OfflineSyncManager，修复NameError
    S7OfflineSyncManager,
    S7TsStore,
)
from edgelite.services.i18n import t as _t

logger = logging.getLogger(__name__)


def _bad_pv(error_code: str) -> PointValue:
    return PointValue(value=None, quality="bad", timestamp=datetime.now(UTC), source=f"s7:{error_code}")


@dataclass
class PointHealthStats:
    success_count: int = 0
    fail_count: int = 0
    consecutive_fails: int = 0
    last_value: Any = None
    last_timestamp: float = 0.0
    same_value_count: int = 0
    _latency_samples: deque = field(
        default_factory=lambda: deque(maxlen=20)
    )  # FIXED-P2: S7-03 改为deque避免列表切片内存分配

    @property
    def success_rate(self) -> float:
        total = self.success_count + self.fail_count
        return self.success_count / total if total > 0 else 1.0

    @property
    def avg_latency_ms(self) -> float:
        return sum(self._latency_samples) / len(self._latency_samples) if self._latency_samples else 0.0

    def record_success(self, latency_ms: float = 0.0) -> None:
        self.success_count += 1
        self.consecutive_fails = 0
        if latency_ms > 0:
            self._latency_samples.append(latency_ms)

    def record_failure(self) -> None:
        self.fail_count += 1
        self.consecutive_fails += 1


class S7Driver(DriverPlugin):
    """西门子S7协议驱动

    配置参数:
        ip: PLC IP地址
        rack: 机架号 (默认0)
        slot: 插槽号 (默认1，S7-1200/1500默认1，S7-300默认2)
        db_number: 数据块编号
        heartbeat_interval: 心跳检测间隔秒数 (默认30)
        pdu_size: 期望PDU大小 (默认0=自动协商)

    常见PLC型号rack/slot配置参考:
        S7-200 Smart: rack=0, slot=0  (通过以太网扩展)
        S7-300:       rack=0, slot=2  (CPU在slot 2)
        S7-400:       rack=0, slot=2  (CPU在slot 2)
        S7-1200:      rack=0, slot=1  (CPU在slot 1)
        S7-1500:      rack=0, slot=1  (CPU在slot 1)
    """

    plugin_name = "siemens_s7"
    plugin_version = "1.0.0"
    supported_protocols = ("siemens_s7", "s7")  # FIXED(P2): 原问题-可变默认值list; 修复-改为tuple
    _required_dependencies: tuple[str, ...] = ("snap7",)  # FIXED(P2): 原问题-可变默认值list; 修复-改为tuple
    config_schema = {
        "description": "Siemens S7 PLC protocol (S7-200 Smart/300/400/1200/1500)",
        "required": ["ip"],
        "properties": {
            "ip": {"type": "string", "description": "PLC IP address", "format": "ipv4"},
            "port": {"type": "integer", "description": "S7 TCP port", "minimum": 1, "maximum": 65535},
            "rack": {"type": "integer", "description": "Hardware rack number", "minimum": 0, "maximum": 7},
            "slot": {"type": "integer", "description": "CPU slot number", "minimum": 0, "maximum": 31},
        },
        "fields": [
            {
                "name": "ip",
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
                "description": "S7 TCP port (default 102, ISO-on-TCP)",
                "default": 102,
            },
            {
                "name": "rack",
                "type": "integer",
                "label": "Rack",
                "description": "Hardware rack number (0-7). S7-200 Smart/300/400/1200/1500 usually 0",
                "default": 0,
            },
            {
                "name": "slot",
                "type": "integer",
                "label": "Slot",
                "description": "CPU slot number (0-31). S7-200 Smart: 0, S7-300: 2, S7-1200/1500: 1",
                "default": 1,
            },
            {
                "name": "connect_timeout",
                "type": "integer",
                "label": "Connection Timeout (s)",
                "description": "TCP connection timeout in seconds (default 5, increase for remote/cloud connections)",
                "default": 5,
            },
            {
                "name": "heartbeat_interval",
                "type": "integer",
                "label": "Heartbeat Interval (s)",
                "description": "Seconds between heartbeat checks (default 30)",
                "default": 30,
            },
            {
                "name": "pdu_size",
                "type": "integer",
                "label": "PDU Size",
                "description": "Desired PDU size in bytes (0=auto-negotiate, default 0)",
                "default": 0,
            },
            {
                "name": "plc_model",
                "type": "string",
                "label": "PLC Model",
                "description": "PLC model (auto-detected on connect, or set manually). For S7-200 SMART, set to 'S7-200 SMART' to enable TSAP connection mode",
                "default": "auto",
                "options": ["auto", "S7-200 SMART", "S7-300", "S7-400", "S7-1200", "S7-1500"],
            },
            {
                "name": "optimized_db",
                "type": "boolean",
                "label": "Optimized DB Access",
                "description": "Enable optimized DB block reading for S7-1200/1500 (merges adjacent addresses)",
                "default": True,
            },
            {
                "name": "db_number",
                "type": "integer",
                "label": "DB Number",
                "description": "Default data block number for reading/writing",
                "default": 1,
                "min": 1,
                "max": 65535,
            },
            {
                "name": "password",
                "type": "string",
                "label": "Password",
                "description": "S7 PLC access password (required for S7-200 SMART with password protection). 8 hex characters, e.g. '01234567' or 'FFFFFFFF' to disable",
                "default": "",
                "secret": True,
            },
            {
                "name": "local_tsap",
                "type": "integer",
                "label": "Local TSAP",
                "description": "Local TSAP for S7-200 SMART TSAP connection (default 0x1000). Only used when rack=0 and slot=0 or plc_model=S7-200",
                "default": 4096,
            },
            {
                "name": "remote_tsap",
                "type": "integer",
                "label": "Remote TSAP",
                "description": "Remote TSAP for S7-200 SMART TSAP connection (default 0x0200). Only used when rack=0 and slot=0 or plc_model=S7-200",
                "default": 512,
            },
            {
                "name": "deadband",
                "type": "number",
                "label": "Deadband",
                "description": "Value change deadband filter (0=disabled)",
                "default": 0,
            },
            {
                "name": "scaling",
                "type": "object",
                "label": "Scaling",
                "description": "Linear scaling: {ratio: 1.0, offset: 0.0}",
                "default": {},
            },
            {
                "name": "clamp",
                "type": "object",
                "label": "Clamp",
                "description": "Value clamping: {min: null, max: null}",
                "default": {},
            },
            {
                "name": "backup_ip",
                "type": "string",
                "label": "Backup IP",
                "description": "Redundant link IP address (failover target when primary fails 3 times)",
                "default": "",
            },
        ],
    }

    experimental = False
    capabilities = DriverCapabilities(
        discover=False, read=True, write=True, subscribe=False, batch_read=True, batch_write=True
    )
    constraints = (
        {
            "type": "protocol_note",
            "message": "S7-200 SMART, optimized block access, and PUT/GET permission differences vary by model",
        },
    )  # FIXED(P2): 原问题-可变默认值list; 修复-改为tuple

    _MAX_RECONNECT_ATTEMPTS = 3
    _RECONNECT_BASE_DELAY = 1.0
    _RECONNECT_MAX_DELAY = 60.0
    # S7-002: 心跳重连最大次数和长间隔
    _HB_RECONNECT_MAX_ATTEMPTS = 50
    _HB_MAX_TOTAL_RECONNECT_DURATION = 86400  # FIXED-P1: 心跳重连最大总时长24小时，超限后标记永久离线
    _HB_LONG_RETRY_INTERVAL = 3600.0  # 1小时
    # S7-MED-001: 渐进式重连配置
    _LONG_RETRY_PROGRESSIVE = True  # 启用渐进式增长
    _LONG_RETRY_INITIAL = 3600.0  # 初始 1 小时
    _LONG_RETRY_MAX = 28800.0  # 上限 8 小时 (2^3 * 1h)
    _READ_TIMEOUT = 30
    _WRITE_TIMEOUT = 10
    _PRIMARY_FAIL_THRESHOLD = 3
    _PASSWORD_FAIL_THRESHOLD = 3
    _AUTH_LOCK_DURATION = 300
    _HB_MIN_INTERVAL = 15
    _HB_MAX_INTERVAL = 60
    _HB_GOOD_LATENCY = 50
    _HB_BAD_LATENCY = 200
    _HB_SAMPLE_WINDOW = 10
    _DEGRADE_SUCCESS_THRESHOLD = 0.80
    _RECOVER_SUCCESS_THRESHOLD = 0.95
    _FROZEN_COUNT_THRESHOLD = 5
    _BATCH_RETRY_MAX = 3
    _MAX_BATCH_RETRY_TIMEOUT = 30
    _WRITE_RATE_LIMIT_MS = 500
    _WRITE_VERIFY_DELAY_MS = 100
    _WRITE_AUDIT_MAX = 1000

    _PLC_PDU_MAP: dict[str, int] = {
        "S7-1200": 480,
        "S7-1500": 960,
        "S7-300": 240,
        "S7-400": 480,
        "S7-200 SMART": 240,
    }

    def __init__(self):
        """Initialize S7Driver.

        Lock Hierarchy:
            - _lock: asyncio.Lock for async concurrent access protection
            - _sync_lock: threading.RLock protecting self._client from TOCTOU race conditions
        """
        super().__init__()  # FIXED-P0: 基类属性(_capabilities/_connection_statuses/_reconnect_state/_circuit_state等)未初始化
        self._running = False
        self._client = None
        self._config: dict = {}
        self._lock = asyncio.Lock()
        self._sync_lock = (
            threading.RLock()
        )  # FIXED-P0: 改为RLock防止死锁，之前Lock不可重入导致旧线程持锁时新线程永久阻塞
        self._reconnect_count: int = 0
        self._reconnect_delay: float = self._RECONNECT_BASE_DELAY
        self._devices: dict[str, dict] = {}
        self._pdu_size: int = 240
        self._heartbeat_interval: int = 30
        self._heartbeat_task: asyncio.Task | None = None
        self._hb_task_lock = asyncio.Lock()  # FIXED-P0: 心跳Task创建锁，防止并发read_points创建双心跳循环
        # _health_stats inherited from base class (dict[str, DriverHealthStats])  # FIXED-P2: 删除遮蔽基类的覆盖初始化
        self._plc_model: str = "Unknown"
        self._password: str = ""
        self._password_negotiated: bool = False
        self._is_s7_200_smart: bool = False
        self._active_ip: str = ""
        self._primary_ip: str = ""
        self._backup_ip: str = ""
        self._using_backup: bool = False
        self._redundancy: LinkRedundancyManager | None = None
        self._redundancy_config: RedundancyConfig | None = None
        self._password_fail_count: int = 0
        self._auth_locked_until: float = 0.0
        self._hb_latency_samples: deque = deque(maxlen=100)  # FIXED-P2: S7-R03 改为deque限容，防止心跳采样无限增长
        self._conn_state: str = ConnectionState.DISCONNECTED.value
        self._cached_pdu_size: int | None = None
        self._point_health: LRUCache = LRUCache(max_size=10000)
        self._degraded: bool = False
        self._collect_interval_multiplier: float = 1.0
        self._point_configs: LRUCache = LRUCache(max_size=10000)  # FIXED-P1: 改用LRUCache防止无界增长
        self._write_timestamps: LRUCache = LRUCache(max_size=10000)  # FIXED-P1: 改用LRUCache防止无界增长
        self._write_audit_log: collections.deque = collections.deque(maxlen=1000)
        self._write_verify_enabled: bool = True
        self._edge_rule_engine: ModbusEdgeRuleEngine | None = None
        self._edge_trigger: EdgeTriggerExecutor | None = None
        self._rule_store: RuleStore | None = None
        self._ts_store: S7TsStore | None = None
        self._offline_sync: S7OfflineSyncManager | None = None
        self._config_version_mgr: S7ConfigVersionManager | None = None
        self._ota_mgr: S7OtaManager | None = None
        self._hb_reconnect_backoff: int = 0
        self._hb_reconnect_count: int = 0  # S7-002: 心跳重连失败计数
        self._hb_exhausted: bool = False  # S7-002: 心跳重连次数已耗尽
        self._hb_permanent_offline: bool = False  # FIXED-P1: 心跳重连24小时窗口耗尽后标记永久离线
        self._hb_first_fail_time: float = 0.0  # FIXED-P1: 心跳重连首次失败时间戳
        # S7-MED-001: 渐进式重连等级
        self._long_retry_level: int = 0  # 0=正常, 1+=渐进式等级
        self._delayed_reconnect_task: asyncio.Task | None = None
        self._reconnect_locks: dict[str, asyncio.Lock] = {}
        self._reconnect_tasks: dict[str, asyncio.Task] = {}
        self._s7_executor: concurrent.futures.ThreadPoolExecutor | None = None
        self._s7_executor_failed: bool = False  # FIXED-P1: 线程池超时标志，触发重建executor
        self._s7_executor_lock: asyncio.Lock = asyncio.Lock()  # FIXED-P0: executor重建+submit原子性保护锁
        self._circuit_open: set[str] = set()  # FIXED-P1: 熔断状态设备集合，阻止无限递归重连
        self._circuit_open_since: dict[str, float] = {}  # FIXED-P0: 熔断开始时间，用于half-open自动恢复
        self._CIRCUIT_RECOVERY_SECONDS = 60.0  # FIXED-P0: 熔断后60秒进入half-open状态允许试探连接
        # FIXED-P3: 保存主事件循环引用，供非异步上下文使用
        self._main_loop: asyncio.AbstractEventLoop | None = None

    _DEFAULT_CONNECT_TIMEOUT = 5

    def _get_s7_executor(self) -> concurrent.futures.ThreadPoolExecutor:
        """Get or create a single-threaded executor for snap7 operations.

        snap7's s7client is not fully thread-safe, so we use a single-threaded
        executor to serialize all snap7 operations and prevent concurrent access.
        """
        if self._s7_executor is None:
            self._s7_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="s7-snap7-")
        return self._s7_executor

    def _run_in_s7_thread(self, func, *args, **kwargs):
        """Run a snap7 operation in the dedicated single thread executor (sync)."""
        return self._get_s7_executor().submit(func, *args, **kwargs)

    async def _run_in_s7_thread_async(self, func, *args, **kwargs):
        """Run a snap7 operation in the dedicated single thread executor (async)."""
        timeout = kwargs.pop("timeout", 30.0)  # FIXED-P1: 从kwargs中提取timeout，防止被submit吞没
        async with self._s7_executor_lock:  # FIXED-P0: executor重建+submit原子性保护，防止并发重建
            # FIXED-P1: 超时后重建executor，避免卡死线程阻塞所有后续操作
            if self._s7_executor_failed:
                # FIXED-P0: 重建前尝试关闭旧client的socket，防止旧线程永久卡死在阻塞I/O
                # 之前：旧线程卡死在snap7 C层socket调用，shutdown(wait=False)无法终止
                # 之后：通过disconnect释放socket，使旧线程的阻塞调用返回错误并退出
                with self._sync_lock:
                    old_client = self._client
                old_executor = self._s7_executor
                if old_client is not None and old_executor is not None:
                    try:
                        # FIXED-P0: 使用asyncio.wrap_future+wait_for替代同步result()，防止阻塞事件循环
                        await asyncio.wait_for(
                            asyncio.wrap_future(old_executor.submit(old_client.disconnect)),
                            timeout=2.0,
                        )
                    # FIXED-P1: 原问题-except Exception: pass 静默吞没异常，改为至少 logger.debug
                    except Exception as e:
                        logger.debug(
                            "[s7] code=DISCONNECT_CLEANUP_FAILED msg=old_client disconnect cleanup failed: %s", e
                        )
                elif old_client is not None:
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(
                            asyncio.to_thread(old_client.disconnect),
                            timeout=2.0,
                        )
                self._s7_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="s7-snap7-")
                self._s7_executor_failed = False
                if old_executor is not None:
                    try:
                        old_executor.shutdown(wait=False, cancel_futures=True)
                    except Exception as e:
                        logger.warning(
                            "[s7] run_in_s7_thread_async failed: %s", e
                        )  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                logger.warning("[s7] code=EXECUTOR_REBUILT msg=S7 executor rebuilt after timeout")
            future = self._get_s7_executor().submit(func, *args, **kwargs)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), timeout=timeout)
        except TimeoutError:
            self._s7_executor_failed = True  # FIXED-P0: 超时后标记executor线程可能卡死，下次调用时重建
            self._set_conn_state(ConnectionState.DISCONNECTED.value)
            # FIXED-P0: 超时后通过 _run_in_s7_thread_async + timeout=5.0 释放 socket，使旧线程的阻塞调用返回错误
            # 之前：old_client.disconnect() 同步调用无超时，对端无响应时长时间阻塞事件循环
            # 之后：所有 snap7 调用（含 disconnect）统一走 executor + asyncio.wait_for(timeout=5.0)
            with self._sync_lock:
                old_client = self._client
            if old_client is not None:
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(old_client.disconnect),
                        timeout=5.0,
                    )
                except TimeoutError:
                    logger.warning("[s7] code=DISCONNECT_TIMEOUT msg=old_client.disconnect timed out after 5.0s")
                except Exception as e:
                    logger.debug("[s7] old_client.disconnect failed: %s", e)
            raise
        except asyncio.CancelledError:
            # FIXED: 外部取消也设置 _s7_executor_failed，因为线程可能仍在执行阻塞操作
            self._s7_executor_failed = True
            raise

    async def _safe_s7_call(self, func, *args, timeout: float = 5.0) -> None:
        """S7-003: 安全调用可能阻塞的 snap7 操作，统一使用 _s7_executor，带超时保护。"""
        try:
            await asyncio.wait_for(self._run_in_s7_thread_async(func, *args), timeout=timeout)
        except TimeoutError:
            logger.error(
                "[s7] code=SNAP7_CALL_TIMEOUT msg=Snap7 operation timed out after %.1fs: %s", timeout, func.__name__
            )
            self._s7_executor_failed = True  # FIXED-P1: 标记executor线程可能卡死，下次调用时重建
            self._set_conn_state(ConnectionState.DISCONNECTED.value)
            raise
        except Exception as e:
            logger.warning("[s7] code=SNAP7_CALL_ERROR msg=Snap7 operation error: %s - %s", func.__name__, e)
            raise

    def _sync_db_read(self, db_number, byte_offset, size):
        with self._sync_lock:
            client = self._client  # FIXED-P0: 快照client引用，防止重连期间client被替换
            if client is None:
                raise ConnectionError("S7 client is not connected")
            # BUG-001: 设置snap7操作超时，防止C层阻塞导致_sync_lock死锁
            old_timeout = None
            try:
                old_timeout = client.get_timeout()
            # FIXED-P1: 原问题-except Exception: pass 静默吞没异常，改为至少 logger.debug
            except Exception as e:
                logger.debug("[s7] code=GET_TIMEOUT_FAILED msg=client.get_timeout() failed: %s", e)
            try:
                # FIXED-P1: 从设备配置读取read_timeout，默认5秒
                # 原问题：5秒超时硬编码，无法适应不同网络环境（如公网连接、慢速PLC等）
                # 修复：从self._config读取read_timeout（毫秒），默认5000ms
                read_timeout_ms = int(self._config.get("read_timeout", 5000)) if self._config else 5000
                client.set_timeout(read_timeout_ms)
                return client.db_read(db_number, byte_offset, size)
            finally:
                if old_timeout is not None:
                    try:
                        client.set_timeout(old_timeout)
                    # FIXED-P1: 原问题-except Exception: pass 静默吞没异常，改为至少 logger.debug
                    except Exception as e:
                        logger.debug(
                            "[s7] code=SET_TIMEOUT_RESTORE_FAILED msg=client.set_timeout(old_timeout) failed: %s", e
                        )

    def _sync_db_write(self, db_number, byte_offset, data):
        with self._sync_lock:
            client = self._client  # FIXED-P0: 快照client引用，防止重连期间client被替换
            if client is None:
                raise ConnectionError("S7 client is not connected")
            # BUG-001: 设置snap7操作超时，防止C层阻塞导致_sync_lock死锁
            old_timeout = None
            try:
                old_timeout = client.get_timeout()
            # FIXED-P1: 原问题-except Exception: pass 静默吞没异常，改为至少 logger.debug
            except Exception as e:
                logger.debug("[s7] code=GET_TIMEOUT_FAILED msg=client.get_timeout() failed: %s", e)
            try:
                # FIXED-P1: 原问题-写入超时硬编码5000ms，与读取超时不一致；改为从配置读取write_timeout
                write_timeout_ms = int(self._config.get("write_timeout", 5000)) if self._config else 5000
                client.set_timeout(write_timeout_ms)
                return client.db_write(db_number, byte_offset, data)
            finally:
                if old_timeout is not None:
                    try:
                        client.set_timeout(old_timeout)
                    # FIXED-P1: 原问题-except Exception: pass 静默吞没异常，改为至少 logger.debug
                    except Exception as e:
                        logger.debug(
                            "[s7] code=SET_TIMEOUT_RESTORE_FAILED msg=client.set_timeout(old_timeout) failed: %s", e
                        )

    def _sync_get_cpu_info(self):
        with self._sync_lock:
            client = self._client  # FIXED-P0: 快照client引用，防止重连期间client被替换
            if client is None:
                raise ConnectionError("S7 client is not connected")
            return client.get_cpu_info()

    async def _s7_connect_with_timeout(self, client, ip: str, rack: int, slot: int) -> None:
        """使用可配置超时的 S7 连接（避免 snap7 C 库硬编码的 10s 超时导致公网连接失败）。

        调用方（start/_do_connect）已通过 set_connection_params 设置好 TSAP 和超时参数，
        此方法仅负责在 asyncio.to_thread 中执行 connect 并施加 asyncio 层面的超时保护。
        """
        timeout = self._config.get("connect_timeout", self._DEFAULT_CONNECT_TIMEOUT)
        await asyncio.wait_for(self._run_in_s7_thread_async(client.connect, ip, rack, slot), timeout=timeout)

    async def start(self, config: dict) -> None:
        """启动S7驱动连接"""
        self._main_loop = asyncio.get_running_loop()  # FIXED-P3: 保存主事件循环引用
        try:
            import snap7
        except ImportError:
            raise ImportError(
                "snap7未安装，请执行: pip install python-snap7。同时需要下载snap7动态库: https://snap7.sourceforge.net/"
            ) from None

        self._config = config
        ip = config.get("ip", "")
        # FIXED-P1: 启动时加载持久化的永久离线状态，防止进程重启后已标记永久离线的设备立即尝试重连
        self._load_permanent_offline_state()
        plc_model_cfg = config.get("plc_model", "auto")
        self._primary_ip = ip
        self._backup_ip = config.get("backup_ip", "")
        self._active_ip = ip
        self._using_backup = False

        self._redundancy_config = RedundancyConfig(
            primary_host=ip,
            primary_port=config.get("port", 102),
            backup_host=self._backup_ip,
            backup_port=config.get("backup_port", config.get("port", 102)),
            failover_threshold=int(config.get("failover_threshold", self._PRIMARY_FAIL_THRESHOLD)),
            recovery_probe_interval=float(config.get("recovery_probe_interval", 30.0)),
            auto_revert=bool(config.get("auto_revert", True)),
            auto_revert_stable_count=int(config.get("auto_revert_stable_count", 3)),
            link_timeout=float(config.get("connect_timeout", self._DEFAULT_CONNECT_TIMEOUT)),
        )
        try:
            from edgelite.engine.event_bus import EventBus

            event_bus = EventBus.instance()
        except Exception:
            event_bus = None
        self._redundancy = LinkRedundancyManager(event_bus=event_bus, config=self._redundancy_config)
        self._redundancy.set_on_switch_callback(self._on_redundancy_switch)

        _PLC_RACK_SLOT_MAP = {
            "S7-200 SMART": (0, 0),
            "S7-300": (0, 2),
            "S7-400": (0, 2),
            "S7-1200": (0, 1),
            "S7-1500": (0, 1),
        }
        auto_rack_slot = _PLC_RACK_SLOT_MAP.get(plc_model_cfg.upper())
        if auto_rack_slot:
            default_rack, default_slot = auto_rack_slot
        else:
            default_rack, default_slot = 0, 1

        try:
            rack = int(config.get("rack", default_rack))
            slot = int(config.get("slot", default_slot))
        except (ValueError, TypeError) as e:
            raise ValueError(f"Invalid S7 rack/slot config value: {e}") from e

        if not ip:
            raise ValueError("S7 driver config missing 'ip' parameter")

        if not (0 <= rack <= 7):
            raise ValueError(f"S7 rack out of range [0-7], got {rack}. Common: S7-300/400 rack=0, S7-1200/1500 rack=0")
        if not (0 <= slot <= 31):
            raise ValueError(f"S7 slot out of range [0-31], got {slot}. Common: S7-300 slot=2, S7-1200/1500 slot=1")

        self._heartbeat_interval = int(config.get("heartbeat_interval", 30))

        # S7-200 SMART 特殊处理：需要 TSAP 连接参数
        self._is_s7_200_smart = plc_model_cfg.upper() in ("S7-200 SMART", "S7-200", "AUTO") and rack == 0 and slot == 0
        if plc_model_cfg.upper() == "S7-200 SMART":
            self._is_s7_200_smart = True

        # 保存密码（用于连接后协商）
        self._password = str(config.get("password", "")).strip()
        if self._password and len(self._password) not in (0, 8):
            logger.warning(
                "[s7] device=%s code=PASSWORD_INVALID msg=密码长度应为8位十六进制字符（0-9/A-F），当前长度=%d",
                ip,
                len(self._password),
            )  # FIXED-P2: 日志中不打印明文密码
            self._password = ""

        try:
            self._set_conn_state(ConnectionState.CONNECTING.value)
            self._client = snap7.client.Client()

            if self._is_s7_200_smart:
                local_tsap = config.get("local_tsap", 0x1000)
                remote_tsap = config.get("remote_tsap", 0x0200)
                timeout = self._config.get("connect_timeout", self._DEFAULT_CONNECT_TIMEOUT)
                self._client.set_connection_params(ip, local_tsap, remote_tsap, timeout)
                logger.info(
                    "[s7] device=%s code=S7_200_SMART msg=使用S7-200 SMART TSAP连接模式 (local_tsap=0x%04X, remote_tsap=0x%04X)",
                    ip,
                    local_tsap,
                    remote_tsap,
                )
            else:
                timeout = self._config.get("connect_timeout", self._DEFAULT_CONNECT_TIMEOUT)
                self._client.set_connection_params(ip, 0, 0, timeout)

            await self._s7_connect_with_timeout(self._client, ip, rack, slot)
            self._running = True
            self._reconnect_count = 0
            self._reconnect_delay = self._RECONNECT_BASE_DELAY

            try:
                # FIXED-P2: 使用专用_s7_executor而非asyncio.to_thread默认线程池
                info = await self._run_in_s7_thread_async(self._sync_get_cpu_info)
                self._plc_model = info.ModuleName if hasattr(info, "ModuleName") else "Unknown"
                if any(kw in self._plc_model.upper() for kw in ["SMART", "S7-200"]):
                    self._is_s7_200_smart = True
                logger.info("[s7] device=%s code=PLC_DETECTED msg=Model: %s", ip, self._plc_model)
            except Exception:
                self._plc_model = "Unknown"

            await self._negotiate_password(ip)

            self._set_conn_state(ConnectionState.PDU_NEGOTIATING.value)
            negotiated_pdu = await self._get_pdu_size()
            model_pdu = self._resolve_pdu_by_model(self._plc_model) if self._plc_model != "Unknown" else 0
            self._pdu_size = max(negotiated_pdu, model_pdu)
            self._cached_pdu_size = self._pdu_size
            self._set_conn_state(ConnectionState.CONNECTED.value)
            self._circuit_open.discard(ip)  # FIXED-P1: 首次连接成功时移除熔断状态
            logger.info(
                "[s7] device=%s code=CONN_OK msg=Connected (rack=%d, slot=%d, pdu=%d, model=%s, password=%s)",
                ip,
                rack,
                slot,
                self._pdu_size,
                self._plc_model,
                "enabled" if self._password else "none",
            )

            async with self._hb_task_lock:  # FIXED-P0: 使用锁保护心跳Task创建
                self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

            device_id = config.get("device_id", ip)
            self._redundancy.register_device(device_id, self._redundancy_config)

            try:
                from edgelite.engine.event_bus import EventBus

                event_bus = EventBus.instance()
            except Exception:
                event_bus = None
            self._edge_rule_engine = ModbusEdgeRuleEngine(event_bus=event_bus)
            self._edge_trigger = EdgeTriggerExecutor(
                device_write_callback=self._async_write_point,
            )
            self._edge_rule_engine.set_on_action_callback(self._edge_trigger.execute)
            self._rule_store = RuleStore()
            for rule in self._rule_store.load_rules():
                self._edge_rule_engine.add_rule(rule)

            ts_retention = int(config.get("ts_retention_days", 7))
            self._ts_store = S7TsStore(retention_days=ts_retention)
            await self._ts_store.start()
            self._offline_sync = S7OfflineSyncManager(
                ts_store=self._ts_store,
                sync_interval=float(config.get("offline_sync_interval", 30.0)),
                batch_size=int(config.get("offline_batch_size", 1000)),
                compress=config.get("offline_compress", "gzip"),
            )
            await self._offline_sync.start()
            self._config_version_mgr = S7ConfigVersionManager()
            await self._config_version_mgr.save_version(
                device_id=config.get("device_id", ip),
                config=config,
                change_summary="initial config on driver start",
                operator="system",
            )
            self._ota_mgr = S7OtaManager()
            self._ota_mgr.set_current_version(config.get("firmware_version", "1.0.0"))
        except asyncio.CancelledError:
            raise
        except Exception:
            await self.stop()
            self._set_conn_state(ConnectionState.DISCONNECTED.value)
            self._log_error(ip, S7DriverErrors.CONN_FAILED, f"Connection/start failed (rack={rack}, slot={slot})")
            raise

    async def stop(self) -> None:
        """停止S7驱动"""
        try:
            if self._heartbeat_task and not self._heartbeat_task.done():
                self._heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._heartbeat_task
            if (
                self._delayed_reconnect_task and not self._delayed_reconnect_task.done()
            ):  # FIXED-P1: 移除hasattr检查，_delayed_reconnect_task已在__init__中初始化
                self._delayed_reconnect_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._delayed_reconnect_task
            for _did, _rtask in list(self._reconnect_tasks.items()):
                if _rtask and not _rtask.done():
                    _rtask.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await _rtask
            self._reconnect_tasks.clear()
            if self._client:
                # S7-003: 使用安全的 snap7 调用，超时后不会永久阻塞
                client = self._client
                # FIXED-P0: 先执行disconnect/destroy再关闭executor，避免_safe_s7_call重建executor导致线程泄漏
                try:
                    await self._safe_s7_call(client.disconnect, timeout=5.0)
                except Exception as e:
                    logger.warning("[s7] stop failed: %s", e)  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                try:
                    await self._safe_s7_call(client.destroy, timeout=5.0)
                except Exception as e:
                    logger.warning("[s7] stop failed: %s", e)  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                if self._s7_executor is not None:
                    self._s7_executor.shutdown(wait=False, cancel_futures=True)
                    self._s7_executor = None
        finally:
            self._running = False
            self._heartbeat_task = None
            self._delayed_reconnect_task = None
            self._reconnect_tasks.clear()
            self._reconnect_locks.clear()
            # CROSS-004: 取消所有后台任务
            await self._cancel_background_tasks()
            with self._sync_lock:
                self._client = None
            if self._edge_trigger:
                await self._edge_trigger.stop()  # #[AUDIT-FIX] stop() is async, must await (was no-op coroutine)
                self._edge_trigger = None
            if self._rule_store:
                self._rule_store.stop()
                self._rule_store = None
            self._edge_rule_engine = None
            if self._offline_sync:
                await self._offline_sync.stop()
                self._offline_sync = None
            if self._ts_store:
                await self._ts_store.stop()
                self._ts_store = None
            if self._config_version_mgr:
                await self._config_version_mgr.stop()  # #[AUDIT-FIX] stop() is async, must await (was no-op coroutine)
                self._config_version_mgr = None
            self._ota_mgr = None
            if self._redundancy:
                self._redundancy.stop()
            self._devices.clear()  # FIXED-P2: stop()清理状态残留，避免reconnect后状态不一致
            self._circuit_open.clear()  # FIXED-P1: 清理熔断状态
            self._circuit_open_since.clear()  # FIXED-P0: 清理熔断时间记录
            with self._stats_lock:  # FIXED-P1: _health_stats.clear()加锁保护，与并发读取路径一致
                self._health_stats.clear()
                self._offline_since.clear()  # FIXED-P2: 移入_stats_lock块内，与基类读写路径锁保护一致
            self._point_health.clear()
            self._degraded = False
            self._reconnect_count = 0
            self._reconnect_delay = 1.0
            self._cached_pdu_size = 0
            self._collect_interval_multiplier = 1.0
            self._point_configs.clear()
            self._write_timestamps.clear()
            self._write_audit_log.clear()
            self._password_negotiated = False
            self._is_s7_200_smart = False
            # S7-002: 清理心跳重连状态
            self._hb_reconnect_count = 0
            self._hb_exhausted = False
            self._hb_permanent_offline = False  # FIXED-P2: 重置永久离线标记，允许restart后心跳启动
            await asyncio.to_thread(
                self._save_permanent_offline_state
            )  # FIXED-P0: 持久化清除，防止重启后从JSON加载旧状态导致驱动永远无法重连
            self._long_retry_level = 0  # FIXED-P2: 重置渐进式重连等级，防止restart后首次重连间隔过长
            self._hb_first_fail_time = (
                0.0  # FIXED-P0: 重置首次失败时间，与__init__类型一致，避免restart后time.monotonic()-None抛TypeError
            )

            logger.info("[s7] code=STOPPED msg=Driver stopped")
            # FIXED-P1: S7-R01 调用基类stop()确保_shutdown_executor和_cancel_background_tasks执行
            await super().stop()

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        """读取S7 PLC测点值

        测点地址格式: "DB1.X0.0" (数据块.类型偏移.位偏移)
        支持的类型前缀:
            X - 位(BOOL)
            B - 字节(INT8)
            W - 字(INT16)
            D - 双字(INT32/FLOAT)
            R - 实数(FLOAT32)
        """
        # FIXED-P1: 心跳重连耗尽后检测连接恢复，主动重置心跳状态加速恢复
        if self._hb_exhausted and await self._is_connected():
            self._hb_exhausted = False
            self._hb_reconnect_count = 0
            self._hb_reconnect_backoff = 0
            self._long_retry_level = 0
            with self._stats_lock:  # FIXED-P0: _offline_since pop纳入_stats_lock，与写入路径锁保护一致
                self._offline_since.pop(device_id, None)
            logger.info("[s7] device=%s code=HB_RECOVERED msg=Connection recovered, heartbeat state reset", device_id)
        # FIXED-P2: 永久离线后检测连接恢复，重置状态并重启心跳Task
        if self._hb_permanent_offline and await self._is_connected():
            self._hb_permanent_offline = False
            await asyncio.to_thread(self._save_permanent_offline_state)  # FIXED-P1: 同步清除持久化的永久离线状态
            self._hb_exhausted = False
            self._hb_reconnect_count = 0
            # FIXED-P1: 重置所有退避/重试状态，防止恢复后首次心跳失败使用过期退避参数
            self._hb_reconnect_backoff = 0
            self._long_retry_level = 0
            self._hb_first_fail_time = 0.0
            with self._stats_lock:  # FIXED-P0: _offline_since pop纳入_stats_lock，与写入路径锁保护一致
                self._offline_since.pop(device_id, None)
            # FIXED-P0: 使用锁保护心跳Task创建，防止并发read_points创建双心跳循环
            async with self._hb_task_lock:
                if self._running and (self._heartbeat_task is None or self._heartbeat_task.done()):
                    self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            logger.info(
                "[s7] device=%s code=HB_PERMANENT_RECOVERED msg=Connection recovered from permanent offline, heartbeat restarted",
                device_id,
            )

        # Check connection quality before reading
        quality = self.get_connection_quality(device_id)
        if quality < 60:
            logger.warning("[s7] device=%s code=QUALITY_LOW quality=%.1f, attempting reconnect", device_id, quality)
            await self._try_reconnect(device_id)
            new_quality = self.get_connection_quality(device_id)
            if new_quality > 80:
                logger.info("[s7] device=%s code=QUALITY_RECOVERED quality=%.1f->%.1f", device_id, quality, new_quality)
            else:
                await self._set_connection_state(
                    device_id, ConnectionState.DEGRADED.value, "Connection quality below threshold after reconnect"
                )
                logger.warning(
                    "[s7] device=%s code=QUALITY_DEGRADED quality=%.1f after reconnect", device_id, new_quality
                )

        if not self._running or not self._client:
            await self._try_reconnect(device_id)
            return {name: _bad_pv(S7DriverErrors.CONN_FAILED) for name in points}

        if self._password and time.time() < self._auth_locked_until:
            self._log_error(
                device_id,
                S7DriverErrors.AUTH_LOCKED,
                f"Auth locked, retry after {self._auth_locked_until - time.time():.0f}s",
            )
            return {name: _bad_pv(S7DriverErrors.AUTH_LOCKED) for name in points}

        result = {}
        start_time = time.monotonic()
        max_total_timeout = self._MAX_BATCH_RETRY_TIMEOUT if hasattr(self, "_MAX_BATCH_RETRY_TIMEOUT") else 30

        try:
            async with self._lock:
                record_packet("tx", "s7", device_id, f"S7 Read: {points}")
                values = await asyncio.wait_for(
                    self._run_in_s7_thread_async(self._read_points_batch, points),
                    timeout=self._READ_TIMEOUT,
                )
                result = values
                record_packet("rx", "s7", device_id, f"S7 Response: {len(result)} values")
        except TimeoutError:  # FIXED-P1: 兼容Python<3.11，asyncio.TimeoutError非TimeoutError子类
            self._record_read_failure(device_id)
            self._log_error(device_id, S7DriverErrors.READ_TIMEOUT, f"Batch read timeout ({self._READ_TIMEOUT}s)")
            segment_bytes = self._pdu_size - 12
            for retry in range(self._BATCH_RETRY_MAX):
                elapsed = time.monotonic() - start_time
                if elapsed >= max_total_timeout:
                    logger.warning(
                        "[s7] device=%s code=MAX_RETRY_TIMEOUT msg=Total retry timeout (%.1fs) reached, giving up",
                        device_id,
                        elapsed,
                    )
                    break
                segment_bytes = max(segment_bytes // 2, 16)
                remaining_timeout = max_total_timeout - elapsed
                logger.warning(
                    "[s7] device=%s code=BATCH_RETRY msg=Retry with reduced segment %d bytes (attempt %d/%d), remaining time %.1fs",
                    device_id,
                    segment_bytes,
                    retry + 1,
                    self._BATCH_RETRY_MAX,
                    remaining_timeout,
                )
                await asyncio.sleep(0.1)
                try:
                    async with self._lock:
                        values = await asyncio.wait_for(
                            self._run_in_s7_thread_async(self._read_points_batch, points, segment_bytes),
                            timeout=min(self._READ_TIMEOUT, remaining_timeout),
                        )
                        result = values
                        record_packet("rx", "s7", device_id, f"S7 Retry Response: {len(result)} values")
                        break
                except Exception as retry_err:
                    logger.warning(
                        "[s7] device=%s code=BATCH_RETRY_FAIL msg=Retry attempt %d failed: %s",
                        device_id,
                        retry + 1,
                        retry_err,
                    )
                    continue
            if not result:
                return {name: _bad_pv(S7DriverErrors.READ_TIMEOUT) for name in points}
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._record_read_failure(device_id)
            self._log_error(device_id, S7DriverErrors.READ_FAILED, f"Batch read failed, fallback to per-point - {e}")
            if not await self._is_connected():  # FIXED-P1: _is_connected已改为async
                await self._try_reconnect(device_id)
                return {name: _bad_pv(S7DriverErrors.READ_FAILED) for name in points}
            for point_addr in points:
                elapsed = time.monotonic() - start_time
                if elapsed >= max_total_timeout:  # FIXED-P2: 逐点读取总超时保护
                    logger.warning(
                        "[s7] device=%s code=PER_POINT_TIMEOUT msg=Total elapsed %.1fs exceeds limit, skipping remaining %d points",
                        device_id,
                        elapsed,
                        len(points) - len(result),
                    )
                    result[point_addr] = _bad_pv(S7DriverErrors.READ_TIMEOUT)
                    continue
                try:
                    value = await asyncio.wait_for(
                        self._run_in_s7_thread_async(self._read_point, point_addr),
                        timeout=self._READ_TIMEOUT,
                    )
                    result[point_addr] = value
                except TimeoutError:  # FIXED-P1: 兼容Python<3.11
                    logger.warning(
                        "[s7] device=%s code=READ_TIMEOUT msg=Point read timeout %s (%ds)",
                        device_id,
                        point_addr,
                        self._READ_TIMEOUT,
                    )
                    result[point_addr] = _bad_pv(S7DriverErrors.READ_TIMEOUT)
                except Exception as e2:
                    logger.warning(
                        "[s7] device=%s code=READ_ERROR msg=Point read failed %s - %s", device_id, point_addr, e2
                    )
                    result[point_addr] = _bad_pv(S7DriverErrors.READ_FAILED)

        # success rule: any non-None value counts as success
        if any(not isinstance(v, PointValue) or v.quality != "bad" for v in result.values()):
            await self._record_read_success(
                device_id
            )  # #[AUDIT-FIX] _record_read_success is async, must await (was no-op coroutine)
        else:
            self._record_read_failure(device_id)

        now = datetime.now(UTC)
        now_ts = time.time()
        global_db = self._config.get("deadband")
        global_scaling = self._config.get("scaling")
        global_clamp = self._config.get("clamp")
        roc_threshold = self._config.get("rate_of_change")
        frozen_threshold = self._config.get("frozen_count", self._FROZEN_COUNT_THRESHOLD)
        processed = {}
        good_count = 0
        total_count = len(result)

        for name, v in result.items():
            if isinstance(v, PointValue):
                if v.quality == "bad":
                    ph = self._point_health.get(name)
                    if ph is None:
                        ph = PointHealthStats()
                        self._point_health.set(name, ph)
                    ph.record_failure()
                    processed[name] = v
                    continue
                raw = v.value
            else:
                raw = v

            ph = self._point_health.get(name)
            if ph is None:
                ph = PointHealthStats()
                self._point_health.set(name, ph)
            pt_cfg = self._point_configs.get(name, {})
            pt_scaling = pt_cfg.get("scaling", global_scaling)
            pt_clamp = pt_cfg.get("clamp", global_clamp)
            pt_deadband = pt_cfg.get("deadband", global_db)  # BUG-004: 修复deadband值未赋值给变量
            pt_roc = pt_cfg.get("rate_of_change", roc_threshold)

            if isinstance(raw, (int, float)) and not math.isnan(raw) and not math.isinf(raw):
                if pt_scaling:
                    raw = self._apply_scaling(raw, pt_scaling)
                if pt_clamp:
                    raw, ok = self._apply_clamp(raw, pt_clamp)
                    if not ok:
                        processed[name] = PointValue(
                            value=raw, quality="bad", timestamp=now, source=f"s7:{S7DriverErrors.VALUE_OUT_OF_RANGE}"
                        )
                        ph.record_failure()
                        continue
                if pt_roc and ph.last_value is not None and ph.last_timestamp > 0:
                    dt = now_ts - ph.last_timestamp
                    if dt > 0:
                        roc = abs(raw - ph.last_value) / dt
                        if roc > pt_roc:
                            processed[name] = PointValue(
                                value=raw,
                                timestamp=now,
                                quality="uncertain",
                                source=f"s7:{S7DriverErrors.RATE_OF_CHANGE}",
                            )
                            ph.record_success()
                            good_count += 1
                            continue
                # BUG-004: 应用deadband死区过滤，值变化小于死区时跳过上报
                if (
                    pt_deadband
                    and ph.last_value is not None
                    and isinstance(raw, (int, float))
                    and isinstance(ph.last_value, (int, float))
                ):
                    if abs(raw - ph.last_value) <= pt_deadband:
                        ph.record_success()
                        good_count += 1
                        continue
                if ph.last_value is not None and raw == ph.last_value:
                    ph.same_value_count += 1
                else:
                    ph.same_value_count = 0
                if ph.same_value_count >= frozen_threshold and isinstance(raw, (int, float)):
                    processed[name] = PointValue(
                        value=raw, timestamp=now, quality="uncertain", source=f"s7:{S7DriverErrors.FROZEN_VALUE}"
                    )
                    ph.record_success()
                    good_count += 1
                    continue
            elif isinstance(raw, float) and (math.isnan(raw) or math.isinf(raw)):
                processed[name] = PointValue(
                    value=None, quality="bad", timestamp=now, source=f"s7:{S7DriverErrors.NAN_INF}"
                )
                ph.record_failure()
                continue

            quality = "good"
            if isinstance(v, PointValue) and v.quality not in ("bad",):
                quality = v.quality
            if isinstance(v, PointValue):
                processed[name] = PointValue(
                    value=raw, timestamp=v.timestamp or now, quality=quality, source=v.source, latency_ms=v.latency_ms
                )
            else:
                processed[name] = PointValue(value=raw, timestamp=now, quality=quality, source="device")
            ph.last_value = raw
            ph.last_timestamp = now_ts
            ph.record_success()
            good_count += 1

        result = processed

        if total_count > 0:
            rate = good_count / total_count
            if rate < self._DEGRADE_SUCCESS_THRESHOLD and not self._degraded:
                self._degraded = True
                self._collect_interval_multiplier = 2.0
                logger.warning(
                    "[s7] device=%s code=DEGRADE_ACTIVE msg=Success rate %.1f%% < %.0f%%, interval x%.1f",
                    device_id,
                    rate * 100,
                    self._DEGRADE_SUCCESS_THRESHOLD * 100,
                    self._collect_interval_multiplier,
                )
            elif rate > self._RECOVER_SUCCESS_THRESHOLD and self._degraded:
                self._degraded = False
                self._collect_interval_multiplier = 1.0
                logger.info(
                    "[s7] device=%s code=DEGRADE_RECOVERED msg=Success rate %.1f%% > %.0f%%, interval restored",
                    device_id,
                    rate * 100,
                    self._RECOVER_SUCCESS_THRESHOLD * 100,
                )

        await self._evaluate_edge_rules(device_id, result)

        if self._ts_store:
            await self._ts_store.write_read_result(device_id, result)

        return result

    def _read_points_batch(self, addresses: list[str], max_segment_bytes: int | None = None) -> dict[str, Any]:
        result = {}
        if not addresses:
            return result

        effective_max = max_segment_bytes or (self._pdu_size - 12)
        if effective_max <= 0:  # FIXED-P2: PDU大小异常时使用默认值，防止传0或负数给snap7
            logger.warning("[s7] PDU size too small (%d), using default 240", self._pdu_size)
            effective_max = 240 - 12

        if self._config.get("optimized_db", True):
            try:
                segments = self._optimize_db_reads(addresses, effective_max)
                for db_number, start_offset, total_bytes, items in segments:
                    try:
                        data = self._sync_db_read(db_number, start_offset, total_bytes)
                        for addr, rel_offset, size, type_char, bit_offset in items:
                            try:
                                result[addr] = self._extract_value(data, rel_offset, size, type_char, bit_offset)
                            except Exception as e:
                                logger.warning("[s7] code=DECODE_ERROR msg=Extract value failed %s - %s", addr, e)
                                result[addr] = _bad_pv(S7DriverErrors.DECODE_FAILED)
                    except Exception as e:
                        logger.warning(
                            "[s7] code=READ_ERROR msg=DB read failed DB%d.%d+%d - %s",
                            db_number,
                            start_offset,
                            total_bytes,
                            e,
                        )
                        for addr, _, _, _, _ in items:
                            result[addr] = _bad_pv(S7DriverErrors.READ_FAILED)
                return result
            except Exception as e:
                logger.warning("[s7] optimize_db_reads failed, fallback to per-point: %s", e, exc_info=True)
                for addr in addresses:
                    try:
                        result[addr] = self._read_point(addr)
                    except Exception as e:
                        logger.warning("[s7] code=READ_ERROR msg=Point read failed %s - %s", addr, e)
                        result[addr] = _bad_pv(S7DriverErrors.READ_FAILED)
                return result
        else:
            for addr in addresses:
                try:
                    result[addr] = self._read_point(addr)
                except Exception as e:
                    logger.warning("[s7] code=READ_ERROR msg=Point read failed %s - %s", addr, e)
                    result[addr] = _bad_pv(S7DriverErrors.READ_FAILED)
            return result

    def _parse_address(self, address: str) -> tuple[int, str, int, int, int]:
        """解析S7地址，返回 (db_number, type_char, byte_offset, bit_offset, size_bytes)"""
        parts = address.split(".")
        if len(parts) < 2 or not parts[0].startswith("DB"):
            raise ValueError(f"Invalid S7 address format: {address}, expected DBN.TB")

        try:
            db_number = int(parts[0][2:])
        except (ValueError, IndexError) as e:
            raise ValueError(f"Invalid S7 DB number in address: {address}") from e
        if len(parts) < 2 or not parts[1]:
            raise ValueError(f"Invalid S7 type/offset in address: {address}")
        type_char = parts[1][0].upper()
        try:
            byte_offset = int(parts[1][1:])
        except ValueError as e:
            raise ValueError(f"Invalid S7 byte offset in address: {address}") from e
        bit_offset = int(parts[2]) if len(parts) > 2 else 0
        if bit_offset < 0 or bit_offset > 7:  # FIXED-P2: 校验bit_offset范围，防止位操作语义错误
            raise ValueError(f"Invalid S7 bit offset in address: {address} (must be 0-7, got {bit_offset})")

        # FIXED(严重): 校验DB编号范围，防止负数或超大值导致snap7 C层内存越界
        if not (1 <= db_number <= 65535):
            raise ValueError(f"S7 DB number out of range [1-65535]: {db_number} in address '{address}'")
        # FIXED(严重): 校验字节偏移范围，负偏移或超大偏移会导致snap7 C层内存越界
        if not (0 <= byte_offset <= 65535):
            raise ValueError(f"S7 byte offset out of range [0-65535]: {byte_offset} in address '{address}'")

        # 根据类型确定读取字节数
        size_map = {"X": 1, "B": 1, "W": 2, "D": 4, "R": 4}
        size = size_map.get(type_char)
        if size is None:
            raise ValueError(f"Unsupported S7 data type: {type_char}")

        return db_number, type_char, byte_offset, bit_offset, size

    def _optimize_db_reads(
        self, addresses: list[str], max_segment_bytes: int | None = None
    ) -> list[tuple[int, int, int, list[tuple[str, int, int, str, int]]]]:
        parsed = []
        for addr in addresses:
            db, type_char, byte_offset, bit_offset, size = self._parse_address(addr)
            parsed.append((db, byte_offset, size, addr, type_char, bit_offset))

        by_db: dict[int, list[tuple[int, int, str, str, int]]] = {}
        for db, offset, size, addr, type_char, bit_offset in parsed:
            by_db.setdefault(db, []).append((offset, size, addr, type_char, bit_offset))

        effective_max = max_segment_bytes or (self._pdu_size - 12)
        # FIXED-P1: PDU边界校验，防止_pdu_size异常(0或<12)时effective_max为负数导致批量读取退化
        if effective_max < 1:
            effective_max = 240 - 12  # 回退到S7默认PDU 240

        # 合并连续范围并按PDU大小分包
        segments: list[tuple[int, int, int, list[tuple[str, int, int, str, int]]]] = []
        for db, items in by_db.items():
            items.sort()
            seg_start = items[0][0]
            seg_end = seg_start + items[0][1]
            seg_items: list[tuple[str, int, int, str, int]] = [(items[0][2], 0, items[0][1], items[0][3], items[0][4])]

            for offset, size, addr, type_char, bit_offset in items[1:]:
                new_end = max(seg_end, offset + size)
                # 允许4字节间隔合并，且不超过PDU限制
                if offset <= seg_end + 4 and (new_end - seg_start) <= effective_max:
                    rel_offset = offset - seg_start
                    seg_items.append((addr, rel_offset, size, type_char, bit_offset))
                    seg_end = new_end
                else:
                    # 保存当前段
                    segments.append((db, seg_start, seg_end - seg_start, seg_items))
                    # 开始新段
                    seg_start = offset
                    seg_end = offset + size
                    seg_items = [(addr, 0, size, type_char, bit_offset)]

            segments.append((db, seg_start, seg_end - seg_start, seg_items))

        return segments

    @staticmethod
    def _extract_value(data: bytearray, offset: int, size: int, type_char: str, bit_offset: int) -> Any:
        import struct

        if not 0 <= bit_offset <= 7:
            logger.warning("S7 bit_offset out of range: %d, clamping to 0-7", bit_offset)
            bit_offset = max(0, min(7, bit_offset))

        if offset + size > len(data):
            raise ValueError(f"S7 data bounds check failed: offset={offset}, size={size}, data_len={len(data)}")

        if type_char == "X":
            return bool(data[offset] & (1 << bit_offset))
        elif type_char == "B":
            return int.from_bytes(data[offset : offset + 1], byteorder="big", signed=True)
        elif type_char == "W":
            return int.from_bytes(data[offset : offset + 2], byteorder="big", signed=True)
        elif type_char == "D":
            return int.from_bytes(data[offset : offset + 4], byteorder="big", signed=True)
        elif type_char == "R":
            val = struct.unpack(">f", data[offset : offset + 4])[0]
            if math.isnan(val) or math.isinf(val):
                raise ValueError(f"NaN/Inf detected: {val}")
            return val
        else:
            raise ValueError(f"Unsupported S7 data type: {type_char}")

    def _read_point(self, address: str) -> Any:
        """同步读取单个测点（在线程池中执行）"""
        parts = address.split(".")
        if len(parts) < 2 or not parts[0].startswith("DB"):
            raise ValueError(f"Invalid S7 address format: {address}, expected DBN.TB")

        try:  # FIXED: 原问题-parts[0][2:]/parts[1]硬索引，格式错误时IndexError/ValueError
            db_number = int(parts[0][2:])
        except (ValueError, IndexError) as e:
            raise ValueError(f"Invalid S7 DB number in address: {address}") from e
        # FIXED-P0: DB编号边界校验，防止无效参数传入snap7 C层导致段错误
        if not 1 <= db_number <= 65535:
            raise ValueError(f"Invalid S7 DB number in address: {address} (must be 1-65535, got {db_number})")
        if len(parts) < 2 or not parts[1]:
            raise ValueError(f"Invalid S7 type/offset in address: {address}")
        type_char = parts[1][0].upper()
        try:
            byte_offset = int(parts[1][1:])
        except ValueError as e:
            raise ValueError(f"Invalid S7 byte offset in address: {address}") from e
        # FIXED-P0: 偏移量边界校验，防止越界内存访问
        if not 0 <= byte_offset <= 65535:
            raise ValueError(f"Invalid S7 byte offset in address: {address} (must be 0-65535, got {byte_offset})")
        bit_offset = int(parts[2]) if len(parts) > 2 else 0
        if bit_offset < 0 or bit_offset > 7:  # FIXED-P2: 校验bit_offset范围，防止位操作语义错误
            raise ValueError(f"Invalid S7 bit offset in address: {address} (must be 0-7, got {bit_offset})")

        if type_char == "X":
            data = self._sync_db_read(db_number, byte_offset, 1)
            # FIXED-P2: 验证返回数据长度，防止截断数据导致解析异常
            if len(data) < 1:
                raise ValueError(f"Insufficient data for {address}: need 1, got {len(data)}")
            return bool(data[0] & (1 << bit_offset))
        elif type_char == "B":
            data = self._sync_db_read(db_number, byte_offset, 1)
            # FIXED-P2: 验证返回数据长度，防止截断数据导致解析异常
            if len(data) < 1:
                raise ValueError(f"Insufficient data for {address}: need 1, got {len(data)}")
            return int.from_bytes(data, byteorder="big", signed=True)
        elif type_char == "W":
            data = self._sync_db_read(db_number, byte_offset, 2)
            # FIXED-P2: 验证返回数据长度，防止截断数据导致解析异常
            if len(data) < 2:
                raise ValueError(f"Insufficient data for {address}: need 2, got {len(data)}")
            return int.from_bytes(data, byteorder="big", signed=True)
        elif type_char == "D":
            data = self._sync_db_read(db_number, byte_offset, 4)
            # FIXED-P2: 验证返回数据长度，防止截断数据导致解析异常
            if len(data) < 4:
                raise ValueError(f"Insufficient data for {address}: need 4, got {len(data)}")
            return int.from_bytes(data, byteorder="big", signed=True)
        elif type_char == "R":
            data = self._sync_db_read(db_number, byte_offset, 4)
            # FIXED-P2: 验证返回数据长度，防止截断数据导致解析异常
            if len(data) < 4:
                raise ValueError(f"Insufficient data for {address}: need 4, got {len(data)}")
            import struct

            val = struct.unpack(">f", data)[0]
            if math.isnan(val) or math.isinf(val):
                raise ValueError(f"NaN/Inf detected: {val}")
            return val
        else:
            raise ValueError(f"不支持的S7数据类型: {type_char}")

    class WriteVerifyError(Exception):
        pass

    class WriteValueInvalidError(Exception):
        pass

    class WriteRateLimitedError(Exception):
        pass

    def _validate_write_value(self, type_char: str, value: Any, bit_offset: int = 0) -> None:
        if type_char == "X":
            if value not in (0, 1, True, False):
                raise self.WriteValueInvalidError(f"BOOL value must be 0/1, got {value}")
        elif type_char == "B":
            if not isinstance(value, int) or value < -128 or value > 127:
                raise self.WriteValueInvalidError(f"BYTE value must be -128~127, got {value}")
        elif type_char == "W":
            if not isinstance(value, int) or value < -32768 or value > 32767:
                raise self.WriteValueInvalidError(f"WORD value must be -32768~32767, got {value}")
        elif type_char == "D":
            if not isinstance(value, int) or value < -2147483648 or value > 2147483647:
                raise self.WriteValueInvalidError(f"DWORD value must be -2147483648~2147483647, got {value}")
        elif type_char == "R":
            if not isinstance(value, (int, float)):
                raise self.WriteValueInvalidError(f"REAL value must be numeric, got {type(value).__name__}")
            if math.isnan(value) or math.isinf(value):
                raise self.WriteValueInvalidError(f"REAL value must not be NaN/Inf, got {value}")

    def _check_write_rate_limit(self, address: str) -> None:
        now = time.monotonic()  # FIXED: 使用monotonic时钟，避免墙钟回拨导致限流失效
        last = self._write_timestamps.get(address, 0.0)
        elapsed_ms = (now - last) * 1000
        if elapsed_ms < self._WRITE_RATE_LIMIT_MS:
            raise self.WriteRateLimitedError(
                f"Write rate limited for {address}, elapsed {elapsed_ms:.0f}ms < {self._WRITE_RATE_LIMIT_MS}ms"
            )
        self._write_timestamps.set(address, now)  # FIXED-P1: LRUCache使用set()而非[]赋值

    def _audit_write(
        self, device_id: str, point: str, old_value: Any, new_value: Any, result: str, error_code: str = ""
    ) -> None:
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "device_id": device_id,
            "point": point,
            "old_value": old_value,
            "new_value": new_value,
            "result": result,
            "error_code": error_code,
        }
        self._write_audit_log.append(entry)

    async def _verify_write(
        self, point: str, value: Any, type_char: str, db_number: int, byte_offset: int, bit_offset: int
    ) -> None:
        await asyncio.sleep(self._WRITE_VERIFY_DELAY_MS / 1000.0)
        read_back = await asyncio.wait_for(
            self._run_in_s7_thread_async(self._read_point, point),
            timeout=2.0,
        )
        if type_char == "R":
            if not isinstance(read_back, float) or not isinstance(value, (int, float)):
                raise self.WriteVerifyError(f"Read-back type mismatch: wrote {value}, read {read_back}")
            if abs(read_back - float(value)) > abs(float(value)) * 1e-6 + 1e-6:
                raise self.WriteVerifyError(f"Read-back mismatch: wrote {value}, read {read_back}")
        else:
            expected = bool(value) if type_char == "X" else value
            if read_back != expected:
                raise self.WriteVerifyError(f"Read-back mismatch: wrote {expected}, read {read_back}")

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        # SEC-FIX(修复2): 驱动层写入权限检查，防止内部服务绕过 API 层鉴权直接写入
        if hasattr(self, "check_permission"):
            from edgelite.security.rbac import Permission

            if not await self.check_permission(Permission.DEVICE_WRITE_POINT):
                logger.warning(
                    "[s7] write denied: role=%s lacks device:write_point, device=%s point=%s",
                    getattr(self, "_current_user_role", "unknown"),
                    device_id,
                    point,
                )
                return False
        if not self._running or not self._client:
            await self._try_reconnect(device_id)
            return False

        parts = point.split(".")
        if len(parts) < 2 or not parts[0].startswith("DB"):
            self._log_error(device_id, S7DriverErrors.WRITE_VALUE_INVALID, f"Invalid address: {point}")
            return False
        type_char = parts[1][0].upper()
        bit_offset = int(parts[2]) if len(parts) > 2 else 0

        try:
            self._validate_write_value(type_char, value, bit_offset)
        except self.WriteValueInvalidError as e:
            self._record_write_failure(device_id)
            self._log_error(device_id, S7DriverErrors.WRITE_VALUE_INVALID, str(e))
            self._audit_write(device_id, point, None, value, "rejected", S7DriverErrors.WRITE_VALUE_INVALID)
            return False

        try:
            self._check_write_rate_limit(point)
        except self.WriteRateLimitedError as e:
            self._record_write_failure(device_id)
            self._log_error(device_id, S7DriverErrors.WRITE_RATE_LIMITED, str(e))
            self._audit_write(device_id, point, None, value, "rate_limited", S7DriverErrors.WRITE_RATE_LIMITED)
            return False

        old_value = None
        try:
            async with self._lock:
                if self._write_verify_enabled:
                    try:
                        old_value = await asyncio.wait_for(
                            self._run_in_s7_thread_async(self._read_point, point),
                            timeout=self._WRITE_TIMEOUT,
                        )
                    except Exception:
                        old_value = None
                record_packet("tx", "s7", device_id, f"S7 Write: {point}={value}")
                await asyncio.wait_for(
                    self._run_in_s7_thread_async(self._write_point, point, value),
                    timeout=self._WRITE_TIMEOUT,
                )
                record_packet("rx", "s7", device_id, f"S7 Write OK: {point}")
                if self._write_verify_enabled:
                    try:
                        db_number = int(parts[0][2:])
                        byte_offset = int(parts[1][1:])
                        await self._verify_write(point, value, type_char, db_number, byte_offset, bit_offset)
                    except self.WriteVerifyError as e:
                        self._record_write_failure(device_id)
                        self._log_error(device_id, S7DriverErrors.WRITE_VERIFY_FAILED, str(e))
                        self._audit_write(
                            device_id, point, old_value, value, "verify_failed", S7DriverErrors.WRITE_VERIFY_FAILED
                        )
                        return False
            self._record_write_success(device_id)
            self._audit_write(device_id, point, old_value, value, "ok")
            return True
        except TimeoutError:  # FIXED-P1: 兼容Python<3.11
            self._audit_write(device_id, point, old_value, value, "timeout", S7DriverErrors.WRITE_TIMEOUT)
            return False
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._record_write_failure(device_id)
            self._log_error(device_id, S7DriverErrors.WRITE_FAILED, f"Write failed {point} - {e}")
            self._audit_write(device_id, point, old_value, value, "failed", S7DriverErrors.WRITE_FAILED)
            if not await self._is_connected():  # FIXED-P1: _is_connected已改为async
                await self._try_reconnect(device_id)
            return False

    def _write_point(self, address: str, value: Any) -> None:
        """同步写入单个测点"""
        parts = address.split(".")
        try:  # FIXED: 原问题-_write_point同样存在硬索引问题
            db_number = int(parts[0][2:])
        except (ValueError, IndexError) as e:
            raise ValueError(f"Invalid S7 DB number in address: {address}") from e
        # FIXED-P0: DB编号边界校验，防止写入PLC非预期内存区域
        if not 1 <= db_number <= 65535:
            raise ValueError(f"Invalid S7 DB number in address: {address} (must be 1-65535, got {db_number})")
        if len(parts) < 2 or not parts[1]:
            raise ValueError(f"Invalid S7 type/offset in address: {address}")
        type_char = parts[1][0].upper()
        try:
            byte_offset = int(parts[1][1:])
        except ValueError as e:
            raise ValueError(f"Invalid S7 byte offset in address: {address}") from e
        # FIXED-P0: 偏移量边界校验，防止越界写入PLC内存
        if not 0 <= byte_offset <= 65535:
            raise ValueError(f"Invalid S7 byte offset in address: {address} (must be 0-65535, got {byte_offset})")
        bit_offset = int(parts[2]) if len(parts) > 2 else 0
        # FIXED-P0: bit_offset范围校验，防止位操作越界
        if not 0 <= bit_offset <= 7:
            raise ValueError(f"Invalid S7 bit offset in address: {address} (must be 0-7, got {bit_offset})")

        if type_char == "X":
            # FIXED-P1: BOOL写入读-改-写在_sync_lock内原子执行，防止并发位写入丢失
            with self._sync_lock:
                client = self._client
                if client is None:
                    raise ConnectionError("S7 client is not connected")
                # FIXED-P1: 原问题-BOOL写入路径直接调用db_read/db_write未设置snap7超时，C层阻塞时_sync_lock被长时间持有
                old_timeout = None
                try:
                    old_timeout = client.get_timeout()
                # FIXED-P1: 原问题-except Exception: pass 静默吞没异常，改为至少 logger.debug
                except Exception as e:
                    logger.debug("[s7] code=GET_TIMEOUT_FAILED msg=client.get_timeout() failed: %s", e)
                try:
                    write_timeout_ms = int(self._config.get("write_timeout", 5000)) if self._config else 5000
                    client.set_timeout(write_timeout_ms)
                    data = client.db_read(db_number, byte_offset, 1)
                    if value:
                        data[0] |= 1 << bit_offset
                    else:
                        data[0] &= ~(1 << bit_offset)
                    client.db_write(db_number, byte_offset, data)
                finally:
                    if old_timeout is not None:
                        try:
                            client.set_timeout(old_timeout)
                        # FIXED-P1: 原问题-except Exception: pass 静默吞没异常，改为至少 logger.debug
                        except Exception as e:
                            logger.debug(
                                "[s7] code=SET_TIMEOUT_RESTORE_FAILED msg=client.set_timeout(old_timeout) failed: %s", e
                            )
        elif type_char == "B":
            data = bytearray([int(value) & 0xFF])
            self._sync_db_write(db_number, byte_offset, data)
        elif type_char == "W":
            data = value.to_bytes(2, byteorder="big", signed=True)
            self._sync_db_write(db_number, byte_offset, data)
        elif type_char == "D":
            data = value.to_bytes(4, byteorder="big", signed=True)
            self._sync_db_write(db_number, byte_offset, data)
        elif type_char == "R":
            import struct

            data = struct.pack(">f", float(value))
            self._sync_db_write(db_number, byte_offset, data)
        else:
            raise ValueError(
                f"Unsupported S7 data type for write: {type_char}"
            )  # FIXED-P2: 不支持的类型抛出异常而非静默跳过

    async def batch_write_points(self, device_id, writes):
        if not self._running or not self._client:
            await self._try_reconnect(device_id)
            return {point: False for point, _ in writes}

        validated = []
        for point, value in writes:
            parts = point.split(".")
            if len(parts) < 2 or not parts[0].startswith("DB"):
                continue
            type_char = parts[1][0].upper()
            try:
                self._validate_write_value(type_char, value)
            except self.WriteValueInvalidError:
                self._audit_write(device_id, point, None, value, "rejected", S7DriverErrors.WRITE_VALUE_INVALID)
                continue
            try:
                self._check_write_rate_limit(point)
            except self.WriteRateLimitedError:
                self._audit_write(device_id, point, None, value, "rate_limited", S7DriverErrors.WRITE_RATE_LIMITED)
                continue
            validated.append((point, value))

        async def _do_write(point, value):
            try:
                async with self._lock:
                    record_packet("tx", "s7", device_id, f"S7 Write: {point}={value}")
                    await asyncio.wait_for(
                        self._run_in_s7_thread_async(self._write_point, point, value),
                        timeout=self._WRITE_TIMEOUT,
                    )
                    record_packet("rx", "s7", device_id, f"S7 Write OK: {point}")
                self._record_write_success(device_id)
                self._audit_write(device_id, point, None, value, "ok")
                return True
            except TimeoutError:  # FIXED-P1: 兼容Python<3.11
                self._record_write_failure(device_id)
                self._log_error(
                    device_id, S7DriverErrors.WRITE_TIMEOUT, f"Write timeout {point} ({self._WRITE_TIMEOUT}s)"
                )
                self._audit_write(device_id, point, None, value, "timeout", S7DriverErrors.WRITE_TIMEOUT)
                return False
            except Exception as e:
                self._record_write_failure(device_id)
                self._log_error(device_id, S7DriverErrors.WRITE_FAILED, f"Write failed {point} - {e}")
                self._audit_write(device_id, point, None, value, "failed", S7DriverErrors.WRITE_FAILED)
                return False

        tasks = [_do_write(point, value) for point, value in validated]
        if not tasks:
            return {point: False for point, _ in writes}
        results = []
        for task in tasks:
            results.append(await task)  # FIXED-P2: 串行执行写入任务，保证写入顺序与请求一致
        result_map = {}
        for i, r in enumerate(results):
            point = validated[i][0]
            result_map[point] = False if isinstance(r, Exception) else bool(r)
        for point, _ in writes:
            if point not in result_map:
                result_map[point] = False
        return result_map

    async def add_device(self, device_id: str, config: dict, points: list[dict] | None = None) -> None:
        """添加S7设备，保存配置和测点映射"""
        if points is None:
            points = []
        self._devices[device_id] = {
            "config": config,
            "points": {p.get("name", p.get("address", "")): p for p in points if p.get("name") or p.get("address")},
        }
        logger.info("S7设备已添加: %s (%d测点)", device_id, len(points))

    async def discover_devices(self, config: dict) -> list[dict]:
        """扫描IP段发现S7设备，通过尝试S7连接测试判断设备是否在线

        config参数:
            network: 网段地址 (如 "192.168.1.0/24") 或单个IP
            host: 单个IP地址 (与network二选一)
            port: S7端口 (默认102)
            rack: 机架号 (默认0)
            slot: 插槽号 (默认1)
            timeout: 连接超时秒数 (默认3)
            max_concurrent: 最大并发数 (默认10)
        """
        try:
            import snap7
        except ImportError:
            logger.warning("[s7] snap7未安装，无法执行S7设备发现")
            return []

        import ipaddress

        network = config.get("network", "")
        host = config.get("host", config.get("ip", ""))
        port = int(config.get("port", 102))
        rack = int(config.get("rack", 0))
        slot = int(config.get("slot", 1))
        timeout = int(config.get("timeout", 3))
        max_concurrent = int(config.get("max_concurrent", 10))

        # 确定要扫描的IP列表
        if network:
            try:
                net = ipaddress.ip_network(network, strict=False)
                # FIXED-P1: 先用num_addresses预判网段大小，防止/8等大网段先生成全部IP再截断导致OOM
                if net.num_addresses > 1026:
                    logger.error(
                        "[s7] code=NETWORK_TOO_LARGE msg=Network %s has %d addresses, max 1026",
                        network,
                        net.num_addresses,
                    )
                    return []
                ips = [str(ip) for ip in net.hosts()]

            except ValueError as e:
                logger.error("[s7] S7发现: 无效的网段 %s - %s", network, e)
                return []
        elif host:
            ips = [host]
        else:
            logger.warning("[s7] S7发现: 未指定network或host参数")
            return []

        discovered = []
        sem = asyncio.Semaphore(max_concurrent)

        async def _probe(ip_addr: str) -> dict | None:
            async with sem:
                client = None
                try:
                    client = snap7.client.Client()
                    client.set_connection_params(ip_addr, 0, 0, timeout)
                    await asyncio.wait_for(
                        asyncio.to_thread(client.connect, ip_addr, rack, slot),
                        timeout=timeout + 1,
                    )
                    try:
                        info = await asyncio.to_thread(client.get_cpu_info)
                        model = info.ModuleName if hasattr(info, "ModuleName") else ""
                        serial = info.SerialNumber if hasattr(info, "SerialNumber") else ""
                    except Exception:
                        model = ""
                        serial = ""
                    return {
                        "device_id": f"s7_{ip_addr.replace('.', '_')}",
                        "name": f"S7 PLC ({ip_addr})" + (f" - {model}" if model else ""),
                        "protocol": "s7",
                        "config": {
                            "ip": ip_addr,
                            "port": port,
                            "rack": rack,
                            "slot": slot,
                        },
                        "points": [],
                        "details": {
                            "model": model,
                            "serial": serial,
                        },
                    }
                except Exception as e:
                    logger.debug("[s7] error: %s", e)
                    return None
                finally:
                    if client:
                        try:
                            await asyncio.wait_for(asyncio.to_thread(client.disconnect), timeout=5.0)
                        except TimeoutError:
                            try:
                                client.destroy()  # FIXED-P2: disconnect超时后仍调用destroy释放C层资源
                            except Exception as e:
                                logger.warning(
                                    "[s7] operation failed: %s", e
                                )  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                        except Exception as e:
                            logger.warning(
                                "[s7] operation failed: %s", e
                            )  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                        try:
                            client.destroy()
                        except Exception as e:
                            logger.warning(
                                "[s7] operation failed: %s", e
                            )  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录

        tasks = [_probe(ip) for ip in ips]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for r in results:
            if isinstance(r, dict):
                discovered.append(r)

        logger.info("S7设备发现完成: 扫描%d个IP, 发现%d台设备", len(ips), len(discovered))
        return discovered

    def remove_device(self, device_id: str) -> None:
        """Remove a device at runtime"""
        with self._stats_lock:  # FIXED-P0: _health_stats和_offline_since pop纳入_stats_lock，与写入路径锁保护一致
            self._health_stats.pop(device_id, None)
            self._offline_since.pop(device_id, None)
        self._circuit_open.discard(device_id)  # FIXED-P1: 清理熔断状态
        self._circuit_open_since.pop(device_id, None)  # FIXED-P1: 清理熔断时间记录
        # FIXED-P1: 取消该设备的重连任务并清理重连锁，防止已移除设备的幽灵任务继续运行
        reconnect_task = self._reconnect_tasks.pop(device_id, None)
        if reconnect_task and not reconnect_task.done():
            reconnect_task.cancel()
        self._reconnect_locks.pop(device_id, None)
        logger.info("S7 device removed: %s", device_id)

    async def _is_connected(self) -> bool:  # FIXED-P1: 改为async，通过s7_executor执行避免阻塞事件循环
        """检查S7客户端连接状态"""
        client = self._client  # FIXED-P1: 快照client引用，防止重连期间使用被替换的client
        if not client:
            return False
        try:
            return await self._run_in_s7_thread_async(client.get_connected, timeout=3.0)
        except Exception as e:
            logger.debug("[s7] error: %s", e)
            return False

    async def _get_pdu_size(self) -> int:
        client = self._client  # FIXED-P1: 快照client引用，防止重连期间使用被替换的client
        try:
            pdu = await self._run_in_s7_thread_async(client.get_pdu_size)
            if pdu and pdu > 0:
                if not (128 <= pdu <= 65535):
                    logger.warning(
                        "[s7] device=%s code=PDU_OUT_OF_RANGE msg=Negotiated PDU size %d out of range [128-65535], using default 240",
                        self._config.get("ip", "unknown"),
                        pdu,
                    )
                    return 240
                return pdu
        except Exception:
            self._log_error(
                self._config.get("ip", "unknown"),
                S7DriverErrors.PDU_FAILED,
                "Failed to get PDU size, using default 240",
            )
        return 240

    def _resolve_pdu_by_model(self, model: str) -> int:
        model_upper = model.upper()
        for key, pdu in self._PLC_PDU_MAP.items():
            if key.upper() in model_upper or model_upper in key.upper():
                return pdu
        if "1200" in model_upper or "121" in model_upper or "122" in model_upper or "123" in model_upper:
            return 480
        if "1500" in model_upper or "151" in model_upper or "152" in model_upper:
            return 960
        if "400" in model_upper or "416" in model_upper or "414" in model_upper:
            return 480
        if "300" in model_upper or "315" in model_upper or "317" in model_upper:
            return 240
        if "200" in model_upper or "SMART" in model_upper:
            return 240
        return 240

    async def _negotiate_password(self, ip: str) -> None:
        if time.time() < self._auth_locked_until:
            self._log_error(
                ip, S7DriverErrors.AUTH_LOCKED, f"Auth locked, {self._auth_locked_until - time.time():.0f}s remaining"
            )
            raise RuntimeError(f"S7 auth locked for {ip}")

        if not self._password:
            self._password_negotiated = True
            return

        try:
            import snap7

            password_bytes = bytes.fromhex(self._password)
            if len(password_bytes) != 8:
                logger.warning("[s7] device=%s code=PASSWORD_INVALID msg=密码应为8字节十六进制", ip)
                self._password_negotiated = False
                return

            client = self._client  # FIXED-P1: 快照client引用，防止重连期间使用被替换的client
            if hasattr(client, "set_password"):
                await self._run_in_s7_thread_async(client.set_password, password_bytes)
                logger.info("[s7] device=%s code=PASSWORD_SET msg=密码已设置，正在验证访问权限...", ip)
                self._password_negotiated = True
                self._password_fail_count = 0
            elif hasattr(client, "password"):
                client.password = password_bytes
                logger.info("[s7] device=%s code=PASSWORD_SET_LEGACY msg=密码已设置（legacy模式）", ip)
                self._password_negotiated = True
                self._password_fail_count = 0
            else:
                logger.warning(
                    "[s7] device=%s code=PASSWORD_NOT_SUPPORTED "
                    "msg=当前snap7版本不支持密码协商，尝试直接访问。 "
                    "如需密码保护，请升级 python-snap7>=1.2.0",
                    ip,
                )
                self._password_negotiated = False

        except Exception as e:
            self._password_fail_count += 1
            if self._password_fail_count >= self._PASSWORD_FAIL_THRESHOLD:
                self._auth_locked_until = time.time() + self._AUTH_LOCK_DURATION
                self._log_error(
                    ip,
                    S7DriverErrors.AUTH_LOCKED,
                    f"Password failed {self._password_fail_count} times, locked for {self._AUTH_LOCK_DURATION}s",
                )
                raise RuntimeError(f"S7 auth locked for {ip}") from e
            self._log_error(
                ip,
                S7DriverErrors.PASSWORD_FAILED,
                f"Password negotiation failed ({self._password_fail_count}/{self._PASSWORD_FAIL_THRESHOLD}) - {e}",
            )
            self._password_negotiated = False

    def _compute_adaptive_hb_interval(self) -> int:
        if not self._hb_latency_samples:
            return self._heartbeat_interval
        avg_latency = sum(self._hb_latency_samples) / len(self._hb_latency_samples)
        if avg_latency < self._HB_GOOD_LATENCY:
            return self._HB_MIN_INTERVAL
        if avg_latency > self._HB_BAD_LATENCY:
            return self._HB_MAX_INTERVAL
        ratio = (avg_latency - self._HB_GOOD_LATENCY) / (self._HB_BAD_LATENCY - self._HB_GOOD_LATENCY)
        return int(self._HB_MIN_INTERVAL + ratio * (self._HB_MAX_INTERVAL - self._HB_MIN_INTERVAL))

    def _permanent_offline_file_path(self) -> str:
        """FIXED-P1: 获取永久离线状态持久化文件路径

        原问题：_hb_permanent_offline 仅存在于内存，进程重启后丢失，
                已标记永久离线的设备会在重启后立即尝试重连，浪费资源并可能触发告警风暴
        修复：将永久离线状态持久化到本地JSON文件，启动时加载
        """
        data_dir = os.environ.get("EDGELITE_DATA_DIR", "data")
        os.makedirs(data_dir, exist_ok=True)
        ip = self._config.get("ip", "unknown") if self._config else "unknown"
        # 对IP做安全编码，防止路径穿越
        safe_ip = "".join(c if c.isalnum() or c in ".-_" else "_" for c in str(ip))
        return os.path.join(data_dir, f"s7_permanent_offline_{safe_ip}.json")

    def _save_permanent_offline_state(self) -> None:
        """FIXED-P1: 保存永久离线状态到JSON文件"""
        try:
            path = self._permanent_offline_file_path()
            state = {
                "ip": self._config.get("ip", "unknown") if self._config else "unknown",
                "permanent_offline": self._hb_permanent_offline,
                "first_fail_time": self._hb_first_fail_time,
                "updated_at": time.time(),
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)
        except Exception as e:
            logger.warning("[s7] code=PERM_OFFLINE_SAVE_FAILED msg=Failed to save permanent offline state: %s", e)

    def _load_permanent_offline_state(self) -> None:
        """FIXED-P1: 从JSON文件加载永久离线状态"""
        try:
            path = self._permanent_offline_file_path()
            if not os.path.exists(path):
                return
            with open(path, encoding="utf-8") as f:
                state = json.load(f)
            if state.get("permanent_offline"):
                self._hb_permanent_offline = True
                self._hb_first_fail_time = state.get("first_fail_time", 0.0)
                logger.warning(
                    "[s7] code=PERM_OFFLINE_LOADED msg=Device loaded as permanently offline from persisted state (ip=%s)",
                    state.get("ip", "unknown"),
                )
        except Exception as e:
            logger.warning("[s7] code=PERM_OFFLINE_LOAD_FAILED msg=Failed to load permanent offline state: %s", e)

    async def _heartbeat_loop(self) -> None:
        device_id = self._config.get("ip", "unknown")
        while self._running:
            try:  # FIXED-P2: 心跳循环顶层异常保护，防止未预见异常导致心跳Task静默死亡
                if self._hb_permanent_offline:  # FIXED-P1: 永久离线设备不再尝试心跳重连
                    break
                effective_interval = self._compute_adaptive_hb_interval()
                await asyncio.sleep(effective_interval)
                if not self._running:
                    break
                if not await self._is_connected():  # FIXED-P1: _is_connected已改为async
                    # FIXED-P1: _offline_since读写纳入_stats_lock，与stop()/reset_health_stats锁保护一致
                    with self._stats_lock:
                        if device_id not in self._offline_since:
                            self._offline_since[device_id] = datetime.now(UTC)
                            self._log_error(device_id, S7DriverErrors.CONN_LOST, f"Connection lost at {time.ctime()}")
                            offline_duration = 0.0
                        else:
                            offline_duration = (datetime.now(UTC) - self._offline_since[device_id]).total_seconds()
                    if offline_duration > 30:
                        # S7-002: 检查是否已达到最大重连次数
                        if self._hb_exhausted:
                            # S7-MED-001: 渐进式长间隔重试模式
                            if self._LONG_RETRY_PROGRESSIVE:
                                # 渐进式增长: 1h → 2h → 4h → 8h (上限)
                                backoff_delay = min(
                                    self._LONG_RETRY_INITIAL * (2**self._long_retry_level), self._LONG_RETRY_MAX
                                )
                                logger.warning(
                                    "[s7] device=%s code=HB_LONG_RETRY msg=Heartbeat exhausted, "
                                    "progressive long retry level=%d delay=%.0fs (%.2fh)",
                                    device_id,
                                    self._long_retry_level,
                                    backoff_delay,
                                    backoff_delay / 3600,
                                )
                            else:
                                backoff_delay = self._HB_LONG_RETRY_INTERVAL
                                logger.warning(
                                    "[s7] device=%s code=HB_RECONNECT_EXHAUSTED msg=Heartbeat reconnect exhausted, "
                                    "long retry in %.0fs (attempt #%d)",
                                    device_id,
                                    backoff_delay,
                                    self._hb_reconnect_count,
                                )
                            await asyncio.sleep(backoff_delay)
                            await self._try_reconnect(device_id)
                            # S7-MED-001: 如果重连成功，重置渐进等级
                            if await self._is_connected():
                                self._long_retry_level = 0
                            else:
                                # 重连失败，增加渐进等级
                                self._long_retry_level = min(self._long_retry_level + 1, 3)
                            continue

                        backoff_delay = min(2**self._hb_reconnect_backoff, 60)
                        logger.warning(
                            "[s7] device=%s code=OFFLINE_TOO_LONG msg=Offline for %.1fs, reconnect with backoff %ds (attempt #%d)",
                            device_id,
                            offline_duration,
                            backoff_delay,
                            self._hb_reconnect_count + 1,
                        )
                        await asyncio.sleep(backoff_delay)
                        self._hb_reconnect_backoff += 1
                        await self._try_reconnect(device_id)
                        if await self._is_connected():
                            with self._stats_lock:  # FIXED-P0: _offline_since pop纳入_stats_lock，与写入路径锁保护一致
                                self._offline_since.pop(device_id, None)
                            self._hb_reconnect_backoff = 0
                            self._hb_reconnect_count = 0  # S7-002: 重置计数
                            self._hb_exhausted = False
                            self._long_retry_level = 0  # S7-MED-001: 重置渐进等级
                            self._hb_first_fail_time = 0.0  # FIXED-P1: 重置首次失败时间
                            self._hb_permanent_offline = False  # FIXED-P1: 重连成功清除永久离线标记
                            await asyncio.to_thread(
                                self._save_permanent_offline_state
                            )  # FIXED-P1: 同步清除持久化的永久离线状态
                            logger.info(
                                "[s7] device=%s code=RECONNECT_OK msg=Reconnected after watchdog trigger", device_id
                            )
                        else:
                            # S7-002: 重连失败，递增计数
                            self._hb_reconnect_count += 1
                            if self._hb_first_fail_time == 0.0:
                                self._hb_first_fail_time = time.monotonic()  # FIXED-P1: 记录首次失败时间
                            elif (time.monotonic() - self._hb_first_fail_time) > self._HB_MAX_TOTAL_RECONNECT_DURATION:
                                self._hb_permanent_offline = True  # FIXED-P1: 超过24小时标记永久离线
                                await asyncio.to_thread(
                                    self._save_permanent_offline_state
                                )  # FIXED-P1: 持久化永久离线状态，进程重启后保留
                                self._set_conn_state(ConnectionState.OFFLINE.value)
                                self._log_error(
                                    device_id,
                                    S7DriverErrors.RECONNECT_FAILED,
                                    f"Heartbeat reconnect exceeded {self._HB_MAX_TOTAL_RECONNECT_DURATION}s, marking permanently offline",
                                )
                                break
                            if self._hb_reconnect_count >= self._HB_RECONNECT_MAX_ATTEMPTS:
                                self._hb_exhausted = True
                                self._long_retry_level = 0  # S7-MED-001: 重置渐进等级
                                self._log_error(
                                    device_id,
                                    S7DriverErrors.RECONNECT_FAILED,
                                    f"Heartbeat reconnect exhausted after {self._hb_reconnect_count} attempts, entering long retry mode",
                                )
                                logger.error(
                                    "[s7] device=%s code=HB_RECONNECT_EXHAUSTED msg=Heartbeat reconnect exhausted after %d attempts, entering long retry mode",
                                    device_id,
                                    self._hb_reconnect_count,
                                )
                else:
                    with self._stats_lock:  # FIXED-P0: _offline_since pop纳入_stats_lock，与写入路径锁保护一致
                        self._offline_since.pop(device_id, None)
                    hb_start = time.monotonic()
                    try:
                        await asyncio.wait_for(
                            self._run_in_s7_thread_async(
                                self._sync_get_cpu_info
                            ),  # FIXED-P2: 心跳通过executor序列化，避免与读写操作并发访问snap7 client
                            timeout=5.0,
                        )
                    except TimeoutError:
                        logger.warning("[s7] device=%s code=HB_TIMEOUT msg=Heartbeat timeout (5s)", device_id)
                    except Exception as e:
                        logger.warning("[s7] device=%s code=HB_FAILED msg=Heartbeat failed: %s", device_id, e)
                    hb_latency_ms = (time.monotonic() - hb_start) * 1000
                    self._hb_latency_samples.append(hb_latency_ms)
            except asyncio.CancelledError:
                break
            except Exception as e:
                if not self._handle_watchdog_exception(
                    e, "s7_heartbeat"
                ):  # FIXED-P1: 使用基类分级异常处理，连续异常触发健康统计和熔断
                    break

    def _set_conn_state(self, new_state: str) -> None:
        """设置连接状态，同步更新_conn_state和基类_connection_statuses"""
        # FIXED(严重): 改用基类_conn_state_lock替代_sync_lock，避免两个不同的锁保护相关联状态导致竞态
        with self._conn_state_lock:
            self._conn_state = new_state
        # FIXED-P1: _devices为空时使用config中的device_id作为回退
        device_ids = (
            list(self._devices.keys())
            if self._devices
            else [self._config.get("device_id", self._config.get("ip", "default"))]
        )
        for device_id in device_ids:
            # FIXED-P3: 使用主事件循环引用，避免get_event_loop()回退丢失状态更新
            loop = self._main_loop
            if loop is None:
                logger.warning("[s7] set_conn_state failed: no event loop available")
                return
            if loop.is_running():
                asyncio.run_coroutine_threadsafe(self._set_connection_state(device_id, new_state), loop)

    def _log_error(self, device_id: str, error_code: str, message: str) -> None:
        i18n_msg = _t(error_code) if error_code.startswith("ERR_") else error_code
        logger.error("[s7] device=%s code=%s %s", device_id, error_code, f"i18n={i18n_msg} {message}")

    async def _try_failover(self, device_id: str) -> bool:
        if not self._redundancy:
            return False
        active_role = self._redundancy.get_active_role(device_id)
        if active_role == LinkRole.BACKUP:
            return False
        if not self._backup_ip:
            return False
        for _ in range(
            self._redundancy_config.failover_threshold if self._redundancy_config else self._PRIMARY_FAIL_THRESHOLD
        ):
            self._redundancy.record_failure(device_id)
        active_role = self._redundancy.get_active_role(device_id)
        if active_role == LinkRole.BACKUP:
            self._active_ip = self._redundancy.get_active_host(device_id)
            self._using_backup = True
            return True
        return False

    def _on_redundancy_switch(self, device_id: str, old_host: str, new_host: str) -> None:
        self._active_ip = new_host
        self._using_backup = new_host == self._backup_ip
        logger.info(
            "[s7] device=%s code=REDUNDANCY_SWITCH msg=%s -> %s (backup=%s)",
            device_id,
            old_host,
            new_host,
            self._using_backup,
        )

    async def _ensure_heartbeat_task_locked(self) -> None:
        """FIXED-P0: 锁保护的心跳Task创建，供同步方法trigger_reconnect通过loop.create_task调度"""
        async with self._hb_task_lock:
            if self._running and (self._heartbeat_task is None or self._heartbeat_task.done()):
                self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    def trigger_reconnect(self) -> None:
        """S7-MED-001: 手动触发重连

        用于外部事件（如配置变更、手动操作）触发立即重连，
        可打破长间隔模式，加速设备恢复。
        """
        device_id = self._config.get("ip", "unknown")
        # 重置心跳重连状态
        self._hb_exhausted = False
        self._hb_reconnect_count = 0
        self._hb_reconnect_backoff = 0
        self._long_retry_level = 0
        self._hb_permanent_offline = False  # FIXED-P2: 手动重连时清除永久离线标记，允许心跳循环恢复
        self._save_permanent_offline_state()  # FIXED-P1: 同步清除持久化的永久离线状态
        self._hb_first_fail_time = 0.0  # FIXED-P2: 手动重连时重置首次失败时间
        self._circuit_open.discard(device_id)  # FIXED-P0: 手动重连时清除熔断状态，允许立即重连
        self._circuit_open_since.pop(device_id, None)  # FIXED-P0: 清除熔断时间记录
        # FIXED-P0: 重启心跳Task，防止_hb_permanent_offline break后心跳监控永久丢失
        # trigger_reconnect是同步方法，通过loop.create_task调度锁保护的异步方法，消除竞态
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(self._ensure_heartbeat_task_locked())
            self._background_tasks.add(task)  # FIXED-P0: 追踪Task防止异常静默丢失
            task.add_done_callback(self._background_tasks.discard)
        except RuntimeError as e:
            # FIXED: 原问题-RuntimeError被静默吞掉，但日志仍声称已触发重连。添加warning日志并回退到_main_loop
            logger.warning(
                "[s7] device=%s code=MANUAL_RECONNECT_NO_LOOP msg=No running event loop: %s, fallback to _main_loop",
                device_id,
                e,
            )
            loop = self._main_loop
            if loop is not None and loop.is_running():
                asyncio.run_coroutine_threadsafe(self._ensure_heartbeat_task_locked(), loop)
        logger.info(
            "[s7] device=%s code=MANUAL_RECONNECT msg=Manual reconnect triggered, reset heartbeat state", device_id
        )

    async def _do_connect(self, ip: str, rack: int, slot: int) -> None:
        import snap7

        with self._sync_lock:
            old_client = self._client
        # FIXED-P0: 先断开旧client再创建新client，避免新client连接失败时C层资源泄漏
        if old_client:
            try:
                await self._safe_s7_call(old_client.disconnect, timeout=5.0)
            except Exception as e:
                logger.warning("[s7] do_connect failed: %s", e)  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
            try:
                await self._safe_s7_call(old_client.destroy, timeout=5.0)
            except Exception as e:
                logger.warning("[s7] do_connect failed: %s", e)  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
        try:
            new_client = snap7.client.Client()
        except Exception as e:
            # FIXED-P1: Client()构造失败时self._client保持原值(已destroy的旧client)，后续操作全部失败且无法恢复
            # 之前：构造异常未捕获，self._client指向已destroy旧client
            # 之后：构造失败时置None并抛出，让调用方触发重连
            with self._sync_lock:
                self._client = None
            raise ConnectionError(f"snap7 Client() construction failed: {e}") from e
        with self._sync_lock:
            self._client = new_client
        try:
            if self._is_s7_200_smart:
                local_tsap = self._config.get("local_tsap", 0x1000)
                remote_tsap = self._config.get("remote_tsap", 0x0200)
                timeout = self._config.get("connect_timeout", self._DEFAULT_CONNECT_TIMEOUT)
                new_client.set_connection_params(ip, local_tsap, remote_tsap, timeout)
            else:
                timeout = self._config.get("connect_timeout", self._DEFAULT_CONNECT_TIMEOUT)
                new_client.set_connection_params(ip, 0, 0, timeout)
            await self._s7_connect_with_timeout(new_client, ip, rack, slot)
        except Exception:
            try:
                new_client.destroy()
            except Exception as e:
                logger.warning("[s7] do_connect failed: %s", e)  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
            self._client = None  # FIXED-P0: 连接失败时置None，防止后续代码误用未连接的Client对象
            raise

    async def _try_reconnect(self, device_id: str, _failover_depth: int = 0) -> None:
        if not self._config:
            return

        if self._password and time.time() < self._auth_locked_until:
            self._log_error(device_id, S7DriverErrors.AUTH_LOCKED, "Skipping reconnect: auth locked")
            return

        lock = self._reconnect_locks.setdefault(device_id, asyncio.Lock())
        # BUG-002: 使用locked()快速检查避免等待，但接受竞态窗口（最坏情况两个协程同时进入，由_do_try_reconnect内部熔断保护）
        if lock.locked():
            logger.debug("[s7] device=%s reconnect already in progress, skipping", device_id)
            return
        async with lock:
            await self._do_try_reconnect(device_id, _failover_depth)

    async def _do_try_reconnect(self, device_id: str, _failover_depth: int) -> None:

        # FIXED-P1: 熔断检查，已熔断设备不再尝试重连
        if device_id in self._circuit_open:
            open_since = self._circuit_open_since.get(device_id, 0.0)
            if time.monotonic() - open_since < self._CIRCUIT_RECOVERY_SECONDS:
                logger.warning(
                    "[s7] device=%s code=CIRCUIT_OPEN msg=Device is circuit-broken, skipping reconnect", device_id
                )
                return
            # FIXED-P0: half-open恢复，熔断超时后允许试探连接
            logger.info(
                "[s7] device=%s code=CIRCUIT_HALF_OPEN msg=Circuit breaker recovery timeout reached, attempting probe",
                device_id,
            )

        self._reconnect_count += 1
        if self._reconnect_count > self._MAX_RECONNECT_ATTEMPTS:
            self._set_conn_state(ConnectionState.OFFLINE.value)
            self._log_error(
                device_id,
                S7DriverErrors.RECONNECT_FAILED,
                f"Max attempts reached ({self._reconnect_count}), circuit breaker activated",
            )
            await self._set_connection_state(device_id, ConnectionState.OFFLINE.value)
            if self._redundancy:
                self._redundancy.record_failure(device_id)
            self._circuit_open.add(device_id)  # FIXED-P1: 加入熔断状态，不再调度_delayed_reconnect递归
            self._circuit_open_since[device_id] = time.monotonic()  # FIXED-P0: 记录熔断开始时间，用于half-open恢复
            self._reconnect_count = 0
            self._reconnect_delay = self._RECONNECT_BASE_DELAY
            return

        delay = min(self._reconnect_delay, self._RECONNECT_MAX_DELAY)
        jitter = random.uniform(0, 1.0)
        total_delay = delay + jitter
        logger.warning(
            "[s7] device=%s code=RECONNECTING msg=Connection lost, retry in %.3fs (attempt #%d, jitter=%.3fs)",
            device_id,
            total_delay,
            self._reconnect_count,
            jitter,
        )
        await asyncio.sleep(total_delay)
        self._reconnect_delay *= 2

        ip = self._redundancy.get_active_host(device_id) if self._redundancy else self._active_ip
        try:
            rack = int(self._config.get("rack", 0))
            slot = int(self._config.get("slot", 1))
        except (ValueError, TypeError) as e:
            self._log_error(device_id, S7DriverErrors.RECONNECT_FAILED, f"Invalid rack/slot config - {e}")
            return

        try:
            import snap7
        except ImportError:
            return

        try:
            self._set_conn_state(ConnectionState.CONNECTING.value)
            await self._do_connect(ip, rack, slot)
            if (
                not self._shutdown_requested.is_set()
            ):  # FIXED-P0: 仅在未请求关闭时设置_running，防止覆盖stop()的停止指令
                self._running = True
            self._reconnect_count = 0
            self._reconnect_delay = self._RECONNECT_BASE_DELAY
            if self._redundancy:
                self._redundancy.record_success(device_id)
                self._using_backup = self._redundancy.get_active_role(device_id) == LinkRole.BACKUP
                self._active_ip = self._redundancy.get_active_host(device_id)
            if self._using_backup:
                logger.info("[s7] device=%s code=FAILOVER_ACTIVE msg=Using backup IP %s", device_id, ip)

            try:
                # FIXED-P2: 重连路径也使用专用_s7_executor
                info = await self._run_in_s7_thread_async(self._sync_get_cpu_info)
                self._plc_model = info.ModuleName if hasattr(info, "ModuleName") else "Unknown"
                logger.info("[s7] device=%s code=PLC_DETECTED msg=Model: %s", ip, self._plc_model)
            except Exception:
                self._plc_model = "Unknown"

            await self._negotiate_password(ip)

            self._set_conn_state(ConnectionState.PDU_NEGOTIATING.value)
            if self._cached_pdu_size:
                self._pdu_size = self._cached_pdu_size
                logger.info("[s7] device=%s code=PDU_CACHED msg=Using cached PDU size %d", ip, self._pdu_size)
            else:
                self._pdu_size = await self._get_pdu_size()
                self._cached_pdu_size = self._pdu_size
            self._set_conn_state(ConnectionState.CONNECTED.value)
            with self._stats_lock:  # FIXED-P0: _offline_since pop纳入_stats_lock，与写入路径锁保护一致
                self._offline_since.pop(device_id, None)
            self._circuit_open.discard(device_id)  # FIXED-P1: 连接成功时移除熔断状态
            self._circuit_open_since.pop(device_id, None)  # FIXED-P0: 连接成功时清除熔断时间记录
            logger.info(
                "[s7] device=%s code=RECONNECT_OK msg=Reconnected (rack=%d, slot=%d, pdu=%d, model=%s)",
                ip,
                rack,
                slot,
                self._pdu_size,
                self._plc_model,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # FIXED-P0: half-open试探失败后重置熔断计时，防止立即再次试探导致快速循环
            if device_id in self._circuit_open:
                self._circuit_open_since[device_id] = time.monotonic()
            if self._redundancy:
                self._redundancy.record_failure(device_id)
            if not self._using_backup:
                failed_over = await self._try_failover(device_id)
                if failed_over and _failover_depth < 1:
                    self._log_error(
                        device_id,
                        S7DriverErrors.FAILOVER_TRIGGERED,
                        f"Primary failed, retry with backup {self._backup_ip}",
                    )
                    self._reconnect_count = 0
                    self._reconnect_delay = self._RECONNECT_BASE_DELAY
                    # FIXED-P2: 直接调用_do_try_reconnect而非_try_reconnect，避免递归获取不可重入的asyncio.Lock导致故障转移重连被跳过
                    await self._do_try_reconnect(device_id, _failover_depth=_failover_depth + 1)
                    return
            self._set_conn_state(ConnectionState.OFFLINE.value)
            self._log_error(ip, S7DriverErrors.RECONNECT_FAILED, f"Reconnect failed (rack={rack}, slot={slot}) - {e}")

    async def _delayed_reconnect(self, device_id: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        if self._running:
            await self._try_reconnect(device_id)

    async def health_check(self, device_id: str) -> bool:
        if not self._running or not self._client:
            return False
        try:
            return await asyncio.wait_for(
                self._is_connected(),  # FIXED-P1: _is_connected已改为async，直接await
                timeout=3.0,
            )
        except Exception:
            return False

    async def reset_reconnect_state(self, device_id: str) -> None:
        """S7-002: 重置心跳重连状态，允许重新尝试重连"""
        # 先调用基类方法
        await super().reset_reconnect_state(device_id)
        # S7-002: 重置心跳重连相关状态
        self._hb_reconnect_count = 0
        self._hb_exhausted = False
        self._hb_reconnect_backoff = 0
        logger.info("[s7] device=%s code=RECONNECT_RESET msg=Heartbeat reconnect state has been reset", device_id)

    def get_redundancy_status(self, device_id: str) -> dict:
        if not self._redundancy:
            return {
                "active_role": "primary" if not self._using_backup else "backup",
                "active_host": self._active_ip,
                "primary_ip": self._primary_ip,
                "backup_ip": self._backup_ip,
                "using_backup": self._using_backup,
                "links": {},
            }
        return self._redundancy.get_status(device_id)

    async def probe_primary_link(self, device_id: str) -> bool:
        if not self._redundancy or not self._primary_ip:
            return False
        probe_client = None
        try:
            rack = int(self._config.get("rack", 0))
            slot = int(self._config.get("slot", 1))
            import snap7

            probe_client = snap7.client.Client()
            timeout = self._config.get("connect_timeout", self._DEFAULT_CONNECT_TIMEOUT)
            probe_client.set_connection_params(self._primary_ip, 0, 0, timeout)
            await asyncio.wait_for(
                asyncio.to_thread(probe_client.connect, self._primary_ip, rack, slot),
                timeout=timeout + 2,
            )
            try:
                await asyncio.wait_for(asyncio.to_thread(probe_client.disconnect), timeout=5.0)
            except (TimeoutError, Exception) as e:
                logger.warning("[s7] probe_primary_link failed: %s", e)  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
            self._redundancy.mark_primary_healthy(device_id)
            return True
        except Exception:
            return False
        finally:
            if probe_client:
                try:
                    probe_client.destroy()
                except Exception as e:
                    logger.warning(
                        "[s7] probe_primary_link failed: %s", e
                    )  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录

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
        try:
            await self.write_point(device_id, point, value)
            return True
        except Exception:
            return False

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
        if self._edge_rule_engine:
            return {
                "active_alarms": self._edge_rule_engine.get_active_alarms(),
                "alarm_history": self._edge_rule_engine.get_alarm_history()[-50:],
                "stats": self._edge_rule_engine.get_stats(),
            }
        return {}

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
            device_id, point_name, start_time, end_time, quality, aggregate, window_seconds, limit
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
        return await self._ts_store.query_by_quality(device_id, point_name, start_time, end_time, quality, limit)

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
        if self._ts_store:
            return self._ts_store.get_stats()
        return {}

    def get_offline_sync_stats(self) -> dict:
        if self._offline_sync:
            return self._offline_sync.get_stats()
        return {}

    async def save_config_version(
        self,
        device_id: str,
        config: dict,
        change_summary: str = "",
        operator: str = "",
    ) -> int:
        if not self._config_version_mgr:
            return 0
        return await self._config_version_mgr.save_version(device_id, config, change_summary, operator)

    async def get_config_current(self, device_id: str) -> dict | None:
        if not self._config_version_mgr:
            return None
        return await self._config_version_mgr.get_current(device_id)

    async def get_config_versions(self, device_id: str) -> list[dict]:
        if not self._config_version_mgr:
            return []
        return await self._config_version_mgr.get_versions(device_id)

    async def get_config_version_config(self, device_id: str, version: int) -> dict | None:
        if not self._config_version_mgr:
            return None
        return await self._config_version_mgr.get_version_config(device_id, version)

    async def rollback_config(self, device_id: str, target_version: int, operator: str = "") -> dict | None:
        if not self._config_version_mgr:
            return None
        return await self._config_version_mgr.rollback(device_id, target_version, operator)

    async def get_config_audit_trail(self, device_id: str, limit: int = 50) -> list[dict]:
        if not self._config_version_mgr:
            return []
        return await self._config_version_mgr.get_audit_trail(device_id, limit)

    def diff_config_versions(self, device_id: str, version_a: int, version_b: int) -> dict | None:
        if not self._config_version_mgr:
            return None
        return self._config_version_mgr.diff_versions(device_id, version_a, version_b)

    async def ota_check_update(self, package_info: dict) -> dict:
        if not self._ota_mgr:
            return {"update_available": False, "error": "ota not initialized"}
        from edgelite.drivers.s7_ota import OtaPackage

        pkg = OtaPackage(
            package_id=package_info.get("package_id", ""),
            version=package_info.get("version", ""),
            firmware_url=package_info.get("firmware_url", ""),
            firmware_hash=package_info.get("firmware_hash", ""),
            firmware_size=package_info.get("firmware_size", 0),
        )
        return await self._ota_mgr.check_update(pkg)

    async def ota_start(self, package_info: dict) -> dict:
        if not self._ota_mgr:
            return {"ok": False, "error": "ota not initialized"}
        from edgelite.drivers.s7_ota import OtaPackage

        pkg = OtaPackage(
            package_id=package_info.get("package_id", ""),
            version=package_info.get("version", ""),
            firmware_url=package_info.get("firmware_url", ""),
            firmware_hash=package_info.get("firmware_hash", ""),
            firmware_size=package_info.get("firmware_size", 0),
        )
        config_snapshot = self._config.copy() if self._config else None
        return await self._ota_mgr.start_ota(pkg, config_snapshot)

    async def ota_rollback(self) -> dict:
        if not self._ota_mgr:
            return {"ok": False, "error": "ota not initialized"}
        return await self._ota_mgr.rollback_ota()

    def ota_get_progress(self) -> dict:
        if not self._ota_mgr:
            return {"status": "unavailable"}
        return self._ota_mgr.get_progress()

    def ota_get_history(self, limit: int = 20) -> list[dict]:
        if not self._ota_mgr:
            return []
        return self._ota_mgr.get_history(limit)
