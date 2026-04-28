"""设备管理业务逻辑"""

from __future__ import annotations

import logging
from typing import Any

from edgelite.storage.sqlite_repo import DeviceRepo, RuleRepo
from edgelite.engine.scheduler import CollectScheduler
from edgelite.engine.lifecycle import DeviceLifecycleManager
from edgelite.drivers.registry import get_driver_registry
from edgelite.drivers.simulator import SimulatorDriver

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
        # device_id -> driver_instance
        self._driver_instances: dict[str, Any] = {}
        # 模拟器驱动（共享实例）
        self._simulator_driver: SimulatorDriver | None = None

    async def _get_simulator_driver(self) -> SimulatorDriver:
        """获取模拟器驱动实例"""
        if self._simulator_driver is None:
            self._simulator_driver = SimulatorDriver()
            await self._simulator_driver.start({})
        return self._simulator_driver

    async def create_device(self, data: dict) -> dict:
        """创建设备"""
        protocol = data["protocol"]
        driver_class = self._registry.get_driver_class(protocol)
        if driver_class is None:
            logger.warning("不支持的协议: %s", protocol)

        device = await self._repo.create(data)

        try:
            if protocol == "simulator":
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
            elif protocol == "modbus_tcp":
                driver = driver_class()
                await driver.start({})
                await driver.add_device(
                    device["device_id"],
                    data.get("config", {}),
                    data.get("points", []),
                )
                self._driver_instances[device["device_id"]] = driver
                if hasattr(driver, 'is_device_connected') and driver.is_device_connected(device["device_id"]):
                    await self._lifecycle.on_device_online(device["device_id"])
                    await self._repo.update_status(device["device_id"], "online")
                    await self._scheduler.start_collect(
                        device["device_id"],
                        driver,
                        data.get("points", []),
                        data.get("collect_interval", 5),
                    )
            else:
                logger.info("设备创建: %s (协议=%s，待实现驱动连接)", device["device_id"], protocol)
        except Exception as e:
            logger.error("设备驱动启动失败，回滚数据库记录: %s - %s", device["device_id"], e)
            await self._repo.delete(device["device_id"])
            raise ValueError(f"设备驱动启动失败: {e}")

        return device

    async def get_device(self, device_id: str) -> dict | None:
        return await self._repo.get(device_id)

    async def list_devices(
        self, page: int = 1, size: int = 20, status: str | None = None, protocol: str | None = None
    ) -> tuple[list[dict], int]:
        return await self._repo.list_all(page, size, status, protocol)

    async def update_device(self, device_id: str, data: dict) -> dict | None:
        return await self._repo.update(device_id, data)

    async def delete_device(self, device_id: str) -> tuple[bool, str | None]:
        """删除设备，返回(成功, 错误信息)"""
        # 检查规则关联
        rules, _ = await self._rule_repo.list_all(device_id=device_id)
        active_rules = [r for r in rules if r.get("enabled", False)]
        if active_rules:
            rule_names = ", ".join(r["name"] for r in active_rules[:3])
            return False, f"设备被规则引用: {rule_names}"

        # 停止采集
        await self._scheduler.stop_collect(device_id)

        # 停止驱动
        driver = self._driver_instances.pop(device_id, None)
        if driver and isinstance(driver, SimulatorDriver):
            driver.remove_device(device_id)

        # 删除记录
        success = await self._repo.delete(device_id)
        return success, None

    async def read_points(self, device_id: str) -> dict[str, Any]:
        """读取设备实时测点值"""
        driver = self._driver_instances.get(device_id)
        if driver is None:
            return {}

        device = await self._repo.get(device_id)
        if device is None:
            return {}

        point_names = [p["name"] for p in device.get("points", [])]
        return await driver.read_points(device_id, point_names)

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

    async def discover_devices(self, protocol: str, config: dict) -> list[dict]:
        """设备发现"""
        driver_class = self._registry.get_driver_class(protocol)
        if driver_class is None:
            return []

        driver = driver_class()
        await driver.start({})
        result = await driver.discover_devices(config)
        await driver.stop()
        return result

    async def load_existing_devices(self) -> None:
        """启动时加载所有已有设备并恢复采集"""
        page = 1
        size = 1000
        while True:
            devices, total = await self._repo.list_all(page=page, size=size)
            for device in devices:
                try:
                    protocol = device["protocol"]
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
                    elif protocol != "simulator":
                        await self._repo.update_status(device["device_id"], "offline")
                except Exception as e:
                    logger.warning("恢复设备采集失败 %s: %s", device["device_id"], e)
            if page * size >= total:
                break
            page += 1
        logger.info("已加载%d个设备", total)
