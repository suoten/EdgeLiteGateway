"""模板服务测试 - 设备/规则模板与设备组 CRUD

覆盖 services/data_import_export.py 的 TemplateService 部分:
- DeviceTemplate / RuleTemplate / DeviceGroup 模板管理
- TemplateService: DB 持久化 + 内存回退双路径
- 设备组管理: 创建/查询/更新/删除/成员管理
- 模板导出/导入
"""

from __future__ import annotations

import sys

sys.path.insert(0, "src")

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from edgelite.services.data_import_export import (
    DeviceGroup,
    DeviceTemplate,
    RuleTemplate,
    TemplateService,
)

# ───────────────────────── 辅助函数 ─────────────────────────


def _make_session_ctx(session=None):
    """构建 database.session() 异步上下文管理器 mock。"""
    session = session or AsyncMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx


def _make_exec_result(rows=None, scalar_one=None, scalars=None, scalar=None, rowcount=0):
    """构建 session.execute 返回值，支持多种访问模式。"""
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
    """构建 RuleTemplateORM mock 对象。"""
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
    """构建 DeviceGroupORM mock 对象。"""
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
def _reset_global_template_service():
    """每个测试前后重置模板服务单例。"""
    from edgelite.services import data_import_export as mod

    orig = mod._template_service
    mod._template_service = None
    yield
    mod._template_service = orig


# ───────────────────────── TemplateService - 设备模板 ─────────────────────────


class TestDeviceTemplates:
    async def test_create_device_template_with_repo(self):
        """通过 TemplateRepo 持久化设备模板"""
        repo = MagicMock()
        repo.create = AsyncMock(return_value={"name": "tmpl1", "created_at": "2024-01-01"})
        svc = TemplateService(template_repo=repo)
        tmpl = DeviceTemplate(name="tmpl1", protocol="modbus_tcp", default_config={"ip": "1.2.3.4"})
        result = await svc.create_device_template(tmpl)
        assert result.template_id == "tmpl1"
        assert result.created_at == "2024-01-01"

    async def test_create_device_template_with_repo_generates_name(self):
        """TemplateRepo 持久化-无 name 时自动生成"""
        repo = MagicMock()
        repo.create = AsyncMock(return_value={"name": "generated", "created_at": "2024"})
        svc = TemplateService(template_repo=repo)
        tmpl = DeviceTemplate(name="", protocol="modbus_tcp")
        await svc.create_device_template(tmpl)

    async def test_get_device_template_with_repo_found(self):
        """从 TemplateRepo 获取设备模板"""
        repo = MagicMock()
        repo.get = AsyncMock(
            return_value={
                "name": "t1",
                "protocol": "modbus_tcp",
                "config_template": {"ip": "1.1.1.1"},
                "point_templates": [{"name": "p1"}],
                "created_at": "2024-01-01",
            }
        )
        svc = TemplateService(template_repo=repo)
        result = await svc.get_device_template("t1")
        assert result is not None
        assert result.protocol == "modbus_tcp"
        assert result.default_config == {"ip": "1.1.1.1"}

    async def test_get_device_template_with_repo_not_found(self):
        """TemplateRepo 获取不存在的模板返回 None"""
        repo = MagicMock()
        repo.get = AsyncMock(return_value=None)
        svc = TemplateService(template_repo=repo)
        assert await svc.get_device_template("missing") is None

    async def test_list_device_templates_with_repo(self):
        """从 TemplateRepo 列出设备模板"""
        repo = MagicMock()
        repo.list_all = AsyncMock(
            return_value=(
                [{"name": "t1", "protocol": "modbus_tcp", "config_template": {}, "point_templates": []}],
                1,
            )
        )
        svc = TemplateService(template_repo=repo)
        result = await svc.list_device_templates()
        assert len(result) == 1
        assert result[0].name == "t1"

    async def test_delete_device_template_with_repo(self):
        """通过 TemplateRepo 删除设备模板"""
        repo = MagicMock()
        repo.delete = AsyncMock(return_value=True)
        svc = TemplateService(template_repo=repo)
        assert await svc.delete_device_template("t1") is True

    async def test_apply_device_template_found(self):
        """应用设备模板创建设备配置"""
        repo = MagicMock()
        repo.get = AsyncMock(
            return_value={
                "name": "t1",
                "protocol": "modbus_tcp",
                "config_template": {"ip": "1.1.1.1"},
                "point_templates": [{"name": "p1"}],
                "created_at": "2024",
            }
        )
        svc = TemplateService(template_repo=repo)
        device = await svc.apply_device_template("t1", "MyDevice", custom_config={"port": 502})
        assert device["protocol"] == "modbus_tcp"
        assert device["config"]["ip"] == "1.1.1.1"
        assert device["config"]["port"] == 502
        assert device["name"] == "MyDevice"
        assert "device_id" in device

    async def test_apply_device_template_not_found(self):
        """应用不存在的设备模板应抛 ValueError"""
        repo = MagicMock()
        repo.get = AsyncMock(return_value=None)
        svc = TemplateService(template_repo=repo)
        with pytest.raises(ValueError, match="Template not found"):
            await svc.apply_device_template("missing", "Dev")

    async def test_create_device_template_fallback(self):
        """无 repo 时回退到内存存储"""
        svc = TemplateService()
        tmpl = DeviceTemplate(name="t1", protocol="modbus_tcp")
        result = await svc.create_device_template(tmpl)
        assert result.template_id != ""
        assert result.created_at != ""

    async def test_get_device_template_fallback(self):
        """内存模式获取设备模板"""
        svc = TemplateService()
        tmpl = DeviceTemplate(template_id="t1", name="T1", protocol="modbus_tcp")
        await svc.create_device_template(tmpl)
        result = await svc.get_device_template("t1")
        assert result is not None
        assert result.name == "T1"

    async def test_get_device_template_fallback_not_found(self):
        """内存模式获取不存在的模板"""
        svc = TemplateService()
        assert await svc.get_device_template("missing") is None

    async def test_list_device_templates_fallback(self):
        """内存模式列出设备模板"""
        svc = TemplateService()
        await svc.create_device_template(DeviceTemplate(name="t1", protocol="modbus_tcp"))
        result = await svc.list_device_templates()
        assert len(result) == 1

    async def test_delete_device_template_fallback(self):
        """内存模式删除设备模板"""
        svc = TemplateService()
        tmpl = DeviceTemplate(template_id="t1", name="T1", protocol="modbus_tcp")
        await svc.create_device_template(tmpl)
        assert await svc.delete_device_template("t1") is True
        assert await svc.delete_device_template("t1") is False


# ───────────────────────── TemplateService - 规则模板 ─────────────────────────


class TestRuleTemplates:
    async def test_create_rule_template_with_db(self):
        """DB 模式创建规则模板"""
        db = MagicMock()
        session = AsyncMock()
        db.session.return_value = _make_session_ctx(session)
        svc = TemplateService(database=db)
        tmpl = RuleTemplate(name="RT", rule_type="threshold")
        result = await svc.create_rule_template(tmpl)
        assert result.template_id != ""
        session.add.assert_called_once()
        session.commit.assert_called_once()

    async def test_create_rule_template_db_fail_fallback(self):
        """DB 持久化失败回退到内存"""
        db = MagicMock()
        db.session.side_effect = RuntimeError("db down")
        svc = TemplateService(database=db)
        tmpl = RuleTemplate(template_id="rt1", name="RT")
        result = await svc.create_rule_template(tmpl)
        assert result.template_id == "rt1"
        assert await svc.get_rule_template("rt1") is not None

    async def test_create_rule_template_no_db(self):
        """无 DB 时内存存储规则模板"""
        svc = TemplateService()
        tmpl = RuleTemplate(name="RT")
        result = await svc.create_rule_template(tmpl)
        assert result.template_id != ""
        assert await svc.get_rule_template(result.template_id) is not None

    async def test_get_rule_template_with_db_found(self):
        """DB 模式获取规则模板"""
        db = MagicMock()
        session = AsyncMock()
        orm = _make_rule_template_orm()
        session.execute = AsyncMock(return_value=_make_exec_result(scalar_one=orm))
        db.session.return_value = _make_session_ctx(session)
        svc = TemplateService(database=db)
        result = await svc.get_rule_template("rt1")
        assert result is not None
        assert result.name == "RT"
        assert result.default_conditions == [{"point": "t"}]

    async def test_get_rule_template_with_db_not_found(self):
        """DB 模式获取不存在的规则模板"""
        db = MagicMock()
        session = AsyncMock()
        session.execute = AsyncMock(return_value=_make_exec_result(scalar_one=None))
        db.session.return_value = _make_session_ctx(session)
        svc = TemplateService(database=db)
        result = await svc.get_rule_template("missing")
        assert result is None

    async def test_get_rule_template_db_fail_fallback(self):
        """DB 读取失败回退到内存"""
        db = MagicMock()
        db.session.side_effect = RuntimeError("db down")
        svc = TemplateService(database=db)
        tmpl = RuleTemplate(template_id="rt1", name="RT")
        await svc.create_rule_template(tmpl)
        result = await svc.get_rule_template("rt1")
        assert result is not None

    async def test_list_rule_templates_with_db(self):
        """DB 模式列出规则模板"""
        db = MagicMock()
        session = AsyncMock()
        orm = _make_rule_template_orm()
        session.execute = AsyncMock(return_value=_make_exec_result(scalars=[orm]))
        db.session.return_value = _make_session_ctx(session)
        svc = TemplateService(database=db)
        result = await svc.list_rule_templates()
        assert len(result) == 1

    async def test_list_rule_templates_db_fail_fallback(self):
        """DB 列出失败回退到内存"""
        db = MagicMock()
        db.session.side_effect = RuntimeError("db down")
        svc = TemplateService(database=db)
        tmpl = RuleTemplate(template_id="rt1", name="RT")
        await svc.create_rule_template(tmpl)
        result = await svc.list_rule_templates()
        assert len(result) == 1

    async def test_update_rule_template_with_db(self):
        """DB 模式更新规则模板"""
        db = MagicMock()
        session = AsyncMock()
        orm = _make_rule_template_orm()
        session.execute = AsyncMock(return_value=_make_exec_result(scalar_one=orm))
        db.session.return_value = _make_session_ctx(session)
        svc = TemplateService(database=db)
        result = await svc.update_rule_template("rt1", {"name": "Updated"})
        assert result is not None
        assert result.name == "Updated"
        session.commit.assert_called_once()

    async def test_update_rule_template_with_db_not_found(self):
        """DB 模式更新不存在的规则模板返回 None"""
        db = MagicMock()
        session = AsyncMock()
        session.execute = AsyncMock(return_value=_make_exec_result(scalar_one=None))
        db.session.return_value = _make_session_ctx(session)
        svc = TemplateService(database=db)
        assert await svc.update_rule_template("missing", {"name": "x"}) is None

    async def test_update_rule_template_fallback(self):
        """内存模式更新规则模板"""
        svc = TemplateService()
        tmpl = RuleTemplate(template_id="rt1", name="RT")
        await svc.create_rule_template(tmpl)
        result = await svc.update_rule_template("rt1", {"name": "Updated", "default_duration": 10})
        assert result is not None
        assert result.name == "Updated"
        assert result.default_duration == 10

    async def test_update_rule_template_fallback_not_found(self):
        """内存模式更新不存在的规则模板"""
        svc = TemplateService()
        assert await svc.update_rule_template("missing", {}) is None

    async def test_delete_rule_template_with_db(self):
        """DB 模式删除规则模板"""
        db = MagicMock()
        session = AsyncMock()
        session.execute = AsyncMock(return_value=_make_exec_result(rowcount=1))
        db.session.return_value = _make_session_ctx(session)
        svc = TemplateService(database=db)
        assert await svc.delete_rule_template("rt1") is True

    async def test_delete_rule_template_with_db_not_found(self):
        """DB 模式删除不存在的规则模板"""
        db = MagicMock()
        session = AsyncMock()
        session.execute = AsyncMock(return_value=_make_exec_result(rowcount=0))
        db.session.return_value = _make_session_ctx(session)
        svc = TemplateService(database=db)
        assert await svc.delete_rule_template("missing") is False

    async def test_delete_rule_template_fallback(self):
        """内存模式删除规则模板"""
        svc = TemplateService()
        tmpl = RuleTemplate(template_id="rt1", name="RT")
        await svc.create_rule_template(tmpl)
        assert await svc.delete_rule_template("rt1") is True
        assert await svc.delete_rule_template("rt1") is False

    async def test_apply_rule_template_found(self):
        """应用规则模板创建规则配置"""
        svc = TemplateService()
        tmpl = RuleTemplate(
            template_id="rt1",
            name="RT",
            rule_type="threshold",
            default_conditions=[{"point": "temp", "operator": ">", "device_id": "PLACEHOLDER"}],
            default_severity="warning",
            default_duration=5,
            notify_channels=["email"],
        )
        await svc.create_rule_template(tmpl)
        rule = await svc.apply_rule_template("rt1", "MyRule", "d1")
        assert rule["device_id"] == "d1"
        assert rule["conditions"][0]["device_id"] == "d1"
        assert rule["severity"] == "warning"
        assert rule["duration"] == 5

    async def test_apply_rule_template_custom_conditions(self):
        """应用规则模板使用自定义条件"""
        svc = TemplateService()
        tmpl = RuleTemplate(template_id="rt1", name="RT")
        await svc.create_rule_template(tmpl)
        rule = await svc.apply_rule_template(
            "rt1",
            "MyRule",
            "d1",
            custom_conditions=[{"point": "x", "operator": "<", "device_id": "old"}],
        )
        assert rule["conditions"][0]["device_id"] == "d1"

    async def test_apply_rule_template_not_found(self):
        """应用不存在的规则模板应抛 ValueError"""
        svc = TemplateService()
        with pytest.raises(ValueError, match="Template not found"):
            await svc.apply_rule_template("missing", "R", "d1")


# ───────────────────────── TemplateService - 设备组 ─────────────────────────


class TestDeviceGroups:
    async def test_create_device_group_with_db(self):
        """DB 模式创建设备组"""
        db = MagicMock()
        session = AsyncMock()
        db.session.return_value = _make_session_ctx(session)
        svc = TemplateService(database=db)
        group = DeviceGroup(name="G1", description="desc", device_ids=["d1"])
        result = await svc.create_device_group(group)
        assert result.group_id != ""
        session.add.assert_called_once()

    async def test_create_device_group_db_fail_fallback(self):
        """DB 持久化失败回退到内存"""
        db = MagicMock()
        db.session.side_effect = RuntimeError("db down")
        svc = TemplateService(database=db)
        group = DeviceGroup(group_id="g1", name="G1")
        result = await svc.create_device_group(group)
        assert result.group_id == "g1"
        assert await svc.get_device_group("g1") is not None

    async def test_create_device_group_no_db(self):
        """无 DB 时内存存储设备组"""
        svc = TemplateService()
        group = DeviceGroup(name="G1")
        result = await svc.create_device_group(group)
        assert result.group_id != ""

    async def test_get_device_group_with_db_found(self):
        """DB 模式获取设备组"""
        db = MagicMock()
        session = AsyncMock()
        orm = _make_group_orm()
        session.execute = AsyncMock(return_value=_make_exec_result(scalar_one=orm))
        db.session.return_value = _make_session_ctx(session)
        svc = TemplateService(database=db)
        result = await svc.get_device_group("g1")
        assert result is not None
        assert result.name == "G"
        assert result.device_ids == ["d1"]

    async def test_get_device_group_with_db_not_found(self):
        """DB 模式获取不存在的设备组"""
        db = MagicMock()
        session = AsyncMock()
        session.execute = AsyncMock(return_value=_make_exec_result(scalar_one=None))
        db.session.return_value = _make_session_ctx(session)
        svc = TemplateService(database=db)
        assert await svc.get_device_group("missing") is None

    async def test_get_device_group_db_fail_fallback(self):
        """DB 读取失败回退到内存"""
        db = MagicMock()
        db.session.side_effect = RuntimeError("db down")
        svc = TemplateService(database=db)
        group = DeviceGroup(group_id="g1", name="G1")
        await svc.create_device_group(group)
        result = await svc.get_device_group("g1")
        assert result is not None

    async def test_list_device_groups_with_db(self):
        """DB 模式列出设备组"""
        db = MagicMock()
        session = AsyncMock()
        orm = _make_group_orm()
        session.execute = AsyncMock(return_value=_make_exec_result(scalars=[orm]))
        db.session.return_value = _make_session_ctx(session)
        svc = TemplateService(database=db)
        result = await svc.list_device_groups()
        assert len(result) == 1

    async def test_list_device_groups_db_fail_fallback(self):
        """DB 列出失败回退到内存"""
        db = MagicMock()
        db.session.side_effect = RuntimeError("db down")
        svc = TemplateService(database=db)
        await svc.create_device_group(DeviceGroup(group_id="g1", name="G1"))
        result = await svc.list_device_groups()
        assert len(result) == 1

    async def test_update_device_group_with_db(self):
        """DB 模式更新设备组"""
        db = MagicMock()
        session = AsyncMock()
        orm = _make_group_orm()
        session.execute = AsyncMock(return_value=_make_exec_result(scalar_one=orm))
        db.session.return_value = _make_session_ctx(session)
        svc = TemplateService(database=db)
        result = await svc.update_device_group("g1", {"name": "Updated"})
        assert result is not None
        assert result.name == "Updated"

    async def test_update_device_group_with_db_not_found(self):
        """DB 模式更新不存在的设备组"""
        db = MagicMock()
        session = AsyncMock()
        session.execute = AsyncMock(return_value=_make_exec_result(scalar_one=None))
        db.session.return_value = _make_session_ctx(session)
        svc = TemplateService(database=db)
        assert await svc.update_device_group("missing", {}) is None

    async def test_update_device_group_fallback(self):
        """内存模式更新设备组"""
        svc = TemplateService()
        await svc.create_device_group(DeviceGroup(group_id="g1", name="G1"))
        result = await svc.update_device_group("g1", {"name": "Updated"})
        assert result.name == "Updated"

    async def test_update_device_group_fallback_not_found(self):
        """内存模式更新不存在的设备组"""
        svc = TemplateService()
        assert await svc.update_device_group("missing", {}) is None

    async def test_delete_device_group_with_db(self):
        """DB 模式删除设备组"""
        db = MagicMock()
        session = AsyncMock()
        session.execute = AsyncMock(return_value=_make_exec_result(rowcount=1))
        db.session.return_value = _make_session_ctx(session)
        svc = TemplateService(database=db)
        assert await svc.delete_device_group("g1") is True

    async def test_delete_device_group_with_db_not_found(self):
        """DB 模式删除不存在的设备组"""
        db = MagicMock()
        session = AsyncMock()
        session.execute = AsyncMock(return_value=_make_exec_result(rowcount=0))
        db.session.return_value = _make_session_ctx(session)
        svc = TemplateService(database=db)
        assert await svc.delete_device_group("missing") is False

    async def test_delete_device_group_fallback(self):
        """内存模式删除设备组"""
        svc = TemplateService()
        await svc.create_device_group(DeviceGroup(group_id="g1", name="G1"))
        assert await svc.delete_device_group("g1") is True
        assert await svc.delete_device_group("g1") is False

    async def test_add_device_to_group_with_db(self):
        """DB 模式添加设备到组"""
        db = MagicMock()
        session = AsyncMock()
        orm = _make_group_orm(device_ids=["d1"])
        session.execute = AsyncMock(return_value=_make_exec_result(scalar_one=orm))
        db.session.return_value = _make_session_ctx(session)
        svc = TemplateService(database=db)
        assert await svc.add_device_to_group("d2", "g1") is True

    async def test_add_device_to_group_db_fail(self):
        """DB 添加设备失败返回 False"""
        db = MagicMock()
        db.session.side_effect = RuntimeError("db down")
        svc = TemplateService(database=db)
        assert await svc.add_device_to_group("d1", "g1") is False

    async def test_add_device_to_group_fallback(self):
        """内存模式添加设备到组"""
        svc = TemplateService()
        await svc.create_device_group(DeviceGroup(group_id="g1", name="G1"))
        assert await svc.add_device_to_group("d1", "g1") is True
        assert await svc.add_device_to_group("d1", "g1") is True

    async def test_remove_device_from_group_with_db(self):
        """DB 模式从组移除设备"""
        db = MagicMock()
        session = AsyncMock()
        orm = _make_group_orm(device_ids=["d1", "d2"])
        session.execute = AsyncMock(return_value=_make_exec_result(scalar_one=orm))
        db.session.return_value = _make_session_ctx(session)
        svc = TemplateService(database=db)
        with svc._lock:
            svc._group_members["d1"] = ["g1"]
        assert await svc.remove_device_from_group("d1", "g1") is True

    async def test_remove_device_from_group_not_member(self):
        """设备不在组中返回 False"""
        svc = TemplateService()
        assert await svc.remove_device_from_group("d1", "g1") is False

    async def test_remove_device_from_group_fallback(self):
        """内存模式从组移除设备"""
        svc = TemplateService()
        await svc.create_device_group(DeviceGroup(group_id="g1", name="G1", device_ids=["d1"]))
        await svc.add_device_to_group("d1", "g1")
        assert await svc.remove_device_from_group("d1", "g1") is True
        assert await svc.remove_device_from_group("d1", "g1") is False

    def test_get_device_groups(self):
        """获取设备所属的所有组"""
        svc = TemplateService()
        g1 = DeviceGroup(group_id="g1", name="G1")
        g2 = DeviceGroup(group_id="g2", name="G2")
        with svc._lock:
            svc._device_groups["g1"] = g1
            svc._device_groups["g2"] = g2
            svc._group_members["d1"] = ["g1", "g2"]
        groups = svc.get_device_groups("d1")
        assert len(groups) == 2

    def test_get_device_groups_empty(self):
        """设备不属于任何组返回空列表"""
        svc = TemplateService()
        assert svc.get_device_groups("d1") == []

    def test_get_group_devices(self):
        """获取组内所有设备"""
        svc = TemplateService()
        g = DeviceGroup(group_id="g1", name="G1", device_ids=["d1", "d2"])
        with svc._lock:
            svc._device_groups["g1"] = g
        assert svc.get_group_devices("g1") == ["d1", "d2"]

    def test_get_group_devices_empty(self):
        """不存在的组返回空列表"""
        svc = TemplateService()
        assert svc.get_group_devices("missing") == []


# ───────────────────────── TemplateService - 导出/导入模板 ─────────────────────────


class TestTemplateExportImport:
    async def test_export_templates(self):
        """导出所有模板为 JSON"""
        svc = TemplateService()
        await svc.create_device_template(DeviceTemplate(name="t1", protocol="modbus_tcp"))
        await svc.create_rule_template(RuleTemplate(name="rt1"))
        await svc.create_device_group(DeviceGroup(name="g1"))
        result = await svc.export_templates()
        parsed = json.loads(result)
        assert parsed["type"] == "templates"
        assert len(parsed["device_templates"]) == 1
        assert len(parsed["rule_templates"]) == 1
        assert len(parsed["device_groups"]) == 1

    async def test_import_templates(self):
        """从 JSON 导入模板"""
        svc = TemplateService()
        data = json.dumps(
            {
                "device_templates": [
                    {"template_id": "t1", "name": "T1", "protocol": "modbus_tcp"},
                ],
                "rule_templates": [
                    {"template_id": "rt1", "name": "RT", "rule_type": "threshold"},
                ],
                "device_groups": [
                    {"group_id": "g1", "name": "G1", "device_ids": ["d1"]},
                ],
            }
        )
        result = svc.import_templates(data)
        assert result["device_templates"] == 1
        assert result["rule_templates"] == 1
        assert result["device_groups"] == 1
        assert await svc.get_device_template("t1") is not None
        assert await svc.get_rule_template("rt1") is not None
        assert await svc.get_device_group("g1") is not None
        assert svc.get_group_devices("g1") == ["d1"]

    async def test_import_templates_empty(self):
        """导入空模板数据"""
        svc = TemplateService()
        result = svc.import_templates(json.dumps({}))
        assert result["device_templates"] == 0
        assert result["rule_templates"] == 0
        assert result["device_groups"] == 0
