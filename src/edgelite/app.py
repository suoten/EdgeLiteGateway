"""FastAPI应用工厂"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query

from edgelite.config import get_config, load_config

logger = logging.getLogger(__name__)


@dataclass
class AppState:
    """应用全局状态"""

    db_conn: Any = None
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
    audit_service: Any = None
    driver_registry: Any = None
    mqtt_forwarder: Any = None
    mqtt_server: Any = None
    opcua_server: Any = None
    modbus_slave: Any = None
    platform_handlers: dict = field(default_factory=dict)
    start_time: float = field(default_factory=time.time)
    integration_endpoint: Any = None
    integration_dispatcher: Any = None
    backhaul_manager: Any = None
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    config: Any = None
    max_ws_connections: int = 100


# 全局应用状态
_app_state = AppState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    config = get_config()

    # 安全检查：检测默认密钥
    _WEAK_SECRET_KEYS = {"change-me-in-production", "your-secret-key-at-least-32-characters"}
    if config.security.secret_key in _WEAK_SECRET_KEYS or len(config.security.secret_key) < 32:
        raise RuntimeError(
            f"安全错误: JWT密钥使用默认值或过短({len(config.security.secret_key)}字符)，"
            "请通过 EDGELITE_SECURITY__SECRET_KEY 环境变量设置至少32字符的随机密钥！"
        )

    _WEAK_INFLUX_TOKENS = {"edgelite-token-change-me", "your-influxdb-token-here"}
    if config.influxdb.token in _WEAK_INFLUX_TOKENS:
        logger.warning("⚠️  安全警告: InfluxDB Token使用默认值，请通过 EDGELITE_INFLUXDB_TOKEN 环境变量设置！")

    # 配置日志
    logging.basicConfig(level=config.logging.level, format=config.logging.format)

    logger.info("EdgeLiteGateway 启动中...")

    initialized: list[tuple[str, Any]] = []

    try:
        # 1. 初始化数据库
        from edgelite.storage.database import Database
        database = Database()
        conn = await database.connect()
        await database.init_tables()
        _app_state.database = database
        _app_state.db_conn = conn
        initialized.append(("database", database))
        initialized.append(("db_conn", conn))
        logger.info("数据库初始化完成")

        # 2. 初始化InfluxDB
        from edgelite.storage.influx_storage import InfluxDBStorage
        influx = InfluxDBStorage()
        await influx.connect()
        _app_state.influx_storage = influx
        initialized.append(("influx", influx))

        # 3. 初始化缓存管理器
        from edgelite.storage.cache import CacheManager
        _app_state.cache_manager = CacheManager(conn)

        # 4. 初始化事件总线
        from edgelite.engine.event_bus import EventBus
        event_bus = EventBus()
        _app_state.event_bus = event_bus
        initialized.append(("event_bus", event_bus))

        # 5. 初始化仓储
        from edgelite.storage.sqlite_repo import DeviceRepo, RuleRepo, AlarmRepo, UserRepo, AuditRepo
        _app_state.config = config
        write_lock = _app_state.write_lock
        device_repo = DeviceRepo(conn, write_lock)
        rule_repo = RuleRepo(conn, write_lock)
        alarm_repo = AlarmRepo(conn, write_lock)
        user_repo = UserRepo(conn, write_lock)

        # 6. 初始化采集调度器
        from edgelite.engine.scheduler import CollectScheduler
        scheduler = CollectScheduler(event_bus, influx, _app_state.cache_manager)
        _app_state.scheduler = scheduler
        initialized.append(("scheduler", scheduler))

        # 7. 初始化设备生命周期管理
        from edgelite.engine.lifecycle import DeviceLifecycleManager
        lifecycle = DeviceLifecycleManager(event_bus)
        _app_state.lifecycle = lifecycle

        # 8. 初始化业务服务
        from edgelite.services.device_service import DeviceService
        from edgelite.services.rule_service import RuleService
        from edgelite.services.alarm_service import AlarmService
        from edgelite.services.data_service import DataService
        from edgelite.services.video_service import VideoService
        from edgelite.services.system_service import SystemService
        from edgelite.services.notify_service import NotifyService

        device_service = DeviceService(device_repo, rule_repo, scheduler, lifecycle)
        rule_service = RuleService(rule_repo, device_repo)
        alarm_service = AlarmService(alarm_repo)
        data_service = DataService(influx, device_repo)
        video_service = VideoService(event_bus)
        notify_service = NotifyService()
        system_service = SystemService(database, device_repo, rule_repo, alarm_repo, user_repo, scheduler, _app_state.start_time)

        _app_state.device_service = device_service
        _app_state.rule_service = rule_service
        _app_state.alarm_service = alarm_service
        _app_state.data_service = data_service
        _app_state.video_service = video_service
        _app_state.notify_service = notify_service
        _app_state.system_service = system_service

        # 8.5 初始化审计日志服务
        try:
            from edgelite.services.audit_service import AuditService
            audit_service = AuditService(db_path=config.database.sqlite_path)
            await audit_service.initialize()
            _app_state.audit_service = audit_service
            initialized.append(("audit_service", audit_service))
            logger.info("审计日志服务已初始化")
        except Exception as e:
            logger.warning("审计日志服务初始化失败: %s", e)

        # 9. 初始化规则评估器
        from edgelite.engine.evaluator import RuleEvaluator
        evaluator = RuleEvaluator(event_bus, rule_repo, alarm_repo)
        _app_state.evaluator = evaluator
        await evaluator.start()
        initialized.append(("evaluator", evaluator))

        # 10. 初始化WebSocket
        from edgelite.ws.manager import ConnectionManager
        from edgelite.ws.channels import WebSocketChannels
        ws_manager = ConnectionManager()
        ws_channels = WebSocketChannels(event_bus, ws_manager)
        _app_state.ws_manager = ws_manager
        _app_state.ws_channels = ws_channels
        await ws_channels.start()
        initialized.append(("ws_channels", ws_channels))

        # 11. 初始化视频提供者
        await video_service.init_provider()

        # 12. 初始化驱动注册表
        from edgelite.drivers.registry import DriverRegistry
        driver_registry = DriverRegistry()
        driver_registry.auto_discover()
        _app_state.driver_registry = driver_registry

        # 13. 初始化MQTT北向转发器
        from edgelite.engine.mqtt_forwarder import MqttForwarder
        mqtt_forwarder = MqttForwarder()
        _app_state.mqtt_forwarder = mqtt_forwarder
        await mqtt_forwarder.start(event_bus)
        initialized.append(("mqtt_forwarder", mqtt_forwarder))

        # 14. 初始化内置MQTT Server 
        mqtt_server_config = getattr(config, "mqtt_server", None)
        if mqtt_server_config and getattr(mqtt_server_config, "enabled", False):
            from edgelite.engine.mqtt_server import MqttServer
            mqtt_server = MqttServer()
            _app_state.mqtt_server = mqtt_server
            await mqtt_server.start({
                "host": getattr(mqtt_server_config, "host", "0.0.0.0"),
                "port": getattr(mqtt_server_config, "port", 1888),
                "ws_port": getattr(mqtt_server_config, "ws_port", None),
                "username": getattr(mqtt_server_config, "username", ""),
                "password": getattr(mqtt_server_config, "password", ""),
            })
            initialized.append(("mqtt_server", mqtt_server))

        # 15. 初始化北向平台对接 
        platforms_config = getattr(config, "platforms", None)
        if platforms_config:
            for platform_name, platform_conf in platforms_config.items():
                if not isinstance(platform_conf, dict) or not platform_conf.get("enabled", False):
                    continue
                try:
                    if platform_name == "iotsharp":
                        from edgelite.platform.iotsharp import IoTSharpHandler
                        handler = IoTSharpHandler()
                        await handler.connect(platform_conf)
                        _app_state.platform_handlers["iotsharp"] = handler
                        logger.info("IoTSharp平台对接已启动")
                    elif platform_name == "thingsboard":
                        from edgelite.platform.thingsboard import ThingsBoardHandler
                        handler = ThingsBoardHandler()
                        await handler.connect(platform_conf)
                        _app_state.platform_handlers["thingsboard"] = handler
                        logger.info("ThingsBoard平台对接已启动")
                    elif platform_name == "huawei_iotda":
                        from edgelite.platform.huawei_iotda import HuaweiIoTDAHandler
                        handler = HuaweiIoTDAHandler()
                        await handler.connect(platform_conf)
                        _app_state.platform_handlers["huawei_iotda"] = handler
                        logger.info("华为云IoTDA平台对接已启动")
                    elif platform_name == "thingscloud":
                        from edgelite.platform.thingscloud import ThingsCloudHandler
                        handler = ThingsCloudHandler()
                        await handler.connect(platform_conf)
                        _app_state.platform_handlers["thingscloud"] = handler
                        logger.info("ThingsCloud平台对接已启动")
                except Exception as e:
                    logger.error("平台对接启动失败 %s: %s", platform_name, e)

        # 16. 初始化内置OPC UA Server 
        opcua_server_config = getattr(config, "opcua_server", None)
        if opcua_server_config and getattr(opcua_server_config, "enabled", False):
            from edgelite.engine.opcua_server import OpcUaServer
            opcua_server = OpcUaServer()
            _app_state.opcua_server = opcua_server
            await opcua_server.start({
                "host": getattr(opcua_server_config, "host", "0.0.0.0"),
                "port": getattr(opcua_server_config, "port", 4840),
                "namespace": getattr(opcua_server_config, "namespace", "urn:edgelite:gateway"),
            })
            initialized.append(("opcua_server", opcua_server))

        # 17. 初始化内置Modbus Slave 
        modbus_slave_config = getattr(config, "modbus_slave", None)
        if modbus_slave_config and getattr(modbus_slave_config, "enabled", False):
            from edgelite.engine.modbus_slave import ModbusSlaveServer
            modbus_slave = ModbusSlaveServer()
            _app_state.modbus_slave = modbus_slave
            await modbus_slave.start({
                "host": getattr(modbus_slave_config, "host", "0.0.0.0"),
                "port": getattr(modbus_slave_config, "port", 502),
                "holding_size": getattr(modbus_slave_config, "holding_size", 1000),
                "input_size": getattr(modbus_slave_config, "input_size", 1000),
            })
            initialized.append(("modbus_slave", modbus_slave))

        # 14. 加载已有设备并恢复采集
        await device_service.load_existing_devices()

        # 15. 自动创建模拟设备
        if config.simulator.auto_create:
            for dev_config in config.simulator.default_devices:
                existing = await device_repo.get(dev_config.device_id)
                if existing is None:
                    points_data = [p.model_dump() for p in dev_config.points]
                    await device_service.create_simulator({
                        "device_id": dev_config.device_id,
                        "name": dev_config.name,
                        "points": points_data,
                        "collect_interval": dev_config.collect_interval,
                    })
                    logger.info("自动创建模拟设备: %s", dev_config.device_id)

        # 注册告警事件处理器（通知）
        async def handle_alarm_for_notify(event):
            if hasattr(event, "rule_id") and event.action == "firing":
                rule = await rule_repo.get(event.rule_id)
                if rule and rule.get("notify_channels"):
                    alarm = await alarm_repo.get(event.alarm_id) if hasattr(event, "alarm_id") else None
                    if alarm:
                        await notify_service.send_notification(rule["notify_channels"], alarm)

        event_bus.register_handler("AlarmEvent", handle_alarm_for_notify)

        # 16. 初始化联调集成端点
        try:
            from edgelite.engine.integration.endpoint import IntegrationEndpoint
            from edgelite.engine.integration.dispatcher import MessageDispatcher
            from edgelite.engine.integration.backhaul import BackhaulManager

            integration_dispatcher = MessageDispatcher()
            integration_dispatcher.register_service("device_service", device_service)
            integration_endpoint = IntegrationEndpoint(dispatcher=integration_dispatcher)
            _app_state.integration_dispatcher = integration_dispatcher
            _app_state.integration_endpoint = integration_endpoint

            backhaul_manager = BackhaulManager(event_bus=event_bus, endpoint=integration_endpoint, buffer_size=1000)
            _app_state.backhaul_manager = backhaul_manager
            await backhaul_manager.start()
            initialized.append(("backhaul_manager", backhaul_manager))
            logger.info("联调集成端点已初始化")
        except ImportError:
            logger.warning("联调集成模块不可用")

    except Exception as init_err:
        logger.error("初始化失败: %s，开始清理已初始化资源", init_err)
        for name, resource in reversed(initialized):
            try:
                if hasattr(resource, 'close'):
                    await resource.close()
                elif hasattr(resource, 'stop'):
                    await resource.stop()
                elif hasattr(resource, 'stop_all'):
                    await resource.stop_all()
            except Exception as cleanup_err:
                logger.warning("清理%s失败: %s", name, cleanup_err)
        raise

    logger.info("EdgeLiteGateway 启动完成 (port=%d)", config.server.port)

    yield

    # 关闭
    logger.info("EdgeLiteGateway 关闭中...")
    try:
        if _app_state.backhaul_manager:
            await _app_state.backhaul_manager.stop()
    except Exception as e:
        logger.warning("关闭backhaul_manager异常: %s", e)
    try:
        await ws_channels.stop()
    except Exception as e:
        logger.warning("关闭ws_channels异常: %s", e)
    try:
        await evaluator.stop()
    except Exception as e:
        logger.warning("关闭evaluator异常: %s", e)
    try:
        await scheduler.stop_all()
    except Exception as e:
        logger.warning("关闭scheduler异常: %s", e)
    if _app_state.event_bus:
        _app_state.event_bus.unregister_all()
    for name, handler in list(_app_state.platform_handlers.items()):
        try:
            await handler.disconnect()
        except Exception as e:
            logger.warning("平台对接关闭异常 %s: %s", name, e)
    for svc_name, svc in [("mqtt_forwarder", _app_state.mqtt_forwarder), ("mqtt_server", _app_state.mqtt_server), ("opcua_server", _app_state.opcua_server), ("modbus_slave", _app_state.modbus_slave)]:
        if svc:
            try:
                await svc.stop()
            except Exception as e:
                logger.warning("关闭%s异常: %s", svc_name, e)
    try:
        await video_service.close()
    except Exception as e:
        logger.warning("关闭video_service异常: %s", e)
    try:
        await notify_service.close()
    except Exception as e:
        logger.warning("关闭notify_service异常: %s", e)
    if _app_state.audit_service:
        try:
            await _app_state.audit_service.close()
        except Exception as e:
            logger.warning("关闭audit_service异常: %s", e)
    try:
        await influx.close()
    except Exception as e:
        logger.warning("关闭influx异常: %s", e)
    try:
        await database.close()
    except Exception as e:
        logger.warning("关闭database异常: %s", e)
    logger.info("EdgeLiteGateway 已关闭")


def create_app() -> FastAPI:
    """创建FastAPI应用"""
    config = get_config()

    app = FastAPI(
        title="EdgeLiteGateway",
        description="轻量级边缘计算物联网网关 API",
        version="1.0.0",
        lifespan=lifespan,
    )

    # CORS
    from fastapi.middleware.cors import CORSMiddleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.server.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-API-Key"],
    )

    # 注册路由
    from edgelite.api import auth, devices, rules, alarms, data, video, system, users
    app.include_router(auth.router)
    app.include_router(devices.router)
    app.include_router(rules.router)
    app.include_router(alarms.router)
    app.include_router(data.router)
    app.include_router(video.router)
    app.include_router(system.router)
    app.include_router(users.router)

    # 审计日志路由
    try:
        from edgelite.api.audit import router as audit_router
        app.include_router(audit_router)
    except Exception as e:
        logger.warning("审计日志路由注册失败: %s", e)

    # 驱动配置管理路由
    try:
        from edgelite.api.drivers import router as drivers_router
        app.include_router(drivers_router)
    except Exception as e:
        logger.warning("驱动配置路由注册失败: %s", e)

    # 平台配置管理路由
    try:
        from edgelite.api.platforms import router as platforms_router
        app.include_router(platforms_router)
    except Exception as e:
        logger.warning("平台配置路由注册失败: %s", e)

    # 表达式管理路由
    try:
        from edgelite.api.expressions import router as expressions_router
        app.include_router(expressions_router)
    except Exception as e:
        logger.warning("表达式管理路由注册失败: %s", e)

    # 联调集成路由
    try:
        from edgelite.api.integration import router as integration_router
        app.include_router(integration_router)
    except Exception as e:
        logger.warning("联调集成路由注册失败: %s", e)

    # WebSocket路由
    @app.websocket("/ws/v1/realtime")
    async def ws_realtime(websocket: WebSocket, token: str = Query(...)):
        if not await _app_state.ws_manager.connect(websocket, "realtime", token):
            return
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            await _app_state.ws_manager.disconnect(websocket, "realtime")

    @app.websocket("/ws/v1/alarm")
    async def ws_alarm(websocket: WebSocket, token: str = Query(...)):
        if not await _app_state.ws_manager.connect(websocket, "alarm", token):
            return
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            await _app_state.ws_manager.disconnect(websocket, "alarm")

    @app.websocket("/ws/v1/device")
    async def ws_device(websocket: WebSocket, token: str = Query(...)):
        if not await _app_state.ws_manager.connect(websocket, "device", token):
            return
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            await _app_state.ws_manager.disconnect(websocket, "device")

    # 联调集成WebSocket端点
    @app.websocket("/ws/v1/integration")
    async def ws_integration(websocket: WebSocket, token: str = Query(...)):
        if not _app_state.integration_endpoint:
            await websocket.close(code=1003, reason="Integration not available")
            return
        from edgelite.security.jwt import decode_token
        try:
            if not decode_token(token):
                await websocket.close(code=4001, reason="Invalid token")
                return
        except Exception:
            await websocket.close(code=4001, reason="Auth failed")
            return
        await websocket.accept()
        session_id = None
        try:
            handshake_msg = await websocket.receive_text()
            import json as _json
            handshake_data = _json.loads(handshake_msg)
            if handshake_data.get("type") == "handshake":
                response = await _app_state.integration_endpoint.handle_handshake(handshake_data)
                session_id = response.get("session_id", "")
                await websocket.send(_json.dumps(response))
                await _app_state.integration_endpoint.register_connection(session_id, websocket)
            while True:
                data = await websocket.receive_text()
                result = await _app_state.integration_endpoint.handle_message(session_id or "", data)
                if result:
                    await websocket.send(_json.dumps(result))
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.warning("Integration WebSocket error: %s", e)
        finally:
            if session_id:
                await _app_state.integration_endpoint.unregister_connection(session_id)

    return app
