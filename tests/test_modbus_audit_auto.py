"""自动生成测试 - src/edgelite/drivers/modbus_audit.py"""

# AUTO-GENERATED
import sys
from pathlib import Path

import pytest

_root = Path(__file__).parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
try:
    from src.edgelite.drivers.modbus_audit import *  # noqa

    _OK = True
except ImportError as _e:
    _OK = False
    _ERR = str(_e)


class TestModbusAuditAuto:
    @pytest.fixture(autouse=True)
    def _check(self):
        if not _OK:
            pytest.skip(f"import failed: {_ERR if not _OK else ''}")

    def test_log_write_callable(self):
        """测试 log_write 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(log_write(1, "", "", "", ""))

    def test_log_config_change_callable(self):
        """测试 log_config_change 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(log_config_change(1, "", {}, {}))

    def test_log_failover_callable(self):
        """测试 log_failover 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(log_failover(1, "", ""))

    def test_log_reconnect_callable(self):
        """测试 log_reconnect 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(log_reconnect(1, ""))

    def test_get_recent_callable(self):
        """测试 get_recent 可调用（异常即失败）"""
        get_recent("")

    def test_get_by_device_callable(self):
        """测试 get_by_device 可调用（异常即失败）"""
        get_by_device(1, "")

    def test_get_by_action_callable(self):
        """测试 get_by_action 可调用（异常即失败）"""
        get_by_action("", "")

    def test_get_stats_callable(self):
        """测试 get_stats 可调用（异常即失败）"""
        get_stats()

    def test_export_csv_callable(self):
        """测试 export_csv 可调用（异常即失败）"""
        export_csv("", "")
