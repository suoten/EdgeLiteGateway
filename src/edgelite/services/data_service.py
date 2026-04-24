"""数据查询业务逻辑"""

from __future__ import annotations

import csv
import io
from typing import Any

from edgelite.storage.influx_storage import InfluxDBStorage
from edgelite.storage.sqlite_repo import DeviceRepo


class DataService:
    """数据查询业务逻辑"""

    def __init__(self, influx_storage: InfluxDBStorage, device_repo: DeviceRepo):
        self._influx = influx_storage
        self._device_repo = device_repo

    async def query_timeseries(
        self,
        device_id: str,
        point_name: str,
        start: str,
        stop: str | None = None,
        aggregate: str | None = None,
    ) -> list[dict]:
        """查询时序数据"""
        return await self._influx.query_points(device_id, point_name, start, stop, aggregate)

    async def get_latest_points(self, device_id: str, point_names: list[str] | None = None) -> dict[str, Any]:
        """获取设备最新测点值"""
        return await self._influx.query_latest(device_id, point_names)

    async def export_data(
        self,
        device_id: str,
        point_name: str,
        start: str,
        stop: str | None = None,
        format: str = "csv",
    ) -> str:
        """导出数据"""
        data = await self._influx.query_points(device_id, point_name, start, stop)

        if format == "csv":
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(["time", "device_id", "point_name", "value", "quality"])
            for record in data:
                writer.writerow([
                    record.get("time", ""),
                    record.get("device_id", ""),
                    record.get("point_name", ""),
                    record.get("value", ""),
                    record.get("quality", ""),
                ])
            return output.getvalue()
        else:
            # JSON格式
            import json
            return json.dumps(data, ensure_ascii=False, indent=2)
