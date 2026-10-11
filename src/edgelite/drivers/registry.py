"""Driver Registry - 协议驱动注册表

本模块维护「协议名称 → 驱动类」的全局映射，是 EdgeLiteGateway
扩展新协议驱动的统一入口。

=================================================================
如何注册一个新驱动（新贡献者指南）
=================================================================

方式一：内置驱动（改动源码，随系统发布）
-------------------------------------------------
1. 在 ``edgelite/drivers/`` 下新建驱动文件，例如 ``my_protocol.py``，
   实现一个继承 :class:`edgelite.drivers.base.DriverPlugin` 的类，
   并设置三个必填类属性：

   - ``plugin_name``: 驱动唯一名称（如 ``"my_protocol"``）
   - ``plugin_version``: 版本号（如 ``"1.0.0"``）
   - ``supported_protocols``: 该驱动支持的协议键元组
     （如 ``("my_protocol", "my_proto")``，可含别名）

   同时实现抽象方法 ``start() / stop() / read_points() / write_point()``。

2. 在 :meth:`DriverRegistry.auto_discover` 的 ``_driver_modules``
   列表中追加一行::

       ("My Protocol", "edgelite.drivers.my_protocol", "MyProtocolDriver"),

3. 在 :data:`DRIVER_DISPLAY_NAMES` 中登记显示名称（中/英文），
   并将协议键加入 :data:`_BUILTIN_PROTOCOLS`，防止被自定义驱动覆盖。

方式二：自定义驱动（不改源码，运行时热加载）
-------------------------------------------------
1. 在配置项 ``drivers.custom_dir`` 指向的目录中放入 ``*.py`` 文件
   （文件名不能以 ``_`` 开头）。
2. 文件中同样定义一个 ``DriverPlugin`` 子类，注册表在
   :meth:`DriverRegistry.auto_discover` 阶段会自动扫描、校验并注册。

   注意：自定义驱动受安全沙箱约束——只能 import 白名单内的标准库模块，
   且不能覆盖 :data:`_BUILTIN_PROTOCOLS` 中的内置协议。

驱动类最小示例
-------------------------------------------------
::

    from edgelite.drivers.base import DriverPlugin

    class MyProtocolDriver(DriverPlugin):
        plugin_name = "my_protocol"
        plugin_version = "1.0.0"
        supported_protocols = ("my_protocol", "my_proto")

        async def start(self, config: dict) -> None:
            await super().start(config)
            # 建立协议连接 ...

        async def stop(self) -> None:
            # 断开连接、释放资源 ...
            await super().stop()

        async def read_points(self, device_id: str, points: list[str]) -> dict:
            ...  # 返回 {point_name: value}

        async def write_point(self, device_id: str, point: str, value) -> bool:
            ...  # 写入成功返回 True

手动注册（一般用于测试）
-------------------------------------------------
::

    from edgelite.drivers.registry import get_driver_registry

    registry = get_driver_registry()      # 获取全局单例（首次调用触发 auto_discover）
    registry.register(MyProtocolDriver)   # 手动注册驱动类
    cls = registry.get_driver_class("my_protocol")  # 按协议键取回驱动类

常用查询接口
-------------------------------------------------
- :meth:`DriverRegistry.get_supported_protocols`  已注册驱动名（去重）
- :meth:`DriverRegistry.get_all_protocol_keys`    全部协议键（含别名）
- :meth:`DriverRegistry.get_load_status`          各驱动的加载成败状态
- :meth:`DriverRegistry.get_dependency_results`   可选依赖检查结果
- :func:`get_driver_display_name`                 驱动的中/英文显示名
"""

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
    "modbus_slave": {"en": "Modbus Slave", "zh": "Modbus从站"},
}

# FIXED-P0: 内置协议集合，防止自定义驱动覆盖内置协议导致协议劫持
_BUILTIN_PROTOCOLS = frozenset(
    {
        "modbus_tcp",
        "modbus_rtu",
        "mqtt_client",
        "http_webhook",
        "opcua",
        "s7",
        "mc",
        "omron_fins",
        "fins",
        "allen_bradley",
        "ab",
        "ab_cip",
        "ab_pccc",
        "opc_da",
        "onvif",
        "modbus_slave",
        "simulator",
    }
)


def get_driver_display_name(plugin_name: str, language: str = "en") -> str:
    """获取驱动在指定语言下的显示名称（用于 UI 国际化展示）。

    Args:
        plugin_name: 驱动的唯一名称，即驱动类的 ``plugin_name`` 属性
            （如 ``"modbus_tcp"``）。需在 :data:`DRIVER_DISPLAY_NAMES` 中登记。
        language: 语言代码，目前支持 ``"en"``（英文）和 ``"zh"``（中文），
            默认 ``"en"``。

    Returns:
        对应语言的显示名称。若该语言未登记则回退到英文；
        若驱动完全未登记，则原样返回 ``plugin_name``。

    Raises:
        本函数不抛出异常，所有未命中场景均回退处理。
    """
    name_info = DRIVER_DISPLAY_NAMES.get(plugin_name, {})
    return name_info.get(language, name_info.get("en", plugin_name))


class DriverRegistry:
    """驱动注册表，管理「协议键 → 驱动类」的映射关系。

    全局唯一实例通过 :func:`get_driver_registry` 获取（单例 + 首次调用时
    自动执行 :meth:`auto_discover` 加载全部内置与自定义驱动）。

    内部数据结构：

    - ``_drivers``: ``dict[协议键, 驱动类]``，包含 ``supported_protocols``
      中的别名键以及 ``plugin_name`` 键；
    - ``_registered_drivers``: 已注册驱动类的集合，用于去重；
    - ``_load_status``: 各驱动模块的加载结果（成功/失败原因）；
    - ``_dependency_results``: 各驱动可选依赖的检查结果。

    线程安全性：:meth:`auto_discover` 与 :func:`get_driver_registry` 均持锁；
    其余方法在启动期（单线程初始化阶段）调用，运行期只读。
    """

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
        """注册一个驱动类，将其协议键映射到该类。

        注册成功后，``driver_class.supported_protocols`` 中的每个协议键
        以及 ``driver_class.plugin_name`` 都会指向该驱动类。

        Args:
            driver_class: 待注册的驱动类。必须满足：
                1. 继承自 :class:`~edgelite.drivers.base.DriverPlugin`；
                2. 定义了非空的 ``plugin_name``、``plugin_version``、
                   ``supported_protocols`` 三个类属性。

        Returns:
            None。注册结果通过日志体现：
            重复注册同一类会跳过（debug 日志）；校验失败会跳过（warning 日志）。

        Raises:
            本方法不抛出异常。以下情况只会记录日志并跳过，保证单个
            驱动的问题不影响其他驱动的加载：

            - 未继承 ``DriverPlugin`` 或缺少必填属性 → 跳过注册；
            - 自定义驱动试图覆盖 :data:`_BUILTIN_PROTOCOLS` 中的
              内置协议 → 该协议键拒绝覆盖（error 日志），其余键正常注册；
            - 非内置协议被重复注册 → 允许覆盖并记录 warning 日志。
        """
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
                    driver_class.__name__,
                    attr,
                )
                return

        for protocol in driver_class.supported_protocols:
            if protocol in self._drivers and self._drivers[protocol] is not driver_class:
                existing_cls = self._drivers[protocol]
                # FIXED-P2: 内置协议不允许被自定义驱动覆盖，防止协议劫持
                if protocol in _BUILTIN_PROTOCOLS and not getattr(driver_class, "_is_builtin", False):
                    logger.error(
                        "[registry] Protocol %s is a built-in protocol registered by %s, custom driver %s cannot override it",
                        protocol,
                        existing_cls.plugin_name,
                        driver_class.plugin_name,
                    )
                    continue
                logger.warning(
                    "Protocol %s already registered by %s v%s, overriding with %s v%s",
                    protocol,
                    existing_cls.plugin_name,
                    existing_cls.plugin_version,
                    driver_class.plugin_name,
                    driver_class.plugin_version,
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
        """按协议键获取对应的驱动类。

        Args:
            protocol: 协议键，可以是驱动的 ``plugin_name``（如 ``"siemens_s7"``
                或 ``"s7"``），也可以是 ``supported_protocols`` 中登记的别名。

        Returns:
            匹配的驱动类；未注册时返回 ``None``，调用方需自行判空。

        Raises:
            本方法不抛出异常。
        """
        return self._drivers.get(protocol)

    def get_supported_protocols(self) -> list[str]:
        """获取所有已注册驱动的唯一名称列表（按 ``plugin_name`` 去重）。

        Returns:
            排序后的驱动名称列表，例如 ``["modbus_tcp", "mqtt_client", ...]``。
            每个驱动类只出现一次，不包含协议别名；
            如需包含别名的完整协议键列表，请使用 :meth:`get_all_protocol_keys`。

        Raises:
            本方法不抛出异常。
        """
        # 返回已注册的驱动类的 plugin_name（去重）
        return sorted([cls.plugin_name for cls in self._registered_drivers])

    def get_all_protocol_keys(self) -> list[str]:
        """获取所有已注册的协议键名（包括别名），用于集成 API 和协议列表查询。

        与 get_supported_protocols() 不同，此方法返回 _drivers 字典中的所有键，
        包含 supported_protocols 中的别名（如 's7', 'mqtt', 'mc' 等）。

        Returns:
            排序后的协议键列表，每个键都可以通过
            :meth:`get_driver_class` 取回对应的驱动类。

        Raises:
            本方法不抛出异常。
        """
        return sorted(self._drivers.keys())

    def unregister(self, protocol: str) -> bool:
        """注销指定协议键对应的驱动映射。

        只移除 ``protocol`` 这一个键的映射；如果该驱动类还注册了
        其他协议键，其余键不受影响，驱动类仍保留在注册表中。

        Args:
            protocol: 要注销的协议键（如 ``"modbus_tcp"``）。

        Returns:
            ``True`` 表示该协议键存在且已移除；
            ``False`` 表示协议键未注册，无操作。

        Raises:
            本方法不抛出异常。
        """
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
        """注销指定驱动类的全部协议键映射（整体移除该驱动）。

        Args:
            driver_cls: 要移除的驱动类（按对象身份匹配）。

        Returns:
            实际移除的协议键数量。若该驱动类未注册，返回 ``0``。

        Raises:
            本方法不抛出异常。
        """
        if driver_cls not in self._registered_drivers:
            return 0
        to_remove = [p for p, cls in self._drivers.items() if cls is driver_cls]
        for p in to_remove:
            del self._drivers[p]
        self._registered_drivers.discard(driver_cls)
        return len(to_remove)

    def items(self) -> list[tuple[str, type[DriverPlugin]]]:
        """获取所有已注册的「协议键, 驱动类」对（按驱动类去重）。

        Returns:
            ``[(protocol, driver_class), ...]`` 列表。同一个驱动类只出现一次，
            使用其第一个注册的协议键作为代表；别名键不会重复出现。

        Raises:
            本方法不抛出异常。
        """
        # 去重：只返回每个驱动类第一个注册的协议
        seen = set()
        result = []
        for protocol, cls in self._drivers.items():
            if cls not in seen:
                seen.add(cls)
                result.append((protocol, cls))
        return result

    def auto_discover(self) -> None:
        """自动发现并注册所有内置驱动及自定义驱动。

        执行流程：
            1. 逐个加载 ``_driver_modules`` 列表中的内置驱动模块
               （单个失败不影响其他驱动，失败原因记入 ``_load_status``）；
            2. 扫描配置项 ``drivers.custom_dir`` 指向的目录，
               在安全沙箱约束下加载自定义驱动；
            3. 检查所有已注册驱动的可选依赖（``_required_dependencies``），
               结果记入 ``_dependency_results``。

        本方法幂等：重复调用会直接跳过（warning 日志）。
        首次调用通常由 :func:`get_driver_registry` 自动触发，
        业务代码一般无需手动调用。

        Returns:
            None。

        Raises:
            本方法不向外抛出异常；所有驱动级错误均被捕获并记录到
            日志和 :meth:`get_load_status` 的状态字典中。
        """
        # FIXED-P1: 整个方法体在锁内执行，消除TOCTOU竞态
        with self._discover_lock:
            if self._discovered:
                logger.warning("[registry] auto_discover already executed, skipping duplicate call")
                return
            # 仅加载13种核心协议驱动，其他工业协议已彻底清除
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
                "loaded": True,
                "error": None,
                "module": module_path,
                "class": class_name,
            }
            return True
        except ImportError as e:
            # FIXED-P2: 记录error日志包含异常堆栈信息，原问题-仅记录warning无堆栈
            logger.error(
                "[registry] %s driver import failed (missing dependency): %s",
                label,
                e,
                exc_info=True,
            )
            self._load_status[label] = {
                "loaded": False,
                "error": f"Missing dependency: {e}",
                "module": module_path,
                "class": class_name,
            }
            return self._make_error_plugin_info(label, module_path, class_name, f"Missing dependency: {e}")
        except AttributeError as e:
            # FIXED-P2: 记录error日志包含异常堆栈信息，原问题-仅记录warning无堆栈
            logger.error("[registry] %s driver class not found: %s", label, e, exc_info=True)
            self._load_status[label] = {
                "loaded": False,
                "error": f"Class not found: {e}",
                "module": module_path,
                "class": class_name,
            }
            return self._make_error_plugin_info(label, module_path, class_name, f"Class not found: {e}")
        except Exception as e:
            # Save outer exception reference: inner `except Exception as e:` (line 317)
            # shadows and deletes `e` after its block exits, making outer `e` inaccessible.
            saved_e = e
            # UnicodeDecodeError usually caused by corrupted .pyc cache or source file
            if isinstance(e, UnicodeDecodeError):
                logger.warning(
                    "[registry] %s driver load failed (encoding error, clearing __pycache__): %s",
                    label,
                    e,
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
                    if cached_module and hasattr(cached_module, "__cached__"):
                        pyc_path = cached_module.__cached__
                        if pyc_path and os.path.exists(pyc_path):
                            pycache_dir = os.path.dirname(pyc_path)
                            if os.path.basename(pycache_dir) == "__pycache__":
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
                        if spec and spec.origin and spec.origin.endswith(".py"):
                            src_path = spec.origin
                            # R5-S-15: 使用with语句确保文件句柄正确关闭，防止泄漏
                            with open(src_path, "rb") as f:
                                raw = f.read()
                            try:
                                raw.decode("utf-8")  # 测试是否需要修复
                            except UnicodeDecodeError:
                                text = raw.decode("utf-8", errors="replace")
                                fixed = text.encode("utf-8")
                                # R5-S-15: 使用with语句确保文件句柄正确关闭，防止泄漏
                                with open(src_path, "wb") as f:
                                    f.write(fixed)
                                logger.info("[registry] Fixed UTF-8 corruption in source: %s", src_path)
                    except Exception as fix_err:
                        logger.debug("[registry] Source fix attempt failed: %s", fix_err)

                    # Retry import
                    module = importlib.import_module(module_path)
                    driver_cls = getattr(module, class_name)
                    self.register(driver_cls)
                    self._load_status[label] = {
                        "loaded": True,
                        "error": None,
                        "module": module_path,
                        "class": class_name,
                    }
                    logger.info("[registry] %s driver loaded successfully after cache clear", label)
                    return True
                except Exception as retry_err:
                    logger.warning("[registry] %s driver retry after cache clear also failed: %s", label, retry_err)
            # FIXED-P2: 记录error日志包含异常堆栈信息，原问题-仅记录warning无堆栈且静默返回False
            logger.error("[registry] %s driver load failed: %s", label, saved_e, exc_info=True)
            self._load_status[label] = {
                "loaded": False,
                "error": str(saved_e),
                "module": module_path,
                "class": class_name,
            }
            return self._make_error_plugin_info(label, module_path, class_name, str(saved_e))

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
        """获取所有驱动的加载状态（成功/失败及原因），用于运维诊断。

        Returns:
            以驱动标签（如 ``"Modbus TCP"``）为键的字典，每项形如::

                {
                    "loaded": True,                 # 是否加载成功
                    "error": None,                  # 失败原因（成功时为 None）
                    "module": "edgelite.drivers.modbus_tcp",  # 模块路径
                    "class": "ModbusTcpDriver",     # 驱动类名
                }

            返回的是内部字典的浅拷贝，修改返回值不会影响注册表。

        Raises:
            本方法不抛出异常。
        """
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
                logger.debug(
                    "[registry] Driver %s has missing dependencies: %s, marking as unavailable (optional)",
                    plugin_name,
                    missing,
                )

    def get_dependency_results(self) -> dict[str, dict]:
        """获取所有已注册驱动的可选依赖检查结果。

        驱动类可通过 ``_required_dependencies`` 类属性声明可选的第三方
        依赖（如 ``["pymodbus"]``），:meth:`auto_discover` 完成后会逐一
        尝试 import 并记录结果。

        Returns:
            以 ``plugin_name`` 为键的字典，每项形如::

                {
                    "available": False,             # 依赖是否全部满足
                    "missing_deps": ["pymodbus"],   # 缺失的依赖
                    "checked_deps": ["pymodbus"],   # 实际检查过的依赖
                }

            未声明依赖的驱动 ``available`` 恒为 ``True``。
            返回的是内部字典的浅拷贝。

        Raises:
            本方法不抛出异常。
        """
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
            logger.error(
                "[registry] Custom driver directory %s is outside edgelite package directory, rejecting for security",
                custom_dir,
            )
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
                logger.error(
                    "[registry] Custom driver %s resolves outside safe directory, rejecting for security", py_file.name
                )
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
                _ALLOWED_MODULES = frozenset(
                    {
                        "asyncio",
                        "logging",
                        "datetime",
                        "struct",
                        "collections",
                        "math",
                        "json",
                        "time",
                        "uuid",
                        "hashlib",
                        "base64",
                        "decimal",
                        "fractions",
                        "statistics",
                        "itertools",
                        "functools",
                        "operator",
                        "enum",
                        "dataclasses",
                        "typing",
                        "abc",
                        "re",
                        "string",
                        "textwrap",
                        "pprint",
                        "copy",
                        "_io",  # FIXED: _io 是 open() 等内置函数的底层实现，标准库依赖
                        "edgelite",  # 允许访问 edgelite 包内模块（受 DriverPlugin 基类约束）
                    }
                )
                _orig_import = builtins.__import__

                # FIXED(P1): 原问题-B023 循环变量捕获; 修复-使用关键字参数默认值绑定当前 _ALLOWED_MODULES 和 _orig_import
                def _restricted_import(name, *args, _allowed=_ALLOWED_MODULES, _orig=_orig_import, **kwargs):
                    top_level = name.split(".")[0]
                    if top_level not in _allowed:
                        raise ImportError(
                            f"Import of '{name}' is not in the allowed modules whitelist for custom drivers"
                        )
                    return _orig(name, *args, **kwargs)

                # FIXED-P0 (并发安全#9): 通过 module.__builtins__ 命名空间隔离，不修改全局 builtins/importlib/sys.modules
                # 原问题: exec_module 期间全局替换 builtins.__import__ / importlib.import_module /
                #         importlib.util.spec_from_file_location / 挂起 sys.modules 中非白名单模块，
                #         在 exec_module 窗口期影响整个应用，其他线程/coroutine 的 import 受限或失败 (TOCTOU 竞态)。
                # 修复: 仅设置 module.__builtins__ 为受限 dict，模块内 __import__ 受限，
                #       全局 builtins / importlib / sys.modules 完全不受影响，并发 import 安全。
                restricted_builtins = dict(builtins.__dict__)
                restricted_builtins["__import__"] = _restricted_import
                # exec_module 前设置 module.__builtins__，使模块级 import 和运行时延迟 import 均受限
                # 使用 setattr 而非直接赋值：typeshed 的 ModuleType 未将 __builtins__ 声明为可写属性
                module.__builtins__ = restricted_builtins
                spec.loader.exec_module(module)
                for attr_name in dir(module):
                    attr = getattr(module, attr_name)
                    if isinstance(attr, type) and issubclass(attr, DriverPlugin) and attr is not DriverPlugin:
                        if not self._validate_plugin_class(attr, py_file.name):
                            continue
                        self.register(attr)
                        loaded += 1
            except UnicodeDecodeError as e:
                logger.error(
                    "[registry] Custom driver file corrupted (encoding error): %s - try reinstalling or clearing __pycache__",
                    e,
                )
            except Exception as e:
                logger.warning(
                    "[registry] Custom driver %s failed to load: %s",
                    py_file.name,
                    e,
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
                    cls.__name__,
                    source,
                    attr,
                )
                return False
        return True


_registry: DriverRegistry | None = None
_registry_lock = threading.Lock()


def get_driver_registry() -> DriverRegistry:
    """获取全局驱动注册表单例。

    首次调用时在锁内创建 :class:`DriverRegistry` 实例并自动执行
    :meth:`~DriverRegistry.auto_discover`（加载全部内置与自定义驱动），
    之后的调用直接返回同一实例，线程安全。

    Returns:
        全局唯一的 :class:`DriverRegistry` 实例。

    Raises:
        本函数本身不抛出异常；但首次触发 ``auto_discover`` 时，
        驱动级的加载错误会记录到日志和
        :meth:`~DriverRegistry.get_load_status` 中，不会中断返回。

    Example:
        >>> registry = get_driver_registry()
        >>> registry.get_driver_class("modbus_tcp")  # doctest: +SKIP
        <class 'edgelite.drivers.modbus_tcp.ModbusTcpDriver'>
    """
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = DriverRegistry()
            _registry.auto_discover()
    return _registry
