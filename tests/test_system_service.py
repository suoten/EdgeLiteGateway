"""系统管理业务逻辑测试 - services/system_service.py（单数 service）"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, "src")

from edgelite.services import system_service as ss_module
from edgelite.services.system_service import SystemService


def _make_repos():
    return {
        "device_repo": AsyncMock(),
        "rule_repo": AsyncMock(),
        "alarm_repo": AsyncMock(),
        "user_repo": AsyncMock(),
    }


def _make_scheduler():
    sched = AsyncMock()
    sched.get_active_devices = AsyncMock(return_value=["d1", "d2"])
    sched.get_task_count = AsyncMock(return_value=5)
    return sched


def _make_service(start_time=0.0):
    repos = _make_repos()
    database = AsyncMock()
    scheduler = _make_scheduler()
    svc = SystemService(
        database=database,
        device_repo=repos["device_repo"],
        rule_repo=repos["rule_repo"],
        alarm_repo=repos["alarm_repo"],
        user_repo=repos["user_repo"],
        scheduler=scheduler,
        start_time=start_time,
    )
    return svc, repos, database, scheduler


@pytest.fixture
def patch_config(tmp_path, monkeypatch):
    backup_dir = tmp_path / "backups"
    sqlite_path = str(tmp_path / "edgelite.db")
    cfg = SimpleNamespace(
        database=SimpleNamespace(backup_dir=str(backup_dir), sqlite_path=sqlite_path)
    )
    monkeypatch.setattr(ss_module, "get_config", lambda: cfg)
    return cfg


@pytest.fixture
def patch_app_state(monkeypatch):
    app_state = MagicMock()
    session_cm = AsyncMock()
    session_cm.__aenter__ = AsyncMock(return_value=MagicMock())
    session_cm.__aexit__ = AsyncMock(return_value=False)
    db_mock = MagicMock()
    db_mock.get_session = MagicMock(return_value=session_cm)
    app_state.database = db_mock
    app_state.evaluator = None
    monkeypatch.setattr("edgelite.app._app_state", app_state)
    return app_state


class TestCollectResources:
    async def test_with_psutil(self, monkeypatch):
        svc, *_ = _make_service()
        memory = SimpleNamespace(total=100, used=50, percent=50.0, available=50)
        disk = SimpleNamespace(total=200, used=100, percent=50.0, free=100)
        net = SimpleNamespace(bytes_sent=10, bytes_recv=20)
        monkeypatch.setattr(ss_module, "psutil", MagicMock())
        ss_module.psutil.cpu_percent = lambda interval=0.1: 12.5
        # psutil.cpu_count() defaults to logical=True; both calls return logical count
        ss_module.psutil.cpu_count = lambda logical=True: 8 if logical else 4
        ss_module.psutil.virtual_memory = lambda: memory
        ss_module.psutil.disk_usage = lambda _p: disk
        ss_module.psutil.net_io_counters = lambda: net
        monkeypatch.setattr(ss_module.os, "getloadavg", lambda: (1.0, 2.0, 3.0), raising=False)
        result = await svc.collect_resources()
        assert result["cpu_percent"] == 12.5
        assert result["cpu_count"] == 8
        assert result["cpu_count_logical"] == 8
        assert result["memory_total"] == 100
        assert result["memory_used"] == 50
        assert result["memory_available"] == 50
        assert result["memory_percent"] == 50.0
        assert result["disk_total"] == 200
        assert result["disk_used"] == 100
        assert result["disk_free"] == 100
        assert result["disk_percent"] == 50.0
        assert result["net_bytes_sent"] == 10
        assert result["net_bytes_recv"] == 20
        assert result["load_avg_1m"] == 1.0
        assert result["load_avg_5m"] == 2.0
        assert result["load_avg_15m"] == 3.0
        assert "collected_at" in result

    async def test_without_psutil(self, monkeypatch):
        svc, *_ = _make_service()
        monkeypatch.setattr(ss_module, "psutil", None)
        result = await svc.collect_resources()
        assert result["cpu_percent"] == 0.0
        assert result["cpu_count"] == 0
        assert result["cpu_count_logical"] == 0
        assert result["memory_total"] == 0
        assert result["memory_used"] == 0
        assert result["memory_available"] == 0
        assert result["memory_percent"] == 0.0
        assert result["disk_total"] == 0
        assert result["disk_used"] == 0
        assert result["disk_free"] == 0
        assert result["disk_percent"] == 0.0
        assert result["net_bytes_sent"] == 0
        assert result["net_bytes_recv"] == 0
        assert result["load_avg_1m"] == 0.0
        assert result["load_avg_15m"] == 0.0

    async def test_load_avg_no_os_getloadavg(self, monkeypatch):
        monkeypatch.delattr(ss_module.os, "getloadavg", raising=False)
        result = SystemService._get_load_avg()
        assert result == (0.0, 0.0, 0.0)

    async def test_load_avg_with_os_getloadavg(self, monkeypatch):
        monkeypatch.setattr(ss_module.os, "getloadavg", lambda: (0.5, 0.6, 0.7), raising=False)
        result = SystemService._get_load_avg()
        assert result == (0.5, 0.6, 0.7)

    async def test_load_avg_swallows_exception(self, monkeypatch):
        def _raise():
            raise OSError("boom")

        monkeypatch.setattr(ss_module.os, "getloadavg", _raise, raising=False)
        result = SystemService._get_load_avg()
        assert result == (0.0, 0.0, 0.0)


class TestGetStatus:
    async def test_with_psutil_and_rule_count_success(self, monkeypatch, patch_app_state):
        svc, repos, database, scheduler = _make_service(start_time=100.0)
        memory = SimpleNamespace(total=1000, used=500, percent=50.0)
        disk = SimpleNamespace(total=2000, used=1000, percent=50.0)
        monkeypatch.setattr(ss_module, "psutil", MagicMock())
        ss_module.psutil.cpu_percent = lambda interval=0.1: 33.3
        ss_module.psutil.virtual_memory = lambda: memory
        ss_module.psutil.disk_usage = lambda _p: disk
        repos["device_repo"].list_all = AsyncMock(return_value=([], 10))
        repos["rule_repo"].list_all = AsyncMock(return_value=([], 7))
        repos["alarm_repo"].list_all = AsyncMock(return_value=([], 3))
        session = MagicMock()
        result_mock = MagicMock()
        result_mock.scalar = MagicMock(return_value=4)
        session.execute = AsyncMock(return_value=result_mock)
        patch_app_state.database.get_session.return_value.__aenter__ = AsyncMock(return_value=session)
        patch_app_state.database.get_session.return_value.__aexit__ = AsyncMock(return_value=False)
        with patch("time.time", return_value=200.0):
            result = await svc.get_status()
        assert result["cpu_percent"] == 33.3
        assert result["memory_total"] == 1000
        assert result["memory_used"] == 500
        assert result["memory_percent"] == 50.0
        assert result["disk_total"] == 2000
        assert result["disk_used"] == 1000
        assert result["disk_percent"] == 50.0
        assert result["device_total"] == 10
        assert result["device_online"] == 2
        assert result["rule_total"] == 7
        assert result["rule_enabled"] == 4
        assert result["alarm_firing"] == 3
        assert result["collect_task_count"] == 5
        assert result["uptime"] == 100
        assert result["version"] == "1.0.0"

    async def test_without_psutil(self, monkeypatch, patch_app_state):
        svc, repos, *_ = _make_service(start_time=50.0)
        monkeypatch.setattr(ss_module, "psutil", None)
        repos["device_repo"].list_all = AsyncMock(return_value=([], 5))
        repos["rule_repo"].list_all = AsyncMock(return_value=([], 2))
        repos["alarm_repo"].list_all = AsyncMock(return_value=([], 0))
        session = MagicMock()
        result_mock = MagicMock()
        result_mock.scalar = MagicMock(return_value=1)
        session.execute = AsyncMock(return_value=result_mock)
        patch_app_state.database.get_session.return_value.__aenter__ = AsyncMock(return_value=session)
        patch_app_state.database.get_session.return_value.__aexit__ = AsyncMock(return_value=False)
        with patch("time.time", return_value=150.0):
            result = await svc.get_status()
        assert result["cpu_percent"] == 0.0
        assert result["memory_total"] == 0
        assert result["disk_total"] == 0
        assert result["rule_enabled"] == 1
        assert result["uptime"] == 100

    async def test_rule_count_exception_returns_zero(self, monkeypatch, patch_app_state):
        svc, repos, *_ = _make_service(start_time=10.0)
        monkeypatch.setattr(ss_module, "psutil", None)
        repos["device_repo"].list_all = AsyncMock(return_value=([], 1))
        repos["rule_repo"].list_all = AsyncMock(return_value=([], 1))
        repos["alarm_repo"].list_all = AsyncMock(return_value=([], 0))
        session = MagicMock()
        session.execute = AsyncMock(side_effect=RuntimeError("db error"))
        patch_app_state.database.get_session.return_value.__aenter__ = AsyncMock(return_value=session)
        patch_app_state.database.get_session.return_value.__aexit__ = AsyncMock(return_value=False)
        with patch("time.time", return_value=110.0):
            result = await svc.get_status()
        assert result["rule_enabled"] == 0
        assert result["uptime"] == 100


class TestCreateBackup:
    async def test_full_success_with_aux_dbs(self, tmp_path, monkeypatch, patch_config):
        svc, repos, database, scheduler = _make_service()
        data_dir = Path(patch_config.database.sqlite_path).parent
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "edgelite_ts.db").write_text("ts")
        (data_dir / "audit.db").write_text("audit")
        database.backup = AsyncMock(return_value=None)
        repos["device_repo"].list_all = AsyncMock(return_value=([{"device_id": "d1"}], 1))
        repos["rule_repo"].list_all = AsyncMock(return_value=([{"rule_id": "r1"}], 1))
        repos["user_repo"].list_all = AsyncMock(
            return_value=(
                [{"user_id": "u1", "username": "admin", "role": "admin", "enabled": True}],
                1,
            )
        )
        fixed_dt = MagicMock()
        fixed_dt.strftime.return_value = "20260101_120000"
        fixed_dt.isoformat.return_value = "2026-01-01T12:00:00+00:00"
        mock_datetime = MagicMock()
        mock_datetime.now.return_value = fixed_dt
        monkeypatch.setattr(ss_module, "datetime", mock_datetime)
        monkeypatch.setattr(ss_module.os, "chmod", lambda *a, **kw: None)
        result = await svc.create_backup()
        assert result["backup_id"] == "20260101_120000"
        assert "backup_20260101_120000.db" in result["db_file"]
        assert "backup_20260101_120000.json" in result["json_file"]
        assert "edgelite_ts.db" in result["aux_dbs"]
        assert "audit.db" in result["aux_dbs"]
        assert result["created_at"] == "2026-01-01T12:00:00+00:00"
        json_file = Path(result["json_file"])
        assert json_file.exists()
        data = json.loads(json_file.read_text(encoding="utf-8"))
        assert data["version"] == "1.0.0"
        assert data["devices"] == [{"device_id": "d1"}]
        assert data["users"] == [
            {"user_id": "u1", "username": "admin", "role": "admin", "enabled": True}
        ]

    async def test_database_backup_failure_raises(self, tmp_path, monkeypatch, patch_config):
        svc, repos, database, scheduler = _make_service()
        database.backup = AsyncMock(side_effect=OSError("disk full"))
        monkeypatch.setattr(ss_module.os, "chmod", lambda *a, **kw: None)
        fixed_dt = MagicMock()
        fixed_dt.strftime.return_value = "20260101_120000"
        fixed_dt.isoformat.return_value = "2026-01-01T12:00:00+00:00"
        mock_datetime = MagicMock()
        mock_datetime.now.return_value = fixed_dt
        monkeypatch.setattr(ss_module, "datetime", mock_datetime)
        with pytest.raises(RuntimeError, match="Database backup failed"):
            await svc.create_backup()

    async def test_aux_db_copy_failure_logged_not_fatal(self, tmp_path, monkeypatch, patch_config):
        svc, repos, database, scheduler = _make_service()
        data_dir = Path(patch_config.database.sqlite_path).parent
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "edge_rules.db").write_text("rules")
        database.backup = AsyncMock(return_value=None)
        repos["device_repo"].list_all = AsyncMock(return_value=([], 0))
        repos["rule_repo"].list_all = AsyncMock(return_value=([], 0))
        repos["user_repo"].list_all = AsyncMock(return_value=([], 0))

        def _fail_copy(*a, **kw):
            raise OSError("copy failed")

        import shutil as _shutil_mod

        monkeypatch.setattr(_shutil_mod, "copy2", _fail_copy)
        monkeypatch.setattr(ss_module.os, "chmod", lambda *a, **kw: None)
        fixed_dt = MagicMock()
        fixed_dt.strftime.return_value = "20260101_120000"
        fixed_dt.isoformat.return_value = "2026-01-01T12:00:00+00:00"
        mock_datetime = MagicMock()
        mock_datetime.now.return_value = fixed_dt
        monkeypatch.setattr(ss_module, "datetime", mock_datetime)
        result = await svc.create_backup()
        assert result["aux_dbs"] == []

    async def test_export_config_failure_raises(self, tmp_path, monkeypatch, patch_config):
        svc, repos, database, scheduler = _make_service()
        database.backup = AsyncMock(return_value=None)
        repos["device_repo"].list_all = AsyncMock(side_effect=RuntimeError("db down"))
        monkeypatch.setattr(ss_module.os, "chmod", lambda *a, **kw: None)
        fixed_dt = MagicMock()
        fixed_dt.strftime.return_value = "20260101_120000"
        fixed_dt.isoformat.return_value = "2026-01-01T12:00:00+00:00"
        mock_datetime = MagicMock()
        mock_datetime.now.return_value = fixed_dt
        monkeypatch.setattr(ss_module, "datetime", mock_datetime)
        with pytest.raises(RuntimeError, match="Config export failed"):
            await svc.create_backup()


class TestListBackups:
    async def test_backup_dir_not_exists(self, patch_config):
        svc, *_ = _make_service()
        result = await svc.list_backups()
        assert result == []

    async def test_empty_dir(self, tmp_path, patch_config):
        svc, *_ = _make_service()
        Path(patch_config.database.backup_dir).mkdir(parents=True, exist_ok=True)
        result = await svc.list_backups()
        assert result == []

    async def test_normal_backups_sorted_desc(self, tmp_path, patch_config):
        svc, *_ = _make_service()
        backup_dir = Path(patch_config.database.backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        for ts in ["20260101_100000", "20260102_100000", "20260103_100000"]:
            (backup_dir / f"backup_{ts}.json").write_text("{}", encoding="utf-8")
        result = await svc.list_backups()
        assert len(result) == 3
        assert result[0]["backup_id"] == "20260103_100000"
        assert result[1]["backup_id"] == "20260102_100000"
        assert result[2]["backup_id"] == "20260101_100000"
        assert all("size" in r and "file" in r for r in result)

    async def test_invalid_backup_id_skipped(self, tmp_path, patch_config):
        svc, *_ = _make_service()
        backup_dir = Path(patch_config.database.backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / "backup_20260101_120000.json").write_text("{}", encoding="utf-8")
        (backup_dir / "backup_invalid.json").write_text("{}", encoding="utf-8")
        (backup_dir / "backup_20260101.json").write_text("{}", encoding="utf-8")
        result = await svc.list_backups()
        assert len(result) == 1
        assert result[0]["backup_id"] == "20260101_120000"

    async def test_stat_failure_skipped(self, tmp_path, patch_config, monkeypatch):
        svc, *_ = _make_service()
        backup_dir = Path(patch_config.database.backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        good = backup_dir / "backup_20260101_120000.json"
        good.write_text("{}", encoding="utf-8")
        bad = backup_dir / "backup_20260102_120000.json"
        bad.write_text("{}", encoding="utf-8")
        original_stat = Path.stat

        def _fake_stat(self, *a, **kw):
            if self.name == bad.name:
                raise OSError("stat failed")
            return original_stat(self, *a, **kw)

        monkeypatch.setattr(Path, "stat", _fake_stat)
        result = await svc.list_backups()
        assert len(result) == 1
        assert result[0]["backup_id"] == "20260101_120000"

    async def test_default_page_size_limit(self, tmp_path, patch_config, monkeypatch):
        from edgelite.constants import _DEFAULT_PAGE_SIZE

        svc, *_ = _make_service()
        backup_dir = Path(patch_config.database.backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        for i in range(_DEFAULT_PAGE_SIZE + 5):
            ts = f"202601{i:02d}_120000"
            (backup_dir / f"backup_{ts}.json").write_text("{}", encoding="utf-8")
        result = await svc.list_backups()
        assert len(result) == _DEFAULT_PAGE_SIZE


class TestRestoreBackup:
    async def test_file_not_exists_returns_false(self, patch_config):
        svc, *_ = _make_service()
        Path(patch_config.database.backup_dir).mkdir(parents=True, exist_ok=True)
        result = await svc.restore_backup("20260101_120000")
        assert result is False

    async def test_json_decode_error_raises(self, tmp_path, patch_config):
        svc, *_ = _make_service()
        backup_dir = Path(patch_config.database.backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / "backup_20260101_120000.json").write_text("{invalid json", encoding="utf-8")
        with pytest.raises(RuntimeError, match="Backup file corrupted"):
            await svc.restore_backup("20260101_120000")

    async def test_format_invalid_not_dict(self, tmp_path, patch_config):
        svc, *_ = _make_service()
        backup_dir = Path(patch_config.database.backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / "backup_20260101_120000.json").write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(RuntimeError, match="expected dict"):
            await svc.restore_backup("20260101_120000")

    async def test_format_invalid_missing_keys(self, tmp_path, patch_config):
        svc, *_ = _make_service()
        backup_dir = Path(patch_config.database.backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / "backup_20260101_120000.json").write_text(
            json.dumps({"version": "1.0.0"}), encoding="utf-8"
        )
        with pytest.raises(RuntimeError, match="missing required keys"):
            await svc.restore_backup("20260101_120000")

    async def test_format_invalid_section_not_list(self, tmp_path, patch_config):
        svc, *_ = _make_service()
        backup_dir = Path(patch_config.database.backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        data = {"devices": "not_a_list", "rules": [], "users": []}
        (backup_dir / "backup_20260101_120000.json").write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(RuntimeError, match="'devices' must be a list"):
            await svc.restore_backup("20260101_120000")

    async def test_restore_create_path_success(self, tmp_path, patch_config, monkeypatch):
        svc, repos, *_ = _make_service()
        backup_dir = Path(patch_config.database.backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "devices": [{"device_id": "d1", "name": "Dev1"}],
            "rules": [{"rule_id": "r1", "name": "Rule1"}],
            "users": [{"user_id": "u1", "username": "admin"}],
        }
        (backup_dir / "backup_20260101_120000.json").write_text(json.dumps(data), encoding="utf-8")
        repos["device_repo"].get_by_id = AsyncMock(return_value=None)
        repos["device_repo"].create = AsyncMock(return_value="d1")
        repos["rule_repo"].get_by_id = AsyncMock(return_value=None)
        repos["rule_repo"].create = AsyncMock(return_value="r1")
        repos["user_repo"].get_by_id = AsyncMock(return_value=None)
        repos["user_repo"].create = AsyncMock(return_value="u1")
        app_state = MagicMock()
        app_state.evaluator = None
        monkeypatch.setattr("edgelite.app._app_state", app_state)
        result = await svc.restore_backup("20260101_120000")
        assert result is True
        repos["device_repo"].create.assert_awaited_once()
        repos["rule_repo"].create.assert_awaited_once()
        repos["user_repo"].create.assert_awaited_once()

    async def test_restore_update_path_success(self, tmp_path, patch_config, monkeypatch):
        svc, repos, *_ = _make_service()
        backup_dir = Path(patch_config.database.backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "devices": [{"device_id": "d1", "name": "Dev1"}],
            "rules": [{"rule_id": "r1", "name": "Rule1"}],
            "users": [{"user_id": "u1", "username": "admin"}],
        }
        (backup_dir / "backup_20260101_120000.json").write_text(json.dumps(data), encoding="utf-8")
        repos["device_repo"].get_by_id = AsyncMock(return_value={"device_id": "d1"})
        repos["device_repo"].update = AsyncMock(return_value=None)
        repos["rule_repo"].get_by_id = AsyncMock(return_value={"rule_id": "r1"})
        repos["rule_repo"].update = AsyncMock(return_value=None)
        repos["user_repo"].get_by_id = AsyncMock(return_value={"user_id": "u1"})
        repos["user_repo"].update = AsyncMock(return_value=None)
        app_state = MagicMock()
        app_state.evaluator = None
        monkeypatch.setattr("edgelite.app._app_state", app_state)
        result = await svc.restore_backup("20260101_120000")
        assert result is True
        repos["device_repo"].update.assert_awaited_once()
        repos["rule_repo"].update.assert_awaited_once()
        repos["user_repo"].update.assert_awaited_once()

    async def test_restore_skips_none_ids(self, tmp_path, patch_config, monkeypatch):
        svc, repos, *_ = _make_service()
        backup_dir = Path(patch_config.database.backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "devices": [{"device_id": None, "name": "Dev1"}, {"device_id": "d1"}],
            "rules": [{"rule_id": None}, {"rule_id": "r1"}],
            "users": [{"user_id": None}, {"user_id": "u1"}],
        }
        (backup_dir / "backup_20260101_120000.json").write_text(json.dumps(data), encoding="utf-8")
        repos["device_repo"].get_by_id = AsyncMock(return_value=None)
        repos["device_repo"].create = AsyncMock(return_value="d1")
        repos["rule_repo"].get_by_id = AsyncMock(return_value=None)
        repos["rule_repo"].create = AsyncMock(return_value="r1")
        repos["user_repo"].get_by_id = AsyncMock(return_value=None)
        repos["user_repo"].create = AsyncMock(return_value="u1")
        app_state = MagicMock()
        app_state.evaluator = None
        monkeypatch.setattr("edgelite.app._app_state", app_state)
        result = await svc.restore_backup("20260101_120000")
        assert result is True
        repos["device_repo"].create.assert_awaited_once()
        repos["rule_repo"].create.assert_awaited_once()
        repos["user_repo"].create.assert_awaited_once()

    async def test_restore_failure_rolls_back_created_only(self, tmp_path, patch_config, monkeypatch):
        svc, repos, *_ = _make_service()
        backup_dir = Path(patch_config.database.backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "devices": [
                {"device_id": "d_new"},
                {"device_id": "d_fail"},
            ],
            "rules": [],
            "users": [],
        }
        (backup_dir / "backup_20260101_120000.json").write_text(json.dumps(data), encoding="utf-8")

        # Both devices go to create branch; first succeeds (appended to rollback
        # list), second fails (triggers rollback which deletes only created ones)
        async def _device_get_by_id(dev_id):
            return None

        create_calls = {"n": 0}

        async def _device_create(dev):
            create_calls["n"] += 1
            if create_calls["n"] == 2:
                raise RuntimeError("create failed")
            return dev["device_id"]

        repos["device_repo"].get_by_id = _device_get_by_id
        repos["device_repo"].create = _device_create
        repos["device_repo"].delete = AsyncMock(return_value=None)
        app_state = MagicMock()
        app_state.evaluator = None
        monkeypatch.setattr("edgelite.app._app_state", app_state)
        with pytest.raises(RuntimeError, match="Restore failed and rolled back"):
            await svc.restore_backup("20260101_120000")
        repos["device_repo"].delete.assert_awaited_once_with("d_new")

    async def test_restore_clears_evaluator_cache(self, tmp_path, patch_config, monkeypatch):
        svc, repos, *_ = _make_service()
        backup_dir = Path(patch_config.database.backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        data = {"devices": [], "rules": [], "users": []}
        (backup_dir / "backup_20260101_120000.json").write_text(json.dumps(data), encoding="utf-8")
        evaluator = MagicMock()
        evaluator._rule_cache = MagicMock()
        evaluator._rule_cache.clear = MagicMock()
        evaluator._duration_tracker = MagicMock()
        evaluator._duration_tracker.clear = MagicMock()
        evaluator._cache_time = 1.5
        app_state = MagicMock()
        app_state.evaluator = evaluator
        monkeypatch.setattr("edgelite.app._app_state", app_state)
        result = await svc.restore_backup("20260101_120000")
        assert result is True
        evaluator._rule_cache.clear.assert_called_once()
        evaluator._duration_tracker.clear.assert_called_once()
        assert evaluator._cache_time == 0.0

    async def test_restore_evaluator_clear_failure_logged(self, tmp_path, patch_config, monkeypatch):
        svc, repos, *_ = _make_service()
        backup_dir = Path(patch_config.database.backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        data = {"devices": [], "rules": [], "users": []}
        (backup_dir / "backup_20260101_120000.json").write_text(json.dumps(data), encoding="utf-8")
        evaluator = MagicMock()
        evaluator._rule_cache = MagicMock()
        evaluator._rule_cache.clear = MagicMock(side_effect=RuntimeError("clear failed"))
        app_state = MagicMock()
        app_state.evaluator = evaluator
        monkeypatch.setattr("edgelite.app._app_state", app_state)
        result = await svc.restore_backup("20260101_120000")
        assert result is True

    async def test_restore_rollback_swallows_delete_exception(self, tmp_path, patch_config, monkeypatch):
        svc, repos, *_ = _make_service()
        backup_dir = Path(patch_config.database.backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "devices": [{"device_id": "d_new"}],
            "rules": [],
            "users": [],
        }
        (backup_dir / "backup_20260101_120000.json").write_text(json.dumps(data), encoding="utf-8")
        repos["device_repo"].get_by_id = AsyncMock(return_value=None)
        repos["device_repo"].create = AsyncMock(side_effect=RuntimeError("create failed"))
        repos["device_repo"].delete = AsyncMock(side_effect=OSError("delete failed"))
        app_state = MagicMock()
        app_state.evaluator = None
        monkeypatch.setattr("edgelite.app._app_state", app_state)
        with pytest.raises(RuntimeError, match="Restore failed and rolled back"):
            await svc.restore_backup("20260101_120000")


class TestExportAllConfig:
    async def test_single_page_export(self):
        svc, repos, *_ = _make_service()
        repos["device_repo"].list_all = AsyncMock(return_value=([{"device_id": "d1"}], 1))
        repos["rule_repo"].list_all = AsyncMock(return_value=([{"rule_id": "r1"}], 1))
        repos["user_repo"].list_all = AsyncMock(
            return_value=(
                [{"user_id": "u1", "username": "admin", "role": "admin", "enabled": True}],
                1,
            )
        )
        result = await svc._export_all_config()
        assert result["version"] == "1.0.0"
        assert "exported_at" in result
        assert result["devices"] == [{"device_id": "d1"}]
        assert result["rules"] == [{"rule_id": "r1"}]
        assert result["users"] == [
            {"user_id": "u1", "username": "admin", "role": "admin", "enabled": True}
        ]

    async def test_multi_page_export(self):
        svc, repos, *_ = _make_service()
        device_page1 = [{"device_id": f"d{i}"} for i in range(10000)]
        device_page2 = [{"device_id": f"d{i}"} for i in range(10000, 15000)]
        call_count = {"device": 0}

        async def _device_list_all(page, size):
            call_count["device"] += 1
            if page == 1:
                return (device_page1, 15000)
            return (device_page2, 15000)

        repos["device_repo"].list_all = _device_list_all
        repos["rule_repo"].list_all = AsyncMock(return_value=([], 0))
        repos["user_repo"].list_all = AsyncMock(return_value=([], 0))
        result = await svc._export_all_config()
        assert len(result["devices"]) == 15000
        assert call_count["device"] == 2

    async def test_empty_batch_breaks_loop(self):
        svc, repos, *_ = _make_service()
        repos["device_repo"].list_all = AsyncMock(return_value=([], 0))
        repos["rule_repo"].list_all = AsyncMock(return_value=([], 0))
        repos["user_repo"].list_all = AsyncMock(return_value=([], 0))
        result = await svc._export_all_config()
        assert result["devices"] == []
        assert result["rules"] == []
        assert result["users"] == []

    async def test_sensitive_fields_masked(self):
        svc, repos, *_ = _make_service()
        devices = [
            {
                "device_id": "d1",
                "config": {
                    "ip": "1.2.3.4",
                    "password": "secret123",
                    "api_key": "key456",
                    "cip_password": "cip789",
                    "secretKey": "sk",
                    "normal_field": "ok",
                },
            }
        ]
        repos["device_repo"].list_all = AsyncMock(return_value=(devices, 1))
        repos["rule_repo"].list_all = AsyncMock(return_value=([], 0))
        repos["user_repo"].list_all = AsyncMock(return_value=([], 0))
        result = await svc._export_all_config()
        config = result["devices"][0]["config"]
        assert config["ip"] == "1.2.3.4"
        assert config["normal_field"] == "ok"
        assert config["password"] == "********"
        assert config["api_key"] == "********"
        assert config["cip_password"] == "********"
        assert config["secretKey"] == "********"

    async def test_sensitive_field_partial_match(self):
        svc, repos, *_ = _make_service()
        devices = [
            {
                "device_id": "d1",
                "config": {
                    "my_password_field": "secret",
                    "user_token_value": "tok",
                },
            }
        ]
        repos["device_repo"].list_all = AsyncMock(return_value=(devices, 1))
        repos["rule_repo"].list_all = AsyncMock(return_value=([], 0))
        repos["user_repo"].list_all = AsyncMock(return_value=([], 0))
        result = await svc._export_all_config()
        config = result["devices"][0]["config"]
        assert config["my_password_field"] == "********"
        assert config["user_token_value"] == "********"

    async def test_config_not_dict_skipped(self):
        svc, repos, *_ = _make_service()
        devices = [
            {"device_id": "d1", "config": "not_a_dict"},
            {"device_id": "d2"},
        ]
        repos["device_repo"].list_all = AsyncMock(return_value=(devices, 2))
        repos["rule_repo"].list_all = AsyncMock(return_value=([], 0))
        repos["user_repo"].list_all = AsyncMock(return_value=([], 0))
        result = await svc._export_all_config()
        assert result["devices"][0]["config"] == "not_a_dict"
        assert "config" not in result["devices"][1]
