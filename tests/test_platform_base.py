"""北向平台对接抽象基类测试 - 离线缓存与重连退避

覆盖 platform/base.py：
- PlatformHandler (ABC): is_connected / _enqueue_offline / _flush_offline_queue
- reconnect_with_backoff: 指数退避 + 抖动 + 重连成功后重置退避 + flush 离线队列
- 离线队列容量上限与丢弃最旧策略
- _flush_offline_queue: QueueFull 时剩余条目重新入队，避免数据丢失
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from edgelite.platform.base import PlatformHandler


class FakePlatformHandler(PlatformHandler):
    """测试用的 PlatformHandler 具体实现，可控制 connect 成功/失败"""

    platform_name = "fake"
    platform_version = "2.0.0"
    config_schema = {"broker": "str"}

    def __init__(self):
        super().__init__()
        self.connect_should_fail = False
        self.connect_call_count = 0
        self.published: list[tuple[str, str, dict]] = []
        self.rpc_callback: Any = None

    async def connect(self, config: dict) -> None:
        self.connect_call_count += 1
        if self.connect_should_fail:
            raise ConnectionError("connect failed")
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def publish_telemetry(self, device_id: str, data: dict[str, Any]) -> None:
        self.published.append(("telemetry", device_id, data))

    async def publish_attributes(self, device_id: str, attrs: dict[str, Any]) -> None:
        self.published.append(("attributes", device_id, attrs))

    async def on_rpc_request(self, callback) -> None:
        self.rpc_callback = callback

    async def publish_device_status(self, device_id: str, online: bool) -> None:
        self.published.append(("status", device_id, {"online": online}))


class TestPlatformHandlerInit:
    def test_initial_state_disconnected(self):
        """新实例应处于未连接状态"""
        p = FakePlatformHandler()
        assert p.is_connected is False
        assert p._connected is False

    def test_initial_offline_queue_empty(self):
        """新实例离线队列应为空"""
        p = FakePlatformHandler()
        assert len(p._offline_queue) == 0

    def test_default_backoff(self):
        """默认重连退避基数应为 1.0"""
        p = FakePlatformHandler()
        assert p._reconnect_backoff == 1.0

    def test_class_attributes(self):
        """类属性应有默认值"""
        assert FakePlatformHandler.platform_name == "fake"
        assert FakePlatformHandler.platform_version == "2.0.0"


class TestEnqueueOffline:
    def test_enqueue_single(self):
        """_enqueue_offline 应将消息加入队列"""
        p = FakePlatformHandler()
        p._enqueue_offline("topic/a", b"payload1", 1)
        assert len(p._offline_queue) == 1
        assert p._offline_queue[0] == ("topic/a", b"payload1", 1)

    def test_enqueue_multiple_preserves_order(self):
        """多次入队应保持 FIFO 顺序"""
        p = FakePlatformHandler()
        p._enqueue_offline("t1", b"p1", 0)
        p._enqueue_offline("t2", b"p2", 1)
        p._enqueue_offline("t3", b"p3", 2)
        assert list(p._offline_queue) == [("t1", b"p1", 0), ("t2", b"p2", 1), ("t3", b"p3", 2)]

    def test_enqueue_drops_oldest_when_full(self):
        """队列满时应丢弃最旧的条目"""
        p = FakePlatformHandler()
        p._offline_queue_max = 3
        p._enqueue_offline("t1", b"p1", 0)
        p._enqueue_offline("t2", b"p2", 0)
        p._enqueue_offline("t3", b"p3", 0)
        p._enqueue_offline("t4", b"p4", 0)  # 应丢弃 t1
        assert len(p._offline_queue) == 3
        assert p._offline_queue[0] == ("t2", b"p2", 0)
        assert p._offline_queue[2] == ("t4", b"p4", 0)


class TestFlushOfflineQueue:
    @pytest.mark.asyncio
    async def test_flush_empty_queue_noop(self):
        """空队列 flush 应为 no-op"""
        p = FakePlatformHandler()
        await p._flush_offline_queue()  # 不应抛异常

    @pytest.mark.asyncio
    async def test_flush_with_pub_queue(self):
        """有 _pub_queue 时应将消息转移到 pub_queue"""
        p = FakePlatformHandler()
        p._pub_queue = asyncio.Queue(maxsize=10)
        p._enqueue_offline("t1", b"p1", 1)
        p._enqueue_offline("t2", b"p2", 1)
        await p._flush_offline_queue()
        assert len(p._offline_queue) == 0
        assert p._pub_queue.qsize() == 2

    @pytest.mark.asyncio
    async def test_flush_without_pub_queue_skips(self):
        """无 _pub_queue 时应跳过但不丢失（队列被清空）"""
        p = FakePlatformHandler()
        p._enqueue_offline("t1", b"p1", 1)
        await p._flush_offline_queue()
        # _pub_queue 不存在，条目被跳过，离线队列已清空
        assert len(p._offline_queue) == 0

    @pytest.mark.asyncio
    async def test_flush_queue_full_re_enqueues_remaining(self):
        """pub_queue 满时应将剩余条目重新放回离线队列"""
        p = FakePlatformHandler()
        p._pub_queue = asyncio.Queue(maxsize=1)  # 只能容纳1条
        p._enqueue_offline("t1", b"p1", 1)
        p._enqueue_offline("t2", b"p2", 1)
        p._enqueue_offline("t3", b"p3", 1)
        await p._flush_offline_queue()
        # 第1条入 pub_queue，第2、3条因 QueueFull 重新入离线队列
        assert p._pub_queue.qsize() == 1
        assert len(p._offline_queue) == 2


class TestReconnectWithBackoff:
    @pytest.mark.asyncio
    async def test_reconnect_success_first_try(self):
        """首次重连成功应立即返回，无退避"""
        p = FakePlatformHandler()
        p.connect_should_fail = False
        # 设置较小的退避基数避免测试慢
        p._reconnect_backoff = 0.01
        await p.reconnect_with_backoff({})
        assert p.is_connected is True
        assert p.connect_call_count == 1
        # 成功后退避应重置为 1.0
        assert p._reconnect_backoff == 1.0

    @pytest.mark.asyncio
    async def test_reconnect_retries_on_failure(self):
        """重连失败应按退避策略重试，最终成功"""
        p = FakePlatformHandler()
        p.connect_should_fail = True
        p._reconnect_backoff = 0.01

        # 0.05秒后让 connect 成功
        async def enable_connect():
            await asyncio.sleep(0.05)
            p.connect_should_fail = False

        asyncio.create_task(enable_connect())
        await p.reconnect_with_backoff({})
        assert p.is_connected is True
        assert p.connect_call_count >= 2  # 至少失败1次+成功1次

    @pytest.mark.asyncio
    async def test_reconnect_resets_backoff_on_success(self):
        """重连成功后退避基数应重置为 1.0"""
        p = FakePlatformHandler()
        p.connect_should_fail = True
        p._reconnect_backoff = 0.5

        async def enable_connect():
            await asyncio.sleep(0.05)
            p.connect_should_fail = False

        asyncio.create_task(enable_connect())
        await p.reconnect_with_backoff({})
        assert p._reconnect_backoff == 1.0


class TestPlatformHandlerAbstract:
    def test_cannot_instantiate_abstract(self):
        """PlatformHandler 是 ABC，不能直接实例化"""
        with pytest.raises(TypeError):
            PlatformHandler()  # type: ignore[abstract]

    def test_missing_abstract_method_raises(self):
        """未实现所有抽象方法的子类不能实例化"""

        class IncompletePlatform(PlatformHandler):
            async def connect(self, config: dict) -> None:
                pass

        with pytest.raises(TypeError):
            IncompletePlatform()  # type: ignore[abstract]
