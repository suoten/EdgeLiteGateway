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


class _MqttAuthPlugin(_BaseAuthPlugin if _AMQTT_AVAILABLE else object):
    """MQTT Server 认证插件 - 基于 username/password 的简单认证。"""

    @dataclass
    class Config:
        """认证配置。"""

        username: str = ""
        password: str = ""

    async def authenticate(self, *, session=None) -> bool:
        """验证客户端连接凭据。

        Returns:
            True if credentials are valid, False otherwise.
        """
        username = getattr(session, "username", None) if session else None
        password = getattr(session, "password", None) if session else None

        config = self.context.config if self.context else None
        if config is None:
            return False

        expected_user = getattr(config, "username", "")
        expected_pass = getattr(config, "password", "")

        # FIXED: fail-closed 策略 - 未配置凭据时拒绝所有连接
        if not expected_user:
            return False

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

        # 构建amqtt broker配置
        # FIXED: aMQTT 0.11.x 的 plugins 字典 key 必须是完整的模块路径（如 amqtt.plugins.xxx.ClassName）
        # 旧版使用的 "sys"/"auth"/"topic-check" 简写不再支持，会导致 PluginImportError
        # FIXED(P3): 原问题-F841未使用局部变量allow_anonymous; 修复-删除赋值
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
            },
        }

        # WebSocket监听
        ws_port = config.get("ws_port")
        if ws_port:
            broker_config["listeners"]["ws"] = {
                "type": "ws",
                "bind": f"{host}:{int(ws_port)}",
            }

        # 认证配置
        # FIXED: aMQTT 0.11.x 不再支持 plugins.auth 简写配置
        # 如果需要认证，应使用自定义认证插件或 TopicAccessControlListPlugin
        username = config.get("username")
        password = config.get("password")
        if username and password:
            # FIXED(严重): 原问题-配置了认证但未启用，接受匿名连接;
            # 修复-配置了认证但无法启用时拒绝启动，避免匿名访问
            logger.error(
                "MQTT Server auth configured (username=%s) but aMQTT 0.11.x requires custom auth plugin. "
                "Refusing to start with anonymous access.",
                username,
            )
            raise RuntimeError(
                "MQTT authentication configured but cannot be enabled. Disable auth config or upgrade aMQTT."
            )

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
