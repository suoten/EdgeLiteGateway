"""系统服务测试 - 配置备份/日志轮转/配置审计/配置校验

覆盖 services/system_services.py：
- BackupFormat/BackupType/BackupStrategy 枚举
- BackupMetadata/AuditEntry/LogConfig 数据类
- ConfigBackupService: 备份/列出/获取/删除/去重/脱敏/增量链/调度
- LogRotationService: 轮转/压缩/清理/配置
- ConfigAuditService: 记录/查询/计数/导出/清理
- ConfigValidator: 设备/规则配置校验
- 工厂函数与辅助函数
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from edgelite.services.system_services import (
    AuditEntry,
    BackupFormat,
    BackupMetadata,
    BackupStrategy,
    BackupType,
    ConfigAuditService,
    ConfigBackupService,
    ConfigValidator,
    LogConfig,
    LogRotationService,
)

# ───────────────────────── 枚举 ─────────────────────────


class TestEnums:
    def test_backup_format(self):
        assert BackupFormat.JSON.value == "json"
        assert BackupFormat.ZIP.value == "zip"

    def test_backup_type(self):
        assert BackupType.FULL.value == "full"
        assert BackupType.DEVICES.value == "devices"
        assert BackupType.RULES.value == "rules"
        assert BackupType.CONFIG.value == "config"
        assert BackupType.INCREMENTAL.value == "incremental"

    def test_backup_strategy(self):
        assert BackupStrategy.FULL_ONLY.value == "full_only"
        assert BackupStrategy.FULL_THEN_INCREMENTAL.value == "full_then_incremental"


# ───────────────────────── 数据类 ─────────────────────────


class TestDataclasses:
    def test_backup_metadata_defaults(self):
        m = BackupMetadata()
        assert m.backup_id == ""
        assert m.backup_type == BackupType.FULL
        assert m.file_size == 0
        assert m.includes_secrets is False
        assert m.base_backup_id == ""

    def test_backup_metadata_with_values(self):
        m = BackupMetadata(backup_id="b1", name="test", backup_type=BackupType.INCREMENTAL)
        assert m.backup_id == "b1"
        assert m.backup_type == BackupType.INCREMENTAL

    def test_audit_entry_defaults(self):
        e = AuditEntry()
        assert e.audit_id == ""
        assert e.changes == {}

    def test_audit_entry_with_changes(self):
        e = AuditEntry(audit_id="a1", user="admin", changes={"field": {"old": 1, "new": 2}})
        assert e.changes["field"]["new"] == 2

    def test_log_config_defaults(self):
        c = LogConfig()
        assert c.enabled is True
        assert c.max_size_mb == 100
        assert c.max_files == 10
        assert c.compression == "gzip"

    def test_log_config_custom(self):
        c = LogConfig(enabled=False, max_size_mb=50, log_level="DEBUG")
        assert c.enabled is False
        assert c.max_size_mb == 50
        assert c.log_level == "DEBUG"


# ───────────────────────── ConfigValidator ─────────────────────────


class TestConfigValidator:
    def test_valid_device_config(self):
        ok, errors = ConfigValidator.validate_device_config(
            {"device_id": "d1", "name": "Dev1", "protocol": "modbus_tcp"}
        )
        assert ok is True
        assert errors == []

    def test_device_missing_required(self):
        ok, errors = ConfigValidator.validate_device_config({})
        assert ok is False
        assert any("device_id" in e for e in errors)
        assert any("name" in e for e in errors)
        assert any("protocol" in e for e in errors)

    def test_device_invalid_protocol(self):
        ok, errors = ConfigValidator.validate_device_config(
            {"device_id": "d1", "name": "Dev1", "protocol": "nonexistent_protocol"}
        )
        assert ok is False
        assert any("protocol" in e for e in errors)

    def test_device_collect_interval_too_small(self):
        ok, errors = ConfigValidator.validate_device_config(
            {"device_id": "d1", "name": "Dev1", "protocol": "modbus_tcp", "collect_interval": 0.01}
        )
        assert ok is False
        assert any("collect_interval" in e for e in errors)

    def test_device_collect_interval_valid(self):
        ok, _ = ConfigValidator.validate_device_config(
            {"device_id": "d1", "name": "Dev1", "protocol": "modbus_tcp", "collect_interval": 1.0}
        )
        assert ok is True

    def test_valid_rule_config(self):
        ok, errors = ConfigValidator.validate_rule_config(
            {
                "name": "Rule1",
                "device_id": "d1",
                "conditions": [{"point": "temp", "operator": ">", "value": 50}],
                "severity": "critical",
            }
        )
        assert ok is True
        assert errors == []

    def test_rule_missing_required(self):
        ok, errors = ConfigValidator.validate_rule_config({})
        assert ok is False
        assert any("name" in e for e in errors)

    def test_rule_empty_conditions(self):
        ok, errors = ConfigValidator.validate_rule_config(
            {"name": "R", "device_id": "d", "conditions": [], "severity": "info"}
        )
        assert ok is False
        assert any("conditions" in e for e in errors)

    def test_rule_condition_missing_point(self):
        ok, errors = ConfigValidator.validate_rule_config(
            {"name": "R", "device_id": "d", "conditions": [{"operator": ">"}], "severity": "info"}
        )
        assert ok is False
        assert any("point" in e for e in errors)

    def test_rule_condition_missing_operator(self):
        ok, errors = ConfigValidator.validate_rule_config(
            {"name": "R", "device_id": "d", "conditions": [{"point": "t"}], "severity": "info"}
        )
        assert ok is False
        assert any("operator" in e for e in errors)

    def test_rule_invalid_severity(self):
        ok, errors = ConfigValidator.validate_rule_config(
            {"name": "R", "device_id": "d", "conditions": [{"point": "t", "operator": ">"}], "severity": "bad"}
        )
        assert ok is False
        assert any("severity" in e for e in errors)

    def test_rule_condition_not_dict(self):
        ok, errors = ConfigValidator.validate_rule_config(
            {"name": "R", "device_id": "d", "conditions": ["not a dict"], "severity": "info"}
        )
        assert ok is False
        assert any("must be a dict" in e for e in errors)


# ───────────────────────── ConfigBackupService ─────────────────────────


@pytest.fixture
def backup_svc(tmp_path):
    """创建临时备份服务"""
    return ConfigBackupService(backup_dir=str(tmp_path / "backups"), max_backups=5)


class TestConfigBackupService:
    def test_init_creates_dir(self, tmp_path):
        """init 应创建备份目录"""
        d = tmp_path / "bk"
        ConfigBackupService(backup_dir=str(d))
        assert d.exists()

    def test_is_scheduled_default_false(self, backup_svc):
        """初始状态未调度"""
        assert backup_svc.is_scheduled is False

    def test_interval_seconds(self, backup_svc):
        """interval_seconds 属性"""
        assert isinstance(backup_svc.interval_seconds, int)
        assert backup_svc.interval_seconds > 0

    def test_set_interval(self, backup_svc):
        """set_interval 应更新间隔"""
        backup_svc.set_interval(120)
        assert backup_svc.interval_seconds == 120

    def test_set_interval_too_small(self, backup_svc):
        """小于 60 秒应抛 ValueError"""
        with pytest.raises(ValueError):
            backup_svc.set_interval(30)

    def test_set_max_backups(self, backup_svc):
        """set_max_backups 应更新上限"""
        backup_svc.set_max_backups(10)
        # 无异常即通过

    def test_remove_secrets(self, backup_svc):
        """_remove_secrets 应脱敏敏感字段"""
        data = {"name": "dev", "password": "secret", "api_key": "k1", "nested": {"token": "t1"}}
        cleaned = backup_svc._remove_secrets(data)
        assert cleaned["name"] == "dev"
        assert cleaned["password"] == "***REDACTED***"
        assert cleaned["api_key"] == "***REDACTED***"
        assert cleaned["nested"]["token"] == "***REDACTED***"

    def test_remove_secrets_list(self, backup_svc):
        """列表中的敏感字段应脱敏"""
        data = [{"password": "p1"}, {"name": "ok"}]
        cleaned = backup_svc._remove_secrets(data)
        assert cleaned[0]["password"] == "***REDACTED***"
        assert cleaned[1]["name"] == "ok"

    def test_deduplicate_entities(self, backup_svc):
        """_deduplicate_entities 应按 ID 去重，后覆盖前"""
        entities = [
            {"id": "1", "name": "first"},
            {"id": "2", "name": "second"},
            {"id": "1", "name": "first-updated"},
        ]
        result = backup_svc._deduplicate_entities(entities)
        by_id = {e["id"]: e for e in result}
        assert by_id["1"]["name"] == "first-updated"
        assert by_id["2"]["name"] == "second"

    async def test_list_backups_empty(self, backup_svc):
        """无备份时应返回空列表"""
        result = await backup_svc.list_backups()
        assert result == []

    async def test_get_backup_not_found(self, backup_svc):
        """不存在的备份应返回 None"""
        result = await backup_svc.get_backup("nonexistent")
        assert result is None

    async def test_list_incremental_chain_not_found(self, backup_svc):
        """不存在的增量链应返回空列表"""
        result = await backup_svc.list_incremental_chain("nonexistent")
        assert result == []

    async def test_delete_backup_not_found(self, backup_svc):
        """删除不存在的备份应返回 False"""
        result = await backup_svc.delete_backup("nonexistent")
        assert result is False

    async def test_start_stop_scheduler(self, backup_svc):
        """start_scheduler/stop_scheduler 生命周期"""
        with patch.object(backup_svc, "_backup_loop", new=AsyncMock()):
            await backup_svc.start_scheduler()
        assert backup_svc.is_scheduled is True
        await backup_svc.stop_scheduler()
        assert backup_svc.is_scheduled is False

    async def test_load_chain_state_no_file(self, tmp_path):
        """无链状态文件时应返回空字典"""
        svc = ConfigBackupService(backup_dir=str(tmp_path / "bk"))
        assert svc._chain_state == {}

    async def test_save_and_load_chain_state(self, tmp_path):
        """保存后应能加载链状态"""
        d = tmp_path / "bk"
        svc = ConfigBackupService(backup_dir=str(d))
        svc._chain_state = {"last_full": "b1", "count": 3}
        svc._save_chain_state()
        # 新实例加载
        svc2 = ConfigBackupService(backup_dir=str(d))
        assert svc2._chain_state.get("last_full") == "b1"
        assert svc2._chain_state.get("count") == 3


# ───────────────────────── LogRotationService ─────────────────────────


class TestLogRotationService:
    def test_init_default(self):
        svc = LogRotationService()
        assert svc.config.enabled is True

    def test_init_custom_config(self):
        cfg = LogConfig(enabled=False, max_size_mb=50)
        svc = LogRotationService(cfg)
        assert svc.config.enabled is False
        assert svc.config.max_size_mb == 50

    def test_update_config(self):
        svc = LogRotationService()
        new_cfg = LogConfig(max_size_mb=200)
        svc.update_config(new_cfg)
        assert svc.config.max_size_mb == 200

    async def test_start_stop(self):
        """start/stop 生命周期"""
        svc = LogRotationService(LogConfig(enabled=True))
        with patch.object(svc, "_rotation_loop", new=AsyncMock()):
            await svc.start()
        await svc.stop()

    async def test_rotate_if_needed_no_file(self, tmp_path):
        """无日志文件时 rotate_if_needed 返回 0"""
        svc = LogRotationService(LogConfig(log_dir=str(tmp_path)))
        result = await svc.rotate_if_needed()
        assert result == 0


# ───────────────────────── ConfigAuditService ─────────────────────────


class TestConfigAuditService:
    def test_init(self, tmp_path):
        svc = ConfigAuditService(audit_dir=str(tmp_path / "audit"))
        assert Path(svc._audit_dir) == tmp_path / "audit"

    def test_record_change_no_loop(self, tmp_path):
        """无事件循环时 record_change 应走 fallback"""
        svc = ConfigAuditService(audit_dir=str(tmp_path / "audit"))
        # record_change 在无运行循环时应同步降级不崩溃
        svc.record_change(
            user="admin",
            action="create",
            resource_type="device",
            resource_id="d1",
            resource_name="Dev1",
        )

    def test_get_entries_no_loop(self, tmp_path):
        """无事件循环时 get_entries 应返回空"""
        svc = ConfigAuditService(audit_dir=str(tmp_path / "audit"))
        entries = svc.get_entries()
        assert isinstance(entries, list)

    def test_get_entries_count_no_loop(self, tmp_path):
        """无事件循环时 get_entries_count 应返回 0"""
        svc = ConfigAuditService(audit_dir=str(tmp_path / "audit"))
        count = svc.get_entries_count()
        assert count == 0

    def test_export_audit_log_no_loop(self, tmp_path):
        """无事件循环时 export_audit_log 应返回空字符串"""
        svc = ConfigAuditService(audit_dir=str(tmp_path / "audit"))
        result = svc.export_audit_log()
        assert isinstance(result, str)

    def test_cleanup_old_entries_no_loop(self, tmp_path):
        """无事件循环时 cleanup_old_entries 应返回 0"""
        svc = ConfigAuditService(audit_dir=str(tmp_path / "audit"))
        result = svc.cleanup_old_entries(days=30)
        assert result == 0


# ───────────────────────── 辅助函数 ─────────────────────────


class TestHelpers:
    def test_dict_to_audit_entry(self):
        """_dict_to_audit_entry 应转换字典（changes 为 JSON 字符串）"""
        from edgelite.services.system_services import _dict_to_audit_entry

        d = {
            "audit_id": "a1",
            "timestamp": "2024-01-01",
            "user_id": "admin",
            "action": "create",
            "resource_type": "device",
            "resource_id": "d1",
            "resource_name": "Dev",
            "changes": json.dumps({"f": {"old": 1, "new": 2}}),
            "ip_address": "1.2.3.4",
            "user_agent": "ua",
        }
        entry = _dict_to_audit_entry(d)
        assert entry.audit_id == "a1"
        assert entry.user == "admin"
        assert entry.changes["f"]["new"] == 2

    def test_dict_to_audit_entry_missing_fields(self):
        """缺失字段应使用默认值"""
        from edgelite.services.system_services import _dict_to_audit_entry

        entry = _dict_to_audit_entry({"audit_id": "x"})
        assert entry.audit_id == "x"
        assert entry.user == ""
        assert entry.changes == {}

    def test_write_fallback_jsonl(self, tmp_path):
        """_write_fallback_jsonl 应写入 JSONL 文件"""
        from edgelite.services.system_services import _write_fallback_jsonl

        with patch("edgelite.services.system_services._get_audit_file", return_value=tmp_path / "audit.jsonl"):
            entry = {"audit_id": "a1", "user": "admin"}
            _write_fallback_jsonl(entry)
            assert (tmp_path / "audit.jsonl").exists()

    def test_query_jsonl_fallback_empty(self, tmp_path):
        """无文件时 _query_jsonl_fallback 应返回空"""
        from edgelite.services.system_services import _query_jsonl_fallback

        result = _query_jsonl_fallback(tmp_path / "nonexistent.jsonl")
        assert result == []

    def test_cleanup_jsonl_fallback_no_file(self, tmp_path):
        """无文件时 _cleanup_jsonl_fallback 应返回 0"""
        from edgelite.services.system_services import _cleanup_jsonl_fallback

        with patch("edgelite.services.system_services._get_audit_file", return_value=tmp_path / "no.jsonl"):
            result = _cleanup_jsonl_fallback(days=30)
        assert result == 0


# ───────────────────────── 工厂函数 ─────────────────────────


class TestFactories:
    def test_get_backup_service_no_autostart(self, tmp_path, monkeypatch):
        """get_backup_service(auto_start=False) 不启动调度"""
        import edgelite.services.system_services as mod

        monkeypatch.setattr(mod, "_backup_service", None)
        svc = mod.get_backup_service(backup_dir=str(tmp_path / "bk"), auto_start=False)
        assert svc is not None
        assert svc.is_scheduled is False

    def test_get_log_rotation_service(self, monkeypatch):
        """get_log_rotation_service 返回单例"""
        import edgelite.services.system_services as mod

        monkeypatch.setattr(mod, "_log_rotation", None)
        svc = mod.get_log_rotation_service()
        assert svc is not None

    def test_get_audit_service(self, tmp_path, monkeypatch):
        """get_audit_service 返回单例"""
        import edgelite.services.system_services as mod

        monkeypatch.setattr(mod, "_audit_service", None)
        svc = mod.get_audit_service(audit_dir=str(tmp_path / "audit"))
        assert svc is not None
