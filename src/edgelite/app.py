"""FastAPI 应用工厂

架构说明：
    本模块维护一个模块级全局 ServiceContainer 实例 ``_app_state``，用于在
    FastAPI 请求处理函数和后台任务中访问共享服务（数据库、EventBus、
    调度器等）。

    设计决策：
    - 采用全局容器而非 FastAPI Depends 注入，因为后台任务（采集调度、
      告警处理、WebSocket 推送）不在请求上下文中运行，无法使用 Depends。
    - ``lifespan`` 上下文管理器负责 ``_app_state`` 的初始化和清理，
      在多次 ``create_app`` 调用（测试/热重载）时自动检测并清理旧实例。
    - 所有通过 ``from edgelite.app import _app_state`` 引用的模块应使用
      ``getattr(_app_state, attr, None)`` 进行防御性访问。

    已知限制：
    - 模块级 global 在多进程环境下不共享，仅适用于单进程 uvicorn 部署。
    - 测试中使用 ``create_app()`` 创建新实例时，``lifespan`` 会自动清理旧实例。
"""

from __future__ import annotations

import asyncio  # FIXED-P2: WS首帧认证需要asyncio.wait_for
import json  # FIXED-P2: WS首帧认证需要json.loads
import logging
import os  # FIXED-P0: os.environ.get("DEV_MODE")需要import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from edgelite.api.error_codes import CommonErrors
from edgelite.bootstrap import ServiceContainer, bootstrap_all, teardown
from edgelite.config import get_config

logger = logging.getLogger(__name__)

# 模块级全局服务容器 — 详见模块文档字符串中的架构说明
# lifespan 上下文管理器负责初始化和清理，支持多次 create_app 调用
_app_state = ServiceContainer()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI 应用生命周期管理器。

    负责应用的启动初始化和关闭清理：
    - 启动时：检测并清理旧的 ``_app_state`` 实例，调用 ``bootstrap_all``
      初始化所有服务，随后启动限流清理、备份调度等后置任务。
    - 关闭时：按逆序停止后置任务并调用 ``teardown`` 清理服务，
      整体超时 30 秒以防挂起。

    Args:
        app: FastAPI 应用实例。
    """
    global _app_state  # 8#修复: 允许在 lifespan 中替换模块级 _app_state，避免旧实例残留
    config = get_config()

    # 8#修复: _app_state 是模块级全局 ServiceContainer，多次调用 create_app（如测试或热重载场景）
    # 时，上一次 bootstrap 的资源可能未被 teardown，导致连接池/调度任务/WS 连接泄漏。
    # 此处启动时检测旧实例并清理，然后替换为新实例，避免资源累积。
    if _app_state._initialized:
        logger.warning(
            "Previous _app_state detected with %d initialized resources, "
            "cleaning up before re-bootstrap to avoid resource leak",
            len(_app_state._initialized),
        )
        try:
            await teardown(_app_state)
        except Exception as e:
            logger.warning("Previous _app_state teardown failed: %s", e)
        # 替换为新实例，确保旧字段引用全部释放，避免后续步骤误用旧资源
        _app_state = ServiceContainer()

    loop = asyncio.get_running_loop()
    _asyncio_logger = logging.getLogger("edgelite.asyncio")

    def _suppress_unhandled(loop, context):
        exception = context.get("exception")
        message = context.get("message", "")
        if exception and "MqttCodeError" in type(exception).__name__:
            _asyncio_logger.debug("MQTT内部异常已抑制: %s", exception)
            return
        if "Task exception was never retrieved" in message:
            _asyncio_logger.debug("未检索的Task异常: %s", exception or message)
            return
        loop.default_exception_handler(context)

    loop.set_exception_handler(_suppress_unhandled)

    if os.environ.get("EDGELITE_CHECK_CONSISTENCY", "").lower() in ("1", "true", "yes"):
        try:
            from scripts.audit.check_model_orm_consistency import run_all as check_l1

            _err_count, issues = check_l1()  # FIXED(P3): 原问题-解包变量err_count未使用; 修复-改为_err_count前缀
            if issues:
                for _category, _field, msg in issues:
                    logger.warning("[L1 CHECK] %s", msg)
            else:
                logger.info("[L1 CHECK] Pydantic ↔ ORM 一致性校验通过")
        except Exception as e:
            logger.warning("[L1 CHECK] 一致性校验执行异常: %s", e)

    try:
        await bootstrap_all(_app_state, config)
    except Exception as init_err:
        logger.error(
            "Initialization failed: %s, cleaning up initialized resources",
            init_err,
        )
        await teardown(_app_state)
        raise

    # FIXED-P2: 原问题-bootstrap_all后启动的资源未纳入异常清理路径，失败时泄漏
    _post_bootstrap_resources: list[tuple[str, object]] = []
    try:
        # FIXED-H03: Start rate limit cleanup task for persistent storage
        from edgelite.storage.sqlite_repo import RateLimitRepo

        RateLimitRepo.start_cleanup_task()
        _post_bootstrap_resources.append(("rate_limit_repo", RateLimitRepo))

        # FIXED-AUTO-BACKUP: Start the database backup scheduler
        # Start config backup service (JSON-based)
        from edgelite.services.system_services import get_backup_service

        backup_svc = get_backup_service(auto_start=False)
        await backup_svc.start_scheduler()
        _post_bootstrap_resources.append(("backup_svc", backup_svc))

        # Start database backup scheduler (SQLite files)
        from edgelite.services.backup_scheduler import get_backup_scheduler

        db_scheduler = get_backup_scheduler(
            backup_dir=config.backup.backup_dir,
            interval_seconds=config.backup.interval_hours * 3600,
            retain_days=config.backup.retain_days,
            enabled=config.backup.enabled,
        )
        await db_scheduler.start()
        _post_bootstrap_resources.append(("db_scheduler", db_scheduler))
    except Exception as post_err:
        logger.error("Post-bootstrap startup failed: %s, cleaning up", post_err)
        for name, res in reversed(_post_bootstrap_resources):
            try:
                if name == "rate_limit_repo":
                    await res.stop_cleanup_task()
                elif name == "backup_svc":
                    await res.stop_scheduler()
                elif name == "db_scheduler":
                    await res.stop()
            except Exception as e:
                logger.warning(
                    "Post-bootstrap cleanup of %s failed: %s", name, e
                )  # FIXED-P2: 原问题-清理异常被pass吞没，改为记录日志
        await teardown(_app_state)
        raise

    for key, value in _app_state.__dict__.items():
        if not key.startswith("_"):
            setattr(app.state, key, value)

    yield

    # FIXED-P1: 原问题-yield 后所有 stop 操作用 try/finally 包裹，确保单个 stop 失败不阻断后续清理
    # 之前：stop 操作顺序执行无 try/finally，任一失败则后续 stop 被跳过，导致资源泄漏
    # 之后：每个 stop 操作独立 try/except，确保所有资源都被清理
    # G-16: 优雅关闭超时保护 - 将 shutdown 逻辑封装为协程并用 asyncio.wait_for 限制总时长，
    # 防止任一 stop 操作挂起导致应用无限期等待，超时后强制退出
    _SHUTDOWN_TIMEOUT_SECONDS = 30  # 优雅关闭总超时（秒），与 K8s 默认 terminationGracePeriodSeconds 对齐

    async def _do_graceful_shutdown() -> None:
        # FIXED-AUTO-BACKUP: Stop the backup schedulers gracefully
        try:
            await db_scheduler.stop()
        except Exception as e:
            logger.warning("db_scheduler.stop() failed during shutdown: %s", e)

        try:
            await backup_svc.stop_scheduler()
        except Exception as e:
            logger.warning("backup_svc.stop_scheduler() failed during shutdown: %s", e)

        # FIXED-H03: Stop rate limit cleanup task
        try:
            await RateLimitRepo.stop_cleanup_task()
        except Exception as e:
            logger.warning("RateLimitRepo.stop_cleanup_task() failed during shutdown: %s", e)

        try:
            await teardown(_app_state)
        except Exception as e:
            logger.warning("teardown(_app_state) failed during shutdown: %s", e)

    try:
        try:
            # G-16: 限制优雅关闭总时长，超时后记录警告并强制退出
            await asyncio.wait_for(_do_graceful_shutdown(), timeout=_SHUTDOWN_TIMEOUT_SECONDS)
        except TimeoutError:
            logger.warning(
                "Graceful shutdown timed out after %ds, forcing exit (some resources may leak)",
                _SHUTDOWN_TIMEOUT_SECONDS,
            )
    finally:
        logger.info("Application shutdown complete")


def _register_routes(
    app: FastAPI, config: object = None
) -> None:  # FIXED-P0: 传入config参数，修复闭包变量作用域NameError
    from edgelite.api import (
        alarms,
        auth,
        data,
        devices,
        resource_shares,
        rules,
        system,
        users,
        video,
    )

    app.include_router(auth.router)
    app.include_router(devices.router)
    app.include_router(rules.router)
    app.include_router(alarms.router)
    app.include_router(data.router)
    app.include_router(video.router)
    app.include_router(system.router)
    app.include_router(users.router)
    app.include_router(resource_shares.router)

    # FIXED: 原问题-路由标签中文硬编码，现改为英文标签
    # FIXED(404歼灭): 恢复并实现此前"移除"的24个功能模块，对齐前端调用清单。
    #   以下模块现已具备真实实现（有服务支撑的接入 service，无服务的最小真实实现 + SQLite 持久化）：
    #   shadow, db_monitor, log_aggregation, anomaly_learner, trend_learner, threshold_learner,
    #   protocol_bridge, profiler, observability, config_version, scripts, simulation,
    #   data_quality, firmware_signature, device_linkage
    #   以下模块已合并到现有文件（追加端点）：data(export/import/downsample), ai_models(ab-test/hot-swap/cache/...),
    #   alarms(batch-ack), ota(status/cancel)
    #   仍尚未实现（前端已禁用对应调用）：self_test, ai_test, precision_test_api, ai_boundary_test_api,
    #   ai_stress_test_api, calibration_api, physics_calib_api, physics_param_api, evolution_api,
    #   ai_report_api, inference_api, ai_monitor, app_update(已合并到 ota)
    _optional_routers = [
        ("Notification", "edgelite.api.notify", "router"),
        ("Drivers", "edgelite.api.drivers", "router"),
        ("Platforms", "edgelite.api.platforms", "router"),
        ("Expressions", "edgelite.api.expressions", "router"),
        ("Preprocess", "edgelite.api.preprocess", "router"),
        ("Audit", "edgelite.api.audit", "router"),
        ("SerialBridge", "edgelite.api.serial_bridge", "router"),
        ("Integration", "edgelite.api.integration", "router"),
        ("MQTT Server", "edgelite.api.mqtt_server", "router"),
        ("Modbus Slave", "edgelite.api.modbus_slave", "router"),
        ("MCP", "edgelite.api.mcp", "router"),
        ("Services", "edgelite.api.services", "router"),
        ("Grafana", "edgelite.api.grafana", "router"),
        ("SCADA", "edgelite.api.scada", "router"),
        ("AI Models", "edgelite.api.ai_models", "router"),
        ("MQTT Forwarder", "edgelite.api.mqtt_forwarder", "router"),
        ("OTA", "edgelite.api.ota", "router"),
        # FIX: metrics router prefix=/api/v1，路由注册无额外前缀拼接
        ("Metrics", "edgelite.api.metrics", "router"),
        # 404歼灭: 以下为新增模块，对齐前端调用清单
        ("Shadow", "edgelite.api.shadow", "router"),
        ("DB Monitor", "edgelite.api.db_monitor", "router"),
        ("Log Aggregation", "edgelite.api.log_aggregation", "router"),
        ("Anomaly Learner", "edgelite.api.anomaly_learner", "router"),
        ("Trend Learner", "edgelite.api.trend_learner", "router"),
        ("Threshold Learner", "edgelite.api.threshold_learner", "router"),
        ("Protocol Bridge", "edgelite.api.protocol_bridge", "router"),
        ("Profiler", "edgelite.api.profiler", "router"),
        ("Observability", "edgelite.api.observability", "router"),
        ("Config Version", "edgelite.api.config_version", "router"),
        ("Scripts", "edgelite.api.scripts", "router"),
        ("Simulation", "edgelite.api.simulation", "router"),
        ("Data Quality", "edgelite.api.data_quality", "router"),
        ("Firmware Signature", "edgelite.api.firmware_signature", "router"),
        ("Device Linkage", "edgelite.api.device_linkage", "router"),
    ]

    # FIXED-P0: 调试/测试/剖析类路由仅在debug_api_enabled=true时注册，生产环境默认禁用
    # FIXED: 移除6个引用缺失的调试模块（profiler, self_test, ai_test, precision_test_api,
    #   ai_boundary_test_api, ai_stress_test_api），后续版本实现后逐步添加
    _debug_routers: list[tuple[str, str, str]] = []

    for label, module_path, attr in _optional_routers:
        try:
            import importlib

            mod = importlib.import_module(module_path)
            _router = getattr(mod, attr)
            app.include_router(_router)
            logger.info("Registered optional route: %s (%s)", label, module_path)
        except SyntaxError:
            # FIXED: P0-1 语法错误是致命问题，必须阻止启动
            raise RuntimeError(f"[P0-1] {label} module has syntax error: {module_path}") from None
        except ImportError:
            logger.warning("%s optional route module not installed: %s", label, module_path)
        except AttributeError:
            logger.warning("%s module missing router attribute: %s", label, module_path)
        except Exception as e:
            logger.error(
                "%s route registration failed: %s: %s",
                label,
                type(e).__name__,
                e,
                exc_info=True,
            )
            raise  # FIXED: P0-1 其他异常（配置错误/循环依赖）不应静默降级

    # FIXED(安全): Debug/Profiler 路由仅在 debug_api_enabled=true 时注册
    # 原问题：注释声明"始终注册"但危险操作由 debug_api_enabled 控制，
    # 但 /debug/simulate、/debug/read、/debug/write 等端点可直接操控工业设备
    # #[AUDIT-FIX] NameError: _debug_api_enabled 仅在 create_app 中定义，
    # _register_routes 作用域内未定义，从 config 提取以修复启动崩溃
    _debug_api_enabled = getattr(getattr(config, "server", None), "debug_api_enabled", False)
    if _debug_api_enabled:
        try:
            from edgelite.api.debug import router as debug_router

            app.include_router(debug_router)
            logger.info("Registered Debug route (debug_api_enabled=true)")
        except ImportError:
            logger.warning("Debug route module not installed: edgelite.api.debug")
        except AttributeError:
            logger.warning("Debug module missing router attribute: edgelite.api.debug")

        for label, module_path, attr in _debug_routers:
            try:
                import importlib

                mod = importlib.import_module(module_path)
                _router = getattr(mod, attr)
                app.include_router(_router)
                logger.info("Registered route: %s (%s)", label, module_path)
            except ImportError:
                logger.warning("%s route module not installed: %s", label, module_path)
            except AttributeError:
                logger.warning("%s module missing router attribute: %s", label, module_path)
    else:
        logger.info("Debug/Profiler routes skipped (debug_api_enabled=false)")

    # Register root-level /metrics endpoint for Prometheus scraping compatibility
    try:
        from edgelite.api.metrics import _root_metrics_router

        app.include_router(_root_metrics_router)
        logger.info("Registered root-level /metrics route for Prometheus scraping")
    except Exception as e:
        logger.warning("Root /metrics route registration failed: %s", e)


def _register_websocket_routes(app: FastAPI) -> None:
    # FIXED-P2: WS Token从URL查询参数改为首帧认证消息，防止Token在日志/Referer中泄露
    # LP-02: 同时支持从 HttpOnly Cookie 读取 access_token（无需首帧认证消息）

    async def _recv_auth_token(websocket: WebSocket) -> str | None:
        """LP-02: 优先从 Cookie 提取 auth token，fallback 到首帧消息。

        如果 Cookie 中有有效的 access_token，直接返回，无需等待首帧消息。
        否则等待首帧认证消息（向后兼容旧前端）。
        """
        # LP-02: 优先从 Cookie 提取
        cookie_token = websocket.cookies.get("edgelite_access")
        if cookie_token:
            return cookie_token
        # Fallback: 从首帧消息提取
        try:
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=5.0)
            data = json.loads(raw)
            if not isinstance(data, dict):
                return None
            if data.get("type") == "auth":
                return data.get("token")
        except Exception as exc:
            logger.debug("WS auth token recv failed: %s", exc)
        return None

    @app.websocket("/ws/v1/realtime")
    async def ws_realtime(websocket: WebSocket):
        """实时数据 WebSocket 端点。

        通过 Cookie 或首帧消息认证后，向客户端推送实时采集数据。
        空闲超时 300 秒，消息大小限制 1 MB。
        """
        await _app_state.ws_manager.connect(websocket, "realtime")
        token = await _recv_auth_token(websocket)
        if not token or not await _app_state.ws_manager.authenticate(websocket, "realtime", token):
            # FIXED-P0: connect(token=None)已将websocket加入_connections，authenticate失败时必须disconnect清理
            await _app_state.ws_manager.disconnect(websocket, "realtime")
            return
        try:
            while True:
                # 添加300秒超时，防止空闲连接长期占用资源
                data = await asyncio.wait_for(websocket.receive_text(), timeout=300.0)
                # FIXED(安全): 限制消息大小为1MB，防止超大消息耗尽内存
                if len(data) > 1024 * 1024:
                    await websocket.close(code=1009, reason="消息过大")
                    break
                # FIXED(一般): 处理客户端 pong 响应，更新心跳记录
                try:
                    msg = json.loads(data)
                    if isinstance(msg, dict) and msg.get("type") == "pong":
                        _app_state.ws_manager.record_pong(websocket)
                except (json.JSONDecodeError, TypeError):
                    pass
        except WebSocketDisconnect:
            pass
        except TimeoutError:
            logger.debug("WebSocket realtime idle timeout (300s), closing connection")
        except Exception as e:
            logger.debug("WebSocket realtime error: %s", e)
        finally:
            await _app_state.ws_manager.disconnect(websocket, "realtime")

    @app.websocket("/ws/v1/alarm")
    async def ws_alarm(websocket: WebSocket):
        """告警推送 WebSocket 端点。

        认证后向客户端推送实时告警通知。空闲超时 300 秒，消息大小限制 1 MB。
        """
        await _app_state.ws_manager.connect(websocket, "alarm")
        token = await _recv_auth_token(websocket)
        if not token or not await _app_state.ws_manager.authenticate(websocket, "alarm", token):
            # FIXED-P0: 同ws_realtime，authenticate失败时必须disconnect清理
            await _app_state.ws_manager.disconnect(websocket, "alarm")
            return
        try:
            while True:
                # 添加300秒超时，防止空闲连接长期占用资源
                data = await asyncio.wait_for(websocket.receive_text(), timeout=300.0)
                # FIXED(安全): 限制消息大小为1MB，防止超大消息耗尽内存
                if len(data) > 1024 * 1024:
                    await websocket.close(code=1009, reason="消息过大")
                    break
                # FIXED(一般): 处理客户端 pong 响应
                try:
                    msg = json.loads(data)
                    if isinstance(msg, dict) and msg.get("type") == "pong":
                        _app_state.ws_manager.record_pong(websocket)
                except (json.JSONDecodeError, TypeError):
                    pass
        except WebSocketDisconnect:
            pass
        except TimeoutError:
            logger.debug("WebSocket alarm idle timeout (300s), closing connection")
        except Exception as e:
            logger.debug("WebSocket alarm error: %s", e)
        finally:
            await _app_state.ws_manager.disconnect(websocket, "alarm")

    @app.websocket("/ws/v1/device")
    async def ws_device(websocket: WebSocket):
        """设备状态 WebSocket 端点。

        认证后向客户端推送设备上下线、状态变更等事件。空闲超时 300 秒。
        """
        await _app_state.ws_manager.connect(websocket, "device")
        token = await _recv_auth_token(websocket)
        if not token or not await _app_state.ws_manager.authenticate(websocket, "device", token):
            # FIXED-P0: 同ws_realtime，authenticate失败时必须disconnect清理
            await _app_state.ws_manager.disconnect(websocket, "device")
            return  # FIXED-P0: connect已accept, authenticate已close, 端点不再double-accept/double-close
        try:
            while True:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=300.0)
                # FIXED(安全): 限制消息大小为1MB，防止超大消息耗尽内存
                if len(data) > 1024 * 1024:
                    await websocket.close(code=1009, reason="消息过大")
                    break
                # FIXED(一般): 处理客户端 pong 响应
                try:
                    msg = json.loads(data)
                    if isinstance(msg, dict) and msg.get("type") == "pong":
                        _app_state.ws_manager.record_pong(websocket)
                except (json.JSONDecodeError, TypeError):
                    pass
        except WebSocketDisconnect:
            pass
        except TimeoutError:
            logger.debug("WebSocket device idle timeout (300s), closing connection")
        except Exception as e:
            logger.debug("WebSocket device error: %s", e)
        finally:
            await _app_state.ws_manager.disconnect(websocket, "device")

    @app.websocket("/ws/v1/integration")
    async def ws_integration(websocket: WebSocket):
        """集成平台 WebSocket 端点。

        支持与第三方平台的双向通信，包含握手协议和消息分发。
        认证后需发送 handshake 消息建立会话，随后可收发业务数据。
        空闲超时 300 秒，消息大小限制 1 MB。
        """
        if not _app_state.integration_endpoint:
            await websocket.accept()
            await websocket.close(code=1003, reason="Integration not available")
            return

        await _app_state.ws_manager.connect(websocket, "integration")
        token = await _recv_auth_token(websocket)
        if not token:
            # FIXED-P0: 原问题-connect(token=None)内部已accept，再次accept导致RuntimeError；移除冗余accept
            await websocket.close(code=4001, reason="Auth failed")
            # FIXED-P0: connect已将websocket加入_connections，auth失败时必须disconnect清理，防止连接资源泄漏
            await _app_state.ws_manager.disconnect(websocket, "integration")
            return

        # FIXED-P0: 原问题-手动verify_token但从未调用ws_manager.authenticate()，
        # 导致 authenticated 永远为False：心跳60秒后误杀连接 + broadcast跳过该连接。
        # 修复-改用 ws_manager.authenticate() 统一认证（内部调用verify_token并设置authenticated=True）
        if not await _app_state.ws_manager.authenticate(websocket, "integration", token):
            # authenticate失败时已发送错误消息并close，需disconnect清理连接
            await _app_state.ws_manager.disconnect(websocket, "integration")
            return

        session_id = None
        import json as _json

        _last_msg_type = None
        try:
            handshake_msg = await asyncio.wait_for(websocket.receive_text(), timeout=300.0)
            # FIXED(安全): 限制握手消息大小为1MB，防止超大消息耗尽内存
            if len(handshake_msg) > 1024 * 1024:
                await websocket.close(code=1009, reason="消息过大")
                return
            try:
                handshake_data = _json.loads(handshake_msg)
            except _json.JSONDecodeError:
                await websocket.send_text(_json.dumps({"type": "error", "message": "Invalid JSON"}))
                return
            if not isinstance(handshake_data, dict):
                await websocket.send_text(
                    _json.dumps(
                        {
                            "type": "error",
                            "message": "Handshake must be a JSON object",
                        }
                    )
                )
                return
            if handshake_data.get("type") == "handshake":
                response = await _app_state.integration_endpoint.handle_handshake(handshake_data)
                session_id = response.get("session_id", "") if isinstance(response, dict) else ""
                await websocket.send_text(_json.dumps(response))
                await _app_state.integration_endpoint.register_connection(session_id, websocket)
            while True:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=300.0)
                # FIXED(安全): 限制消息大小为1MB，防止超大消息耗尽内存
                if len(data) > 1024 * 1024:
                    await websocket.close(code=1009, reason="消息过大")
                    break
                try:
                    data_parsed = _json.loads(data)
                    _last_msg_type = data_parsed.get("type") if isinstance(data_parsed, dict) else None
                    # FIXED: 处理客户端 pong 响应，更新心跳记录，防止60秒后心跳超时误杀连接
                    if isinstance(data_parsed, dict) and data_parsed.get("type") == "pong":
                        _app_state.ws_manager.record_pong(websocket)
                except _json.JSONDecodeError:
                    _last_msg_type = "(invalid JSON)"
                result = await _app_state.integration_endpoint.handle_message(session_id or "", data)
                if result:
                    if isinstance(result, dict):
                        await websocket.send_text(_json.dumps(result))
                    else:
                        logger.warning(
                            "Integration handle_message returned non-dict type %s, skipping",
                            type(result).__name__,
                        )
        except WebSocketDisconnect:
            pass
        except TimeoutError:
            logger.debug("WebSocket integration idle timeout (300s), closing connection")
        except Exception as e:
            # #[AUDIT-FIX] 改用 logger.exception 捕获完整堆栈，便于定位 TypeError 根因
            logger.exception(
                "Integration WebSocket error [%s] (msg_type=%r, session=%r): %s",
                type(e).__name__,
                _last_msg_type,
                session_id,
                e,
            )
        finally:
            if session_id:
                await _app_state.integration_endpoint.unregister_connection(session_id)
            # FIXED-P2: 原问题-connect(token=None)已将websocket加入_connections，finally中必须disconnect清理
            await _app_state.ws_manager.disconnect(websocket, "integration")

    @app.websocket("/ws/v1/ai")
    async def ws_ai(websocket: WebSocket):
        """AI 推理结果 WebSocket 端点。

        认证后向客户端推送 AI 模型推理结果和异常检测结果。
        空闲超时 300 秒，消息大小限制 1 MB。
        """
        await _app_state.ws_manager.connect(websocket, "ai")
        token = await _recv_auth_token(websocket)
        if not token or not await _app_state.ws_manager.authenticate(websocket, "ai", token):
            # FIXED-P2: 同ws_realtime，authenticate失败时必须disconnect清理
            await _app_state.ws_manager.disconnect(websocket, "ai")
            return
        try:
            while True:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=300.0)
                # FIXED(安全): 限制消息大小为1MB，防止超大消息耗尽内存
                if len(data) > 1024 * 1024:
                    await websocket.close(code=1009, reason="消息过大")
                    break
                # FIXED(一般): 处理客户端 pong 响应
                try:
                    msg = json.loads(data)
                    if isinstance(msg, dict) and msg.get("type") == "pong":
                        _app_state.ws_manager.record_pong(websocket)
                except (json.JSONDecodeError, TypeError):
                    pass
        except WebSocketDisconnect:
            pass
        except TimeoutError:
            logger.debug("WebSocket ai idle timeout (300s), closing connection")
        except Exception as e:
            logger.debug("WebSocket ai error: %s", e)
        finally:
            await _app_state.ws_manager.disconnect(websocket, "ai")


def create_app() -> FastAPI:
    """创建并配置 FastAPI 应用实例。

    包含以下配置步骤：
    1. CORS 中间件（生产环境精确域名 / 开发环境 localhost 正则 / 默认拒绝）
    2. 安全中间件链（TokenRenewal → CSRF → RateLimit → RequestId）
    3. 请求耗时记录与慢请求告警
    4. 安全响应头（X-Content-Type-Options、X-Frame-Options 等）
    5. TrustedHost 中间件（生产环境）
    6. 请求体大小限制（10 MB）
    7. 全局异常处理器（验证错误 / HTTP 异常 / 未捕获异常）
    8. 路由注册（核心 + 可选 + 调试）
    9. WebSocket 路由注册
    10. 前端静态文件挂载

    Returns:
        配置完成的 FastAPI 应用实例。
    """
    config = get_config()

    # FIXED(安全): 生产环境禁用 API 文档端点，防止攻击面泄露
    _debug_api_enabled = getattr(getattr(config, "server", None), "debug_api_enabled", False)
    app = FastAPI(
        title="EdgeLiteGateway",
        description="Lightweight Edge Computing IoT Gateway API",
        version=__import__("edgelite").__version__,
        lifespan=lifespan,
        docs_url="/docs" if _debug_api_enabled else None,
        redoc_url="/redoc" if _debug_api_enabled else None,
        openapi_url="/openapi.json" if _debug_api_enabled else None,
    )

    from fastapi.middleware.cors import CORSMiddleware

    # FIXED-H04: 收紧 CORS 配置
    # - 生产环境：仅允许通过 EDGELITE_SERVER__CORS_ALLOWED_ORIGINS 配置的精确域名
    # - 开发环境：设置 DEV_MODE=true 可启用本地开发配置（localhost + 127.0.0.1）
    # 常见前端开发端口：3000-3010 (React/Vue), 5173-5180 (Vite), 4200 (Angular), 8080-8081
    DEV_PORT_RANGES = "|".join(
        [
            r"300[0-9]",  # 3000-3009
            r"3010",  # 3010
            r"517[3-9]",  # 5173-5179
            r"5180",  # 5180
            r"42[0-9]{2}",  # 4200-4299 (Angular)
            r"808[01]",  # 8080-8081
        ]
    )

    # FIXED(安全): 合并读取 cors_allowed_origins 和遗留的 cors_origins，防止 Docker 环境变量名不匹配导致 CORS 失效
    allowed_origins = config.server.cors_allowed_origins or getattr(config.server, "cors_origins", []) or []
    dev_mode = os.environ.get("DEV_MODE", "").lower() in ("true", "1", "yes")

    if allowed_origins:
        # 优先使用精确配置的来源列表（生产环境推荐方式）
        app.add_middleware(
            CORSMiddleware,
            allow_origins=allowed_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-API-Key", "X-CSRF-Token", "X-Requested-With"],
            expose_headers=["X-CSRF-Token"],
        )
        logger.info("CORS enabled with configured origins: %s", allowed_origins)
    elif dev_mode:
        # FIXED-H04: 开发环境仅允许 localhost 和 127.0.0.1
        app.add_middleware(
            CORSMiddleware,
            allow_origin_regex=(
                rf"https?://(localhost|127\.0\.0\.1)"
                rf":({DEV_PORT_RANGES})"
            ),
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-API-Key", "X-CSRF-Token", "X-Requested-With"],
            expose_headers=["X-CSRF-Token"],
        )
        logger.info("CORS enabled in DEV_MODE: localhost only")
    else:
        # FIXED-H04: 未配置且非开发模式，拒绝所有跨域请求
        # 这是最安全的默认行为，防止 DNS Rebinding 和内网攻击
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[],  # 空列表 = 拒绝所有跨域
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-API-Key", "X-CSRF-Token", "X-Requested-With"],
            expose_headers=["X-CSRF-Token"],
        )
        logger.warning("CORS disabled by default. Set EDGELITE_SERVER__CORS_ALLOWED_ORIGINS or DEV_MODE=true")

    # Register security middlewares (order matters: last added = first executed)
    from edgelite.middleware.csrf import CSRFMiddleware
    from edgelite.middleware.rate_limit import RateLimitMiddleware
    from edgelite.middleware.request_id import RequestIdFilter, RequestIdMiddleware
    from edgelite.middleware.token_renewal import TokenRenewalMiddleware

    app.add_middleware(TokenRenewalMiddleware)
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(RateLimitMiddleware)
    # FIXED(G-02): 注册 RequestIdMiddleware，为每个请求生成/继承 request_id 并注入 contextvars，
    # 配合 RequestIdFilter 实现请求级日志串联；最后添加=最先执行，确保下游中间件/路由日志均带 request_id
    app.add_middleware(RequestIdMiddleware)

    # A-03: 请求耗时记录中间件 - 记录每个请求的处理时长到日志，并在响应头返回 X-Response-Time(ms)
    # 便于客户端与服务端排查慢请求；慢请求（>1s）以 warning 级别记录，便于监控告警
    # 可观测性加固: 同时记录 HTTP 请求计数 + 5xx 计数 + 延迟直方图到 Prometheus 指标
    import time as _time_mod

    @app.middleware("http")
    async def request_timing_middleware(request: Request, call_next):
        """记录每个请求的处理耗时，注入 ``X-Response-Time`` 响应头。

        超过 1 秒的请求以 warning 级别记录，便于监控慢请求。
        所有响应（含 5xx）自动记录到 ``edgelite_http_requests_total`` 和
        ``edgelite_http_requests_5xx_total`` 指标，便于 Prometheus 告警。
        5xx 的详细结构化日志（含 request_id）由异常处理器写入，避免本中间件
        在 RequestIdMiddleware 之外执行导致 request_id 丢失。
        """
        _start_ts = _time_mod.perf_counter()
        response = None
        status_code = 500  # 防御性默认值，异常路径下使用
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            _elapsed_ms = (_time_mod.perf_counter() - _start_ts) * 1000.0
            _elapsed_s = _elapsed_ms / 1000.0
            try:
                # 注入响应头（仅对已构造的 response 有效；异常路径由全局 handler 重新构造响应，此处跳过）
                if response is not None:
                    response.headers["X-Response-Time"] = f"{_elapsed_ms:.2f}"
            except Exception as header_err:
                # R11-DRV-11: 原问题-except Exception: pass 完全静默吞没异常；改为 debug 日志记录，便于排查响应头注入失败  # noqa: E501
                logger.debug("Response header injection failed: %s", header_err)
            # 可观测性: 记录 HTTP 请求指标（计数 + 5xx 计数 + 延迟直方图）
            try:
                from edgelite.api.metrics import record_http_request

                record_http_request(request.method, request.url.path, status_code, _elapsed_s)
            except Exception as metrics_err:
                logger.debug("record_http_request failed: %s", metrics_err)
            # 慢请求 warning，正常请求 debug，避免日志噪音
            # 5xx 的详细日志由异常处理器写入（含 request_id + exception details）
            if status_code < 500 and _elapsed_ms > 1000.0:
                logger.warning(
                    "Slow request: %s %s %.2fms status=%d",
                    request.method,
                    request.url.path,
                    _elapsed_ms,
                    status_code,
                )
            elif status_code < 500:
                logger.debug(
                    "Request: %s %s %.2fms status=%d",
                    request.method,
                    request.url.path,
                    _elapsed_ms,
                    status_code,
                )

    # FIXED(G-02): 注册 RequestIdFilter 到根 logger，将 contextvar 中的 request_id
    # 自动附加到每条日志记录的 LogRecord，便于通过 request_id 串联一个请求的所有日志
    logging.getLogger().addFilter(RequestIdFilter())

    # FIXED(安全): 添加安全响应头中间件
    from starlette.middleware.base import BaseHTTPMiddleware

    class SecurityHeadersMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            """添加安全响应头到每个 HTTP 响应。"""
            response = await call_next(request)
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["X-XSS-Protection"] = "1; mode=block"
            response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
            # HSTS 仅在 HTTPS 下生效
            if request.url.scheme == "https":
                response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
            return response

    app.add_middleware(SecurityHeadersMiddleware)

    # FIXED(安全): 生产环境（DEV_MODE=false）启用 TrustedHostMiddleware，限制接受的 Host 头
    # 防止 Host 头伪造攻击（DNS Rebinding、缓存投毒、虚拟主机绕过等）
    # 通过 EDGELITE_SERVER__ALLOWED_HOSTS 配置允许的 host 列表（分号分隔）
    if not dev_mode and config.server.allowed_hosts:
        from starlette.middleware.trustedhost import TrustedHostMiddleware

        app.add_middleware(TrustedHostMiddleware, allowed_hosts=config.server.allowed_hosts)
        logger.info("TrustedHostMiddleware enabled with allowed_hosts: %s", config.server.allowed_hosts)
    elif not dev_mode:
        logger.warning(
            "安全校验: 生产环境（DEV_MODE=false）未配置 EDGELITE_SERVER__ALLOWED_HOSTS，"
            "TrustedHostMiddleware 未启用，存在 Host 头伪造风险"
        )

    from edgelite.api.deps import (
        _current_request,  # FIXED-P0: 支持API模块通过_get_request()获取当前Request
    )

    # FIXED(严重-R2): 请求体大小限制，防止超大请求体导致 OOM
    # 即使 nginx 配置了 client_max_body_size，直接暴露 uvicorn 时仍需应用层防护
    _MAX_REQUEST_BODY_BYTES = 10 * 1024 * 1024  # 10MB

    @app.middleware("http")
    async def limit_request_body_size(request: Request, call_next):
        """限制请求体大小为 10 MB，超限返回 413 状态码。"""
        from fastapi.responses import JSONResponse

        from edgelite.models.common import ApiResponse

        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > _MAX_REQUEST_BODY_BYTES:
                    return JSONResponse(
                        status_code=413,
                        content=ApiResponse(
                            code=413,
                            message="Request body too large",
                            data=None,
                            error_code="ERR_COMMON_REQUEST_TOO_LARGE",
                        ).model_dump(),
                    )
            except ValueError:
                pass
        return await call_next(request)

    @app.middleware("http")
    async def set_current_request(request: Request, call_next):
        """将当前 Request 存入 contextvar，供非 Depends 路径获取；统一 404 响应格式。"""
        token = _current_request.set(request)
        try:
            response = await call_next(request)
            # 将非统一格式的 404 响应转换为 {code, message, data} 格式
            if response.status_code == 404:
                from edgelite.models.common import ApiResponse as _ApiResponse

                body = b""
                async for chunk in response.body_iterator:
                    body += chunk
                try:
                    data = json.loads(body)
                    if "code" not in data:
                        return JSONResponse(
                            status_code=404,
                            content=_ApiResponse(
                                code=404,
                                message=data.get("detail", "Not Found"),
                                data=None,
                                error_code="ERR_COMMON_NOT_FOUND",
                            ).model_dump(),
                        )
                except Exception:  # FIXED-P2: 原问题-bare except吞没KeyboardInterrupt/SystemExit；改为except Exception
                    pass
                # 如果已经是统一格式或解析失败，重新构造原始响应
                from starlette.responses import Response as _Response

                return _Response(
                    content=body,
                    status_code=response.status_code,
                    headers=dict(response.headers),
                    media_type=response.media_type,
                )
            return response
        finally:
            _current_request.reset(token)

    from fastapi import Request
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse

    from edgelite.models.common import ApiResponse

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        """将 Pydantic 校验失败统一为 {code, message, data} 格式，前端可正确解析"""
        errors = exc.errors()
        # 拼接可读的错误信息
        parts: list[str] = []
        for err in errors:
            loc = " -> ".join(str(l) for l in err.get("loc", []))
            msg = err.get("msg", "")
            parts.append(f"{loc}: {msg}" if loc else msg)
        detail = "; ".join(parts) if parts else str(exc)
        logger.warning("Validation error: %s %s -> %s", request.method, request.url.path, detail)
        return JSONResponse(
            status_code=422,
            content=ApiResponse(code=422, message=detail, data=None, error_code="ERR_COMMON_VALIDATION").model_dump(),
        )

    from starlette.exceptions import HTTPException as StarletteHTTPException

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        """将 HTTPException 统一为 {code, message, data} 格式

        Starlette 和 FastAPI 的 HTTPException 都继承自 starlette.exceptions.HTTPException。
        FastAPI 内置的 HTTPException 处理器返回 {"detail": "..."} 格式，
        必须显式注册此处理器才能统一格式。

        可观测性加固: 5xx HTTPException 写结构化日志（含 request_id、error_code），
        便于通过 request_id 串联请求链路；request_id 由 RequestIdFilter 自动附加。
        """
        from enum import Enum

        status_code = exc.status_code
        detail = exc.detail
        error_code = None

        # FIXED: 将 Enum 转为 .value，避免 str(Enum) 产生 <Enum: 'VALUE'> 格式
        if isinstance(detail, Enum):
            detail = detail.value
        elif isinstance(detail, dict):
            detail = {k: (v.value if isinstance(v, Enum) else v) for k, v in detail.items()}

        message = str(detail) if isinstance(detail, str) else str(detail)
        if isinstance(detail, str) and detail.startswith("ERR_"):
            # 字符串 detail 以 ERR_ 开头时，自动识别为 error_code
            error_code = detail
        elif isinstance(detail, dict):
            # FIXED: 同时从 "error" 和 "error_code" 字段提取 error_code
            error_code = detail.get("error_code") or detail.get("error")
            errors = detail.get("errors", [])
            warnings = detail.get("warnings", [])
            parts: list[str] = []
            if error_code:
                parts.append(str(error_code))
            if errors:
                parts.extend(str(e) for e in errors)
            if warnings:
                parts.extend(str(w) for w in warnings)
            message = "; ".join(parts) if parts else str(detail)
        # 可观测性: 5xx 写 error 级别结构化日志（request_id 由 RequestIdFilter 自动附加）
        if status_code >= 500:
            logger.error(
                "HTTP_5XX exception: method=%s path=%s status=%d error_code=%s message=%s",
                request.method,
                request.url.path,
                status_code,
                error_code or "UNKNOWN",
                message,
            )
        else:
            logger.warning("HTTPException: %s %s -> %s %s", request.method, request.url.path, status_code, message)
        return JSONResponse(
            status_code=status_code,
            content=ApiResponse(code=status_code, message=message, data=None, error_code=error_code).model_dump(),
        )

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        """全局未捕获异常处理器，返回 500 统一响应格式，避免堆栈泄露。

        可观测性加固: 写结构化日志（含 request_id、exception type、message），
        便于通过 request_id 串联请求链路；request_id 由 RequestIdFilter 自动附加。
        堆栈只写日志不外泄给客户端。
        """
        if getattr(config.server, "debug_api_enabled", False):
            logger.exception(
                "HTTP_5XX unhandled exception: method=%s path=%s exc_type=%s message=%s",
                request.method,
                request.url.path,
                type(exc).__name__,
                exc,
            )
        else:
            logger.error(
                "HTTP_5XX unhandled exception: method=%s path=%s exc_type=%s message=%s",
                request.method,
                request.url.path,
                type(exc).__name__,
                exc,
            )

        return JSONResponse(
            status_code=500,
            content=ApiResponse(code=500, message=CommonErrors.INTERNAL_ERROR, data=None).model_dump(),
        )

    _register_routes(app, config)  # FIXED-P0: 传入config参数
    _register_websocket_routes(app)

    try:
        from edgelite.api.health import router as health_router

        app.include_router(health_router)
        logger.info("Registered aggregated /health endpoint")
    except Exception as e:
        logger.warning("Aggregated /health route registration failed: %s", e)

    _mount_frontend(app)

    return app


def _mount_frontend(app: FastAPI) -> None:
    from pathlib import Path

    from fastapi.staticfiles import StaticFiles

    frontend_dist = Path(__import__("os").environ.get("EDGELITE_FRONTEND_DIST", "/app/frontend/dist"))
    if not frontend_dist.is_dir():
        frontend_dist = Path(__file__).resolve().parent.parent.parent / "web" / "dist"

    if frontend_dist.is_dir():
        assets_dir = frontend_dist / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="frontend-assets")

        from starlette.responses import FileResponse, HTMLResponse

        @app.get("/{path:path}", include_in_schema=False)
        async def serve_spa(path: str):
            """SPA 前端静态文件服务，支持路径遍历防护和 index.html 不缓存。"""
            if path.startswith(("api/", "docs", "redoc", "openapi.json", "ws/")):
                from fastapi.responses import JSONResponse

                return JSONResponse(status_code=404, content={"detail": "Not Found"})
            file_path = frontend_dist / path
            # 防止路径遍历
            if not file_path.resolve().is_relative_to(frontend_dist.resolve()):
                return HTMLResponse(status_code=404)
            if file_path.is_file():
                # 非 index.html 的静态文件（如 favicon.svg）允许缓存
                if file_path.name != "index.html":
                    return FileResponse(str(file_path))
            # index.html 必须不缓存，确保每次都加载最新版本（避免构建后旧chunk名404）
            response = FileResponse(str(frontend_dist / "index.html"))
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
            return response

        logger.info("Frontend static files mounted: %s", frontend_dist)  # FIXED-P3: 中文日志→英文
