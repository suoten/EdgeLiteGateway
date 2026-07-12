"""WebSocket 频道管理与事件格式化单元测试。

覆盖 src/edgelite/ws/channels.py：频道订阅/退订、事件格式化
（point_update/alarm/device_status/integration）、敏感字段脱敏、频道隔离。
"""

from __future__ import annotations

import asyncio

import pytest

from edgelite.engine.event_bus import AlarmEvent, DeviceStatusEvent, EventBus, PointUpdateEvent
from edgelite.ws.channels import (
    WebSocketChannels,
    _mask_sensitive_fields,
    _mask_url_credentials,
)
from edgelite.ws.manager import ConnectionManager

# ─── _mask_url_credentials ───


def test_mask_url_with_credentials():
    """URL 中嵌入的 user:pass 被掩码。"""
    url = "redis://user:pass@host:6379/0"
    masked = _mask_url_credentials(url)
    assert "pass" not in masked
    assert "***" in masked
    assert "host:6379" in masked


def test_mask_url_without_credentials():
    """无凭据的 URL 原样返回。"""
    url = "http://localhost:8086"
    assert _mask_url_credentials(url) == url


def test_mask_url_empty_string():
    """空字符串原样返回。"""
    assert _mask_url_credentials("") == ""


# ─── _mask_sensitive_fields ───


def test_mask_sensitive_password_field():
    """password 字段整体掩码。"""
    data = {"username": "admin", "password": "secret123"}
    masked = _mask_sensitive_fields(data)
    assert masked["username"] == "admin"
    assert masked["password"] == "***"


def test_mask_sensitive_token_field():
    """token/api_key/secret/credential 字段整体掩码。"""
    data = {
        "api_key": "ak-12345",
        "access_token": "tok-abc",
        "client_secret": "sec-xyz",
        "credential": "cred-001",
    }
    masked = _mask_sensitive_fields(data)
    for key in data:
        assert masked[key] == "***"


def test_mask_nested_dict():
    """嵌套字典中的敏感字段也被掩码。"""
    data = {"config": {"mqtt": {"password": "pw", "host": "localhost"}}}
    masked = _mask_sensitive_fields(data)
    assert masked["config"]["mqtt"]["password"] == "***"
    assert masked["config"]["mqtt"]["host"] == "localhost"


def test_mask_list_of_dicts():
    """列表中的字典敏感字段也被掩码。"""
    data = [{"name": "dev1", "token": "t1"}, {"name": "dev2", "token": "t2"}]
    masked = _mask_sensitive_fields(data)
    assert masked[0]["token"] == "***"
    assert masked[1]["token"] == "***"
    assert masked[0]["name"] == "dev1"


def test_mask_non_dict_non_list_passthrough():
    """非 dict/list/str 类型原样返回。"""
    assert _mask_sensitive_fields(42) == 42
    assert _mask_sensitive_fields(True) is True
    assert _mask_sensitive_fields(None) is None


def test_mask_url_in_non_sensitive_string():
    """非敏感字符串中的 URL 凭据也被掩码。"""
    data = {"endpoint": "redis://user:pass@host:6379"}
    masked = _mask_sensitive_fields(data)
    assert "pass" not in masked["endpoint"]
    assert "***" in masked["endpoint"]


# ─── _format_point_update ───


def test_format_point_update_valid_event():
    """PointUpdateEvent 正确格式化为 dict。"""
    event = PointUpdateEvent(
        device_id="dev1", point_name="temp", value=42.5, quality="good", timestamp=1234567890
    )
    result = WebSocketChannels._format_point_update(event)
    assert result is not None
    assert result["type"] == "point_update"
    assert result["device_id"] == "dev1"
    assert result["point_name"] == "temp"
    assert result["value"] == 42.5
    assert result["quality"] == "good"
    assert result["timestamp"] == 1234567890


def test_format_point_update_wrong_type():
    """非 PointUpdateEvent 返回 None。"""
    event = AlarmEvent(
        alarm_id="a1", rule_id="r1", rule_name="Rule", device_id="d1",
        device_name="Dev", severity="critical", action="fire", timestamp=123
    )
    assert WebSocketChannels._format_point_update(event) is None


# ─── _format_alarm ───


def test_format_alarm_valid_event():
    """AlarmEvent 正确格式化。"""
    event = AlarmEvent(
        alarm_id="a1", rule_id="r1", rule_name="High Temp", device_id="d1",
        device_name="Furnace", severity="critical", action="fire", timestamp=1234567890
    )
    result = WebSocketChannels._format_alarm(event)
    assert result is not None
    assert result["type"] == "alarm"
    assert result["alarm_id"] == "a1"
    assert result["severity"] == "critical"
    assert result["action"] == "fire"


def test_format_alarm_wrong_type():
    """非 AlarmEvent 返回 None。"""
    event = PointUpdateEvent(
        device_id="dev1", point_name="temp", value=42.5, quality="good", timestamp=123
    )
    assert WebSocketChannels._format_alarm(event) is None


# ─── _format_device_status ───


def test_format_device_status_valid_event():
    """DeviceStatusEvent 正确格式化。"""
    event = DeviceStatusEvent(
        device_id="dev1", old_status="online", new_status="offline", timestamp=1234567890
    )
    result = WebSocketChannels._format_device_status(event)
    assert result is not None
    assert result["type"] == "device_status"
    assert result["device_id"] == "dev1"
    assert result["old_status"] == "online"
    assert result["new_status"] == "offline"


def test_format_device_status_wrong_type():
    """非 DeviceStatusEvent 返回 None。"""
    event = AlarmEvent(
        alarm_id="a1", rule_id="r1", rule_name="Rule", device_id="d1",
        device_name="Dev", severity="critical", action="fire", timestamp=123
    )
    assert WebSocketChannels._format_device_status(event) is None


# ─── _format_integration ───


def test_format_integration_dict_event_masks_sensitive():
    """dict 类型集成事件经脱敏后返回。"""
    event = {"platform": "thingsboard", "token": "tb-token-123", "url": "http://tb.local"}
    result = WebSocketChannels._format_integration(event)
    assert result is not None
    assert result["platform"] == "thingsboard"
    assert result["token"] == "***"
    assert result["url"] == "http://tb.local"


def test_format_integration_filters_known_event_types():
    """AlarmEvent/DeviceStatusEvent/PointUpdateEvent 不应出现在 integration 频道。"""
    alarm = AlarmEvent(
        alarm_id="a1", rule_id="r1", rule_name="Rule", device_id="d1",
        device_name="Dev", severity="critical", action="fire", timestamp=123
    )
    assert WebSocketChannels._format_integration(alarm) is None


def test_format_integration_none_event():
    """None 事件返回 None。"""
    assert WebSocketChannels._format_integration(None) is None


def test_format_integration_object_with_model_dump():
    """有 model_dump 方法的对象经脱敏后返回。"""

    class FakeEvent:
        def model_dump(self):
            return {"name": "test", "api_key": "key-123"}

    result = WebSocketChannels._format_integration(FakeEvent())
    assert result is not None
    assert result["name"] == "test"
    assert result["api_key"] == "***"


# ─── WebSocketChannels start/stop ───


@pytest.mark.asyncio
async def test_channels_start_stop():
    """start 订阅 4 个频道并创建任务，stop 取消任务并退订。"""
    event_bus = EventBus()
    mgr = ConnectionManager()
    channels = WebSocketChannels(event_bus, mgr)
    await channels.start()
    assert len(channels._tasks) == 4
    assert len(channels._subscription_names) == 4
    await channels.stop()
    assert len(channels._tasks) == 0
    assert len(channels._subscription_names) == 0


@pytest.mark.asyncio
async def test_channels_stop_idempotent():
    """重复 stop 不抛错。"""
    event_bus = EventBus()
    mgr = ConnectionManager()
    channels = WebSocketChannels(event_bus, mgr)
    await channels.start()
    await channels.stop()
    await channels.stop()  # 不应抛错


# ─── 频道事件分发 ───


@pytest.mark.asyncio
async def test_realtime_channel_broadcasts_point_update():
    """realtime 频道广播 PointUpdateEvent。"""
    from unittest.mock import patch

    event_bus = EventBus()
    mgr = ConnectionManager(max_connections=10)

    # 模拟一个已认证连接（内联 MockWebSocket 避免跨模块导入）
    class _MockWS:
        def __init__(self):
            self._headers = {}
            self.accepted = False
            self.closed = False
            self.close_code = None
            self.sent_messages: list = []

        @property
        def headers(self):
            return self._headers

        async def accept(self):
            self.accepted = True

        async def close(self, code=1000, reason=""):
            self.closed = True
            self.close_code = code

        async def send_json(self, data):
            self.sent_messages.append(("json", data))

        async def send_text(self, text):
            self.sent_messages.append(("text", text))

    ws = _MockWS()
    with patch("edgelite.ws.manager.verify_token", return_value={"sub": "u1", "role": "admin", "username": "a"}):
        await mgr.connect(ws, "realtime", token="t1")

    channels = WebSocketChannels(event_bus, mgr)
    await channels.start()

    # 发布事件
    await event_bus.publish(
        PointUpdateEvent(device_id="dev1", point_name="temp", value=25.0, quality="good", timestamp=123)
    )
    await asyncio.sleep(0.2)  # 等待事件处理

    assert len(ws.sent_messages) > 0

    await channels.stop()
