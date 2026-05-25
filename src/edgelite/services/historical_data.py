"""Enhanced historical data query service with downsampling, aggregation and statistics

Features:
- Advanced time range queries with multiple formats
- Downsampling with configurable aggregation (mean, max, min, sum, count, first, last)
- Statistical analysis (percentile, stddev, variance)
- Data export to CSV/JSON
- Retention policy management
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

from edgelite.storage.influx_storage import InfluxDBStorage

logger = logging.getLogger(__name__)


class AggregationType(Enum):
    """Aggregation function types"""
    MEAN = "mean"
    SUM = "sum"
    MAX = "max"
    MIN = "min"
    COUNT = "count"
    FIRST = "first"
    LAST = "last"
    MEDIAN = "median"
    PERCENTILE_90 = "percentile_90"
    PERCENTILE_95 = "percentile_95"
    PERCENTILE_99 = "percentile_99"
    STDDEV = "stddev"
    VARIANCE = "variance"


class TimeRangeFormat(Enum):
    """Time range format"""
    RELATIVE = "relative"  # -1h, -24h, -7d
    ABSOLUTE = "absolute"  # 2024-01-01T00:00:00Z
    DURATION = "duration"  # 1h, 1d, 1w


@dataclass
class QueryOptions:
    """Options for historical data queries"""
    start: str = "-1h"
    stop: str = ""
    aggregate: str = ""  # e.g., "5m", "1h"
    aggregation: AggregationType = AggregationType.MEAN
    fill: str = "null"  # null, none, previous, linear, number
    limit: int = 10000
    filter_quality: str = ""  # Filter by quality level
    interpolation: str = "linear"  # linear, previous, next


@dataclass
class QueryResult:
    """Result of a historical data query"""
    device_id: str = ""
    point_name: str = ""
    start_time: str = ""
    end_time: str = ""
    count: int = 0
    data_points: list[dict] = field(default_factory=list)
    statistics: dict[str, Any] = field(default_factory=dict)
    query_ms: float = 0.0


@dataclass
class Statistics:
    """Statistical summary of data"""
    count: int = 0
    sum: float = 0.0
    mean: float = 0.0
    min: float = 0.0
    max: float = 0.0
    stddev: float = 0.0
    variance: float = 0.0
    median: float = 0.0
    percentile_90: float = 0.0
    percentile_95: float = 0.0
    percentile_99: float = 0.0
    first_value: float = 0.0
    last_value: float = 0.0
    first_time: str = ""
    last_time: str = ""


class HistoricalDataService:
    """Service for advanced historical data queries"""

    def __init__(self, influx_storage: InfluxDBStorage):
        self._influx = influx_storage

    async def query(
        self,
        device_id: str,
        point_name: str,
        options: QueryOptions | None = None,
    ) -> QueryResult:
        """Query historical data with advanced options"""
        import time

        options = options or QueryOptions()
        start_time = time.time()

        result = QueryResult(
            device_id=device_id,
            point_name=point_name,
            start_time=options.start,
            end_time=options.stop or "now",
        )

        # Perform base query
        data = await self._influx.query_points(
            device_id=device_id,
            point_name=point_name,
            start=options.start,
            stop=options.stop if options.stop else None,
            aggregate=options.aggregate,
            max_points=options.limit,
        )

        # Filter by quality if specified
        if options.filter_quality:
            data = [d for d in data if d.get("quality") == options.filter_quality]

        result.data_points = data
        result.count = len(data)

        # Calculate statistics if we have data
        if data:
            values = [d["value"] for d in data if d.get("value") is not None]
            if values:
                result.statistics = self._calculate_statistics(values, data)

        result.query_ms = (time.time() - start_time) * 1000
        return result

    def _calculate_statistics(self, values: list[float], data: list[dict]) -> dict[str, Any]:
        """Calculate statistical summary of values"""
        if not values:
            return {}

        n = len(values)
        stats = Statistics()

        # Basic statistics
        stats.count = n
        stats.sum = sum(values)
        stats.mean = stats.sum / n
        stats.min = min(values)
        stats.max = max(values)

        # Variance and standard deviation
        if n > 1:
            variance = sum((x - stats.mean) ** 2 for x in values) / (n - 1)
            stats.variance = variance
            stats.stddev = math.sqrt(variance)

        # Sorted values for percentiles
        sorted_values = sorted(values)
        stats.median = self._percentile(sorted_values, 50)
        stats.percentile_90 = self._percentile(sorted_values, 90)
        stats.percentile_95 = self._percentile(sorted_values, 95)
        stats.percentile_99 = self._percentile(sorted_values, 99)

        # First and last
        stats.first_value = values[0]
        stats.last_value = values[-1]
        if data:
            stats.first_time = data[0].get("time", "")
            stats.last_time = data[-1].get("time", "")

        return {
            "count": stats.count,
            "sum": round(stats.sum, 6),
            "mean": round(stats.mean, 6),
            "min": round(stats.min, 6),
            "max": round(stats.max, 6),
            "stddev": round(stats.stddev, 6),
            "variance": round(stats.variance, 6),
            "median": round(stats.median, 6),
            "percentile_90": round(stats.percentile_90, 6),
            "percentile_95": round(stats.percentile_95, 6),
            "percentile_99": round(stats.percentile_99, 6),
            "first_value": round(stats.first_value, 6),
            "last_value": round(stats.last_value, 6),
            "first_time": stats.first_time,
            "last_time": stats.last_time,
        }

    @staticmethod
    def _percentile(sorted_values: list[float], percentile: float) -> float:
        """Calculate percentile from sorted values"""
        if not sorted_values:
            return 0.0
        n = len(sorted_values)
        index = (percentile / 100) * (n - 1)
        lower = int(math.floor(index))
        upper = int(math.ceil(index))
        if lower == upper:
            return sorted_values[lower]
        weight = index - lower
        return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight

    async def query_multi_point(
        self,
        device_id: str,
        point_names: list[str],
        options: QueryOptions | None = None,
    ) -> dict[str, QueryResult]:
        """Query multiple points at once"""
        options = options or QueryOptions()
        results = {}

        # Query each point
        for point_name in point_names:
            result = await self.query(device_id, point_name, options)
            results[point_name] = result

        return results

    async def query_aggregated(
        self,
        device_id: str,
        point_name: str,
        window: str,
        aggregation: AggregationType = AggregationType.MEAN,
        start: str = "-24h",
        stop: str = "",
    ) -> list[dict]:
        """Query with time window aggregation"""
        # Use InfluxDB's built-in aggregation
        result = await self.query(
            device_id,
            point_name,
            QueryOptions(
                start=start,
                stop=stop,
                aggregate=window,
                aggregation=aggregation,
            ),
        )
        return result.data_points

    async def export_data(
        self,
        device_id: str,
        point_name: str,
        start: str = "-24h",
        stop: str = "",
        format: str = "json",
    ) -> str:
        """Export data to JSON or CSV format"""
        result = await self.query(
            device_id,
            point_name,
            QueryOptions(start=start, stop=stop, limit=100000),
        )

        if format.lower() == "csv":
            return self._export_csv(result)
        else:
            return self._export_json(result)

    def _export_json(self, result: QueryResult) -> str:
        """Export query result to JSON"""
        export_data = {
            "device_id": result.device_id,
            "point_name": result.point_name,
            "start_time": result.start_time,
            "end_time": result.end_time,
            "count": result.count,
            "statistics": result.statistics,
            "data_points": result.data_points,
        }
        return json.dumps(export_data, indent=2, ensure_ascii=False)

    def _export_csv(self, result: QueryResult) -> str:
        """Export query result to CSV"""
        output = io.StringIO()
        if not result.data_points:
            return ""

        fieldnames = ["time", "value", "quality"]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for point in result.data_points:
            writer.writerow({
                "time": point.get("time", ""),
                "value": point.get("value", ""),
                "quality": point.get("quality", ""),
            })
        return output.getvalue()

    async def query_trend(
        self,
        device_id: str,
        point_name: str,
        start: str = "-24h",
        stop: str = "",
        bucket_size: str = "1h",
    ) -> dict[str, Any]:
        """Analyze data trend over time"""
        # Query hourly aggregates
        hourly_data = await self.query_aggregated(
            device_id,
            point_name,
            window=bucket_size,
            aggregation=AggregationType.MEAN,
            start=start,
            stop=stop,
        )

        if not hourly_data:
            return {"trend": "unknown", "slope": 0, "data": []}

        # Calculate trend using linear regression
        values = [d["value"] for d in hourly_data if d.get("value") is not None]
        times = list(range(len(values)))

        if len(values) < 2:
            return {"trend": "insufficient_data", "slope": 0, "data": hourly_data}

        # Simple linear regression
        n = len(values)
        sum_x = sum(times)
        sum_y = sum(values)
        sum_xy = sum(t * v for t, v in zip(times, values))
        sum_x2 = sum(t * t for t in times)

        denominator = n * sum_x2 - sum_x * sum_x
        if denominator == 0:
            slope = 0
        else:
            slope = (n * sum_xy - sum_x * sum_y) / denominator

        # Determine trend direction
        if abs(slope) < 0.001:
            trend = "stable"
        elif slope > 0:
            trend = "increasing"
        else:
            trend = "decreasing"

        return {
            "trend": trend,
            "slope": round(slope, 6),
            "data_points": len(values),
            "data": hourly_data,
        }

    async def query_correlation(
        self,
        device_id: str,
        point1: str,
        point2: str,
        start: str = "-24h",
        stop: str = "",
    ) -> dict[str, Any]:
        """Calculate correlation between two points"""
        # Query both points
        result1 = await self.query(
            device_id, point1, QueryOptions(start=start, stop=stop, limit=5000)
        )
        result2 = await self.query(
            device_id, point2, QueryOptions(start=start, stop=stop, limit=5000)
        )

        if not result1.data_points or not result2.data_points:
            return {"correlation": None, "reason": "insufficient_data"}

        # Build time-indexed values
        values1 = {d["time"]: d["value"] for d in result1.data_points if d.get("value") is not None}
        values2 = {d["time"]: d["value"] for d in result2.data_points if d.get("value") is not None}

        # Find common timestamps
        common_times = set(values1.keys()) & set(values2.keys())
        if len(common_times) < 3:
            return {"correlation": None, "reason": "insufficient_common_points"}

        x_values = [values1[t] for t in sorted(common_times)]
        y_values = [values2[t] for t in sorted(common_times)]

        # Calculate Pearson correlation coefficient
        n = len(x_values)
        if n < 2:
            return {"correlation": None, "reason": "insufficient_data"}

        sum_x = sum(x_values)
        sum_y = sum(y_values)
        sum_xy = sum(x * y for x, y in zip(x_values, y_values))
        sum_x2 = sum(x * x for x in x_values)
        sum_y2 = sum(y * y for y in y_values)

        numerator = n * sum_xy - sum_x * sum_y
        denominator = math.sqrt((n * sum_x2 - sum_x ** 2) * (n * sum_y2 - sum_y ** 2))

        if denominator == 0:
            correlation = 0
        else:
            correlation = numerator / denominator

        # Interpret correlation
        abs_corr = abs(correlation)
        if abs_corr >= 0.8:
            strength = "strong"
        elif abs_corr >= 0.5:
            strength = "moderate"
        elif abs_corr >= 0.3:
            strength = "weak"
        else:
            strength = "negligible"

        direction = "positive" if correlation > 0 else "negative"

        return {
            "correlation": round(correlation, 4),
            "interpretation": f"{strength} {direction} correlation",
            "common_points": len(common_times),
        }


class DeviceShadowService:
    """Device shadow service for caching device state and offline data"""

    def __init__(self, influx_storage: InfluxDBStorage):
        self._influx = influx_storage
        self._shadows: dict[str, dict] = {}  # device_id -> shadow state
        self._offline_cache: dict[str, list[dict]] = {}  # device_id -> pending commands
        self._update_callbacks: list[dict] = []

    async def get_shadow(self, device_id: str) -> dict | None:
        """Get device shadow state"""
        # Try cache first
        if device_id in self._shadows:
            return self._shadows[device_id]

        # Query latest values from InfluxDB
        latest = await self._influx.query_latest(device_id)
        if not latest:
            return None

        # Build shadow
        shadow = {
            "device_id": device_id,
            "state": {
                "reported": {
                    point_name: data.get("value")
                    for point_name, data in latest.items()
                },
                "desired": {},
                "metadata": {
                    point_name: {
                        "timestamp": data.get("time"),
                        "quality": data.get("quality", "unknown"),
                    }
                    for point_name, data in latest.items()
                },
            },
            "timestamp": datetime.now(UTC).isoformat(),
            "version": 1,
        }

        self._shadows[device_id] = shadow
        return shadow

    async def update_reported_state(
        self,
        device_id: str,
        point_values: dict[str, float],
    ) -> dict:
        """Update device's reported state"""
        shadow = await self.get_shadow(device_id)
        if not shadow:
            shadow = {
                "device_id": device_id,
                "state": {"reported": {}, "desired": {}, "metadata": {}},
                "version": 0,
            }

        # Update reported values
        reported = shadow["state"]["reported"]
        metadata = shadow["state"]["metadata"]
        now = datetime.now(UTC).isoformat()

        for point_name, value in point_values.items():
            reported[point_name] = value
            metadata[point_name] = {
                "timestamp": now,
                "quality": "reported",
            }

        shadow["state"]["reported"] = reported
        shadow["state"]["metadata"] = metadata
        shadow["timestamp"] = now
        shadow["version"] = shadow.get("version", 0) + 1

        self._shadows[device_id] = shadow

        # Trigger callbacks
        for callback in self._update_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback.get("fn")):
                    await callback["fn"](device_id, shadow)
                else:
                    callback["fn"](device_id, shadow)
            except Exception as e:
                logger.debug("Shadow update callback error: %s", e)

        return shadow

    async def update_desired_state(
        self,
        device_id: str,
        point_values: dict[str, float],
    ) -> dict:
        """Update device's desired state (for command sending)"""
        shadow = await self.get_shadow(device_id)
        if not shadow:
            shadow = {
                "device_id": device_id,
                "state": {"reported": {}, "desired": {}, "metadata": {}},
                "version": 0,
            }

        desired = shadow["state"]["desired"]
        now = datetime.now(UTC).isoformat()

        for point_name, value in point_values.items():
            desired[point_name] = value

        shadow["state"]["desired"] = desired
        shadow["timestamp"] = now
        shadow["version"] = shadow.get("version", 0) + 1

        self._shadows[device_id] = shadow
        return shadow

    def register_update_callback(self, callback: callable) -> None:
        """Register callback for shadow updates"""
        self._update_callbacks.append({"fn": callback})

    def unregister_update_callback(self, callback: callable) -> None:
        """Unregister shadow update callback"""
        self._update_callbacks = [c for c in self._update_callbacks if c.get("fn") != callback]

    async def get_delta(self, device_id: str) -> dict | None:
        """Get delta between reported and desired state"""
        shadow = await self.get_shadow(device_id)
        if not shadow:
            return None

        reported = shadow["state"].get("reported", {})
        desired = shadow["state"].get("desired", {})

        delta = {}
        for point, desired_value in desired.items():
            reported_value = reported.get(point)
            if reported_value != desired_value:
                delta[point] = {
                    "desired": desired_value,
                    "reported": reported_value,
                }

        return delta if delta else None

    async def cache_offline_command(
        self,
        device_id: str,
        command: dict,
    ) -> bool:
        """Cache command for device when offline"""
        if device_id not in self._offline_cache:
            self._offline_cache[device_id] = []

        self._offline_cache[device_id].append({
            "command": command,
            "timestamp": datetime.now(UTC).isoformat(),
            "retries": 0,
        })

        logger.info("Command cached for offline device: %s", device_id)
        return True

    async def get_pending_commands(self, device_id: str) -> list[dict]:
        """Get pending commands for device"""
        return list(self._offline_cache.get(device_id, []))

    async def clear_pending_commands(self, device_id: str) -> int:
        """Clear pending commands for device"""
        if device_id in self._offline_cache:
            count = len(self._offline_cache[device_id])
            del self._offline_cache[device_id]
            return count
        return 0

    def get_all_shadows(self) -> list[dict]:
        """Get all device shadows"""
        return list(self._shadows.values())

    def clear_shadow(self, device_id: str) -> bool:
        """Clear device shadow from cache"""
        if device_id in self._shadows:
            del self._shadows[device_id]
            return True
        return False


# Global instances
_historical_service: HistoricalDataService | None = None
_shadow_service: DeviceShadowService | None = None


def get_historical_service(influx_storage: InfluxDBStorage | None = None) -> HistoricalDataService:
    """Get or create historical data service"""
    global _historical_service
    if _historical_service is None:
        if influx_storage is None:
            from edgelite.app import _app_state
            influx_storage = getattr(_app_state, "influx_storage", None)
        if influx_storage:
            _historical_service = HistoricalDataService(influx_storage)
    return _historical_service


def get_shadow_service(influx_storage: InfluxDBStorage | None = None) -> DeviceShadowService:
    """Get or create device shadow service"""
    global _shadow_service
    if _shadow_service is None:
        if influx_storage is None:
            from edgelite.app import _app_state
            influx_storage = getattr(_app_state, "influx_storage", None)
        if influx_storage:
            _shadow_service = DeviceShadowService(influx_storage)
    return _shadow_service
