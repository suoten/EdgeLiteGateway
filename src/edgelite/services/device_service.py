"""设备管理业务逻辑"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from edgelite.constants import _MAX_QUERY_SIZE
from edgelite.drivers.registry import get_driver_registry
from edgelite.drivers.simulator import SimulatorDriver
from edgelite.engine.lifecycle import DeviceLifecycleManager
from edgelite.engine.scheduler import CollectScheduler
from edgelite.storage.sqlite_repo import DeviceRepo, RuleRepo

logger = logging.getLogger(__name__)


class DeviceService:
    """设备管理业务逻辑"""

    def __init__(
        self,
        device_repo: DeviceRepo,
        rule_repo: RuleRepo,
        scheduler: CollectScheduler,
        lifecycle: DeviceLifecycleManager,
    ):
        self._repo = device_repo
        self._rule_repo = rule_repo
        self._scheduler = scheduler
        self._lifecycle = lifecycle
        self._registry = get_driver_registry()
        self._driver_instances: dict[str, Any] = {}
        self._simulator_driver: SimulatorDriver | None = None
        self._lock = asyncio.Lock()

    async def _get_simulator_driver(self) -> SimulatorDriver:
        """获取模拟器驱动实例"""
        if self._simulator_driver is None:
            self._simulator_driver = SimulatorDriver()
            await self._simulator_driver.start({})
        return self._simulator_driver

    async def create_device(self, data: dict) -> dict:
        """创建设备"""
        async with self._lock:
            return await self._create_device_unlocked(data)

    async def _create_device_unlocked(self, data: dict) -> dict:
        protocol = data.get("protocol")  # FIXED: 原问题-data["protocol"]硬索引
        if protocol is None:
            raise ValueError("Missing required field: protocol")
        driver_class = self._registry.get_driver_class(protocol)
        if driver_class is None and protocol != "simulator":
            raise ValueError(f"Unsupported protocol: {protocol}")  # FIXED: 原问题-中文硬编码错误消息

        device = await self._repo.create(data)

        _driver_instance: Any = None
        _is_simulator = False
        try:
            if protocol == "simulator":
                _is_simulator = True
                driver = await self._get_simulator_driver()
                driver.add_device(device["device_id"], data.get("points", []))
                self._driver_instances[device["device_id"]] = driver
                await self._lifecycle.on_device_online(device["device_id"])
                await self._repo.update_status(device["device_id"], "online")
                await self._scheduler.start_collect(
                    device["device_id"],
                    driver,
                    data.get("points", []),
                    data.get("collect_interval", 5),
                )
            elif driver_class is not None:
                driver = driver_class()
                _driver_instance = driver  # FIXED: 记录引用用于异常回滚
                await driver.start(data.get("config", {}))
                with contextlib.suppress(NotImplementedError):
                    await driver.add_device(
                        device["device_id"],
                        data.get("config", {}),
                        data.get("points", []),
                    )
                self._driver_instances[device["device_id"]] = driver
                connected = hasattr(driver, "is_device_connected") and driver.is_device_connected(
                    device["device_id"]
                )
                if connected:
                    await self._lifecycle.on_device_online(device["device_id"])
                    await self._repo.update_status(device["device_id"], "online")
                else:
                    await self._repo.update_status(device["device_id"], "offline")
                await self._scheduler.start_collect(
                    device["device_id"],
                    driver,
                    data.get("points", []),
                    data.get("collect_interval", 5),
                )
            else:
                logger.warning("设备创建: %s (协议=%s，无注册驱动)", device["device_id"], protocol)
        except Exception as e:
            logger.error("设备驱动启动失败，回滚数据库记录: %s - %s", device["device_id"], e)
            await self._repo.delete(device["device_id"])
            # FIXED: 若driver.start()成功但后续步骤失败，需停止已启动的driver防止资源泄漏
            # simulator驱动是共享单例，不能stop
            if _driver_instance is not None and not _is_simulator:
                try:
                    await _driver_instance.stop()
                except Exception as stop_err:
                    logger.debug("驱动停止失败(回滚中): %s", stop_err)
            raise ValueError(f"Device driver start failed: {e}") from e  # FIXED: 原问题-中文硬编码错误消息

        return device

    async def get_device(self, device_id: str) -> dict | None:
        return await self._repo.get(device_id)

    async def list_devices(
        self,
        page: int = 1,
        size: int = 20,
        status: str | None = None,
        protocol: str | None = None,
        search: str | None = None,
    ) -> tuple[list[dict], int]:
        return await self._repo.list_all(page, size, status, protocol, search)

    async def update_device(self, device_id: str, data: dict) -> dict | None:
        return await self._repo.update(device_id, data)

    async def delete_device(self, device_id: str) -> tuple[bool, str | None]:
        """删除设备，返回(成功, 错误信息)"""
        async with self._lock:
            return await self._delete_device_unlocked(device_id)

    async def _delete_device_unlocked(self, device_id: str) -> tuple[bool, str | None]:
        # 检查规则关联
        rules, _ = await self._rule_repo.list_all(device_id=device_id)
        active_rules = [r for r in rules if r.get("enabled", False)]
        if active_rules:
            rule_names = ", ".join(r["name"] for r in active_rules[:3])
            return False, f"Device referenced by rules: {rule_names}"  # FIXED: 原问题-中文硬编码错误消息

        # 停止采集
        await self._scheduler.stop_collect(device_id)

        # 停止驱动
        driver = self._driver_instances.pop(device_id, None)
        if driver:
            if isinstance(driver, SimulatorDriver):
                driver.remove_device(device_id)
            elif hasattr(driver, "stop"):
                try:
                    await driver.stop()
                except Exception as e:
                    logger.warning("驱动停止失败 %s: %s", device_id, e)

        # 删除记录
        success = await self._repo.delete(device_id)
        return success, None

    async def read_points(self, device_id: str) -> dict[str, Any]:
        """读取设备测点值：优先从调度器缓存获取（毫秒级），缓存无数据时走驱动实时读取"""
        # 1. 优先从调度器缓存获取最近采集值（毫秒级返回）
        if self._scheduler:
            cached = self._scheduler.get_last_values(device_id)
            if cached:
                return cached

        # 2. 缓存无数据时走驱动实时读取（可能较慢或失败）
        driver = self._driver_instances.get(device_id)
        if driver is None:
            return {}

        device = await self._repo.get(device_id)
        if device is None:
            return {}

        points = device.get("points") or []
        point_names = [p.get("name") for p in points if p.get("name") is not None]
        try:
            return await driver.read_points(device_id, point_names)
        except Exception as e:
            # FIXED: 驱动读取异常时返回空数据而非传播异常，避免前端无限等待
            logger.warning("驱动读取测点异常 %s: %s", device_id, e)
            return {}

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        """写入设备测点值"""
        driver = self._driver_instances.get(device_id)
        if driver is None:
            return False
        return await driver.write_point(device_id, point, value)

    async def create_simulator(self, data: dict) -> dict:
        """创建模拟设备"""
        data["protocol"] = "simulator"
        data.setdefault("config", {"timeout": 5.0})
        return await self.create_device(data)

    async def start_collect(self, device_id: str) -> bool:
        """启动设备采集"""
        device = await self._repo.get(device_id)
        if device is None:
            raise ValueError(f"Device not found: {device_id}")
        driver = self._driver_instances.get(device_id)
        if driver is None:
            raise ValueError(f"Device driver not found: {device_id}")
        await self._scheduler.start_collect(
            device_id,
            driver,
            device.get("points", []),
            device.get("collect_interval", 5),
        )
        await self._lifecycle.on_device_online(device_id)
        await self._repo.update_status(device_id, "online")
        logger.info("Device %s collect started", device_id)
        return True

    async def stop_collect(self, device_id: str) -> bool:
        """停止设备采集"""
        await self._scheduler.stop_collect(device_id)
        await self._lifecycle.on_device_offline(device_id)
        await self._repo.update_status(device_id, "offline")
        logger.info("Device %s collect stopped", device_id)
        return True

    async def discover_devices(self, protocol: str, config: dict) -> list[dict]:
        """设备发现"""
        driver_class = self._registry.get_driver_class(protocol)
        if driver_class is None:
            return []

        driver = driver_class()
        await driver.start({})
        try:
            result = await driver.discover_devices(config)
        finally:
            await driver.stop()
        return result

    async def batch_delete_devices(self, device_ids: list[str]) -> dict:
        """Batch delete devices, returns {device_id: (success, error_msg)}"""
        results: dict[str, tuple[bool, str | None]] = {}
        for device_id in device_ids:
            success, error = await self.delete_device(device_id)
            results[device_id] = (success, error)
        return results

    async def batch_start_collect(self, device_ids: list[str]) -> dict:
        """Batch start collect, returns {device_id: (success, error_msg)}"""
        results: dict[str, tuple[bool, str | None]] = {}
        for device_id in device_ids:
            try:
                await self.start_collect(device_id)
                results[device_id] = (True, None)
            except Exception as e:
                results[device_id] = (False, str(e))
        return results

    async def batch_stop_collect(self, device_ids: list[str]) -> dict:
        """Batch stop collect, returns {device_id: (success, error_msg)}"""
        results: dict[str, tuple[bool, str | None]] = {}
        for device_id in device_ids:
            try:
                await self.stop_collect(device_id)
                results[device_id] = (True, None)
            except Exception as e:
                results[device_id] = (False, str(e))
        return results

    async def load_existing_devices(self) -> None:
        """启动时加载所有已有设备并恢复采集"""
        page = 1
        size = _MAX_QUERY_SIZE  # FIXED: 原问题-size=1000魔法数字
        while True:
            devices, total = await self._repo.list_all(page=page, size=size)
            for device in devices:
                try:
                    protocol = device.get("protocol")  # FIXED: 原问题-device["protocol"]硬索引
                    if protocol is None:
                        continue
                    driver_class = self._registry.get_driver_class(protocol)
                    if driver_class is None:
                        continue

                    if protocol == "simulator":
                        driver = await self._get_simulator_driver()
                        driver.add_device(device["device_id"], device.get("points", []))
                        self._driver_instances[device["device_id"]] = driver
                        await self._scheduler.start_collect(
                            device["device_id"],
                            driver,
                            device.get("points", []),
                            device.get("collect_interval", 5),
                        )
                    elif driver_class is not None:
                        driver = driver_class()
                        await driver.start(device.get("config", {}))
                        with contextlib.suppress(NotImplementedError):
                            await driver.add_device(
                                device["device_id"],
                                device.get("config", {}),
                                device.get("points", []),
                            )
                        self._driver_instances[device["device_id"]] = driver
                        connected = hasattr(
                            driver, "is_device_connected"
                        ) and driver.is_device_connected(device["device_id"])
                        if connected:
                            await self._lifecycle.on_device_online(device["device_id"])
                            await self._repo.update_status(device["device_id"], "online")
                        else:
                            await self._repo.update_status(device["device_id"], "offline")
                        await self._scheduler.start_collect(
                            device["device_id"],
                            driver,
                            device.get("points", []),
                            device.get("collect_interval", 5),
                        )
                except Exception as e:
                    logger.warning("恢复设备采集失败 %s: %s", device["device_id"], e)
            if page * size >= total:
                break
            page += 1
        logger.info("已加载%d个设备", total)
