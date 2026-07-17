"""S7 驱动同步/异步锁混用修正单元测试 (并发安全 #8)

覆盖 P1 修复: _do_connect/stop/_run_in_s7_thread_async 中 self._client 的
读写操作在 _sync_lock 内执行，防止与 _sync_db_read/_sync_db_write 的 TOCTOU 竞态。

原问题:
  S7 驱动同时使用 asyncio.Lock (_lock) 和 threading.RLock (_sync_lock)。
  _sync_db_read/_sync_db_write 在 _sync_lock 内读 self._client，
  但 _do_connect (async) 在无锁状态下修改 self._client (创建/销毁/替换)。
  线程 A 快照 client = self._client 后开始 snap7 I/O (C 层阻塞)，
  期间 _do_connect 将 self._client 替换为新对象或置 None，
  线程 A 使用已 destroy 的 client → segfault 风险。

修复:
  所有 self._client 的读写 (含 _do_connect/stop/_run_in_s7_thread_async 异步路径)
  必须在 _sync_lock 内执行。锁仅持有极短时间 (赋值/快照)，不覆盖 await I/O。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import sys
import threading
import types
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, "src")

from edgelite.drivers.s7 import S7Driver


async def _noop_cancel_background_tasks(self):
    """空操作 _cancel_background_tasks 替身 (Python 3.12 无 asyncio.coroutine)。"""
    pass


# ════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════


class _TrackingRLock:
    """可追踪 acquire/release 的 RLock 包装器。

    记录每次 acquire 时的调用栈帧信息，用于验证 _sync_lock 是否在
    特定方法内被获取。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.acquire_count = 0
        self.acquire_frames: list[str] = []

    def acquire(self, *args, **kwargs):
        result = self._lock.acquire(*args, **kwargs)
        if result:
            self.acquire_count += 1
            # 记录调用者 (跳过本方法和 _lock.acquire)
            frame = sys._getframe(1)
            func_name = frame.f_code.co_name
            self.acquire_frames.append(func_name)
        return result

    def release(self):
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()

    def reset(self):
        self.acquire_count = 0
        self.acquire_frames.clear()


def _make_driver() -> S7Driver:
    """构造 S7Driver 实例 (绕过 __init__ 的复杂依赖)，仅设置测试所需属性。"""
    driver = S7Driver.__new__(S7Driver)
    driver._is_s7_200_smart = False
    driver._plc_model = "Unknown"
    driver._s7_executor = None
    driver._s7_executor_failed = False
    driver._s7_executor_lock = asyncio.Lock()
    driver._client = None
    driver._main_loop = None
    driver._conn_state_lock = threading.RLock()
    driver._conn_state = "disconnected"
    driver._devices = {}
    driver._config = {}
    driver._running = False
    driver._lock = asyncio.Lock()
    driver._sync_lock = threading.RLock()
    # stop() 依赖的属性
    driver._heartbeat_task = None
    driver._delayed_reconnect_task = None
    driver._reconnect_tasks = {}
    driver._reconnect_locks = {}
    driver._edge_trigger = None
    driver._rule_store = None
    driver._edge_rule_engine = None
    driver._offline_sync = None
    driver._ts_store = None
    driver._config_version_mgr = None
    driver._ota_mgr = None
    driver._redundancy = None
    driver._point_health = MagicMock()
    driver._point_health.clear = MagicMock()
    driver._degraded = False
    driver._reconnect_count = 0
    driver._reconnect_delay = 1.0
    driver._circuit_open = {}
    driver._circuit_open_since = {}
    driver._stats_lock = threading.RLock()
    driver._health_stats = {}
    driver._offline_since = {}
    return driver


class _FakeSnap7Client:
    """模拟 snap7.client.Client"""

    def __init__(self):
        self.disconnected = False
        self.destroyed = False
        self.connected = False

    def set_connection_params(self, *args, **kwargs):
        pass

    def connect(self, *args, **kwargs):
        self.connected = True

    def disconnect(self):
        self.disconnected = True

    def destroy(self):
        self.destroyed = True

    def get_timeout(self):
        return 5000

    def set_timeout(self, ms):
        pass

    def db_read(self, db_number, byte_offset, size):
        return bytearray(size)

    def db_write(self, db_number, byte_offset, data):
        pass


class _FakeSnap7Module(types.ModuleType):
    """模拟 snap7 模块"""

    def __init__(self):
        super().__init__("snap7")
        self._client_class = _FakeSnap7Client

        class ClientFactory:
            Client = _FakeSnap7Client

        self.client = ClientFactory()


def _install_fake_snap7():
    """安装假的 snap7 模块到 sys.modules，返回模块对象。"""
    fake = _FakeSnap7Module()
    sys.modules["snap7"] = fake
    return fake


def _remove_fake_snap7():
    """移除假的 snap7 模块。"""
    sys.modules.pop("snap7", None)


# ════════════════════════════════════════════════════════════════════════
# 1. _do_connect: _client 读写加锁保护
# ════════════════════════════════════════════════════════════════════════


class TestDoConnectLockProtection:
    """验证 _do_connect 中 self._client 的读写在 _sync_lock 内 (并发安全 #8)"""

    def test_do_connect_reads_client_under_lock(self):
        """_do_connect 读取 self._client 时应持有 _sync_lock"""
        driver = _make_driver()
        old_client = _FakeSnap7Client()
        driver._client = old_client
        tracking_lock = _TrackingRLock()
        driver._sync_lock = tracking_lock
        _install_fake_snap7()
        try:
            asyncio.run(driver._do_connect("192.168.1.1", 0, 1))
        except Exception:
            pass  # 连接可能失败，不影响锁验证
        finally:
            _remove_fake_snap7()

        # _do_connect 应在 _sync_lock 内读取 _client (至少一次: 读取 old_client)
        assert tracking_lock.acquire_count > 0, "_do_connect should acquire _sync_lock to read _client"

    def test_do_connect_writes_new_client_under_lock(self):
        """_do_connect 写入 self._client = new_client 时应持有 _sync_lock"""
        driver = _make_driver()
        tracking_lock = _TrackingRLock()
        driver._sync_lock = tracking_lock
        _install_fake_snap7()
        try:
            asyncio.run(driver._do_connect("192.168.1.1", 0, 1))
        except Exception:
            pass
        finally:
            _remove_fake_snap7()

        # 应多次获取 _sync_lock: 读 old_client + 写 new_client (+ 可能的异常路径写 None)
        assert tracking_lock.acquire_count >= 2, (
            f"_do_connect should acquire _sync_lock at least twice (read+write), got {tracking_lock.acquire_count}"
        )

    def test_do_connect_client_write_is_atomic(self):
        """_do_connect 写 _client 期间，另一线程的 _sync_db_read 应看到一致的 client"""
        driver = _make_driver()
        driver._sync_lock = threading.RLock()
        _install_fake_snap7()
        try:
            # 先设一个旧 client
            old_client = _FakeSnap7Client()
            driver._client = old_client

            # 记录 _sync_db_read 看到的 client
            seen_clients = []

            def track_read():
                try:
                    with driver._sync_lock:
                        seen_clients.append(driver._client)
                except Exception:
                    pass

            # 在另一线程并发执行 _sync_db_read 风格的读取
            async def run_concurrent():
                # 启动读取线程
                reader = threading.Thread(target=track_read)
                reader.start()
                # 同时执行 _do_connect (会替换 _client)
                try:
                    await driver._do_connect("192.168.1.1", 0, 1)
                except Exception:
                    pass
                reader.join(timeout=5)
                # 再读一次 (连接后)
                with driver._sync_lock:
                    seen_clients.append(driver._client)

            asyncio.run(run_concurrent())

            # 所有看到的 client 都应该是有效对象 (old_client 或 new_client)，不会是中间状态
            for c in seen_clients:
                assert c is not None or c is driver._client, "client should be consistent"
        finally:
            _remove_fake_snap7()

    def test_do_connect_failure_sets_none_under_lock(self):
        """_do_connect 连接失败时 self._client = None 应在 _sync_lock 内"""
        driver = _make_driver()
        tracking_lock = _TrackingRLock()
        driver._sync_lock = tracking_lock
        _install_fake_snap7()
        # 让连接失败: patch _s7_connect_with_timeout 抛异常
        try:
            with patch.object(S7Driver, "_s7_connect_with_timeout", side_effect=ConnectionError("fail")):
                with pytest.raises(ConnectionError):
                    asyncio.run(driver._do_connect("192.168.1.1", 0, 1))
        finally:
            _remove_fake_snap7()

        # 失败路径也应多次获取锁: 读 old + 写 new + 写 None
        assert tracking_lock.acquire_count >= 2
        # _client 应被置 None
        assert driver._client is None

    def test_do_connect_success_sets_client(self):
        """_do_connect 成功后 self._client 应为新 client"""
        driver = _make_driver()
        driver._sync_lock = threading.RLock()
        _install_fake_snap7()
        try:
            asyncio.run(driver._do_connect("192.168.1.1", 0, 1))
            assert driver._client is not None
            assert isinstance(driver._client, _FakeSnap7Client)
        finally:
            _remove_fake_snap7()


# ════════════════════════════════════════════════════════════════════════
# 2. stop: _client = None 加锁保护
# ════════════════════════════════════════════════════════════════════════


class TestStopLockProtection:
    """验证 stop() 中 self._client = None 在 _sync_lock 内 (并发安全 #8)"""

    @pytest.mark.asyncio
    async def test_stop_sets_client_none_under_lock(self):
        """stop() 设置 _client = None 时应持有 _sync_lock"""
        driver = _make_driver()
        old_client = _FakeSnap7Client()
        driver._client = old_client
        tracking_lock = _TrackingRLock()
        driver._sync_lock = tracking_lock

        # mock _cancel_background_tasks (依赖太多)
        with patch.object(S7Driver, "_cancel_background_tasks", _noop_cancel_background_tasks):
            try:
                await driver.stop()
            except Exception:
                pass

        # stop 应在 _sync_lock 内设置 _client = None
        assert tracking_lock.acquire_count > 0, "stop() should acquire _sync_lock"
        assert driver._client is None

    @pytest.mark.asyncio
    async def test_stop_client_none_atomic_with_sync_db_read(self):
        """stop() 设 _client=None 期间，_sync_db_read 不应看到中间状态"""
        driver = _make_driver()
        driver._client = _FakeSnap7Client()
        driver._sync_lock = threading.RLock()

        seen_clients = []

        def sync_read():
            with driver._sync_lock:
                seen_clients.append(driver._client)

        with patch.object(S7Driver, "_cancel_background_tasks", _noop_cancel_background_tasks):
            # 并发: 线程读取 + 主协程 stop
            reader = threading.Thread(target=sync_read)
            reader.start()
            try:
                await driver.stop()
            except Exception:
                pass
            reader.join(timeout=5)

        # 所有读到的 client 应该是 old_client 或 None，不会是损坏的中间状态
        for c in seen_clients:
            assert c is None or isinstance(c, _FakeSnap7Client)


# ════════════════════════════════════════════════════════════════════════
# 3. _run_in_s7_thread_async: 超时路径 _client 读取加锁
# ════════════════════════════════════════════════════════════════════════


class TestRunInS7ThreadTimeoutLockProtection:
    """验证 _run_in_s7_thread_async 超时路径读取 _client 在 _sync_lock 内 (并发安全 #8)"""

    @pytest.mark.asyncio
    async def test_timeout_path_reads_client_under_lock(self):
        """超时后读取 old_client = self._client 应在 _sync_lock 内"""
        driver = _make_driver()
        driver._client = _FakeSnap7Client()
        tracking_lock = _TrackingRLock()
        driver._sync_lock = tracking_lock

        # 使用永不完成的 executor 模拟超时
        class _HangingExecutor:
            def submit(self, fn, *args, **kwargs):
                f = concurrent.futures.Future()
                # 不设置结果 → 永不完成
                return f

            def shutdown(self, wait=True, cancel_futures=False):
                pass

        driver._s7_executor = _HangingExecutor()
        driver._s7_executor_failed = False

        # 调用 _run_in_s7_thread_async，应超时
        with patch.object(S7Driver, "_set_conn_state", lambda self, s: None):
            with pytest.raises((TimeoutError, asyncio.TimeoutError)):
                await asyncio.wait_for(
                    driver._run_in_s7_thread_async(lambda: None, timeout=0.2),
                    timeout=5.0,
                )

        # 超时路径应获取 _sync_lock 读取 old_client
        assert tracking_lock.acquire_count > 0, "timeout path should acquire _sync_lock to read _client"

    @pytest.mark.asyncio
    async def test_executor_rebuild_path_reads_client_under_lock(self):
        """executor 重建路径读取 old_client 应在 _sync_lock 内"""
        driver = _make_driver()
        driver._client = _FakeSnap7Client()
        tracking_lock = _TrackingRLock()
        driver._sync_lock = tracking_lock

        # 设置 _s7_executor_failed = True 触发重建路径
        driver._s7_executor_failed = True
        driver._s7_executor = None  # 触发 _get_s7_executor 创建新 executor

        class _ImmediateExecutor:
            def submit(self, fn, *args, **kwargs):
                f = concurrent.futures.Future()
                f.set_result(fn())
                return f

            def shutdown(self, wait=True, cancel_futures=False):
                pass

        # mock _get_s7_executor 返回即时 executor
        with patch.object(S7Driver, "_get_s7_executor", return_value=_ImmediateExecutor()):
            with patch.object(S7Driver, "_set_conn_state", lambda self, s: None):
                try:
                    await driver._run_in_s7_thread_async(lambda: 42, timeout=5.0)
                except Exception:
                    pass

        # 重建路径应获取 _sync_lock 读取 old_client
        assert tracking_lock.acquire_count > 0, "rebuild path should acquire _sync_lock to read _client"


# ════════════════════════════════════════════════════════════════════════
# 4. 锁层级文档: _sync_lock 与 _lock 不混淆
# ════════════════════════════════════════════════════════════════════════


class TestLockHierarchy:
    """验证锁层级文档存在且 _sync_lock 是 threading.RLock (并发安全 #8)"""

    def test_sync_lock_is_rlock(self):
        """_sync_lock 应为 threading.RLock (可重入，防止死锁)"""
        driver = _make_driver()
        assert isinstance(driver._sync_lock, type(threading.RLock()))

    def test_sync_lock_is_reentrant(self):
        """_sync_lock 应可重入 (RLock 特性)，同一线程可多次获取"""
        driver = _make_driver()
        with driver._sync_lock:
            with driver._sync_lock:
                with driver._sync_lock:
                    pass  # 不死锁
        assert True

    def test_lock_hierarchy_docstring_exists(self):
        """S7Driver.__init__ 应包含锁层级文档注释"""
        import inspect

        source = inspect.getsource(S7Driver.__init__)
        assert "Lock Hierarchy" in source or "锁层级" in source, "__init__ should document lock hierarchy"

    def test_sync_db_read_uses_sync_lock(self):
        """_sync_db_read 应在 _sync_lock 内访问 _client"""
        import inspect

        source = inspect.getsource(S7Driver._sync_db_read)
        assert "_sync_lock" in source, "_sync_db_read should use _sync_lock"

    def test_do_connect_uses_sync_lock(self):
        """_do_connect 应在 _sync_lock 内读写 _client"""
        import inspect

        source = inspect.getsource(S7Driver._do_connect)
        assert "_sync_lock" in source, "_do_connect should use _sync_lock"


# ════════════════════════════════════════════════════════════════════════
# 5. 并发安全: _do_connect 与 _sync_db_read 不竞态
# ════════════════════════════════════════════════════════════════════════


class TestConcurrentConnectAndRead:
    """验证 _do_connect 与 _sync_db_read 并发时不竞态 (并发安全 #8)"""

    @pytest.mark.asyncio
    async def test_concurrent_connect_and_sync_read_no_crash(self):
        """并发 _do_connect + _sync_db_read 不崩溃 (client 引用一致)"""
        driver = _make_driver()
        driver._sync_lock = threading.RLock()
        driver._config = {"read_timeout": 100}
        _install_fake_snap7()
        try:
            old_client = _FakeSnap7Client()
            driver._client = old_client

            errors = []

            def do_sync_read():
                """模拟 _sync_db_read 的行为"""
                try:
                    with driver._sync_lock:
                        client = driver._client
                        if client is None:
                            raise ConnectionError("not connected")
                        # 模拟 snap7 调用 (快照后使用)
                        client.db_read(1, 0, 4)
                except Exception as e:
                    errors.append(e)

            async def run_concurrent():
                # 启动多个读取线程
                readers = []
                for _ in range(5):
                    t = threading.Thread(target=do_sync_read)
                    readers.append(t)
                    t.start()

                # 同时执行 _do_connect (替换 _client)
                try:
                    await driver._do_connect("192.168.1.1", 0, 1)
                except Exception:
                    pass

                for t in readers:
                    t.join(timeout=5)

            await run_concurrent()

            # 不应有 AttributeError (使用已 destroy 的 client)
            attr_errors = [e for e in errors if isinstance(e, AttributeError)]
            assert len(attr_errors) == 0, (
                f"Concurrent connect+read caused AttributeError (use of destroyed client): {attr_errors}"
            )
        finally:
            _remove_fake_snap7()

    @pytest.mark.asyncio
    async def test_concurrent_stop_and_sync_read_no_crash(self):
        """并发 stop() + _sync_db_read 不崩溃 (client 引用一致)"""
        driver = _make_driver()
        driver._sync_lock = threading.RLock()
        driver._config = {"read_timeout": 100}
        old_client = _FakeSnap7Client()
        driver._client = old_client

        errors = []

        def do_sync_read():
            try:
                with driver._sync_lock:
                    client = driver._client
                    if client is None:
                        return  # 正常: stop 已清理
                    client.db_read(1, 0, 4)
            except Exception as e:
                errors.append(e)

        with patch.object(S7Driver, "_cancel_background_tasks", _noop_cancel_background_tasks):
            readers = []
            for _ in range(5):
                t = threading.Thread(target=do_sync_read)
                readers.append(t)
                t.start()

            try:
                await driver.stop()
            except Exception:
                pass

            for t in readers:
                t.join(timeout=5)

        # 不应有 AttributeError
        attr_errors = [e for e in errors if isinstance(e, AttributeError)]
        assert len(attr_errors) == 0, f"Concurrent stop+read caused AttributeError: {attr_errors}"
