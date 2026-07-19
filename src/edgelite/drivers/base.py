"""驱动插件抽象基类"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
import random
import re
from abc import ABC, abstractmethod
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from threading import Event, Lock, RLock
from typing import Any

logger = logging.getLogger(__name__)

_RC_JITTER_FACTOR = 0.5


class LRUCache:
    """CROSS-003: 线程安全的 LRU 缓存实现

    用途：限制测点值缓存等字典的最大容量，防止无限增长

    使用 OrderedDict 实现，访问时将条目移到末尾，
    超出容量时自动淘汰最旧的条目。

    注意：此实现是线程安全的，适用于多线程环境（如同步库调用）
    """

    def __init__(self, max_size: int = 10000):
        self._max_size = max_size
        self._data: OrderedDict = OrderedDict()
        self._lock = Lock()

    def get(self, key: Any, default: Any = None) -> Any | None:
        """获取值，如果是存在的键则更新访问顺序"""
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
                return self._data[key]
            return default

    def set(self, key: Any, value: Any) -> None:
        """设置值，自动淘汰超出容量的最旧条目"""
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
                self._data[key] = value
            else:
                self._data[key] = value
                while len(self._data) > self._max_size:
                    self._data.popitem(last=False)

    def pop(self, key: Any, default: Any = None) -> Any:
        """删除并返回指定键的值"""
        with self._lock:
            return self._data.pop(key, default)

    def clear(self) -> None:
        """清空缓存"""
        with self._lock:
            self._data.clear()

    def __contains__(self, key: Any) -> bool:
        with self._lock:
            return key in self._data

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def keys(self) -> list:
        with self._lock:
            return list(self._data.keys())


@dataclass
class DriverCapabilities:
    """驱动能力声明"""

    discover: bool = False
    read: bool = True
    write: bool = False
    subscribe: bool = False
    batch_read: bool = False
    batch_write: bool = False


class ConnectionState(Enum):
    """连接状态枚举"""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    PDU_NEGOTIATING = "pdu_negotiating"
    CIP_NEGOTIATING = "cip_negotiating"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    OFFLINE = "offline"


@dataclass
class PointValue:
    """统一测点值信封"""

    value: Any
    timestamp: datetime | None = None
    quality: str = "good"  # good / bad / uncertain
    source: str = "device"  # device / cache / simulated / subscribed
    latency_ms: float = 0.0


@dataclass
class ConnectionStatus:
    """连接状态详情"""

    state: str = "disconnected"
    reason: str = ""
    since: datetime | None = None
    last_error: str = ""
    metrics: dict = field(default_factory=dict)


@dataclass
class ConfigValidationResult:
    """配置校验结果"""

    valid: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class DriverHealthStats:
    """驱动健康状态统计"""

    device_id: str = ""
    total_reads: int = 0
    failed_reads: int = 0
    total_writes: int = 0
    failed_writes: int = 0
    total_failures: int = 0  # 驱动级别总失败次数（连接错误、未知异常等）
    last_success_read: datetime | None = None
    last_failed_read: datetime | None = None
    consecutive_failures: int = 0
    total_downtime_seconds: float = 0.0
    last_online_at: datetime | None = None
    last_offline_at: datetime | None = None
    connection_quality_score: float = 100.0
    total_reconnects: int = 0
    avg_latency_ms: float = 0.0
    degradation_reason: str = ""
    _latency_samples: deque = field(
        default_factory=lambda: deque(maxlen=100)
    )  # FIXED-P2: 使用deque替代列表切片，避免频繁内存分配
    # FIXED: _record_latency 使用 self._stats_lock 保护 _latency_samples 并发写入，
    # 但 _stats_lock 原本只定义在 BaseDriver 上，DriverHealthStats 实例没有该属性，
    # 导致每次 _record_point_success → stats._record_latency 抛出 AttributeError，
    # 异常传播到 read_points → 熔断器 _on_failure，5次后熔断器 OPEN 阻断所有采集。
    _stats_lock: RLock = field(default_factory=RLock, repr=False, compare=False)
    _MOVING_AVG_WINDOW = 20

    @property
    def read_error_rate(self) -> float:
        if self.total_reads == 0:
            return 0.0
        return self.failed_reads / self.total_reads

    @property
    def write_error_rate(self) -> float:
        if self.total_writes == 0:
            return 0.0
        return self.failed_writes / self.total_writes

    def _record_latency(self, latency_ms: float) -> None:
        with self._stats_lock:
            self._latency_samples.append(latency_ms)
            # FIXED-P2: BASE-R02 避免list()全量复制，直接对deque尾部求滑动平均
            n = len(self._latency_samples)
            window = min(self._MOVING_AVG_WINDOW, n)
            if n <= self._MOVING_AVG_WINDOW:
                self.avg_latency_ms = sum(self._latency_samples) / n
            else:
                tail_sum = 0.0
                for i in range(n - window, n):
                    tail_sum += self._latency_samples[i]
                self.avg_latency_ms = tail_sum / window

    @property
    def p95_latency_ms(self) -> float:
        if not self._latency_samples:
            return 0.0
        sorted_samples = sorted(self._latency_samples)
        idx = int(len(sorted_samples) * 0.95)
        return sorted_samples[min(idx, len(sorted_samples) - 1)]

    @property
    def is_healthy(self) -> bool:
        return self.consecutive_failures < 5 and self.read_error_rate < 0.1

    @property
    def health_score(self) -> float:
        score = 100.0
        score -= min(self.consecutive_failures * 10, 40)
        score -= min(self.read_error_rate * 150, 30)
        if self.avg_latency_ms > 1000:
            score -= min((self.avg_latency_ms - 1000) / 100, 20)
        if self.total_reconnects > 0:
            score -= min(self.total_reconnects * 2, 10)
        return max(0.0, score)

    @property
    def effective_state(self) -> str:
        if self.consecutive_failures == 0 and self.read_error_rate < 0.1:
            return ConnectionState.CONNECTED.value
        if self.consecutive_failures >= 5:
            return ConnectionState.OFFLINE.value
        if self.connection_quality_score < 50:
            return ConnectionState.DEGRADED.value
        return ConnectionState.DEGRADED.value


class DriverPlugin(ABC):
    """协议驱动插件基类"""

    plugin_name: str = ""
    plugin_version: str = "0.1.0"
    supported_protocols: tuple[str, ...] = ()  # FIXED(P2): 原问题-可变默认值list; 修复-改为tuple
    config_schema: dict = {}

    # Production boundary declarations (optional, but recommended).
    # These are exposed via /api/v1/drivers/meta for UI and self-test gating.
    experimental: bool = False
    capabilities: DriverCapabilities | None = None
    constraints: tuple[dict[str, Any], ...] = ()

    fallback_handler: Callable | None = None

    _failure_threshold: int = 5
    _recovery_timeout: float = 30.0
    _half_open_max_calls: int = 3
    _half_open_calls: dict[str, int] = {}

    _VALID_STATE_TRANSITIONS: dict[str, set[str]] = {
        ConnectionState.DISCONNECTED.value: {
            ConnectionState.CONNECTING.value,
            ConnectionState.CONNECTED.value,
            ConnectionState.OFFLINE.value,
        },
        ConnectionState.CONNECTING.value: {
            ConnectionState.CONNECTED.value,
            ConnectionState.DISCONNECTED.value,
            ConnectionState.PDU_NEGOTIATING.value,
            ConnectionState.CIP_NEGOTIATING.value,
            "cert_validating",  # FIXED-P2: OPC UA证书验证状态
            "session_creating",  # FIXED-P2: OPC UA会话创建状态
            ConnectionState.OFFLINE.value,
        },
        "cert_validating": {  # FIXED-P2: OPC UA证书验证→会话创建/断开
            "session_creating",
            ConnectionState.CONNECTED.value,
            ConnectionState.DISCONNECTED.value,
            ConnectionState.OFFLINE.value,
        },
        "session_creating": {  # FIXED-P2: OPC UA会话创建→连接/断开
            ConnectionState.CONNECTED.value,
            ConnectionState.DISCONNECTED.value,
            ConnectionState.OFFLINE.value,
        },
        ConnectionState.PDU_NEGOTIATING.value: {
            ConnectionState.CIP_NEGOTIATING.value,
            ConnectionState.CONNECTED.value,
            ConnectionState.DISCONNECTED.value,
            ConnectionState.OFFLINE.value,
        },
        ConnectionState.CIP_NEGOTIATING.value: {
            ConnectionState.CONNECTED.value,
            ConnectionState.DISCONNECTED.value,
            ConnectionState.OFFLINE.value,
        },
        ConnectionState.CONNECTED.value: {
            ConnectionState.DISCONNECTED.value,
            ConnectionState.DEGRADED.value,
            ConnectionState.OFFLINE.value,
        },
        ConnectionState.DEGRADED.value: {
            ConnectionState.CONNECTED.value,
            ConnectionState.DISCONNECTED.value,
            ConnectionState.OFFLINE.value,
        },
        ConnectionState.OFFLINE.value: {
            ConnectionState.CONNECTING.value,
            ConnectionState.DISCONNECTED.value,
        },
    }

    def __init__(self) -> None:
        self._running: bool = False
        self._data_callback: Callable | None = None
        self._health_stats: dict[str, DriverHealthStats] = {}
        self._offline_since: dict[str, datetime] = {}
        self._device_configs: dict[str, dict] = {}
        cls_caps = getattr(self.__class__, "capabilities", None)
        if isinstance(cls_caps, DriverCapabilities):
            self._capabilities: DriverCapabilities = cls_caps
        else:
            self._capabilities = DriverCapabilities()
        self._connection_statuses: dict[str, ConnectionStatus] = {}
        self._reconnect_state: dict[str, dict] = {}
        # FIXED-P0: 熔断器改为设备级，避免单设备故障熔断全部设备
        self._circuit_states: dict[str, str] = {}  # device_id -> "closed"/"open"/"half_open"
        self._circuit_open_sinces: dict[str, datetime | None] = {}  # device_id -> datetime
        self._half_open_calls: dict[str, int] = {}  # device_id -> int
        self._failure_threshold: int = 5
        self._recovery_timeout: float = 30.0
        self._half_open_max_calls: int = 3
        self._reconnect_lock: asyncio.Lock = asyncio.Lock()
        self._circuit_lock: Lock = Lock()  # FIXED-P2: 原问题-_circuit_lock为asyncio.Lock，同步回退路径无法使用，改用threading.Lock统一保护_circuit_states
        # CROSS-001: 独立线程池用于同步库调用
        self._executor: concurrent.futures.ThreadPoolExecutor | None = None
        self._executor_max_workers: int = 4
        self._executor_name_prefix: str = "driver_io"
        # BASE-MED-001: 追踪正在运行的 Future 以便正确清理
        self._executor_futures: set[asyncio.Future] = set()
        # FIXED-P1: _executor_futures 集合无上限，添加大小上限告警阈值
        # 原问题：_executor_futures 集合无大小限制，若大量并发任务提交且未及时清理（如超时未触发finally），
        #         集合可能无限增长导致内存泄漏，且无法感知异常堆积
        # 修复：设置告警阈值，超过时记录warning日志，便于运维感知
        self._executor_futures_warn_threshold: int = 64
        self._executor_lock: asyncio.Lock = asyncio.Lock()
        self._shutdown_requested: Event = Event()
        # FIXED-P2: 区分executor关闭与重建，避免重建期间并发任务被静默丢弃
        self._executor_shutting_down: bool = False
        # BASE-MED-001: executor 超时等待上限（秒）
        self._executor_shutdown_timeout: float = 10.0
        # CROSS-004: 后台任务追踪
        self._background_tasks: set[asyncio.Task] = set()
        # FIXED-P0: _health_stats 并发访问保护锁（RLock允许同一线程重入，防止嵌套调用死锁）
        self._stats_lock: RLock = RLock()
        # FIXED-P1: _connection_statuses 并发保护锁；改用RLock(可重入)使同步方法(is_device_connected/get_connection_status)
        # 也能用with保护读取，且get_connection_status调用is_device_connected时嵌套加锁不会死锁。
        # 锁内临界区无await，不会阻塞事件循环。
        self._conn_state_lock: RLock = RLock()
        # FIXED-P2: BASE-03 watchdog异常历史用deque限容+Lock保护防竞态
        self._watchdog_exception_history: deque = deque(maxlen=60)
        self._watchdog_history_lock: Lock = Lock()

    @abstractmethod
    async def start(self, config: dict) -> None:
        """启动驱动

        Implementations should call _set_device_config(device_id, device_config)
        when they manage multiple devices internally.
        """

    @abstractmethod
    async def stop(self) -> None:
        """停止驱动

        子类实现时必须调用 await super().stop() 以确保基类资源清理。
        基类清理包括：取消后台任务、关闭线程池。
        """
        await self._cancel_background_tasks()
        await self._shutdown_executor()

    def _create_executor(self) -> concurrent.futures.ThreadPoolExecutor:
        """CROSS-001: 创建独立的 ThreadPoolExecutor

        子类可覆盖 _executor_max_workers 和 _executor_name_prefix 来自定义
        """
        # FIXED-P0: 创建新executor时重置关闭标志，防止驱动重启后所有同步调用静默返回None
        self._executor_shutting_down = False
        self._shutdown_requested.clear()
        return concurrent.futures.ThreadPoolExecutor(
            max_workers=self._executor_max_workers, thread_name_prefix=f"{self._executor_name_prefix}_"
        )

    def _register_task(self, coro, name: str | None = None) -> asyncio.Task:
        """CROSS-004: 创建并注册后台任务，自动追踪引用

        Args:
            coro: 协程对象
            name: 可选的任务名称

        Returns:
            创建的 asyncio.Task 对象
        """
        task = asyncio.create_task(coro, name=name)
        task.add_done_callback(self._background_tasks.discard)
        self._background_tasks.add(task)
        return task

    async def _cancel_background_tasks(self) -> None:
        """CROSS-004: 取消所有后台任务并等待完成"""
        if not self._background_tasks:
            return

        for task in self._background_tasks:
            if not task.done():
                task.cancel()

        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)

        self._background_tasks.clear()

    async def _run_in_executor(self, func: Callable, *args: Any, timeout: float | None = 10.0) -> Any:
        """BASE-MED-001: 在独立线程池中执行同步函数，带超时保护和 Future 追踪

        超时机制：
        - 使用共享的 _shutdown_requested 事件标志来通知线程停止
        - 包装函数会定期检查该标志，如果已设置则立即返回
        - 避免了 asyncio.wait_for 取消后线程仍在运行的问题

        Args:
            func: 要执行的同步函数
            *args: 函数参数
            timeout: 超时时间（秒），默认 10 秒

        Returns:
            函数返回值

        Raises:
            asyncio.TimeoutError: 执行超时
        """
        # FIXED-P0: 线程池懒创建加锁保护，防止并发调用导致Executor泄漏
        if self._executor is None:
            async with self._executor_lock:
                if self._executor is None:
                    self._executor = self._create_executor()

        loop = asyncio.get_running_loop()
        future: asyncio.Future | None = None

        def _wrapped_func():
            """包装函数：检查 shutdown 标志，避免线程残留"""
            # FIXED-P1: executor关闭时抛出异常而非静默返回None，让调用方能区分"任务被丢弃"和"正常返回None"
            if self._executor_shutting_down:
                raise RuntimeError("executor is shutting down, task rejected")
            return func(*args)  # FIXED-P0: 移除未定义的kwargs引用，_run_in_executor签名无**kwargs

        try:
            if timeout:
                # 提交任务到线程池并追踪 Future
                future = loop.run_in_executor(self._executor, _wrapped_func)
                async with self._executor_lock:
                    self._executor_futures.add(future)
                    # FIXED-P1: 检查 _executor_futures 集合大小，超过阈值时告警
                    if len(self._executor_futures) > self._executor_futures_warn_threshold:
                        logger.warning(
                            "[driver] BASE-MED-001: executor_futures size=%d exceeds threshold=%d, "
                            "possible future leak (func=%s)",
                            len(self._executor_futures),
                            self._executor_futures_warn_threshold,
                            func.__name__ if hasattr(func, "__name__") else str(func),
                        )
                try:
                    result: Any = await asyncio.wait_for(future, timeout=timeout)
                    return result
                except TimeoutError:
                    func_name = func.__name__ if hasattr(func, "__name__") else str(func)
                    logger.warning(
                        "[driver] BASE-MED-001: executor timeout after %.1fs, "
                        "rebuilding executor to prevent deadlock: func=%s",
                        timeout,
                        func_name,
                    )
                    # FIXED-P2: 超时后重建executor，不再设置_shutdown_requested以避免并发任务被静默丢弃
                    # 原问题：set()后并发协程的_wrapped_func检测到is_set()直接return None
                    async with self._executor_lock:
                        old_executor = self._executor
                        self._executor = self._create_executor()
                        # FIXED-P2: 仅清除当前超时的Future，保留其他并发调用的Future追踪
                        self._executor_futures.discard(future)
                    if old_executor:
                        with contextlib.suppress(Exception):
                            old_executor.shutdown(wait=False, cancel_futures=True)
                    raise
            else:
                future = loop.run_in_executor(self._executor, _wrapped_func)
                async with self._executor_lock:
                    self._executor_futures.add(future)
                    # FIXED-P1: 检查 _executor_futures 集合大小，超过阈值时告警
                    if len(self._executor_futures) > self._executor_futures_warn_threshold:
                        logger.warning(
                            "[driver] BASE-MED-001: executor_futures size=%d exceeds threshold=%d, "
                            "possible future leak (func=%s)",
                            len(self._executor_futures),
                            self._executor_futures_warn_threshold,
                            func.__name__ if hasattr(func, "__name__") else str(func),
                        )
                result = await future
                return result
        except TimeoutError:
            raise
        except Exception:
            raise
        finally:
            # 完成后从追踪集合中移除 Future
            if future is not None:
                async with self._executor_lock:
                    self._executor_futures.discard(future)

    async def _shutdown_executor(self) -> None:
        """BASE-MED-001: 安全关闭线程池

        关闭流程：
        1. 设置 _shutdown_requested 事件，通知所有运行中的线程停止
        2. 等待最多 _executor_shutdown_timeout 秒让线程自然结束
        3. 强制 shutdown executor（不等待）
        4. 清理 Future 追踪集合
        """
        self._shutdown_requested.set()
        # FIXED-P2: 标记executor正在关闭，阻止新任务提交到旧线程
        self._executor_shutting_down = True

        if self._executor is not None:
            try:
                # FIXED-P2: 轮询检查executor是否空闲，空闲则立即继续，而非无条件等待10秒
                # 之前：无条件sleep(10秒)，即使没有正在运行的任务也等待
                # 之后：每0.5秒检查一次executor是否空闲，空闲则立即继续
                elapsed = 0.0
                poll_interval = 0.5
                while elapsed < self._executor_shutdown_timeout:
                    await asyncio.sleep(poll_interval)
                    elapsed += poll_interval
                    if not self._executor_futures:
                        break
                    # 检查所有future是否已完成
                    all_done = all(f.done() for f in self._executor_futures)
                    if all_done:
                        break
            except asyncio.CancelledError:
                pass

        # FIXED-P1: BASE-01 强制shutdown放入try/finally确保CancelledError不跳过
        try:
            if self._executor:
                try:
                    self._executor.shutdown(wait=False, cancel_futures=True)
                    logger.debug("[driver] BASE-MED-001: executor shutdown completed")
                except Exception as e:
                    logger.debug("[driver] executor shutdown error: %s", e)

            async with self._executor_lock:
                self._executor = None
                # FIXED-P1: 清理前强制cancel所有未完成的pending future，防止资源泄漏
                # 原问题：仅clear()集合而不cancel future，已提交但未运行的future可能仍占用线程池资源
                # 修复：遍历所有未done的future显式调用cancel()，再清空集合
                cancelled_count = 0
                for fut in self._executor_futures:
                    if not fut.done():
                        with contextlib.suppress(Exception):
                            if fut.cancel():
                                cancelled_count += 1
                if cancelled_count > 0:
                    logger.info(
                        "[driver] BASE-MED-001: cancelled %d pending executor futures during shutdown",
                        cancelled_count,
                    )
                self._executor_futures.clear()
        except asyncio.CancelledError:
            if self._executor:
                with contextlib.suppress(Exception):
                    self._executor.shutdown(wait=False, cancel_futures=True)
                self._executor = None
            # FIXED-P1: CancelledError路径同样强制cancel所有pending future
            for fut in self._executor_futures:
                if not fut.done():
                    with contextlib.suppress(Exception):
                        fut.cancel()
            self._executor_futures.clear()
            raise

    def _handle_watchdog_exception(self, exc: Exception, context: str = "watchdog") -> bool:
        """CROSS-002: 分级处理 watchdog 循环中的异常

        Args:
            exc: 捕获的异常
            context: 上下文描述，用于日志

        Returns:
            True 如果异常被处理（应该继续运行），False 如果应该停止

        异常分级：
        - asyncio.CancelledError: 正常取消，记录 info
        - ConnectionError, OSError: 连接错误，记录 warning
        - 其他异常: 记录 error 并包含堆栈，触发健康统计
        """
        import time
        import traceback

        if isinstance(exc, asyncio.CancelledError):
            logger.info("[driver] %s cancelled normally", context)
            return False

        # 跟踪连续异常
        now = time.time()
        # FIXED-P2: BASE-03 使用deque(maxlen=60)+Lock保护，替代无锁list
        with self._watchdog_history_lock:
            self._watchdog_exception_history.append((now, type(exc)))
            recent_count = len(self._watchdog_exception_history)

        # 检查是否是已知的连接类异常
        known_connection_errors = (
            ConnectionError,
            ConnectionRefusedError,
            ConnectionResetError,
            BrokenPipeError,
            TimeoutError,
            OSError,
        )

        if isinstance(exc, known_connection_errors):
            if recent_count >= 10:
                logger.critical(
                    "[driver] %s: too many connection errors (%d in last 60s), driver may be unhealthy: %s",
                    context,
                    recent_count,
                    exc,
                )
                self._record_driver_failure(context)
            else:
                logger.warning("[driver] %s: connection error (count=%d in 60s): %s", context, recent_count, exc)
            return True

        # 未知异常 - 记录错误并包含堆栈
        if recent_count >= 10:
            logger.critical(
                "[driver] %s: too many exceptions (%d in last 60s), driver may be unhealthy:\n%s\n%s",
                context,
                recent_count,
                exc,
                traceback.format_exc(),
            )
            self._record_driver_failure(context)
        else:
            logger.error(
                "[driver] %s: unexpected error (count=%d in 60s): %s\n%s",
                context,
                recent_count,
                exc,
                traceback.format_exc(),
            )
            self._record_driver_failure(context)

        return True

    def _record_driver_failure(self, context: str) -> None:
        """CROSS-002: 记录驱动失败到健康统计"""
        # FIXED-P1: 与_record_read_success/failure锁保护一致
        with self._stats_lock:
            # FIXED-P1: context可能是设备ID或描述字符串，若不是设备ID则递增所有设备
            if hasattr(self, "_health_stats"):
                device_id = context
                if device_id in self._health_stats:
                    stats = self._health_stats[device_id]
                    stats.consecutive_failures += 1
                    stats.total_failures += 1
                else:
                    # 驱动级故障影响所有设备
                    for _did, stats in self._health_stats.items():
                        stats.consecutive_failures += 1
                        stats.total_failures += 1

    @abstractmethod
    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        """读取测点值，返回 {point_name: value}"""

    @abstractmethod
    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        """写入测点值"""

    async def write_points_batch(self, device_id: str, points: dict[str, Any]) -> dict[str, bool]:
        """批量写入多个测点（可选实现）。

        默认实现：逐点调用 write_point()，子类可覆盖优化。

        Args:
            device_id: 设备ID
            points: 测点名到值的字典 {point_name: value}

        Returns:
            {point_name: success} - 每个测点的写入结果
        """
        results = {}
        for point_name, value in points.items():
            try:
                results[point_name] = await self.write_point(device_id, point_name, value)
            except Exception as e:
                # FIXED-P2: 不吞没取消和系统中断异常
                if isinstance(e, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                    raise
                logger.warning(
                    "write_points_batch failed for %s: %s", point_name, e
                )  # FIXED-P2: debug→warning，写入失败应可观测
                results[point_name] = False
        return results

    async def discover_devices(self, config: dict) -> list[dict]:
        """发现设备（可选实现）"""
        return []

    # FIXED: 原问题-add_device使用NotImplementedError而非@abstractmethod，子类未实现时不在实例化阶段报错
    # add_device 保持为可选方法（非 abstractmethod），但改用更明确的文档说明
    async def add_device(self, device_id: str, config: dict, points: list[dict] | None = None) -> None:
        """添加设备到驱动实例（可选实现）。未实现时抛出 NotImplementedError。"""
        raise NotImplementedError(f"{self.__class__.__name__} does not implement add_device")

    def is_device_connected(self, device_id: str) -> bool:
        """检查设备是否已连接（可选实现）。

        默认返回 True：驱动 start() 成功即视为设备在线，
        子类可覆写此方法实现精确的连通性检测。

        注意：子类覆写时请优先检查自身设备状态，
        基类仅提供"驱动未启动时返回False"的兜底保护。
        """
        if not getattr(self, "_running", False):
            return False  # FIXED-P2: 驱动未启动时返回False，避免上层误判设备在线
        # FIXED-P1: 使用_conn_state_lock保护_connection_statuses并发读取
        with self._conn_state_lock:
            conn_status = self._connection_statuses.get(device_id)
        if conn_status is not None:
            return conn_status.state == ConnectionState.CONNECTED.value
        return True

    def on_data(self, callback: Callable) -> None:
        """注册数据回调（可选，用于推送型协议如MQTT）。子类如需支持推送，应覆盖此方法保存callback。"""
        self._data_callback = callback

    @property
    def is_running(self) -> bool:
        """驱动是否运行中"""
        return getattr(self, "_running", False)

    # ─── 心跳检测与健康状态 ───

    def get_health_stats(self, device_id: str) -> DriverHealthStats | None:
        """获取设备健康状态统计"""
        with self._stats_lock:  # FIXED-P2: 与写入路径锁保护一致
            return self._health_stats.get(device_id)

    def get_all_health_stats(self) -> dict[str, DriverHealthStats]:
        """获取所有设备的健康状态统计"""
        with self._stats_lock:  # FIXED-P2: 与写入路径锁保护一致
            return dict(self._health_stats)

    def reset_health_stats(self, device_id: str) -> None:
        """重置设备健康统计"""
        with self._stats_lock:  # FIXED-P2: 与写入路径锁保护一致
            self._health_stats.pop(device_id, None)
            # FIXED-P1: BASE-R01 _offline_since.pop移入锁内，与_record_read_success锁内访问一致
            self._offline_since.pop(device_id, None)
        # FIXED-P2: 清理所有设备级字典条目，防止设备移除后内存泄漏
        # FIXED-P1: 熔断状态字典清理纳入_circuit_lock，与_check_circuit_breaker/_record_circuit_failure锁保护一致
        with self._circuit_lock:
            self._circuit_states.pop(device_id, None)
            self._circuit_open_sinces.pop(device_id, None)
            self._half_open_calls.pop(device_id, None)
        with self._stats_lock:  # FIXED-P0: 设备级字典弹出纳入锁保护，与异步读写路径一致
            self._connection_statuses.pop(device_id, None)
            self._reconnect_state.pop(device_id, None)
            self._device_configs.pop(device_id, None)

    async def _record_read_success(self, device_id: str) -> None:
        """记录读取成功"""
        with self._stats_lock:  # FIXED-P0: 并发访问保护
            stats = self._health_stats.setdefault(device_id, DriverHealthStats(device_id=device_id))
            stats.total_reads += 1
            stats.last_success_read = datetime.now(UTC)
            stats.consecutive_failures = 0
            if device_id in self._offline_since:
                offline_since = self._offline_since.pop(device_id)
                if offline_since is not None:
                    offline_duration = (datetime.now(UTC) - offline_since).total_seconds()
                    stats.total_downtime_seconds += max(0.0, offline_duration)
                stats.last_offline_at = None
            self._update_connection_quality(stats)
        await self._record_circuit_success(device_id)  # FIXED-P0: 传入device_id，设备级熔断
        self._evaluate_degradation(device_id)

    def _record_read_failure(self, device_id: str) -> None:
        """记录读取失败"""
        with self._stats_lock:  # FIXED-P0: 并发访问保护
            stats = self._health_stats.setdefault(device_id, DriverHealthStats(device_id=device_id))
            stats.total_reads += 1
            stats.failed_reads += 1
            stats.last_failed_read = datetime.now(UTC)
            stats.consecutive_failures += 1
            if device_id not in self._offline_since:
                self._offline_since[device_id] = datetime.now(UTC)
                stats.last_offline_at = datetime.now(UTC)
            self._update_connection_quality(stats)
        # FIXED-P0: 同步方法中安全调度异步任务，传入device_id实现设备级熔断
        try:
            loop = asyncio.get_running_loop()
            # FIXED-P2: 追踪后台任务，防止异常被静默吞没
            task = loop.create_task(self._record_circuit_failure(device_id))
            self._background_tasks.add(task)

            def _on_done(t: asyncio.Task, _tasks=self._background_tasks) -> None:
                _tasks.discard(t)
                if not t.cancelled() and t.exception():
                    logger.error("[driver] _record_circuit_failure task failed: %s", t.exception())

            task.add_done_callback(_on_done)
        except RuntimeError:
            # FIXED-P0: 无事件循环时直接同步更新熔断状态，避免熔断器失效
            # FIXED-P0: 原问题-同步回退路径先获取_circuit_lock再获取_stats_lock，与异步路径顺序相反构成ABBA死锁；
            # 统一锁获取顺序为_stats_lock→_circuit_lock，先复制数据再获取_circuit_lock
            with self._stats_lock:
                cur_stats: DriverHealthStats | None = self._health_stats.get(device_id)
                consecutive = cur_stats.consecutive_failures if cur_stats else 0
            with self._circuit_lock:
                state = self._circuit_states.get(device_id, "closed")
                # FIXED-P2: 同步回退路径补充half_open→open转换，与异步路径_record_circuit_failure一致
                if state == "half_open" or (state == "closed" and consecutive >= self._failure_threshold):
                    self._circuit_states[device_id] = "open"
                    self._circuit_open_sinces[device_id] = datetime.now(UTC)
        self._evaluate_degradation(device_id)

    def _record_write_success(self, device_id: str) -> None:
        """记录写入成功"""
        with self._stats_lock:  # FIXED-P0: 并发访问保护，与_read_success/failure一致
            stats = self._health_stats.setdefault(device_id, DriverHealthStats(device_id=device_id))
            stats.total_writes += 1

    def _record_write_failure(self, device_id: str) -> None:
        """记录写入失败"""
        with self._stats_lock:  # FIXED-P0: 并发访问保护，与_read_success/failure一致
            stats = self._health_stats.setdefault(device_id, DriverHealthStats(device_id=device_id))
            stats.total_writes += 1
            stats.failed_writes += 1

    def _update_connection_quality(self, stats: DriverHealthStats) -> None:
        """更新连接质量评分

        评分算法：
        - 基础分: 100
        - 每次连续失败: -10
        - 错误率每增加1%: -2
        - 最低0分
        """
        score = 100.0
        score -= min(stats.consecutive_failures * 10, 50)  # 连续失败最多扣50分
        score -= min(stats.read_error_rate * 200, 50)  # 错误率最多扣50分
        stats.connection_quality_score = max(0.0, score)

    def get_connection_quality(self, device_id: str) -> float:
        """获取设备连接质量评分 (0-100)"""
        with self._stats_lock:  # FIXED-P1: 与写入路径锁保护一致
            stats = self._health_stats.get(device_id)
            return stats.connection_quality_score if stats else 100.0

    async def health_check(self, device_id: str) -> bool:
        """执行设备健康检查（可选实现）。

        子类可覆盖此方法实现自定义的健康检查逻辑，
        例如发送心跳包、读取测试寄存器等。

        默认实现：检查 is_device_connected() 状态。

        Returns:
            True 表示设备健康，False 表示设备异常
        """
        return self.is_device_connected(device_id)

    async def reconnect(self, device_id: str) -> bool:
        """重连设备（非抽象，默认实现）。

        默认实现仅记录警告，子类应覆盖此方法实现协议特定的重连逻辑。
        重连操作只应影响指定设备，不应停止整个驱动。

        Args:
            device_id: 设备ID

        Returns:
            True 表示重连成功，False 表示重连失败
        """
        logger.warning(
            "reconnect() not implemented by %s for device %s; subclass should implement device-specific reconnect",
            self.__class__.__name__,
            device_id,
        )
        return False

    async def reset_reconnect_state(self, device_id: str) -> None:
        """重置设备重连状态，允许重新尝试重连"""
        async with self._reconnect_lock:
            self._reconnect_state.pop(device_id, None)

    async def reconnect_with_backoff(
        self,
        device_id: str,
        base: float = 5.0,
        max_delay: float = 60.0,
        max_attempts: int = 3,
    ) -> bool:
        """指数退避重连策略。

        重连间隔: min(base × 2^attempt, max_delay)
        默认 base=5s, max=60s → 5s, 10s, 20s

        达到 max_attempts 后标记设备 OFFLINE，
        可通过 reset_reconnect_state() 重置后重新尝试。

        Args:
            device_id: 设备ID
            base: 基础退避间隔(秒)
            max_delay: 最大退避间隔(秒)
            max_attempts: 最大重连尝试次数

        Returns:
            True 表示最终重连成功，False 表示所有尝试均失败
        """
        # FIXED-P0: 将 sleep 移到锁外，避免一个设备退避等待阻塞所有设备重连
        async with self._reconnect_lock:
            state = self._reconnect_state.setdefault(
                device_id,
                {
                    "attempt": 0,
                    "base": base,
                    "max_delay": max_delay,
                },
            )
            attempt = state["attempt"]

            if attempt >= max_attempts:
                logger.warning(
                    "Max reconnect attempts reached for device %s (%d), marking offline",
                    device_id,
                    max_attempts,
                )
                await self._set_connection_state(
                    device_id, ConnectionState.OFFLINE.value, "max reconnect attempts reached"
                )
                return False

            delay = min(state["base"] * (2**attempt), state["max_delay"])
            delay *= (
                (1.0 - _RC_JITTER_FACTOR) + random.random() * _RC_JITTER_FACTOR
            )  # FIXED-P4: 原问题-退避无抖动，多设备同时重连惊群效应；添加jitter分散重试时间
            # 预递增尝试计数，防止并发重入重复计数
            state["attempt"] = attempt + 1
            logger.info(
                "Reconnect with backoff: device=%s, attempt=%d, delay=%.1fs",
                device_id,
                attempt,
                delay,
            )

        # 退避等待在锁外执行，允许其他设备并发重连
        await asyncio.sleep(delay)

        try:
            success = await self.reconnect(device_id)
            if success:
                async with self._reconnect_lock:
                    if device_id in self._reconnect_state:
                        self._reconnect_state[device_id]["attempt"] = 0
                return True
            return False
        except Exception as e:
            logger.error("Reconnect with backoff exception for %s: %s", device_id, e)
            return False

    async def _check_circuit_breaker(self, device_id: str = "") -> bool:
        """检查熔断器是否允许请求通过。

        FIXED-P0: 改为设备级熔断，每个设备独立状态

        Returns:
            True 表示允许通过，False 表示熔断器打开拒绝请求
        """

        # FIXED-P0: 原问题-async方法中直接with threading.Lock，若锁被线程池持有则阻塞事件循环；
        # 改为asyncio.to_thread执行锁内操作，避免事件循环阻塞
        def _check_sync():
            with self._circuit_lock:
                circuit_state = self._circuit_states.get(device_id, "closed")
                if circuit_state == "closed":
                    return True
                if circuit_state == "open":
                    open_since = self._circuit_open_sinces.get(device_id)
                    if open_since is None:
                        return True
                    elapsed = (datetime.now(UTC) - open_since).total_seconds()
                    if elapsed >= self._recovery_timeout:
                        self._circuit_states[device_id] = "half_open"
                        # FIXED-P1: 转换时本次请求计入half_open配额，避免允许请求数比配置多1个
                        self._half_open_calls[device_id] = 1
                        logger.info("Circuit breaker transitioned to half_open for device %s", device_id)
                        return True
                    return False
                if circuit_state == "half_open":
                    calls = self._half_open_calls.get(device_id, 0)
                    if calls < self._half_open_max_calls:
                        self._half_open_calls[device_id] = calls + 1
                        return True
                    return False
                return True

        return await asyncio.to_thread(_check_sync)

    async def _record_circuit_success(self, device_id: str = "") -> None:
        """记录熔断器成功调用"""

        # FIXED-P0: 同_check_circuit_breaker，async方法中threading.Lock改为asyncio.to_thread
        def _success_sync():
            with self._circuit_lock:
                circuit_state = self._circuit_states.get(device_id, "closed")
                if circuit_state == "half_open":
                    self._circuit_states[device_id] = "closed"
                    self._circuit_open_sinces[device_id] = None
                    logger.info("Circuit breaker closed after successful call for device %s", device_id)

        await asyncio.to_thread(_success_sync)

    async def _record_circuit_failure(self, device_id: str = "") -> None:
        """记录熔断器失败调用"""
        # FIXED-P0: 先在_stats_lock内复制所需数据，再在锁外获取_circuit_lock，避免嵌套锁ABBA死锁
        with self._stats_lock:
            stats = self._health_stats.get(device_id)
            consecutive_failures = stats.consecutive_failures if stats else 0

        # FIXED-P0: 同_check_circuit_breaker，async方法中threading.Lock改为asyncio.to_thread
        def _failure_sync():
            with self._circuit_lock:
                circuit_state = self._circuit_states.get(device_id, "closed")
                if circuit_state == "half_open":
                    self._circuit_states[device_id] = "open"
                    self._circuit_open_sinces[device_id] = datetime.now(UTC)
                    logger.warning("Circuit breaker reopened from half_open for device %s", device_id)
                    return
                if consecutive_failures >= self._failure_threshold:
                    self._circuit_states[device_id] = "open"
                    self._circuit_open_sinces[device_id] = datetime.now(UTC)
                    logger.warning(
                        "Circuit breaker opened for device %s after %d consecutive failures",
                        device_id,
                        self._failure_threshold,
                    )

        await asyncio.to_thread(_failure_sync)

    def _evaluate_degradation(self, device_id: str) -> None:
        """评估并更新设备DEGRADED状态"""
        target_state = None
        degradation_reason = ""
        with self._stats_lock:  # FIXED-P1: 与写入路径锁保护一致
            stats = self._health_stats.get(device_id)
            if not stats:
                return
            if stats.connection_quality_score < 50:
                if stats.degradation_reason == "":
                    if stats.consecutive_failures >= 5:
                        stats.degradation_reason = "consecutive_failures>=5"
                    elif stats.read_error_rate > 0.1:
                        stats.degradation_reason = "read_error_rate>10%"
                    else:
                        stats.degradation_reason = "quality_score<50"
                target_state = ConnectionState.DEGRADED.value
                degradation_reason = stats.degradation_reason
            elif stats.degradation_reason:
                degradation_reason = ""
                stats.degradation_reason = ""
                if stats.consecutive_failures == 0:
                    target_state = ConnectionState.CONNECTED.value
        # BUG-003: 将create_task移到锁外，减少锁持有时间，避免高频失败时大量Task快速创建
        if target_state:
            try:
                loop = asyncio.get_running_loop()
                task = loop.create_task(
                    self._set_connection_state(
                        device_id,
                        target_state,
                        degradation_reason,
                    )
                )
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
            except RuntimeError:
                logger.warning("_evaluate_degradation: cannot schedule _set_connection_state, no running event loop")

    def _apply_deadband(self, new_value: Any, last_value: Any, deadband: float | dict | None = None) -> Any:
        if deadband is None or last_value is None or new_value is None:
            return new_value
        if not isinstance(new_value, (int, float)) or not isinstance(last_value, (int, float)):
            return new_value
        threshold: float = 0.0
        if isinstance(deadband, dict):
            db_type = deadband.get("type", "absolute")
            db_threshold = deadband.get("threshold", 0.0)
            # FIXED-P4: 百分比模式threshold=0时视为禁用死区
            if db_type == "percent" and db_threshold <= 0.0:
                return new_value
            if db_type == "percent" and last_value == 0:
                return new_value
            if db_type == "percent" and last_value != 0:
                threshold = abs(last_value) * (db_threshold / 100.0)
            else:
                threshold = db_threshold
        else:
            threshold = deadband
        if abs(new_value - last_value) < threshold:
            return last_value
        return new_value

    def _apply_scaling(self, value: Any, scaling: dict | None = None) -> Any:
        if scaling is None or value is None:
            return value
        if isinstance(value, (int, float)):
            ratio = scaling.get("ratio", 1.0)
            offset = scaling.get("offset", 0.0)
            return value * ratio + offset
        return value

    def _apply_clamp(self, value: Any, clamp: dict | None = None) -> tuple[Any, bool]:
        if clamp is None or value is None:
            return value, True
        if isinstance(value, (int, float)):
            min_val = clamp.get("min")
            max_val = clamp.get("max")
            # FIXED-P4: 越界时返回裁剪值而非None，防止空值传播
            if min_val is not None and value < min_val:
                return (min_val, False)
            if max_val is not None and value > max_val:
                return (max_val, False)
        return value, True

    # ─── 能力与连接状态 ───

    def get_capabilities(self) -> DriverCapabilities:
        """获取驱动能力声明"""
        return self._capabilities

    def get_connection_status(self, device_id: str) -> ConnectionStatus:
        """获取设备连接状态"""
        # FIXED-P1: 使用_conn_state_lock保护_connection_statuses并发读取
        with self._conn_state_lock:
            if device_id in self._connection_statuses:
                return self._connection_statuses[device_id]
        connected = self.is_device_connected(device_id)
        state = ConnectionState.CONNECTED.value if connected else ConnectionState.DISCONNECTED.value
        return ConnectionStatus(state=state, since=datetime.now(UTC))

    async def _set_connection_state(self, device_id: str, state: str, reason: str = "") -> bool:
        with self._conn_state_lock:  # FIXED-P1: RLock保护_connection_statuses原子更新（临界区无await）
            existing = self._connection_statuses.get(device_id)
            current_state = existing.state if existing else None
            if current_state is not None:
                allowed = self._VALID_STATE_TRANSITIONS.get(current_state)
                if allowed is not None and state not in allowed:
                    logger.warning(
                        "Invalid state transition for device %s: %s -> %s, rejected",
                        device_id,
                        current_state,
                        state,
                    )
                    return False
            last_error = (
                reason
                if state
                in (
                    ConnectionState.DISCONNECTED.value,
                    ConnectionState.OFFLINE.value,
                    ConnectionState.DEGRADED.value,
                )
                else (existing.last_error if existing else "")
            )
            self._connection_statuses[device_id] = ConnectionStatus(
                state=state,
                reason=reason,
                since=datetime.now(UTC),
                last_error=last_error,
            )
            return True

    # ─── 配置校验 ───

    def validate_config(self, config: dict) -> ConfigValidationResult:
        """基础配置校验。

        检查 config_schema 中声明的必填字段，以及常见的 IP / port / URL 格式。
        子类可覆盖此方法以添加协议特定的校验逻辑。
        """
        errors: list[str] = []
        warnings: list[str] = []

        required_fields = self.config_schema.get("required", [])
        for field_name in required_fields:
            if field_name not in config:
                errors.append(f"Missing required field: {field_name}")

        ip_keys = ["host", "ip", "address", "server"]
        ip_pattern = re.compile(
            r"^(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}"
            r"(?:25[0-5]|2[0-4]\d|[01]?\d\d?)$"
        )
        for key in ip_keys:
            if key in config and config[key] and not ip_pattern.match(str(config[key])):
                warnings.append(f"Field '{key}' does not look like a valid IPv4 address")

        port_keys = ["port"]
        for key in port_keys:
            if key in config:
                try:
                    port = int(config[key])
                    if not (1 <= port <= 65535):
                        errors.append(f"Field '{key}' must be between 1 and 65535, got {port}")
                except (ValueError, TypeError):
                    errors.append(f"Field '{key}' must be an integer, got {config[key]!r}")

        url_keys = ["url", "endpoint", "broker", "webhook_url"]
        url_pattern = re.compile(r"^https?://[\w.-]+(:\d+)?(/.*)?$")
        for key in url_keys:
            if key in config and config[key]:
                if not url_pattern.match(str(config[key])):
                    warnings.append(f"Field '{key}' does not look like a valid URL")

        properties = self.config_schema.get("properties", {})
        for field_name, prop in properties.items():
            if field_name not in config:
                continue
            value = config[field_name]
            if "minimum" in prop and isinstance(value, (int, float)):
                if value < prop["minimum"]:
                    errors.append(f"Field '{field_name}' must be >= {prop['minimum']}, got {value}")
            if "maximum" in prop and isinstance(value, (int, float)):
                if value > prop["maximum"]:
                    errors.append(f"Field '{field_name}' must be <= {prop['maximum']}, got {value}")
            if "enum" in prop and value not in prop["enum"]:
                errors.append(f"Field '{field_name}' must be one of {prop['enum']}, got {value!r}")

        return ConfigValidationResult(
            valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
        )

    # ─── 写入策略 ───

    def get_write_policy(self, device_id: str) -> dict:
        """获取设备写入策略。

        子类可覆盖以返回协议 / 设备特定的写入策略。
        """
        return {
            "write_enabled": True,
            "whitelist_mode": False,
            "whitelisted_points": [],
        }

    def check_write_allowed(self, device_id: str, point: str) -> bool:
        """检查是否允许写入指定测点。

        如果白名单模式开启，则只允许白名单内的测点；
        否则根据 capabilities.write 决定。
        """
        if not self._capabilities.write:
            return False
        policy = self.get_write_policy(device_id)
        if not policy.get("write_enabled", True):
            return False
        if policy.get("whitelist_mode", False):
            return point in policy.get("whitelisted_points", [])
        return True

    async def check_permission(self, permission: Any) -> bool:
        """默认写入权限检查。

        SEC-FIX(修复2): 为未实现角色锁的驱动（simulator/modbus_slave/s7/opcua 等）
        提供统一的权限检查入口，使驱动层 write_point 可一致调用，防止内部服务绕过
        API 层鉴权直接写入。无 _current_user_role 属性时放行（兼容测试/模拟驱动）；
        子类可覆盖以实现严格角色锁（见 modbus_tcp/modbus_rtu）。
        """
        role = getattr(self, "_current_user_role", None)
        if role is None:
            return True
        try:
            from edgelite.security.rbac import has_permission

            return has_permission(role, permission)
        except ImportError:
            return False

    # ─── 可观测性指标 ───

    def get_observability_metrics(self, device_id: str) -> dict:
        """获取设备可观测性指标"""
        with self._stats_lock:  # FIXED-P1: 与写入路径锁保护一致
            stats = self._health_stats.get(device_id)
            if stats is None:
                return {
                    "read_error_rate": 0.0,
                    "write_error_rate": 0.0,
                    "consecutive_failures": 0,
                    "connection_quality_score": 100.0,
                    "total_downtime_seconds": 0.0,
                    "last_online_at": None,
                    "last_offline_at": None,
                    "avg_latency_ms": 0.0,
                    "reconnect_count": 0,
                }
            return {
                "read_error_rate": stats.read_error_rate,
                "write_error_rate": stats.write_error_rate,
                "consecutive_failures": stats.consecutive_failures,
                "connection_quality_score": stats.connection_quality_score,
                "total_downtime_seconds": stats.total_downtime_seconds,
                "last_online_at": stats.last_online_at,
                "last_offline_at": stats.last_offline_at,
                "avg_latency_ms": stats.avg_latency_ms,
                "reconnect_count": stats.total_reconnects,
            }


class DriverExceptionMapper:
    """驱动异常映射工具类，将常见异常映射为标准化错误码"""

    _EXCEPTION_MAP: dict[type[Exception], str] = {
        ConnectionRefusedError: "ERR_NETWORK_CONNECTION_REFUSED",
        TimeoutError: "ERR_NETWORK_TIMEOUT",
        asyncio.TimeoutError: "ERR_NETWORK_TIMEOUT",
        NotImplementedError: "ERR_DRIVER_NOT_FOUND",
        PermissionError: "ERR_AUTH_PERMISSION_DENIED",
    }

    @staticmethod
    def map_exception(exc: Exception, protocol: str = "") -> str:
        """将异常映射为标准化错误码。

        Args:
            exc: 捕获的异常
            protocol: 协议名称（可选，用于日志上下文）

        Returns:
            标准化错误码字符串
        """
        if isinstance(exc, ConnectionRefusedError):
            return "ERR_NETWORK_CONNECTION_REFUSED"
        if isinstance(exc, ConnectionResetError):
            return "ERR_NETWORK_CONNECTION_REFUSED"
        if isinstance(exc, ConnectionAbortedError):
            return "ERR_NETWORK_CONNECTION_REFUSED"
        if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
            return "ERR_NETWORK_TIMEOUT"
        if isinstance(exc, OSError):
            msg = str(exc).lower()
            if "dns" in msg:
                return "ERR_NETWORK_DNS_FAILED"
            if "network" in msg or "unreachable" in msg or "no route" in msg:
                return "ERR_NETWORK_HOST_UNREACHABLE"
            return "ERR_NETWORK_HOST_UNREACHABLE"
        if isinstance(exc, NotImplementedError):
            return "ERR_DRIVER_NOT_FOUND"
        if isinstance(exc, ValueError):
            msg = str(exc).lower()
            if "config" in msg:
                return "ERR_DEVICE_CONFIG_INVALID"
            return "ERR_DEVICE_CONFIG_INVALID"
        if isinstance(exc, PermissionError):
            return "ERR_AUTH_PERMISSION_DENIED"
        return "ERR_COMMON_INTERNAL_ERROR"
