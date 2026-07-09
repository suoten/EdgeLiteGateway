"""驱动心跳检测与主动恢复模块"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
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
        stale_cycles: int = 3,
        restart_cycles: int = 10,
    ):
        """
        Args:
            check_interval: 心跳检测间隔（秒）
            max_offline_duration: 最大离线时长阈值，超过则触发告警（秒）
            auto_reconnect: 是否自动触发重连
            max_reconnect_attempts: 最大自动重连尝试次数
            stale_cycles: 连续无数据产出周期数，超过则标记为stale
            restart_cycles: 连续stale周期数，超过则重启采集任务
        """
        self._check_interval = check_interval
        self._max_offline_duration = max_offline_duration
        self._auto_reconnect = auto_reconnect
        self._max_reconnect_attempts = max_reconnect_attempts
        self._stale_cycles = stale_cycles
        self._restart_cycles = restart_cycles

        self._running = False
        self._task: asyncio.Task | None = None
        self._drivers: dict[str, DriverPlugin] = {}
        self._device_configs: dict[str, dict] = {}
        self._offline_history: dict[str, list[dict]] = {}  # device_id -> offline events
        self._reconnecting: set[str] = set()  # FIXED-P2: 防止同一设备并发重连
        self._lock = asyncio.Lock()
        self._event_bus: Any = None
        self._on_status_change: callable | None = None

        self._reconnect_backoff: dict[str, dict] = {}  # device_id -> {attempt, base_interval, max_interval}
        self._stale_counters: dict[str, int] = {}  # device_id -> consecutive stale cycles
        self._stale_devices: set[str] = set()  # currently stale device ids
        self._reconnect_attempts: dict[str, int] = {}  # FIXED-P0: 看门狗重连使用独立计数器而非consecutive_failures
        self._circuit_open: set[str] = set()  # FIXED-P0: 熔断状态设备集合，重连耗尽后进入此状态
        self._circuit_probe_time: dict[str, float] = {}  # FIXED-P0: 熔断状态上次探测时间
        # FIXED-BugR4X: 原问题-_check_long_offline每次循环都发布long_offline告警无去重导致告警风暴，
        # 修复-新增_long_offline_alarmed集合记录已告警设备，避免重复发布；设备恢复时清除
        self._long_offline_alarmed: set[str] = set()
        # FIXED-P1: 限制并发心跳检测数量，防止设备数多时耗尽连接池或触发对端限流
        self._heartbeat_concurrency = asyncio.Semaphore(20)

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
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("Driver watchdog stopped")

    def set_event_bus(self, event_bus: Any) -> None:
        """设置事件总线"""
        self._event_bus = event_bus

    def set_status_change_callback(self, callback: callable) -> None:
        """设置状态变更回调"""
        self._on_status_change = callback

    async def register_driver(self, device_id: str, driver: DriverPlugin, config: dict) -> None:  # FIXED-P0: 改为async，_lock为asyncio.Lock
        """注册驱动"""
        async with self._lock:
            self._drivers[device_id] = driver
            self._device_configs[device_id] = config
            self._offline_history.setdefault(device_id, [])
        logger.debug("Driver watchdog registered: %s", device_id)

    async def unregister_driver(self, device_id: str) -> None:  # FIXED-P0: 改为async，_lock为asyncio.Lock
        """取消注册"""
        async with self._lock:
            self._drivers.pop(device_id, None)
            self._device_configs.pop(device_id, None)
            self._reconnect_backoff.pop(device_id, None)
            self._stale_counters.pop(device_id, None)
            self._stale_devices.discard(device_id)
            self._reconnect_attempts.pop(device_id, None)
            self._circuit_open.discard(device_id)
            self._circuit_probe_time.pop(device_id, None)
        logger.debug("Driver watchdog unregistered: %s", device_id)

    async def check_device(self, device_id: str) -> HeartbeatResult:
        """执行设备心跳检测"""
        # FIXED(严重): 原问题-_drivers无锁访问，与register/unregister并发修改竞态;
        # 修复-在锁内读取driver快照，锁外执行health_check避免长时间持锁
        async with self._lock:
            driver = self._drivers.get(device_id)
        if not driver:
            return HeartbeatResult(
                device_id=device_id,
                success=False,
                latency_ms=0,
                error_message="Driver not registered",
            )

        start_time = asyncio.get_running_loop().time()  # FIXED-P2: 使用get_running_loop替代废弃的get_event_loop
        try:
            # 调用驱动的 health_check 方法
            if asyncio.iscoroutinefunction(driver.health_check):
                healthy = await driver.health_check(device_id)
            else:
                healthy = driver.health_check(device_id)

            latency_ms = (asyncio.get_running_loop().time() - start_time) * 1000  # FIXED-P2: 使用get_running_loop替代废弃的get_event_loop

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
            latency_ms = (asyncio.get_running_loop().time() - start_time) * 1000  # FIXED-P1: 使用get_running_loop替代废弃get_event_loop
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
        async with self._lock:  # FIXED-P0: 锁内创建_drivers快照，防止迭代期间register/unregister修改
            device_ids = list(self._drivers.keys())
        results = {}
        # FIXED-P1: 原问题-asyncio.gather同时为所有设备发起心跳，设备数多时耗尽连接池。
        # 改为通过_heartbeat_concurrency信号量限制并发心跳检测数量。
        async def _check_with_limit(dev_id: str) -> HeartbeatResult:
            async with self._heartbeat_concurrency:
                return await self.check_device(dev_id)
        tasks = [
            _check_with_limit(device_id)
            for device_id in device_ids
        ]
        completed = await asyncio.gather(*tasks, return_exceptions=True)
        for device_id, result in zip(device_ids, completed, strict=False):
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
        async with self._lock:  # FIXED-P2: 共享状态修改加锁，与register/unregister互斥
            driver = self._drivers.get(device_id)
            if not driver:
                return

            if device_id in self._reconnect_backoff:
                self._reconnect_backoff[device_id]["attempt"] = 0
                logger.debug("Backoff reset for device: %s", device_id)

            self._reconnect_attempts.pop(device_id, None)
            self._circuit_open.discard(device_id)
            self._circuit_probe_time.pop(device_id, None)

            was_stale = device_id in self._stale_devices
            if was_stale:
                self._stale_devices.discard(device_id)
                self._stale_counters.pop(device_id, None)

            self._stale_counters.pop(device_id, None)
            # FIXED-BugR4X: 原问题-设备恢复后_long_offline_alarmed未清除导致下次离线无法再次告警，
            # 修复-心跳恢复时从_long_offline_alarmed中移除device_id，允许下次超长离线重新告警
            self._long_offline_alarmed.discard(device_id)
        # FIXED-P2: 事件发布在锁外，避免_lock与event_bus嵌套
        if was_stale:
            logger.info("Device recovered from stale: %s", device_id)
            await self._publish_stale_recovery_event(device_id)

        async with self._lock:
            driver = self._drivers.get(device_id)
        if not driver:
            return
        stats = driver.get_health_stats(device_id)
        if stats and stats.consecutive_failures > 0:
            old_status = "degraded" if stats.connection_quality_score < 80 else "offline"
            logger.info(
                "Device recovered: %s (offline_duration=%.1fs, consecutive_failures=%d)",
                device_id,
                stats.total_downtime_seconds,
                stats.consecutive_failures,
            )
            await self._publish_recovery_event(device_id, stats, old_status=old_status)

    async def _on_heartbeat_failure(
        self, device_id: str, result: HeartbeatResult
    ) -> None:
        """处理心跳失败"""
        should_publish_stale = False
        should_publish_restart = False
        should_publish_offline = False
        should_reconnect = False
        reconnect_in_circuit = False
        stale_count = 0
        async with self._lock:  # FIXED-P2: 共享状态修改加锁，与register/unregister互斥
            driver = self._drivers.get(device_id)
            if not driver:
                return

            self._stale_counters[device_id] = self._stale_counters.get(device_id, 0) + 1
            stale_count = self._stale_counters[device_id]
            if stale_count >= self._stale_cycles and device_id not in self._stale_devices:
                self._stale_devices.add(device_id)
                should_publish_stale = True

            if stale_count >= self._restart_cycles:
                should_publish_restart = True

            stats = driver.get_health_stats(device_id)
            if stats and stats.consecutive_failures == 1:
                self._offline_history.setdefault(device_id, []).append({
                    "since": datetime.now(UTC).isoformat(),
                    "reason": result.error_message,
                })
                if len(self._offline_history[device_id]) > 100:
                    self._offline_history[device_id] = self._offline_history[device_id][-100:]
                should_publish_offline = True

            if self._auto_reconnect:
                # FIXED-P2: 原问题-stats为None时永不触发重连；stats为None说明驱动无健康统计，
                # 仍应在重连次数未耗尽时尝试重连
                if stats is None:
                    if self._reconnect_attempts.get(device_id, 0) < self._max_reconnect_attempts:
                        should_reconnect = True
                    # FIXED-Bug15: stats is None 的设备进入熔断后也应触发探测，之前永久卡死
                    elif device_id in self._circuit_open:
                        import time as _time
                        now = _time.monotonic()
                        last_probe = self._circuit_probe_time.get(device_id, 0)
                        if now - last_probe >= 300:
                            self._circuit_probe_time[device_id] = now
                            reconnect_in_circuit = True
                elif stats:
                    if device_id in self._circuit_open:
                        import time as _time
                        now = _time.monotonic()
                        last_probe = self._circuit_probe_time.get(device_id, 0)
                        if now - last_probe >= 300:
                            self._circuit_probe_time[device_id] = now
                            reconnect_in_circuit = True
                    elif self._reconnect_attempts.get(device_id, 0) < self._max_reconnect_attempts:
                        should_reconnect = True
        if should_publish_stale:
            logger.warning("Device marked stale: %s (stale_cycles=%d)", device_id, stale_count)
            await self._publish_stale_event(device_id)

        if should_publish_restart:
            logger.error(
                "Device stale cycles exceeded restart threshold: %s (cycles=%d >= %d), triggering restart",
                device_id, stale_count, self._restart_cycles,
            )
            await self._publish_restart_event(device_id)

        if should_publish_offline:
            logger.warning("Device offline: %s (reason=%s)", device_id, result.error_message)
            await self._publish_offline_event(device_id, result.error_message)

        if reconnect_in_circuit:
            logger.info("Circuit-open probe for device: %s", device_id)
            # FIXED-P0: 原问题-_reconnecting检查和add在锁外，并发心跳失败可能同时通过检查导致同一设备并发重连。
            # 改为在锁内原子化检查并标记。
            async with self._lock:
                if device_id in self._reconnecting:
                    return
                self._reconnecting.add(device_id)
            try:
                await self._trigger_reconnect(device_id)
            finally:
                async with self._lock:
                    self._reconnecting.discard(device_id)
        elif should_reconnect:
            async with self._lock:
                if device_id in self._reconnecting:
                    return
                self._reconnecting.add(device_id)
            try:
                await self._trigger_reconnect(device_id)
            finally:
                async with self._lock:
                    self._reconnecting.discard(device_id)

    async def _check_long_offline(self, results: dict[str, HeartbeatResult]) -> None:
        """检查超长离线设备"""
        # FIXED-P0: asyncio.Lock必须用async with，不能用同步with
        async with self._lock:
            drivers_snapshot = dict(self._drivers)
        for device_id, result in results.items():
            if not result.success:
                driver = drivers_snapshot.get(device_id)
                if not driver:
                    continue

                stats = driver.get_health_stats(device_id)
                if stats and stats.total_downtime_seconds >= self._max_offline_duration:
                    # FIXED-BugR4X: 原问题-每次循环都发布long_offline告警无去重导致告警风暴，
                    # 修复-发布前检查device_id是否已在_long_offline_alarmed中，是则跳过，否则添加后发布
                    if device_id in self._long_offline_alarmed:
                        continue
                    self._long_offline_alarmed.add(device_id)
                    logger.error(
                        "Device offline too long: %s (duration=%.1fs > threshold=%.1fs)",
                        device_id,
                        stats.total_downtime_seconds,
                        self._max_offline_duration,
                    )
                    await self._publish_long_offline_event(device_id, stats.total_downtime_seconds)

    async def _trigger_reconnect(self, device_id: str) -> None:
        """触发设备重连（优先使用驱动基类的reconnect_with_backoff）"""
        # FIXED(严重): _drivers无锁访问保护
        async with self._lock:
            driver = self._drivers.get(device_id)
        if not driver:
            return

        if hasattr(driver, "reconnect_with_backoff") and asyncio.iscoroutinefunction(
            driver.reconnect_with_backoff
        ):
            try:
                success = await driver.reconnect_with_backoff(device_id)
                if not success:
                    logger.warning(
                        "Driver reconnect_with_backoff exhausted for device %s, falling back to watchdog backoff",
                        device_id,
                    )
                    await self._reconnect_with_backoff(device_id)
            except Exception as e:
                logger.error("Driver reconnect_with_backoff error for %s: %s", device_id, e)
                await self._reconnect_with_backoff(device_id)
        else:
            await self._reconnect_with_backoff(device_id)

    async def _reconnect_with_backoff(
        self,
        device_id: str,
        base_interval: float = 5.0,
        max_interval: float = 60.0,
    ) -> None:
        """指数退避重连策略

        重连间隔公式: min(base_interval × 2^attempt, max_interval)
        默认 base=5s, max=60s → 5, 10, 20, 40, 60, 60...
        """
        backoff = self._reconnect_backoff.setdefault(device_id, {
            "attempt": 0,
            "base_interval": base_interval,
            "max_interval": max_interval,
        })

        attempt = backoff["attempt"]
        base = backoff["base_interval"]
        cap = backoff["max_interval"]
        delay = min(base * (2 ** attempt), cap)
        delay *= 0.5 + random.random() * 0.5  # FIXED-P4: 原问题-退避无抖动，多设备同时重连惊群效应

        logger.info(
            "Reconnect with backoff: device=%s, attempt=%d, delay=%.1fs",
            device_id, attempt, delay,
        )

        await asyncio.sleep(delay)

        # FIXED(严重): _drivers无锁访问保护
        async with self._lock:
            driver = self._drivers.get(device_id)
        if not driver:
            return

        try:
            if hasattr(driver, "reconnect") and asyncio.iscoroutinefunction(
                driver.reconnect
            ):
                success = await driver.reconnect(device_id)
            else:
                logger.warning(
                    "Driver %s has no reconnect() method, cannot auto-reconnect device %s",
                    type(driver).__name__, device_id,
                )
                success = False

            if success:
                # FIXED-P0: asyncio.Lock必须用async with
                async with self._lock:
                    backoff["attempt"] = 0
                    self._reconnect_attempts.pop(device_id, None)
                    self._circuit_open.discard(device_id)
                    self._circuit_probe_time.pop(device_id, None)
                logger.info("Reconnect succeeded: device=%s, backoff reset", device_id)
            else:
                # FIXED-P0: asyncio.Lock必须用async with
                async with self._lock:
                    backoff["attempt"] = attempt + 1
                    self._reconnect_attempts[device_id] = self._reconnect_attempts.get(device_id, 0) + 1
                    if self._reconnect_attempts[device_id] >= self._max_reconnect_attempts:
                        import time as _time
                        self._circuit_open.add(device_id)
                        # FIXED-Bug15: 初始化为当前 monotonic 时间，之前为 0 导致系统运行 5 分钟后熔断探测立即触发（300s 冷却失效）
                        self._circuit_probe_time[device_id] = _time.monotonic()
                        logger.warning("Device %s entered circuit-open state after %d reconnect failures", device_id, self._reconnect_attempts[device_id])
                logger.warning(
                    "Reconnect failed: device=%s, next attempt=%d",
                    device_id, backoff["attempt"],
                )
        except Exception as e:
            # FIXED-P0: 原问题-异常处理中修改backoff["attempt"]和_reconnect_attempts在锁外执行，
            # 多协程并发重连同一设备时计数错乱。改为在锁内修改共享状态。
            async with self._lock:
                backoff["attempt"] = attempt + 1
                self._reconnect_attempts[device_id] = self._reconnect_attempts.get(device_id, 0) + 1  # FIXED-P0: 看门狗重连使用独立计数器而非consecutive_failures
                if self._reconnect_attempts[device_id] >= self._max_reconnect_attempts:
                    import time as _time
                    self._circuit_open.add(device_id)
                    # FIXED-Bug15: 同上，初始化为当前 monotonic 时间
                    self._circuit_probe_time[device_id] = _time.monotonic()
            logger.warning("Reconnect exception for %s: %s", device_id, e)

    async def _publish_recovery_event(self, device_id: str, stats: Any, old_status: str = "offline") -> None:
        """发布设备恢复事件"""
        if self._event_bus:
            try:
                from edgelite.engine.event_bus import DeviceStatusEvent

                event = DeviceStatusEvent(
                    device_id=device_id,
                    old_status=old_status,
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

    async def _publish_stale_recovery_event(self, device_id: str) -> None:
        """发布设备从stale/degraded恢复事件"""
        if self._event_bus:
            try:
                from edgelite.engine.event_bus import DeviceStatusEvent

                event = DeviceStatusEvent(
                    device_id=device_id,
                    old_status="degraded",
                    new_status="online",
                )
                await self._event_bus.publish(event)
            except Exception as e:
                logger.debug("Failed to publish stale recovery event: %s", e)

        if self._on_status_change:
            try:
                self._on_status_change(device_id, "online")
            except Exception as e:
                logger.debug("Status change callback error: %s", e)

    async def _publish_stale_event(self, device_id: str) -> None:
        """发布设备stale状态事件"""
        if self._event_bus:
            try:
                from edgelite.engine.event_bus import DeviceStatusEvent

                event = DeviceStatusEvent(
                    device_id=device_id,
                    old_status="online",
                    new_status="degraded",
                )
                await self._event_bus.publish(event)
            except Exception as e:
                logger.debug("Failed to publish stale event: %s", e)

        if self._on_status_change:
            try:
                self._on_status_change(device_id, "degraded")
            except Exception as e:
                logger.debug("Status change callback error: %s", e)

    async def _publish_restart_event(self, device_id: str) -> None:
        """发布设备采集重启事件"""
        if self._event_bus:
            try:
                from edgelite.engine.event_bus import DeviceStatusEvent

                event = DeviceStatusEvent(
                    device_id=device_id,
                    old_status="degraded",
                    new_status="restarting",
                )
                await self._event_bus.publish(event)
            except Exception as e:
                logger.debug("Failed to publish restart event: %s", e)

        if self._on_status_change:
            try:
                self._on_status_change(device_id, "restarting")
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
        """获取连接状态摘要（近似值，同步方法无法加asyncio.Lock）"""
        drivers_snapshot = dict(self._drivers)
        total = len(drivers_snapshot)
        online = 0
        offline = 0
        degraded = 0
        stale = len(self._stale_devices)

        for device_id, driver in drivers_snapshot.items():
            stats = driver.get_health_stats(device_id)
            if stats:
                if stats.consecutive_failures == 0:
                    online += 1
                else:
                    offline += 1
                if stats.connection_quality_score < 80:
                    degraded += 1
            else:
                online += 1

        return {
            "total_devices": total,
            "online": online,
            "offline": offline,
            "degraded": degraded,
            "stale": stale,
            "health_rate": round((online / total * 100) if total > 0 else 100, 2),
        }

    async def get_connection_summary_async(self) -> dict:
        """获取连接状态摘要（精确值，asyncio.Lock保护）"""
        async with self._lock:  # FIXED-P1: 锁内读取_drivers/_stale_devices快照，与register/unregister互斥
            drivers_snapshot = dict(self._drivers)
            stale_count = len(self._stale_devices)

        total = len(drivers_snapshot)
        online = 0
        offline = 0
        degraded = 0

        for device_id, driver in drivers_snapshot.items():
            stats = driver.get_health_stats(device_id)
            if stats:
                if stats.consecutive_failures == 0:
                    online += 1
                else:
                    offline += 1
                if stats.connection_quality_score < 80:
                    degraded += 1
            else:
                online += 1

        return {
            "total_devices": total,
            "online": online,
            "offline": offline,
            "degraded": degraded,
            "stale": stale_count,
            "health_rate": round((online / total * 100) if total > 0 else 100, 2),
        }


# 全局看门狗实例
_driver_watchdog: DriverWatchdog | None = None
_driver_watchdog_lock = threading.Lock()  # FIXED-P0: 全局单例初始化竞态保护


def get_driver_watchdog() -> DriverWatchdog:
    """获取全局驱动看门狗"""
    global _driver_watchdog
    with _driver_watchdog_lock:  # FIXED-P0: 全局单例初始化竞态保护
        if _driver_watchdog is None:
            _driver_watchdog = DriverWatchdog()
        return _driver_watchdog
