"""MQTT Sparkplug B协议驱动 - 基于aiomqtt + sparkplugb ProtoBuf实现"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any

from edgelite.config import get_config
from edgelite.constants import _MQTT_KEEPALIVE, _SPARKPLUG_RECONNECT_MAX_DELAY
from edgelite.drivers.base import DriverPlugin
from edgelite.utils import timestamp_ms

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
    plugin_version = "1.1.0"
    supported_protocols = ["sparkplug_b"]
    config_schema = {
        "description": "MQTT Sparkplug B industrial IoT protocol, standardized device data pub/sub",
        "fields": [
            {"name": "group_id", "type": "string", "label": "Group ID",
             "description": "Sparkplug B logical group ID", "default": "group1", "required": True},
            {"name": "edge_node_id", "type": "string", "label": "Edge Node ID",
             "description": "Gateway node ID in Sparkplug B", "default": "edgelite_node", "required": True},
            {"name": "mqtt_broker", "type": "string", "label": "Broker Address",
             "description": "MQTT Broker address", "default": "localhost", "required": True},
            {"name": "mqtt_port", "type": "integer", "label": "Port",
             "description": "MQTT Broker port", "default": 1883},
            {"name": "enable_cmd_response", "type": "boolean", "label": "Enable Command Response",
             "description": "Send command response messages (DCMD/RESP)", "default": True},
            {"name": "batch_interval_ms", "type": "integer", "label": "Batch Interval (ms)",
             "description": "Batch publish interval for DDATA messages", "default": 100},
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
        self._bd_seq: int = 0  # FIXED: P2-2 bdSeq硬编码为0，改为持久化计数器
        self._bd_seq_file: str = ""  # FIXED: P2-2 bdSeq持久化文件路径
        self._birth_debounce_ms: int = 1000

        self._device_points: dict[str, dict[str, Any]] = {}
        self._device_metadata: dict[str, dict] = {}
        self._latest_values: dict[str, dict[str, Any]] = {}
        self._values_lock = asyncio.Lock()

        self._nbirth_published: bool = False
        self._dbirth_published: set[str] = set()
        # 新增: 命令响应和批量优化
        self._enable_cmd_response: bool = True
        self._batch_interval_ms: int = 100
        self._pending_metrics: dict[str, list[dict]] = {}  # device_id -> metrics
        self._batch_task: asyncio.Task | None = None

    def _load_bd_seq(self) -> None:
        """FIXED: P2-2 从持久化文件加载bdSeq"""
        import json
        from pathlib import Path

        if not self._bd_seq_file:
            return
        try:
            p = Path(self._bd_seq_file)
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                self._bd_seq = int(data.get("bd_seq", 0)) & 0xFF
        except Exception:
            pass

    def _save_bd_seq(self) -> None:
        """FIXED: P2-2 将bdSeq持久化到文件"""
        import json
        from pathlib import Path

        if not self._bd_seq_file:
            return
        try:
            p = Path(self._bd_seq_file)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"bd_seq": self._bd_seq}, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def _next_seq(self) -> int:
        seq = self._seq_num
        self._seq_num = (self._seq_num + 1) % 256
        return seq

    def _next_bd_seq(self) -> int:
        """FIXED: P2-2 bdSeq持久化递增，非每次重启从0开始"""
        bd = self._bd_seq
        self._bd_seq = (self._bd_seq + 1) % 256
        self._save_bd_seq()
        return bd

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
            payload.timestamp = timestamp_ms()  # FIXED: 原问题-直接调用int(time.time()*1000)，未使用统一工具函数
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
                    metric.alias = int(m.get("alias"))  # FIXED: 原问题-m["alias"]硬访问
                if m.get("timestamp") is not None:
                    metric.timestamp = int(m.get("timestamp"))  # FIXED: 原问题-m["timestamp"]硬访问
                if m.get("is_historical"):
                    metric.is_historical = True
                if m.get("is_transient"):
                    metric.is_transient = True
                if m.get("metadata"):
                    meta = metric.metadata
                    for k, v in m.get("metadata", {}).items():  # FIXED: 原问题-m["metadata"]硬访问
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
        sp_config = getattr(app_config, "sparkplug_b", None)  # FIXED: 原问题-直接访问sparkplug_b属性，配置缺失时AttributeError
        if sp_config is None:
            raise ValueError("SparkplugB configuration section not found in config")

        self._group_id = config.get("group_id", sp_config.group_id)
        self._edge_node_id = config.get("edge_node_id", sp_config.edge_node_id)
        self._birth_debounce_ms = config.get("birth_debounce_ms", sp_config.birth_debounce_ms)

        # FIXED: P2-2 加载持久化的bdSeq，重启后递增而非从0开始
        from pathlib import Path

        self._bd_seq_file = str(Path("data/sparkplug_b") / f"bd_seq_{self._edge_node_id}.json")
        self._load_bd_seq()

        self._running = True
        self._connect_task = asyncio.create_task(self._connect_loop(), name="sparkplug-b-connect")
        logger.info("Sparkplug B驱动启动: group=%s, node=%s", self._group_id, self._edge_node_id)

    async def stop(self) -> None:
        self._running = False
        self._save_bd_seq()  # FIXED: P2-2 停止前持久化bdSeq

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
        async with self._values_lock:
            values = self._latest_values.get(device_id, {}).copy()
        return {p: values.get(p) for p in points if values.get(p) is not None}  # FIXED: 原问题-values[p]硬访问可能KeyError，改为values.get(p)缺失时跳过

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
            async with self._values_lock:
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
        max_delay = _SPARKPLUG_RECONNECT_MAX_DELAY  # FIXED: 原问题-魔法数字，提取为命名常量

        while self._running:
            try:
                import aiomqtt

                ndeath_topic = self._build_topic("NDEATH")
                # FIXED: P2-2 bdSeq应为最新值而非硬编码0，SCADA据此判断驱动重启
                ndeath_metrics = [{"name": "bdSeq", "value": self._bd_seq}]
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
                    keepalive=_MQTT_KEEPALIVE,
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
            {"name": "bdSeq", "value": self._next_bd_seq()},  # FIXED: P2-2 bdSeq持久化递增
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
            # FIXED: 原问题-metadata.get("points")可能返回None，链式.get()会AttributeError
            points_meta = metadata.get("points") or {}
            point_meta = points_meta.get(point_name, {})
            if "datatype" in point_meta:
                _v = point_meta["datatype"]
                if _v is not None: m["datatype"] = _v  # FIXED: 原问题-键存在但值可能为None
            if "alias" in point_meta:
                _v = point_meta["alias"]
                if _v is not None: m["alias"] = _v  # FIXED: 原问题-键存在但值可能为None
            if "metadata" in point_meta:
                _v = point_meta["metadata"]
                if _v is not None: m["metadata"] = _v  # FIXED: 原问题-键存在但值可能为None
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
                # 执行写入操作
                success = await self.write_point(device_id, point, value)

                # 发送命令响应
                if self._enable_cmd_response:
                    await self._send_cmd_response(device_id, point, value, success)

                if success:
                    logger.info("DCMD写入成功: %s/%s=%s", device_id, point, value)
                    if self._data_callback:
                        try:
                            await self._data_callback(device_id, {point: value})
                        except Exception as e:
                            logger.error("数据回调执行失败: %s", e)
                else:
                    logger.warning("DCMD写入失败: %s/%s", device_id, point)

    async def _send_cmd_response(self, device_id: str, point: str, value: Any, success: bool) -> None:
        """发送命令响应消息"""
        if not self._client:
            return

        topic = self._build_topic("RESP", device_id)
        metrics = [{
            "name": f"{point}_response",
            "value": "Success" if success else "Failure",
        }, {
            "name": f"{point}_value",
            "value": value,
        }]
        payload = self._encode_payload(metrics, seq=self._next_seq())
        if payload:
            try:
                await self._client.publish(topic, payload, qos=1)
            except Exception as e:
                logger.error("命令响应发布失败: %s", e)

    async def handle_point_update(self, event: Any) -> None:
        if not self._running:
            return

        device_id = getattr(event, "device_id", "")
        point_name = getattr(event, "point_name", "")
        value = getattr(event, "value", None)

        if not device_id or not point_name:
            return

        async with self._values_lock:
            self._latest_values.setdefault(device_id, {})[point_name] = value
        self._device_points.setdefault(device_id, {})[point_name] = value

        metrics = [{"name": point_name, "value": value}]
        # 批量模式: 缓存指标，定期发布
        self._pending_metrics.setdefault(device_id, []).append({
            "name": point_name,
            "value": value,
        })
        # 启动批量发布协程
        if self._batch_task is None or self._batch_task.done():
            self._batch_task = asyncio.create_task(self._batch_publish_loop())

    async def _batch_publish_loop(self) -> None:
        """批量发布循环，定期将缓存的指标发布"""
        while self._running and self._pending_metrics:
            try:
                await asyncio.sleep(self._batch_interval_ms / 1000.0)

                async with self._values_lock:
                    metrics_to_publish = dict(self._pending_metrics)
                    self._pending_metrics.clear()

                for device_id, metrics in metrics_to_publish.items():
                    if metrics:
                        await self._publish_ddata(device_id, metrics)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("批量发布异常: %s", e)

    async def discover_devices(self, config: dict) -> list[dict]:
        """通过MQTT订阅Sparkplug B主题发现在线设备

        订阅 spBv1.0/+/DBIRTH/+ 主题，等待设备上线广播，
        解析DBIRTH消息中的设备信息和测点列表。

        config参数:
            mqtt_broker: MQTT Broker地址 (默认localhost)
            mqtt_port: MQTT Broker端口 (默认1883)
            mqtt_username: MQTT用户名 (可选)
            mqtt_password: MQTT密码 (可选)
            group_id: 要发现的组ID (可选，不指定则发现所有组)
            timeout: 等待发现的超时秒数 (默认10)
        """
        try:
            import aiomqtt
        except ImportError:
            logger.warning("aiomqtt未安装，无法执行Sparkplug B设备发现")
            return []

        if _pb2 is None:
            logger.warning("sparkplugb库未安装，无法解码Sparkplug B消息")
            return []

        broker = config.get("mqtt_broker", "localhost")
        port = int(config.get("mqtt_port", 1883))
        username = config.get("mqtt_username") or None
        password = config.get("mqtt_password") or None
        group_id = config.get("group_id", "")
        timeout = float(config.get("timeout", 10.0))

        discovered = []
        discovered_ids: set[str] = set()

        # 构建订阅主题: spBv1.0/{group_id}/DBIRTH/+
        if group_id:
            birth_topic = f"spBv1.0/{group_id}/DBIRTH/+"
        else:
            birth_topic = "spBv1.0/+/DBIRTH/+"

        try:
            async with aiomqtt.Client(
                hostname=broker,
                port=port,
                username=username,
                password=password,
            ) as client:
                await client.subscribe(birth_topic, qos=1)

                deadline = asyncio.get_running_loop().time() + timeout
                while True:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    try:
                        message = await asyncio.wait_for(
                            client.messages.__anext__(),
                            timeout=remaining,
                        )
                    except (StopAsyncIteration, TimeoutError):
                        break

                    try:
                        topic_str = str(message.topic)
                        payload_data = message.payload

                        # 解析主题: spBv1.0/{group}/DBIRTH/{edge_node}/{device_id}
                        parts = topic_str.split("/")
                        if len(parts) < 5 or parts[0] != "spBv1.0":
                            continue

                        sp_group = parts[1]
                        msg_type = parts[2]
                        edge_node = parts[3] if len(parts) > 3 else ""
                        device = parts[4] if len(parts) > 4 else ""

                        if msg_type != "DBIRTH":
                            continue

                        # 解析Payload获取设备测点信息
                        metrics = self._decode_payload(payload_data)
                        points = []
                        if metrics:
                            for m in metrics:
                                point_name = m.get("name", "")
                                if point_name:
                                    points.append({
                                        "name": point_name,
                                        "datatype": m.get("datatype"),
                                    })

                        # 如果主题中没有device部分，使用edge_node作为device
                        device_name = device or edge_node
                        device_id = f"spb_{sp_group}_{edge_node}_{device_name}"

                        if device_id not in discovered_ids:
                            discovered_ids.add(device_id)
                            discovered.append({
                                "device_id": device_id,
                                "name": f"Sparkplug B Device ({sp_group}/{edge_node}/{device_name})",
                                "protocol": "sparkplug_b",
                                "config": {
                                    "group_id": sp_group,
                                    "edge_node_id": edge_node,
                                    "device_id": device_name,
                                },
                                "points": points,
                            })

                    except Exception as e:
                        logger.debug("Sparkplug B发现: 解析消息失败 - %s", e)
                        continue

        except Exception as e:
            logger.error("Sparkplug B设备发现失败: %s", e)

        logger.info("Sparkplug B设备发现完成: 发现%d台设备", len(discovered))
        return discovered
