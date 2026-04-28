"""自定义驱动插件管理器"""

from __future__ import annotations
import importlib
import importlib.util
import inspect
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

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

    def discover_custom_drivers(self, custom_dir: str) -> list[PluginInfo]:
        """扫描自定义驱动目录，加载所有DriverPlugin子类"""
        if not custom_dir:
            return []

        driver_dir = Path(custom_dir)
        if not driver_dir.is_dir():
            logger.warning("自定义驱动目录不存在: %s", custom_dir)
            return []

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
                self._loaded_plugins[py_file.stem] = info
                discovered.append(info)
                logger.error("加载自定义驱动失败 %s: %s", py_file, e)

        return discovered

    def _load_module(self, path: Path):
        """动态加载Python模块"""
        spec = importlib.util.spec_from_file_location(path.stem, str(path))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _find_driver_subclasses(self, module) -> list[type]:
        """查找模块中所有DriverPlugin的子类"""
        classes = []
        for name, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, DriverPlugin) and obj is not DriverPlugin:
                classes.append(obj)
        return classes

    def _register_plugin(self, cls: type, info: PluginInfo):
        """注册驱动到DriverRegistry"""
        existing = self._registry.get_driver_class(info.name)
        if existing is not None:
            logger.warning("自定义驱动'%s'与内置驱动同名，将覆盖", info.name)

        self._registry._drivers[info.name] = cls
        self._loaded_plugins[info.name] = info
        logger.info("已加载自定义驱动: %s (%s)", info.name, info.class_name)

    def reload_plugin(self, plugin_name: str) -> Optional[PluginInfo]:
        """热重载指定驱动"""
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
            self._registry._drivers[plugin_name] = cls
            logger.info("已重载驱动: %s", plugin_name)
            return info
        except Exception as e:
            info.is_loaded = False
            info.error = str(e)
            logger.error("重载驱动失败 %s: %s", plugin_name, e)
            return info

    def unload_plugin(self, plugin_name: str) -> bool:
        """卸载指定驱动"""
        info = self._loaded_plugins.get(plugin_name)
        if not info or not info.is_custom:
            return False

        if plugin_name in self._registry._drivers:
            del self._registry._drivers[plugin_name]
        info.is_loaded = False
        logger.info("已卸载驱动: %s", plugin_name)
        return True

    def list_plugins(self) -> list[PluginInfo]:
        """列出所有已加载的插件"""
        return list(self._loaded_plugins.values())
