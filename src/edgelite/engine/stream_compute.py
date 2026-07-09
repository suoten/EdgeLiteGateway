"""边缘流计算引擎 - CEP复杂事件处理引擎

边缘流计算提供实时数据处理能力，支持：
- 时间窗口聚合 (滚动窗口/滑动窗口/会话窗口)
- 滑动统计 (移动平均/移动极值/移动方差)
- 模式检测 (上升沿/下降沿/变化率/异常突增)
- 流式计算算子 (filter/map/aggregate/window/join)
- 多流关联 (基于时间窗口的流join)
- 动态规则热更新
- 与Scheduler集成 - 自动订阅事件总线
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from edgelite.engine.event_bus import EventBus, PointUpdateEvent, StreamResultEvent

try:
    from edgelite.api.debug import record_packet
except ImportError:
    record_packet = None

logger = logging.getLogger(__name__)


@dataclass
class StreamEvent:
    """流事件"""
    device_id: str
    point_name: str
    value: float
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    quality: str = "good"


@dataclass
class WindowResult:
    """窗口计算结果"""
    window_id: str
    window_type: str  # tumbling/sliding/session
    start_time: datetime
    end_time: datetime
    point_name: str
    aggregate: str  # avg/sum/min/max/count/std
    value: float
    count: int


@dataclass
class PatternMatch:
    """模式匹配结果"""
    pattern_id: str
    matched: bool
    confidence: float
    details: dict


class TumblingWindow:
    """滚动窗口 - 数据不重叠"""

    def __init__(self, size_seconds: float):
        self._size = size_seconds
        self._buffer: deque[StreamEvent] = deque(maxlen=int(size_seconds * 1000))  # FIXED-P2: W15 提高maxlen倍率到1000点/秒
        self._window_start: float | None = None
        self._maxlen_warned: bool = False

    def add(self, event: StreamEvent) -> list[StreamEvent]:
        """添加事件，返回过期事件"""
        expired = []
        now_ts = event.timestamp.timestamp()

        if self._window_start is None:
            self._window_start = now_ts

        if len(self._buffer) >= self._buffer.maxlen and not self._maxlen_warned:  # FIXED-P2: W15 缓冲区满时告警
            logger.warning("TumblingWindow buffer full (maxlen=%d), data may be dropped", self._buffer.maxlen)
            self._maxlen_warned = True
        self._buffer.append(event)

        # 检查窗口是否结束
        if now_ts - self._window_start >= self._size:
            expired = list(self._buffer)
            self._buffer.clear()
            self._window_start = now_ts

        return expired


class SlidingWindow:
    """滑动窗口 - 数据可重叠，支持移动平均等"""

    def __init__(self, size_seconds: float, slide_seconds: float,
                 agg_func: str = "avg", min_count: int = 1,
                 allowed_lateness: float = 5.0):
        self._size = size_seconds
        self._slide = slide_seconds
        self._agg_func = agg_func
        self._min_count = min_count
        self._allowed_lateness = allowed_lateness  # FIXED-P1: 允许迟到阈值（秒）
        self._watermark: float | None = None  # FIXED-P1: 水位线（基于已见最大事件时间戳）
        self._buffer: deque[StreamEvent] = deque(maxlen=max(1000, int(size_seconds * 1000)))  # FIXED-P2: W14 SlidingWindow._buffer无界增长，添加maxlen
        self._last_emit: float | None = None

    def add(self, event: StreamEvent) -> dict | None:
        """添加事件，返回满足窗口条件的聚合结果

        Returns:
            聚合结果字典，包含 value/count/aggregate 等字段；
            窗口数据不足或未到滑动间隔时返回 None
        """
        now_ts = event.timestamp.timestamp()

        # FIXED-P1: 水位线机制 - 基于已见最大事件时间戳，丢弃严重迟到事件
        if self._watermark is not None:
            if now_ts < self._watermark - self._allowed_lateness:
                # 事件严重迟到（超出允许迟到阈值），丢弃
                return None
        # 更新水位线为已见最大事件时间戳
        if self._watermark is None or now_ts > self._watermark:
            self._watermark = now_ts

        # 清理过期数据
        cutoff = now_ts - self._size
        while self._buffer and self._buffer[0].timestamp.timestamp() < cutoff:
            self._buffer.popleft()

        self._buffer.append(event)

        # 检查是否需要输出
        if self._last_emit is not None and now_ts - self._last_emit < self._slide:
            return None

        # 窗口内数据不足时不输出
        if len(self._buffer) < self._min_count:
            return None

        self._last_emit = now_ts
        return self._compute_aggregate()

    def _compute_aggregate(self) -> dict:
        """计算窗口内数据的聚合值"""
        values = [e.value for e in self._buffer]
        agg = self._agg_func

        if agg == "avg":
            result = sum(values) / len(values)
        elif agg == "sum":
            result = sum(values)
        elif agg == "min":
            result = min(values)
        elif agg == "max":
            result = max(values)
        elif agg == "count":
            result = float(len(values))
        elif agg == "std":
            mean = sum(values) / len(values)
            variance = sum((x - mean) ** 2 for x in values) / len(values)
            result = variance ** 0.5
        else:
            result = values[-1]

        return {
            "value": result,
            "count": len(values),
            "aggregate": agg,
            "window_size": self._size,
        }

    def get_values(self) -> list[float]:
        """获取窗口内所有值"""
        return [e.value for e in self._buffer]


class SessionWindow:
    """会话窗口 - 基于空闲时间分割"""

    def __init__(self, timeout_seconds: float):
        self._timeout = timeout_seconds
        self._buffer: deque[StreamEvent] = deque(maxlen=10000)  # FIXED-P2: 设置maxlen防止内存无限增长
        self._last_event_time: float | None = None

    def add(self, event: StreamEvent) -> tuple[list[StreamEvent] | None, bool]:
        """添加事件，返回过期事件和是否新会话开始"""
        now_ts = event.timestamp.timestamp()

        expired = None
        new_session = False

        if self._last_event_time is not None and now_ts - self._last_event_time > self._timeout:
            # 会话超时，返回缓冲区数据
            expired = list(self._buffer)
            self._buffer.clear()
            new_session = True

        self._buffer.append(event)
        self._last_event_time = now_ts

        return expired, new_session


class StreamProcessor:
    """流处理器 - 支持链式算子"""

    def __init__(self, processor_id: str):
        self._id = processor_id
        self._operators: list[Callable] = []
        self._running = False
        self._stats = {
            "processed": 0,
            "errors": 0,
            "last_process_time": None,
        }

    def filter(self, predicate: Callable[[StreamEvent], bool]) -> StreamProcessor:
        """添加过滤算子"""
        def _filter(event: StreamEvent) -> StreamEvent | None:
            try:
                return event if predicate(event) else None
            except Exception as e:
                logger.warning("Stream filter predicate failed: %s", e)  # FIXED-P1: 原问题-异常静默丢弃事件无日志
                return None

        self._operators.append(_filter)
        return self

    def map(self, transform: Callable[[StreamEvent], Any]) -> StreamProcessor:
        """添加映射算子"""
        def _map(event: StreamEvent) -> StreamEvent | None:
            try:
                result = transform(event)
                if isinstance(result, StreamEvent):
                    return result
                elif result is None:
                    return None
                else:
                    # 创建新事件
                    return StreamEvent(
                        device_id=event.device_id,
                        point_name=event.point_name,
                        value=result,
                        timestamp=event.timestamp,
                        quality=event.quality,
                    )
            except Exception as e:
                logger.warning("Stream map transform failed: %s", e)  # FIXED-P1: 原问题-异常静默丢弃事件无日志
                return None

        self._operators.append(_map)
        return self

    def aggregate(self, window_seconds: float, agg_func: str = "avg") -> StreamProcessor:
        """添加聚合算子"""
        windows: dict[str, deque[StreamEvent]] = {}

        def _aggregate(event: StreamEvent) -> StreamEvent | None:
            key = f"{event.device_id}:{event.point_name}"
            if key not in windows:
                windows[key] = deque(maxlen=10000)  # FIXED-P2: 设置maxlen防止内存无限增长

            win = windows[key]
            # FIXED-P1: 使用事件时间戳而非 time.time() 做 cutoff，保证事件时间语义一致
            cutoff = event.timestamp.timestamp() - window_seconds
            while win and win[0].timestamp.timestamp() < cutoff:
                win.popleft()
            win.append(event)

            if not win:
                return None

            values = [e.value for e in win]
            if agg_func == "avg":
                result = sum(values) / len(values)
            elif agg_func == "sum":
                result = sum(values)
            elif agg_func == "min":
                result = min(values)
            elif agg_func == "max":
                result = max(values)
            elif agg_func == "count":
                result = float(len(values))
            elif agg_func == "std":
                mean = sum(values) / len(values)
                variance = sum((x - mean) ** 2 for x in values) / len(values)
                result = variance ** 0.5
            else:
                result = values[-1]

            return StreamEvent(
                device_id=event.device_id,
                point_name=f"{event.point_name}_{agg_func}",
                value=result,
                timestamp=datetime.now(UTC),
                quality=event.quality,
            )

        self._operators.append(_aggregate)
        return self

    def detect_rise(self, threshold: float, name: str = "rise") -> StreamProcessor:
        """检测上升沿"""
        last_values: dict[str, float] = {}

        def _detect_rise(event: StreamEvent) -> StreamEvent | None:
            key = f"{event.device_id}:{event.point_name}"
            last = last_values.get(key, 0)

            if event.value > last and event.value - last >= threshold:
                last_values[key] = event.value
                return StreamEvent(
                    device_id=event.device_id,
                    point_name=f"{event.point_name}_{name}",
                    value=1.0,
                    timestamp=event.timestamp,
                    quality=event.quality,
                )

            last_values[key] = event.value
            return None

        self._operators.append(_detect_rise)
        return self

    def detect_change_rate(self, max_rate: float, window_seconds: float = 5.0) -> StreamProcessor:
        """检测变化率超限"""
        history: dict[str, deque[tuple[float, float]]] = {}  # key -> [(timestamp, value)]

        def _detect_rate(event: StreamEvent) -> StreamEvent | None:
            key = f"{event.device_id}:{event.point_name}"
            now = event.timestamp.timestamp()

            if key not in history:
                history[key] = deque(maxlen=10000)

            h = history[key]
            cutoff = now - window_seconds
            while h and h[0][0] < cutoff:
                h.popleft()

            if len(h) > 0:
                prev_ts, prev_val = h[-1]
                dt = now - prev_ts
                if dt > 0:
                    rate = abs(event.value - prev_val) / dt
                    if rate > max_rate:
                        h.append((now, event.value))
                        return StreamEvent(
                            device_id=event.device_id,
                            point_name=f"{event.point_name}_rate_exceeded",
                            value=rate,
                            timestamp=event.timestamp,
                            quality="suspect",
                        )

            h.append((now, event.value))
            return None

        self._operators.append(_detect_rate)
        return self

    def detect_anomaly(self, std_multiplier: float = 3.0) -> StreamProcessor:
        """基于统计的异常检测 (超过N倍标准差视为异常)"""
        # PERF: 原实现使用 list + pop(0) O(n) + 每次全量重算 mean/variance O(n)；
        # 改为 deque(maxlen) 消除 pop(0) 开销 + Welford 增量算法 O(1) 计算统计量
        history: dict[str, deque] = {}
        # Welford 状态: key -> (count, mean, M2)
        welford: dict[str, tuple[int, float, float]] = {}
        min_samples = 10
        max_samples = 1000

        def _detect_anomaly(event: StreamEvent) -> StreamEvent | None:
            key = f"{event.device_id}:{event.point_name}"

            if key not in history:
                history[key] = deque(maxlen=max_samples)
                welford[key] = (0, 0.0, 0.0)

            h = history[key]
            count, mean, m2 = welford[key]

            # PERF: deque 已满时，先手动 popleft 获取旧值并做反向 Welford 更新，
            # 避免 deque 自动淘汰时无法获取被丢弃的值
            if len(h) >= max_samples:
                old_val = h.popleft()
                old_count = count
                count -= 1
                if count > 0:
                    old_mean = mean
                    mean = (old_mean * old_count - old_val) / count
                    m2 = m2 - (old_val - old_mean) * (old_val - mean)
                else:
                    mean = 0.0
                    m2 = 0.0

            # Welford 增量更新
            h.append(event.value)
            count += 1
            delta = event.value - mean
            mean += delta / count
            delta2 = event.value - mean
            m2 += delta * delta2

            welford[key] = (count, mean, m2)

            if count < min_samples:
                return None

            # 总体方差（与原实现 sum((x-mean)**2)/len(h) 语义一致）
            variance = m2 / count if count > 0 else 0.0
            std = variance ** 0.5

            if std > 0 and abs(event.value - mean) > std_multiplier * std:
                return StreamEvent(
                    device_id=event.device_id,
                    point_name=f"{event.point_name}_anomaly",
                    value=event.value,
                    timestamp=event.timestamp,
                    quality="suspect",
                )

            return None

        self._operators.append(_detect_anomaly)
        return self

    def process(self, event: StreamEvent) -> list[StreamEvent]:
        """处理事件，返回处理结果列表"""
        self._stats["last_process_time"] = time.time()
        results = []

        current: StreamEvent | None = event
        for op in self._operators:
            if current is None:
                break
            try:
                self._stats["processed"] += 1
                current = op(current)
            except Exception as e:
                logger.warning("StreamProcessor算子执行失败: %s", e)
                self._stats["errors"] += 1
                break

        if current is not None:
            results.append(current)

        return results

    def get_stats(self) -> dict:
        """获取统计信息"""
        return dict(self._stats)


class StreamComputeEngine:
    """边缘流计算引擎 - 管理多个流处理器"""

    def __init__(self):
        self._running = False
        self._processors: dict[str, StreamProcessor] = {}
        self._rules: dict[str, dict] = {}
        self._callbacks: list[Callable] = []
        self._task: asyncio.Task | None = None
        self._event_queue: asyncio.Queue = asyncio.Queue(maxsize=10000)
        self._windows: dict[str, TumblingWindow | SlidingWindow | SessionWindow] = {}
        self._event_bus: EventBus | None = None
        self._subscriber_queue: asyncio.Queue | None = None
        self._event_handler_task: asyncio.Task | None = None
        # 窗口聚合结果缓存: "device_id:point_name:window_seconds:aggregate" -> float
        # FIXED-P2: 改用 OrderedDict + LRU 淘汰，max=10000，防止无界增长
        self._window_results: OrderedDict[str, float] = OrderedDict()
        self._window_results_max = 10000
        # R11-ENG-08: 保护 _window_results 的读写，move_to_end 与 _process_loop 写入互斥
        self._window_results_lock = threading.Lock()
        # 规则对应的滑动窗口实例: rule_id -> SlidingWindow
        self._rule_windows: dict[str, SlidingWindow] = {}
        self._stats = {
            "total_processed": 0,
            "total_errors": 0,
            "processors_count": 0,
        }
        # FIXED-P0: 共享状态锁，保护 _processors/_rules/_windows/_window_results/_rule_windows/_stats
        # 防止 _process_loop 在 await 让出后，API 端点修改状态导致后续访问命中已移除的 processor
        self._state_lock = asyncio.Lock()

    async def start(self, event_bus: EventBus | None = None) -> None:
        """启动流计算引擎

        Args:
            event_bus: 可选的事件总线，用于接收Scheduler事件
        """
        self._running = True
        self._task = asyncio.create_task(self._process_loop())

        # 与EventBus集成，自动订阅事件
        if event_bus is not None:
            self._event_bus = event_bus
            self._subscriber_queue = await event_bus.subscribe("stream_compute")
            self._event_handler_task = asyncio.create_task(self._event_handler_loop())
            logger.info("流计算引擎启动，已订阅EventBus")

    async def connect_to_event_bus(self, event_bus: EventBus) -> None:
        """连接到事件总线

        Args:
            event_bus: 事件总线实例
        """
        if self._event_bus is not None and self._running:
            logger.warning("流计算引擎已连接到事件总线")
            return

        self._event_bus = event_bus
        self._subscriber_queue = await event_bus.subscribe("stream_compute")
        self._event_handler_task = asyncio.create_task(self._event_handler_loop())
        logger.info("流计算引擎已连接到EventBus")

    async def _event_handler_loop(self) -> None:
        """事件总线处理循环"""
        if self._subscriber_queue is None:
            return

        logger.info("StreamCompute事件处理循环启动")
        while self._running:
            try:
                event = await asyncio.wait_for(self._subscriber_queue.get(), timeout=1.0)

                # 转换PointUpdateEvent为StreamEvent
                if isinstance(event, PointUpdateEvent):
                    stream_event = StreamEvent(
                        device_id=event.device_id,
                        point_name=event.point_name,
                        value=event.value if event.value is not None else 0.0,
                        timestamp=datetime.fromisoformat(event.timestamp) if isinstance(event.timestamp, str) else event.timestamp,
                        quality=event.quality,
                    )
                    await self.submit_event(stream_event)
                elif isinstance(event, StreamEvent):
                    await self.submit_event(event)

            except TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("StreamCompute事件处理异常: %s", e)

    async def stop(self) -> None:
        """停止流计算引擎"""
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self._event_handler_task:
            self._event_handler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._event_handler_task
        logger.info("流计算引擎停止")

    async def submit_event(self, event: StreamEvent) -> None:
        """提交事件到流处理队列（异步版本）"""
        try:
            self._event_queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("流计算引擎队列已满，丢弃事件: %s.%s", event.device_id, event.point_name)

    async def submit(self, device_id: str, point_name: str, value: float, quality: str = "good") -> None:
        """提交事件到流处理队列"""
        event = StreamEvent(
            device_id=device_id,
            point_name=point_name,
            value=value,
            timestamp=datetime.now(UTC),
            quality=quality,
        )
        await self.submit_event(event)

    def create_processor(self, processor_id: str) -> StreamProcessor:
        """创建流处理器"""
        processor = StreamProcessor(processor_id)
        self._processors[processor_id] = processor
        self._stats["processors_count"] = len(self._processors)
        logger.info("创建流处理器: %s", processor_id)
        return processor

    def register_callback(self, callback: Callable) -> None:
        """注册结果回调"""
        self._callbacks.append(callback)

    async def create_tumbling_window(
        self, window_id: str, size_seconds: float
    ) -> TumblingWindow:
        """创建滚动窗口"""
        window = TumblingWindow(size_seconds)
        self._windows[window_id] = window
        return window

    async def create_sliding_window(
        self, window_id: str, size_seconds: float, slide_seconds: float
    ) -> SlidingWindow:
        """创建滑动窗口"""
        window = SlidingWindow(size_seconds, slide_seconds)
        self._windows[window_id] = window
        return window

    async def create_session_window(
        self, window_id: str, timeout_seconds: float
    ) -> SessionWindow:
        """创建会话窗口"""
        window = SessionWindow(timeout_seconds)
        self._windows[window_id] = window
        return window

    async def add_rule(self, rule_id: str, rule_config: dict) -> None:
        """添加流计算规则"""
        async with self._state_lock:
            self._rules[rule_id] = rule_config
        logger.info("添加流计算规则: %s", rule_id)

    async def remove_rule(self, rule_id: str) -> None:
        """移除流计算规则"""
        async with self._state_lock:
            self._rules.pop(rule_id, None)
            # FIXED-P0: 窗口键改为 rule_id:device_id:point_name 后，按前缀清理所有关联窗口
            prefix = f"{rule_id}:"
            keys_to_remove = [k for k in self._rule_windows if k.startswith(prefix)]
            for k in keys_to_remove:
                self._rule_windows.pop(k, None)
            # FIXED(一般): 同步清理 _windows 中以 rule_id 为前缀的窗口条目，
            # 防止 _windows dict 无上限增长导致内存泄漏
            win_keys_to_remove = [k for k in self._windows if k.startswith(prefix) or k == rule_id]
            for k in win_keys_to_remove:
                self._windows.pop(k, None)
            # 清理该规则关联的 Processor
            for key in [f"_pattern_{rule_id}", f"_anomaly_{rule_id}"]:
                self._processors.pop(key, None)
            self._stats["processors_count"] = len(self._processors)
        logger.info("移除流计算规则: %s", rule_id)

    async def _process_loop(self) -> None:
        """事件处理循环"""
        while self._running:
            try:
                event = await asyncio.wait_for(self._event_queue.get(), timeout=1.0)

                # 记录输入事件
                if record_packet:
                    try:
                        record_packet("rx", "stream_compute", event.device_id,
                                      f"{event.point_name}={event.value}")
                    except Exception as e:
                        # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                        logger.debug("记录调试数据包失败: %s", e)

                # 对每个规则处理
                # FIXED-P0: 加锁保护规则处理全过程，防止 await 让出后 API 端点修改 _processors/_rules 导致竞态
                async with self._state_lock:
                    # FIXED-P1: 迭代快照 list(self._rules.items())，避免 await 期间字典被修改导致 RuntimeError
                    for rule_id, rule in list(self._rules.items()):
                        try:
                            results = await self._process_rule(rule_id, rule, event)
                            for result in results:
                                await self._emit_result(result)
                        except Exception as e:
                            logger.error("流计算规则执行失败 %s: %s", rule_id, e)

                    self._stats["total_processed"] += 1

            except TimeoutError:
                continue
            except Exception as e:
                self._stats["total_errors"] += 1
                logger.error("流计算引擎处理异常: %s", e)

    async def _process_rule(self, rule_id: str, rule: dict, event: StreamEvent) -> list[StreamEvent]:
        """执行规则"""
        results = []

        rule_type = rule.get("type", "filter")
        point_filter = rule.get("point_filter", "")

        # 点位过滤
        if point_filter and not (event.point_name == point_filter or event.point_name.startswith(point_filter)):
            return results

        if rule_type == "filter":
            predicate_str = rule.get("predicate", "value > 0")
            if self._eval_predicate(predicate_str, event.value):
                results.append(event)

        elif rule_type == "aggregate":
            window_seconds = rule.get("window_seconds", 60)
            agg_func = rule.get("aggregate", "avg")
            min_count = rule.get("min_count", 1)
            slide_seconds = rule.get("slide_seconds", 1.0)

            # FIXED-P0: 窗口键改为 rule_id:device_id:point_name，避免多设备数据混合聚合
            win_key = f"{rule_id}:{event.device_id}:{event.point_name}"
            if win_key not in self._rule_windows:
                self._rule_windows[win_key] = SlidingWindow(
                    size_seconds=window_seconds,
                    slide_seconds=slide_seconds,
                    agg_func=agg_func,
                    min_count=min_count,
                )
            window = self._rule_windows[win_key]
            agg_result = window.add(event)

            if agg_result is not None:
                # 缓存窗口聚合结果
                result_key = f"{event.device_id}:{event.point_name}:{window_seconds}:{agg_func}"
                # FIXED-P2: OrderedDict + LRU 淘汰，max=10000
                # R11-ENG-08: 加锁保护 _window_results 读写，与 get_window_result 的 move_to_end 互斥
                with self._window_results_lock:
                    self._window_results[result_key] = agg_result["value"]
                    self._window_results.move_to_end(result_key)
                    while len(self._window_results) > self._window_results_max:
                        self._window_results.popitem(last=False)

                # 构造聚合结果事件
                agg_event = StreamEvent(
                    device_id=event.device_id,
                    point_name=f"{event.point_name}:{agg_func}",
                    value=agg_result["value"],
                    timestamp=datetime.now(UTC),
                    quality=event.quality,
                )
                results.append(agg_event)

                # 发布窗口聚合结果到EventBus
                await self._publish_result(WindowResult(
                    window_id=rule_id,
                    window_type="sliding",
                    start_time=datetime.now(UTC),
                    end_time=datetime.now(UTC),
                    point_name=event.point_name,
                    aggregate=agg_func,
                    value=agg_result["value"],
                    count=agg_result["count"],
                ))

                # 发布到 EventBus
                if self._event_bus:
                    try:
                        bus_event = PointUpdateEvent(
                            device_id=event.device_id,
                            point_name=f"{event.point_name}:{agg_func}",
                            value=agg_result["value"],
                            quality="good",
                        )
                        await self._event_bus.publish(bus_event)
                    except Exception as e:
                        logger.error("Failed to publish window aggregate to EventBus: %s", e)

        elif rule_type == "pattern":
            pattern = rule.get("pattern", "rise")
            threshold = rule.get("threshold", 1.0)
            if pattern == "3sigma":
                # 3σ异常检测：获取或创建滑动窗口
                # FIXED-P0: 窗口键改为 rule_id:device_id:point_name，避免多设备数据混合聚合
                win_key = f"{rule_id}:{event.device_id}:{event.point_name}"
                if win_key not in self._rule_windows:
                    window_seconds = rule.get("window_seconds", 60)
                    self._rule_windows[win_key] = SlidingWindow(
                        size_seconds=window_seconds,
                        slide_seconds=1.0,
                        agg_func="avg",
                        min_count=10,
                    )
                window = self._rule_windows[win_key]
                window.add(event)
                values = window.get_values()
                match = self._detect_3sigma(values)
                if match:
                    await self._publish_result(match)
                    results.append(StreamEvent(
                        device_id=event.device_id,
                        point_name=f"{event.point_name}_3sigma",
                        value=match.confidence,
                        timestamp=event.timestamp,
                        quality="suspect",
                    ))
            else:
                # 获取或创建 Processor 实例
                proc_key = f"_pattern_{rule_id}"
                if proc_key not in self._processors:
                    processor = StreamProcessor(proc_key)
                    if pattern == "rise":
                        processor.detect_rise(threshold)
                    elif pattern == "rate":
                        max_rate = rule.get("max_rate", 10.0)
                        processor.detect_change_rate(max_rate)
                    self._processors[proc_key] = processor
                    self._stats["processors_count"] = len(self._processors)
                results = self._processors[proc_key].process(event)
                # 发布模式匹配结果
                for r in results:
                    await self._publish_result(PatternMatch(
                        pattern_id=pattern,
                        matched=True,
                        confidence=1.0,
                        details={"point_name": r.point_name, "value": r.value},
                    ))

        elif rule_type == "anomaly":
            std_multiplier = rule.get("std_multiplier", 3.0)
            # 获取或创建 Processor 实例
            proc_key = f"_anomaly_{rule_id}"
            if proc_key not in self._processors:
                processor = StreamProcessor(proc_key)
                processor.detect_anomaly(std_multiplier)
                self._processors[proc_key] = processor
                self._stats["processors_count"] = len(self._processors)
            results = self._processors[proc_key].process(event)
            # 发布异常检测结果
            for r in results:
                await self._publish_result(PatternMatch(
                    pattern_id="anomaly",
                    matched=True,
                    confidence=1.0,
                    details={"point_name": r.point_name, "value": r.value, "std_multiplier": std_multiplier},
                ))

        return results

    def _detect_3sigma(self, values: list[float]) -> PatternMatch | None:
        """3σ异常值检测"""
        if len(values) < 10:
            return None
        import statistics
        mean = statistics.mean(values)
        try:
            std = statistics.stdev(values)
        except statistics.StatisticsError:
            return None
        if std == 0:
            return None
        last_val = values[-1]
        z_score = abs(last_val - mean) / std
        if z_score > 3:
            return PatternMatch(
                pattern_id="3sigma",
                matched=True,
                confidence=min(z_score / 6, 1.0),
                details={"z_score": z_score, "mean": mean, "std": std, "value": last_val},
            )
        return None

    async def _publish_result(self, result: WindowResult | PatternMatch) -> None:
        """发布计算结果到事件总线"""
        # 记录输出结果
        if record_packet:
            try:
                if isinstance(result, WindowResult):
                    record_packet("tx", "stream_compute", result.point_name,
                                  f"window:{result.window_id}:{result.aggregate}={result.value}")
                elif isinstance(result, PatternMatch):
                    record_packet("tx", "stream_compute", result.pattern_id,
                                  f"pattern:{result.pattern_id}:matched={result.matched}:conf={result.confidence}")
            except Exception as e:
                # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                logger.debug("记录输出结果调试包失败: %s", e)

        if self._event_bus:
            try:
                # FIXED-P0: 构造 StreamResultEvent 子类发布，修复原 publish 传两个参数签名不匹配
                if isinstance(result, WindowResult):
                    event = StreamResultEvent(
                        result_type="window",
                        window_id=result.window_id,
                        window_type=result.window_type,
                        point_name=result.point_name,
                        aggregate=result.aggregate,
                        value=result.value,
                        count=result.count,
                    )
                    await self._event_bus.publish(event)
                elif isinstance(result, PatternMatch):
                    event = StreamResultEvent(
                        result_type="pattern",
                        pattern_id=result.pattern_id,
                        matched=result.matched,
                        confidence=result.confidence,
                        details=result.details,
                    )
                    await self._event_bus.publish(event)
            except Exception as e:
                logger.warning("Failed to publish stream result: %s", e)

    @staticmethod
    def _eval_predicate(predicate: str, value: float) -> bool:
        """评估简单谓词表达式"""
        try:
            # FIXED-P2: 使用re精确匹配操作符，避免"=="误匹配"!="中的子串
            import re
            match = re.match(r'^(.+?)\s*(!=|==|>=|<=|>|<)\s*(.+)$', predicate.strip())
            if match:
                left_expr = match.group(1).strip()
                symbol = match.group(2)
                right_expr = match.group(3).strip()
                threshold = float(right_expr)
                # FIXED-BugR4X: 原问题-left_expr.replace('.','').replace('-','').isdigit()对科学计数法(1e5)和多小数点(1.2.3)误判；修复-改用try/except float()解析，仅当left_expr为合法数字字面量时覆盖value
                try:
                    value = float(left_expr)
                except ValueError:
                    pass  # left_expr非数字字面量，保留传入的value
                if symbol == "<":
                    return value < threshold
                elif symbol == ">":
                    return value > threshold
                elif symbol == "==":
                    return value == threshold
                elif symbol == "!=":
                    return value != threshold
                elif symbol == "<=":
                    return value <= threshold
                elif symbol == ">=":
                    return value >= threshold
            return True
        except Exception as e:
            logger.warning("Predicate eval failed: %s", e)  # FIXED-P1: 原问题-谓词求值异常默认返回True(放行)可能误触发告警；改为记录日志
            return False  # FIXED-P1: 异常时拒绝(安全侧)而非放行

    async def _emit_result(self, event: StreamEvent) -> None:
        """发送结果到回调"""
        for callback in self._callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(event)
                else:
                    callback(event)
            except Exception as e:
                logger.error("流计算回调执行失败: %s", e)

    def get_window_result(self, device_id: str, point_name: str,
                          window_seconds: int, aggregate: str) -> float | None:
        """获取当前窗口聚合结果

        Args:
            device_id: 设备ID
            point_name: 测点名称
            window_seconds: 窗口大小（秒）
            aggregate: 聚合函数名 (avg/sum/min/max/count/std)

        Returns:
            聚合值，无结果时返回 None
        """
        key = f"{device_id}:{point_name}:{window_seconds}:{aggregate}"
        # FIXED-P0: 使用快照避免与 _process_loop 并发写入竞态
        snapshot = dict(self._window_results)
        if key in snapshot:
            # FIXED-P2: LRU 访问时移动到末尾，保持最近访问的热度
            # R11-ENG-08: move_to_end 加锁，防止与 _process_rule 写入并发导致 OrderedDict 内部状态不一致
            with self._window_results_lock:
                self._window_results.move_to_end(key)
        return snapshot.get(key)

    def get_stats(self) -> dict:
        """获取引擎统计信息"""
        # FIXED-P0: 返回快照，避免与 _process_loop 并发修改竞态
        return {
            **dict(self._stats),
            "rules_count": len(self._rules),
            "windows_count": len(self._windows),
            "queue_size": self._event_queue.qsize(),
        }

    def get_processor_stats(self, processor_id: str) -> dict | None:
        """获取处理器统计"""
        # FIXED-P0: 使用快照避免与 remove_rule 并发删除竞态
        processors = dict(self._processors)
        processor = processors.get(processor_id)
        return processor.get_stats() if processor else None


# 全局实例
_stream_engine: StreamComputeEngine | None = None
_stream_engine_lock = threading.Lock()  # FIXED-P2: 全局单例初始化竞态保护


def get_stream_engine() -> StreamComputeEngine:
    """获取流计算引擎全局实例"""
    global _stream_engine
    with _stream_engine_lock:  # FIXED-P2: 全局单例初始化竞态保护
        if _stream_engine is None:
            _stream_engine = StreamComputeEngine()
    return _stream_engine
