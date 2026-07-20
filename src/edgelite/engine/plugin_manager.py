"""自定义驱动插件管理器"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import inspect
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from edgelite.drivers.base import DriverPlugin

logger = logging.getLogger(__name__)


@dataclass
class PluginInfo:
    name: str
    module_path: str
    class_name: str
    is_custom: bool = False
    is_loaded: bool = False
    error: str = ""


class PluginManager:
    """自定义驱动插件管理器"""

    def __init__(self, driver_registry: Any):
        self._registry = driver_registry
        self._loaded_plugins: dict[str, PluginInfo] = {}
        self._allowed_dir: Path | None = None
        # FIXED-P0: 添加锁保护_loaded_plugins字典的并发访问
        self._lock = threading.Lock()
        # FIXED-P1: 引用scheduler，用于reload/unload时停止活跃驱动
        self._scheduler: Any = None

    def set_scheduler(self, scheduler: Any) -> None:
        """FIXED-P1: 设置scheduler引用，用于reload/unload时停止活跃驱动"""
        self._scheduler = scheduler

    async def discover_custom_drivers_async(self, custom_dir: str) -> list[PluginInfo]:
        """FIXED-P1: 异步版本，使用asyncio.to_thread包装同步加载操作，防止阻塞事件循环"""
        return await asyncio.to_thread(self.discover_custom_drivers, custom_dir)

    def discover_custom_drivers(self, custom_dir: str) -> list[PluginInfo]:
        """扫描自定义驱动目录，加载所有DriverPlugin子类"""
        if not custom_dir:
            return []

        driver_dir = Path(custom_dir).resolve()
        if not driver_dir.is_dir():
            logger.warning("自定义驱动目录不存在: %s", custom_dir)
            return []

        self._allowed_dir = driver_dir
        logger.info("自定义驱动目录: %s（仅允许从此目录加载）", driver_dir)

        discovered = []
        for py_file in driver_dir.glob("*.py"):
            if py_file.name.startswith("_"):
                continue
            try:
                module = self._load_module(py_file)
                plugin_classes = self._find_driver_subclasses(module)
                for cls in plugin_classes:
                    info = PluginInfo(
                        name=cls.plugin_name,
                        module_path=str(py_file),
                        class_name=cls.__name__,
                        is_custom=True,
                        is_loaded=True,
                    )
                    self._register_plugin(cls, info)
                    discovered.append(info)
            except Exception as e:
                info = PluginInfo(
                    name=py_file.stem,
                    module_path=str(py_file),
                    class_name="",
                    is_custom=True,
                    is_loaded=False,
                    error=str(e),
                )
                with self._lock:  # FIXED-P0: 加锁保护_loaded_plugins
                    self._loaded_plugins[py_file.stem] = info
                discovered.append(info)
                logger.error("加载自定义驱动失败 %s: %s", py_file, e)

        return discovered

    def _load_module(self, path: Path):
        """动态加载Python模块（带路径白名单安全检查）"""
        resolved = path.resolve()
        if self._allowed_dir and not resolved.is_relative_to(self._allowed_dir):
            raise ValueError(f"安全拒绝: 文件 {resolved} 不在允许的目录 {self._allowed_dir} 内")
        if resolved.suffix != ".py":
            raise ValueError(f"安全拒绝: 仅允许加载.py文件，收到 {resolved.suffix}")
        spec = importlib.util.spec_from_file_location(path.stem, str(resolved))
        if spec is None or spec.loader is None:
            raise ValueError(f"无法加载模块: {resolved}")
        module = importlib.util.module_from_spec(spec)
        # FIXED(严重): 原问题-exec_module执行任意Python文件;
        # 修复-安全依赖目录白名单校验（见上方resolved.is_relative_to检查），
        # 仅允许从_allowed_dir目录加载.py文件，拒绝路径穿越
        spec.loader.exec_module(module)
        return module

    def _find_driver_subclasses(self, module) -> list[type[DriverPlugin]]:
        """查找模块中所有DriverPlugin的子类"""
        classes: list[type[DriverPlugin]] = []
        for _name, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, DriverPlugin) and obj is not DriverPlugin:
                classes.append(obj)
        return classes

    def _register_plugin(self, cls: type, info: PluginInfo):
        existing = self._registry.get_driver_class(info.name)
        if existing is not None:
            # FIXED(严重): 原问题-同名自定义驱动覆盖内置驱动;
            # 修复-拒绝覆盖内置驱动，跳过注册
            logger.warning("Plugin '%s' already registered, skipping", info.name)
            return

        self._registry.register(cls)
        with self._lock:  # FIXED-P0: 加锁保护_loaded_plugins
            self._loaded_plugins[info.name] = info
        logger.info("已加载自定义驱动: %s (%s)", info.name, info.class_name)

    async def reload_plugin_async(self, plugin_name: str) -> PluginInfo | None:
        """FIXED-P1: 异步版本，使用asyncio.to_thread包装同步加载，并在reload前停止活跃驱动"""
        # FIXED-P1: reload前停止所有使用该插件创建的活跃驱动实例
        if self._scheduler:
            try:
                # 通知scheduler停止使用该插件的所有采集任务
                if hasattr(self._scheduler, "stop_collect_by_protocol"):
                    await self._scheduler.stop_collect_by_protocol(plugin_name)
                    logger.info("已停止插件 %s 的所有活跃驱动实例，准备重载", plugin_name)
                else:
                    logger.warning("reload_plugin: scheduler不支持stop_collect_by_protocol，活跃驱动可能需要手动重启")
            except Exception as e:
                logger.warning("reload_plugin: 停止活跃驱动失败: %s", e)

        info = await asyncio.to_thread(self.reload_plugin, plugin_name)
        return info

    def reload_plugin(self, plugin_name: str) -> PluginInfo | None:
        """热重载指定驱动"""
        with self._lock:  # FIXED-P0: 加锁保护_loaded_plugins读取
            info = self._loaded_plugins.get(plugin_name)
        if not info:
            return None

        try:
            module = self._load_module(Path(info.module_path))
            plugin_classes = self._find_driver_subclasses(module)
            if not plugin_classes:
                raise ValueError("未找到DriverPlugin子类")

            cls = plugin_classes[0]
            info.is_loaded = True
            info.error = ""
            info.class_name = cls.__name__
            self._registry.register(cls)
            with self._lock:  # FIXED-P0: 加锁保护_loaded_plugins写入
                self._loaded_plugins[plugin_name] = info
            logger.info("已重载驱动: %s", plugin_name)
            return info
        except Exception as e:
            info.is_loaded = False
            info.error = str(e)
            logger.error("重载驱动失败 %s: %s", plugin_name, e)
            return info

    async def unload_plugin_async(self, plugin_name: str) -> bool:
        """FIXED-P1: 异步版本，unload前停止活跃驱动"""
        if self._scheduler:
            try:
                if hasattr(self._scheduler, "stop_collect_by_protocol"):
                    await self._scheduler.stop_collect_by_protocol(plugin_name)
            except Exception as e:
                logger.warning("unload_plugin: 停止活跃驱动失败: %s", e)
        return self.unload_plugin(plugin_name)

    def unload_plugin(self, plugin_name: str) -> bool:
        """卸载指定驱动"""
        with self._lock:  # FIXED-P0: 加锁保护_loaded_plugins读取
            info = self._loaded_plugins.get(plugin_name)
        if not info or not info.is_custom:
            return False

        cls = self._registry.get_driver_class(plugin_name)
        if cls:
            self._registry.unregister_driver(cls)
        info.is_loaded = False
        with self._lock:  # FIXED-P0: 加锁保护_loaded_plugins写入
            self._loaded_plugins[plugin_name] = info
        logger.info("已卸载驱动: %s", plugin_name)
        return True

    def list_plugins(self) -> list[PluginInfo]:
        """列出所有已加载的插件"""
        with self._lock:  # FIXED-P0: 加锁保护_loaded_plugins读取
            return list(self._loaded_plugins.values())

    async def stop(self) -> None:
        """停止插件管理器，卸载所有自定义驱动"""
        with self._lock:  # FIXED-P0: 加锁保护_loaded_plugins读取
            plugin_names = list(self._loaded_plugins.keys())
        for plugin_name in plugin_names:
            self.unload_plugin(plugin_name)
        with self._lock:  # FIXED-P0: 加锁保护_loaded_plugins清空
            self._loaded_plugins.clear()
        logger.info("PluginManager已停止，所有自定义驱动已卸载")
