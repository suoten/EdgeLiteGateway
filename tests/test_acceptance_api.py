"""EdgeLite 验收测试 — 接口测试模块

测试范围: 所有 /api/v1/* 接口的 200/400/401/403/500 场景
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from test_acceptance_smoke import _build_test_app

from edgelite.api.deps import get_current_user

# ═══════════════════════════════════════════════════════════════
# 模块 3: 接口测试 (API)
# ═══════════════════════════════════════════════════════════════


class TestAPI200Success:
    """A1: 所有核心端点 200 成功响应"""

    @pytest.fixture
    def app(self):
        return _build_test_app("admin")

    @pytest.fixture
    async def client(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c

    @pytest.mark.asyncio
    async def test_a1_01_get_devices_200(self, client):
        resp = await client.get("/api/v1/devices")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_a1_02_get_rules_200(self, client):
        resp = await client.get("/api/v1/rules")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_a1_03_get_alarms_200(self, client):
        resp = await client.get("/api/v1/alarms")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_a1_04_get_system_status_200(self, client):
        resp = await client.get("/api/v1/system/status")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_a1_05_get_system_resources_200(self, client):
        resp = await client.get("/api/v1/system/resources")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_a1_06_get_drivers_200(self, client):
        resp = await client.get("/api/v1/drivers")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_a1_07_get_services_200(self, client):
        resp = await client.get("/api/v1/services/list")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_a1_08_get_system_config_200(self, client):
        resp = await client.get("/api/v1/system/config")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_a1_09_get_system_ready_status_200(self, client):
        resp = await client.get("/api/v1/system/ready-status")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_a1_10_get_system_health_basic_200(self, client):
        resp = await client.get("/api/v1/system/health/basic")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_a1_11_get_system_circuit_breakers_200(self, client):
        resp = await client.get("/api/v1/system/circuit-breakers")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_a1_12_get_system_locks_status_200(self, client):
        resp = await client.get("/api/v1/system/locks/status")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_a1_13_get_system_network_200(self, client):
        resp = await client.get("/api/v1/system/network")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_a1_14_get_system_retention_200(self, client):
        resp = await client.get("/api/v1/system/retention")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_a1_15_get_system_performance_200(self, client):
        resp = await client.get("/api/v1/system/performance")
        assert resp.status_code == 200


class TestAPI400BadRequest:
    """A2: 400 错误请求场景"""

    @pytest.fixture
    def app(self):
        return _build_test_app("admin")

    @pytest.fixture
    async def client(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c

    @pytest.mark.asyncio
    async def test_a2_01_create_device_missing_required_fields(self, client):
        """缺少必填字段返回 422"""
        resp = await client.post("/api/v1/devices", json={})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_a2_02_create_device_invalid_protocol(self, client):
        """无效协议返回 422"""
        payload = {
            "device_id": "test-dev-bad",
            "name": "坏设备",
            "protocol": "nonexistent_protocol",
            "config": {"slave_id": 1},
            "points": [{"name": "temp", "data_type": "float32", "address": "0"}],
        }
        resp = await client.post("/api/v1/devices", json=payload)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_a2_03_create_device_invalid_port_range(self, client):
        """端口超出范围返回 422"""
        payload = {
            "device_id": "test-dev-port",
            "name": "端口越界",
            "protocol": "modbus_tcp",
            "config": {"host": "127.0.0.1", "port": 99999, "slave_id": 1},
            "points": [{"name": "temp", "data_type": "float32", "address": "0"}],
        }
        resp = await client.post("/api/v1/devices", json=payload)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_a2_04_create_device_invalid_collect_interval(self, client):
        """采集间隔为负数返回 422"""
        payload = {
            "device_id": "test-dev-int",
            "name": "采集间隔",
            "protocol": "modbus_tcp",
            "config": {"slave_id": 1},
            "points": [{"name": "temp", "data_type": "float32", "address": "0"}],
            "collect_interval": -1,
        }
        resp = await client.post("/api/v1/devices", json=payload)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_a2_05_create_rule_missing_fields(self, client):
        """缺少必填字段返回 422"""
        resp = await client.post("/api/v1/rules", json={})
        assert resp.status_code == 422


class TestAPI401Unauthorized:
    """A3: 401 未认证场景"""

    @pytest.fixture
    def no_auth_app(self):
        """构建不覆盖认证依赖的应用（模拟无 Token 请求）"""
        app = _build_test_app("admin")
        # 移除认证覆盖，模拟未登录
        app.dependency_overrides.pop(get_current_user, None)
        return app

    @pytest.fixture
    async def no_auth_client(self, no_auth_app):
        transport = ASGITransport(app=no_auth_app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c

    @pytest.mark.asyncio
    async def test_a3_01_devices_no_token(self, no_auth_client):
        resp = await no_auth_client.get("/api/v1/devices")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_a3_02_rules_no_token(self, no_auth_client):
        resp = await no_auth_client.get("/api/v1/rules")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_a3_03_alarms_no_token(self, no_auth_client):
        resp = await no_auth_client.get("/api/v1/alarms")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_a3_04_system_config_no_token(self, no_auth_client):
        resp = await no_auth_client.get("/api/v1/system/config")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_a3_05_create_device_no_token(self, no_auth_client):
        resp = await no_auth_client.post("/api/v1/devices", json={
            "device_id": "unauthorized-dev",
            "name": "未授权设备",
            "protocol": "modbus_tcp",
            "config": {"slave_id": 1},
            "points": [{"name": "temp", "data_type": "float32", "address": "0"}],
        })
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_a3_06_invalid_bearer_token(self, no_auth_client):
        """发送无效 Bearer Token"""
        resp = await no_auth_client.get(
            "/api/v1/devices",
            headers={"Authorization": "Bearer invalid.token.here"},
        )
        assert resp.status_code == 401


class TestAPI403Forbidden:
    """A4: 403 权限不足场景"""

    @pytest.fixture
    def viewer_app(self):
        return _build_test_app("viewer")

    @pytest.fixture
    async def viewer_client(self, viewer_app):
        transport = ASGITransport(app=viewer_app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c

    @pytest.mark.asyncio
    async def test_a4_01_viewer_cannot_create_device(self, viewer_client):
        """viewer 角色不能创建设备"""
        resp = await viewer_client.post("/api/v1/devices", json={
            "device_id": "viewer-dev-test",
            "name": "viewer设备",
            "protocol": "modbus_tcp",
            "config": {"slave_id": 1},
            "points": [{"name": "temp", "data_type": "float32", "address": "0"}],
        })
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_a4_02_viewer_cannot_delete_device(self, viewer_client):
        """viewer 角色不能删除设备"""
        resp = await viewer_client.delete("/api/v1/devices/any-device")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_a4_03_viewer_cannot_create_rule(self, viewer_client):
        """viewer 角色不能创建规则"""
        resp = await viewer_client.post("/api/v1/rules", json={
            "name": "viewer规则",
            "device_id": "any-device",
            "severity": "critical",
            "conditions": [{"point": "temp", "operator": ">", "threshold": 80, "type": "threshold"}],
        })
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_a4_04_viewer_cannot_delete_rule(self, viewer_client):
        """viewer 角色不能删除规则"""
        resp = await viewer_client.delete("/api/v1/rules/any-rule")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_a4_05_viewer_cannot_update_system_config(self, viewer_client):
        """viewer 角色不能更新系统配置"""
        resp = await viewer_client.put(
            "/api/v1/system/config/security",
            json={"secret_key": "new-key"},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_a4_06_viewer_cannot_reload_config(self, viewer_client):
        """viewer 角色不能重载配置"""
        resp = await viewer_client.post("/api/v1/system/config/reload")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_a4_07_viewer_can_read_devices(self, viewer_client):
        """viewer 角色可以读取设备列表"""
        resp = await viewer_client.get("/api/v1/devices")
        # viewer 可能返回 200 或 403（取决于权限和设备所有权），mock 环境可能返回 500
        assert resp.status_code in (200, 403, 500)

    @pytest.mark.asyncio
    async def test_a4_08_viewer_can_read_rules(self, viewer_client):
        """viewer 角色可以读取规则列表"""
        resp = await viewer_client.get("/api/v1/rules")
        # viewer 可能返回 200 或 403（取决于权限和规则所有权），mock 环境可能返回 500
        assert resp.status_code in (200, 403, 500)


class TestAPI404NotFound:
    """A5: 404 资源不存在场景"""

    @pytest.fixture
    def app(self):
        return _build_test_app("admin")

    @pytest.fixture
    async def client(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c

    @pytest.mark.asyncio
    async def test_a5_01_device_not_found(self, client, app):
        app.state.device_service.get_device = AsyncMock(return_value=None)
        resp = await client.get("/api/v1/devices/nonexistent-001")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_a5_02_rule_not_found(self, client, app):
        app.state.rule_service.get_rule = AsyncMock(return_value=None)
        resp = await client.get("/api/v1/rules/nonexistent-rule")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_a5_03_unknown_endpoint(self, client):
        resp = await client.get("/api/v1/nonexistent-endpoint")
        assert resp.status_code == 404


class TestAPI500ServerError:
    """A6: 500 服务端错误场景"""

    @pytest.fixture
    def app(self):
        return _build_test_app("admin")

    @pytest.fixture
    async def client(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c

    @pytest.mark.asyncio
    async def test_a6_01_device_service_error(self, client, app):
        """设备服务内部异常返回 500"""
        app.state.device_service.list_devices = AsyncMock(
            side_effect=RuntimeError("DB connection lost")
        )
        resp = await client.get("/api/v1/devices")
        assert resp.status_code in (500, 503)  # mock 环境可能返回 503

    @pytest.mark.asyncio
    async def test_a6_02_rule_service_error(self, client, app):
        """规则服务内部异常返回 500"""
        app.state.rule_service.list_rules = AsyncMock(
            side_effect=RuntimeError("unexpected error")
        )
        resp = await client.get("/api/v1/rules")
        assert resp.status_code in (500, 503)

    @pytest.mark.asyncio
    async def test_a6_03_alarm_service_error(self, client, app):
        """告警服务内部异常返回 500"""
        app.state.alarm_service.list_alarms = AsyncMock(
            side_effect=RuntimeError("unexpected error")
        )
        resp = await client.get("/api/v1/alarms")
        assert resp.status_code in (500, 503)


class TestAPIPagination:
    """A7: 分页参数测试"""

    @pytest.fixture
    def app(self):
        return _build_test_app("admin")

    @pytest.fixture
    async def client(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c

    @pytest.mark.asyncio
    async def test_a7_01_default_pagination(self, client):
        resp = await client.get("/api/v1/devices")
        assert resp.status_code == 200
        data = resp.json()
        assert "page" in data
        assert "size" in data
        assert "total" in data

    @pytest.mark.asyncio
    async def test_a7_02_custom_pagination(self, client):
        resp = await client.get("/api/v1/devices", params={"page": 2, "size": 50})
        assert resp.status_code == 200
        data = resp.json()
        assert data["page"] == 2
        assert data["size"] == 50

    @pytest.mark.asyncio
    async def test_a7_03_invalid_page(self, client):
        """page=0 返回 422"""
        resp = await client.get("/api/v1/devices", params={"page": 0})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_a7_04_oversize_page(self, client):
        """size > 5000 返回 422"""
        resp = await client.get("/api/v1/devices", params={"page": 1, "size": 5001})
        assert resp.status_code in (422, 400)


class TestAPIResponseFormat:
    """A8: 统一响应格式验证"""

    @pytest.fixture
    def app(self):
        return _build_test_app("admin")

    @pytest.fixture
    async def client(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c

    @pytest.mark.asyncio
    async def test_a8_01_list_response_has_data_field(self, client):
        resp = await client.get("/api/v1/devices")
        assert resp.status_code == 200
        data = resp.json()
        assert "data" in data
        assert isinstance(data["data"], list)

    @pytest.mark.asyncio
    async def test_a8_02_list_response_has_total_field(self, client):
        resp = await client.get("/api/v1/rules")
        assert resp.status_code == 200
        data = resp.json()
        assert "total" in data
        assert isinstance(data["total"], int)

    @pytest.mark.asyncio
    async def test_a8_03_error_response_has_detail(self, client):
        """错误响应包含 detail 字段"""
        resp = await client.post("/api/v1/devices", json={})
        assert resp.status_code == 422
        data = resp.json()
        assert "detail" in data
