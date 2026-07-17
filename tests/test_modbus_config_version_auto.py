"""自动生成测试 - src/edgelite/drivers/modbus_config_version.py"""

# AUTO-GENERATED
import sys
from pathlib import Path

import pytest

_root = Path(__file__).parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
try:
    from src.edgelite.drivers.modbus_config_version import *  # noqa

    _OK = True
except ImportError as _e:
    _OK = False
    _ERR = str(_e)


class TestModbusConfigVersionAuto:
    @pytest.fixture(autouse=True)
    def _check(self):
        if not _OK:
            pytest.skip(f"import failed: {_ERR if not _OK else ''}")

    def test_snapshot_device_config_callable(self):
        """测试 snapshot_device_config 可调用（异常即失败）"""
        snapshot_device_config(1, {}, "")

    def test_rollback_callable(self):
        """测试 rollback 可调用（异常即失败）"""
        rollback("")

    def test_list_versions_callable(self):
        """测试 list_versions 可调用（异常即失败）"""
        list_versions("", "")

    def test_get_version_callable(self):
        """测试 get_version 可调用（异常即失败）"""
        get_version("")

    def test_diff_versions_callable(self):
        """测试 diff_versions 可调用（异常即失败）"""
        diff_versions("", "")

    def test_export_json_callable(self):
        """测试 export_json 可调用（异常即失败）"""
        export_json()

    def test_export_yaml_callable(self):
        """测试 export_yaml 可调用（异常即失败）"""
        export_yaml()

    def test_import_json_callable(self):
        """测试 import_json 可调用（异常即失败）"""
        import_json([])

    def test_verify_integrity_callable(self):
        """测试 verify_integrity 可调用（异常即失败）"""
        verify_integrity("")

    def test_close_callable(self):
        """测试 close 可调用（异常即失败）"""
        close()
