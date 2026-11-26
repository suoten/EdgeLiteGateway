"""OPC UA 时间序列数据缓存与离线同步管理（内存实现）

FIX-DRV-005: 文件曾损坏（未闭合 docstring 导致 SyntaxError），之后被恢复为"最小桩"，
但桩的接口与 OpcUaDriver 的实际调用不符：``init_data_persistence()`` 传入的
``compress`` / ``batch_size`` 参数桩不接受，``OpcUaTsStore.start()`` 及其后的
write_read_result / query / query_latest / get_stats、离线同步的 set_online /
set_upload_callback / force_sync / get_stats 桩里都没有——这些路径一经调用必然
TypeError / AttributeError。

本实现按驱动的调用方式补齐接口，数据保存在内存环形缓冲中（条数上限 _MAX_POINTS，
超出后淘汰最旧数据），按保留天数清理过期数据。``query`` / ``query_latest`` 的返回结构
与 Modbus 时序库一致：``{"time": ISO8601, "value": ..., "quality": ...}``。

待上传与历史查询共用同一份缓冲：每条记录带自增序号 ``seq``，离线同步只上传序号大于
"已同步游标"的记录，上传成功后推进游标（不删除数据），因此历史数据在保留期内始终可查。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

_MAX_POINTS = 100_000


def _to_epoch_seconds(value: Any) -> float | None:
    """datetime / epoch 秒 / epoch 毫秒 / ISO 字符串 → epoch 秒；无法识别返回 None"""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.timestamp()
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        # epoch 毫秒（~1.7e12）与 epoch 秒（~1.7e9）量级相差 1000 倍，可据此区分
        return value / 1000.0 if value > 1e11 else float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            return None
    return None


def _record_to_result(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "time": datetime.fromtimestamp(float(record["timestamp"]), tz=UTC).isoformat(),
        "value": record.get("value"),
        "quality": record.get("quality") or "good",
    }


class OpcUaTsStore:
    """OPC UA 驱动本地时序数据缓存（内存环形缓冲）"""

    def __init__(self, retention_days: int = 30) -> None:
        self._retention_days = retention_days
        self._buffer: deque[dict[str, Any]] = deque(maxlen=_MAX_POINTS)
        self._running = False
        self._total_writes = 0
        self._dropped = 0
        self._next_seq = 1
        self._synced_seq = 0  # 已同步游标：seq <= 该值的记录已交给上传回调

    async def start(self) -> None:
        """启动存储（内存实现无需建连）"""
        self._running = True
        logger.info(
            "[opcua_ts_store] started (in-memory, retention=%dd, max_points=%d)",
            self._retention_days,
            _MAX_POINTS,
        )

    async def stop(self) -> None:
        """停止存储；缓冲区保留，已写入数据仍可查询"""
        self._running = False
        logger.info("[opcua_ts_store] stopped (total_writes=%d)", self._total_writes)

    @property
    def is_running(self) -> bool:
        return self._running

    def append(self, point_data: dict[str, Any]) -> None:
        """Append a data point to the local store."""
        if not isinstance(point_data, dict):
            return
        record = dict(point_data)
        record.setdefault("timestamp", time.time())
        record.setdefault("quality", "good")
        record["seq"] = self._next_seq
        self._next_seq += 1
        if len(self._buffer) == self._buffer.maxlen:
            self._dropped += 1
        self._buffer.append(record)
        self._total_writes += 1

    async def write_read_result(self, device_id: str, result: dict[str, Any]) -> int:
        """写入一次读取结果（key 为点位名，值为裸值或带 .value/.quality 的 PointValue）"""
        if not result:
            return 0
        written = 0
        now = time.time()
        for point_name, value in result.items():
            if not isinstance(point_name, str) or not point_name:
                continue
            quality = "good"
            if hasattr(value, "value") and hasattr(value, "quality"):
                quality = getattr(value, "quality", None) or "good"
                value = value.value
            self.append(
                {
                    "device_id": device_id,
                    "point_name": point_name,
                    "value": value,
                    "quality": quality,
                    "timestamp": now,
                }
            )
            written += 1
        return written

    async def query(
        self,
        device_id: str,
        point_name: str,
        start_time: Any,
        end_time: Any | None = None,
        quality: str | None = None,
        aggregate: str | None = None,
        window_seconds: int | None = None,
        limit: int = 10000,
    ) -> list[dict[str, Any]]:
        """按时序查询点位数据，支持质量过滤与聚合（mean/avg/max/min/sum/count）"""
        start_s = _to_epoch_seconds(start_time)
        if start_s is None:
            return []
        end_s = _to_epoch_seconds(end_time) if end_time is not None else time.time()
        if end_s is None:
            return []
        rows = [
            r
            for r in self._buffer
            if r.get("device_id") == device_id
            and r.get("point_name") == point_name
            and start_s <= float(r.get("timestamp", 0)) <= end_s
            and (quality is None or r.get("quality", "good") == quality)
        ]
        rows.sort(key=lambda r: r.get("timestamp", 0))

        if aggregate and window_seconds:
            window = max(1, int(window_seconds))
            buckets: dict[int, list[float]] = {}
            for r in rows:
                v = r.get("value")
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    continue
                buckets.setdefault(int(float(r["timestamp"]) // window) * window, []).append(v)
            agg = str(aggregate).lower()
            results = []
            for window_start in sorted(buckets):
                values = buckets[window_start]
                if agg == "sum":
                    agg_value: float = sum(values)
                elif agg == "min":
                    agg_value = min(values)
                elif agg == "max":
                    agg_value = max(values)
                elif agg == "count":
                    agg_value = len(values)
                else:  # mean / avg / 未知聚合名一律按平均值
                    agg_value = sum(values) / len(values)
                results.append(
                    {
                        "time": datetime.fromtimestamp(window_start, tz=UTC).isoformat(),
                        "value": agg_value,
                        "quality": "good",
                    }
                )
            return results[: max(0, limit)]

        return [_record_to_result(r) for r in rows[: max(0, limit)]]

    async def query_latest(self, device_id: str, point_names: list[str]) -> dict[str, dict[str, Any]]:
        """查询多个点位的最新值：{point_name: {"time", "value", "quality"}}"""
        if not point_names:
            return {}
        wanted = set(point_names)
        latest: dict[str, dict[str, Any]] = {}
        for r in self._buffer:  # 缓冲区按写入顺序排列，后出现的即最新
            if r.get("device_id") == device_id and r.get("point_name") in wanted:
                latest[r["point_name"]] = r
        return {name: _record_to_result(r) for name, r in latest.items()}

    def get_stats(self) -> dict[str, Any]:
        """返回存储统计信息（同步）"""
        return {
            "total_writes": self._total_writes,
            "dropped": self._dropped,
            "buffered": len(self._buffer),
            "pending_sync": sum(1 for r in self._buffer if r["seq"] > self._synced_seq),
            "max_points": self._buffer.maxlen,
            "retention_days": self._retention_days,
            "storage": "memory",
            "connected": self._running,
        }

    def get_pending(self, limit: int = 1000) -> list[dict[str, Any]]:
        """获取尚未上传的数据点（按写入顺序，最多 limit 条）；不改变缓冲区"""
        return [dict(r) for r in self._buffer if r["seq"] > self._synced_seq][: max(0, limit)]

    def mark_synced(self, count: int) -> None:
        """把最早的 count 条待上传记录标记为已上传（推进游标，数据保留以供历史查询）"""
        pending = [r["seq"] for r in self._buffer if r["seq"] > self._synced_seq]
        for seq in pending[: max(0, count)]:
            self._synced_seq = seq

    def cleanup_expired(self) -> int:
        """Remove expired data points. Returns count of removed items."""
        cutoff = time.time() - self._retention_days * 86400
        kept = [r for r in self._buffer if float(r.get("timestamp", 0)) >= cutoff]
        removed = len(self._buffer) - len(kept)
        if removed:
            self._buffer.clear()
            self._buffer.extend(kept)
        return removed


class OpcUaOfflineSyncManager:
    """OPC UA 离线数据同步管理器

    链路离线时数据留在 :class:`OpcUaTsStore`；恢复在线后由 ``force_sync``（或后台定时任务）
    把待上传记录交给 ``set_upload_callback`` 注册的上传回调。
    """

    def __init__(
        self,
        ts_store: OpcUaTsStore | None = None,
        sync_interval: float = 30.0,
        batch_size: int = 1000,
        compress: str = "gzip",
    ) -> None:
        self._ts_store = ts_store
        self._sync_interval = max(1.0, float(sync_interval))
        self._batch_size = max(1, int(batch_size))
        self._compress = compress or "none"
        self._running = False
        self._online = True
        self._upload_callback: Any = None
        self._task: asyncio.Task | None = None
        self._resync_tasks: set[asyncio.Task] = set()
        self._stats = {"sync_total": 0, "sync_failed": 0, "last_sync_at": None}

    async def start(self) -> None:
        """Start the offline sync manager."""
        self._running = True
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._sync_loop(), name="opcua-offline-sync")
        logger.info(
            "OpcUaOfflineSyncManager started (interval=%.0fs batch_size=%d)",
            self._sync_interval,
            self._batch_size,
        )

    async def stop(self) -> None:
        """Stop the offline sync manager."""
        self._running = False
        task, self._task = self._task, None
        pending = [t for t in self._resync_tasks if t is not asyncio.current_task()]
        for t in [task, *pending]:
            if t is not None:
                t.cancel()
        # 用 asyncio.wait 等待收尾：任务被取消不会抛错，而调用方自身被取消时仍能向上传播
        to_wait = {t for t in [task, *pending] if t is not None}
        if to_wait:
            _done, still_pending = await asyncio.wait(to_wait, timeout=5.0)
            if still_pending:
                logger.warning("[opcua_offline_sync] sync task did not stop within 5s")

    def set_online(self, online: bool) -> None:
        """切换链路在线状态：由离线恢复为在线时立即补传一次（不等下一个同步周期）"""
        was_online = self._online
        self._online = bool(online)
        if not (self._online and not was_online and self._running):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # 没有运行中的事件循环：交给后台同步循环在下一个周期处理
        task = loop.create_task(self.force_sync(), name="opcua-offline-sync-resume")
        self._resync_tasks.add(task)
        task.add_done_callback(self._resync_tasks.discard)

    def set_upload_callback(self, callback: Any) -> None:
        """注册上传回调：``callback(records: list[dict]) -> int | Awaitable[int]``（返回成功条数）"""
        self._upload_callback = callback

    async def _sync_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._sync_interval)
                if self._running and self._online:
                    await self.force_sync()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # 后台任务不能因单次失败退出
                logger.warning("[opcua_offline_sync] sync loop error: %s", e)

    async def force_sync(self) -> int:
        """立即同步一批待上传记录，返回成功条数（离线或未注册回调时返回 0）"""
        if not self._ts_store or not self._online or self._upload_callback is None:
            return 0
        records = self._ts_store.get_pending(limit=self._batch_size)
        if not records:
            return 0
        self._stats["sync_total"] += 1
        try:
            result = self._upload_callback(records)
            if asyncio.iscoroutine(result):
                result = await result
            synced = max(0, int(result or 0))
        except Exception as e:  # 上传失败保留数据，等待下次重试
            self._stats["sync_failed"] += 1
            logger.warning("[opcua_offline_sync] upload failed, will retry: %s", e)
            return 0
        if synced > 0:
            self._ts_store.mark_synced(min(synced, len(records)))
            self._stats["last_sync_at"] = datetime.now(UTC).isoformat()
        return min(synced, len(records))

    async def sync_now(self) -> int:
        """Trigger an immediate sync. Returns number of synced records."""
        return await self.force_sync()

    def get_stats(self) -> dict[str, Any]:
        store_stats = self._ts_store.get_stats() if self._ts_store else {"pending_sync": 0}
        return {
            **self._stats,
            "running": self._running,
            "online": self._online,
            "sync_interval": self._sync_interval,
            "batch_size": self._batch_size,
            "compress": self._compress,
            "pending": store_stats.get("pending_sync", 0),
        }
