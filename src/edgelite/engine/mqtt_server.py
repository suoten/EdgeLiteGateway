"""内置MQTT Server - 基于amqtt库，提供轻量级MQTT Broker服务

Pro版特性：内置MQTT Server方便前端直连和系统级联，
无需外挂Mosquitto即可实现MQTT发布/订阅。
默认端口1888，支持WebSocket接入(/mqtt)。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


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
                "amqtt未安装，内置MQTT Server不可用。"
                "请执行: pip install amqtt"
            )
            return

        config = config or {}
        host = config.get("host", "0.0.0.0")
        port = int(config.get("port", 1888))

        # 构建amqtt broker配置
        broker_config = {
            "listeners": {
                "default": {
                    "type": "tcp",
                    "bind": f"{host}:{port}",
                },
            },
            "sys_interval": 10,
            "auth": {
                "allow-anonymous": not config.get("username"),
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
        username = config.get("username")
        password = config.get("password")
        if username and password:
            broker_config["auth"]["password-db"] = {
                username: password,
            }

        try:
            self._broker = Broker(broker_config)
            self._task = asyncio.create_task(
                self._broker.start(), name="mqtt-server"
            )
            self._running = True
            logger.info("内置MQTT Server启动: %s:%d", host, port)
            if ws_port:
                logger.info("MQTT WebSocket端口: %d", int(ws_port))
        except Exception as e:
            logger.error("内置MQTT Server启动失败: %s", e)
            self._broker = None

    async def stop(self) -> None:
        """停止内置MQTT Server"""
        self._running = False
        if self._broker:
            try:
                await self._broker.shutdown()
            except Exception as e:
                logger.warning("MQTT Server关闭异常: %s", e)
            self._broker = None
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("内置MQTT Server已停止")

    @property
    def is_running(self) -> bool:
        """MQTT Server是否运行中"""
        return self._running
