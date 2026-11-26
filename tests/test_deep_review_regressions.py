"""深度复查回归测试（2026-11）

每条用例对应一处曾经的必然故障：

1. ``config.save_config``：可选的文件级版本管理器（``edgelite.services.config_version``）缺失时
   不应中止保存——此前社区版所有落盘路径（通知配置、内置服务启停）恒失败。
2. ``DeviceService`` 配置版本方法：驱动的 async 实现必须被 await——此前对 FINS / AB / MC /
   OPC UA / S7 返回的是协程对象，版本从未保存、回滚 500。
3. ``DeviceService.count_devices_by_driver`` / ``system._check_drivers``：注册表没有
   ``list_drivers()``，此前协议标签恒为 unknown、健康检查恒报 0 个驱动。
4. ``PlatformService``：``platform.mqtt_utils`` / ``js_sandbox`` 缺失时，主题模板校验使用内置
   规则，高级模板/脚本接口返回"功能不可用"而不是抛 ModuleNotFoundError。
5. OPC UA 驱动的配置版本 / 时序存储 / OTA 辅助模块：接口必须与 ``OpcUaDriver`` 的调用方式一致
   （原桩缺少驱动调用的方法与字段，一经调用必然 AttributeError / TypeError）。
6. 日志调用的 ``%`` 占位符数量必须与参数数量一致（否则 logging 内部格式化抛 TypeError，告警丢失）。
7. 脚本沙箱：死循环脚本被真正打断且不泄漏线程/名额、不占用 asyncio 默认线程池；脚本内的异常处理可用。
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import os
import re
import sys
import time
import types
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from edgelite.config import load_config, save_config
from edgelite.services.device_service import DeviceService
from edgelite.services.platform_service import PlatformService

_SECRET = "unit-test-secret-key-for-config-persist-0123456789abcdef"

# ── 1. config.save_config ──────────────────────────────────────────────────


@pytest.fixture
def config_env(tmp_path, monkeypatch):
    """独立的环境与全局配置状态，避免影响其他用例"""
    import edgelite.config as config_mod

    monkeypatch.setenv("EDGELITE_SECURITY__SECRET_KEY", _SECRET)
    monkeypatch.setattr(config_mod, "_config", config_mod._config)
    # SecretManager 未初始化时加解密按设计降级为明文。固定为该状态，使用例不依赖主密钥配置
    # 与用例执行顺序（干净环境 / CI 上 init_secret_manager 会直接拒绝启动）。
    secret_manager = MagicMock()
    secret_manager.is_initialized.return_value = False
    monkeypatch.setattr(config_mod, "get_secret_manager", lambda: secret_manager)
    return tmp_path / "cfg" / "config.yaml"


def test_save_config_works_without_optional_version_manager(config_env, monkeypatch):
    """社区版没有 edgelite.services.config_version：配置必须能正常写盘并读回"""
    monkeypatch.setitem(sys.modules, "edgelite.services.config_version", None)  # 模拟模块不存在
    cfg = load_config(str(config_env))  # 文件不存在 → 默认值 + 环境变量

    save_config(cfg, config_env)

    assert config_env.exists()
    reloaded = load_config(str(config_env))
    assert reloaded.notify.dingtalk.enabled == cfg.notify.dingtalk.enabled
    assert reloaded.model_dump(exclude={"security"}) == cfg.model_dump(exclude={"security"})


def test_save_config_still_aborts_when_snapshot_fails(config_env, monkeypatch):
    """企业版：版本管理器存在但快照失败时仍应中止保存（文件不得被写入）"""

    class _BrokenManager:
        def save_version_sync(self, data: str) -> None:
            raise RuntimeError("snapshot disk full")

    fake_module = types.ModuleType("edgelite.services.config_version")
    fake_module.get_config_version_manager = lambda: _BrokenManager()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "edgelite.services.config_version", fake_module)
    cfg = load_config(str(config_env))

    with pytest.raises(RuntimeError, match="snapshot disk full"):
        save_config(cfg, config_env)
    assert not config_env.exists()


# ── 2. DeviceService 配置版本 ──────────────────────────────────────────────


class _AsyncConfigVersionDriver:
    """与 FINS / AB / MC / OPC UA / S7 一致：配置版本方法全部是 async"""

    plugin_name = "fins"

    def __init__(self) -> None:
        self.saved: list[tuple] = []
        self._config: dict = {}

    async def save_config_version(self, device_id, config, change_summary="", operator=""):
        self.saved.append((device_id, config, change_summary, operator))
        return 7

    async def get_config_current(self, device_id):
        return {"points": [{"name": "p1", "address": "D100"}]}

    async def get_config_versions(self, device_id):
        return [{"version": 1}, {"version": 7}]

    async def get_config_version_config(self, device_id, version):
        return {"version": version, "points": [{"name": "p1", "address": "D100"}]}

    async def rollback_config(self, device_id, target_version, operator=""):
        return {"version": 8, "config": {"points": [{"name": "p1", "address": "D100"}]}}

    async def get_config_audit_trail(self, device_id, limit=50):
        return [{"action": "save"}]

    async def diff_config_versions(self, device_id, version_a, version_b):
        return {"changed": True, "from": version_a, "to": version_b}


class _SyncConfigVersionDriver:
    """与 Modbus 系列一致：同名方法是同步实现，必须继续兼容"""

    plugin_name = "modbus_tcp"

    def __init__(self) -> None:
        self._config: dict = {}

    def save_config_version(self, device_id, config, change_summary="", operator=""):
        return 3

    def get_config_current(self, device_id):
        return {"points": []}

    def get_config_versions(self, device_id):
        return [{"version": 3}]

    def get_config_version_config(self, device_id, version):
        return {"version": version, "points": []}

    def rollback_config(self, device_id, target_version, operator=""):
        return {"version": 4, "config": {"points": []}}

    def get_config_audit_trail(self, device_id, limit=50):
        return []

    def diff_config_versions(self, device_id, version_a, version_b):
        return None


@pytest.fixture
def device_service():
    with patch("edgelite.services.device_service.get_driver_registry", return_value=MagicMock()):
        yield DeviceService(
            device_repo=MagicMock(),
            rule_repo=MagicMock(),
            scheduler=MagicMock(),
            lifecycle=MagicMock(),
            template_repo=None,
        )


@pytest.mark.asyncio
async def test_config_version_methods_await_async_driver(device_service):
    driver = _AsyncConfigVersionDriver()
    device_service._driver_instances["dev1"] = driver

    assert await device_service.save_config_version("dev1", {"points": []}, "init", "admin") == 7
    assert driver.saved == [("dev1", {"points": []}, "init", "admin")]

    current = await device_service.get_config_current("dev1")
    assert isinstance(current, dict) and current["points"][0]["address"] == "D100"

    versions = await device_service.get_config_versions("dev1")
    assert [v["version"] for v in versions] == [1, 7]

    cfg = await device_service.get_config_version_config("dev1", 7)
    assert cfg == {"version": 7, "points": [{"name": "p1", "address": "D100"}]}

    result = await device_service.rollback_config("dev1", 7, "admin")
    assert isinstance(result, dict) and result["version"] == 8
    assert driver._config == {"points": [{"name": "p1", "address": "D100"}]}  # 回滚结果已写回驱动

    trail = await device_service.get_config_audit_trail("dev1")
    assert trail == [{"action": "save"}]

    diff = await device_service.diff_config_versions("dev1", 1, 7)
    assert diff == {"changed": True, "from": 1, "to": 7}


@pytest.mark.asyncio
async def test_config_version_methods_keep_sync_driver_working(device_service):
    driver = _SyncConfigVersionDriver()
    device_service._driver_instances["dev2"] = driver

    assert await device_service.save_config_version("dev2", {"points": []}) == 3
    assert await device_service.get_config_current("dev2") == {"points": []}
    assert await device_service.get_config_versions("dev2") == [{"version": 3}]
    assert await device_service.get_config_version_config("dev2", 3) == {"version": 3, "points": []}
    result = await device_service.rollback_config("dev2", 3)
    assert result == {"version": 4, "config": {"points": []}}
    assert driver._config == {"points": []}
    assert await device_service.get_config_audit_trail("dev2") == []
    assert await device_service.diff_config_versions("dev2", 1, 3) is None


def test_count_devices_by_driver(device_service):
    modbus, fins = MagicMock(), MagicMock()
    modbus.plugin_name, fins.plugin_name = "modbus_tcp", "fins"
    device_service._driver_instances.update({"a": modbus, "b": modbus, "c": fins})

    assert device_service.count_devices_by_driver() == {"modbus_tcp": 2, "fins": 1}


@pytest.mark.asyncio
async def test_system_driver_registry_check_uses_real_registry_api():
    """注册表没有 list_drivers()：健康检查此前恒返回 0 个驱动"""
    from edgelite.api.system import _check_drivers

    class _Modbus:
        plugin_name = "modbus_tcp"

    class _Fins:
        plugin_name = "fins"

    registry = MagicMock()
    registry.items.return_value = [("modbus_tcp", _Modbus), ("modbus", _Modbus), ("fins", _Fins)]
    # 同一驱动类的协议别名应去重；_check_drivers 接收的是服务容器
    state = types.SimpleNamespace(driver_registry=registry)
    assert await _check_drivers(state) == {"status": "up", "registered_count": 2, "drivers": ["fins", "modbus_tcp"]}


# ── 3. PlatformService 模板 / 脚本（企业版组件缺失） ────────────────────────


@pytest.fixture
def service_without_enterprise_modules(monkeypatch):
    """移除 tests/conftest.py 注入的桩模块，模拟社区版运行环境"""
    monkeypatch.delitem(sys.modules, "edgelite.platform.mqtt_utils", raising=False)
    monkeypatch.delitem(sys.modules, "edgelite.platform.js_sandbox", raising=False)
    return PlatformService.__new__(PlatformService)


def test_topic_template_validation_falls_back_to_builtin_rules(service_without_enterprise_modules):
    svc = service_without_enterprise_modules

    ok = svc.validate_topic_template("edgelite/{device_id}/{point_id}")
    assert ok == {"valid": True, "errors": [], "variables": ["device_id", "point_id"]}
    assert svc.validate_topic_template("v1/devices/me/telemetry")["valid"] is True

    assert svc.validate_topic_template("a/#/b")["valid"] is False
    assert svc.validate_topic_template("a/{device_id")["valid"] is False  # 花括号不配对
    assert svc.validate_topic_template("a/{dev id}/c")["valid"] is False  # 变量名非法
    assert svc.validate_topic_template("a/de+vice/c")["valid"] is False  # + 必须独占一层


def test_validate_config_accepts_topic_template_without_mqtt_utils(service_without_enterprise_modules):
    """此前：带 topic_template 的平台配置校验直接 ModuleNotFoundError"""
    svc = service_without_enterprise_modules
    config = {"broker": "127.0.0.1", "port": 1883, "token": "tb-token", "topic_template": "v1/devices/me/telemetry"}

    assert svc.validate_config("thingsboard", config) == []

    bad = dict(config, topic_template="a/#/b")
    assert any(err.startswith("ERR_PLATFORM_VALIDATION_TOPIC:") for err in svc.validate_config("thingsboard", bad))


@pytest.mark.asyncio
async def test_advanced_template_and_script_report_unavailable(service_without_enterprise_modules):
    svc = service_without_enterprise_modules

    adv = svc.validate_advanced_template("{{x}}")
    assert adv["valid"] is False
    assert adv["errors"] and adv["errors"][0].startswith("ERR_FEATURE_UNAVAILABLE")

    preview = svc.preview_template("{{x}}", {"x": 1})
    assert preview["success"] is False
    assert preview["error"].startswith("ERR_FEATURE_UNAVAILABLE")

    script = svc.validate_script("return 1")
    assert script == {
        "valid": False,
        "errors": ["ERR_FEATURE_UNAVAILABLE: JavaScript scripting is not available"],
    }

    executed = await svc.test_script("return 1", {"a": 1})
    assert executed["success"] is False
    assert executed["error"].startswith("ERR_FEATURE_UNAVAILABLE")


# ── 5. OPC UA 配置版本 / 时序存储 / OTA ─────────────────────────────────────


@pytest.fixture
def opcua_modules(monkeypatch):
    """加载真实的 OPC UA 辅助模块。

    tests/test_opcua.py 会把 MagicMock 塞进 sys.modules 以绕开这些模块；这里临时移除后
    再导入，保证测到的是真实实现。
    """
    mods = {}
    for name in ("opcua_config_version", "opcua_ts_store", "opcua_ota"):
        monkeypatch.delitem(sys.modules, f"edgelite.drivers.{name}", raising=False)
        mods[name] = importlib.import_module(f"edgelite.drivers.{name}")
    return mods


@pytest.mark.asyncio
async def test_opcua_config_version_manager_matches_driver_usage(opcua_modules, tmp_path):
    cv = opcua_modules["opcua_config_version"]
    db_path = str(tmp_path / "cv" / "cv.db")
    mgr = cv.OpcUaConfigVersionManager(db_path=db_path)
    v1_points = [{"name": "temp", "address": "ns=2;s=Tag"}]
    v2_points = [{"name": "temp", "address": "ns=2;s=Tag2"}]
    try:
        # save_version 返回 int 版本号（驱动里与 0 比较）
        assert await mgr.save_version("dev1", {"points": v1_points}, "init", "admin") == 1
        assert await mgr.save_version("dev1", {"points": v2_points}, "edit", "admin") == 2

        current = await mgr.get_current("dev1")
        assert current["version"] == 2 and current["config"] == {"points": v2_points}

        versions = await mgr.get_versions("dev1")  # 驱动只传 device_id 一个参数
        assert [v["version"] for v in versions] == [2, 1]
        assert all("config" not in v for v in versions)

        cfg = await mgr.get_version_config("dev1", 1)
        assert cfg["config"] == {"points": v1_points}
        assert await mgr.get_version_config("dev1", 99) is None

        rolled = await mgr.rollback("dev1", 1, "admin")
        assert rolled["version"] == 1 and rolled["config"] == {"points": v1_points}
        assert await mgr.rollback("dev1", 99, "admin") is None

        trail = await mgr.get_audit_trail("dev1", 10)
        assert sorted(t["action"] for t in trail) == ["rollback", "save", "save"]

        diff = await mgr.diff_versions("dev1", 1, 2)
        assert diff["changes"] == [{"key": "points", "old_value": v1_points, "new_value": v2_points}]
        assert "missing" in (await mgr.diff_versions("dev1", 1, 99))
    finally:
        await mgr.stop()

    # 持久化：重新打开后版本历史仍在
    reopened = cv.OpcUaConfigVersionManager(db_path=db_path)
    try:
        assert [v["version"] for v in await reopened.get_versions("dev1")] == [2, 1]
    finally:
        await reopened.stop()


@pytest.mark.asyncio
async def test_opcua_ts_store_and_offline_sync_match_driver_usage(opcua_modules):
    ts_mod = opcua_modules["opcua_ts_store"]
    store = ts_mod.OpcUaTsStore(retention_days=7)
    await store.start()
    since = datetime.now(UTC) - timedelta(minutes=1)

    assert await store.write_read_result("dev1", {"temp": 21.5, "press": 1.2}) == 2
    rows = await store.query("dev1", "temp", since)
    assert [(r["value"], r["quality"]) for r in rows] == [(21.5, "good")]
    assert set(rows[0]) == {"time", "value", "quality"}
    assert (await store.query_latest("dev1", ["temp", "press"]))["press"]["value"] == 1.2
    agg = await store.query("dev1", "temp", since, aggregate="avg", window_seconds=60)
    assert len(agg) == 1 and agg[0]["value"] == 21.5
    assert store.get_stats()["total_writes"] == 2

    uploaded: list[dict] = []

    async def _upload(records):
        uploaded.extend(records)
        return len(records)

    # 驱动 init_data_persistence() 传入的参数必须都被接受
    sync = ts_mod.OpcUaOfflineSyncManager(ts_store=store, sync_interval=0.2, batch_size=10, compress="gzip")
    sync.set_upload_callback(_upload)
    await sync.start()
    try:
        assert await sync.force_sync() == 2
        assert len(uploaded) == 2
        assert await sync.force_sync() == 0  # 没有新数据
        assert len(await store.query("dev1", "temp", since)) == 1  # 同步后历史数据仍可查询

        sync.set_online(False)
        await store.write_read_result("dev1", {"temp": 22.0})
        assert await sync.force_sync() == 0  # 离线：不上传
        assert sync.get_stats()["pending"] == 1
        sync.set_online(True)  # 恢复在线：触发补传
        await asyncio.sleep(0.05)
        assert len(uploaded) == 3
        assert sync.get_stats()["pending"] == 0
    finally:
        await sync.stop()
    await store.stop()
    assert store.get_stats()["connected"] is False


@pytest.mark.asyncio
async def test_opcua_ota_stub_matches_driver_usage(opcua_modules):
    ota = opcua_modules["opcua_ota"]
    # 驱动用这些字段名构造 OtaPackage
    pkg = ota.OtaPackage(
        package_id="p1", version="1.2.0", firmware_url="http://fw/f.bin", firmware_hash="abc", firmware_size=10
    )
    assert (pkg.package_id, pkg.version, pkg.firmware_size) == ("p1", "1.2.0", 10)

    mgr = ota.OpcUaOtaManager()
    assert (await mgr.check_update(pkg))["update_available"] is False
    start = await mgr.start_ota(pkg, {"snapshot": True})
    assert start["ok"] is False and start["error"] == ota.ERR_OTA_NOT_SUPPORTED
    assert (await mgr.rollback_ota())["ok"] is False
    assert mgr.get_progress()["status"] == "idle"
    assert [h["action"] for h in mgr.get_history(10)] == ["start", "rollback"]


# ── 6. 日志占位符守卫 ───────────────────────────────────────────────────────

_LOG_SPEC = re.compile(r"%(?:\([^)]*\))?[#0 +\-]*(?:\d+|\*)?(?:\.\d+|\.\*)?[hlL]?[diouxXeEfFgGcrsa%]")


def _is_logger(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id in ("logger", "log", "LOG", "_logger", "LOGGER")
    if isinstance(node, ast.Attribute):
        return node.attr in ("logger", "log", "LOG", "_logger", "LOGGER")
    return False


def _placeholder_count(fmt: str) -> int:
    count = 0
    for match in _LOG_SPEC.finditer(fmt):
        if match.group(0) == "%%":
            continue
        count += 2 if "*" in match.group(0) else 1
    return count


def test_logging_calls_have_matching_placeholders():
    """logger.<level>("fmt", *args) 的占位符数量必须与参数数量一致。

    回归：mqtt_client.py 的 UTF-8 解码失败告警给了 2 个参数却只有 1 个 %s，logging 内部
    格式化抛 TypeError，这条告警永远打不出来。
    """
    import edgelite

    package_root = edgelite.__file__.rsplit("__init__", 1)[0]
    levels = {"debug", "info", "warning", "error", "critical", "exception", "warn"}
    mismatches: list[str] = []
    for dirpath, _dirs, files in os.walk(package_root):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            tree = ast.parse(open(path, encoding="utf-8").read())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                func = node.func
                first = 1 if (func.attr == "log" and _is_logger(func.value)) else 0
                if first == 0 and not (func.attr in levels and _is_logger(func.value)):
                    continue
                args = node.args
                if len(args) <= first:
                    continue
                fmt_node = args[first]
                if not (isinstance(fmt_node, ast.Constant) and isinstance(fmt_node.value, str)):
                    continue  # f-string / 变量格式串无法静态判断
                placeholders = _placeholder_count(fmt_node.value)
                if placeholders != len(args) - first - 1:
                    mismatches.append(f"{os.path.relpath(path, package_root)}:{node.lineno}")
    assert not mismatches, f"日志格式串占位符与参数数量不一致: {mismatches}"


# ── 7. 脚本沙箱 ─────────────────────────────────────────────────────────────

_NL = "\n"


def _script_threads():
    import threading

    from edgelite.engine import sandbox

    return [t for t in threading.enumerate() if t.name == sandbox._SCRIPT_THREAD_NAME]


async def _wait_script_threads_gone(timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while _script_threads() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    return not _script_threads()


def _free_script_slots() -> int:
    from edgelite.engine import sandbox

    acquired = 0
    for _ in range(sandbox._MAX_SCRIPT_WORKERS):
        acquired += 1 if sandbox._script_slots.acquire(blocking=False) else 0
    for _ in range(acquired):
        sandbox._script_slots.release()
    return acquired


def _src(*lines: str) -> str:
    return _NL.join(lines)


# 能通过白名单、但试图熬过超时的脚本（每一项都必须被超时打断）
_RUNAWAY_LOOPS = {
    "plain": _src("x = 0", "while True:", "    x += 1", "result = x"),
    "except Exception loop": _src(
        "x = 0", "while True:", "    try:", "        x += 1", "    except Exception:", "        continue", "result = x"
    ),
    "bare except loop": _src(
        "x = 0", "while True:", "    try:", "        x += 1", "    except:", "        continue", "result = x"
    ),
    "except BaseException loop": _src(
        "x = 0",
        "while True:",
        "    try:",
        "        x += 1",
        "    except BaseException:",
        "        continue",
        "result = x",
    ),
    "alias catch": _src(
        "E = BaseException",
        "x = 0",
        "while True:",
        "    try:",
        "        x += 1",
        "    except E:",
        "        continue",
        "result = x",
    ),
    "nested loop caught by alias": _src(
        "E = BaseException",
        "x = 0",
        "while True:",
        "    try:",
        "        while True:",
        "            x += 1",
        "    except E:",
        "        continue",
        "result = x",
    ),
    "loop inside except handler": _src(
        "y = 0",
        "while True:",
        "    try:",
        "        raise ValueError('x')",
        "    except:",
        "        while True:",
        "            y += 1",
        "result = y",
    ),
    "finally": _src(
        "x = 0", "while True:", "    try:", "        x += 1", "    finally:", "        y = x", "result = x"
    ),
    "nested function loop": _src(
        "def spin(n):", "    while True:", "        n += 1", "    return n", "result = spin(0)"
    ),
    "nested function with bare except": _src(
        "def spin():",
        "    while True:",
        "        try:",
        "            x = 1",
        "        except:",
        "            continue",
        "    return 1",
        "result = spin()",
    ),
    "nested loops": _src(
        "x = 0",
        "for i in range(10):",
        "    for j in range(10):",
        "        while True:",
        "            x += 1",
        "result = x",
    ),
}


@pytest.mark.asyncio
async def test_sandbox_scripts_can_handle_exceptions():
    """白名单允许 try/except，但受限内建里没有任何异常类——`except ZeroDivisionError:` 曾必然 NameError"""
    from edgelite.engine.sandbox import run_script_safely

    script = "try:" + _NL + "    result = 1 / 0" + _NL + "except ZeroDivisionError:" + _NL + "    result = 0"
    assert await run_script_safely(script, {"result": None}) == 0

    script = (
        "result = 0"
        + _NL
        + "for item in [1, 'x', 3]:"
        + _NL
        + "    try:"
        + _NL
        + "        result += int(item)"
        + _NL
        + "    except (TypeError, ValueError):"
        + _NL
        + "        result -= 1"
    )
    assert await run_script_safely(script, {"result": None}) == 3


@pytest.mark.parametrize(
    "script",
    [
        _src("try:", "    result = 1", "except:", "    result = 2"),
        _src("try:", "    result = 1", "except BaseException:", "    result = 2"),
        _src("try:", "    result = 1", "except (ValueError, BaseException):", "    result = 2"),
        _src("try:", "    result = 1", "except BaseException as e:", "    result = 2"),
    ],
    ids=["bare", "BaseException", "tuple", "as"],
)
def test_sandbox_keeps_all_catching_handlers_allowed(script):
    """捕获一切的异常处理器是既有脚本可用的写法，继续放行。

    超时不靠禁止这类写法保证，而是靠编译前注入到每个循环体开头的到期检查（见失控脚本用例）：
    早先"跟踪函数抛异常"的做法在异常被捕获后会失效，按名字拒绝 except BaseException 也能被
    `E = BaseException` 起别名绕过。
    """
    from edgelite.engine.sandbox import _validate_script_ast

    assert _validate_script_ast(script) is True


@pytest.mark.asyncio
async def test_sandbox_all_catching_handler_runs():
    from edgelite.engine.sandbox import run_script_safely

    script = _src("try:", "    raise ValueError('x')", "except BaseException as e:", "    result = str(e)")
    assert await run_script_safely(script, {"result": None}) == "x"


@pytest.mark.parametrize(
    "script",
    [
        _src("def __edgelite_deadline_check__():", "    pass", "result = 1"),
        _src("def spin(__edgelite_deadline_check__=print):", "    while True:", "        x = 1", "result = spin()"),
        _src("f = lambda __edgelite_deadline_check__: 1", "result = f(1)"),
        _src(
            "try:", "    result = 1 / 0", "except ZeroDivisionError as __edgelite_deadline_check__:", "    result = 2"
        ),
        _src("__edgelite_deadline_check__ = print", "result = 1"),
        _src("result = __edgelite_deadline_check__()"),
    ],
    ids=["def", "param-default", "lambda-param", "except-as", "assign", "call"],
)
def test_sandbox_rejects_shadowing_the_deadline_check(script):
    """到期检查是编译前注入脚本的一次调用；脚本不得用 dunder 标识符定义同名函数 / 参数来覆盖或遮蔽它"""
    from edgelite.engine.sandbox import _DEADLINE_CHECK_NAME, _validate_script_ast

    assert _DEADLINE_CHECK_NAME in script
    assert _validate_script_ast(script) is False


@pytest.mark.asyncio
async def test_sandbox_contains_base_exception_raised_by_script():
    """脚本 raise BaseException：按执行失败处理，不能泄漏到只捕获 Exception 的调用方"""
    from edgelite.engine.sandbox import run_script_safely

    assert await run_script_safely("raise BaseException('boom')", {"result": None}) is False


@pytest.mark.asyncio
async def test_sandbox_stops_runaway_scripts_without_leaking():
    """死循环脚本必须被真正打断，线程与并发名额都要收回。

    回归：原实现把脚本放进 asyncio 全局默认线程池，超时只是"不再等待"——每次评估永久泄漏一个
    满核线程，默认线程池占满后全进程的 to_thread 调用一起卡死。
    """
    from edgelite.engine import sandbox

    for name, script in _RUNAWAY_LOOPS.items():
        assert sandbox._validate_script_ast(script), name
        for _ in range(2):
            with pytest.raises(TimeoutError):
                await sandbox.run_script_safely(script, {"result": False}, timeout=0.2)
    assert await _wait_script_threads_gone(), "超时后的脚本线程必须结束"
    assert _free_script_slots() == sandbox._MAX_SCRIPT_WORKERS


@pytest.mark.asyncio
async def test_sandbox_does_not_use_default_executor():
    from edgelite.engine.sandbox import run_script_safely

    async def _forbidden(*args, **kwargs):
        raise AssertionError("脚本不得放进 asyncio 默认线程池")

    with patch("asyncio.to_thread", new=_forbidden):
        assert await run_script_safely("result = 1 + 1", {"result": False}) == 2


@pytest.mark.asyncio
async def test_sandbox_fails_fast_when_saturated():
    """脚本线程名额占满时立即失败，而不是排队等到超时"""
    from edgelite.engine import sandbox

    for _ in range(sandbox._MAX_SCRIPT_WORKERS):
        assert sandbox._script_slots.acquire(blocking=False)
    try:
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="saturated"):
            await sandbox.run_script_safely("result = 1", {"result": False}, timeout=5.0)
        assert time.monotonic() - started < 1.0
    finally:
        for _ in range(sandbox._MAX_SCRIPT_WORKERS):
            sandbox._script_slots.release()


# ── 8. 固件路径白名单 ───────────────────────────────────────────────────────


def test_firmware_path_rejects_unc_and_absolute_without_resolving(monkeypatch, tmp_path):
    """UNC / 绝对路径必须在词法检查阶段拒绝，不能先 resolve()。

    回归：Windows 上 Path(UNC).resolve() 会真的访问网络路径，不可达时抛 FileNotFoundError
    （接口 500），且这是 async 端点里的同步调用——单个请求即可阻塞整个事件循环直到网络查找超时。
    """
    from pathlib import Path

    from fastapi import HTTPException

    from edgelite.api import firmware_signature as fs

    def _boom(self, strict=False):
        raise AssertionError("resolve() must not be called before the lexical path checks")

    unc = r"\\server\share\fw.bin"
    assert unc.startswith("\\\\")  # 确认是 UNC 路径（两个反斜杠开头），而不是被转义破坏的字符串

    monkeypatch.delenv("EDGELITE_OTA_DIR", raising=False)
    with patch.object(Path, "resolve", _boom):
        for bad in (unc, r"C:\Windows\system32\config\SAM", "data/ota/../x.bin"):
            with pytest.raises(HTTPException) as e:
                fs._validate_firmware_path(bad)
            assert e.value.status_code == 400, bad
    # POSIX 风格绝对路径在 Windows 上 is_absolute() 为假，会走 resolve()（仅本地路径，不触网）后按白名单拒绝
    with pytest.raises(HTTPException) as e:
        fs._validate_firmware_path("/etc/shadow")
    assert e.value.status_code in (400, 404)

    # 显式配置的自定义 OTA 目录：目录内的绝对路径放行，目录外（含 UNC）的仍然拒绝
    custom = tmp_path / "custom-ota"
    custom.mkdir()
    (custom / "fw.bin").write_bytes(b"fw")
    monkeypatch.setenv("EDGELITE_OTA_DIR", str(custom))
    assert fs._validate_firmware_path(str(custom / "fw.bin")) == (custom / "fw.bin").resolve()
    original_resolve = Path.resolve

    def _no_unc_resolve(self, strict=False):
        # 对配置目录本身 resolve() 是允许的；UNC 路径在词法比较阶段就应被拒绝，不得触网
        assert not str(self).startswith("\\\\"), "UNC path must be rejected without calling resolve()"
        return original_resolve(self, strict)

    with patch.object(Path, "resolve", _no_unc_resolve):
        with pytest.raises(HTTPException) as e:
            fs._validate_firmware_path(unc)
        assert e.value.status_code == 400


# ── 9. 指标端点认证：Bearer 形式的 Grafana API Key ─────────────────────────


def _metrics_request(headers: dict[str, str]):
    from starlette.requests import Request

    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/metrics",
            "query_string": b"",
            # ASGI 规范要求原始作用域里的请求头名为小写
            "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()],
            "client": ("127.0.0.1", 12345),
        }
    )


@pytest.fixture
def grafana_key(monkeypatch):
    from edgelite.api import metrics as metrics_api

    config = types.SimpleNamespace(grafana=types.SimpleNamespace(api_key="elg_test_key|w\\ith$&specials"))
    monkeypatch.setattr("edgelite.config.get_config", lambda: config)
    return metrics_api, config


@pytest.mark.asyncio
async def test_metrics_auth_accepts_grafana_api_key_as_bearer(grafana_key):
    """Prometheus v2.45 只支持 `Authorization: Bearer <凭证>`（不支持自定义请求头），否则自带监控栈永远 401"""
    metrics_api, config = grafana_key

    info = await metrics_api._authenticate_metrics(
        _metrics_request({"Authorization": f"Bearer {config.grafana.api_key}"})
    )
    assert info == {
        "auth_type": "api_key",
        "username": "grafana",
        "role": "api_key",
        "api_key_scope": metrics_api.APIKeyPermission.METRICS_READ,
    }


@pytest.mark.asyncio
async def test_metrics_auth_rejects_wrong_or_missing_credentials(grafana_key):
    metrics_api, _config = grafana_key

    for headers in ({}, {"Authorization": "Bearer not-the-key"}, {"X-API-Key": "not-the-key"}):
        with pytest.raises(HTTPException) as e:
            await metrics_api._authenticate_metrics(_metrics_request(headers))
        assert e.value.status_code == 401, headers


@pytest.mark.asyncio
async def test_metrics_auth_bearer_api_key_requires_configured_key(monkeypatch):
    """未配置 grafana.api_key 时，任何 Bearer 凭证都不能走 API Key 通道（尤其空 Key 不能匹配任意值）"""
    from edgelite.api import metrics as metrics_api

    monkeypatch.setattr(
        "edgelite.config.get_config", lambda: types.SimpleNamespace(grafana=types.SimpleNamespace(api_key=""))
    )
    assert await metrics_api._bearer_api_key_auth("") is None
    assert await metrics_api._bearer_api_key_auth("anything") is None
    monkeypatch.setattr("edgelite.config.get_config", lambda: types.SimpleNamespace())
    assert await metrics_api._bearer_api_key_auth("anything") is None
