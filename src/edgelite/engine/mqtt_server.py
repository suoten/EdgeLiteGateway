"""内置MQTT Server - 基于amqtt库，提供轻量级MQTT Broker服务

Pro版特性：内置MQTT Server方便前端直连和系统级联，
无需外挂Mosquitto即可实现MQTT发布/订阅。
默认端口1888，支持WebSocket接入(/mqtt)。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# FIXED: 添加模块级 _AMQTT_AVAILABLE 标志和 _MqttAuthPlugin 认证插件
try:
    from amqtt.broker import Broker as _Broker
    from amqtt.plugins.authentication import BaseAuthPlugin as _BaseAuthPlugin

    _AMQTT_AVAILABLE = True
except ImportError:
    _AMQTT_AVAILABLE = False
    _Broker = None  # type: ignore[assignment,misc]
    _BaseAuthPlugin = None  # type: ignore[assignment,misc]


class _MqttAuthPlugin(_BaseAuthPlugin if _AMQTT_AVAILABLE else object):  # type: ignore[misc]
    """MQTT Server 认证插件 - 基于 username/password 的简单认证。"""

    @dataclass
    class Config:
        """认证配置。"""

        username: str = ""
        password: str = ""

    async def authenticate(self, *, session=None) -> bool:
        """验证客户端连接凭据。

        Returns:
            True if credentials are valid or anonymous access is allowed, False otherwise.
        """
        username = getattr(session, "username", None) if session else None
        password = getattr(session, "password", None) if session else None

        config = self.context.config if self.context else None
        if config is None:
            return False

        expected_user = getattr(config, "username", "")
        expected_pass = getattr(config, "password", "")

        # 无认证模式：未配置凭据时允许匿名连接
        # bootstrap 已将无认证服务器降级为 localhost 绑定，本地连接安全
        if not expected_user:
            return True

        # 认证模式：校验用户名和密码
        return username == expected_user and password == expected_pass


class MqttServer:
    """内置MQTT Server

    基于amqtt（asyncio MQTT broker）实现，
    提供轻量级MQTT Broker服务，支持：
    - 标准MQTT 3.1.1协议
    - WebSocket接入
    - 认证（username/password）
    - 系统级联（其他网关/设备可连接）
    """

    def __init__(self):
        self._running = False
        self._broker = None
        self._task: asyncio.Task | None = None

    async def start(self, config: dict | None = None) -> None:
        """启动内置MQTT Server

        Args:
            config: 配置参数
                host: 监听地址 (默认"0.0.0.0")
                port: 监听端口 (默认1888)
                ws_port: WebSocket端口 (默认无)
                username: 认证用户名 (可选)
                password: 认证密码 (可选)
        """
        try:
            from amqtt.broker import Broker
        except ImportError:
            logger.warning(
                "amqtt not installed, built-in MQTT Server unavailable. Run: pip install amqtt"
            )  # FIXED-P3: 中文日志→英文
            return

        config = config or {}
        host = config.get("host", "127.0.0.1")  # FIXED-P4: 默认绑定localhost，与config层一致
        port = int(config.get("port", 1888))

        # 认证配置: 通过自定义 _MqttAuthPlugin 实现
        # 无凭据时允许匿名连接（已由 bootstrap 降级为 localhost 绑定）
        # 有凭据时校验用户名和密码
        username = config.get("username")
        password = config.get("password")
        if username:
            logger.info("MQTT Server auth enabled: username=%s", username)

        # 构建amqtt broker配置
        # aMQTT 0.11.x 的 plugins 字典 key 必须是完整的模块路径
        broker_config = {
            "listeners": {
                "default": {
                    "type": "tcp",
                    "bind": f"{host}:{port}",
                },
            },
            "plugins": {
                "amqtt.plugins.logging_amqtt.EventLoggerPlugin": {},
                "amqtt.plugins.topic_checking.TopicTabooPlugin": {},
                "edgelite.engine.mqtt_server._MqttAuthPlugin": {
                    "username": username or "",
                    "password": password or "",
                },
            },
        }

        # WebSocket监听
        ws_port = config.get("ws_port")
        if ws_port:
            broker_config["listeners"]["ws"] = {
                "type": "ws",
                "bind": f"{host}:{int(ws_port)}",
            }

        try:
            self._broker = Broker(broker_config)
            self._task = asyncio.create_task(self._broker.start(), name="mqtt-server")

            def _on_broker_done(
                task: asyncio.Task,
            ) -> None:  # FIXED-P1: broker启动任务异常处理，原实现任务异常被静默丢失
                if task.cancelled():
                    return
                exc = task.exception()
                if exc is not None:
                    logger.error("Built-in MQTT Server broker task failed: %s", exc)
                    self._running = False
                    self._broker = None

            self._task.add_done_callback(_on_broker_done)
            self._running = True
            logger.info("Built-in MQTT Server started: %s:%d", host, port)  # FIXED-P3: 中文日志→英文
            if ws_port:
                logger.info("MQTT WebSocket port: %d", int(ws_port))  # FIXED-P3: 中文日志→英文
        except Exception as e:
            logger.error("Built-in MQTT Server start failed: %s", e)  # FIXED-P3: 中文日志→英文
            self._broker = None
            self._running = False

    async def stop(self) -> None:
        """停止内置MQTT Server"""
        self._running = False
        if self._broker:
            try:
                await self._broker.shutdown()
            except Exception as e:
                logger.warning("MQTT Server shutdown error: %s", e)  # FIXED-P3: 中文日志→英文
            self._broker = None
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        logger.info("Built-in MQTT Server stopped")  # FIXED-P3: 中文日志→英文

    @property
    def is_running(self) -> bool:
        """MQTT Server是否运行中"""
        return self._running
