"""应用启动引导 — 将 lifespan 初始化逻辑拆分为独立函数"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from edgelite.constants import _LOG_BACKUP_COUNT, _LOG_DIR, _LOG_MAX_BYTES

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
    app_updater: Any = None
    audit_service: Any = None
    plugin_manager: Any = None
    preprocessor: Any = None
    serial_bridge: Any = None
    ai_engine: Any = None
    ai_service: Any = None
    inference_scheduler: Any = None  # FIXED: AI 推理调度器，注入 MCPToolService [2026-06-29]
    driver_watchdog: Any = None
    shadow_service: Any = None
    disk_monitor: Any = None  # 磁盘空间监控器

    _repos: dict = field(default_factory=dict, repr=False)
    _initialized: list = field(default_factory=list, repr=False)
    _migration_status: dict = field(default_factory=dict, repr=False)

    def track(self, name: str, resource: Any):
        self._initialized.append((name, resource))


# FIXED-P0: 已知的占位符和弱密钥列表，启动时检测这些值并拒绝启动
_DEFAULT_KEY_PATTERNS = [
    "CHANGE_ME",
    "<your-secret-key-here>",
    "<your-",
    "your-secret-key",
    "change_me",
    "changeme",
    "secret_key_here",
    "placeholder",
]


def _ensure_secret_key(config) -> None:
    if not config.security.secret_key:
        raise RuntimeError(
            "JWT secret key is not configured! "
            "Set EDGELITE_SECURITY__SECRET_KEY environment variable in .env file. "
            'Generate a random key: python -c "import secrets; print(secrets.token_urlsafe(32))"'
        )
    if len(config.security.secret_key) < 32:
        raise RuntimeError(
            f"JWT secret key too short ({len(config.security.secret_key)} chars, minimum 32). "
            'Generate a random key: python -c "import secrets; print(secrets.token_urlsafe(32))"'
        )
    # FIXED-P0: 检测默认占位符密钥，防止用户使用 .env 模板中的示例值部署
    _key_lower = config.security.secret_key.lower()
    for pattern in _DEFAULT_KEY_PATTERNS:
        if pattern.lower() in _key_lower:
            raise RuntimeError(
                f"JWT secret key appears to be a default placeholder (contains '{pattern}'). "
                "For security, replace EDGELITE_SECURITY__SECRET_KEY in .env with a real random key. "
                'Generate: python -c "import secrets; print(secrets.token_urlsafe(32))"'
            )


async def bootstrap_storage(c: ServiceContainer, config) -> None:
    from edgelite.storage.cache import CacheManager
    from edgelite.storage.database import Database
    from edgelite.storage.influx_storage import InfluxDBStorage
    from edgelite.storage.sqlite_repo import AlarmRepo, DeviceRepo, RuleRepo, TemplateRepo, UserRepo

    database = Database()
    await database.connect()
    await database.init_tables()
    c.database = database
    c.track("database", database)
    logger.info("Database initialized")  # FIXED-P3: 中文日志→英文

    influx = InfluxDBStorage()
    await influx.connect()
    c.influx_storage = influx
    c.track("influx", influx)

    c.cache_manager = CacheManager(database)
    c.track("cache_manager", c.cache_manager)  # FIXED: P0-2 缓存管理器需追踪以便teardown时正确清理
    # FIXED-P0: 启动孤儿数据补偿任务，防止缓存孤儿数据永久丢失
    c.cache_manager.start_orphan_compaction()

    # FIXED-FINE-GRAINED-LOCK: 使用细粒度表锁
    # DeviceRepo 使用 devices 表锁
    c._repos["device"] = DeviceRepo(database, database.write_lock, Database.TABLE_DEVICES)
    # RuleRepo 使用 rules 表锁
    c._repos["rule"] = RuleRepo(database, database.write_lock, Database.TABLE_RULES)
    # AlarmRepo 使用 alarms 表锁
    c._repos["alarm"] = AlarmRepo(database, database.write_lock, Database.TABLE_ALARMS)
    # UserRepo 使用 users 表锁
    c._repos["user"] = UserRepo(database, database.write_lock, Database.TABLE_USERS)
    # TemplateRepo 使用 templates 表锁
    c._repos["template"] = TemplateRepo(database, database.write_lock, Database.TABLE_TEMPLATES)
    for repo_name, repo in c._repos.items():
        c.track(f"repo_{repo_name}", repo)  # FIXED: P0-2 所有仓库实例需追踪

    # Start database monitor
    try:
        from edgelite.services.db_monitor import get_db_monitor

        db_monitor = get_db_monitor()
        db_monitor.set_database(database)
        await db_monitor.start()
        c.track("db_monitor", db_monitor)
        logger.info("Database monitor started")
    except Exception as e:
        logger.warning("Database monitor start failed: %s", e)

    # FIXED(严重): 从 SQLite 恢复用户会话状态，消除 session_manager 重启后 fail-open 窗口
    try:
        from edgelite.security.session_manager import restore_sessions

        restored = restore_sessions()
        if restored:
            logger.info("Restored %d user session(s) from SQLite", restored)
    except Exception as e:
        logger.warning("Session restore failed: %s", e)


async def bootstrap_engine(c: ServiceContainer, config) -> None:
    from edgelite.engine.event_bus import EventBus
    from edgelite.engine.lifecycle import DeviceLifecycleManager
    from edgelite.engine.preprocessor import DataPreprocessor
    from edgelite.engine.scheduler import CollectScheduler

    event_bus = EventBus()
    c.event_bus = event_bus
    c.track("event_bus", event_bus)

    # FIXED: 启用告警事件持久化兜底（进程崩溃后重启可重放未投递告警）[2026-06-29]
    # best-effort: DB 初始化失败仅记录日志，不阻塞启动；replay 在 bootstrap_ws 后执行（订阅者就绪）
    try:
        _eb_cfg = getattr(config, "event_bus", None)
        outbox_path = (
            str(getattr(_eb_cfg, "alarm_outbox_path", "data/alarm_outbox.db")) if _eb_cfg else "data/alarm_outbox.db"
        )
        event_bus.enable_alarm_persistence(outbox_path)
    except Exception as e:
        logger.warning("Alarm outbox persistence init failed (best-effort): %s", e)

    # 将EventBus注入InfluxDBStorage，支持降级/恢复事件发布
    if c.influx_storage and hasattr(c.influx_storage, "set_event_bus"):
        c.influx_storage.set_event_bus(event_bus)

    scheduler = CollectScheduler(event_bus, c.influx_storage, c.cache_manager)
    c.scheduler = scheduler
    c.track("scheduler", scheduler)  # FIXED: P0-2 scheduler需追踪

    preprocessor = DataPreprocessor()
    if config.preprocess.enabled:
        scheduler.set_preprocessor(preprocessor)
    c.preprocessor = preprocessor
    c.track("preprocessor", preprocessor)  # FIXED: P0-2 preprocessor需追踪

    c.lifecycle = DeviceLifecycleManager(event_bus)
    c.track("lifecycle", c.lifecycle)  # FIXED: P0-2 lifecycle需追踪

    # 磁盘空间监控：定期检查磁盘使用率，超阈值时发布告警并触发 WAL checkpoint
    try:
        from edgelite.monitoring.disk_monitor import DiskSpaceMonitor

        db_path = str(getattr(getattr(config, "database", None), "path", "data/edgelite.db"))
        disk_monitor = DiskSpaceMonitor(event_bus=event_bus, db_path=db_path)
        disk_monitor.start()
        c.disk_monitor = disk_monitor
        c.track("disk_monitor", disk_monitor)
    except Exception as e:
        logger.warning("DiskSpaceMonitor init failed (best-effort): %s", e)


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

    c.device_service = DeviceService(device_repo, rule_repo, c.scheduler, c.lifecycle, c._repos["template"])
    c.track("device_service", c.device_service)  # FIXED: P0-2 device_service需追踪
    c.rule_service = RuleService(rule_repo, device_repo)
    c.track("rule_service", c.rule_service)  # FIXED: P0-2 rule_service需追踪

    # FIXED: 创建名称解析器，用于告警恢复/确认时查询 rule_name 和 device_name
    class _AlarmNameResolver:
        def __init__(self, rule_repo, device_repo):
            self._rule_repo = rule_repo
            self._device_repo = device_repo

        async def resolve(self, rule_id: str, device_id: str) -> tuple[str, str]:
            rule_name = ""
            device_name = ""
            if rule_id:
                try:
                    rule = await self._rule_repo.get(rule_id)
                    if rule:
                        rule_name = rule.get("name", "")
                # FIXED-P1: 原问题-except Exception: pass 静默吞没异常，改为至少 logger.debug
                except Exception as e:
                    logger.debug("Rule name lookup failed for rule_id=%s: %s", rule_id, e)
            if device_id:
                try:
                    device = await self._device_repo.get(device_id)
                    if device:
                        device_name = device.get("name", "")
                # FIXED-P1: 原问题-except Exception: pass 静默吞没异常，改为至少 logger.debug
                except Exception as e:
                    logger.debug("Device name lookup failed for device_id=%s: %s", device_id, e)
            return rule_name, device_name

    c.alarm_service = AlarmService(
        alarm_repo,
        event_bus=c.event_bus,
        name_resolver=_AlarmNameResolver(rule_repo, device_repo),
    )
    await c.alarm_service.start()  # FIXED: 启动 AlarmService（注册 EventBus handler + cleanup task）
    c.track("alarm_service", c.alarm_service)  # FIXED: P0-2 alarm_service需追踪
    c.data_service = DataService(c.influx_storage, device_repo)
    c.track("data_service", c.data_service)  # FIXED: P0-2 data_service需追踪
    c.video_service = VideoService(c.event_bus)
    c.track("video_service", c.video_service)  # FIXED: P0-2 video_service需追踪
    c.notify_service = NotifyService()
    c.track("notify_service", c.notify_service)  # FIXED: P0-2 notify_service需追踪
    c.system_service = SystemService(
        c.database,
        device_repo,
        rule_repo,
        alarm_repo,
        user_repo,
        c.scheduler,
        c.start_time,
    )
    c.track("system_service", c.system_service)

    audit_service = AuditService(db_path=c.database.audit_db_path)
    await audit_service.initialize()
    c.audit_service = audit_service
    c.track("audit_service", audit_service)


async def bootstrap_evaluator(c: ServiceContainer, config) -> None:
    from edgelite.engine.evaluator import RuleEvaluator

    ai_engine = getattr(c, "ai_engine", None)
    device_repo = c._repos.get("device")
    evaluator = RuleEvaluator(
        c.event_bus,
        c._repos["rule"],
        c._repos["alarm"],
        ai_engine=ai_engine,
        device_repo=device_repo,
    )
    c.evaluator = evaluator
    await evaluator.start()
    c.track("evaluator", evaluator)


async def bootstrap_ws(c: ServiceContainer, config) -> None:
    from edgelite.ws.channels import WebSocketChannels
    from edgelite.ws.manager import ConnectionManager

    # FIXED: 从配置读取 max_connections 和 WS Origin 白名单 [2026-06-29]
    _ws_cfg = getattr(config, "websocket", None) or getattr(config.server, "websocket", None)
    _max_conn = int(getattr(_ws_cfg, "max_connections", 100)) if _ws_cfg else 100
    ws_manager = ConnectionManager(max_connections=_max_conn)
    # FIXED: 设置 WS Origin 白名单，防止 CSWSH 跨站 WebSocket 劫持 [2026-06-29]
    # 与 CORS allowed_origins 共用同一列表；DEV_MODE 或未配置时不校验（开发模式）
    _cors_origins = getattr(getattr(config, "server", None), "cors_allowed_origins", None)
    _dev_mode = getattr(getattr(config, "server", None), "dev_mode", False)
    if _cors_origins and not _dev_mode:
        ws_manager.set_allowed_origins(list(_cors_origins))
    ws_channels = WebSocketChannels(c.event_bus, ws_manager)
    c.ws_manager = ws_manager
    c.track("ws_manager", ws_manager)  # FIXED: P0-2 ws_manager需追踪
    c.ws_channels = ws_channels
    await ws_channels.start()
    c.track("ws_channels", ws_channels)
    # FIXED-P1: 原问题-start_heartbeat()从未调用，客户端异常断开后僵尸连接无限累积直到max_connections
    await ws_manager.start_heartbeat()
    # FIXED: 重放崩溃前未投递的告警事件（必须在 WS 订阅者就绪后执行，否则重放事件无消费者）[2026-06-29]
    # best-effort: 重放失败仅记录日志，不阻塞启动
    try:
        replayed = await c.event_bus.replay_pending_alarms()
        if replayed > 0:
            logger.info("Replayed %d pending alarm events from outbox", replayed)
    except Exception as e:
        logger.warning("Alarm outbox replay failed (best-effort): %s", e)


async def bootstrap_drivers(c: ServiceContainer, config) -> None:
    from edgelite.drivers.registry import get_driver_registry
    from edgelite.engine.plugin_manager import PluginManager

    # FIXED: 原问题-创建新DriverRegistry实例导致驱动注册执行两次，
    # 改用全局单例get_driver_registry()，与DeviceService共享同一实例
    driver_registry = get_driver_registry()
    c.driver_registry = driver_registry

    c.plugin_manager = PluginManager(driver_registry)
    custom_dir = config.drivers.custom_dir if hasattr(config, "drivers") and config.drivers.custom_dir else ""
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
        # FIXED-P0: 内置MQTT Server无认证时发出安全警告
        mqtt_user = getattr(mqtt_server_config, "username", "")
        mqtt_pass = getattr(mqtt_server_config, "password", "")
        allow_no_auth = getattr(mqtt_server_config, "allow_no_auth", False)
        if not mqtt_user or not mqtt_pass:
            _dev_mode = os.environ.get("DEV_MODE", "").lower() in ("true", "1", "yes")
            if not allow_no_auth:
                # 兼容模式：无认证时自动降级为仅本地绑定，不阻止启动
                log_fn = logger.info if _dev_mode else logger.warning
                log_fn(
                    "SECURITY: MQTT Server enabled without authentication! "
                    "Auto-fallback to localhost-only binding (127.0.0.1). "
                    "Set mqtt_server.username and mqtt_server.password for production use."
                )
                # 强制绑定 localhost，防止外部无认证连接
                if not hasattr(mqtt_server_config, "_original_host"):
                    mqtt_server_config._original_host = getattr(mqtt_server_config, "host", "0.0.0.0")
                mqtt_server_config.host = "127.0.0.1"
            else:
                logger.warning(
                    "SECURITY: MQTT Server running without authentication (allow_no_auth=true). Anyone can connect!"
                )

        from edgelite.engine.mqtt_server import MqttServer

        mqtt_server = MqttServer()
        c.mqtt_server = mqtt_server
        await mqtt_server.start(
            {
                "host": getattr(mqtt_server_config, "host", "127.0.0.1"),  # FIXED-P4: 默认绑定localhost，与config层一致
                "port": getattr(mqtt_server_config, "port", 1888),
                "ws_port": getattr(mqtt_server_config, "ws_port", None),
                "username": mqtt_user,
                "password": mqtt_pass,
            }
        )
        c.track("mqtt_server", mqtt_server)


async def bootstrap_platforms(c: ServiceContainer, config) -> None:
    platforms_config = getattr(config, "platforms", None)
    if not platforms_config:
        return

    from edgelite.services.platform_service import PlatformService

    svc = PlatformService(c.platform_handlers)

    for platform_name, platform_conf in platforms_config.items():
        if not isinstance(platform_conf, dict) or not platform_conf.get("enabled", False):
            continue
        try:
            result = await svc.connect(platform_name, platform_conf)
            logger.info("Platform %s integration started: %s", platform_name, result.get("status"))
        except Exception as e:
            logger.error("Platform integration start failed %s: %s", platform_name, e)


async def bootstrap_modbus_slave(c: ServiceContainer, config) -> None:
    modbus_slave_config = getattr(config, "modbus_slave", None)
    if not modbus_slave_config or not getattr(modbus_slave_config, "enabled", False):
        return

    from edgelite.engine.modbus_slave import ModbusSlaveServer

    modbus_slave = ModbusSlaveServer()
    c.modbus_slave = modbus_slave
    await modbus_slave.start(
        {
            "host": getattr(modbus_slave_config, "host", "127.0.0.1"),  # FIXED-P4: 默认绑定localhost，与config层一致
            "port": getattr(modbus_slave_config, "port", 5020),
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
                logger.info(
                    "Auto-created simulator device: %s",
                    dev_config.device_id,
                )

    # FIXED(P3): 原问题-F841未使用局部变量rule_repo/alarm_repo/notify_service(仅被注释代码引用); 修复-删除赋值

    # FIXED-P1: 原问题-此handler与AlarmService.handle_alarm_event->_send_notification重复发送firing通知
    # 导致钉钉/企业微信/邮件/Webhook收到两条重复通知。
    # AlarmService已通过NotificationManager发送通知，此处移除重复handler。
    # 如需按规则路由notify_channels，应在AlarmService._send_notification中查rule的notify_channels。
    # async def handle_alarm_for_notify(event):
    #     if hasattr(event, "rule_id") and event.action == "firing":
    #         rule = await rule_repo.get(event.rule_id)
    #         if rule and rule.get("notify_channels"):
    #             alarm = (
    #                 await alarm_repo.get(event.alarm_id) if hasattr(event, "alarm_id") else None
    #             )
    #             if alarm:
    #                 await notify_service.send_notification(rule["notify_channels"], alarm)
    #
    # c.event_bus.register_handler("AlarmEvent", handle_alarm_for_notify)


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

        # FIX: 将 device_service 和 backhaul_manager 绑定到 endpoint，
        # 使 RPC 反向控制 API 和缓冲区刷新功能可用
        integration_endpoint._device_service = c.device_service  # type: ignore[attr-defined]

        backhaul_manager = BackhaulManager(event_bus=c.event_bus, endpoint=integration_endpoint, buffer_size=1000)
        c.backhaul_manager = backhaul_manager

        # FIX: 回填 backhaul_manager 引用（必须在创建后赋值）
        integration_endpoint._backhaul = backhaul_manager  # type: ignore[attr-defined]

        await backhaul_manager.start()
        c.track("backhaul_manager", backhaul_manager)
        logger.info("Integration endpoint initialized")
    except ImportError:
        logger.warning("Integration module not available")


async def bootstrap_app_updater(c: ServiceContainer, config) -> None:
    try:
        from edgelite.engine.app_updater import AppUpdater

        c.app_updater = AppUpdater()
        logger.info("App updater initialized")
    except ImportError:
        logger.debug("App updater module not available (optional in community edition)")
    except Exception as e:
        logger.debug("App updater init failed: %s (optional)", e)


def _get_project_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


async def bootstrap_ai(c: ServiceContainer, config) -> None:
    ai_config = getattr(config, "ai_inference", None)
    if not ai_config or not getattr(ai_config, "enabled", False):
        logger.info("AI inference engine is disabled in config, skipping initialization")
        return

    try:
        from edgelite.engine.edge_ai_inference import AiInferenceEngine, _check_onnxruntime
        from edgelite.services.ai_service import AiModelService

        models_dir = ai_config.models_dir
        if not models_dir:
            # 优先使用包内 ai_models 目录，不依赖工作目录
            models_dir = str(Path(__file__).resolve().parent / "ai_models")

        if not _check_onnxruntime():
            logger.warning(
                "onnxruntime not installed! AI models will be loaded as 'inactive' status. "
                "Install it to enable AI inference: pip install onnxruntime"
            )

        ai_engine = AiInferenceEngine(
            models_dir=models_dir,
            enabled=True,
        )
        await ai_engine.initialize(event_bus=c.event_bus)
        c.ai_engine = ai_engine
        c.track("ai_engine", ai_engine)

        ai_service = AiModelService(ai_engine, c.database)
        await ai_service.restore_stats_from_db()
        c.ai_service = ai_service
        c.track("ai_service", ai_service)

        models = ai_engine.get_loaded_models()
        active_count = sum(1 for w in models.values() if w.status == "active")
        inactive_count = sum(1 for w in models.values() if w.status == "inactive")
        unavailable_count = sum(1 for w in models.values() if w.status == "unavailable")
        logger.info(
            "AI inference engine initialized: %d active, %d inactive, %d unavailable",
            active_count,
            inactive_count,
            unavailable_count,
        )
        if inactive_count > 0 and not _check_onnxruntime():
            logger.warning(
                "%d models are inactive because onnxruntime is not installed. "
                "Run: pip install onnxruntime  then restart or enable models via API.",
                inactive_count,
            )

        # FIXED: 创建 InferenceScheduler 并注入 MCPToolService，解决 4 个 AI MCP 工具永远不可用问题 [2026-06-29]
        # 原 _ai_scheduler 从未被注入，ai_inference/ai_model_status 工具永远返回 503
        try:
            from edgelite.engine.inference_scheduler import InferenceScheduler

            scheduler = InferenceScheduler()
            await scheduler.start()
            c.inference_scheduler = scheduler
            c.track("inference_scheduler", scheduler)
            # 注册所有已加载的 active 模型到调度器
            for model_id, wrapper in models.items():
                if wrapper.status == "active" and hasattr(wrapper, "predict"):

                    async def _make_infer_fn(mid=model_id, w=wrapper):
                        async def _infer(input_data):
                            return await w.predict(input_data)

                        return _infer

                    infer_fn = await _make_infer_fn()
                    await scheduler.register_model(model_id, infer_fn)
            # 注入到 MCPToolService 单例
            from edgelite.api.mcp import _mcp_tools

            _mcp_tools.set_ai_dependencies(ai_scheduler=scheduler)
            logger.info(
                "InferenceScheduler injected into MCPToolService (%d models registered)", len(scheduler._model_infer_fn)
            )
        except Exception as e:
            logger.warning("InferenceScheduler init failed (MCP AI tools may be unavailable): %s", e)
    except ImportError as e:
        logger.warning("AI inference engine module not available: %s", e)
    except Exception as e:
        logger.warning("AI inference engine init failed: %s", e)


async def bootstrap_video(c: ServiceContainer, config) -> None:
    await c.video_service.init_provider()


async def bootstrap_driver_watchdog(c: ServiceContainer, config) -> None:
    from edgelite.engine.driver_watchdog import get_driver_watchdog

    watchdog = get_driver_watchdog()
    watchdog.set_event_bus(c.event_bus)
    await watchdog.start()
    c.driver_watchdog = watchdog
    c.track("driver_watchdog", watchdog)


async def bootstrap_shadow(c: ServiceContainer, config) -> None:
    from edgelite.services.shadow_service import ShadowService

    shadow_service = ShadowService()
    shadow_service.set_device_service(c.device_service)
    shadow_service.set_event_bus(c.event_bus)
    # SEC-FIX: 注入审计服务，desired 同步写入需记录审计日志
    shadow_service.set_audit_service(getattr(c, "audit_service", None))
    await shadow_service.start()
    c.shadow_service = shadow_service
    c.track("shadow_service", shadow_service)
    logger.info("Shadow service initialized")


async def bootstrap_config_reload(c: ServiceContainer, config) -> None:
    """FIXED-P0: 启动配置热加载器，原代码从未调用get_config_hot_reloader().start()导致热加载功能完全不可用"""
    from edgelite.config_reload import get_config_hot_reloader

    reloader = get_config_hot_reloader()
    await reloader.start()
    c.track("config_hot_reloader", reloader)
    logger.info("Config hot reloader started")


async def bootstrap_log_rotation(c: ServiceContainer, config) -> None:
    """FIXED-P1: 启动日志轮转服务，原代码从未调用get_log_rotation_service().start()导致日志压缩和清理功能不可用"""
    from edgelite.services.system_services import get_log_rotation_service

    log_rotation = get_log_rotation_service()
    await log_rotation.start()
    c.track("log_rotation", log_rotation)
    logger.info("Log rotation service started")

    # FIXED: 启动 TLS 证书过期巡检任务（每 6 小时检查一次）[2026-06-29]
    # 原完全缺失证书过期检测，证书过期后服务静默失效
    async def _cert_expiry_monitor():
        while True:
            try:
                await asyncio.sleep(6 * 3600)  # 6 小时
                from edgelite.engine.tls_security import CertManager

                cert_mgr = CertManager()
                _cfg = config
                # 检查 MQTT TLS 证书
                mqtt_tls = getattr(getattr(_cfg, "mqtt_server", None), "tls", None)
                if mqtt_tls:
                    for label, attr in [("ca", "ca_path"), ("cert", "cert_path")]:
                        path = getattr(mqtt_tls, attr, None)
                        if path and Path(path).exists():
                            try:
                                result = cert_mgr.validate_cert(path)
                                days = result.get("days_remaining")
                                if days is not None and days < 30:
                                    logger.warning(
                                        "TLS certificate %s expiring soon: %s (days_remaining=%d)",
                                        label,
                                        path,
                                        days,
                                    )
                            except Exception as e:
                                logger.debug("Cert check failed for %s: %s", path, e)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Cert expiry monitor error: %s", e)

    # FIXED: 使用简单的包装器跟踪任务，便于 teardown 统一取消 [2026-06-29]
    class _TaskWrapper:
        def __init__(self, task):
            self._task = task

        async def stop(self):
            if not self._task.done():
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._task

    c._cert_monitor = _TaskWrapper(asyncio.create_task(_cert_expiry_monitor(), name="cert-expiry-monitor"))  # type: ignore[attr-defined]
    c.track("cert_monitor", c._cert_monitor)  # type: ignore[attr-defined]


async def _verify_startup_chain(c: ServiceContainer, config) -> list[tuple[str, bool, str]]:
    """P0-4.1: 启动验证链，汇总各关键组件可用性"""
    results: list[tuple[str, bool, str]] = []

    # 1. 安全配置验证
    try:
        _ensure_secret_key(config)
        results.append(("secret_key", True, "ok"))
    except Exception as e:
        results.append(("secret_key", False, str(e)))

    # 2. 数据库连接验证（P0-3.2已增强SELECT 1，在bootstrap_storage内执行；
    #    此处确认database已初始化且可用）
    db = getattr(c, "database", None)
    if db and hasattr(db, "_engine") and db._engine is not None:
        try:
            async with db.get_session() as session:
                from sqlalchemy import text

                await session.execute(text("SELECT 1"))
            results.append(("database", True, "ok"))
        except Exception as e:
            results.append(("database", False, f"SELECT 1 failed: {e}"))
    else:
        results.append(("database", False, "not initialized"))

    # 3. InfluxDB/降级存储验证
    influx = getattr(c, "influx_storage", None)
    if influx:
        try:
            ok = await influx.check_health()
            if ok:
                results.append(("influxdb", True, "ok"))
            # FIXED-P0: using_fallback改为async，需await
            elif await influx.using_fallback():
                results.append(("influxdb", True, "degraded: using SQLite fallback"))
            else:
                results.append(("influxdb", False, "unavailable and no fallback"))
        except Exception as e:
            results.append(("influxdb", False, str(e)))
    else:
        results.append(("influxdb", False, "not initialized"))

    # 4. EventBus验证
    event_bus = getattr(c, "event_bus", None)
    results.append(("event_bus", True, "ok" if event_bus else "not initialized"))

    # 5. Scheduler验证
    scheduler = getattr(c, "scheduler", None)
    results.append(("scheduler", True, "ok" if scheduler else "not initialized"))

    return results


async def _auto_install_deps() -> None:
    """启动时检测缺失依赖，缺失则记录 ERROR 并退出。

    9#修复说明：
    - 原 _auto_install_deps 会自动 pip install 缺失依赖，存在两类问题：
      1. 供应链风险：运行时自动从 PyPI 拉取，可能遭遇 typo-squatting 或恶意包替换；
      2. 在 read_only 容器（Dockerfile USER appuser + read_only: true）中 pip install 必然失败，
         且容器内无 pip 缓存目录写权限，导致日志噪音与启动失败被掩盖为 warning。
    - 修复后：仅在启动时校验依赖是否齐全，缺失则直接 raise RuntimeError 拒绝启动，
      引导运维通过 requirements.txt 在构建阶段补齐依赖。
    """
    import importlib

    # requirements.txt 中声明的包名 → Python import 名 的映射
    # 只检查核心依赖（可选依赖由用户按需安装）
    _REQUIRED_PACKAGES: dict[str, str] = {
        "fastapi": "fastapi",
        "uvicorn": "uvicorn",
        "pydantic": "pydantic",
        "python-dotenv": "dotenv",
        "aiosqlite": "aiosqlite",
        "influxdb-client": "influxdb_client",
        "pymodbus": "pymodbus",
        "httpx": "httpx",
        "pyyaml": "yaml",
        "PyJWT": "jwt",
        "bcrypt": "bcrypt",
        "psutil": "psutil",
        "aiomqtt": "aiomqtt",
        "asyncua": "asyncua",
        "pyserial": "serial",
        "pymcprotocol": "pymcprotocol",
        "fins": "fins",
        "pylogix": "pylogix",
        "python-snap7": "snap7",
        "onvif-zeep": "onvif",
        "cryptography": "cryptography",
        "zeroconf": "zeroconf",
        "aiohttp": "aiohttp",
        "RestrictedPython": "RestrictedPython",
        "lxml": "lxml",
        "pyserial-asyncio": "serial_asyncio",
        # sparkplugb: PyPI 上不存在该包名，自动安装会持续失败产生噪音日志。
        # 如需 SparkPlug B 支持，请参考官方文档手动安装依赖。
        "sqlalchemy": "sqlalchemy",
        "alembic": "alembic",
        "numpy": "numpy",
        "onnx": "onnx",
        "onnxruntime": "onnxruntime",
        "opencv-python-headless": "cv2",
    }

    missing: list[str] = []
    for pip_name, import_name in _REQUIRED_PACKAGES.items():
        try:
            importlib.import_module(import_name)
        except ImportError:
            missing.append(pip_name)

    if not missing:
        return

    # 9#修复: 移除运行时 pip install 逻辑，缺失依赖直接拒绝启动并记录 ERROR，
    # 避免供应链风险与 read_only 容器中失败被静默掩盖
    missing_str = ", ".join(missing)
    logger.error(
        "Missing %d required dependencies: %s. "
        "Auto-install at runtime has been disabled (supply-chain risk + read-only container incompatibility). "
        "Please install via 'pip install -r requirements.txt' or rebuild the Docker image.",
        len(missing),
        missing_str,
    )
    raise RuntimeError(
        f"Missing required dependencies: {missing_str}. "
        f"Install via 'pip install -r requirements.txt' or rebuild Docker image. "
        f"Auto-install at runtime has been disabled for security and read-only container compatibility."
    )


async def bootstrap_all(c: ServiceContainer, config) -> None:
    _ensure_secret_key(config)

    if not config.influxdb.token:
        logger.warning(
            "InfluxDB Token not configured, time-series data storage unavailable. "
            "Set via EDGELITE_INFLUXDB__TOKEN env var!"
        )  # FIXED-P3: 中文日志→英文

    logging.basicConfig(level=config.logging.level, format=config.logging.format)

    # FIXED: 抑制 pymodbus v3 弃用警告（ModbusDeviceContext/ModbusServerContext 等）
    # 这些 API 在 v4 中将被移除，当前版本使用 v3 API 是正确的
    import warnings as _warnings

    _warnings.filterwarnings("ignore", message=r".*deprecated and will be removed in v4.*", category=DeprecationWarning)
    # FIXED: 抑制 pymodbus 通过 logging 模块输出的弃用警告（v3 API 在当前版本是正确的）
    logging.getLogger("pymodbus.logging").setLevel(logging.ERROR)
    # FIXED: 抑制 requests 库的 urllib3/chardet 版本不匹配警告（无害，第三方库版本兼容性问题）
    try:
        from requests.exceptions import RequestsDependencyWarning

        _warnings.filterwarnings("ignore", category=RequestsDependencyWarning)
    except ImportError:
        pass

    # FIXED-P0: 注册 SensitiveFilter 到根 logger，自动脱敏日志中的密码/Token/密钥
    try:
        from edgelite.security.data_masking import SensitiveFilter

        _sensitive_filter = SensitiveFilter()
        logging.getLogger().addFilter(_sensitive_filter)
    except Exception as _sf_err:
        logger.warning("SensitiveFilter registration failed: %s", _sf_err)

    # FIXED: 抑制 amqtt 库在 Windows 上的已知噪音日志
    # on_socket_unregister_write / "No data from" / "Failed to initialize client session: No more data"
    # 这些是客户端断开时的正常清理过程，amqtt 库错误地以 ERROR 级别记录
    class _AmqttNoiseFilter(logging.Filter):
        _SUPPRESSED_FRAGMENTS = (
            "on_socket_unregister_write",
            "No data from",
            "Failed to initialize client session: No more data",
        )

        def filter(self, record: logging.LogRecord) -> bool:
            msg = record.getMessage()
            for frag in self._SUPPRESSED_FRAGMENTS:
                if frag in msg:
                    return False
            return True

    for _logger_name in ("mqtt", "amqtt.broker", "amqtt"):
        logging.getLogger(_logger_name).addFilter(_AmqttNoiseFilter())

    # FIXED: 日志目录优先使用 config.logging.log_dir，回退到 _LOG_DIR 常量 [2026-06-29]
    # 原硬编码 _LOG_DIR，运维修改 config.logging.log_dir 无效
    _cfg_log_dir = getattr(config.logging, "log_dir", None) or _LOG_DIR
    log_dir = Path(_cfg_log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    # FIXED: 日志文件大小/备份数优先使用 config 值，回退到常量 [2026-06-29]
    _cfg_max_bytes = getattr(config.logging, "max_bytes", None) or _LOG_MAX_BYTES
    _cfg_backup_count = getattr(config.logging, "backup_count", None) or _LOG_BACKUP_COUNT

    # FIXED: 支持结构化 JSON 日志输出 [2026-06-29]
    # 原 config.logging.json_format 定义但从未生效，运维设 EDGELITE_LOGGING__JSON_FORMAT=true 无效果
    _use_json = bool(getattr(config.logging, "json_format", False))
    _log_formatter: logging.Formatter
    if _use_json:
        try:
            from edgelite.engine.structured_logger import StructuredFormatter

            _log_formatter = StructuredFormatter()
        except Exception as e:
            logger.warning("StructuredFormatter unavailable, falling back to plain format: %s", e)
            _log_formatter = logging.Formatter(config.logging.format)
    else:
        _log_formatter = logging.Formatter(config.logging.format)

    file_handler = RotatingFileHandler(
        log_dir / "edgelite.log",
        maxBytes=_cfg_max_bytes,
        backupCount=_cfg_backup_count,
        encoding="utf-8",
    )
    # FIXED-P1: 原问题-getattr(logging, "info")返回函数而非级别常量；改为大写后取值并校验类型
    _log_level = getattr(logging, str(config.logging.level).upper(), None)
    if not isinstance(_log_level, int):
        _log_level = logging.INFO
    file_handler.setLevel(_log_level)
    file_handler.setFormatter(_log_formatter)
    logging.getLogger().addHandler(file_handler)

    error_handler = RotatingFileHandler(
        log_dir / "edgelite-error.log",
        maxBytes=_cfg_max_bytes,
        backupCount=_cfg_backup_count,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(_log_formatter)
    logging.getLogger().addHandler(error_handler)

    logger.info("Log file output to: %s", log_dir.resolve())

    # 初始化日志聚合器 — 注册Handler到root logger，自动收集所有日志到内存
    try:
        from edgelite.engine.log_aggregator import get_log_aggregator

        _log_agg = get_log_aggregator()
        logger.info("Log aggregator initialized (max_entries=%d)", _log_agg._max_entries)
    except Exception as _la_err:
        logger.warning("Log aggregator init failed: %s", _la_err)

    logger.info("EdgeLiteGateway starting...")

    from edgelite.engine.graceful_restarter import GracefulRestarter

    restart_info = GracefulRestarter.check_and_cleanup_marker()
    if restart_info:
        logger.info(
            "Graceful restart completed: %s -> %s (success=%s)",
            restart_info.get("old_version", "?"),
            restart_info.get("new_version", "?"),
            restart_info.get("success", False),
        )

    c.config = config

    # R5-F-01: 初始化 SecretManager（在配置加载之后、其他服务初始化之前）
    # 主密钥来源优先级：显式参数 > EDGELITE_MASTER_KEY 环境变量 > data/.master_key 文件
    # 未配置主密钥时：开发模式（DEV_MODE=true）警告继续，生产模式拒绝启动
    from edgelite.security.secret_manager import init_secret_manager

    init_secret_manager()
    logger.info("SecretManager initialized")

    # Auto-install missing dependencies from requirements.txt on startup
    await _auto_install_deps()

    # P0-4.1: 启动验证链 — 每个bootstrap步骤失败时收集错误
    _bootstrap_steps = [
        ("storage", bootstrap_storage),
        ("engine", bootstrap_engine),
        ("driver_watchdog", bootstrap_driver_watchdog),
        ("services", bootstrap_services),
        ("ai", bootstrap_ai),
        ("evaluator", bootstrap_evaluator),
        ("ws", bootstrap_ws),
        ("video", bootstrap_video),
        ("drivers", bootstrap_drivers),
        ("mqtt", bootstrap_mqtt),
        ("platforms", bootstrap_platforms),
        ("modbus_slave", bootstrap_modbus_slave),
        ("devices", bootstrap_devices),
        ("shadow", bootstrap_shadow),
        ("integration", bootstrap_integration),
        ("app_updater", bootstrap_app_updater),
        ("config_reload", bootstrap_config_reload),
        ("log_rotation", bootstrap_log_rotation),
    ]
    _step_errors: list[tuple[str, Exception]] = []
    for step_name, step_fn in _bootstrap_steps:
        try:
            await step_fn(c, config)
        except Exception as e:
            _step_errors.append((step_name, e))
            logger.critical("Bootstrap step '%s' failed: %s", step_name, e)
            break  # FIXED-P1: 原问题-步骤失败后仍继续执行依赖步骤导致级联AttributeError；改为立即中断

    # P0-4.1: 启动后验证链
    _verification_errors: list[str] = []
    if not _step_errors:
        try:
            verification_results = await _verify_startup_chain(c, config)
            for comp, ok, msg in verification_results:
                if not ok:
                    _verification_errors.append(f"{comp}: {msg}")
                    logger.critical("Startup verification failed — %s: %s", comp, msg)
        except Exception as e:
            _verification_errors.append(f"verification_chain: {e}")
            logger.critical("Startup verification chain exception: %s", e)

    # 任一失败 → 清理已初始化资源并拒绝启动
    if _step_errors or _verification_errors:
        try:
            await teardown(c)
        except Exception as te:
            logger.warning("Teardown during startup failure raised: %s", te)

        error_detail = []
        for name, exc in _step_errors:
            error_detail.append(f"step[{name}]: {exc}")
        for ve in _verification_errors:
            error_detail.append(f"verify: {ve}")
        raise RuntimeError(f"EdgeLite startup failed, {len(error_detail)} error(s): " + "; ".join(error_detail))

    logger.info("EdgeLiteGateway startup complete (port=%d)", config.server.port)


async def _safe_stop(resource, method_name: str, label: str) -> None:
    try:
        await getattr(resource, method_name)()
    except Exception as e:
        logger.warning("Shutdown %s exception: %s", label, e)


async def teardown(c: ServiceContainer) -> None:
    logger.info("EdgeLiteGateway shutting down...")

    # P0-4.3: 有序优雅关闭，按依赖逆序停止

    # 1. 停止所有采集任务
    if c.scheduler:
        await _safe_stop(c.scheduler, "stop_all", "scheduler")

    # 1.5 停止磁盘空间监控（在数据库关闭前停止，避免 WAL checkpoint 与 DB 关闭竞态）
    if c.disk_monitor:
        await _safe_stop(c.disk_monitor, "stop", "disk_monitor")

    # 2. 停止所有驱动
    driver_registry = getattr(c, "driver_registry", None)
    if driver_registry and hasattr(driver_registry, "stop_all"):
        await _safe_stop(driver_registry, "stop_all", "driver_registry")
    elif driver_registry and hasattr(driver_registry, "stop"):
        await _safe_stop(driver_registry, "stop", "driver_registry")
    driver_watchdog = getattr(c, "driver_watchdog", None)
    if driver_watchdog:
        await _safe_stop(driver_watchdog, "stop", "driver_watchdog")

    # 先关闭AI引擎释放ONNX资源（从_initialized列表中移除，避免后续重复关闭）
    if c.ai_engine:
        await _safe_stop(c.ai_engine, "shutdown", "ai_engine")
        c._initialized = [(n, r) for n, r in c._initialized if r is not c.ai_engine]

    # FIXED: 关闭 InferenceScheduler 释放推理线程池 [2026-06-29]
    inference_scheduler = getattr(c, "inference_scheduler", None)
    if inference_scheduler:
        await _safe_stop(inference_scheduler, "stop", "inference_scheduler")
        c._initialized = [(n, r) for n, r in c._initialized if r is not inference_scheduler]

    # 按注册逆序清理已初始化资源（跳过已单独关闭的scheduler/driver相关）
    _already_stopped = {c.scheduler, driver_registry, driver_watchdog, c.ai_engine, inference_scheduler}
    for name, resource in reversed(c._initialized):
        if resource in _already_stopped:
            continue
        try:
            if hasattr(resource, "shutdown"):
                await resource.shutdown()
            elif hasattr(resource, "close"):
                await resource.close()
            elif hasattr(resource, "stop"):
                await resource.stop()
            elif hasattr(resource, "stop_all"):
                await resource.stop_all()
        except Exception as e:
            logger.warning("Shutdown %s exception: %s", name, e)

    # 2.5 停止孤儿数据补偿任务
    if c.cache_manager and hasattr(c.cache_manager, "stop_orphan_compaction"):
        c.cache_manager.stop_orphan_compaction()

    # 3. 关闭WebSocket连接（必须在数据库关闭之前，避免WebSocket处理中的请求因DB已关闭而失败）
    if c.ws_manager and hasattr(c.ws_manager, "close"):
        await _safe_stop(c.ws_manager, "close", "ws_manager")

    # 4. 关闭数据库连接
    if c.database and hasattr(c.database, "close"):
        await _safe_stop(c.database, "close", "database")

    # 5. 关闭MQTT连接
    if c.mqtt_forwarder and hasattr(c.mqtt_forwarder, "close"):
        await _safe_stop(c.mqtt_forwarder, "close", "mqtt_forwarder")

    # 6. 等待应用后台任务完成（超时5s后强制取消）
    import asyncio

    current = asyncio.current_task()
    # R7-S-12: 原白名单 edgelite_ 前缀匹配不到任何任务（代码库中无此命名模式），
    # 导致后台任务无法被取消而泄漏。改为黑名单模式：排除 uvicorn/asyncio/starlette/fastapi
    # 等系统内部任务，取消所有其他应用后台任务。
    _SYSTEM_TASK_PREFIXES = ("uvicorn", "asyncio", "lifespan", "starlette", "fastapi")

    def _is_system_task(task: asyncio.Task) -> bool:
        """判断是否为系统内部任务（uvicorn/asyncio/starlette/fastapi）"""
        name = task.get_name() or ""
        if name.startswith(_SYSTEM_TASK_PREFIXES):
            return True
        # 部分系统任务未显式命名（自动生成 Task-N），通过协程所属模块进一步识别
        coro = getattr(task, "get_coro", lambda: None)()
        if coro is not None:
            frame = getattr(coro, "cr_frame", None)
            if frame is not None:
                mod = frame.f_globals.get("__name__", "") or ""
                if mod.startswith(_SYSTEM_TASK_PREFIXES):
                    return True
        return False

    pending = [t for t in asyncio.all_tasks() if t is not current and not _is_system_task(t)]
    if pending:
        logger.info("Waiting for %d pending async task(s) to complete...", len(pending))
        try:
            await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=5.0)
        except TimeoutError:  # FIXED-P1: 原问题-Python<3.11中asyncio.TimeoutError不是TimeoutError子类，需同时捕获
            logger.warning("%d task(s) did not complete within 5s, force cancelling", len(pending))
            for t in pending:
                t.cancel(msg="shutdown timeout")

    for name, handler in list(c.platform_handlers.items()):
        try:
            await handler.disconnect()
        except Exception as e:
            logger.warning("Platform shutdown exception %s: %s", name, e)

    if c.event_bus:
        c.event_bus.unregister_all()

    logger.info("EdgeLiteGateway shutdown complete")
