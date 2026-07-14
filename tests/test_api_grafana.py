"""Grafana 集成 API 路由测试

覆盖 src/edgelite/api/grafana.py：
- _is_grafana_url_safe: SSRF 校验 (内网/环回/链路本地/保留/组播/域名解析)
- _get_grafana_config: 配置读取（含异常分支）
- GET /api/v1/grafana/config: 获取 Grafana 服务信息与配置
- GET /api/v1/grafana/dashboards: 列出仪表板（含 SSRF/导入错误/HTTP 错误码分支）
- GET /api/v1/grafana/embed-url: 获取嵌入 URL (含 uid 校验/SSRF 校验)
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, "src")

from httpx import ASGITransport, AsyncClient

from edgelite.api.grafana import _get_grafana_config, _is_grafana_url_safe, router
from edgelite.api.error_codes import GrafanaErrors
from edgelite.services.service_manager import DependencyInfo, ServiceInfo, ServiceState


# ── 辅助函数 ──────────────────────────────────────────────────────────────────


def _make_service_info(
    state: ServiceState = ServiceState.RUNNING,
    dependencies: list[DependencyInfo] | None = None,
    current_config: dict | None = None,
) -> ServiceInfo:
    """构造 ServiceInfo 对象用于 mock get_service_info 返回值"""
    return ServiceInfo(
        name="grafana",
        display_name="Grafana",
        description="Grafana dashboard service",
        config_section="grafana",
        state=state,
        dependencies=dependencies or [DependencyInfo(package="grafana", installed=True, version="10.0.0")],
        current_config=current_config if current_config is not None else {"foo": "bar"},
    )


def _make_grafana_config(
    enabled: bool = True,
    url: str = "https://grafana.example.com",
    api_key: str = "test-api-key",
    datasource: str = "InfluxDB",
) -> SimpleNamespace:
    return SimpleNamespace(enabled=enabled, url=url, api_key=api_key, datasource=datasource)


@pytest.fixture
async def client():
    """构建带认证覆盖的测试客户端，默认无服务注入"""
    from conftest import make_app

    app = make_app(router, role="admin", services={})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c, app


# ── _is_grafana_url_safe ──────────────────────────────────────────────────────


class TestIsGrafanaUrlSafe:
    def test_empty_url_returns_false(self):
        assert _is_grafana_url_safe("") is False

    def test_non_http_scheme_returns_false(self):
        assert _is_grafana_url_safe("ftp://example.com") is False
        assert _is_grafana_url_safe("file:///etc/passwd") is False
        assert _is_grafana_url_safe("javascript:alert(1)") is False

    def test_no_scheme_returns_false(self):
        assert _is_grafana_url_safe("example.com") is False

    def test_missing_hostname_returns_false(self):
        # scheme 合法但无 hostname
        assert _is_grafana_url_safe("http://") is False

    def test_public_ip_returns_true(self):
        # 8.8.8.8 是公共 DNS，不属于内网/环回/保留
        assert _is_grafana_url_safe("http://8.8.8.8:3000") is True

    def test_loopback_ip_returns_false(self):
        assert _is_grafana_url_safe("http://127.0.0.1:3000") is False

    def test_private_ip_returns_false(self):
        assert _is_grafana_url_safe("http://192.168.1.1:3000") is False
        assert _is_grafana_url_safe("http://10.0.0.1:3000") is False
        assert _is_grafana_url_safe("http://172.16.0.1:3000") is False

    def test_link_local_ip_returns_false(self):
        assert _is_grafana_url_safe("http://169.254.1.1:3000") is False

    def test_multicast_ip_returns_false(self):
        assert _is_grafana_url_safe("http://224.0.0.1:3000") is False

    def test_reserved_ip_returns_false(self):
        # 0.0.0.0 属于 reserved
        assert _is_grafana_url_safe("http://0.0.0.0:3000") is False

    def test_domain_resolves_to_public_returns_true(self):
        # 通过 mock getaddrinfo 返回公共 IP
        with patch("edgelite.api.grafana.socket.getaddrinfo") as mock_getaddr:
            mock_getaddr.return_value = [(0, 0, 0, "", ("8.8.8.8", 0))]
            assert _is_grafana_url_safe("https://grafana.example.com") is True

    def test_domain_resolves_to_private_returns_false(self):
        with patch("edgelite.api.grafana.socket.getaddrinfo") as mock_getaddr:
            mock_getaddr.return_value = [(0, 0, 0, "", ("192.168.1.1", 0))]
            assert _is_grafana_url_safe("https://grafana.example.com") is False

    def test_domain_resolution_fails_returns_false(self):
        import socket

        with patch("edgelite.api.grafana.socket.getaddrinfo") as mock_getaddr:
            mock_getaddr.side_effect = socket.gaierror("DNS failed")
            assert _is_grafana_url_safe("https://nonexistent.invalid") is False

    def test_domain_resolution_returns_empty_returns_false(self):
        with patch("edgelite.api.grafana.socket.getaddrinfo") as mock_getaddr:
            mock_getaddr.return_value = []
            assert _is_grafana_url_safe("https://grafana.example.com") is False

    def test_domain_with_unparseable_ip_in_addrinfo_skipped(self):
        # sockaddr 中的 IP 无法解析时应跳过，最终全部跳过返回 True（无危险 IP）
        with patch("edgelite.api.grafana.socket.getaddrinfo") as mock_getaddr:
            mock_getaddr.return_value = [(0, 0, 0, "", ("not-an-ip", 0))]
            # 无危险 IP 命中，返回 True
            assert _is_grafana_url_safe("https://grafana.example.com") is True

    def test_urlparse_exception_returns_false(self):
        with patch("edgelite.api.grafana.urlparse", side_effect=ValueError("bad url")):
            assert _is_grafana_url_safe("http://example.com") is False


# ── _get_grafana_config ───────────────────────────────────────────────────────


class TestGetGrafanaConfig:
    def test_returns_config_when_available(self):
        cfg = _make_grafana_config()
        with patch("edgelite.config.get_config", return_value=SimpleNamespace(grafana=cfg)):
            result = _get_grafana_config()
            assert result is cfg

    def test_returns_none_when_config_has_no_grafana_attr(self):
        with patch("edgelite.config.get_config", return_value=SimpleNamespace()):
            assert _get_grafana_config() is None

    def test_returns_none_when_get_config_raises(self):
        with patch("edgelite.config.get_config", side_effect=RuntimeError("no config")):
            assert _get_grafana_config() is None


# ── GET /api/v1/grafana/config ────────────────────────────────────────────────


class TestGetGrafanaConfigEndpoint:
    async def test_returns_404_when_service_not_found(self, client):
        """服务管理器返回 None 时应抛 404"""
        c, _ = client
        with patch("edgelite.services.service_manager.get_service_manager") as mock_mgr:
            mock_mgr.return_value.get_service_info.return_value = None
            resp = await c.get("/api/v1/grafana/config")
        assert resp.status_code == 404

    async def test_returns_config_with_defaults_when_grafana_config_missing(self, client):
        """grafana_config 为 None 时使用默认值"""
        c, _ = client
        info = _make_service_info(state=ServiceState.RUNNING, current_config={"existing": "val"})
        with (
            patch("edgelite.services.service_manager.get_service_manager") as mock_mgr,
            patch("edgelite.api.grafana._get_grafana_config", return_value=None),
        ):
            mock_mgr.return_value.get_service_info.return_value = info
            resp = await c.get("/api/v1/grafana/config")

        assert resp.status_code == 200
        body = resp.json()
        data = body["data"]
        # 默认 url/datasource/api_key
        assert data["url"] == "http://localhost:3001"
        assert data["datasource"] == "InfluxDB"
        assert data["api_key"] == ""
        assert data["enabled"] is True  # state != disabled
        assert data["state"] == "running"
        # current_config 应合并 url/datasource
        assert data["current_config"]["existing"] == "val"
        assert data["current_config"]["url"] == "http://localhost:3001"
        assert data["current_config"]["datasource"] == "InfluxDB"
        # 依赖信息
        assert len(data["dependencies"]) == 1
        assert data["dependencies"][0]["package"] == "grafana"
        assert data["dependencies"][0]["installed"] is True

    async def test_returns_config_with_api_key_masked(self, client):
        """有 api_key 时应返回 ***configured***"""
        c, _ = client
        info = _make_service_info(state=ServiceState.RUNNING, current_config={})
        cfg = _make_grafana_config(api_key="secret-key")
        with (
            patch("edgelite.services.service_manager.get_service_manager") as mock_mgr,
            patch("edgelite.api.grafana._get_grafana_config", return_value=cfg),
        ):
            mock_mgr.return_value.get_service_info.return_value = info
            resp = await c.get("/api/v1/grafana/config")

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["api_key"] == "***configured***"
        assert data["current_config"]["api_key"] == "***configured***"

    async def test_disabled_state_sets_enabled_false(self, client):
        """state=disabled 时 enabled 应为 False"""
        c, _ = client
        info = _make_service_info(state=ServiceState.DISABLED)
        with (
            patch("edgelite.services.service_manager.get_service_manager") as mock_mgr,
            patch("edgelite.api.grafana._get_grafana_config", return_value=None),
        ):
            mock_mgr.return_value.get_service_info.return_value = info
            resp = await c.get("/api/v1/grafana/config")

        assert resp.status_code == 200
        assert resp.json()["data"]["enabled"] is False

    async def test_current_config_not_dict_is_preserved(self, client):
        """current_config 非 dict 时不应抛异常"""
        c, _ = client
        info = _make_service_info(state=ServiceState.RUNNING, current_config=None)
        with (
            patch("edgelite.services.service_manager.get_service_manager") as mock_mgr,
            patch("edgelite.api.grafana._get_grafana_config", return_value=None),
        ):
            mock_mgr.return_value.get_service_info.return_value = info
            resp = await c.get("/api/v1/grafana/config")

        assert resp.status_code == 200
        # current_config 为 None 时不进入 dict 合并分支，保持 None
        cfg = resp.json()["data"]["current_config"]
        assert cfg["url"] == "http://localhost:3001"
        assert cfg["datasource"] == "InfluxDB"

    async def test_returns_500_when_service_manager_raises(self, client):
        """get_service_manager 抛非 HTTPException 异常时应返回 500"""
        c, _ = client
        with patch("edgelite.services.service_manager.get_service_manager", side_effect=RuntimeError("boom")):
            resp = await c.get("/api/v1/grafana/config")
        assert resp.status_code == 500
        assert resp.json()["detail"] == GrafanaErrors.CONNECTION_FAILED


# ── GET /api/v1/grafana/dashboards ────────────────────────────────────────────


class TestListGrafanaDashboards:
    async def test_returns_503_when_grafana_not_enabled(self, client):
        """grafana_config 为 None 或 enabled=False 时返回 503 NOT_ENABLED"""
        c, _ = client
        with patch("edgelite.api.grafana._get_grafana_config", return_value=None):
            resp = await c.get("/api/v1/grafana/dashboards")
        assert resp.status_code == 503
        assert resp.json()["detail"] == GrafanaErrors.NOT_ENABLED

    async def test_returns_503_when_enabled_false(self, client):
        c, _ = client
        cfg = _make_grafana_config(enabled=False)
        with patch("edgelite.api.grafana._get_grafana_config", return_value=cfg):
            resp = await c.get("/api/v1/grafana/dashboards")
        assert resp.status_code == 503
        assert resp.json()["detail"] == GrafanaErrors.NOT_ENABLED

    async def test_returns_503_when_api_key_missing(self, client):
        """api_key 为空时返回 503 API_KEY_MISSING"""
        c, _ = client
        cfg = _make_grafana_config(api_key="")
        with patch("edgelite.api.grafana._get_grafana_config", return_value=cfg):
            resp = await c.get("/api/v1/grafana/dashboards")
        assert resp.status_code == 503
        assert resp.json()["detail"] == GrafanaErrors.API_KEY_MISSING

    async def test_returns_500_when_url_unsafe(self, client):
        """grafana_url SSRF 校验失败时返回 500 CONNECTION_FAILED"""
        c, _ = client
        cfg = _make_grafana_config(url="http://127.0.0.1:3000")
        with patch("edgelite.api.grafana._get_grafana_config", return_value=cfg):
            resp = await c.get("/api/v1/grafana/dashboards")
        assert resp.status_code == 500
        assert resp.json()["detail"] == GrafanaErrors.CONNECTION_FAILED

    async def test_returns_dashboards_on_200(self, client):
        """HTTP 200 时应返回仪表板列表"""
        c, _ = client
        cfg = _make_grafana_config(url="https://grafana.example.com", api_key="key")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [{"uid": "abc", "title": "Dashboard 1"}]

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("edgelite.api.grafana._get_grafana_config", return_value=cfg),
            patch("edgelite.api.grafana._is_grafana_url_safe", return_value=True),
            patch("httpx.AsyncClient", return_value=mock_client),
        ):
            resp = await c.get("/api/v1/grafana/dashboards")

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["dashboards"] == [{"uid": "abc", "title": "Dashboard 1"}]
        # 验证 Bearer header 被设置
        mock_client.get.assert_awaited_once()
        _, kwargs = mock_client.get.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer key"

    async def test_returns_502_on_non_200_status(self, client):
        """HTTP 非 200 时 raise HTTPException(BAD_STATUS) 在 try 内被 except Exception 捕获转为 CONNECTION_FAILED"""
        c, _ = client
        cfg = _make_grafana_config()

        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.json.return_value = []

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("edgelite.api.grafana._get_grafana_config", return_value=cfg),
            patch("edgelite.api.grafana._is_grafana_url_safe", return_value=True),
            patch("httpx.AsyncClient", return_value=mock_client),
        ):
            resp = await c.get("/api/v1/grafana/dashboards")

        # BAD_STATUS HTTPException 在 try 内抛出，被 except Exception 捕获转为 CONNECTION_FAILED
        assert resp.status_code == 502
        assert resp.json()["detail"] == GrafanaErrors.CONNECTION_FAILED

    async def test_returns_502_on_request_exception(self, client):
        """请求抛异常时返回 502 CONNECTION_FAILED"""
        c, _ = client
        cfg = _make_grafana_config()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=RuntimeError("connect failed"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("edgelite.api.grafana._get_grafana_config", return_value=cfg),
            patch("edgelite.api.grafana._is_grafana_url_safe", return_value=True),
            patch("httpx.AsyncClient", return_value=mock_client),
        ):
            resp = await c.get("/api/v1/grafana/dashboards")

        assert resp.status_code == 502
        assert resp.json()["detail"] == GrafanaErrors.CONNECTION_FAILED

    async def test_returns_503_when_httpx_import_fails(self, client):
        """httpx 导入失败时返回 503 DEPS_MISSING"""
        c, _ = client
        cfg = _make_grafana_config()

        # 通过让内置 import 抛 ImportError 触发 except ImportError 分支
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "httpx":
                raise ImportError("no httpx")
            return real_import(name, *args, **kwargs)

        with (
            patch("edgelite.api.grafana._get_grafana_config", return_value=cfg),
            patch("edgelite.api.grafana._is_grafana_url_safe", return_value=True),
            patch("builtins.__import__", side_effect=fake_import),
        ):
            resp = await c.get("/api/v1/grafana/dashboards")

        assert resp.status_code == 503
        assert resp.json()["detail"] == GrafanaErrors.DEPS_MISSING


# ── GET /api/v1/grafana/embed-url ─────────────────────────────────────────────


class TestGetGrafanaEmbedUrl:
    async def test_returns_503_when_not_enabled(self, client):
        """grafana_config 为 None 时返回 503 NOT_ENABLED"""
        c, _ = client
        with patch("edgelite.api.grafana._get_grafana_config", return_value=None):
            resp = await c.get("/api/v1/grafana/embed-url")
        assert resp.status_code == 503
        assert resp.json()["detail"] == GrafanaErrors.NOT_ENABLED

    async def test_returns_503_when_enabled_false(self, client):
        c, _ = client
        cfg = _make_grafana_config(enabled=False)
        with patch("edgelite.api.grafana._get_grafana_config", return_value=cfg):
            resp = await c.get("/api/v1/grafana/embed-url")
        assert resp.status_code == 503
        assert resp.json()["detail"] == GrafanaErrors.NOT_ENABLED

    async def test_returns_500_when_url_unsafe(self, client):
        """SSRF 校验失败时返回 500 CONNECTION_FAILED"""
        c, _ = client
        cfg = _make_grafana_config(url="http://10.0.0.1:3000")
        with patch("edgelite.api.grafana._get_grafana_config", return_value=cfg):
            resp = await c.get("/api/v1/grafana/embed-url")
        assert resp.status_code == 500
        assert resp.json()["detail"] == GrafanaErrors.CONNECTION_FAILED

    async def test_returns_url_without_uid(self, client):
        """无 uid 时返回基础 kiosk URL"""
        c, _ = client
        cfg = _make_grafana_config(url="https://grafana.example.com/")
        with (
            patch("edgelite.api.grafana._get_grafana_config", return_value=cfg),
            patch("edgelite.api.grafana._is_grafana_url_safe", return_value=True),
        ):
            resp = await c.get("/api/v1/grafana/embed-url")
        assert resp.status_code == 200
        url = resp.json()["data"]["url"]
        assert url == "https://grafana.example.com/?kiosk&theme=light"

    async def test_returns_url_with_valid_uid(self, client):
        """有效 uid 时返回 /d/{uid}?kiosk&theme=light"""
        c, _ = client
        cfg = _make_grafana_config(url="https://grafana.example.com")
        with (
            patch("edgelite.api.grafana._get_grafana_config", return_value=cfg),
            patch("edgelite.api.grafana._is_grafana_url_safe", return_value=True),
        ):
            resp = await c.get("/api/v1/grafana/embed-url?dashboard_uid=abc-123_def")
        assert resp.status_code == 200
        url = resp.json()["data"]["url"]
        assert url == "https://grafana.example.com/d/abc-123_def?kiosk&theme=light"

    async def test_returns_400_when_uid_invalid(self, client):
        """uid 含非法字符时返回 400 INVALID_UID"""
        c, _ = client
        cfg = _make_grafana_config(url="https://grafana.example.com")
        with (
            patch("edgelite.api.grafana._get_grafana_config", return_value=cfg),
            patch("edgelite.api.grafana._is_grafana_url_safe", return_value=True),
        ):
            resp = await c.get("/api/v1/grafana/embed-url?dashboard_uid=bad@uid!")
        assert resp.status_code == 400
        assert resp.json()["detail"] == GrafanaErrors.INVALID_UID

    async def test_returns_500_on_unexpected_exception(self, client):
        """try 内非 HTTP 异常应返回 500 CONNECTION_FAILED"""
        c, _ = client
        cfg = _make_grafana_config()
        with (
            patch("edgelite.api.grafana._get_grafana_config", return_value=cfg),
            patch("edgelite.api.grafana._is_grafana_url_safe", side_effect=RuntimeError("boom")),
        ):
            resp = await c.get("/api/v1/grafana/embed-url")
        assert resp.status_code == 500
        assert resp.json()["detail"] == GrafanaErrors.CONNECTION_FAILED

    async def test_uid_max_length_enforced(self, client):
        """uid 超过 128 字符应被 Query 校验拒绝（422）"""
        c, _ = client
        long_uid = "a" * 129
        resp = await c.get(f"/api/v1/grafana/embed-url?dashboard_uid={long_uid}")
        assert resp.status_code == 422
