"""InfluxDB时序存储

性能修复：所有influxdb-client同步API通过asyncio.to_thread调用，
避免阻塞事件循环。influxdb-client的write_api/query_api均为同步API，
直接在async函数中调用会阻塞整个事件循环。

降级方案：InfluxDB不可用时自动降级到SQLite时序存储，
InfluxDB恢复后SQLite中的数据增量同步回InfluxDB。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any

from influxdb_client.client.influxdb_client import InfluxDBClient
from influxdb_client.client.write.point import Point
from influxdb_client.client.write_api import WriteOptions

from edgelite.config import get_config
from edgelite.constants import _INFLUX_CONNECT_TIMEOUT_MS, _INFLUX_WRITE_TIMEOUT_S
from edgelite.storage.sqlite_ts import SqliteTimeSeriesStorage

logger = logging.getLogger(__name__)


class InfluxDBStorage:
    """InfluxDB时序数据存储，支持SQLite降级"""

    def __init__(self):
        config = get_config()
        self._url = config.influxdb.url
        self._token = config.influxdb.token
        self._org = config.influxdb.org
        self._bucket = config.influxdb.bucket
        self._retention_days = config.influxdb.retention_days
        self._client: InfluxDBClient | None = None
        self._write_api = None
        self._query_api = None
        self._buckets_api = None
        self._available = False
        self._fail_count = 0
        # SQLite降级存储
        self._sqlite_ts: SqliteTimeSeriesStorage | None = None
        self._sync_task: asyncio.Task | None = None
        self._sync_running = False

    async def connect(self) -> None:
        """建立InfluxDB连接，失败时初始化SQLite降级存储"""
        try:
            self._client = InfluxDBClient(
                url=self._url,
                token=self._token,
                org=self._org,
                timeout=_INFLUX_CONNECT_TIMEOUT_MS,  # FIXED: 原问题-timeout=5000魔法数字
            )
            # 验证连接（带超时保护，通过to_thread避免阻塞）
            try:
                health = await asyncio.wait_for(
                    asyncio.to_thread(self._client.health),
                    timeout=_INFLUX_WRITE_TIMEOUT_S,  # FIXED: 原问题-timeout=5.0魔法数字
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
                self._buckets_api = self._client.buckets_api()
                logger.info("InfluxDB连接成功: %s", self._url)
                await self._ensure_retention_policy()
            else:
                logger.warning("InfluxDB不可用，将使用SQLite降级模式")
                await self._init_sqlite_fallback()
        except Exception as e:
            self._available = False
            logger.warning("InfluxDB连接失败: %s，将使用SQLite降级模式", e)
            await self._init_sqlite_fallback()

        # 启动同步循环
        config = get_config()
        if config.influxdb.auto_sync_on_recovery:
            await self.start_sync()

    async def _init_sqlite_fallback(self) -> None:
        """初始化SQLite降级存储"""
        if self._sqlite_ts is not None:
            return
        config = get_config()
        if config.influxdb.fallback_backend == "sqlite":
            self._sqlite_ts = SqliteTimeSeriesStorage(
                db_path=config.influxdb.sqlite_ts_path
            )
            await self._sqlite_ts.start()
            logger.info("SQLite降级存储已初始化: %s", config.influxdb.sqlite_ts_path)

    async def _ensure_sqlite_started(self) -> None:
        """确保SQLite降级存储已启动"""
        if self._sqlite_ts is None:
            await self._init_sqlite_fallback()

    async def _ensure_retention_policy(self) -> None:
        """Ensure InfluxDB bucket has the configured retention policy, apply if mismatch"""
        if not self._client or not self._buckets_api:
            return
        try:
            bucket = await asyncio.to_thread(self._buckets_api.find_bucket_by_name, self._bucket)
            if bucket:
                current_rp = getattr(bucket, "retention_rules", None)
                expected_secs = self._retention_days * 86400
                if current_rp and hasattr(current_rp, "retention_secs"):
                    secs = current_rp.retention_secs
                    if secs > 0 and secs != expected_secs:
                        logger.info(
                            "InfluxDB retention: %s current=%d days, configured=%d days, "
                            "applying update",
                            self._bucket, secs // 86400, self._retention_days,
                        )
                        await self._apply_retention_policy(bucket, expected_secs)
                    elif secs == 0:
                        logger.info(
                            "InfluxDB retention: %s unlimited, configured=%d days, applying update",
                            self._bucket, self._retention_days,
                        )
                        await self._apply_retention_policy(bucket, expected_secs)
                    else:
                        logger.debug(
                            "InfluxDB retention policy already correct: %s (%d days)",
                            self._bucket, self._retention_days,
                        )
                else:
                    # No retention rules set, apply configured policy
                    logger.info(
                        "InfluxDB retention: %s no rules set, applying %d days",
                        self._bucket, self._retention_days,
                    )
                    await self._apply_retention_policy(bucket, expected_secs)
            else:
                logger.warning("InfluxDB bucket not found: %s", self._bucket)
        except Exception as e:
            logger.debug("InfluxDB retention policy check failed: %s", e)

    async def _apply_retention_policy(self, bucket: Any, expected_secs: int) -> None:
        """Apply retention policy update to the InfluxDB bucket"""
        try:
            from influxdb_client.domain.bucket_retention_rules import BucketRetentionRules

            bucket.retention_rules = BucketRetentionRules(
                type="expire",
                every_seconds=expected_secs,
            )
            await asyncio.to_thread(self._buckets_api.update_bucket, bucket)
            logger.info(
                "InfluxDB retention policy updated: %s -> %d days",
                self._bucket, expected_secs // 86400,
            )
        except Exception as e:
            logger.error("Failed to update InfluxDB retention policy: %s", e)

    async def cleanup_expired_data(self) -> int:
        """Clean up expired data older than retention_days via Flux query + delete"""
        if not self._available or not self._query_api:
            # InfluxDB不可用时，清理SQLite中的旧数据
            if self._sqlite_ts:
                return await self._sqlite_ts.cleanup_old_data(self._retention_days)
            return 0
        return await self._delete_old_data()

    async def _delete_old_data(self) -> int:
        """Delete old data from InfluxDB using DeletePredicateRequest"""
        if not self._available or not self._query_api or not self._client:
            return 0
        try:
            from datetime import timedelta

            from influxdb_client import DeletePredicateRequest

            cutoff = datetime.now(UTC) - timedelta(days=self._retention_days)
            predicate = DeletePredicateRequest(
                start="1970-01-01T00:00:00Z",
                stop=cutoff.isoformat(),
            )
            delete_api = self._client.delete_api()
            await asyncio.to_thread(
                delete_api.delete,
                predicate,
                self._bucket,
                self._org,
            )
            logger.info(
                "InfluxDB expired data cleaned up (retention=%d days)", self._retention_days
            )
            return 1
        except Exception as e:
            logger.error("InfluxDB expired data cleanup failed: %s", e)
            return 0

    async def close(self) -> None:
        """关闭连接"""
        await self.stop_sync()
        if self._write_api:
            await asyncio.to_thread(self._write_api.close)
        if self._client:
            await asyncio.to_thread(self._client.close)
            self._client = None
        if self._sqlite_ts:
            await self._sqlite_ts.stop()
            self._sqlite_ts = None
        self._available = False

    @property
    def available(self) -> bool:
        return self._available

    @property
    def using_fallback(self) -> bool:
        """是否正在使用SQLite降级模式"""
        return not self._available and self._sqlite_ts is not None

    async def check_health(self) -> bool:
        """检查InfluxDB可用性，失败后尝试自动恢复"""
        if not self._client:
            return False
        try:
            health = await asyncio.to_thread(self._client.health)
            is_healthy = health.status == "pass"
            if is_healthy:
                self._fail_count = 0  # FIXED: P2-1 成功后重置失败计数，防止一次抖动后永久不可用
                if not self._available:
                    self._available = True
                    from influxdb_client.client.write_api import WriteOptions

                    cfg = get_config()
                    self._write_api = self._client.write_api(
                        write_options=WriteOptions(
                            batch_size=cfg.influxdb.batch_size,
                            flush_interval=cfg.influxdb.flush_interval,
                        )
                    )
                    self._query_api = self._client.query_api()
                    self._buckets_api = self._client.buckets_api()
                    logger.info("InfluxDB connection recovered")
            else:
                self._available = False
            return is_healthy
        except Exception as e:
            self._available = False
            logger.debug("InfluxDB health check exception: %s", e)
            return False

    async def write_point(
        self,
        device_id: str,
        point_name: str,
        value: float,
        timestamp: datetime | None = None,
        quality: str = "good",
    ) -> bool:
        """写入单条测点数据

        当InfluxDB不可用时，将数据写入SQLite降级存储以便后续同步恢复。
        """
        if not self._available or not self._write_api:
            await self._fallback_write(device_id, point_name, value, timestamp, quality)
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
            logger.error("InfluxDB写入失败: %s，降级到SQLite", e)
            await self._fallback_write(device_id, point_name, value, timestamp, quality)
            self._fail_count += 1
            if self._fail_count >= 3:
                self._available = False
            return False

    async def _fallback_write(
        self,
        device_id: str,
        point_name: str,
        value: float | Any,
        timestamp: datetime | None,
        quality: str,
    ) -> None:
        """InfluxDB不可用时将数据写入SQLite降级存储"""
        try:
            await self._ensure_sqlite_started()
            if self._sqlite_ts is None:
                return

            timestamp_ns = None
            if timestamp:
                ts_epoch = timestamp.timestamp()
                timestamp_ns = int(ts_epoch * 1e9)
            await self._sqlite_ts.write_point(
                measurement="device_points",
                device_id=device_id,
                point_name=point_name,
                value=value,
                quality=quality,
                timestamp_ns=timestamp_ns,
                tags={"device_id": device_id, "point_name": point_name, "quality": quality},
            )
        except Exception:
            logger.error("SQLite降级写入失败: 数据可能在InfluxDB中断期间丢失")

    async def _fallback_to_cache(
        self,
        device_id: str,
        point_name: str,
        value: float | Any,
        timestamp: datetime | None,
        quality: str,
    ) -> bool:
        """InfluxDB不可用时将数据写入本地缓存，防止断网丢数据"""
        try:
            from edgelite.app import _app_state

            cache_manager = getattr(_app_state, "cache_manager", None)
            if cache_manager is None:
                logger.debug("CacheManager not available, data will be lost during outage")
                return False
            ts_str = timestamp.isoformat() if timestamp else datetime.now(UTC).isoformat()
            return await cache_manager.add_to_cache(
                measurement="device_points",
                tags={"device_id": device_id, "point_name": point_name, "quality": quality},
                fields={"value": float(value)},
                timestamp=ts_str,
            )
        except Exception:
            logger.error("Cache fallback failed: data may be lost during InfluxDB outage")
            return False

    async def write_points_batch(self, records: list[dict]) -> bool:
        """批量写入测点数据

        当InfluxDB不可用时，将数据写入SQLite降级存储以便后续同步恢复。
        """
        if not self._available or not self._write_api:
            await self._fallback_batch_write(records)
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

            if not points:
                return True
            await asyncio.to_thread(self._write_api.write, bucket=self._bucket, record=points)
            self._fail_count = 0
            return True
        except Exception as e:
            logger.error("InfluxDB批量写入失败: %s，降级到SQLite", e)
            await self._fallback_batch_write(records)
            self._fail_count += 1
            if self._fail_count >= 3:
                self._available = False
            return False

    async def _fallback_batch_write(self, records: list[dict]) -> None:
        """InfluxDB不可用时批量写入SQLite降级存储"""
        try:
            await self._ensure_sqlite_started()
            if self._sqlite_ts is None:
                return

            sqlite_points = []
            for rec in records:
                timestamp_ns = None
                ts = rec.get("timestamp")
                if ts:
                    if isinstance(ts, datetime):
                        timestamp_ns = int(ts.timestamp() * 1e9)
                    elif isinstance(ts, (int, float)):
                        timestamp_ns = int(ts)
                sqlite_points.append({
                    "measurement": "device_points",
                    "device_id": rec.get("device_id", ""),
                    "point_name": rec.get("point_name", ""),
                    "value": rec.get("value"),
                    "quality": rec.get("quality", "good"),
                    "timestamp_ns": timestamp_ns,
                    "tags": {
                        "device_id": rec.get("device_id", ""),
                        "point_name": rec.get("point_name", ""),
                        "quality": rec.get("quality", "good"),
                    },
                })
            await self._sqlite_ts.write_points_batch(sqlite_points)
        except Exception:
            logger.error("SQLite降级批量写入失败: 数据可能在InfluxDB中断期间丢失")

    @staticmethod
    def _escape_flux_value(value: str) -> str:
        """转义 Flux 查询中的字符串值，使用单引号（Flux 要求单引号包裹字符串字面量）"""
        escaped = (
            value.replace("\\", "\\\\")
            .replace("'", "\\'")
            .replace("\n", "\\n")
            .replace("\r", "\\r")
        )
        return f"'{escaped}'"

    async def query_points(
        self,
        device_id: str,
        point_name: str,
        start: str,
        stop: str | None = None,
        aggregate: str | None = None,
        max_points: int = 10000,
    ) -> list[dict]:
        """查询时序数据，InfluxDB不可用时从SQLite查询"""
        if not self._available or not self._query_api:
            return await self._fallback_query_points(
                device_id, point_name, start, stop, aggregate, max_points
            )

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
        # FIXED: P2-1 Flux 字符串字面量必须用单引号包裹，_escape_flux_value 已返回带单引号的值
        flux = f"""
from(bucket: "{self._bucket}")
  |> range(start: {safe_start}{stop_clause})
  |> filter(fn: (r) => r._measurement == "device_points")
  |> filter(fn: (r) => r.device_id == {safe_device_id})
  |> filter(fn: (r) => r.point_name == {safe_point_name})
"""
        if aggregate:
            if not re.match(r"^\d+[smh]$", aggregate):
                logger.error("非法的聚合窗口参数: %s", aggregate)
                return []
            flux += f"  |> aggregateWindow(every: {aggregate}, fn: mean, createEmpty: false)\n"
            flux += f"  |> limit(n: {max_points})\n"
            flux += '  |> yield(name: "result")'
        else:
            flux += f"  |> limit(n: {max_points})\n"
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

    async def _fallback_query_points(
        self,
        device_id: str,
        point_name: str,
        start: str,
        stop: str | None = None,
        aggregate: str | None = None,
        max_points: int = 10000,
    ) -> list[dict]:
        """InfluxDB不可用时从SQLite查询时序数据"""
        if not self._sqlite_ts:
            return []

        try:
            start_ns = self._parse_time_to_ns(start)
            stop_ns = self._parse_time_to_ns(stop) if stop else None

            window_seconds = None
            agg_fn = None
            if aggregate:
                agg_fn = "mean"
                window_seconds = self._parse_aggregate_to_seconds(aggregate)

            return await self._sqlite_ts.query_points(
                device_id=device_id,
                point_name=point_name,
                start_ns=start_ns,
                stop_ns=stop_ns,
                aggregate=agg_fn,
                window_seconds=window_seconds,
                limit=max_points,
            )
        except Exception as e:
            logger.error("SQLite降级查询失败: %s", e)
            return []

    async def query_latest(
        self, device_id: str, point_names: list[str] | None = None
    ) -> dict[str, Any]:
        """查询设备最新测点值，InfluxDB不可用时从SQLite查询"""
        if not self._available or not self._query_api:
            return await self._fallback_query_latest(device_id, point_names)

        safe_device_id = self._escape_flux_value(device_id)
        point_filter = ""
        if point_names:
            safe_names = ", ".join(self._escape_flux_value(n) for n in point_names)
            # FIXED: P2-1 Flux point_name filter now uses single-quoted escaped values
            point_filter = (
                f"  |> filter(fn: (r) => contains(value: r.point_name, set: [{safe_names}]))\n"
            )

        # FIXED: P2-1 Flux string literals must use single quotes
        flux = f"""
from(bucket: "{self._bucket}")
  |> range(start: -1h)
  |> filter(fn: (r) => r._measurement == "device_points")
  |> filter(fn: (r) => r.device_id == {safe_device_id})
  {point_filter}
  |> last()
"""

        try:
            tables = await asyncio.wait_for(
                asyncio.to_thread(self._query_api.query, flux, self._org),
                timeout=10.0,
            )
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
        except TimeoutError:
            logger.warning("InfluxDB最新值查询超时(10s): device=%s", device_id)
            return {}
        except Exception as e:
            logger.error("InfluxDB最新值查询失败: %s", e)
            return {}

    async def _fallback_query_latest(
        self, device_id: str, point_names: list[str] | None = None
    ) -> dict[str, Any]:
        """InfluxDB不可用时从SQLite查询最新测点值"""
        if not self._sqlite_ts or not point_names:
            return {}
        try:
            return await self._sqlite_ts.query_latest(device_id, point_names)
        except Exception as e:
            logger.error("SQLite降级最新值查询失败: %s", e)
            return {}

    @staticmethod
    def _parse_time_to_ns(time_str: str) -> int:
        """将Flux风格的时间字符串转换为纳秒时间戳"""
        now_ns = time.time_ns()

        # 相对时间: -1h, -30m, -2d 等
        rel_match = re.match(r"^-(\d+)([smhdwMy])$", time_str)
        if rel_match:
            amount = int(rel_match.group(1))
            unit = rel_match.group(2)
            multipliers = {
                "s": 1, "m": 60, "h": 3600, "d": 86400,
                "w": 604800, "M": 2592000, "y": 31536000,
            }
            seconds = amount * multipliers.get(unit, 1)
            return now_ns - int(seconds * 1e9)

        # 绝对时间: 2024-01-01T00:00:00Z
        try:
            dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1e9)
        except (ValueError, TypeError):
            pass

        # 纯数字（纳秒）
        try:
            return int(time_str)
        except (ValueError, TypeError):
            pass

        return now_ns - 3600_000_000_000  # 默认1小时前

    @staticmethod
    def _parse_aggregate_to_seconds(aggregate: str) -> int:
        """将Flux风格的聚合窗口转换为秒数"""
        match = re.match(r"^(\d+)([smh])$", aggregate)
        if match:
            amount = int(match.group(1))
            unit = match.group(2)
            multipliers = {"s": 1, "m": 60, "h": 3600}
            return amount * multipliers.get(unit, 1)
        return 60  # 默认1分钟

    # ---- 增量同步 ----

    async def start_sync(self) -> None:
        """启动SQLite到InfluxDB的增量同步循环"""
        if self._sync_running:
            return
        self._sync_running = True
        config = get_config()
        self._sync_task = asyncio.create_task(
            self._sync_loop(interval=config.influxdb.sync_interval)
        )
        logger.info("SQLite->InfluxDB增量同步已启动 (间隔=%ds)", config.influxdb.sync_interval)

    async def stop_sync(self) -> None:
        """停止增量同步循环"""
        self._sync_running = False
        if self._sync_task and not self._sync_task.done():
            self._sync_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sync_task
        self._sync_task = None
        logger.info("SQLite->InfluxDB增量同步已停止")

    async def _sync_loop(self, interval: int = 30) -> None:
        """增量同步循环：InfluxDB恢复后，从SQLite读取未同步数据增量写入InfluxDB"""
        while self._sync_running:
            try:
                if self._available and self._sqlite_ts:
                    unsynced = await self._sqlite_ts.get_unsynced_count()
                    if unsynced > 0:
                        await self._sync_batch()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("增量同步循环异常: %s", e)

            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break

    async def _sync_batch(self) -> int:
        """执行一批增量同步"""
        if not self._sqlite_ts or not self._available or not self._write_api:
            return 0

        config = get_config()
        batch_size = config.influxdb.sync_batch_size

        try:
            records = await self._sqlite_ts.get_unsynced_records(limit=batch_size)
            if not records:
                return 0

            points = []
            max_id = 0
            for rec in records:
                max_id = max(max_id, rec["id"])
                value = rec.get("value")
                if value is None:
                    continue

                import math
                try:
                    float_val = float(value)
                    if math.isnan(float_val) or math.isinf(float_val):
                        continue
                except (ValueError, TypeError):
                    continue

                p = (
                    Point(rec.get("measurement", "device_points"))
                    .tag("device_id", rec.get("device_id", ""))
                    .tag("point_name", rec.get("point_name", ""))
                    .tag("quality", rec.get("quality", "good"))
                    .field("value", float_val)
                )

                timestamp_ns = rec.get("timestamp_ns")
                if timestamp_ns:
                    ts = datetime.fromtimestamp(timestamp_ns / 1e9, tz=UTC)
                    p = p.time(ts)

                points.append(p)

            if points:
                await asyncio.to_thread(
                    self._write_api.write, bucket=self._bucket, record=points
                )
                await self._sqlite_ts.mark_synced(max_id)
                logger.info(
                    "增量同步: %d条数据已同步到InfluxDB (max_id=%d)", len(points), max_id
                )
                return len(points)

            # 即使没有有效points也标记已同步，避免重复处理
            await self._sqlite_ts.mark_synced(max_id)
            return 0
        except Exception as e:
            logger.error("增量同步批次失败: %s", e)
            return 0

    async def get_fallback_stats(self) -> dict:
        """获取降级存储统计信息"""
        if self._sqlite_ts:
            return await self._sqlite_ts.get_stats()
        return {
            "total_records": 0,
            "db_size_bytes": 0,
            "oldest_record": None,
            "newest_record": None,
            "unsynced_count": 0,
        }
