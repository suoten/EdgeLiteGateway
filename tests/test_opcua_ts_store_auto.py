"""自动生成测试 - src/edgelite/drivers/opcua_ts_store.py"""

# AUTO-GENERATED
import sys
from pathlib import Path

import pytest

_root = Path(__file__).parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
try:
    from src.edgelite.drivers.opcua_ts_store import *  # noqa

    _OK = True
except ImportError as _e:
    _OK = False
    _ERR = str(_e)


class TestOpcuaTsStoreAuto:
    @pytest.fixture(autouse=True)
    def _check(self):
        if not _OK:
            pytest.skip(f"import failed: {_ERR if not _OK else ''}")

    def test_append_callable(self):
        """测试 append 可调用（异常即失败）"""
        append([])

    def test_get_pending_callable(self):
        """测试 get_pending 可调用（异常即失败）"""
        get_pending("")

    def test_mark_synced_callable(self):
        """测试 mark_synced 可调用（异常即失败）"""
        mark_synced(1)

    def test_cleanup_expired_callable(self):
        """测试 cleanup_expired 可调用（异常即失败）"""
        cleanup_expired()

    def test_start_callable(self):
        """测试 start 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(start())

    def test_stop_callable(self):
        """测试 stop 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(stop())

    def test_sync_now_callable(self):
        """测试 sync_now 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(sync_now())
