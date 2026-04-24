"""驱动注册表"""

from __future__ import annotations

import logging
from typing import Type

from edgelite.drivers.base import DriverPlugin

logger = logging.getLogger(__name__)


class DriverRegistry:
    """驱动注册表，管理协议→驱动类的映射"""

    def __init__(self):
        self._drivers: dict[str, Type[DriverPlugin]] = {}

    def register(self, driver_class: Type[DriverPlugin]) -> None:
        """注册驱动类"""
        for protocol in driver_class.supported_protocols:
            self._drivers[protocol] = driver_class
            logger.info(
                "注册驱动: %s v%s -> %s",
                driver_class.plugin_name,
                driver_class.plugin_version,
                protocol,
            )

    def get_driver_class(self, protocol: str) -> Type[DriverPlugin] | None:
        """按协议类型获取驱动类"""
        return self._drivers.get(protocol)

    def get_supported_protocols(self) -> list[str]:
        """获取所有支持的协议"""
        return list(self._drivers.keys())

    def auto_discover(self) -> None:
        """自动发现并注册所有内置驱动"""
        # Modbus TCP
        try:
            from edgelite.drivers.modbus_tcp import ModbusTcpDriver
            self.register(ModbusTcpDriver)
        except ImportError as e:
            logger.warning("Modbus TCP驱动加载失败: %s", e)

        # 模拟器
        try:
            from edgelite.drivers.simulator import SimulatorDriver
            self.register(SimulatorDriver)
        except ImportError as e:
            logger.warning("模拟器驱动加载失败: %s", e)

        # MQTT Client
        try:
            from edgelite.drivers.mqtt_client import MqttClientDriver
            self.register(MqttClientDriver)
        except ImportError as e:
            logger.warning("MQTT Client驱动加载失败: %s", e)

        # HTTP Webhook
        try:
            from edgelite.drivers.http_webhook import HttpWebhookDriver
            self.register(HttpWebhookDriver)
        except ImportError as e:
            logger.warning("HTTP Webhook驱动加载失败: %s", e)

        # OPC-UA
        try:
            from edgelite.drivers.opcua import OpcUaDriver
            self.register(OpcUaDriver)
        except ImportError as e:
            logger.warning("OPC-UA驱动加载失败: %s", e)

        # 西门子S7 (Pro版)
        try:
            from edgelite.drivers.s7 import S7Driver
            self.register(S7Driver)
        except ImportError as e:
            logger.warning("西门子S7驱动加载失败: %s", e)

        # 三菱MC (Pro版)
        try:
            from edgelite.drivers.mc import McDriver
            self.register(McDriver)
        except ImportError as e:
            logger.warning("三菱MC驱动加载失败: %s", e)

        # 欧姆龙FINS (Pro版)
        try:
            from edgelite.drivers.fins import OmronFinsDriver
            self.register(OmronFinsDriver)
        except ImportError as e:
            logger.warning("欧姆龙FINS驱动加载失败: %s", e)

        # Allen-Bradley (Pro版)
        try:
            from edgelite.drivers.allen_bradley import AllenBradleyDriver
            self.register(AllenBradleyDriver)
        except ImportError as e:
            logger.warning("Allen-Bradley驱动加载失败: %s", e)

        # OPC DA Client (Pro版)
        try:
            from edgelite.drivers.opc_da import OpcDaDriver
            self.register(OpcDaDriver)
        except ImportError as e:
            logger.warning("OPC DA驱动加载失败: %s", e)

        # FANUC CNC (Pro版)
        try:
            from edgelite.drivers.fanuc import FanucCncDriver
            self.register(FanucCncDriver)
        except ImportError as e:
            logger.warning("FANUC CNC驱动加载失败: %s", e)

        # MTConnect (Pro版)
        try:
            from edgelite.drivers.mtconnect import MTConnectDriver
            self.register(MTConnectDriver)
        except ImportError as e:
            logger.warning("MTConnect驱动加载失败: %s", e)

        # 托利多称重 (Pro版)
        try:
            from edgelite.drivers.toledo import ToledoDriver
            self.register(ToledoDriver)
        except ImportError as e:
            logger.warning("托利多称重驱动加载失败: %s", e)

        logger.info("驱动自动发现完成，支持协议: %s", self.get_supported_protocols())


# 全局驱动注册表
_registry: DriverRegistry | None = None


def get_driver_registry() -> DriverRegistry:
    """获取全局驱动注册表"""
    global _registry
    if _registry is None:
        _registry = DriverRegistry()
        _registry.auto_discover()
    return _registry
