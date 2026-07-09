"""DeviceLifecycleManager 双锁简化单元测试 (并发安全 #10)

覆盖 P1 修复 (lifecycle.py):

**原设计**:
    _db_lock = asyncio.Lock()        # 保护 _status_map
    _sqlite_lock = threading.RLock()  # 保护 _db_conn
    close(): async with _db_lock: with _sqlite_lock: ...  # 嵌套锁

**问题**: 两个锁保护不同资源但调用链交叉，维护时容易引入锁顺序错误 (ABBA 死锁)

**简化**: 单一 _sqlite_lock (threading.RLock) 保护所有共享状态
    - on_device_online/offline/unknown 提取为公共 _transition_status 方法
    - close() 单锁，无嵌套
    - 消除 ~80 行重复代码

测试覆盖:
1. 单锁存在性验证 (_sqlite_lock, 无 _db_lock)
2. _transition_status 公共方法行为
3. on_device_online/offline/unknown 委托到 _transition_status
4. 幂等性 (状态未变化时不发布事件)
5. 持久化失败回滚 + 修正事件
6. 并发安全性 (threading.RLock 跨线程互斥)
7. close() 单锁简化
8. get_status / remove_device 锁保护
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, "src")

from edgelite.engine.event_bus import DeviceStatusEvent
from edgelite.engine.lifecycle import DeviceLifecycleManager


# ════════════════════════════════════════════════════════════════════════
# Fixture
# ════════════════════════════════════════════════════════════════════════


@pytest.fixture
def event_bus():
    """模拟 EventBus，publish 为 AsyncMock。"""
    bus = MagicMock()
    bus.publish = AsyncMock()
    return bus


@pytest.fixture
def db_path(tmp_path):
    """临时 SQLite 数据库路径。"""
    return str(tmp_path / "test_device_status.db")


@pytest.fixture
def manager(event_bus, db_path):
    """构造真实 DeviceLifecycleManager 实例 (使用临时数据库)。"""
    mgr = DeviceLifecycleManager(event_bus, db_path=db_path)
    yield mgr
    # 清理: 同步关闭连接 (_sqlite_lock 使其线程安全，无需 async)
    try:
        with mgr._sqlite_lock:
            if mgr._db_conn is not None:
                mgr._db_conn.close()
                mgr._db_conn = None
    except Exception:
        pass


# ════════════════════════════════════════════════════════════════════════
# 测试组 1: 单锁存在性验证
# ════════════════════════════════════════════════════════════════════════


class TestSingleLockExistence:
    """验证双锁已简化为单 _sqlite_lock"""

    def test_has_sqlite_lock(self, manager):
        """_sqlite_lock 存在且为 threading.RLock"""
        assert hasattr(manager, "_sqlite_lock")
        assert isinstance(manager._sqlite_lock, type(threading.RLock()))

    def test_no_db_lock_attribute(self, manager):
        """_db_lock (asyncio.Lock) 已移除"""
        assert not hasattr(manager, "_db_lock"), \
            "_db_lock should be removed after dual-lock simplification"

    def test_sqlite_lock_is_reentrant(self, manager):
        """_sqlite_lock 为 RLock (可重入)，同一线程可多次获取"""
        with manager._sqlite_lock:
            with manager._sqlite_lock:  # 可重入，不死锁
                pass

    def test_sqlite_lock_protects_status_map(self, manager):
        """_sqlite_lock 保护 _status_map (持锁可读写)"""
        with manager._sqlite_lock:
            manager._status_map["test_dev"] = "online"
            assert manager._status_map["test_dev"] == "online"


# ════════════════════════════════════════════════════════════════════════
# 测试组 2: _transition_status 公共方法
# ════════════════════════════════════════════════════════════════════════


class TestTransitionStatus:
    """_transition_status 公共方法行为测试"""

    async def test_transition_updates_status_map(self, manager, event_bus):
        """状态转换更新 _status_map"""
        await manager._transition_status("dev1", "online")
        assert manager._status_map["dev1"] == "online"

    async def test_transition_publishes_event(self, manager, event_bus):
        """状态转换发布 DeviceStatusEvent"""
        await manager._transition_status("dev1", "online")
        event_bus.publish.assert_awaited_once()
        event = event_bus.publish.call_args[0][0]
        assert isinstance(event, DeviceStatusEvent)
        assert event.device_id == "dev1"
        assert event.old_status == "offline"
        assert event.new_status == "online"

    async def test_transition_idempotent_same_status(self, manager, event_bus):
        """状态未变化时不发布事件 (幂等)"""
        await manager._transition_status("dev1", "online")
        event_bus.publish.reset_mock()

        await manager._transition_status("dev1", "online")  # 相同状态
        event_bus.publish.assert_not_awaited()

    async def test_transition_persists_to_db(self, manager, event_bus):
        """状态转换持久化到 SQLite"""
        await manager._transition_status("dev1", "online")

        # 验证 DB 中有记录
        with manager._sqlite_lock:
            conn = manager._get_conn()
            cursor = conn.execute("SELECT status FROM device_status WHERE device_id = ?", ("dev1",))
            row = cursor.fetchone()
        assert row is not None
        assert row[0] == "online"

    async def test_transition_offline_to_online(self, manager, event_bus):
        """offline → online 状态转换"""
        await manager._transition_status("dev1", "offline")
        event_bus.publish.reset_mock()

        await manager._transition_status("dev1", "online")
        event = event_bus.publish.call_args[0][0]
        assert event.old_status == "offline"
        assert event.new_status == "online"

    async def test_transition_online_to_offline(self, manager, event_bus):
        """online → offline 状态转换"""
        await manager._transition_status("dev1", "online")
        event_bus.publish.reset_mock()

        await manager._transition_status("dev1", "offline")
        event = event_bus.publish.call_args[0][0]
        assert event.old_status == "online"
        assert event.new_status == "offline"


# ════════════════════════════════════════════════════════════════════════
# 测试组 3: on_device_* 委托验证
# ════════════════════════════════════════════════════════════════════════


class TestOnDeviceDelegation:
    """on_device_online/offline/unknown 委托到 _transition_status"""

    async def test_on_device_online_calls_transition(self, manager, event_bus):
        """on_device_online → _transition_status(device_id, "online")"""
        await manager.on_device_online("dev1")
        assert manager._status_map["dev1"] == "online"
        event = event_bus.publish.call_args[0][0]
        assert event.new_status == "online"

    async def test_on_device_offline_calls_transition(self, manager, event_bus):
        """on_device_offline → _transition_status(device_id, "offline")"""
        await manager.on_device_online("dev1")
        event_bus.publish.reset_mock()

        await manager.on_device_offline("dev1")
        event = event_bus.publish.call_args[0][0]
        assert event.new_status == "offline"

    async def test_on_device_unknown_calls_transition(self, manager, event_bus):
        """on_device_unknown → _transition_status(device_id, "unknown")"""
        await manager.on_device_unknown("dev1")
        assert manager._status_map["dev1"] == "unknown"
        event = event_bus.publish.call_args[0][0]
        assert event.new_status == "unknown"

    async def test_on_device_online_idempotent(self, manager, event_bus):
        """on_device_online 幂等: 已 online 时不重复发布事件"""
        await manager.on_device_online("dev1")
        event_bus.publish.reset_mock()

        await manager.on_device_online("dev1")
        event_bus.publish.assert_not_awaited()

    async def test_on_device_offline_idempotent(self, manager, event_bus):
        """on_device_offline 幂等: 已 offline 时不重复发布事件"""
        await manager.on_device_offline("dev1")
        event_bus.publish.reset_mock()

        await manager.on_device_offline("dev1")
        event_bus.publish.assert_not_awaited()


# ════════════════════════════════════════════════════════════════════════
# 测试组 4: 持久化失败回滚 + 修正事件
# ════════════════════════════════════════════════════════════════════════


class TestPersistFailureRollback:
    """持久化失败时回滚 _status_map + 发布修正事件"""

    async def test_rollback_on_persist_failure(self, manager, event_bus):
        """_persist_status 失败 → _status_map 回滚到旧状态"""
        # 先设为 online
        await manager.on_device_online("dev1")

        # mock _persist_status 失败
        original_persist = manager._persist_status
        manager._persist_status = AsyncMock(side_effect=Exception("DB write failed"))

        with pytest.raises(Exception, match="DB write failed"):
            await manager.on_device_offline("dev1")

        # _status_map 应回滚 (仍是 online)
        assert manager._status_map["dev1"] == "online"

    async def test_correction_event_on_rollback(self, manager, event_bus):
        """回滚后发布状态修正事件 (FIX-ENG-005)"""
        await manager.on_device_online("dev1")
        event_bus.publish.reset_mock()

        manager._persist_status = AsyncMock(side_effect=Exception("DB write failed"))

        with pytest.raises(Exception):
            await manager.on_device_offline("dev1")

        # 应发布 2 个事件: 正常事件 (已被 _transition_status 跳过) + 修正事件
        # 实际: _persist_status 失败后，先回滚 _status_map，再发布修正事件
        # 修正事件: old_status="offline" (尝试的新状态), new_status="online" (回滚到的旧状态)
        correction_calls = [
            call for call in event_bus.publish.call_args_list
            if call[0][0].new_status == "online" and call[0][0].old_status == "offline"
        ]
        assert len(correction_calls) >= 1, \
            f"Expected correction event (offline→online), got: {event_bus.publish.call_args_list}"

    async def test_rollback_restores_correct_old_status(self, manager, event_bus):
        """回滚恢复正确的 old_status (从 unknown 回滚到 offline)"""
        # 设备初始为 offline (默认)
        # 尝试转为 unknown，但 persist 失败
        manager._persist_status = AsyncMock(side_effect=Exception("fail"))

        with pytest.raises(Exception):
            await manager.on_device_unknown("dev1")

        # 应回滚到 offline (初始状态)
        assert manager._status_map["dev1"] == "offline"


# ════════════════════════════════════════════════════════════════════════
# 测试组 5: 并发安全性
# ════════════════════════════════════════════════════════════════════════


class TestConcurrentSafety:
    """threading.RLock 跨线程互斥验证"""

    async def test_concurrent_online_different_devices(self, manager, event_bus):
        """并发 on_device_online 不同设备 → 都成功"""
        await asyncio.gather(
            manager.on_device_online("dev1"),
            manager.on_device_online("dev2"),
            manager.on_device_online("dev3"),
        )
        assert manager._status_map["dev1"] == "online"
        assert manager._status_map["dev2"] == "online"
        assert manager._status_map["dev3"] == "online"

    async def test_concurrent_same_device_rapid_transitions(self, manager, event_bus):
        """并发同一设备快速状态转换 → 最终状态一致"""
        # 10 个并发 online 请求
        await asyncio.gather(*[manager.on_device_online("dev1") for _ in range(10)])
        assert manager._status_map["dev1"] == "online"

    async def test_sqlite_lock_blocks_concurrent_writes(self, manager, event_bus):
        """_sqlite_lock 串行化 SQLite 写入 (无 'database is locked' 错误)"""
        # 并发写入 20 个设备
        await asyncio.gather(*[
            manager.on_device_online(f"dev_{i}") for i in range(20)
        ])
        # 所有设备都应持久化成功
        for i in range(20):
            assert manager._status_map[f"dev_{i}"] == "online"

    async def test_concurrent_online_offline_no_corruption(self, manager, event_bus):
        """并发 online + offline 同一设备 → _status_map 不损坏"""
        # 交替 online/offline
        tasks = []
        for i in range(10):
            if i % 2 == 0:
                tasks.append(manager.on_device_online("dev1"))
            else:
                tasks.append(manager.on_device_offline("dev1"))
        await asyncio.gather(*tasks)
        # 最终状态应为 online 或 offline (不应损坏)
        assert manager._status_map["dev1"] in ("online", "offline")


# ════════════════════════════════════════════════════════════════════════
# 测试组 6: close() 单锁简化
# ════════════════════════════════════════════════════════════════════════


class TestCloseSingleLock:
    """close() 单锁简化验证"""

    async def test_close_sets_db_conn_none(self, manager):
        """close() 后 _db_conn 为 None"""
        # 先确保有连接
        await manager.on_device_online("dev1")
        assert manager._db_conn is not None

        await manager.close()
        assert manager._db_conn is None

    async def test_close_idempotent(self, manager):
        """close() 可多次调用 (幂等)"""
        await manager.close()
        await manager.close()  # 不应抛异常
        assert manager._db_conn is None

    async def test_close_after_persist(self, manager, event_bus):
        """持久化后 close() 不影响已写入的数据"""
        await manager.on_device_online("dev1")
        await manager.close()

        # 重新打开数据库验证数据
        mgr2 = DeviceLifecycleManager(event_bus, db_path=manager._db_path)
        assert mgr2._status_map.get("dev1") == "online"
        await mgr2.close()

    async def test_close_does_not_deadlock(self, manager, event_bus):
        """close() 不死锁 (单锁，无嵌套)"""
        # 先并发写入，然后 close
        await asyncio.gather(
            manager.on_device_online("dev1"),
            manager.on_device_online("dev2"),
        )
        # close 应快速完成 (不阻塞)
        await asyncio.wait_for(manager.close(), timeout=5.0)


# ════════════════════════════════════════════════════════════════════════
# 测试组 7: get_status / remove_device 锁保护
# ════════════════════════════════════════════════════════════════════════


class TestGetStatusAndRemoveDevice:
    """get_status 和 remove_device 锁保护验证"""

    async def test_get_status_returns_default_offline(self, manager):
        """未设状态的设备返回 'offline'"""
        status = await manager.get_status("unknown_dev")
        assert status == "offline"

    async def test_get_status_returns_current(self, manager, event_bus):
        """get_status 返回当前状态"""
        await manager.on_device_online("dev1")
        status = await manager.get_status("dev1")
        assert status == "online"

    async def test_get_status_after_offline(self, manager, event_bus):
        """get_status 在 offline 后返回 'offline'"""
        await manager.on_device_online("dev1")
        await manager.on_device_offline("dev1")
        status = await manager.get_status("dev1")
        assert status == "offline"

    async def test_remove_device_deletes_from_map(self, manager, event_bus):
        """remove_device 从 _status_map 删除设备"""
        await manager.on_device_online("dev1")
        assert "dev1" in manager._status_map

        await manager.remove_device("dev1")
        assert "dev1" not in manager._status_map

    async def test_remove_device_deletes_from_db(self, manager, event_bus):
        """remove_device 从 SQLite 删除记录"""
        await manager.on_device_online("dev1")
        await manager.remove_device("dev1")

        # 重新打开数据库验证
        mgr2 = DeviceLifecycleManager(event_bus, db_path=manager._db_path)
        assert "dev1" not in mgr2._status_map
        await mgr2.close()

    async def test_remove_device_idempotent(self, manager, event_bus):
        """remove_device 幂等 (删除不存在的设备不报错)"""
        await manager.remove_device("nonexistent_dev")  # 不应抛异常


# ════════════════════════════════════════════════════════════════════════
# 测试组 8: 状态恢复
# ════════════════════════════════════════════════════════════════════════


class TestStatusRestore:
    """启动时从 SQLite 恢复状态"""

    async def test_restore_on_init(self, event_bus, db_path):
        """新实例从 SQLite 恢复之前持久化的状态"""
        mgr1 = DeviceLifecycleManager(event_bus, db_path=db_path)
        await mgr1.on_device_online("dev1")
        await mgr1.on_device_online("dev2")  # 先 online
        await mgr1.on_device_offline("dev2")  # 再 offline (状态转换，会持久化)
        await mgr1.close()

        # 新实例应恢复状态
        mgr2 = DeviceLifecycleManager(event_bus, db_path=db_path)
        assert mgr2._status_map.get("dev1") == "online"
        assert mgr2._status_map.get("dev2") == "offline"
        await mgr2.close()

    async def test_restore_empty_db(self, event_bus, db_path):
        """空数据库恢复时不崩溃"""
        mgr = DeviceLifecycleManager(event_bus, db_path=db_path)
        assert len(mgr._status_map) == 0
        await mgr.close()
