"""MQTT北向转发器测试

覆盖模块：
- edgelite.engine.mqtt_forwarder.MqttForwarder: 启动/停止/事件处理/连接循环/
  发布循环/离线持久化/重传/SSL/状态查询
- edgelite.engine.mqtt_forwarder._sanitize_topic_segment: topic 注入防护
- edgelite.api.mqtt_forwarder: 离线队列状态 API 端点

所有外部依赖（aiomqtt、网络、_app_state）均被 mock，不发起真实网络调用。
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import ssl
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, "src")

from conftest import make_app
from fastapi import HTTPException
from fastapi.testclient import TestClient

from edgelite.api.mqtt_forwarder import router
from edgelite.engine.mqtt_forwarder import MqttForwarder, _sanitize_topic_segment


async def _wait_for(condition, timeout: float = 1.5, interval: float = 0.01) -> bool:
    """轮询等待条件成立，避免硬编码 sleep。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        await asyncio.sleep(interval)
    return False


@contextlib.contextmanager
def _fast_terminate(forwarder: MqttForwarder):
    """将 asyncio.sleep 替换为立即返回并将 _running 置 False，用于快速终止连接循环。"""
    real_sleep = asyncio.sleep

    async def _sleep(delay, *args, **kwargs):
        forwarder._running = False
        await real_sleep(0)

    with patch("asyncio.sleep", _sleep):
        yield


def _make_aiomqtt_mock(raises=None):
    """构造 mock aiomqtt 模块，Client 为异步上下文管理器。"""
    mock_client = AsyncMock()
    mock_client.publish = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=mock_client)
    cm.__aexit__ = AsyncMock(return_value=None)
    aiomqtt_mod = MagicMock()
    if raises is not None:
        aiomqtt_mod.Client = MagicMock(side_effect=raises)
    else:
        aiomqtt_mod.Client = MagicMock(return_value=cm)
    aiomqtt_mod.TLSParameters = MagicMock()
    return aiomqtt_mod, mock_client


@pytest.fixture(autouse=True)
def mock_engine_config(monkeypatch):
    """提供可变的引擎配置对象并 patch get_config。"""
    mqtt = SimpleNamespace(
        broker="broker.example.com",
        port=1883,
        username="user",
        password="pass",
        topic_prefix="edgelite",
        offline_cache_enabled=True,
        offline_db_path="data/mqtt_offline_queue.db",
        max_queue_size=10000,
        max_retries=100,
        retry_interval=5.0,
        ring_buffer_capacity=50000,
        ring_buffer_compress=True,
    )
    cfg = SimpleNamespace(
        mqtt=mqtt,
        mqtt_server=SimpleNamespace(enabled=False, host="127.0.0.1", port=1888),
        mqtt_tls=None,
    )
    monkeypatch.setattr("edgelite.engine.mqtt_forwarder.get_config", lambda: cfg)
    return cfg


@pytest.fixture
def forwarder():
    """创建 MqttForwarder 实例，测试后清理 SQLite 连接。"""
    fwd = MqttForwarder()
    yield fwd
    if fwd._offline_db is not None:
        with contextlib.suppress(Exception):
            fwd._offline_db.close()


class TestSanitizeTopicSegment:
    def test_replaces_slash(self):
        """斜杠应被替换为下划线"""
        assert _sanitize_topic_segment("a/b") == "a_b"

    def test_replaces_wildcards_and_null(self):
        """+、#、空字节应被替换"""
        assert _sanitize_topic_segment("a+b#c") == "a_b_c"
        assert _sanitize_topic_segment("a\0b") == "a_b"

    def test_empty_returns_empty(self):
        """空字符串应返回空"""
        assert _sanitize_topic_segment("") == ""

    def test_no_special_chars(self):
        """无特殊字符应原样返回"""
        assert _sanitize_topic_segment("device_1") == "device_1"


class TestInitAndProperties:
    def test_defaults(self, forwarder):
        """新实例应处于未运行/未连接状态"""
        assert forwarder._running is False
        assert forwarder._connected is False
        assert forwarder._sent_count == 0
        assert forwarder._handlers_registered is False

    def test_is_connected_property(self, forwarder):
        """is_connected 应反映 _connected"""
        assert forwarder.is_connected is False
        forwarder._connected = True
        assert forwarder.is_connected is True


class TestStartStop:
    async def test_start_no_broker_skips(self, forwarder, mock_engine_config):
        """未配置 broker 应直接返回不启动"""
        mock_engine_config.mqtt.broker = ""
        await forwarder.start()
        assert forwarder._running is False

    async def test_start_with_event_bus_registers_handlers(self, forwarder, mock_engine_config, monkeypatch):
        """有 event_bus 应注册三个事件处理器并启动连接任务"""
        event_bus = MagicMock()
        monkeypatch.setattr(forwarder, "_init_offline_db", MagicMock())
        monkeypatch.setattr(forwarder, "_init_ring_buffer", MagicMock())
        monkeypatch.setattr(forwarder, "_connect_loop", AsyncMock())
        monkeypatch.setattr("edgelite.engine.mqtt_forwarder.OfflineQueue", MagicMock())

        await forwarder.start(event_bus=event_bus)
        try:
            assert forwarder._running is True
            assert forwarder._handlers_registered is True
            event_bus.register_handler.assert_any_call("PointUpdateEvent", forwarder._on_point_update)
            event_bus.register_handler.assert_any_call("AlarmEvent", forwarder._on_alarm_event)
            event_bus.register_handler.assert_any_call("DeviceStatusEvent", forwarder._on_device_status)
        finally:
            await forwarder.stop()
        assert forwarder._handlers_registered is False
        event_bus.unregister_handler.assert_any_call("PointUpdateEvent", forwarder._on_point_update)

    async def test_start_already_running_stops_first(self, forwarder, mock_engine_config, monkeypatch):
        """已运行时应先停止再重启"""
        forwarder._running = True
        forwarder._connect_task = asyncio.create_task(asyncio.sleep(100))
        monkeypatch.setattr(forwarder, "_init_offline_db", MagicMock())
        monkeypatch.setattr(forwarder, "_init_ring_buffer", MagicMock())
        monkeypatch.setattr(forwarder, "_connect_loop", AsyncMock())
        monkeypatch.setattr("edgelite.engine.mqtt_forwarder.OfflineQueue", MagicMock())

        await forwarder.start()
        await forwarder.stop()
        assert forwarder._running is False

    async def test_stop_cancels_tasks(self, forwarder):
        """stop 应取消 replay_task 与 connect_task"""
        forwarder._running = True
        forwarder._replay_task = asyncio.create_task(asyncio.sleep(100))
        forwarder._connect_task = asyncio.create_task(asyncio.sleep(100))
        await forwarder.stop()
        assert forwarder._running is False
        assert forwarder._replay_task.done()
        assert forwarder._connect_task.done()

    async def test_stop_without_tasks(self, forwarder):
        """无任务时 stop 不应抛异常"""
        forwarder._running = True
        await forwarder.stop()
        assert forwarder._running is False


class TestEventHandlers:
    async def test_on_point_update_enqueues(self, forwarder):
        """PointUpdateEvent 应入队为 point_update 消息"""
        forwarder._pub_queue = asyncio.Queue()
        event = SimpleNamespace(device_id="d1", point_name="p1", value=10, quality="good")
        await forwarder._on_point_update(event)
        data = forwarder._pub_queue.get_nowait()
        assert data["type"] == "point_update"
        assert data["device_id"] == "d1"
        assert data["value"] == 10
        assert "msg_id" in data

    async def test_on_point_update_no_queue(self, forwarder):
        """无队列时应直接返回"""
        forwarder._pub_queue = None
        await forwarder._on_point_update(SimpleNamespace(device_id="d1", point_name="p", value=1, quality="good"))

    async def test_on_point_update_queue_full_persists(self, forwarder, tmp_path):
        """队列满且开启离线缓存时应持久化到 SQLite"""
        forwarder._pub_queue = asyncio.Queue(maxsize=1)
        forwarder._pub_queue.put_nowait({"x": 1})
        forwarder._offline_cache_enabled = True
        forwarder._offline_db_path = str(tmp_path / "off.db")
        forwarder._init_offline_db()
        await forwarder._on_point_update(SimpleNamespace(device_id="d1", point_name="p", value=1, quality="good"))
        count = forwarder._offline_db.execute("SELECT COUNT(*) FROM offline_queue").fetchone()[0]
        assert count == 1

    async def test_on_point_update_queue_full_disabled_drops(self, forwarder):
        """队列满且禁用离线缓存时应丢弃不抛异常"""
        forwarder._pub_queue = asyncio.Queue(maxsize=1)
        forwarder._pub_queue.put_nowait({"x": 1})
        forwarder._offline_cache_enabled = False
        await forwarder._on_point_update(SimpleNamespace(device_id="d1", point_name="p", value=1, quality="good"))

    async def test_on_alarm_event_enqueues(self, forwarder):
        """AlarmEvent 应入队为 alarm 消息"""
        forwarder._pub_queue = asyncio.Queue()
        event = SimpleNamespace(alarm_id="a1", device_id="d1", severity="critical", action="firing")
        await forwarder._on_alarm_event(event)
        data = forwarder._pub_queue.get_nowait()
        assert data["type"] == "alarm"
        assert data["alarm_id"] == "a1"
        assert data["severity"] == "critical"

    async def test_on_alarm_event_queue_full_persists(self, forwarder, tmp_path):
        """告警队列满时应持久化"""
        forwarder._pub_queue = asyncio.Queue(maxsize=1)
        forwarder._pub_queue.put_nowait({"x": 1})
        forwarder._offline_cache_enabled = True
        forwarder._offline_db_path = str(tmp_path / "off.db")
        forwarder._init_offline_db()
        await forwarder._on_alarm_event(
            SimpleNamespace(alarm_id="a1", device_id="d1", severity="high", action="firing")
        )
        count = forwarder._offline_db.execute("SELECT COUNT(*) FROM offline_queue").fetchone()[0]
        assert count == 1

    async def test_on_device_status_enqueues(self, forwarder):
        """DeviceStatusEvent 应入队为 device_status 消息"""
        forwarder._pub_queue = asyncio.Queue()
        event = SimpleNamespace(device_id="d1", old_status="offline", new_status="online")
        await forwarder._on_device_status(event)
        data = forwarder._pub_queue.get_nowait()
        assert data["type"] == "device_status"
        assert data["new_status"] == "online"

    async def test_on_device_status_queue_full_persists(self, forwarder, tmp_path):
        """设备状态队列满时应持久化"""
        forwarder._pub_queue = asyncio.Queue(maxsize=1)
        forwarder._pub_queue.put_nowait({"x": 1})
        forwarder._offline_cache_enabled = True
        forwarder._offline_db_path = str(tmp_path / "off.db")
        forwarder._init_offline_db()
        await forwarder._on_device_status(SimpleNamespace(device_id="d1", old_status="off", new_status="on"))
        count = forwarder._offline_db.execute("SELECT COUNT(*) FROM offline_queue").fetchone()[0]
        assert count == 1


class TestBuildSslContext:
    def test_disabled_returns_none(self, forwarder):
        """TLS 未启用应返回 None"""
        cfg = SimpleNamespace(mqtt_tls=SimpleNamespace(enabled=False))
        assert forwarder._build_ssl_context(cfg) is None

    def test_insecure_blocked_by_default(self, forwarder, monkeypatch):
        """未显式允许时 cert_reqs=none 应抛 ValueError"""
        monkeypatch.delenv("EDGELITE_ALLOW_INSECURE_TLS", raising=False)
        cfg = SimpleNamespace(
            mqtt_tls=SimpleNamespace(enabled=True, cert_reqs="none", ca_cert="", client_cert="", client_key="")
        )
        with pytest.raises(ValueError, match="cert_reqs=none prohibited"):
            forwarder._build_ssl_context(cfg)

    def test_insecure_allowed_with_env(self, forwarder, monkeypatch):
        """设置环境变量后 cert_reqs=none 应允许"""
        monkeypatch.setenv("EDGELITE_ALLOW_INSECURE_TLS", "1")
        cfg = SimpleNamespace(
            mqtt_tls=SimpleNamespace(enabled=True, cert_reqs="none", ca_cert="", client_cert="", client_key="")
        )
        ctx = forwarder._build_ssl_context(cfg)
        assert ctx is not None
        assert ctx.verify_mode == ssl.CERT_NONE

    def test_required_mode(self, forwarder):
        """cert_reqs=required 应启用主机名校验"""
        cfg = SimpleNamespace(
            mqtt_tls=SimpleNamespace(enabled=True, cert_reqs="required", ca_cert="", client_cert="", client_key="")
        )
        ctx = forwarder._build_ssl_context(cfg)
        assert ctx.verify_mode == ssl.CERT_REQUIRED
        assert ctx.check_hostname is True

    def test_optional_mode(self, forwarder):
        """cert_reqs=optional 应关闭主机名校验"""
        cfg = SimpleNamespace(
            mqtt_tls=SimpleNamespace(enabled=True, cert_reqs="optional", ca_cert="", client_cert="", client_key="")
        )
        ctx = forwarder._build_ssl_context(cfg)
        assert ctx.verify_mode == ssl.CERT_OPTIONAL
        assert ctx.check_hostname is False

    def test_loads_ca_and_client_cert(self, forwarder, tmp_path, monkeypatch):
        """存在 CA/客户端证书文件时应加载"""
        mock_ctx = MagicMock()
        monkeypatch.setattr(ssl, "create_default_context", lambda: mock_ctx)
        ca = tmp_path / "ca.pem"
        ca.write_text("ca")
        cert = tmp_path / "client.pem"
        cert.write_text("cert")
        key = tmp_path / "client.key"
        key.write_text("key")
        cfg = SimpleNamespace(
            mqtt_tls=SimpleNamespace(
                enabled=True, cert_reqs="required", ca_cert=str(ca), client_cert=str(cert), client_key=str(key)
            )
        )
        ctx = forwarder._build_ssl_context(cfg)
        assert ctx is mock_ctx
        mock_ctx.load_verify_locations.assert_called_once_with(str(ca))
        mock_ctx.load_cert_chain.assert_called_once_with(str(cert), str(key))

    def test_unknown_cert_reqs_defaults_required(self, forwarder, monkeypatch):
        """未知 cert_reqs 应回退为 CERT_REQUIRED"""
        monkeypatch.setattr(ssl, "create_default_context", lambda: MagicMock(spec=ssl.SSLContext))
        cfg = SimpleNamespace(
            mqtt_tls=SimpleNamespace(enabled=True, cert_reqs="bogus", ca_cert="", client_cert="", client_key="")
        )
        ctx = forwarder._build_ssl_context(cfg)
        assert ctx is not None


class TestConnectLoop:
    async def test_ssrf_blocked_returns(self, forwarder, mock_engine_config, monkeypatch):
        """SSRF 拦截应立即停止并返回"""
        monkeypatch.setattr("edgelite.engine.mqtt_forwarder._is_broker_host_safe", lambda host: False)
        forwarder._running = True
        await forwarder._connect_loop()
        assert forwarder._running is False

    async def test_success_path(self, forwarder, mock_engine_config, monkeypatch):
        """连接成功应重置失败计数并在退出时置 _connected=False"""
        monkeypatch.setattr("edgelite.engine.mqtt_forwarder._is_broker_host_safe", lambda host: True)
        forwarder._running = True
        forwarder._offline_queue = AsyncMock()
        forwarder._offline_queue.flush = AsyncMock(return_value=0)
        aiomqtt_mod, _ = _make_aiomqtt_mock()
        with patch.dict(sys.modules, {"aiomqtt": aiomqtt_mod}), _fast_terminate(forwarder):
            await asyncio.wait_for(forwarder._connect_loop(), timeout=3)
        assert forwarder._consecutive_failures == 0
        assert forwarder._connected is False

    async def test_import_error_handled(self, forwarder, mock_engine_config, monkeypatch):
        """aiomqtt 缺失应被捕获并退出循环"""
        monkeypatch.setattr("edgelite.engine.mqtt_forwarder._is_broker_host_safe", lambda host: True)
        forwarder._running = True
        with patch.dict(sys.modules, {"aiomqtt": None}), _fast_terminate(forwarder):
            await asyncio.wait_for(forwarder._connect_loop(), timeout=3)
        assert forwarder._connected is False

    async def test_connection_exception_retries(self, forwarder, mock_engine_config, monkeypatch):
        """连接异常应累加失败计数并重试"""
        monkeypatch.setattr("edgelite.engine.mqtt_forwarder._is_broker_host_safe", lambda host: True)
        forwarder._running = True
        aiomqtt_mod, _ = _make_aiomqtt_mock(raises=ConnectionRefusedError("Connection refused"))
        with patch.dict(sys.modules, {"aiomqtt": aiomqtt_mod}), _fast_terminate(forwarder):
            await asyncio.wait_for(forwarder._connect_loop(), timeout=3)
        assert forwarder._consecutive_failures >= 1
        assert forwarder._connected is False

    async def test_switches_to_builtin_server(self, forwarder, mock_engine_config, monkeypatch):
        """broker=localhost:1883 且内置 Server 启用时应切换端口"""
        mock_engine_config.mqtt.broker = "localhost"
        mock_engine_config.mqtt.port = 1883
        mock_engine_config.mqtt_server.enabled = True
        mock_engine_config.mqtt_server.host = "127.0.0.1"
        mock_engine_config.mqtt_server.port = 1888
        monkeypatch.setattr("edgelite.engine.mqtt_forwarder._is_broker_host_safe", lambda host: True)
        forwarder._running = True
        aiomqtt_mod, _ = _make_aiomqtt_mock()
        with patch.dict(sys.modules, {"aiomqtt": aiomqtt_mod}), _fast_terminate(forwarder):
            await asyncio.wait_for(forwarder._connect_loop(), timeout=3)
        call_kwargs = aiomqtt_mod.Client.call_args.kwargs
        assert call_kwargs["hostname"] == "127.0.0.1"
        assert call_kwargs["port"] == 1888

    async def test_generic_exception_logged(self, forwarder, mock_engine_config, monkeypatch):
        """非 Connection refused 的异常应走通用错误日志分支"""
        monkeypatch.setattr("edgelite.engine.mqtt_forwarder._is_broker_host_safe", lambda host: True)
        forwarder._running = True
        aiomqtt_mod, _ = _make_aiomqtt_mock(raises=RuntimeError("some weird error"))
        with patch.dict(sys.modules, {"aiomqtt": aiomqtt_mod}), _fast_terminate(forwarder):
            await asyncio.wait_for(forwarder._connect_loop(), timeout=3)
        assert forwarder._consecutive_failures >= 1


class TestPublishLoop:
    async def test_publishes_when_connected(self, forwarder, mock_engine_config):
        """已连接时应调用 client.publish 并累加 sent_count"""
        forwarder._running = True
        forwarder._connected = True
        forwarder._pub_queue = asyncio.Queue()
        forwarder._pub_queue.put_nowait({"type": "point_update", "device_id": "dev1", "value": 42})
        mock_client = AsyncMock()
        task = asyncio.create_task(forwarder._publish_loop(mock_client))
        try:
            assert await _wait_for(lambda: forwarder._sent_count >= 1)
        finally:
            forwarder._running = False
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        mock_client.publish.assert_called_once()
        topic = mock_client.publish.call_args.args[0]
        assert "dev1" in topic
        assert forwarder._sent_count == 1

    async def test_disconnected_persists_to_sqlite(self, forwarder, mock_engine_config, tmp_path):
        """断连时应将消息持久化到 SQLite 离线队列"""
        forwarder._running = True
        forwarder._connected = False
        forwarder._pub_queue = asyncio.Queue()
        forwarder._pub_queue.put_nowait({"type": "alarm", "device_id": "dev1"})
        forwarder._offline_db_path = str(tmp_path / "off.db")
        forwarder._init_offline_db()
        forwarder._offline_queue = None
        task = asyncio.create_task(forwarder._publish_loop(AsyncMock()))
        try:
            assert await _wait_for(
                lambda: forwarder._offline_db.execute("SELECT COUNT(*) FROM offline_queue").fetchone()[0] >= 1
            )
        finally:
            forwarder._running = False
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        count = forwarder._offline_db.execute("SELECT COUNT(*) FROM offline_queue").fetchone()[0]
        assert count >= 1

    async def test_disconnected_uses_offline_queue(self, forwarder, mock_engine_config):
        """断连且禁用离线缓存时应通过 OfflineQueue.enqueue 持久化"""
        forwarder._running = True
        forwarder._connected = False
        forwarder._offline_cache_enabled = False
        forwarder._pub_queue = asyncio.Queue()
        forwarder._pub_queue.put_nowait({"type": "alarm", "device_id": "dev1"})
        mock_oq = AsyncMock()
        forwarder._offline_queue = mock_oq
        task = asyncio.create_task(forwarder._publish_loop(AsyncMock()))
        try:
            assert await _wait_for(lambda: mock_oq.enqueue.called)
        finally:
            forwarder._running = False
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        mock_oq.enqueue.assert_called_once()
        assert "alarm" in mock_oq.enqueue.call_args.args[0]

    async def test_publish_error_not_connected_persists(self, forwarder, mock_engine_config, tmp_path):
        """publish 抛 not currently connected 应置断连并持久化"""
        forwarder._running = True
        forwarder._connected = True
        forwarder._pub_queue = asyncio.Queue()
        forwarder._pub_queue.put_nowait({"type": "point_update", "device_id": "dev1", "value": 1})
        forwarder._offline_db_path = str(tmp_path / "off.db")
        forwarder._init_offline_db()
        forwarder._offline_queue = None
        mock_client = AsyncMock()
        mock_client.publish = AsyncMock(side_effect=RuntimeError("Client is not currently connected"))
        task = asyncio.create_task(forwarder._publish_loop(mock_client))
        try:
            assert await _wait_for(
                lambda: forwarder._offline_db.execute("SELECT COUNT(*) FROM offline_queue").fetchone()[0] >= 1
            )
        finally:
            forwarder._running = False
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        assert forwarder._connected is False

    async def test_publish_other_error_logged(self, forwarder, mock_engine_config):
        """publish 抛其他异常应走错误日志分支且不持久化"""
        forwarder._running = True
        forwarder._connected = True
        forwarder._offline_cache_enabled = False
        forwarder._offline_queue = None
        forwarder._pub_queue = asyncio.Queue()
        forwarder._pub_queue.put_nowait({"type": "point_update", "device_id": "dev1", "value": 1})
        mock_client = AsyncMock()
        mock_client.publish = AsyncMock(side_effect=RuntimeError("unexpected boom"))
        task = asyncio.create_task(forwarder._publish_loop(mock_client))
        try:
            await asyncio.sleep(0.05)
        finally:
            forwarder._running = False
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        assert forwarder._connected is True


class TestOfflineDbAndPersist:
    def test_init_offline_db_creates_table(self, forwarder, tmp_path):
        """初始化应创建 offline_queue 表"""
        forwarder._offline_db_path = str(tmp_path / "off.db")
        forwarder._init_offline_db()
        assert forwarder._offline_db is not None
        cols = forwarder._offline_db.execute("PRAGMA table_info(offline_queue)").fetchall()
        names = {row[1] for row in cols}
        assert "priority" in names

    def test_init_offline_db_failure_disables_cache(self, forwarder, tmp_path, monkeypatch):
        """SQLite 初始化失败应禁用离线缓存"""
        monkeypatch.setattr(
            "edgelite.engine.mqtt_forwarder.sqlite3.connect",
            MagicMock(side_effect=sqlite3.OperationalError("fail")),
        )
        forwarder._offline_db_path = str(tmp_path / "off.db")
        forwarder._init_offline_db()
        assert forwarder._offline_cache_enabled is False
        assert forwarder._offline_db is None

    async def test_persist_message_writes_sqlite_and_ring(self, forwarder, tmp_path):
        """持久化应同时写入 SQLite 和 RingBuffer"""
        from edgelite.storage.ring_buffer import RingBuffer

        forwarder._offline_db_path = str(tmp_path / "off.db")
        forwarder._init_offline_db()
        forwarder._ring_buffer = RingBuffer(capacity=100, compress=False)
        await forwarder._persist_message("t/a", '{"v":1}', qos=1, priority="alarm", data={"type": "alarm"})
        count = forwarder._offline_db.execute("SELECT COUNT(*) FROM offline_queue").fetchone()[0]
        assert count == 1
        assert forwarder._ring_buffer.get_stats()["pending"] == 1

    async def test_persist_message_no_db_no_ring(self, forwarder):
        """无 DB 无 RingBuffer 时持久化不应抛异常"""
        forwarder._offline_db = None
        forwarder._ring_buffer = None
        await forwarder._persist_message("t", "{}", 1, "")

    def test_persist_message_sync_returns_id(self, forwarder, tmp_path):
        """同步持久化应返回 lastrowid"""
        forwarder._offline_db_path = str(tmp_path / "off.db")
        forwarder._init_offline_db()
        rid = forwarder._persist_message_sync("t", "{}", 1, "")
        assert rid is not None and rid > 0

    def test_persist_message_sync_no_db(self, forwarder):
        """无 DB 时同步持久化应返回 None"""
        forwarder._offline_db = None
        assert forwarder._persist_message_sync("t", "{}", 1, "") is None

    async def test_check_queue_capacity_evicts_oldest(self, forwarder, tmp_path):
        """达到上限时应淘汰最旧记录"""
        forwarder._offline_db_path = str(tmp_path / "off.db")
        forwarder._init_offline_db()
        forwarder._max_queue_size = 2
        for i in range(2):
            forwarder._offline_db.execute(
                "INSERT INTO offline_queue (topic,payload,qos,created_at,status) VALUES (?,?,?,?,?)",
                (f"t{i}", "{}", 1, time.time(), "pending"),
            )
        forwarder._offline_db.commit()
        assert forwarder._get_pending_count() == 2
        await forwarder._check_queue_capacity()
        assert forwarder._get_pending_count() == 1

    def test_evict_oldest_no_db(self, forwarder):
        """无 DB 时淘汰应为空操作"""
        forwarder._offline_db = None
        forwarder._evict_oldest()

    def test_get_pending_count_no_db(self, forwarder):
        """无 DB 时待处理计数应为 0"""
        forwarder._offline_db = None
        assert forwarder._get_pending_count() == 0

    def test_get_pending_count_query_error(self, forwarder, tmp_path):
        """查询异常应返回 0"""
        forwarder._offline_db_path = str(tmp_path / "off.db")
        forwarder._init_offline_db()
        forwarder._offline_db.execute("DROP TABLE offline_queue")
        forwarder._offline_db.commit()
        assert forwarder._get_pending_count() == 0


class TestRingBufferInit:
    def test_init_ring_buffer_success(self, forwarder, mock_engine_config):
        """正常初始化应创建 RingBuffer"""
        forwarder._init_ring_buffer(mock_engine_config)
        assert forwarder._ring_buffer is not None

    def test_init_ring_buffer_failure(self, forwarder, mock_engine_config, monkeypatch):
        """RingBuffer 构造失败应回退为 None"""
        monkeypatch.setattr("edgelite.engine.mqtt_forwarder.RingBuffer", MagicMock(side_effect=RuntimeError("fail")))
        forwarder._init_ring_buffer(mock_engine_config)
        assert forwarder._ring_buffer is None

    def test_restore_no_db_returns(self, forwarder):
        """无 DB 时恢复应直接返回"""
        forwarder._ring_buffer = MagicMock()
        forwarder._offline_db = None
        forwarder._restore_ring_buffer_from_sqlite()

    def test_restore_from_sqlite_success(self, forwarder, tmp_path):
        """应从 SQLite 恢复 pending 记录到 RingBuffer"""
        from edgelite.storage.ring_buffer import RingBuffer

        forwarder._offline_db_path = str(tmp_path / "off.db")
        forwarder._init_offline_db()
        forwarder._offline_db.execute(
            "INSERT INTO offline_queue (topic,payload,qos,created_at,status,priority) VALUES (?,?,?,?,?,?)",
            ("t/a", '{"v":1}', 1, time.time(), "pending", ""),
        )
        forwarder._offline_db.commit()
        forwarder._ring_buffer = RingBuffer(capacity=100, compress=False)
        forwarder._restore_ring_buffer_from_sqlite()
        assert forwarder._ring_buffer.get_stats()["pending"] >= 1

    def test_restore_from_sqlite_exception(self, forwarder, tmp_path):
        """恢复过程异常应被捕获不抛出"""
        from edgelite.storage.ring_buffer import RingBuffer

        forwarder._offline_db_path = str(tmp_path / "off.db")
        forwarder._init_offline_db()
        forwarder._offline_db.execute("DROP TABLE offline_queue")
        forwarder._offline_db.commit()
        forwarder._ring_buffer = RingBuffer(capacity=10, compress=False)
        forwarder._restore_ring_buffer_from_sqlite()


class TestReplay:
    async def test_replay_from_ring_buffer_success(self, forwarder):
        """RingBuffer 重传成功应标记 synced 并累加 sent_count"""
        forwarder._running = True
        forwarder._connected = True
        forwarder._ring_buffer = MagicMock()
        forwarder._ring_buffer.get_pending = AsyncMock(
            side_effect=[[{"_id": 1, "topic": "t/a", "payload": '{"v":1}', "qos": 1, "sqlite_id": 10}], []]
        )
        forwarder._ring_buffer.mark_synced = AsyncMock()
        forwarder._ring_buffer.mark_failed = AsyncMock()
        forwarder._offline_db = None
        mock_client = AsyncMock()
        result = await forwarder._replay_from_ring_buffer(mock_client)
        assert result is True
        mock_client.publish.assert_called_once()
        forwarder._ring_buffer.mark_synced.assert_called_once_with([1])
        assert forwarder._sent_count == 1

    async def test_replay_from_ring_buffer_publish_failure(self, forwarder):
        """重传 publish 失败应回退为 pending"""
        forwarder._running = True
        forwarder._connected = True
        forwarder._ring_buffer = MagicMock()
        forwarder._ring_buffer.get_pending = AsyncMock(
            side_effect=[[{"_id": 5, "topic": "t", "payload": "{}", "qos": 1}], []]
        )
        forwarder._ring_buffer.mark_synced = AsyncMock()
        forwarder._ring_buffer.mark_failed = AsyncMock()
        forwarder._offline_db = None
        mock_client = AsyncMock()
        mock_client.publish = AsyncMock(side_effect=RuntimeError("boom"))
        result = await forwarder._replay_from_ring_buffer(mock_client)
        assert result is True
        forwarder._ring_buffer.mark_failed.assert_called_once_with([5])
        forwarder._ring_buffer.mark_synced.assert_not_called()

    async def test_replay_from_ring_buffer_empty(self, forwarder):
        """RingBuffer 无记录应返回 False"""
        forwarder._ring_buffer = MagicMock()
        forwarder._ring_buffer.get_pending = AsyncMock(return_value=[])
        result = await forwarder._replay_from_ring_buffer(AsyncMock())
        assert result is False

    async def test_replay_from_ring_buffer_deletes_sqlite(self, forwarder, tmp_path):
        """重传成功应删除对应 SQLite 记录"""
        forwarder._running = True
        forwarder._connected = True
        forwarder._offline_db_path = str(tmp_path / "off.db")
        forwarder._init_offline_db()
        forwarder._offline_db.execute(
            "INSERT INTO offline_queue (topic,payload,qos,created_at,status,priority) VALUES (?,?,?,?,?,?)",
            ("t/a", '{"v":1}', 1, time.time(), "pending", ""),
        )
        forwarder._offline_db.commit()
        sqlite_id = forwarder._offline_db.execute("SELECT id FROM offline_queue").fetchone()[0]
        forwarder._ring_buffer = MagicMock()
        forwarder._ring_buffer.get_pending = AsyncMock(
            side_effect=[[{"_id": 1, "topic": "t/a", "payload": '{"v":1}', "qos": 1, "sqlite_id": sqlite_id}], []]
        )
        forwarder._ring_buffer.mark_synced = AsyncMock()
        forwarder._ring_buffer.mark_failed = AsyncMock()
        await forwarder._replay_from_ring_buffer(AsyncMock())
        count = forwarder._offline_db.execute("SELECT COUNT(*) FROM offline_queue").fetchone()[0]
        assert count == 0

    async def test_replay_from_sqlite_success(self, forwarder, tmp_path):
        """SQLite 重传成功应删除已发送记录"""
        forwarder._running = True
        forwarder._connected = True
        forwarder._offline_db_path = str(tmp_path / "off.db")
        forwarder._init_offline_db()
        forwarder._offline_db.execute(
            "INSERT INTO offline_queue (topic,payload,qos,created_at,status,priority,retry_count) "
            "VALUES (?,?,?,?,?,?,?)",
            ("t/a", '{"v":1}', 1, time.time(), "pending", "", 0),
        )
        forwarder._offline_db.commit()
        mock_client = AsyncMock()
        await forwarder._replay_from_sqlite(mock_client)
        mock_client.publish.assert_called_once()
        assert forwarder._sent_count == 1
        count = forwarder._offline_db.execute("SELECT COUNT(*) FROM offline_queue").fetchone()[0]
        assert count == 0

    async def test_replay_from_sqlite_no_db(self, forwarder):
        """无 DB 时 SQLite 重传应 sleep 后返回"""
        forwarder._offline_db = None
        forwarder._retry_interval = 0.01
        await forwarder._replay_from_sqlite(AsyncMock())

    async def test_replay_from_sqlite_query_error(self, forwarder, tmp_path):
        """查询失败应被捕获并返回"""
        forwarder._offline_db_path = str(tmp_path / "off.db")
        forwarder._init_offline_db()
        forwarder._offline_db.execute("DROP TABLE offline_queue")
        forwarder._offline_db.commit()
        await forwarder._replay_from_sqlite(AsyncMock())

    async def test_replay_from_sqlite_publish_failure(self, forwarder, tmp_path):
        """重传失败应增加 retry_count"""
        forwarder._running = True
        forwarder._connected = True
        forwarder._offline_db_path = str(tmp_path / "off.db")
        forwarder._init_offline_db()
        forwarder._offline_db.execute(
            "INSERT INTO offline_queue (topic,payload,qos,created_at,status,priority,retry_count) "
            "VALUES (?,?,?,?,?,?,?)",
            ("t/a", '{"v":1}', 1, time.time(), "pending", "", 0),
        )
        forwarder._offline_db.commit()
        mock_client = AsyncMock()
        mock_client.publish = AsyncMock(side_effect=RuntimeError("boom"))
        await forwarder._replay_from_sqlite(mock_client)
        rc = forwarder._offline_db.execute("SELECT retry_count FROM offline_queue").fetchone()[0]
        assert rc == 1

    async def test_replay_offline_queue_terminates(self, forwarder):
        """_replay_offline_queue 应在 running=False 后退出"""
        forwarder._running = True
        forwarder._connected = True
        forwarder._retry_interval = 0.01
        forwarder._ring_buffer = MagicMock()
        forwarder._ring_buffer.get_pending = AsyncMock(return_value=[])
        forwarder._offline_db = None
        task = asyncio.create_task(forwarder._replay_offline_queue(AsyncMock()))
        await asyncio.sleep(0.05)
        forwarder._running = False
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


class TestGetOfflineQueueStatus:
    def test_without_db(self, forwarder):
        """无 DB 时应返回禁用状态结构"""
        forwarder._offline_db = None
        forwarder._ring_buffer = None
        status = forwarder.get_offline_queue_status()
        assert status["enabled"] is True
        assert status["pending_count"] == 0
        assert status["db_size_bytes"] == 0
        assert status["oldest_timestamp"] is None

    def test_with_empty_db(self, forwarder, tmp_path):
        """空 DB 应返回非零 db_size 与 None oldest"""
        forwarder._offline_db_path = str(tmp_path / "off.db")
        forwarder._init_offline_db()
        forwarder._sent_count = 3
        status = forwarder.get_offline_queue_status()
        assert status["enabled"] is True
        assert status["sent_count"] == 3
        assert status["db_size_bytes"] > 0
        assert status["oldest_timestamp"] is None

    def test_with_pending_records(self, forwarder, tmp_path):
        """有待处理记录应返回正确 pending_count 与 oldest_timestamp"""
        forwarder._offline_db_path = str(tmp_path / "off.db")
        forwarder._init_offline_db()
        ts = time.time()
        forwarder._offline_db.execute(
            "INSERT INTO offline_queue (topic,payload,qos,created_at,status,priority) VALUES (?,?,?,?,?,?)",
            ("t/a", "{}", 1, ts, "pending", ""),
        )
        forwarder._offline_db.commit()
        status = forwarder.get_offline_queue_status()
        assert status["pending_count"] == 1
        assert status["oldest_timestamp"] is not None

    def test_status_exception_returns_safe(self, forwarder, tmp_path):
        """内部异常应返回安全的零值状态"""
        forwarder._offline_db_path = str(tmp_path / "off.db")
        forwarder._init_offline_db()
        forwarder._offline_db.execute("DROP TABLE offline_queue")
        forwarder._offline_db.commit()
        status = forwarder.get_offline_queue_status()
        assert status["pending_count"] == 0


class TestApiEndpoint:
    def test_no_forwarder_returns_disabled(self):
        """app.state 无 forwarder 应返回禁用状态"""
        app = make_app(router)
        client = TestClient(app)
        resp = client.get("/api/v1/mqtt-forwarder/offline-queue/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 0
        assert body["data"]["enabled"] is False
        assert body["data"]["pending_count"] == 0

    def test_with_forwarder_returns_status(self):
        """有 forwarder 应返回其状态数据"""
        mock_fwd = MagicMock()
        mock_fwd.get_offline_queue_status.return_value = {
            "enabled": True,
            "pending_count": 5,
            "sent_count": 10,
            "oldest_timestamp": 123.0,
            "db_size_bytes": 1024,
            "ring_buffer": None,
        }
        app = make_app(router, services={"mqtt_forwarder": mock_fwd})
        client = TestClient(app)
        resp = client.get("/api/v1/mqtt-forwarder/offline-queue/status")
        assert resp.status_code == 200
        assert resp.json()["data"]["pending_count"] == 5

    def test_exception_returns_500(self):
        """forwarder 抛普通异常应返回 500"""
        mock_fwd = MagicMock()
        mock_fwd.get_offline_queue_status.side_effect = RuntimeError("boom")
        app = make_app(router, services={"mqtt_forwarder": mock_fwd})
        client = TestClient(app)
        resp = client.get("/api/v1/mqtt-forwarder/offline-queue/status")
        assert resp.status_code == 500

    def test_http_exception_reraised(self):
        """forwarder 抛 HTTPException 应原样抛出"""
        mock_fwd = MagicMock()
        mock_fwd.get_offline_queue_status.side_effect = HTTPException(status_code=403, detail="forbidden")
        app = make_app(router, services={"mqtt_forwarder": mock_fwd})
        client = TestClient(app)
        resp = client.get("/api/v1/mqtt-forwarder/offline-queue/status")
        assert resp.status_code == 403
