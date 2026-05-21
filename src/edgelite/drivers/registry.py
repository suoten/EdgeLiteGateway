"""驱动注册表"""

from __future__ import annotations

import logging

from edgelite.drivers.base import DriverPlugin

logger = logging.getLogger(__name__)


class DriverRegistry:
    """驱动注册表，管理协议→驱动类的映射"""

    def __init__(self):
        self._drivers: dict[str, type[DriverPlugin]] = {}
        self._discovered = False

    def register(self, driver_class: type[DriverPlugin]) -> None:
        """注册驱动类"""
        for protocol in driver_class.supported_protocols:
            self._drivers[protocol] = driver_class
            logger.info(
                "注册驱动: %s v%s -> %s",
                driver_class.plugin_name,
                driver_class.plugin_version,
                protocol,
            )
        self._drivers[driver_class.plugin_name] = driver_class

    def get_driver_class(self, protocol: str) -> type[DriverPlugin] | None:
        """按协议类型获取驱动类"""
        return self._drivers.get(protocol)

    def get_supported_protocols(self) -> list[str]:
        """获取所有支持的协议"""
        return list(self._drivers.keys())

    def unregister(self, protocol: str) -> bool:
        """注销指定协议驱动"""
        if protocol in self._drivers:
            del self._drivers[protocol]
            return True
        return False

    def unregister_driver(self, driver_cls: type) -> int:
        """注销指定驱动类的所有协议，返回移除数量"""
        to_remove = [p for p, cls in self._drivers.items() if cls is driver_cls]
        for p in to_remove:
            del self._drivers[p]
        return len(to_remove)

    def items(self) -> list[tuple[str, type[DriverPlugin]]]:
        """获取所有已注册的协议-驱动对"""
        return list(self._drivers.items())

    def auto_discover(self) -> None:
        """自动发现并注册所有内置驱动及自定义驱动"""
        if self._discovered:
            logger.warning("auto_discover已执行过，跳过重复调用")
            return
        self._discovered = True
        _driver_modules = [
            ("Modbus TCP", "edgelite.drivers.modbus_tcp", "ModbusTcpDriver"),
            ("Modbus RTU", "edgelite.drivers.modbus_rtu", "ModbusRtuDriver"),
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
            ("串口设备", "edgelite.drivers.serial_port", "SerialPortDriver"),
            ("数据库接入", "edgelite.drivers.database_source", "DatabaseSourceDriver"),
            ("扫码枪", "edgelite.drivers.barcode_scanner", "BarcodeScannerDriver"),
            ("Sparkplug B", "edgelite.drivers.sparkplug_b", "SparkplugBDriver"),
            ("DL/T 645", "edgelite.drivers.dlt645", "Dlt645Driver"),
            ("IEC 104", "edgelite.drivers.iec104", "Iec104Driver"),
            ("KUKA EKRL", "edgelite.drivers.kuka", "KukaDriver"),
            ("ABB RWS", "edgelite.drivers.abb_robot", "AbbRobotDriver"),
            ("ONVIF", "edgelite.drivers.onvif_driver", "OnvifDriver"),
            ("视频(GB28181)", "edgelite.drivers.video", "VideoDriver"),
        ]

        for label, module_path, class_name in _driver_modules:
            self._load_driver(label, module_path, class_name)

        self._discover_custom_drivers()

        logger.info("驱动自动发现完成，支持协议: %s", self.get_supported_protocols())

    def _load_driver(self, label: str, module_path: str, class_name: str) -> bool:
        """加载单个驱动模块"""
        try:
            import importlib

            module = importlib.import_module(module_path)
            driver_cls = getattr(module, class_name)
            self.register(driver_cls)
            return True
        except ImportError as e:
            logger.warning("%s驱动导入失败(缺少依赖): %s", label, e)
        except AttributeError as e:
            logger.warning("%s驱动类不存在: %s", label, e)
        except Exception as e:
            logger.warning("%s驱动加载失败: %s", label, e)
        return False

    def _discover_custom_drivers(self) -> None:
        """从custom_dir发现并加载自定义驱动"""
        try:
            from edgelite.config import get_config

            custom_dir = get_config().drivers.custom_dir
        except Exception:
            return

        if not custom_dir:
            return

        import importlib
        import importlib.util
        from pathlib import Path

        custom_path = Path(custom_dir)
        if not custom_path.is_dir():
            logger.warning("自定义驱动目录不存在: %s", custom_dir)
            return

        logger.info("扫描自定义驱动目录: %s", custom_dir)
        loaded = 0
        for py_file in custom_path.glob("*.py"):
            if py_file.name.startswith("_"):
                continue
            module_name = f"edgelite.drivers.custom_{py_file.stem}"
            try:
                spec = importlib.util.spec_from_file_location(module_name, py_file)
                if spec is None or spec.loader is None:
                    continue
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                for attr_name in dir(module):
                    attr = getattr(module, attr_name)
                    if (
                        isinstance(attr, type)
                        and issubclass(attr, DriverPlugin)
                        and attr is not DriverPlugin
                    ):
                        self.register(attr)
                        loaded += 1
            except Exception as e:
                logger.warning("自定义驱动 %s 加载失败: %s", py_file.name, e)

        if loaded > 0:
            logger.info("从 %s 加载了 %d 个自定义驱动", custom_dir, loaded)


_registry: DriverRegistry | None = None


def get_driver_registry() -> DriverRegistry:
    """获取全局驱动注册表"""
    global _registry
    if _registry is None:
        _registry = DriverRegistry()
        _registry.auto_discover()
    return _registry
