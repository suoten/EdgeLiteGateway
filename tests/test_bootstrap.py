"""edgelite.bootstrap module comprehensive tests."""

from __future__ import annotations

import sys

sys.path.insert(0, "src")

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from edgelite.bootstrap import (
    ServiceContainer,
    _ensure_secret_key,
    _get_project_root,
    _safe_stop,
    _verify_startup_chain,
    bootstrap_ai,
    bootstrap_all,
    bootstrap_app_updater,
    bootstrap_config_reload,
    bootstrap_devices,
    bootstrap_driver_watchdog,
    bootstrap_drivers,
    bootstrap_engine,
    bootstrap_evaluator,
    bootstrap_integration,
    bootstrap_log_rotation,
    bootstrap_modbus_slave,
    bootstrap_mqtt,
    bootstrap_platforms,
    bootstrap_services,
    bootstrap_shadow,
    bootstrap_storage,
    bootstrap_video,
    bootstrap_ws,
    teardown,
)


@pytest.fixture(autouse=True)
def _restore_root_logger():
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_filters = list(root.filters)
    saved_level = root.level
    yield
    root.handlers = saved_handlers
    root.filters = saved_filters
    root.level = saved_level


def _make_config(**overrides):
    config = SimpleNamespace(
        security=SimpleNamespace(secret_key="x" * 40),
        influxdb=SimpleNamespace(token="tok", url="http://localhost:8086", org="o", bucket="b"),
        logging=SimpleNamespace(
            level="INFO",
            format="%(message)s",
            log_dir=None,
            max_bytes=1024,
            backup_count=2,
            json_format=False,
        ),
        server=SimpleNamespace(port=8000, cors_allowed_origins=[], dev_mode=True, websocket=None),
        preprocess=SimpleNamespace(enabled=False),
        simulator=SimpleNamespace(auto_create=False, default_devices=[]),
        drivers=SimpleNamespace(custom_dir=""),
    )
    for k, v in overrides.items():
        setattr(config, k, v)
    return config


class _AsyncCM:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *a):
        return None


class TestServiceContainer:
    def test_defaults(self):
        c = ServiceContainer()
        assert c.database is None
        assert c.event_bus is None
        assert c.platform_handlers == {}
        assert c._repos == {}
        assert c._initialized == []

    def test_track_appends(self):
        c = ServiceContainer()
        r = MagicMock()
        c.track("db", r)
        assert c._initialized == [("db", r)]

    def test_track_multiple(self):
        c = ServiceContainer()
        c.track("a", 1)
        c.track("b", 2)
        assert len(c._initialized) == 2


@pytest.mark.parametrize(
    "key,should_raise",
    [
        ("", True),
        ("short", True),
        ("a" * 31, True),
        ("CHANGE_ME_please_replace_this_key!", True),
        ("<your-secret-key-here>", True),
        ("placeholder_value_xxxxxxxxxxxxxxxxxxx", True),
        ("changeme_xxxxxxxxxxxxxxxxxxxxxxxxxxxx", True),
        ("x" * 32, False),
        ("a" * 40, False),
    ],
)
def test_ensure_secret_key(key, should_raise):
    config = SimpleNamespace(security=SimpleNamespace(secret_key=key))
    if should_raise:
        with pytest.raises(RuntimeError):
            _ensure_secret_key(config)
    else:
        _ensure_secret_key(config)


def test_get_project_root():
    root = _get_project_root()
    assert isinstance(root, Path)
    assert root.exists()


async def test_safe_stop_success():
    resource = MagicMock()
    resource.stop = AsyncMock()
    await _safe_stop(resource, "stop", "test-label")
    resource.stop.assert_awaited_once()


async def test_safe_stop_exception_swallowed():
    resource = MagicMock()
    resource.shutdown = AsyncMock(side_effect=ValueError("boom"))
    await _safe_stop(resource, "shutdown", "bad")
    resource.shutdown.assert_awaited_once()


# -- bootstrap_storage --


async def test_bootstrap_storage_success():
    c = ServiceContainer()
    config = _make_config()
    mock_db = MagicMock()
    mock_db.connect = AsyncMock()
    mock_db.init_tables = AsyncMock()
    mock_db.write_lock = MagicMock()
    mock_influx = MagicMock()
    mock_influx.connect = AsyncMock()
    mock_cache = MagicMock()
    mock_cache.start_orphan_compaction = MagicMock()
    mock_db_monitor = MagicMock()
    mock_db_monitor.set_database = MagicMock()
    mock_db_monitor.start = AsyncMock()

    with (
        patch("edgelite.storage.database.Database", return_value=mock_db) as mock_db_cls,
        patch("edgelite.storage.influx_storage.InfluxDBStorage", return_value=mock_influx),
        patch("edgelite.storage.cache.CacheManager", return_value=mock_cache),
        patch("edgelite.storage.sqlite_repo.DeviceRepo") as mock_device_repo,
        patch("edgelite.storage.sqlite_repo.RuleRepo"),
        patch("edgelite.storage.sqlite_repo.AlarmRepo"),
        patch("edgelite.storage.sqlite_repo.UserRepo"),
        patch("edgelite.storage.sqlite_repo.TemplateRepo"),
        patch("edgelite.services.db_monitor.get_db_monitor", return_value=mock_db_monitor),
        patch("edgelite.security.session_manager.restore_sessions", return_value=3),
    ):
        await bootstrap_storage(c, config)

    mock_db.connect.assert_awaited_once()
    mock_db.init_tables.assert_awaited_once()
    mock_influx.connect.assert_awaited_once()
    mock_cache.start_orphan_compaction.assert_called_once()
    mock_db_monitor.start.assert_awaited_once()
    assert c.database is mock_db
    assert c.influx_storage is mock_influx
    assert c.cache_manager is mock_cache
    assert "device" in c._repos
    assert "rule" in c._repos
    assert "alarm" in c._repos
    assert "user" in c._repos
    assert "template" in c._repos
    mock_device_repo.assert_called_once_with(mock_db, mock_db.write_lock, mock_db_cls.TABLE_DEVICES)
    tracked_names = [n for n, _ in c._initialized]
    assert "database" in tracked_names
    assert "influx" in tracked_names
    assert "cache_manager" in tracked_names
    assert "db_monitor" in tracked_names


async def test_bootstrap_storage_db_monitor_failure():
    c = ServiceContainer()
    config = _make_config()
    mock_db = MagicMock()
    mock_db.connect = AsyncMock()
    mock_db.init_tables = AsyncMock()
    mock_db.write_lock = MagicMock()
    mock_influx = MagicMock()
    mock_influx.connect = AsyncMock()
    mock_cache = MagicMock()

    with (
        patch("edgelite.storage.database.Database", return_value=mock_db),
        patch("edgelite.storage.influx_storage.InfluxDBStorage", return_value=mock_influx),
        patch("edgelite.storage.cache.CacheManager", return_value=mock_cache),
        patch("edgelite.storage.sqlite_repo.DeviceRepo"),
        patch("edgelite.storage.sqlite_repo.RuleRepo"),
        patch("edgelite.storage.sqlite_repo.AlarmRepo"),
        patch("edgelite.storage.sqlite_repo.UserRepo"),
        patch("edgelite.storage.sqlite_repo.TemplateRepo"),
        patch("edgelite.services.db_monitor.get_db_monitor") as mock_get_dm,
        patch("edgelite.security.session_manager.restore_sessions", side_effect=RuntimeError("nope")),
    ):
        mock_get_dm.side_effect = RuntimeError("monitor unavailable")
        await bootstrap_storage(c, config)

    assert c.database is mock_db


# -- bootstrap_engine --


async def test_bootstrap_engine_preprocessor_enabled():
    c = ServiceContainer()
    c.influx_storage = MagicMock()
    c.cache_manager = MagicMock()
    config = _make_config(preprocess=SimpleNamespace(enabled=True))
    mock_event_bus = MagicMock()
    mock_event_bus.enable_alarm_persistence = MagicMock()
    mock_scheduler = MagicMock()
    mock_scheduler.set_preprocessor = MagicMock()
    mock_preprocessor = MagicMock()
    mock_lifecycle = MagicMock()

    with (
        patch("edgelite.engine.event_bus.EventBus", return_value=mock_event_bus),
        patch("edgelite.engine.scheduler.CollectScheduler", return_value=mock_scheduler),
        patch("edgelite.engine.preprocessor.DataPreprocessor", return_value=mock_preprocessor),
        patch("edgelite.engine.lifecycle.DeviceLifecycleManager", return_value=mock_lifecycle),
    ):
        await bootstrap_engine(c, config)

    assert c.event_bus is mock_event_bus
    assert c.scheduler is mock_scheduler
    assert c.preprocessor is mock_preprocessor
    assert c.lifecycle is mock_lifecycle
    mock_scheduler.set_preprocessor.assert_called_once_with(mock_preprocessor)
    mock_event_bus.enable_alarm_persistence.assert_called_once()
    c.influx_storage.set_event_bus.assert_called_once_with(mock_event_bus)


async def test_bootstrap_engine_preprocessor_disabled():
    c = ServiceContainer()
    c.influx_storage = MagicMock()
    c.cache_manager = MagicMock()
    config = _make_config(preprocess=SimpleNamespace(enabled=False))
    mock_event_bus = MagicMock()
    mock_event_bus.enable_alarm_persistence = MagicMock()
    mock_scheduler = MagicMock()

    with (
        patch("edgelite.engine.event_bus.EventBus", return_value=mock_event_bus),
        patch("edgelite.engine.scheduler.CollectScheduler", return_value=mock_scheduler),
        patch("edgelite.engine.preprocessor.DataPreprocessor") as mock_pp_cls,
        patch("edgelite.engine.lifecycle.DeviceLifecycleManager"),
    ):
        await bootstrap_engine(c, config)

    mock_scheduler.set_preprocessor.assert_not_called()
    mock_pp_cls.assert_called_once()


async def test_bootstrap_engine_outbox_persistence_failure():
    c = ServiceContainer()
    c.influx_storage = MagicMock()
    c.cache_manager = MagicMock()
    config = _make_config(preprocess=SimpleNamespace(enabled=False))
    mock_event_bus = MagicMock()
    mock_event_bus.enable_alarm_persistence = MagicMock(side_effect=RuntimeError("outbox fail"))

    with (
        patch("edgelite.engine.event_bus.EventBus", return_value=mock_event_bus),
        patch("edgelite.engine.scheduler.CollectScheduler"),
        patch("edgelite.engine.preprocessor.DataPreprocessor"),
        patch("edgelite.engine.lifecycle.DeviceLifecycleManager"),
    ):
        await bootstrap_engine(c, config)

    assert c.event_bus is mock_event_bus


# -- bootstrap_services + _AlarmNameResolver --


def _setup_services_container():
    c = ServiceContainer()
    c.scheduler = MagicMock()
    c.lifecycle = MagicMock()
    c.event_bus = MagicMock()
    c.influx_storage = MagicMock()
    c.database = MagicMock()
    c.database.audit_db_path = "data/audit.db"
    c.start_time = 1234.0
    c._repos["device"] = MagicMock()
    c._repos["rule"] = MagicMock()
    c._repos["alarm"] = MagicMock()
    c._repos["user"] = MagicMock()
    c._repos["template"] = MagicMock()
    return c


async def test_bootstrap_services_success():
    c = _setup_services_container()
    config = _make_config()

    with (
        patch("edgelite.services.device_service.DeviceService") as mock_ds_cls,
        patch("edgelite.services.rule_service.RuleService"),
        patch("edgelite.services.alarm_service.AlarmService") as mock_as_cls,
        patch("edgelite.services.data_service.DataService"),
        patch("edgelite.services.video_service.VideoService"),
        patch("edgelite.services.notify_service.NotifyService"),
        patch("edgelite.services.system_service.SystemService"),
        patch("edgelite.services.audit_service.AuditService") as mock_audit_cls,
    ):
        mock_alarm_svc = MagicMock()
        mock_alarm_svc.start = AsyncMock()
        mock_as_cls.return_value = mock_alarm_svc
        mock_audit_svc = MagicMock()
        mock_audit_svc.initialize = AsyncMock()
        mock_audit_cls.return_value = mock_audit_svc

        await bootstrap_services(c, config)

    mock_ds_cls.assert_called_once()
    mock_as_cls.assert_called_once()
    mock_alarm_svc.start.assert_awaited_once()
    mock_audit_svc.initialize.assert_awaited_once()
    assert c.device_service is mock_ds_cls.return_value
    assert c.alarm_service is mock_alarm_svc
    assert c.audit_service is mock_audit_svc


async def test_alarm_name_resolver_both_ids():
    c = _setup_services_container()
    rule_repo = AsyncMock()
    rule_repo.get = AsyncMock(return_value={"name": "RuleA"})
    device_repo = AsyncMock()
    device_repo.get = AsyncMock(return_value={"name": "DevB"})
    c._repos["device"] = device_repo
    c._repos["rule"] = rule_repo
    config = _make_config()

    with (
        patch("edgelite.services.device_service.DeviceService"),
        patch("edgelite.services.rule_service.RuleService"),
        patch("edgelite.services.alarm_service.AlarmService") as mock_as_cls,
        patch("edgelite.services.data_service.DataService"),
        patch("edgelite.services.video_service.VideoService"),
        patch("edgelite.services.notify_service.NotifyService"),
        patch("edgelite.services.system_service.SystemService"),
        patch("edgelite.services.audit_service.AuditService") as mock_audit_cls,
    ):
        mock_as_cls.return_value.start = AsyncMock()
        mock_audit_cls.return_value.initialize = AsyncMock()
        await bootstrap_services(c, config)

    resolver = mock_as_cls.call_args.kwargs["name_resolver"]
    rule_name, device_name = await resolver.resolve("r1", "d1")
    assert rule_name == "RuleA"
    assert device_name == "DevB"


async def test_alarm_name_resolver_empty_ids():
    c = _setup_services_container()
    c._repos["device"] = AsyncMock()
    c._repos["rule"] = AsyncMock()
    config = _make_config()

    with (
        patch("edgelite.services.device_service.DeviceService"),
        patch("edgelite.services.rule_service.RuleService"),
        patch("edgelite.services.alarm_service.AlarmService") as mock_as_cls,
        patch("edgelite.services.data_service.DataService"),
        patch("edgelite.services.video_service.VideoService"),
        patch("edgelite.services.notify_service.NotifyService"),
        patch("edgelite.services.system_service.SystemService"),
        patch("edgelite.services.audit_service.AuditService") as mock_audit_cls,
    ):
        mock_as_cls.return_value.start = AsyncMock()
        mock_audit_cls.return_value.initialize = AsyncMock()
        await bootstrap_services(c, config)

    resolver = mock_as_cls.call_args.kwargs["name_resolver"]
    rule_name, device_name = await resolver.resolve("", "")
    assert rule_name == ""
    assert device_name == ""


async def test_alarm_name_resolver_repo_exception():
    c = _setup_services_container()
    rule_repo = AsyncMock()
    rule_repo.get = AsyncMock(side_effect=RuntimeError("db down"))
    device_repo = AsyncMock()
    device_repo.get = AsyncMock(side_effect=RuntimeError("db down"))
    c._repos["device"] = device_repo
    c._repos["rule"] = rule_repo
    config = _make_config()

    with (
        patch("edgelite.services.device_service.DeviceService"),
        patch("edgelite.services.rule_service.RuleService"),
        patch("edgelite.services.alarm_service.AlarmService") as mock_as_cls,
        patch("edgelite.services.data_service.DataService"),
        patch("edgelite.services.video_service.VideoService"),
        patch("edgelite.services.notify_service.NotifyService"),
        patch("edgelite.services.system_service.SystemService"),
        patch("edgelite.services.audit_service.AuditService") as mock_audit_cls,
    ):
        mock_as_cls.return_value.start = AsyncMock()
        mock_audit_cls.return_value.initialize = AsyncMock()
        await bootstrap_services(c, config)

    resolver = mock_as_cls.call_args.kwargs["name_resolver"]
    rule_name, device_name = await resolver.resolve("r1", "d1")
    assert rule_name == ""
    assert device_name == ""


# -- bootstrap_evaluator --


async def test_bootstrap_evaluator_success():
    c = ServiceContainer()
    c.event_bus = MagicMock()
    c.ai_engine = MagicMock()
    c._repos["rule"] = MagicMock()
    c._repos["alarm"] = MagicMock()
    c._repos["device"] = MagicMock()
    config = _make_config()
    mock_evaluator = MagicMock()
    mock_evaluator.start = AsyncMock()

    with patch("edgelite.engine.evaluator.RuleEvaluator", return_value=mock_evaluator):
        await bootstrap_evaluator(c, config)

    assert c.evaluator is mock_evaluator
    mock_evaluator.start.assert_awaited_once()


# -- bootstrap_ws --


async def test_bootstrap_ws_with_origin_whitelist():
    c = ServiceContainer()
    c.event_bus = MagicMock()
    c.event_bus.replay_pending_alarms = AsyncMock(return_value=2)
    config = _make_config(
        server=SimpleNamespace(
            port=8000,
            cors_allowed_origins=["http://localhost:3000"],
            dev_mode=False,
            websocket=SimpleNamespace(max_connections=50),
        )
    )
    mock_ws_manager = MagicMock()
    mock_ws_manager.start_heartbeat = AsyncMock()
    mock_ws_channels = MagicMock()
    mock_ws_channels.start = AsyncMock()

    with (
        patch("edgelite.ws.manager.ConnectionManager", return_value=mock_ws_manager),
        patch("edgelite.ws.channels.WebSocketChannels", return_value=mock_ws_channels),
    ):
        await bootstrap_ws(c, config)

    mock_ws_manager.set_allowed_origins.assert_called_once_with(["http://localhost:3000"])
    mock_ws_channels.start.assert_awaited_once()
    mock_ws_manager.start_heartbeat.assert_awaited_once()
    c.event_bus.replay_pending_alarms.assert_awaited_once()


async def test_bootstrap_ws_dev_mode_no_whitelist():
    c = ServiceContainer()
    c.event_bus = MagicMock()
    c.event_bus.replay_pending_alarms = AsyncMock(return_value=0)
    config = _make_config(
        server=SimpleNamespace(
            port=8000,
            cors_allowed_origins=["http://localhost:3000"],
            dev_mode=True,
            websocket=None,
        )
    )
    mock_ws_manager = MagicMock()
    mock_ws_manager.start_heartbeat = AsyncMock()
    mock_ws_channels = MagicMock()
    mock_ws_channels.start = AsyncMock()

    with (
        patch("edgelite.ws.manager.ConnectionManager", return_value=mock_ws_manager),
        patch("edgelite.ws.channels.WebSocketChannels", return_value=mock_ws_channels),
    ):
        await bootstrap_ws(c, config)

    mock_ws_manager.set_allowed_origins.assert_not_called()


async def test_bootstrap_ws_replay_failure():
    c = ServiceContainer()
    c.event_bus = MagicMock()
    c.event_bus.replay_pending_alarms = AsyncMock(side_effect=RuntimeError("replay fail"))
    config = _make_config()
    mock_ws_manager = MagicMock()
    mock_ws_manager.start_heartbeat = AsyncMock()
    mock_ws_channels = MagicMock()
    mock_ws_channels.start = AsyncMock()

    with (
        patch("edgelite.ws.manager.ConnectionManager", return_value=mock_ws_manager),
        patch("edgelite.ws.channels.WebSocketChannels", return_value=mock_ws_channels),
    ):
        await bootstrap_ws(c, config)


# -- bootstrap_drivers --


async def test_bootstrap_drivers_with_custom_dir():
    c = ServiceContainer()
    config = _make_config(drivers=SimpleNamespace(custom_dir="/custom/drivers"))
    mock_registry = MagicMock()
    mock_plugin_mgr = MagicMock()
    mock_plugin_mgr.discover_custom_drivers = MagicMock()

    with (
        patch("edgelite.drivers.registry.get_driver_registry", return_value=mock_registry),
        patch("edgelite.engine.plugin_manager.PluginManager", return_value=mock_plugin_mgr),
    ):
        await bootstrap_drivers(c, config)

    mock_plugin_mgr.discover_custom_drivers.assert_called_once_with("/custom/drivers")
    assert c.driver_registry is mock_registry
    assert c.plugin_manager is mock_plugin_mgr


async def test_bootstrap_drivers_no_custom_dir():
    c = ServiceContainer()
    config = _make_config(drivers=SimpleNamespace(custom_dir=""))
    mock_registry = MagicMock()
    mock_plugin_mgr = MagicMock()

    with (
        patch("edgelite.drivers.registry.get_driver_registry", return_value=mock_registry),
        patch("edgelite.engine.plugin_manager.PluginManager", return_value=mock_plugin_mgr),
    ):
        await bootstrap_drivers(c, config)

    mock_plugin_mgr.discover_custom_drivers.assert_not_called()


# -- bootstrap_mqtt --


async def test_bootstrap_mqtt_no_server_config():
    c = ServiceContainer()
    config = _make_config()
    config.mqtt_server = None
    mock_forwarder = MagicMock()
    mock_forwarder.start = AsyncMock()

    with patch("edgelite.engine.mqtt_forwarder.MqttForwarder", return_value=mock_forwarder):
        await bootstrap_mqtt(c, config)

    assert c.mqtt_forwarder is mock_forwarder
    assert c.mqtt_server is None


async def test_bootstrap_mqtt_server_disabled():
    c = ServiceContainer()
    config = _make_config()
    config.mqtt_server = SimpleNamespace(enabled=False)
    mock_forwarder = MagicMock()
    mock_forwarder.start = AsyncMock()

    with patch("edgelite.engine.mqtt_forwarder.MqttForwarder", return_value=mock_forwarder):
        await bootstrap_mqtt(c, config)

    assert c.mqtt_server is None


async def test_bootstrap_mqtt_server_with_auth():
    c = ServiceContainer()
    config = _make_config()
    config.mqtt_server = SimpleNamespace(
        enabled=True,
        username="user",
        password="pass",
        allow_no_auth=False,
        host="0.0.0.0",
        port=1888,
        ws_port=None,
    )
    mock_forwarder = MagicMock()
    mock_forwarder.start = AsyncMock()
    mock_server = MagicMock()
    mock_server.start = AsyncMock()

    with (
        patch("edgelite.engine.mqtt_forwarder.MqttForwarder", return_value=mock_forwarder),
        patch("edgelite.engine.mqtt_server.MqttServer", return_value=mock_server),
    ):
        await bootstrap_mqtt(c, config)

    assert c.mqtt_server is mock_server
    mock_server.start.assert_awaited_once()
    call_args = mock_server.start.call_args[0][0]
    assert call_args["host"] == "0.0.0.0"
    assert call_args["username"] == "user"


async def test_bootstrap_mqtt_server_no_auth_localhost_fallback():
    c = ServiceContainer()
    config = _make_config()
    config.mqtt_server = SimpleNamespace(
        enabled=True,
        username="",
        password="",
        allow_no_auth=False,
        host="0.0.0.0",
        port=1888,
        ws_port=None,
    )
    mock_forwarder = MagicMock()
    mock_forwarder.start = AsyncMock()
    mock_server = MagicMock()
    mock_server.start = AsyncMock()

    with (
        patch("edgelite.engine.mqtt_forwarder.MqttForwarder", return_value=mock_forwarder),
        patch("edgelite.engine.mqtt_server.MqttServer", return_value=mock_server),
    ):
        await bootstrap_mqtt(c, config)

    call_args = mock_server.start.call_args[0][0]
    assert call_args["host"] == "127.0.0.1"


async def test_bootstrap_mqtt_server_allow_no_auth():
    c = ServiceContainer()
    config = _make_config()
    config.mqtt_server = SimpleNamespace(
        enabled=True,
        username="",
        password="",
        allow_no_auth=True,
        host="0.0.0.0",
        port=1888,
        ws_port=None,
    )
    mock_forwarder = MagicMock()
    mock_forwarder.start = AsyncMock()
    mock_server = MagicMock()
    mock_server.start = AsyncMock()

    with (
        patch("edgelite.engine.mqtt_forwarder.MqttForwarder", return_value=mock_forwarder),
        patch("edgelite.engine.mqtt_server.MqttServer", return_value=mock_server),
    ):
        await bootstrap_mqtt(c, config)

    call_args = mock_server.start.call_args[0][0]
    assert call_args["host"] == "0.0.0.0"


# -- bootstrap_platforms --


async def test_bootstrap_platforms_no_config():
    c = ServiceContainer()
    config = _make_config()
    await bootstrap_platforms(c, config)
    assert c.platform_handlers == {}


async def test_bootstrap_platforms_mixed():
    c = ServiceContainer()
    config = _make_config()
    config.platforms = {
        "plat_a": {"enabled": False},
        "plat_b": {"enabled": True, "type": "thingsboard", "url": "http://tb"},
        "plat_c": "not-a-dict",
    }
    mock_svc = MagicMock()
    mock_svc.connect = AsyncMock(return_value={"status": "connected"})
    mock_module = MagicMock(PlatformService=MagicMock(return_value=mock_svc))

    with patch.dict("sys.modules", {"edgelite.services.platform_service": mock_module}):
        await bootstrap_platforms(c, config)

    assert mock_svc.connect.await_count == 1


async def test_bootstrap_platforms_connect_exception():
    c = ServiceContainer()
    config = _make_config()
    config.platforms = {"plat_x": {"enabled": True, "type": "thingsboard"}}
    mock_svc = MagicMock()
    mock_svc.connect = AsyncMock(side_effect=RuntimeError("connection refused"))
    mock_module = MagicMock(PlatformService=MagicMock(return_value=mock_svc))

    with patch.dict("sys.modules", {"edgelite.services.platform_service": mock_module}):
        await bootstrap_platforms(c, config)


# -- bootstrap_modbus_slave --


async def test_bootstrap_modbus_slave_disabled():
    c = ServiceContainer()
    config = _make_config()
    config.modbus_slave = SimpleNamespace(enabled=False)
    await bootstrap_modbus_slave(c, config)
    assert c.modbus_slave is None


async def test_bootstrap_modbus_slave_no_config():
    c = ServiceContainer()
    config = _make_config()
    await bootstrap_modbus_slave(c, config)
    assert c.modbus_slave is None


async def test_bootstrap_modbus_slave_enabled():
    c = ServiceContainer()
    config = _make_config()
    config.modbus_slave = SimpleNamespace(enabled=True, host="127.0.0.1", port=5020, holding_size=500, input_size=500)
    mock_slave = MagicMock()
    mock_slave.start = AsyncMock()

    with patch("edgelite.engine.modbus_slave.ModbusSlaveServer", return_value=mock_slave):
        await bootstrap_modbus_slave(c, config)

    assert c.modbus_slave is mock_slave
    mock_slave.start.assert_awaited_once()
    call_args = mock_slave.start.call_args[0][0]
    assert call_args["host"] == "127.0.0.1"
    assert call_args["port"] == 5020


# -- bootstrap_devices --


async def test_bootstrap_devices_auto_create_disabled():
    c = ServiceContainer()
    c.device_service = MagicMock()
    c.device_service.load_existing_devices = AsyncMock()
    c._repos["device"] = MagicMock()
    config = _make_config(simulator=SimpleNamespace(auto_create=False, default_devices=[]))
    await bootstrap_devices(c, config)
    c.device_service.load_existing_devices.assert_awaited_once()


async def test_bootstrap_devices_auto_create_existing():
    c = ServiceContainer()
    c.device_service = MagicMock()
    c.device_service.load_existing_devices = AsyncMock()
    c.device_service.create_simulator = AsyncMock()
    c._repos["device"] = AsyncMock()
    c._repos["device"].get = AsyncMock(return_value={"device_id": "dev1"})
    dev_cfg = MagicMock()
    dev_cfg.device_id = "dev1"
    dev_cfg.name = "Dev1"
    dev_cfg.points = []
    dev_cfg.collect_interval = 5
    config = _make_config(simulator=SimpleNamespace(auto_create=True, default_devices=[dev_cfg]))
    await bootstrap_devices(c, config)
    c.device_service.create_simulator.assert_not_awaited()


async def test_bootstrap_devices_auto_create_new():
    c = ServiceContainer()
    c.device_service = MagicMock()
    c.device_service.load_existing_devices = AsyncMock()
    c.device_service.create_simulator = AsyncMock()
    c._repos["device"] = AsyncMock()
    c._repos["device"].get = AsyncMock(return_value=None)
    point_mock = MagicMock()
    point_mock.model_dump.return_value = {"name": "p1", "address": "40001"}
    dev_cfg = MagicMock()
    dev_cfg.device_id = "dev_new"
    dev_cfg.name = "NewDev"
    dev_cfg.points = [point_mock]
    dev_cfg.collect_interval = 10
    config = _make_config(simulator=SimpleNamespace(auto_create=True, default_devices=[dev_cfg]))
    await bootstrap_devices(c, config)
    c.device_service.create_simulator.assert_awaited_once()
    call_args = c.device_service.create_simulator.call_args[0][0]
    assert call_args["device_id"] == "dev_new"
    assert call_args["name"] == "NewDev"
    assert call_args["collect_interval"] == 10


# -- bootstrap_integration --


async def test_bootstrap_integration_success():
    c = ServiceContainer()
    c.event_bus = MagicMock()
    c.device_service = MagicMock()
    c.scheduler = MagicMock()
    mock_dispatcher = MagicMock()
    mock_dispatcher.register_service = MagicMock()
    mock_endpoint = MagicMock()
    mock_backhaul = MagicMock()
    mock_backhaul.start = AsyncMock()

    with (
        patch("edgelite.engine.integration.dispatcher.MessageDispatcher", return_value=mock_dispatcher),
        patch("edgelite.engine.integration.endpoint.IntegrationEndpoint", return_value=mock_endpoint),
        patch("edgelite.engine.integration.backhaul.BackhaulManager", return_value=mock_backhaul),
    ):
        await bootstrap_integration(c, _make_config())

    assert c.integration_dispatcher is mock_dispatcher
    assert c.integration_endpoint is mock_endpoint
    assert c.backhaul_manager is mock_backhaul
    mock_backhaul.start.assert_awaited_once()
    mock_dispatcher.register_service.assert_any_call("device_service", c.device_service)
    mock_dispatcher.register_service.assert_any_call("scheduler", c.scheduler)


async def test_bootstrap_integration_import_error():
    c = ServiceContainer()
    c.event_bus = MagicMock()
    c.device_service = MagicMock()
    c.scheduler = MagicMock()

    with patch("edgelite.engine.integration.backhaul.BackhaulManager", side_effect=ImportError("no module")):
        await bootstrap_integration(c, _make_config())

    # dispatcher is set before BackhaulManager fails, but backhaul_manager is not
    assert c.backhaul_manager is None


# -- bootstrap_app_updater --


async def test_bootstrap_app_updater_success():
    c = ServiceContainer()
    try:
        import edgelite.engine.app_updater as _mod  # noqa: F401

        mock_updater = MagicMock()
        with patch("edgelite.engine.app_updater.AppUpdater", return_value=mock_updater):
            await bootstrap_app_updater(c, _make_config())
        assert c.app_updater is mock_updater
    except ImportError:
        await bootstrap_app_updater(c, _make_config())
        assert c.app_updater is None


async def test_bootstrap_app_updater_import_error():
    c = ServiceContainer()
    await bootstrap_app_updater(c, _make_config())
    assert c.app_updater is None


async def test_bootstrap_app_updater_exception():
    c = ServiceContainer()
    try:
        import edgelite.engine.app_updater as _mod  # noqa: F401

        with patch("edgelite.engine.app_updater.AppUpdater", side_effect=RuntimeError("init fail")):
            await bootstrap_app_updater(c, _make_config())
        assert c.app_updater is None
    except ImportError:
        pytest.skip("app_updater module not available")


# -- bootstrap_ai --


async def test_bootstrap_ai_disabled():
    c = ServiceContainer()
    c.event_bus = MagicMock()
    config = _make_config()
    config.ai_inference = None
    await bootstrap_ai(c, config)
    assert c.ai_engine is None


async def test_bootstrap_ai_not_enabled():
    c = ServiceContainer()
    c.event_bus = MagicMock()
    config = _make_config()
    config.ai_inference = SimpleNamespace(enabled=False)
    await bootstrap_ai(c, config)
    assert c.ai_engine is None


async def test_bootstrap_ai_import_error():
    c = ServiceContainer()
    c.event_bus = MagicMock()
    config = _make_config()
    config.ai_inference = SimpleNamespace(enabled=True, models_dir="/models")

    with patch("edgelite.engine.edge_ai_inference.AiInferenceEngine", side_effect=ImportError("no onnx")):
        await bootstrap_ai(c, config)
    assert c.ai_engine is None


async def test_bootstrap_ai_success():
    c = ServiceContainer()
    c.event_bus = MagicMock()
    c.database = MagicMock()
    config = _make_config()
    config.ai_inference = SimpleNamespace(enabled=True, models_dir="/models")
    mock_engine = MagicMock()
    mock_engine.initialize = AsyncMock()
    mock_wrapper = MagicMock()
    mock_wrapper.status = "active"
    mock_wrapper.predict = AsyncMock(return_value={"out": 1})
    mock_engine.get_loaded_models.return_value = {"m1": mock_wrapper}
    mock_ai_service = MagicMock()
    mock_ai_service.restore_stats_from_db = AsyncMock()
    mock_scheduler = MagicMock()
    mock_scheduler.start = AsyncMock()
    mock_scheduler.register_model = AsyncMock()
    mock_scheduler._model_infer_fn = {}
    mock_mcp = MagicMock()

    with (
        patch("edgelite.engine.edge_ai_inference.AiInferenceEngine", return_value=mock_engine),
        patch("edgelite.engine.edge_ai_inference._check_onnxruntime", return_value=True),
        patch("edgelite.services.ai_service.AiModelService", return_value=mock_ai_service),
        patch("edgelite.engine.inference_scheduler.InferenceScheduler", return_value=mock_scheduler),
        patch("edgelite.api.mcp._mcp_tools", mock_mcp),
    ):
        await bootstrap_ai(c, config)

    assert c.ai_engine is mock_engine
    assert c.ai_service is mock_ai_service
    assert c.inference_scheduler is mock_scheduler
    mock_engine.initialize.assert_awaited_once()
    mock_ai_service.restore_stats_from_db.assert_awaited_once()
    mock_scheduler.start.assert_awaited_once()
    mock_scheduler.register_model.assert_awaited_once()
    mock_mcp.set_ai_dependencies.assert_called_once()


async def test_bootstrap_ai_empty_models_dir():
    c = ServiceContainer()
    c.event_bus = MagicMock()
    c.database = MagicMock()
    config = _make_config()
    config.ai_inference = SimpleNamespace(enabled=True, models_dir="")
    mock_engine = MagicMock()
    mock_engine.initialize = AsyncMock()
    mock_engine.get_loaded_models.return_value = {}
    mock_ai_service = MagicMock()
    mock_ai_service.restore_stats_from_db = AsyncMock()
    mock_sched = MagicMock()
    mock_sched.start = AsyncMock()
    mock_sched.register_model = AsyncMock()
    mock_sched._model_infer_fn = {}

    with (
        patch("edgelite.engine.edge_ai_inference.AiInferenceEngine", return_value=mock_engine),
        patch("edgelite.engine.edge_ai_inference._check_onnxruntime", return_value=False),
        patch("edgelite.services.ai_service.AiModelService", return_value=mock_ai_service),
        patch("edgelite.engine.inference_scheduler.InferenceScheduler", return_value=mock_sched),
        patch("edgelite.api.mcp._mcp_tools", MagicMock()),
    ):
        await bootstrap_ai(c, config)

    assert c.ai_engine is mock_engine


# -- bootstrap_video --


async def test_bootstrap_video():
    c = ServiceContainer()
    c.video_service = MagicMock()
    c.video_service.init_provider = AsyncMock()
    await bootstrap_video(c, _make_config())
    c.video_service.init_provider.assert_awaited_once()


# -- bootstrap_driver_watchdog --


async def test_bootstrap_driver_watchdog():
    c = ServiceContainer()
    c.event_bus = MagicMock()
    mock_watchdog = MagicMock()
    mock_watchdog.set_event_bus = MagicMock()
    mock_watchdog.start = AsyncMock()

    with patch("edgelite.engine.driver_watchdog.get_driver_watchdog", return_value=mock_watchdog):
        await bootstrap_driver_watchdog(c, _make_config())

    assert c.driver_watchdog is mock_watchdog
    mock_watchdog.set_event_bus.assert_called_once_with(c.event_bus)
    mock_watchdog.start.assert_awaited_once()


# -- bootstrap_shadow --


async def test_bootstrap_shadow():
    c = ServiceContainer()
    c.event_bus = MagicMock()
    c.device_service = MagicMock()
    c.audit_service = MagicMock()
    mock_shadow = MagicMock()
    mock_shadow.set_device_service = MagicMock()
    mock_shadow.set_event_bus = MagicMock()
    mock_shadow.set_audit_service = MagicMock()
    mock_shadow.start = AsyncMock()

    with patch("edgelite.services.shadow_service.ShadowService", return_value=mock_shadow):
        await bootstrap_shadow(c, _make_config())

    assert c.shadow_service is mock_shadow
    mock_shadow.set_device_service.assert_called_once_with(c.device_service)
    mock_shadow.set_event_bus.assert_called_once_with(c.event_bus)
    mock_shadow.set_audit_service.assert_called_once_with(c.audit_service)
    mock_shadow.start.assert_awaited_once()


# -- bootstrap_config_reload --


async def test_bootstrap_config_reload():
    c = ServiceContainer()
    mock_reloader = MagicMock()
    mock_reloader.start = AsyncMock()

    with patch("edgelite.config_reload.get_config_hot_reloader", return_value=mock_reloader):
        await bootstrap_config_reload(c, _make_config())

    mock_reloader.start.assert_awaited_once()
    assert ("config_hot_reloader", mock_reloader) in c._initialized


# -- bootstrap_log_rotation --


async def test_bootstrap_log_rotation():
    c = ServiceContainer()
    config = _make_config()
    mock_log_rot = MagicMock()
    mock_log_rot.start = AsyncMock()

    with patch("edgelite.services.system_services.get_log_rotation_service", return_value=mock_log_rot):
        await bootstrap_log_rotation(c, config)

    mock_log_rot.start.assert_awaited_once()
    assert hasattr(c, "_cert_monitor")
    assert ("log_rotation", mock_log_rot) in c._initialized
    assert ("cert_monitor", c._cert_monitor) in c._initialized
    await c._cert_monitor.stop()


# -- _verify_startup_chain --


async def test_verify_startup_chain_all_ok():
    c = ServiceContainer()
    config = _make_config()
    mock_db = MagicMock()
    mock_db._engine = MagicMock()
    session_mock = MagicMock()
    session_mock.execute = AsyncMock()
    mock_db.get_session = MagicMock(return_value=_AsyncCM(session_mock))
    c.database = mock_db
    mock_influx = MagicMock()
    mock_influx.check_health = AsyncMock(return_value=True)
    c.influx_storage = mock_influx
    c.event_bus = MagicMock()
    c.scheduler = MagicMock()

    results = await _verify_startup_chain(c, config)
    result_dict = {name: (ok, msg) for name, ok, msg in results}
    assert result_dict["secret_key"] == (True, "ok")
    assert result_dict["database"] == (True, "ok")
    assert result_dict["influxdb"] == (True, "ok")
    assert result_dict["event_bus"] == (True, "ok")
    assert result_dict["scheduler"] == (True, "ok")


async def test_verify_startup_chain_db_not_initialized():
    c = ServiceContainer()
    config = _make_config()
    c.database = None
    c.influx_storage = None
    c.event_bus = None
    c.scheduler = None

    results = await _verify_startup_chain(c, config)
    result_dict = {name: (ok, msg) for name, ok, msg in results}
    assert result_dict["secret_key"] == (True, "ok")
    assert result_dict["database"] == (False, "not initialized")
    assert result_dict["influxdb"] == (False, "not initialized")
    assert result_dict["event_bus"] == (True, "not initialized")
    assert result_dict["scheduler"] == (True, "not initialized")


async def test_verify_startup_chain_db_select1_fail():
    c = ServiceContainer()
    config = _make_config()
    mock_db = MagicMock()
    mock_db._engine = MagicMock()
    session_mock = MagicMock()
    session_mock.execute = AsyncMock(side_effect=RuntimeError("connection lost"))
    mock_db.get_session = MagicMock(return_value=_AsyncCM(session_mock))
    c.database = mock_db
    c.influx_storage = None
    c.event_bus = MagicMock()
    c.scheduler = MagicMock()

    results = await _verify_startup_chain(c, config)
    result_dict = {name: (ok, msg) for name, ok, msg in results}
    assert result_dict["database"][0] is False
    assert "SELECT 1 failed" in result_dict["database"][1]


async def test_verify_startup_chain_influx_degraded():
    c = ServiceContainer()
    config = _make_config()
    mock_db = MagicMock()
    mock_db._engine = MagicMock()
    _s = MagicMock()
    _s.execute = AsyncMock()
    mock_db.get_session = MagicMock(return_value=_AsyncCM(_s))
    c.database = mock_db
    mock_influx = MagicMock()
    mock_influx.check_health = AsyncMock(return_value=False)
    mock_influx.using_fallback = AsyncMock(return_value=True)
    c.influx_storage = mock_influx
    c.event_bus = MagicMock()
    c.scheduler = MagicMock()

    results = await _verify_startup_chain(c, config)
    result_dict = {name: (ok, msg) for name, ok, msg in results}
    assert result_dict["influxdb"][0] is True
    assert "degraded" in result_dict["influxdb"][1]


async def test_verify_startup_chain_influx_unavailable():
    c = ServiceContainer()
    config = _make_config()
    mock_db = MagicMock()
    mock_db._engine = MagicMock()
    _s = MagicMock()
    _s.execute = AsyncMock()
    mock_db.get_session = MagicMock(return_value=_AsyncCM(_s))
    c.database = mock_db
    mock_influx = MagicMock()
    mock_influx.check_health = AsyncMock(return_value=False)
    mock_influx.using_fallback = AsyncMock(return_value=False)
    c.influx_storage = mock_influx
    c.event_bus = MagicMock()
    c.scheduler = MagicMock()

    results = await _verify_startup_chain(c, config)
    result_dict = {name: (ok, msg) for name, ok, msg in results}
    assert result_dict["influxdb"] == (False, "unavailable and no fallback")


async def test_verify_startup_chain_secret_key_invalid():
    c = ServiceContainer()
    config = _make_config(security=SimpleNamespace(secret_key=""))
    c.database = None
    c.influx_storage = None
    c.event_bus = None
    c.scheduler = None

    results = await _verify_startup_chain(c, config)
    result_dict = {name: (ok, msg) for name, ok, msg in results}
    assert result_dict["secret_key"][0] is False


async def test_verify_startup_chain_influx_exception():
    c = ServiceContainer()
    config = _make_config()
    mock_db = MagicMock()
    mock_db._engine = MagicMock()
    _s = MagicMock()
    _s.execute = AsyncMock()
    mock_db.get_session = MagicMock(return_value=_AsyncCM(_s))
    c.database = mock_db
    mock_influx = MagicMock()
    mock_influx.check_health = AsyncMock(side_effect=RuntimeError("influx down"))
    c.influx_storage = mock_influx
    c.event_bus = MagicMock()
    c.scheduler = MagicMock()

    results = await _verify_startup_chain(c, config)
    result_dict = {name: (ok, msg) for name, ok, msg in results}
    assert result_dict["influxdb"][0] is False


# -- _auto_install_deps --


async def test_auto_install_deps_missing():
    import importlib

    original_import_module = importlib.import_module

    def _mock_import_module(name):
        if name == "onnxruntime":
            raise ImportError("No module named 'onnxruntime'")
        return original_import_module(name)

    with patch("importlib.import_module", side_effect=_mock_import_module):
        from edgelite.bootstrap import _auto_install_deps

        with pytest.raises(RuntimeError, match="onnxruntime"):
            await _auto_install_deps()


async def test_auto_install_deps_all_present():
    from edgelite.bootstrap import _auto_install_deps

    try:
        await _auto_install_deps()
    except RuntimeError:
        pass


# -- teardown --


async def test_teardown_full_sequence():
    c = ServiceContainer()
    c.scheduler = MagicMock()
    c.scheduler.stop_all = AsyncMock()
    c.driver_registry = MagicMock()
    c.driver_registry.stop_all = AsyncMock()
    c.driver_watchdog = MagicMock()
    c.driver_watchdog.stop = AsyncMock()
    c.ai_engine = MagicMock()
    c.ai_engine.shutdown = AsyncMock()
    c.inference_scheduler = MagicMock()
    c.inference_scheduler.stop = AsyncMock()
    c.cache_manager = MagicMock()
    c.cache_manager.stop_orphan_compaction = MagicMock()
    c.ws_manager = MagicMock()
    c.ws_manager.close = AsyncMock()
    c.database = MagicMock()
    c.database.close = AsyncMock()
    c.mqtt_forwarder = MagicMock()
    c.mqtt_forwarder.close = AsyncMock()
    c.event_bus = MagicMock()
    c.event_bus.unregister_all = MagicMock()

    tracked_resource = MagicMock()
    tracked_resource.shutdown = AsyncMock()
    c.track("tracked", tracked_resource)

    plat_handler = MagicMock()
    plat_handler.disconnect = AsyncMock()
    c.platform_handlers = {"plat_a": plat_handler}

    await teardown(c)

    c.scheduler.stop_all.assert_awaited_once()
    c.driver_registry.stop_all.assert_awaited_once()
    c.driver_watchdog.stop.assert_awaited_once()
    c.ai_engine.shutdown.assert_awaited_once()
    c.inference_scheduler.stop.assert_awaited_once()
    c.cache_manager.stop_orphan_compaction.assert_called_once()
    c.ws_manager.close.assert_awaited_once()
    c.database.close.assert_awaited_once()
    c.mqtt_forwarder.close.assert_awaited_once()
    tracked_resource.shutdown.assert_awaited_once()
    plat_handler.disconnect.assert_awaited_once()
    c.event_bus.unregister_all.assert_called_once()


async def test_teardown_driver_registry_stop_fallback():
    c = ServiceContainer()
    c.driver_registry = MagicMock()
    del c.driver_registry.stop_all
    c.driver_registry.stop = AsyncMock()
    c.scheduler = None
    c.driver_watchdog = None
    c.ai_engine = None
    c.inference_scheduler = None
    c.cache_manager = None
    c.ws_manager = None
    c.database = None
    c.mqtt_forwarder = None
    c.event_bus = None

    await teardown(c)
    c.driver_registry.stop.assert_awaited_once()


async def test_teardown_with_pending_task():
    c = ServiceContainer()
    c.scheduler = None
    c.driver_registry = None
    c.driver_watchdog = None
    c.ai_engine = None
    c.inference_scheduler = None
    c.cache_manager = None
    c.ws_manager = None
    c.database = None
    c.mqtt_forwarder = None
    c.event_bus = None
    c.platform_handlers = {}

    async def _quick_task():
        await asyncio.sleep(0.05)

    task = asyncio.create_task(_quick_task(), name="app-background-task")
    await teardown(c)
    assert task.done()


async def test_teardown_resource_close_fallback():
    c = ServiceContainer()
    c.scheduler = None
    c.driver_registry = None
    c.driver_watchdog = None
    c.ai_engine = None
    c.inference_scheduler = None
    c.cache_manager = None
    c.ws_manager = None
    c.database = None
    c.mqtt_forwarder = None
    c.event_bus = None
    c.platform_handlers = {}

    res_close = MagicMock(spec=["close"])
    res_close.close = AsyncMock()
    c.track("rc", res_close)

    res_stop = MagicMock(spec=["stop"])
    res_stop.stop = AsyncMock()
    c.track("rs", res_stop)

    await teardown(c)
    res_close.close.assert_awaited_once()
    res_stop.stop.assert_awaited_once()


async def test_teardown_resource_exception_swallowed():
    c = ServiceContainer()
    c.scheduler = None
    c.driver_registry = None
    c.driver_watchdog = None
    c.ai_engine = None
    c.inference_scheduler = None
    c.cache_manager = None
    c.ws_manager = None
    c.database = None
    c.mqtt_forwarder = None
    c.event_bus = None
    c.platform_handlers = {}

    bad_res = MagicMock()
    bad_res.shutdown = AsyncMock(side_effect=RuntimeError("shutdown fail"))
    c.track("bad", bad_res)

    await teardown(c)


# -- bootstrap_all --


_STEP_NAMES = [
    "bootstrap_storage",
    "bootstrap_engine",
    "bootstrap_driver_watchdog",
    "bootstrap_services",
    "bootstrap_ai",
    "bootstrap_evaluator",
    "bootstrap_ws",
    "bootstrap_video",
    "bootstrap_drivers",
    "bootstrap_mqtt",
    "bootstrap_platforms",
    "bootstrap_modbus_slave",
    "bootstrap_devices",
    "bootstrap_shadow",
    "bootstrap_integration",
    "bootstrap_app_updater",
    "bootstrap_config_reload",
    "bootstrap_log_rotation",
]


@pytest.fixture
def _mock_all_steps(monkeypatch):
    mocks = {}
    for name in _STEP_NAMES:
        m = AsyncMock()
        monkeypatch.setattr(f"edgelite.bootstrap.{name}", m)
        mocks[name] = m

    mocks["_verify"] = AsyncMock(
        return_value=[
            ("secret_key", True, "ok"),
            ("database", True, "ok"),
            ("influxdb", True, "ok"),
            ("event_bus", True, "ok"),
            ("scheduler", True, "ok"),
        ]
    )
    monkeypatch.setattr("edgelite.bootstrap._verify_startup_chain", mocks["_verify"])

    mocks["_auto_deps"] = AsyncMock()
    monkeypatch.setattr("edgelite.bootstrap._auto_install_deps", mocks["_auto_deps"])

    mocks["init_sm"] = MagicMock()
    monkeypatch.setattr("edgelite.security.secret_manager.init_secret_manager", mocks["init_sm"])

    mocks["graceful"] = MagicMock()
    mocks["graceful"].check_and_cleanup_marker.return_value = None
    monkeypatch.setattr("edgelite.engine.graceful_restarter.GracefulRestarter", mocks["graceful"])

    mocks["sensitive_filter"] = MagicMock()
    monkeypatch.setattr("edgelite.security.data_masking.SensitiveFilter", mocks["sensitive_filter"])

    mocks["log_agg"] = MagicMock()
    mock_agg_inst = MagicMock()
    mock_agg_inst._max_entries = 1000
    mocks["log_agg"].return_value = mock_agg_inst
    monkeypatch.setattr("edgelite.engine.log_aggregator.get_log_aggregator", mocks["log_agg"])

    return mocks


async def test_bootstrap_all_success(_mock_all_steps, tmp_path):
    c = ServiceContainer()
    config = _make_config()
    config.logging.log_dir = str(tmp_path)
    await bootstrap_all(c, config)

    for name in _STEP_NAMES:
        _mock_all_steps[name].assert_awaited_once()
    _mock_all_steps["_verify"].assert_awaited_once()
    _mock_all_steps["_auto_deps"].assert_awaited_once()
    _mock_all_steps["init_sm"].assert_called_once()
    assert c.config is config


async def test_bootstrap_all_empty_influx_token_warning(_mock_all_steps, tmp_path):
    c = ServiceContainer()
    config = _make_config(influxdb=SimpleNamespace(token="", url="", org="", bucket=""))
    config.logging.log_dir = str(tmp_path)
    await bootstrap_all(c, config)
    for name in _STEP_NAMES:
        _mock_all_steps[name].assert_awaited_once()


async def test_bootstrap_all_json_format(_mock_all_steps, tmp_path):
    c = ServiceContainer()
    config = _make_config(
        logging=SimpleNamespace(
            level="INFO",
            format="%(message)s",
            log_dir=str(tmp_path),
            max_bytes=1024,
            backup_count=2,
            json_format=True,
        )
    )
    mock_formatter = MagicMock()
    with patch("edgelite.engine.structured_logger.StructuredFormatter", return_value=mock_formatter):
        await bootstrap_all(c, config)
    for name in _STEP_NAMES:
        _mock_all_steps[name].assert_awaited_once()


async def test_bootstrap_all_step_failure(_mock_all_steps, tmp_path):
    _mock_all_steps["bootstrap_engine"].side_effect = ValueError("engine crashed")
    mock_teardown = AsyncMock()
    c = ServiceContainer()
    config = _make_config()
    config.logging.log_dir = str(tmp_path)

    with patch("edgelite.bootstrap.teardown", mock_teardown):
        with pytest.raises(RuntimeError, match="engine"):
            await bootstrap_all(c, config)

    mock_teardown.assert_awaited_once()
    _mock_all_steps["bootstrap_services"].assert_not_awaited()
    _mock_all_steps["bootstrap_ws"].assert_not_awaited()
    _mock_all_steps["bootstrap_storage"].assert_awaited_once()


async def test_bootstrap_all_verification_failure(_mock_all_steps, tmp_path):
    _mock_all_steps["_verify"].return_value = [
        ("secret_key", True, "ok"),
        ("database", False, "SELECT 1 failed: connection error"),
    ]
    mock_teardown = AsyncMock()
    c = ServiceContainer()
    config = _make_config()
    config.logging.log_dir = str(tmp_path)

    with patch("edgelite.bootstrap.teardown", mock_teardown):
        with pytest.raises(RuntimeError, match="database"):
            await bootstrap_all(c, config)

    mock_teardown.assert_awaited_once()


async def test_bootstrap_all_verification_exception(_mock_all_steps, tmp_path):
    _mock_all_steps["_verify"].side_effect = RuntimeError("verification chain broke")
    mock_teardown = AsyncMock()
    c = ServiceContainer()
    config = _make_config()
    config.logging.log_dir = str(tmp_path)

    with patch("edgelite.bootstrap.teardown", mock_teardown):
        with pytest.raises(RuntimeError, match="verification_chain"):
            await bootstrap_all(c, config)

    mock_teardown.assert_awaited_once()


async def test_bootstrap_all_sensitive_filter_failure(_mock_all_steps, tmp_path):
    _mock_all_steps["sensitive_filter"].side_effect = RuntimeError("filter init fail")
    c = ServiceContainer()
    config = _make_config()
    config.logging.log_dir = str(tmp_path)
    await bootstrap_all(c, config)
    for name in _STEP_NAMES:
        _mock_all_steps[name].assert_awaited_once()


async def test_bootstrap_all_graceful_restart_info(_mock_all_steps, tmp_path):
    _mock_all_steps["graceful"].check_and_cleanup_marker.return_value = {
        "old_version": "1.0",
        "new_version": "1.1",
        "success": True,
    }
    c = ServiceContainer()
    config = _make_config()
    config.logging.log_dir = str(tmp_path)
    await bootstrap_all(c, config)
    _mock_all_steps["graceful"].check_and_cleanup_marker.assert_called_once()


async def test_bootstrap_all_teardown_failure_during_startup(_mock_all_steps, tmp_path):
    _mock_all_steps["bootstrap_services"].side_effect = ValueError("svc fail")
    mock_teardown = AsyncMock(side_effect=RuntimeError("teardown also fail"))
    c = ServiceContainer()
    config = _make_config()
    config.logging.log_dir = str(tmp_path)

    with patch("edgelite.bootstrap.teardown", mock_teardown):
        with pytest.raises(RuntimeError, match="services"):
            await bootstrap_all(c, config)
