"""InfluxDB时序存储

性能修复：所有influxdb-client同步API通过asyncio.to_thread调用，
避免阻塞事件循环。influxdb-client的write_api/query_api均为同步API，
直接在async函数中调用会阻塞整个事件循环。
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import Any

from influxdb_client.client.influxdb_client import InfluxDBClient
from influxdb_client.client.write.point import Point
from influxdb_client.client.write_api import WriteOptions

from edgelite.config import get_config

logger = logging.getLogger(__name__)


class InfluxDBStorage:
    """InfluxDB时序数据存储"""

    def __init__(self):
        config = get_config()
        self._url = config.influxdb.url
        self._token = config.influxdb.token
        self._org = config.influxdb.org
        self._bucket = config.influxdb.bucket
        self._client: InfluxDBClient | None = None
        self._write_api = None
        self._query_api = None
        self._available = False
        self._fail_count = 0

    async def connect(self) -> None:
        """建立InfluxDB连接"""
        try:
            self._client = InfluxDBClient(
                url=self._url,
                token=self._token,
                org=self._org,
                timeout=5000,
            )
            # 验证连接（带超时保护，通过to_thread避免阻塞）
            try:
                health = await asyncio.wait_for(
                    asyncio.to_thread(self._client.health),
                    timeout=5.0,
                )
                self._available = health.status == "pass"
            except (TimeoutError, Exception) as e:
                self._available = False
                logger.warning("InfluxDB健康检查超时或失败: %s", e)

            if self._available:
                config = get_config()
                self._write_api = self._client.write_api(
                    write_options=WriteOptions(
                        batch_size=config.influxdb.batch_size,
                        flush_interval=config.influxdb.flush_interval,
                    )
                )
                self._query_api = self._client.query_api()
                logger.info("InfluxDB连接成功: %s", self._url)
            else:
                logger.warning("InfluxDB不可用，将使用缓存模式")
        except Exception as e:
            self._available = False
            logger.warning("InfluxDB连接失败: %s，将使用缓存模式", e)

    async def close(self) -> None:
        """关闭连接"""
        if self._write_api:
            await asyncio.to_thread(self._write_api.close)
        if self._client:
            await asyncio.to_thread(self._client.close)
            self._client = None
        self._available = False

    @property
    def available(self) -> bool:
        return self._available

    async def check_health(self) -> bool:
        """检查InfluxDB可用性"""
        if not self._client:
            return False
        try:
            health = await asyncio.to_thread(self._client.health)
            self._available = health.status == "pass"
            if self._available:
                self._fail_count = 0
            return self._available
        except Exception:
            self._available = False
            return False

    async def write_point(
        self,
        device_id: str,
        point_name: str,
        value: float,
        timestamp: datetime | None = None,
        quality: str = "good",
    ) -> bool:
        """写入单条测点数据"""
        if not self._available or not self._write_api:
            return False

        try:
            import math

            float_val = float(value)
            if math.isnan(float_val) or math.isinf(float_val):
                logger.warning(
                    "InfluxDB写入跳过: 值为NaN/Infinity (device=%s, point=%s)",
                    device_id,
                    point_name,
                )
                return False

            point = (
                Point("device_points")
                .tag("device_id", device_id)
                .tag("point_name", point_name)
                .tag("quality", quality)
                .field("value", float_val)
            )
            if timestamp:
                point = point.time(timestamp)

            await asyncio.to_thread(self._write_api.write, bucket=self._bucket, record=point)
            self._fail_count = 0
            return True
        except Exception as e:
            logger.error("InfluxDB写入失败: %s", e)
            self._fail_count += 1
            if self._fail_count >= 3:
                self._available = False
            return False

    async def write_points_batch(self, records: list[dict]) -> bool:
        """批量写入测点数据

        records格式: [{"device_id": ..., "point_name": ..., "value": ...,
                       "timestamp": ..., "quality": ...}]
        """
        if not self._available or not self._write_api:
            return False

        try:
            import math

            points = []
            for rec in records:
                device_id = rec.get("device_id")
                point_name = rec.get("point_name")
                raw_value = rec.get("value")
                if not device_id or not point_name or raw_value is None:
                    logger.warning("批量写入跳过: 缺少必填字段 (device_id/point_name/value)")
                    continue
                float_val = float(raw_value)
                if math.isnan(float_val) or math.isinf(float_val):
                    logger.warning(
                        "批量写入跳过: 值为NaN/Infinity (device=%s, point=%s)",
                        device_id,
                        point_name,
                    )
                    continue
                p = (
                    Point("device_points")
                    .tag("device_id", device_id)
                    .tag("point_name", point_name)
                    .tag("quality", rec.get("quality", "good"))
                    .field("value", float_val)
                )
                if rec.get("timestamp"):
                    p = p.time(rec["timestamp"])
                points.append(p)

            await asyncio.to_thread(self._write_api.write, bucket=self._bucket, record=points)
            self._fail_count = 0
            return True
        except Exception as e:
            logger.error("InfluxDB批量写入失败: %s", e)
            self._fail_count += 1
            if self._fail_count >= 3:
                self._available = False
            return False

    @staticmethod
    def _escape_flux_value(value: str) -> str:
        """转义 Flux 查询中的字符串值，防止注入"""
        return (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("'", "\\'")
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("|", "\\|")
        )

    async def query_points(
        self,
        device_id: str,
        point_name: str,
        start: str,
        stop: str | None = None,
        aggregate: str | None = None,
    ) -> list[dict]:
        """查询时序数据"""
        if not self._available or not self._query_api:
            return []

        time_range_re = re.compile(r"^-?\d+[smhdwMy]$|^\d{4}-\d{2}-\d{2}")
        for param_name, param_val in [("start", start), ("stop", stop)]:
            if param_val and not (
                time_range_re.match(param_val)
                or param_val.lstrip("-").replace(".", "", 1).isdigit()
            ):
                logger.error("非法的时间范围参数 %s: %s", param_name, param_val)
                return []

        safe_device_id = self._escape_flux_value(device_id)
        safe_point_name = self._escape_flux_value(point_name)
        safe_start = self._escape_flux_value(start)
        safe_stop = self._escape_flux_value(stop) if stop else ""

        stop_clause = f", stop: {safe_stop}" if stop else ""
        flux = f"""
from(bucket: "{self._bucket}")
  |> range(start: {safe_start}{stop_clause})
  |> filter(fn: (r) => r._measurement == "device_points")
  |> filter(fn: (r) => r.device_id == "{safe_device_id}")
  |> filter(fn: (r) => r.point_name == "{safe_point_name}")
"""
        if aggregate:
            if not re.match(r"^\d+[smh]$", aggregate):
                logger.error("非法的聚合窗口参数: %s", aggregate)
                return []
            flux += f"  |> aggregateWindow(every: {aggregate}, fn: mean, createEmpty: false)\n"

        flux += '  |> yield(name: "result")'

        try:
            tables = await asyncio.to_thread(self._query_api.query, flux, self._org)
            results = []
            for table in tables:
                for record in table.records:
                    results.append(
                        {
                            "time": record.get_time().isoformat() if record.get_time() else None,
                            "value": record.get_value(),
                            "device_id": record.values.get("device_id"),
                            "point_name": record.values.get("point_name"),
                            "quality": record.values.get("quality"),
                        }
                    )
            return results
        except Exception as e:
            logger.error("InfluxDB查询失败: %s", e)
            return []

    async def query_latest(
        self, device_id: str, point_names: list[str] | None = None
    ) -> dict[str, Any]:
        """查询设备最新测点值"""
        if not self._available or not self._query_api:
            return {}

        safe_device_id = self._escape_flux_value(device_id)
        point_filter = ""
        if point_names:
            safe_names = ", ".join(f'"{self._escape_flux_value(n)}"' for n in point_names)
            point_filter = (
                f"  |> filter(fn: (r) => contains(value: r.point_name, set: [{safe_names}]))\n"
            )

        flux = f"""
from(bucket: "{self._bucket}")
  |> range(start: -1h)
  |> filter(fn: (r) => r._measurement == "device_points")
  |> filter(fn: (r) => r.device_id == "{safe_device_id}")
  {point_filter}
  |> last()
"""

        try:
            tables = await asyncio.to_thread(self._query_api.query, flux, self._org)
            result = {}
            for table in tables:
                for record in table.records:
                    pn = record.values.get("point_name")
                    if pn:
                        result[pn] = {
                            "value": record.get_value(),
                            "time": record.get_time().isoformat() if record.get_time() else None,
                            "quality": record.values.get("quality", "good"),
                        }
            return result
        except Exception as e:
            logger.error("InfluxDB最新值查询失败: %s", e)
            return {}
