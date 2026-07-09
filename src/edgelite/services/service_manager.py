"""服务管理器 - 统一管理所有可选服务的生命周期、依赖检查和动态启停"""

from __future__ import annotations

import asyncio
import contextlib  # FIXED-P1: install_dependency 超时处理需要 contextlib.suppress
import importlib
import logging
import platform
import sys
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from edgelite.config import get_config, update_config_section

logger = logging.getLogger(__name__)


class ServiceErrors(StrEnum):
    START_FAILED = "ERR_SVC_START_FAILED"
    UNKNOWN_SERVICE = "ERR_SVC_UNKNOWN_SERVICE"
    DEPS_INSTALL_FAILED = "ERR_SVC_DEPS_INSTALL_FAILED"
    ALREADY_RUNNING = "ERR_SVC_ALREADY_RUNNING"
    NOT_RUNNING = "ERR_SVC_NOT_RUNNING"
    CONFIG_UPDATE_FAILED = "ERR_SVC_CONFIG_UPDATE_FAILED"
    DEPS_MISSING = "ERR_SVC_DEPS_MISSING"
    PIP_VERIFY_FAILED = "ERR_SVC_PIP_VERIFY_FAILED"
    SERVICE_ENABLED_STARTED = "ERR_SVC_ENABLED_STARTED"
    SERVICE_DISABLED = "ERR_SVC_DISABLED"
    SERVICE_STARTED = "ERR_SVC_STARTED"
    SERVICE_STOPPED = "ERR_SVC_STOPPED"
    CONFIG_UPDATED_RESTARTED = "ERR_SVC_CONFIG_UPDATED_RESTARTED"
    CONFIG_UPDATED_RESTART_FAILED = "ERR_SVC_CONFIG_UPDATED_RESTART_FAILED"
    CONFIG_UPDATED = "ERR_SVC_CONFIG_UPDATED"


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
    "pyserial-asyncio": "serial_asyncio",
    "onvif-zeep": "onvif",
    "onvif-zeep-async": "onvif",
    "pymodbus": "pymodbus",
    "pymcprotocol": "pymcprotocol",
    "pylogix": "pylogix",
    "snap7": "snap7",
    "asyncua": "asyncua",
    "fins": "fins",
}

SERVICE_DEFINITIONS = {
    "mqtt_server": {
        "display_name": "SVC_MQTT_SERVER_DISPLAY_NAME",
        "description": "SVC_MQTT_SERVER_DESCRIPTION",
        "icon": "radio",
        "category": "builtin",
        "config_section": "mqtt_server",
        "dependencies": ["amqtt"],
        "engine_class": "edgelite.engine.mqtt_server.MqttServer",
        "use_cases": [
            "SVC_MQTT_SERVER_USE_CASE_1",
            "SVC_MQTT_SERVER_USE_CASE_2",
            "SVC_MQTT_SERVER_USE_CASE_3",
        ],
        "related_features": [
            {"name": "SVC_MQTT_SERVER_RELATED_1_NAME", "route": "Devices", "hint": "SVC_MQTT_SERVER_RELATED_1_HINT"},
            {"name": "SVC_MQTT_SERVER_RELATED_2_NAME", "route": "PlatformConfig", "hint": "SVC_MQTT_SERVER_RELATED_2_HINT"},
        ],
        "setup_guide": [
            "SVC_MQTT_SERVER_GUIDE_1",
            "SVC_MQTT_SERVER_GUIDE_2",
            "SVC_MQTT_SERVER_GUIDE_3",
            "SVC_MQTT_SERVER_GUIDE_4",
        ],
        "config_schema": {
            "host": {
                "type": "string",
                "default": "127.0.0.1",  # FIXED-P3: 默认值改为localhost，与引擎层一致
                "label": "SVC_MQTT_SERVER_HOST_LABEL",
                "description": "SVC_MQTT_SERVER_HOST_DESC",
            },
            "port": {
                "type": "integer",
                "default": 1888,
                "label": "SVC_MQTT_SERVER_PORT_LABEL",
                "description": "SVC_MQTT_SERVER_PORT_DESC",
            },
            "ws_port": {
                "type": "integer",
                "default": 8083,
                "label": "SVC_MQTT_SERVER_WS_PORT_LABEL",
                "description": "SVC_MQTT_SERVER_WS_PORT_DESC",
                "nullable": True,
            },
            "username": {
                "type": "string",
                "default": "",
                "label": "SVC_MQTT_SERVER_USERNAME_LABEL",
                "description": "SVC_MQTT_SERVER_USERNAME_DESC",
            },
            "password": {
                "type": "string",
                "default": "",
                "label": "SVC_MQTT_SERVER_PASSWORD_LABEL",
                "description": "SVC_MQTT_SERVER_PASSWORD_DESC",
            },
        },
    },
    "modbus_slave": {
        "display_name": "SVC_MODBUS_SLAVE_DISPLAY_NAME",
        "description": "SVC_MODBUS_SLAVE_DESCRIPTION",
        "icon": "power",
        "category": "builtin",
        "config_section": "modbus_slave",
        "dependencies": ["pymodbus"],
        "engine_class": "edgelite.engine.modbus_slave.ModbusSlaveServer",
        "use_cases": [
            "SVC_MODBUS_SLAVE_USE_CASE_1",
            "SVC_MODBUS_SLAVE_USE_CASE_2",
            "SVC_MODBUS_SLAVE_USE_CASE_3",
        ],
        "related_features": [
            {"name": "SVC_MODBUS_SLAVE_RELATED_1_NAME", "route": "Devices", "hint": "SVC_MODBUS_SLAVE_RELATED_1_HINT"},
            {"name": "SVC_MODBUS_SLAVE_RELATED_2_NAME", "route": "DataQuery", "hint": "SVC_MODBUS_SLAVE_RELATED_2_HINT"},
        ],
        "setup_guide": [
            "SVC_MODBUS_SLAVE_GUIDE_1",
            "SVC_MODBUS_SLAVE_GUIDE_2",
            "SVC_MODBUS_SLAVE_GUIDE_3",
            "SVC_MODBUS_SLAVE_GUIDE_4",
        ],
        "config_schema": {
            "host": {
                "type": "string",
                "default": "127.0.0.1",  # FIXED-P3: 默认值改为localhost，与引擎层一致
                "label": "SVC_MODBUS_SLAVE_HOST_LABEL",
                "description": "SVC_MODBUS_SLAVE_HOST_DESC",
            },
            "port": {
                "type": "integer",
                "default": 502,
                "label": "SVC_MODBUS_SLAVE_PORT_LABEL",
                "description": "SVC_MODBUS_SLAVE_PORT_DESC",
            },
            "holding_size": {
                "type": "integer",
                "default": 1000,
                "label": "SVC_MODBUS_SLAVE_HOLDING_LABEL",
                "description": "SVC_MODBUS_SLAVE_HOLDING_DESC",
            },
            "input_size": {
                "type": "integer",
                "default": 1000,
                "label": "SVC_MODBUS_SLAVE_INPUT_LABEL",
                "description": "SVC_MODBUS_SLAVE_INPUT_DESC",
            },
        },
    },
    "serial_bridge": {
        "display_name": "SVC_SERIAL_BRIDGE_DISPLAY_NAME",
        "description": "SVC_SERIAL_BRIDGE_DESCRIPTION",
        "icon": "swap",
        "category": "builtin",
        "config_section": "serial_bridge",
        "dependencies": ["pyserial", "pyserial-asyncio"],
        "engine_class": "edgelite.engine.serial_bridge.SerialTcpBridge",
        "use_cases": [
            "SVC_SERIAL_BRIDGE_USE_CASE_1",
            "SVC_SERIAL_BRIDGE_USE_CASE_2",
            "SVC_SERIAL_BRIDGE_USE_CASE_3",
        ],
        "related_features": [
            {"name": "SVC_SERIAL_BRIDGE_RELATED_1_NAME", "route": "Devices", "hint": "SVC_SERIAL_BRIDGE_RELATED_1_HINT"},
            {"name": "SVC_SERIAL_BRIDGE_RELATED_2_NAME", "route": "DriverConfig", "hint": "SVC_SERIAL_BRIDGE_RELATED_2_HINT"},
        ],
        "setup_guide": [
            "SVC_SERIAL_BRIDGE_GUIDE_1",
            "SVC_SERIAL_BRIDGE_GUIDE_2",
            "SVC_SERIAL_BRIDGE_GUIDE_3",
            "SVC_SERIAL_BRIDGE_GUIDE_4",
            "SVC_SERIAL_BRIDGE_GUIDE_5",
        ],
        "config_schema": {
            "serial_port": {
                "type": "string",
                "default": "COM1" if platform.system() == "Windows" else "/dev/ttyUSB0",
                "label": "SVC_SERIAL_BRIDGE_SERIAL_PORT_LABEL",
                "description": "SVC_SERIAL_BRIDGE_SERIAL_PORT_DESC",
            },
            "baud_rate": {
                "type": "integer",
                "default": 9600,
                "label": "SVC_SERIAL_BRIDGE_BAUD_RATE_LABEL",
                "description": "SVC_SERIAL_BRIDGE_BAUD_RATE_DESC",
            },
            "tcp_port": {
                "type": "integer",
                "default": 9000,
                "label": "SVC_SERIAL_BRIDGE_TCP_PORT_LABEL",
                "description": "SVC_SERIAL_BRIDGE_TCP_PORT_DESC",
            },
            "ip_whitelist": {
                "type": "array",
                "default": [],
                "label": "SVC_SERIAL_BRIDGE_IP_WHITELIST_LABEL",
                "description": "SVC_SERIAL_BRIDGE_IP_WHITELIST_DESC",
            },
            "max_clients": {
                "type": "integer",
                "default": 5,
                "label": "SVC_SERIAL_BRIDGE_MAX_CLIENTS_LABEL",
                "description": "SVC_SERIAL_BRIDGE_MAX_CLIENTS_DESC",
            },
        },
    },
    "mcp_server": {
        "display_name": "SVC_MCP_SERVER_DISPLAY_NAME",
        "description": "SVC_MCP_SERVER_DESCRIPTION",
        "icon": "puzzle",
        "category": "integration",
        "config_section": "mcp_server",
        "dependencies": [],
        "engine_class": None,
        "use_cases": [
            "SVC_MCP_SERVER_USE_CASE_1",
            "SVC_MCP_SERVER_USE_CASE_2",
            "SVC_MCP_SERVER_USE_CASE_3",
        ],
        "related_features": [
            {"name": "SVC_MCP_SERVER_RELATED_1_NAME", "route": "Devices", "hint": "SVC_MCP_SERVER_RELATED_1_HINT"},
            {"name": "SVC_MCP_SERVER_RELATED_2_NAME", "route": "Alarms", "hint": "SVC_MCP_SERVER_RELATED_2_HINT"},
            {"name": "SVC_MCP_SERVER_RELATED_3_NAME", "route": "Rules", "hint": "SVC_MCP_SERVER_RELATED_3_HINT"},
        ],
        "setup_guide": [
            "SVC_MCP_SERVER_GUIDE_1",
            "SVC_MCP_SERVER_GUIDE_2",
            "SVC_MCP_SERVER_GUIDE_3",
            "SVC_MCP_SERVER_GUIDE_4",
        ],
        "config_schema": {},
    },
    "grafana": {
        "display_name": "SVC_GRAFANA_DISPLAY_NAME",
        "description": "SVC_GRAFANA_DESCRIPTION",
        "icon": "chart",
        "category": "integration",
        "config_section": "grafana",
        "dependencies": ["httpx"],
        "engine_class": None,
        "use_cases": [
            "SVC_GRAFANA_USE_CASE_1",
            "SVC_GRAFANA_USE_CASE_2",
            "SVC_GRAFANA_USE_CASE_3",
        ],
        "related_features": [
            {"name": "SVC_GRAFANA_RELATED_1_NAME", "route": "DataQuery", "hint": "SVC_GRAFANA_RELATED_1_HINT"},
            {"name": "SVC_GRAFANA_RELATED_2_NAME", "route": "System", "hint": "SVC_GRAFANA_RELATED_2_HINT"},
        ],
        "setup_guide": [
            "SVC_GRAFANA_GUIDE_1",
            "SVC_GRAFANA_GUIDE_2",
            "SVC_GRAFANA_GUIDE_3",
            "SVC_GRAFANA_GUIDE_4",
            "SVC_GRAFANA_GUIDE_5",
        ],
        "config_schema": {
            "url": {
                "type": "string",
                "default": "http://localhost:3001",
                "label": "SVC_GRAFANA_URL_LABEL",
                "description": "SVC_GRAFANA_URL_DESC",
            },
            "api_key": {
                "type": "string",
                "default": "",
                "label": "SVC_GRAFANA_API_KEY_LABEL",
                "description": "SVC_GRAFANA_API_KEY_DESC",
            },
            "datasource": {
                "type": "string",
                "default": "InfluxDB",
                "label": "SVC_GRAFANA_DATASOURCE_LABEL",
                "description": "SVC_GRAFANA_DATASOURCE_DESC",
            },
        },
    },
}


class ServiceManager:
    """统一服务管理器"""

    def __init__(self):
        self._instances: dict[str, Any] = {}
        self._install_tasks: dict[str, asyncio.Task] = {}
        # FIX-P1: 串行化 start_service/stop_service/update_service_config，
        # 避免 start_service 的 is_running 检查与 _start_service_instance 之间
        # 的 TOCTOU 竞态导致重复启动或状态不一致。
        self._op_lock = asyncio.Lock()

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
        # FIXED-P1: 原代码无超时控制，pip install 可能长时间运行（网络问题/编译依赖）
        # 添加 300 秒超时，避免无限等待
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
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
            except TimeoutError:
                proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
                logger.error("Dependency install timed out (300s): %s", package_name)
                return {
                    "package": package_name,
                    "success": False,
                    "error": "Installation timed out after 300 seconds",
                }
            if proc.returncode == 0:
                logger.info("Dependency installed: %s", package_name)
                return {
                    "package": package_name,
                    "success": True,
                    "output": stdout.decode(errors="replace"),
                }
            else:
                logger.error("Dependency install failed: %s - %s", package_name, stderr.decode(errors="replace"))
                return {
                    "package": package_name,
                    "success": False,
                    "error": stderr.decode(errors="replace"),
                }
        except Exception as e:
            logger.error("Dependency install exception: %s - %s", package_name, e)
            return {"package": package_name, "success": False, "error": str(e)}

    async def install_service_dependencies(self, service_name: str) -> dict:
        svc_def = SERVICE_DEFINITIONS.get(service_name)
        if not svc_def:
            return {"error": ServiceErrors.UNKNOWN_SERVICE, "detail": service_name}

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
                            "error": ServiceErrors.PIP_VERIFY_FAILED,
                            "detail": f"pip:{dep}, import:{_PIP_TO_IMPORT.get(dep, dep)}",
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
                error_message=ServiceErrors.UNKNOWN_SERVICE,
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
                    logger.debug("Failed to get service %s running info: %s", service_name, e)
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
                except Exception as e:  # FIXED: 原问题-获取串口桥接统计异常静默置零，无日志
                    logger.debug("Failed to get serial bridge stats: %s", e)
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
            return {"success": False, "error": ServiceErrors.UNKNOWN_SERVICE, "detail": service_name}

        deps = self.check_dependencies(service_name)
        missing = [d.package for d in deps if not d.installed]
        if missing:
            return {
                "success": False,
                "error": ServiceErrors.DEPS_MISSING,
                "missing_dependencies": missing,
            }

        config_section = svc_def["config_section"]
        values = {"enabled": True}
        if config_values:
            values.update(config_values)

        try:
            update_config_section(config_section, values)
        except Exception as e:
            logger.error("Config update failed: %s - %s", config_section, e)
            return {"success": False, "error": ServiceErrors.CONFIG_UPDATE_FAILED, "detail": str(e)}

        try:
            await self._start_service_instance(service_name, config_values)
        except RuntimeError as e:
            logger.warning("Service enabled but start failed: %s - %s", service_name, e)
            update_config_section(config_section, {"enabled": False})
            return {
                "success": False,
                "error": ServiceErrors.START_FAILED,
                "error_type": "runtime",
                "detail": str(e),
                "hint": self._get_start_error_hint(service_name, e),
            }
        except Exception as e:
            logger.error("Service start failed: %s - %s", service_name, e)
            update_config_section(config_section, {"enabled": False})
            return {
                "success": False,
                "error": ServiceErrors.START_FAILED,
                "error_type": "runtime",
                "detail": str(e),
                "hint": self._get_start_error_hint(service_name, e),
            }

        return {"success": True, "message": ServiceErrors.SERVICE_ENABLED_STARTED}

    async def disable_service(self, service_name: str) -> dict:
        svc_def = SERVICE_DEFINITIONS.get(service_name)
        if not svc_def:
            return {"success": False, "error": ServiceErrors.UNKNOWN_SERVICE}

        instance = self._get_instance(service_name)
        if instance:
            try:
                await self._stop_service_instance(service_name)
            except Exception as e:
                logger.warning("Service stop exception: %s - %s", service_name, e)

        config_section = svc_def["config_section"]
        try:
            update_config_section(config_section, {"enabled": False})
        except Exception as e:
            logger.error("Config update failed: %s - %s", config_section, e)
            return {"success": False, "error": ServiceErrors.CONFIG_UPDATE_FAILED, "detail": str(e)}

        return {"success": True, "message": ServiceErrors.SERVICE_DISABLED}

    async def start_service(self, service_name: str) -> dict:
        # FIX-P1: 使用 _op_lock 串行化，避免 is_running 检查与 _start_service_instance
        # 之间的 TOCTOU 竞态（并发调用可能重复启动同一服务）。
        async with self._op_lock:
            svc_def = SERVICE_DEFINITIONS.get(service_name)
            if not svc_def:
                return {"success": False, "error": ServiceErrors.UNKNOWN_SERVICE}

            instance = self._get_instance(service_name)
            if instance and hasattr(instance, "is_running") and instance.is_running:
                return {"success": True, "message": ServiceErrors.ALREADY_RUNNING}

            if not self.all_dependencies_met(service_name):
                return {"success": False, "error": ServiceErrors.DEPS_INSTALL_FAILED}

            try:
                await self._start_service_instance(service_name)
                return {"success": True, "message": ServiceErrors.SERVICE_STARTED}
            except RuntimeError as e:
                return {"success": False, "error": ServiceErrors.START_FAILED, "error_type": "runtime", "detail": str(e), "hint": self._get_start_error_hint(service_name, e)}
            except Exception as e:
                return {"success": False, "error": ServiceErrors.START_FAILED, "detail": str(e), "hint": self._get_start_error_hint(service_name, e)}

    async def stop_service(self, service_name: str) -> dict:
        # FIX-P1: 使用 _op_lock 串行化，与 start_service/update_service_config 互斥，
        # 避免 stop 与 start 并发导致状态不一致。
        async with self._op_lock:
            svc_def = SERVICE_DEFINITIONS.get(service_name)
            if not svc_def:
                return {"success": False, "error": ServiceErrors.UNKNOWN_SERVICE}

            instance = self._get_instance(service_name)
            if not instance:
                return {"success": True, "message": ServiceErrors.NOT_RUNNING}

            try:
                await self._stop_service_instance(service_name)
                return {"success": True, "message": ServiceErrors.SERVICE_STOPPED}
            except Exception as e:
                return {"success": False, "error": str(e)}

    def _get_start_error_hint(self, service_name: str, error: Exception) -> str:
        err_msg = str(error).lower()
        hints = {
            "mqtt_server": {
                "address already in use": "ERR_SVC_HINT_MQTT_PORT_IN_USE",
                "permission denied": "ERR_SVC_HINT_MQTT_PORT_PERMISSION",
            },
            "modbus_slave": {
                "address already in use": "ERR_SVC_HINT_MODBUS_PORT_IN_USE",
                "permission denied": "ERR_SVC_HINT_MODBUS_PORT_PERMISSION",
                "'break' outside loop": "ERR_SVC_HINT_CODE_SYNTAX_ERROR",
            },
            "serial_bridge": {
                "could not open port": "ERR_SVC_HINT_SERIAL_NOT_FOUND",
                "permission denied": "ERR_SVC_HINT_SERIAL_PERMISSION",
                "permission": "ERR_SVC_HINT_SERIAL_PERMISSION",
                "file not found": "ERR_SVC_HINT_SERIAL_PATH_NOT_FOUND",
                "不存在": "ERR_SVC_HINT_SERIAL_PATH_NOT_FOUND",
                "does not exist": "ERR_SVC_HINT_SERIAL_PATH_NOT_FOUND",
                "no such file": "ERR_SVC_HINT_SERIAL_PATH_NOT_FOUND",
                "已被占用": "ERR_SVC_HINT_SERIAL_NOT_FOUND",
                "already in use": "ERR_SVC_HINT_SERIAL_NOT_FOUND",
            },
        }
        service_hints = hints.get(service_name, {})
        for pattern, hint in service_hints.items():
            if pattern in err_msg:
                return hint
        if "address already in use" in err_msg:
            return "ERR_SVC_HINT_PORT_IN_USE"
        if "permission denied" in err_msg:
            return "ERR_SVC_HINT_PERMISSION_DENIED"
        if "connection refused" in err_msg:
            return "ERR_SVC_HINT_CONNECTION_REFUSED"
        if "timeout" in err_msg:
            return "ERR_SVC_HINT_TIMEOUT"
        return "ERR_SVC_HINT_CHECK_CONFIG"

    async def _start_service_instance(
        self, service_name: str, config_values: dict | None = None
    ) -> None:
        svc_def = SERVICE_DEFINITIONS[service_name]
        # FIXED-P0: 原代码第664行 `svc_def["engine_class"]` 是无效语句（无副作用），已删除

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
                "host": getattr(section_config, "host", "127.0.0.1"),  # FIXED-P2: fallback改为localhost
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
                "host": getattr(section_config, "host", "127.0.0.1"),  # FIXED-P2: fallback改为localhost
                "port": getattr(section_config, "port", 5020),
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

        # FIXED-P0: 原代码无异常处理，instance.stop() 抛出异常时
        # self._set_instance(service_name, None) 不会执行，实例残留导致状态不一致
        try:
            if hasattr(instance, "stop"):
                await instance.stop()
        except Exception as e:
            logger.warning("Service %s stop raised exception: %s", service_name, e)
        finally:
            self._set_instance(service_name, None)

    async def update_service_config(self, service_name: str, config_values: dict) -> dict:
        # FIX-P1: 使用 _op_lock 串行化，与 start_service/stop_service 互斥，
        # 避免配置更新时的 stop+start 与并发 start/stop 交错导致状态不一致。
        async with self._op_lock:
            svc_def = SERVICE_DEFINITIONS.get(service_name)
            if not svc_def:
                return {"success": False, "error": ServiceErrors.UNKNOWN_SERVICE}

            config_section = svc_def["config_section"]
            try:
                update_config_section(config_section, config_values)
            except Exception as e:
                return {"success": False, "error": ServiceErrors.CONFIG_UPDATE_FAILED, "detail": str(e)}

            instance = self._get_instance(service_name)
            if instance and hasattr(instance, "is_running") and instance.is_running:
                try:
                    await self._stop_service_instance(service_name)
                    await self._start_service_instance(service_name)
                    return {"success": True, "message": ServiceErrors.CONFIG_UPDATED_RESTARTED}
                except Exception as e:
                    # FIXED-P1: 原代码返回 success=True 误导调用者，实际服务已停止
                    # 改为返回 success=False 明确表示重启失败
                    return {
                        "success": False,
                        "error": ServiceErrors.CONFIG_UPDATED_RESTART_FAILED,
                        "detail": str(e),
                        "hint": self._get_start_error_hint(service_name, e),
                    }

            return {"success": True, "message": ServiceErrors.CONFIG_UPDATED}


_service_manager: ServiceManager | None = None


def get_service_manager() -> ServiceManager:
    global _service_manager
    if _service_manager is None:
        _service_manager = ServiceManager()
    return _service_manager
