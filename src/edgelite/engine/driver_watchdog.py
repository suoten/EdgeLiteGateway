"""驱动心跳检测与主动恢复模块"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, UTC
from typing import Any

from edgelite.drivers.base import DriverPlugin

logger = logging.getLogger(__name__)


@dataclass
class HeartbeatResult:
    """心跳检测结果"""

    device_id: str
    success: bool
    latency_ms: float
    error_message: str | None = None
    timestamp: datetime | None = None


class DriverWatchdog:
    """驱动心跳检测与主动恢复管理器

    功能：
    - 定期 Ping 设备检测连通性
    - 统计断线时长和次数
    - 触发自动重连
    - 记录连接质量历史
    - 发布设备状态变更事件
    """

    def __init__(
        self,
        check_interval: float = 30.0,
        max_offline_duration: float = 300.0,
        auto_reconnect: bool = True,
        max_reconnect_attempts: int = 3,
    ):
        """
        Args:
            check_interval: 心跳检测间隔（秒）
            max_offline_duration: 最大离线时长阈值，超过则触发告警（秒）
            auto_reconnect: 是否自动触发重连
            max_reconnect_attempts: 最大自动重连尝试次数
        """
        self._check_interval = check_interval
        self._max_offline_duration = max_offline_duration
        self._auto_reconnect = auto_reconnect
        self._max_reconnect_attempts = max_reconnect_attempts

        self._running = False
        self._task: asyncio.Task | None = None
        self._drivers: dict[str, DriverPlugin] = {}
        self._device_configs: dict[str, dict] = {}
        self._offline_history: dict[str, list[dict]] = {}  # device_id -> offline events
        self._lock = asyncio.Lock()
        self._event_bus: Any = None
        self._on_status_change: callable | None = None

    async def start(self) -> None:
        """启动看门狗"""
        self._running = True
        self._task = asyncio.create_task(self._watchdog_loop())
        logger.info("Driver watchdog started (interval=%.1fs)", self._check_interval)

    async def stop(self) -> None:
        """停止看门狗"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Driver watchdog stopped")

    def set_event_bus(self, event_bus: Any) -> None:
        """设置事件总线"""
        self._event_bus = event_bus

    def set_status_change_callback(self, callback: callable) -> None:
        """设置状态变更回调"""
        self._on_status_change = callback

    def register_driver(self, device_id: str, driver: DriverPlugin, config: dict) -> None:
        """注册驱动"""
        self._drivers[device_id] = driver
        self._device_configs[device_id] = config
        self._offline_history.setdefault(device_id, [])
        logger.debug("Driver watchdog registered: %s", device_id)

    def unregister_driver(self, device_id: str) -> None:
        """取消注册"""
        self._drivers.pop(device_id, None)
        self._device_configs.pop(device_id, None)
        logger.debug("Driver watchdog unregistered: %s", device_id)

    async def check_device(self, device_id: str) -> HeartbeatResult:
        """执行设备心跳检测"""
        driver = self._drivers.get(device_id)
        if not driver:
            return HeartbeatResult(
                device_id=device_id,
                success=False,
                latency_ms=0,
                error_message="Driver not registered",
            )

        start_time = asyncio.get_event_loop().time()
        try:
            # 调用驱动的 health_check 方法
            if asyncio.iscoroutine_function(driver.health_check):
                healthy = await driver.health_check(device_id)
            else:
                healthy = driver.health_check(device_id)

            latency_ms = (asyncio.get_event_loop().time() - start_time) * 1000

            if healthy:
                result = HeartbeatResult(
                    device_id=device_id,
                    success=True,
                    latency_ms=latency_ms,
                    timestamp=datetime.now(UTC),
                )
                await self._on_heartbeat_success(device_id, result)
            else:
                result = HeartbeatResult(
                    device_id=device_id,
                    success=False,
                    latency_ms=latency_ms,
                    error_message="Health check failed",
                    timestamp=datetime.now(UTC),
                )
                await self._on_heartbeat_failure(device_id, result)

            return result

        except Exception as e:
            latency_ms = (asyncio.get_event_loop().time() - start_time) * 1000
            result = HeartbeatResult(
                device_id=device_id,
                success=False,
                latency_ms=latency_ms,
                error_message=str(e),
                timestamp=datetime.now(UTC),
            )
            await self._on_heartbeat_failure(device_id, result)
            return result

    async def check_all_devices(self) -> dict[str, HeartbeatResult]:
        """检查所有设备"""
        results = {}
        tasks = [
            self.check_device(device_id)
            for device_id in self._drivers.keys()
        ]
        completed = await asyncio.gather(*tasks, return_exceptions=True)
        for device_id, result in zip(self._drivers.keys(), completed):
            if isinstance(result, Exception):
                results[device_id] = HeartbeatResult(
                    device_id=device_id,
                    success=False,
                    latency_ms=0,
                    error_message=str(result),
                )
            else:
                results[device_id] = result
        return results

    async def _watchdog_loop(self) -> None:
        """看门狗主循环"""
        while self._running:
            try:
                await asyncio.sleep(self._check_interval)
                results = await self.check_all_devices()

                # 检查超长离线
                await self._check_long_offline(results)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Watchdog loop error: %s", e)

    async def _on_heartbeat_success(
        self, device_id: str, result: HeartbeatResult
    ) -> None:
        """处理心跳成功"""
        driver = self._drivers.get(device_id)
        if not driver:
            return

        # 记录成功
        stats = driver.get_health_stats(device_id)
        if stats and stats.consecutive_failures > 0:
            # 从离线恢复
            logger.info(
                "Device recovered: %s (offline_duration=%.1fs, consecutive_failures=%d)",
                device_id,
                stats.total_downtime_seconds,
                stats.consecutive_failures,
            )
            await self._publish_recovery_event(device_id, stats)

    async def _on_heartbeat_failure(
        self, device_id: str, result: HeartbeatResult
    ) -> None:
        """处理心跳失败"""
        driver = self._drivers.get(device_id)
        if not driver:
            return

        stats = driver.get_health_stats(device_id)
        if stats and stats.consecutive_failures == 1:
            # 首次检测到离线
            logger.warning(
                "Device offline: %s (reason=%s)",
                device_id,
                result.error_message,
            )
            await self._publish_offline_event(device_id, result.error_message)

        # 检查是否需要自动重连
        if self._auto_reconnect and stats:
            if stats.consecutive_failures >= self._max_reconnect_attempts:
                await self._trigger_reconnect(device_id)

    async def _check_long_offline(self, results: dict[str, HeartbeatResult]) -> None:
        """检查超长离线设备"""
        for device_id, result in results.items():
            if not result.success:
                driver = self._drivers.get(device_id)
                if not driver:
                    continue

                stats = driver.get_health_stats(device_id)
                if stats and stats.total_downtime_seconds >= self._max_offline_duration:
                    logger.error(
                        "Device offline too long: %s (duration=%.1fs > threshold=%.1fs)",
                        device_id,
                        stats.total_downtime_seconds,
                        self._max_offline_duration,
                    )
                    await self._publish_long_offline_event(device_id, stats.total_downtime_seconds)

    async def _trigger_reconnect(self, device_id: str) -> None:
        """触发设备重连"""
        logger.info("Triggering reconnect for: %s", device_id)
        driver = self._drivers.get(device_id)
        if not driver:
            return

        # 调用驱动的重连逻辑
        try:
            if hasattr(driver, "_try_reconnect") and asyncio.iscoroutine_function(
                driver._try_reconnect
            ):
                await driver._try_reconnect(device_id)
            elif hasattr(driver, "reconnect") and asyncio.iscoroutine_function(
                driver.reconnect
            ):
                await driver.reconnect(device_id)
        except Exception as e:
            logger.warning("Reconnect failed for %s: %s", device_id, e)

    async def _publish_recovery_event(self, device_id: str, stats: Any) -> None:
        """发布设备恢复事件"""
        if self._event_bus:
            try:
                from edgelite.engine.event_bus import DeviceStatusEvent

                event = DeviceStatusEvent(
                    device_id=device_id,
                    old_status="offline",
                    new_status="online",
                )
                await self._event_bus.publish(event)
            except Exception as e:
                logger.debug("Failed to publish recovery event: %s", e)

        if self._on_status_change:
            try:
                self._on_status_change(device_id, "online")
            except Exception as e:
                logger.debug("Status change callback error: %s", e)

    async def _publish_offline_event(self, device_id: str, reason: str | None) -> None:
        """发布设备离线事件"""
        if self._event_bus:
            try:
                from edgelite.engine.event_bus import DeviceStatusEvent

                event = DeviceStatusEvent(
                    device_id=device_id,
                    old_status="online",
                    new_status="offline",
                )
                await self._event_bus.publish(event)
            except Exception as e:
                logger.debug("Failed to publish offline event: %s", e)

        if self._on_status_change:
            try:
                self._on_status_change(device_id, "offline")
            except Exception as e:
                logger.debug("Status change callback error: %s", e)

    async def _publish_long_offline_event(
        self, device_id: str, duration: float
    ) -> None:
        """发布超长离线事件"""
        if self._event_bus:
            try:
                from edgelite.engine.event_bus import AlarmEvent

                event = AlarmEvent(
                    alarm_id=f"long_offline_{device_id}",
                    rule_id="system_long_offline",
                    device_id=device_id,
                    severity="critical",
                    action="firing",
                    trigger_value={"duration": duration},
                    rule_type="system",
                )
                await self._event_bus.publish(event)
            except Exception as e:
                logger.debug("Failed to publish long offline event: %s", e)

    def get_offline_history(self, device_id: str) -> list[dict]:
        """获取设备离线历史"""
        return list(self._offline_history.get(device_id, []))

    def get_connection_summary(self) -> dict:
        """获取连接状态摘要"""
        total = len(self._drivers)
        online = 0
        offline = 0
        degraded = 0  # 连接质量分数 < 80

        for device_id, driver in self._drivers.items():
            stats = driver.get_health_stats(device_id)
            if stats:
                if stats.consecutive_failures == 0:
                    online += 1
                else:
                    offline += 1
                if stats.connection_quality_score < 80:
                    degraded += 1
            else:
                online += 1  # 无统计视为在线

        return {
            "total_devices": total,
            "online": online,
            "offline": offline,
            "degraded": degraded,
            "health_rate": round((online / total * 100) if total > 0 else 100, 2),
        }


# 全局看门狗实例
_driver_watchdog: DriverWatchdog | None = None


def get_driver_watchdog() -> DriverWatchdog:
    """获取全局驱动看门狗"""
    global _driver_watchdog
    if _driver_watchdog is None:
        _driver_watchdog = DriverWatchdog()
    return _driver_watchdog
