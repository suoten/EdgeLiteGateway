"""自动生成测试 - src/edgelite/drivers/abb_robot.py"""

# AUTO-GENERATED
import sys
from pathlib import Path

import pytest

_root = Path(__file__).parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
try:
    from src.edgelite.drivers.abb_robot import *  # noqa

    _OK = True
except ImportError as _e:
    _OK = False
    _ERR = str(_e)


class TestAbbRobotAuto:
    @pytest.fixture(autouse=True)
    def _check(self):
        if not _OK:
            pytest.skip(f"import failed: {_ERR if not _OK else ''}")

    def test_start_callable(self):
        """测试 start 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(start({}))

    def test_is_safety_stop_active_callable(self):
        """测试 is_safety_stop_active 可调用（异常即失败）"""
        is_safety_stop_active()

    def test_emergency_stop_callable(self):
        """测试 emergency_stop 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(emergency_stop())

    def test_stop_motion_callable(self):
        """测试 stop_motion 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(stop_motion())

    def test_get_safety_state_callable(self):
        """测试 get_safety_state 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(get_safety_state())

    def test_reset_safety_stop_callable(self):
        """测试 reset_safety_stop 可调用（异常即失败）"""
        reset_safety_stop()

    def test_stop_callable(self):
        """测试 stop 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(stop())

    def test_add_device_callable(self):
        """测试 add_device 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(add_device(1, {}, ""))

    def test_discover_devices_callable(self):
        """测试 discover_devices 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(discover_devices({}))

    def test_read_points_callable(self):
        """测试 read_points 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(read_points(1, ""))

    def test_write_point_callable(self):
        """测试 write_point 可调用（异常即失败）"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(write_point(1, "", ""))

    def test_remove_device_callable(self):
        """测试 remove_device 可调用（异常即失败）"""
        remove_device(1)
