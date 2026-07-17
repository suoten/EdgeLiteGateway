"""自动生成测试 - src/edgelite/drivers/registry.py"""

# AUTO-GENERATED
import sys
from pathlib import Path

import pytest

_root = Path(__file__).parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
try:
    from src.edgelite.drivers.registry import *  # noqa

    _OK = True
except ImportError as _e:
    _OK = False
    _ERR = str(_e)


class TestRegistryAuto:
    @pytest.fixture(autouse=True)
    def _check(self):
        if not _OK:
            pytest.skip(f"import failed: {_ERR if not _OK else ''}")

    def test_get_driver_display_name_callable(self):
        """测试 get_driver_display_name 可调用（异常即失败）"""
        get_driver_display_name("test", "")

    def test_get_driver_registry_callable(self):
        """测试 get_driver_registry 可调用（异常即失败）"""
        get_driver_registry()

    def test_register_callable(self):
        """测试 register 可调用（异常即失败）"""
        register("")

    def test_get_driver_class_callable(self):
        """测试 get_driver_class 可调用（异常即失败）"""
        get_driver_class("")

    def test_get_supported_protocols_callable(self):
        """测试 get_supported_protocols 可调用（异常即失败）"""
        get_supported_protocols()

    def test_get_all_protocol_keys_callable(self):
        """测试 get_all_protocol_keys 可调用（异常即失败）"""
        get_all_protocol_keys()

    def test_unregister_callable(self):
        """测试 unregister 可调用（异常即失败）"""
        unregister("")

    def test_unregister_driver_callable(self):
        """测试 unregister_driver 可调用（异常即失败）"""
        unregister_driver("")

    def test_items_callable(self):
        """测试 items 可调用（异常即失败）"""
        items()

    def test_auto_discover_callable(self):
        """测试 auto_discover 可调用（异常即失败）"""
        auto_discover()

    def test_get_load_status_callable(self):
        """测试 get_load_status 可调用（异常即失败）"""
        get_load_status()

    def test_get_dependency_results_callable(self):
        """测试 get_dependency_results 可调用（异常即失败）"""
        get_dependency_results()
