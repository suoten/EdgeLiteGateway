"""数据查询业务逻辑"""

from __future__ import annotations

import csv
import io
import logging
from typing import TYPE_CHECKING, Any

from edgelite.storage.influx_storage import InfluxDBStorage
from edgelite.storage.sqlite_repo import DeviceRepo

if TYPE_CHECKING:
    from edgelite.services.historical_data import HistoricalDataService

logger = logging.getLogger(__name__)


# FIXED-P1: CSV 注入防护函数，防止以 =/+/-/@ 开头的单元格被 Excel 当作公式执行
def _sanitize_csv_cell(value: Any) -> Any:
    """Sanitize a CSV cell value to prevent formula injection.

    If the value is a string starting with =, +, -, or @, prefix it with a single quote
    to prevent Excel from interpreting it as a formula.
    """
    if isinstance(value, str) and value and value[0] in ("=", "+", "-", "@"):
        return f"'{value}"
    return value


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
        limit: int = 10000,
        offset: int = 0,
        agg_fn: str | None = None,
    ) -> list[dict]:
        """查询时序数据"""
        # FIXED-Bug29: 接受 limit/offset 参数，与 API 层调用匹配
        # 之前：API 层传入 limit/offset 但方法签名不接受，导致 TypeError → 500 错误
        # FIXED(严重): 新增 agg_fn 参数，传递用户指定的聚合函数到存储层
        # InfluxDB Flux 无原生 OFFSET，取 offset+limit 条后切片
        fetch_limit = limit + offset
        data = await self._influx.query_points(
            device_id, point_name, start, stop, aggregate, max_points=fetch_limit, agg_fn=agg_fn
        )
        if offset > 0:
            data = data[offset:]
        return data

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
        # FIXED-Bug10: query_points 参数名为 max_points 而非 limit，之前会抛 TypeError 导致导出 API 100% 失败
        data = await self._influx.query_points(device_id, point_name, start, stop, max_points=limit)

        if fmt == "csv":
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(["time", "device_id", "point_name", "value", "quality"])
            for record in data:
                # FIXED-P1: CSV 注入防护，以 =/+/-/@ 开头的单元格 Excel 会当作公式执行
                writer.writerow(
                    [
                        _sanitize_csv_cell(record.get("time", "")),
                        _sanitize_csv_cell(record.get("device_id", "")),
                        _sanitize_csv_cell(record.get("point_name", "")),
                        _sanitize_csv_cell(record.get("value", "")),
                        _sanitize_csv_cell(record.get("quality", "")),
                    ]
                )
            return output.getvalue()
        else:
            # JSON格式
            import json

            return json.dumps(data, ensure_ascii=False, indent=2)

    async def stream_export_data(
        self,
        device_id: str,
        point_name: str,
        start: str,
        stop: str | None = None,
        fmt: str = "csv",
        limit: int = 50000,
        batch_size: int = 1000,
    ):
        """R9-S-16: 流式导出数据，分批查询并逐步 yield，避免全量加载到内存。

        Args:
            device_id: 设备ID
            point_name: 点位名称
            start: 开始时间
            stop: 结束时间
            fmt: 导出格式 csv 或 json
            limit: 总记录数上限
            batch_size: 每批查询的记录数

        Yields:
            bytes: 格式化后的数据块
        """
        import json

        remaining = limit
        offset = 0
        first_batch = True

        while remaining > 0:
            current_batch = min(batch_size, remaining)
            # 分批查询，使用 offset 实现分页
            data = await self._influx.query_points(
                device_id, point_name, start, stop,
                max_points=current_batch,
                offset=offset,
            )
            if not data:
                break

            if fmt == "csv":
                output = io.StringIO()
                writer = csv.writer(output)
                # 仅在第一批输出 CSV 表头
                if first_batch:
                    writer.writerow(["time", "device_id", "point_name", "value", "quality"])
                for record in data:
                    # FIXED-P1: CSV 注入防护
                    writer.writerow(
                        [
                            _sanitize_csv_cell(record.get("time", "")),
                            _sanitize_csv_cell(record.get("device_id", "")),
                            _sanitize_csv_cell(record.get("point_name", "")),
                            _sanitize_csv_cell(record.get("value", "")),
                            _sanitize_csv_cell(record.get("quality", "")),
                        ]
                    )
                chunk = output.getvalue()
                if chunk:
                    yield chunk.encode("utf-8")
            else:
                # JSON 流式输出：第一批以 [ 开头，后续批次以 , 分隔，最后以 ] 结束
                if first_batch:
                    yield b"["
                else:
                    yield b","
                # 输出本批记录的 JSON（不含外层数组括号）
                batch_json = json.dumps(data, ensure_ascii=False)
                # 去掉外层的 [ 和 ]
                yield batch_json[1:-1].encode("utf-8")

            first_batch = False
            offset += len(data)
            remaining -= len(data)
            # 若返回数据少于请求量，说明已无更多数据
            if len(data) < current_batch:
                break

        # JSON 格式需要闭合数组括号
        if fmt != "csv":
            if first_batch:
                # 没有任何数据输出，返回空数组
                yield b"[]"
            else:
                yield b"]"

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
        # FIXED-P1: 限制 point_names 数量，防止大量测点查询导致性能问题/OOM
        max_points = 100
        if len(point_names) > max_points:
            logger.warning(
                "query_multi_point: point_names count %d exceeds limit %d, truncating",
                len(point_names), max_points,
            )
            point_names = point_names[:max_points]

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
