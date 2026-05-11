"""MQTT Sparkplug B协议驱动 - 基于aiomqtt + sparkplugb ProtoBuf实现"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from typing import Any

from edgelite.config import get_config
from edgelite.drivers.base import DriverPlugin

logger = logging.getLogger(__name__)

try:
    from sparkplugb import sparkplug_b_pb2 as _pb2
except ImportError:
    _pb2 = None


_SPB_DATATYPE_BOOLEAN = 1
_SPB_DATATYPE_INT8 = 2
_SPB_DATATYPE_INT16 = 3
_SPB_DATATYPE_INT32 = 4
_SPB_DATATYPE_INT64 = 5
_SPB_DATATYPE_UINT8 = 6
_SPB_DATATYPE_UINT16 = 7
_SPB_DATATYPE_UINT32 = 8
_SPB_DATATYPE_UINT64 = 9
_SPB_DATATYPE_FLOAT = 10
_SPB_DATATYPE_DOUBLE = 11
_SPB_DATATYPE_STRING = 12
_SPB_DATATYPE_DATETIME = 13
_SPB_DATATYPE_TEXT = 14


def _map_datatype(value: Any) -> int | None:
    if isinstance(value, bool):
        return _SPB_DATATYPE_BOOLEAN
    if isinstance(value, int):
        if value >= 0:
            if value <= 4294967295:
                return _SPB_DATATYPE_UINT32
            else:
                return _SPB_DATATYPE_UINT64
        else:
            return _SPB_DATATYPE_INT64 if abs(value) > 2147483647 else _SPB_DATATYPE_INT32
    if isinstance(value, float):
        return _SPB_DATATYPE_DOUBLE
    if isinstance(value, str):
        return _SPB_DATATYPE_STRING
    return None


def _set_metric_value(metric: Any, datatype: int, value: Any) -> None:
    if datatype == _SPB_DATATYPE_BOOLEAN:
        metric.boolean_value = bool(value)
    elif datatype in (
        _SPB_DATATYPE_INT8,
        _SPB_DATATYPE_INT16,
        _SPB_DATATYPE_INT32,
        _SPB_DATATYPE_UINT8,
        _SPB_DATATYPE_UINT16,
        _SPB_DATATYPE_UINT32,
    ):
        metric.int_value = int(value)
    elif datatype in (_SPB_DATATYPE_INT64, _SPB_DATATYPE_UINT64, _SPB_DATATYPE_DATETIME):
        metric.long_value = int(value)
    elif datatype == _SPB_DATATYPE_FLOAT:
        metric.float_value = float(value)
    elif datatype == _SPB_DATATYPE_DOUBLE:
        metric.double_value = float(value)
    elif datatype in (_SPB_DATATYPE_STRING, _SPB_DATATYPE_TEXT):
        metric.string_value = str(value)
    else:
        metric.string_value = str(value)


def _get_metric_value(metric: Any) -> Any:
    which = metric.WhichOneof("value")
    if which is None:
        return None
    return getattr(metric, which)


class SparkplugBDriver(DriverPlugin):
    """MQTT Sparkplug B 协议驱动"""

    plugin_name = "sparkplug_b"
    plugin_version = "0.1.0"
    supported_protocols = ["sparkplug_b"]
    config_schema = {
        "description": "MQTT Sparkplug B工业物联网协议，标准化设备数据发布/订阅",
        "fields": [
            {"name": "group_id", "type": "string", "label": "组ID", "description": "Sparkplug B逻辑组ID", "default": "group1", "required": True},
            {"name": "edge_node_id", "type": "string", "label": "边缘节点ID", "description": "本网关在Sparkplug B中的节点ID", "default": "edgelite_node", "required": True},
            {"name": "mqtt_broker", "type": "string", "label": "Broker地址", "description": "MQTT Broker地址", "default": "localhost", "required": True},
            {"name": "mqtt_port", "type": "integer", "label": "端口", "description": "MQTT Broker端口", "default": 1883},
        ],
    }

    def __init__(self):
        self._running = False
        self._client = None
        self._connect_task: asyncio.Task | None = None
        self._data_callback: Callable | None = None

        self._group_id: str = ""
        self._edge_node_id: str = ""
        self._seq_num: int = 0
        self._birth_debounce_ms: int = 1000

        self._device_points: dict[str, dict[str, Any]] = {}
        self._device_metadata: dict[str, dict] = {}
        self._latest_values: dict[str, dict[str, Any]] = {}

        self._nbirth_published: bool = False
        self._dbirth_published: set[str] = set()

    def _next_seq(self) -> int:
        seq = self._seq_num
        self._seq_num = (self._seq_num + 1) % 256
        return seq

    def _build_topic(self, msg_type: str, device_id: str | None = None) -> str:
        topic = f"spBv1.0/{self._group_id}/{msg_type}/{self._edge_node_id}"
        if device_id is not None:
            topic = f"{topic}/{device_id}"
        return topic

    def _encode_payload(self, metrics: list[dict], seq: int | None = None) -> bytes | None:
        if _pb2 is None:
            logger.error("sparkplugb库未安装，无法编码Payload")
            return None

        try:
            payload = _pb2.Payload()
            payload.timestamp = int(time.time() * 1000)
            if seq is not None:
                payload.seq = seq

            for m in metrics:
                value = m.get("value")
                datatype = m.get("datatype") or _map_datatype(value)
                if datatype is None:
                    logger.warning(
                        "跳过不支持的类型测点: name=%s, type=%s",
                        m.get("name"),
                        type(value).__name__,
                    )
                    continue

                metric = payload.metrics.add()
                metric.name = m.get("name", "")
                metric.datatype = datatype
                _set_metric_value(metric, datatype, value)

                if m.get("alias") is not None:
                    metric.alias = int(m["alias"])
                if m.get("timestamp") is not None:
                    metric.timestamp = int(m["timestamp"])
                if m.get("is_historical"):
                    metric.is_historical = True
                if m.get("is_transient"):
                    metric.is_transient = True
                if m.get("metadata"):
                    meta = metric.metadata
                    for k, v in m["metadata"].items():
                        if isinstance(v, str):
                            meta.content[k] = v

            return payload.SerializeToString()
        except Exception as e:
            logger.error("Sparkplug B编码失败: %s", e)
            return None

    def _decode_payload(self, data: bytes) -> list[dict] | None:
        if _pb2 is None:
            logger.error("sparkplugb库未安装，无法解码Payload")
            return None

        try:
            payload = _pb2.Payload()
            payload.ParseFromString(data)
            metrics = []
            for m in payload.metrics:
                metrics.append(
                    {
                        "name": m.name,
                        "value": _get_metric_value(m),
                        "datatype": m.datatype,
                        "timestamp": m.timestamp if m.HasField("timestamp") else None,
                        "alias": m.alias if m.HasField("alias") else None,
                        "is_historical": m.is_historical,
                        "is_transient": m.is_transient,
                    }
                )
            return metrics
        except Exception as e:
            logger.error("Sparkplug B解码失败: %s", e)
            return None

    async def start(self, config: dict) -> None:
        app_config = get_config()
        sp_config = app_config.sparkplug_b

        self._group_id = config.get("group_id", sp_config.group_id)
        self._edge_node_id = config.get("edge_node_id", sp_config.edge_node_id)
        self._birth_debounce_ms = config.get("birth_debounce_ms", sp_config.birth_debounce_ms)

        self._running = True
        self._connect_task = asyncio.create_task(self._connect_loop(), name="sparkplug-b-connect")
        logger.info("Sparkplug B驱动启动: group=%s, node=%s", self._group_id, self._edge_node_id)

    async def stop(self) -> None:
        self._running = False

        for device_id in list(self._dbirth_published):
            await self._publish_ddeath(device_id)

        if self._connect_task and not self._connect_task.done():
            self._connect_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._connect_task

        if self._client:
            try:
                await self._client.__aexit__(None, None, None)
            except Exception as e:
                logger.debug("SparkPlug断开连接失败: %s", e)
            self._client = None

        self._nbirth_published = False
        self._dbirth_published.clear()
        logger.info("Sparkplug B驱动停止")

    async def add_device(
        self, device_id: str, config: dict, points: list[dict] | None = None
    ) -> None:
        if points is None:
            points = []
        self._device_metadata[device_id] = config
        self._device_points[device_id] = {p.get("name", p.get("address", "")): None for p in points}
        self._latest_values[device_id] = {}

        if self._client and self._nbirth_published:
            await self._publish_dbirth(device_id)

    async def remove_device(self, device_id: str) -> None:
        if device_id in self._dbirth_published:
            await self._publish_ddeath(device_id)
        self._device_metadata.pop(device_id, None)
        self._device_points.pop(device_id, None)
        self._latest_values.pop(device_id, None)

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        values = self._latest_values.get(device_id, {})
        return {p: values[p] for p in points if p in values}

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        if not self._client:
            return False

        topic = self._build_topic("DDATA", device_id)
        metrics = [{"name": point, "value": value}]
        payload = self._encode_payload(metrics, seq=self._next_seq())
        if payload is None:
            return False

        try:
            await self._client.publish(topic, payload)
            self._latest_values.setdefault(device_id, {})[point] = value
            self._device_points.setdefault(device_id, {})[point] = value
            return True
        except Exception as e:
            logger.error("Sparkplug B发布失败: %s - %s", device_id, e)
            return False

    def on_data(self, callback: Callable) -> None:
        self._data_callback = callback

    async def _connect_loop(self) -> None:
        app_config = get_config()
        sp_config = app_config.sparkplug_b

        broker = sp_config.mqtt_broker
        port = sp_config.mqtt_port
        username = sp_config.mqtt_username or None
        password = sp_config.mqtt_password or None

        retry_delay = 1.0
        max_delay = 30.0

        while self._running:
            try:
                import aiomqtt

                ndeath_topic = self._build_topic("NDEATH")
                ndeath_metrics = [{"name": "bdSeq", "value": 0}]
                ndeath_payload = self._encode_payload(ndeath_metrics, seq=0) or b""

                will = aiomqtt.Will(
                    topic=ndeath_topic,
                    payload=ndeath_payload,
                    qos=1,
                    retain=False,
                )

                if sp_config.tls_enabled:
                    import ssl

                    ctx = ssl.create_default_context()
                    if sp_config.tls_ca_cert:
                        ctx.load_verify_locations(sp_config.tls_ca_cert)
                    if sp_config.tls_client_cert and sp_config.tls_client_key:
                        ctx.load_cert_chain(sp_config.tls_client_cert, sp_config.tls_client_key)

                async with aiomqtt.Client(
                    hostname=broker,
                    port=port,
                    username=username,
                    password=password,
                    keepalive=60,
                    will=will,
                ) as client:
                    self._client = client
                    retry_delay = 1.0
                    logger.info("Sparkplug B MQTT连接成功: %s:%d", broker, port)

                    ncmd_topic = self._build_topic("NCMD")
                    dcmd_topic = self._build_topic("DCMD", device_id="+")
                    await client.subscribe(ncmd_topic, qos=1)
                    await client.subscribe(dcmd_topic, qos=1)

                    await self._publish_nbirth()

                    async for message in client.messages:
                        if not self._running:
                            break
                        await self._handle_message(message)

            except asyncio.CancelledError:
                raise
            except ImportError as e:
                logger.error("依赖库未安装: %s，%.1fs后重试", e, retry_delay)
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, max_delay)
            except Exception as e:
                logger.error("Sparkplug B MQTT连接异常: %s，%.1fs后重试", e, retry_delay)
                self._client = None
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, max_delay)
            finally:
                self._client = None

    async def _publish_nbirth(self) -> None:
        if not self._client:
            return

        metrics = [
            {"name": "bdSeq", "value": 0},
            {
                "name": "Node Control/NextSeq",
                "value": self._seq_num,
                "datatype": _SPB_DATATYPE_INT32,
            },
            {"name": "Node Control/Rebirth", "value": False},
        ]

        for device_id in self._device_metadata:
            metrics.append({"name": f"Device/{device_id}/online", "value": True})

        topic = self._build_topic("NBIRTH")
        payload = self._encode_payload(metrics, seq=self._next_seq())
        if payload:
            try:
                await self._client.publish(topic, payload, qos=1)
                self._nbirth_published = True
                logger.info("NBIRTH发布成功: %d个指标", len(metrics))
            except Exception as e:
                logger.error("NBIRTH发布失败: %s", e)

    async def _publish_dbirth(self, device_id: str) -> None:
        if not self._client:
            return

        points = self._device_points.get(device_id, {})
        metadata = self._device_metadata.get(device_id, {})
        metrics = []

        for point_name, value in points.items():
            m: dict[str, Any] = {"name": point_name, "value": value if value is not None else 0}
            point_meta = metadata.get("points", {}).get(point_name, {})
            if "datatype" in point_meta:
                m["datatype"] = point_meta["datatype"]
            if "alias" in point_meta:
                m["alias"] = point_meta["alias"]
            if "metadata" in point_meta:
                m["metadata"] = point_meta["metadata"]
            metrics.append(m)

        if not metrics:
            metrics.append({"name": "online", "value": True})

        topic = self._build_topic("DBIRTH", device_id)
        payload = self._encode_payload(metrics, seq=self._next_seq())
        if payload:
            try:
                await self._client.publish(topic, payload, qos=1)
                self._dbirth_published.add(device_id)
                logger.info("DBIRTH发布成功: %s, %d个测点", device_id, len(metrics))
            except Exception as e:
                logger.error("DBIRTH发布失败: %s - %s", device_id, e)

    async def _publish_ddeath(self, device_id: str) -> None:
        if not self._client:
            return

        topic = self._build_topic("DDEATH", device_id)
        payload = self._encode_payload([], seq=self._next_seq())
        if payload:
            try:
                await self._client.publish(topic, payload, qos=1)
                self._dbirth_published.discard(device_id)
                logger.info("DDEATH发布成功: %s", device_id)
            except Exception as e:
                logger.error("DDEATH发布失败: %s - %s", device_id, e)

    async def _publish_ndata(self, metrics: list[dict]) -> None:
        if not self._client or not self._nbirth_published:
            return

        topic = self._build_topic("NDATA")
        payload = self._encode_payload(metrics, seq=self._next_seq())
        if payload:
            try:
                await self._client.publish(topic, payload)
            except Exception as e:
                logger.error("NDATA发布失败: %s", e)

    async def _publish_ddata(self, device_id: str, metrics: list[dict]) -> None:
        if not self._client:
            return

        if device_id not in self._dbirth_published:
            await self._publish_dbirth(device_id)
            if device_id not in self._dbirth_published:
                return

        topic = self._build_topic("DDATA", device_id)
        payload = self._encode_payload(metrics, seq=self._next_seq())
        if payload:
            try:
                await self._client.publish(topic, payload)
            except Exception as e:
                logger.error("DDATA发布失败: %s - %s", device_id, e)

    async def _handle_message(self, message: Any) -> None:
        try:
            topic_str = str(message.topic)
            payload_data = message.payload

            metrics = self._decode_payload(payload_data)
            if metrics is None:
                logger.warning("无法解码Sparkplug B消息: %s", topic_str)
                return

            parts = topic_str.split("/")
            if len(parts) < 4 or parts[0] != "spBv1.0":
                return

            msg_type = parts[3]
            device_id = parts[4] if len(parts) > 4 else None

            if msg_type == "NCMD":
                await self._handle_ncmd(metrics)
            elif msg_type == "DCMD" and device_id:
                await self._handle_dcmd(device_id, metrics)
            else:
                logger.debug("忽略Sparkplug B消息类型: %s", msg_type)

        except Exception as e:
            logger.error("Sparkplug B消息处理失败: %s", e)

    async def _handle_ncmd(self, metrics: list[dict]) -> None:
        for m in metrics:
            name = m.get("name", "")
            if name == "Node Control/Rebirth" and m.get("value") is True:
                logger.info("收到Rebirth命令，重新发布所有Birth消息")
                await self._publish_nbirth()
                for device_id in list(self._device_metadata.keys()):
                    await self._publish_dbirth(device_id)

    async def _handle_dcmd(self, device_id: str, metrics: list[dict]) -> None:
        for m in metrics:
            point = m.get("name", "")
            value = m.get("value")
            if point and value is not None:
                success = await self.write_point(device_id, point, value)
                if success:
                    logger.info("DCMD写入成功: %s/%s=%s", device_id, point, value)
                    if self._data_callback:
                        try:
                            await self._data_callback(device_id, {point: value})
                        except Exception as e:
                            logger.error("数据回调执行失败: %s", e)
                else:
                    logger.warning("DCMD写入失败: %s/%s", device_id, point)

    async def handle_point_update(self, event: Any) -> None:
        if not self._running:
            return

        device_id = getattr(event, "device_id", "")
        point_name = getattr(event, "point_name", "")
        value = getattr(event, "value", None)

        if not device_id or not point_name:
            return

        self._latest_values.setdefault(device_id, {})[point_name] = value
        self._device_points.setdefault(device_id, {})[point_name] = value

        metrics = [{"name": point_name, "value": value}]
        await self._publish_ddata(device_id, metrics)

    async def discover_devices(self, config: dict) -> list[dict]:
        return []
