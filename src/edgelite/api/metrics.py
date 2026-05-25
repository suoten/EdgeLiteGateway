"""Prometheus 指标端点 - /metrics"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Response
from fastapi.responses import PlainTextResponse

router = APIRouter(tags=["Metrics"])


def _escape_label(s: str) -> str:
    """转义Prometheus标签值"""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_metric(
    name: str,
    labels: dict[str, str],
    value: float,
    timestamp: float | None = None,
    metric_type: str = "gauge",
) -> str:
    """格式化Prometheus指标"""
    label_str = ""
    if labels:
        label_parts = [f'{k}="{_escape_label(str(v))}"' for k, v in labels.items()]
        label_str = "{" + ",".join(label_parts) + "}"

    line = f"{name}{label_str} {value}"
    if timestamp is not None:
        line += f" {int(timestamp * 1000)}"
    return line


class PrometheusExporter:
    """Prometheus指标导出器"""

    def __init__(self):
        self._metrics: dict[str, dict[str, Any]] = {}

    def gauge(self, name: str, value: float, labels: dict[str, str] | None = None, description: str = "") -> None:
        """设置Gauge指标"""
        if name not in self._metrics:
            self._metrics[name] = {"type": "gauge", "description": description, "values": {}}
        self._metrics[name]["values"][self._labels_key(labels or {})] = value

    def counter(self, name: str, value: float, labels: dict[str, str] | None = None, description: str = "") -> None:
        """增加Counter指标"""
        if name not in self._metrics:
            self._metrics[name] = {"type": "counter", "description": description, "values": {}}
        key = self._labels_key(labels or {})
        current = self._metrics[name]["values"].get(key, 0.0)
        self._metrics[name]["values"][key] = current + value

    def histogram(
        self,
        name: str,
        value: float,
        labels: dict[str, str] | None = None,
        description: str = "",
        buckets: tuple[float, ...] = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    ) -> None:
        """记录Histogram指标"""
        if name not in self._metrics:
            self._metrics[name] = {"type": "histogram", "description": description, "values": {}, "buckets": buckets}

        key = self._labels_key(labels or {})
        if key not in self._metrics[name]["values"]:
            self._metrics[name]["values"][key] = {"sum": 0.0, "count": 0, "buckets": {b: 0 for b in buckets}}

        entry = self._metrics[name]["values"][key]
        entry["sum"] += value
        entry["count"] += 1
        for b in buckets:
            if value <= b:
                entry["buckets"][b] += 1

    def summary(
        self,
        name: str,
        value: float,
        labels: dict[str, str] | None = None,
        description: str = "",
        quantiles: tuple[float, ...] = (0.5, 0.9, 0.95, 0.99),
    ) -> None:
        """记录Summary指标"""
        if name not in self._metrics:
            self._metrics[name] = {"type": "summary", "description": description, "values": {}, "quantiles": quantiles}

        key = self._labels_key(labels or {})
        if key not in self._metrics[name]["values"]:
            self._metrics[name]["values"][key] = {"sum": 0.0, "count": 0, "values": []}

        entry = self._metrics[name]["values"][key]
        entry["sum"] += value
        entry["count"] += 1
        entry["values"].append(value)

    @staticmethod
    def _labels_key(labels: dict[str, str]) -> str:
        """生成标签组合的唯一键"""
        return ",".join(f"{k}={v}" for k, v in sorted(labels.items()))

    def render(self) -> str:
        """渲染Prometheus格式文本"""
        output = []
        timestamp = time.time()

        for name, metric in self._metrics.items():
            metric_type = metric["type"]
            description = metric.get("description", "")

            # HELP行
            if description:
                output.append(f"# HELP {name} {description}")

            # TYPE行
            output.append(f"# TYPE {name} {metric_type}")

            if metric_type == "gauge":
                for labels_key, value in metric["values"].items():
                    labels = self._parse_labels(labels_key)
                    output.append(_format_metric(name, labels, value, timestamp))

            elif metric_type == "counter":
                for labels_key, value in metric["values"].items():
                    labels = self._parse_labels(labels_key)
                    output.append(_format_metric(name, labels, value, timestamp))

            elif metric_type == "histogram":
                buckets = metric.get("buckets", ())
                for labels_key, data in metric["values"].items():
                    labels = self._parse_labels(labels_key)
                    for b in buckets:
                        output.append(_format_metric(
                            f"{name}_bucket",
                            {**labels, "le": str(b)},
                            data["buckets"].get(b, 0),
                            timestamp,
                        ))
                    output.append(_format_metric(f"{name}_bucket", {**labels, "le": "+Inf"}, data["count"], timestamp))
                    output.append(_format_metric(f"{name}_sum", labels, data["sum"], timestamp))
                    output.append(_format_metric(f"{name}_count", labels, data["count"], timestamp))

            elif metric_type == "summary":
                quantiles = metric.get("quantiles", ())
                for labels_key, data in metric["values"].items():
                    labels = self._parse_labels(labels_key)
                    sorted_values = sorted(data["values"])
                    count = len(sorted_values)
                    for q in quantiles:
                        idx = int(q * count) if count > 0 else 0
                        idx = min(idx, count - 1) if count > 0 else 0
                        quantile_value = sorted_values[idx] if sorted_values else 0.0
                        output.append(_format_metric(
                            f"{name}",
                            {**labels, "quantile": str(q)},
                            quantile_value,
                            timestamp,
                        ))
                    output.append(_format_metric(f"{name}_sum", labels, data["sum"], timestamp))
                    output.append(_format_metric(f"{name}_count", labels, data["count"], timestamp))

        return "\n".join(output) + "\n"

    @staticmethod
    def _parse_labels(labels_key: str) -> dict[str, str]:
        """解析标签键为字典"""
        labels = {}
        if not labels_key:
            return labels
        for pair in labels_key.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                labels[k.strip()] = v.strip()
        return labels


# 全局指标导出器实例
_exporter: PrometheusExporter | None = None


def get_exporter() -> PrometheusExporter:
    """获取全局指标导出器"""
    global _exporter
    if _exporter is None:
        _exporter = PrometheusExporter()
    return _exporter


def _collect_system_metrics(exporter: PrometheusExporter) -> None:
    """收集系统指标"""
    import psutil

    # CPU指标
    cpu_percent = psutil.cpu_percent(interval=0.1)
    exporter.gauge("edgelite_cpu_usage_percent", cpu_percent, {"core": "total"})

    for i, percent in enumerate(psutil.cpu_percent(interval=0.1, percpu=True)):
        exporter.gauge("edgelite_cpu_usage_percent", percent, {"core": str(i)})

    # 内存指标
    mem = psutil.virtual_memory()
    exporter.gauge("edgelite_memory_total_bytes", mem.total)
    exporter.gauge("edgelite_memory_available_bytes", mem.available)
    exporter.gauge("edgelite_memory_used_bytes", mem.used)
    exporter.gauge("edgelite_memory_usage_percent", mem.percent)

    # 磁盘指标
    disk = psutil.disk_usage("/")
    exporter.gauge("edgelite_disk_total_bytes", disk.total)
    exporter.gauge("edgelite_disk_free_bytes", disk.free)
    exporter.gauge("edgelite_disk_used_bytes", disk.used)
    exporter.gauge("edgelite_disk_usage_percent", disk.percent)

    # 网络指标
    net = psutil.net_io_counters()
    exporter.counter("edgelite_network_bytes_total", net.bytes_recv + net.bytes_sent)
    exporter.counter("edgelite_network_packets_total", net.packets_recv + net.packets_sent)


def _collect_device_metrics(exporter: PrometheusExporter) -> None:
    """收集设备指标"""
    try:
        from edgelite.app import _app_state

        if not hasattr(_app_state, "scheduler") or not _app_state.scheduler:
            return

        scheduler = _app_state.scheduler
        stats = scheduler.get_collect_stats()
        active_devices = scheduler.get_active_devices()

        # 设备数量
        exporter.gauge("edgelite_devices_total", len(active_devices))

        # 设备采集统计
        for device_id, stat in stats.items():
            labels = {"device_id": device_id}
            exporter.gauge("edgelite_collect_avg_latency_ms", stat.avg_latency_ms, labels)
            exporter.gauge("edgelite_collect_max_latency_ms", stat.max_latency_ms, labels)
            exporter.counter("edgelite_collect_total_calls", stat.total_calls, labels)
            exporter.counter("edgelite_collect_timeout_count", stat.timeout_count, labels)

    except Exception:
        pass


def _collect_alarm_metrics(exporter: PrometheusExporter) -> None:
    """收集告警指标"""
    try:
        from edgelite.storage.sqlite_repo import AlarmRepo

        # 获取告警统计
        # 实际实现需要访问alarm_repo
    except Exception:
        pass


@router.get("/metrics")
async def prometheus_metrics():
    """Prometheus指标端点

    返回格式符合Prometheus exposition format。
    支持与Prometheus server集成进行指标采集。
    """
    exporter = get_exporter()

    # 收集各类指标
    try:
        _collect_system_metrics(exporter)
    except ImportError:
        pass  # psutil未安装

    _collect_device_metrics(exporter)
    _collect_alarm_metrics(exporter)

    # 渲染Prometheus格式
    content = exporter.render()

    return PlainTextResponse(content=content, media_type="text/plain; charset=utf-8")


@router.get("/metrics.json")
async def prometheus_metrics_json():
    """Prometheus指标端点 (JSON格式)

    返回OpenMetrics JSON格式，用于自定义采集。
    """
    exporter = get_exporter()
    metrics = []

    for name, metric in exporter._metrics.items():
        for labels_key, value in metric["values"].items():
            labels = exporter._parse_labels(labels_key)
            metrics.append({
                "name": name,
                "type": metric["type"],
                "description": metric.get("description", ""),
                "labels": labels,
                "value": value,
            })

    return {"metrics": metrics, "timestamp": time.time()}
