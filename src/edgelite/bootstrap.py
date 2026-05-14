"""应用启动引导 — 将 lifespan 初始化逻辑拆分为独立函数"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ServiceContainer:
    """持有所有已初始化的服务实例"""

    database: Any = None
    influx_storage: Any = None
    cache_manager: Any = None
    event_bus: Any = None
    scheduler: Any = None
    evaluator: Any = None
    lifecycle: Any = None
    ws_manager: Any = None
    ws_channels: Any = None
    device_service: Any = None
    rule_service: Any = None
    alarm_service: Any = None
    data_service: Any = None
    video_service: Any = None
    system_service: Any = None
    notify_service: Any = None
    driver_registry: Any = None
    mqtt_forwarder: Any = None
    mqtt_server: Any = None
    modbus_slave: Any = None
    platform_handlers: dict = field(default_factory=dict)
    start_time: float = field(default_factory=time.time)
    integration_endpoint: Any = None
    integration_dispatcher: Any = None
    backhaul_manager: Any = None
    config: Any = None
    ota_manager: Any = None
    audit_service: Any = None
    plugin_manager: Any = None
    preprocessor: Any = None
    serial_bridge: Any = None

    _repos: dict = field(default_factory=dict, repr=False)
    _initialized: list = field(default_factory=list, repr=False)

    def track(self, name: str, resource: Any):
        self._initialized.append((name, resource))


def _ensure_secret_key(config) -> None:
    if not config.security.secret_key:
        import secrets

        config.security.secret_key = secrets.token_urlsafe(48)
        logger.warning(
            "⚠️  JWT密钥未配置，已自动生成随机密钥。"
            "生产环境请通过 EDGELITE_SECURITY__SECRET_KEY 环境变量设置固定密钥！"
        )
        # FIXED: 原问题-自动生成的JWT密钥明文写入.env文件存在泄露风险
        # 现不再写入.env，仅通过环境变量或配置文件设置持久密钥
        # 运行时生成的密钥在进程重启后变更，所有已颁发token失效
        # 生产环境必须通过环境变量设置固定密钥
    elif len(config.security.secret_key) < 32:
        logger.warning(
            "⚠️  安全警告: JWT密钥过短(%d字符)，"
            "生产环境请通过 EDGELITE_SECURITY__SECRET_KEY 环境变量设置至少32字符的随机密钥！",
            len(config.security.secret_key),
        )


async def bootstrap_storage(c: ServiceContainer, config) -> None:
    from edgelite.storage.cache import CacheManager
    from edgelite.storage.database import Database
    from edgelite.storage.influx_storage import InfluxDBStorage
    from edgelite.storage.sqlite_repo import AlarmRepo, DeviceRepo, RuleRepo, UserRepo

    database = Database()
    await database.connect()
    await database.init_tables()
    c.database = database
    c.track("database", database)
    logger.info("数据库初始化完成")

    influx = InfluxDBStorage()
    await influx.connect()
    c.influx_storage = influx
    c.track("influx", influx)

    c.cache_manager = CacheManager(database)

    write_lock = database.write_lock
    c._repos["device"] = DeviceRepo(database, write_lock)
    c._repos["rule"] = RuleRepo(database, write_lock)
    c._repos["alarm"] = AlarmRepo(database, write_lock)
    c._repos["user"] = UserRepo(database, write_lock)


async def bootstrap_engine(c: ServiceContainer, config) -> None:
    from edgelite.engine.event_bus import EventBus
    from edgelite.engine.lifecycle import DeviceLifecycleManager
    from edgelite.engine.preprocessor import DataPreprocessor
    from edgelite.engine.scheduler import CollectScheduler

    event_bus = EventBus()
    c.event_bus = event_bus
    c.track("event_bus", event_bus)

    scheduler = CollectScheduler(event_bus, c.influx_storage, c.cache_manager)
    c.scheduler = scheduler
    c.track("scheduler", scheduler)

    preprocessor = DataPreprocessor()
    if config.preprocess.enabled:
        scheduler.set_preprocessor(preprocessor)
    c.preprocessor = preprocessor

    c.lifecycle = DeviceLifecycleManager(event_bus)


async def bootstrap_services(c: ServiceContainer, config) -> None:
    from edgelite.services.alarm_service import AlarmService
    from edgelite.services.audit_service import AuditService
    from edgelite.services.data_service import DataService
    from edgelite.services.device_service import DeviceService
    from edgelite.services.notify_service import NotifyService
    from edgelite.services.rule_service import RuleService
    from edgelite.services.system_service import SystemService
    from edgelite.services.video_service import VideoService

    device_repo = c._repos["device"]
    rule_repo = c._repos["rule"]
    alarm_repo = c._repos["alarm"]
    user_repo = c._repos["user"]

    c.device_service = DeviceService(device_repo, rule_repo, c.scheduler, c.lifecycle)
    c.rule_service = RuleService(rule_repo, device_repo)
    c.alarm_service = AlarmService(alarm_repo)
    c.data_service = DataService(c.influx_storage, device_repo)
    c.video_service = VideoService(c.event_bus)
    c.notify_service = NotifyService()
    c.system_service = SystemService(
        c.database, device_repo, rule_repo, alarm_repo, user_repo,
        c.scheduler, c.start_time,
    )

    audit_service = AuditService(db_path=c.database.audit_db_path)
    await audit_service.initialize()
    c.audit_service = audit_service
    c.track("audit_service", audit_service)


async def bootstrap_evaluator(c: ServiceContainer, config) -> None:
    from edgelite.engine.evaluator import RuleEvaluator

    evaluator = RuleEvaluator(c.event_bus, c._repos["rule"], c._repos["alarm"])
    c.evaluator = evaluator
    await evaluator.start()
    c.track("evaluator", evaluator)


async def bootstrap_ws(c: ServiceContainer, config) -> None:
    from edgelite.ws.channels import WebSocketChannels
    from edgelite.ws.manager import ConnectionManager

    ws_manager = ConnectionManager()
    ws_channels = WebSocketChannels(c.event_bus, ws_manager)
    c.ws_manager = ws_manager
    c.ws_channels = ws_channels
    await ws_channels.start()
    c.track("ws_channels", ws_channels)


async def bootstrap_drivers(c: ServiceContainer, config) -> None:
    from edgelite.drivers.registry import DriverRegistry
    from edgelite.engine.plugin_manager import PluginManager

    driver_registry = DriverRegistry()
    driver_registry.auto_discover()
    c.driver_registry = driver_registry

    c.plugin_manager = PluginManager(driver_registry)
    custom_dir = (
        config.drivers.custom_dir
        if hasattr(config, "drivers") and config.drivers.custom_dir
        else ""
    )
    if custom_dir:
        c.plugin_manager.discover_custom_drivers(custom_dir)


async def bootstrap_mqtt(c: ServiceContainer, config) -> None:
    from edgelite.engine.mqtt_forwarder import MqttForwarder

    mqtt_forwarder = MqttForwarder()
    c.mqtt_forwarder = mqtt_forwarder
    await mqtt_forwarder.start(c.event_bus)
    c.track("mqtt_forwarder", mqtt_forwarder)

    mqtt_server_config = getattr(config, "mqtt_server", None)
    if mqtt_server_config and getattr(mqtt_server_config, "enabled", False):
        from edgelite.engine.mqtt_server import MqttServer

        mqtt_server = MqttServer()
        c.mqtt_server = mqtt_server
        await mqtt_server.start(
            {
                "host": getattr(mqtt_server_config, "host", "0.0.0.0"),
                "port": getattr(mqtt_server_config, "port", 1888),
                "ws_port": getattr(mqtt_server_config, "ws_port", None),
                "username": getattr(mqtt_server_config, "username", ""),
                "password": getattr(mqtt_server_config, "password", ""),
            }
        )
        c.track("mqtt_server", mqtt_server)


async def bootstrap_platforms(c: ServiceContainer, config) -> None:
    platforms_config = getattr(config, "platforms", None)
    if not platforms_config:
        return

    platform_map = {
        "iotsharp": ("edgelite.platform.iotsharp", "IoTSharpHandler"),
        "thingsboard": ("edgelite.platform.thingsboard", "ThingsBoardHandler"),
        "huawei_iotda": ("edgelite.platform.huawei_iotda", "HuaweiIoTDAHandler"),
        "thingscloud": ("edgelite.platform.thingscloud", "ThingsCloudHandler"),
        "thingspanel": ("edgelite.platform.thingspanel", "ThingsPanelHandler"),
    }

    for platform_name, platform_conf in platforms_config.items():
        if not isinstance(platform_conf, dict) or not platform_conf.get("enabled", False):
            continue
        mapping = platform_map.get(platform_name)
        if not mapping:
            continue
        try:
            import importlib
            module = importlib.import_module(mapping[0])
            handler_cls = getattr(module, mapping[1])
            handler = handler_cls()
            await handler.connect(platform_conf)
            c.platform_handlers[platform_name] = handler
            logger.info("%s平台对接已启动", platform_name)
        except Exception as e:
            logger.error("平台对接启动失败 %s: %s", platform_name, e)


async def bootstrap_modbus_slave(c: ServiceContainer, config) -> None:
    modbus_slave_config = getattr(config, "modbus_slave", None)
    if not modbus_slave_config or not getattr(modbus_slave_config, "enabled", False):
        return

    from edgelite.engine.modbus_slave import ModbusSlaveServer

    modbus_slave = ModbusSlaveServer()
    c.modbus_slave = modbus_slave
    await modbus_slave.start(
        {
            "host": getattr(modbus_slave_config, "host", "0.0.0.0"),
            "port": getattr(modbus_slave_config, "port", 502),
            "holding_size": getattr(modbus_slave_config, "holding_size", 1000),
            "input_size": getattr(modbus_slave_config, "input_size", 1000),
        }
    )
    c.track("modbus_slave", modbus_slave)


async def bootstrap_devices(c: ServiceContainer, config) -> None:
    await c.device_service.load_existing_devices()

    if config.simulator.auto_create:
        for dev_config in config.simulator.default_devices:
            existing = await c._repos["device"].get(dev_config.device_id)
            if existing is None:
                points_data = [p.model_dump() for p in dev_config.points]
                await c.device_service.create_simulator(
                    {
                        "device_id": dev_config.device_id,
                        "name": dev_config.name,
                        "points": points_data,
                        "collect_interval": dev_config.collect_interval,
                    }
                )
                logger.info("自动创建模拟设备: %s", dev_config.device_id)

    rule_repo = c._repos["rule"]
    alarm_repo = c._repos["alarm"]
    notify_service = c.notify_service

    async def handle_alarm_for_notify(event):
        if hasattr(event, "rule_id") and event.action == "firing":
            rule = await rule_repo.get(event.rule_id)
            if rule and rule.get("notify_channels"):
                alarm = (
                    await alarm_repo.get(event.alarm_id) if hasattr(event, "alarm_id") else None
                )
                if alarm:
                    await notify_service.send_notification(rule["notify_channels"], alarm)

    c.event_bus.register_handler("AlarmEvent", handle_alarm_for_notify)


async def bootstrap_integration(c: ServiceContainer, config) -> None:
    try:
        from edgelite.engine.integration.backhaul import BackhaulManager
        from edgelite.engine.integration.dispatcher import MessageDispatcher
        from edgelite.engine.integration.endpoint import IntegrationEndpoint

        integration_dispatcher = MessageDispatcher()
        integration_dispatcher.register_service("device_service", c.device_service)
        integration_dispatcher.register_service("scheduler", c.scheduler)
        integration_endpoint = IntegrationEndpoint(dispatcher=integration_dispatcher)
        c.integration_dispatcher = integration_dispatcher
        c.integration_endpoint = integration_endpoint

        backhaul_manager = BackhaulManager(
            event_bus=c.event_bus, endpoint=integration_endpoint, buffer_size=1000
        )
        c.backhaul_manager = backhaul_manager
        await backhaul_manager.start()
        c.track("backhaul_manager", backhaul_manager)
        logger.info("联调集成端点已初始化")
    except ImportError:
        logger.warning("联调集成模块不可用")


async def bootstrap_ota(c: ServiceContainer, config) -> None:
    try:
        from edgelite.engine.ota_manager import OTAManager

        c.ota_manager = OTAManager()
        logger.info("OTA升级管理器已初始化")
    except ImportError:
        logger.warning("OTA升级管理器模块不可用")
    except Exception as e:
        logger.warning("OTA升级管理器初始化失败: %s", e)


async def bootstrap_video(c: ServiceContainer, config) -> None:
    await c.video_service.init_provider()


async def bootstrap_all(c: ServiceContainer, config) -> None:
    _ensure_secret_key(config)

    if not config.influxdb.token:
        logger.warning(
            "⚠️  InfluxDB Token未配置，时序数据存储功能不可用。"
            "请通过 EDGELITE_INFLUXDB__TOKEN 环境变量设置！"
        )

    logging.basicConfig(level=config.logging.level, format=config.logging.format)
    logger.info("EdgeLiteGateway 启动中...")

    c.config = config

    await bootstrap_storage(c, config)
    await bootstrap_engine(c, config)
    await bootstrap_services(c, config)
    await bootstrap_evaluator(c, config)
    await bootstrap_ws(c, config)
    await bootstrap_video(c, config)
    await bootstrap_drivers(c, config)
    await bootstrap_mqtt(c, config)
    await bootstrap_platforms(c, config)
    await bootstrap_modbus_slave(c, config)
    await bootstrap_devices(c, config)
    await bootstrap_integration(c, config)
    await bootstrap_ota(c, config)

    logger.info("EdgeLiteGateway 启动完成 (port=%d)", config.server.port)


async def teardown(c: ServiceContainer) -> None:
    logger.info("EdgeLiteGateway 关闭中...")

    for name, resource in reversed(c._initialized):
        try:
            if hasattr(resource, "close"):
                await resource.close()
            elif hasattr(resource, "stop"):
                await resource.stop()
            elif hasattr(resource, "stop_all"):
                await resource.stop_all()
        except Exception as e:
            logger.warning("关闭%s异常: %s", name, e)

    for name, handler in list(c.platform_handlers.items()):
        try:
            await handler.disconnect()
        except Exception as e:
            logger.warning("平台对接关闭异常 %s: %s", name, e)

    if c.event_bus:
        c.event_bus.unregister_all()

    logger.info("EdgeLiteGateway 已关闭")
