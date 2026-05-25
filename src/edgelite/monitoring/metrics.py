"""Prometheus 指标收集器 - 系统、驱动、业务指标"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, UTC
from typing import Any

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
        collector.inc_counter("driver_read_total", labels={"device": "PLC-1"})
        collector.observe_histogram("driver_read_latency_seconds", 0.123)
        metrics_text = collector.get_metrics()
    """

    def __init__(self):
        self._counters: dict[str, float] = {}
        self._gauges: dict[str, float] = {}
        self._histograms: dict[str, list[float]] = {}
        self._labels: dict[str, dict[str, str]] = {}
        self._lock = asyncio.Lock()
        self._start_time = time.time()
        self._custom_collectors: list[callable] = []

    def inc_counter(self, name: str, value: float = 1, labels: dict[str, str] | None = None) -> None:
        """递增计数器

        Args:
            name: 指标名称
            value: 增量值
            labels: 标签字典
        """
        key = self._make_key(name, labels)
        self._counters[key] = self._counters.get(key, 0) + value

    def set_gauge(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        """设置仪表值

        Args:
            name: 指标名称
            value: 仪表值
            labels: 标签字典
        """
        key = self._make_key(name, labels)
        self._gauges[key] = value

    def observe_histogram(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        """观察直方图值

        Args:
            name: 指标名称
            value: 观察值
            labels: 标签字典
        """
        key = self._make_key(name, labels)
        if key not in self._histograms:
            self._histograms[key] = []
        self._histograms[key].append(value)
        # 限制存储数量
        if len(self._histograms[key]) > 10000:
            self._histograms[key] = self._histograms[key][-5000:]

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
        lines.extend(self._format_counters("edgelite_driver", {
            "read_total": "Driver read operations total",
            "read_failed_total": "Driver read failed operations total",
            "write_total": "Driver write operations total",
            "write_failed_total": "Driver write failed operations total",
        }))

        lines.extend(self._format_gauges("edgelite_driver", {
            "connection_status": "Driver connection status (1=connected, 0=disconnected)",
            "connection_quality_score": "Driver connection quality score (0-100)",
            "consecutive_failures": "Consecutive failure count",
        }))

        lines.extend(self._format_histograms("edgelite_driver", {
            "read_latency_seconds": "Driver read latency histogram",
            "write_latency_seconds": "Driver write latency histogram",
        }))

        # 2. 队列级指标
        lines.extend(self._format_gauges("edgelite_queue", {
            "depth": "Queue depth",
            "backpressure_total": "Total backpressure triggers",
        }))

        lines.extend(self._format_counters("edgelite_queue", {
            "enqueued_total": "Total enqueued items",
            "dequeued_total": "Total dequeued items",
            "dropped_total": "Total dropped items due to backpressure",
        }))

        # 3. 告警指标
        lines.extend(self._format_counters("edgelite_alarm", {
            "firing_total": "Total alarm firings",
            "recovery_total": "Total alarm recoveries",
            "acknowledged_total": "Total alarm acknowledgements",
        }))

        lines.extend(self._format_gauges("edgelite_alarm", {
            "current_firing": "Current firing alarms count",
        }))

        # 4. 采集指标
        lines.extend(self._format_gauges("edgelite_collect", {
            "active_tasks": "Active collection tasks",
            "total_devices": "Total registered devices",
            "online_devices": "Online devices count",
            "offline_devices": "Offline devices count",
        }))

        lines.extend(self._format_histograms("edgelite_collect", {
            "latency_seconds": "Collection latency histogram",
            "batch_size": "Collection batch size histogram",
        }))

        # 5. 系统指标
        lines.extend(self._format_system_metrics())

        # 6. 业务级指标
        lines.extend(self._format_counters("edgelite_business", {
            "data_points_total": "Total data points collected",
            "rules_triggered_total": "Total rule triggers",
            "notifications_sent_total": "Total notifications sent",
            "api_requests_total": "Total API requests",
        }))

        # 7. 自定义指标
        lines.extend(self._format_custom_metrics())

        return "\n".join(lines) + "\n"

    def _format_counters(self, prefix: str, metrics: dict[str, str]) -> list[str]:
        """格式化计数器指标"""
        lines = []
        for metric_name, help_text in metrics.items():
            full_name = f"{prefix}_{metric_name}"
            lines.append(f"# HELP {full_name} {help_text}")
            lines.append(f"# TYPE {full_name} counter")

            for key, value in self._counters.items():
                if key.startswith(metric_name) or key == full_name:
                    lines.append(f"{full_name} {value}")

        return lines

    def _format_gauges(self, prefix: str, metrics: dict[str, str]) -> list[str]:
        """格式化仪表指标"""
        lines = []
        for metric_name, help_text in metrics.items():
            full_name = f"{prefix}_{metric_name}"
            lines.append(f"# HELP {full_name} {help_text}")
            lines.append(f"# TYPE {full_name} gauge")

            for key, value in self._gauges.items():
                if key.startswith(metric_name) or key == full_name:
                    lines.append(f"{full_name} {value}")

        return lines

    def _format_histograms(self, prefix: str, metrics: dict[str, str]) -> list[str]:
        """格式化直方图指标"""
        lines = []
        for metric_name, help_text in metrics.items():
            full_name = f"{prefix}_{metric_name}"
            lines.append(f"# HELP {full_name} {help_text}")
            lines.append(f"# TYPE {full_name} histogram")

            for key, values in self._histograms.items():
                if key.startswith(metric_name) or key == full_name:
                    if not values:
                        continue

                    values_sorted = sorted(values)
                    count = len(values_sorted)
                    total = sum(values_sorted)

                    # 计算分位数
                    p50_idx = int(count * 0.5)
                    p95_idx = int(count * 0.95)
                    p99_idx = int(count * 0.99)

                    base_labels = key[len(full_name) + 1:] if len(key) > len(full_name) else ""

                    lines.append(f'{full_name}_count{{{base_labels}}} {count}')
                    lines.append(f'{full_name}_sum{{{base_labels}}} {total:.6f}')

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
            cpu_percent = psutil.cpu_percent(interval=0.1)
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
        self._counters.clear()
        self._gauges.clear()
        self._histograms.clear()


# 便捷函数：记录驱动读取指标
def record_driver_read(device_id: str, success: bool, latency_ms: float) -> None:
    """记录驱动读取操作指标"""
    collector = metrics_collector()
    labels = {"device": device_id}

    collector.inc_counter("driver_read_total", labels=labels)
    if not success:
        collector.inc_counter("driver_read_failed_total", labels=labels)
    collector.observe_histogram("driver_read_latency_seconds", latency_ms / 1000.0, labels=labels)


def record_driver_write(device_id: str, success: bool, latency_ms: float) -> None:
    """记录驱动写入操作指标"""
    collector = metrics_collector()
    labels = {"device": device_id}

    collector.inc_counter("driver_write_total", labels=labels)
    if not success:
        collector.inc_counter("driver_write_failed_total", labels=labels)
    collector.observe_histogram("driver_write_latency_seconds", latency_ms / 1000.0, labels=labels)


def record_connection_status(device_id: str, connected: bool, quality_score: float = 100.0) -> None:
    """记录连接状态"""
    collector = metrics_collector()
    collector.set_gauge("driver_connection_status", 1 if connected else 0, {"device": device_id})
    collector.set_gauge("driver_connection_quality_score", quality_score, {"device": device_id})


def record_alarm(action: str, severity: str | None = None) -> None:
    """记录告警指标"""
    collector = metrics_collector()
    labels = {"severity": severity} if severity else None

    if action == "firing":
        collector.inc_counter("alarm_firing_total", labels=labels)
        collector.inc_counter("alarm_current_firing", labels=labels)
    elif action == "recovery":
        collector.inc_counter("alarm_recovery_total", labels=labels)
        collector.inc_counter("alarm_current_firing", -1, labels=labels)
    elif action == "ack":
        collector.inc_counter("alarm_acknowledged_total", labels=labels)


def record_queue_metrics(depth: int, backpressure: bool = False) -> None:
    """记录队列指标"""
    collector = metrics_collector()
    collector.set_gauge("queue_depth", depth)
    if backpressure:
        collector.inc_counter("queue_backpressure_total")


def record_collection_latency(device_id: str, latency_ms: float, batch_size: int) -> None:
    """记录采集延迟"""
    collector = metrics_collector()
    collector.observe_histogram("collect_latency_seconds", latency_ms / 1000.0, {"device": device_id})
    collector.observe_histogram("collect_batch_size", float(batch_size), {"device": device_id})


# 全局实例
_metrics_collector: MetricsCollector | None = None


def metrics_collector() -> MetricsCollector:
    """获取全局指标收集器"""
    global _metrics_collector
    if _metrics_collector is None:
        _metrics_collector = MetricsCollector()
    return _metrics_collector
