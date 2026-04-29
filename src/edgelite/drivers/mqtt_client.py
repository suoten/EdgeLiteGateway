"""MQTT Client驱动 - 基于aiomqtt实现"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

from edgelite.drivers.base import DriverPlugin
from edgelite.config import get_config

logger = logging.getLogger(__name__)


class MqttClientDriver(DriverPlugin):
    """MQTT Client协议驱动，订阅设备数据主题"""

    plugin_name = "mqtt_client"
    plugin_version = "0.1.0"
    supported_protocols = ["mqtt"]

    def __init__(self):
        self._running = False
        # device_id -> config
        self._device_configs: dict[str, dict] = {}
        # device_id -> points定义
        self._device_points: dict[str, list[dict]] = {}
        # device_id -> latest_values
        self._latest_values: dict[str, dict[str, Any]] = {}
        # MQTT客户端
        self._client = None
        self._connect_task: asyncio.Task | None = None
        # 数据回调
        self._data_callback: Callable | None = None

    async def start(self, config: dict) -> None:
        """启动MQTT客户端连接"""
        self._running = True
        self._connect_task = asyncio.create_task(self._connect_loop(), name="mqtt-connect")
        logger.info("MQTT Client驱动启动")

    async def stop(self) -> None:
        """停止驱动"""
        self._running = False
        if self._connect_task and not self._connect_task.done():
            self._connect_task.cancel()
            try:
                await self._connect_task
            except asyncio.CancelledError:
                pass
        if self._client:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None
        logger.info("MQTT Client驱动停止")

    async def add_device(self, device_id: str, config: dict, points: list[dict]) -> None:
        """添加MQTT设备"""
        self._device_configs[device_id] = config
        self._device_points[device_id] = points
        self._latest_values[device_id] = {}

        # 如果已连接，订阅该设备的主题
        subscribe_topic = config.get("subscribe_topic", f"edgelite/{device_id}/data")
        if self._client:
            try:
                await self._client.subscribe(subscribe_topic)
                logger.info("MQTT订阅: %s -> %s", device_id, subscribe_topic)
            except Exception as e:
                logger.error("MQTT订阅失败: %s - %s", device_id, e)

    async def remove_device(self, device_id: str) -> None:
        """移除设备"""
        self._device_configs.pop(device_id, None)
        self._device_points.pop(device_id, None)
        self._latest_values.pop(device_id, None)

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        """读取测点值（返回最新缓存值）"""
        values = self._latest_values.get(device_id, {})
        return {p: values.get(p) for p in points if p in values}

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        """写入测点值（通过MQTT发布）"""
        if not self._client:
            return False

        config = self._device_configs.get(device_id, {})
        publish_topic = config.get("publish_topic", f"edgelite/{device_id}/command")

        try:
            message = json.dumps({"point": point, "value": value}, ensure_ascii=False)
            await self._client.publish(publish_topic, message.encode())
            return True
        except Exception as e:
            logger.error("MQTT发布失败: %s - %s", device_id, e)
            return False

    def on_data(self, callback: Callable) -> None:
        """注册数据回调"""
        self._data_callback = callback

    async def _connect_loop(self) -> None:
        """MQTT连接与消息接收循环"""
        config = get_config()
        broker = config.mqtt.broker
        port = config.mqtt.port
        username = config.mqtt.username or None
        password = config.mqtt.password or None

        while self._running:
            try:
                import aiomqtt

                # TLS支持
                ssl_context = None
                tls_config = getattr(config.mqtt, 'tls', None)
                if tls_config:
                    try:
                        from edgelite.engine.mqtt_tls import MqttTlsHelper
                        ssl_context = MqttTlsHelper.create_ssl_context(
                            ca_cert=getattr(tls_config, 'ca_cert', ''),
                            client_cert=getattr(tls_config, 'client_cert', ''),
                            client_key=getattr(tls_config, 'client_key', ''),
                            cert_reqs=getattr(tls_config, 'cert_reqs', 'required'),
                        )
                        if ssl_context:
                            logger.info("MQTT TLS已启用")
                    except Exception as e:
                        logger.error("MQTT TLS配置失败: %s", e)

                async with aiomqtt.Client(
                    hostname=broker,
                    port=port,
                    username=username,
                    password=password,
                    keepalive=60,
                    tls_params=ssl_context,
                ) as client:
                    self._client = client
                    logger.info("MQTT连接成功: %s:%d", broker, port)

                    # 订阅所有设备的主题
                    for device_id, dev_config in self._device_configs.items():
                        topic = dev_config.get("subscribe_topic", f"edgelite/{device_id}/data")
                        await client.subscribe(topic)

                    # 消息循环
                    async for message in client.messages:
                        if not self._running:
                            break
                        await self._handle_message(message)

            except asyncio.CancelledError:
                raise
            except ImportError:
                logger.error("aiomqtt未安装，MQTT驱动不可用")
                await asyncio.sleep(30)
            except Exception as e:
                logger.error("MQTT连接异常: %s，5秒后重试", e)
                self._client = None
                await asyncio.sleep(5)

    async def _handle_message(self, message: Any) -> None:
        """处理收到的MQTT消息"""
        try:
            topic = str(message.topic)
            payload = message.payload.decode("utf-8")

            # 尝试解析JSON
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                data = {"value": payload}

            # 根据主题匹配设备
            for device_id, dev_config in self._device_configs.items():
                subscribe_topic = dev_config.get("subscribe_topic", f"edgelite/{device_id}/data")
                if topic == subscribe_topic or topic.endswith(subscribe_topic):
                    # 更新缓存值
                    if isinstance(data, dict):
                        self._latest_values[device_id].update(data)
                    else:
                        self._latest_values[device_id]["value"] = data

                    # 触发数据回调
                    if self._data_callback:
                        await self._data_callback(device_id, data)
                    break

        except Exception as e:
            logger.error("MQTT消息处理失败: %s", e)
