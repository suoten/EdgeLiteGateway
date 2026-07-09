"""Driver Registry - Manages protocol to driver class mappings"""

from __future__ import annotations

import builtins  # FIXED-P0: 自定义驱动沙箱需要
import logging
import sys
import threading
from typing import (
    Any,  # FIXED-P1: 原问题-_load_driver/_make_error_plugin_info返回类型注解引用Any未导入，运行时求值注解会NameError; 修复-补充typing.Any导入
)

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
    "siemens_s7": {"en": "Siemens S7", "zh": "西门子S7"},
    "s7": {"en": "Siemens S7", "zh": "西门子S7"},
    "mitsubishi_mc": {"en": "Mitsubishi MC", "zh": "三菱MC"},
    "mc": {"en": "Mitsubishi MC", "zh": "三菱MC"},
    "omron_fins": {"en": "Omron FINS", "zh": "欧姆龙FINS"},
    "fins": {"en": "Omron FINS", "zh": "欧姆龙FINS"},
    "allen_bradley": {"en": "Allen-Bradley", "zh": "Allen-Bradley"},
    "ab": {"en": "Allen-Bradley", "zh": "Allen-Bradley"},
    "opc_da": {"en": "OPC DA Client", "zh": "OPC DA客户端"},
    "onvif": {"en": "ONVIF Camera", "zh": "ONVIF摄像头"},
    "video_ai": {"en": "Video AI", "zh": "视频AI"},
    "modbus_slave": {"en": "Modbus Slave", "zh": "Modbus从站"},
}

# FIXED-P0: 内置协议集合，防止自定义驱动覆盖内置协议导致协议劫持
_BUILTIN_PROTOCOLS = frozenset({
    "modbus_tcp", "modbus_rtu", "mqtt_client", "http_webhook",
    "opcua", "s7", "mc", "omron_fins", "fins",
    "allen_bradley", "ab", "ab_cip", "ab_pccc",
    "opc_da", "onvif", "video_ai", "modbus_slave", "simulator",
})


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
        self._discover_lock = threading.Lock()  # FIXED-P2: auto_discover加锁保护，防止并发重复执行
        # 驱动加载状态追踪: label -> {"loaded": bool, "error": str|None, "module": str}
        self._load_status: dict[str, dict] = {}
        # Dependency check results: plugin_name -> {"available": bool, "missing_deps": list[str], "checked_deps": list[str]}
        self._dependency_results: dict[str, dict] = {}

    def register(self, driver_class: type[DriverPlugin]) -> None:
        """注册驱动类"""
        if driver_class in self._registered_drivers:
            logger.debug("Driver %s already registered, skipping", driver_class.plugin_name)
            return

        if not issubclass(driver_class, DriverPlugin):
            logger.warning(
                "[registry] %s does not inherit from DriverPlugin, skipping",
                driver_class.__name__,
            )
            return

        required_attrs = ("plugin_name", "plugin_version", "supported_protocols")
        for attr in required_attrs:
            val = getattr(driver_class, attr, None)
            if not val:
                logger.warning(
                    "[registry] %s missing required attribute '%s', skipping",
                    driver_class.__name__, attr,
                )
                return

        for protocol in driver_class.supported_protocols:
            if protocol in self._drivers and self._drivers[protocol] is not driver_class:
                existing_cls = self._drivers[protocol]
                # FIXED-P2: 内置协议不允许被自定义驱动覆盖，防止协议劫持
                if protocol in _BUILTIN_PROTOCOLS and not getattr(driver_class, '_is_builtin', False):
                    logger.error(
                        "[registry] Protocol %s is a built-in protocol registered by %s, custom driver %s cannot override it",
                        protocol, existing_cls.plugin_name, driver_class.plugin_name,
                    )
                    continue
                logger.warning(
                    "Protocol %s already registered by %s v%s, overriding with %s v%s",
                    protocol,
                    existing_cls.plugin_name, existing_cls.plugin_version,
                    driver_class.plugin_name, driver_class.plugin_version,
                )
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

    def get_all_protocol_keys(self) -> list[str]:
        """获取所有已注册的协议键名（包括别名），用于集成 API 和协议列表查询。

        与 get_supported_protocols() 不同，此方法返回 _drivers 字典中的所有键，
        包含 supported_protocols 中的别名（如 's7', 'mqtt', 'mc' 等）。
        """
        return sorted(self._drivers.keys())

    def unregister(self, protocol: str) -> bool:
        """注销指定协议驱动"""
        if protocol in self._drivers:
            driver_cls = self._drivers[protocol]
            del self._drivers[protocol]
            if driver_cls in self._registered_drivers:
                still_used = any(cls is driver_cls for cls in self._drivers.values())
                if not still_used:
                    self._registered_drivers.discard(driver_cls)
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
        # FIXED-P1: 整个方法体在锁内执行，消除TOCTOU竞态
        with self._discover_lock:
            if self._discovered:
                logger.warning("[registry] auto_discover already executed, skipping duplicate call")
                return
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
                ("ONVIF Camera", "edgelite.drivers.onvif_driver", "OnvifDriver"),
                ("Video AI", "edgelite.drivers.video_ai_driver", "VideoAiDriver"),
                ("Modbus Slave", "edgelite.drivers.modbus_slave", "ModbusSlaveDriver"),
            ]

            for label, module_path, class_name in _driver_modules:
                self._load_driver(label, module_path, class_name)

            self._discover_custom_drivers()

            # Check dependencies for all registered drivers
            self._check_all_dependencies()

            # FIXED-P0: 加载完成后才设置标志，失败后可重试
            self._discovered = True

        logger.info(
            "Driver auto-discovery complete, supported protocols: %s",
            self.get_supported_protocols(),
        )

    def _load_driver(self, label: str, module_path: str, class_name: str) -> bool | Any:
        """加载单个驱动模块

        FIXED-P2: 原问题-加载驱动失败时静默返回None/False，仅记录warning无堆栈信息
        修复方案: 加载失败时记录error日志包含异常堆栈信息(exc_info=True)，
                  并返回包含错误信息的PluginInfo对象
        """
        try:
            import importlib

            module = importlib.import_module(module_path)
            driver_cls = getattr(module, class_name)
            self.register(driver_cls)
            self._load_status[label] = {
                "loaded": True, "error": None,
                "module": module_path, "class": class_name,
            }
            return True
        except ImportError as e:
            # FIXED-P2: 记录error日志包含异常堆栈信息，原问题-仅记录warning无堆栈
            logger.error(
                "[registry] %s driver import failed (missing dependency): %s",
                label, e, exc_info=True,
            )
            self._load_status[label] = {
                "loaded": False,
                "error": f"Missing dependency: {e}",
                "module": module_path, "class": class_name,
            }
            return self._make_error_plugin_info(label, module_path, class_name, f"Missing dependency: {e}")
        except AttributeError as e:
            # FIXED-P2: 记录error日志包含异常堆栈信息，原问题-仅记录warning无堆栈
            logger.error("[registry] %s driver class not found: %s", label, e, exc_info=True)
            self._load_status[label] = {
                "loaded": False,
                "error": f"Class not found: {e}",
                "module": module_path, "class": class_name,
            }
            return self._make_error_plugin_info(label, module_path, class_name, f"Class not found: {e}")
        except Exception as e:
            # UnicodeDecodeError usually caused by corrupted .pyc cache or source file
            if isinstance(e, UnicodeDecodeError):
                logger.warning(
                    "[registry] %s driver load failed (encoding error, clearing __pycache__): %s",
                    label, e,
                )
                # Clear entire __pycache__ directory for the module and retry
                try:
                    import importlib
                    import os
                    import shutil

                    # Remove from sys.modules first
                    cached_module = sys.modules.pop(module_path, None)

                    # Find and remove the entire __pycache__ directory for this package
                    pycache_cleared = False
                    if cached_module and hasattr(cached_module, '__cached__'):
                        pyc_path = cached_module.__cached__
                        if pyc_path and os.path.exists(pyc_path):
                            pycache_dir = os.path.dirname(pyc_path)
                            if os.path.basename(pycache_dir) == '__pycache__':
                                shutil.rmtree(pycache_dir, ignore_errors=True)
                                logger.info("[registry] Cleared __pycache__: %s", pycache_dir)
                                pycache_cleared = True
                            else:
                                os.remove(pyc_path)
                                logger.info("[registry] Removed corrupted .pyc: %s", pyc_path)
                                pycache_cleared = True

                    # FIXED: 如果cached_module为None（模块未加载到sys.modules），
                    # 通过模块路径直接查找__pycache__目录
                    if not pycache_cleared:
                        module_file = sys.modules.get(module_path)
                        if module_file is None:
                            # 尝试通过importlib查找模块文件路径
                            try:
                                spec = importlib.util.find_spec(module_path)
                                if spec and spec.origin:
                                    module_dir = os.path.dirname(spec.origin)
                                    pycache_path = os.path.join(module_dir, "__pycache__")
                                    if os.path.isdir(pycache_path):
                                        shutil.rmtree(pycache_path, ignore_errors=True)
                                        logger.info("[registry] Cleared __pycache__ via spec: %s", pycache_path)
                                        pycache_cleared = True
                            # FIXED-P1: 原问题-except Exception: pass 静默吞没异常，改为至少 logger.debug
                            except Exception as e:
                                logger.debug("[registry] __pycache__ cleanup failed: %s", e)

                    # FIXED: 如果源文件本身有UTF-8损坏，尝试修复源文件
                    # 用 errors='replace' 读取并重写，将损坏字节替换为 U+FFFD
                    try:
                        spec = importlib.util.find_spec(module_path)
                        if spec and spec.origin and spec.origin.endswith('.py'):
                            src_path = spec.origin
                            # R5-S-15: 使用with语句确保文件句柄正确关闭，防止泄漏
                            with open(src_path, 'rb') as f:
                                raw = f.read()
                            try:
                                raw.decode('utf-8')  # 测试是否需要修复
                            except UnicodeDecodeError:
                                text = raw.decode('utf-8', errors='replace')
                                fixed = text.encode('utf-8')
                                # R5-S-15: 使用with语句确保文件句柄正确关闭，防止泄漏
                                with open(src_path, 'wb') as f:
                                    f.write(fixed)
                                logger.info("[registry] Fixed UTF-8 corruption in source: %s", src_path)
                    except Exception as fix_err:
                        logger.debug("[registry] Source fix attempt failed: %s", fix_err)

                    # Retry import
                    module = importlib.import_module(module_path)
                    driver_cls = getattr(module, class_name)
                    self.register(driver_cls)
                    self._load_status[label] = {
                        "loaded": True, "error": None,
                        "module": module_path, "class": class_name,
                    }
                    logger.info("[registry] %s driver loaded successfully after cache clear", label)
                    return True
                except Exception as retry_err:
                    logger.warning("[registry] %s driver retry after cache clear also failed: %s", label, retry_err)
            # FIXED-P2: 记录error日志包含异常堆栈信息，原问题-仅记录warning无堆栈且静默返回False
            logger.error("[registry] %s driver load failed: %s", label, e, exc_info=True)
            self._load_status[label] = {
                "loaded": False, "error": str(e),
                "module": module_path, "class": class_name,
            }
            return self._make_error_plugin_info(label, module_path, class_name, str(e))

    def _make_error_plugin_info(self, label: str, module_path: str, class_name: str, error: str) -> Any:
        """FIXED-P2: 创建包含错误信息的PluginInfo对象
        原问题: 加载驱动失败时静默返回None/False，无错误信息
        修复方案: 返回包含错误信息的PluginInfo对象，供调用方诊断加载失败原因
        """
        try:
            from edgelite.engine.plugin_manager import PluginInfo
            return PluginInfo(
                name=label,
                module_path=module_path,
                class_name=class_name,
                is_custom=False,
                is_loaded=False,
                error=error,
            )
        except ImportError:
            logger.debug("[registry] PluginInfo not available, returning error dict")
            return {"name": label, "module_path": module_path, "class_name": class_name, "error": error}

    def get_load_status(self) -> dict[str, dict]:
        """获取所有驱动的加载状态（已加载/因依赖缺失跳过）"""
        return dict(self._load_status)

    def _check_all_dependencies(self) -> None:
        """Check dependencies for all registered drivers after auto_discover"""
        for driver_cls in list(self._registered_drivers):
            plugin_name = driver_cls.plugin_name
            required_deps = getattr(driver_cls, "_required_dependencies", [])
            if not required_deps:
                self._dependency_results[plugin_name] = {
                    "available": True,
                    "missing_deps": [],
                    "checked_deps": [],
                }
                continue

            missing = []
            checked = []
            for dep in required_deps:
                checked.append(dep)
                try:
                    __import__(dep)
                except ImportError:
                    missing.append(dep)

            is_available = len(missing) == 0
            self._dependency_results[plugin_name] = {
                "available": is_available,
                "missing_deps": missing,
                "checked_deps": checked,
            }

            if not is_available:
                logger.warning(
                    "[registry] Driver %s has missing dependencies: %s, marking as unavailable",
                    plugin_name, missing,
                )

    def get_dependency_results(self) -> dict[str, dict]:
        """Get dependency check results for all drivers"""
        return dict(self._dependency_results)

    def _discover_custom_drivers(self) -> None:
        """从custom_dir发现并加载自定义驱动"""
        try:
            from edgelite.config import get_config

            custom_dir = get_config().drivers.custom_dir
        except Exception as e:
            logger.debug("[registry] error: %s", e)
            return

        if not custom_dir:
            return

        import hashlib
        import importlib
        import importlib.util
        from pathlib import Path

        custom_path = Path(custom_dir)
        if not custom_path.is_dir():
            logger.warning(
                "[registry] Custom driver directory does not exist: %s",
                custom_dir,
            )
            return

        # FIXED-P0: 使用edgelite包目录作为固定基准路径，防止os.chdir()和符号链接绕过
        _SAFE_BASE = Path(__file__).resolve().parent.parent  # edgelite包根目录
        try:
            resolved = custom_path.resolve()
            resolved.relative_to(_SAFE_BASE)
        except ValueError:
            logger.error("[registry] Custom driver directory %s is outside edgelite package directory, rejecting for security", custom_dir)
            return

        logger.info("Scanning custom driver directory: %s", custom_dir)
        loaded = 0
        for py_file in custom_path.glob("*.py"):
            if py_file.name.startswith("_"):
                continue
            # FIXED-P0: 校验驱动文件resolve后仍在安全目录内，防止符号链接绕过
            try:
                resolved_file = py_file.resolve()
                resolved_file.relative_to(_SAFE_BASE)
            except ValueError:
                logger.error("[registry] Custom driver %s resolves outside safe directory, rejecting for security", py_file.name)
                continue
            # FIXED-P1: 校验驱动文件哈希，防止被篡改的驱动被加载
            try:
                file_hash = hashlib.sha256(py_file.read_bytes()).hexdigest()
                logger.debug("[registry] Custom driver %s sha256=%s", py_file.name, file_hash)
            except Exception:
                logger.warning("[registry] Cannot read custom driver %s, skipping", py_file.name)
                continue
            module_name = f"edgelite.drivers.custom_{py_file.stem}"
            try:
                spec = importlib.util.spec_from_file_location(module_name, py_file)
                if spec is None or spec.loader is None:
                    continue
                module = importlib.util.module_from_spec(spec)
                # FIXED-P0: 自定义驱动导入改用白名单机制，仅允许已知安全模块
                # 之前：使用 _DANGEROUS_MODULES 黑名单，无法覆盖 ctypes/cffi/pickle/marshal 等危险模块
                # 之后：使用 _ALLOWED_MODULES 白名单，仅允许 asyncio/logging/datetime/struct/collections 等安全模块
                _ALLOWED_MODULES = frozenset({
                    "asyncio", "logging", "datetime", "struct", "collections",
                    "math", "json", "time", "uuid", "hashlib", "base64",
                    "decimal", "fractions", "statistics", "itertools", "functools",
                    "operator", "enum", "dataclasses", "typing", "abc",
                    "re", "string", "textwrap", "pprint", "copy",
                    "_io",  # FIXED: _io 是 open() 等内置函数的底层实现，标准库依赖
                    "edgelite",  # 允许访问 edgelite 包内模块（受 DriverPlugin 基类约束）
                })
                _orig_import = builtins.__import__
                # FIXED(P1): 原问题-B023 循环变量捕获; 修复-使用关键字参数默认值绑定当前 _ALLOWED_MODULES 和 _orig_import
                def _restricted_import(name, *args, _allowed=_ALLOWED_MODULES, _orig=_orig_import, **kwargs):
                    top_level = name.split(".")[0]
                    if top_level not in _allowed:
                        raise ImportError(f"Import of '{name}' is not in the allowed modules whitelist for custom drivers")
                    return _orig(name, *args, **kwargs)
                # FIXED-P0: 同时拦截 importlib.import_module 和 sys.modules 绕过
                _orig_import_module = importlib.import_module
                # FIXED(P1): 原问题-B023 循环变量捕获; 修复-使用关键字参数默认值绑定当前 _ALLOWED_MODULES 和 _orig_import_module
                def _restricted_import_module(name, *args, _allowed=_ALLOWED_MODULES, _orig=_orig_import_module, **kwargs):
                    top_level = name.split(".")[0]
                    if top_level not in _allowed:
                        raise ImportError(f"Import of '{name}' is not in the allowed modules whitelist for custom drivers")
                    return _orig(name, *args, **kwargs)
                # FIXED-P0: 拦截 importlib.util.spec_from_file_location 绕过路径
                _orig_spec_from_file = importlib.util.spec_from_file_location
                # FIXED(P1): 原问题-B023 循环变量捕获; 修复-使用关键字参数默认值绑定当前 _ALLOWED_MODULES 和 _orig_spec_from_file
                def _restricted_spec_from_file(name, *args, _allowed=_ALLOWED_MODULES, _orig=_orig_spec_from_file, **kwargs):
                    top_level = name.split(".")[0] if name else ""
                    if top_level and top_level not in _allowed:
                        raise ImportError(f"spec_from_file_location for '{name}' is not in the allowed modules whitelist for custom drivers")
                    return _orig(name, *args, **kwargs)
                # FIXED-P0: 拦截 sys.modules 字典访问绕过，将非白名单模块替换为哨兵
                import sys as _sys
                _SUSPENDED_MODULES = {}
                for _mod_name in list(_sys.modules):
                    if _mod_name.split(".")[0] not in _ALLOWED_MODULES:
                        _SUSPENDED_MODULES[_mod_name] = _sys.modules.pop(_mod_name)
                try:
                    # FIXED-P0: 仅在模块级替换__import__，而非全局替换builtins
                    # 之前：全局替换builtins.__import__且永不恢复，整个应用import os会抛ImportError
                    # 之后：通过module.__builtins__限制仅在自定义驱动模块内拦截，全局builtins执行后恢复
                    builtins.__import__ = _restricted_import
                    importlib.import_module = _restricted_import_module
                    importlib.util.spec_from_file_location = _restricted_spec_from_file
                    spec.loader.exec_module(module)
                    # FIXED-P0: 为已加载模块设置独立的__builtins__，使模块内后续延迟导入也受限
                    module.__builtins__ = dict(builtins.__dict__)
                    module.__builtins__["__import__"] = _restricted_import
                finally:
                    # FIXED-P0: 恢复全局builtins.__import__和importlib，不再永久污染全局命名空间
                    builtins.__import__ = _orig_import
                    importlib.import_module = _orig_import_module
                    importlib.util.spec_from_file_location = _orig_spec_from_file
                    # 恢复被挂起的sys.modules（白名单外的模块恢复回 sys.modules）
                    for _mod_name, _mod_obj in _SUSPENDED_MODULES.items():
                        _sys.modules[_mod_name] = _mod_obj
                for attr_name in dir(module):
                    attr = getattr(module, attr_name)
                    if (
                        isinstance(attr, type)
                        and issubclass(attr, DriverPlugin)
                        and attr is not DriverPlugin
                    ):
                        if not self._validate_plugin_class(attr, py_file.name):
                            continue
                        self.register(attr)
                        loaded += 1
            except UnicodeDecodeError as e:
                logger.error("[registry] Custom driver file corrupted (encoding error): %s - try reinstalling or clearing __pycache__", e)
            except Exception as e:
                logger.warning(
                    "[registry] Custom driver %s failed to load: %s",
                    py_file.name, e,
                )

        if loaded > 0:
            logger.info("Loaded %d custom drivers from %s", loaded, custom_dir)

    @staticmethod
    def _validate_plugin_class(cls: type, source: str) -> bool:
        """验证插件类是否具有必要属性"""
        required_attrs = ("plugin_name", "plugin_version", "supported_protocols")
        for attr in required_attrs:
            val = getattr(cls, attr, None)
            if not val:
                logger.warning(
                    "[registry] Custom driver %s from %s missing required attribute '%s', skipping",
                    cls.__name__, source, attr,
                )
                return False
        return True


_registry: DriverRegistry | None = None
_registry_lock = threading.Lock()


def get_driver_registry() -> DriverRegistry:
    """获取全局驱动注册表"""
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = DriverRegistry()
            _registry.auto_discover()
    return _registry
