"""sqlite_repo 扩展单元测试（文件1）：校验器、BaseRepo、DeviceRepo、TemplateRepo、RuleRepo。"""

import sys
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

sys.path.insert(0, "src")

from edgelite.api.error_codes import RepoErrors  # noqa: E402
from edgelite.models.db import (  # noqa: E402
    Base,
    DeviceORM,
    DeviceTemplateORM,
    RuleORM,
    StaleDataError,
)
from edgelite.storage.sqlite_repo import (  # noqa: E402
    BaseRepo,
    DeviceRepo,
    RuleRepo,
    TemplateRepo,
    _get_optimistic_lock_retries,
    _orm_to_alarm,
    _orm_to_device,
    _orm_to_rule,
    _orm_to_template,
    _orm_to_user,
    _orm_to_user_full,
    _orm_to_user_safe,
    _safe_json_loads,
    _validate_alarm_data,
    _validate_device_config,
    _validate_device_data,
    _validate_device_update_data,
    _validate_dns_resolution,
    _validate_ipv4_or_hostname,
    _validate_points,
    _validate_rule_data,
    _validate_rule_update_data,
    _validate_template_data,
    _validate_template_update_data,
    _validate_trigger_value,
    _validate_user_data,
    _validate_user_update_data,
    retry_on_stale,
)

# ──────────────────────────────── 夹具 ────────────────────────────────


@pytest_asyncio.fixture
async def db_session():
    """内存 SQLite 会话，每测试独立。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    session = factory()
    yield session
    await session.close()
    await engine.dispose()


@pytest_asyncio.fixture
async def device_repo(db_session):
    return DeviceRepo(db_session)


@pytest_asyncio.fixture
async def rule_repo(db_session):
    return RuleRepo(db_session)


@pytest_asyncio.fixture
async def template_repo(db_session):
    return TemplateRepo(db_session)


# ──────────────────────────────── 辅助 ────────────────────────────────


def _dev(device_id="d1", **kw):
    d = {
        "device_id": device_id,
        "name": "Device",
        "protocol": "simulator",
        "status": "offline",
        "config": {},
        "points": [],
        "collect_interval": 5,
    }
    d.update(kw)
    return d


def _rule(name="r1", **kw):
    r = {
        "name": name,
        "device_id": "d1",
        "conditions": [{"point": "temp", "operator": ">", "threshold": 30}],
        "logic": "AND",
        "duration": 0,
        "severity": "warning",
        "enabled": True,
        "notify_channels": [],
    }
    r.update(kw)
    return r


# ════════════════════ 模块级辅助函数 ════════════════════


class TestSafeJsonLoads:
    """_safe_json_loads 安全 JSON 解析。"""

    def test_dict_string(self):
        assert _safe_json_loads('{"a": 1}') == {"a": 1}

    def test_list_string(self):
        assert _safe_json_loads("[1, 2]") == [1, 2]

    def test_corrupt_returns_default(self):
        assert _safe_json_loads("{bad", default={}) == {}

    def test_non_string_passthrough(self):
        assert _safe_json_loads({"x": 1}) == {"x": 1}
        assert _safe_json_loads(42) == 42
        assert _safe_json_loads(None, default="d") is None

    def test_corrupt_increments_counter(self):
        from edgelite.storage import sqlite_repo as mod

        before = mod._corrupt_json_count
        _safe_json_loads("not json", default=None)
        assert mod._corrupt_json_count == before + 1


class TestOptimisticLockRetries:
    """_get_optimistic_lock_retries 从配置读取。"""

    def test_default(self):
        assert _get_optimistic_lock_retries() >= 1

    def test_config_error_fallback(self):
        with patch("edgelite.config.get_config", side_effect=RuntimeError("boom")):
            assert _get_optimistic_lock_retries() == 3


class TestRetryOnStale:
    """retry_on_stale 装饰器行为。"""

    async def test_success_no_retry(self):
        calls = 0

        @retry_on_stale(max_retries=3, base_delay=0.001)
        async def fn():
            nonlocal calls
            calls += 1
            return "ok"

        assert await fn() == "ok"
        assert calls == 1

    async def test_retries_then_succeeds(self):
        calls = 0

        @retry_on_stale(max_retries=3, base_delay=0.001)
        async def fn():
            nonlocal calls
            calls += 1
            if calls < 2:
                raise StaleDataError("conflict")
            return "ok"

        assert await fn() == "ok"
        assert calls == 2

    async def test_exhausted_raises(self):
        @retry_on_stale(max_retries=2, base_delay=0.001)
        async def fn():
            raise StaleDataError("conflict")

        with pytest.raises(StaleDataError):
            await fn()


class TestHostValidators:
    """_validate_ipv4_or_hostname / _validate_dns_resolution。"""

    def test_valid_ip(self):
        _validate_ipv4_or_hostname("192.168.1.1", "host")

    def test_valid_hostname(self):
        _validate_ipv4_or_hostname("plc-01.local", "host")

    def test_empty_rejected(self):
        with pytest.raises(ValueError):
            _validate_ipv4_or_hostname("", "host")

    def test_non_string_rejected(self):
        with pytest.raises(ValueError):
            _validate_ipv4_or_hostname(123, "host")

    def test_invalid_chars_rejected(self):
        with pytest.raises(ValueError):
            _validate_ipv4_or_hostname("host!bad", "host")

    def test_dns_invalid_chars(self):
        with pytest.raises(ValueError):
            _validate_dns_resolution("host;rm", "host")


# ════════════════════ 设备配置校验器 ════════════════════


class TestValidateDeviceConfig:
    """_validate_device_config 各协议分支覆盖。"""

    def test_empty_config_ok(self):
        _validate_device_config({}, "modbus_tcp")
        _validate_device_config(None, "simulator")

    def test_non_dict_rejected(self):
        with pytest.raises(ValueError):
            _validate_device_config("not dict", "simulator")

    def test_modbus_tcp_valid(self):
        _validate_device_config({"host": "192.168.1.1", "port": 502, "unit_id": 1}, "modbus_tcp")

    def test_modbus_tcp_bad_port(self):
        with pytest.raises(ValueError):
            _validate_device_config({"port": 99999}, "modbus_tcp")

    def test_modbus_tcp_bad_unit_id(self):
        with pytest.raises(ValueError):
            _validate_device_config({"unit_id": 300}, "modbus_tcp")

    def test_modbus_tcp_bad_timeout(self):
        with pytest.raises(ValueError):
            _validate_device_config({"timeout": -1}, "modbus_tcp")

    def test_modbus_tcp_bad_retries(self):
        with pytest.raises(ValueError):
            _validate_device_config({"retries": -1}, "modbus_tcp")

    def test_modbus_tcp_bad_mode(self):
        with pytest.raises(ValueError):
            _validate_device_config({"mode": "bad"}, "modbus_tcp")

    def test_modbus_tcp_bad_host_type(self):
        with pytest.raises(ValueError):
            _validate_device_config({"host": 123}, "modbus_tcp")

    def test_modbus_rtu_requires_serial_port(self):
        with pytest.raises(ValueError, match="serial_port"):
            _validate_device_config({"port": 1}, "modbus_rtu")

    def test_modbus_rtu_valid(self):
        _validate_device_config(
            {"serial_port": "/dev/ttyS0", "baudrate": 9600, "parity": "N", "stopbits": 1},
            "modbus_rtu",
        )

    def test_modbus_rtu_bad_baud(self):
        with pytest.raises(ValueError):
            _validate_device_config({"serial_port": "/dev/ttyS0", "baudrate": -1}, "modbus_rtu")

    def test_modbus_rtu_bad_parity(self):
        with pytest.raises(ValueError):
            _validate_device_config({"serial_port": "/dev/ttyS0", "parity": "X"}, "modbus_rtu")

    def test_modbus_rtu_bad_stopbits(self):
        with pytest.raises(ValueError):
            _validate_device_config({"serial_port": "/dev/ttyS0", "stopbits": 3}, "modbus_rtu")

    def test_s7_valid(self):
        _validate_device_config({"ip": "10.0.0.1", "rack": 0, "slot": 1, "pdu_size": 240}, "siemens_s7")

    def test_s7_bad_rack(self):
        with pytest.raises(ValueError):
            _validate_device_config({"rack": 99}, "siemens_s7")

    def test_s7_bad_plc_model(self):
        with pytest.raises(ValueError):
            _validate_device_config({"plc_model": "S7-999"}, "siemens_s7")

    def test_s7_bad_password_len(self):
        with pytest.raises(ValueError):
            _validate_device_config({"password": "short"}, "siemens_s7")

    def test_s7_valid_password(self):
        _validate_device_config({"password": "abcd1234"}, "siemens_s7")

    def test_mc_valid(self):
        _validate_device_config(
            {"host": "192.168.1.2", "port": 5000, "network": 1, "station": 2, "cpu_type": "Q"}, "mitsubishi_mc"
        )

    def test_mc_bad_cpu_type(self):
        with pytest.raises(ValueError):
            _validate_device_config({"cpu_type": "Z"}, "mitsubishi_mc")

    def test_mc_bad_network(self):
        with pytest.raises(ValueError):
            _validate_device_config({"network": 999}, "mitsubishi_mc")

    def test_fins_valid(self):
        _validate_device_config({"host": "10.0.0.5", "port": 9600, "network": 1, "node": 2, "unit": 3}, "omron_fins")

    def test_fins_bad_node(self):
        with pytest.raises(ValueError):
            _validate_device_config({"node": 999}, "omron_fins")

    def test_ab_valid(self):
        _validate_device_config({"host": "10.0.0.3", "port": 44818, "slot": 0}, "allen_bradley")

    def test_ab_bad_slot(self):
        with pytest.raises(ValueError):
            _validate_device_config({"slot": 99}, "allen_bradley")

    def test_opcua_valid(self):
        _validate_device_config(
            {"endpoint": "opc.tcp://localhost:4840", "namespace_index": 1, "security_mode": "None"}, "opc_ua"
        )

    def test_opcua_bad_endpoint(self):
        with pytest.raises(ValueError):
            _validate_device_config({"endpoint": "http://bad"}, "opc_ua")

    def test_opcua_bad_security_mode(self):
        with pytest.raises(ValueError):
            _validate_device_config({"security_mode": "Bad"}, "opc_ua")

    def test_opcua_bad_security_policy(self):
        with pytest.raises(ValueError):
            _validate_device_config({"security_policy": "Unknown"}, "opc_ua")

    def test_opcda_valid(self):
        _validate_device_config({"host": "10.0.0.4", "prog_id": "Graybox.Simulator"}, "opc_da")

    def test_opcda_bad_prog_id(self):
        with pytest.raises(ValueError):
            _validate_device_config({"prog_id": 123}, "opc_da")

    def test_mqtt_valid(self):
        _validate_device_config(
            {"broker": "192.168.1.10", "port": 1883, "qos": 1, "keepalive": 60, "topic": "test/t"}, "mqtt_client"
        )

    def test_mqtt_bad_qos(self):
        with pytest.raises(ValueError):
            _validate_device_config({"qos": 5}, "mqtt_client")

    def test_mqtt_bad_topic(self):
        with pytest.raises(ValueError):
            _validate_device_config({"topic": 123}, "mqtt_client")

    def test_mqtt_bad_keepalive(self):
        with pytest.raises(ValueError):
            _validate_device_config({"keepalive": -1}, "mqtt_client")

    def test_http_webhook_valid(self):
        _validate_device_config({"url": "https://hook.example.com/cb", "method": "POST", "timeout": 30}, "http_webhook")

    def test_http_webhook_bad_url(self):
        with pytest.raises(ValueError):
            _validate_device_config({"url": "ftp://bad"}, "http_webhook")

    def test_http_webhook_bad_method(self):
        with pytest.raises(ValueError):
            _validate_device_config({"url": "https://x.com", "method": "TRACE"}, "http_webhook")

    def test_simulator_valid(self):
        _validate_device_config({"mode": "sine", "interval_ms": 1000}, "simulator")

    def test_simulator_bad_mode(self):
        with pytest.raises(ValueError):
            _validate_device_config({"mode": "bad"}, "simulator")

    def test_simulator_bad_interval(self):
        with pytest.raises(ValueError):
            _validate_device_config({"interval_ms": -1}, "simulator")

    def test_onvif_valid(self):
        _validate_device_config({"host": "192.168.1.20", "port": 80, "user": "admin", "password": "pass"}, "onvif")

    def test_onvif_bad_user_type(self):
        with pytest.raises(ValueError):
            _validate_device_config({"user": 123}, "onvif")

    def test_videoai_valid(self):
        _validate_device_config({"rtsp_url": "rtsp://cam/stream", "detect_interval": 5}, "video_ai")

    def test_videoai_bad_rtsp(self):
        with pytest.raises(ValueError):
            _validate_device_config({"rtsp_url": "ftp://bad"}, "video_ai")

    def test_generic_valid(self):
        _validate_device_config({"host": "10.0.0.9", "port": 8080}, "generic_proto")

    def test_generic_bad_port(self):
        with pytest.raises(ValueError):
            _validate_device_config({"port": 0}, "generic_proto")


# ════════════════════ 点位校验 ════════════════════


class TestValidatePoints:
    """_validate_points 各字段校验。"""

    def test_none_ok(self):
        _validate_points(None, "modbus_tcp")

    def test_non_list_rejected(self):
        with pytest.raises(ValueError):
            _validate_points("not list", "simulator")

    def test_non_dict_point_rejected(self):
        with pytest.raises(ValueError):
            _validate_points(["bad"], "simulator")

    def test_missing_name(self):
        with pytest.raises(ValueError, match="name"):
            _validate_points([{"address": "0"}], "modbus_tcp")

    def test_missing_address_for_modbus(self):
        with pytest.raises(ValueError, match="address"):
            _validate_points([{"name": "t"}], "modbus_tcp")

    def test_simulator_no_address_ok(self):
        _validate_points([{"name": "t"}], "simulator")

    def test_bad_data_type(self):
        with pytest.raises(ValueError):
            _validate_points([{"name": "t", "address": "0", "data_type": "bad"}], "modbus_tcp")

    def test_bad_scan_rate(self):
        with pytest.raises(ValueError):
            _validate_points([{"name": "t", "address": "0", "scan_rate": -1}], "modbus_tcp")

    def test_bad_scale(self):
        with pytest.raises(ValueError):
            _validate_points([{"name": "t", "address": "0", "scale": "bad"}], "modbus_tcp")

    def test_bad_offset(self):
        with pytest.raises(ValueError):
            _validate_points([{"name": "t", "address": "0", "offset": "bad"}], "modbus_tcp")

    def test_bad_deadband(self):
        with pytest.raises(ValueError):
            _validate_points([{"name": "t", "address": "0", "deadband": -1}], "modbus_tcp")

    def test_bad_writable(self):
        with pytest.raises(ValueError):
            _validate_points([{"name": "t", "address": "0", "writable": "yes"}], "modbus_tcp")

    def test_bad_bit(self):
        with pytest.raises(ValueError):
            _validate_points([{"name": "t", "address": "0", "bit": 99}], "modbus_tcp")

    def test_bad_register_type(self):
        with pytest.raises(ValueError):
            _validate_points([{"name": "t", "address": "0", "register_type": "bad"}], "modbus_tcp")

    def test_valid_point_full(self):
        _validate_points(
            [
                {
                    "name": "temp",
                    "address": "0",
                    "data_type": "float32",
                    "scan_rate": 1,
                    "scale": 1.0,
                    "offset": 0,
                    "deadband": 0.5,
                    "writable": False,
                    "bit": 0,
                    "register_type": "holding",
                }
            ],
            "modbus_tcp",
        )

    def test_address_int_ok(self):
        _validate_points([{"name": "t", "address": 100}], "modbus_tcp")

    def test_valid_data_types(self):
        for dt in ("bool", "int16", "uint16", "int32", "uint32", "float32", "float64", "string", "bit", "byte"):
            _validate_points([{"name": "t", "address": "0", "data_type": dt}], "modbus_tcp")

    def test_register_type_numeric(self):
        _validate_points([{"name": "t", "address": "0", "register_type": 3}], "modbus_tcp")

    def test_http_webhook_no_address_ok(self):
        _validate_points([{"name": "t"}], "http_webhook")


# ════════════════════ 业务数据校验 ════════════════════


class TestValidateDeviceData:
    """_validate_device_data / _validate_device_update_data。"""

    def test_valid(self):
        _validate_device_data(_dev())

    def test_missing_device_id(self):
        with pytest.raises(ValueError, match="device_id"):
            _validate_device_data({"name": "x", "protocol": "simulator"})

    def test_missing_name(self):
        with pytest.raises(ValueError, match="name"):
            _validate_device_data({"device_id": "d", "protocol": "simulator"})

    def test_bad_protocol(self):
        with pytest.raises(ValueError, match="protocol"):
            _validate_device_data(_dev(protocol="bad_protocol"))

    def test_bad_status(self):
        with pytest.raises(ValueError, match="status"):
            _validate_device_data(_dev(status="bogus"))

    def test_bad_collect_interval(self):
        with pytest.raises(ValueError, match="collect_interval"):
            _validate_device_data(_dev(collect_interval=0))

    def test_bad_config_type(self):
        with pytest.raises(ValueError, match="config"):
            _validate_device_data(_dev(config="not dict"))

    def test_update_bad_status(self):
        with pytest.raises(ValueError):
            _validate_device_update_data({"status": "bogus"})

    def test_update_bad_collect_interval(self):
        with pytest.raises(ValueError):
            _validate_device_update_data({"collect_interval": 0})

    def test_update_bad_config_type(self):
        with pytest.raises(ValueError):
            _validate_device_update_data({"config": "x"})

    def test_update_config_with_fallback_protocol(self):
        _validate_device_update_data({"config": {}}, current_protocol="simulator")


class TestValidateRuleData:
    """规则/通知/条件校验。"""

    def test_valid(self):
        _validate_rule_data(_rule())

    def test_missing_name(self):
        with pytest.raises(ValueError, match="name"):
            _validate_rule_data({"severity": "warning", "conditions": []})

    def test_bad_severity(self):
        with pytest.raises(ValueError):
            _validate_rule_data(_rule(severity="bogus"))

    def test_bad_logic(self):
        with pytest.raises(ValueError):
            _validate_rule_data(_rule(logic="XOR"))

    def test_bad_duration(self):
        with pytest.raises(ValueError):
            _validate_rule_data(_rule(duration=-1))

    def test_bad_conditions_not_list(self):
        with pytest.raises(ValueError):
            _validate_rule_data(_rule(conditions="bad"))

    def test_condition_missing_point(self):
        with pytest.raises(ValueError, match="point"):
            _validate_rule_data(_rule(conditions=[{"operator": ">"}]))

    def test_condition_missing_operator(self):
        with pytest.raises(ValueError, match="operator"):
            _validate_rule_data(_rule(conditions=[{"point": "t"}]))

    def test_bad_notify_channels(self):
        with pytest.raises(ValueError):
            _validate_rule_data(_rule(notify_channels="bad"))

    def test_bad_notify_channel_value(self):
        with pytest.raises(ValueError):
            _validate_rule_data(_rule(notify_channels=["telegram"]))

    def test_update_bad_severity(self):
        with pytest.raises(ValueError):
            _validate_rule_update_data({"severity": "bogus"})

    def test_update_bad_logic(self):
        with pytest.raises(ValueError):
            _validate_rule_update_data({"logic": "XOR"})

    def test_update_bad_conditions(self):
        with pytest.raises(ValueError):
            _validate_rule_update_data({"conditions": "bad"})

    def test_trigger_value_not_dict(self):
        with pytest.raises(ValueError):
            _validate_trigger_value("bad")

    def test_trigger_value_ok(self):
        _validate_trigger_value({"v": 1})


class TestValidateUserData:
    """用户/模板/告警数据校验。"""

    def test_valid_user(self):
        _validate_user_data({"username": "u1", "password": "Abcd1234!", "role": "admin"})

    def test_missing_username(self):
        with pytest.raises(ValueError):
            _validate_user_data({"password": "Abcd1234!"})

    def test_missing_password(self):
        with pytest.raises(ValueError):
            _validate_user_data({"username": "u"})

    def test_short_password(self):
        with pytest.raises(ValueError):
            _validate_user_data({"username": "u", "password": "Ab1!"})

    def test_long_password(self):
        with pytest.raises(ValueError):
            _validate_user_data({"username": "u", "password": "A1!" + "a" * 80})

    def test_weak_password_no_special(self):
        with pytest.raises(ValueError):
            _validate_user_data({"username": "u", "password": "Abcd1234"})

    def test_bad_role(self):
        with pytest.raises(ValueError):
            _validate_user_data({"username": "u", "password": "Abcd1234!", "role": "super"})

    def test_update_bad_role(self):
        with pytest.raises(ValueError):
            _validate_user_update_data({"role": "super"})

    def test_update_bad_password(self):
        with pytest.raises(ValueError):
            _validate_user_update_data({"password": "short"})

    def test_valid_template(self):
        _validate_template_data({"name": "t1", "protocol": "simulator"})

    def test_template_missing_name(self):
        with pytest.raises(ValueError):
            _validate_template_data({"protocol": "simulator"})

    def test_template_bad_protocol(self):
        with pytest.raises(ValueError):
            _validate_template_data({"name": "t", "protocol": "bad"})

    def test_template_update_bad_protocol(self):
        with pytest.raises(ValueError):
            _validate_template_update_data({"protocol": "bad"})

    def test_valid_alarm(self):
        _validate_alarm_data({"severity": "critical", "rule_type": "threshold", "message": "x"})

    def test_alarm_bad_severity(self):
        with pytest.raises(ValueError):
            _validate_alarm_data({"severity": "bogus"})

    def test_alarm_bad_rule_type(self):
        with pytest.raises(ValueError):
            _validate_alarm_data({"rule_type": "bogus"})

    def test_alarm_long_message(self):
        with pytest.raises(ValueError):
            _validate_alarm_data({"message": "x" * 300})

    def test_alarm_bad_trigger_value(self):
        with pytest.raises(ValueError):
            _validate_alarm_data({"trigger_value": "bad"})


# ════════════════════ ORM 转换辅助 ════════════════════


class TestOrmHelpers:
    """_orm_to_* 系列转换函数。"""

    def test_orm_to_device(self):
        orm = DeviceORM(
            device_id="d1",
            name="n",
            protocol="simulator",
            status="online",
            config="{}",
            points="[]",
            collect_interval=5,
            version=2,
        )
        d = _orm_to_device(orm)
        assert d["device_id"] == "d1"
        assert d["config"] == {}
        assert d["points"] == []
        assert d["version"] == 2

    def test_orm_to_device_corrupt_json(self):
        orm = DeviceORM(
            device_id="d1",
            name="n",
            protocol="simulator",
            status="online",
            config="bad",
            points="bad",
            collect_interval=5,
        )
        d = _orm_to_device(orm)
        assert d["config"] == {}
        assert d["points"] == []

    def test_orm_to_template(self):
        orm = DeviceTemplateORM(
            name="t1", protocol="simulator", config_template='{"k":1}', point_templates="[1]", version=1
        )
        t = _orm_to_template(orm)
        assert t["name"] == "t1"
        assert t["config_template"] == {"k": 1}
        assert t["version"] == 1

    def test_orm_to_rule(self):
        orm = RuleORM(
            rule_id="r1",
            name="n",
            device_id="d1",
            conditions='[{"point":"t"}]',
            logic="AND",
            duration=0,
            severity="warning",
            enabled=True,
            notify_channels='["dingtalk"]',
            script="s",
            rule_type="threshold",
            version=1,
        )
        r = _orm_to_rule(orm)
        assert r["rule_id"] == "r1"
        assert r["conditions"] == [{"point": "t"}]
        assert r["enabled"] is True
        assert r["script"] == "s"

    def test_orm_to_alarm(self):
        from edgelite.models.db import AlarmORM

        orm = AlarmORM(
            alarm_id="a1",
            rule_id="r1",
            device_id="d1",
            severity="critical",
            status="firing",
            message="m",
            trigger_value='{"v":1}',
            trigger_count=2,
            rule_type="threshold",
        )
        a = _orm_to_alarm(orm)
        assert a["alarm_id"] == "a1"
        assert a["trigger_value"] == {"v": 1}
        assert a["trigger_count"] == 2

    def test_orm_to_user_variants(self):
        from edgelite.models.db import UserORM

        orm = UserORM(
            user_id="u1",
            username="bob",
            password="hashed",
            role="admin",
            enabled=True,
            must_change_password=True,
            version=3,
        )
        u = _orm_to_user(orm)
        assert u["username"] == "bob"
        assert "password" not in u
        uf = _orm_to_user_full(orm)
        assert uf["password"] == "hashed"
        us = _orm_to_user_safe(orm)
        assert "password" not in us
        assert us["version"] == 3


# ════════════════════ BaseRepo ════════════════════


class TestBaseRepo:
    """BaseRepo 基础行为。"""

    def test_init_with_session(self, db_session):
        repo = BaseRepo(db_session)
        assert repo._is_database_mode is False
        assert repo._external_session is db_session

    async def test_commit_deprecated_raises(self, db_session):
        repo = BaseRepo(db_session)
        with pytest.raises(RuntimeError, match="deprecated"):
            await repo._commit(db_session)

    def test_get_session_external(self, db_session):
        repo = BaseRepo(db_session)
        assert repo._get_session() is db_session

    def test_get_session_no_session(self):
        repo = BaseRepo(None)
        with pytest.raises(RuntimeError):
            repo._get_session()

    async def test_auto_session_external(self, db_session):
        repo = BaseRepo(db_session)
        async with repo._auto_session() as s:
            assert s is db_session

    async def test_auto_session_no_session(self):
        repo = BaseRepo(None)
        with pytest.raises(RuntimeError):
            async with repo._auto_session():
                pass

    async def test_safe_query_returns_default(self):
        repo = BaseRepo(None)

        async def boom():
            raise ValueError("x")

        assert await repo._safe_query(boom(), default="d") == "d"

    async def test_safe_write_reraises(self):
        repo = BaseRepo(None)

        async def boom():
            raise ValueError("x")

        with pytest.raises(ValueError):
            await repo._safe_write(boom())

    async def test_safe_write_integrity_reraises(self):
        repo = BaseRepo(None)

        async def boom():
            raise IntegrityError("stmt", {}, Exception("orig"))

        with pytest.raises(IntegrityError):
            await repo._safe_write(boom())

    async def test_table_write_lock_no_lock(self, db_session):
        repo = BaseRepo(db_session)
        async with repo._table_write_lock():
            pass

    async def test_write_write_lock_no_lock(self, db_session):
        repo = BaseRepo(db_session)
        async with repo._write_write_lock():
            pass


# ════════════════════ DeviceRepo ════════════════════


class TestDeviceRepo:
    """DeviceRepo 完整 CRUD 与查询。"""

    async def test_create_and_get(self, device_repo):
        d = await device_repo.create(_dev())
        assert d["device_id"] == "d1"
        got = await device_repo.get("d1")
        assert got["name"] == "Device"

    async def test_create_duplicate(self, device_repo):
        await device_repo.create(_dev())
        with pytest.raises(ValueError, match=RepoErrors.DEVICE_EXISTS):
            await device_repo.create(_dev())

    async def test_create_invalid(self, device_repo):
        with pytest.raises(ValueError):
            await device_repo.create(_dev(protocol="bad"))

    async def test_get_missing(self, device_repo):
        assert await device_repo.get("nope") is None

    async def test_get_by_ids(self, device_repo):
        await device_repo.create(_dev("d1"))
        await device_repo.create(_dev("d2"))
        result = await device_repo.get_by_ids(["d1", "d2", "d3"])
        assert len(result) == 2

    async def test_get_by_ids_empty(self, device_repo):
        assert await device_repo.get_by_ids([]) == []

    async def test_list_all_filters(self, device_repo):
        await device_repo.create(_dev("d1", status="online", protocol="modbus_tcp"))
        await device_repo.create(_dev("d2", status="offline", protocol="simulator"))
        items, total = await device_repo.list_all(status="online")
        assert total == 1
        assert items[0]["device_id"] == "d1"
        items2, total2 = await device_repo.list_all(protocol="simulator")
        assert total2 == 1

    async def test_list_all_search(self, device_repo):
        await device_repo.create(_dev("d1", name="TemperatureSensor"))
        await device_repo.create(_dev("d2", name="PressureGauge"))
        items, total = await device_repo.list_all(search="Temp")
        assert total == 1

    async def test_list_all_search_too_short(self, device_repo):
        await device_repo.create(_dev("d1", name="Temperature"))
        items, total = await device_repo.list_all(search="T")
        assert total == 1  # single char search ignored, returns all

    async def test_list_all_created_by(self, device_repo):
        await device_repo.create(_dev("d1"), created_by="u1")
        await device_repo.create(_dev("d2"), created_by="u2")
        items, total = await device_repo.list_all(created_by="u1")
        assert total == 1

    async def test_list_all_collect_status(self, device_repo):
        await device_repo.create(_dev("d1", status="online"))
        await device_repo.create(_dev("d2", status="offline"))
        _, total_c = await device_repo.list_all(collect_status="collecting")
        assert total_c == 1
        _, total_s = await device_repo.list_all(collect_status="stopped")
        assert total_s == 1

    async def test_list_all_cursor(self, device_repo):
        await device_repo.create(_dev("d1"))
        await device_repo.create(_dev("d2"))
        items, total, cursor = await device_repo.list_all(cursor="2099-01-01T00:00:00+00:00", size=10)
        assert total == 2
        assert len(items) == 2

    async def test_list_device_ids_by_owner(self, device_repo):
        await device_repo.create(_dev("d1"), created_by="u1")
        ids = await device_repo.list_device_ids_by_owner("u1")
        assert ids == ["d1"]

    async def test_list_devices_by_ids(self, device_repo):
        await device_repo.create(_dev("d1"))
        result = await device_repo.list_devices_by_ids(["d1"])
        assert len(result) == 1
        assert await device_repo.list_devices_by_ids([]) == []

    async def test_get_status_counts(self, device_repo):
        await device_repo.create(_dev("d1", status="online"))
        await device_repo.create(_dev("d2", status="online"))
        await device_repo.create(_dev("d3", status="offline"))
        counts = await device_repo.get_status_counts()
        assert counts.get("online") == 2
        assert counts.get("offline") == 1

    async def test_get_status_counts_with_ids(self, device_repo):
        await device_repo.create(_dev("d1", status="online"))
        await device_repo.create(_dev("d2", status="offline"))
        counts = await device_repo.get_status_counts(["d1"])
        assert counts == {"online": 1}
        assert await device_repo.get_status_counts([]) == {}

    async def test_update(self, device_repo):
        await device_repo.create(_dev())
        updated = await device_repo.update("d1", {"name": "Updated", "status": "online"})
        assert updated["name"] == "Updated"
        assert updated["status"] == "online"
        assert updated["version"] == 2

    async def test_update_missing(self, device_repo):
        assert await device_repo.update("nope", {"name": "x"}) is None

    async def test_update_version_conflict(self, device_repo):
        await device_repo.create(_dev())
        with pytest.raises(StaleDataError):
            await device_repo.update("d1", {"name": "x", "_version": 999})

    async def test_update_invalid_status(self, device_repo):
        await device_repo.create(_dev())
        with pytest.raises(RuntimeError):
            await device_repo.update("d1", {"status": "bogus"})

    async def test_update_status(self, device_repo):
        await device_repo.create(_dev())
        await device_repo.update_status("d1", "online")
        d = await device_repo.get("d1")
        assert d["status"] == "online"

    async def test_update_status_invalid(self, device_repo):
        with pytest.raises(ValueError):
            await device_repo.update_status("d1", "bogus")

    async def test_delete(self, device_repo):
        await device_repo.create(_dev())
        assert await device_repo.delete("d1") is True
        assert await device_repo.get("d1") is None

    async def test_delete_missing(self, device_repo):
        assert await device_repo.delete("nope") is False

    async def test_delete_with_owner_check_admin(self, device_repo):
        await device_repo.create(_dev("d1"), created_by="u1")
        assert await device_repo.delete_with_owner_check("d1", "u2", is_admin=True) == "deleted"

    async def test_delete_with_owner_check_owner(self, device_repo):
        await device_repo.create(_dev("d1"), created_by="u1")
        assert await device_repo.delete_with_owner_check("d1", "u1", is_admin=False) == "deleted"

    async def test_delete_with_owner_check_not_authorized(self, device_repo):
        await device_repo.create(_dev("d1"), created_by="u1")
        assert await device_repo.delete_with_owner_check("d1", "u2", is_admin=False) == "not_authorized"

    async def test_delete_with_owner_check_not_found(self, device_repo):
        assert await device_repo.delete_with_owner_check("nope", "u1", is_admin=False) == "not_found"

    async def test_list_by_protocol(self, device_repo):
        await device_repo.create(_dev("d1", protocol="simulator"))
        await device_repo.create(_dev("d2", protocol="modbus_tcp"))
        result = await device_repo.list_by_protocol("simulator")
        assert len(result) == 1

    async def test_bulk_create_in_session(self, device_repo, db_session):
        results = await device_repo.bulk_create_in_session(
            db_session,
            [_dev("d1"), _dev("d2"), {"device_id": "bad", "name": "", "protocol": "simulator"}],
            created_by="u1",
        )
        await db_session.commit()
        assert results[0][0] is True
        assert results[1][0] is True
        assert results[2][0] is False

    async def test_bulk_upsert_in_session_skip(self, device_repo, db_session):
        await device_repo.create(_dev("d1"))
        created, skipped, errors = await device_repo.bulk_upsert_in_session(
            [_dev("d1"), _dev("d2")], db_session, skip_existing=True
        )
        await db_session.commit()
        assert skipped == 1
        assert created == 1

    async def test_bulk_upsert_in_session_overwrite(self, device_repo, db_session):
        await device_repo.create(_dev("d1", name="old"))
        created, skipped, errors = await device_repo.bulk_upsert_in_session(
            [_dev("d1", name="new")], db_session, skip_existing=False
        )
        await db_session.commit()
        assert skipped == 0
        d = await device_repo.get("d1")
        assert d["name"] == "new"


# ════════════════════ TemplateRepo ════════════════════


class TestTemplateRepo:
    """TemplateRepo CRUD。"""

    async def test_create_and_get(self, template_repo):
        t = await template_repo.create(
            {"name": "t1", "protocol": "simulator", "config_template": {}, "point_templates": []}
        )
        assert t["name"] == "t1"
        got = await template_repo.get("t1")
        assert got["protocol"] == "simulator"

    async def test_create_duplicate(self, template_repo):
        await template_repo.create({"name": "t1", "protocol": "simulator"})
        with pytest.raises(ValueError, match=RepoErrors.TEMPLATE_EXISTS):
            await template_repo.create({"name": "t1", "protocol": "simulator"})

    async def test_create_invalid(self, template_repo):
        with pytest.raises(ValueError):
            await template_repo.create({"name": "t1", "protocol": "bad"})

    async def test_get_missing(self, template_repo):
        assert await template_repo.get("nope") is None

    async def test_list_all(self, template_repo):
        await template_repo.create({"name": "t1", "protocol": "simulator"})
        await template_repo.create({"name": "t2", "protocol": "modbus_tcp"})
        items, total = await template_repo.list_all()
        assert total == 2

    async def test_list_all_created_by(self, template_repo):
        await template_repo.create({"name": "t1", "protocol": "simulator"}, created_by="u1")
        items, total = await template_repo.list_all(created_by="u1")
        assert total == 1

    async def test_delete(self, template_repo):
        await template_repo.create({"name": "t1", "protocol": "simulator"})
        assert await template_repo.delete("t1") is True
        assert await template_repo.get("t1") is None

    async def test_delete_missing(self, template_repo):
        assert await template_repo.delete("nope") is False

    async def test_update(self, template_repo):
        await template_repo.create({"name": "t1", "protocol": "simulator"})
        updated = await template_repo.update("t1", {"protocol": "modbus_tcp", "config_template": {"host": "1.2.3.4"}})
        assert updated["protocol"] == "modbus_tcp"
        assert updated["config_template"] == {"host": "1.2.3.4"}
        assert updated["version"] == 2

    async def test_update_missing(self, template_repo):
        assert await template_repo.update("nope", {"protocol": "simulator"}) is None

    async def test_update_version_conflict(self, template_repo):
        await template_repo.create({"name": "t1", "protocol": "simulator"})
        with pytest.raises(StaleDataError):
            await template_repo.update("t1", {"protocol": "modbus_tcp", "_version": 999})


# ════════════════════ RuleRepo ════════════════════


class TestRuleRepo:
    """RuleRepo 完整 CRUD。"""

    async def test_create_and_get(self, rule_repo, device_repo):
        await device_repo.create(_dev())
        r = await rule_repo.create(_rule())
        assert r["name"] == "r1"
        got = await rule_repo.get(r["rule_id"])
        assert got["name"] == "r1"

    async def test_create_invalid(self, rule_repo):
        with pytest.raises(ValueError):
            await rule_repo.create({"name": "", "severity": "warning"})

    async def test_create_with_device_limit_ok(self, rule_repo, device_repo):
        await device_repo.create(_dev())
        r = await rule_repo.create_with_device_limit(_rule(), "d1", max_rules=5)
        assert r["name"] == "r1"

    async def test_create_with_device_limit_exceeded(self, rule_repo, device_repo):
        await device_repo.create(_dev())
        await rule_repo.create_with_device_limit(_rule(name="r1"), "d1", max_rules=1)
        with pytest.raises(ValueError, match="limit"):
            await rule_repo.create_with_device_limit(_rule(name="r2"), "d1", max_rules=1)

    async def test_get_missing(self, rule_repo):
        assert await rule_repo.get("nope") is None

    async def test_list_filters(self, rule_repo, device_repo):
        await device_repo.create(_dev())
        await rule_repo.create(_rule(name="r1", severity="critical"))
        await rule_repo.create(_rule(name="r2", severity="warning"))
        items, total = await rule_repo.list(severity="critical")
        assert total == 1
        items2, total2 = await rule_repo.list(device_id="d1")
        assert total2 == 2

    async def test_list_search(self, rule_repo, device_repo):
        await device_repo.create(_dev())
        await rule_repo.create(_rule(name="TempAlarm"))
        items, total = await rule_repo.list(search="Temp")
        assert total == 1

    async def test_list_all_delegates(self, rule_repo, device_repo):
        await device_repo.create(_dev())
        await rule_repo.create(_rule())
        items, total = await rule_repo.list_all()
        assert total == 1

    async def test_list_rules_by_ids(self, rule_repo, device_repo):
        await device_repo.create(_dev())
        r = await rule_repo.create(_rule())
        result = await rule_repo.list_rules_by_ids([r["rule_id"]])
        assert len(result) == 1
        assert await rule_repo.list_rules_by_ids([]) == []

    async def test_update(self, rule_repo, device_repo):
        await device_repo.create(_dev())
        r = await rule_repo.create(_rule())
        updated = await rule_repo.update(r["rule_id"], {"name": "new", "severity": "critical"})
        assert updated["name"] == "new"
        assert updated["severity"] == "critical"

    async def test_update_missing(self, rule_repo):
        assert await rule_repo.update("nope", {"name": "x"}) is None

    async def test_update_version_conflict(self, rule_repo, device_repo):
        await device_repo.create(_dev())
        r = await rule_repo.create(_rule())
        with pytest.raises(StaleDataError):
            await rule_repo.update(r["rule_id"], {"name": "x", "version": 999})

    async def test_update_invalid(self, rule_repo, device_repo):
        await device_repo.create(_dev())
        r = await rule_repo.create(_rule())
        with pytest.raises(ValueError):
            await rule_repo.update(r["rule_id"], {"severity": "bogus"})

    async def test_set_enabled_toggle(self, rule_repo, device_repo):
        await device_repo.create(_dev())
        r = await rule_repo.create(_rule())
        await rule_repo.set_enabled(r["rule_id"], False)
        got = await rule_repo.get(r["rule_id"])
        assert got["enabled"] is False
        await rule_repo.toggle(r["rule_id"], True)
        got2 = await rule_repo.get(r["rule_id"])
        assert got2["enabled"] is True

    async def test_delete(self, rule_repo, device_repo):
        await device_repo.create(_dev())
        r = await rule_repo.create(_rule())
        assert await rule_repo.delete(r["rule_id"]) is True
        assert await rule_repo.get(r["rule_id"]) is None

    async def test_delete_missing(self, rule_repo):
        assert await rule_repo.delete("nope") is False

    async def test_list_by_device(self, rule_repo, device_repo):
        await device_repo.create(_dev())
        await rule_repo.create(_rule(name="r1", enabled=True))
        await rule_repo.create(_rule(name="r2", enabled=False))
        result = await rule_repo.list_by_device("d1")
        assert len(result) == 1  # only enabled

    async def test_list_enabled_by_point(self, rule_repo, device_repo):
        await device_repo.create(_dev())
        await rule_repo.create(_rule(name="r1", conditions=[{"point": "temp", "operator": ">", "threshold": 30}]))
        await rule_repo.create(_rule(name="r2", conditions=[{"point": "pressure", "operator": "<", "threshold": 10}]))
        result = await rule_repo.list_enabled_by_point("d1", "temp")
        assert len(result) == 1

    async def test_upsert_bulk_skip(self, rule_repo, device_repo, db_session):
        await device_repo.create(_dev())
        existing = await rule_repo.create(_rule(name="r1"))
        rec = _rule(name="r1")
        rec["rule_id"] = existing["rule_id"]
        created, skipped, errors = await rule_repo.upsert_bulk([rec, _rule(name="r2")], db_session, skip_existing=True)
        await db_session.commit()
        assert skipped == 1

    async def test_upsert_bulk_overwrite(self, rule_repo, device_repo, db_session):
        await device_repo.create(_dev())
        existing = await rule_repo.create(_rule(name="r1"))
        rec = _rule(name="r1-updated")
        rec["rule_id"] = existing["rule_id"]
        created, skipped, errors = await rule_repo.upsert_bulk([rec], db_session, skip_existing=False)
        await db_session.commit()
        got = await rule_repo.get(existing["rule_id"])
        assert got["name"] == "r1-updated"
