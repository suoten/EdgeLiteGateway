"""配置热加载管理器单元测试。

覆盖 src/edgelite/config_reload.py：SHA256 变更检测、回滚基准、指数退避、
错误去重、回调通知、变更历史、回滚、手动 reload、watch/unwatch、单例工厂。
"""

from __future__ import annotations

import pytest

from edgelite.config_reload import (
    ConfigChange,
    ConfigHotReloader,
    HotReloadConfig,
    get_config_hot_reloader,
)

# ─── _detect_config_type ───


def test_detect_config_type_device():
    assert ConfigHotReloader._detect_config_type("/path/devices.yaml") == "device"


def test_detect_config_type_rule():
    assert ConfigHotReloader._detect_config_type("/path/rules.json") == "rule"


def test_detect_config_type_alarm():
    assert ConfigHotReloader._detect_config_type("/path/alarms.yaml") == "alarm"


def test_detect_config_type_driver():
    assert ConfigHotReloader._detect_config_type("/path/drivers.yaml") == "driver"


def test_detect_config_type_system_default():
    assert ConfigHotReloader._detect_config_type("/path/config.yaml") == "system"


def test_detect_config_type_case_insensitive():
    assert ConfigHotReloader._detect_config_type("/path/DEVICES.yaml") == "device"
    assert ConfigHotReloader._detect_config_type("/path/Rules.json") == "rule"


# ─── _compute_backoff_interval ───


def test_backoff_zero_failures():
    """0 次失败时使用 watch_interval。"""
    reloader = ConfigHotReloader(HotReloadConfig(watch_interval=5.0))
    assert reloader._compute_backoff_interval() == 5.0


def test_backoff_one_failure():
    """1 次失败时 5*2=10s。"""
    reloader = ConfigHotReloader(HotReloadConfig(watch_interval=5.0))
    reloader._consecutive_failures = 1
    assert reloader._compute_backoff_interval() == 10.0


def test_backoff_two_failures():
    """2 次失败时 5*4=20s。"""
    reloader = ConfigHotReloader(HotReloadConfig(watch_interval=5.0))
    reloader._consecutive_failures = 2
    assert reloader._compute_backoff_interval() == 20.0


def test_backoff_capped_at_60s():
    """退避上限 60s。"""
    reloader = ConfigHotReloader(HotReloadConfig(watch_interval=5.0))
    reloader._consecutive_failures = 10  # 5*1024=5120, 应被截断为 60
    assert reloader._compute_backoff_interval() == 60.0


# ─── _record_parse_failure ───


def test_record_parse_failure_increments_counter():
    """_record_parse_failure 累加失败计数。"""
    reloader = ConfigHotReloader()
    assert reloader._consecutive_failures == 0
    reloader._record_parse_failure("/path/config.yaml", ValueError("bad yaml"))
    assert reloader._consecutive_failures == 1


def test_record_parse_failure_dedup_same_error():
    """相同错误特征只记录一次高级别日志。"""
    reloader = ConfigHotReloader()
    exc = ValueError("bad yaml")
    reloader._record_parse_failure("/path/config.yaml", exc)
    first_sig = reloader._last_error_signature
    reloader._record_parse_failure("/path/config.yaml", exc)
    assert reloader._last_error_signature == first_sig
    assert reloader._consecutive_failures == 2


def test_record_parse_failure_different_error_updates_signature():
    """不同错误特征更新签名。"""
    reloader = ConfigHotReloader()
    reloader._record_parse_failure("/path/config.yaml", ValueError("error1"))
    sig1 = reloader._last_error_signature
    reloader._record_parse_failure("/path/config.yaml", ValueError("error2"))
    assert reloader._last_error_signature != sig1


# ─── register_callback / unregister_callback ───


def test_register_callback():
    """register_callback 添加回调到对应类型。"""
    reloader = ConfigHotReloader()
    cb = lambda change: None  # noqa: E731
    reloader.register_callback("device", cb)
    assert cb in reloader._change_callbacks["device"]


def test_register_callback_new_type():
    """register_callback 支持自定义类型。"""
    reloader = ConfigHotReloader()
    cb = lambda change: None  # noqa: E731
    reloader.register_callback("custom_type", cb)
    assert cb in reloader._change_callbacks["custom_type"]


def test_unregister_callback():
    """unregister_callback 移除回调。"""
    reloader = ConfigHotReloader()
    cb = lambda change: None  # noqa: E731
    reloader.register_callback("device", cb)
    reloader.unregister_callback("device", cb)
    assert cb not in reloader._change_callbacks["device"]


def test_unregister_callback_nonexistent_no_error():
    """unregister_callback 不存在的回调不抛错。"""
    reloader = ConfigHotReloader()
    reloader.unregister_callback("device", lambda c: None)


# ─── _notify_callbacks ───


@pytest.mark.asyncio
async def test_notify_callbacks_specific_type():
    """特定类型回调被触发。"""
    reloader = ConfigHotReloader()
    received = []
    reloader.register_callback("device", lambda change: received.append(change))
    change = ConfigChange(config_type="device", config_id="dev1", old_value=None, new_value={"k": "v"})
    await reloader._notify_callbacks("device", change)
    assert len(received) == 1
    assert received[0] is change


@pytest.mark.asyncio
async def test_notify_callbacks_wildcard():
    """通配符 * 回调被触发。"""
    reloader = ConfigHotReloader()
    received = []
    reloader.register_callback("*", lambda change: received.append(change))
    change = ConfigChange(config_type="device", config_id="dev1", old_value=None, new_value={})
    await reloader._notify_callbacks("device", change)
    assert len(received) == 1


@pytest.mark.asyncio
async def test_notify_callbacks_async():
    """异步回调被正确 await。"""
    reloader = ConfigHotReloader()
    received = []

    async def async_cb(change):
        received.append(change)

    reloader.register_callback("rule", async_cb)
    change = ConfigChange(config_type="rule", config_id="r1", old_value=None, new_value={})
    await reloader._notify_callbacks("rule", change)
    assert len(received) == 1


@pytest.mark.asyncio
async def test_notify_callbacks_exception_does_not_propagate():
    """回调抛异常不中断其他回调。"""
    reloader = ConfigHotReloader()
    received = []

    def bad_cb(change):
        raise RuntimeError("boom")

    def good_cb(change):
        received.append(change)

    reloader.register_callback("device", bad_cb)
    reloader.register_callback("device", good_cb)
    change = ConfigChange(config_type="device", config_id="dev1", old_value=None, new_value={})
    await reloader._notify_callbacks("device", change)
    assert len(received) == 1


# ─── _record_change / get_change_history ───


@pytest.mark.asyncio
async def test_record_and_get_change_history():
    """记录变更并查询历史。"""
    reloader = ConfigHotReloader(HotReloadConfig(max_history=10))
    change = ConfigChange(config_type="device", config_id="dev1", old_value=None, new_value={"v": 1})
    await reloader._record_change(change)
    history = await reloader.get_change_history()
    assert len(history) == 1
    assert history[0]["config_type"] == "device"
    assert history[0]["config_id"] == "dev1"


@pytest.mark.asyncio
async def test_change_history_filtered_by_type():
    """按类型过滤变更历史。"""
    reloader = ConfigHotReloader()
    await reloader._record_change(ConfigChange(config_type="device", config_id="d1", old_value=None, new_value={}))
    await reloader._record_change(ConfigChange(config_type="rule", config_id="r1", old_value=None, new_value={}))
    device_history = await reloader.get_change_history(config_type="device")
    assert len(device_history) == 1
    assert device_history[0]["config_type"] == "device"


@pytest.mark.asyncio
async def test_change_history_limit():
    """limit 限制返回数量。"""
    reloader = ConfigHotReloader(HotReloadConfig(max_history=100))
    for i in range(10):
        await reloader._record_change(
            ConfigChange(config_type="device", config_id=f"d{i}", old_value=None, new_value={})
        )
    history = await reloader.get_change_history(limit=5)
    assert len(history) == 5


@pytest.mark.asyncio
async def test_change_history_max_history_trims():
    """超过 max_history 时自动截断。"""
    reloader = ConfigHotReloader(HotReloadConfig(max_history=3))
    for i in range(5):
        await reloader._record_change(
            ConfigChange(config_type="device", config_id=f"d{i}", old_value=None, new_value={})
        )
    history = await reloader.get_change_history()
    assert len(history) == 3  # 只保留最近 3 条


# ─── rollback ───


@pytest.mark.asyncio
async def test_rollback_success():
    """回滚到上一个版本。"""
    reloader = ConfigHotReloader()
    old_val = {"v": 1}
    new_val = {"v": 2}
    change = ConfigChange(config_type="device", config_id="dev1", old_value=old_val, new_value=new_val)
    await reloader._record_change(change)
    result = await reloader.rollback("device", "dev1")
    assert result is True
    history = await reloader.get_change_history()
    # 应有 2 条记录（原始变更 + 回滚变更）
    assert len(history) == 2
    rollback_entry = history[-1]
    assert rollback_entry["changed_by"] == "rollback"


@pytest.mark.asyncio
async def test_rollback_no_previous_version():
    """无历史版本时回滚返回 False。"""
    reloader = ConfigHotReloader()
    result = await reloader.rollback("device", "nonexistent")
    assert result is False


@pytest.mark.asyncio
async def test_rollback_old_value_none_fails():
    """old_value 为 None 时无法回滚。"""
    reloader = ConfigHotReloader()
    change = ConfigChange(config_type="device", config_id="dev1", old_value=None, new_value={"v": 1})
    await reloader._record_change(change)
    result = await reloader.rollback("device", "dev1")
    assert result is False


# ─── reload_device_config / reload_rule_config ───


@pytest.mark.asyncio
async def test_reload_device_config():
    """手动设备配置热更新记录变更并触发回调。"""
    reloader = ConfigHotReloader()
    received = []
    reloader.register_callback("device", lambda c: received.append(c))
    change = await reloader.reload_device_config("dev1", {"name": "new"})
    assert change.config_type == "device"
    assert change.config_id == "dev1"
    assert change.changed_by == "manual"
    assert len(received) == 1


@pytest.mark.asyncio
async def test_reload_rule_config():
    """手动规则配置热更新。"""
    reloader = ConfigHotReloader()
    received = []
    reloader.register_callback("rule", lambda c: received.append(c))
    change = await reloader.reload_rule_config("rule1", {"enabled": True})
    assert change.config_type == "rule"
    assert change.config_id == "rule1"
    assert len(received) == 1


# ─── _watch_file / unwatch_file ───


@pytest.mark.asyncio
async def test_watch_file_yaml(tmp_path):
    """_watch_file 监控 YAML 文件并计算哈希。"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("key: value\n", encoding="utf-8")
    reloader = ConfigHotReloader()
    await reloader._watch_file(str(config_file))
    assert str(config_file) in reloader._watched_files
    # _old_ 前缀存储解析后的值
    assert reloader._watched_files[f"_old_{config_file}"] == {"key": "value"}


@pytest.mark.asyncio
async def test_watch_file_json(tmp_path):
    """_watch_file 监控 JSON 文件。"""
    config_file = tmp_path / "config.json"
    config_file.write_text('{"key": "value"}', encoding="utf-8")
    reloader = ConfigHotReloader()
    await reloader._watch_file(str(config_file))
    assert str(config_file) in reloader._watched_files
    assert reloader._watched_files[f"_old_{config_file}"] == {"key": "value"}


@pytest.mark.asyncio
async def test_watch_file_nonexistent():
    """_watch_file 不存在的文件不抛错。"""
    reloader = ConfigHotReloader()
    await reloader._watch_file("/nonexistent/path/config.yaml")
    assert "/nonexistent/path/config.yaml" not in reloader._watched_files


@pytest.mark.asyncio
async def test_unwatch_file(tmp_path):
    """unwatch_file 移除监控文件。"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("key: value\n", encoding="utf-8")
    reloader = ConfigHotReloader()
    await reloader._watch_file(str(config_file))
    reloader.unwatch_file(str(config_file))
    assert str(config_file) not in reloader._watched_files
    assert f"_old_{config_file}" not in reloader._watched_files


# ─── _check_changes ───


@pytest.mark.asyncio
async def test_check_changes_detects_modification(tmp_path):
    """_check_changes 检测到文件修改并触发回调。"""
    config_file = tmp_path / "devices.yaml"
    config_file.write_text("device1:\n  name: dev1\n", encoding="utf-8")

    reloader = ConfigHotReloader(HotReloadConfig(auto_backup=False))
    await reloader._watch_file(str(config_file))

    received = []
    reloader.register_callback("device", lambda c: received.append(c))

    # 修改文件
    config_file.write_text("device1:\n  name: dev1_updated\n", encoding="utf-8")
    await reloader._check_changes()

    assert len(received) == 1
    assert received[0].config_type == "device"
    # 哈希应被更新
    new_hash = reloader._watched_files[str(config_file)]
    assert new_hash != ""


@pytest.mark.asyncio
async def test_check_changes_no_modification_no_callback(tmp_path):
    """_check_changes 文件未修改时不触发回调。"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("key: value\n", encoding="utf-8")

    reloader = ConfigHotReloader(HotReloadConfig(auto_backup=False))
    await reloader._watch_file(str(config_file))

    received = []
    reloader.register_callback("system", lambda c: received.append(c))

    await reloader._check_changes()
    assert len(received) == 0


@pytest.mark.asyncio
async def test_check_changes_resets_backoff_on_success(tmp_path):
    """_check_changes 解析成功后重置退避计数。"""
    config_file = tmp_path / "devices.yaml"
    config_file.write_text("key: value\n", encoding="utf-8")

    reloader = ConfigHotReloader(HotReloadConfig(auto_backup=False))
    await reloader._watch_file(str(config_file))

    # 模拟之前有失败
    reloader._consecutive_failures = 3
    reloader._last_error_signature = "some_sig"

    # 修改文件（合法变更）
    config_file.write_text("key: new_value\n", encoding="utf-8")
    await reloader._check_changes()

    assert reloader._consecutive_failures == 0
    assert reloader._last_error_signature is None


@pytest.mark.asyncio
async def test_check_changes_parse_failure_increments_backoff(tmp_path):
    """_check_changes 解析失败时累加退避计数。"""
    config_file = tmp_path / "devices.yaml"
    config_file.write_text("key: value\n", encoding="utf-8")

    reloader = ConfigHotReloader(HotReloadConfig(auto_backup=False))
    await reloader._watch_file(str(config_file))

    # 写入非法 YAML
    config_file.write_text("key: [invalid yaml\n", encoding="utf-8")
    await reloader._check_changes()

    assert reloader._consecutive_failures > 0


# ─── _backup_config ───


@pytest.mark.asyncio
async def test_backup_config_creates_backup(tmp_path):
    """_backup_config 创建备份文件。"""
    backup_dir = tmp_path / "backups"
    reloader = ConfigHotReloader(HotReloadConfig(auto_backup=True, backup_dir=str(backup_dir)))
    config_file = tmp_path / "config.yaml"
    config_file.write_text("key: value\n", encoding="utf-8")

    await reloader._backup_config(str(config_file), "key: value\n")

    backups = list(backup_dir.glob("config_*.yaml"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "key: value\n"


@pytest.mark.asyncio
async def test_backup_config_disabled(tmp_path):
    """auto_backup=False 时不创建备份。"""
    backup_dir = tmp_path / "backups"
    reloader = ConfigHotReloader(HotReloadConfig(auto_backup=False, backup_dir=str(backup_dir)))
    config_file = tmp_path / "config.yaml"
    await reloader._backup_config(str(config_file), "content")
    assert not backup_dir.exists() or not list(backup_dir.iterdir())


# ─── start / stop ───


@pytest.mark.asyncio
async def test_start_stop_with_empty_config_dir(tmp_path, monkeypatch):
    """start/stop 在无配置文件的目录下正常工作。"""
    monkeypatch.setenv("EDGELITE_CONFIG", str(tmp_path / "config.yaml"))
    reloader = ConfigHotReloader(HotReloadConfig(watch_interval=0.01))
    await reloader.start()
    assert reloader._running is True
    assert reloader._task is not None
    await reloader.stop()
    assert reloader._running is False
    assert reloader._task is None


@pytest.mark.asyncio
async def test_start_idempotent(tmp_path, monkeypatch):
    """重复 start 不创建多个监控任务。"""
    monkeypatch.setenv("EDGELITE_CONFIG", str(tmp_path / "config.yaml"))
    reloader = ConfigHotReloader(HotReloadConfig(watch_interval=0.01))
    await reloader.start()
    task1 = reloader._task
    await reloader.start()
    assert reloader._task is task1
    await reloader.stop()


# ─── get_config_hot_reloader ───


def test_get_config_hot_reloader_singleton():
    """get_config_hot_reloader 返回单例。"""
    import edgelite.config_reload as cr_module

    # 重置全局实例
    cr_module._config_hot_reloader = None
    r1 = get_config_hot_reloader()
    r2 = get_config_hot_reloader()
    assert r1 is r2
    # 清理
    cr_module._config_hot_reloader = None


# ─── _scan_config_files ───


@pytest.mark.asyncio
async def test_scan_config_files_finds_yaml_and_json(tmp_path, monkeypatch):
    """_scan_config_files 扫描 .yaml 和 .json 文件。"""
    (tmp_path / "devices.yaml").write_text("key: value\n", encoding="utf-8")
    (tmp_path / "rules.json").write_text('{"k": "v"}', encoding="utf-8")
    (tmp_path / "readme.txt").write_text("not config", encoding="utf-8")

    monkeypatch.setenv("EDGELITE_CONFIG", str(tmp_path / "config.yaml"))
    reloader = ConfigHotReloader()
    await reloader._scan_config_files()

    assert str(tmp_path / "devices.yaml") in reloader._watched_files
    assert str(tmp_path / "rules.json") in reloader._watched_files
    assert str(tmp_path / "readme.txt") not in reloader._watched_files


@pytest.mark.asyncio
async def test_scan_config_files_nonexistent_dir(monkeypatch):
    """_scan_config_files 配置目录不存在时不抛错。"""
    monkeypatch.setenv("EDGELITE_CONFIG", "/nonexistent/dir/config.yaml")
    reloader = ConfigHotReloader()
    await reloader._scan_config_files()
    assert len(reloader._watched_files) == 0
