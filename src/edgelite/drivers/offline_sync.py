"""Modbus 离线同步管理模块（仅 TCP 使用）

在边缘侧维护"在线/离线"状态：在线时定期从时序存储读取未同步数据，
通过上传回调推送到云端；离线时数据沉淀在本地时序存储，待恢复在线后增量同步。

被 modbus_tcp.py 导入：from edgelite.drivers.offline_sync import OfflineSyncManager
TCP 调用：OfflineSyncManager(ts_store=self._ts_store, sync_interval=..., batch_size=..., compress=...)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


class OfflineSyncManager:
    """离线同步管理器

    内部维护 _online 状态、_upload_callback、_sync_task。
    start 创建 asyncio.create_task 的同步循环；在线时定期从 ts_store 读取未同步数据，
    调用 upload_callback 上传。force_sync 立即触发一次同步。
    """

    def __init__(
        self,
        ts_store: Any | None = None,
        sync_interval: float = 30.0,
        batch_size: int = 1000,
        compress: str = "gzip",
    ) -> None:
        self._ts_store = ts_store
        self._sync_interval = max(1.0, float(sync_interval))
        self._batch_size = max(1, int(batch_size))
        self._compress = compress or "none"
        # 运行状态
        self._online: bool = True
        self._running: bool = False  # FIXED: 添加运行标志，_sync_loop 中检查以防止任务取消后仍执行（Layer 3）
        self._upload_callback: Callable | None = None
        self._sync_task: asyncio.Task | None = None
        # 统计
        self._synced_count: int = 0
        self._sync_cycles: int = 0
        self._last_sync_at: float = 0.0
        self._last_error: str = ""

    async def start(self) -> None:
        """启动同步循环任务"""
        if self._sync_task is not None and not self._sync_task.done():
            return  # 已启动，避免重复创建
        self._running = True  # FIXED: 设置运行标志
        self._sync_task = asyncio.create_task(self._sync_loop(), name="modbus-offline-sync")
        logger.info(
            "[offline_sync] 已启动 sync_interval=%.1fs batch=%d compress=%s",
            self._sync_interval,
            self._batch_size,
            self._compress,
        )

    async def stop(self) -> None:
        """停止同步循环"""
        self._running = False  # FIXED: 清除运行标志
        if self._sync_task is not None and not self._sync_task.done():
            self._sync_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sync_task
        self._sync_task = None
        logger.info("[offline_sync] 已停止 (累计同步 %d 条)", self._synced_count)

    async def _sync_loop(self) -> None:
        """同步循环：定期触发一次同步"""
        while self._running:  # FIXED: 检查 _running 标志，防止取消信号被忽略时任务无限运行
            try:
                await asyncio.sleep(self._sync_interval)
                await self._do_sync()
            except asyncio.CancelledError:
                break
            except Exception as e:
                # 循环内异常不应导致任务退出
                self._last_error = str(e)
                logger.warning("[offline_sync] 同步循环异常: %s", e)
                # 短暂退避后继续
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.sleep(self._sync_interval)

    async def _do_sync(self) -> int:
        """执行一次同步：读取未同步数据并上传

        返回本次同步的记录数。
        兼容具备 get_unsynced_records/sync_completed 的时序存储（如 SqliteTimeSeriesStorage），
        以及不具备该接口的存储（如 ModbusTsStore，此时返回 0）。
        """
        if not self._online:
            return 0
        if self._upload_callback is None:
            return 0
        if self._ts_store is None:
            return 0

        # 读取未同步记录（仅当时序存储提供该接口时）
        records: list[dict] = []
        getter = getattr(self._ts_store, "get_unsynced_records", None)
        if getter is None:
            # 时序存储未提供增量读取接口，无可同步数据
            return 0
        try:
            result = getter(limit=self._batch_size)
            if asyncio.iscoroutine(result):
                result = await result
            if isinstance(result, list):
                records = result
        except Exception as e:
            self._last_error = str(e)
            logger.warning("[offline_sync] 读取未同步记录失败: %s", e)
            return 0

        if not records:
            return 0

        # 调用上传回调（兼容 sync/async 回调）
        uploaded = 0
        try:
            cb_result = self._upload_callback(records)
            if asyncio.iscoroutine(cb_result):
                cb_result = await cb_result
            uploaded = int(cb_result) if cb_result is not None else len(records)
        except Exception as e:
            self._last_error = str(e)
            logger.error("[offline_sync] 上传回调执行失败: %s", e)
            return 0

        # 标记已同步（仅当时序存储提供该接口时）
        max_id = 0
        for r in records:
            if isinstance(r, dict):
                rid = r.get("id", 0)
                if isinstance(rid, int) and rid > max_id:
                    max_id = rid
        if max_id > 0:
            marker = getattr(self._ts_store, "sync_completed", None) or getattr(self._ts_store, "mark_synced", None)
            if marker is not None:
                try:
                    mres = marker(max_id)
                    if asyncio.iscoroutine(mres):
                        await mres
                except Exception as e:
                    logger.warning("[offline_sync] 标记同步偏移失败: %s", e)

        self._synced_count += uploaded
        self._sync_cycles += 1
        self._last_sync_at = time.time()
        self._last_error = ""
        logger.debug("[offline_sync] 同步 %d 条记录", uploaded)
        return uploaded

    async def force_sync(self) -> int:
        """强制同步一次，返回同步的记录数"""
        return await self._do_sync()

    # ------------------------------------------------------------------
    # 状态与回调设置（同步方法）
    # ------------------------------------------------------------------
    def set_online(self, online: bool) -> None:
        """设置在线/离线状态

        在线时同步循环会定期上传；离线时数据沉淀在本地时序存储。
        """
        self._online = bool(online)
        logger.info("[offline_sync] 在线状态切换为 %s", self._online)

    def set_upload_callback(self, callback: Callable) -> None:
        """设置上传回调

        callback 签名：(batch: list[dict]) -> int | Awaitable[int]
        返回成功上传的记录数。
        """
        self._upload_callback = callback
        logger.info("[offline_sync] 上传回调已设置")

    def get_stats(self) -> dict[str, Any]:
        """返回同步统计信息"""
        ts_connected = False
        ts_stats: dict[str, Any] = {}
        if self._ts_store is not None:
            ts_connected = True
            # 尝试获取时序存储统计（ModbusTsStore.get_stats 是同步方法）
            getter = getattr(self._ts_store, "get_stats", None)
            if getter is not None:
                try:
                    s = getter()
                    if not asyncio.iscoroutine(s):
                        ts_stats = s or {}
                except Exception:
                    ts_stats = {}
        return {
            "online": self._online,
            "synced_count": self._synced_count,
            "sync_cycles": self._sync_cycles,
            "last_sync_at": self._last_sync_at,
            "sync_interval": self._sync_interval,
            "batch_size": self._batch_size,
            "compress": self._compress,
            "has_callback": self._upload_callback is not None,
            "ts_store_connected": ts_connected,
            "ts_store_stats": ts_stats,
            "last_error": self._last_error,
        }
