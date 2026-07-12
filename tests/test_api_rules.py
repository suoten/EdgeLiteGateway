"""规则管理 API 端点测试 - 覆盖 api/rules.py 全部端点。

覆盖范围：
- list_rules (admin/非admin/共享资源/异常/参数校验)
- create_rule (成功/审计失败/ValueError/异常)
- test_rule_definition (成功/异常)
- batch_delete / batch_enable / batch_disable (混合成功失败/访问拒绝/异常)
- get_rule (成功/404/访问拒绝/异常)
- update_rule (成功/404/StaleDataError/访问拒绝/异常/version注入)
- delete_rule (成功/404/审计pending失败/删除失败/访问拒绝/异常)
- enable_rule / disable_rule (成功/404/返回None/访问拒绝/异常)
- test_rule (成功/404/ValueError/异常/访问拒绝)
- _check_rule_access (admin/owner/shared/denied)
- BatchRuleIds 模型校验
"""

from __future__ import annotations

import sys

sys.path.insert(0, "src")

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from edgelite.api.deps import get_current_user
from edgelite.api.rules import BatchRuleIds, _check_rule_access, router
from edgelite.models.db import StaleDataError


# ───────────────────────── 辅助构造 ─────────────────────────


def _rule_dict(rule_id: str = "r1", created_by: str = "test-admin", version: int = 1) -> dict:
    """构造可被 RuleResponse 校验通过的规则字典"""
    return {
        "rule_id": rule_id,
        "name": "test-rule",
        "device_id": "dev1",
        "conditions": [
            {"point": "temp", "operator": ">", "threshold": 50.0, "type": "threshold"}
        ],
        "logic": "AND",
        "duration": 0,
        "severity": "warning",
        "enabled": True,
        "notify_channels": ["dingtalk"],
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "created_by": created_by,
        "version": version,
        "inference_count": 0,
        "error_count": 0,
    }


def _rule_create_body() -> dict:
    """构造合法的 RuleCreate 请求体"""
    return {
        "name": "test-rule",
        "device_id": "dev1",
        "conditions": [
            {"point": "temp", "operator": ">", "threshold": 50.0, "type": "threshold"}
        ],
        "logic": "AND",
        "duration": 0,
        "severity": "warning",
        "notify_channels": ["dingtalk"],
    }


def _make_rule_service() -> AsyncMock:
    """构造 mock 规则服务，方法默认返回空/None，测试中按需覆写"""
    svc = AsyncMock()
    svc.list_rules = AsyncMock(return_value=([], 0))
    svc.list_rules_by_ids = AsyncMock(return_value=[])
    svc.create_rule = AsyncMock(return_value=_rule_dict())
    svc.get_rule = AsyncMock(return_value=None)
    svc.update_rule = AsyncMock(return_value=None)
    svc.delete_rule = AsyncMock(return_value=True)
    svc.enable_rule = AsyncMock(return_value=_rule_dict())
    svc.disable_rule = AsyncMock(return_value=_rule_dict())
    svc.test_rule = AsyncMock(return_value={"triggered": True})
    return svc


def _make_audit_service() -> AsyncMock:
    """构造 mock 审计服务"""
    svc = AsyncMock()
    svc.log = AsyncMock(return_value=None)
    return svc


def _share_namespace() -> SimpleNamespace:
    """构造带 write_lock 的 _app_state 替身（供 _check_rule_access / list_rules 共享分支使用）"""
    return SimpleNamespace(database=SimpleNamespace(write_lock=MagicMock()))


@pytest.fixture
def make_env():
    """工厂夹具：构建 (app, svc, audit_svc)，可指定角色"""
    from fastapi import FastAPI

    def _factory(role: str = "admin") -> tuple:
        svc = _make_rule_service()
        audit_svc = _make_audit_service()
        app = FastAPI(title="EdgeLite Rules Test")
        app.include_router(router)
        user = {"user_id": "test-admin", "username": "testadmin", "role": role}
        app.dependency_overrides[get_current_user] = lambda: user
        app.state.rule_service = svc
        app.state.audit_service = audit_svc
        return app, svc, audit_svc

    return _factory


@pytest.fixture
async def admin_env(make_env):
    """admin 角色环境，返回 (client, svc, audit)"""
    app, svc, audit = make_env(role="admin")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c, svc, audit


# ───────────────────────── _check_rule_access 直测 ─────────────────────────


class TestCheckRuleAccess:
    async def test_admin_always_allowed(self):
        user = {"role": "admin", "user_id": "u1"}
        await _check_rule_access({"rule_id": "r1", "created_by": "other"}, user)

    async def test_owner_allowed(self):
        user = {"role": "operator", "user_id": "u1"}
        await _check_rule_access({"rule_id": "r1", "created_by": "u1"}, user)

    async def test_shared_allowed(self):
        user = {"role": "operator", "user_id": "u1"}
        rule = {"rule_id": "r1", "created_by": "other"}
        mock_repo = MagicMock()
        mock_repo.check_user_has_access = AsyncMock(return_value=True)
        with (
            patch("edgelite.app._app_state", _share_namespace()),
            patch("edgelite.storage.sqlite_repo.ResourceShareRepo", return_value=mock_repo),
        ):
            await _check_rule_access(rule, user)
        mock_repo.check_user_has_access.assert_awaited_once()

    async def test_denied_raises_403(self):
        from fastapi import HTTPException

        user = {"role": "operator", "user_id": "u1"}
        rule = {"rule_id": "r1", "created_by": "other"}
        mock_repo = MagicMock()
        mock_repo.check_user_has_access = AsyncMock(return_value=False)
        with (
            patch("edgelite.app._app_state", _share_namespace()),
            patch("edgelite.storage.sqlite_repo.ResourceShareRepo", return_value=mock_repo),
        ):
            with pytest.raises(HTTPException) as exc:
                await _check_rule_access(rule, user)
        assert exc.value.status_code == 403


# ───────────────────────── list_rules ─────────────────────────


class TestListRules:
    async def test_admin_list_success(self, admin_env):
        c, svc, _ = admin_env
        svc.list_rules = AsyncMock(return_value=([_rule_dict("r1"), _rule_dict("r2")], 2))
        resp = await c.get("/api/v1/rules")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] == 2
        assert len(body["data"]) == 2
        svc.list_rules.assert_awaited_once()
        assert svc.list_rules.call_args.kwargs.get("created_by") is None

    async def test_admin_list_with_filters(self, admin_env):
        c, svc, _ = admin_env
        svc.list_rules = AsyncMock(return_value=([], 0))
        resp = await c.get(
            "/api/v1/rules", params={"device_id": "dev1", "search": "temp", "severity": "critical"}
        )
        assert resp.status_code == 200
        args = svc.list_rules.call_args
        assert args.args[2] == "dev1"
        assert args.args[3] == "temp"
        assert args.args[4] == "critical"

    async def test_invalid_severity_returns_422(self, admin_env):
        c, _, _ = admin_env
        resp = await c.get("/api/v1/rules", params={"severity": "bogus"})
        assert resp.status_code == 422

    async def test_list_exception_returns_500(self, admin_env):
        c, svc, _ = admin_env
        svc.list_rules = AsyncMock(side_effect=RuntimeError("boom"))
        resp = await c.get("/api/v1/rules")
        assert resp.status_code == 500
        assert resp.json()["detail"] == "ERR_RULE_LIST_FAILED"

    async def test_non_admin_no_shared_ids(self, make_env):
        app, svc, _ = make_env(role="operator")
        svc.list_rules = AsyncMock(return_value=([_rule_dict("r1", created_by="test-admin")], 1))
        mock_repo = MagicMock()
        mock_repo.get_shared_resource_ids = AsyncMock(return_value=set())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            with (
                patch("edgelite.app._app_state", _share_namespace()),
                patch("edgelite.storage.sqlite_repo.ResourceShareRepo", return_value=mock_repo),
            ):
                resp = await c.get("/api/v1/rules")
        assert resp.status_code == 200, resp.text
        assert resp.json()["total"] == 1
        assert svc.list_rules.call_args.kwargs.get("created_by") == "test-admin"

    async def test_non_admin_with_shared_ids_merges(self, make_env):
        app, svc, _ = make_env(role="operator")
        owned = _rule_dict("r1", created_by="test-admin")
        svc.list_rules = AsyncMock(return_value=([owned], 1))
        shared_rule = _rule_dict("r2", created_by="other")
        svc.list_rules_by_ids = AsyncMock(return_value=[shared_rule])
        mock_repo = MagicMock()
        mock_repo.get_shared_resource_ids = AsyncMock(return_value={"r2"})
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            with (
                patch("edgelite.app._app_state", _share_namespace()),
                patch("edgelite.storage.sqlite_repo.ResourceShareRepo", return_value=mock_repo),
            ):
                resp = await c.get("/api/v1/rules")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] == 2
        ids = {r["rule_id"] for r in body["data"]}
        assert ids == {"r1", "r2"}


# ───────────────────────── create_rule ─────────────────────────


class TestCreateRule:
    async def test_create_success(self, admin_env):
        c, svc, audit = admin_env
        svc.create_rule = AsyncMock(return_value=_rule_dict("r-new"))
        resp = await c.post("/api/v1/rules", json=_rule_create_body())
        assert resp.status_code == 201, resp.text
        assert resp.json()["data"]["rule_id"] == "r-new"
        svc.create_rule.assert_awaited_once()
        assert svc.create_rule.call_args.kwargs.get("created_by") == "test-admin"
        audit.log.assert_awaited_once()

    async def test_create_with_request_ip_and_ua(self, admin_env):
        c, svc, audit = admin_env
        svc.create_rule = AsyncMock(return_value=_rule_dict())
        resp = await c.post(
            "/api/v1/rules", json=_rule_create_body(), headers={"User-Agent": "pytest-agent"}
        )
        assert resp.status_code == 201
        kwargs = audit.log.call_args.kwargs
        assert kwargs.get("ip_address") == "127.0.0.1"
        assert kwargs.get("user_agent") == "pytest-agent"

    async def test_create_audit_failure_still_succeeds(self, admin_env):
        c, svc, audit = admin_env
        svc.create_rule = AsyncMock(return_value=_rule_dict())
        audit.log = AsyncMock(side_effect=RuntimeError("audit down"))
        resp = await c.post("/api/v1/rules", json=_rule_create_body())
        assert resp.status_code == 201

    async def test_create_value_error_returns_422(self, admin_env):
        c, svc, _ = admin_env
        svc.create_rule = AsyncMock(side_effect=ValueError("bad condition"))
        resp = await c.post("/api/v1/rules", json=_rule_create_body())
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail["error_code"] == "ERR_RULE_CONDITION_INVALID"

    async def test_create_exception_returns_500(self, admin_env):
        c, svc, _ = admin_env
        svc.create_rule = AsyncMock(side_effect=RuntimeError("db down"))
        resp = await c.post("/api/v1/rules", json=_rule_create_body())
        assert resp.status_code == 500
        assert resp.json()["detail"] == "ERR_RULE_CREATE_FAILED"

    async def test_create_invalid_body_returns_422(self, admin_env):
        c, _, _ = admin_env
        resp = await c.post("/api/v1/rules", json={"name": "x", "conditions": []})
        assert resp.status_code == 422


# ───────────────────────── test_rule_definition ─────────────────────────


class TestTestRuleDefinition:
    async def test_definition_success(self, admin_env):
        c, _, _ = admin_env
        resp = await c.post("/api/v1/rules/test", json=_rule_create_body())
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["evaluable"] is True
        assert data["rule_name"] == "test-rule"
        assert len(data["conditions"]) == 1

    async def test_definition_invalid_body_returns_422(self, admin_env):
        c, _, _ = admin_env
        resp = await c.post("/api/v1/rules/test", json={"name": "x", "conditions": []})
        assert resp.status_code == 422


# ───────────────────────── batch_delete_rules ─────────────────────────


class TestBatchDelete:
    async def test_batch_delete_mixed(self, admin_env):
        c, svc, _ = admin_env
        svc.list_rules_by_ids = AsyncMock(
            return_value=[_rule_dict("r1"), _rule_dict("r2")]
        )
        svc.delete_rule = AsyncMock(return_value=True)
        resp = await c.post("/api/v1/rules/batch/delete", json={"rule_ids": ["r1", "r2", "r3"]})
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["success_count"] == 2
        assert "r3" in data["failed"]

    async def test_batch_delete_not_found_failed(self, admin_env):
        c, svc, _ = admin_env
        svc.list_rules_by_ids = AsyncMock(return_value=[_rule_dict("r1")])
        svc.delete_rule = AsyncMock(return_value=False)
        resp = await c.post("/api/v1/rules/batch/delete", json={"rule_ids": ["r1"]})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["success_count"] == 0
        assert data["failed"]["r1"] == "ERR_RULE_NOT_FOUND"

    async def test_batch_delete_access_denied(self, admin_env):
        from fastapi import HTTPException

        c, svc, _ = admin_env
        # admin 拥有 RULE_DELETE 权限；patch _check_rule_access 模拟资源所有权拒绝
        svc.list_rules_by_ids = AsyncMock(return_value=[_rule_dict("r1", created_by="other")])
        svc.delete_rule = AsyncMock(return_value=True)
        with patch(
            "edgelite.api.rules._check_rule_access",
            new=AsyncMock(side_effect=HTTPException(403, "denied")),
        ):
            resp = await c.post("/api/v1/rules/batch/delete", json={"rule_ids": ["r1"]})
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["success_count"] == 0
        assert "r1" in data["failed"]

    async def test_batch_delete_exception_returns_500(self, admin_env):
        c, svc, _ = admin_env
        svc.list_rules_by_ids = AsyncMock(side_effect=RuntimeError("boom"))
        resp = await c.post("/api/v1/rules/batch/delete", json={"rule_ids": ["r1"]})
        assert resp.status_code == 500
        assert resp.json()["detail"] == "ERR_RULE_DELETE_FAILED"

    async def test_batch_delete_empty_ids_returns_422(self, admin_env):
        c, _, _ = admin_env
        resp = await c.post("/api/v1/rules/batch/delete", json={"rule_ids": []})
        assert resp.status_code == 422

    async def test_batch_delete_audit_failure_still_succeeds(self, admin_env):
        c, svc, audit = admin_env
        svc.list_rules_by_ids = AsyncMock(return_value=[_rule_dict("r1")])
        svc.delete_rule = AsyncMock(return_value=True)
        audit.log = AsyncMock(side_effect=RuntimeError("audit down"))
        resp = await c.post("/api/v1/rules/batch/delete", json={"rule_ids": ["r1"]})
        assert resp.status_code == 200
        assert resp.json()["data"]["success_count"] == 1


# ───────────────────────── batch_enable_rules ─────────────────────────


class TestBatchEnable:
    async def test_batch_enable_success(self, admin_env):
        c, svc, _ = admin_env
        svc.list_rules_by_ids = AsyncMock(return_value=[_rule_dict("r1"), _rule_dict("r2")])
        svc.enable_rule = AsyncMock(return_value=_rule_dict())
        resp = await c.post("/api/v1/rules/batch/enable", json={"rule_ids": ["r1", "r2", "r3"]})
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["success_count"] == 2
        assert "r3" in data["failed"]

    async def test_batch_enable_returns_none_failed(self, admin_env):
        c, svc, _ = admin_env
        svc.list_rules_by_ids = AsyncMock(return_value=[_rule_dict("r1")])
        svc.enable_rule = AsyncMock(return_value=None)
        resp = await c.post("/api/v1/rules/batch/enable", json={"rule_ids": ["r1"]})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["success_count"] == 0
        assert data["failed"]["r1"] == "ERR_RULE_NOT_FOUND"

    async def test_batch_enable_exception_returns_500(self, admin_env):
        c, svc, _ = admin_env
        svc.list_rules_by_ids = AsyncMock(side_effect=RuntimeError("boom"))
        resp = await c.post("/api/v1/rules/batch/enable", json={"rule_ids": ["r1"]})
        assert resp.status_code == 500
        assert resp.json()["detail"] == "ERR_RULE_ENABLE_FAILED"


# ───────────────────────── batch_disable_rules ─────────────────────────


class TestBatchDisable:
    async def test_batch_disable_success(self, admin_env):
        c, svc, _ = admin_env
        svc.list_rules_by_ids = AsyncMock(return_value=[_rule_dict("r1")])
        svc.disable_rule = AsyncMock(return_value=_rule_dict())
        resp = await c.post("/api/v1/rules/batch/disable", json={"rule_ids": ["r1", "r2"]})
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["success_count"] == 1
        assert "r2" in data["failed"]

    async def test_batch_disable_returns_none_failed(self, admin_env):
        c, svc, _ = admin_env
        svc.list_rules_by_ids = AsyncMock(return_value=[_rule_dict("r1")])
        svc.disable_rule = AsyncMock(return_value=None)
        resp = await c.post("/api/v1/rules/batch/disable", json={"rule_ids": ["r1"]})
        assert resp.status_code == 200
        assert resp.json()["data"]["failed"]["r1"] == "ERR_RULE_NOT_FOUND"

    async def test_batch_disable_exception_returns_500(self, admin_env):
        c, svc, _ = admin_env
        svc.list_rules_by_ids = AsyncMock(side_effect=RuntimeError("boom"))
        resp = await c.post("/api/v1/rules/batch/disable", json={"rule_ids": ["r1"]})
        assert resp.status_code == 500
        assert resp.json()["detail"] == "ERR_RULE_DISABLE_FAILED"


# ───────────────────────── get_rule ─────────────────────────


class TestGetRule:
    async def test_get_success(self, admin_env):
        c, svc, _ = admin_env
        svc.get_rule = AsyncMock(return_value=_rule_dict("r1"))
        resp = await c.get("/api/v1/rules/r1")
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["rule_id"] == "r1"

    async def test_get_not_found(self, admin_env):
        c, svc, _ = admin_env
        svc.get_rule = AsyncMock(return_value=None)
        resp = await c.get("/api/v1/rules/r1")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "ERR_RULE_NOT_FOUND"

    async def test_get_exception_returns_500(self, admin_env):
        c, svc, _ = admin_env
        svc.get_rule = AsyncMock(side_effect=RuntimeError("boom"))
        resp = await c.get("/api/v1/rules/r1")
        assert resp.status_code == 500
        assert resp.json()["detail"] == "ERR_RULE_GET_FAILED"

    async def test_get_non_admin_owner_allowed(self, make_env):
        app, svc, _ = make_env(role="operator")
        svc.get_rule = AsyncMock(return_value=_rule_dict("r1", created_by="test-admin"))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/api/v1/rules/r1")
        assert resp.status_code == 200, resp.text

    async def test_get_non_admin_denied_403(self, make_env):
        app, svc, _ = make_env(role="operator")
        svc.get_rule = AsyncMock(return_value=_rule_dict("r1", created_by="other"))
        mock_repo = MagicMock()
        mock_repo.check_user_has_access = AsyncMock(return_value=False)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            with (
                patch("edgelite.app._app_state", _share_namespace()),
                patch("edgelite.storage.sqlite_repo.ResourceShareRepo", return_value=mock_repo),
            ):
                resp = await c.get("/api/v1/rules/r1")
        assert resp.status_code == 403


# ───────────────────────── update_rule ─────────────────────────


class TestUpdateRule:
    async def test_update_success_with_version_injection(self, admin_env):
        c, svc, audit = admin_env
        before = _rule_dict("r1", version=3)
        svc.get_rule = AsyncMock(return_value=before)
        updated = _rule_dict("r1", version=4)
        svc.update_rule = AsyncMock(return_value=updated)
        resp = await c.put("/api/v1/rules/r1", json={"name": "updated"})
        assert resp.status_code == 200, resp.text
        data_arg = svc.update_rule.call_args.args[1]
        assert data_arg["_version"] == 3
        audit.log.assert_awaited_once()

    async def test_update_before_not_found(self, admin_env):
        c, svc, _ = admin_env
        svc.get_rule = AsyncMock(return_value=None)
        resp = await c.put("/api/v1/rules/r1", json={"name": "updated"})
        assert resp.status_code == 404
        assert resp.json()["detail"] == "ERR_RULE_NOT_FOUND"

    async def test_update_returns_none_404(self, admin_env):
        c, svc, _ = admin_env
        svc.get_rule = AsyncMock(return_value=_rule_dict("r1"))
        svc.update_rule = AsyncMock(return_value=None)
        resp = await c.put("/api/v1/rules/r1", json={"name": "updated"})
        assert resp.status_code == 404

    async def test_update_stale_data_returns_409(self, admin_env):
        c, svc, _ = admin_env
        svc.get_rule = AsyncMock(return_value=_rule_dict("r1"))
        svc.update_rule = AsyncMock(side_effect=StaleDataError("version mismatch"))
        resp = await c.put("/api/v1/rules/r1", json={"name": "updated"})
        assert resp.status_code == 409
        assert resp.json()["detail"] == "ERR_REPO_STALE_DATA_ERROR"

    async def test_update_exception_returns_500(self, admin_env):
        c, svc, _ = admin_env
        svc.get_rule = AsyncMock(return_value=_rule_dict("r1"))
        svc.update_rule = AsyncMock(side_effect=RuntimeError("boom"))
        resp = await c.put("/api/v1/rules/r1", json={"name": "updated"})
        assert resp.status_code == 500
        assert resp.json()["detail"] == "ERR_RULE_UPDATE_FAILED"

    async def test_update_non_admin_denied_403(self, make_env):
        app, svc, _ = make_env(role="viewer")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.put("/api/v1/rules/r1", json={"name": "updated"})
        assert resp.status_code == 403

    async def test_update_audit_failure_still_succeeds(self, admin_env):
        c, svc, audit = admin_env
        svc.get_rule = AsyncMock(return_value=_rule_dict("r1"))
        svc.update_rule = AsyncMock(return_value=_rule_dict("r1"))
        audit.log = AsyncMock(side_effect=RuntimeError("audit down"))
        resp = await c.put("/api/v1/rules/r1", json={"name": "updated"})
        assert resp.status_code == 200


# ───────────────────────── delete_rule ─────────────────────────


class TestDeleteRule:
    async def test_delete_success(self, admin_env):
        c, svc, audit = admin_env
        svc.get_rule = AsyncMock(return_value=_rule_dict("r1"))
        svc.delete_rule = AsyncMock(return_value=True)
        resp = await c.delete("/api/v1/rules/r1")
        assert resp.status_code == 200, resp.text
        assert audit.log.await_count >= 2

    async def test_delete_not_found(self, admin_env):
        c, svc, _ = admin_env
        svc.get_rule = AsyncMock(return_value=None)
        resp = await c.delete("/api/v1/rules/r1")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "ERR_RULE_NOT_FOUND"

    async def test_delete_returns_false_404_with_failed_audit(self, admin_env):
        c, svc, audit = admin_env
        svc.get_rule = AsyncMock(return_value=_rule_dict("r1"))
        svc.delete_rule = AsyncMock(return_value=False)
        resp = await c.delete("/api/v1/rules/r1")
        assert resp.status_code == 404
        statuses = [kw.kwargs.get("status") for kw in audit.log.await_args_list]
        assert "failed" in statuses

    async def test_delete_audit_pending_failure_returns_500(self, admin_env):
        c, svc, audit = admin_env
        svc.get_rule = AsyncMock(return_value=_rule_dict("r1"))
        audit.log = AsyncMock(side_effect=RuntimeError("audit down"))
        resp = await c.delete("/api/v1/rules/r1")
        assert resp.status_code == 500
        assert resp.json()["detail"] == "ERR_RULE_DELETE_FAILED"

    async def test_delete_exception_returns_500(self, admin_env):
        c, svc, _ = admin_env
        svc.get_rule = AsyncMock(side_effect=RuntimeError("boom"))
        resp = await c.delete("/api/v1/rules/r1")
        assert resp.status_code == 500
        assert resp.json()["detail"] == "ERR_RULE_DELETE_FAILED"

    async def test_delete_non_admin_denied_403(self, make_env):
        app, svc, _ = make_env(role="viewer")
        svc.get_rule = AsyncMock(return_value=_rule_dict("r1", created_by="other"))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.delete("/api/v1/rules/r1")
        assert resp.status_code == 403

    async def test_delete_success_audit_failed_logged(self, admin_env):
        c, svc, audit = admin_env
        svc.get_rule = AsyncMock(return_value=_rule_dict("r1"))
        svc.delete_rule = AsyncMock(return_value=True)
        audit.log = AsyncMock(side_effect=[None, RuntimeError("audit down"), RuntimeError("audit down")])
        resp = await c.delete("/api/v1/rules/r1")
        assert resp.status_code == 200


# ───────────────────────── enable_rule ─────────────────────────


class TestEnableRule:
    async def test_enable_success(self, admin_env):
        c, svc, audit = admin_env
        svc.get_rule = AsyncMock(return_value=_rule_dict("r1"))
        svc.enable_rule = AsyncMock(return_value=_rule_dict("r1"))
        resp = await c.post("/api/v1/rules/r1/enable")
        assert resp.status_code == 200, resp.text
        audit.log.assert_awaited_once()

    async def test_enable_not_found(self, admin_env):
        c, svc, _ = admin_env
        svc.get_rule = AsyncMock(return_value=None)
        resp = await c.post("/api/v1/rules/r1/enable")
        assert resp.status_code == 404

    async def test_enable_returns_none_404(self, admin_env):
        c, svc, _ = admin_env
        svc.get_rule = AsyncMock(return_value=_rule_dict("r1"))
        svc.enable_rule = AsyncMock(return_value=None)
        resp = await c.post("/api/v1/rules/r1/enable")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "ERR_RULE_NOT_FOUND"

    async def test_enable_exception_returns_500(self, admin_env):
        c, svc, _ = admin_env
        svc.get_rule = AsyncMock(return_value=_rule_dict("r1"))
        svc.enable_rule = AsyncMock(side_effect=RuntimeError("boom"))
        resp = await c.post("/api/v1/rules/r1/enable")
        assert resp.status_code == 500
        assert resp.json()["detail"] == "ERR_RULE_ENABLE_FAILED"

    async def test_enable_audit_failure_still_succeeds(self, admin_env):
        c, svc, audit = admin_env
        svc.get_rule = AsyncMock(return_value=_rule_dict("r1"))
        svc.enable_rule = AsyncMock(return_value=_rule_dict("r1"))
        audit.log = AsyncMock(side_effect=RuntimeError("audit down"))
        resp = await c.post("/api/v1/rules/r1/enable")
        assert resp.status_code == 200


# ───────────────────────── disable_rule ─────────────────────────


class TestDisableRule:
    async def test_disable_success(self, admin_env):
        c, svc, audit = admin_env
        svc.get_rule = AsyncMock(return_value=_rule_dict("r1"))
        svc.disable_rule = AsyncMock(return_value=_rule_dict("r1"))
        resp = await c.post("/api/v1/rules/r1/disable")
        assert resp.status_code == 200, resp.text
        audit.log.assert_awaited_once()

    async def test_disable_not_found(self, admin_env):
        c, svc, _ = admin_env
        svc.get_rule = AsyncMock(return_value=None)
        resp = await c.post("/api/v1/rules/r1/disable")
        assert resp.status_code == 404

    async def test_disable_returns_none_404(self, admin_env):
        c, svc, _ = admin_env
        svc.get_rule = AsyncMock(return_value=_rule_dict("r1"))
        svc.disable_rule = AsyncMock(return_value=None)
        resp = await c.post("/api/v1/rules/r1/disable")
        assert resp.status_code == 404

    async def test_disable_exception_returns_500(self, admin_env):
        c, svc, _ = admin_env
        svc.get_rule = AsyncMock(return_value=_rule_dict("r1"))
        svc.disable_rule = AsyncMock(side_effect=RuntimeError("boom"))
        resp = await c.post("/api/v1/rules/r1/disable")
        assert resp.status_code == 500
        assert resp.json()["detail"] == "ERR_RULE_DISABLE_FAILED"

    async def test_disable_non_admin_owner_allowed(self, make_env):
        app, svc, _ = make_env(role="operator")
        svc.get_rule = AsyncMock(return_value=_rule_dict("r1", created_by="test-admin"))
        svc.disable_rule = AsyncMock(return_value=_rule_dict("r1"))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/api/v1/rules/r1/disable")
        assert resp.status_code == 200, resp.text


# ───────────────────────── test_rule ─────────────────────────


class TestTestRule:
    async def test_test_rule_success(self, admin_env):
        c, svc, _ = admin_env
        svc.get_rule = AsyncMock(return_value=_rule_dict("r1"))
        svc.test_rule = AsyncMock(return_value={"triggered": True, "matched": ["temp"]})
        resp = await c.post("/api/v1/rules/r1/test", json={"point_values": {"temp": 60.0}})
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["triggered"] is True

    async def test_test_rule_not_found(self, admin_env):
        c, svc, _ = admin_env
        svc.get_rule = AsyncMock(return_value=None)
        resp = await c.post("/api/v1/rules/r1/test", json={"point_values": {"temp": 60.0}})
        assert resp.status_code == 404

    async def test_test_rule_value_error_returns_422(self, admin_env):
        c, svc, _ = admin_env
        svc.get_rule = AsyncMock(return_value=_rule_dict("r1"))
        svc.test_rule = AsyncMock(side_effect=ValueError("invalid point"))
        resp = await c.post("/api/v1/rules/r1/test", json={"point_values": {"temp": 60.0}})
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail["error_code"] == "ERR_RULE_CONDITION_INVALID"

    async def test_test_rule_exception_returns_500(self, admin_env):
        c, svc, _ = admin_env
        svc.get_rule = AsyncMock(return_value=_rule_dict("r1"))
        svc.test_rule = AsyncMock(side_effect=RuntimeError("boom"))
        resp = await c.post("/api/v1/rules/r1/test", json={"point_values": {"temp": 60.0}})
        assert resp.status_code == 500
        assert resp.json()["detail"] == "ERR_RULE_TEST_FAILED"

    async def test_test_rule_non_admin_denied_403(self, make_env):
        app, svc, _ = make_env(role="operator")
        svc.get_rule = AsyncMock(return_value=_rule_dict("r1", created_by="other"))
        mock_repo = MagicMock()
        mock_repo.check_user_has_access = AsyncMock(return_value=False)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            with (
                patch("edgelite.app._app_state", _share_namespace()),
                patch("edgelite.storage.sqlite_repo.ResourceShareRepo", return_value=mock_repo),
            ):
                resp = await c.post(
                    "/api/v1/rules/r1/test", json={"point_values": {"temp": 60.0}}
                )
        assert resp.status_code == 403


# ───────────────────────── BatchRuleIds 模型 ─────────────────────────


class TestBatchRuleIdsModel:
    def test_valid_ids(self):
        m = BatchRuleIds(rule_ids=["a", "b"])
        assert m.rule_ids == ["a", "b"]

    def test_empty_rejected(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            BatchRuleIds(rule_ids=[])

    def test_too_many_rejected(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            BatchRuleIds(rule_ids=[str(i) for i in range(101)])
