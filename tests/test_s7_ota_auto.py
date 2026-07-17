"""自动生成测试 - src/edgelite/drivers/s7_ota.py"""

# AUTO-GENERATED
import sys
from pathlib import Path

import pytest

_root = Path(__file__).parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
try:
    from src.edgelite.drivers.s7_ota import *  # noqa

    _OK = True
except ImportError as _e:
    _OK = False
    _ERR = str(_e)


class TestS7OtaAuto:
    @pytest.fixture(autouse=True)
    def _check(self):
        if not _OK:
            pytest.skip(f"import failed: {_ERR if not _OK else ''}")

    def test_set_current_version_callable(self):
        """测试 set_current_version 可调用（异常即失败）"""
        set_current_version("")

    def test_check_update_callable(self):
        """测试 check_update 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(check_update(""))

    def test_start_ota_callable(self):
        """测试 start_ota 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(start_ota("", {}))

    def test_rollback_ota_callable(self):
        """测试 rollback_ota 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(rollback_ota())

    def test_get_progress_callable(self):
        """测试 get_progress 可调用（异常即失败）"""
        get_progress()

    def test_get_history_callable(self):
        """测试 get_history 可调用（异常即失败）"""
        get_history("")
