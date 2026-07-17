"""自定义驱动沙箱命名空间隔离单元测试 (并发安全 #9)

覆盖 P0 修复: 全局替换 builtins/importlib/sys.modules → module.__builtins__ 命名空间隔离。

原问题:
  _discover_custom_drivers 在 exec_module 期间全局替换:
  - builtins.__import__ → _restricted_import
  - importlib.import_module → _restricted_import_module
  - importlib.util.spec_from_file_location → _restricted_spec_from_file
  - sys.modules: 挂起非白名单模块 (pop)
  这些全局突变在 exec_module 窗口期影响整个应用:
  其他线程/coroutine 的 import 会受限或失败 (TOCTOU 竞态)。

修复:
  通过 module.__builtins__ 命名空间隔离:
  - exec_module 前设置 module.__builtins__ = restricted dict
  - 全局 builtins / importlib / sys.modules 完全不受影响
  - 模块内 __import__ 受限，模块外不受影响
"""

from __future__ import annotations

import builtins
import importlib
import importlib.util
import shutil
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, "src")

from edgelite.drivers.registry import DriverRegistry  # noqa: E402

# edgelite 包根目录 (用于 _SAFE_BASE 校验，自定义驱动目录必须在此内)
_EDGELITE_ROOT = Path(__file__).resolve().parent.parent / "src" / "edgelite"

# 测试驱动源码 — 记录沙箱环境信息到类属性
_TEST_DRIVER_SOURCE = '''
"""测试用自定义驱动 — 验证沙箱命名空间隔离"""

import asyncio  # 白名单模块 — 应成功

from edgelite.drivers.base import DriverPlugin

# 模块级: 记录沙箱状态
_whitelisted_import_ok = True  # asyncio 导入成功 (执行到这里说明白名单 import 正常)

_non_whitelisted_import_error = None
try:
    import os  # 非白名单模块 — 应失败
    _non_whitelisted_import_error = None  # 不应执行到这里
except ImportError as e:
    _non_whitelisted_import_error = str(e)

# 记录模块的 __builtins__ (命名空间隔离的核心验证点)
_module_builtins = __builtins__


class TestSandboxDriver(DriverPlugin):
    """测试沙箱驱动"""
    plugin_name = "test_sandbox_driver"
    plugin_version = "1.0.0"
    supported_protocols = ("test_sandbox_protocol",)

    # 通过类属性暴露沙箱状态供测试验证
    _whitelisted_import_ok = _whitelisted_import_ok
    _non_whitelisted_import_error = _non_whitelisted_import_error
    _module_builtins = _module_builtins

    @classmethod
    def _try_import_os_runtime(cls):
        """运行时延迟 import os (应被沙箱拦截)"""
        try:
            import os  # noqa: F401
            return True  # 不应执行到这里
        except ImportError:
            return False

    @classmethod
    def _try_import_json_runtime(cls):
        """运行时延迟 import json (白名单模块，应成功)"""
        try:
            import json
            return json is not None
        except ImportError:
            return False
'''


@pytest.fixture
def loaded_registry():
    """创建已加载测试自定义驱动的 DriverRegistry。

    在 edgelite 包内创建临时自定义驱动目录 (满足 _SAFE_BASE 校验)，
    加载后 yield registry (附带 _test_orig_globals 保存加载前的全局状态)，
    测试后清理临时文件和 __pycache__。
    """
    custom_dir = _EDGELITE_ROOT / "_test_custom_sandbox"
    custom_dir.mkdir(exist_ok=True)
    driver_file = custom_dir / "test_sandbox_driver.py"
    driver_file.write_text(_TEST_DRIVER_SOURCE, encoding="utf-8")

    # 保存加载前的全局状态 (用于验证全局未被修改)
    orig_globals = {
        "builtins_import": builtins.__import__,
        "importlib_import_module": importlib.import_module,
        "spec_from_file_location": importlib.util.spec_from_file_location,
    }

    # Mock config 返回临时自定义驱动目录
    mock_config = type("MockConfig", (), {})()
    mock_config.drivers = type("MockDrivers", (), {"custom_dir": str(custom_dir)})()

    registry = DriverRegistry()

    with patch("edgelite.config.get_config", return_value=mock_config):
        registry._discover_custom_drivers()

    # 将原始全局状态附加到 registry 供测试验证
    registry._test_orig_globals = orig_globals

    yield registry

    # 清理: 临时目录 + __pycache__ + sys.modules
    shutil.rmtree(custom_dir, ignore_errors=True)
    pycache = _EDGELITE_ROOT / "drivers" / "__pycache__"
    for f in pycache.glob("*test_sandbox*"):
        try:
            f.unlink()
        except OSError:
            pass
    sys.modules.pop("edgelite.drivers.custom_test_sandbox_driver", None)


# ════════════════════════════════════════════════════════════════════════
# 全局状态不受影响 (并发安全核心)
# ════════════════════════════════════════════════════════════════════════


class TestGlobalStateNotModified:
    """全局 builtins / importlib / sys.modules 不被修改 (并发安全核心)"""

    def test_global_builtins_import_not_replaced(self, loaded_registry):
        """builtins.__import__ 加载前后一致 (未被全局替换)"""
        orig = loaded_registry._test_orig_globals["builtins_import"]
        assert builtins.__import__ is orig

    def test_global_importlib_not_replaced(self, loaded_registry):
        """importlib.import_module 加载前后一致 (未被全局替换)"""
        orig = loaded_registry._test_orig_globals["importlib_import_module"]
        assert importlib.import_module is orig

    def test_global_spec_from_file_not_replaced(self, loaded_registry):
        """importlib.util.spec_from_file_location 加载前后一致"""
        orig = loaded_registry._test_orig_globals["spec_from_file_location"]
        assert importlib.util.spec_from_file_location is orig

    def test_sys_modules_not_modified(self, loaded_registry):
        """sys.modules 中非白名单模块未被移除"""
        # os, sys, subprocess 等应仍在 sys.modules 中
        assert "os" in sys.modules
        assert "sys" in sys.modules
        assert "subprocess" in sys.modules
        assert "socket" in sys.modules

    def test_concurrent_import_works_after_loading(self, loaded_registry):
        """加载后全局 import 正常工作 (非白名单模块可导入)"""
        # 如果全局 builtins.__import__ 被替换，这些 import 会失败
        import os
        import socket
        import subprocess

        assert os is not None
        assert subprocess is not None
        assert socket is not None


# ════════════════════════════════════════════════════════════════════════
# 模块级命名空间隔离
# ════════════════════════════════════════════════════════════════════════


class TestModuleNamespaceIsolation:
    """module.__builtins__ 命名空间隔离 — 仅限制自定义驱动模块内"""

    def test_custom_driver_loaded(self, loaded_registry):
        """自定义驱动成功加载并注册"""
        assert "test_sandbox_protocol" in loaded_registry._drivers
        driver_cls = loaded_registry._drivers["test_sandbox_protocol"]
        assert driver_cls.plugin_name == "test_sandbox_driver"

    def test_whitelisted_import_succeeds_at_load_time(self, loaded_registry):
        """加载时白名单模块 (asyncio) 可导入"""
        driver_cls = loaded_registry._drivers["test_sandbox_protocol"]
        assert driver_cls._whitelisted_import_ok is True

    def test_non_whitelisted_import_blocked_at_load_time(self, loaded_registry):
        """加载时非白名单模块 (os) 被拦截"""
        driver_cls = loaded_registry._drivers["test_sandbox_protocol"]
        assert driver_cls._non_whitelisted_import_error is not None
        assert "not in the allowed modules whitelist" in driver_cls._non_whitelisted_import_error

    def test_non_whitelisted_import_blocked_at_runtime(self, loaded_registry):
        """运行时延迟 import os 被沙箱拦截 (module.__builtins__ 持久生效)"""
        driver_cls = loaded_registry._drivers["test_sandbox_protocol"]
        assert driver_cls._try_import_os_runtime() is False

    def test_whitelisted_import_succeeds_at_runtime(self, loaded_registry):
        """运行时延迟 import json (白名单) 成功"""
        driver_cls = loaded_registry._drivers["test_sandbox_protocol"]
        assert driver_cls._try_import_json_runtime() is True

    def test_module_builtins_is_restricted_dict(self, loaded_registry):
        """模块的 __builtins__ 是受限 dict (非 builtins 模块)"""
        driver_cls = loaded_registry._drivers["test_sandbox_protocol"]
        module_builtins = driver_cls._module_builtins
        # 命名空间隔离: __builtins__ 应为 dict 而非 builtins 模块
        assert isinstance(module_builtins, dict)
        # __import__ 应为受限版本 (调用导入非白名单模块会抛 ImportError)
        restricted_import = module_builtins["__import__"]
        with pytest.raises(ImportError, match="not in the allowed modules whitelist"):
            restricted_import("os")

    def test_module_builtins_whitelisted_import_works(self, loaded_registry):
        """受限 __import__ 仍可导入白名单模块"""
        driver_cls = loaded_registry._drivers["test_sandbox_protocol"]
        module_builtins = driver_cls._module_builtins
        restricted_import = module_builtins["__import__"]
        # 白名单模块应可导入
        result = restricted_import("json")
        assert result is not None


# ════════════════════════════════════════════════════════════════════════
# 并发安全: 加载期间其他线程不受影响
# ════════════════════════════════════════════════════════════════════════


class TestConcurrentImportSafety:
    """并发安全: 自定义驱动加载期间其他线程的 import 不受影响"""

    def test_import_during_loading_succeeds(self, tmp_path):
        """模拟并发: 在加载自定义驱动期间，另一线程 import 非白名单模块应成功

        验证: 全局 builtins.__import__ 不被替换 → 并发 import 安全。
        用线程 barrier 同步: 主线程开始加载 → 工作线程 import → 主线程完成加载。
        """
        import threading

        # 创建临时自定义驱动目录
        custom_dir = _EDGELITE_ROOT / "_test_concurrent_sandbox"
        custom_dir.mkdir(exist_ok=True)
        driver_file = custom_dir / "test_concurrent_driver.py"
        driver_file.write_text(
            "from edgelite.drivers.base import DriverPlugin\n"
            "class TestConcurrentDriver(DriverPlugin):\n"
            "    plugin_name = 'test_concurrent'\n"
            "    plugin_version = '1.0.0'\n"
            "    supported_protocols = ('test_concurrent_protocol',)\n",
            encoding="utf-8",
        )

        mock_config = type("MockConfig", (), {})()
        mock_config.drivers = type("MockDrivers", (), {"custom_dir": str(custom_dir)})()

        # 工作线程: 持续 import 非白名单模块
        import_errors: list[Exception] = []
        barrier = threading.Barrier(2)
        stop_flag = threading.Event()

        def worker_import():
            barrier.wait()  # 同步启动
            while not stop_flag.is_set():
                try:
                    import os  # noqa: F401
                    import socket  # noqa: F401
                except Exception as e:
                    import_errors.append(e)
                    break

        # 保存加载前状态
        orig_builtins_import = builtins.__import__

        try:
            thread = threading.Thread(target=worker_import, daemon=True)
            thread.start()

            # 主线程: 加载自定义驱动 (与工作线程并发)
            barrier.wait()
            registry = DriverRegistry()
            with patch("edgelite.config.get_config", return_value=mock_config):
                registry._discover_custom_drivers()

            stop_flag.set()
            thread.join(timeout=5.0)

            # 工作线程的 import 不应出错
            assert import_errors == [], f"Concurrent import failed during loading: {import_errors}"
            # 全局 builtins.__import__ 不变
            assert builtins.__import__ is orig_builtins_import
        finally:
            shutil.rmtree(custom_dir, ignore_errors=True)
            pycache = _EDGELITE_ROOT / "drivers" / "__pycache__"
            for f in pycache.glob("*test_concurrent*"):
                try:
                    f.unlink()
                except OSError:
                    pass
            sys.modules.pop("edgelite.drivers.custom_test_concurrent_driver", None)
