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
        self._discovered = False

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
        if self._discovered:
            logger.warning("auto_discover已执行过，跳过重复调用")
            return
        self._discovered = True
        _driver_modules = [
            ("Modbus TCP", "edgelite.drivers.modbus_tcp", "ModbusTcpDriver"),
            ("模拟器", "edgelite.drivers.simulator", "SimulatorDriver"),
            ("MQTT Client", "edgelite.drivers.mqtt_client", "MqttClientDriver"),
            ("HTTP Webhook", "edgelite.drivers.http_webhook", "HttpWebhookDriver"),
            ("OPC-UA", "edgelite.drivers.opcua", "OpcUaDriver"),
            ("西门子S7", "edgelite.drivers.s7", "S7Driver"),
            ("三菱MC", "edgelite.drivers.mc", "McDriver"),
            ("欧姆龙FINS", "edgelite.drivers.fins", "OmronFinsDriver"),
            ("Allen-Bradley", "edgelite.drivers.allen_bradley", "AllenBradleyDriver"),
            ("OPC DA", "edgelite.drivers.opc_da", "OpcDaDriver"),
            ("FANUC CNC", "edgelite.drivers.fanuc", "FanucCncDriver"),
            ("MTConnect", "edgelite.drivers.mtconnect", "MTConnectDriver"),
            ("托利多称重", "edgelite.drivers.toledo", "ToledoDriver"),
            ("BACnet", "edgelite.drivers.bacnet", "BACnetDriver"),
            ("串口设备", "edgelite.drivers.serial_port", "SerialPortDriver"),
            ("数据库接入", "edgelite.drivers.database_source", "DatabaseSourceDriver"),
            ("扫码枪", "edgelite.drivers.barcode_scanner", "BarcodeScannerDriver"),
        ]

        for label, module_path, class_name in _driver_modules:
            try:
                import importlib
                module = importlib.import_module(module_path)
                driver_cls = getattr(module, class_name)
                self.register(driver_cls)
            except Exception as e:
                logger.warning("%s驱动加载失败: %s", label, e)

        logger.info("驱动自动发现完成，支持协议: %s", self.get_supported_protocols())


_registry: DriverRegistry | None = None


def get_driver_registry() -> DriverRegistry:
    """获取全局驱动注册表"""
    global _registry
    if _registry is None:
        _registry = DriverRegistry()
        _registry.auto_discover()
    return _registry
