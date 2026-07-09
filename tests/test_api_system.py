"""API unit tests for system module — cert/rotate endpoint coverage.

FIXED: 重新创建丢失的 test_api_system.py (untracked 文件 stash 恢复失败导致丢失)
仅覆盖 cert/rotate 端点，验证 Phase 4 修复的证书轮换功能 [2026-06-29]
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from edgelite.api.system import router


def _services_for_system() -> dict:
    """Build the app.state services required by system endpoints."""
    from conftest import make_mock_audit_service

    return {
        "system_service": AsyncMock(),
        "scheduler": AsyncMock(),
        "audit_service": make_mock_audit_service(),
    }


@pytest.fixture
async def client():
    from conftest import make_app

    app = make_app(router, role="admin", services=_services_for_system())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c, app


# ── /cert/rotate ──
# FIXED: Phase 4 修复 — 端点从 501 占位符改为实际证书状态检查实现 [2026-06-29]
# 当前实现：读取 mqtt_server.tls 配置中的证书路径，调用 CertManager.validate_cert
# 返回每个证书的 valid/expired/missing 状态及过期天数


async def test_cert_rotate_returns_200_with_certificate_status(client):
    """cert/rotate 应返回 200 及证书状态字典（无证书配置时返回空 results）。"""
    c, _ = client
    resp = await c.post("/api/v1/system/cert/rotate")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["code"] == 200
    assert "certificates" in body["data"]
