"""自动生成测试 - src/edgelite/drivers/bacnet.py"""

# AUTO-GENERATED
import sys
from pathlib import Path

import pytest

_root = Path(__file__).parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
try:
    from src.edgelite.drivers.bacnet import *  # noqa

    _OK = True
except ImportError as _e:
    _OK = False
    _ERR = str(_e)


class TestBacnetAuto:
    @pytest.fixture(autouse=True)
    def _check(self):
        if not _OK:
            pytest.skip(f"import failed: {_ERR if not _OK else ''}")

    def test_add_segment_callable(self):
        """测试 add_segment 可调用（异常即失败）"""
        add_segment(1, 1, "", "", [])

    def test_cancel_callable(self):
        """测试 cancel 可调用（异常即失败）"""
        cancel(1)

    def test_get_pending_count_callable(self):
        """测试 get_pending_count 可调用（异常即失败）"""
        get_pending_count()

    def test_connect_callable(self):
        """测试 connect 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(connect())

    def test_close_callable(self):
        """测试 close 可调用（异常即失败）"""
        close()

    def test_set_cov_callback_callable(self):
        """测试 set_cov_callback 可调用（异常即失败）"""
        set_cov_callback("")

    def test_read_property_callable(self):
        """测试 read_property 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(read_property("", "", "", 1))

    def test_write_property_callable(self):
        """测试 write_property 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(write_property("", "", "", 1, "", ""))

    def test_who_is_callable(self):
        """测试 who_is 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(who_is("", ""))

    def test_handle_i_am_callable(self):
        """测试 handle_i_am 可调用（异常即失败）"""
        handle_i_am("")

    def test_subscribe_cov_callable(self):
        """测试 subscribe_cov 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(subscribe_cov("", "", "", ""))

    def test_read_property_multiple_callable(self):
        """测试 read_property_multiple 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(read_property_multiple("", ""))

    def test_handle_response_callable(self):
        """测试 handle_response 可调用（异常即失败）"""
        handle_response(1, [])

    def test_handle_cov_notification_callable(self):
        """测试 handle_cov_notification 可调用（异常即失败）"""
        handle_cov_notification([])

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

    def test_on_data_callable(self):
        """测试 on_data 可调用（异常即失败）"""
        on_data("")

    def test_is_device_connected_callable(self):
        """测试 is_device_connected 可调用（异常即失败）"""
        is_device_connected(1)

    def test_get_cov_status_callable(self):
        """测试 get_cov_status 可调用（异常即失败）"""
        get_cov_status()

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

    def test_read_device_info_callable(self):
        """测试 read_device_info 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(read_device_info(1))

    def test_subscribe_cov_point_callable(self):
        """测试 subscribe_cov_point 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(subscribe_cov_point(1, "", ""))

    def test_subscribe_all_cov_callable(self):
        """测试 subscribe_all_cov 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(subscribe_all_cov(1, ""))
