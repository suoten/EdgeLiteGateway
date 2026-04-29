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
        self._device_configs: dict[str, dict] = {}
        self._device_points: dict[str, list[dict]] = {}
        self._latest_values: dict[str, dict[str, Any]] = {}
        self._connect_task: asyncio.Task | None = None
        self._data_callback: Callable | None = None
        self._pub_queue: asyncio.Queue | None = None

    async def start(self, config: dict) -> None:
        self._running = True
        self._pub_queue = asyncio.Queue(maxsize=1000)
        self._connect_task = asyncio.create_task(self._connect_loop(), name="mqtt-client-connect")
        logger.info("MQTT Client驱动启动")

    async def stop(self) -> None:
        self._running = False
        if self._connect_task and not self._connect_task.done():
            self._connect_task.cancel()
        if self._connect_task:
            try:
                await self._connect_task
            except asyncio.CancelledError:
                pass
        logger.info("MQTT Client驱动停止")

    async def add_device(self, device_id: str, config: dict, points: list[dict]) -> None:
        self._device_configs[device_id] = config
        self._device_points[device_id] = points
        self._latest_values[device_id] = {}

    async def remove_device(self, device_id: str) -> None:
        self._device_configs.pop(device_id, None)
        self._device_points.pop(device_id, None)
        self._latest_values.pop(device_id, None)

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        values = self._latest_values.get(device_id, {})
        return {p: values.get(p) for p in points if p in values}

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        if not self._pub_queue:
            return False
        config = self._device_configs.get(device_id, {})
        publish_topic = config.get("publish_topic", f"edgelite/{device_id}/command")
        message = json.dumps({"point": point, "value": value}, ensure_ascii=False)
        try:
            self._pub_queue.put_nowait((publish_topic, message.encode("utf-8")))
            return True
        except asyncio.QueueFull:
            logger.warning("MQTT发布队列已满，丢弃消息")
            return False

    def on_data(self, callback: Callable) -> None:
        self._data_callback = callback

    async def _connect_loop(self) -> None:
        config = get_config()
        broker = config.mqtt.broker
        port = config.mqtt.port
        username = config.mqtt.username or None
        password = config.mqtt.password or None

        while self._running:
            try:
                import aiomqtt

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
                    tls_params=aiomqtt.TLSParameters(ssl_context=ssl_context) if ssl_context else None,
                ) as client:
                    logger.info("MQTT连接成功: %s:%d", broker, port)

                    for device_id, dev_config in self._device_configs.items():
                        topic = dev_config.get("subscribe_topic", f"edgelite/{device_id}/data")
                        await client.subscribe(topic)

                    msg_task = asyncio.create_task(
                        self._message_loop(client), name="mqtt-client-msg"
                    )
                    pub_task = asyncio.create_task(
                        self._publish_loop(client), name="mqtt-client-publish"
                    )

                    try:
                        while self._running:
                            await asyncio.sleep(1)
                    finally:
                        for t in [msg_task, pub_task]:
                            if not t.done():
                                t.cancel()
                        for t in [msg_task, pub_task]:
                            try:
                                await t
                            except asyncio.CancelledError:
                                pass

            except asyncio.CancelledError:
                raise
            except ImportError:
                logger.error("aiomqtt未安装，MQTT驱动不可用")
                await asyncio.sleep(30)
            except Exception as e:
                logger.error("MQTT连接异常: %s，5秒后重试", e)
                await asyncio.sleep(5)

    async def _message_loop(self, client: Any) -> None:
        try:
            async for message in client.messages:
                if not self._running:
                    break
                await self._handle_message(message)
        except asyncio.CancelledError:
            pass

    async def _publish_loop(self, client: Any) -> None:
        try:
            while self._running:
                try:
                    topic, payload = await asyncio.wait_for(
                        self._pub_queue.get(), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue
                try:
                    await client.publish(topic, payload)
                except Exception as e:
                    logger.error("MQTT发布失败: %s", e)
        except asyncio.CancelledError:
            pass

    async def _handle_message(self, message: Any) -> None:
        try:
            topic = str(message.topic)
            payload = message.payload.decode("utf-8")

            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                data = {"value": payload}

            for device_id, dev_config in self._device_configs.items():
                subscribe_topic = dev_config.get("subscribe_topic", f"edgelite/{device_id}/data")
                if topic == subscribe_topic or topic.endswith(subscribe_topic):
                    if isinstance(data, dict):
                        self._latest_values[device_id].update(data)
                    else:
                        self._latest_values[device_id]["value"] = data

                    if self._data_callback:
                        await self._data_callback(device_id, data)
                    break

        except Exception as e:
            logger.error("MQTT消息处理失败: %s", e)
