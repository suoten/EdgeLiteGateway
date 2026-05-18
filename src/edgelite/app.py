"""FastAPI应用工厂"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect

from edgelite.bootstrap import ServiceContainer, bootstrap_all, teardown
from edgelite.config import get_config

logger = logging.getLogger(__name__)

_app_state = ServiceContainer()


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = get_config()

    try:
        await bootstrap_all(_app_state, config)
    except Exception as init_err:
        logger.error("初始化失败: %s，开始清理已初始化资源", init_err)
        await teardown(_app_state)
        raise

    for key, value in _app_state.__dict__.items():
        if not key.startswith("_"):
            setattr(app.state, key, value)

    yield

    await teardown(_app_state)


def _register_routes(app: FastAPI) -> None:
    from edgelite.api import alarms, auth, data, devices, rules, system, users, video

    app.include_router(auth.router)
    app.include_router(devices.router)
    app.include_router(rules.router)
    app.include_router(alarms.router)
    app.include_router(data.router)
    app.include_router(video.router)
    app.include_router(system.router)
    app.include_router(users.router)

    # FIXED: 原问题-路由标签中文硬编码，现改为英文标签
    _optional_routers = [
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
        ("OTA", "edgelite.api.ota", "router"),
        ("Services", "edgelite.api.services", "router"),
        ("Grafana", "edgelite.api.grafana", "router"),
        ("SCADA", "edgelite.api.scada", "router"),
    ]

    for label, module_path, attr in _optional_routers:
        try:
            import importlib
            mod = importlib.import_module(module_path)
            app.include_router(getattr(mod, attr))
        except Exception as e:
            logger.warning("%s路由注册失败: %s", label, e)


def _register_websocket_routes(app: FastAPI) -> None:
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

    @app.websocket("/ws/v1/integration")
    async def ws_integration(websocket: WebSocket, token: str = Query(...)):
        if not _app_state.integration_endpoint:
            await websocket.close(code=1003, reason="Integration not available")
            return
        from edgelite.security.jwt import verify_token

        try:
            verify_token(token, token_type="access")
        except Exception:
            await websocket.close(code=4001, reason="Auth failed")
            return
        await websocket.accept()
        session_id = None
        try:
            handshake_msg = await websocket.receive_text()
            import json as _json

            # FIXED: 原问题-WebSocket握手消息JSON解析无异常保护，恶意连接可导致崩溃
            try:
                handshake_data = _json.loads(handshake_msg)
            except _json.JSONDecodeError:
                await websocket.send(_json.dumps({"type": "error", "message": "Invalid JSON"}))
                return
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


def create_app() -> FastAPI:
    config = get_config()

    app = FastAPI(
        title="EdgeLiteGateway",
        description="Lightweight Edge Computing IoT Gateway API",
        version=__import__("edgelite").__version__,
        lifespan=lifespan,
    )

    from fastapi.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.server.cors_origins,
        allow_origin_regex=r"https?://(localhost|127\.0\.0\.1|\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})(:\d+)?",
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-API-Key"],
    )

    from fastapi import Request
    from fastapi.responses import JSONResponse

    from edgelite.models.common import ApiResponse

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        logger.exception("Unhandled exception: %s %s -> %s", request.method, request.url.path, exc)
        # FIXED: 原问题-全局异常处理器中文硬编码，现使用错误码
        from edgelite.api.error_codes import CommonErrors

        return JSONResponse(
            status_code=500,
            content=ApiResponse(
                code=1, message=CommonErrors.INTERNAL_ERROR, data=None
            ).model_dump(),
        )

    _register_routes(app)
    _register_websocket_routes(app)

    @app.get("/health", tags=["System"], summary="Health Check", include_in_schema=False)
    async def health_check():
        return {"status": "ok"}

    _mount_frontend(app)

    return app


def _mount_frontend(app: FastAPI) -> None:
    from pathlib import Path

    from fastapi.staticfiles import StaticFiles

    frontend_dist = Path("/app/frontend/dist")
    if not frontend_dist.is_dir():
        frontend_dist = Path(__file__).resolve().parent.parent.parent / "web" / "dist"

    if frontend_dist.is_dir():
        assets_dir = frontend_dist / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="frontend-assets")

        from starlette.responses import FileResponse

        @app.get("/{path:path}", include_in_schema=False)
        async def serve_spa(path: str):
            if path.startswith(("api/", "docs", "redoc", "openapi.json", "ws/")):
                from fastapi.responses import JSONResponse
                return JSONResponse(status_code=404, content={"detail": "Not Found"})
            file_path = frontend_dist / path
            if file_path.is_file():
                return FileResponse(str(file_path))
            return FileResponse(str(frontend_dist / "index.html"))

        logger.info("前端静态文件已挂载: %s", frontend_dist)
