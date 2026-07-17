"""自定义驱动插件管理器测试 - 发现/加载/安全校验/重载/卸载

覆盖 engine/plugin_manager.py：
- PluginInfo 数据类
- PluginManager: 构造/set_scheduler/discover_custom_drivers(空/不存在)/list_plugins
- _load_module: 路径白名单安全校验/非 .py 拒绝
- _find_driver_subclasses: 查找 DriverPlugin 子类
- _register_plugin: 同名跳过
- reload_plugin: 不存在返回 None / 成功 / 失败
- unload_plugin: 不存在/非自定义/成功
- stop: 清空所有插件
"""

from __future__ import annotations

import textwrap

import pytest

from edgelite.drivers.base import DriverPlugin
from edgelite.engine.plugin_manager import PluginInfo, PluginManager


class FakeRegistry:
    """模拟驱动注册表"""

    def __init__(self):
        self._drivers: dict[str, type] = {}

    def get_driver_class(self, name: str):
        return self._drivers.get(name)

    def register(self, cls: type) -> None:
        self._drivers[cls.plugin_name] = cls

    def unregister_driver(self, cls: type) -> None:
        self._drivers.pop(cls.plugin_name, None)


class DummyDriver(DriverPlugin):
    """测试用驱动子类"""

    plugin_name = "dummy_test"

    async def connect(self, config):  # type: ignore[override]
        pass

    async def disconnect(self):  # type: ignore[override]
        pass

    async def read_points(self, points):  # type: ignore[override]
        return {}

    async def write_point(self, point, value):  # type: ignore[override]
        pass


class TestPluginInfo:
    def test_defaults(self):
        info = PluginInfo(name="test", module_path="/path/to/mod.py", class_name="TestDriver")
        assert info.name == "test"
        assert info.module_path == "/path/to/mod.py"
        assert info.class_name == "TestDriver"
        assert info.is_custom is False
        assert info.is_loaded is False
        assert info.error == ""

    def test_custom_loaded(self):
        info = PluginInfo(
            name="custom",
            module_path="/path/custom.py",
            class_name="CustomDriver",
            is_custom=True,
            is_loaded=True,
        )
        assert info.is_custom is True
        assert info.is_loaded is True

    def test_with_error(self):
        info = PluginInfo(
            name="bad",
            module_path="/path/bad.py",
            class_name="",
            is_custom=True,
            is_loaded=False,
            error="ImportError: missing dep",
        )
        assert info.error == "ImportError: missing dep"
        assert info.is_loaded is False


class TestPluginManagerConstructor:
    def test_defaults(self):
        registry = FakeRegistry()
        pm = PluginManager(registry)
        assert pm._registry is registry
        assert pm._loaded_plugins == {}
        assert pm._allowed_dir is None
        assert pm._scheduler is None

    def test_set_scheduler(self):
        pm = PluginManager(FakeRegistry())
        scheduler = object()
        pm.set_scheduler(scheduler)
        assert pm._scheduler is scheduler


class TestDiscoverCustomDrivers:
    def test_empty_string_returns_empty(self):
        pm = PluginManager(FakeRegistry())
        result = pm.discover_custom_drivers("")
        assert result == []

    def test_nonexistent_dir_returns_empty(self):
        pm = PluginManager(FakeRegistry())
        result = pm.discover_custom_drivers("/nonexistent/path/xyz")
        assert result == []

    def test_empty_dir_returns_empty(self, tmp_path):
        pm = PluginManager(FakeRegistry())
        result = pm.discover_custom_drivers(str(tmp_path))
        assert result == []

    def test_discovers_valid_driver(self, tmp_path):
        """发现并加载有效的 DriverPlugin 子类"""
        # 创建一个包含 DriverPlugin 子类的 .py 文件
        driver_file = tmp_path / "my_driver.py"
        driver_file.write_text(
            textwrap.dedent("""
            from edgelite.drivers.base import DriverPlugin

            class MyTestDriver(DriverPlugin):
                plugin_name = "my_test_driver"

                async def connect(self, config):
                    pass

                async def disconnect(self):
                    pass

                async def read_points(self, points):
                    return {}

                async def write_point(self, point, value):
                    pass
        """)
        )
        registry = FakeRegistry()
        pm = PluginManager(registry)
        result = pm.discover_custom_drivers(str(tmp_path))
        assert len(result) == 1
        assert result[0].name == "my_test_driver"
        assert result[0].is_loaded is True
        assert result[0].is_custom is True
        # 应已注册到 registry
        assert registry.get_driver_class("my_test_driver") is not None

    def test_skips_underscore_files(self, tmp_path):
        """以 _ 开头的文件应被跳过"""
        driver_file = tmp_path / "_internal.py"
        driver_file.write_text("# should be skipped")
        pm = PluginManager(FakeRegistry())
        result = pm.discover_custom_drivers(str(tmp_path))
        assert result == []

    def test_load_error_records_error(self, tmp_path):
        """加载失败的文件应记录错误信息"""
        bad_file = tmp_path / "bad_driver.py"
        bad_file.write_text("syntax error !!! @@@")
        pm = PluginManager(FakeRegistry())
        result = pm.discover_custom_drivers(str(tmp_path))
        assert len(result) == 1
        assert result[0].is_loaded is False
        assert result[0].error != ""
        assert result[0].name == "bad_driver"


class TestLoadModuleSecurity:
    def test_path_traversal_rejected(self, tmp_path):
        """路径穿越到白名单外应被拒绝"""
        pm = PluginManager(FakeRegistry())
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        pm._allowed_dir = allowed.resolve()

        outside = tmp_path / "outside.py"
        with pytest.raises(ValueError, match="安全拒绝"):
            pm._load_module(outside)

    def test_non_py_extension_rejected(self, tmp_path):
        """非 .py 扩展名应被拒绝"""
        pm = PluginManager(FakeRegistry())
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        pm._allowed_dir = allowed.resolve()

        malicious = allowed / "malicious.txt"
        with pytest.raises(ValueError, match="安全拒绝"):
            pm._load_module(malicious)


class TestFindDriverSubclasses:
    def test_finds_subclasses(self):
        """查找模块中的 DriverPlugin 子类"""
        pm = PluginManager(FakeRegistry())
        # 使用当前模块作为测试模块
        import sys

        module = sys.modules[__name__]
        subclasses = pm._find_driver_subclasses(module)
        # DummyDriver 应被发现
        assert DummyDriver in subclasses

    def test_excludes_base_class(self):
        """DriverPlugin 基类本身不应被包含"""
        pm = PluginManager(FakeRegistry())
        import sys

        module = sys.modules[__name__]
        subclasses = pm._find_driver_subclasses(module)
        assert DriverPlugin not in subclasses


class TestRegisterPlugin:
    def test_skip_existing_driver(self):
        """同名驱动已注册时应跳过"""
        registry = FakeRegistry()
        registry.register(DummyDriver)  # 预注册
        pm = PluginManager(registry)

        info = PluginInfo(
            name="dummy_test",
            module_path="/path/dummy.py",
            class_name="DummyDriver",
            is_custom=True,
        )
        pm._register_plugin(DummyDriver, info)
        # 不应崩溃，也不应覆盖
        assert registry.get_driver_class("dummy_test") is DummyDriver


class TestReloadPlugin:
    def test_reload_nonexistent_returns_none(self):
        pm = PluginManager(FakeRegistry())
        result = pm.reload_plugin("nonexistent")
        assert result is None

    def test_reload_success(self, tmp_path):
        """重载已加载的插件"""
        driver_file = tmp_path / "reloadable.py"
        driver_file.write_text(
            textwrap.dedent("""
            from edgelite.drivers.base import DriverPlugin

            class ReloadableDriver(DriverPlugin):
                plugin_name = "reloadable_driver"

                async def connect(self, config):
                    pass

                async def disconnect(self):
                    pass

                async def read_points(self, points):
                    return {}

                async def write_point(self, point, value):
                    pass
        """)
        )
        registry = FakeRegistry()
        pm = PluginManager(registry)
        pm.discover_custom_drivers(str(tmp_path))
        # 重载
        result = pm.reload_plugin("reloadable_driver")
        assert result is not None
        assert result.is_loaded is True

    def test_reload_failure_marks_unloaded(self, tmp_path):
        """重载失败时标记为未加载"""
        driver_file = tmp_path / "will_break.py"
        driver_file.write_text(
            textwrap.dedent("""
            from edgelite.drivers.base import DriverPlugin

            class WillBreakDriver(DriverPlugin):
                plugin_name = "will_break_driver"

                async def connect(self, config):
                    pass

                async def disconnect(self):
                    pass

                async def read_points(self, points):
                    return {}

                async def write_point(self, point, value):
                    pass
        """)
        )
        registry = FakeRegistry()
        pm = PluginManager(registry)
        pm.discover_custom_drivers(str(tmp_path))
        # 破坏文件内容使其无法加载
        driver_file.write_text("broken !!! @@@")
        result = pm.reload_plugin("will_break_driver")
        assert result is not None
        assert result.is_loaded is False
        assert result.error != ""


class TestUnloadPlugin:
    def test_unload_nonexistent_returns_false(self):
        pm = PluginManager(FakeRegistry())
        assert pm.unload_plugin("nonexistent") is False

    def test_unload_non_custom_returns_false(self):
        """非自定义插件不能卸载"""
        pm = PluginManager(FakeRegistry())
        # 手动添加一个非自定义插件
        info = PluginInfo(
            name="builtin",
            module_path="/path/builtin.py",
            class_name="BuiltinDriver",
            is_custom=False,
        )
        pm._loaded_plugins["builtin"] = info
        assert pm.unload_plugin("builtin") is False

    def test_unload_success(self, tmp_path):
        """成功卸载自定义插件"""
        driver_file = tmp_path / "unloadable.py"
        driver_file.write_text(
            textwrap.dedent("""
            from edgelite.drivers.base import DriverPlugin

            class UnloadableDriver(DriverPlugin):
                plugin_name = "unloadable_driver"

                async def connect(self, config):
                    pass

                async def disconnect(self):
                    pass

                async def read_points(self, points):
                    return {}

                async def write_point(self, point, value):
                    pass
        """)
        )
        registry = FakeRegistry()
        pm = PluginManager(registry)
        pm.discover_custom_drivers(str(tmp_path))
        assert registry.get_driver_class("unloadable_driver") is not None
        result = pm.unload_plugin("unloadable_driver")
        assert result is True
        # 应已从 registry 注销
        assert registry.get_driver_class("unloadable_driver") is None


class TestListPlugins:
    def test_empty(self):
        pm = PluginManager(FakeRegistry())
        assert pm.list_plugins() == []

    def test_returns_loaded_plugins(self, tmp_path):
        driver_file = tmp_path / "listable.py"
        driver_file.write_text(
            textwrap.dedent("""
            from edgelite.drivers.base import DriverPlugin

            class ListableDriver(DriverPlugin):
                plugin_name = "listable_driver"

                async def connect(self, config):
                    pass

                async def disconnect(self):
                    pass

                async def read_points(self, points):
                    return {}

                async def write_point(self, point, value):
                    pass
        """)
        )
        pm = PluginManager(FakeRegistry())
        pm.discover_custom_drivers(str(tmp_path))
        plugins = pm.list_plugins()
        assert len(plugins) == 1
        assert plugins[0].name == "listable_driver"


class TestStop:
    @pytest.mark.asyncio
    async def test_stop_clears_plugins(self, tmp_path):
        """stop 应清空所有已加载插件"""
        driver_file = tmp_path / "stoppable.py"
        driver_file.write_text(
            textwrap.dedent("""
            from edgelite.drivers.base import DriverPlugin

            class StoppableDriver(DriverPlugin):
                plugin_name = "stoppable_driver"

                async def connect(self, config):
                    pass

                async def disconnect(self):
                    pass

                async def read_points(self, points):
                    return {}

                async def write_point(self, point, value):
                    pass
        """)
        )
        registry = FakeRegistry()
        pm = PluginManager(registry)
        pm.discover_custom_drivers(str(tmp_path))
        assert len(pm._loaded_plugins) == 1
        await pm.stop()
        assert pm._loaded_plugins == {}
