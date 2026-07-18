#!/usr/bin/env python3
"""EdgeLite Gateway E2E 冒烟测试。

此文件位于 e2e/ 目录，用于端到端测试检测。
执行方式: pytest e2e/test_e2e_smoke.py -v
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from edgelite.api.deps import get_current_user
from edgelite.api.health import router as health_router

_TEST_SECRET = "test-secret-key-for-e2e-testing-only-32chars!!"
_TEST_USER = {"user_id": "test-admin", "username": "testadmin", "role": "admin"}


@pytest.fixture
def e2e_app():
    """E2E 测试应用。"""
    os.environ.setdefault("EDGELITE_SECURITY__SECRET_KEY", _TEST_SECRET)
    os.environ.setdefault("DEV_MODE", "true")
    app = FastAPI(title="EdgeLite E2E Test")
    app.include_router(health_router)
    app.dependency_overrides[get_current_user] = lambda: _TEST_USER
    app.state.database = MagicMock()
    app.state.audit_service = AsyncMock()
    app.state.audit_service.log = AsyncMock(return_value=None)
    return app


@pytest.mark.e2e
class TestE2ESmoke:
    """端到端冒烟测试。"""

    @pytest.mark.asyncio
    async def test_e2e_health_live(self, e2e_app):
        """E2E: 验证 liveness 端点。"""
        transport = ASGITransport(app=e2e_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health/live")
            assert resp.status_code == 200
            assert resp.json().get("status") == "ok"

    @pytest.mark.asyncio
    async def test_e2e_health_ready(self, e2e_app):
        """E2E: 验证 readiness 端点。"""
        transport = ASGITransport(app=e2e_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health")
            assert resp.status_code in (200, 503)
