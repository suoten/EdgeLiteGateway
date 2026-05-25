"""数据查询业务逻辑"""

from __future__ import annotations

import csv
import io
import logging
from typing import Any

from edgelite.storage.influx_storage import InfluxDBStorage
from edgelite.storage.sqlite_repo import DeviceRepo

logger = logging.getLogger(__name__)


class DataService:
    """数据查询业务逻辑"""

    def __init__(self, influx_storage: InfluxDBStorage, device_repo: DeviceRepo):
        self._influx = influx_storage
        self._device_repo = device_repo
        self._historical_svc: HistoricalDataService | None = None

    @property
    def historical_service(self) -> HistoricalDataService:
        """Lazy-load HistoricalDataService"""
        if self._historical_svc is None:
            from edgelite.services.historical_data import HistoricalDataService
            self._historical_svc = HistoricalDataService(self._influx)
        return self._historical_svc

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

    async def get_latest_points(
        self, device_id: str, point_names: list[str] | None = None
    ) -> dict[str, Any]:
        """获取设备最新测点值"""
        return await self._influx.query_latest(device_id, point_names)

    async def export_data(
        self,
        device_id: str,
        point_name: str,
        start: str,
        stop: str | None = None,
        fmt: str = "csv",
        limit: int = 100000,  # FIXED-P2: export_data无数据量限制，长时间范围导出可能OOM，添加limit参数
    ) -> str:
        """导出数据"""
        data = await self._influx.query_points(device_id, point_name, start, stop, limit=limit)

        if fmt == "csv":
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(["time", "device_id", "point_name", "value", "quality"])
            for record in data:
                writer.writerow(
                    [
                        record.get("time", ""),
                        record.get("device_id", ""),
                        record.get("point_name", ""),
                        record.get("value", ""),
                        record.get("quality", ""),
                    ]
                )
            return output.getvalue()
        else:
            # JSON格式
            import json

            return json.dumps(data, ensure_ascii=False, indent=2)

    async def query_trend(
        self,
        device_id: str,
        point_name: str,
        start: str = "-24h",
        stop: str | None = None,
        bucket_size: str = "1h",
    ) -> dict[str, Any]:
        """Query data trend with linear regression analysis"""
        return await self.historical_service.query_trend(
            device_id, point_name, start=start, stop=stop or "", bucket_size=bucket_size,
        )

    async def query_correlation(
        self,
        device_id: str,
        point1: str,
        point2: str,
        start: str = "-24h",
        stop: str | None = None,
    ) -> dict[str, Any]:
        """Calculate correlation between two data points"""
        return await self.historical_service.query_correlation(
            device_id, point1, point2, start=start, stop=stop or "",
        )

    async def get_statistics(
        self,
        device_id: str,
        point_name: str,
        start: str = "-24h",
        stop: str | None = None,
        aggregate: str | None = None,
    ) -> dict[str, Any]:
        """Get statistical summary of data points"""
        from edgelite.services.historical_data import QueryOptions
        options = QueryOptions(start=start, stop=stop or "", aggregate=aggregate or "")
        result = await self.historical_service.query(device_id, point_name, options)
        return {
            "device_id": result.device_id,
            "point_name": result.point_name,
            "count": result.count,
            "statistics": result.statistics,
        }

    async def query_multi_point(
        self,
        device_id: str,
        point_names: list[str],
        start: str = "-1h",
        stop: str | None = None,
        aggregate: str | None = None,
    ) -> dict[str, Any]:
        """Query multiple data points at once"""
        from edgelite.services.historical_data import QueryOptions
        options = QueryOptions(start=start, stop=stop or "", aggregate=aggregate or "")
        results = await self.historical_service.query_multi_point(
            device_id, point_names, options,
        )
        return {
            name: {
                "device_id": r.device_id,
                "point_name": r.point_name,
                "count": r.count,
                "data_points": r.data_points,
                "statistics": r.statistics,
            }
            for name, r in results.items()
        }
