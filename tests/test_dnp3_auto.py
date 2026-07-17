"""自动生成测试 - src/edgelite/drivers/dnp3.py"""

# AUTO-GENERATED
import sys
from pathlib import Path

import pytest

_root = Path(__file__).parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
try:
    from src.edgelite.drivers.dnp3 import *  # noqa

    _OK = True
except ImportError as _e:
    _OK = False
    _ERR = str(_e)


class TestDnp3Auto:
    @pytest.fixture(autouse=True)
    def _check(self):
        if not _OK:
            pytest.skip(f"import failed: {_ERR if not _OK else ''}")

    def test_connect_callable(self):
        """测试 connect 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(connect())

    def test_close_callable(self):
        """测试 close 可调用（异常即失败）"""
        close()

    def test_read_binary_inputs_callable(self):
        """测试 read_binary_inputs 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(read_binary_inputs("", 1))

    def test_read_analog_inputs_callable(self):
        """测试 read_analog_inputs 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(read_analog_inputs("", 1, ""))

    def test_read_counters_callable(self):
        """测试 read_counters 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(read_counters("", 1))

    def test_write_binary_output_callable(self):
        """测试 write_binary_output 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(write_binary_output("", "", ""))

    def test_write_analog_output_callable(self):
        """测试 write_analog_output 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(write_analog_output("", "", ""))

    def test_enable_unsolicited_callable(self):
        """测试 enable_unsolicited 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(enable_unsolicited(""))

    def test_start_callable(self):
        """测试 start 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(start({}))

    def test_stop_callable(self):
        """测试 stop 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(stop())

    def test_add_device_callable(self):
        """测试 add_device 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(add_device(1, {}, ""))

    def test_remove_device_callable(self):
        """测试 remove_device 可调用（异常即失败）"""
        remove_device(1)

    def test_read_points_callable(self):
        """测试 read_points 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(read_points(1, ""))

    def test_write_point_callable(self):
        """测试 write_point 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(write_point(1, "", ""))

    def test_discover_devices_callable(self):
        """测试 discover_devices 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(discover_devices({}))

    def test_is_device_connected_callable(self):
        """测试 is_device_connected 可调用（异常即失败）"""
        is_device_connected(1)
