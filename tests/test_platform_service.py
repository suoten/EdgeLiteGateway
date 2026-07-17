"""platform_service 综合单元测试

覆盖 edgelite.services.platform_service.PlatformService 的全部公开方法与模块级辅助函数：
- 配置脱敏 (_mask_sensitive_config_fields)
- 平台注册表 (_ensure_registry / list_supported / get_config_schema / _build_full_schema)
- 配置校验 (validate_config)
- 连接管理 (connect / disconnect / reload_config / test_connection / _test_north_adapter)
- 状态查询 (list_platforms / get_status / get_dashboard_data / get_north_metrics)
- 北向适配器代理 (get_message_preview / get_broker_quality / get_tb_* / get_platform_*)
- 配置导入导出 (export_config / import_config / _flatten_custom_config)
- 模板与脚本 (validate_topic_template / validate_advanced_template / preview_template
  / validate_script / test_script)
- MQTT 测试发布 (mqtt_test_publish)

由于 edgelite.models.north / edgelite.platform.north_base / mqtt_utils / js_sandbox
模块在当前代码库中缺失（北向适配器重建中），测试通过 sys.modules 注入桩模块
来满足 platform_service 的导入依赖。
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, "src")

# 北向适配器缺失模块的桩实现在 conftest.py 中统一设置（edgelite.models.north /
# edgelite.platform.north_base / mqtt_utils / js_sandbox），此处直接引用
# conftest 注入的 BaseNorthAdapter 供 _make_mock_adapter_cls 使用。
_BaseNorthAdapter = sys.modules["edgelite.platform.north_base"].BaseNorthAdapter  # noqa: E402

# ── 安全导入 platform_service ─────────────────────────────────────────────
from edgelite.services.platform_service import (  # noqa: E402
    _PLATFORM_REGISTRY,
    PlatformService,
    _build_full_schema,
    _build_north_config,
    _ensure_registry,
    _mask_sensitive_config_fields,
)


# ── 辅助函数 ──────────────────────────────────────────────────────────────
def _valid_iotsharp_config() -> dict:
    """返回可通过 validate_config 的 iotsharp 配置"""
    return {
        "broker": "iotsharp.example.com",
        "port": 1883,
        "device_token": "tok-abc",
    }


def _valid_thingsboard_config() -> dict:
    return {
        "broker": "thingsboard.example.com",
        "port": 1883,
        "token": "tb-token",
    }


def _make_mock_adapter_cls(name: str = "MockAdapter", connected_after_start: bool = True):
    """创建一个 BaseNorthAdapter 子类用于 north adapter 路径测试"""

    class _Adapter(_BaseNorthAdapter):
        platform_name = name
        platform_version = "9.9.9"

        async def start(self, config: Any = None) -> None:
            self._connected = connected_after_start
            self._state = "connected" if connected_after_start else "disconnected"

    return _Adapter


def _make_mock_handler_cls(name: str = "mock", connect_fails: bool = False):
    """创建一个模拟 handler 类（不继承 PlatformHandler，纯 MagicMock 风格）"""

    class _Handler:
        platform_name = name
        platform_version = "1.0.0"

        def __init__(self):
            self._connected = False

        async def connect(self, config: dict) -> None:
            if connect_fails:
                raise ConnectionError("connect failed")
            self._connected = True

        async def disconnect(self) -> None:
            self._connected = False

    return _Handler


# ═══════════════════════════════════════════════════════════════════════════
# 模块级辅助函数测试
# ═══════════════════════════════════════════════════════════════════════════
class TestMaskSensitiveConfigFields:
    def test_masks_password_in_dict(self):
        result = _mask_sensitive_config_fields({"password": "secret123", "user": "admin"})
        assert result["password"] == "***"
        assert result["user"] == "admin"

    def test_masks_all_sensitive_field_names(self):
        for field in ("password", "secret", "api_key", "token", "access_secret"):
            result = _mask_sensitive_config_fields({field: "val"})
            assert result[field] == "***", f"field {field} not masked"

    def test_does_not_mask_falsy_values(self):
        """空值/None/0 不脱敏（保留原值）"""
        result = _mask_sensitive_config_fields({"password": "", "token": None})
        assert result["password"] == ""
        assert result["token"] is None

    def test_non_sensitive_fields_preserved(self):
        result = _mask_sensitive_config_fields({"broker": "host", "port": 1883})
        assert result == {"broker": "host", "port": 1883}

    def test_recursive_dict(self):
        result = _mask_sensitive_config_fields({"outer": {"password": "p", "name": "x"}, "token": "t"})
        assert result["outer"]["password"] == "***"
        assert result["outer"]["name"] == "x"
        assert result["token"] == "***"

    def test_list_recursive(self):
        result = _mask_sensitive_config_fields([{"password": "p1"}, {"name": "x"}])
        assert result[0]["password"] == "***"
        assert result[1] == {"name": "x"}

    def test_scalar_passthrough(self):
        assert _mask_sensitive_config_fields("hello") == "hello"
        assert _mask_sensitive_config_fields(42) == 42
        assert _mask_sensitive_config_fields(None) is None

    def test_nested_list_in_dict(self):
        result = _mask_sensitive_config_fields({"brokers": [{"password": "p", "host": "h"}]})
        assert result["brokers"][0]["password"] == "***"
        assert result["brokers"][0]["host"] == "h"

    def test_empty_containers(self):
        assert _mask_sensitive_config_fields({}) == {}
        assert _mask_sensitive_config_fields([]) == []


class TestEnsureRegistry:
    def test_returns_registry_dict(self):
        _PLATFORM_REGISTRY.clear()
        reg = _ensure_registry()
        assert isinstance(reg, dict)
        assert len(reg) > 0

    def test_registry_contains_all_platforms(self):
        _PLATFORM_REGISTRY.clear()
        reg = _ensure_registry()
        for name in ("iotsharp", "thingsboard", "huawei_iotda", "thingscloud", "thingspanel", "custom"):
            assert name in reg, f"missing platform: {name}"

    def test_registry_entries_have_required_keys(self):
        _PLATFORM_REGISTRY.clear()
        reg = _ensure_registry()
        for name, entry in reg.items():
            assert "label" in entry, f"{name} missing label"
            assert "description" in entry, f"{name} missing description"
            assert "module" in entry, f"{name} missing module"
            assert "class" in entry, f"{name} missing class"
            assert "base_fields" in entry, f"{name} missing base_fields"

    def test_registry_cached_on_second_call(self):
        _PLATFORM_REGISTRY.clear()
        first = _ensure_registry()
        second = _ensure_registry()
        assert first is second


class TestBuildFullSchema:
    def test_schema_has_fields_and_sections(self):
        schema = _build_full_schema([{"name": "broker", "type": "string"}])
        assert "fields" in schema
        assert "sections" in schema
        assert schema["fields"] == [{"name": "broker", "type": "string"}]

    def test_schema_sections_contain_all_categories(self):
        schema = _build_full_schema([])
        section_titles = [s["title"] for s in schema["sections"]]
        assert "MQTT Connection" in section_titles
        assert "TLS/SSL" in section_titles
        assert "Last Will" in section_titles
        assert "Topic Template" in section_titles
        assert "Payload Format" in section_titles
        assert "QoS Policy" in section_titles

    def test_mqtt5_section_has_condition(self):
        schema = _build_full_schema([])
        mqtt5_sections = [s for s in schema["sections"] if "MQTT 5.0" in s["title"]]
        assert len(mqtt5_sections) == 1
        assert mqtt5_sections[0]["condition"] == "protocol_version == 5"


class TestBuildNorthConfig:
    def test_builds_with_minimal_config(self):
        cfg = {"broker": "host.example.com", "port": 1883}
        nc = _build_north_config("iotsharp", cfg)
        assert nc.platform_type == "iotsharp"
        assert nc.mqtt.broker_host == "host.example.com"
        assert nc.mqtt.broker_port == 1883

    def test_broker_host_fallback_to_broker_host_key(self):
        nc = _build_north_config("custom", {"broker_host": "fallback.host"})
        assert nc.mqtt.broker_host == "fallback.host"

    def test_username_fallback_to_token(self):
        nc = _build_north_config("thingsboard", {"token": "tb-tok"})
        assert nc.mqtt.username == "tb-tok"

    def test_password_fallback_to_secret(self):
        nc = _build_north_config("huawei_iotda", {"secret": "hw-secret"})
        assert nc.mqtt.password == "hw-secret"

    def test_default_port_when_missing(self):
        nc = _build_north_config("custom", {"broker": "h"})
        assert nc.mqtt.broker_port == 1883

    def test_tls_config_parsed(self):
        cfg = {"broker": "h", "tls_enabled": "true", "ca_cert": "/path/ca", "verify_server": "false"}
        nc = _build_north_config("custom", cfg)
        assert nc.mqtt.tls.enabled is True
        assert nc.mqtt.tls.ca_cert == "/path/ca"
        assert nc.mqtt.tls.verify_server is False

    def test_will_config_parsed(self):
        cfg = {"broker": "h", "will_enabled": "true", "will_topic": "t/will", "will_qos": 2}
        nc = _build_north_config("custom", cfg)
        assert nc.mqtt.will.enabled is True
        assert nc.mqtt.will.topic == "t/will"
        assert nc.mqtt.will.qos == 2

    def test_mqtt5_props_enabled_when_protocol_v5(self):
        nc = _build_north_config("custom", {"broker": "h", "protocol_version": 5})
        assert nc.mqtt.mqtt5_props.enabled is True

    def test_mqtt5_props_disabled_when_protocol_v4(self):
        nc = _build_north_config("custom", {"broker": "h", "protocol_version": 4})
        assert nc.mqtt.mqtt5_props.enabled is False

    def test_topic_template_config(self):
        cfg = {"broker": "h", "topic_prefix": "gw", "topic_template": "{prefix}/{device_id}"}
        nc = _build_north_config("custom", cfg)
        assert nc.topic.prefix == "gw"
        assert nc.topic.template == "{prefix}/{device_id}"

    def test_payload_config(self):
        cfg = {"broker": "h", "payload_format": "cbor", "enable_compression": "false"}
        nc = _build_north_config("custom", cfg)
        assert nc.payload.format == "cbor"
        assert nc.payload.enable_compression is False

    def test_qos_policy(self):
        cfg = {"broker": "h", "default_qos": 1, "alarm_qos": 2}
        nc = _build_north_config("custom", cfg)
        assert nc.qos_policy.default_qos == 1
        assert nc.qos_policy.alarm_qos == 2

    def test_connection_params_preserved(self):
        cfg = {"broker": "h", "custom_field": "val"}
        nc = _build_north_config("custom", cfg)
        assert nc.connection_params == cfg

    def test_publish_mode_and_batch_size(self):
        cfg = {"broker": "h", "publish_mode": "batch", "batch_size": 50}
        nc = _build_north_config("custom", cfg)
        assert nc.publish_mode == "batch"
        assert nc.batch_size == 50


# ═══════════════════════════════════════════════════════════════════════════
# PlatformService 初始化与属性
# ═══════════════════════════════════════════════════════════════════════════
class TestPlatformServiceInit:
    def test_default_init_empty_handlers(self):
        svc = PlatformService()
        assert svc.handlers == {}
        assert svc.adapters == {}
        assert svc._adapter_configs == {}

    def test_init_with_custom_handlers(self):
        h = MagicMock()
        svc = PlatformService(handlers={"foo": h})
        assert svc.handlers["foo"] is h

    def test_handlers_property_readonly_reference(self):
        svc = PlatformService()
        assert svc.handlers is svc._handlers

    def test_adapters_property_readonly_reference(self):
        svc = PlatformService()
        assert svc.adapters is svc._adapters


# ═══════════════════════════════════════════════════════════════════════════
# list_platforms / list_supported / get_config_schema
# ═══════════════════════════════════════════════════════════════════════════
class TestListPlatforms:
    def test_empty_returns_empty_list(self):
        assert PlatformService().list_platforms() == []

    def test_with_handler_only(self):
        h = MagicMock()
        h.platform_name = "iotsharp"
        h.platform_version = "1.0.0"
        h._connected = True
        svc = PlatformService(handlers={"iotsharp": h})
        result = svc.list_platforms()
        assert len(result) == 1
        assert result[0]["name"] == "iotsharp"
        assert result[0]["connected"] is True
        assert result[0]["state"] == "unknown"

    def test_with_adapter(self):
        svc = PlatformService()
        adapter = _BaseNorthAdapter()
        adapter.platform_name = "tb"
        adapter.platform_version = "2.0"
        adapter._connected = True
        adapter._state = "connected"
        svc._adapters["thingsboard"] = adapter
        result = svc.list_platforms()
        assert result[0]["name"] == "tb"
        assert result[0]["version"] == "2.0"
        assert result[0]["connected"] is True
        assert result[0]["state"] == "connected"

    def test_adapter_takes_precedence_over_handler(self):
        """当同名 adapter 和 handler 都存在时，adapter 优先"""
        svc = PlatformService()
        h = MagicMock()
        h.platform_name = "h_name"
        svc._handlers["x"] = h
        adapter = _BaseNorthAdapter()
        adapter.platform_name = "a_name"
        svc._adapters["x"] = adapter
        result = svc.list_platforms()
        assert result[0]["name"] == "a_name"


class TestListSupported:
    def test_returns_all_registered_platforms(self):
        result = PlatformService().list_supported()
        names = [p["name"] for p in result]
        for expected in ("iotsharp", "thingsboard", "huawei_iotda", "thingscloud", "thingspanel", "custom"):
            assert expected in names

    def test_entries_have_label_and_description(self):
        result = PlatformService().list_supported()
        for entry in result:
            assert "label" in entry
            assert "description" in entry


class TestGetConfigSchema:
    def test_existing_platform_returns_schema(self):
        svc = PlatformService()
        schema = svc.get_config_schema("iotsharp")
        assert schema is not None
        assert "fields" in schema
        assert "sections" in schema

    def test_unknown_platform_returns_none(self):
        assert PlatformService().get_config_schema("nonexistent") is None

    def test_schema_fields_from_base_fields(self):
        schema = PlatformService().get_config_schema("thingsboard")
        field_names = [f["name"] for f in schema["fields"]]
        assert "broker" in field_names
        assert "token" in field_names


# ═══════════════════════════════════════════════════════════════════════════
# validate_config
# ═══════════════════════════════════════════════════════════════════════════
class TestValidateConfig:
    def test_unsupported_platform(self):
        errors = PlatformService().validate_config("nonexistent", {})
        assert errors == ["ERR_PLATFORM_VALIDATION_UNSUPPORTED"]

    def test_valid_config_no_errors(self):
        errors = PlatformService().validate_config("iotsharp", _valid_iotsharp_config())
        assert errors == []

    def test_missing_required_field(self):
        cfg = {"broker": "h", "port": 1883}  # missing device_token
        errors = PlatformService().validate_config("iotsharp", cfg)
        assert "ERR_PLATFORM_VALIDATION_REQUIRED:device_token" in errors

    def test_required_field_zero_is_valid(self):
        """val=0 时 `not val and val != 0` 为 False，不应报 required 错误"""
        cfg = {"broker": "h", "port": 0, "device_token": "t"}
        errors = PlatformService().validate_config("iotsharp", cfg)
        # port=0 不会触发 required 错误（0 != 0 为 False）
        assert all("REQUIRED" not in e for e in errors)

    def test_broker_format_invalid(self):
        cfg = {"broker": "invalid broker!!", "port": 1883, "device_token": "t"}
        errors = PlatformService().validate_config("iotsharp", cfg)
        assert "ERR_PLATFORM_VALIDATION_BROKER_FORMAT" in errors

    def test_broker_format_valid_hostname(self):
        cfg = {"broker": "mqtt.example.com", "port": 1883, "device_token": "t"}
        errors = PlatformService().validate_config("iotsharp", cfg)
        assert not any("BROKER_FORMAT" in e for e in errors)

    def test_broker_format_valid_ipv4(self):
        cfg = {"broker": "192.168.1.100", "port": 1883, "device_token": "t"}
        errors = PlatformService().validate_config("iotsharp", cfg)
        assert not any("BROKER_FORMAT" in e for e in errors)

    def test_broker_with_mqtt_prefix_stripped(self):
        cfg = {"broker": "mqtts://mqtt.example.com", "port": 8883, "device_token": "t"}
        errors = PlatformService().validate_config("iotsharp", cfg)
        assert not any("BROKER_FORMAT" in e for e in errors)

    def test_port_out_of_range_low(self):
        cfg = {"broker": "h", "port": 0, "device_token": "t"}
        errors = PlatformService().validate_config("iotsharp", cfg)
        assert "ERR_PLATFORM_VALIDATION_PORT_RANGE" in errors

    def test_port_out_of_range_high(self):
        cfg = {"broker": "h", "port": 70000, "device_token": "t"}
        errors = PlatformService().validate_config("iotsharp", cfg)
        assert "ERR_PLATFORM_VALIDATION_PORT_RANGE" in errors

    def test_port_not_a_number(self):
        cfg = {"broker": "h", "port": "abc", "device_token": "t"}
        errors = PlatformService().validate_config("iotsharp", cfg)
        assert "ERR_PLATFORM_VALIDATION_PORT_NUMBER" in errors

    def test_port_none_skipped(self):
        """port_val is None 时跳过端口校验"""
        cfg = {"broker": "h", "device_token": "t"}
        errors = PlatformService().validate_config("iotsharp", cfg)
        assert not any("PORT" in e for e in errors)

    def test_field_too_long(self):
        long_val = "x" * 300
        cfg = {"broker": "h", "port": 1883, "device_token": "t", "username": long_val}
        errors = PlatformService().validate_config("iotsharp", cfg)
        assert "ERR_PLATFORM_VALIDATION_TOO_LONG:username" in errors

    def test_long_text_field_exempt_from_length_check(self):
        """ca_cert 等长文本字段允许超过 256 字符"""
        long_cert = "x" * 300
        cfg = {"broker": "h", "port": 1883, "device_token": "t", "ca_cert": long_cert}
        errors = PlatformService().validate_config("iotsharp", cfg)
        assert not any("TOO_LONG" in e for e in errors)

    def test_topic_template_invalid(self):
        cfg = {"broker": "h", "port": 1883, "device_token": "t", "topic_template": "{unclosed"}
        errors = PlatformService().validate_config("iotsharp", cfg)
        assert any("ERR_PLATFORM_VALIDATION_TOPIC" in e for e in errors)

    def test_topic_template_valid(self):
        cfg = {"broker": "h", "port": 1883, "device_token": "t", "topic_template": "{prefix}/{device_id}"}
        errors = PlatformService().validate_config("iotsharp", cfg)
        assert not any("TOPIC" in e for e in errors)


# ═══════════════════════════════════════════════════════════════════════════
# connect
# ═══════════════════════════════════════════════════════════════════════════
class TestConnect:
    async def test_already_connected_returns_already_connected(self):
        svc = PlatformService()
        svc._adapters["iotsharp"] = _BaseNorthAdapter()
        result = await svc.connect("iotsharp", _valid_iotsharp_config())
        assert result == {"status": "already_connected"}

    async def test_unsupported_platform_raises_value_error(self):
        svc = PlatformService()
        with pytest.raises(ValueError, match="Unsupported platform"):
            await svc.connect("nonexistent", {})

    async def test_validation_error_raises_value_error(self):
        svc = PlatformService()
        with pytest.raises(ValueError, match="ERR_PLATFORM_VALIDATION"):
            await svc.connect("iotsharp", {"broker": "!!invalid", "port": 1883, "device_token": "t"})

    async def test_connect_north_adapter_success(self):
        mock_adapter_cls = _make_mock_adapter_cls("iotsharp")
        mock_module = MagicMock()
        mock_module.IoTSharpNorthAdapter = mock_adapter_cls

        with patch(
            "edgelite.services.platform_service.importlib.import_module",
            return_value=mock_module,
        ):
            svc = PlatformService()
            result = await svc.connect("iotsharp", _valid_iotsharp_config())
            assert result == {"status": "connecting"}
            assert "iotsharp" in svc.adapters
            assert "iotsharp" in svc._adapter_configs

    async def test_connect_north_adapter_start_failure_raises_runtime_error(self):
        mock_adapter_cls = _make_mock_adapter_cls("iotsharp")

        class _FailAdapter(mock_adapter_cls):
            async def start(self, config):
                raise ConnectionError("start failed")

        mock_module = MagicMock()
        mock_module.IoTSharpNorthAdapter = _FailAdapter

        with patch(
            "edgelite.services.platform_service.importlib.import_module",
            return_value=mock_module,
        ):
            svc = PlatformService()
            with pytest.raises(RuntimeError, match="Platform connect failed"):
                await svc.connect("iotsharp", _valid_iotsharp_config())

    async def test_connect_handler_path_success(self):
        """north_adapter 导入失败时走 handler 路径"""
        mock_handler_cls = _make_mock_handler_cls("iotsharp")

        def import_side_effect(name):
            if "north_adapters" in name:
                raise ImportError("no north adapters")
            mod = MagicMock()
            mod.IoTSharpHandler = mock_handler_cls
            return mod

        with patch(
            "edgelite.services.platform_service.importlib.import_module",
            side_effect=import_side_effect,
        ):
            svc = PlatformService()
            result = await svc.connect("iotsharp", _valid_iotsharp_config())
            assert result == {"status": "connecting"}
            assert "iotsharp" in svc.handlers

    async def test_connect_handler_connect_failure_raises_runtime_error(self):
        mock_handler_cls = _make_mock_handler_cls("iotsharp", connect_fails=True)

        def import_side_effect(name):
            if "north_adapters" in name:
                raise ImportError("no north adapters")
            mod = MagicMock()
            mod.IoTSharpHandler = mock_handler_cls
            return mod

        with patch(
            "edgelite.services.platform_service.importlib.import_module",
            side_effect=import_side_effect,
        ):
            svc = PlatformService()
            with pytest.raises(RuntimeError, match="Platform connect failed"):
                await svc.connect("iotsharp", _valid_iotsharp_config())

    async def test_connect_handler_import_failure_raises_runtime_error(self):
        def import_side_effect(name):
            if "north_adapters" in name:
                raise ImportError("no north adapters")
            raise ImportError("handler missing")

        with patch(
            "edgelite.services.platform_service.importlib.import_module",
            side_effect=import_side_effect,
        ):
            svc = PlatformService()
            with pytest.raises(RuntimeError, match="Platform handler load failed"):
                await svc.connect("iotsharp", _valid_iotsharp_config())

    async def test_connect_north_adapter_not_subclass_falls_to_handler(self):
        """north_class 存在但不是 BaseNorthAdapter 子类时走 handler 路径"""

        class _NonAdapter:
            pass

        mock_na_module = MagicMock()
        mock_na_module.IoTSharpNorthAdapter = _NonAdapter
        mock_handler_cls = _make_mock_handler_cls("iotsharp")

        call_count = {"n": 0}

        def import_side_effect(name):
            call_count["n"] += 1
            if "north_adapters" in name:
                return mock_na_module
            mod = MagicMock()
            mod.IoTSharpHandler = mock_handler_cls
            return mod

        with patch(
            "edgelite.services.platform_service.importlib.import_module",
            side_effect=import_side_effect,
        ):
            svc = PlatformService()
            result = await svc.connect("iotsharp", _valid_iotsharp_config())
            assert result == {"status": "connecting"}
            assert "iotsharp" in svc.handlers
            assert "iotsharp" not in svc.adapters

    async def test_connect_concurrent_same_platform_returns_already_connected(self):
        """并发 connect 同一平台应只创建一个 adapter（锁保护）"""
        mock_adapter_cls = _make_mock_adapter_cls("iotsharp")

        started = asyncio.Event()

        class _SlowAdapter(mock_adapter_cls):
            async def start(self, config):
                started.set()
                await asyncio.sleep(0.1)
                self._connected = True
                self._state = "connected"

        mock_module = MagicMock()
        mock_module.IoTSharpNorthAdapter = _SlowAdapter

        with patch(
            "edgelite.services.platform_service.importlib.import_module",
            return_value=mock_module,
        ):
            svc = PlatformService()
            t1 = asyncio.create_task(svc.connect("iotsharp", _valid_iotsharp_config()))
            t2 = asyncio.create_task(svc.connect("iotsharp", _valid_iotsharp_config()))
            r1, r2 = await asyncio.gather(t1, t2)
            results = sorted([r1["status"], r2["status"]])
            assert "connecting" in results
            assert "already_connected" in results


# ═══════════════════════════════════════════════════════════════════════════
# disconnect
# ═══════════════════════════════════════════════════════════════════════════
class TestDisconnect:
    async def test_disconnect_adapter(self):
        svc = PlatformService()
        adapter = _BaseNorthAdapter()
        adapter._connected = True
        svc._adapters["iotsharp"] = adapter
        svc._adapter_configs["iotsharp"] = MagicMock()
        result = await svc.disconnect("iotsharp")
        assert result == {"status": "disconnected"}
        assert "iotsharp" not in svc.adapters
        assert adapter._connected is False

    async def test_disconnect_adapter_stop_exception_swallowed(self):
        svc = PlatformService()
        adapter = _BaseNorthAdapter()
        adapter.stop = AsyncMock(side_effect=RuntimeError("stop err"))
        svc._adapters["iotsharp"] = adapter
        result = await svc.disconnect("iotsharp")
        assert result == {"status": "disconnected"}

    async def test_disconnect_handler(self):
        svc = PlatformService()
        handler = MagicMock()
        handler.disconnect = AsyncMock()
        svc._handlers["iotsharp"] = handler
        result = await svc.disconnect("iotsharp")
        assert result == {"status": "disconnected"}
        handler.disconnect.assert_awaited_once()

    async def test_disconnect_handler_exception_swallowed(self):
        svc = PlatformService()
        handler = MagicMock()
        handler.disconnect = AsyncMock(side_effect=RuntimeError("disconnect err"))
        svc._handlers["iotsharp"] = handler
        result = await svc.disconnect("iotsharp")
        assert result == {"status": "disconnected"}

    async def test_disconnect_not_connected_raises_key_error(self):
        svc = PlatformService()
        with pytest.raises(KeyError, match="not connected"):
            await svc.disconnect("nonexistent")

    async def test_disconnect_removes_handler_from_handlers(self):
        svc = PlatformService()
        handler = MagicMock()
        handler.disconnect = AsyncMock()
        svc._handlers["iotsharp"] = handler
        await svc.disconnect("iotsharp")
        assert "iotsharp" not in svc.handlers


# ═══════════════════════════════════════════════════════════════════════════
# get_status
# ═══════════════════════════════════════════════════════════════════════════
class TestGetStatus:
    def test_status_with_adapter(self):
        svc = PlatformService()
        adapter = _BaseNorthAdapter()
        adapter.platform_name = "tb"
        adapter.platform_version = "2.0"
        adapter._connected = True
        adapter._state = "connected"
        adapter._queue.size = 5
        adapter._metrics.messages_total = 100
        adapter._metrics.errors_total = 2
        adapter._metrics.dedup_dropped = 1
        adapter._metrics.compressed_total = 3
        adapter._last_heartbeat = 12345.0
        svc._adapters["thingsboard"] = adapter
        status = svc.get_status("thingsboard")
        assert status["connected"] is True
        assert status["name"] == "tb"
        assert status["version"] == "2.0"
        assert status["state"] == "connected"
        assert status["queue_size"] == 5
        assert status["messages_total"] == 100
        assert status["errors_total"] == 2
        assert status["dedup_dropped"] == 1
        assert status["compressed_total"] == 3
        assert status["last_heartbeat"] == 12345.0

    def test_status_with_handler(self):
        svc = PlatformService()
        h = MagicMock()
        h._connected = True
        h.platform_name = "iotsharp"
        h.platform_version = "1.0.0"
        svc._handlers["iotsharp"] = h
        status = svc.get_status("iotsharp")
        assert status["connected"] is True
        assert status["name"] == "iotsharp"
        assert status["version"] == "1.0.0"
        assert status["state"] == "unknown"

    def test_status_not_connected(self):
        status = PlatformService().get_status("nonexistent")
        assert status == {"connected": False, "state": "disconnected"}

    def test_status_handler_without_connected_attr(self):
        svc = PlatformService()
        h = MagicMock(spec=[])  # no _connected attr
        h.platform_name = "x"
        h.platform_version = "1.0"
        svc._handlers["x"] = h
        status = svc.get_status("x")
        assert status["connected"] is False


# ═══════════════════════════════════════════════════════════════════════════
# test_connection / _test_north_adapter
# ═══════════════════════════════════════════════════════════════════════════
class TestTestConnection:
    async def test_unsupported_platform(self):
        with pytest.raises(ValueError, match="Unsupported platform"):
            await PlatformService().test_connection("nonexistent", {})

    async def test_validation_error(self):
        with pytest.raises(ValueError, match="ERR_PLATFORM_VALIDATION"):
            await PlatformService().test_connection("iotsharp", {"broker": "!!", "port": 1883, "device_token": "t"})

    async def test_north_adapter_success(self):
        mock_adapter_cls = _make_mock_adapter_cls("iotsharp", connected_after_start=True)
        mock_module = MagicMock()
        mock_module.IoTSharpNorthAdapter = mock_adapter_cls

        with patch(
            "edgelite.services.platform_service.importlib.import_module",
            return_value=mock_module,
        ):
            result = await PlatformService().test_connection("iotsharp", _valid_iotsharp_config())
            assert result["success"] is True
            assert "successful" in result["message"]

    async def test_north_adapter_not_connected(self):
        mock_adapter_cls = _make_mock_adapter_cls("iotsharp", connected_after_start=False)
        mock_module = MagicMock()
        mock_module.IoTSharpNorthAdapter = mock_adapter_cls

        with patch(
            "edgelite.services.platform_service.importlib.import_module",
            return_value=mock_module,
        ):
            result = await PlatformService().test_connection("iotsharp", _valid_iotsharp_config())
            assert result["success"] is False

    async def test_north_adapter_start_exception(self):
        class _FailAdapter(_BaseNorthAdapter):
            async def start(self, config):
                raise RuntimeError("boom")

        mock_module = MagicMock()
        mock_module.IoTSharpNorthAdapter = _FailAdapter

        with patch(
            "edgelite.services.platform_service.importlib.import_module",
            return_value=mock_module,
        ):
            result = await PlatformService().test_connection("iotsharp", _valid_iotsharp_config())
            assert result["success"] is False
            assert "check logs" in result["message"]

    async def test_north_adapter_start_exception_stop_also_fails(self):
        class _FailAdapter(_BaseNorthAdapter):
            async def start(self, config):
                raise RuntimeError("boom")

            async def stop(self):
                raise RuntimeError("stop also boom")

        mock_module = MagicMock()
        mock_module.IoTSharpNorthAdapter = _FailAdapter

        with patch(
            "edgelite.services.platform_service.importlib.import_module",
            return_value=mock_module,
        ):
            result = await PlatformService().test_connection("iotsharp", _valid_iotsharp_config())
            assert result["success"] is False

    async def test_handler_path_success(self):
        mock_handler_cls = _make_mock_handler_cls("iotsharp")

        def import_side_effect(name):
            if "north_adapters" in name:
                raise ImportError("no adapters")
            mod = MagicMock()
            mod.IoTSharpHandler = mock_handler_cls
            return mod

        with patch(
            "edgelite.services.platform_service.importlib.import_module",
            side_effect=import_side_effect,
        ):
            result = await PlatformService().test_connection("iotsharp", _valid_iotsharp_config())
            assert result["success"] is True

    async def test_handler_path_timeout(self):
        """handler connect 不设置 _connected → 超时返回失败"""

        class _SlowHandler:
            platform_name = "iotsharp"

            def __init__(self):
                self._connected = False

            async def connect(self, config):
                pass  # never sets _connected

            async def disconnect(self):
                pass

        def import_side_effect(name):
            if "north_adapters" in name:
                raise ImportError("no adapters")
            mod = MagicMock()
            mod.IoTSharpHandler = _SlowHandler
            return mod

        with (
            patch(
                "edgelite.services.platform_service.importlib.import_module",
                side_effect=import_side_effect,
            ),
            patch.object(asyncio, "sleep", new=AsyncMock()),
        ):
            result = await PlatformService().test_connection("iotsharp", _valid_iotsharp_config())
            assert result["success"] is False
            assert "timed out" in result["message"].lower() or "timed out" in result["message"]

    async def test_handler_path_connect_exception(self):
        class _FailHandler:
            platform_name = "iotsharp"

            def __init__(self):
                self._connected = False

            async def connect(self, config):
                raise OSError("network error")

            async def disconnect(self):
                pass

        def import_side_effect(name):
            if "north_adapters" in name:
                raise ImportError("no adapters")
            mod = MagicMock()
            mod.IoTSharpHandler = _FailHandler
            return mod

        with patch(
            "edgelite.services.platform_service.importlib.import_module",
            side_effect=import_side_effect,
        ):
            result = await PlatformService().test_connection("iotsharp", _valid_iotsharp_config())
            assert result["success"] is False

    async def test_handler_path_import_error_returns_missing_dependency(self):
        def import_side_effect(name):
            if "north_adapters" in name:
                raise ImportError("no adapters")
            raise ImportError("missing_xyz_module")

        with patch(
            "edgelite.services.platform_service.importlib.import_module",
            side_effect=import_side_effect,
        ):
            result = await PlatformService().test_connection("iotsharp", _valid_iotsharp_config())
            assert result["success"] is False

    async def test_handler_load_attribute_error(self):
        def import_side_effect(name):
            if "north_adapters" in name:
                raise ImportError("no adapters")
            # spec=[] 使 getattr(mod, "IoTSharpHandler") 抛出 AttributeError，
            # 被 test_connection 中的 except (ImportError, AttributeError) 捕获
            mod = MagicMock(spec=[])
            return mod

        with patch(
            "edgelite.services.platform_service.importlib.import_module",
            side_effect=import_side_effect,
        ):
            result = await PlatformService().test_connection("iotsharp", _valid_iotsharp_config())
            assert result["success"] is False

    async def test_north_adapter_import_succeeds_but_not_subclass_falls_to_handler(self):
        class _NonAdapter:
            pass

        mock_na_module = MagicMock()
        mock_na_module.IoTSharpNorthAdapter = _NonAdapter
        mock_handler_cls = _make_mock_handler_cls("iotsharp")

        def import_side_effect(name):
            if "north_adapters" in name:
                return mock_na_module
            mod = MagicMock()
            mod.IoTSharpHandler = mock_handler_cls
            return mod

        with patch(
            "edgelite.services.platform_service.importlib.import_module",
            side_effect=import_side_effect,
        ):
            result = await PlatformService().test_connection("iotsharp", _valid_iotsharp_config())
            assert result["success"] is True


# ═══════════════════════════════════════════════════════════════════════════
# 代理方法：get_message_preview / get_broker_quality / get_tb_* / get_platform_*
# ═══════════════════════════════════════════════════════════════════════════
class TestProxyMethods:
    def test_get_message_preview_with_adapter(self):
        svc = PlatformService()
        adapter = MagicMock()
        adapter.get_message_preview.return_value = [{"topic": "t"}]
        svc._adapters["x"] = adapter
        assert svc.get_message_preview("x") == [{"topic": "t"}]

    def test_get_message_preview_no_adapter(self):
        assert PlatformService().get_message_preview("x") == []

    def test_get_message_preview_adapter_no_method(self):
        svc = PlatformService()
        adapter = MagicMock(spec=[])  # no get_message_preview
        svc._adapters["x"] = adapter
        assert svc.get_message_preview("x") == []

    def test_get_broker_quality_with_adapter(self):
        svc = PlatformService()
        adapter = MagicMock()
        adapter.get_broker_quality.return_value = {"avg_latency_ms": 10.0}
        svc._adapters["x"] = adapter
        assert svc.get_broker_quality("x") == {"avg_latency_ms": 10.0}

    def test_get_broker_quality_no_adapter_returns_defaults(self):
        result = PlatformService().get_broker_quality("x")
        assert result["avg_latency_ms"] == 0.0
        assert result["packet_loss_count"] == 0
        assert result["samples"] == 0

    def test_get_tb_devices_with_adapter(self):
        svc = PlatformService()
        adapter = MagicMock()
        adapter.get_device_list.return_value = [{"id": "d1"}]
        svc._adapters["x"] = adapter
        assert svc.get_tb_devices("x") == [{"id": "d1"}]

    def test_get_tb_devices_no_adapter(self):
        assert PlatformService().get_tb_devices("x") == []

    def test_get_tb_rpc_logs_with_adapter(self):
        svc = PlatformService()
        adapter = MagicMock()
        adapter.get_rpc_logs.return_value = [{"rpc": 1}]
        svc._adapters["x"] = adapter
        assert svc.get_tb_rpc_logs("x") == [{"rpc": 1}]

    def test_get_tb_rpc_logs_no_adapter(self):
        assert PlatformService().get_tb_rpc_logs("x") == []

    def test_get_tb_alarm_records_with_adapter(self):
        svc = PlatformService()
        adapter = MagicMock()
        adapter.get_alarm_records.return_value = [{"alarm": 1}]
        svc._adapters["x"] = adapter
        assert svc.get_tb_alarm_records("x") == [{"alarm": 1}]

    def test_get_tb_alarm_records_no_adapter(self):
        assert PlatformService().get_tb_alarm_records("x") == []

    def test_get_tb_sync_status_with_adapter(self):
        svc = PlatformService()
        adapter = MagicMock()
        adapter.get_sync_status.return_value = {"total_devices": 5}
        svc._adapters["x"] = adapter
        assert svc.get_tb_sync_status("x") == {"total_devices": 5}

    def test_get_tb_sync_status_no_adapter_returns_defaults(self):
        result = PlatformService().get_tb_sync_status("x")
        assert result["total_devices"] == 0
        assert result["registered_devices"] == 0
        assert result["rpc_pending"] == 0

    def test_get_platform_shadow_with_adapter(self):
        svc = PlatformService()
        adapter = MagicMock()
        adapter.get_shadow_cache.return_value = {"key": "val"}
        svc._adapters["x"] = adapter
        assert svc.get_platform_shadow("x") == {"key": "val"}

    def test_get_platform_shadow_no_adapter(self):
        assert PlatformService().get_platform_shadow("x") == {}

    def test_get_platform_command_logs_with_adapter(self):
        svc = PlatformService()
        adapter = MagicMock()
        adapter.get_command_logs.return_value = [{"cmd": 1}]
        svc._adapters["x"] = adapter
        assert svc.get_platform_command_logs("x") == [{"cmd": 1}]

    def test_get_platform_command_logs_no_adapter(self):
        assert PlatformService().get_platform_command_logs("x") == []

    def test_get_platform_alarm_records_with_adapter(self):
        svc = PlatformService()
        adapter = MagicMock()
        adapter.get_alarm_records.return_value = [{"a": 1}]
        svc._adapters["x"] = adapter
        assert svc.get_platform_alarm_records("x") == [{"a": 1}]

    def test_get_platform_alarm_records_no_adapter(self):
        assert PlatformService().get_platform_alarm_records("x") == []

    def test_get_platform_device_mapping_with_adapter(self):
        svc = PlatformService()
        adapter = MagicMock()
        adapter.get_device_mapping.return_value = [{"d": "m"}]
        svc._adapters["x"] = adapter
        assert svc.get_platform_device_mapping("x") == [{"d": "m"}]

    def test_get_platform_device_mapping_no_adapter(self):
        assert PlatformService().get_platform_device_mapping("x") == []

    def test_get_broker_status_with_adapter(self):
        svc = PlatformService()
        adapter = MagicMock()
        adapter.get_broker_status.return_value = [{"broker": "b1"}]
        svc._adapters["x"] = adapter
        assert svc.get_broker_status("x") == [{"broker": "b1"}]

    def test_get_broker_status_no_adapter(self):
        assert PlatformService().get_broker_status("x") == []


# ═══════════════════════════════════════════════════════════════════════════
# get_dashboard_data / get_north_metrics
# ═══════════════════════════════════════════════════════════════════════════
class TestDashboardAndMetrics:
    async def test_dashboard_data_empty_only_registry(self):
        svc = PlatformService()
        result = await svc.get_dashboard_data()
        # 所有注册平台都应出现，状态 disconnected
        names = [d["platform_name"] for d in result]
        assert "iotsharp" in names
        for entry in result:
            assert entry["connected"] is False
            assert entry["state"] == "disconnected"

    async def test_dashboard_data_with_adapter(self):
        svc = PlatformService()
        adapter = _BaseNorthAdapter()
        adapter.platform_name = "iotsharp"
        adapter._connected = True
        svc._adapters["iotsharp"] = adapter
        result = await svc.get_dashboard_data()
        iotsharp_entry = next(d for d in result if d["platform_name"] == "iotsharp")
        assert iotsharp_entry["connected"] is True

    async def test_dashboard_data_with_handler(self):
        svc = PlatformService()
        h = MagicMock()
        h._connected = True
        svc._handlers["iotsharp"] = h
        result = await svc.get_dashboard_data()
        iotsharp_entry = next(d for d in result if d["platform_name"] == "iotsharp")
        assert iotsharp_entry["connected"] is True
        assert iotsharp_entry["state"] == "unknown"

    async def test_dashboard_data_adapter_and_handler_different_names(self):
        svc = PlatformService()
        adapter = _BaseNorthAdapter()
        adapter.platform_name = "iotsharp"
        adapter._connected = True
        svc._adapters["iotsharp"] = adapter
        h = MagicMock()
        h._connected = True
        svc._handlers["thingsboard"] = h
        result = await svc.get_dashboard_data()
        names = {d["platform_name"] for d in result}
        assert "iotsharp" in names
        assert "thingsboard" in names

    def test_get_north_metrics_empty(self):
        assert PlatformService().get_north_metrics() == ""

    def test_get_north_metrics_with_adapters(self):
        svc = PlatformService()
        a1 = MagicMock()
        a1.get_prometheus_metrics.return_value = "# m1"
        a2 = MagicMock()
        a2.get_prometheus_metrics.return_value = "# m2"
        svc._adapters["a"] = a1
        svc._adapters["b"] = a2
        result = svc.get_north_metrics()
        assert "# m1" in result
        assert "# m2" in result


# ═══════════════════════════════════════════════════════════════════════════
# export_config / import_config / _flatten_custom_config
# ═══════════════════════════════════════════════════════════════════════════
class TestExportConfig:
    def test_export_no_adapter_no_config_returns_empty(self):
        result = PlatformService().export_config("iotsharp")
        assert result["platform_name"] == "iotsharp"
        assert result["config"] == {}
        assert "exported_at" in result

    def test_export_from_adapter_configs(self):
        svc = PlatformService()
        nc = _build_north_config("iotsharp", {"broker": "h", "password": "secret", "port": 1883, "device_token": "t"})
        svc._adapter_configs["iotsharp"] = nc
        result = svc.export_config("iotsharp")
        cfg = result["config"]
        # password 在 mqtt 子配置中应被脱敏
        mqtt_cfg = cfg.get("mqtt", {})
        assert mqtt_cfg.get("password") == "***"
        assert "exported_at" in result

    def test_export_from_adapter_custom_config(self):
        svc = PlatformService()
        adapter = MagicMock()
        adapter.get_custom_config.return_value = {"broker": "h", "password": "p123", "name": "x"}
        adapter.platform_version = "2.0"
        svc._adapters["custom"] = adapter
        result = svc.export_config("custom")
        assert result["version"] == "2.0"
        assert result["config"]["password"] == "***"
        assert result["config"]["name"] == "x"

    def test_export_adapter_custom_config_empty_falls_to_adapter_configs(self):
        svc = PlatformService()
        adapter = MagicMock()
        adapter.get_custom_config.return_value = None
        svc._adapters["iotsharp"] = adapter
        nc = _build_north_config("iotsharp", {"broker": "h", "port": 1883, "device_token": "t"})
        svc._adapter_configs["iotsharp"] = nc
        result = svc.export_config("iotsharp")
        assert "config" in result
        assert result["config"]  # not empty

    def test_export_adapter_without_get_custom_config(self):
        svc = PlatformService()
        adapter = MagicMock(spec=[])  # no get_custom_config
        svc._adapters["iotsharp"] = adapter
        nc = _build_north_config("iotsharp", {"broker": "h", "port": 1883, "device_token": "t"})
        svc._adapter_configs["iotsharp"] = nc
        result = svc.export_config("iotsharp")
        assert "config" in result


class TestImportConfig:
    def test_unsupported_platform_raises(self):
        with pytest.raises(ValueError, match="Unsupported platform"):
            PlatformService().import_config("nonexistent", {})

    def test_import_valid_config(self):
        cfg = {"broker": "h", "port": 1883, "device_token": "t"}
        result = PlatformService().import_config("iotsharp", {"config": cfg})
        assert result["status"] == "imported"
        assert result["config"] == cfg

    def test_import_flat_config(self):
        cfg = {"broker": "h", "port": 1883, "device_token": "t"}
        result = PlatformService().import_config("iotsharp", cfg)
        assert result["status"] == "imported"

    def test_import_invalid_config_raises(self):
        cfg = {"broker": "!!invalid", "port": 1883, "device_token": "t"}
        with pytest.raises(ValueError, match="ERR_PLATFORM_VALIDATION"):
            PlatformService().import_config("iotsharp", cfg)

    def test_import_nested_custom_config_flattened(self):
        nested = {
            "config": {
                "brokers": [{"broker_host": "gw.example.com", "broker_port": 1883, "username": "u", "password": "p"}],
                "template": {"topic_template": "{prefix}/{device_id}"},
                "script": {"enabled": True, "script": "function transform(){}"},
                "gateway_id": "gw-1",
                "payload_format": "custom",
                "qos_policy": {"default_qos": 1, "alarm_qos": 2},
            }
        }
        result = PlatformService().import_config("custom", nested)
        flat = result["config"]
        assert flat["broker"] == "gw.example.com"
        assert flat["port"] == 1883
        assert flat["username"] == "u"
        assert flat["password"] == "p"
        assert flat["topic_template"] == "{prefix}/{device_id}"
        assert flat["script_enabled"] == "true"
        assert flat["script_code"] == "function transform(){}"
        assert flat["gateway_id"] == "gw-1"
        assert flat["default_qos"] == 1
        assert flat["alarm_qos"] == 2


class TestFlattenCustomConfig:
    def test_flatten_empty(self):
        result = PlatformService()._flatten_custom_config({})
        assert result == {}

    def test_flatten_brokers_extraction(self):
        nested = {"brokers": [{"broker_host": "h", "broker_port": 1883, "username": "u", "password": "p"}]}
        result = PlatformService()._flatten_custom_config(nested)
        assert result["broker"] == "h"
        assert result["port"] == 1883
        assert result["username"] == "u"
        assert result["password"] == "p"
        assert result["brokers"] == nested["brokers"]

    def test_flatten_brokers_empty_list(self):
        result = PlatformService()._flatten_custom_config({"brokers": []})
        assert "broker" not in result

    def test_flatten_template_keys(self):
        nested = {
            "template": {
                "topic_template": "t",
                "payload_template": "p",
                "batch_payload_template": "b",
                "status_topic_template": "s",
                "topic_prefix": "pre",
            }
        }
        result = PlatformService()._flatten_custom_config(nested)
        assert result["topic_template"] == "t"
        assert result["payload_template"] == "p"
        assert result["batch_payload_template"] == "b"
        assert result["status_topic_template"] == "s"
        assert result["topic_prefix"] == "pre"

    def test_flatten_script_keys(self):
        nested = {"script": {"enabled": True, "script": "code", "script_language": "js"}}
        result = PlatformService()._flatten_custom_config(nested)
        assert result["script_enabled"] == "true"
        assert result["script_code"] == "code"
        assert result["script_language"] == "js"

    def test_flatten_top_level_keys(self):
        nested = {
            "gateway_id": "gw",
            "payload_format": "json",
            "enable_compression": "true",
            "compress_threshold": 512,
            "dedup_window_seconds": 60,
        }
        result = PlatformService()._flatten_custom_config(nested)
        assert result["gateway_id"] == "gw"
        assert result["payload_format"] == "json"
        assert result["enable_compression"] == "true"
        assert result["compress_threshold"] == 512
        assert result["dedup_window_seconds"] == 60

    def test_flatten_user_properties(self):
        nested = {"user_properties": [{"k": "v"}]}
        result = PlatformService()._flatten_custom_config(nested)
        assert result["mqtt5_user_properties"] == [{"k": "v"}]

    def test_flatten_qos_policy(self):
        nested = {"qos_policy": {"default_qos": 2, "alarm_qos": 1}}
        result = PlatformService()._flatten_custom_config(nested)
        assert result["default_qos"] == 2
        assert result["alarm_qos"] == 1

    def test_flatten_brokers_not_list_skipped(self):
        nested = {"brokers": "not_a_list"}
        result = PlatformService()._flatten_custom_config(nested)
        assert "broker" not in result


# ═══════════════════════════════════════════════════════════════════════════
# 模板与脚本
# ═══════════════════════════════════════════════════════════════════════════
class TestTemplateValidation:
    def test_validate_topic_template_valid(self):
        result = PlatformService().validate_topic_template("{prefix}/{device_id}")
        assert result["valid"] is True
        assert result["errors"] == []
        assert "prefix" in result["variables"]
        assert "device_id" in result["variables"]

    def test_validate_topic_template_invalid(self):
        result = PlatformService().validate_topic_template("{unclosed")
        assert result["valid"] is False
        assert len(result["errors"]) > 0

    def test_validate_advanced_template_valid(self):
        result = PlatformService().validate_advanced_template("template content")
        assert result["valid"] is True
        assert result["template_type"] == "payload"

    def test_validate_advanced_template_with_type(self):
        result = PlatformService().validate_advanced_template("t", template_type="topic")
        assert result["template_type"] == "topic"

    def test_validate_advanced_template_empty(self):
        result = PlatformService().validate_advanced_template("")
        assert result["valid"] is False

    def test_preview_template_payload(self):
        result = PlatformService().preview_template("tpl", {"value": 1})
        assert result["success"] is True
        assert "result" in result

    def test_preview_template_topic(self):
        result = PlatformService().preview_template("tpl", {"value": 1}, template_type="topic")
        assert result["success"] is True

    def test_preview_template_batch_payload(self):
        result = PlatformService().preview_template(
            "tpl", {"points": [{"value": 1}, {"value": 2}]}, template_type="batch_payload"
        )
        assert result["success"] is True

    def test_preview_template_exception(self):
        with patch(
            "edgelite.platform.mqtt_utils.AdvancedTemplateEngine.render_payload",
            side_effect=RuntimeError("render fail"),
        ):
            result = PlatformService().preview_template("tpl", {})
            assert result["success"] is False
            assert "error" in result


class TestScriptValidation:
    def test_validate_script_valid(self):
        result = PlatformService().validate_script("function transform() { return 1; }")
        assert result["valid"] is True
        assert result["errors"] == []

    def test_validate_script_empty(self):
        result = PlatformService().validate_script("")
        assert result["valid"] is False

    async def test_test_script_success(self):
        result = await PlatformService().test_script("code", {"val": 1}, {"ctx": "x"})
        assert result["success"] is True
        assert "data" in result
        assert "execution_ms" in result

    async def test_test_script_no_context(self):
        result = await PlatformService().test_script("code", {"val": 1})
        assert result["success"] is True


# ═══════════════════════════════════════════════════════════════════════════
# mqtt_test_publish
# ═══════════════════════════════════════════════════════════════════════════
class TestMqttTestPublish:
    async def test_wildcard_hash_rejected(self):
        result = await PlatformService().mqtt_test_publish("x", "topic/#", "payload")
        assert result["success"] is False
        assert "wildcards" in result["message"]

    async def test_wildcard_plus_rejected(self):
        result = await PlatformService().mqtt_test_publish("x", "topic/+", "payload")
        assert result["success"] is False
        assert "wildcards" in result["message"]

    async def test_adapter_not_found(self):
        result = await PlatformService().mqtt_test_publish("nonexistent", "topic/a", "payload")
        assert result["success"] is False
        assert "error" in result

    async def test_adapter_not_connected(self):
        svc = PlatformService()
        adapter = MagicMock()
        adapter._connected = False
        svc._adapters["x"] = adapter
        result = await svc.mqtt_test_publish("x", "topic/a", "payload")
        assert result["success"] is False

    async def test_no_mqtt_client(self):
        svc = PlatformService()
        adapter = MagicMock()
        adapter._connected = True
        adapter._mqtt_client = None
        svc._adapters["x"] = adapter
        result = await svc.mqtt_test_publish("x", "topic/a", "payload")
        assert result["success"] is False
        assert "MQTT client" in result["error"]

    async def test_publish_success(self):
        svc = PlatformService()
        mqtt_client = MagicMock()
        mqtt_client.publish = AsyncMock()
        adapter = MagicMock()
        adapter._connected = True
        adapter._mqtt_client = mqtt_client
        svc._adapters["x"] = adapter
        result = await svc.mqtt_test_publish("x", "topic/a", "payload", qos=1)
        assert result["success"] is True
        mqtt_client.publish.assert_awaited_once_with("topic/a", b"payload", qos=1)

    async def test_publish_exception(self):
        svc = PlatformService()
        mqtt_client = MagicMock()
        mqtt_client.publish = AsyncMock(side_effect=RuntimeError("publish err"))
        adapter = MagicMock()
        adapter._connected = True
        adapter._mqtt_client = mqtt_client
        svc._adapters["x"] = adapter
        result = await svc.mqtt_test_publish("x", "topic/a", "payload")
        assert result["success"] is False
        assert "publish err" in result["error"]

    async def test_publish_default_qos(self):
        svc = PlatformService()
        mqtt_client = MagicMock()
        mqtt_client.publish = AsyncMock()
        adapter = MagicMock()
        adapter._connected = True
        adapter._mqtt_client = mqtt_client
        svc._adapters["x"] = adapter
        await svc.mqtt_test_publish("x", "topic/a", "payload")
        mqtt_client.publish.assert_awaited_once_with("topic/a", b"payload", qos=0)


# ═══════════════════════════════════════════════════════════════════════════
# reload_config
# ═══════════════════════════════════════════════════════════════════════════
class TestReloadConfig:
    async def test_platform_not_found_raises_key_error(self):
        svc = PlatformService()
        with pytest.raises(KeyError, match="not found"):
            await svc.reload_config("nonexistent", {})

    async def test_validation_error_raises_value_error(self):
        svc = PlatformService()
        svc._adapters["iotsharp"] = _BaseNorthAdapter()
        with pytest.raises(ValueError, match="ERR_PLATFORM_VALIDATION"):
            await svc.reload_config("iotsharp", {"broker": "!!", "port": 1883, "device_token": "t"})

    async def test_reload_adapter_success(self):
        svc = PlatformService()
        adapter = _BaseNorthAdapter()
        adapter._connected = True
        svc._adapters["iotsharp"] = adapter
        old_config = MagicMock()
        svc._adapter_configs["iotsharp"] = old_config
        result = await svc.reload_config("iotsharp", _valid_iotsharp_config())
        assert result["status"] == "reloaded"
        assert "connected" in result
        assert svc._adapter_configs["iotsharp"] is not old_config

    async def test_reload_adapter_start_failure_removes_adapter(self):
        svc = PlatformService()
        adapter = _BaseNorthAdapter()
        adapter.stop = AsyncMock()
        adapter.start = AsyncMock(side_effect=RuntimeError("start fail"))
        svc._adapters["iotsharp"] = adapter
        svc._adapter_configs["iotsharp"] = MagicMock()
        with pytest.raises(RuntimeError, match="Config reload failed"):
            await svc.reload_config("iotsharp", _valid_iotsharp_config())
        assert "iotsharp" not in svc.adapters
        assert "iotsharp" not in svc._adapter_configs

    async def test_reload_adapter_stop_exception_continues(self):
        svc = PlatformService()
        adapter = _BaseNorthAdapter()
        adapter.stop = AsyncMock(side_effect=RuntimeError("stop err"))
        adapter.start = AsyncMock()
        svc._adapters["iotsharp"] = adapter
        result = await svc.reload_config("iotsharp", _valid_iotsharp_config())
        assert result["status"] == "reloaded"

    async def test_reload_handler_returns_not_supported(self):
        svc = PlatformService()
        h = MagicMock()
        svc._handlers["iotsharp"] = h
        result = await svc.reload_config("iotsharp", _valid_iotsharp_config())
        assert result == {"status": "reload_not_supported"}

    async def test_reload_handler_validation_error_raises(self):
        svc = PlatformService()
        svc._handlers["iotsharp"] = MagicMock()
        with pytest.raises(ValueError, match="ERR_PLATFORM_VALIDATION"):
            await svc.reload_config("iotsharp", {"broker": "!!", "port": 1883, "device_token": "t"})
