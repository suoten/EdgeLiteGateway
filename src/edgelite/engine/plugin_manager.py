"""插件热加载管理器 - v1.1 Pro版特性"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
from pathlib import Path
from typing import Type

from edgelite.drivers.base import DriverPlugin

logger = logging.getLogger(__name__)


class PluginManager:
    """插件热加载管理器，支持运行时加载/卸载驱动"""

    def __init__(self, plugin_dir: str = "plugins"):
        self._plugin_dir = Path(plugin_dir)
        self._loaded_plugins: dict[str, Type[DriverPlugin]] = {}
        self._plugin_instances: dict[str, DriverPlugin] = {}
        self._watch_task = None

    async def start(self) -> None:
        """启动插件管理器"""
        self._plugin_dir.mkdir(parents=True, exist_ok=True)
        await self._load_all_plugins()
        logger.info("插件管理器启动，已加载 %d 个插件", len(self._loaded_plugins))

    async def stop(self) -> None:
        """停止插件管理器"""
        for plugin_name in list(self._plugin_instances.keys()):
            await self.unload_plugin(plugin_name)
        logger.info("插件管理器停止")

    async def _load_all_plugins(self) -> None:
        """加载插件目录下所有插件"""
        if not self._plugin_dir.exists():
            return

        for plugin_file in self._plugin_dir.glob("*.py"):
            if plugin_file.name.startswith("_"):
                continue
            try:
                await self.load_plugin_from_file(plugin_file)
            except Exception as e:
                logger.error("加载插件失败 %s: %s", plugin_file, e)

    async def load_plugin_from_file(self, file_path: Path) -> str | None:
        """从文件加载插件"""
        plugin_name = file_path.stem

        # 动态导入模块
        spec = importlib.util.spec_from_file_location(f"plugin_{plugin_name}", file_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"无法加载插件文件: {file_path}")

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        # 查找DriverPlugin子类
        driver_class = None
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if (
                isinstance(attr, type)
                and issubclass(attr, DriverPlugin)
                and attr is not DriverPlugin
            ):
                driver_class = attr
                break

        if driver_class is None:
            raise ValueError(f"插件文件 {file_path} 中未找到DriverPlugin子类")

        # 注册插件
        self._loaded_plugins[plugin_name] = driver_class
        logger.info("插件加载成功: %s v%s", driver_class.plugin_name, driver_class.plugin_version)

        return plugin_name

    async def unload_plugin(self, plugin_name: str) -> bool:
        """卸载插件"""
        if plugin_name not in self._loaded_plugins:
            return False

        # 停止插件实例
        instance = self._plugin_instances.pop(plugin_name, None)
        if instance:
            await instance.stop()

        # 移除注册
        self._loaded_plugins.pop(plugin_name)
        logger.info("插件卸载: %s", plugin_name)
        return True

    async def reload_plugin(self, plugin_name: str) -> bool:
        """热重载插件"""
        plugin_file = self._plugin_dir / f"{plugin_name}.py"
        if not plugin_file.exists():
            return False

        await self.unload_plugin(plugin_name)
        await self.load_plugin_from_file(plugin_file)
        logger.info("插件热重载: %s", plugin_name)
        return True

    def get_plugin_class(self, plugin_name: str) -> Type[DriverPlugin] | None:
        """获取插件类"""
        return self._loaded_plugins.get(plugin_name)

    def list_plugins(self) -> list[dict]:
        """列出所有已加载插件"""
        return [
            {
                "name": name,
                "plugin_name": cls.plugin_name,
                "version": cls.plugin_version,
                "protocols": cls.supported_protocols,
                "active": name in self._plugin_instances,
            }
            for name, cls in self._loaded_plugins.items()
        ]

    async def create_instance(self, plugin_name: str, config: dict) -> DriverPlugin | None:
        """创建插件实例"""
        cls = self._loaded_plugins.get(plugin_name)
        if cls is None:
            return None

        instance = cls()
        await instance.start(config)
        self._plugin_instances[plugin_name] = instance
        return instance
