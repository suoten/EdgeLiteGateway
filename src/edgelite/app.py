"""FastAPI应用工厂"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect

from edgelite.config import get_config

logger = logging.getLogger(__name__)


@dataclass
class AppState:
    """应用全局状态"""

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
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    config: Any = None
    ota_manager: Any = None
    audit_service: Any = None
    max_ws_connections: int = 100
    plugin_manager: Any = None
    preprocessor: Any = None
    serial_bridge: Any = None


# 全局应用状态
_app_state = AppState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    config = get_config()

    if not config.security.secret_key:
        import secrets

        config.security.secret_key = secrets.token_urlsafe(48)
        logger.warning(
            "⚠️  JWT密钥未配置，已自动生成随机密钥。"
            "生产环境请通过 EDGELITE_SECURITY__SECRET_KEY 环境变量设置固定密钥！"
        )
        try:
            from pathlib import Path

            env_file = Path(".env")
            lines = []
            if env_file.exists():
                lines = env_file.read_text(encoding="utf-8").splitlines()
            lines = [
                line for line in lines
                if not line.startswith("EDGELITE_SECURITY__SECRET_KEY=")
            ]
            lines.append(f"EDGELITE_SECURITY__SECRET_KEY={config.security.secret_key}")
            env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
            try:
                import os
                os.chmod(str(env_file), 0o600)
            except OSError:
                pass
            logger.info("已将自动生成的JWT密钥保存到 .env 文件")
        except Exception as e:
            logger.warning("无法保存JWT密钥到.env文件: %s", e)
    elif len(config.security.secret_key) < 32:
        logger.warning(
            "⚠️  安全警告: JWT密钥过短(%d字符)，"
            "生产环境请通过 EDGELITE_SECURITY__SECRET_KEY 环境变量设置至少32字符的随机密钥！",
            len(config.security.secret_key),
        )

    if not config.influxdb.token:
        logger.warning(
            "⚠️  InfluxDB Token未配置，时序数据存储功能不可用。"
            "请通过 EDGELITE_INFLUXDB__TOKEN 环境变量设置！"
        )

    # 配置日志
    logging.basicConfig(level=config.logging.level, format=config.logging.format)

    logger.info("EdgeLiteGateway 启动中...")

    initialized: list[tuple[str, Any]] = []

    try:
        # 1. 初始化数据库
        from edgelite.storage.database import Database

        database = Database()
        await database.connect()
        await database.init_tables()
        _app_state.database = database
        initialized.append(("database", database))
        logger.info("数据库初始化完成")

        # 2. 初始化InfluxDB
        from edgelite.storage.influx_storage import InfluxDBStorage

        influx = InfluxDBStorage()
        await influx.connect()
        _app_state.influx_storage = influx
        initialized.append(("influx", influx))

        # 3. 初始化缓存管理器
        from edgelite.storage.cache import CacheManager

        _app_state.cache_manager = CacheManager(database)

        # 4. 初始化事件总线
        from edgelite.engine.event_bus import EventBus

        event_bus = EventBus()
        _app_state.event_bus = event_bus
        initialized.append(("event_bus", event_bus))

        # 5. 初始化仓储
        from edgelite.storage.sqlite_repo import AlarmRepo, DeviceRepo, RuleRepo, UserRepo

        _app_state.config = config
        write_lock = database.write_lock
        device_repo = DeviceRepo(database, write_lock)
        rule_repo = RuleRepo(database, write_lock)
        alarm_repo = AlarmRepo(database, write_lock)
        user_repo = UserRepo(database, write_lock)

        # 6. 初始化采集调度器
        from edgelite.engine.scheduler import CollectScheduler

        scheduler = CollectScheduler(event_bus, influx, _app_state.cache_manager)
        _app_state.scheduler = scheduler
        initialized.append(("scheduler", scheduler))

        # 6.5 初始化数据预处理
        from edgelite.engine.preprocessor import DataPreprocessor

        preprocessor = DataPreprocessor()
        if config.preprocess.enabled:
            scheduler.set_preprocessor(preprocessor)
        _app_state.preprocessor = preprocessor

        # 7. 初始化设备生命周期管理
        from edgelite.engine.lifecycle import DeviceLifecycleManager

        lifecycle = DeviceLifecycleManager(event_bus)
        _app_state.lifecycle = lifecycle

        # 8. 初始化业务服务
        from edgelite.services.alarm_service import AlarmService
        from edgelite.services.data_service import DataService
        from edgelite.services.device_service import DeviceService
        from edgelite.services.notify_service import NotifyService
        from edgelite.services.rule_service import RuleService
        from edgelite.services.system_service import SystemService
        from edgelite.services.video_service import VideoService

        device_service = DeviceService(device_repo, rule_repo, scheduler, lifecycle)
        rule_service = RuleService(rule_repo, device_repo)
        alarm_service = AlarmService(alarm_repo)
        data_service = DataService(influx, device_repo)
        video_service = VideoService(event_bus)
        notify_service = NotifyService()
        system_service = SystemService(
            database,
            device_repo,
            rule_repo,
            alarm_repo,
            user_repo,
            scheduler,
            _app_state.start_time,
        )

        _app_state.device_service = device_service
        _app_state.rule_service = rule_service
        _app_state.alarm_service = alarm_service
        _app_state.data_service = data_service
        _app_state.video_service = video_service
        _app_state.notify_service = notify_service
        _app_state.system_service = system_service

        # 8.5 初始化审计日志服务
        from edgelite.services.audit_service import AuditService

        audit_service = AuditService(db_path=database.audit_db_path)
        await audit_service.initialize()
        _app_state.audit_service = audit_service
        initialized.append(("audit_service", audit_service))

        # 9. 初始化规则评估器
        from edgelite.engine.evaluator import RuleEvaluator

        evaluator = RuleEvaluator(event_bus, rule_repo, alarm_repo)
        _app_state.evaluator = evaluator
        await evaluator.start()
        initialized.append(("evaluator", evaluator))

        # 10. 初始化WebSocket
        from edgelite.ws.channels import WebSocketChannels
        from edgelite.ws.manager import ConnectionManager

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

        # 12.5 初始化自定义驱动加载
        from edgelite.engine.plugin_manager import PluginManager

        _app_state.plugin_manager = PluginManager(_app_state.driver_registry)
        custom_dir = (
            config.drivers.custom_dir
            if hasattr(config, "drivers") and config.drivers.custom_dir
            else ""
        )
        if custom_dir:
            _app_state.plugin_manager.discover_custom_drivers(custom_dir)

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
            await mqtt_server.start(
                {
                    "host": getattr(mqtt_server_config, "host", "0.0.0.0"),
                    "port": getattr(mqtt_server_config, "port", 1888),
                    "ws_port": getattr(mqtt_server_config, "ws_port", None),
                    "username": getattr(mqtt_server_config, "username", ""),
                    "password": getattr(mqtt_server_config, "password", ""),
                }
            )
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
                    elif platform_name == "thingspanel":
                        from edgelite.platform.thingspanel import ThingsPanelHandler

                        handler = ThingsPanelHandler()
                        await handler.connect(platform_conf)
                        _app_state.platform_handlers["thingspanel"] = handler
                        logger.info("ThingsPanel平台对接已启动")
                except Exception as e:
                    logger.error("平台对接启动失败 %s: %s", platform_name, e)

        # 16. 初始化内置Modbus Slave
        modbus_slave_config = getattr(config, "modbus_slave", None)
        if modbus_slave_config and getattr(modbus_slave_config, "enabled", False):
            from edgelite.engine.modbus_slave import ModbusSlaveServer

            modbus_slave = ModbusSlaveServer()
            _app_state.modbus_slave = modbus_slave
            await modbus_slave.start(
                {
                    "host": getattr(modbus_slave_config, "host", "0.0.0.0"),
                    "port": getattr(modbus_slave_config, "port", 502),
                    "holding_size": getattr(modbus_slave_config, "holding_size", 1000),
                    "input_size": getattr(modbus_slave_config, "input_size", 1000),
                }
            )
            initialized.append(("modbus_slave", modbus_slave))

        # 17. 加载已有设备并恢复采集
        await device_service.load_existing_devices()

        # 18. 自动创建模拟设备
        if config.simulator.auto_create:
            for dev_config in config.simulator.default_devices:
                existing = await device_repo.get(dev_config.device_id)
                if existing is None:
                    points_data = [p.model_dump() for p in dev_config.points]
                    await device_service.create_simulator(
                        {
                            "device_id": dev_config.device_id,
                            "name": dev_config.name,
                            "points": points_data,
                            "collect_interval": dev_config.collect_interval,
                        }
                    )
                    logger.info("自动创建模拟设备: %s", dev_config.device_id)

        # 注册告警事件处理器（通知）
        async def handle_alarm_for_notify(event):
            if hasattr(event, "rule_id") and event.action == "firing":
                rule = await rule_repo.get(event.rule_id)
                if rule and rule.get("notify_channels"):
                    alarm = (
                        await alarm_repo.get(event.alarm_id) if hasattr(event, "alarm_id") else None
                    )
                    if alarm:
                        await notify_service.send_notification(rule["notify_channels"], alarm)

        event_bus.register_handler("AlarmEvent", handle_alarm_for_notify)

        # 16. 初始化联调集成端点
        try:
            from edgelite.engine.integration.backhaul import BackhaulManager
            from edgelite.engine.integration.dispatcher import MessageDispatcher
            from edgelite.engine.integration.endpoint import IntegrationEndpoint

            integration_dispatcher = MessageDispatcher()
            integration_dispatcher.register_service("device_service", device_service)
            integration_dispatcher.register_service("scheduler", scheduler)
            integration_endpoint = IntegrationEndpoint(dispatcher=integration_dispatcher)
            _app_state.integration_dispatcher = integration_dispatcher
            _app_state.integration_endpoint = integration_endpoint

            backhaul_manager = BackhaulManager(
                event_bus=event_bus, endpoint=integration_endpoint, buffer_size=1000
            )
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
                if hasattr(resource, "close"):
                    await resource.close()
                elif hasattr(resource, "stop"):
                    await resource.stop()
                elif hasattr(resource, "stop_all"):
                    await resource.stop_all()
            except Exception as cleanup_err:
                logger.warning("清理%s失败: %s", name, cleanup_err)
        raise

    # 初始化OTA管理器
    try:
        from edgelite.engine.ota_manager import OTAManager

        ota_manager = OTAManager()
        _app_state.ota_manager = ota_manager
        logger.info("OTA升级管理器已初始化")
    except ImportError:
        logger.warning("OTA升级管理器模块不可用")
    except Exception as e:
        logger.warning("OTA升级管理器初始化失败: %s", e)

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
    for svc_name, svc in [
        ("mqtt_forwarder", _app_state.mqtt_forwarder),
        ("mqtt_server", _app_state.mqtt_server),
        ("modbus_slave", _app_state.modbus_slave),
    ]:
        if svc:
            try:
                await svc.stop()
            except Exception as e:
                logger.warning("关闭%s异常: %s", svc_name, e)
    if _app_state.plugin_manager:
        try:
            await _app_state.plugin_manager.stop()
        except Exception as e:
            logger.warning("关闭plugin_manager异常: %s", e)
    try:
        await video_service.close()
    except Exception as e:
        logger.warning("关闭video_service异常: %s", e)
    try:
        await notify_service.close()
    except Exception as e:
        logger.warning("关闭notify_service异常: %s", e)
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
    config = get_config()

    app = FastAPI(
        title="EdgeLiteGateway",
        description="轻量级边缘计算物联网网关 API",
        version=__import__("edgelite").__version__,
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

    from fastapi import Request
    from fastapi.responses import JSONResponse

    from edgelite.models.common import ApiResponse

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        logger.exception("未处理的异常: %s %s -> %s", request.method, request.url.path, exc)
        return JSONResponse(
            status_code=500,
            content=ApiResponse(
                code=1, message="服务器内部错误，请稍后重试", data=None
            ).model_dump(),
        )

    # 注册路由
    from edgelite.api import alarms, auth, data, devices, rules, system, users, video

    app.include_router(auth.router)
    app.include_router(devices.router)
    app.include_router(rules.router)
    app.include_router(alarms.router)
    app.include_router(data.router)
    app.include_router(video.router)
    app.include_router(system.router)
    app.include_router(users.router)

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

    # 数据预处理路由
    try:
        from edgelite.api.preprocess import router as preprocess_router

        app.include_router(preprocess_router)
    except Exception as e:
        logger.warning("数据预处理路由注册失败: %s", e)

    # 审计日志路由
    try:
        from edgelite.api.audit import router as audit_router

        app.include_router(audit_router)
    except Exception as e:
        logger.warning("审计日志路由注册失败: %s", e)

    # 串口透传路由
    try:
        from edgelite.api.serial_bridge import router as serial_bridge_router

        app.include_router(serial_bridge_router)
    except Exception as e:
        logger.warning("串口透传路由注册失败: %s", e)

    # 联调集成路由
    try:
        from edgelite.api.integration import router as integration_router

        app.include_router(integration_router)
    except Exception as e:
        logger.warning("联调集成路由注册失败: %s", e)

    # MQTT Server管理路由
    try:
        from edgelite.api.mqtt_server import router as mqtt_server_router

        app.include_router(mqtt_server_router)
    except Exception as e:
        logger.warning("MQTT Server路由注册失败: %s", e)

    # Modbus Slave管理路由
    try:
        from edgelite.api.modbus_slave import router as modbus_slave_router

        app.include_router(modbus_slave_router)
    except Exception as e:
        logger.warning("Modbus Slave路由注册失败: %s", e)

    # MCP协议路由
    try:
        from edgelite.api.mcp import router as mcp_router

        app.include_router(mcp_router)
    except Exception as e:
        logger.warning("MCP协议路由注册失败: %s", e)

    # OTA升级路由
    try:
        from edgelite.api.ota import router as ota_router

        app.include_router(ota_router)
    except Exception as e:
        logger.warning("OTA升级路由注册失败: %s", e)

    # 服务管理路由
    try:
        from edgelite.api.services import router as services_router

        app.include_router(services_router)
    except Exception as e:
        logger.warning("服务管理路由注册失败: %s", e)

    # Grafana集成路由
    try:
        from edgelite.api.grafana import router as grafana_router

        app.include_router(grafana_router)
    except Exception as e:
        logger.warning("Grafana集成路由注册失败: %s", e)

    # 组态管理路由
    try:
        from edgelite.api.scada import router as scada_router

        app.include_router(scada_router)
    except Exception as e:
        logger.warning("组态管理路由注册失败: %s", e)

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
        except Exception as e:
            logger.debug("WebSocket realtime 连接异常: %s", e)
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
        except Exception as e:
            logger.debug("WebSocket alarm 连接异常: %s", e)
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
        except Exception as e:
            logger.debug("WebSocket device 连接异常: %s", e)
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
                result = await _app_state.integration_endpoint.handle_message(
                    session_id or "", data
                )
                if result:
                    await websocket.send(_json.dumps(result))
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.warning("Integration WebSocket error: %s", e)
        finally:
            if session_id:
                await _app_state.integration_endpoint.unregister_connection(session_id)

    # Health check endpoint for Docker HEALTHCHECK
    @app.get("/health", tags=["系统"], summary="健康检查", include_in_schema=False)
    async def health_check():
        return {"status": "ok"}

    return app
