"""自动生成测试 - src/edgelite/drivers/fins_ota.py"""

# AUTO-GENERATED
import sys
from pathlib import Path

import pytest

_root = Path(__file__).parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
try:
    from src.edgelite.drivers.fins_ota import *  # noqa

    _OK = True
except ImportError as _e:
    _OK = False
    _ERR = str(_e)


class TestFinsOtaAuto:
    @pytest.fixture(autouse=True)
    def _check(self):
        if not _OK:
            pytest.skip(f"import failed: {_ERR if not _OK else ''}")

    def test_check_update_callable(self):
        """测试 check_update 可调用（异常即失败）"""
        check_update("")

    def test_start_ota_callable(self):
        """测试 start_ota 可调用（异常即失败）"""
        start_ota("", {})

    def test_rollback_ota_callable(self):
        """测试 rollback_ota 可调用（异常即失败）"""
        rollback_ota()

    def test_get_progress_callable(self):
        """测试 get_progress 可调用（异常即失败）"""
        get_progress()

    def test_get_history_callable(self):
        """测试 get_history 可调用（异常即失败）"""
        get_history("")
