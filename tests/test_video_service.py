"""视频接入服务测试

覆盖 services/video_service.py：
- VideoService: init_provider / close 生命周期
- register_video_device / get_stream_url / ptz_control / get_device_status
- handle_webhook: webhook 处理 + 告警事件发布（含异常分离处理）
- 无 provider 时的降级返回
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, "src")

from edgelite.drivers.video.provider import DeviceStatus
from edgelite.engine.event_bus import AlarmEvent
from edgelite.services.video_service import VideoService


@pytest.fixture
def event_bus():
    bus = MagicMock()
    bus.publish = AsyncMock()
    return bus


@pytest.fixture
def video_svc(event_bus):
    return VideoService(event_bus)


@pytest.fixture
def mock_provider():
    """构造一个已 connect 的 mock provider"""
    p = AsyncMock()
    p.connect = AsyncMock()
    p.close = AsyncMock()
    p.register_device = AsyncMock(return_value=True)
    p.get_stream_url = AsyncMock(return_value="rtsp://stream/url")
    p.ptz_control = AsyncMock(return_value=True)
    p.get_device_status = AsyncMock(return_value=DeviceStatus.ONLINE)
    p.handle_webhook = AsyncMock()
    return p


class TestVideoServiceInit:
    def test_initial_state(self, video_svc: VideoService):
        """新建实例无 provider"""
        assert video_svc._provider is None
        assert video_svc._event_bus is not None

    async def test_init_provider_success(self, video_svc: VideoService, mock_provider):
        """init_provider 成功时设置 provider"""
        with patch("edgelite.services.video_service.PyGBSentryProvider", return_value=mock_provider):
            await video_svc.init_provider()
        assert video_svc._provider is mock_provider
        mock_provider.connect.assert_awaited_once()

    async def test_init_provider_failure_sets_none(self, video_svc: VideoService, mock_provider):
        """init_provider connect 抛异常时 provider 置 None"""
        mock_provider.connect.side_effect = RuntimeError("connect failed")
        with patch("edgelite.services.video_service.PyGBSentryProvider", return_value=mock_provider):
            await video_svc.init_provider()
        assert video_svc._provider is None

    async def test_init_provider_constructor_failure(self, video_svc: VideoService):
        """PyGBSentryProvider 构造抛异常时 provider 置 None"""
        with patch("edgelite.services.video_service.PyGBSentryProvider", side_effect=RuntimeError("ctor fail")):
            await video_svc.init_provider()
        assert video_svc._provider is None


class TestVideoServiceClose:
    async def test_close_with_provider(self, video_svc: VideoService, mock_provider):
        video_svc._provider = mock_provider
        await video_svc.close()
        mock_provider.close.assert_awaited_once()
        assert video_svc._provider is None

    async def test_close_without_provider(self, video_svc: VideoService):
        """无 provider 时 close 不报错"""
        await video_svc.close()
        assert video_svc._provider is None

    async def test_close_idempotent(self, video_svc: VideoService, mock_provider):
        """重复 close 不操作已关闭的 provider"""
        video_svc._provider = mock_provider
        await video_svc.close()
        await video_svc.close()
        assert mock_provider.close.await_count == 1
        assert video_svc._provider is None


class TestRegisterVideoDevice:
    async def test_register_without_provider(self, video_svc: VideoService):
        """无 provider 时返回 False"""
        assert await video_svc.register_video_device("dev1", {}) is False

    async def test_register_with_provider(self, video_svc: VideoService, mock_provider):
        video_svc._provider = mock_provider
        result = await video_svc.register_video_device("dev1", {"ip": "1.2.3.4"})
        assert result is True
        mock_provider.register_device.assert_awaited_once_with("dev1", {"ip": "1.2.3.4"})

    async def test_register_provider_returns_false(self, video_svc: VideoService, mock_provider):
        mock_provider.register_device.return_value = False
        video_svc._provider = mock_provider
        assert await video_svc.register_video_device("dev1", {}) is False


class TestGetStreamUrl:
    async def test_get_stream_url_without_provider(self, video_svc: VideoService):
        """无 provider 时返回空字符串"""
        assert await video_svc.get_stream_url("dev1") == ""

    async def test_get_stream_url_with_provider(self, video_svc: VideoService, mock_provider):
        video_svc._provider = mock_provider
        url = await video_svc.get_stream_url("dev1", "2")
        assert url == "rtsp://stream/url"
        mock_provider.get_stream_url.assert_awaited_once_with("dev1", "2")

    async def test_get_stream_url_default_channel(self, video_svc: VideoService, mock_provider):
        """默认 channel_id 为 1"""
        video_svc._provider = mock_provider
        await video_svc.get_stream_url("dev1")
        mock_provider.get_stream_url.assert_awaited_once_with("dev1", "1")


class TestPtzControl:
    async def test_ptz_without_provider(self, video_svc: VideoService):
        assert await video_svc.ptz_control("dev1", "1", "left") is False

    async def test_ptz_with_provider(self, video_svc: VideoService, mock_provider):
        video_svc._provider = mock_provider
        result = await video_svc.ptz_control("dev1", "1", "left", speed=5)
        assert result is True
        mock_provider.ptz_control.assert_awaited_once_with("dev1", "1", "left", speed=5)

    async def test_ptz_provider_returns_false(self, video_svc: VideoService, mock_provider):
        mock_provider.ptz_control.return_value = False
        video_svc._provider = mock_provider
        assert await video_svc.ptz_control("dev1", "1", "right") is False


class TestGetDeviceStatus:
    async def test_status_without_provider(self, video_svc: VideoService):
        """无 provider 时返回 UNKNOWN"""
        assert await video_svc.get_device_status("dev1") == DeviceStatus.UNKNOWN

    async def test_status_with_provider(self, video_svc: VideoService, mock_provider):
        video_svc._provider = mock_provider
        status = await video_svc.get_device_status("dev1")
        assert status == DeviceStatus.ONLINE
        mock_provider.get_device_status.assert_awaited_once_with("dev1")


class TestHandleWebhook:
    async def test_webhook_without_provider(self, video_svc: VideoService, event_bus):
        """无 provider 时直接返回，不发布事件"""
        await video_svc.handle_webhook({"type": "alarm", "device_id": "dev1"})
        event_bus.publish.assert_not_called()

    async def test_webhook_non_alarm_no_publish(self, video_svc: VideoService, mock_provider, event_bus):
        """非告警事件不发布到 EventBus"""
        video_svc._provider = mock_provider
        await video_svc.handle_webhook({"type": "status_update", "device_id": "dev1"})
        mock_provider.handle_webhook.assert_awaited_once()
        event_bus.publish.assert_not_called()

    async def test_webhook_alarm_publishes_event(self, video_svc: VideoService, mock_provider, event_bus):
        """告警事件发布 AlarmEvent 到 EventBus"""
        video_svc._provider = mock_provider
        event_data = {
            "type": "motion_alarm",
            "device_id": "cam1",
            "alarm_id": "a1",
            "rule_id": "r1",
            "severity": "critical",
        }
        await video_svc.handle_webhook(event_data)
        mock_provider.handle_webhook.assert_awaited_once_with(event_data)
        event_bus.publish.assert_awaited_once()
        published_event = event_bus.publish.await_args.args[0]
        assert isinstance(published_event, AlarmEvent)
        assert published_event.alarm_id == "a1"
        assert published_event.rule_id == "r1"
        assert published_event.device_id == "cam1"
        assert published_event.severity == "critical"
        assert published_event.action == "firing"
        assert published_event.rule_type == "video"
        assert published_event.trigger_value == event_data

    async def test_webhook_alarm_default_fields(self, video_svc: VideoService, mock_provider, event_bus):
        """告警事件缺省字段使用默认值"""
        video_svc._provider = mock_provider
        event_data = {"type": "Alarm", "device_id": "cam2"}
        await video_svc.handle_webhook(event_data)
        published_event = event_bus.publish.await_args.args[0]
        assert isinstance(published_event, AlarmEvent)
        assert published_event.alarm_id == "video_cam2_Alarm"
        assert published_event.rule_id == "video_alarm"
        assert published_event.severity == "warning"
        assert published_event.device_id == "cam2"

    async def test_webhook_alarm_no_device_id(self, video_svc: VideoService, mock_provider, event_bus):
        """告警事件无 device_id 时 alarm_id 仍生成"""
        video_svc._provider = mock_provider
        await video_svc.handle_webhook({"type": "alarm_triggered"})
        published_event = event_bus.publish.await_args.args[0]
        assert published_event.alarm_id == "video__alarm_triggered"
        assert published_event.device_id == ""

    async def test_webhook_provider_error_still_publishes_alarm(
        self, video_svc: VideoService, mock_provider, event_bus
    ):
        """provider.handle_webhook 抛异常时，告警事件仍应发布（异常分离）"""
        video_svc._provider = mock_provider
        mock_provider.handle_webhook.side_effect = RuntimeError("webhook fail")
        event_data = {"type": "alarm", "device_id": "cam1"}
        await video_svc.handle_webhook(event_data)
        event_bus.publish.assert_awaited_once()
        published_event = event_bus.publish.await_args.args[0]
        assert isinstance(published_event, AlarmEvent)
        assert published_event.device_id == "cam1"

    async def test_webhook_provider_error_non_alarm_no_publish(self, video_svc: VideoService, mock_provider, event_bus):
        """provider.handle_webhook 抛异常且非告警时不发布"""
        video_svc._provider = mock_provider
        mock_provider.handle_webhook.side_effect = RuntimeError("webhook fail")
        await video_svc.handle_webhook({"type": "status", "device_id": "cam1"})
        event_bus.publish.assert_not_called()

    async def test_webhook_alarm_case_insensitive(self, video_svc: VideoService, mock_provider, event_bus):
        """告警类型匹配不区分大小写"""
        video_svc._provider = mock_provider
        await video_svc.handle_webhook({"type": "ALARM", "device_id": "cam1"})
        event_bus.publish.assert_awaited_once()

    async def test_webhook_empty_type_non_alarm(self, video_svc: VideoService, mock_provider, event_bus):
        """空 type 不触发告警发布"""
        video_svc._provider = mock_provider
        await video_svc.handle_webhook({"device_id": "cam1"})
        mock_provider.handle_webhook.assert_awaited_once()
        event_bus.publish.assert_not_called()

    async def test_webhook_full_flow_after_close(self, video_svc: VideoService, mock_provider, event_bus):
        """close 后 handle_webhook 不操作 provider"""
        video_svc._provider = mock_provider
        await video_svc.close()
        await video_svc.handle_webhook({"type": "alarm"})
        mock_provider.handle_webhook.assert_not_called()
        event_bus.publish.assert_not_called()
