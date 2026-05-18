"""服务管理器 - 统一管理所有可选服务的生命周期、依赖检查和动态启停"""

from __future__ import annotations

import asyncio
import importlib
import logging
import sys
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from edgelite.config import get_config, update_config_section

logger = logging.getLogger(__name__)


class ServiceState(StrEnum):
    DISABLED = "disabled"
    ENABLED = "enabled"
    RUNNING = "running"
    ERROR = "error"
    INSTALLING = "installing"


@dataclass
class DependencyInfo:
    package: str
    installed: bool = False
    version: str = ""


@dataclass
class ServiceInfo:
    name: str
    display_name: str
    description: str
    config_section: str
    state: ServiceState = ServiceState.DISABLED
    dependencies: list[DependencyInfo] = field(default_factory=list)
    error_message: str = ""
    config_schema: dict = field(default_factory=dict)
    current_config: dict = field(default_factory=dict)
    running_info: dict = field(default_factory=dict)
    icon: str = ""
    category: str = "builtin"
    use_cases: list[str] = field(default_factory=list)
    related_features: list[dict] = field(default_factory=list)
    setup_guide: list[str] = field(default_factory=list)


# pip包名到Python导入名的映射（当两者不一致时需要在此添加）
_PIP_TO_IMPORT = {
    "pyserial": "serial",
}

SERVICE_DEFINITIONS = {
    "mqtt_server": {
        "display_name": "内置 MQTT Server",
        "description": "轻量级MQTT Broker服务，支持标准MQTT 3.1.1协议和WebSocket接入",
        "icon": "radio",
        "category": "builtin",
        "config_section": "mqtt_server",
        "dependencies": ["amqtt"],
        "engine_class": "edgelite.engine.mqtt_server.MqttServer",
        "use_cases": [
            "本地设备通过MQTT协议接入网关",
            "作为开发测试用的MQTT Broker",
            "南向设备模拟数据上报",
        ],
        "related_features": [
            {"name": "设备管理", "route": "Devices", "hint": "MQTT设备可通过此Broker接入"},
            {"name": "平台对接", "route": "PlatformConfig", "hint": "北向平台可订阅MQTT数据"},
        ],
        "setup_guide": [
            "开启服务开关启用MQTT Server",
            "如缺少依赖，点击「一键安装」自动安装amqtt",
            "配置监听端口和认证信息",
            "启动服务后，设备即可通过MQTT协议连接",
        ],
        "config_schema": {
            "host": {
                "type": "string",
                "default": "0.0.0.0",
                "label": "监听地址",
                "description": "MQTT Broker监听的IP地址，0.0.0.0表示所有网卡",
            },
            "port": {
                "type": "integer",
                "default": 1888,
                "label": "TCP端口",
                "description": "MQTT TCP连接端口，默认1888避免与外部Broker冲突",
            },
            "ws_port": {
                "type": "integer",
                "default": 8083,
                "label": "WebSocket端口",
                "description": "WebSocket连接端口，供浏览器端客户端使用",
                "nullable": True,
            },
            "username": {
                "type": "string",
                "default": "",
                "label": "认证用户名",
                "description": "留空则允许匿名连接",
            },
            "password": {
                "type": "string",
                "default": "",
                "label": "认证密码",
                "description": "留空则不需要密码",
            },
        },
    },
    "modbus_slave": {
        "display_name": "内置 Modbus Slave",
        "description": "Modbus TCP从站模拟器，支持四类寄存器和设备数据映射",
        "icon": "power",
        "category": "builtin",
        "config_section": "modbus_slave",
        "dependencies": ["pymodbus"],
        "engine_class": "edgelite.engine.modbus_slave.ModbusSlaveServer",
        "use_cases": [
            "模拟Modbus从站设备供上位机/PLC读取",
            "将网关采集的设备数据映射到Modbus寄存器",
            "开发调试Modbus通信功能",
        ],
        "related_features": [
            {"name": "设备管理", "route": "Devices", "hint": "设备测点数据可映射到Modbus寄存器"},
            {"name": "数据查询", "route": "DataQuery", "hint": "查询映射到寄存器的设备数据"},
        ],
        "setup_guide": [
            "开启服务开关启用Modbus Slave",
            "如缺少依赖，点击「一键安装」自动安装pymodbus",
            "配置监听端口和寄存器数量",
            "启动服务后，上位机可通过Modbus TCP读取数据",
        ],
        "config_schema": {
            "host": {
                "type": "string",
                "default": "0.0.0.0",
                "label": "监听地址",
                "description": "Modbus从站监听的IP地址",
            },
            "port": {
                "type": "integer",
                "default": 502,
                "label": "监听端口",
                "description": "Modbus TCP标准端口为502",
            },
            "holding_size": {
                "type": "integer",
                "default": 1000,
                "label": "保持寄存器数量",
                "description": "可读写的保持寄存器数量",
            },
            "input_size": {
                "type": "integer",
                "default": 1000,
                "label": "输入寄存器数量",
                "description": "只读的输入寄存器数量",
            },
        },
    },
    "serial_bridge": {
        "display_name": "串口桥接",
        "description": "串口到TCP的双向数据透传桥接服务",
        "icon": "swap",
        "category": "builtin",
        "config_section": "serial_bridge",
        "dependencies": ["pyserial"],
        "engine_class": "edgelite.engine.serial_bridge.SerialTcpBridge",
        "use_cases": [
            "远程访问本地串口设备",
            "通过网络透传串口数据到远程服务器",
            "多客户端同时访问同一串口设备",
        ],
        "related_features": [
            {"name": "设备管理", "route": "Devices", "hint": "串口设备可通过桥接远程管理"},
            {"name": "驱动配置", "route": "DriverConfig", "hint": "配置串口驱动参数"},
        ],
        "setup_guide": [
            "确保网关已连接串口设备（如USB转串口）",
            "开启服务开关启用串口桥接",
            "如缺少依赖，点击「一键安装」自动安装pyserial",
            "配置串口设备路径、波特率和TCP端口",
            "启动服务后，远程客户端可通过TCP连接访问串口",
        ],
        "config_schema": {
            "serial_port": {
                "type": "string",
                "default": "/dev/ttyUSB0",
                "label": "串口设备",
                "description": "串口设备路径，Linux如/dev/ttyUSB0，Windows如COM3",
            },
            "baud_rate": {
                "type": "integer",
                "default": 9600,
                "label": "波特率",
                "description": "串口通信波特率，需与设备一致",
            },
            "tcp_port": {
                "type": "integer",
                "default": 9000,
                "label": "TCP监听端口",
                "description": "远程客户端连接的TCP端口",
            },
            "ip_whitelist": {
                "type": "array",
                "default": [],
                "label": "IP白名单",
                "description": "允许连接的IP地址列表，留空则允许所有",
            },
            "max_clients": {
                "type": "integer",
                "default": 5,
                "label": "最大客户端数",
                "description": "同时连接的最大客户端数量",
            },
        },
    },
    "mcp_server": {
        "display_name": "MCP Server",
        "description": "Model Context Protocol服务端，提供AI助手与网关交互的标准协议接口",
        "icon": "puzzle",
        "category": "integration",
        "config_section": "mcp_server",
        "dependencies": [],
        "engine_class": None,
        "use_cases": [
            "让AI助手（如Claude、ChatGPT）直接查询设备状态",
            "通过AI对话控制设备读写操作",
            "AI辅助分析告警和规则配置",
        ],
        "related_features": [
            {"name": "设备管理", "route": "Devices", "hint": "AI可查询和控制设备"},
            {"name": "告警中心", "route": "Alarms", "hint": "AI可分析活跃告警"},
            {"name": "规则管理", "route": "Rules", "hint": "AI可查看和辅助配置规则"},
        ],
        "setup_guide": [
            "开启服务开关启用MCP Server",
            "MCP Server无需额外依赖，启用即可使用",
            "在AI客户端配置SSE端点：http://网关地址:端口/api/v1/mcp/sse",
            "如需认证，创建API Key并在客户端配置",
        ],
        "config_schema": {},
    },
    "grafana": {
        "display_name": "Grafana监控",
        "description": "Grafana可视化监控集成，支持仪表板嵌入和数据源配置",
        "icon": "chart",
        "category": "integration",
        "config_section": "grafana",
        "dependencies": ["httpx"],
        "engine_class": None,
        "use_cases": [
            "在网关界面直接查看Grafana仪表板",
            "通过Grafana对设备数据进行高级可视化分析",
            "统一监控大屏展示",
        ],
        "related_features": [
            {"name": "数据查询", "route": "DataQuery", "hint": "Grafana可查询InfluxDB中的历史数据"},
            {"name": "系统管理", "route": "System", "hint": "系统状态可同步到Grafana监控"},
        ],
        "setup_guide": [
            "确保已部署Grafana服务（默认端口3001）",
            "开启服务开关启用Grafana集成",
            "如缺少依赖，点击「一键安装」自动安装httpx",
            "配置Grafana地址和API Key",
            "在Grafana中配置InfluxDB数据源指向网关的时序数据库",
        ],
        "config_schema": {
            "url": {
                "type": "string",
                "default": "http://localhost:3001",
                "label": "Grafana地址",
                "description": "Grafana服务的完整访问地址",
            },
            "api_key": {
                "type": "string",
                "default": "",
                "label": "API Key",
                "description": "Grafana API密钥，用于访问仪表板接口",
            },
            "datasource": {
                "type": "string",
                "default": "InfluxDB",
                "label": "数据源名称",
                "description": "Grafana中配置的数据源名称",
            },
        },
    },
}


class ServiceManager:
    """统一服务管理器"""

    def __init__(self):
        self._instances: dict[str, Any] = {}
        self._install_tasks: dict[str, asyncio.Task] = {}

    def _get_app_state(self):
        from edgelite.app import _app_state

        return _app_state

    def check_dependency(self, package_name: str) -> DependencyInfo:
        import_name = _PIP_TO_IMPORT.get(package_name, package_name)
        try:
            mod = importlib.import_module(import_name)
            version = getattr(mod, "__version__", "")
            return DependencyInfo(package=package_name, installed=True, version=version)
        except ImportError:
            return DependencyInfo(package=package_name, installed=False)

    def check_dependencies(self, service_name: str) -> list[DependencyInfo]:
        svc_def = SERVICE_DEFINITIONS.get(service_name)
        if not svc_def:
            return []
        return [self.check_dependency(dep) for dep in svc_def["dependencies"]]

    def all_dependencies_met(self, service_name: str) -> bool:
        return all(d.installed for d in self.check_dependencies(service_name))

    async def install_dependency(self, package_name: str) -> dict:
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "pip",
                "install",
                package_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode == 0:
                logger.info("依赖安装成功: %s", package_name)
                return {
                    "package": package_name,
                    "success": True,
                    "output": stdout.decode(errors="replace"),
                }
            else:
                logger.error("依赖安装失败: %s - %s", package_name, stderr.decode(errors="replace"))
                return {
                    "package": package_name,
                    "success": False,
                    "error": stderr.decode(errors="replace"),
                }
        except Exception as e:
            logger.error("依赖安装异常: %s - %s", package_name, e)
            return {"package": package_name, "success": False, "error": str(e)}

    async def install_service_dependencies(self, service_name: str) -> dict:
        svc_def = SERVICE_DEFINITIONS.get(service_name)
        if not svc_def:
            return {"error": f"Unknown service: {service_name}"}  # FIXED: 原问题-中文硬编码错误消息

        results = []
        for dep in svc_def["dependencies"]:
            info = self.check_dependency(dep)
            if not info.installed:
                result = await self.install_dependency(dep)
                if result.get("success"):
                    verify = self.check_dependency(dep)
                    if not verify.installed:
                        result = {
                            "package": dep,
                            "success": False,
                            "error": (
                                f"pip安装成功但模块验证失败"
                                f"(pip包:{dep}, 导入名:{_PIP_TO_IMPORT.get(dep, dep)})"
                            ),
                        }
                results.append(result)
            else:
                results.append({"package": dep, "success": True, "skipped": True})

        all_ok = all(r.get("success", False) for r in results)
        return {"service": service_name, "all_installed": all_ok, "results": results}

    def get_service_info(self, service_name: str) -> ServiceInfo:
        svc_def = SERVICE_DEFINITIONS.get(service_name)
        if not svc_def:
            return ServiceInfo(
                name=service_name,
                display_name=service_name,
                description="",
                config_section="",
                state=ServiceState.ERROR,
                error_message=f"Unknown service: {service_name}",  # FIXED: 原问题-中文硬编码错误消息
            )

        config = get_config()
        config_section = svc_def["config_section"]
        section_config = getattr(config, config_section, None)
        enabled = getattr(section_config, "enabled", False) if section_config else False

        deps = self.check_dependencies(service_name)
        all_deps_met = all(d.installed for d in deps)

        instance = self._get_instance(service_name)
        is_running = False
        running_info = {}
        if instance:
            if hasattr(instance, "is_running"):
                is_running = instance.is_running
            elif hasattr(instance, "get_status"):
                try:
                    stats = instance.get_status()
                    is_running = getattr(stats, "running", False)
                    running_info = {
                        "serial_rx_bytes": getattr(stats, "serial_rx_bytes", 0),
                        "serial_tx_bytes": getattr(stats, "serial_tx_bytes", 0),
                        "tcp_rx_bytes": getattr(stats, "tcp_rx_bytes", 0),
                        "tcp_tx_bytes": getattr(stats, "tcp_tx_bytes", 0),
                        "client_count": getattr(stats, "client_count", 0),
                    }
                except Exception as e:
                    logger.debug("获取服务 %s 运行信息失败: %s", service_name, e)
            if is_running and service_name == "mqtt_server":
                try:
                    if hasattr(instance, "get_client_count"):
                        running_info["connections"] = instance.get_client_count()
                    elif hasattr(instance, "_clients"):
                        clients = instance._clients
                        running_info["connections"] = (
                            len(clients) if isinstance(clients, (list, set, dict)) else 0
                        )
                    else:
                        running_info["connections"] = 0
                except Exception:
                    running_info["connections"] = 0
            if is_running and service_name == "serial_bridge":
                try:
                    if hasattr(instance, "get_status"):
                        sb_stats = instance.get_status()
                        running_info["total_connections"] = getattr(
                            sb_stats, "total_connections",
                            getattr(sb_stats, "client_count", 0)
                        )
                except Exception:
                    running_info["total_connections"] = 0

        api_only_services = {"mcp_server", "grafana"}

        if is_running or (service_name in api_only_services and enabled and all_deps_met):
            state = ServiceState.RUNNING
        elif enabled and all_deps_met:
            state = ServiceState.ENABLED
        elif enabled and not all_deps_met:
            state = ServiceState.ERROR
        else:
            state = ServiceState.DISABLED

        current_config = {}
        if section_config:
            current_config = {
                k: getattr(section_config, k) for k in section_config.model_fields if k != "enabled"
            }

        return ServiceInfo(
            name=service_name,
            display_name=svc_def["display_name"],
            description=svc_def["description"],
            config_section=config_section,
            state=state,
            dependencies=deps,
            config_schema=svc_def["config_schema"],
            current_config=current_config,
            running_info=running_info,
            icon=svc_def.get("icon", ""),
            category=svc_def.get("category", "builtin"),
            use_cases=svc_def.get("use_cases", []),
            related_features=svc_def.get("related_features", []),
            setup_guide=svc_def.get("setup_guide", []),
        )

    def list_services(self) -> list[ServiceInfo]:
        return [self.get_service_info(name) for name in SERVICE_DEFINITIONS]

    def _get_instance(self, service_name: str) -> Any:
        app_state = self._get_app_state()
        return getattr(app_state, service_name, None)

    def _set_instance(self, service_name: str, instance: Any) -> None:
        app_state = self._get_app_state()
        if service_name == "serial_bridge":
            app_state.serial_bridge = instance
        else:
            setattr(app_state, service_name, instance)

    async def enable_service(self, service_name: str, config_values: dict | None = None) -> dict:
        svc_def = SERVICE_DEFINITIONS.get(service_name)
        if not svc_def:
            return {"success": False, "error": f"Unknown service: {service_name}"}  # FIXED: 原问题-中文硬编码错误消息

        deps = self.check_dependencies(service_name)
        missing = [d.package for d in deps if not d.installed]
        if missing:
            return {
                "success": False,
                "error": f"Missing dependencies: {', '.join(missing)}",  # FIXED: 原问题-中文硬编码错误消息
                "missing_dependencies": missing,
                "hint": "请先安装依赖后再启用服务",
            }

        config_section = svc_def["config_section"]
        values = {"enabled": True}
        if config_values:
            values.update(config_values)

        try:
            update_config_section(config_section, values)
        except Exception as e:
            logger.error("更新配置失败: %s - %s", config_section, e)
            return {"success": False, "error": f"更新配置失败: {e}"}

        try:
            await self._start_service_instance(service_name, config_values)
        except RuntimeError as e:
            logger.warning("服务启用但启动失败: %s - %s", service_name, e)
            return {
                "success": True,
                "warning": f"服务已启用但启动失败: {e}",
                "error_type": "runtime",
                "detail": str(e),
            }
        except Exception as e:
            logger.error("启动服务失败: %s - %s", service_name, e)
            return {"success": True, "warning": f"服务已启用但启动失败: {e}", "error_type": "runtime", "detail": str(e)}

        return {"success": True, "message": f"{svc_def['display_name']}已启用并启动"}

    async def disable_service(self, service_name: str) -> dict:
        svc_def = SERVICE_DEFINITIONS.get(service_name)
        if not svc_def:
            return {"success": False, "error": f"未知服务: {service_name}"}

        instance = self._get_instance(service_name)
        if instance:
            try:
                await self._stop_service_instance(service_name)
            except Exception as e:
                logger.warning("停止服务异常: %s - %s", service_name, e)

        config_section = svc_def["config_section"]
        try:
            update_config_section(config_section, {"enabled": False})
        except Exception as e:
            logger.error("更新配置失败: %s - %s", config_section, e)
            return {"success": False, "error": f"更新配置失败: {e}"}

        return {"success": True, "message": f"{svc_def['display_name']}已停用"}

    async def start_service(self, service_name: str) -> dict:
        svc_def = SERVICE_DEFINITIONS.get(service_name)
        if not svc_def:
            return {"success": False, "error": f"未知服务: {service_name}"}

        instance = self._get_instance(service_name)
        if instance and hasattr(instance, "is_running") and instance.is_running:
            return {"success": True, "message": "服务已在运行中"}

        if not self.all_dependencies_met(service_name):
            return {"success": False, "error": "缺少依赖，请先安装依赖"}

        try:
            await self._start_service_instance(service_name)
            return {"success": True, "message": f"{svc_def['display_name']}已启动"}
        except RuntimeError as e:
            return {"success": False, "error": str(e), "error_type": "runtime"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def stop_service(self, service_name: str) -> dict:
        svc_def = SERVICE_DEFINITIONS.get(service_name)
        if not svc_def:
            return {"success": False, "error": f"未知服务: {service_name}"}

        instance = self._get_instance(service_name)
        if not instance:
            return {"success": True, "message": "服务未运行"}

        try:
            await self._stop_service_instance(service_name)
            return {"success": True, "message": f"{svc_def['display_name']}已停止"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _start_service_instance(
        self, service_name: str, config_values: dict | None = None
    ) -> None:
        svc_def = SERVICE_DEFINITIONS[service_name]
        svc_def["engine_class"]

        if service_name == "mcp_server":
            return

        if service_name == "grafana":
            return

        config = get_config()
        section_config = getattr(config, svc_def["config_section"], None)

        if service_name == "mqtt_server":
            from edgelite.engine.mqtt_server import MqttServer

            instance = MqttServer()
            start_config = {
                "host": getattr(section_config, "host", "0.0.0.0"),
                "port": getattr(section_config, "port", 1888),
                "ws_port": getattr(section_config, "ws_port", None),
                "username": getattr(section_config, "username", ""),
                "password": getattr(section_config, "password", ""),
            }
            if config_values:
                start_config.update(config_values)
            await instance.start(start_config)
            self._set_instance(service_name, instance)

        elif service_name == "modbus_slave":
            from edgelite.engine.modbus_slave import ModbusSlaveServer

            instance = ModbusSlaveServer()
            start_config = {
                "host": getattr(section_config, "host", "0.0.0.0"),
                "port": getattr(section_config, "port", 502),
                "holding_size": getattr(section_config, "holding_size", 1000),
                "input_size": getattr(section_config, "input_size", 1000),
            }
            if config_values:
                start_config.update(config_values)
            await instance.start(start_config)
            self._set_instance(service_name, instance)

        elif service_name == "serial_bridge":
            from edgelite.engine.serial_bridge import SerialTcpBridge

            instance = SerialTcpBridge()
            start_config = {
                "serial_port": getattr(section_config, "serial_port", "/dev/ttyUSB0"),
                "baudrate": getattr(section_config, "baud_rate", 9600),
                "tcp_port": getattr(section_config, "tcp_port", 9000),
                "allowed_ips": getattr(section_config, "ip_whitelist", []),
            }
            if config_values:
                start_config.update(config_values)
            await instance.start(start_config)
            self._set_instance(service_name, instance)

    async def _stop_service_instance(self, service_name: str) -> None:
        instance = self._get_instance(service_name)
        if not instance:
            return

        if hasattr(instance, "stop"):
            await instance.stop()

        self._set_instance(service_name, None)

    async def update_service_config(self, service_name: str, config_values: dict) -> dict:
        svc_def = SERVICE_DEFINITIONS.get(service_name)
        if not svc_def:
            return {"success": False, "error": f"未知服务: {service_name}"}

        config_section = svc_def["config_section"]
        try:
            update_config_section(config_section, config_values)
        except Exception as e:
            return {"success": False, "error": f"更新配置失败: {e}"}

        instance = self._get_instance(service_name)
        if instance and hasattr(instance, "is_running") and instance.is_running:
            try:
                await self._stop_service_instance(service_name)
                await self._start_service_instance(service_name)
                return {"success": True, "message": "配置已更新，服务已重启"}
            except Exception as e:
                return {"success": True, "warning": f"配置已更新但服务重启失败: {e}"}

        return {"success": True, "message": "配置已更新"}


_service_manager: ServiceManager | None = None


def get_service_manager() -> ServiceManager:
    global _service_manager
    if _service_manager is None:
        _service_manager = ServiceManager()
    return _service_manager
