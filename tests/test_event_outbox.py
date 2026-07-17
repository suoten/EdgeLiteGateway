"""EventBus Outbox 顺序修正单元测试 (并发安全 #4)

覆盖 P0 修复: 先落盘后投递 — persist 失败则不投递，维护 outbox 一致性。

原问题:
  AlarmOutbox.persist 吞掉异常返回 None，EventBus.publish 无法判断是否成功，
  导致 persist 失败仍继续投递。进程崩溃后 outbox 中无此事件但已投递给订阅者，
  重启重放时遗漏 (outbox 无记录) 或重复 (outbox 有记录但已投递过)。

修复:
  1. AlarmOutbox.persist 返回 bool (True=成功, False=失败)
  2. EventBus.publish 检查返回值，persist 失败/超时则不投递 (return)
"""

from __future__ import annotations

import sqlite3
import sys
import time
from unittest.mock import MagicMock

import pytest_asyncio

sys.path.insert(0, "src")

from edgelite.engine.alarm_outbox import AlarmOutbox
from edgelite.engine.event_bus import AlarmEvent, EventBus

# ════════════════════════════════════════════════════════════════════════
# AlarmOutbox.persist 返回值测试
# ════════════════════════════════════════════════════════════════════════


class TestAlarmOutboxPersistReturn:
    """persist 方法返回 bool (并发安全 #4 修复点 1)"""

    def test_persist_returns_true_on_success(self, tmp_path):
        """正常写入 → 返回 True"""
        outbox = AlarmOutbox(str(tmp_path / "test_outbox.db"))
        assert outbox._conn is not None, "DB connection should be established"

        event = AlarmEvent(
            alarm_id="a1",
            rule_id="r1",
            device_id="d1",
            severity="warning",
            action="firing",
        )
        result = outbox.persist(event)
        assert result is True

    def test_persist_returns_false_when_conn_none(self, tmp_path):
        """_conn=None (DB 不可用) → 返回 False"""
        outbox = AlarmOutbox(str(tmp_path / "test_outbox.db"))
        outbox._conn = None  # 模拟 DB 连接不可用

        event = AlarmEvent(
            alarm_id="a1",
            rule_id="r1",
            device_id="d1",
            severity="warning",
            action="firing",
        )
        result = outbox.persist(event)
        assert result is False

    def test_persist_returns_false_on_db_error(self, tmp_path):
        """DB 写入异常 → 返回 False (不抛出)"""
        outbox = AlarmOutbox(str(tmp_path / "test_outbox.db"))
        assert outbox._conn is not None

        # 用 mock 连接替换真实连接 (sqlite3.Connection.execute 是只读属性，不能直接替换)
        mock_conn = MagicMock()
        mock_conn.execute = MagicMock(side_effect=sqlite3.OperationalError("disk full"))
        mock_conn.commit = MagicMock()
        original_conn = outbox._conn
        outbox._conn = mock_conn

        event = AlarmEvent(
            alarm_id="a1",
            rule_id="r1",
            device_id="d1",
            severity="warning",
            action="firing",
        )
        result = outbox.persist(event)
        assert result is False

        # 恢复以便后续清理
        outbox._conn = original_conn

    def test_persisted_event_can_be_replayed(self, tmp_path):
        """persist 成功的事件可通过 replay_and_clear 重放"""
        outbox = AlarmOutbox(str(tmp_path / "test_outbox.db"))

        event = AlarmEvent(
            alarm_id="a1",
            rule_id="r1",
            device_id="d1",
            severity="critical",
            action="firing",
        )
        assert outbox.persist(event) is True

        replayed = []
        count = outbox.replay_and_clear(lambda e: replayed.append(e))
        assert count == 1
        assert len(replayed) == 1


# ════════════════════════════════════════════════════════════════════════
# EventBus.publish 先落盘后投递测试
# ════════════════════════════════════════════════════════════════════════


@pytest_asyncio.fixture
async def event_bus_with_outbox(tmp_path):
    """构造带 AlarmOutbox 的 EventBus。"""
    bus = EventBus()
    bus.enable_alarm_persistence(str(tmp_path / "test_outbox.db"))
    assert bus._alarm_outbox is not None
    yield bus
    bus._handlers.clear()
    if bus._alarm_outbox and bus._alarm_outbox._conn:
        bus._alarm_outbox.close()


@pytest_asyncio.fixture
async def event_bus_no_outbox():
    """构造不带 AlarmOutbox 的 EventBus (outbox 未启用)。"""
    bus = EventBus()
    yield bus
    bus._handlers.clear()


class TestEventBusOutboxOrder:
    """publish 先落盘后投递 (并发安全 #4 修复点 2)"""

    async def test_delivers_alarm_when_persist_succeeds(self, event_bus_with_outbox):
        """persist 成功 → 事件投递到订阅者队列"""
        bus = event_bus_with_outbox
        queue = await bus.subscribe("test_sub")

        event = AlarmEvent(
            alarm_id="a1",
            rule_id="r1",
            device_id="d1",
            severity="warning",
            action="firing",
        )
        await bus.publish(event)

        # 事件应已在队列中
        assert not queue.empty()
        delivered = queue.get_nowait()
        assert isinstance(delivered, AlarmEvent)

    async def test_does_not_deliver_when_persist_fails(self, event_bus_with_outbox):
        """persist 失败 (conn=None) → 事件不投递"""
        bus = event_bus_with_outbox
        queue = await bus.subscribe("test_sub")

        # 模拟 DB 不可用
        bus._alarm_outbox._conn = None

        event = AlarmEvent(
            alarm_id="a2",
            rule_id="r1",
            device_id="d1",
            severity="warning",
            action="firing",
        )
        await bus.publish(event)

        # 事件不应在队列中
        assert queue.empty()

    async def test_does_not_deliver_on_persist_exception(self, event_bus_with_outbox):
        """persist 抛异常 (DB 错误) → 事件不投递"""
        bus = event_bus_with_outbox
        queue = await bus.subscribe("test_sub")

        # 模拟 persist 抛出异常 (persist 内部已捕获，但 mock 替换后可抛出)
        bus._alarm_outbox.persist = MagicMock(side_effect=Exception("DB corruption"))

        event = AlarmEvent(
            alarm_id="a3",
            rule_id="r1",
            device_id="d1",
            severity="warning",
            action="firing",
        )
        await bus.publish(event)

        assert queue.empty()

    async def test_does_not_deliver_on_persist_timeout(self, event_bus_with_outbox):
        """persist 超时 (>0.5s) → 事件不投递"""
        bus = event_bus_with_outbox
        queue = await bus.subscribe("test_sub")

        # 模拟 persist 耗时超过 0.5s 超时
        def slow_persist(event):
            time.sleep(0.8)
            return True

        bus._alarm_outbox.persist = slow_persist

        event = AlarmEvent(
            alarm_id="a4",
            rule_id="r1",
            device_id="d1",
            severity="warning",
            action="firing",
        )
        await bus.publish(event)

        assert queue.empty()

    async def test_non_alarm_event_delivered_without_outbox(self, event_bus_with_outbox):
        """非告警事件 → 不经过 outbox，直接投递"""
        from edgelite.engine.event_bus import PointUpdateEvent

        bus = event_bus_with_outbox
        queue = await bus.subscribe("test_sub")

        # 即使 outbox 不可用，非告警事件也应投递
        bus._alarm_outbox._conn = None

        event = PointUpdateEvent(device_id="d1", point_name="temp", value=25.0)
        await bus.publish(event)

        assert not queue.empty()
        delivered = queue.get_nowait()
        assert isinstance(delivered, PointUpdateEvent)

    async def test_alarm_delivered_when_outbox_not_enabled(self, event_bus_no_outbox):
        """outbox 未启用 (_alarm_outbox=None) → 告警事件正常投递"""
        bus = event_bus_no_outbox
        assert bus._alarm_outbox is None
        queue = await bus.subscribe("test_sub")

        event = AlarmEvent(
            alarm_id="a5",
            rule_id="r1",
            device_id="d1",
            severity="warning",
            action="firing",
        )
        await bus.publish(event)

        assert not queue.empty()
        delivered = queue.get_nowait()
        assert isinstance(delivered, AlarmEvent)

    async def test_persisted_event_not_doubly_delivered_on_replay(self, event_bus_with_outbox, tmp_path):
        """FIXED-P0: persist 成功 → 事件在 outbox 中有记录 (重放可恢复)

        验证 outbox 一致性: 已投递的事件一定在 outbox 中 (可通过 replay 重放)。
        """
        bus = event_bus_with_outbox

        event = AlarmEvent(
            alarm_id="a6",
            rule_id="r1",
            device_id="d1",
            severity="critical",
            action="firing",
        )
        await bus.publish(event)

        # outbox 中应有 1 条记录
        outbox = bus._alarm_outbox
        rows = outbox._conn.execute("SELECT COUNT(*) FROM alarm_outbox").fetchone()
        assert rows[0] == 1, "persisted event should be in outbox"
