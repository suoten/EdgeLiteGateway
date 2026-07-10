"""三菱MC协议驱动 - 基于pymcprotocol库，支持iQ-R/iQ-Q系列PLC

支持：
- 三菱MC协议 (MELSEC Communication) TCP通信
- iQ-R/iQ-Q/L/FX系列PLC
- 批量读取优化 - 减少通信开销
- 位/字/浮点/32位多种数据类型
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import random
import struct
import threading
import time
from collections import OrderedDict, deque
from datetime import UTC, datetime
from typing import Any

from edgelite.api.debug import record_packet
from edgelite.api.error_codes import McDriverErrors
from edgelite.drivers.base import ConnectionState, DriverCapabilities, DriverPlugin, PointValue
from edgelite.security.rbac import Permission, has_permission  # FIXED-P1: 写入权限检查所需
from edgelite.services.i18n import t as _t

logger = logging.getLogger(__name__)


class McDriver(DriverPlugin):
    """三菱MC协议驱动

    配置参数:
        ip: PLC IP地址
        port: 端口号 (默认5007 for iQ-R, 5002 for Q series)
        plc_type: PLC型号 (默认"iQ-R")
    """

    plugin_name = "mitsubishi_mc"
    plugin_version = "1.1.0"
    supported_protocols = ("mitsubishi_mc", "mc")  # FIXED(P2): 原问题-可变默认值list; 修复-改为tuple
    _required_dependencies: tuple[str, ...] = ("pymcprotocol",)  # FIXED(P2): 原问题-可变默认值list; 修复-改为tuple
    config_schema = {
        "description": "Mitsubishi MC protocol (MELSEC Communication), supports Q/L/FX/iQ-R/iQ-F series PLC. FX5U uses SLMP on port 5001",
        "required": ["host"],
        "properties": {
            "host": {"type": "string", "description": "PLC IP address", "format": "ipv4"},
            "port": {"type": "integer", "description": "MC protocol port", "minimum": 1, "maximum": 65535},
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
                "name": "backup_host",
                "type": "string",
                "label": "Backup IP",
                "description": "Backup PLC IP for link redundancy (auto failover after 3 primary failures, target <3s)",
                "default": "",
            },
            {
                "name": "port",
                "type": "integer",
                "label": "Port",
                "description": "MC protocol port: 5007 for Q/L/iQ-R, 5001 for FX5U (SLMP)",
                "default": 5007,
            },
            {
                "name": "plc_type",
                "type": "string",
                "label": "PLC Type",
                "description": "Q series=Q, L series=L, FX5U=Fx5U, iQ-R=iQ-R",
                "default": "Q",
                "options": ["iQ-R", "Q", "L", "Fx5U", "FX5U"],
            },
            {
                "name": "frame_type",
                "type": "string",
                "label": "Frame Format",
                "description": "MC frame format: 3E (default, supports Q/L/FX5U), 4E (with network), or auto",
                "default": "3E",
                "options": ["3E", "4E", "auto"],
            },
            {
                "name": "communication_mode",
                "type": "string",
                "label": "Communication Mode",
                "description": "MC communication mode: binary (default, compact 2-byte/word frame) or ascii (text 4-byte/word frame, for legacy Q series via serial gateway/modem link)",
                "default": "binary",
                "options": ["binary", "ascii"],
            },
            {
                "name": "network_no",
                "type": "integer",
                "label": "Network No.",
                "description": "Network number (Q series default 1, FX5U/iQ-R default 0)",
                "default": 0,
            },
            {
                "name": "pc_no",
                "type": "integer",
                "label": "PC No.",
                "description": "PC number (default 255, FX5U SLMP usually 255)",
                "default": 255,
                "min": 0,
                "max": 255,
            },
            {
                "name": "device_type",
                "type": "string",
                "label": "Device Type",
                "description": "Default device type for point addresses",
                "default": "D",
                "options": ["X", "Y", "M", "D", "W", "R"],
            },
            {
                "name": "batch_size",
                "type": "integer",
                "label": "Batch Size",
                "description": "Number of points to read in parallel (default 10)",
                "default": 10,
            },
            {
                "name": "timeout",
                "type": "number",
                "label": "Timeout (s)",
                "default": 5,
                "description": "Connection timeout in seconds",
            },
            {
                "name": "slmp_direct_mode",
                "type": "boolean",
                "label": "SLMP Direct Mode (FX5U)",
                "description": "Enable SLMP direct mode for FX5U (uses station number 0xFF instead of network/pc_no). Required for FX5U Ethernet port direct connection",
                "default": False,
            },
            {
                "name": "deadband",
                "type": "number",
                "label": "Deadband",
                "description": "Absolute deadband threshold (disabled if 0)",
                "default": 0,
            },
            {
                "name": "scaling_ratio",
                "type": "number",
                "label": "Scaling Ratio",
                "description": "Linear scaling ratio (y = ratio * x + offset)",
                "default": 1.0,
            },
            {
                "name": "scaling_offset",
                "type": "number",
                "label": "Scaling Offset",
                "description": "Linear scaling offset",
                "default": 0.0,
            },
            {
                "name": "clamp_min",
                "type": "number",
                "label": "Clamp Min",
                "description": "Min valid value (below = bad quality)",
                "default": None,
            },
            {
                "name": "clamp_max",
                "type": "number",
                "label": "Clamp Max",
                "description": "Max valid value (above = bad quality)",
                "default": None,
            },
            {
                "name": "rate_of_change_threshold",
                "type": "number",
                "label": "Rate of Change Threshold",
                "description": "Max allowed value change per second (0=disabled)",
                "default": 0,
            },
            {
                "name": "frozen_value_count",
                "type": "integer",
                "label": "Frozen Value Count",
                "description": "Consecutive same-value count to detect frozen (0=disabled)",
                "default": 5,
            },
            {
                "name": "collect_interval",
                "type": "number",
                "label": "Collect Interval (s)",
                "description": "Base collection interval in seconds",
                "default": 1,
            },
            {
                "name": "byte_order",
                "type": "string",
                "label": "Byte Order",
                "description": "Word byte order for 32-bit values: big (default) or little (FX5U). FX5U auto-detects as little",
                "default": "big",
                "options": ["big", "little"],
            },
            {
                "name": "ts_storage_enabled",
                "type": "boolean",
                "label": "TS Storage Enabled",
                "description": "Enable local time-series storage (InfluxDB/SQLite) with offline queue",
                "default": False,
            },
            {
                "name": "ts_influx_url",
                "type": "string",
                "label": "InfluxDB URL",
                "description": "InfluxDB URL (empty = SQLite fallback)",
                "default": "",
            },
            {
                "name": "ts_influx_org",
                "type": "string",
                "label": "InfluxDB Org",
                "description": "InfluxDB organization",
                "default": "edgelite",
            },
            {
                "name": "ts_influx_bucket",
                "type": "string",
                "label": "InfluxDB Bucket",
                "description": "InfluxDB bucket",
                "default": "edgelite",
            },
            {
                "name": "ts_influx_token",
                "type": "string",
                "label": "InfluxDB Token",
                "description": "InfluxDB API token",
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
            "message": "Model differences (Q/L/FX/iQ-R) affect address ranges and supported commands",
        },
    )  # FIXED(P2): 原问题-可变默认值list; 修复-改为tuple

    _MAX_RECONNECT_ATTEMPTS = 3
    _RECONNECT_BASE_DELAY = 1.0
    _RECONNECT_MAX_DELAY = 60.0
    _READ_TIMEOUT = 30
    _WRITE_TIMEOUT = 10
    _FAILOVER_THRESHOLD = 3
    _FAILOVER_FAST_DELAY = 0.5
    _FAILOVER_MAX_DURATION_MS = 3000
    _WATCHDOG_INTERVAL_GOOD = 15
    _WATCHDOG_INTERVAL_BAD = 60
    _WATCHDOG_MAX_INTERVAL = 120  # MC-MED-001: 自适应探测间隔上限（秒）
    _DEGRADE_LEVELS = (1, 5, 30, 60)  # FIXED(P2): 原问题-可变默认值list; 修复-改为tuple
    _FROZEN_VALUE_WINDOW = 5
    _RATE_OF_CHANGE_WINDOW = 10

    def __init__(self):
        super().__init__()  # FIXED-P0: 基类属性未初始化
        self._running = False
        self._client = None
        self._config: dict = {}
        self._lock = asyncio.Lock()
        self._sync_lock = threading.RLock()  # FIXED-P0: threading.Lock→RLock，防止_call_sync嵌套调用时死锁
        self._reconnect_count: dict[str, int] = {}
        self._reconnect_delay: float = self._RECONNECT_BASE_DELAY
        self._devices: dict[str, dict] = {}
        self._watchdog_task: asyncio.Task | None = None
        # _health_stats inherited from base class (dict[str, DriverHealthStats])  # FIXED-P2: 删除遮蔽基类的覆盖初始化
        self._network_no: int = 0
        self._pc_no: int = 255
        self._is_fx5u: bool = False
        self._slmp_direct_mode: bool = False
        self._communication_mode: str = "binary"  # 通信模式: binary (默认) 或 ascii (旧式 Q 系列串口网关)
        self._fx5u_lock = asyncio.Lock()
        self._active_ip: str = ""
        self._backup_ip: str = ""
        self._primary_ip: str = ""
        self._using_backup: bool = False
        # CROSS-001: MC 驱动使用独立线程池
        self._executor_max_workers = 2
        self._executor_name_prefix = "mc"
        self._primary_fail_count: int = 0
        self._failover_count: int = 0
        self._failover_start_mono: float = 0.0
        self._last_failover_time: str = ""
        self._last_failover_duration_ms: float = 0.0
        self._conn_state: str = ConnectionState.DISCONNECTED.value
        self._last_latency_ms: float = 0.0
        self._last_good_values: OrderedDict[str, PointValue] = OrderedDict()  # FIXED-P1: 改为OrderedDict支持LRU淘汰
        self._watchdog_interval: float = self._WATCHDOG_INTERVAL_GOOD
        self._point_stats: OrderedDict[str, dict] = OrderedDict()  # FIXED-P1: 改为OrderedDict，支持O(1)淘汰
        # CROSS-003: 使用 LRU 缓存限制 _last_values 容量
        self._MAX_LAST_VALUES = 10000
        self._last_values: OrderedDict[str, Any] = OrderedDict()
        self._last_timestamps: OrderedDict[str, datetime] = OrderedDict()  # FIXED-P2: 改为OrderedDict支持LRU淘汰
        self._value_history: OrderedDict[str, deque] = OrderedDict()  # FIXED-P2: 改为OrderedDict支持LRU淘汰
        self._degrade_level: int = 0
        self._degrade_enter_time: float = 0.0
        self._DEGRADE_RECOVERY_MINUTES = 10
        self._effective_batch_size: int = 10
        self._write_rate_limits: OrderedDict[str, float] = OrderedDict()  # FIXED-P2: 改为OrderedDict支持LRU淘汰
        self._MAX_AUX_DICT_SIZE = 10000  # FIXED-P2: 辅助字典容量上限
        self._write_audit_log: deque = deque(maxlen=1000)
        self._WRITE_RATE_MIN_INTERVAL = 0.5
        self._WRITE_VERIFY_DELAY = 0.05
        self._rule_engine = None
        self._trigger_executor = None
        self._rule_store = None
        self._ts_storage = None
        self._offline_queue = None
        self._persist_enabled: bool = False
        self._upload_task: asyncio.Task | None = None
        self._ota_manager = None
        self._config_version_mgr = None
        self._mc_audit = None
        self._delayed_reconnect_task: asyncio.Task | None = None
        self._reconnect_locks: dict[str, asyncio.Lock] = {}
        self._reconnect_tasks: dict[str, asyncio.Task] = {}
        self._byte_order: str = "big"
        self._probe_fail_count: int = 0
        self._is_reconnecting: bool = False  # MC-002: 重连状态标志，防止 watchdog 重复触发
        self._circuit_open: set[str] = set()  # FIXED-P1: 熔断状态设备集合，阻止无限递归重连
        self._circuit_open_since: dict[str, float] = {}  # FIXED-P2: 熔断开始时间，用于half-open自动恢复
        self._CIRCUIT_RECOVERY_SECONDS = 60.0  # FIXED-P2: 熔断后60秒进入half-open状态允许试探连接
        self._current_user_role: str = "viewer"  # FIXED-P2: 默认角色改为viewer，遵循最小权限原则
        self._role_lock = asyncio.Lock()  # FIXED-P1: 角色读写锁

    async def _call_sync(self, sync_func, *args, timeout: float = 10.0, write: bool = False):
        """MC-CRIT-001: 在独立线程池中执行同步函数，使用 _sync_lock 保护并发访问
        FIXED-P0: 所有操作（读和写）均持有 _sync_lock，防止pymcprotocol内部状态错乱
        原问题：write=False时无锁并发读取，pymcprotocol的Type3E/Type4E client内部维护
                发送/接收缓冲区和序列号状态，多线程并发调用同一client的read方法会触发
                内部状态错乱（如序列号跳跃、响应错位、缓冲区覆盖），导致数据错误或异常
        修复：所有操作统一使用_locked_call持锁执行，串行化对同一client的访问。
              虽然降低了读并发度，但pymcprotocol client非线程安全，正确性优先于性能。
        """

        def _locked_call():
            with self._sync_lock:
                return sync_func(*args)

        return await self._run_in_executor(_locked_call, timeout=timeout)

    def _sync_write_point(
        self, address, value
    ):  # FIXED-P0: 移除 _sync_lock，由 _call_sync 的 _locked_call 统一加锁，避免 threading.Lock 不可重入死锁
        return self._write_point(address, value)

    def _sync_write_device(
        self, addr, values_list
    ):  # FIXED-P0: 移除 _sync_lock，由 _call_sync 的 _locked_call 统一加锁，避免 threading.Lock 不可重入死锁
        if self._client is None:
            raise RuntimeError("MC client not connected")
        return self._client.write_device(addr, values_list)

    def _apply_comm_mode(self, client, mode: str | None = None) -> None:
        """应用通信模式 (binary/ascii) 到 pymcprotocol client。

        MC协议支持两种帧格式 (MELSEC Communication Protocol Reference):
        - binary: 紧凑帧, 2字节/word, 子头部 0x5000(3E)/0x5400(4E) 二进制编码 (pymcprotocol 默认)
        - ascii:  文本帧, 4字节/word (十六进制文本), 子头部 "5000"/"5400" ASCII编码
                  用于旧式 Q 系列 PLC 通过串口网关/调制解调器链路通信的场景

        行为:
        - binary 模式: 短路优化, 不调用 setaccessopt (pymcprotocol 默认即为 binary)
        - ascii 模式: 调用 client.setaccessopt(commtype='ascii') 切换帧格式,
                      仅设置 commtype, 不修改 network/pc 等其他访问参数
        - setaccessopt 失败时优雅降级: 记录警告日志, 保持 client 在默认 binary 模式,
          不抛出异常以确保已建立的连接仍可用

        Args:
            client: pymcprotocol Type3E/Type4E 实例 (二者均通过继承提供 setaccessopt)
            mode: 通信模式 ('binary' 或 'ascii'); 为 None 时使用 self._communication_mode
        """
        if mode is None:
            mode = getattr(self, "_communication_mode", "binary")
        mode = str(mode).lower() if mode else "binary"
        if mode not in ("binary", "ascii"):
            logger.warning("[mc] 通信模式无效: %s，回退到 binary", mode)
            mode = "binary"

        # binary 模式短路优化: pymcprotocol 默认即为 binary, 无需调用 setaccessopt
        if mode == "binary":
            return

        # ascii 模式: 仅切换 commtype (同步切换 commtype 和 _wordsize), 不修改其他访问参数
        try:
            client.setaccessopt(commtype="ascii")
            logger.info("[mc] 通信模式切换为 ascii (文本帧, 4字节/word)")
        except Exception as e:
            # 优雅降级: setaccessopt 失败时保持 client 默认 binary 模式, 不中断已建立的连接
            logger.warning("[mc] 通信模式切换 ascii 失败, 保持默认 binary 模式: %s", e)

    async def start(self, config: dict) -> None:
        """启动MC驱动连接"""
        try:
            from pymcprotocol import Type3E
        except ImportError:
            raise ImportError("pymcprotocol未安装，请执行: pip install pymcprotocol>=0.3.0") from None

        self._config = config
        ip = config.get("host", "") or config.get("ip", "")
        port = int(config.get("port", 5007))
        plc_type = config.get("plc_type", "iQ-R")
        slmp_direct_mode = config.get("slmp_direct_mode", False)

        self._primary_ip = ip
        self._backup_ip = config.get("backup_host", "")
        self._active_ip = ip
        self._using_backup = False
        self._primary_fail_count = 0
        self._set_conn_state(ConnectionState.CONNECTING.value)

        if not ip:
            self._log_error("", McDriverErrors.CONFIG_INVALID, "missing host")
            raise ValueError("MC driver config missing host parameter")

        if not (1 <= port <= 65535):
            self._log_error("", McDriverErrors.CONFIG_INVALID, f"port out of range: {port}")
            raise ValueError(f"MC driver port out of range [1-65535], got: {port}")

        frame_type = config.get("frame_type", "3E")
        if frame_type == "auto":
            frame_type = "3E"

        network_no = int(
            config.get("network_no", 0 if "R" in plc_type or "Fx5U" in plc_type or "FX" in plc_type.upper() else 1)
        )
        pc_no = int(config.get("pc_no", 255))
        self._network_no = network_no
        self._pc_no = pc_no

        # FX5U 特殊处理
        self._is_fx5u = plc_type.upper() in ("FX5U", "FX5", "FX5U SLMP", "FX3U")
        self._slmp_direct_mode = slmp_direct_mode
        self._byte_order = config.get("byte_order", "little" if self._is_fx5u else "big")

        # 通信模式解析: binary (默认, 向后兼容) 或 ascii (旧式 Q 系列串口网关)
        communication_mode = str(config.get("communication_mode", "binary")).lower()
        if communication_mode not in ("binary", "ascii"):
            logger.warning("[mc] 通信模式无效: %s，回退到 binary", communication_mode)
            communication_mode = "binary"
        self._communication_mode = communication_mode

        if self._is_fx5u:
            # FX5U 默认端口 5001（SLMP）
            if port == 5007:
                port = 5001
                logger.info("[mc] device=%s code=FX5U_DETECTED msg=FX5U detected, using SLMP port 5001", ip)
            # FX5U SLMP 直接模式：使用帧格式3E + 站号0xFF
            if self._slmp_direct_mode:
                logger.info("[mc] device=%s code=SLMP_DIRECT_MODE msg=FX5U SLMP直接模式已启用，使用站号0xFF", ip)

        try:
            if frame_type == "4E":
                from pymcprotocol import Type4E

                self._client = Type4E(plctype=plc_type)
            else:
                from pymcprotocol import Type3E

                self._client = Type3E(plctype=plc_type)
            await self._call_sync(self._client.connect, ip, port, write=True)  # FIXED-P1: 连接操作修改client状态，持锁
            # 连接成功后应用通信模式 (binary/ascii); ascii 切换失败时优雅降级为 binary
            self._apply_comm_mode(self._client)
            self._running = True
            self._circuit_open.discard(ip)  # FIXED-P1: 首次连接成功时移除熔断状态
            self._set_conn_state(ConnectionState.CONNECTED.value)
            await self._start_watchdog()
            self._init_edge_rules()
            await self._init_ts_storage(config)  # FIXED-P0: _init_ts_storage改为async以调用connect()
            self._init_ota(config)
            self._init_config_version()
            self._init_audit()
            logger.info(
                "MC驱动连接成功: %s:%d (%s, frame=%s, fx5u=%s, slmp_direct=%s)",
                ip,
                port,
                plc_type,
                frame_type,
                self._is_fx5u,
                self._slmp_direct_mode,
            )
        except Exception as e:
            self._log_error(ip, McDriverErrors.CONN_FAILED, str(e))
            try:
                await self.stop()
            except Exception as e:
                logger.warning("[mc] operation failed: %s", e)  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
            raise

    async def stop(self) -> None:
        await self._stop_watchdog()
        if (
            self._delayed_reconnect_task and not self._delayed_reconnect_task.done()
        ):  # FIXED-P1: 移除hasattr检查，_delayed_reconnect_task已在__init__中初始化
            self._delayed_reconnect_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._delayed_reconnect_task
        for _device_id, task in list(
            self._reconnect_tasks.items()
        ):  # FIXED(P3): 原问题-B007循环变量device_id未使用; 修复-改为_device_id
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._reconnect_tasks.clear()
        self._reconnect_locks.clear()
        try:
            if self._client:
                try:
                    await self._call_sync(
                        self._client.close, timeout=5.0, write=True
                    )  # FIXED-P1: 关闭操作修改client状态，持锁
                except TimeoutError:
                    logger.warning("[mc] MC client close timeout (5s)")
                except Exception as e:
                    logger.warning("[mc] MC驱动断开异常: %s", e)
        finally:
            self._running = False
            self._client = None
            self._delayed_reconnect_task = None
            # FIXED-P0: 原问题-stop() 方法完全遗漏 _trigger_executor 和 _rule_store 的清理
            # 第1629行创建的 EdgeTriggerExecutor 实例在驱动停止时被遗忘，
            # 其 SQLite 连接和后台任务（_upload_task/_pulse_tasks）泄漏
            if self._trigger_executor:
                try:
                    await self._trigger_executor.stop()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning("[mc] code=STOP_TRIGGER_FAILED trigger_executor.stop failed: %s", e)
                self._trigger_executor = None
            if self._rule_store:
                self._rule_store.stop()
                self._rule_store = None
            # FIXED: 原问题-stop() 完全遗漏 _ts_storage 清理，导致每次 stop→start 重连
            # 泄漏一个完整 InfluxDB 客户端 + SQLite emergency 连接 + sync 后台任务 [2026-06-29]
            if self._ts_storage is not None:
                try:
                    close_fn = getattr(self._ts_storage, "close", None)
                    if close_fn is not None:
                        result = close_fn()
                        if asyncio.iscoroutine(result):
                            await asyncio.wait_for(result, timeout=5.0)
                except TimeoutError:
                    logger.warning("[mc] _ts_storage close timeout (5s)")
                except Exception as e:
                    logger.warning("[mc] _ts_storage close failed: %s", e)
                self._ts_storage = None
            if self._offline_queue is not None:
                try:
                    close_fn = getattr(self._offline_queue, "close", None)
                    if close_fn is not None:
                        close_fn()
                except Exception as e:
                    logger.warning("[mc] _offline_queue close failed: %s", e)
                self._offline_queue = None
            self._set_conn_state(ConnectionState.DISCONNECTED.value)
            # MC-002: 清理重连状态
            self._is_reconnecting = False
            # CROSS-004: 取消所有后台任务（先取消，再清理状态，防止任务访问已清理状态）
            await self._cancel_background_tasks()
            # FIXED-P2: 清理状态残留，避免 stop→start 后继承过期状态
            self._devices.clear()
            with self._stats_lock:  # FIXED-P1: 与基类写入路径锁保护一致
                self._health_stats.clear()
                self._offline_since.clear()  # FIXED-P0: 移入_stats_lock块内，与基类读写路径锁保护一致
            self._point_stats.clear()
            self._last_values.clear()
            self._circuit_open.clear()  # FIXED-P1: 清理熔断状态
            self._last_timestamps.clear()
            self._value_history.clear()
            self._degrade_level = 0
            self._effective_batch_size = 10
            self._write_rate_limits.clear()
            self._write_audit_log.clear()
            self._last_good_values.clear()
            self._primary_fail_count = 0
            self._reconnect_count.clear()
            # CROSS-001: 关闭独立线程池
            await self._shutdown_executor()
            logger.info("MC驱动已停止")

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        """读取三菱PLC测点值

        测点地址格式: "D100" (数据寄存器), "M0" (内部继电器),
                      "X0" (输入), "Y0" (输出), "W0" (链接寄存器)
        字/位操作通过地址后缀区分:
            "D100" - 读取16位字
            "D100.0" - 读取位
            "D100.U" - 读取无符号16位
            "D100.L" - 读取32位长字
            "D100.F" - 读取浮点数

        支持批量读取优化，自动分组并发读取
        """
        # Check connection quality before reading
        quality = self.get_connection_quality(device_id)
        if quality < 60:
            logger.warning("[mc] device=%s code=QUALITY_LOW quality=%.1f, attempting reconnect", device_id, quality)
            await self._try_reconnect(device_id)
            new_quality = self.get_connection_quality(device_id)
            if new_quality > 80:
                logger.info("[mc] device=%s code=QUALITY_RECOVERED quality=%.1f->%.1f", device_id, quality, new_quality)
            else:
                await self._set_connection_state(
                    device_id, ConnectionState.DEGRADED.value, "Connection quality below threshold after reconnect"
                )
                logger.warning(
                    "[mc] device=%s code=QUALITY_DEGRADED quality=%.1f after reconnect", device_id, new_quality
                )

        if not self._running or not self._client:
            self._log_error(device_id, McDriverErrors.CONN_LOST, "not running or no client")
            await self._try_reconnect(device_id)
            now = datetime.now(UTC)
            if self._using_backup and self._last_good_values:
                result = {}
                for p in points:
                    cached = self._last_good_values.get(p)
                    if cached is not None:
                        result[p] = PointValue(value=cached.value, quality="uncertain", timestamp=now, source="cache")
                    else:
                        result[p] = PointValue(value=None, quality="bad", timestamp=now)
                return result
            return {p: PointValue(value=None, quality="bad", timestamp=now) for p in points}

        batch_size = self._effective_batch_size or self._config.get("batch_size", 10)
        result = {}

        for i in range(0, len(points), batch_size):
            batch = points[i : i + batch_size]
            try:
                batch_result = await asyncio.wait_for(
                    self._read_points_batch(batch),
                    timeout=self._READ_TIMEOUT,
                )
                bad_count = sum(1 for v in batch_result.values() if isinstance(v, PointValue) and v.quality == "bad")
                if bad_count == len(batch) and batch_size > 1:
                    self._log_error(
                        device_id, McDriverErrors.BATCH_RETRY, "all bad, retry with batch_size=1"
                    )  # FIXED-P3: 原问题-f-string无占位符(ruff F541); 修复-移除多余f前缀
                    self._effective_batch_size = max(1, batch_size // 2)
                    retry_result = {}
                    # FIXED-P1: 逐点重试增加总超时预算(60秒)，防止设备离线时N个点阻塞N*30秒
                    _retry_deadline = time.monotonic() + 60.0
                    for single_p in batch:
                        if time.monotonic() > _retry_deadline:
                            now = datetime.now(UTC)
                            retry_result[single_p] = PointValue(value=None, quality="bad", timestamp=now)
                            continue
                        try:
                            _remaining = max(1.0, _retry_deadline - time.monotonic())
                            single_r = await asyncio.wait_for(
                                self._read_points_batch([single_p]), timeout=min(self._READ_TIMEOUT, _remaining)
                            )
                            retry_result.update(single_r)
                        except Exception:
                            now = datetime.now(UTC)
                            logger.warning("[mc] retry read failed for %s", single_p, exc_info=True)
                            retry_result[single_p] = PointValue(value=None, quality="bad", timestamp=now)
                    batch_result = retry_result
                else:
                    if bad_count > len(batch) * 0.5 and batch_size > 1:
                        self._effective_batch_size = max(1, batch_size // 2)
                    elif bad_count == 0 and self._effective_batch_size < self._config.get("batch_size", 10):
                        self._effective_batch_size = min(batch_size * 2, self._config.get("batch_size", 10))
            except TimeoutError:  # FIXED-P1: 兼容Python<3.11
                self._record_read_failure(device_id)
                self._log_error(device_id, McDriverErrors.READ_TIMEOUT, f"batch read timeout ({self._READ_TIMEOUT}s)")
                now = datetime.now(UTC)
                batch_result = {p: PointValue(value=None, quality="bad", timestamp=now) for p in batch}
                if batch_size > 1:
                    self._effective_batch_size = max(1, batch_size // 2)
            result.update(batch_result)

        self._update_degrade_level(device_id)
        if result:
            good_count = sum(1 for v in result.values() if isinstance(v, PointValue) and v.quality == "good")
            if good_count:
                await self._record_read_success(
                    device_id
                )  # #[AUDIT-FIX] _record_read_success is async, must await (was no-op coroutine)
            else:
                self._record_read_failure(device_id)
        else:
            self._record_read_failure(device_id)
        await self._evaluate_rules(device_id, result)
        await self._persist_points(device_id, result)
        return result

    async def _read_points_batch(self, points: list[str]) -> dict[str, Any]:
        tasks = [self._read_point_async(p) for p in points]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        now = datetime.now(UTC)
        roc_threshold = self._config.get("rate_of_change_threshold", 0) or None
        frozen_count = self._config.get("frozen_value_count", self._FROZEN_VALUE_WINDOW)
        out: dict[str, Any] = {}
        for p, r in zip(
            points, results, strict=True
        ):  # FIXED(P2): 原问题-B905 zip无strict; 修复-添加strict=True(points与results等长)
            pcfg = self._get_point_config(p)
            p_deadband = pcfg.get("deadband", self._config.get("deadband", 0)) or None
            p_scaling = None
            sr = pcfg.get("scaling_ratio", self._config.get("scaling_ratio", 1.0))
            so = pcfg.get("scaling_offset", self._config.get("scaling_offset", 0.0))
            if sr != 1.0 or so != 0.0:
                p_scaling = {"ratio": sr, "offset": so}
            p_clamp = None
            cmin = pcfg.get("clamp_min", self._config.get("clamp_min"))
            cmax = pcfg.get("clamp_max", self._config.get("clamp_max"))
            if cmin is not None or cmax is not None:
                p_clamp = {"min": cmin, "max": cmax}
            if isinstance(r, Exception) or r is None:
                out[p] = PointValue(value=None, quality="bad", timestamp=now)
                self._update_point_stats(p, False)
                continue
            if isinstance(r, PointValue):
                out[p] = r
                continue
            val = r
            if self._check_nan_inf(p, val):
                out[p] = PointValue(value=None, quality="bad", timestamp=now)
                self._update_point_stats(p, False)
                continue
            val = self._apply_scaling(val, p_scaling)
            clamped_val, ok = self._apply_clamp(val, p_clamp)
            if not ok:
                out[p] = PointValue(value=None, quality="bad", timestamp=now)
                self._update_point_stats(p, False)
                continue
            quality = "good"
            if roc_threshold and isinstance(clamped_val, (int, float)):
                roc_q = self._check_rate_of_change(p, clamped_val, now, roc_threshold)
                if roc_q:
                    quality = "uncertain"
            if quality == "good" and isinstance(clamped_val, (int, float)):
                frozen_q = self._check_frozen_value(p, clamped_val, frozen_count)
                if frozen_q:
                    quality = "uncertain"
            last_val = self._last_values.get(p)
            if p in self._last_values:
                self._last_values.move_to_end(p)  # FIXED-P1: 读取时也更新LRU顺序，避免FIFO淘汰重要测点
            if (
                p_deadband
                and last_val is not None
                and isinstance(clamped_val, (int, float))
                and isinstance(last_val, (int, float))
            ):
                clamped_val = self._apply_deadband(clamped_val, last_val, p_deadband)
            # CROSS-003: LRU 缓存更新
            if p in self._last_values:
                self._last_values.move_to_end(p)
            self._last_values[p] = clamped_val
            # 超过容量时淘汰最旧条目
            while len(self._last_values) > self._MAX_LAST_VALUES:
                self._last_values.pop(next(iter(self._last_values)))
            self._last_timestamps[p] = now
            # FIXED-P2: 辅助字典容量淘汰
            while len(self._last_timestamps) > self._MAX_AUX_DICT_SIZE:
                self._last_timestamps.pop(next(iter(self._last_timestamps)))
            out[p] = PointValue(value=clamped_val, quality=quality, timestamp=now)
            if quality == "good":
                self._last_good_values[p] = out[p]
                # FIXED-P1: _last_good_values容量淘汰
                while len(self._last_good_values) > self._MAX_AUX_DICT_SIZE:
                    self._last_good_values.pop(next(iter(self._last_good_values)))
            self._update_point_stats(p, True)
        return out

    async def _read_point_async(self, address: str) -> Any:
        try:
            record_packet("tx", "mc", "", f"MC Read: {address}")
            if self._is_fx5u:
                async with self._fx5u_lock:
                    result = await self._call_sync(
                        self._read_point, address
                    )  # FIXED-P0: _sync_read_point不存在，应为_read_point
            else:
                result = await self._call_sync(
                    self._read_point, address
                )  # FIXED-P0: _sync_read_point不存在，应为_read_point
            record_packet("rx", "mc", "", f"MC Read: {address} = {result}")
            return result
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._log_error("", McDriverErrors.READ_FAILED, f"{address}: {e}")
            logger.warning("[mc] read failed: %s - %s", address, e, exc_info=True)
            return PointValue(value=None, quality="bad", timestamp=datetime.now(UTC))

    def _check_connection(self) -> bool:  # FIXED-P0: 实现缺失的连接检查方法，watchdog依赖此方法
        """同步检查PLC连接是否存活，通过尝试读取一个已知寄存器判断"""
        try:
            if self._client is None:
                return False
            self._client.read_word_device("D0", 1)
            return True
        except Exception as e:
            logger.warning("[mc] ping failed: %s", e)  # FIXED-P1: 原问题-ping异常返回False无日志
            return False

    def _read_point(self, address: str) -> Any:
        r"""同步读取单个测点

        FX5U SLMP 特殊支持:
        - 直接模式地址: U0\G100 (模块0, 地址100)
        - 扩展链路地址: J0\X100 (链接网络0)
        - 标准软元件: D, M, X, Y, W, R, T, C
        """
        # FIXED-P0: 快照client引用，防止重连期间client被替换导致使用部分初始化的对象
        client = self._client
        if client is None:
            raise ConnectionError("MC client is not connected")
        # 解析地址
        addr, suffix = self._parse_address(address)

        # FX5U SLMP 直接模式地址解析: U{n}\G{addr}
        if self._is_fx5u and ("\\G" in addr or "\\g" in addr):
            return self._read_fx5u_slmp_direct(addr, suffix, client=client)
        # FX5U 链接网络地址: J{n}\{type}{addr}
        elif self._is_fx5u and ("\\J" in addr or "\\j" in addr or addr.startswith("J")):
            return self._read_fx5u_network(addr, suffix, client=client)
        elif suffix == "bit":
            # 位读取
            result = client.read_bit_device(addr, 1)
            if not result:  # FIXED-P2: 检查返回值是否为空，防止IndexError
                raise ValueError(f"Empty response from read_bit_device({addr}, 1)")
            return result[0]
        elif suffix == "word":
            # 字读取(16位有符号)
            values = client.read_device(addr, 1)
            if not values:  # FIXED-P2: 检查返回值是否为空，防止IndexError
                raise ValueError(f"Empty response from read_device({addr}, 1)")
            return values[0]
        elif suffix == "uword":
            # 无符号字读取
            values = client.read_device(addr, 1)
            if not values:  # FIXED-P2: 检查返回值是否为空，防止IndexError
                raise ValueError(f"Empty response from read_device({addr}, 1)")
            return values[0] & 0xFFFF
        elif suffix == "long":
            # 双字读取(32位)
            values = client.read_device(addr, 2)
            # FIXED-P1: 数据不足时抛出异常而非返回PointValue，保持返回类型一致
            if not isinstance(values, (list, tuple)) or len(values) < 2:
                raise ValueError(f"Insufficient data from read_device({addr}, 2): {values}")
            # FIXED-P1: 校验列表元素类型，防止None/非int元素导致位运算TypeError
            if any(not isinstance(v, int) for v in values[:2]):
                raise ValueError(f"Non-integer value in read_device({addr}, 2): {values}")
            if self._byte_order == "little":
                return ((values[1] & 0xFFFF) << 16) | (values[0] & 0xFFFF)  # FIXED-P2: 移位前掩码，防止负数符号扩展
            else:
                return ((values[0] & 0xFFFF) << 16) | (values[1] & 0xFFFF)  # FIXED-P2: 移位前掩码，防止负数符号扩展
        elif suffix == "float":
            values = client.read_device(addr, 2)
            # FIXED-P1: 数据不足时抛出异常而非返回PointValue，保持返回类型一致
            if not isinstance(values, (list, tuple)) or len(values) < 2:
                raise ValueError(f"Insufficient data from read_device({addr}, 2): {values}")
            if any(not isinstance(v, int) for v in values[:2]):
                raise ValueError(f"Non-integer value in read_device({addr}, 2): {values}")
            if self._byte_order == "little":
                raw = struct.pack("<HH", values[0] & 0xFFFF, values[1] & 0xFFFF)
                return struct.unpack("<f", raw)[0]
            else:
                raw = struct.pack(">HH", values[0] & 0xFFFF, values[1] & 0xFFFF)
                return struct.unpack(">f", raw)[0]
        else:
            values = client.read_device(addr, 1)
            if not values:  # FIXED-P1: 空值检查，防止IndexError
                raise ValueError(f"Empty response from read_device({addr}, 1)")
            return values[0]

    def _read_fx5u_slmp_direct(self, addr: str, suffix: str, client: Any = None) -> Any:
        r"""FX5U SLMP 直接模式读取 (U{n}\G{addr})

        FX5U SLMP 直接模式使用 U{n}\G{addr} 格式直接访问 CPU 模块软元件。
        - U0\G100: CPU 模块, 地址100
        - U1\G100: 智能模块1, 地址100
        """
        # FIXED-P1: 使用client快照替代self._client直接访问，防止重连期间使用被替换的旧client
        c = client if client is not None else self._client
        # 解析 U{n}\G{addr} 格式
        try:
            parts = addr.replace("\\", "/").split("/")
            if len(parts) >= 2:
                # U0/G100 格式
                module_str = parts[0].upper().replace("U", "")
                addr_str = parts[1].upper().replace("G", "")
                module_no = int(module_str) if module_str else 0
                element_addr = int(addr_str)
            else:
                # 回退：尝试直接解析
                return c.read_device(addr, 1)[0]

            # FX5U SLMP 直接模式
            # 通过 set_accessopt 设置直接模式
            if hasattr(c, "set_accessopt"):
                # FIXED-P0: 保存完整的accessopt参数，而非依赖私有属性_accessopt
                # 之前：getattr(c, '_accessopt', None)获取私有属性，空字典时set_accessopt(**{})重置为默认值
                # 之后：保存实际传入的参数，恢复时使用保存的参数确保一致性
                old_accessopt = getattr(c, "_accessopt", None)
                had_accessopt = hasattr(c, "_accessopt")
                # 保存旧参数的深拷贝，防止引用被修改
                saved_accessopt = dict(old_accessopt) if old_accessopt else None
                c.set_accessopt(module=module_no, device_type="G")
                try:
                    # 构建访问路径: G{addr}
                    access_addr = f"G{element_addr}"
                    if suffix == "bit":
                        return c.read_bit_device(access_addr, 1)[0]
                    else:
                        values = c.read_device(access_addr, 1)
                        return values[0] if values else None
                finally:
                    try:
                        if hasattr(c, "set_accessopt") and had_accessopt and saved_accessopt:
                            c.set_accessopt(**saved_accessopt)
                        elif hasattr(c, "set_accessopt") and had_accessopt and not saved_accessopt:
                            # 旧accessopt为空字典，说明之前未设置accessopt，重置为默认
                            if hasattr(c, "reset_accessopt"):
                                c.reset_accessopt()
                    except Exception as e:
                        logger.warning("[mc] FX5U accessopt restore failed: %s", e)
            else:
                # 回退：直接使用地址
                values = c.read_device(addr, 1)
                if not values:  # FIXED-P1: 空值检查
                    raise ValueError(f"Empty response from read_device({addr}, 1)")
                return values[0]
        except (
            ConnectionError,
            OSError,
            TimeoutError,
        ):  # FIXED-P3: 原问题-as e绑定异常对象但e未使用(ruff F841); 修复-移除as e绑定  # FIXED-P2: 连接类异常直接向上抛出，不回退到标准读取（回退同样会失败且掩盖连接错误）
            raise
        except Exception as e:
            logger.warning("[mc] FX5U SLMP直接模式读取失败 %s: %s", addr, e, exc_info=True)
            # 回退：使用标准读取
            values = c.read_device(addr.replace("\\", ""), 1)
            if not values:  # FIXED-P1: 空值检查
                raise ValueError(
                    f"Empty response from read_device({addr}, 1)"
                ) from e  # FIXED(P3): 原问题-B904 异常链丢失; 修复-添加 from e
            return values[0]

    def _read_fx5u_network(self, addr: str, suffix: str, client: Any = None) -> Any:
        r"""FX5U 扩展网络读取 (J{n}\{type}{addr})

        FX5U 支持访问其他网络上的 PLC 数据。
        """
        # FIXED-P1: 使用client快照替代self._client直接访问，防止重连期间使用被替换的旧client
        c = client if client is not None else self._client
        try:
            # 解析 J{n}\{type}{addr} 格式
            parts = addr.replace("\\", "/").split("/")
            if len(parts) >= 2:
                network_str = parts[0].upper().replace("J", "")
                device_str = parts[1]
                network_no = int(network_str) if network_str else 0
                # FIXED-P3: 原问题-dev_type/dev_addr赋值后未使用(ruff F841); 修复-移除无用的设备类型/地址解析，后续直接使用device_str
            else:
                values = c.read_device(addr, 1)
                if not values:  # FIXED-P1: 空值检查
                    raise ValueError(f"Empty response from read_device({addr}, 1)")
                return values[0]

            # 通过 pymcprotocol 的网络访问功能
            if hasattr(c, "set_accessopt"):
                old_opt = getattr(c, "_accessopt", None)
                had_opt = hasattr(c, "_accessopt")  # FIXED-P1: 记录原始状态
                c.set_accessopt(network_no=network_no)
                try:
                    if suffix == "bit":
                        return c.read_bit_device(device_str, 1)[0]
                    else:
                        values = c.read_device(device_str, 1)
                        return values[0] if values else None
                finally:
                    try:
                        if had_opt and hasattr(c, "set_accessopt"):  # FIXED-P1: 用had_opt替代old_opt真值判断
                            c.set_accessopt(**old_opt)
                    except Exception as e:
                        logger.warning("[mc] operation failed: %s", e)  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
            else:
                values = c.read_device(addr.replace("\\", ""), 1)
                if not values:  # FIXED-P1: 空值检查
                    raise ValueError(f"Empty response from read_device({addr}, 1)")
                return values[0]
        except (
            ConnectionError,
            OSError,
            TimeoutError,
        ):  # FIXED-P3: 原问题-as e绑定异常对象但e未使用(ruff F841); 修复-移除as e绑定  # FIXED-P2: 连接类异常直接向上抛出，不回退
            raise
        except Exception as e:
            logger.warning("[mc] FX5U网络读取失败 %s: %s", addr, e, exc_info=True)
            values = c.read_device(addr.replace("\\", ""), 1)
            if not values:  # FIXED-P1: 空值检查
                raise ValueError(
                    f"Empty response from read_device({addr}, 1)"
                ) from e  # FIXED(P3): 原问题-B904 异常链丢失; 修复-添加 from e
            return values[0]

    def _parse_address(self, address: str) -> tuple[str, str]:
        r"""解析MC地址，返回(设备地址, 类型后缀)

        FX5U SLMP 特殊支持:
        - FX5U 地址格式: D1000 (4位/6位数字), M1000, X100, Y100
        - FX5U 直接模式: 使用 U{n}\G{addr} 访问模块软元件
        - FX5U 字软元件: D, W, R, T(Current), C(Current)
        - FX5U 位软元件: M, X, Y, SM, SD, L, F, V, B, SB, DX, DY, S
        """
        parts = address.split(".")
        addr = parts[0]

        if len(parts) > 1:
            bit_suffix = parts[1]
            if bit_suffix.isdigit():
                # FX5U 支持 D1000.0 格式（位访问）
                return f"{addr}.{bit_suffix}", "bit"
            # FIXED-P0: 添加B/byte/int8后缀映射，使_write_point的字节写入分支可达
            suffix_map = {
                "U": "uword",
                "L": "long",
                "F": "float",
                "B": "B",
                "BYTE": "byte",
                "INT8": "int8",
            }
            return addr, suffix_map.get(bit_suffix.upper(), "word")

        # 根据设备类型判断默认读取方式
        device_type = addr[0].upper() if addr else ""
        # FX5U SLMP 位软元件列表
        bit_devices = (
            "M",
            "X",
            "Y",
            "SM",
            "SD",
            "L",
            "F",
            "V",
            "B",
            "SB",
            "DX",
            "DY",
            "S",
            "J",
            "K",
            "U",
        )  # FIXED-P1: 移除Z（变址寄存器是字设备）
        # FX5U 字软元件列表 (FIXED-P3: 原问题-word_devices赋值后未使用(ruff F841); 修复-移除无用变量，字设备判断走默认return addr, "word")
        if device_type in bit_devices:
            return addr, "bit"
        # FX5U 计时器/计数器当前值: T, C (字访问)
        if device_type in ("T", "C") and not addr.startswith("TN") and not addr.startswith("CN"):
            return addr, "word"
        return addr, "word"

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        if not await self.check_permission(Permission.DEVICE_WRITE_POINT):  # FIXED-P1: 写入操作添加权限检查
            self._log_error(device_id, "WRITE_DENIED", f"role={self._current_user_role} lacks device:write")
            return False
        if not self._running or not self._client:
            await self._try_reconnect(device_id)
            return False
        addr, suffix = self._parse_address(point)
        if not self._validate_write_value(value, suffix):
            self._log_error(device_id, McDriverErrors.WRITE_VALUE_INVALID, f"{point}={value} suffix={suffix}")
            self._record_write_audit(device_id, point, addr, suffix, None, value, "rejected_invalid")
            return False
        now_mono = time.monotonic()
        last_write = self._write_rate_limits.get(point, 0.0)
        if now_mono - last_write < self._WRITE_RATE_MIN_INTERVAL:
            self._log_error(
                device_id, McDriverErrors.WRITE_RATE_LIMITED, f"{point} interval={now_mono - last_write:.3f}s"
            )
            return False
        old_value = None
        try:
            old_pv = await self._read_points_batch([point])
            old_pv_val = old_pv.get(point)
            if isinstance(old_pv_val, PointValue) and old_pv_val.quality == "good":
                old_value = old_pv_val.value
        except Exception as e:
            logger.warning("[mc] write_point failed: %s", e)  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
        # FIXED-P1: write_point添加3次重试与50-100ms退避
        # 原问题：write_point单次写入失败即返回False，网络抖动或PLC瞬时繁忙时写入成功率低
        # 修复：添加最多3次重试，重试间添加50-100ms随机退避，避免密集重试冲击PLC
        _WRITE_MAX_RETRIES = 3
        _write_success = False
        _write_error: Exception | None = None
        _write_timed_out = False
        for _attempt in range(_WRITE_MAX_RETRIES):
            try:
                record_packet("tx", "mc", device_id, f"MC Write: {point} = {value}")
                async with self._lock:
                    await self._call_sync(
                        self._sync_write_point, point, value, timeout=self._WRITE_TIMEOUT, write=True
                    )  # FIXED-P1: 写操作持锁
                record_packet("rx", "mc", device_id, f"MC Write: {point} = {value} OK")
                self._write_rate_limits[point] = time.monotonic()
                # FIXED-P2: 辅助字典容量淘汰
                while len(self._write_rate_limits) > self._MAX_AUX_DICT_SIZE:
                    self._write_rate_limits.pop(next(iter(self._write_rate_limits)))
                _write_success = True
                break
            except TimeoutError:  # FIXED-P1: 兼容Python<3.11
                _write_timed_out = True
                _write_error = None  # TimeoutError 不存储详情
                if _attempt < _WRITE_MAX_RETRIES - 1:
                    await asyncio.sleep(random.uniform(0.05, 0.1))  # FIXED-P1: 50-100ms退避
            except asyncio.CancelledError:
                raise
            except Exception as e:
                _write_error = e
                if _attempt < _WRITE_MAX_RETRIES - 1:
                    await asyncio.sleep(random.uniform(0.05, 0.1))  # FIXED-P1: 50-100ms退避
        if not _write_success:
            if _write_timed_out:
                self._record_write_audit(device_id, point, addr, suffix, old_value, value, "timeout")
            else:
                self._record_write_failure(device_id)
                self._log_error(device_id, McDriverErrors.WRITE_FAILED, f"{point}: {_write_error}")
                self._record_write_audit(device_id, point, addr, suffix, old_value, value, "failed")
            return False
        verify_ok = True
        try:
            await asyncio.sleep(self._WRITE_VERIFY_DELAY)
            verify_pv = await self._read_points_batch([point])
            verify_val = verify_pv.get(point)
            read_back = (
                verify_val.value if isinstance(verify_val, PointValue) and verify_val.quality == "good" else None
            )
            expected = int(value) if suffix != "bit" else int(bool(value))
            if read_back is not None and isinstance(read_back, (int, float)) and isinstance(expected, (int, float)):
                if abs(read_back - expected) > 1:
                    self._log_error(
                        device_id, McDriverErrors.WRITE_VERIFY_FAILED, f"{point} wrote={expected} read={read_back}"
                    )
                    verify_ok = False
        except Exception:
            verify_ok = False  # FIXED-P2: 验证异常时verify_ok应为False，而非默认True
        result_str = "ok" if verify_ok else "verify_mismatch"
        if verify_ok:
            self._record_write_success(device_id)
        else:
            self._record_write_failure(device_id)
        self._record_write_audit(device_id, point, addr, suffix, old_value, value, result_str)
        return verify_ok

    def _write_point(self, address: str, value: Any) -> None:
        """同步写入单个测点"""
        # FIXED-P0: 快照client引用，防止重连期间使用被替换的旧client
        client = self._client
        if client is None:
            raise ConnectionError("MC client is not connected")
        addr, suffix = self._parse_address(address)

        if suffix == "bit":
            client.write_bit_device(addr, [int(bool(value))])
        elif suffix in ("B", "byte", "int8"):
            # FIXED-P0: 字节类型写入，仅写入低8位到指定地址，避免覆盖相邻寄存器
            client.write_device(addr, [int(value) & 0xFF])
        elif suffix == "long" or (hasattr(self, "_point_types") and self._point_types.get(address) == "long"):
            word_val = int(value) & 0xFFFFFFFF
            hi = (word_val >> 16) & 0xFFFF
            lo = word_val & 0xFFFF
            # FIXED-P0: long写入根据byte_order调整字序，与读取逻辑一致
            if self._byte_order == "little":
                client.write_device(addr, [lo, hi])
            else:
                client.write_device(addr, [hi, lo])
        elif suffix == "float" or (hasattr(self, "_point_types") and self._point_types.get(address) == "float"):
            if self._byte_order == "little":
                raw = struct.pack("<f", float(value))
                lo = struct.unpack("<H", raw[0:2])[0]
                hi = struct.unpack("<H", raw[2:4])[0]
                client.write_device(addr, [lo, hi])
            else:
                raw = struct.pack(">f", float(value))
                hi = struct.unpack(">H", raw[0:2])[0]
                lo = struct.unpack(">H", raw[2:4])[0]
                client.write_device(addr, [hi, lo])
        else:
            client.write_device(addr, [int(value) & 0xFFFF])

    async def write_points_batch(self, device_id: str, points: dict[str, Any]) -> dict[str, bool]:
        # FIXED-P1: 方法名和参数类型与基类 write_points_batch 对齐
        if not await self.check_permission(Permission.DEVICE_WRITE_POINT):  # FIXED-P1: 批量写入同样需要权限检查
            self._log_error(device_id, "WRITE_DENIED", f"role={self._current_user_role} lacks device:write")
            return {point: False for point in points}
        if not self._running or not self._client:
            await self._try_reconnect(device_id)
            return {point: False for point in points}
        # 将 dict 转换为 list[tuple] 以适配内部合并逻辑
        writes = list(points.items())
        merged = self._merge_contiguous_writes(writes)
        results_map: dict[str, bool] = {}

        async def _do_single(point, value):
            return await self.write_point(device_id, point, value)

        async def _do_merged(group_key, addr, values_list):
            # FIXED-P0: 合并写入路径添加值验证，与单点写入路径一致，防止超范围值写入PLC
            for v in values_list:
                if not self._validate_write_value(v, "word"):
                    self._record_write_failure(device_id)
                    self._log_error(device_id, McDriverErrors.WRITE_FAILED, f"batch {addr}: value {v} out of range")
                    return False
            try:
                record_packet("tx", "mc", device_id, f"MC Batch Write: {addr} x{len(values_list)}")
                async with self._lock:
                    await self._call_sync(
                        self._sync_write_device, addr, values_list, timeout=self._WRITE_TIMEOUT, write=True
                    )  # FIXED-P1: 写操作持锁
                record_packet("rx", "mc", device_id, f"MC Batch Write: {addr} x{len(values_list)} OK")
                self._record_write_success(device_id)
                return True
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._record_write_failure(device_id)
                self._log_error(device_id, McDriverErrors.WRITE_FAILED, f"batch {addr}: {e}")
                return False

        for entry in merged:
            if entry.get("merged"):
                ok = await _do_merged(entry["key"], entry["addr"], entry["values"])
                for pt in entry["points"]:
                    results_map[pt] = ok
            else:
                point, value = entry["point"], entry["value"]
                results_map[point] = await _do_single(point, value)
        return results_map

    def _merge_contiguous_writes(self, writes: list[tuple[str, Any]]) -> list[dict]:
        # FIXED-P2: long/float后缀占用2个寄存器，合并会导致相邻地址数据被覆盖
        _MULTI_REGISTER_SUFFIXES = {"long", "float"}

        def _addr_key(address: str) -> tuple[str, int] | None:
            addr, _suffix = self._parse_address(address)  # FIXED(P3): 原问题-解包变量suffix未使用; 修复-改为_suffix前缀
            if not addr or not addr[0].isalpha():
                return None
            dev_type = addr[0].upper()
            try:
                num = int(addr[1:])
            except ValueError:
                return None
            return (dev_type, num)

        def _suffix_of(address: str) -> str:
            _, suffix = self._parse_address(address)
            return suffix

        items = []
        for point, value in writes:
            key = _addr_key(point)
            items.append({"point": point, "value": value, "key": key, "merged": False})
        i = 0
        result = []
        while i < len(items):
            key = items[i]["key"]
            if key is None or items[i]["value"] is None:
                result.append(items[i])
                i += 1
                continue
            # FIXED-P2: long/float占用2个寄存器，不能与下一个地址合并
            if _suffix_of(items[i]["point"]) in _MULTI_REGISTER_SUFFIXES:
                result.append(items[i])
                i += 1
                continue
            group = [items[i]]
            j = i + 1
            while j < len(items):
                nkey = items[j]["key"]
                if nkey and nkey[0] == key[0] and nkey[1] == key[1] + (j - i):
                    # FIXED-P2: 当前点占用多寄存器时停止合并
                    if _suffix_of(items[j]["point"]) in _MULTI_REGISTER_SUFFIXES:
                        break
                    try:
                        int(
                            items[j]["value"]
                        )  # FIXED-P3: 原问题-iv赋值后未使用(ruff F841); 修复-仅保留int()调用用于类型校验，不绑定变量
                        group.append(items[j])
                        j += 1
                    except (ValueError, TypeError):
                        break
                else:
                    break
            if len(group) > 1:
                # FIXED-P1: 合并前检查每个值是否为整数，浮点值静默截断会绕过_validate_write_value检查
                values_list = []
                for it in group:
                    v = it["value"]
                    if isinstance(v, float) and v != int(v):
                        # 浮点值无法无损转为整数，拆分为单独写入
                        result.append(it)
                        group = [g for g in group if g is not it]
                        continue
                    values_list.append(int(v))
                if len(values_list) <= 1:
                    for it in group:
                        result.append(it)
                    i = j
                    continue
                result.append(
                    {
                        "merged": True,
                        "key": key,
                        "addr": group[0]["key"][0] + str(group[0]["key"][1]),
                        "values": values_list,
                        "points": [it["point"] for it in group],
                    }
                )
                i = j
            else:
                result.append(items[i])
                i += 1
        return result

    async def _try_reconnect(self, device_id: str) -> None:
        if not self._config:
            return
        lock = self._reconnect_locks.setdefault(
            device_id, asyncio.Lock()
        )  # FIXED-P1: setdefault 原子操作，避免创建与检查的竞态
        if lock.locked():
            return
        # FIXED-P0: _is_reconnecting 标志在锁内设置/重置，避免竞态条件
        async with lock:
            self._is_reconnecting = True
            try:
                await self._do_reconnect(device_id)
            finally:
                self._is_reconnecting = False

    async def _do_reconnect(self, device_id: str) -> None:
        if not self._config:
            return
        # FIXED-P1: 熔断检查，已熔断设备不再尝试重连
        # FIXED-P2: 增加half-open自动恢复，熔断超过_CIRCUIT_RECOVERY_SECONDS后允许一次试探连接
        if device_id in self._circuit_open:
            open_since = self._circuit_open_since.get(device_id, 0.0)
            if time.monotonic() - open_since < self._CIRCUIT_RECOVERY_SECONDS:
                logger.warning(
                    "[mc] device=%s code=CIRCUIT_OPEN msg=Device is circuit-broken, skipping reconnect", device_id
                )
                return
            logger.info(
                "[mc] device=%s code=CIRCUIT_HALF_OPEN msg=Circuit breaker recovery timeout reached, attempting probe",
                device_id,
            )
        count = self._reconnect_count.get(device_id, 0) + 1
        self._reconnect_count[device_id] = count
        if count > self._MAX_RECONNECT_ATTEMPTS:
            self._log_error(
                device_id, McDriverErrors.RECONNECT_FAILED, f"max attempts reached: {count}, circuit breaker activated"
            )
            self._set_conn_state(ConnectionState.OFFLINE.value)
            self._circuit_open.add(device_id)  # FIXED-P1: 加入熔断状态，不再调度_delayed_reconnect递归
            self._circuit_open_since[device_id] = time.monotonic()  # FIXED-P2: 记录熔断开始时间，用于half-open恢复
            self._reconnect_count.pop(device_id, None)
            self._reconnect_delay = self._RECONNECT_BASE_DELAY
            return
        has_backup = bool(self._backup_ip) and not self._using_backup
        if has_backup:
            delay = self._FAILOVER_FAST_DELAY
        else:
            delay = min(self._RECONNECT_BASE_DELAY * (2 ** (count - 1)), self._RECONNECT_MAX_DELAY)
        jitter_ms = random.randint(0, 200 if has_backup else 1000)
        total_delay = delay + jitter_ms / 1000.0
        logger.warning(
            "[mc] reconnect in %.2fs (attempt %d, fast=%s, jitter=%dms): %s",
            total_delay,
            count,
            has_backup,
            jitter_ms,
            device_id,
        )
        self._set_conn_state(ConnectionState.CONNECTING.value)
        if self._failover_start_mono == 0.0 and has_backup:
            self._failover_start_mono = time.monotonic()
        await asyncio.sleep(total_delay)
        target_ip = self._active_ip
        if has_backup:
            self._primary_fail_count += 1
            if self._primary_fail_count >= self._FAILOVER_THRESHOLD:
                target_ip = self._backup_ip
                self._active_ip = self._backup_ip
                self._using_backup = True
                self._failover_count += 1
                failover_dur = (time.monotonic() - self._failover_start_mono) * 1000 if self._failover_start_mono else 0
                self._last_failover_duration_ms = failover_dur
                self._last_failover_time = datetime.now(UTC).isoformat()
                self._failover_start_mono = 0.0
                self._log_error(
                    device_id,
                    McDriverErrors.FAILOVER_TRIGGERED,
                    f"primary->{self._backup_ip} duration={failover_dur:.0f}ms",
                )
                if failover_dur > self._FAILOVER_MAX_DURATION_MS:
                    self._log_error(
                        device_id,
                        McDriverErrors.FAILOVER_FAST,
                        f"failover took {failover_dur:.0f}ms > {self._FAILOVER_MAX_DURATION_MS}ms",
                    )
        elif self._using_backup:
            pass
        else:
            if self._backup_ip:
                self._primary_fail_count += 1
        port = int(self._config.get("port", 5007))
        plc_type = self._config.get("plc_type", "iQ-R")
        if not target_ip:
            if self._backup_ip and not self._using_backup:
                target_ip = self._backup_ip
                self._active_ip = self._backup_ip
                self._using_backup = True
                self._failover_count += 1
                self._last_failover_time = datetime.now(UTC).isoformat()
                self._log_error(
                    device_id, McDriverErrors.FAILOVER_NO_BACKUP, "no primary, fallback to backup"
                )  # FIXED-P3: 原问题-f-string无占位符(ruff F541); 修复-移除多余f前缀
            else:
                return
        # FIXED-P0: CAS模式 - 先创建新client，成功后再替换旧client，避免窗口期self._client指向已关闭对象
        old_client = self._client
        new_client = None
        try:
            frame_type = self._config.get("frame_type", "3E")
            if frame_type == "auto":
                frame_type = "3E"
            if frame_type == "4E":
                from pymcprotocol import Type4E

                new_client = Type4E(plctype=plc_type)
            else:
                from pymcprotocol import Type3E

                new_client = Type3E(plctype=plc_type)
            if self._is_fx5u and port == 5007:
                port = 5001
            t0 = time.monotonic()
            await self._call_sync(new_client.connect, target_ip, port, timeout=5.0)
            self._last_latency_ms = (time.monotonic() - t0) * 1000
            # 重连后应用通信模式 (binary/ascii); ascii 切换失败时优雅降级为 binary
            self._apply_comm_mode(new_client)
            # FIXED-P0: 连接成功后在_sync_lock内替换旧client，防止与_call_sync的_locked_call并发访问竞态
            with self._sync_lock:
                self._client = new_client
            # FIXED-P0: 关闭旧client，防止连接泄漏
            if old_client:
                try:
                    await self._call_sync(old_client.close, timeout=5.0)
                except Exception as e:
                    logger.warning("[mc] old client close failed during reconnect: %s", e)
                    # FIXED-P2: close失败时尝试强制关闭底层socket，释放PLC连接槽位
                    # 之前：close失败仅记录warning，旧TCP连接可能仍存活占用PLC连接槽位
                    # 之后：close失败时尝试关闭底层socket，确保释放连接资源
                    try:
                        sock = getattr(old_client, "socket", None)
                        if sock is None:
                            sock = getattr(getattr(old_client, "_socket", None), "_sock", None)
                        if sock is not None:
                            sock.settimeout(0.5)
                            sock.close()
                    # FIXED-P1: 原问题-except Exception: pass 静默吞没异常，改为至少 logger.debug
                    except Exception as e:
                        logger.debug("[mc] old client socket close failed: %s", e)
            # FIXED-P0: 重连成功时检查stop()是否已调用，防止stop后驱动恢复运行
            if not self._running:
                try:
                    await self._call_sync(
                        self._client.close, timeout=5.0, write=True
                    )  # FIXED-P1: 关闭操作修改client状态，持锁
                except Exception as e:
                    logger.warning("[mc] operation failed: %s", e)  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                self._client = None
                return
            self._running = True
            self._reconnect_count.pop(device_id, None)
            self._primary_fail_count = 0
            self._failover_start_mono = 0.0
            self._circuit_open.discard(device_id)  # FIXED-P1: 连接成功时移除熔断状态
            self._circuit_open_since.pop(device_id, None)  # FIXED-P2: 连接成功时清除熔断时间
            self._set_conn_state(ConnectionState.CONNECTED.value)
            logger.info(
                "MC reconnect OK: %s:%d (fx5u=%s backup=%s latency=%.0fms)",
                target_ip,
                port,
                self._is_fx5u,
                self._using_backup,
                self._last_latency_ms,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # FIXED-P0: 连接失败时关闭新client（非旧client），恢复旧client引用
            if new_client:
                try:
                    await self._call_sync(new_client.close, timeout=5.0)
                except Exception as e:
                    logger.warning(
                        "[mc] new client close failed during reconnect: %s", e
                    )  # FIXED-P2: 原问题-close异常被静默吞没，添加日志记录
            # 旧client仍然可用（未关闭），恢复引用
            self._client = old_client
            # FIXED-P1: half-open试探失败后重置熔断计时，防止立即再次试探导致快速循环
            if device_id in self._circuit_open:
                self._circuit_open_since[device_id] = time.monotonic()
            self._log_error(target_ip, McDriverErrors.RECONNECT_FAILED, str(e))
            self._set_conn_state(
                ConnectionState.DEGRADED.value
                if self._reconnect_count.get(device_id, 0) < self._MAX_RECONNECT_ATTEMPTS
                else ConnectionState.OFFLINE.value
            )

    async def _delayed_reconnect(self, delay: float, device_id: str = "") -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        if self._running:
            if not device_id:
                device_id = next(iter(self._devices), None) or self._active_ip
                if not device_id:
                    return
            await self._try_reconnect(device_id)

    async def _try_revert_primary(self, device_id: str) -> None:
        if not self._using_backup or not self._primary_ip:
            return
        if not device_id:
            return
        self._active_ip = self._primary_ip
        self._using_backup = False
        self._primary_fail_count = 0
        self._failover_start_mono = 0.0
        self._log_error(device_id, McDriverErrors.FAILOVER_REVERT, f"backup->{self._primary_ip}")
        await self._try_reconnect(device_id)

    async def add_device(self, device_id: str, config: dict, points: list[dict] | None = None) -> None:
        """添加MC协议设备，保存配置和测点映射"""
        if points is None:
            points = []
        self._devices[device_id] = {
            "config": config,
            "points": {p.get("name", p.get("address", "")): p for p in points if p.get("name") or p.get("address")},
        }
        if self._config_version_mgr:
            await self._config_version_mgr.save_version(device_id, config, change_summary="device added")
        if self._mc_audit:
            self._mc_audit.log_config_change(device_id, list(config.keys()), {}, config)
        logger.info("MC设备已添加: %s (%d测点)", device_id, len(points))

    async def discover_devices(self, config: dict) -> list[dict]:
        """扫描IP段发现三菱MC协议设备，通过尝试连接测试判断设备是否在线

        config参数:
            network: 网段地址 (如 "192.168.1.0/24") 或单个IP
            host: 单个IP地址 (与network二选一)
            port: MC协议端口 (默认5007)
            plc_type: PLC型号 (默认"iQ-R")
            timeout: 连接超时秒数 (默认3)
            max_concurrent: 最大并发数 (默认10)
        """
        try:
            from pymcprotocol import Type3E
        except ImportError:
            logger.warning("[mc] pymcprotocol未安装，无法执行MC设备发现")
            return []

        import ipaddress

        network = config.get("network", "")
        host = config.get("host", config.get("ip", ""))
        port = int(config.get("port", 5007))
        plc_type = config.get("plc_type", "iQ-R")
        timeout = int(config.get("timeout", 3))
        max_concurrent = int(config.get("max_concurrent", 10))

        # FX5U 默认使用 SLMP 端口
        is_fx5u = plc_type.upper() in ("FX5U", "FX5", "FX5U SLMP", "FX3U")
        if is_fx5u and port == 5007:
            port = 5001

        if network:
            try:
                net = ipaddress.ip_network(network, strict=False)
                ips = [str(ip) for ip in net.hosts()]
            except ValueError as e:
                logger.error("[mc] MC发现: 无效的网段 %s - %s", network, e)
                return []
        elif host:
            ips = [host]
        else:
            logger.warning("[mc] MC发现: 未指定network或host参数")
            return []

        discovered = []
        sem = asyncio.Semaphore(max_concurrent)

        async def _probe(ip_addr: str) -> dict | None:
            async with sem:
                try:
                    client = Type3E(plctype=plc_type)
                    await self._run_in_executor(client.connect, ip_addr, port, timeout=timeout + 1)
                    try:
                        await self._run_in_executor(client.close, timeout=3.0)
                    except Exception as e:
                        logger.debug("[mc] error: %s", e)
                    return {
                        "device_id": f"mc_{ip_addr.replace('.', '_')}",
                        "name": f"Mitsubishi PLC ({ip_addr})"
                        + (
                            " [FX5U]" if is_fx5u else ""
                        ),  # FIXED-P3: 原问题-f-string无占位符(ruff F541); 修复-移除多余f前缀
                        "protocol": "mc",
                        "config": {
                            "host": ip_addr,
                            "port": port,
                            "plc_type": plc_type,
                        },
                        "points": [],
                        "details": {
                            "is_fx5u": is_fx5u,
                        },
                    }
                except Exception as e:
                    logger.debug("[mc] error: %s", e)
                    return None

        tasks = [_probe(ip) for ip in ips]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for r in results:
            if isinstance(r, dict):
                discovered.append(r)

        logger.info("MC设备发现完成: 扫描%d个IP, 发现%d台设备", len(ips), len(discovered))
        return discovered

    async def _start_watchdog(self) -> None:
        await self._stop_watchdog()
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())

    async def _stop_watchdog(self) -> None:
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(self._watchdog_task, timeout=2.0)
            self._watchdog_task = None

    async def _watchdog_loop(self) -> None:
        while self._running:
            try:
                # MC-MED-001: 使用 _WATCHDOG_MAX_INTERVAL 替代硬编码的 300
                probe_interval = min(15 * (2 ** min(self._probe_fail_count, 3)), self._WATCHDOG_MAX_INTERVAL)
                await asyncio.sleep(probe_interval)
                if not self._running:
                    break
                connected = await self._call_sync(self._check_connection, timeout=3.0)
                if connected:  # FIXED-P3: 原问题-now=time.monotonic()赋值后未使用(ruff F841); 修复-移除无用赋值
                    self._probe_fail_count = 0
                    if self._last_latency_ms < 100:
                        self._watchdog_interval = self._WATCHDOG_INTERVAL_GOOD
                    else:
                        self._watchdog_interval = self._WATCHDOG_INTERVAL_BAD
                    if self._using_backup and self._primary_ip:
                        probe = None  # FIXED-P2: 初始化probe变量，确保except分支也能关闭
                        try:
                            from pymcprotocol import Type3E

                            probe = Type3E(plctype=self._config.get("plc_type", "iQ-R"))
                            await self._run_in_executor(
                                probe.connect, self._primary_ip, int(self._config.get("port", 5007)), timeout=3.0
                            )
                            try:
                                await self._run_in_executor(probe.close, timeout=2.0)
                            except TimeoutError:
                                logger.warning(
                                    "[mc] code=PROBE_CLOSE_TIMEOUT msg=Probe close timed out after 2s, continuing"
                                )
                            except Exception as e:
                                logger.warning(
                                    "[mc] watchdog_loop failed: %s", e
                                )  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                            self._probe_fail_count = 0
                            await self._try_revert_primary(list(self._devices.keys())[0] if self._devices else "")
                        except Exception:
                            self._probe_fail_count += 1
                            if probe is not None:  # FIXED-P2: probe连接失败时也尝试关闭，防止socket泄漏
                                try:
                                    await asyncio.to_thread(probe.close)
                                except Exception as e:
                                    logger.warning(
                                        "[mc] watchdog_loop failed: %s", e
                                    )  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                else:
                    self._probe_fail_count += 1
                for device_id in list(self._devices.keys()):
                    if not connected:
                        with self._stats_lock:  # FIXED-P0: _offline_since读写纳入_stats_lock，与写入路径锁保护一致
                            if device_id not in self._offline_since:
                                self._offline_since[device_id] = datetime.now(
                                    UTC
                                )  # FIXED-P0: _offline_since统一使用datetime类型
                                self._set_conn_state(ConnectionState.DEGRADED.value)
                            elif (
                                datetime.now(UTC) - self._offline_since[device_id]
                            ).total_seconds() > 30:  # FIXED-P0: _offline_since统一使用datetime类型
                                self._set_conn_state(ConnectionState.OFFLINE.value)
                                # MC-002: 检查是否正在重连，避免重复触发
                                if not self._is_reconnecting:
                                    await self._try_reconnect(device_id)
                    else:
                        with self._stats_lock:  # FIXED-P0: _offline_since pop纳入_stats_lock，与写入路径锁保护一致
                            if device_id in self._offline_since:
                                self._offline_since.pop(device_id, None)
                                self._set_conn_state(ConnectionState.CONNECTED.value)
            except asyncio.CancelledError:
                break
            except Exception as e:
                # CROSS-002: 分级处理异常，不静默吞没
                if not self._handle_watchdog_exception(e, "mc_watchdog"):
                    break

    def remove_device(self, device_id: str) -> None:
        """Remove a device at runtime"""
        with self._stats_lock:  # FIXED-P1: 与基类写入路径锁保护一致
            self._health_stats.pop(device_id, None)
            self._offline_since.pop(device_id, None)  # FIXED-P0: 纳入_stats_lock，与watchdog写入路径锁保护一致
        self._reconnect_count.pop(device_id, None)
        self._circuit_open.discard(device_id)  # FIXED-P1: 清理熔断状态
        old_task = self._reconnect_tasks.get(device_id)
        if old_task and not old_task.done():
            old_task.cancel()
        self._reconnect_tasks.pop(device_id, None)
        self._reconnect_locks.pop(device_id, None)
        # FIXED-P2: _value_history字典无容量限制，remove_device不清理
        for key in [k for k in self._value_history if k.startswith(device_id)]:
            del self._value_history[key]
        # FIXED-P2: _write_rate_limits字典无容量限制，remove_device不清理
        for key in [k for k in self._write_rate_limits if k.startswith(device_id)]:
            del self._write_rate_limits[key]
        # FIXED-P1: 清理_devices字典，防止watchdog继续遍历已移除设备触发无效重连
        self._devices.pop(device_id, None)
        logger.info("MC device removed: %s", device_id)

    async def check_permission(self, permission: Permission) -> bool:  # FIXED-P1: 权限检查方法
        async with self._role_lock:
            return has_permission(self._current_user_role, permission)

    async def set_user_role(
        self, role: str
    ) -> None:  # FIXED-P1: 改为async并使用_role_lock，与check_permission和TCP驱动一致
        async with self._role_lock:
            self._current_user_role = role

    async def health_check(self, device_id: str) -> bool:
        if not self._running:
            return False
        if not self._client:
            return False
        try:
            return await self._call_sync(self._check_connection, timeout=3.0)
        except Exception as e:
            logger.warning("[mc] check_connection failed: %s", e)  # FIXED-P1: 原问题-连接检查异常返回False无日志
            return False

    def _log_error(self, device_id: str, error_code: str, message: str) -> None:
        i18n_msg = _t(error_code) if error_code.startswith("ERR_") else error_code
        logger.error("[mc] device=%s code=%s i18n=%s msg=%s", device_id, error_code, i18n_msg, message)

    def _set_conn_state(self, new_state: str) -> None:
        with self._sync_lock:  # FIXED-P1: _conn_state赋值加锁保护，防止watchdog与重连并发读写竞态
            valid_transitions = {
                ConnectionState.DISCONNECTED.value: {ConnectionState.CONNECTING.value},
                ConnectionState.CONNECTING.value: {
                    ConnectionState.CONNECTED.value,
                    ConnectionState.DISCONNECTED.value,
                    ConnectionState.OFFLINE.value,
                },
                ConnectionState.CONNECTED.value: {
                    ConnectionState.DEGRADED.value,
                    ConnectionState.DISCONNECTED.value,
                    ConnectionState.CONNECTING.value,
                },
                ConnectionState.DEGRADED.value: {
                    ConnectionState.CONNECTED.value,
                    ConnectionState.OFFLINE.value,
                    ConnectionState.CONNECTING.value,
                },
                ConnectionState.OFFLINE.value: {ConnectionState.CONNECTING.value, ConnectionState.DISCONNECTED.value},
            }
            allowed = valid_transitions.get(self._conn_state, set())
            if new_state in allowed or new_state == self._conn_state:
                old = self._conn_state
                self._conn_state = new_state
                if old != new_state:
                    logger.info("[mc] state: %s -> %s", old, new_state)
                    for device_id in list(self._devices.keys()):
                        try:
                            loop = asyncio.get_running_loop()
                            task = loop.create_task(self._set_connection_state(device_id, new_state))
                            self._background_tasks.add(task)
                            task.add_done_callback(self._background_tasks.discard)
                        except RuntimeError as e:
                            logger.debug(
                                "[mc] set_conn_state failed: %s", e
                            )  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
            else:
                logger.warning("[mc] state transition blocked: %s -> %s", self._conn_state, new_state)

    def get_conn_state(self) -> str:
        return self._conn_state

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

    def _update_point_stats(self, point: str, success: bool) -> None:
        s = self._point_stats.setdefault(
            point, {"success_count": 0, "fail_count": 0, "consecutive_fails": 0, "latency_samples": deque(maxlen=20)}
        )
        if success:
            s["success_count"] += 1
            s["consecutive_fails"] = 0
        else:
            s["fail_count"] += 1
            s["consecutive_fails"] += 1
        # FIXED-P1: 使用OrderedDict的popitem(last=False)进行O(1)淘汰，替代创建key列表的O(n)操作
        if len(self._point_stats) > 10000:
            evict_count = len(self._point_stats) // 5
            for _ in range(evict_count):
                self._point_stats.popitem(last=False)

    def get_point_stats(self, point: str) -> dict:
        s = self._point_stats.get(point, {})
        avg_lat = 0.0
        if s.get("latency_samples"):
            avg_lat = sum(s["latency_samples"]) / len(s["latency_samples"])
        return {
            "success_count": s.get("success_count", 0),
            "fail_count": s.get("fail_count", 0),
            "consecutive_fails": s.get("consecutive_fails", 0),
            "avg_latency_ms": avg_lat,
        }

    def _check_nan_inf(self, point: str, value: Any) -> bool:
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            self._log_error(point, McDriverErrors.NAN_INF, f"value={value}")
            return True
        return False

    def _check_rate_of_change(self, point: str, value: float, now: datetime, threshold: float) -> bool:
        last_val = self._last_values.get(point)
        if point in self._last_values:
            self._last_values.move_to_end(point)  # FIXED-P1: 读取时也更新LRU顺序，避免FIFO淘汰重要测点
        last_ts = self._last_timestamps.get(point)
        if last_val is None or last_ts is None:
            return False
        dt = (now - last_ts).total_seconds()
        if dt <= 0:
            return False
        rate = abs(value - last_val) / dt
        if rate > threshold:
            self._log_error(point, McDriverErrors.RATE_OF_CHANGE, f"rate={rate:.2f} threshold={threshold}")
            return True
        return False

    def _check_frozen_value(self, point: str, value: float, window: int) -> bool:
        hist = self._value_history.setdefault(point, deque(maxlen=self._FROZEN_VALUE_WINDOW))
        hist.append(value)
        # FIXED-P1: _value_history外层容量淘汰
        while len(self._value_history) > self._MAX_AUX_DICT_SIZE:
            self._value_history.popitem(last=False)
        if len(hist) >= window and all(abs(v - hist[0]) < 1e-9 for v in hist):
            self._log_error(point, McDriverErrors.FROZEN_VALUE, f"frozen for {window} samples")
            return True
        return False

    def _get_point_config(self, point: str) -> dict:
        for dev_info in list(self._devices.values()):  # FIXED-P1: 使用list()快照，防止迭代中dict被修改
            pt = dev_info.get("points", {}).get(point)
            if pt:
                return pt
        return {}

    def _update_degrade_level(self, device_id: str) -> None:
        total = 0
        bad = 0
        for s in self._point_stats.values():
            total += s.get("success_count", 0) + s.get("fail_count", 0)
            bad += s.get("fail_count", 0)
        if total == 0:
            return
        success_rate = (total - bad) / total
        if success_rate >= 0.8:
            new_level = 0
        elif success_rate >= 0.5:
            new_level = 1
        elif success_rate >= 0.2:
            new_level = 2
        else:
            new_level = 3
        if new_level > 0 and self._degrade_level == 0:
            self._degrade_enter_time = time.monotonic()
        if new_level == 0 and self._degrade_level > 0:
            self._degrade_enter_time = 0.0
        if self._degrade_level > 0 and new_level == self._degrade_level:
            elapsed_minutes = (time.monotonic() - self._degrade_enter_time) / 60.0
            if elapsed_minutes >= self._DEGRADE_RECOVERY_MINUTES and success_rate >= 0.8:
                new_level = max(0, self._degrade_level - 1)
                logger.info(
                    "[mc] degrade time-based recovery: level %d->%d after %.1f min (success_rate=%.2f)",
                    self._degrade_level,
                    new_level,
                    elapsed_minutes,
                    success_rate,
                )
                if new_level == 0:
                    self._degrade_enter_time = 0.0
        if new_level != self._degrade_level:
            old_interval = (
                self._DEGRADE_LEVELS[self._degrade_level] if self._degrade_level < len(self._DEGRADE_LEVELS) else 60
            )
            self._degrade_level = new_level
            new_interval = self._DEGRADE_LEVELS[new_level] if new_level < len(self._DEGRADE_LEVELS) else 60
            if new_level > 0:
                self._set_conn_state(ConnectionState.DEGRADED.value)
                self._log_error(
                    device_id,
                    McDriverErrors.DEGRADE_ACTIVE,
                    f"success_rate={success_rate:.2f} interval={new_interval}s",
                )
            else:
                if self._conn_state == ConnectionState.DEGRADED.value:
                    self._set_conn_state(ConnectionState.CONNECTED.value)
                    self._log_error(device_id, McDriverErrors.DEGRADE_RECOVERED, f"success_rate={success_rate:.2f}")
            logger.info(
                "[mc] degrade_level: %d->%d interval: %ds->%ds", new_level, new_level, old_interval, new_interval
            )

    def get_degrade_interval(self) -> float:
        if self._degrade_level < len(self._DEGRADE_LEVELS):
            return self._DEGRADE_LEVELS[self._degrade_level]
        return 60

    def _validate_write_value(self, value: Any, suffix: str) -> bool:
        if value is None:
            return False
        if suffix == "bit":
            try:
                v = int(bool(value))
                return v in (0, 1)
            except (ValueError, TypeError):
                return False
        try:
            iv = int(value)
            # FIXED-P0: Python float类型值(如3.14)通过int()不抛异常但被静默截断，需前置检查
            if isinstance(value, float) and value != iv and suffix in ("word", "uword", "long", "B", "byte", "int8"):
                return False
        except (
            ValueError,
            TypeError,
            OverflowError,
        ):  # FIXED-P0: OverflowError在int(float('inf'))时抛出，需捕获防止write_point崩溃
            try:
                fv = float(value)
                if math.isnan(fv) or math.isinf(fv):
                    return False
                if suffix == "float":
                    return True
                if fv != int(fv) and suffix in ("word", "uword", "long"):
                    return False
                iv = int(fv)
            except (ValueError, TypeError, OverflowError):
                return False
        if suffix == "word" or suffix == "uword":
            return -32768 <= iv <= 65535
        elif suffix == "long":
            return -2147483648 <= iv <= 4294967295
        elif suffix == "float":
            fv = float(value)  # FIXED-P1: int(value)成功时fv未定义，确保赋值
            return math.isfinite(fv)  # FIXED-SIM103: 直接返回条件, 拒绝NaN/Inf防止写入PLC异常值
        elif suffix in ("B", "byte"):  # FIXED-P1: byte无符号0~255
            return 0 <= iv <= 255
        elif suffix == "int8":  # FIXED-P1: int8有符号-128~127，与byte区分
            return -128 <= iv <= 127
        else:
            return -32768 <= iv <= 65535

    def _record_write_audit(
        self, device_id: str, point: str, address: str, area_code: str, old_value: Any, new_value: Any, result: str
    ) -> None:
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "user": "",
            "device_id": device_id,
            "point_id": point,
            "area_code": area_code,
            "address": address,
            "old_value": old_value,
            "new_value": new_value,
            "result": result,
        }
        self._write_audit_log.append(entry)

    def get_write_audit_log(self, device_id: str | None = None, limit: int = 100) -> list[dict]:
        entries = list(self._write_audit_log)
        if device_id is None:
            return entries[-limit:]
        return [e for e in entries if e.get("device_id") == device_id][-limit:]

    def _init_edge_rules(self) -> None:
        try:
            from edgelite.drivers.edge_rule_engine import ModbusEdgeRuleEngine
            from edgelite.drivers.edge_triggers import EdgeTriggerExecutor
            from edgelite.drivers.rule_store import RuleStore

            try:
                from edgelite.engine.event_bus import EventBus

                event_bus = EventBus.instance()
            except Exception:
                event_bus = None
            self._rule_store = RuleStore()
            self._rule_engine = ModbusEdgeRuleEngine(event_bus=event_bus)
            self._trigger_executor = EdgeTriggerExecutor(
                device_write_callback=self._edge_write_callback,
            )
            self._rule_engine.set_on_action_callback(self._trigger_executor.execute)
            rules = self._rule_store.load_rules()
            # 校验主库 rule 是否存在，清理 edge_rules.db 中的孤儿规则
            # 补偿 RuleRepo.delete 跨库清理失败的情况
            try:
                from edgelite.app import _app_state

                _db = getattr(_app_state, "database", None)
                _db_path = getattr(_db, "db_path", None) if _db else None
                if _db_path:
                    import sqlite3 as _sqlite3

                    _conn = _sqlite3.connect(f"file:{_db_path}?mode=ro", uri=True)
                    try:
                        _cursor = _conn.execute("SELECT rule_id FROM rules")
                        _valid_ids = {row[0] for row in _cursor.fetchall()}
                    finally:
                        _conn.close()
                    _orphan_count = self._rule_store.cleanup_orphan_rules(_valid_ids)
                    if _orphan_count > 0:
                        logger.warning("[mc] 清理 %d 条孤儿边缘规则", _orphan_count)
                    # 清理后重新加载规则
                    rules = self._rule_store.load_rules()
            except Exception as _e:
                logger.debug("[mc] 孤儿规则校验跳过: %s", _e)
            for r in rules:
                self._rule_engine.add_rule(r)
            logger.info("[mc] edge rules loaded: %d rules", len(rules))
        except Exception as e:
            logger.warning("[mc] edge rule engine init failed: %s", e)
            self._rule_engine = None
            self._trigger_executor = None
            self._rule_store = None

    async def _edge_write_callback(self, device_id: str, point: str, value: Any) -> dict:
        # FIXED-P0: 签名对齐EdgeTriggerExecutor._do_write_point调用约定(device_id, point, value)
        try:
            await self.write_points_batch(device_id, {point: value})
            return {"success": True, "device_id": device_id, "point": point}
        except Exception as e:
            return {"success": False, "device_id": device_id, "point": point, "error": str(e)}

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
            except Exception as e:
                logger.debug("[mc] rule eval failed: %s %s - %s", device_id, point_name, e)

    async def reload_rules(self) -> int:
        if not self._rule_engine or not self._rule_store:
            return 0
        existing = {r.rule_id for r in self._rule_engine._rules.values()}
        for rid in existing:
            await self._rule_engine.remove_rule(rid)
        rules = self._rule_store.load_rules()
        for r in rules:
            self._rule_engine.add_rule(r)
        logger.info("[mc] rules reloaded: %d rules", len(rules))
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
            logger.info("[mc] edge rule added: %s", rule.rule_id)
            return True
        except Exception as e:
            logger.error("[mc] add edge rule failed: %s", e)
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

    async def _init_ts_storage(
        self, config: dict
    ) -> None:  # FIXED-P0: 改为async方法，设置属性后调用connect()初始化InfluxDB客户端
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
            from edgelite.storage.influx_storage import InfluxDBStorage

            self._ts_storage = InfluxDBStorage()
            self._ts_storage._url = ts_config.get("influx_url", "")
            self._ts_storage._org = ts_config.get("influx_org", "edgelite")
            self._ts_storage._bucket = ts_config.get("influx_bucket", "edgelite")
            self._ts_storage._token = ts_config.get("influx_token", "")
            await (
                self._ts_storage.connect()
            )  # FIXED-P0: 原代码绕过connect()直接设置私有属性，导致_client为None、_available为False，时序存储始终降级
            self._persist_enabled = True
            logger.info("[mc] ts storage: InfluxDB mode")
        except Exception:
            try:
                from edgelite.storage.sqlite_ts import SqliteTimeSeriesStorage

                self._ts_storage = SqliteTimeSeriesStorage(db_path=ts_config.get("sqlite_path", "data/mc_ts.db"))
                self._persist_enabled = True
                logger.info("[mc] ts storage: SQLite fallback mode")
            except Exception as e2:
                self._log_error("", McDriverErrors.TS_STORAGE_INIT_FAILED, str(e2))
                self._persist_enabled = False
                self._ts_storage = None
        try:
            from edgelite.storage.offline_queue import OfflineQueue

            self._offline_queue = OfflineQueue(
                db_path=ts_config.get("offline_db_path", "data/mc_offline.db"),
                max_size_mb=ts_config.get("offline_max_mb", 200),
            )
        except Exception as e:
            logger.warning("[mc] offline queue init failed: %s", e)
            self._offline_queue = None

    async def _persist_points(self, device_id: str, result: dict[str, Any]) -> None:
        if not self._persist_enabled or not self._ts_storage:
            return
        records = []
        now_iso = datetime.now(UTC).isoformat()
        for point_name, pv in result.items():
            if not isinstance(pv, PointValue):
                continue
            ts = pv.timestamp.isoformat() if pv.timestamp else now_iso
            records.append(
                {
                    "device_id": device_id,
                    "point_name": point_name,
                    "value": pv.value,
                    "quality": pv.quality,
                    "timestamp": ts,
                }
            )
        if not records:
            return
        try:
            if hasattr(self._ts_storage, "write_points_batch"):
                ok = await self._ts_storage.write_points_batch(records)
            elif hasattr(self._ts_storage, "write_point"):
                for r in records:
                    await self._ts_storage.write_point(
                        measurement="mc",
                        device_id=r["device_id"],
                        point_name=r["point_name"],
                        value=r["value"],
                        quality=r["quality"],
                        timestamp_ns=r["timestamp"],
                    )
                ok = True
            else:
                ok = False
            if not ok and self._offline_queue:
                for r in records:
                    await self._offline_queue.enqueue("mc_ts", r)
                logger.debug("[mc] %d records queued offline", len(records))
        except Exception as e:
            if self._offline_queue:
                for r in records:
                    await self._offline_queue.enqueue("mc_ts", r)
            logger.debug("[mc] persist failed, queued offline: %s", e)

    async def start_upload(self) -> None:
        if self._upload_task and not self._upload_task.done():
            return
        self._upload_task = asyncio.create_task(self._upload_loop())
        logger.info("[mc] upload loop started")

    async def stop_upload(self) -> None:
        if self._upload_task and not self._upload_task.done():
            self._upload_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._upload_task
        self._upload_task = None
        logger.info("[mc] upload loop stopped")

    async def _upload_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(30)
                if not self._offline_queue:
                    continue
                sent = await self._offline_queue.flush(send_callback=self._upload_send_callback)
                if sent > 0:
                    logger.info("[mc] offline queue uploaded: %d records", sent)
                purged = await self._offline_queue.purge_max_retries(max_retries=10)
                if purged > 0:
                    logger.warning("[mc] offline queue purged %d records exceeding max retries (10)", purged)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("[mc] upload loop error: %s", e)

    async def _upload_send_callback(
        self, topic: str, payload: Any
    ) -> bool:  # FIXED-P0: 回调签名与offline_queue.flush(send_callback(item["topic"], item["payload"]))不匹配，原签名(self, record: dict)导致TypeError
        if not self._ts_storage:
            return False
        try:
            record = payload if isinstance(payload, dict) else {"topic": topic, "payload": payload}
            if isinstance(record.get("payload"), str):
                import json

                record["payload"] = json.loads(record["payload"])
            p = record.get("payload", record)
            if hasattr(self._ts_storage, "write_points_batch"):
                ok = await self._ts_storage.write_points_batch([p])
            else:
                ok = await self._ts_storage.write_point(
                    measurement="mc",
                    device_id=p.get("device_id", ""),
                    point_name=p.get("point_name", ""),
                    value=p.get("value"),
                    quality=p.get("quality", "good"),
                    timestamp_ns=p.get("timestamp", ""),
                )
            return ok
        except Exception as e:
            logger.warning("[mc] persist_points failed: %s", e)  # FIXED-P1: 原问题-持久化异常返回False无日志
            return False

    async def force_sync(self) -> int:
        if not self._ts_storage or not hasattr(self._ts_storage, "force_sync"):
            return 0
        try:
            return await self._ts_storage.force_sync()
        except Exception as e:
            logger.warning("[mc] force_sync failed: %s", e)  # FIXED-P1: 原问题-同步异常返回0无日志
            return 0

    def get_storage_stats(self) -> dict:
        stats = {"persist_enabled": self._persist_enabled, "storage_type": "none"}
        if self._ts_storage:
            if hasattr(self._ts_storage, "get_stats"):
                stats["ts"] = self._ts_storage.get_stats()
            if hasattr(self._ts_storage, "get_fallback_stats"):
                stats["fallback"] = self._ts_storage.get_fallback_stats()
            stats["storage_type"] = type(self._ts_storage).__name__
        if self._offline_queue:
            try:
                stats["offline_queue"] = {"db_path": self._offline_queue._db_path}
            except Exception as e:
                logger.warning("[mc] get_storage_stats failed: %s", e)  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
        return stats

    def _init_ota(self, config: dict) -> None:
        try:
            from edgelite.drivers.mc_ota import McOtaManager

            self._ota_manager = McOtaManager()
            version = config.get("firmware_version", "1.0.0")
            self._ota_manager.set_current_version(version)
            logger.info("[mc] OTA manager initialized (v%s)", version)
        except Exception as e:
            logger.warning("[mc] OTA init failed: %s", e)
            self._ota_manager = None

    def _init_config_version(self) -> None:
        try:
            from edgelite.drivers.mc_config_version import McConfigVersionManager

            self._config_version_mgr = McConfigVersionManager()
            logger.info("[mc] config version manager initialized")
        except Exception as e:
            logger.warning("[mc] config version init failed: %s", e)
            self._config_version_mgr = None

    def _init_audit(self) -> None:
        try:
            from edgelite.drivers.mc_audit import McAudit

            audit_svc = None
            try:
                from edgelite.services.audit_service import AuditService

                audit_svc = AuditService()
            except Exception as e:
                logger.warning("[mc] init_audit failed: %s", e)  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
            self._mc_audit = McAudit(audit_service=audit_svc)
            logger.info("[mc] audit initialized")
        except Exception as e:
            logger.warning("[mc] audit init failed: %s", e)
            self._mc_audit = None

    async def save_config_version(self, device_id: str, config: dict, operator: str = "system") -> int:
        if not self._config_version_mgr:
            return 0
        return await self._config_version_mgr.save_version(device_id, config, operator=operator)

    async def rollback_config(self, device_id: str, target_version: int, operator: str = "system") -> dict | None:
        if not self._config_version_mgr:
            return None
        result = await self._config_version_mgr.rollback(device_id, target_version, operator=operator)
        if result and self._mc_audit:
            self._mc_audit.log_config_version(
                device_id, "mc_config_rollback", target_version, result.get("version", 0), operator
            )
        return result

    async def get_config_versions(self, device_id: str, limit: int = 50) -> list[dict]:
        if not self._config_version_mgr:
            return []
        return await self._config_version_mgr.get_versions(device_id, limit=limit)

    async def get_config_audit_trail(self, device_id: str, limit: int = 100) -> list[dict]:
        if not self._config_version_mgr:
            return []
        return await self._config_version_mgr.get_audit_trail(device_id, limit=limit)

    def check_rbac(self, device_id: str, permission: str, role: str) -> bool:
        try:
            from edgelite.security.rbac import has_permission

            granted = has_permission(role, permission)
        except Exception:
            granted = False  # FIXED-P0: 安全模块不可用时默认拒绝(fail-closed)，而非默认放行
        if self._mc_audit:
            self._mc_audit.log_rbac_check(device_id, permission, role, granted)
        return granted

    def get_ota_progress(self) -> dict:
        if not self._ota_manager:
            return {"status": "not_available"}
        return self._ota_manager.get_progress()

    def get_audit_stats(self) -> dict:
        if not self._mc_audit:
            return {}
        return self._mc_audit.get_stats()
