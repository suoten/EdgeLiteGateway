"""磁盘空间监控模块

定期检查磁盘使用率，当超过阈值时：
1. 通过 EventBus 发布 SystemAlertEvent 告警
2. 当可用空间低于 1GB 时，强制执行 SQLite WAL checkpoint (TRUNCATE)
   以释放 WAL 文件占用的磁盘空间

由 lifecycle.py 在应用启动时创建监控协程，在应用关闭时取消。
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from edgelite.engine.event_bus import EventBus

logger = logging.getLogger(__name__)

# ── 常量 ──
_DISK_MONITOR_INTERVAL = 60  # 检查间隔（秒）
_DISK_WARNING_PERCENT = 85  # 告警阈值（%）
_DISK_CRITICAL_PERCENT = 90  # 严重告警阈值（%）
_DISK_LOW_SPACE_BYTES = 1 * 1024 * 1024 * 1024  # 1GB - 触发 WAL checkpoint

try:
    import psutil
except ImportError:
    psutil = None


class DiskSpaceMonitor:
    """磁盘空间监控器

    定期检查磁盘使用率，发布告警事件并在空间不足时触发 WAL checkpoint。

    Attributes:
        _event_bus: 事件总线，用于发布 SystemAlertEvent
        _db_path: SQLite 数据库路径，用于 WAL checkpoint
        _task: asyncio 监控任务
        _last_alert_percent: 上次告警时的磁盘使用率（用于去重，避免重复告警）
    """

    def __init__(
        self,
        event_bus: EventBus | None = None,
        db_path: str | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._db_path = db_path
        self._task: asyncio.Task | None = None
        self._last_alert_percent: float = 0.0

    @staticmethod
    def get_disk_usage() -> dict[str, Any] | None:
        """获取当前磁盘使用情况。

        Returns:
            包含 total/used/free/percent 的字典，psutil 不可用时返回 None
        """
        if psutil is None:
            return None
        try:
            disk_path = "C:\\" if os.name == "nt" else "/"
            usage = psutil.disk_usage(disk_path)
            return {
                "total": usage.total,
                "used": usage.used,
                "free": usage.free,
                "percent": usage.percent,
            }
        except Exception as e:
            logger.warning("Failed to get disk usage: %s", e)
            return None

    async def _trigger_wal_checkpoint(self) -> None:
        """当磁盘空间不足时，强制执行 SQLite WAL checkpoint (TRUNCATE)。

        WAL 文件可能积累大量未 checkpoint 的数据，在磁盘空间紧张时
        执行 TRUNCATE checkpoint 可以立即回收 WAL 文件空间。
        """
        if not self._db_path or not os.path.exists(self._db_path):
            return
        try:
            import sqlite3

            def _do_checkpoint() -> str:
                conn = sqlite3.connect(self._db_path, timeout=10)
                try:
                    cursor = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    row = cursor.fetchone()
                    return f"wal_checkpoint(TRUNCATE): {row}"
                finally:
                    conn.close()

            result = await asyncio.to_thread(_do_checkpoint)
            logger.info("Disk space low - triggered WAL checkpoint: %s", result)
        except Exception as e:
            logger.warning("Failed to trigger WAL checkpoint: %s", e)

    async def _publish_alert(self, percent: float, free_bytes: int) -> None:
        """发布磁盘空间告警事件。

        Args:
            percent: 磁盘使用率（%）
            free_bytes: 可用空间（字节）
        """
        if self._event_bus is None:
            return

        from edgelite.engine.event_bus import SystemAlertEvent

        severity = "critical" if percent >= _DISK_CRITICAL_PERCENT else "warning"

        event = SystemAlertEvent(
            alert_type="disk_space",
            severity=severity,
            message=f"Disk usage {percent:.1f}%, free space {free_bytes / 1024 / 1024:.0f}MB",
            details={
                "disk_percent": percent,
                "disk_free_bytes": free_bytes,
                "threshold_warning": _DISK_WARNING_PERCENT,
                "threshold_critical": _DISK_CRITICAL_PERCENT,
            },
        )

        try:
            await self._event_bus.publish(event)
        except Exception as e:
            logger.warning("Failed to publish disk space alert: %s", e)

    async def _monitor_loop(self) -> None:
        """磁盘空间监控主循环。

        每 60 秒检查一次磁盘使用率：
        - 使用率 >= 85% 时发布告警
        - 使用率 >= 90% 或可用空间 < 1GB 时触发 WAL checkpoint
        - 使用率恢复正常时清除告警状态
        """
        logger.info("DiskSpaceMonitor started (interval=%ds)", _DISK_MONITOR_INTERVAL)
        while True:
            try:
                await asyncio.sleep(_DISK_MONITOR_INTERVAL)

                disk_info = self.get_disk_usage()
                if disk_info is None:
                    continue

                percent = disk_info["percent"]
                free_bytes = disk_info["free"]

                # 磁盘使用率超过告警阈值
                if percent >= _DISK_WARNING_PERCENT:
                    # 避免重复告警：仅在百分比变化超过 2% 时重新发布
                    if abs(percent - self._last_alert_percent) >= 2.0 or self._last_alert_percent == 0:
                        logger.warning(
                            "Disk space alert: %.1f%% used, %.0fMB free",
                            percent,
                            free_bytes / 1024 / 1024,
                        )
                        await self._publish_alert(percent, free_bytes)
                        self._last_alert_percent = percent
                else:
                    # 恢复正常时重置告警状态
                    if self._last_alert_percent > 0:
                        logger.info(
                            "Disk space recovered: %.1f%% used (was %.1f%%)",
                            percent,
                            self._last_alert_percent,
                        )
                        self._last_alert_percent = 0.0

                # 可用空间低于 1GB 时触发 WAL checkpoint
                if free_bytes < _DISK_LOW_SPACE_BYTES or percent >= _DISK_CRITICAL_PERCENT:
                    await self._trigger_wal_checkpoint()

            except asyncio.CancelledError:
                logger.info("DiskSpaceMonitor cancelled")
                raise
            except Exception as e:
                logger.warning("DiskSpaceMonitor error: %s", e)

    def start(self) -> None:
        """启动磁盘空间监控协程。"""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._monitor_loop(), name="disk-space-monitor")

    async def stop(self) -> None:
        """停止磁盘空间监控协程。"""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            logger.info("DiskSpaceMonitor stopped")
