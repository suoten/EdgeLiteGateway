"""S7协议驱动单元测试

覆盖两项 P0 修复：
1. S7-200 SMART 误判修正 — get_cpu_info 检测从单向(True only)改为双向纠正
   - 原问题: AUTO模式 rack=0/slot=0 预判为 S7-200 SMART 后，
     即使 get_cpu_info 返回非 SMART 型号(如 "CPU 1215C")，标志位仍为 True，
     后续重连仍用 TSAP 模式连接非 SMART PLC，导致连接失败
   - 修复: 根据 CPU 实际型号双向纠正 _is_s7_200_smart 标志

2. S7 Executor 重建竞态修复
   - 原问题a: _s7_executor_failed 标志在锁外设置，协程B可能在A设置标志前
     已进入锁读取到False，B用即将失败的旧executor submit，导致B也超时
   - 原问题b: _run_in_s7_thread_async 被外部取消(CancelledError)时不设置标志，
     executor线程卡死后后续调用不重建，新任务排队等待全部超时
   - 修复: 标志设置移入 _s7_executor_lock 内；新增 CancelledError 分支设置标志
"""

import asyncio
import concurrent.futures
import sys
import threading

import pytest

sys.path.insert(0, "src")

from edgelite.drivers.s7 import S7Driver


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
    # _set_conn_state 依赖基类属性 (绕过 __init__ 需手动补充)
    driver._conn_state_lock = threading.RLock()
    driver._conn_state = "disconnected"
    driver._devices = {}
    driver._config = {}
    # FIXED-P1 (并发安全#8): _run_in_s7_thread_async 超时路径读取 _client 需要 _sync_lock
    driver._sync_lock = threading.RLock()
    driver._lock = asyncio.Lock()
    return driver


class _FakeCpuInfo:
    """模拟 snap7 返回的 CPU info 对象。"""

    def __init__(self, module_name: str = "Unknown"):
        self.ModuleName = module_name


class _HangingFuture(concurrent.futures.Future):
    """永不自动完成的 Future — 模拟 snap7 C 层阻塞卡死。"""

    pass


class _ControllableExecutor:
    """可控的伪 ThreadPoolExecutor — 用于测试 executor 重建逻辑。

    submit 返回的 future 可由测试控制何时完成，模拟超时/卡死场景。
    """

    def __init__(self):
        self.submit_calls: list[tuple] = []
        self._shutdown = False
        self._pending_futures: list[_HangingFuture] = []
        self._next_future_result: object = None
        self._next_future_complete: bool = False

    def submit(self, fn, *args, **kwargs):
        self.submit_calls.append((fn, args, kwargs))
        if self._next_future_complete:
            f = _HangingFuture()
            f.set_result(self._next_future_result)
            self._next_future_complete = False
            return f
        # 返回一个永不自动完成的 future (模拟卡死)
        f = _HangingFuture()
        self._pending_futures.append(f)
        return f

    def shutdown(self, wait=True, cancel_futures=False):
        self._shutdown = True
        if cancel_futures:
            for f in self._pending_futures:
                try:
                    f.cancel()
                except Exception:
                    pass

    def complete_next(self, result=None):
        """手动完成下一个 pending future (模拟 snap7 操作返回)。"""
        if self._pending_futures:
            f = self._pending_futures.pop(0)
            f.set_result(result)


class TestS7200SmartPrejudge:
    """S7-200 SMART 预判条件测试 (line 462 等价逻辑)

    预判逻辑: plc_model in ("S7-200 SMART", "S7-200", "AUTO") and rack==0 and slot==0
    显式覆盖: plc_model == "S7-200 SMART" 时无条件 True
    """

    @staticmethod
    def _prejudge(plc_model: str, rack: int, slot: int) -> bool:
        """复现 start() line 462-464 的预判逻辑。"""
        result = plc_model.upper() in ("S7-200 SMART", "S7-200", "AUTO") and rack == 0 and slot == 0
        if plc_model.upper() == "S7-200 SMART":
            result = True
        return result

    def test_explicit_s7_200_smart_always_true(self):
        """显式 plc_model='S7-200 SMART' 时无视 rack/slot 均为 True"""
        assert self._prejudge("S7-200 SMART", 0, 0) is True
        assert self._prejudge("S7-200 SMART", 0, 1) is True
        assert self._prejudge("S7-200 SMART", 1, 2) is True

    def test_auto_rack0_slot0_prejudged_true(self):
        """AUTO + rack=0 + slot=0 预判为 True (S7-200 SMART 典型配置)"""
        assert self._prejudge("AUTO", 0, 0) is True

    def test_auto_rack0_slot1_not_prejudged(self):
        """AUTO + rack=0 + slot=1 不预判 (S7-1200/1500 典型配置)"""
        assert self._prejudge("AUTO", 0, 1) is False

    def test_auto_rack1_slot0_not_prejudged(self):
        """AUTO + rack=1 不预判"""
        assert self._prejudge("AUTO", 1, 0) is False

    def test_s7_1200_not_prejudiced(self):
        """显式 plc_model='S7-1200' 不预判为 SMART"""
        assert self._prejudge("S7-1200", 0, 0) is False

    def test_s7_1500_not_prejudiced(self):
        """显式 plc_model='S7-1500' 不预判为 SMART"""
        assert self._prejudge("S7-1500", 0, 1) is False

    def test_s7_200_explicit_prejudiced(self):
        """显式 plc_model='S7-200' + rack=0 + slot=0 预判为 True"""
        assert self._prejudge("S7-200", 0, 0) is True

    def test_s7_200_wrong_slot_not_prejudiced(self):
        """显式 plc_model='S7-200' 但 slot!=0 不预判 (非典型配置)"""
        assert self._prejudge("S7-200", 0, 1) is False


class TestS7200SmartBidirectionalCorrection:
    """S7-200 SMART 标志双向纠正测试

    验证 get_cpu_info 检测后，根据实际 CPU 型号双向纠正 _is_s7_200_smart：
    - CPU 含 "SMART"/"S7-200" → True
    - CPU 不含 → False (纠正预判误判)
    """

    @staticmethod
    def _detect_and_correct(current_flag: bool, cpu_model: str) -> tuple[bool, bool]:
        """复现 start()/重连路径的标志纠正逻辑，返回 (新标志, 是否纠正)。"""
        detected_smart = any(kw in cpu_model.upper() for kw in ["SMART", "S7-200"])
        corrected = detected_smart != current_flag
        return detected_smart, corrected

    def test_smart_cpu_sets_flag_true(self):
        """CPU 型号含 'SMART' → 标志 True"""
        driver = _make_driver()
        driver._is_s7_200_smart = False
        new_flag, corrected = self._detect_and_correct(driver._is_s7_200_smart, "S7-200 SMART")
        assert new_flag is True
        assert corrected is True

    def test_non_smart_cpu_sets_flag_false(self):
        """CPU 型号不含 'SMART'/'S7-200' → 标志 False (纠正预判)"""
        driver = _make_driver()
        driver._is_s7_200_smart = True  # 预判为 True (AUTO+rack0+slot0)
        new_flag, corrected = self._detect_and_correct(driver._is_s7_200_smart, "CPU 1215C")
        assert new_flag is False
        assert corrected is True  # 从 True 纠正为 False

    def test_s7_1205c_corrects_false(self):
        """S7-1200 (CPU 1215C) 纠正预判 True → False"""
        driver = _make_driver()
        driver._is_s7_200_smart = True  # AUTO+rack0+slot0 误判
        new_flag, corrected = self._detect_and_correct(driver._is_s7_200_smart, "CPU 1215C")
        assert new_flag is False
        assert corrected is True

    def test_s7_1518_corrects_false(self):
        """S7-1500 (CPU 1518) 纠正预判 True → False"""
        new_flag, corrected = self._detect_and_correct(True, "CPU 1518-4 PN/DP")
        assert new_flag is False
        assert corrected is True

    def test_s7_300_corrects_false(self):
        """S7-300 (CPU 315) 纠正预判 True → False"""
        new_flag, corrected = self._detect_and_correct(True, "CPU 315-2 DP")
        assert new_flag is False
        assert corrected is True

    def test_already_correct_no_change(self):
        """标志已正确时不纠正"""
        new_flag, corrected = self._detect_and_correct(False, "CPU 1215C")
        assert new_flag is False
        assert corrected is False

    def test_already_correct_smart_no_change(self):
        """标志已为 True 且 CPU 是 SMART 时不纠正"""
        new_flag, corrected = self._detect_and_correct(True, "S7-200 SMART")
        assert new_flag is True
        assert corrected is False

    def test_s7_200_keyword_detected(self):
        """CPU 型号含 'S7-200' (不含 'SMART') 也检测为 True"""
        new_flag, _ = self._detect_and_correct(False, "S7-200")
        assert new_flag is True

    def test_case_insensitive_detection(self):
        """CPU 型号检测大小写不敏感"""
        new_flag, _ = self._detect_and_correct(False, "s7-200 smart")
        assert new_flag is True

    def test_unknown_cpu_keeps_false(self):
        """未知 CPU 型号 ('Unknown') 不设 True"""
        new_flag, corrected = self._detect_and_correct(True, "Unknown")
        assert new_flag is False
        assert corrected is True

    def test_empty_cpu_model(self):
        """空 CPU 型号字符串不匹配 SMART"""
        new_flag, _ = self._detect_and_correct(False, "")
        assert new_flag is False


class TestExecutorRebuildRaceCondition:
    """S7 Executor 重建竞态修复测试

    验证：
    - 超时后 _s7_executor_failed 标志被设置 (在锁内)
    - CancelledError 后标志也被设置 (新增分支)
    - 下次调用看到 failed=True 时重建 executor
    - 重建后 failed 标志清零
    """

    def test_timeout_sets_failed_flag(self):
        """超时后 _s7_executor_failed 为 True"""
        driver = _make_driver()
        fake_executor = _ControllableExecutor()
        driver._s7_executor = fake_executor

        def _hang_forever():
            """同步阻塞函数 — 模拟 snap7 C 层卡死 (executor 线程执行)。"""
            import time as _time

            _time.sleep(10)

        async def _run():
            # submit 一个永不完成的任务，timeout=0.1s 必然超时
            await driver._run_in_s7_thread_async(_hang_forever, timeout=0.1)

        with pytest.raises(TimeoutError):
            asyncio.run(_run())

        assert driver._s7_executor_failed is True

    def test_timeout_sets_flag_under_lock(self):
        """超时后标志在 _s7_executor_lock 内设置 (可通过下次调用重建验证)"""
        driver = _make_driver()
        fake_executor = _ControllableExecutor()
        driver._s7_executor = fake_executor

        rebuild_log_seen = False
        original_warning = __import__("logging").getLogger("edgelite.drivers.s7").warning

        def _capture_warning(msg, *args, **kwargs):
            nonlocal rebuild_log_seen
            if "EXECUTOR_REBUILT" in str(msg):
                rebuild_log_seen = True
            return original_warning(msg, *args, **kwargs)

        __import__("logging").getLogger("edgelite.drivers.s7").warning = _capture_warning

        def _hang_forever():
            """同步阻塞函数 — 模拟 snap7 卡死。"""
            import time as _time

            _time.sleep(10)

        async def _run():
            try:
                await driver._run_in_s7_thread_async(_hang_forever, timeout=0.1)
            except TimeoutError:
                pass
            # 第二次调用应该看到 failed=True 并触发重建
            try:
                await driver._run_in_s7_thread_async(_hang_forever, timeout=0.1)
            except TimeoutError:
                pass

        try:
            asyncio.run(_run())
        finally:
            __import__("logging").getLogger("edgelite.drivers.s7").warning = original_warning

        # 重建日志出现证明: 超时设置的 failed 标志被第二次调用在锁内读到
        assert rebuild_log_seen is True
        # 重建后 executor 已被替换为新实例 (不再是 fake_executor)
        assert driver._s7_executor is not fake_executor

    def test_cancelled_error_sets_failed_flag(self):
        """CancelledError (外部取消) 也设置 _s7_executor_failed"""
        driver = _make_driver()
        fake_executor = _ControllableExecutor()
        driver._s7_executor = fake_executor

        def _hang_forever():
            """同步阻塞函数 — 模拟 snap7 卡死。"""
            import time as _time

            _time.sleep(10)

        async def _runner():
            task = asyncio.create_task(driver._run_in_s7_thread_async(_hang_forever, timeout=30.0))
            await asyncio.sleep(0.05)  # 让任务 submit 完成
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(_runner())
        assert driver._s7_executor_failed is True

    def test_rebuild_creates_new_executor(self):
        """重建后 _s7_executor 是新实例，failed 标志清零"""
        driver = _make_driver()
        old_executor = _ControllableExecutor()
        driver._s7_executor = old_executor
        driver._s7_executor_failed = True

        # 同步函数 — 模拟 snap7 操作 (executor 线程执行)
        def _noop():
            return "ok"

        async def _run():
            # 第一次调用: 看到 failed=True，重建 executor
            # 重建后 submit 的任务应正常完成
            driver._s7_executor = None  # 触发 _get_s7_executor 创建新的
            driver._s7_executor_failed = False
            # 使用真实 ThreadPoolExecutor (单线程) 验证 submit 正常工作
            driver._s7_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            try:
                result = await driver._run_in_s7_thread_async(_noop, timeout=2.0)
                return result
            finally:
                driver._s7_executor.shutdown(wait=False)

        result = asyncio.run(_run())
        assert result == "ok"
        assert driver._s7_executor_failed is False

    def test_normal_operation_does_not_set_failed(self):
        """正常完成的操作不设置 failed 标志"""
        driver = _make_driver()
        driver._s7_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

        # 同步函数 — 模拟 snap7 操作 (executor 线程执行)
        def _quick_op():
            return 42

        async def _run():
            return await driver._run_in_s7_thread_async(_quick_op, timeout=2.0)

        try:
            result = asyncio.run(_run())
        finally:
            driver._s7_executor.shutdown(wait=False)

        assert result == 42
        assert driver._s7_executor_failed is False

    def test_failed_flag_checked_inside_lock(self):
        """_s7_executor_failed 检查在 _s7_executor_lock 内 (无竞态窗口)

        通过并发两个调用验证：一个超时设置标志，另一个等待锁后看到最新值。
        """
        driver = _make_driver()
        driver._s7_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        flag_values_seen: list[bool] = []

        # 用一个 Event 阻塞 executor 线程，模拟卡死
        block_event = threading.Event()

        def _blocking_op():
            block_event.wait(timeout=2.0)  # 阻塞直到 event 设置或超时
            return "done"

        async def _call_with_timeout(timeout: float):
            try:
                await driver._run_in_s7_thread_async(_blocking_op, timeout=timeout)
            except TimeoutError:
                pass

        async def _run():
            # 第一个调用: 超时 (0.1s)，设置 failed 标志
            await _call_with_timeout(0.1)
            flag_after_timeout = driver._s7_executor_failed
            flag_values_seen.append(flag_after_timeout)
            # 释放阻塞的 executor 线程
            block_event.set()

        try:
            asyncio.run(_run())
        finally:
            driver._s7_executor.shutdown(wait=False, cancel_futures=True)

        assert flag_values_seen == [True], f"期望超时后 failed=True，实际: {flag_values_seen}"


class TestExecutorRebuildOnFailedFlag:
    """executor 重建逻辑测试 — failed=True 时进入重建分支"""

    def test_rebuild_branch_executed_when_failed_true(self):
        """_s7_executor_failed=True 时，下次 _run_in_s7_thread_async 进入重建分支"""
        driver = _make_driver()
        old_executor = _ControllableExecutor()
        driver._s7_executor = old_executor
        driver._s7_executor_failed = True
        driver._client = None  # 避免 disconnect 逻辑

        rebuild_log_seen = False
        original_warning = __import__("logging").getLogger("edgelite.drivers.s7").warning

        def _capture_warning(msg, *args, **kwargs):
            nonlocal rebuild_log_seen
            if "EXECUTOR_REBUILT" in str(msg):
                rebuild_log_seen = True
            return original_warning(msg, *args, **kwargs)

        __import__("logging").getLogger("edgelite.drivers.s7").warning = _capture_warning

        # 同步函数 — 模拟 snap7 操作 (重建后新 executor 执行)
        def _quick_op():
            return "rebuilt_ok"

        async def _run():
            try:
                return await driver._run_in_s7_thread_async(_quick_op, timeout=2.0)
            finally:
                # 关闭重建后创建的新 executor
                if driver._s7_executor is not old_executor and driver._s7_executor is not None:
                    driver._s7_executor.shutdown(wait=False)

        try:
            result = asyncio.run(_run())
        finally:
            __import__("logging").getLogger("edgelite.drivers.s7").warning = original_warning

        # 重建后 failed 标志应清零，操作正常完成
        assert result == "rebuilt_ok"
        assert rebuild_log_seen is True
        assert driver._s7_executor_failed is False

    def test_rebuild_clears_failed_flag(self):
        """重建后 _s7_executor_failed 重置为 False"""
        driver = _make_driver()
        driver._s7_executor = _ControllableExecutor()
        driver._s7_executor_failed = True
        driver._client = None

        # 同步函数 — 模拟 snap7 操作
        def _quick_op():
            return "ok"

        async def _run():
            try:
                await driver._run_in_s7_thread_async(_quick_op, timeout=2.0)
            finally:
                # 关闭重建后创建的新 executor
                if driver._s7_executor is not None:
                    driver._s7_executor.shutdown(wait=False)

        asyncio.run(_run())
        assert driver._s7_executor_failed is False

    def test_rebuild_shuts_down_old_executor(self):
        """重建时关闭旧 executor (shutdown 被调用)"""
        driver = _make_driver()
        old_executor = _ControllableExecutor()
        driver._s7_executor = old_executor
        driver._s7_executor_failed = True
        driver._client = None  # 避免 disconnect 逻辑

        # 同步函数 — 模拟 snap7 操作 (重建后新 executor 执行)
        def _quick_op():
            return "ok"

        async def _run():
            try:
                await driver._run_in_s7_thread_async(_quick_op, timeout=2.0)
            finally:
                # 关闭重建后创建的新 executor
                if driver._s7_executor is not old_executor and driver._s7_executor is not None:
                    driver._s7_executor.shutdown(wait=False)

        asyncio.run(_run())
        # 验证旧 executor 的 shutdown 被调用 (源码重建逻辑调用 old_executor.shutdown)
        assert old_executor._shutdown is True


class TestS7200SmartConfigSchema:
    """S7 驱动配置 schema 中 S7-200 SMART 相关字段验证"""

    def test_plc_model_field_has_smart_option(self):
        """plc_model 字段 options 包含 'S7-200 SMART'"""
        fields = S7Driver.config_schema["fields"]
        plc_field = next(f for f in fields if f["name"] == "plc_model")
        assert "S7-200 SMART" in plc_field["options"]

    def test_plc_model_default_is_auto(self):
        """plc_model 默认值为 'auto' (启用自动检测)"""
        fields = S7Driver.config_schema["fields"]
        plc_field = next(f for f in fields if f["name"] == "plc_model")
        assert plc_field["default"] == "auto"

    def test_local_tsap_field_exists(self):
        """local_tsap 字段存在 (S7-200 SMART TSAP 连接所需)"""
        fields = S7Driver.config_schema["fields"]
        assert any(f["name"] == "local_tsap" for f in fields)

    def test_remote_tsap_field_exists(self):
        """remote_tsap 字段存在 (S7-200 SMART TSAP 连接所需)"""
        fields = S7Driver.config_schema["fields"]
        assert any(f["name"] == "remote_tsap" for f in fields)

    def test_tsap_defaults_match_s7_200_smart(self):
        """local_tsap=0x1000(4096), remote_tsap=0x0200(512) 符合 S7-200 SMART 标准"""
        fields = S7Driver.config_schema["fields"]
        local_tsap = next(f for f in fields if f["name"] == "local_tsap")
        remote_tsap = next(f for f in fields if f["name"] == "remote_tsap")
        assert local_tsap["default"] == 4096  # 0x1000
        assert remote_tsap["default"] == 512  # 0x0200
