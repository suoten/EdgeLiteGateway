"""OTA API 安全回归测试（审计 P1-2 / P1-3 修复）

覆盖 src/edgelite/api/ota.py：
- P1-2: 全部端点必须以 Depends(require_permission(...)) 注入权限检查——
  原写法 ``user: CurrentUser = require_permission(...)`` 未包 Depends()，
  FastAPI 按注解注入 get_current_user、把权限检查函数当普通默认值忽略，
  viewer 可调用 POST /ota/apply、/ota/rollback（RBAC 静默失效）。
- P1-3: OTA 完整性链——version 字符白名单、download_url 强制 HTTPS、
  apply 必须携带 expected_sha256 传给 ota_manager。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, "src")

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from edgelite.api.deps import get_current_user, get_ota_manager
from edgelite.api.error_codes import CommonErrors, OtaErrors
from edgelite.api.ota import _validate_download_url, _validate_version, router

_ADMIN = {"user_id": "u1", "username": "admin", "role": "admin"}
_VIEWER = {"user_id": "u2", "username": "viewer", "role": "viewer"}


@pytest.fixture
def app():
    application = FastAPI()
    application.include_router(router)
    return application


@pytest.fixture
async def admin_client(app):
    app.dependency_overrides[get_current_user] = lambda: dict(_ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _mock_mgr(check_result=None):
    mgr = SimpleNamespace(
        check_update=AsyncMock(return_value=check_result),
        download_update=AsyncMock(return_value=Path("data/ota/update-1.2.4.zip")),
        apply_update=AsyncMock(return_value=True),
        rollback=AsyncMock(return_value={"restored": True}),
        list_backups=AsyncMock(return_value=[]),
        cancel=AsyncMock(return_value=True),
    )
    return mgr


# ── P1-2: RBAC 权限检查必须真实生效 ──


class TestOtaRbac:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("post", "/api/v1/ota/apply"),
            ("post", "/api/v1/ota/rollback"),
            ("post", "/api/v1/ota/cancel"),
        ],
    )
    async def test_viewer_cannot_call_manage_endpoints(self, app, method, path):
        """P1-2 回归: viewer（无 SYSTEM_MANAGE）调用管理端点必须 403。

        修复前：权限检查函数未包 Depends 被 FastAPI 忽略，viewer 请求会穿透到业务逻辑。
        """
        app.dependency_overrides[get_current_user] = lambda: dict(_VIEWER)
        mgr = _mock_mgr()
        app.dependency_overrides[get_ota_manager] = lambda: mgr
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.request(method, path)
        assert resp.status_code == 403
        # 关键断言：请求必须被权限检查拦截，不能触达 OTA manager
        mgr.apply_update.assert_not_awaited()
        mgr.rollback.assert_not_awaited()

    async def test_viewer_can_call_read_endpoints(self, app):
        """viewer 拥有 SYSTEM_READ，读端点应正常放行（权限收紧不误伤）。"""
        app.dependency_overrides[get_current_user] = lambda: dict(_VIEWER)
        app.dependency_overrides[get_ota_manager] = lambda: None
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            assert (await c.get("/api/v1/ota/check")).status_code == 200
            assert (await c.get("/api/v1/ota/status")).status_code == 200
            assert (await c.get("/api/v1/ota/backups")).status_code == 200

    def test_no_bare_require_permission_without_depends(self):
        """P1-2 结构断言: ota.py 不得再出现未包 Depends 的 require_permission 裸写法。"""
        import inspect

        from edgelite.api import ota as ota_module

        src = inspect.getsource(ota_module)
        assert "user: CurrentUser = require_permission" not in src
        assert src.count("Depends(require_permission(") == 6


# ── P1-3: 完整性链校验 ──


class TestVersionValidation:
    def test_valid_versions(self):
        assert _validate_version("1.2.4") == "1.2.4"
        assert _validate_version("v2.0.0-rc.1_build-9") == "v2.0.0-rc.1_build-9"

    @pytest.mark.parametrize(
        "version",
        [
            "",
            "../evil",
            "1.2.4/../../etc",
            "a b",
            "版本1",
            "1.2.4;rm",
            None,
        ],
    )
    def test_invalid_versions_rejected(self, version):
        from fastapi import HTTPException

        if version is None:
            with pytest.raises(HTTPException) as e:
                _validate_version(version)
            assert e.value.status_code == 400
        else:
            with pytest.raises(HTTPException) as e:
                _validate_version(version)
            assert e.value.status_code == 400
            assert e.value.detail == OtaErrors.INVALID_VERSION


class TestDownloadUrlValidation:
    def test_https_allowed(self):
        assert _validate_download_url("https://ota.example.com/pkg.zip").startswith("https://")

    def test_http_rejected_by_default(self, monkeypatch):
        """P1-3 回归: http:// 下载地址默认必须 400（防中间人替换固件包）。"""
        monkeypatch.delenv("EDGELITE_ALLOW_INSECURE_TLS", raising=False)
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as e:
            _validate_download_url("http://ota.example.com/pkg.zip")
        assert e.value.status_code == 400
        assert e.value.detail == CommonErrors.VALIDATION_FAILED

    def test_http_allowed_with_explicit_env(self, monkeypatch):
        """仅 EDGELITE_ALLOW_INSECURE_TLS=1 时显式放行 http（受控测试环境）。"""
        monkeypatch.setenv("EDGELITE_ALLOW_INSECURE_TLS", "1")
        assert _validate_download_url("http://ota.example.com/pkg.zip").startswith("http://")

    def test_ftp_rejected_even_with_env(self, monkeypatch):
        monkeypatch.setenv("EDGELITE_ALLOW_INSECURE_TLS", "1")
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as e:
            _validate_download_url("ftp://ota.example.com/pkg.zip")
        assert e.value.status_code == 400


class TestApplyIntegrityChain:
    async def test_apply_passes_expected_sha256(self, app, monkeypatch):
        """P1-3 回归: apply 必须把 check_update 结果中的 sha256 传给 apply_update。"""
        monkeypatch.delenv("EDGELITE_ALLOW_INSECURE_TLS", raising=False)
        sha = "a" * 64
        mgr = _mock_mgr({"version": "1.2.4", "download_url": "https://ota.example.com/pkg.zip", "sha256": sha})
        app.dependency_overrides[get_current_user] = lambda: dict(_ADMIN)
        app.dependency_overrides[get_ota_manager] = lambda: mgr
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/api/v1/ota/apply")
        assert resp.status_code == 200
        assert resp.json()["data"]["success"] is True
        mgr.apply_update.assert_awaited_once()
        args, kwargs = mgr.apply_update.call_args
        assert kwargs.get("expected_sha256") == sha

    async def test_apply_rejects_missing_sha256(self, app, monkeypatch):
        """P1-3 回归: OTA 服务器未提供 sha256 时拒绝应用（fail-closed，防退化到无校验路径）。"""
        monkeypatch.delenv("EDGELITE_ALLOW_INSECURE_TLS", raising=False)
        mgr = _mock_mgr({"version": "1.2.4", "download_url": "https://ota.example.com/pkg.zip"})
        app.dependency_overrides[get_current_user] = lambda: dict(_ADMIN)
        app.dependency_overrides[get_ota_manager] = lambda: mgr
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/api/v1/ota/apply")
        assert resp.status_code == 400
        assert resp.json()["detail"] == CommonErrors.VALIDATION_FAILED
        mgr.download_update.assert_not_awaited()
        mgr.apply_update.assert_not_awaited()

    async def test_apply_rejects_path_traversal_version(self, app, monkeypatch):
        """P1-3 回归: version 含路径遍历字符必须 400，不触发下载。"""
        mgr = _mock_mgr(
            {"version": "../../evil", "download_url": "https://ota.example.com/pkg.zip", "sha256": "a" * 64}
        )
        app.dependency_overrides[get_current_user] = lambda: dict(_ADMIN)
        app.dependency_overrides[get_ota_manager] = lambda: mgr
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/api/v1/ota/apply")
        assert resp.status_code == 400
        assert resp.json()["detail"] == OtaErrors.INVALID_VERSION
        mgr.download_update.assert_not_awaited()
        mgr.apply_update.assert_not_awaited()

    async def test_apply_rejects_http_download_url(self, app, monkeypatch):
        """P1-3 回归: apply 对 http:// 下载地址必须 400，不触发下载。"""
        monkeypatch.delenv("EDGELITE_ALLOW_INSECURE_TLS", raising=False)
        mgr = _mock_mgr({"version": "1.2.4", "download_url": "http://ota.example.com/pkg.zip", "sha256": "a" * 64})
        app.dependency_overrides[get_current_user] = lambda: dict(_ADMIN)
        app.dependency_overrides[get_ota_manager] = lambda: mgr
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/api/v1/ota/apply")
        assert resp.status_code == 400
        mgr.download_update.assert_not_awaited()


class TestRollbackVersionValidation:
    async def test_rollback_rejects_traversal_version(self, app):
        """rollback 的 version 同样拼路径，必须做白名单校验。"""
        mgr = _mock_mgr()
        app.dependency_overrides[get_current_user] = lambda: dict(_ADMIN)
        app.dependency_overrides[get_ota_manager] = lambda: mgr
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/api/v1/ota/rollback", params={"version": "../backups"})
        assert resp.status_code == 400
        assert resp.json()["detail"] == OtaErrors.INVALID_VERSION
        mgr.rollback.assert_not_awaited()

    async def test_rollback_allows_valid_version(self, app):
        mgr = _mock_mgr()
        app.dependency_overrides[get_current_user] = lambda: dict(_ADMIN)
        app.dependency_overrides[get_ota_manager] = lambda: mgr
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/api/v1/ota/rollback", params={"version": "1.2.3"})
        assert resp.status_code == 200
        mgr.rollback.assert_awaited_once_with("1.2.3")
