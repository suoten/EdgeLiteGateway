"""Prometheus 指标收集器 - 系统、驱动、业务指标"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable

logger = logging.getLogger(__name__)


class MetricsCollector:
    """Prometheus 指标收集器

    功能：
    - 驱动级指标：连接状态、读写延迟、错误率
    - 队列级指标：队列深度、背压触发次数
    - 业务级指标：告警触发/恢复次数
    - 系统级指标：CPU、内存、磁盘、网络
    - 自定义指标：用户可注册自定义业务指标

    使用方式：
        collector = MetricsCollector()
        collector.inc_counter("edgelite_driver_read_total", labels={"device": "PLC-1"})
        collector.observe_histogram("edgelite_driver_read_latency_seconds", 0.123)
        metrics_text = collector.get_metrics()
    """

    def __init__(self):
        self._counters: dict[str, float] = {}
        self._gauges: dict[str, float] = {}
        self._histograms: dict[str, deque[float]] = {}
        self._labels: dict[str, dict[str, str]] = {}
        self._lock = threading.Lock()  # FIXED-P0: 原问题-使用asyncio.Lock但同步方法中从未acquire，并发写入导致数据竞争；改为threading.Lock并实际使用  # noqa: E501
        self._start_time = time.time()
        self._custom_collectors: list[
            Callable[[], str | list[str]]
        ] = []  # FIXED-P2: 原问题-类型标注使用内置callable而非typing.Callable

    def inc_counter(self, name: str, value: float = 1, labels: dict[str, str] | None = None) -> None:
        """递增计数器

        Args:
            name: 指标名称
            value: 增量值
            labels: 标签字典
        """
        # R8-S-08: Prometheus Counter 语义要求单调递增，拒绝负值避免计数器回退
        if value < 0:
            value = 0
        key = self._make_key(name, labels)
        with self._lock:  # FIXED-P0: 原问题-锁从未acquire，并发写入数据竞争
            self._counters[key] = self._counters.get(key, 0) + value

    def set_gauge(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        """设置仪表值

        Args:
            name: 指标名称
            value: 仪表值
            labels: 标签字典
        """
        key = self._make_key(name, labels)
        with self._lock:  # FIXED-P0: 原问题-锁从未acquire，并发写入数据竞争
            self._gauges[key] = value

    def inc_gauge(self, name: str, value: float = 1, labels: dict[str, str] | None = None) -> None:
        """递增/递减仪表值

        Args:
            name: 指标名称
            value: 增量值（可为负数递减）
            labels: 标签字典
        """
        key = self._make_key(name, labels)
        with self._lock:
            new_value = self._gauges.get(key, 0) + value
            # R8-S-09: 递减场景下保护不为负，避免告警当前触发数等指标出现负值
            if value < 0:
                new_value = max(0, new_value)
            self._gauges[key] = new_value

    def observe_histogram(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        """观察直方图值

        Args:
            name: 指标名称
            value: 观察值
            labels: 标签字典
        """
        key = self._make_key(name, labels)
        with self._lock:  # FIXED-P0: 原问题-锁从未acquire，并发写入数据竞争
            if key not in self._histograms:
                self._histograms[key] = deque(
                    maxlen=10000
                )  # FIXED-P1: 原问题-list超10000后切片[-5000:]为O(n)，改为deque(maxlen)自动淘汰旧值
            self._histograms[key].append(value)

    def set_labels(self, name: str, labels: dict[str, str]) -> None:
        """设置指标标签"""
        self._labels[name] = labels

    def _make_key(self, name: str, labels: dict[str, str] | None) -> str:
        """生成指标键"""
        if not labels:
            return name
        label_str = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
        return f"{name}{{{label_str}}}"

    def register_custom_collector(self, collector: callable) -> None:
        """注册自定义指标收集器"""
        self._custom_collectors.append(collector)

    def get_metrics(self) -> str:
        """获取 Prometheus 格式的指标文本

        Returns:
            Prometheus 格式的指标文本
        """
        lines = []

        # 1. 驱动级指标
        lines.extend(
            self._format_counters(
                "edgelite_driver",
                {
                    "read_total": "Driver read operations total",
                    "read_failed_total": "Driver read failed operations total",
                    "write_total": "Driver write operations total",
                    "write_failed_total": "Driver write failed operations total",
                    "consecutive_failures": "Driver consecutive failure count",
                },
            )
        )

        lines.extend(
            self._format_gauges(
                "edgelite_driver",
                {
                    "connection_status": "Driver connection status (1=connected, 0=disconnected)",
                    "connection_quality_score": "Driver connection quality score (0-100)",
                    "consecutive_failures": "Consecutive failure count",
                },
            )
        )

        lines.extend(
            self._format_histograms(
                "edgelite_driver",
                {
                    "read_latency_seconds": "Driver read latency histogram",
                    "write_latency_seconds": "Driver write latency histogram",
                },
            )
        )

        # 2. 队列级指标
        lines.extend(
            self._format_gauges(
                "edgelite_queue",
                {
                    "depth": "Queue depth",
                    "backpressure_total": "Total backpressure triggers",
                },
            )
        )

        lines.extend(
            self._format_counters(
                "edgelite_queue",
                {
                    "enqueued_total": "Total enqueued items",
                    "dequeued_total": "Total dequeued items",
                    "dropped_total": "Total dropped items due to backpressure",
                },
            )
        )

        # 3. 告警指标
        lines.extend(
            self._format_counters(
                "edgelite_alarm",
                {
                    "firing_total": "Total alarm firings",
                    "recovery_total": "Total alarm recoveries",
                    "acknowledged_total": "Total alarm acknowledgements",
                },
            )
        )

        lines.extend(
            self._format_gauges(
                "edgelite_alarm",
                {
                    "current_firing": "Current firing alarms count",
                },
            )
        )

        # 4. 采集指标
        lines.extend(
            self._format_gauges(
                "edgelite_collect",
                {
                    "active_tasks": "Active collection tasks",
                    "total_devices": "Total registered devices",
                    "online_devices": "Online devices count",
                    "offline_devices": "Offline devices count",
                },
            )
        )

        lines.extend(
            self._format_histograms(
                "edgelite_collect",
                {
                    "latency_seconds": "Collection latency histogram",
                    "batch_size": "Collection batch size histogram",
                },
            )
        )

        # 5. 系统指标
        lines.extend(
            self._format_gauges(
                "edgelite_system",
                {
                    "cpu_percent": "CPU usage percent (from record_system_metrics)",
                    "memory_bytes": "Memory usage in bytes (from record_system_metrics)",
                    "disk_percent": "Disk usage percent (from record_system_metrics)",
                },
            )
        )
        lines.extend(self._format_system_metrics())

        # 6. 业务级指标
        lines.extend(
            self._format_counters(
                "edgelite_business",
                {
                    "data_points_total": "Total data points collected",
                    "rules_triggered_total": "Total rule triggers",
                    "notifications_sent_total": "Total notifications sent",
                    "api_requests_total": "Total API requests",
                },
            )
        )

        # 7. 自定义指标
        lines.extend(self._format_custom_metrics())

        return "\n".join(lines) + "\n"

    def _format_counters(self, prefix: str, metrics: dict[str, str]) -> list[str]:
        """格式化计数器指标"""
        lines = []
        for metric_name, help_text in metrics.items():
            full_name = f"{prefix}_{metric_name}"
            matched = False
            with self._lock:
                items = list(self._counters.items())
            for key, value in items:
                # FIXED-P0: 原问题-匹配逻辑key.startswith(metric_name)无法匹配带前缀的键名（如"edgelite_driver_read_total"不匹配"read_total"），导致所有指标永不输出  # noqa: E501
                if key == full_name or key.startswith(full_name + "{"):
                    if not matched:
                        lines.append(f"# HELP {full_name} {help_text}")
                        lines.append(f"# TYPE {full_name} counter")
                        matched = True
                    lines.append(f"{key} {value}")  # FIXED-P1: 原问题-输出仅full_name丢失标签，改为输出完整key保留标签

        return lines

    def _format_gauges(self, prefix: str, metrics: dict[str, str]) -> list[str]:
        """格式化仪表指标"""
        lines = []
        for metric_name, help_text in metrics.items():
            full_name = f"{prefix}_{metric_name}"
            matched = False
            with self._lock:
                items = list(self._gauges.items())
            for key, value in items:
                # FIXED-P0: 原问题-同_format_counters，匹配逻辑错误导致仪表指标永不输出
                if key == full_name or key.startswith(full_name + "{"):
                    if not matched:
                        lines.append(f"# HELP {full_name} {help_text}")
                        lines.append(f"# TYPE {full_name} gauge")
                        matched = True
                    lines.append(f"{key} {value}")

        return lines

    def _format_histograms(self, prefix: str, metrics: dict[str, str]) -> list[str]:
        """格式化直方图指标"""
        lines = []
        for metric_name, help_text in metrics.items():
            full_name = f"{prefix}_{metric_name}"
            matched = False
            with self._lock:
                items = list(self._histograms.items())
            for key, values in items:
                # FIXED-P0: 原问题-同_format_counters，匹配逻辑错误导致直方图指标永不输出
                if key == full_name or key.startswith(full_name + "{"):
                    if not values:
                        continue
                    if not matched:
                        lines.append(f"# HELP {full_name} {help_text}")
                        lines.append(f"# TYPE {full_name} histogram")
                        matched = True

                    values_sorted = sorted(values)
                    count = len(values_sorted)
                    total = sum(values_sorted)

                    # 计算分位数
                    p50_idx = min(int(count * 0.5), count - 1)
                    p95_idx = min(int(count * 0.95), count - 1)
                    p99_idx = min(
                        int(count * 0.99), count - 1
                    )  # FIXED-P1: 原问题-count=1时int(0.99)=0安全，但count=100时int(99.0)=99越界；加min保护

                    # 提取标签部分
                    base_labels = key[len(full_name) + 1 :] if len(key) > len(full_name) else ""

                    lines.append(f"{full_name}_count{{{base_labels}}} {count}")
                    lines.append(f"{full_name}_sum{{{base_labels}}} {total:.6f}")

                    if count > 0:
                        lines.append(f'{full_name}{{quantile="0.5",{base_labels}}} {values_sorted[p50_idx]:.6f}')
                        lines.append(f'{full_name}{{quantile="0.95",{base_labels}}} {values_sorted[p95_idx]:.6f}')
                        lines.append(f'{full_name}{{quantile="0.99",{base_labels}}} {values_sorted[p99_idx]:.6f}')

        return lines

    def _format_system_metrics(self) -> list[str]:
        """格式化系统指标"""
        lines = []
        lines.append("# HELP edgelite_system_uptime_seconds System uptime in seconds")
        lines.append("# TYPE edgelite_system_uptime_seconds gauge")
        lines.append(f"edgelite_system_uptime_seconds {time.time() - self._start_time:.2f}")

        try:
            import psutil

            # CPU
            cpu_percent = psutil.cpu_percent(
                interval=None
            )  # FIXED-P1: 原问题-interval=0.1阻塞100ms，在async事件循环中导致卡顿；改为None非阻塞返回上次调用以来的平均值  # noqa: E501
            lines.append("# HELP edgelite_system_cpu_percent CPU usage percent")
            lines.append("# TYPE edgelite_system_cpu_percent gauge")
            lines.append(f"edgelite_system_cpu_percent {cpu_percent:.2f}")

            # 内存
            mem = psutil.virtual_memory()
            lines.append("# HELP edgelite_system_memory_bytes System memory info")
            lines.append("# TYPE edgelite_system_memory_bytes gauge")
            lines.append(f'edgelite_system_memory_total{{type="total"}} {mem.total}')
            lines.append(f'edgelite_system_memory_used{{type="used"}} {mem.used}')
            lines.append(f'edgelite_system_memory_percent {{type="percent"}} {mem.percent:.2f}')

            # 磁盘
            disk = psutil.disk_usage("/")
            lines.append("# HELP edgelite_system_disk_percent Disk usage percent")
            lines.append("# TYPE edgelite_system_disk_percent gauge")
            lines.append(f"edgelite_system_disk_percent {disk.percent:.2f}")

        except ImportError:
            logger.debug("psutil not available, system metrics skipped")
        except Exception as e:
            logger.warning("Failed to collect system metrics: %s", e)

        return lines

    def _format_custom_metrics(self) -> list[str]:
        """格式化自定义指标"""
        lines = []
        for collector in self._custom_collectors:
            try:
                result = collector()
                if isinstance(result, str):
                    lines.append(result)
                elif isinstance(result, list):
                    lines.extend(result)
            except Exception as e:
                logger.warning("Custom collector failed: %s", e)
        return lines

    def reset(self) -> None:
        """重置所有指标"""
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._histograms.clear()
            self._labels.clear()  # FIXED-P1: 原问题-reset未清理_labels，重置后残留标签元数据


def record_driver_read(device_id: str, driver_name: str = "", success: bool = True, latency_ms: float = 0.0) -> None:
    """记录驱动读取操作指标"""
    collector = metrics_collector()
    labels = {"device": device_id}
    if driver_name:
        labels["driver"] = driver_name

    collector.inc_counter(
        "edgelite_driver_read_total", labels=labels
    )  # FIXED-P0: 原问题-指标名缺少"edgelite_"前缀，与_format_counters期望的full_name不匹配，指标永不输出
    if not success:
        collector.inc_counter("edgelite_driver_read_failed_total", labels=labels)
    collector.observe_histogram("edgelite_driver_read_latency_seconds", latency_ms / 1000.0, labels=labels)


def record_driver_write(device_id: str, driver_name: str = "", success: bool = True, latency_ms: float = 0.0) -> None:
    """记录驱动写入操作指标"""
    collector = metrics_collector()
    labels = {"device": device_id}
    if driver_name:
        labels["driver"] = driver_name

    collector.inc_counter(
        "edgelite_driver_write_total", labels=labels
    )  # FIXED-P0: 原问题-同record_driver_read，指标名缺少前缀
    if not success:
        collector.inc_counter("edgelite_driver_write_failed_total", labels=labels)
    collector.observe_histogram("edgelite_driver_write_latency_seconds", latency_ms / 1000.0, labels=labels)


def record_connection_status(
    device_id: str, driver_name: str = "", connected: bool = True, quality_score: float = 100.0
) -> None:
    """记录连接状态"""
    collector = metrics_collector()
    labels = {"device": device_id}
    if driver_name:
        labels["driver"] = driver_name
    collector.set_gauge(
        "edgelite_driver_connection_status", 1 if connected else 0, labels
    )  # FIXED-P0: 原问题-指标名缺少前缀
    collector.set_gauge("edgelite_driver_connection_quality_score", quality_score, labels)
    if not connected:
        collector.inc_counter("edgelite_driver_consecutive_failures", labels=labels)


def record_system_metrics(cpu_percent: float, memory_bytes: float, disk_percent: float) -> None:
    """记录系统级指标"""
    collector = metrics_collector()
    collector.set_gauge("edgelite_system_cpu_percent", cpu_percent)  # FIXED-P0: 原问题-指标名缺少前缀
    collector.set_gauge("edgelite_system_memory_bytes", memory_bytes)
    collector.set_gauge("edgelite_system_disk_percent", disk_percent)


def record_alarm(action: str, severity: str | None = None) -> None:
    """记录告警指标"""
    collector = metrics_collector()
    labels = {"severity": severity} if severity else None

    if action == "firing":
        collector.inc_counter("edgelite_alarm_firing_total", labels=labels)  # FIXED-P0: 原问题-指标名缺少前缀
        collector.inc_gauge(
            "edgelite_alarm_current_firing", labels=labels
        )  # FIXED-P0: 原问题-alarm_current_firing为Gauge但使用inc_counter存储在_counters字典，_format_gauges查找_gauges字典永远找不到；且counter递减-1违反Prometheus语义  # noqa: E501
    elif action == "recovery":
        collector.inc_counter("edgelite_alarm_recovery_total", labels=labels)
        collector.inc_gauge(
            "edgelite_alarm_current_firing", -1, labels=labels
        )  # FIXED-P0: 原问题-使用inc_counter(-1)递减counter，改为inc_gauge(-1)正确递减仪表
    elif action == "ack":
        collector.inc_counter("edgelite_alarm_acknowledged_total", labels=labels)


def record_queue_metrics(depth: int, backpressure: bool = False) -> None:
    """记录队列指标"""
    collector = metrics_collector()
    collector.set_gauge("edgelite_queue_depth", depth)  # FIXED-P0: 原问题-指标名缺少前缀
    if backpressure:
        collector.inc_counter("edgelite_queue_backpressure_total")


def record_collection_latency(device_id: str, latency_ms: float, batch_size: int) -> None:
    """记录采集延迟"""
    collector = metrics_collector()
    collector.observe_histogram(
        "edgelite_collect_latency_seconds", latency_ms / 1000.0, {"device": device_id}
    )  # FIXED-P0: 原问题-指标名缺少前缀
    collector.observe_histogram("edgelite_collect_batch_size", float(batch_size), {"device": device_id})


# 全局实例
_metrics_collector: MetricsCollector | None = None


def metrics_collector() -> MetricsCollector:
    """获取全局指标收集器"""
    global _metrics_collector
    if _metrics_collector is None:
        _metrics_collector = MetricsCollector()
    return _metrics_collector
