"""Driver Registry - Manages protocol to driver class mappings"""

from __future__ import annotations

import logging

from edgelite.drivers.base import DriverPlugin

logger = logging.getLogger(__name__)

# Driver display names for i18n support
DRIVER_DISPLAY_NAMES = {
    "modbus_tcp": {"en": "Modbus TCP", "zh": "Modbus TCP"},
    "modbus_rtu": {"en": "Modbus RTU", "zh": "Modbus RTU"},
    "simulator": {"en": "Simulator", "zh": "模拟器"},
    "mqtt_client": {"en": "MQTT Client", "zh": "MQTT客户端"},
    "http_webhook": {"en": "HTTP Webhook", "zh": "HTTP Webhook"},
    "opcua": {"en": "OPC UA Client", "zh": "OPC UA客户端"},
    "s7": {"en": "Siemens S7", "zh": "西门子S7"},
    "mc": {"en": "Mitsubishi MC", "zh": "三菱MC"},
    "fins": {"en": "Omron FINS", "zh": "欧姆龙FINS"},
    "allen_bradley": {"en": "Allen-Bradley", "zh": "Allen-Bradley"},
    "opc_da": {"en": "OPC DA Client", "zh": "OPC DA客户端"},
    "fanuc": {"en": "FANUC CNC", "zh": "FANUC CNC"},
    "mtconnect": {"en": "MTConnect", "zh": "MTConnect"},
    "toledo": {"en": "Toledo Scale", "zh": "托利多称重"},
    "serial_port": {"en": "Serial Port", "zh": "串口设备"},
    "database_source": {"en": "Database", "zh": "数据库接入"},
    "barcode_scanner": {"en": "Barcode Scanner", "zh": "扫码枪"},
    "sparkplug_b": {"en": "Sparkplug B", "zh": "Sparkplug B"},
    "dlt645": {"en": "DL/T 645-2007", "zh": "DL/T 645"},
    "iec104": {"en": "IEC 60870-5-104", "zh": "IEC 104"},
    "kuka": {"en": "KUKA KRL", "zh": "KUKA KRL"},
    "abb_robot": {"en": "ABB Robot", "zh": "ABB机器人"},
    "onvif": {"en": "ONVIF Camera", "zh": "ONVIF摄像头"},
    "video": {"en": "Video (GB28181)", "zh": "视频(GB28181)"},
    "opcua_server": {"en": "OPC UA Server", "zh": "OPC UA服务端"},
    "profinet": {"en": "Profinet DCP", "zh": "Profinet DCP"},
    "ethercat": {"en": "EtherCAT", "zh": "EtherCAT"},
}


def get_driver_display_name(plugin_name: str, language: str = "en") -> str:
    """Get the display name for a driver in the specified language"""
    name_info = DRIVER_DISPLAY_NAMES.get(plugin_name, {})
    return name_info.get(language, name_info.get("en", plugin_name))


class DriverRegistry:
    """驱动注册表，管理协议→驱动类的映射"""

    def __init__(self):
        self._drivers: dict[str, type[DriverPlugin]] = {}
        self._registered_drivers: set[type[DriverPlugin]] = set()  # 追踪已注册的驱动类，避免重复
        self._discovered = False

    def register(self, driver_class: type[DriverPlugin]) -> None:
        """注册驱动类"""
        # 避免重复注册同一驱动类
        if driver_class in self._registered_drivers:
            logger.debug("Driver %s already registered, skipping", driver_class.plugin_name)
            return
        
        for protocol in driver_class.supported_protocols:
            self._drivers[protocol] = driver_class
            logger.info(
                "Registered driver: %s v%s -> %s",
                driver_class.plugin_name,
                driver_class.plugin_version,
                protocol,
            )
        self._drivers[driver_class.plugin_name] = driver_class
        self._registered_drivers.add(driver_class)

    def get_driver_class(self, protocol: str) -> type[DriverPlugin] | None:
        """按协议类型获取驱动类"""
        return self._drivers.get(protocol)

    def get_supported_protocols(self) -> list[str]:
        """获取所有支持的唯一驱动名称（去重）"""
        # 返回已注册的驱动类的 plugin_name（去重）
        return sorted([cls.plugin_name for cls in self._registered_drivers])

    def unregister(self, protocol: str) -> bool:
        """注销指定协议驱动"""
        if protocol in self._drivers:
            del self._drivers[protocol]
            return True
        return False

    def unregister_driver(self, driver_cls: type) -> int:
        """注销指定驱动类的所有协议，返回移除数量"""
        if driver_cls not in self._registered_drivers:
            return 0
        to_remove = [p for p, cls in self._drivers.items() if cls is driver_cls]
        for p in to_remove:
            del self._drivers[p]
        self._registered_drivers.discard(driver_cls)
        return len(to_remove)

    def items(self) -> list[tuple[str, type[DriverPlugin]]]:
        """获取所有已注册的协议-驱动对（去重）"""
        # 去重：只返回每个驱动类第一个注册的协议
        seen = set()
        result = []
        for protocol, cls in self._drivers.items():
            if cls not in seen:
                seen.add(cls)
                result.append((protocol, cls))
        return result

    def auto_discover(self) -> None:
        """自动发现并注册所有内置驱动及自定义驱动"""
        if self._discovered:
            logger.warning("auto_discover already executed, skipping duplicate call")
            return
        self._discovered = True
        _driver_modules = [
            ("Modbus TCP", "edgelite.drivers.modbus_tcp", "ModbusTcpDriver"),
            ("Modbus RTU", "edgelite.drivers.modbus_rtu", "ModbusRtuDriver"),
            ("Simulator", "edgelite.drivers.simulator", "SimulatorDriver"),
            ("MQTT Client", "edgelite.drivers.mqtt_client", "MqttClientDriver"),
            ("HTTP Webhook", "edgelite.drivers.http_webhook", "HttpWebhookDriver"),
            ("OPC UA", "edgelite.drivers.opcua", "OpcUaDriver"),
            ("Siemens S7", "edgelite.drivers.s7", "S7Driver"),
            ("Mitsubishi MC", "edgelite.drivers.mc", "McDriver"),
            ("Omron FINS", "edgelite.drivers.fins", "OmronFinsDriver"),
            ("Allen-Bradley", "edgelite.drivers.allen_bradley", "AllenBradleyDriver"),
            ("OPC DA", "edgelite.drivers.opc_da", "OpcDaDriver"),
            ("FANUC CNC", "edgelite.drivers.fanuc", "FanucCncDriver"),
            ("MTConnect", "edgelite.drivers.mtconnect", "MTConnectDriver"),
            ("Toledo Scale", "edgelite.drivers.toledo", "ToledoDriver"),
            ("Serial Port", "edgelite.drivers.serial_port", "SerialPortDriver"),
            ("Database", "edgelite.drivers.database_source", "DatabaseSourceDriver"),
            ("Barcode Scanner", "edgelite.drivers.barcode_scanner", "BarcodeScannerDriver"),
            ("Sparkplug B", "edgelite.drivers.sparkplug_b", "SparkplugBDriver"),
            ("DL/T 645", "edgelite.drivers.dlt645", "Dlt645Driver"),
            ("IEC 104", "edgelite.drivers.iec104", "Iec104Driver"),
            ("KUKA KRL", "edgelite.drivers.kuka", "KukaDriver"),
            ("ABB Robot", "edgelite.drivers.abb_robot", "AbbRobotDriver"),
            ("ONVIF Camera", "edgelite.drivers.onvif_driver", "OnvifDriver"),
            ("Video GB28181", "edgelite.drivers.video", "VideoDriver"),
            ("OPC UA Server", "edgelite.drivers.opcua_server", "OpcUaServerDriver"),
            ("Profinet DCP", "edgelite.drivers.profinet", "ProfinetDriver"),
            ("EtherCAT", "edgelite.drivers.ethercat", "EtherCATDriver"),
        ]

        for label, module_path, class_name in _driver_modules:
            self._load_driver(label, module_path, class_name)

        self._discover_custom_drivers()

        logger.info("Driver auto-discovery complete, supported protocols: %s", self.get_supported_protocols())

    def _load_driver(self, label: str, module_path: str, class_name: str) -> bool:
        """加载单个驱动模块"""
        try:
            import importlib

            module = importlib.import_module(module_path)
            driver_cls = getattr(module, class_name)
            self.register(driver_cls)
            return True
        except ImportError as e:
            logger.warning("%s driver import failed (missing dependency): %s", label, e)
        except AttributeError as e:
            logger.warning("%s driver class not found: %s", label, e)
        except Exception as e:
            logger.warning("%s driver load failed: %s", label, e)
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
            logger.warning("Custom driver directory does not exist: %s", custom_dir)
            return

        logger.info("Scanning custom driver directory: %s", custom_dir)
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
                logger.warning("Custom driver %s failed to load: %s", py_file.name, e)

        if loaded > 0:
            logger.info("Loaded %d custom drivers from %s", loaded, custom_dir)


_registry: DriverRegistry | None = None


def get_driver_registry() -> DriverRegistry:
    """获取全局驱动注册表"""
    global _registry
    if _registry is None:
        _registry = DriverRegistry()
        _registry.auto_discover()
    return _registry
