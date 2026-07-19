"""设备管理业务逻辑"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
import time
from datetime import UTC, datetime  # FIXED-P1: read_points归一化需要
from typing import Any, cast

from edgelite.constants import _MAX_QUERY_SIZE
from edgelite.drivers.base import DriverHealthStats, DriverPlugin
from edgelite.drivers.registry import get_driver_registry
from edgelite.drivers.simulator import SimulatorDriver
from edgelite.engine.lifecycle import DeviceLifecycleManager
from edgelite.engine.scheduler import CollectScheduler
from edgelite.storage.sqlite_repo import DeviceRepo, RuleRepo, TemplateRepo, _now

logger = logging.getLogger(__name__)

# FIXED(严重): 使用 contextvars 隔离协程级用户上下文，避免多协程并发写入
# 同一设备时 driver._current_write_user 实例属性被互相覆盖，导致审计日志记录错误用户
_current_write_user_var: contextvars.ContextVar[str] = contextvars.ContextVar("current_write_user", default="")


class DeviceService:
    """设备管理业务逻辑"""

    def __init__(
        self,
        device_repo: DeviceRepo,
        rule_repo: RuleRepo,
        scheduler: CollectScheduler,
        lifecycle: DeviceLifecycleManager,
        template_repo: TemplateRepo | None = None,
    ):
        self._repo = device_repo
        self._rule_repo = rule_repo
        self._scheduler = scheduler
        self._lifecycle = lifecycle
        self._template_repo = template_repo
        self._registry = get_driver_registry()
        self._driver_instances: dict[str, Any] = {}
        self._simulator_driver: SimulatorDriver | None = None
        self._lock = asyncio.Lock()
        # FIXED: per-device 采集锁，串行化 start_collect/stop_collect 的 check-then-act 流程，
        # 防止并发调用导致状态不一致（如同时 start+stop 导致 scheduler 已停但状态仍 online）
        self._collect_locks: dict[str, asyncio.Lock] = {}
        self._cleanup_tasks: set[asyncio.Task] = set()
        self._sidecar_compensation_task: asyncio.Task | None = None  # FIXED-P0: 跨库级联清理补偿任务

    async def _get_simulator_driver(self) -> SimulatorDriver:
        """获取模拟器驱动实例（线程安全，获取 self._lock）"""
        async with self._lock:
            return await self._get_simulator_driver_unlocked()

    async def _get_simulator_driver_unlocked(self) -> SimulatorDriver:
        """获取模拟器驱动实例（调用者必须已持有 self._lock）"""
        if self._simulator_driver is None:
            self._simulator_driver = SimulatorDriver()
            await self._simulator_driver.start({})
        return self._simulator_driver

    async def get_driver_instance(self, device_id: str) -> Any:
        """FIXED-P0: 公开访问器，替代直接访问_driver_instances私有属性，确保锁保护。
        返回指定设备的驱动实例，若不存在返回None。"""
        async with self._lock:
            return self._driver_instances.get(device_id)

    async def remove_driver_instance(self, device_id: str) -> Any:
        """FIXED-P0: 公开访问器，替代直接pop _driver_instances私有属性，确保锁保护。
        移除并返回指定设备的驱动实例，若不存在返回None。"""
        async with self._lock:
            return self._driver_instances.pop(device_id, None)

    async def get_lifecycle(self) -> Any:
        """FIXED-P1: 公开访问器，替代直接 getattr 访问 _lifecycle 私有属性，确保锁保护。
        返回生命周期管理器实例，若未注入返回None。"""
        async with self._lock:
            return self._lifecycle

    async def get_repo(self) -> Any:
        """FIXED-P1: 公开访问器，替代直接 getattr 访问 _repo 私有属性，确保锁保护。
        返回设备仓库实例，若未注入返回None。"""
        async with self._lock:
            return self._repo

    async def create_device(self, data: dict, created_by: str | None = None) -> dict:
        async with self._lock:
            return await self._create_device_unlocked(data, created_by)

    async def _create_device_unlocked(self, data: dict, created_by: str | None = None) -> dict:
        protocol = data.get("protocol")  # FIXED: 原问题-data["protocol"]硬索引
        if protocol is None:
            raise ValueError("Missing required field: protocol")
        driver_class = self._registry.get_driver_class(protocol)
        if driver_class is None and protocol != "simulator":
            raise ValueError(f"Unsupported protocol: {protocol}")

        device = await self._repo.create(data, created_by=created_by)

        _driver_instance: Any = None
        _is_simulator = False
        try:
            if protocol == "simulator":
                _is_simulator = True
                # FIXED-P0: 原代码调用 _get_simulator_driver() 会再次获取 self._lock，
                # 但 asyncio.Lock 不可重入，导致死锁。改为调用 _get_simulator_driver_unlocked()，
                # 因为 _create_device_unlocked 的调用者 create_device 已持有 self._lock。
                driver: DriverPlugin = await self._get_simulator_driver_unlocked()
                await driver.add_device(
                    device["device_id"], {}, data.get("points", [])
                )  # FIXED: 补充 await 并修正参数顺序
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
                # R8-S-01 修复: driver.start() 在 async with self._lock 内调用，
                # 无超时可能无限阻塞全局锁，导致其他设备操作（create/delete/read）全部阻塞。
                # 添加 30s 超时保护，超时则抛出 TimeoutError 触发下方 except 回滚逻辑。
                await asyncio.wait_for(driver.start(data.get("config", {})), timeout=30.0)
                with contextlib.suppress(NotImplementedError):
                    await driver.add_device(
                        device["device_id"],
                        data.get("config", {}),
                        data.get("points", []),
                    )
                self._driver_instances[device["device_id"]] = driver
                connected = hasattr(driver, "is_device_connected") and driver.is_device_connected(device["device_id"])
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
                logger.warning(
                    "Device created: %s (protocol=%s, no registered driver)",
                    device["device_id"],
                    protocol,
                )
        except Exception as e:
            logger.error(
                "Device driver start failed, rolling back database record: %s - %s",
                device["device_id"],
                e,
            )
            await self._repo.delete(device["device_id"])
            # R8-S-03 修复: 回滚仅删除 DB 记录和停止 driver，不调用 scheduler.stop_collect，
            # 若 start_collect 已部分启动采集任务则会留下僵尸采集任务持续占用资源。
            # 此处防御性调用 stop_collect（幂等，无任务时不会报错），加 5s 超时避免阻塞回滚。
            try:
                await asyncio.wait_for(self._scheduler.stop_collect(device["device_id"]), timeout=5.0)
            except Exception as stop_collect_err:
                logger.debug(
                    "stop_collect failed during rollback for %s: %s",
                    device["device_id"],
                    stop_collect_err,
                )
            # FIXED: 若driver.start()成功但后续步骤失败，需停止已启动的driver防止资源泄漏
            # simulator驱动是共享单例，不能stop，但需移除设备映射
            if _is_simulator:
                try:
                    _driver_instance = self._driver_instances.pop(device["device_id"], None)
                    if _driver_instance is not None:
                        _driver_instance.remove_device(device["device_id"])
                except Exception as sim_err:
                    logger.debug("Simulator remove_device failed (during rollback): %s", sim_err)
            elif _driver_instance is not None:
                # FIXED-P1: 原问题-非模拟器路径except块未从_driver_instances中移除已停止的driver，
                # 导致残留driver实例被后续read_points/get_device_health等方法使用，返回陈旧数据
                self._driver_instances.pop(device["device_id"], None)
                try:
                    await _driver_instance.stop()
                except Exception as stop_err:
                    logger.debug("Driver stop failed (during rollback): %s", stop_err)
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
        created_by: str | None = None,
        collect_status: str | None = None,
    ) -> tuple[list[dict], int]:
        return cast(
            tuple[list[dict], int],
            await self._repo.list_all(
                page, size, status, protocol, search, created_by, collect_status=collect_status
            ),
        )

    async def list_device_ids_by_owner(self, created_by: str) -> list[str]:
        return await self._repo.list_device_ids_by_owner(created_by)

    async def list_devices_by_ids(self, device_ids: list[str]) -> list[dict]:
        """LP-07: 批量查询设备列表，避免 N+1 查询。"""
        return await self._repo.list_devices_by_ids(device_ids)

    async def get_status_counts(self, device_ids: list[str] | None = None) -> dict[str, int]:
        """R9-S-08: 使用SQL聚合查询按状态统计设备数量，避免全量加载到内存。"""
        return await self._repo.get_status_counts(device_ids)

    async def update_device(self, device_id: str, data: dict) -> dict | None:
        """更新设备，若配置/点位变更且设备运行中则重载驱动。

        R9-S-01 修复: 原实现仅更新数据库，不重载运行中的驱动实例，
        导致配置/点位变更不生效。现检测 config/points 变化，若设备处于
        运行状态（有驱动实例）则重载驱动。使用 self._lock 保护检查，
        防止并发更新竞态。
        """
        # 获取变更前设备数据，用于判断 config/points 是否变化及失败回滚
        old_device = await self._repo.get(device_id)

        # 更新数据库
        updated = await self._repo.update(device_id, data)
        if updated is None:
            return None

        # 判断 config/points 是否发生变化
        config_changed = False
        if old_device is not None:
            if "config" in data and data["config"] != old_device.get("config"):
                config_changed = True
            if "points" in data and data["points"] != old_device.get("points"):
                config_changed = True

        # 若配置/点位变化且设备处于运行状态，重载驱动
        if config_changed and old_device is not None:
            # 使用锁保护检查，防止并发更新竞态
            async with self._lock:
                has_driver = device_id in self._driver_instances
            if has_driver:
                await self._reload_driver_for_device(device_id, old_device)

        return updated

    async def _reload_driver_for_device(self, device_id: str, old_device: dict) -> None:
        """重载设备驱动：停止旧驱动 → 移除实例 → 用新配置加载并启动。

        R9-S-01: 在锁保护下执行关键步骤，防止并发更新竞态。
        失败时回滚到旧配置并重启旧驱动，记录错误日志。
        """
        # 1. 锁保护下移除旧驱动实例
        async with self._lock:
            old_driver = self._driver_instances.pop(device_id, None)

        # 停止采集（锁外执行，避免长时间持锁阻塞其他设备操作）
        try:
            await asyncio.wait_for(self._scheduler.stop_collect(device_id), timeout=5.0)
        except Exception as e:
            logger.warning("重载驱动-停止采集失败 %s: %s", device_id, e)

        # 停止旧驱动
        if old_driver is not None:
            if isinstance(old_driver, SimulatorDriver):
                await old_driver.remove_device(device_id)  # FIXED: 补充 await
            elif hasattr(old_driver, "stop"):
                try:
                    await asyncio.wait_for(old_driver.stop(), timeout=5.0)
                except Exception as e:
                    logger.warning("重载驱动-停止旧驱动失败 %s: %s", device_id, e)

        # 2. 用新配置加载并启动驱动（_load_driver_for_device 内部处理锁）
        try:
            new_device = await self._repo.get(device_id)
            if new_device is None:
                logger.error("重载驱动失败-设备不存在 %s", device_id)
                return
            await self._load_driver_for_device(new_device)
            logger.info("设备驱动重载成功 %s", device_id)
        except Exception as e:
            # 3. 重载失败，回滚到旧配置
            logger.error("设备驱动重载失败，回滚到旧配置 %s: %s", device_id, e)
            rollback_data = {}
            if "config" in old_device:
                rollback_data["config"] = old_device["config"]
            if "points" in old_device:
                rollback_data["points"] = old_device["points"]
            if rollback_data:
                try:
                    await self._repo.update(device_id, rollback_data)
                except Exception as rb_err:
                    logger.error("回滚设备配置失败 %s: %s", device_id, rb_err)
            # 重启旧驱动
            try:
                await self._load_driver_for_device(old_device)
            except Exception as restart_err:
                logger.error("回滚-重启旧驱动失败 %s: %s", device_id, restart_err)

    async def delete_device(self, device_id: str) -> tuple[bool, str | None]:
        """删除设备，返回(成功, 错误信息)

        FIXED: 删除操作不再使用 self._lock，避免因其他操作（如 create_device 中
        driver.start() 阻塞）持锁导致删除超时。删除的核心操作是 DB 删除（快速），
        清理操作异步执行，不需要全局互斥锁保护。
        """
        return await self._delete_device_unlocked(device_id)

    async def _delete_device_unlocked(self, device_id: str) -> tuple[bool, str | None]:
        # 检查规则关联
        rules, _, *_ = await self._rule_repo.list_all(device_id=device_id)
        active_rules = [r for r in rules if r.get("enabled", False)]
        if active_rules:
            rule_names = ", ".join(r["name"] for r in active_rules[:3])
            return False, (f"Device referenced by rules: {rule_names}")  # FIXED: 原问题-中文硬编码错误消息

        # 先删除数据库记录（快速操作），再异步清理资源
        # 这样即使后续清理超时，设备记录也已删除，用户不会看到超时
        success = await self._repo.delete(device_id)
        if not success:
            return False, "Failed to delete device record"

        # FIXED(致命): 记录待清理的 driver 引用，cleanup 时校验是否仍为同一实例
        # 原问题：_schedule_cleanup 异步执行 pop(device_id)，期间若用户用相同 device_id
        # 创建新设备，cleanup 会误删新设备的 driver 实例，导致新设备无法采集
        old_driver = self._driver_instances.get(device_id)
        # 异步清理采集任务和驱动实例（不阻塞删除响应）
        self._schedule_cleanup(device_id, old_driver)

        return True, None

    def _schedule_cleanup(self, device_id: str, expected_driver: Any = None) -> None:
        """后台异步清理设备的采集任务、驱动实例和sidecar数据，不阻塞删除操作

        FIXED(致命): 增加 expected_driver 参数，cleanup 时校验 _driver_instances[device_id]
        是否仍为同一实例，避免误删删除后新建的同 device_id 设备的 driver。
        """

        async def _cleanup():
            try:
                try:
                    await asyncio.wait_for(self._scheduler.stop_collect(device_id), timeout=5.0)
                except TimeoutError:
                    logger.warning("stop_collect timeout for %s, cleanup in background", device_id)
                except Exception as e:
                    logger.warning("stop_collect failed for %s: %s", device_id, e)

                # R8-S-05 修复: 删除设备不调用 lifecycle 清理，导致状态残留和 WebSocket 盲区。
                # 先 on_device_offline 发布下线事件（通知前端 WebSocket），再 remove_device 清理状态记录。
                try:
                    await self._lifecycle.on_device_offline(device_id)
                except Exception as e:
                    logger.warning("lifecycle.on_device_offline failed for %s: %s", device_id, e)
                try:
                    await self._lifecycle.remove_device(device_id)
                except Exception as e:
                    logger.warning("lifecycle.remove_device failed for %s: %s", device_id, e)

                # FIXED(致命): 仅当 _driver_instances[device_id] 仍是删除时记录的同一实例才清理
                # 避免误删删除后用相同 device_id 新建的设备 driver
                current_driver = self._driver_instances.get(device_id)
                if current_driver is not None and current_driver is expected_driver:
                    self._driver_instances.pop(device_id, None)
                    if isinstance(current_driver, SimulatorDriver):
                        await current_driver.remove_device(device_id)  # FIXED: 补充 await
                    elif hasattr(current_driver, "stop"):
                        try:
                            await asyncio.wait_for(current_driver.stop(), timeout=5.0)
                        except TimeoutError:
                            logger.warning("Driver stop timeout for %s, forcing removal", device_id)
                        except Exception as e:
                            logger.warning("Driver stop failed %s: %s", device_id, e)
                elif current_driver is not None and current_driver is not expected_driver:
                    logger.info(
                        "Skip driver cleanup for %s: device re-created with new driver instance",
                        device_id,
                    )

                try:
                    await self._repo.cleanup_sidecar_data(device_id)
                except Exception as e:
                    logger.warning("Sidecar cleanup failed for %s (compensation will retry): %s", device_id, e)

                # R11-DRV-02: 清理设备级采集锁，防止设备删除后 _collect_locks 字典残留造成内存泄漏
                self._collect_locks.pop(device_id, None)
            except asyncio.CancelledError:
                logger.debug("Cleanup task cancelled for %s", device_id)
            except Exception as e:
                logger.error("Unexpected error in cleanup task for %s: %s", device_id, e, exc_info=True)

        try:
            task = asyncio.get_running_loop().create_task(_cleanup(), name=f"cleanup-{device_id}")
            self._cleanup_tasks.add(task)

            # R9-S-06修复: done_callback 需检查并记录任务异常，避免异常被静默吞没
            def _on_cleanup_done(t: asyncio.Task, _tasks=self._cleanup_tasks) -> None:
                _tasks.discard(t)
                if t.cancelled():
                    logger.debug("Cleanup task cancelled: %s", t.get_name())
                    return
                exc = t.exception()
                if exc is not None:
                    logger.error(
                        "Cleanup task %s failed with exception: %s",
                        t.get_name(),
                        exc,
                        exc_info=exc,
                    )

            task.add_done_callback(_on_cleanup_done)
        except RuntimeError:
            pass

    async def read_points(self, device_id: str) -> dict[str, Any]:
        """读取设备测点值：优先从调度器缓存获取，缓存无数据时走驱动实时读取"""
        # FIXED-P1: 统一返回类型，缓存路径和驱动路径均归一化为PointValue
        # 1. 优先从调度器缓存获取最近采集值（毫秒级返回）
        if self._scheduler:
            cached = await self._scheduler.get_last_values(device_id)
            if cached:
                # 将缓存中的原始值包装为PointValue，与驱动路径返回类型一致
                from edgelite.drivers.base import PointValue

                now = datetime.now(UTC)
                return {
                    k: v
                    if isinstance(v, PointValue)
                    else PointValue(value=v, quality="good", timestamp=now, source="cache")
                    for k, v in cached.items()
                }

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
            result = await driver.read_points(device_id, point_names)
            # 归一化：将驱动返回中的原始值包装为PointValue
            from edgelite.drivers.base import PointValue

            now = datetime.now(UTC)
            return {
                k: v
                if isinstance(v, PointValue)
                else PointValue(value=v, quality="good", timestamp=now, source="device")
                for k, v in result.items()
            }
        except Exception as e:
            # FIXED: 驱动读取异常时返回空数据而非传播异常，避免前端无限等待
            logger.warning("Driver read points error %s: %s", device_id, e)
            return {}

    async def write_point(self, device_id: str, point: str, value: Any, user: dict | None = None) -> bool:
        """写入设备测点值

        SEC-FIX-V02: 接收 user 信息，在写入前设置驱动层用户角色，使驱动层 RBAC 真正生效
        SEC-FIX-V04: 将操作人传递给驱动审计日志，解决 user 字段恒为空的问题
        FIXED(严重): 使用 contextvars 隔离协程级用户上下文，避免多协程并发写入
        同一设备时 driver._current_write_user 实例属性被互相覆盖
        """
        driver = self._driver_instances.get(device_id)
        if driver is None:
            return False
        caps = getattr(driver, "capabilities", None)
        if caps is not None:
            write_supported = getattr(caps, "write", None)
            if write_supported is False:
                raise ValueError("ERR_DEVICE_CAPABILITY_NOT_SUPPORTED: write")
        # SEC-FIX-V02: 写入前设置用户角色，修复驱动层 _current_user_role 恒为 viewer 的死代码
        if user is not None:
            role = user.get("role", "viewer")
            set_role = getattr(driver, "set_user_role", None)
            if set_role is not None:
                try:
                    await set_role(role) if __import__("inspect").iscoroutinefunction(set_role) else set_role(role)
                except Exception as e:
                    logger.debug("set_user_role failed (non-fatal): %s", e)
            # FIXED(严重): 优先使用 contextvars 隔离协程级用户上下文
            # 保留实例属性作为向后兼容 fallback（旧驱动代码仍读取 _current_write_user）
            username = user.get("username", "")
            _current_write_user_var.set(username)
            driver._current_write_user = username  # type: ignore[attr-defined]
        return await driver.write_point(device_id, point, value)

    async def discover_devices(self, protocol: str, config: dict) -> list[dict]:
        """发现指定协议的设备"""
        driver_class = self._registry.get_driver_class(protocol)
        if driver_class is None:
            raise ValueError(f"Unsupported protocol for discovery: {protocol}")
        driver = driver_class()
        try:
            await driver.start(config)
            if not hasattr(driver, "discover_devices"):
                raise ValueError(f"Driver '{protocol}' does not support device discovery")
            devices = await driver.discover_devices(config)
            return devices
        finally:
            try:
                await driver.stop()
            except Exception as e:
                logger.debug("Driver stop after discovery failed: %s", e)

    async def create_simulator(self, data: dict, created_by: str | None = None) -> dict:
        """创建模拟设备"""
        data["protocol"] = "simulator"
        data.setdefault("config", {"timeout": 5.0})
        return await self.create_device(data, created_by=created_by)

    async def start_collect(self, device_id: str) -> bool:
        """启动设备采集"""
        # FIXED: 使用 per-device 锁串行化 check-then-act，防止并发 start/stop 导致状态不一致
        lock = self._collect_locks.setdefault(device_id, asyncio.Lock())
        async with lock:
            device = await self._repo.get(device_id)
            if device is None:
                raise ValueError(f"Device not found: {device_id}")
            # 状态守卫：已 online 时直接返回，避免重复启动采集
            if device.get("status") == "online":
                logger.debug("Device %s already online, skip start_collect", device_id)
                return True
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
        # FIXED: 使用 per-device 锁串行化 check-then-act，防止并发 start/stop 导致状态不一致
        lock = self._collect_locks.setdefault(device_id, asyncio.Lock())
        async with lock:
            device = await self._repo.get(device_id)
            if device is None:
                raise ValueError(f"Device not found: {device_id}")
            # 状态守卫：已 offline 时直接返回，避免重复停止采集
            if device.get("status") == "offline":
                logger.debug("Device %s already offline, skip stop_collect", device_id)
                return True
            await self._scheduler.stop_collect(device_id)
            await self._lifecycle.on_device_offline(device_id)
            await self._repo.update_status(device_id, "offline")
            logger.info("Device %s collect stopped", device_id)
            return True

    async def get_device_health(self, device_id: str) -> dict | None:
        """返回设备驱动健康统计（用于前端详情页展示）。
        说明：统计来源于驱动基类 DriverPlugin 的 _record_* 计数逻辑，
        只有驱动实现调用了这些方法才会有数据。
        """
        driver = self._driver_instances.get(device_id)
        if driver is None:
            return None

        is_connected: bool | None = None
        if hasattr(driver, "is_device_connected"):
            try:
                is_connected = bool(driver.is_device_connected(device_id))
            except Exception as e:
                # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                logger.warning("获取设备连接状态失败: %s", e)
                is_connected = None

        stats: DriverHealthStats | None = None
        if hasattr(driver, "get_health_stats"):
            try:
                stats = driver.get_health_stats(device_id)
            except Exception as e:
                # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                logger.warning("获取设备健康统计失败: %s", e)
                stats = None

        if stats is None:
            return {
                "device_id": device_id,
                "is_connected": is_connected,
            }

        def _dt(v):
            return v.isoformat() if v is not None else None

        result = {
            "device_id": device_id,
            "is_connected": is_connected,
            "connection_quality_score": getattr(stats, "connection_quality_score", None),
            "consecutive_failures": getattr(stats, "consecutive_failures", None),
            "total_reads": getattr(stats, "total_reads", None),
            "failed_reads": getattr(stats, "failed_reads", None),
            "total_writes": getattr(stats, "total_writes", None),
            "failed_writes": getattr(stats, "failed_writes", None),
            "last_success_read": _dt(getattr(stats, "last_success_read", None)),
            "last_failed_read": _dt(getattr(stats, "last_failed_read", None)),
            "last_offline_at": _dt(getattr(stats, "last_offline_at", None)),
            "total_downtime_seconds": getattr(stats, "total_downtime_seconds", None),
        }
        if hasattr(driver, "get_redundancy_status"):
            try:
                result["redundancy"] = driver.get_redundancy_status(device_id)
                rs = result["redundancy"]
                result["current_ip"] = rs.get("active_host", "")
                result["using_backup"] = rs.get("active_role") == "backup"
            except Exception as e:
                # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                logger.warning("获取冗余状态失败: %s", e)
        if hasattr(driver, "_using_backup"):
            try:
                result["using_backup"] = driver._using_backup
            except Exception as e:
                # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                logger.warning("获取备用链路状态失败: %s", e)
        if hasattr(driver, "_active_ip"):
            try:
                if "current_ip" not in result:
                    result["current_ip"] = driver._active_ip
            except Exception as e:
                # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                logger.warning("获取活跃IP失败: %s", e)
        if hasattr(driver, "_pdu_size"):
            try:
                result["pdu_size"] = driver._pdu_size
            except Exception as e:
                # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                logger.warning("获取PDU大小失败: %s", e)
        if hasattr(driver, "_plc_model"):
            try:
                result["plc_model"] = driver._plc_model
            except Exception as e:
                # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                logger.warning("获取PLC型号失败: %s", e)
        if hasattr(driver, "_auth_locked_until"):
            try:
                remaining = driver._auth_locked_until - time.time()
                result["auth_locked"] = remaining > 0
                result["auth_lock_remaining"] = max(0, int(remaining))
            except Exception as e:
                # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                logger.warning("获取认证锁定状态失败: %s", e)
        return result

    async def reset_device_health(self, device_id: str) -> bool:
        driver = self._driver_instances.get(device_id)
        if driver is None:
            return False
        if not hasattr(driver, "reset_health_stats"):
            return False
        try:
            driver.reset_health_stats(device_id)
            return True
        except Exception as e:
            # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
            logger.warning("重置设备健康统计失败: %s", e)
            return False

    async def probe_primary_link(self, device_id: str) -> bool:
        driver = self._driver_instances.get(device_id)
        if driver is None:
            return False
        if not hasattr(driver, "probe_primary_link"):
            return False
        try:
            return await driver.probe_primary_link(device_id)
        except Exception as e:
            # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
            logger.warning("探测主链路失败: %s", e)
            return False

    async def get_device_ops_data(self, device_id: str) -> dict | None:
        driver = self._driver_instances.get(device_id)
        if driver is None:
            return None
        result: dict = {"device_id": device_id}
        if hasattr(driver, "get_health_stats"):
            try:
                stats = driver.get_health_stats(device_id)
                if stats:
                    # FIX-P1: 原代码 elif 条件与 if 完全相同(hasattr 检测)，
                    # elif 分支为死代码永远不会执行。改为根据返回值类型分发：
                    # dict 用 .get() 访问，对象/dataclass 用 getattr() 访问。
                    if isinstance(stats, dict):
                        result["total_reads"] = stats.get("total_reads", 0)
                        result["failed_reads"] = stats.get("failed_reads", 0)
                        result["total_writes"] = stats.get("total_writes", 0)
                        result["failed_writes"] = stats.get("failed_writes", 0)
                        result["total_reconnects"] = stats.get("total_reconnects", 0)
                        result["avg_latency_ms"] = stats.get("avg_latency_ms", 0.0)
                        result["online_rate"] = stats.get("online_rate", 1.0)
                        result["state"] = stats.get("state", "disconnected")
                        result["current_broker"] = stats.get("current_broker", "")
                        result["qos_current"] = stats.get("qos_current", 0)
                        result["tls_status"] = stats.get("tls_status", "disabled")
                    else:
                        # 非 dict 返回值（如 dataclass/对象），按属性访问
                        result["total_reads"] = getattr(stats, "total_reads", 0)
                        result["failed_reads"] = getattr(stats, "failed_reads", 0)
                        result["total_writes"] = getattr(stats, "total_writes", 0)
                        result["failed_writes"] = getattr(stats, "failed_writes", 0)
                        result["total_reconnects"] = getattr(stats, "total_reconnects", 0)
                        result["avg_latency_ms"] = getattr(stats, "avg_latency_ms", 0.0)
            except Exception as e:
                # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                logger.warning("获取设备运行统计失败: %s", e)
        if hasattr(driver, "get_connection_status"):
            try:
                status = driver.get_connection_status(device_id)
                if status:
                    result["state"] = getattr(status, "state", "disconnected")
                    result["state_reason"] = getattr(status, "reason", "")
            except Exception as e:
                # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                logger.warning("获取连接状态失败: %s", e)
        if hasattr(driver, "is_device_connected"):
            try:
                result["is_connected"] = bool(driver.is_device_connected(device_id))
            except Exception as conn_err:
                logger.debug("is_device_connected failed for %s: %s", device_id, conn_err)
                result["is_connected"] = False
        if hasattr(driver, "_port_available"):
            port_path = ""
            if hasattr(driver, "_device_port_map"):
                port_path = driver._device_port_map.get(device_id, "")
            if port_path:
                result["port_status"] = "available" if driver._port_available.get(port_path, True) else "disconnected"
                result["port_path"] = port_path
        if hasattr(driver, "get_polling_interval"):
            try:
                result["polling_interval"] = driver.get_polling_interval(device_id)
            except Exception as e:
                # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                logger.warning("获取轮询间隔失败: %s", e)
        if hasattr(driver, "_degrade_level"):
            result["degrade_level"] = driver._degrade_level.get(device_id, 0)
        online_rate = 1.0
        total_reads = result.get("total_reads", 0) or 0
        failed_reads = result.get("failed_reads", 0) or 0
        if total_reads > 0:
            online_rate = (total_reads - failed_reads) / total_reads
        result["online_rate"] = round(online_rate, 4)
        if hasattr(driver, "get_latency_history"):
            try:
                result["latency_history"] = driver.get_latency_history(device_id)
            except Exception as e:
                # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                logger.warning("获取延迟历史失败: %s", e)
                result["latency_history"] = []
        if hasattr(driver, "get_reconnect_history"):
            try:
                result["reconnect_history"] = driver.get_reconnect_history(device_id)
            except Exception as e:
                # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                logger.warning("获取重连历史失败: %s", e)
                result["reconnect_history"] = []
        if hasattr(driver, "get_quality_stream"):
            try:
                result["quality_stream"] = driver.get_quality_stream(device_id, limit=100)
            except Exception as e:
                # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                logger.warning("获取质量流失败: %s", e)
                result["quality_stream"] = []
        return result

    async def get_point_health(self, device_id: str) -> list[dict] | None:
        driver = self._driver_instances.get(device_id)
        if driver is None:
            return None
        if not hasattr(driver, "get_point_stats"):
            return None
        device_points = []
        if hasattr(driver, "_device_points"):
            device_points = driver._device_points.get(device_id, [])
        if not device_points:
            return []
        # R9-S-04 修复: 使用 asyncio.gather 并行获取所有测点健康统计，
        # Semaphore 限制并发数避免测点过多时压力过大
        sem = asyncio.Semaphore(20)

        async def _get_one_point_stats(pt: dict) -> dict:
            name = pt.get("name", "")
            async with sem:
                try:
                    # get_point_stats 为同步方法，使用 to_thread 并行执行避免阻塞事件循环
                    stats = await asyncio.to_thread(driver.get_point_stats, device_id, name)
                except Exception as e:
                    logger.warning("获取测点健康统计失败 %s/%s: %s", device_id, name, e)
                    stats = None
            if stats:
                return {"point_name": name, **stats}
            else:
                return {
                    "point_name": name,
                    "success_count": 0,
                    "fail_count": 0,
                    "avg_latency_ms": 0,
                    "consecutive_fails": 0,
                    "success_rate": 1.0,
                    "quality_history": [],
                    "current_quality": "good",
                    "last_success_at": None,
                }

        result = await asyncio.gather(*[_get_one_point_stats(pt) for pt in device_points])
        return list(result)

    async def get_write_audit(
        self,
        device_id: str,
        limit: int = 100,
        result: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> list[dict] | None:
        driver = self._driver_instances.get(device_id)
        if driver is None:
            return None
        if not hasattr(driver, "get_write_audit_log"):
            return None
        logs = driver.get_write_audit_log(device_id, limit=1000)
        if result is not None:
            logs = [e for e in logs if e.get("result") == result]
        if start_time is not None:
            logs = [e for e in logs if e.get("timestamp", "") >= start_time]
        if end_time is not None:
            logs = [e for e in logs if e.get("timestamp", "") <= end_time]
        return logs[-limit:]

    async def save_config_version(
        self,
        device_id: str,
        config: dict,
        change_summary: str = "",
        operator: str = "",
    ) -> int:
        driver = self._driver_instances.get(device_id)
        if driver is None or not hasattr(driver, "save_config_version"):
            return 0
        return driver.save_config_version(device_id, config, change_summary, operator)

    async def get_config_current(self, device_id: str) -> dict | None:
        driver = self._driver_instances.get(device_id)
        if driver is None or not hasattr(driver, "get_config_current"):
            return None
        return driver.get_config_current(device_id)

    async def get_config_versions(self, device_id: str) -> list[dict]:
        driver = self._driver_instances.get(device_id)
        if driver is None or not hasattr(driver, "get_config_versions"):
            return []
        return driver.get_config_versions(device_id)

    async def get_config_version_config(self, device_id: str, version: int) -> dict | None:
        driver = self._driver_instances.get(device_id)
        if driver is None or not hasattr(driver, "get_config_version_config"):
            return None
        return driver.get_config_version_config(device_id, version)

    async def rollback_config(self, device_id: str, target_version: int, operator: str = "") -> dict | None:
        driver = self._driver_instances.get(device_id)
        if driver is None or not hasattr(driver, "rollback_config"):
            return None

        # SEC-FIX: 回滚前对比目标版本与当前版本的点位不可变字段（address/data_type/access_mode）
        # 回滚是管理员操作，允许执行但记录审计日志，标注哪些字段发生变更
        current_points: list = []
        target_points: list = []
        try:
            current_cfg = await self.get_config_current(device_id)
            if isinstance(current_cfg, dict):
                current_points = current_cfg.get("points", []) or []
        except Exception as e:
            logger.warning("rollback_config: 获取当前配置失败 %s: %s", device_id, e)
        try:
            target_cfg = await self.get_config_version_config(device_id, target_version)
            if isinstance(target_cfg, dict):
                target_points = target_cfg.get("points", []) or []
        except Exception as e:
            logger.warning("rollback_config: 获取目标版本配置失败 %s v%s: %s", device_id, target_version, e)

        immutable_changes: list[str] = []
        if isinstance(current_points, list) and isinstance(target_points, list):
            cur_map = {
                (p.get("name") if isinstance(p, dict) else None): p for p in current_points if isinstance(p, dict)
            }
            for new_pt in target_points:
                if not isinstance(new_pt, dict):
                    continue
                pt_name = new_pt.get("name")
                if not pt_name:
                    continue
                old_pt = cur_map.get(pt_name)
                if old_pt is None:
                    continue
                for sensitive_field in ("address", "data_type", "access_mode"):
                    old_val = old_pt.get(sensitive_field)
                    new_val = new_pt.get(sensitive_field)
                    if old_val != new_val:
                        immutable_changes.append(f"point {pt_name}.{sensitive_field}: {old_val!r} -> {new_val!r}")
        if immutable_changes:
            logger.warning(
                "rollback_config: %s 回滚到 v%s 将修改点位不可变字段: %s (operator=%s)",
                device_id,
                target_version,
                immutable_changes,
                operator or "system",
            )

        result = driver.rollback_config(device_id, target_version, operator)
        if result and hasattr(driver, "_config"):
            driver._config.update(result.get("config", {}))

        # SEC-FIX: 回滚后记录审计日志（管理员操作允许，但需审计）
        try:
            from edgelite.app import _app_state

            audit_service = getattr(_app_state, "audit_service", None)
            if audit_service is not None:
                from edgelite.services.audit_service import AuditAction

                await audit_service.log(
                    action=AuditAction.CONFIG_VERSION_ROLLBACK,
                    user_id=None,
                    username=operator or "system",
                    resource_type="device",
                    resource_id=device_id,
                    details={
                        "target_version": target_version,
                        "immutable_field_changes": immutable_changes,
                        "new_version": result.get("version") if result else None,
                    },
                    status="success",
                )
        except Exception as e:
            logger.warning("rollback_config: 记录审计日志失败 %s: %s", device_id, e)

        return result

    async def get_config_audit_trail(self, device_id: str, limit: int = 50) -> list[dict]:
        driver = self._driver_instances.get(device_id)
        if driver is None or not hasattr(driver, "get_config_audit_trail"):
            return []
        return driver.get_config_audit_trail(device_id, limit)

    async def diff_config_versions(self, device_id: str, version_a: int, version_b: int) -> dict | None:
        driver = self._driver_instances.get(device_id)
        if driver is None or not hasattr(driver, "diff_config_versions"):
            return None
        return driver.diff_config_versions(device_id, version_a, version_b)

    async def list_device_health(self) -> list[dict]:
        """返回所有设备的驱动健康统计摘要（用于列表页全量请求）"""
        result: list[dict] = []
        # FIXED(严重): 原问题-直接迭代 self._driver_instances.items()，与 create_device/
        # delete_device 中的有锁写入构成竞态，并发增删设备会触发
        # "RuntimeError: dictionary changed size during iteration"。
        # 修复：取快照后再迭代。
        for device_id, driver in list(self._driver_instances.items()):
            is_connected: bool | None = None
            if hasattr(driver, "is_device_connected"):
                try:
                    is_connected = bool(driver.is_device_connected(device_id))
                except Exception as e:
                    logger.warning("获取设备连接状态失败 %s: %s", device_id, e)
                    is_connected = None

            stats: DriverHealthStats | None = None
            if hasattr(driver, "get_health_stats"):
                try:
                    stats = driver.get_health_stats(device_id)
                except Exception as e:
                    logger.warning("获取设备健康统计失败 %s: %s", device_id, e)
                    stats = None

            def _dt(v):
                return v.isoformat() if v is not None else None

            entry = {
                "device_id": device_id,
                "is_connected": is_connected,
                "connection_quality_score": getattr(stats, "connection_quality_score", None) if stats else None,
                "consecutive_failures": getattr(stats, "consecutive_failures", None) if stats else None,
                "total_reads": getattr(stats, "total_reads", None) if stats else None,
                "failed_reads": getattr(stats, "failed_reads", None) if stats else None,
                "total_writes": getattr(stats, "total_writes", None) if stats else None,
                "failed_writes": getattr(stats, "failed_writes", None) if stats else None,
                "last_success_read": _dt(getattr(stats, "last_success_read", None)) if stats else None,
            }
            result.append(entry)
        return result

    async def list_device_health_paginated(
        self,
        page: int = 1,
        size: int = 100,
        device_ids: set[str] | None = None,
    ) -> tuple[list[dict], int]:
        """返回设备健康统计摘要（分页）。

        FIXED: 原问题-list_device_health 一次性返回所有设备健康状态，设备数量大时
        响应慢、内存高；修复-在 service 层按 device_ids 过滤后分页返回。

        device_ids 为 None 表示不过滤（admin），为集合时仅返回其中的设备。
        健康数据来自内存中的驱动实例，无法避免全量构建，但分页可显著减小响应体。
        """
        all_health = await self.list_device_health()
        if device_ids is not None:
            all_health = [h for h in all_health if h.get("device_id") in device_ids]
        total = len(all_health)
        start = (page - 1) * size
        end = start + size
        return all_health[start:end], total

    async def list_device_health_for_ids(self, device_ids: list[str]) -> dict[str, dict]:
        """按 device_id 列表返回驱动健康统计摘要（用于列表分页按需请求）"""
        if not device_ids:
            return {}

        # R9-S-03 修复: 使用 asyncio.gather 并行执行所有设备的健康检查，
        # Semaphore(20) 限制并发数防止过大并发压力
        sem = asyncio.Semaphore(20)

        async def _get_one_device_health(device_id: str) -> tuple[str, dict | None]:
            """获取单个设备的健康统计，返回 (device_id, health_dict or None)"""
            driver = self._driver_instances.get(device_id)
            if driver is None:
                return device_id, None

            is_connected: bool | None = None
            if hasattr(driver, "is_device_connected"):
                try:
                    # 同步方法，使用 to_thread 并行执行避免阻塞事件循环
                    is_connected = bool(await asyncio.to_thread(driver.is_device_connected, device_id))
                except Exception as e:
                    # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                    logger.warning("获取设备连接状态失败 %s: %s", device_id, e)
                    is_connected = None

            stats: DriverHealthStats | None = None
            if hasattr(driver, "get_health_stats"):
                try:
                    stats = await asyncio.to_thread(driver.get_health_stats, device_id)
                except Exception as e:
                    # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                    logger.warning("获取设备健康统计失败 %s: %s", device_id, e)
                    stats = None

            def _dt(v):
                return v.isoformat() if v is not None else None

            entry = {
                "is_connected": is_connected,
                "connection_quality_score": (getattr(stats, "connection_quality_score", None) if stats else None),
                "consecutive_failures": (getattr(stats, "consecutive_failures", None) if stats else None),
                "total_reads": (getattr(stats, "total_reads", None) if stats else None),
                "failed_reads": (getattr(stats, "failed_reads", None) if stats else None),
                "total_writes": (getattr(stats, "total_writes", None) if stats else None),
                "failed_writes": (getattr(stats, "failed_writes", None) if stats else None),
                "last_success_read": _dt(getattr(stats, "last_success_read", None) if stats else None),
            }
            return device_id, entry

        async def _guarded_get(device_id: str) -> tuple[str, dict | None]:
            # 单个设备失败不阻塞其他设备
            async with sem:
                try:
                    return await _get_one_device_health(device_id)
                except Exception as e:
                    logger.warning("获取设备健康统计异常 %s: %s", device_id, e)
                    return device_id, None

        pairs = await asyncio.gather(*[_guarded_get(did) for did in device_ids])
        result: dict[str, dict] = {}
        for device_id, entry in pairs:
            if entry is not None:
                result[device_id] = entry
        return result

    async def batch_delete_devices(
        self,
        device_ids: list[str],
        user_id: str | None = None,
        is_admin: bool = False,
    ) -> dict:
        """Batch delete devices with atomic ownership check.

        When user_id is provided, each device is deleted via a conditional SQL
        statement that atomically verifies ownership (or shared access) before
        deleting, eliminating TOCTOU race conditions.

        Returns {device_id: (success, error_msg)}
        """
        # R9-S-06 修复: 使用 asyncio.gather 并行执行批量删除，
        # Semaphore 限制并发数，单个失败不阻塞其他设备
        sem = asyncio.Semaphore(20)

        async def _delete_one(device_id: str) -> tuple[str, tuple[bool, str | None]]:
            async with sem:
                try:
                    if user_id is not None:
                        success, error = await self._delete_device_with_owner_check(device_id, user_id, is_admin)
                    else:
                        success, error = await self.delete_device(device_id)
                    return device_id, (success, error)
                except Exception as e:
                    return device_id, (False, str(e))

        pairs = await asyncio.gather(*[_delete_one(did) for did in device_ids])
        results: dict[str, tuple[bool, str | None]] = dict(pairs)
        return results

    async def _delete_device_with_owner_check(
        self, device_id: str, user_id: str, is_admin: bool
    ) -> tuple[bool, str | None]:
        """Delete a single device with atomic ownership verification.

        Flow:
        1. Check rule associations (no side effects)
        2. Atomic conditional DELETE in DB (ownership check + delete in one SQL)
        3. If DB delete succeeded, clean up in-memory state asynchronously

        FIXED: 不再使用 self._lock，避免因其他操作持锁导致删除超时。
        DB 层的 delete_with_owner_check 已有原子性保证，不需要应用层互斥锁。
        """
        rules, _, *_ = await self._rule_repo.list_all(device_id=device_id)
        active_rules = [r for r in rules if r.get("enabled", False)]
        if active_rules:
            rule_names = ", ".join(r["name"] for r in active_rules[:3])
            return False, f"Device referenced by rules: {rule_names}"

        delete_result = await self._repo.delete_with_owner_check(device_id, user_id, is_admin)

        if delete_result == "not_authorized":
            return False, "Not authorized to delete this device"
        if delete_result == "not_found":
            return False, None

        # FIXED: 清理操作改为异步执行，不阻塞删除响应
        # 先删DB再异步清理，确保用户不会因清理超时而等待
        # FIXED(致命): 传入 expected_driver 防止 cleanup 误删删除后新建的同 device_id 设备的 driver
        old_driver = self._driver_instances.get(device_id)
        self._schedule_cleanup(device_id, old_driver)

        return True, None

    async def batch_start_collect(self, device_ids: list[str]) -> dict:
        """Batch start collect, returns {device_id: (success, error_msg)}"""
        # R9-S-06 修复: 使用 asyncio.gather 并行执行批量启动采集
        sem = asyncio.Semaphore(20)

        async def _start_one(device_id: str) -> tuple[str, tuple[bool, str | None]]:
            async with sem:
                try:
                    await self.start_collect(device_id)
                    return device_id, (True, None)
                except Exception as e:
                    return device_id, (False, str(e))

        pairs = await asyncio.gather(*[_start_one(did) for did in device_ids])
        results: dict[str, tuple[bool, str | None]] = dict(pairs)
        return results

    async def batch_stop_collect(self, device_ids: list[str]) -> dict:
        """Batch stop collect, returns {device_id: (success, error_msg)}"""
        # R9-S-06 修复: 使用 asyncio.gather 并行执行批量停止采集
        sem = asyncio.Semaphore(20)

        async def _stop_one(device_id: str) -> tuple[str, tuple[bool, str | None]]:
            async with sem:
                try:
                    await self.stop_collect(device_id)
                    return device_id, (True, None)
                except Exception as e:
                    return device_id, (False, str(e))

        pairs = await asyncio.gather(*[_stop_one(did) for did in device_ids])
        results: dict[str, tuple[bool, str | None]] = dict(pairs)
        return results

    async def load_existing_devices(self) -> None:
        """启动时加载所有已有设备并恢复采集"""
        page = 1
        size = _MAX_QUERY_SIZE  # FIXED: 原问题-size=1000魔法数字
        total = 0
        # R9-S-13 修复: 使用 asyncio.gather 并行启动设备驱动，
        # Semaphore(10) 限制并发数，单个失败不影响其他设备启动
        sem = asyncio.Semaphore(10)

        async def _start_one_device(device: dict) -> None:
            """启动单个设备的驱动和采集，失败仅记录日志不抛出"""
            async with sem:
                try:
                    protocol = device.get("protocol")  # FIXED: 原问题-device["protocol"]硬索引
                    if protocol is None:
                        return
                    driver_class = self._registry.get_driver_class(protocol)
                    if driver_class is None:
                        return

                    if protocol == "simulator":
                        driver = await self._get_simulator_driver()
                        await driver.add_device(device["device_id"], device.get("points", []))
                        self._driver_instances[device["device_id"]] = driver
                        await self._scheduler.start_collect(
                            device["device_id"],
                            driver,
                            device.get("points", []),
                            device.get("collect_interval", 5),
                        )
                    elif driver_class is not None:
                        driver = driver_class()
                        # R8-S-01 修复: 与 create_device 保持一致，driver.start() 加 30s 超时保护，
                        # 避免单个设备驱动启动无限阻塞导致其余设备无法恢复采集。
                        try:
                            await asyncio.wait_for(driver.start(device.get("config", {})), timeout=30.0)
                        except TimeoutError:
                            logger.warning(
                                "Driver start timed out for device %s, skipping",
                                device.get("device_id"),
                            )
                            return
                        with contextlib.suppress(NotImplementedError):
                            await driver.add_device(
                                device["device_id"],
                                device.get("config", {}),
                                device.get("points", []),
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
                            device.get("points", []),
                            device.get("collect_interval", 5),
                        )
                except Exception as e:
                    logger.warning(
                        "Failed to restore device collection %s: %s",
                        device.get("device_id", "<unknown>"),
                        e,
                    )

        while True:
            devices, total, *_ = await self._repo.list_all(page=page, size=size)
            # 并行启动当前页所有设备
            await asyncio.gather(*[_start_one_device(device) for device in devices])
            if page * size >= total:
                break
            page += 1
        logger.info("Loaded %d devices", total)
        # FIXED-P0: 启动跨库级联清理补偿后台任务
        self._start_sidecar_compensation()

    def _start_sidecar_compensation(self) -> None:
        """FIXED-P0: 启动sidecar清理补偿后台任务，定期重试失败的跨库清理"""
        if self._sidecar_compensation_task is not None:
            return
        self._sidecar_compensation_task = asyncio.create_task(
            self._run_sidecar_compensation(), name="sidecar-cleanup-compensation"
        )
        logger.info("Sidecar清理补偿任务已启动")

    async def stop_sidecar_compensation(self) -> None:
        """FIXED-P0: 停止sidecar清理补偿后台任务"""
        if self._sidecar_compensation_task is not None:
            self._sidecar_compensation_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sidecar_compensation_task
            self._sidecar_compensation_task = None
            logger.info("Sidecar清理补偿任务已停止")

    async def _run_sidecar_compensation(self) -> None:
        """FIXED-P0: 后台补偿任务循环——定期扫描并重试失败的sidecar清理

        扫描间隔30秒，每条记录的重试间隔由指数退避控制（初始60s，上限600s）。
        """
        while True:
            try:
                await asyncio.sleep(30)
                await self._repo.retry_pending_sidecar_cleanups()
            except asyncio.CancelledError:
                logger.debug("Sidecar清理补偿任务已取消")
                raise
            except Exception as e:
                logger.error("Sidecar清理补偿任务异常: %s", e, exc_info=True)
                await asyncio.sleep(60)  # 异常后等待60秒再重试

    # ------------------------------------------------------------------
    # 设备模板管理
    # ------------------------------------------------------------------

    async def create_template(self, device_id: str, template_name: str, created_by: str | None = None) -> dict:
        """从现有设备创建模板（提取 protocol、config、points 结构）"""
        if self._template_repo is None:
            raise ValueError("Template repository not available")

        device = await self._repo.get(device_id)
        if device is None:
            raise ValueError(f"Device not found: {device_id}")

        template_data = {
            "name": template_name,
            "protocol": device["protocol"],
            "config_template": device.get("config", {}),
            "point_templates": device.get("points", []),
        }
        return await self._template_repo.create(template_data, created_by=created_by)

    async def list_templates(self, created_by: str | None = None) -> list[dict]:
        """列出所有模板"""
        if self._template_repo is None:
            raise ValueError("Template repository not available")
        items, _total = await self._template_repo.list_all(created_by=created_by)
        return items

    async def create_from_template(self, template_name: str, overrides: dict, created_by: str | None = None) -> dict:
        """从模板创建设备（可覆盖名称、IP等）"""
        if self._template_repo is None:
            raise ValueError("Template repository not available")

        template = await self._template_repo.get(template_name)
        if template is None:
            raise ValueError(f"Template not found: {template_name}")

        device_data = {
            "device_id": overrides["device_id"],
            "name": overrides["name"],
            "protocol": template["protocol"],
            "config": overrides.get("config") or template.get("config_template", {}),
            "points": template.get("point_templates", []),
            "collect_interval": overrides.get("collect_interval", 5),
        }
        # FIXED-Bug23: 传递 created_by，避免 TypeError 和非管理员看不到自己创建的设备
        return await self.create_device(device_data, created_by=created_by)

    async def delete_template(self, template_name: str) -> bool:
        """删除模板"""
        if self._template_repo is None:
            raise ValueError("Template repository not available")
        return await self._template_repo.delete(template_name)

    # ------------------------------------------------------------------
    # 批量导入导出
    # ------------------------------------------------------------------

    async def export_devices(self, device_ids: list[str] | None = None) -> list[dict]:
        """导出设备配置为字典列表（device_ids为空则导出全部）

        R9-S-07 修复: 原实现返回 json.dumps 字符串，API 层再 json.loads 解析，
        造成不必要的序列化/反序列化往返。改为直接返回 list[dict]。
        """
        if device_ids:
            # FIXED-P1: 原问题-循环调用get导致N+1查询，设备数量多时性能严重下降；改为批量查询
            devices = await self._repo.get_by_ids(device_ids)
        else:
            page = 1
            size = _MAX_QUERY_SIZE
            devices = []
            while True:
                batch, total, *_ = await self._repo.list_all(page=page, size=size)
                devices.extend(batch)
                if page * size >= total:
                    break
                page += 1

        export_data = []
        for device in devices:
            export_data.append(
                {
                    "device_id": device["device_id"],
                    "name": device["name"],
                    "protocol": device["protocol"],
                    "config": device.get("config", {}),
                    "points": device.get("points", []),
                    "collect_interval": device.get("collect_interval", 5),
                }
            )

        return export_data

    async def import_devices(
        self, devices_data: list[dict], overwrite: bool = False, atomic: bool = False, created_by: str | None = None
    ) -> dict:
        """从设备字典列表导入设备

        R9-S-07 修复: 原实现接受 JSON 字符串并 json.loads 解析，API 层需先 json.dumps，
        造成不必要的序列化/反序列化往返。改为直接接受 list[dict]。

        Args:
            devices_data: 设备数据列表（字典列表）
            overwrite: 是否覆盖已存在的设备
            atomic: 是否使用事务模式（全有或全无）
            created_by: 创建者用户ID

        Returns:
            dict with success, failed, errors, and mode fields
        """
        if not isinstance(devices_data, list):
            return {
                "success": 0,
                "failed": 0,
                "errors": ["Expected a list of devices"],
                "mode": "partial" if not atomic else "atomic",
            }

        supported_protocols = set(self._registry.get_all_protocol_keys())
        supported_protocols.update({"simulator", "video", "modbus_rtu"})

        # FIXED-ATOMIC-IMPORT: 原子模式使用单一事务
        if atomic:
            # FIXED-Bug24: 传递 created_by，避免非管理员导入后看不到自己的设备
            return await self._import_devices_atomic(
                devices_data, overwrite, supported_protocols, created_by=created_by
            )

        # 部分成功模式（原逻辑）
        success = 0
        failed = 0
        errors: list[str] = []

        for idx, item in enumerate(devices_data):
            device_id = item.get("device_id", f"<index {idx}>")
            try:
                protocol = item.get("protocol")
                if not protocol:
                    errors.append(f"{device_id}: Missing required field 'protocol'")
                    failed += 1
                    continue

                if protocol not in supported_protocols:
                    errors.append(f"{device_id}: Unsupported protocol '{protocol}'")
                    failed += 1
                    continue

                if not item.get("name"):
                    errors.append(f"{device_id}: Missing required field 'name'")
                    failed += 1
                    continue

                if not item.get("points"):
                    errors.append(f"{device_id}: Missing required field 'points'")
                    failed += 1
                    continue

                existing = await self._repo.get(item["device_id"])
                if existing is not None:
                    if overwrite:
                        update_data = {}
                        if "name" in item:
                            update_data["name"] = item["name"]
                        if "config" in item:
                            update_data["config"] = item["config"]
                        if "points" in item:
                            update_data["points"] = item["points"]
                        if "collect_interval" in item:
                            update_data["collect_interval"] = item["collect_interval"]
                        result = await self.update_device(item["device_id"], update_data)
                        if result is None:
                            errors.append(f"{device_id}: Update failed")
                            failed += 1
                        else:
                            success += 1
                    else:
                        errors.append(f"{device_id}: Device already exists (use overwrite=true to update)")
                        failed += 1
                    continue

                device_data = {
                    "device_id": item["device_id"],
                    "name": item["name"],
                    "protocol": protocol,
                    "config": item.get("config", {}),
                    "points": item.get("points", []),
                    "collect_interval": item.get("collect_interval", 5),
                }
                await self.create_device(device_data, created_by=created_by)
                success += 1
            except Exception as e:
                errors.append(f"{device_id}: {e}")
                failed += 1

        return {"success": success, "failed": failed, "errors": errors, "mode": "partial"}

    async def _import_devices_atomic(
        self,
        devices_data: list,
        overwrite: bool,
        supported_protocols: set,
        created_by: str | None = None,
    ) -> dict:
        """原子性批量导入设备（全有或全无）

        任一设备失败，整个事务回滚。
        """
        from edgelite.app import _app_state

        errors: list[str] = []
        # FIXED-Bug28: 移除无效果的 len(devices_data) 语句

        try:
            # 获取数据库 session 进行原子操作
            db = getattr(_app_state, "database", None)
            if not db:
                return {
                    "success": 0,
                    "failed": len(devices_data),
                    "errors": ["Database not available"],
                    "mode": "atomic",
                }

            async with db.session() as session:
                try:
                    # FIXED(严重): 原问题-循环内逐条 SELECT 检查设备是否存在导致 N+1 查询
                    # 修复：导入前先一次性查询所有已存在的 device_id 集合，循环内改为内存查找
                    from sqlalchemy import select

                    from edgelite.models.db import DeviceORM

                    _import_device_ids = [item.get("device_id") for item in devices_data if item.get("device_id")]
                    existing_devices_map: dict[str, DeviceORM] = {}
                    if _import_device_ids:
                        existing_result = await session.execute(
                            select(DeviceORM).where(DeviceORM.device_id.in_(_import_device_ids))
                        )
                        for row in existing_result.scalars().all():
                            existing_devices_map[row.device_id] = row

                    for idx, item in enumerate(devices_data):
                        device_id = item.get("device_id", f"<index {idx}>")

                        # 验证协议
                        protocol = item.get("protocol")
                        if not protocol:
                            errors.append(f"{device_id}: Missing required field 'protocol'")
                            continue
                        if protocol not in supported_protocols:
                            errors.append(f"{device_id}: Unsupported protocol '{protocol}'")
                            continue

                        # 验证必填字段
                        if not item.get("name"):
                            errors.append(f"{device_id}: Missing required field 'name'")
                            continue
                        if not item.get("points"):
                            errors.append(f"{device_id}: Missing required field 'points'")
                            continue

                        # FIXED-Bug25: 复用 _validate_device_data 进行完整业务验证
                        # 与 DeviceRepo.create 保持一致，防止非法配置（如端口越界、缺 serial_port）入库
                        try:
                            from edgelite.storage.sqlite_repo import _validate_device_data

                            _validate_device_data(item)
                        except ValueError as ve:
                            errors.append(f"{device_id}: {ve}")
                            continue

                        # 检查设备是否存在（从预加载的 map 中查找，避免 N+1 查询）
                        existing = existing_devices_map.get(device_id)

                        if existing is not None:
                            if not overwrite:
                                errors.append(f"{device_id}: Device already exists (use overwrite=true to update)")
                                continue
                            # SEC-FIX: overwrite 时校验点位不可变字段（address/data_type/access_mode）
                            # 若已存在点位的不可变字段被修改，跳过该设备导入并记录错误，
                            # 防止通过 import+overwrite 绕过 update_device 的不可变校验
                            try:
                                existing_points = json.loads(existing.points) if existing.points else []
                            except (TypeError, ValueError):
                                existing_points = []
                            new_points = item.get("points", []) or []
                            existing_pt_map = {
                                (p.get("name") if isinstance(p, dict) else None): p
                                for p in existing_points
                                if isinstance(p, dict)
                            }
                            immutable_violations: list[str] = []
                            for new_pt in new_points:
                                if not isinstance(new_pt, dict):
                                    continue
                                pt_name = new_pt.get("name")
                                if not pt_name:
                                    continue
                                old_pt = existing_pt_map.get(pt_name)
                                if old_pt is None:
                                    continue
                                for sensitive_field in ("address", "data_type", "access_mode"):
                                    old_val = old_pt.get(sensitive_field)
                                    new_val = new_pt.get(sensitive_field)
                                    if old_val != new_val:
                                        immutable_violations.append(
                                            f"point {pt_name}.{sensitive_field}: {old_val!r} -> {new_val!r}"
                                        )
                            if immutable_violations:
                                errors.append(
                                    f"{device_id}: immutable point fields changed "
                                    f"({'; '.join(immutable_violations)}); "
                                    f"skip import to preserve immutability"
                                )
                                logger.warning(
                                    "Import overwrite blocked for %s: immutable fields changed: %s",
                                    device_id,
                                    immutable_violations,
                                )
                                continue
                            # 更新现有设备
                            existing.name = item["name"]
                            existing.config = json.dumps(item.get("config", {}), ensure_ascii=False)
                            existing.points = json.dumps(item.get("points", []), ensure_ascii=False)
                            if "collect_interval" in item:
                                existing.collect_interval = item["collect_interval"]
                            existing.updated_at = _now()
                            existing.version = (existing.version or 0) + 1
                        else:
                            # 创建新设备
                            from datetime import UTC

                            now = datetime.now(UTC)
                            orm = DeviceORM(
                                device_id=item["device_id"],
                                name=item["name"],
                                protocol=protocol,
                                status="offline",
                                config=json.dumps(item.get("config", {}), ensure_ascii=False),
                                points=json.dumps(item.get("points", []), ensure_ascii=False),
                                collect_interval=item.get("collect_interval", 5),
                                # FIXED-Bug24: 原子模式设置 created_by，避免非管理员看不到导入的设备
                                created_by=created_by,
                                created_at=now,
                                updated_at=now,
                            )
                            session.add(orm)

                    # 如果有任何错误，回滚并返回
                    if errors:
                        await session.rollback()
                        return {
                            "success": 0,
                            "failed": len(devices_data),
                            "errors": errors,
                            "mode": "atomic",
                        }

                    # 全部成功，提交事务
                    await session.commit()

                    # FIXED-Bug26: 事务提交后加载驱动并启动采集
                    # 驱动加载失败不回滚事务（设备已入库），记录错误供后续重试
                    driver_errors: list[str] = []
                    for item in devices_data:
                        dev_id = item.get("device_id", "")
                        try:
                            device = await self._repo.get(dev_id)
                            if device is None:
                                continue
                            await self._load_driver_for_device(device)
                        except Exception as de:
                            driver_errors.append(f"{dev_id}: driver load failed: {de}")
                            logger.warning("Driver load failed for imported device %s: %s", dev_id, de)

                    return {
                        "success": len(devices_data),
                        "failed": 0,
                        "errors": driver_errors,
                        "mode": "atomic",
                    }

                except Exception as e:
                    await session.rollback()
                    errors.append(f"Transaction failed: {e}")
                    return {
                        "success": 0,
                        "failed": len(devices_data),
                        "errors": errors,
                        "mode": "atomic",
                    }

        except Exception as e:
            logger.error("Atomic import failed: %s", e)
            return {
                "success": 0,
                "failed": len(devices_data),
                "errors": [f"Atomic import failed: {e}"],
                "mode": "atomic",
            }

    async def _load_driver_for_device(self, device: dict) -> None:
        """为已存在的设备记录加载驱动实例并启动采集。

        FIXED-Bug26: 原子导入后设备无驱动实例，无法采集。
        提取自 _create_device_unlocked 的驱动加载逻辑，用于导入后补加载。
        驱动加载失败由调用方处理（记录错误，不回滚事务）。
        """
        import contextlib

        protocol = device.get("protocol")
        if protocol is None:
            return

        # R8-S-04 修复: 原代码直接写 self._driver_instances[device_id] 未获取 self._lock，
        # 与 create_device/delete_device/read_points 等持锁操作并发时会产生数据竞争。
        # 此处将 driver 实例化与 start()/add_device() 放在锁外（避免长时间持锁阻塞
        # 其他设备操作，与 R8-S-01 原则一致），仅将 _driver_instances 的写入放入锁内保护。
        driver = None
        if protocol == "simulator":
            driver = await self._get_simulator_driver()
            await driver.add_device(
                device["device_id"], {}, device.get("points", [])
            )  # FIXED: 补充 await 并修正参数顺序
        else:
            driver_class = self._registry.get_driver_class(protocol)
            if driver_class is None:
                logger.warning(
                    "No registered driver for protocol %s, device %s skipped",
                    protocol,
                    device.get("device_id"),
                )
                return
            driver = driver_class()
            await driver.start(device.get("config", {}))
            with contextlib.suppress(NotImplementedError):
                await driver.add_device(
                    device["device_id"],
                    device.get("config", {}),
                    device.get("points", []),
                )

        async with self._lock:
            self._driver_instances[device["device_id"]] = driver
            driver_ref = driver

        await self._scheduler.start_collect(
            device["device_id"],
            driver_ref,
            device.get("points", []),
            device.get("collect_interval", 5),
        )
