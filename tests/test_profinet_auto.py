"""自动生成测试 - src/edgelite/drivers/profinet.py"""

# AUTO-GENERATED
import sys
from pathlib import Path

import pytest

_root = Path(__file__).parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
try:
    from src.edgelite.drivers.profinet import *  # noqa

    _OK = True
except ImportError as _e:
    _OK = False
    _ERR = str(_e)


class TestProfinetAuto:
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

    def test_discover_devices_callable(self):
        """测试 discover_devices 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(discover_devices(""))

    def test_get_device_by_name_callable(self):
        """测试 get_device_by_name 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(get_device_by_name("test", ""))

    def test_read_io_data_callable(self):
        """测试 read_io_data 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(read_io_data("", "", "", 1))

    def test_write_io_data_callable(self):
        """测试 write_io_data 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(write_io_data("", "", "", []))

    def test_handle_packet_callable(self):
        """测试 handle_packet 可调用（异常即失败）"""
        handle_packet([])

    def test_connection_made_callable(self):
        """测试 connection_made 可调用（异常即失败）"""
        connection_made("")

    def test_datagram_received_callable(self):
        """测试 datagram_received 可调用（异常即失败）"""
        datagram_received([], "")

    def test_error_received_callable(self):
        """测试 error_received 可调用（异常即失败）"""
        error_received("")

    def test_start_callable(self):
        """测试 start 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(start({}))

    def test_stop_callable(self):
        """测试 stop 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(stop())

    def test_read_points_callable(self):
        """测试 read_points 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(read_points(1, ""))

    def test_write_point_callable(self):
        """测试 write_point 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(write_point(1, "", ""))

    def test_add_device_callable(self):
        """测试 add_device 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(add_device(1, {}, ""))

    def test_remove_device_callable(self):
        """测试 remove_device 可调用（异常即失败）"""
        remove_device(1)

    def test_discover_devices_callable(self):
        """测试 discover_devices 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(discover_devices({}))

    def test_is_device_connected_callable(self):
        """测试 is_device_connected 可调用（异常即失败）"""
        is_device_connected(1)
