"""数据导入导出服务测试 - 导出/导入/模板管理/冲突处理

覆盖 services/data_import_export.py:
- _sanitize_csv_cell: CSV 注入防护
- ExportFormat / ImportMode 枚举
- ImportResult / DeviceTemplate / RuleTemplate / DeviceGroup 数据类
- DataExportService: 分页导出设备/规则/全部 (JSON/CSV)
- DataImportService: 导入设备/规则/全部 (JSON/CSV, 多冲突模式, 原子事务+回退)
- TemplateService: 设备/规则模板与设备组 CRUD (DB 持久化+内存回退)
- 工厂函数 get_export_service / get_import_service / get_template_service
"""

from __future__ import annotations

import sys

sys.path.insert(0, "src")

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from edgelite.services.data_import_export import (
    DataExportService,
    DataImportService,
    DeviceGroup,
    DeviceTemplate,
    ExportFormat,
    ImportMode,
    ImportResult,
    RuleTemplate,
    TemplateService,
    _sanitize_csv_cell,
    get_export_service,
    get_import_service,
    get_template_service,
)


def _make_session_ctx(session=None):
    """session ctx"""
    session = session or AsyncMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx


def _make_exec_result(rows=None, scalar_one=None, scalars=None, scalar=None, rowcount=0):
    """exec result"""
    result = MagicMock()
    rows = rows if rows is not None else []
    result.__iter__ = lambda self: iter(rows)
    result.__aiter__ = lambda self: iter([])
    result.scalar_one_or_none.return_value = scalar_one
    result.scalar.return_value = scalar
    result.rowcount = rowcount
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = scalars or []
    result.scalars.return_value = scalars_mock
    return result


def _make_rule_template_orm(
    tid="rt1",
    name="RT",
    description="d",
    rule_type="threshold",
    conditions=None,
    severity="warning",
    duration=0,
    channels=None,
):
    """rule orm"""
    return SimpleNamespace(
        template_id=tid,
        name=name,
        description=description,
        rule_type=rule_type,
        default_conditions=json.dumps(conditions or [{"point": "t"}]),
        default_severity=severity,
        default_duration=duration,
        notify_channels=json.dumps(channels or ["email"]),
        created_at="2024-01-01T00:00:00",
        updated_at="2024-01-01T00:00:00",
    )


def _make_group_orm(gid="g1", name="G", description="d", parent_id=None, device_ids=None, tags=None):
    """group orm"""
    return SimpleNamespace(
        group_id=gid,
        name=name,
        description=description,
        parent_id=parent_id,
        device_ids=json.dumps(device_ids or ["d1"]),
        tags=json.dumps(tags or {"k": "v"}),
        created_at="2024-01-01T00:00:00",
        updated_at="2024-01-01T00:00:00",
    )


@pytest.fixture(autouse=True)
def _reset_global_services():
    """重置单例"""
    from edgelite.services import data_import_export as mod

    orig = (mod._export_service, mod._import_service, mod._template_service)
    mod._export_service = None
    mod._import_service = None
    mod._template_service = None
    yield
    mod._export_service, mod._import_service, mod._template_service = orig


def _make_repo(**overrides):
    """repo mock"""
    repo = MagicMock()
    repo._database = None
    repo.list_all = AsyncMock(return_value=([], 0))
    repo.list_devices_by_ids = AsyncMock(return_value=[])
    repo.list_rules_by_ids = AsyncMock(return_value=[])
    repo.upsert_bulk = AsyncMock(return_value=(0, 0, []))
    repo.get = AsyncMock(return_value=None)
    repo.create = AsyncMock(return_value={})
    repo.update = AsyncMock(return_value={})
    repo.delete = AsyncMock(return_value=True)
    for k, v in overrides.items():
        setattr(repo, k, v)
    return repo


class TestSanitizeCsvCell:
    def test_non_string_unchanged(self):
        """非字符串不变"""
        assert _sanitize_csv_cell(42) == 42
        assert _sanitize_csv_cell(None) is None
        assert _sanitize_csv_cell(3.14) == 3.14

    def test_safe_string_unchanged(self):
        """安全字符串不变"""
        assert _sanitize_csv_cell("hello") == "hello"
        assert _sanitize_csv_cell("normal text") == "normal text"

    def test_empty_string_unchanged(self):
        """空字符串不变"""
        assert _sanitize_csv_cell("") == ""

    def test_dangerous_prefix_quoted(self):
        """危险前缀加引号"""
        assert _sanitize_csv_cell("=cmd") == "'=cmd"
        assert _sanitize_csv_cell("+1+1") == "'+1+1"
        assert _sanitize_csv_cell("-evil") == "'-evil"
        assert _sanitize_csv_cell("@ref") == "'@ref"
        assert _sanitize_csv_cell("\ttab") == "'\ttab"
        assert _sanitize_csv_cell("\rcr") == "'\rcr"


class TestEnums:
    def test_export_format_values(self):
        """导出格式"""
        assert ExportFormat.JSON.value == "json"
        assert ExportFormat.CSV.value == "csv"

    def test_import_mode_values(self):
        """冲突模式"""
        assert ImportMode.SKIP.value == "skip"
        assert ImportMode.OVERWRITE.value == "overwrite"
        assert ImportMode.RENAME.value == "rename"
        assert ImportMode.ERROR.value == "error"


class TestDataclasses:
    def test_import_result_defaults(self):
        """默认值"""
        r = ImportResult()
        assert r.success is True
        assert r.total_count == 0
        assert r.imported_count == 0
        assert r.skipped_count == 0
        assert r.error_count == 0
        assert r.errors == []
        assert r.warnings == []
        assert r.imported_items == []
        assert r.skipped_items == []

    def test_device_template_defaults(self):
        """默认值"""
        t = DeviceTemplate()
        assert t.template_id == ""
        assert t.name == ""
        assert t.protocol == ""
        assert t.default_config == {}
        assert t.default_points == []
        assert t.collect_interval == 5
        assert t.tags == {}

    def test_rule_template_defaults(self):
        """默认值"""
        t = RuleTemplate()
        assert t.rule_type == "threshold"
        assert t.default_severity == "warning"
        assert t.default_duration == 0
        assert t.default_conditions == []
        assert t.notify_channels == []

    def test_device_group_defaults(self):
        """默认值"""
        g = DeviceGroup()
        assert g.group_id == ""
        assert g.device_ids == []
        assert g.tags == {}
        assert g.parent_id == ""

    def test_dataclass_with_values(self):
        """自定义值"""
        r = ImportResult(success=False, total_count=5, errors=["err"])
        assert r.success is False
        assert r.total_count == 5
        assert r.errors == ["err"]


class TestDataExportService:
    @pytest.fixture
    def export_svc(self):
        return DataExportService(_make_repo(), _make_repo(), _make_repo())

    async def test_list_all_paginated_single_page(self):
        """单页即止"""
        repo = _make_repo()
        items = [{"device_id": "d1"}]
        repo.list_all = AsyncMock(return_value=(items, 1))
        result = await DataExportService._list_all_paginated(repo, page_size=100)
        assert result == items
        assert repo.list_all.call_count == 1

    async def test_list_all_paginated_multi_page(self):
        """多页循环"""
        repo = _make_repo()
        page1 = [{"id": i} for i in range(100)]
        page2 = [{"id": i} for i in range(100, 150)]
        repo.list_all = AsyncMock(side_effect=[(page1, 150), (page2, 150)])
        result = await DataExportService._list_all_paginated(repo, page_size=100)
        assert len(result) == 150
        assert repo.list_all.call_count == 2

    async def test_list_all_paginated_empty(self):
        """空结果停止"""
        repo = _make_repo()
        repo.list_all = AsyncMock(return_value=([], 0))
        result = await DataExportService._list_all_paginated(repo)
        assert result == []

    async def test_list_all_paginated_reaches_total(self):
        """达到total停止"""
        repo = _make_repo()
        page = [{"id": i} for i in range(100)]
        repo.list_all = AsyncMock(return_value=(page, 100))
        result = await DataExportService._list_all_paginated(repo, page_size=100)
        assert len(result) == 100
        assert repo.list_all.call_count == 1

    async def test_export_devices_json_with_ids(self, export_svc):
        """按ID导出设备JSON"""
        devices = [{"device_id": "d1", "name": "Dev1", "protocol": "modbus_tcp"}]
        export_svc._device_repo.list_devices_by_ids = AsyncMock(return_value=devices)
        result = await export_svc.export_devices(device_ids=["d1"], format=ExportFormat.JSON)
        parsed = json.loads(result)
        assert parsed["type"] == "devices"
        assert parsed["count"] == 1
        assert parsed["devices"] == devices
        export_svc._device_repo.list_devices_by_ids.assert_called_once_with(["d1"])

    async def test_export_devices_json_all(self, export_svc):
        """导出全部设备JSON"""
        devices = [{"device_id": "d1"}, {"device_id": "d2"}]
        export_svc._device_repo.list_all = AsyncMock(return_value=(devices, 2))
        result = await export_svc.export_devices(format=ExportFormat.JSON)
        parsed = json.loads(result)
        assert parsed["count"] == 2

    async def test_export_devices_csv(self, export_svc):
        """导出设备CSV"""
        devices = [
            {"device_id": "d1", "name": "Dev1", "protocol": "modbus_tcp", "status": "online", "collect_interval": 5}
        ]
        export_svc._device_repo.list_all = AsyncMock(return_value=(devices, 1))
        result = await export_svc.export_devices(format=ExportFormat.CSV)
        assert "device_id" in result
        assert "d1" in result
        assert "Dev1" in result

    async def test_export_devices_csv_empty(self, export_svc):
        """空列表CSV"""
        export_svc._device_repo.list_all = AsyncMock(return_value=([], 0))
        result = await export_svc.export_devices(format=ExportFormat.CSV)
        assert result == ""

    async def test_export_devices_csv_injection_sanitized(self, export_svc):
        """CSV净化"""
        devices = [{"device_id": "=evil", "name": "x", "protocol": "p"}]
        export_svc._device_repo.list_all = AsyncMock(return_value=(devices, 1))
        result = await export_svc.export_devices(format=ExportFormat.CSV)
        assert "'=evil" in result

    async def test_export_rules_json_with_ids(self, export_svc):
        """按ID导出规则JSON"""
        rules = [{"rule_id": "r1", "name": "Rule1"}]
        export_svc._rule_repo.list_rules_by_ids = AsyncMock(return_value=rules)
        result = await export_svc.export_rules(rule_ids=["r1"], format=ExportFormat.JSON)
        parsed = json.loads(result)
        assert parsed["type"] == "rules"
        assert parsed["count"] == 1

    async def test_export_rules_csv(self, export_svc):
        """导出规则CSV"""
        rules = [{"rule_id": "r1", "name": "R1", "device_id": "d1", "severity": "warning", "enabled": True}]
        export_svc._rule_repo.list_all = AsyncMock(return_value=(rules, 1))
        result = await export_svc.export_rules(format=ExportFormat.CSV)
        assert "rule_id" in result
        assert "r1" in result

    async def test_export_rules_csv_empty(self, export_svc):
        """空规则CSV"""
        export_svc._rule_repo.list_all = AsyncMock(return_value=([], 0))
        result = await export_svc.export_rules(format=ExportFormat.CSV)
        assert result == ""

    async def test_export_all(self, export_svc):
        """导出全部"""
        devices = [{"device_id": "d1"}]
        rules = [{"rule_id": "r1"}]
        export_svc._device_repo.list_all = AsyncMock(return_value=(devices, 1))
        export_svc._rule_repo.list_all = AsyncMock(return_value=(rules, 1))
        result = await export_svc.export_all()
        parsed = json.loads(result)
        assert parsed["type"] == "full_backup"
        assert parsed["device_count"] == 1
        assert parsed["rule_count"] == 1
        assert parsed["devices"] == devices
        assert parsed["rules"] == rules


class TestParseJson:
    @pytest.fixture
    def import_svc(self):
        return DataImportService(_make_repo(), _make_repo())

    def test_parse_json_dict_devices(self, import_svc):
        """解析devices"""
        items = import_svc._parse_json(json.dumps({"devices": [{"device_id": "d1"}]}))
        assert items == [{"device_id": "d1"}]

    def test_parse_json_dict_rules(self, import_svc):
        """解析rules"""
        items = import_svc._parse_json(json.dumps({"rules": [{"rule_id": "r1"}]}))
        assert items == [{"rule_id": "r1"}]

    def test_parse_json_top_level_list(self, import_svc):
        """解析list"""
        items = import_svc._parse_json(json.dumps([{"a": 1}]))
        assert items == [{"a": 1}]

    def test_parse_json_invalid_top_level(self, import_svc):
        """顶层非法"""
        with pytest.raises(ValueError, match="expected dict or list"):
            import_svc._parse_json(json.dumps("just a string"))

    def test_parse_json_missing_key(self, import_svc):
        """缺少键"""
        with pytest.raises(ValueError, match="missing 'devices' or 'rules'"):
            import_svc._parse_json(json.dumps({"other": []}))

    def test_parse_json_items_not_list(self, import_svc):
        """值非list"""
        with pytest.raises(ValueError, match="expected list"):
            import_svc._parse_json(json.dumps({"devices": "not a list"}))

    def test_parse_json_item_not_dict(self, import_svc):
        """元素非dict"""
        with pytest.raises(ValueError, match="expected dict"):
            import_svc._parse_json(json.dumps({"devices": ["not a dict"]}))

    def test_parse_devices_csv(self, import_svc):
        """解析CSV设备"""
        csv_data = "device_id,name,protocol\nd1,Dev1,modbus_tcp\n"
        items = import_svc._parse_devices_csv(csv_data)
        assert len(items) == 1
        assert items[0]["device_id"] == "d1"

    def test_parse_rules_csv(self, import_svc):
        """解析CSV规则"""
        csv_data = "rule_id,name,device_id\nr1,R1,d1\n"
        items = import_svc._parse_rules_csv(csv_data)
        assert len(items) == 1
        assert items[0]["rule_id"] == "r1"


class TestImportDevices:
    @pytest.fixture
    def import_svc(self):
        return DataImportService(_make_repo(), _make_repo())

    async def test_import_devices_empty(self, import_svc):
        """空数据导入"""
        result = await import_svc.import_devices(json.dumps({"devices": []}))
        assert result.success is True
        assert result.total_count == 0
        assert result.imported_count == 0

    async def test_import_devices_parse_error(self, import_svc):
        """解析错误"""
        result = await import_svc.import_devices("invalid json")
        assert result.success is False
        assert any("Parse error" in e for e in result.errors)

    async def test_import_devices_atomic_success(self, import_svc):
        """原子导入成功"""
        db = MagicMock()
        session = AsyncMock()
        session.execute = AsyncMock(return_value=_make_exec_result(rows=[]))
        db.session.return_value = _make_session_ctx(session)
        import_svc._device_repo._database = db
        import_svc._device_repo.upsert_bulk = AsyncMock(return_value=(2, 0, []))

        data = json.dumps({"devices": [{"device_id": "d1"}, {"device_id": "d2"}]})
        result = await import_svc.import_devices(data, mode=ImportMode.OVERWRITE)
        assert result.success is True
        assert result.imported_count == 2
        session.commit.assert_called_once()

    async def test_import_devices_atomic_skip(self, import_svc):
        """SKIP模式"""
        db = MagicMock()
        session = AsyncMock()
        session.execute = AsyncMock(return_value=_make_exec_result(rows=[]))
        db.session.return_value = _make_session_ctx(session)
        import_svc._device_repo._database = db
        import_svc._device_repo.upsert_bulk = AsyncMock(return_value=(1, 1, []))

        result = await import_svc.import_devices(json.dumps({"devices": [{"device_id": "d1"}]}), mode=ImportMode.SKIP)
        assert result.imported_count == 1
        assert result.skipped_count == 1

    async def test_import_devices_atomic_rename(self, import_svc):
        """RENAME重命名"""
        db = MagicMock()
        session = AsyncMock()
        session.execute = AsyncMock(return_value=_make_exec_result(rows=[("d1",)]))
        db.session.return_value = _make_session_ctx(session)
        import_svc._device_repo._database = db
        import_svc._device_repo.upsert_bulk = AsyncMock(return_value=(1, 0, []))

        result = await import_svc.import_devices(json.dumps({"devices": [{"device_id": "d1"}]}), mode=ImportMode.RENAME)
        assert any("Renamed device" in w for w in result.warnings)
        import_svc._device_repo.upsert_bulk.assert_called_once()
        items_arg = import_svc._device_repo.upsert_bulk.call_args[0][0]
        assert items_arg[0]["device_id"] == "d1_imported"

    async def test_import_devices_atomic_error_conflict(self, import_svc):
        """ERROR冲突"""
        db = MagicMock()
        session = AsyncMock()
        session.execute = AsyncMock(return_value=_make_exec_result(rows=[("d1",)]))
        db.session.return_value = _make_session_ctx(session)
        import_svc._device_repo._database = db

        result = await import_svc.import_devices(json.dumps({"devices": [{"device_id": "d1"}]}), mode=ImportMode.ERROR)
        assert result.success is False
        assert any("Atomic import failed" in e for e in result.errors)

    async def test_import_devices_atomic_upsert_errors(self, import_svc):
        """upsert错误回滚"""
        db = MagicMock()
        session = AsyncMock()
        session.execute = AsyncMock(return_value=_make_exec_result(rows=[]))
        db.session.return_value = _make_session_ctx(session)
        import_svc._device_repo._database = db
        import_svc._device_repo.upsert_bulk = AsyncMock(return_value=(0, 0, ["d1: invalid"]))

        result = await import_svc.import_devices(
            json.dumps({"devices": [{"device_id": "d1"}]}), mode=ImportMode.OVERWRITE
        )
        assert result.success is False
        assert result.error_count == 1
        session.rollback.assert_called_once()

    async def test_import_devices_fallback_skip(self, import_svc):
        """回退SKIP"""
        import_svc._device_repo._database = None
        import_svc._device_repo.get = AsyncMock(return_value={"device_id": "d1"})

        result = await import_svc.import_devices(json.dumps({"devices": [{"device_id": "d1"}]}), mode=ImportMode.SKIP)
        assert result.skipped_count == 1
        assert result.imported_count == 0

    async def test_import_devices_fallback_create_new(self, import_svc):
        """回退创建新设备"""
        import_svc._device_repo._database = None
        import_svc._device_repo.get = AsyncMock(return_value=None)
        import_svc._device_repo.create = AsyncMock(return_value={"device_id": "new"})

        result = await import_svc.import_devices(json.dumps({"devices": [{"name": "Dev"}]}), mode=ImportMode.SKIP)
        assert result.imported_count == 1
        assert any("Generated device_id" in w for w in result.warnings)

    async def test_import_devices_fallback_overwrite(self, import_svc):
        """回退OVERWRITE"""
        import_svc._device_repo._database = None
        import_svc._device_repo.get = AsyncMock(return_value={"device_id": "d1"})
        import_svc._device_repo.update = AsyncMock(return_value={"device_id": "d1"})

        result = await import_svc.import_devices(
            json.dumps({"devices": [{"device_id": "d1", "name": "updated"}]}),
            mode=ImportMode.OVERWRITE,
        )
        assert result.imported_count == 1
        import_svc._device_repo.update.assert_called_once()

    async def test_import_devices_fallback_rename(self, import_svc):
        """回退RENAME"""
        import_svc._device_repo._database = None
        import_svc._device_repo.get = AsyncMock(return_value={"device_id": "d1"})
        import_svc._device_repo.create = AsyncMock(return_value={"device_id": "d1_imported"})

        result = await import_svc.import_devices(json.dumps({"devices": [{"device_id": "d1"}]}), mode=ImportMode.RENAME)
        assert result.imported_count == 1
        assert any("Renamed device" in w for w in result.warnings)

    async def test_import_devices_fallback_error_conflict(self, import_svc):
        """回退ERROR冲突"""
        import_svc._device_repo._database = None
        import_svc._device_repo.get = AsyncMock(return_value={"device_id": "d1"})

        result = await import_svc.import_devices(json.dumps({"devices": [{"device_id": "d1"}]}), mode=ImportMode.ERROR)
        assert result.success is False
        assert result.error_count == 1
        assert any("already exists" in e for e in result.errors)

    async def test_import_devices_fallback_create_exception(self, import_svc):
        """回退create异常"""
        import_svc._device_repo._database = None
        import_svc._device_repo.get = AsyncMock(return_value=None)
        import_svc._device_repo.create = AsyncMock(side_effect=RuntimeError("db error"))

        result = await import_svc.import_devices(json.dumps({"devices": [{"device_id": "d1"}]}), mode=ImportMode.SKIP)
        assert result.success is False
        assert result.error_count == 1

    async def test_import_devices_csv_format(self, import_svc):
        """CSV导入设备"""
        import_svc._device_repo._database = None
        import_svc._device_repo.get = AsyncMock(return_value=None)
        import_svc._device_repo.create = AsyncMock(return_value={"device_id": "d1"})

        csv_data = "device_id,name,protocol\nd1,Dev1,modbus_tcp\n"
        result = await import_svc.import_devices(csv_data, format=ExportFormat.CSV)
        assert result.imported_count == 1


class TestImportRules:
    @pytest.fixture
    def import_svc(self):
        return DataImportService(_make_repo(), _make_repo())

    async def test_import_rules_empty(self, import_svc):
        """空规则导入"""
        result = await import_svc.import_rules(json.dumps({"rules": []}))
        assert result.success is True
        assert result.total_count == 0

    async def test_import_rules_parse_error(self, import_svc):
        """规则解析错误"""
        result = await import_svc.import_rules("not json")
        assert result.success is False
        assert any("Parse error" in e for e in result.errors)

    async def test_import_rules_atomic_success(self, import_svc):
        """原子规则成功"""
        db = MagicMock()
        session = AsyncMock()
        session.execute = AsyncMock(return_value=_make_exec_result(rows=[]))
        db.session.return_value = _make_session_ctx(session)
        import_svc._rule_repo._database = db
        import_svc._rule_repo.upsert_bulk = AsyncMock(return_value=(1, 0, []))

        result = await import_svc.import_rules(json.dumps({"rules": [{"rule_id": "r1"}]}), mode=ImportMode.OVERWRITE)
        assert result.imported_count == 1
        session.commit.assert_called_once()

    async def test_import_rules_atomic_rename(self, import_svc):
        """规则RENAME"""
        db = MagicMock()
        session = AsyncMock()
        session.execute = AsyncMock(return_value=_make_exec_result(rows=[("r1",)]))
        db.session.return_value = _make_session_ctx(session)
        import_svc._rule_repo._database = db
        import_svc._rule_repo.upsert_bulk = AsyncMock(return_value=(1, 0, []))

        result = await import_svc.import_rules(json.dumps({"rules": [{"rule_id": "r1"}]}), mode=ImportMode.RENAME)
        assert any("Renamed rule" in w for w in result.warnings)
        items_arg = import_svc._rule_repo.upsert_bulk.call_args[0][0]
        assert items_arg[0]["rule_id"] == "r1_imported"

    async def test_import_rules_atomic_error_conflict(self, import_svc):
        """规则ERROR冲突"""
        db = MagicMock()
        session = AsyncMock()
        session.execute = AsyncMock(return_value=_make_exec_result(rows=[("r1",)]))
        db.session.return_value = _make_session_ctx(session)
        import_svc._rule_repo._database = db

        result = await import_svc.import_rules(json.dumps({"rules": [{"rule_id": "r1"}]}), mode=ImportMode.ERROR)
        assert result.success is False
        assert any("Atomic import failed" in e for e in result.errors)

    async def test_import_rules_atomic_upsert_errors(self, import_svc):
        """规则upsert错误"""
        db = MagicMock()
        session = AsyncMock()
        session.execute = AsyncMock(return_value=_make_exec_result(rows=[]))
        db.session.return_value = _make_session_ctx(session)
        import_svc._rule_repo._database = db
        import_svc._rule_repo.upsert_bulk = AsyncMock(return_value=(0, 0, ["r1: bad"]))

        result = await import_svc.import_rules(json.dumps({"rules": [{"rule_id": "r1"}]}), mode=ImportMode.OVERWRITE)
        assert result.success is False
        session.rollback.assert_called_once()

    async def test_import_rules_fallback_skip(self, import_svc):
        """规则回退SKIP"""
        import_svc._rule_repo._database = None
        import_svc._rule_repo.get = AsyncMock(return_value={"rule_id": "r1"})

        result = await import_svc.import_rules(json.dumps({"rules": [{"rule_id": "r1"}]}), mode=ImportMode.SKIP)
        assert result.skipped_count == 1

    async def test_import_rules_fallback_overwrite(self, import_svc):
        """规则回退OVERWRITE"""
        import_svc._rule_repo._database = None
        import_svc._rule_repo.get = AsyncMock(return_value={"rule_id": "r1"})
        import_svc._rule_repo.update = AsyncMock(return_value={"rule_id": "r1"})

        result = await import_svc.import_rules(json.dumps({"rules": [{"rule_id": "r1"}]}), mode=ImportMode.OVERWRITE)
        assert result.imported_count == 1
        import_svc._rule_repo.update.assert_called_once()

    async def test_import_rules_fallback_rename(self, import_svc):
        """规则回退RENAME"""
        import_svc._rule_repo._database = None
        import_svc._rule_repo.get = AsyncMock(return_value={"rule_id": "r1"})
        import_svc._rule_repo.create = AsyncMock(return_value={"rule_id": "r1_imported"})

        result = await import_svc.import_rules(json.dumps({"rules": [{"rule_id": "r1"}]}), mode=ImportMode.RENAME)
        assert result.imported_count == 1
        assert any("Renamed rule" in w for w in result.warnings)

    async def test_import_rules_csv_format(self, import_svc):
        """CSV导入规则"""
        import_svc._rule_repo._database = None
        import_svc._rule_repo.get = AsyncMock(return_value=None)
        import_svc._rule_repo.create = AsyncMock(return_value={"rule_id": "r1"})

        csv_data = "rule_id,name,device_id\nr1,R1,d1\n"
        result = await import_svc.import_rules(csv_data, format=ExportFormat.CSV)
        assert result.imported_count == 1


class TestImportAll:
    @pytest.fixture
    def import_svc(self):
        return DataImportService(_make_repo(), _make_repo())

    async def test_import_all_json_decode_error(self, import_svc):
        """JSON解析失败"""
        result = await import_svc.import_all("not json")
        assert "error" in result

    async def test_import_all_non_dict_top_level(self, import_svc):
        """顶层非dict"""
        result = await import_svc.import_all(json.dumps([1, 2, 3]))
        assert "error" in result

    async def test_import_all_non_list_devices(self, import_svc):
        """devices非list"""
        result = await import_svc.import_all(json.dumps({"devices": "bad"}))
        assert "error" in result

    async def test_import_all_non_list_rules(self, import_svc):
        """rules非list"""
        result = await import_svc.import_all(json.dumps({"rules": "bad"}))
        assert "error" in result

    async def test_import_all_atomic_success(self, import_svc):
        """全部导入成功"""
        db = MagicMock()
        session = AsyncMock()
        db.session.return_value = _make_session_ctx(session)
        import_svc._device_repo._database = db
        import_svc._rule_repo._database = db
        import_svc._device_repo.upsert_bulk = AsyncMock(return_value=(2, 0, []))
        import_svc._rule_repo.upsert_bulk = AsyncMock(return_value=(1, 0, []))

        data = json.dumps(
            {
                "devices": [{"device_id": "d1"}],
                "rules": [{"rule_id": "r1"}],
            }
        )
        result = await import_svc.import_all(data, mode=ImportMode.OVERWRITE)
        assert result["devices"].success is True
        assert result["devices"].imported_count == 2
        assert result["rules"].success is True
        assert result["rules"].imported_count == 1
        session.commit.assert_called_once()

    async def test_import_all_atomic_errors_rollback(self, import_svc):
        """错误回滚验证"""
        db = MagicMock()
        session = AsyncMock()
        session.execute = AsyncMock(return_value=_make_exec_result(scalar=0))
        db.session.return_value = _make_session_ctx(session)
        import_svc._device_repo._database = db
        import_svc._rule_repo._database = db
        import_svc._device_repo.upsert_bulk = AsyncMock(return_value=(0, 0, ["d1: bad"]))
        import_svc._rule_repo.upsert_bulk = AsyncMock(return_value=(0, 0, []))

        data = json.dumps({"devices": [{"device_id": "d1"}], "rules": []})
        result = await import_svc.import_all(data, mode=ImportMode.OVERWRITE)
        assert result["devices"].success is False
        session.rollback.assert_called_once()

    async def test_import_all_atomic_exception_rollback(self, import_svc):
        """异常回滚"""
        db = MagicMock()
        session = AsyncMock()
        session.execute = AsyncMock(return_value=_make_exec_result(scalar=0))
        db.session.return_value = _make_session_ctx(session)
        import_svc._device_repo._database = db
        import_svc._rule_repo._database = db
        import_svc._device_repo.upsert_bulk = AsyncMock(side_effect=RuntimeError("crash"))

        data = json.dumps({"devices": [{"device_id": "d1"}], "rules": []})
        result = await import_svc.import_all(data, mode=ImportMode.OVERWRITE)
        assert result["devices"].success is False
        assert result["rules"].success is False

    async def test_import_all_session_creation_error(self, import_svc):
        """session失败"""
        db = MagicMock()
        db.session.side_effect = RuntimeError("no connection")
        import_svc._device_repo._database = db
        import_svc._rule_repo._database = db

        data = json.dumps({"devices": [{"device_id": "d1"}], "rules": []})
        result = await import_svc.import_all(data)
        assert result["devices"].success is False
        assert result["rules"].success is False

    async def test_import_all_fallback_success(self, import_svc):
        """回退分别成功"""
        import_svc._device_repo._database = None
        import_svc._rule_repo._database = None
        import_svc._device_repo.get = AsyncMock(return_value=None)
        import_svc._device_repo.create = AsyncMock(return_value={"device_id": "d1"})
        import_svc._rule_repo.get = AsyncMock(return_value=None)
        import_svc._rule_repo.create = AsyncMock(return_value={"rule_id": "r1"})

        data = json.dumps({"devices": [{"device_id": "d1"}], "rules": [{"rule_id": "r1"}]})
        result = await import_svc.import_all(data)
        assert result["devices"].success is True
        assert result["rules"].success is True

    async def test_import_all_fallback_device_failed_skip_rules(self, import_svc):
        """回退设备失败跳过规则"""
        import_svc._device_repo._database = None
        import_svc._rule_repo._database = None
        import_svc._device_repo.get = AsyncMock(return_value=None)
        import_svc._device_repo.create = AsyncMock(side_effect=RuntimeError("fail"))

        data = json.dumps({"devices": [{"device_id": "d1"}], "rules": [{"rule_id": "r1"}]})
        result = await import_svc.import_all(data)
        assert result["devices"].success is False
        assert result["devices"].imported_count == 0
        assert "rules" in result
        assert result["rules"].success is False

    async def test_import_all_fallback_rule_failed_rollback_devices(self, import_svc):
        """回退规则失败回滚设备"""
        import_svc._device_repo._database = None
        import_svc._rule_repo._database = None
        import_svc._device_repo.get = AsyncMock(return_value=None)
        import_svc._device_repo.create = AsyncMock(return_value={"device_id": "d1"})
        import_svc._rule_repo.get = AsyncMock(return_value=None)
        import_svc._rule_repo.create = AsyncMock(side_effect=RuntimeError("rule fail"))

        data = json.dumps({"devices": [{"device_id": "d1"}], "rules": [{"rule_id": "r1"}]})
        result = await import_svc.import_all(data)
        import_svc._device_repo.delete.assert_called_once_with("d1")
        assert result["devices"].imported_count == 0
        assert result["devices"].success is False


class TestFactoryFunctions:
    def test_get_export_service_creates_with_repos(self):
        """创建导出服务"""
        repo = _make_repo()
        svc = get_export_service(repo, repo, repo)
        assert svc is not None
        assert isinstance(svc, DataExportService)

    def test_get_export_service_returns_existing(self):
        """返回同一实例"""
        repo = _make_repo()
        svc1 = get_export_service(repo, repo, repo)
        svc2 = get_export_service()
        assert svc1 is svc2

    def test_get_export_service_none_without_repos(self):
        """无repo返回None"""
        assert get_export_service() is None

    def test_get_import_service_creates_with_repos(self):
        """创建导入服务"""
        repo = _make_repo()
        svc = get_import_service(repo, repo)
        assert svc is not None
        assert isinstance(svc, DataImportService)

    def test_get_import_service_returns_existing(self):
        """返回同一实例"""
        repo = _make_repo()
        svc1 = get_import_service(repo, repo)
        svc2 = get_import_service()
        assert svc1 is svc2

    def test_get_import_service_none_without_repos(self):
        """无repo返回None"""
        assert get_import_service() is None

    def test_get_template_service_creates(self):
        """创建模板单例"""
        svc1 = get_template_service()
        svc2 = get_template_service()
        assert svc1 is svc2
        assert isinstance(svc1, TemplateService)


class TestDeviceTemplateSvc:
    async def test_create_get_list_delete(self):
        svc = TemplateService()
        t = await svc.create_device_template(DeviceTemplate(name="t1", protocol="modbus_tcp"))
        assert t.template_id != ""
        assert await svc.get_device_template(t.template_id) is not None
        assert len(await svc.list_device_templates()) == 1
        assert await svc.delete_device_template(t.template_id) is True
        assert await svc.delete_device_template(t.template_id) is False

    async def test_apply_device_template(self):
        svc = TemplateService()
        await svc.create_device_template(
            DeviceTemplate(template_id="t1", name="T", protocol="modbus_tcp", default_config={"ip": "1.1.1.1"})
        )
        d = await svc.apply_device_template("t1", "Dev", custom_config={"port": 502})
        assert d["protocol"] == "modbus_tcp" and d["config"]["port"] == 502

    async def test_apply_device_template_not_found(self):
        with pytest.raises(ValueError, match="Template not found"):
            await TemplateService().apply_device_template("x", "D")


class TestRuleTemplateSvc:
    async def test_create_get_list_delete(self):
        svc = TemplateService()
        t = await svc.create_rule_template(RuleTemplate(name="rt"))
        assert t.template_id != ""
        assert await svc.get_rule_template(t.template_id) is not None
        assert len(await svc.list_rule_templates()) == 1
        assert await svc.delete_rule_template(t.template_id) is True

    async def test_update_rule_template(self):
        svc = TemplateService()
        t = await svc.create_rule_template(RuleTemplate(template_id="rt1", name="RT"))
        r = await svc.update_rule_template("rt1", {"name": "Updated", "default_duration": 10})
        assert r.name == "Updated" and r.default_duration == 10
        assert await svc.update_rule_template("missing", {}) is None

    async def test_apply_rule_template(self):
        svc = TemplateService()
        await svc.create_rule_template(
            RuleTemplate(
                template_id="rt1",
                name="RT",
                default_conditions=[{"point": "t", "device_id": "X"}],
                default_severity="warning",
                default_duration=5,
            )
        )
        r = await svc.apply_rule_template("rt1", "Rule", "d1")
        assert r["device_id"] == "d1" and r["conditions"][0]["device_id"] == "d1"

    async def test_apply_rule_template_not_found(self):
        with pytest.raises(ValueError, match="Template not found"):
            await TemplateService().apply_rule_template("x", "R", "d")


class TestDeviceGroupSvc:
    async def test_create_get_list_delete(self):
        svc = TemplateService()
        g = await svc.create_device_group(DeviceGroup(name="G1", device_ids=["d1"]))
        assert g.group_id != ""
        assert await svc.get_device_group(g.group_id) is not None
        assert len(await svc.list_device_groups()) == 1
        assert await svc.delete_device_group(g.group_id) is True

    async def test_update_device_group(self):
        svc = TemplateService()
        await svc.create_device_group(DeviceGroup(group_id="g1", name="G"))
        r = await svc.update_device_group("g1", {"name": "Updated"})
        assert r.name == "Updated"
        assert await svc.update_device_group("missing", {}) is None

    async def test_add_remove_device(self):
        svc = TemplateService()
        await svc.create_device_group(DeviceGroup(group_id="g1", name="G"))
        assert await svc.add_device_to_group("d1", "g1") is True
        assert await svc.remove_device_from_group("d1", "g1") is True
        assert await svc.remove_device_from_group("d1", "g1") is False

    def test_get_device_groups_and_devices(self):
        svc = TemplateService()
        g = DeviceGroup(group_id="g1", name="G", device_ids=["d1", "d2"])
        with svc._lock:
            svc._device_groups["g1"] = g
            svc._group_members["d1"] = ["g1"]
        assert svc.get_group_devices("g1") == ["d1", "d2"]
        assert len(svc.get_device_groups("d1")) == 1
        assert svc.get_device_groups("x") == []
        assert svc.get_group_devices("x") == []


class TestTemplateExportImport:
    async def test_export_templates(self):
        svc = TemplateService()
        await svc.create_device_template(DeviceTemplate(name="t", protocol="modbus_tcp"))
        await svc.create_rule_template(RuleTemplate(name="rt"))
        await svc.create_device_group(DeviceGroup(name="g"))
        parsed = json.loads(await svc.export_templates())
        assert parsed["type"] == "templates"
        assert len(parsed["device_templates"]) == 1

    async def test_import_templates(self):
        svc = TemplateService()
        data = json.dumps(
            {
                "device_templates": [{"template_id": "t1", "name": "T", "protocol": "modbus_tcp"}],
                "rule_templates": [{"template_id": "rt1", "name": "RT", "rule_type": "threshold"}],
                "device_groups": [{"group_id": "g1", "name": "G", "device_ids": ["d1"]}],
            }
        )
        r = svc.import_templates(data)
        assert r == {"device_templates": 1, "rule_templates": 1, "device_groups": 1}
        assert await svc.get_device_template("t1") is not None
        assert svc.get_group_devices("g1") == ["d1"]
