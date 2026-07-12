"""全局测试隔离夹具与共享夹具。

FIXED: 原问题-test_acceptance_smoke/test_acceptance_functional 直接 mutate 全局
edgelite.app._app_state 的属性（database/config/audit_service 等）且无清理，
导致后续 test_security_session_manager 等依赖 _app_state 干净状态的测试失败
（_get_db_path 返回 MagicMock 而非 None，SQLite 持久化路径异常）。

本文件提供：
1. _isolate_app_state (autouse): 每个测试前后快照/恢复 _app_state 实例属性
2. mock_config: 可变的测试配置对象，patch get_config 供 jwt/csrf 等模块测试使用
3. 北向适配器缺失模块的统一桩实现（edgelite.models.north / north_base / mqtt_utils / js_sandbox）
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from typing import Any

import pytest

# ═══════════════════════════════════════════════════════════════════════════
# 北向适配器缺失模块的统一桩实现
# ═══════════════════════════════════════════════════════════════════════════
# edgelite.models.north / edgelite.platform.north_base / mqtt_utils / js_sandbox
# 在当前代码库中不存在（北向适配器重建中）。此处提供统一桩实现，供
# platform_service.py 和 api/platforms.py 导入。
#
# MUST 在 conftest.py（任何测试文件之前）执行，确保所有测试共享同一组类对象。
# 否则 test_api_platforms.py 和 test_platform_service.py 各自注入不同的桩会导致
# platform_service.py 绑定到错误的类（如空 type() 类不接受构造参数）。
from pydantic import BaseModel


class _Mqtt5Properties(BaseModel):
    enabled: bool = False
    message_expiry_interval: Any = None
    content_type: str = ""
    response_topic: str = ""
    correlation_data: str = ""
    user_properties: dict = {}


class _MqttTlsConfig(BaseModel):
    enabled: bool = False
    ca_cert: str = ""
    client_cert: str = ""
    client_key: str = ""
    verify_server: bool = True


class _MqttWillConfig(BaseModel):
    enabled: bool = False
    topic: str = ""
    payload: str = ""
    qos: int = 1
    retain: bool = True


class _MqttConnectionConfig(BaseModel):
    broker_host: str = ""
    broker_port: int = 1883
    client_id: str = ""
    username: str = ""
    password: str = ""
    keepalive: int = 60
    clean_session: bool = True
    protocol_version: int = 4
    transport: str = "tcp"
    tls: Any = None
    will: Any = None
    mqtt5_props: Any = None


class _TopicTemplateConfig(BaseModel):
    prefix: str = "edgelite"
    template: str = "{prefix}/{device_id}/{point_id}"
    status_template: str = "{prefix}/{device_id}/status"


class _PayloadConfig(BaseModel):
    format: str = "json"
    custom_template: str = ""
    compress_threshold: int = 1024
    enable_compression: bool = True


class _QosPolicy(BaseModel):
    default_qos: int = 0
    alarm_qos: int = 1
    rules: list = []


class _NorthConfig(BaseModel):
    platform_type: str = ""
    connection_params: dict = {}
    publish_mode: str = "realtime"
    batch_size: int = 100
    timeout: float = 10.0
    enable_qos: bool = True
    mqtt: Any = None
    topic: Any = None
    payload: Any = None
    qos_policy: Any = None
    dedup_window_seconds: int = 300


_north_mod = types.ModuleType("edgelite.models.north")
for _cls in (
    _Mqtt5Properties,
    _MqttTlsConfig,
    _MqttWillConfig,
    _MqttConnectionConfig,
    _TopicTemplateConfig,
    _PayloadConfig,
    _QosPolicy,
    _NorthConfig,
):
    setattr(_north_mod, _cls.__name__.lstrip("_"), _cls)
sys.modules["edgelite.models.north"] = _north_mod


# ── 桩 BaseNorthAdapter —— 供 issubclass 检查与默认行为 ──────────────────
class BaseNorthAdapter:
    """北向适配器基类桩实现，供 platform_service.py 的 issubclass 检查。"""

    platform_name: str = "stub"
    platform_version: str = "1.0.0"

    def __init__(self, config: Any = None):
        self._connected = False
        self._state = "disconnected"
        self._queue = SimpleNamespace(size=0)
        self._metrics = SimpleNamespace(
            messages_total=0, errors_total=0, dedup_dropped=0, compressed_total=0
        )
        self._last_heartbeat = None
        self._mqtt_client = None

    async def start(self, config: Any = None) -> None:
        self._connected = True
        self._state = "connected"

    async def stop(self) -> None:
        self._connected = False
        self._state = "disconnected"

    async def is_connected(self) -> bool:
        return self._connected

    async def get_dashboard_data(self, label: str = "") -> dict:
        return {"platform_name": self.platform_name, "label": label, "connected": self._connected}

    def get_prometheus_metrics(self) -> str:
        return f"# metrics for {self.platform_name}"


_north_base_mod = types.ModuleType("edgelite.platform.north_base")
_north_base_mod.BaseNorthAdapter = BaseNorthAdapter
sys.modules["edgelite.platform.north_base"] = _north_base_mod


# ── 桩 mqtt_utils (TopicTemplateEngine / AdvancedTemplateEngine) ──────────
class TopicTemplateEngine:
    @staticmethod
    def validate_template(template: str):
        if not template:
            return True, []
        if "{" in template and "}" not in template:
            return False, ["unclosed brace"]
        return True, []

    @staticmethod
    def extract_variables(template: str):
        import re

        return re.findall(r"\{(\w+)\}", template)


class AdvancedTemplateEngine:
    def __init__(self, gateway_id: str = ""):
        self.gateway_id = gateway_id

    @staticmethod
    def validate_template(template: str):
        if not template:
            return False, ["empty template"]
        return True, []

    @staticmethod
    def extract_variables(template: str):
        import re

        return re.findall(r"\{(\w+)\}", template)

    def render_topic(self, template, dp, gateway_id):
        return f"rendered_topic:{template}"

    def render_payload(self, template, dp, gateway_id):
        return f"rendered_payload:{template}"

    def render_batch_payload(self, template, dps, gateway_id):
        return f"rendered_batch:{template}"


_mqtt_utils_mod = types.ModuleType("edgelite.platform.mqtt_utils")
_mqtt_utils_mod.TopicTemplateEngine = TopicTemplateEngine
_mqtt_utils_mod.AdvancedTemplateEngine = AdvancedTemplateEngine
sys.modules["edgelite.platform.mqtt_utils"] = _mqtt_utils_mod


# ── 桩 js_sandbox (JsSandbox) ────────────────────────────────────────────
class JsSandboxResult:
    def __init__(self, success=True, data=None, error=None, execution_ms=0.0):
        self.success = success
        self.data = data
        self.error = error
        self.execution_ms = execution_ms


class JsSandbox:
    def validate_script(self, script: str):
        if not script:
            return False, ["empty script"]
        return True, []

    async def execute(self, script, payload, context):
        return JsSandboxResult(
            success=True, data={"out": payload}, error=None, execution_ms=1.0
        )


_js_sandbox_mod = types.ModuleType("edgelite.platform.js_sandbox")
_js_sandbox_mod.JsSandbox = JsSandbox
sys.modules["edgelite.platform.js_sandbox"] = _js_sandbox_mod


@pytest.fixture(autouse=True)
def _isolate_app_state():
    """每个测试前后快照/恢复全局 _app_state 属性，防止跨测试污染。

    注意：patch("edgelite.app._app_state", ...) 替换的是模块名绑定，不影响本夹具
    捕获的原始 ServiceContainer 实例；patch 退出后模块名恢复，本夹具再恢复实例属性。
    """
    try:
        from edgelite.app import _app_state

        snapshot = dict(vars(_app_state))
        target = _app_state
    except Exception:
        # 导入失败（极早期测试环境）时直接放行，不阻塞测试
        yield
        return

    yield

    # teardown：恢复 _app_state 到测试前的状态
    try:
        current = dict(vars(target))
        # 删除测试期间新增的属性
        for key in current:
            if key not in snapshot:
                try:
                    delattr(target, key)
                except AttributeError:
                    pass
        # 恢复被修改的属性
        for key, value in snapshot.items():
            setattr(target, key, value)
    except Exception:
        pass


@pytest.fixture
def mock_config(monkeypatch):
    """提供可变的测试配置对象，patch get_config 使 jwt/csrf 等模块使用测试配置。

    jwt.py 在模块级 `from edgelite.config import get_config`，需同时 patch
    edgelite.security.jwt.get_config；csrf.py 延迟导入 edgelite.config.get_config，
    需 patch edgelite.config.get_config。两者返回同一对象，使测试中
    `from edgelite.config import get_config; get_config()` 与 jwt 内部调用一致。

    security 字段覆盖 jwt.py / csrf.py 全部读取项，且 csrf_secret 与 secret_key
    不同（F5 CSRF 独立于 JWT 断言所需）。security 为 SimpleNamespace，测试可用
    monkeypatch.setattr(mock_config.security, ...) 临时改值。
    """
    security = SimpleNamespace(
        secret_key="test-secret-key-for-jwt-testing-32+chars!",
        secret_key_previous=None,
        algorithm="HS256",
        key_id="test-kid",
        previous_key_id="old-kid",
        max_token_ttl_days=30,
        access_token_expire_minutes=30,
        refresh_token_expire_days=7,
        csrf_secret="csrf-secret-key-for-testing-32+chars!!",
        cookie_secure=False,
    )
    config = SimpleNamespace(security=security)

    monkeypatch.setattr("edgelite.config.get_config", lambda: config)
    # jwt.py 模块级已绑定 get_config，需单独 patch 其模块属性
    monkeypatch.setattr("edgelite.security.jwt.get_config", lambda: config)
    return config


# ── 共享测试辅助函数（供 test_api_system 等通过 `from conftest import` 使用）──


def make_mock_audit_service():
    """返回带 log 方法的 mock 审计服务，供需要 audit_service 的端点测试使用。"""
    from unittest.mock import AsyncMock

    svc = AsyncMock()
    svc.log = AsyncMock(return_value=None)
    return svc


def make_app(router=None, role: str = "admin", services: dict | None = None):
    """构建测试用 FastAPI 应用：覆盖认证依赖 + 注入 mock 服务。

    Args:
        router: 要挂载的 APIRouter（None 时挂载空应用，仅用于依赖测试）
        role: 覆盖认证用户的角色（admin/operator/viewer）
        services: 注入 app.state 的服务字典
    """
    import os

    from fastapi import FastAPI

    from edgelite.api.deps import get_current_user

    os.environ.setdefault(
        "EDGELITE_SECURITY__SECRET_KEY", "test-secret-key-for-testing-only-32chars!"
    )
    os.environ.setdefault("DEV_MODE", "true")

    app = FastAPI(title="EdgeLite Test")
    if router is not None:
        app.include_router(router)

    # 覆盖认证依赖：require_permission 内部依赖 get_current_user
    user = {"user_id": "test-admin", "username": "testadmin", "role": role}
    app.dependency_overrides[get_current_user] = lambda: user

    # 注入服务到 app.state
    if services:
        for key, value in services.items():
            setattr(app.state, key, value)

    return app

