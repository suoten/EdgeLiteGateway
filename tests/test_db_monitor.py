"""数据库监控器测试 - 连接池统计与慢查询计数

覆盖 services/db_monitor.py：
- DatabaseMonitor: 单例工厂、慢查询计数、连接池统计
- 线程安全：threading.Lock 保护并发修改
"""

from __future__ import annotations

import pytest

from edgelite.services.db_monitor import DatabaseMonitor, get_db_monitor


class TestDatabaseMonitorSingleton:
    def test_get_db_monitor_returns_singleton(self):
        """get_db_monitor 应返回全局单例"""
        m1 = get_db_monitor()
        m2 = get_db_monitor()
        assert m1 is m2

    def test_get_db_monitor_creates_instance(self):
        """首次调用应创建 DatabaseMonitor 实例"""
        m = get_db_monitor()
        assert isinstance(m, DatabaseMonitor)


class TestDatabaseMonitorInit:
    def test_initial_state(self):
        """新建实例初始状态应为零值"""
        m = DatabaseMonitor()
        assert m._database is None
        assert m._running is False
        assert m._slow_queries == 0
        assert m._active_connections == 0
        assert m._idle_connections == 0
        assert m._waiting_count == 0

    def test_set_database(self):
        """set_database 应存储数据库实例引用"""
        m = DatabaseMonitor()
        fake_db = object()
        m.set_database(fake_db)
        assert m._database is fake_db


class TestDatabaseMonitorSlowQueries:
    def test_record_slow_query_increments(self):
        """record_slow_query 应递增计数"""
        m = DatabaseMonitor()
        assert m.get_slow_query_count() == 0
        m.record_slow_query()
        assert m.get_slow_query_count() == 1
        m.record_slow_query()
        m.record_slow_query()
        assert m.get_slow_query_count() == 3

    def test_record_slow_query_thread_safe(self):
        """并发调用 record_slow_query 应准确计数（线程安全）"""
        import threading

        m = DatabaseMonitor()
        iterations = 100

        def worker():
            for _ in range(iterations):
                m.record_slow_query()

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert m.get_slow_query_count() == 10 * iterations


class TestDatabaseMonitorPoolStats:
    def test_get_pool_stats_initial(self):
        """初始连接池统计应为零"""
        m = DatabaseMonitor()
        stats = m.get_pool_stats()
        assert stats == {
            "active_connections": 0,
            "idle_connections": 0,
            "waiting_count": 0,
        }

    def test_update_pool_stats(self):
        """update_pool_stats 应更新连接池统计"""
        m = DatabaseMonitor()
        m.update_pool_stats(active=5, idle=3, waiting=2)
        stats = m.get_pool_stats()
        assert stats["active_connections"] == 5
        assert stats["idle_connections"] == 3
        assert stats["waiting_count"] == 2

    def test_update_pool_stats_overwrites(self):
        """重复调用 update_pool_stats 应覆盖旧值"""
        m = DatabaseMonitor()
        m.update_pool_stats(active=10, idle=5, waiting=3)
        m.update_pool_stats(active=2, idle=1, waiting=0)
        stats = m.get_pool_stats()
        assert stats["active_connections"] == 2
        assert stats["idle_connections"] == 1
        assert stats["waiting_count"] == 0


class TestDatabaseMonitorLifecycle:
    @pytest.mark.asyncio
    async def test_start_sets_running(self):
        """start 应设置 _running=True"""
        m = DatabaseMonitor()
        await m.start()
        assert m._running is True

    @pytest.mark.asyncio
    async def test_stop_clears_running(self):
        """stop 应设置 _running=False"""
        m = DatabaseMonitor()
        await m.start()
        await m.stop()
        assert m._running is False

    @pytest.mark.asyncio
    async def test_start_stop_idempotent(self):
        """重复 start/stop 不应抛异常"""
        m = DatabaseMonitor()
        await m.start()
        await m.start()
        await m.stop()
        await m.stop()
        assert m._running is False
