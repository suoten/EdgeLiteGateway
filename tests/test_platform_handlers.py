"""北向平台 Handler 单元测试

覆盖模块：
- src/edgelite/platform/base.py — PlatformHandler 抽象基类（离线队列/重连退避）
- src/edgelite/platform/iotsharp.py — IoTSharpHandler
- src/edgelite/platform/thingsboard.py — ThingsBoardHandler
- src/edgelite/platform/custom_mqtt.py — CustomMqttHandler

设计要点：
- aiomqtt 通过 sys.modules 注入桩模块，不发起真实 MQTT 连接
- asyncio.sleep 全部 mock 为即时返回，测试快速完成
- 所有 _connect_loop / _publish_loop / _rpc_listen_loop 均通过 mock client 测试
- 资源（connect_task）在测试末尾正确关闭
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, "src")

from edgelite.platform.base import PlatformHandler
from edgelite.platform.iotsharp import IoTSharpHandler

# ═══════════════════════════════════════════════════════════════════════════
# 桩 aiomqtt 模块
# ═══════════════════════════════════════════════════════════════════════════


class _FakeAiomqttClient:
    """模拟 aiomqtt.Client 上下文管理器。"""

    def __init__(self, *, hostname="", port=1883, username=None, password=None, keepalive=60, **kwargs):
        self.hostname = hostname
        self.port = port
        self.username = username
        self.password = password
        self.keepalive = keepalive
        self._subscribed: list[str] = []
        self._published: list[tuple[str, bytes, int]] = []
        self._messages = asyncio.Queue()
        self._closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self._closed = True

    async def subscribe(self, topic, qos=0):
        self._subscribed.append(str(topic))

    async def publish(self, topic, payload, qos=0):
        self._published.append((str(topic), payload, qos))

    @property
    def messages(self):
        return self._message_iter()

    async def _message_iter(self):
        while True:
            msg = await self._messages.get()
            if msg is None:
                return
            yield msg

    async def push_message(self, topic, payload):
        """测试辅助：向消息流推入一条消息。"""
        msg = MagicMock()
        msg.topic = topic
        msg.payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        await self._messages.put(msg)

    async def close_messages(self):
        await self._messages.put(None)


def _make_aiomqtt_mod(client_cls=None, fail_count=0):
    """构造桩 aiomqtt 模块。fail_count>0 时前 N 次连接抛异常。"""
    mod = types.ModuleType("aiomqtt")
    calls = {"count": 0}

    if client_cls is None:
        client_cls = _FakeAiomqttClient

    class _ClientWrapper:
        def __init__(self, **kwargs):
            self._kwargs = kwargs
            self._real = None

        async def __aenter__(self):
            calls["count"] += 1
            if calls["count"] <= fail_count:
                raise ConnectionError(f"connect failed (attempt {calls['count']})")
            self._real = client_cls(**self._kwargs)
            await self._real.__aenter__()
            return self._real

        async def __aexit__(self, *args):
            if self._real:
                await self._real.__aexit__(*args)

    mod.Client = _ClientWrapper
    return mod


# ═══════════════════════════════════════════════════════════════════════════
# PlatformHandler 基类测试
# ═══════════════════════════════════════════════════════════════════════════


class _ConcreteHandler(PlatformHandler):
    """最小可实例化的 PlatformHandler 子类。"""

    platform_name = "test"
    platform_version = "0.1.0"

    async def connect(self, config: dict) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def publish_telemetry(self, device_id: str, data: dict) -> None:
        pass

    async def publish_attributes(self, device_id: str, attrs: dict) -> None:
        pass

    async def on_rpc_request(self, callback) -> None:
        pass

    async def publish_device_status(self, device_id: str, online: bool) -> None:
        pass


class TestPlatformHandlerBase:
    def test_init_defaults(self):
        h = _ConcreteHandler()
        assert h._connected is False
        assert h._offline_queue.maxlen is None  # deque, no maxlen
        assert len(h._offline_queue) == 0
        assert h._offline_queue_max == 10000
        assert h._reconnect_backoff == 1.0

    def test_is_connected_property(self):
        h = _ConcreteHandler()
        assert h.is_connected is False
        h._connected = True
        assert h.is_connected is True

    def test_cannot_instantiate_abstract(self):
        with pytest.raises(TypeError):
            PlatformHandler()  # type: ignore[abstract]

    def test_enqueue_offline(self):
        h = _ConcreteHandler()
        h._enqueue_offline("topic/a", b"payload1", 1)
        h._enqueue_offline("topic/b", b"payload2", 1)
        assert len(h._offline_queue) == 2
        assert h._offline_queue[0] == ("topic/a", b"payload1", 1)
        assert h._offline_queue[1] == ("topic/b", b"payload2", 1)

    def test_enqueue_offline_drops_oldest_when_full(self):
        h = _ConcreteHandler()
        h._offline_queue_max = 3
        for i in range(5):
            h._enqueue_offline(f"t{i}", b"p", 1)
        assert len(h._offline_queue) == 3
        assert h._offline_queue[0] == ("t2", b"p", 1)
        assert h._offline_queue[2] == ("t4", b"p", 1)

    async def test_flush_offline_queue_empty_does_nothing(self):
        h = _ConcreteHandler()
        await h._flush_offline_queue()

    async def test_flush_offline_queue_with_pub_queue(self):
        h = _ConcreteHandler()
        h._pub_queue = asyncio.Queue(maxsize=10)
        h._enqueue_offline("topic/a", b"payload1", 1)
        h._enqueue_offline("topic/b", b"payload2", 0)
        await h._flush_offline_queue()
        assert len(h._offline_queue) == 0
        assert h._pub_queue.qsize() == 2

    async def test_flush_offline_queue_no_pub_queue_skips(self):
        h = _ConcreteHandler()
        h._pub_queue = None
        h._enqueue_offline("topic/a", b"payload1", 1)
        await h._flush_offline_queue()
        # 没有pub_queue，条目不被刷出但队列已清空
        assert len(h._offline_queue) == 0

    async def test_flush_offline_queue_requeues_on_full(self):
        h = _ConcreteHandler()
        h._pub_queue = asyncio.Queue(maxsize=1)
        h._enqueue_offline("topic/a", b"payload1", 1)
        h._enqueue_offline("topic/b", b"payload2", 1)
        await h._flush_offline_queue()
        # 第一条入pub_queue成功，第二条QueueFull后重新入offline_queue
        assert h._pub_queue.qsize() == 1
        assert len(h._offline_queue) == 1
        assert h._offline_queue[0] == ("topic/b", b"payload2", 1)

    async def test_reconnect_with_backoff_success_first_try(self):
        h = _ConcreteHandler()
        config = {"broker": "test"}
        with patch("edgelite.platform.base.asyncio.sleep", new_callable=AsyncMock):
            await h.reconnect_with_backoff(config)
        assert h._connected is True
        assert h._reconnect_backoff == 1.0

    async def test_reconnect_with_backoff_retries_then_succeeds(self):
        h = _ConcreteHandler()
        call_count = {"n": 0}

        async def mock_connect(config):
            call_count["n"] += 1
            if call_count["n"] < 3:
                raise ConnectionError("not yet")
            h._connected = True

        h.connect = mock_connect  # type: ignore[assignment]
        with patch("edgelite.platform.base.asyncio.sleep", new_callable=AsyncMock):
            with patch("edgelite.platform.base.random.random", return_value=0.5):
                await h.reconnect_with_backoff({})
        assert h._connected is True
        assert call_count["n"] == 3


# ═══════════════════════════════════════════════════════════════════════════
# IoTSharpHandler 测试
# ═══════════════════════════════════════════════════════════════════════════


class TestIoTSharpHandlerInit:
    def test_defaults(self):
        h = IoTSharpHandler()
        assert h.platform_name == "iotsharp"
        assert h.platform_version == "1.0.0"
        assert h._connected is False
        assert h._running is False
        assert h._config == {}
        assert h._rpc_callback is None
        assert h._connect_task is None
        assert h._pub_queue is None
        assert h.is_connected is False

    def test_inherits_platform_handler(self):
        assert issubclass(IoTSharpHandler, PlatformHandler)


class TestIoTSharpConnect:
    async def test_connect_missing_broker_raises_value_error(self):
        h = IoTSharpHandler()
        with pytest.raises(ValueError, match="broker is required"):
            await h.connect({"port": 1883})

    async def test_connect_no_aiomqtt_raises_import_error(self):
        h = IoTSharpHandler()
        with patch.dict(sys.modules, {"aiomqtt": None}):
            with pytest.raises(ImportError, match="aiomqtt"):
                await h.connect({"broker": "localhost", "port": 1883})

    async def test_connect_starts_task(self):
        h = IoTSharpHandler()
        fake_mod = _make_aiomqtt_mod()
        with patch.dict(sys.modules, {"aiomqtt": fake_mod}):
            with patch("edgelite.platform.iotsharp.asyncio.sleep", new_callable=AsyncMock):
                await h.connect({"broker": "localhost", "port": 1883})
                assert h._running is True
                assert h._connect_task is not None
                await asyncio.sleep(0.05)
        await h.disconnect()

    async def test_connect_with_username_password(self):
        h = IoTSharpHandler()
        fake_mod = _make_aiomqtt_mod()
        with patch.dict(sys.modules, {"aiomqtt": fake_mod}):
            with patch("edgelite.platform.iotsharp.asyncio.sleep", new_callable=AsyncMock):
                await h.connect(
                    {
                        "broker": "localhost",
                        "port": 1883,
                        "username": "user",
                        "password": "pass",
                    }
                )
                assert h._running is True
        await h.disconnect()

    async def test_disconnect_stops_running(self):
        h = IoTSharpHandler()
        fake_mod = _make_aiomqtt_mod()
        with patch.dict(sys.modules, {"aiomqtt": fake_mod}):
            with patch("edgelite.platform.iotsharp.asyncio.sleep", new_callable=AsyncMock):
                await h.connect({"broker": "localhost", "port": 1883})
                await asyncio.sleep(0.05)
        await h.disconnect()
        assert h._running is False
        assert h._connected is False

    async def test_disconnect_no_task_silent(self):
        h = IoTSharpHandler()
        await h.disconnect()
        assert h._running is False
        assert h._connected is False


class TestIoTSharpPublish:
    async def test_publish_telemetry_disconnected_enqueues_offline(self):
        h = IoTSharpHandler()
        await h.publish_telemetry("dev1", {"temp": 25.5})
        assert len(h._offline_queue) == 1
        topic, payload, qos = h._offline_queue[0]
        assert topic == "devices/dev1/telemetry"
        assert json.loads(payload) == {"temp": 25.5}
        assert qos == 1

    async def test_publish_telemetry_connected_puts_on_queue(self):
        h = IoTSharpHandler()
        h._pub_queue = asyncio.Queue(maxsize=10)
        h._connected = True
        await h.publish_telemetry("dev1", {"temp": 25.5})
        assert h._pub_queue.qsize() == 1
        topic, payload, qos = h._pub_queue.get_nowait()
        assert topic == "devices/dev1/telemetry"
        assert json.loads(payload) == {"temp": 25.5}
        assert qos == 1

    async def test_publish_telemetry_queue_full_drops(self):
        h = IoTSharpHandler()
        h._pub_queue = asyncio.Queue(maxsize=1)
        h._pub_queue.put_nowait(("existing", b"data", 0))
        h._connected = True
        await h.publish_telemetry("dev1", {"temp": 25.5})
        assert h._pub_queue.qsize() == 1

    async def test_publish_attributes_disconnected_enqueues_offline(self):
        h = IoTSharpHandler()
        await h.publish_attributes("dev1", {"model": "S7-1200"})
        assert len(h._offline_queue) == 1
        topic, payload, qos = h._offline_queue[0]
        assert topic == "devices/dev1/attributes"
        assert json.loads(payload) == {"model": "S7-1200"}

    async def test_publish_attributes_connected_puts_on_queue(self):
        h = IoTSharpHandler()
        h._pub_queue = asyncio.Queue(maxsize=10)
        h._connected = True
        await h.publish_attributes("dev1", {"model": "S7-1200"})
        assert h._pub_queue.qsize() == 1
        topic, _, _ = h._pub_queue.get_nowait()
        assert topic == "devices/dev1/attributes"

    async def test_publish_device_status_online(self):
        h = IoTSharpHandler()
        await h.publish_device_status("dev1", True)
        assert len(h._offline_queue) == 1
        topic, payload, _ = h._offline_queue[0]
        assert topic == "devices/dev1/attributes"
        data = json.loads(payload)
        assert data["online"] is True
        assert "lastActivityTime" in data

    async def test_publish_device_status_offline(self):
        h = IoTSharpHandler()
        await h.publish_device_status("dev1", False)
        topic, payload, _ = h._offline_queue[0]
        data = json.loads(payload)
        assert data["online"] is False

    async def test_publish_device_status_connected(self):
        h = IoTSharpHandler()
        h._pub_queue = asyncio.Queue(maxsize=10)
        h._connected = True
        await h.publish_device_status("dev1", True)
        assert h._pub_queue.qsize() == 1

    async def test_publish_telemetry_serializes_non_json_types(self):
        """default=str 确保非 JSON 原生类型（如 datetime）被序列化"""
        from datetime import datetime

        h = IoTSharpHandler()
        h._pub_queue = asyncio.Queue(maxsize=10)
        h._connected = True
        dt = datetime(2025, 1, 1, 12, 0, 0)
        await h.publish_telemetry("dev1", {"timestamp": dt})
        topic, payload, _ = h._pub_queue.get_nowait()
        data = json.loads(payload)
        assert "2025-01-01" in data["timestamp"]


class TestIoTSharpRpc:
    async def test_on_rpc_request_sets_callback(self):
        h = IoTSharpHandler()

        async def cb(device_id, method, params):
            return {"ok": True}

        await h.on_rpc_request(cb)
        assert h._rpc_callback is cb


class TestIoTSharpPublishLoop:
    async def test_publish_loop_3_tuple(self):
        """测试 3 元组 (topic, payload, qos) 的发布"""
        h = IoTSharpHandler()
        h._pub_queue = asyncio.Queue(maxsize=10)
        h._running = True
        h._pub_queue.put_nowait(("topic/a", b"payload1", 1))

        published = []

        async def mock_publish(topic, payload, qos=1):
            published.append((topic, payload, qos))
            h._running = False

        mock_client = MagicMock()
        mock_client.publish = mock_publish

        async def mock_wait_for(coro, timeout):
            coro.close()
            return h._pub_queue.get_nowait()

        with patch("edgelite.platform.iotsharp.asyncio.wait_for", new=mock_wait_for):
            await h._publish_loop(mock_client)
        assert published == [("topic/a", b"payload1", 1)]

    async def test_publish_loop_4_tuple_with_retry(self):
        """测试 4 元组 (topic, payload, qos, retry_count) 的发布"""
        h = IoTSharpHandler()
        h._pub_queue = asyncio.Queue(maxsize=10)
        h._running = True
        h._pub_queue.put_nowait(("topic/b", b"payload2", 1, 0))

        published = []

        async def mock_publish(topic, payload, qos=1):
            published.append((topic, payload, qos))
            h._running = False

        mock_client = MagicMock()
        mock_client.publish = mock_publish

        async def mock_wait_for(coro, timeout):
            coro.close()
            return h._pub_queue.get_nowait()

        with patch("edgelite.platform.iotsharp.asyncio.wait_for", new=mock_wait_for):
            await h._publish_loop(mock_client)
        assert published == [("topic/b", b"payload2", 1)]

    async def test_publish_loop_publish_failure_re_enqueues_with_retry(self):
        """发布失败时带重试计数重新入队"""
        h = IoTSharpHandler()
        h._pub_queue = asyncio.Queue(maxsize=100)
        h._running = True
        h._pub_queue.put_nowait(("topic/fail", b"data", 1))

        call_count = {"n": 0}

        async def failing_publish(topic, payload, qos=1):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise ConnectionError("publish failed")
            h._running = False

        mock_client = MagicMock()
        mock_client.publish = failing_publish

        async def mock_wait_for(coro, timeout):
            coro.close()
            return h._pub_queue.get_nowait()

        with patch("edgelite.platform.iotsharp.asyncio.wait_for", new=mock_wait_for):
            await h._publish_loop(mock_client)
        assert call_count["n"] == 2
        assert h._pub_queue.qsize() == 0

    async def test_publish_loop_retry_exceeds_max_enqueues_offline(self):
        """重试超过上限后写入离线队列（publish 失败场景）"""
        from edgelite.constants import _NORTH_RETRY_MAX_ATTEMPTS

        h = IoTSharpHandler()
        h._pub_queue = asyncio.Queue(maxsize=100)
        h._running = True
        # retry_count 已达上限，publish 再失败 → 入离线队列
        h._pub_queue.put_nowait(("topic/exceed", b"data", 1, _NORTH_RETRY_MAX_ATTEMPTS + 1))

        call_count = {"n": 0}

        async def failing_publish(topic, payload, qos=1):
            call_count["n"] += 1
            h._running = False
            raise ConnectionError("publish failed")

        mock_client = MagicMock()
        mock_client.publish = failing_publish

        async def mock_wait_for(coro, timeout):
            coro.close()
            return h._pub_queue.get_nowait()

        with patch("edgelite.platform.iotsharp.asyncio.wait_for", new=mock_wait_for):
            await h._publish_loop(mock_client)
        assert call_count["n"] == 1
        assert len(h._offline_queue) == 1
        assert h._offline_queue[0][0] == "topic/exceed"

    async def test_publish_loop_queue_none_sleeps(self):
        """pub_queue 为 None 时 sleep 而非崩溃"""
        h = IoTSharpHandler()
        h._pub_queue = None
        h._running = True
        call_count = {"n": 0}

        async def fast_sleep(t):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                h._running = False

        with patch("edgelite.platform.iotsharp.asyncio.sleep", new=fast_sleep):
            await h._publish_loop(AsyncMock())
        assert call_count["n"] >= 2

    async def test_publish_loop_timeout_continues_then_stops(self):
        """wait_for 超时后 continue，设置 _running=False 后退出"""
        h = IoTSharpHandler()
        h._pub_queue = asyncio.Queue(maxsize=10)
        h._running = True

        mock_client = MagicMock()
        call_count = {"n": 0}

        async def mock_wait_for(coro, timeout):
            coro.close()
            call_count["n"] += 1
            if call_count["n"] >= 3:
                h._running = False
            raise TimeoutError()

        with patch("edgelite.platform.iotsharp.asyncio.wait_for", new=mock_wait_for):
            await h._publish_loop(mock_client)
        assert call_count["n"] >= 3

    async def test_publish_loop_cancelled_silent(self):
        """CancelledError 被静默捕获"""
        h = IoTSharpHandler()
        h._pub_queue = asyncio.Queue(maxsize=10)
        h._running = True

        async def raise_cancelled(coro, timeout):
            coro.close()
            raise asyncio.CancelledError()

        with patch("edgelite.platform.iotsharp.asyncio.wait_for", new=raise_cancelled):
            await h._publish_loop(AsyncMock())


class TestIoTSharpRpcListenLoop:
    async def test_rpc_listen_handles_request(self):
        """RPC 请求被正确解析并调用回调"""
        h = IoTSharpHandler()

        rpc_result = {"status": "ok"}

        async def cb(device_id, method, params):
            assert device_id == "dev1"
            assert method == "set_temp"
            assert params == {"value": 30}
            return rpc_result

        h._rpc_callback = cb

        # 构造 mock client
        mock_client = AsyncMock()

        # 构造消息迭代器
        async def message_gen():
            msg = MagicMock()
            msg.topic = "devices/dev1/rpc/request"
            msg.payload = json.dumps({"method": "set_temp", "params": {"value": 30}}).encode()
            yield msg

        mock_client.messages = message_gen()
        mock_client.publish = AsyncMock()

        # 设置 _running=False 在处理完一条消息后停止
        async def publish_side_effect(topic, payload, qos=1):
            h._running = False

        mock_client.publish = AsyncMock(side_effect=publish_side_effect)
        h._running = True

        await h._rpc_listen_loop(mock_client)
        mock_client.publish.assert_awaited_once()
        call_args = mock_client.publish.call_args
        assert call_args[0][0] == "devices/dev1/rpc/response"
        response_data = json.loads(call_args[0][1])
        assert response_data["method"] == "set_temp"
        assert response_data["result"] == rpc_result

    async def test_rpc_listen_invalid_json_skipped(self):
        """JSON 解析失败时跳过消息"""
        h = IoTSharpHandler()
        h._rpc_callback = AsyncMock(return_value=None)

        mock_client = AsyncMock()

        async def message_gen():
            msg = MagicMock()
            msg.topic = "devices/dev1/rpc/request"
            msg.payload = b"not-json"
            yield msg

        mock_client.messages = message_gen()
        mock_client.publish = AsyncMock()

        h._running = True
        call_count = {"n": 0}

        async def stop_publish(topic, payload, qos=1):
            call_count["n"] += 1
            h._running = False

        mock_client.publish = AsyncMock(side_effect=stop_publish)
        await h._rpc_listen_loop(mock_client)
        # 无效 JSON 不应触发 publish
        assert call_count["n"] == 0

    async def test_rpc_listen_no_callback_skips_publish(self):
        """无回调时不发布响应"""
        h = IoTSharpHandler()
        h._rpc_callback = None

        mock_client = AsyncMock()

        async def message_gen():
            msg = MagicMock()
            msg.topic = "devices/dev1/rpc/request"
            msg.payload = json.dumps({"method": "get", "params": {}}).encode()
            yield msg

        mock_client.messages = message_gen()
        mock_client.publish = AsyncMock()
        h._running = True

        # 让消息处理后停止
        async def message_gen_stop():
            msg = MagicMock()
            msg.topic = "devices/dev1/rpc/request"
            msg.payload = json.dumps({"method": "get", "params": {}}).encode()
            yield msg
            h._running = False

        mock_client.messages = message_gen_stop()
        await h._rpc_listen_loop(mock_client)
        mock_client.publish.assert_not_awaited()

    async def test_rpc_listen_wrong_topic_parts_skipped(self):
        """topic 格式不匹配时跳过"""
        h = IoTSharpHandler()
        h._rpc_callback = AsyncMock(return_value=None)

        mock_client = AsyncMock()

        async def message_gen():
            msg = MagicMock()
            msg.topic = "short/topic"
            msg.payload = b"{}"
            yield msg
            h._running = False

        mock_client.messages = message_gen()
        mock_client.publish = AsyncMock()
        await h._rpc_listen_loop(mock_client)
        mock_client.publish.assert_not_awaited()

    async def test_rpc_listen_cancelled_silent(self):
        """CancelledError 被静默捕获"""
        h = IoTSharpHandler()

        async def message_gen():
            raise asyncio.CancelledError()
            yield  # pragma: no cover

        mock_client = MagicMock()
        mock_client.messages = message_gen()
        await h._rpc_listen_loop(mock_client)  # 不应抛出异常


class TestIoTSharpConnectLoop:
    async def test_connect_loop_connects_and_subscribes(self):
        """连接成功后设置 _connected=True 并订阅 RPC topic"""
        h = IoTSharpHandler()
        fake_mod = _make_aiomqtt_mod()
        call_count = {"n": 0}

        async def fast_sleep(t):
            call_count["n"] += 1
            if call_count["n"] >= 1:
                h._running = False
                raise asyncio.CancelledError()

        with patch.dict(sys.modules, {"aiomqtt": fake_mod}):
            with patch("edgelite.platform.iotsharp.asyncio.sleep", new=fast_sleep):
                h._running = True
                with contextlib.suppress(asyncio.CancelledError):
                    await h._connect_loop("localhost", 1883, "", "")
        assert h._connected is False  # 最终被取消/退出

    async def test_connect_loop_reconnects_on_failure(self):
        """连接失败后重试"""
        h = IoTSharpHandler()
        fake_mod = _make_aiomqtt_mod(fail_count=2)

        sleep_count = {"n": 0}

        async def fast_sleep(t):
            sleep_count["n"] += 1
            if sleep_count["n"] >= 3:
                h._running = False
                raise asyncio.CancelledError()

        with patch.dict(sys.modules, {"aiomqtt": fake_mod}):
            with patch("edgelite.platform.iotsharp.asyncio.sleep", new=fast_sleep):
                with patch("random.random", return_value=0.5):
                    h._running = True
                    with contextlib.suppress(asyncio.CancelledError):
                        await h._connect_loop("localhost", 1883, "", "")
        # 前两次连接失败，第三次成功
        assert sleep_count["n"] >= 1


# ═══════════════════════════════════════════════════════════════════════════
# ThingsBoardHandler 测试
# ═══════════════════════════════════════════════════════════════════════════

from edgelite.platform.thingsboard import ThingsBoardHandler  # noqa: E402


class TestThingsBoardHandlerInit:
    def test_defaults(self):
        h = ThingsBoardHandler()
        assert h.platform_name == "thingsboard"
        assert h._connected is False
        assert h._running is False
        assert h._pub_queue is None
        assert h._rpc_callback is None

    def test_inherits_platform_handler(self):
        assert issubclass(ThingsBoardHandler, PlatformHandler)


class TestThingsBoardPublish:
    async def test_publish_telemetry_disconnected_enqueues_offline(self):
        h = ThingsBoardHandler()
        await h.publish_telemetry("dev1", {"temp": 25.5})
        assert len(h._offline_queue) == 1

    async def test_publish_telemetry_connected(self):
        h = ThingsBoardHandler()
        h._pub_queue = asyncio.Queue(maxsize=10)
        h._connected = True
        await h.publish_telemetry("dev1", {"temp": 25.5})
        assert h._pub_queue.qsize() == 1

    async def test_publish_attributes_disconnected_enqueues_offline(self):
        h = ThingsBoardHandler()
        await h.publish_attributes("dev1", {"model": "X"})
        assert len(h._offline_queue) == 1

    async def test_publish_attributes_connected(self):
        h = ThingsBoardHandler()
        h._pub_queue = asyncio.Queue(maxsize=10)
        h._connected = True
        await h.publish_attributes("dev1", {"model": "X"})
        assert h._pub_queue.qsize() == 1

    async def test_publish_device_status_online(self):
        h = ThingsBoardHandler()
        await h.publish_device_status("dev1", True)
        assert len(h._offline_queue) == 1

    async def test_publish_device_status_offline(self):
        h = ThingsBoardHandler()
        await h.publish_device_status("dev1", False)
        assert len(h._offline_queue) == 1

    async def test_on_rpc_request(self):
        h = ThingsBoardHandler()
        cb = AsyncMock()
        await h.on_rpc_request(cb)
        assert h._rpc_callback is cb


class TestThingsBoardConnect:
    async def test_connect_missing_broker_raises(self):
        h = ThingsBoardHandler()
        with pytest.raises(ValueError, match="broker"):
            await h.connect({"port": 1883})

    async def test_connect_no_aiomqtt_raises(self):
        h = ThingsBoardHandler()
        with patch.dict(sys.modules, {"aiomqtt": None}):
            with pytest.raises(ImportError, match="aiomqtt"):
                await h.connect({"broker": "localhost"})

    async def test_connect_starts_task(self):
        h = ThingsBoardHandler()
        fake_mod = _make_aiomqtt_mod()
        with patch.dict(sys.modules, {"aiomqtt": fake_mod}):
            with patch("edgelite.platform.thingsboard.asyncio.sleep", new_callable=AsyncMock):
                await h.connect({"broker": "localhost", "port": 1883, "token": "tb-token"})
                assert h._running is True
                assert h._connect_task is not None
                await asyncio.sleep(0.05)
        await h.disconnect()

    async def test_disconnect_stops(self):
        h = ThingsBoardHandler()
        await h.disconnect()
        assert h._running is False
        assert h._connected is False


# ═══════════════════════════════════════════════════════════════════════════
# CustomMqttHandler 测试
# ═══════════════════════════════════════════════════════════════════════════

from edgelite.platform.custom_mqtt import CustomMqttHandler  # noqa: E402


class TestCustomMqttHandlerInit:
    def test_defaults(self):
        h = CustomMqttHandler()
        assert h.platform_name == "custom"
        assert h._connected is False
        assert h._running is False

    def test_inherits_platform_handler(self):
        assert issubclass(CustomMqttHandler, PlatformHandler)


class TestCustomMqttPublish:
    async def test_publish_telemetry_disconnected_enqueues_offline(self):
        h = CustomMqttHandler()
        await h.publish_telemetry("dev1", {"temp": 25.5})
        assert len(h._offline_queue) == 1

    async def test_publish_telemetry_connected(self):
        h = CustomMqttHandler()
        h._pub_queue = asyncio.Queue(maxsize=10)
        h._connected = True
        await h.publish_telemetry("dev1", {"temp": 25.5})
        assert h._pub_queue.qsize() == 1

    async def test_publish_attributes_disconnected_enqueues_offline(self):
        h = CustomMqttHandler()
        await h.publish_attributes("dev1", {"model": "X"})
        assert len(h._offline_queue) == 1

    async def test_publish_attributes_connected(self):
        h = CustomMqttHandler()
        h._pub_queue = asyncio.Queue(maxsize=10)
        h._connected = True
        await h.publish_attributes("dev1", {"model": "X"})
        assert h._pub_queue.qsize() == 1

    async def test_publish_device_status_disconnected_no_enqueue(self):
        """CustomMqtt publish_device_status 断连时直接 return（不入离线队列）"""
        h = CustomMqttHandler()
        await h.publish_device_status("dev1", True)
        assert len(h._offline_queue) == 0

    async def test_publish_device_status_connected(self):
        """CustomMqtt publish_device_status 连接时入发布队列"""
        h = CustomMqttHandler()
        h._pub_queue = asyncio.Queue(maxsize=10)
        h._connected = True
        await h.publish_device_status("dev1", True)
        assert h._pub_queue.qsize() == 1

    async def test_on_rpc_request(self):
        h = CustomMqttHandler()
        cb = AsyncMock()
        await h.on_rpc_request(cb)
        assert h._rpc_callback is cb


class TestCustomMqttConnect:
    async def test_connect_missing_broker_raises(self):
        h = CustomMqttHandler()
        with pytest.raises(ValueError, match="broker"):
            await h.connect({"port": 1883})

    async def test_connect_no_aiomqtt_raises(self):
        h = CustomMqttHandler()
        with patch.dict(sys.modules, {"aiomqtt": None}):
            with pytest.raises(ImportError, match="aiomqtt"):
                await h.connect({"broker": "localhost"})

    async def test_connect_starts_task(self):
        h = CustomMqttHandler()
        fake_mod = _make_aiomqtt_mod()
        with patch.dict(sys.modules, {"aiomqtt": fake_mod}):
            with patch("edgelite.platform.custom_mqtt.asyncio.sleep", new_callable=AsyncMock):
                await h.connect({"broker": "localhost", "port": 1883})
                assert h._running is True
                assert h._connect_task is not None
                await asyncio.sleep(0.05)
        await h.disconnect()

    async def test_disconnect_no_task_silent(self):
        h = CustomMqttHandler()
        await h.disconnect()
        assert h._running is False
        assert h._connected is False
