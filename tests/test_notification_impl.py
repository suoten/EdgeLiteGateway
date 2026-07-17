"""Comprehensive unit tests for services/notification_impl.py.

Covers:
- Helper functions: _sanitize_email_header, _interpolate_template, _SafeDict,
  _check_dingtalk_host, _check_wecom_host
- Dataclasses: AlarmNotification, NotificationChannelConfig, DingTalkConfig,
  WeComConfig, EmailConfig, WebhookConfig
- NotificationChannel base: rate limit, cooldown, notify retry, session mgmt
- DingTalkChannel / WeComChannel: send + test (success, SSRF, errors, exceptions)
- EmailChannel: send + _send_sync + test + _test_connection + _format_duration
- WebhookChannel: _build_ssrf_safe_request, send (retries, 4xx, exceptions), test
- NotificationManager: register/unregister/list/enable, send_notification,
  convenience senders, escalation scheduling, test_channel, close
- Global: get_notification_manager, init_notification_manager
"""

from __future__ import annotations

import asyncio
import sys
from email.mime.multipart import MIMEMultipart
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, "src")

from edgelite.services import notification_impl as ni
from edgelite.services.notification_impl import (
    AlarmNotification,
    DingTalkChannel,
    DingTalkConfig,
    EmailChannel,
    EmailConfig,
    NotificationChannel,
    NotificationChannelConfig,
    NotificationManager,
    WebhookChannel,
    WebhookConfig,
    WeComChannel,
    WeComConfig,
    _check_dingtalk_host,
    _check_wecom_host,
    _interpolate_template,
    _sanitize_email_header,
    get_notification_manager,
    init_notification_manager,
)

# ---------------------------------------------------------------------------
# Test helpers / fixtures
# ---------------------------------------------------------------------------


def _make_notification(**overrides) -> AlarmNotification:
    """Build an AlarmNotification with sensible defaults."""
    defaults = dict(
        alarm_id="alm-1",
        rule_id="rule-1",
        rule_name="Temperature High",
        device_id="dev-1",
        device_name="Boiler-1",
        severity="critical",
        action="firing",
        message="Temp exceeded threshold",
        trigger_value={"temp": 95.5},
    )
    defaults.update(overrides)
    return AlarmNotification(**defaults)


class _MockResp:
    """Async context manager mock for aiohttp response."""

    def __init__(self, status: int = 200, json_data: dict | None = None, text: str = ""):
        self.status = status
        self._json = json_data if json_data is not None else {}
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        return self._json

    async def text(self):
        return self._text


def _make_session_mock(resp: _MockResp | None = None, exc: Exception | None = None):
    """Create a mock aiohttp ClientSession whose .post returns an async CM."""
    session = MagicMock()
    post_cm = MagicMock()
    if exc is not None:
        post_cm.__aenter__ = AsyncMock(side_effect=exc)
        post_cm.__aexit__ = AsyncMock(return_value=False)
    else:
        post_cm.__aenter__ = AsyncMock(return_value=resp or _MockResp())
        post_cm.__aexit__ = AsyncMock(return_value=False)
    session.post = MagicMock(return_value=post_cm)
    session.closed = False
    session.close = AsyncMock(return_value=None)
    return session


@pytest.fixture(autouse=True)
def _reset_global_manager():
    """Reset the global notification manager singleton between tests."""
    saved = ni._notification_manager
    ni._notification_manager = None
    yield
    ni._notification_manager = saved


@pytest.fixture(autouse=True)
def _no_sleep():
    """Patch asyncio.sleep to be a no-op across notification_impl."""
    with patch.object(ni.asyncio, "sleep", new=AsyncMock(return_value=None)):
        yield


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


class TestSanitizeEmailHeader:
    def test_strips_cr_and_lf(self):
        assert _sanitize_email_header("a\r\nb\rcc\ndd") == "abccdd"

    def test_passes_through_clean(self):
        assert _sanitize_email_header("clean value") == "clean value"

    def test_non_string_returned_as_is(self):
        assert _sanitize_email_header(123) == 123
        assert _sanitize_email_header(None) is None

    def test_empty_string(self):
        assert _sanitize_email_header("") == ""


class TestInterpolateTemplate:
    def test_replaces_known_variables(self):
        n = _make_notification()
        out = _interpolate_template("{alarm_id}/{rule_name}/{device_name}/{severity}/{action}", n)
        assert out == "alm-1/Temperature High/Boiler-1/critical/firing"

    def test_unknown_variable_kept_as_placeholder(self):
        n = _make_notification()
        out = _interpolate_template("hello {unknown_var}", n)
        assert out == "hello {unknown_var}"

    def test_empty_template_returned_as_is(self):
        n = _make_notification()
        assert _interpolate_template("", n) == ""

    def test_message_none_treated_as_empty(self):
        n = _make_notification(message=None)  # type: ignore[arg-type]
        out = _interpolate_template("M:{message}", n)
        assert out == "M:"

    def test_trigger_count_and_escalation(self):
        n = _make_notification(
            action="escalated",
            escalation_level=2,
            trigger_count=5,
            original_severity="minor",
            duration_seconds=120.0,
        )
        out = _interpolate_template("{trigger_count}/{escalation_level}/{original_severity}/{duration_seconds}", n)
        assert out == "5/2/minor/120.0"

    def test_format_map_falls_back_via_safe_dict(self):
        n = _make_notification()
        out = _interpolate_template("x {0} y", n)
        assert out == "x {0} y"


class TestSafeDict:
    def test_missing_key_returns_placeholder(self):
        d = ni._SafeDict({"a": 1})
        assert d["a"] == 1
        assert d["missing"] == "{missing}"


class TestHostChecks:
    def test_dingtalk_official_host(self):
        assert _check_dingtalk_host("https://oapi.dingtalk.com/robot/send?token=x") is True

    def test_dingtalk_wrong_host(self):
        assert _check_dingtalk_host("https://evil.example.com/robot/send") is False

    def test_dingtalk_invalid_url(self):
        assert _check_dingtalk_host("not a url") is False
        assert _check_dingtalk_host(None) is False  # type: ignore[arg-type]
        assert _check_dingtalk_host(12345) is False  # type: ignore[arg-type]

    def test_wecom_official_host(self):
        assert _check_wecom_host("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x") is True

    def test_wecom_wrong_host(self):
        assert _check_wecom_host("https://evil.example.com/") is False

    def test_wecom_invalid_url(self):
        assert _check_wecom_host("::::") is False
        assert _check_wecom_host(None) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


class TestDataclasses:
    def test_alarm_notification_defaults(self):
        n = AlarmNotification(
            alarm_id="a",
            rule_id="r",
            rule_name="rn",
            device_id="d",
            device_name="dn",
            severity="info",
            action="firing",
            message="m",
        )
        assert n.trigger_value == {}
        assert n.escalation_level == 0
        assert n.original_severity == ""
        assert n.trigger_count == 1
        assert n.duration_seconds == 0.0
        assert n.timestamp  # auto-generated

    def test_channel_config_defaults(self):
        c = NotificationChannelConfig()
        assert c.enabled is True
        assert c.max_per_minute == 10
        assert c.cooldown_seconds == 60.0
        assert c.message_template == ""

    def test_dingtalk_config_defaults(self):
        c = DingTalkConfig()
        assert c.webhook_url == ""
        assert c.secret == ""
        assert c.at_mobiles == []
        assert c.is_at_all is False

    def test_wecom_config_defaults(self):
        c = WeComConfig()
        assert c.webhook_url == ""
        assert c.corp_id == ""
        assert c.agent_id == ""

    def test_email_config_defaults(self):
        c = EmailConfig()
        assert c.smtp_host == "localhost"
        assert c.smtp_port == 587
        assert c.use_tls is True
        assert c.use_ssl is False
        assert c.to_addresses == []

    def test_webhook_config_defaults(self):
        c = WebhookConfig()
        assert c.url == ""
        assert c.method == "POST"
        assert c.headers == {}
        assert c.auth_type == "none"
        assert c.retry_count == 3
        assert c.retry_delay == 1.0


# ---------------------------------------------------------------------------
# NotificationChannel base class
# ---------------------------------------------------------------------------


class _ConcreteChannel(NotificationChannel):
    """Minimal concrete channel for base-class tests."""

    async def send(self, notification: AlarmNotification) -> bool:
        return True

    async def test(self) -> tuple[bool, str]:
        return True, "ok"


class TestNotificationChannelBase:
    def _make_channel(self, **cfg):
        config = NotificationChannelConfig(
            max_per_minute=cfg.get("max_per_minute", 10),
            cooldown_seconds=cfg.get("cooldown_seconds", 60.0),
            enabled=cfg.get("enabled", True),
            name=cfg.get("name", "concrete"),
        )
        return _ConcreteChannel(config)

    def test_name_property(self):
        ch = self._make_channel(name="custom-name")
        assert ch.name == "custom-name"
        ch2 = _ConcreteChannel(NotificationChannelConfig())
        assert ch2.name == "_ConcreteChannel"

    def test_enabled_properties(self):
        ch = self._make_channel(enabled=True)
        assert ch.is_enabled is True
        ch.set_enabled(False)
        assert ch.is_enabled is False

    def test_get_session_creates_and_reuses(self):
        ch = self._make_channel()
        with patch("edgelite.services.notification_impl.aiohttp.ClientSession") as mock_cs:
            # FIX: MagicMock 的 .closed 属性默认返回真值(MagicMock)，导致 _get_session
            # 误判已关闭而二次创建。显式置为 False 以测试复用路径。
            mock_cs.return_value.closed = False
            s1 = ch._get_session()
            s2 = ch._get_session()
            assert mock_cs.call_count == 1
            assert s1 is s2

    async def test_get_session_recreates_when_closed(self):
        ch = self._make_channel()
        with patch("edgelite.services.notification_impl.aiohttp.ClientSession") as mock_cs:
            mock_cs.return_value.closed = True
            ch._get_session()
            ch._get_session()
            assert mock_cs.call_count == 2

    async def test_close_releases_session(self):
        ch = self._make_channel()
        sess = MagicMock()
        sess.closed = False
        sess.close = AsyncMock()
        ch._session = sess
        await ch.close()
        sess.close.assert_awaited_once()
        assert ch._session is None

    async def test_close_when_no_session(self):
        ch = self._make_channel()
        await ch.close()
        assert ch._session is None

    async def test_close_when_already_closed_session(self):
        ch = self._make_channel()
        sess = MagicMock()
        sess.closed = True
        sess.close = AsyncMock()
        ch._session = sess
        await ch.close()
        sess.close.assert_not_called()

    def test_check_rate_limit_within_limit(self):
        ch = self._make_channel(max_per_minute=3)
        assert ch._check_rate_limit("k") is True
        assert ch._check_rate_limit("k") is True
        assert ch._check_rate_limit("k") is True
        assert ch._check_rate_limit("k") is False

    def test_check_rate_limit_different_keys_independent(self):
        ch = self._make_channel(max_per_minute=1)
        assert ch._check_rate_limit("k1") is True
        assert ch._check_rate_limit("k2") is True
        assert ch._check_rate_limit("k1") is False

    def test_check_rate_limit_minute_reset(self):
        ch = self._make_channel(max_per_minute=1)
        assert ch._check_rate_limit("k") is True
        assert ch._check_rate_limit("k") is False
        ch._last_sent["_minute"] = "forced-old-minute"
        assert ch._check_rate_limit("k") is True

    def test_check_rate_limit_expires_old_keys(self):
        ch = self._make_channel(max_per_minute=10, cooldown_seconds=0.1)
        ch._check_rate_limit("k")
        ch._last_sent["old"] = 0.0
        ch._last_sent["_minute"] = "trigger-reset"
        ch._check_rate_limit("k2")
        assert "old" not in ch._last_sent

    def test_check_rate_limit_enforces_max_keys(self):
        ch = self._make_channel(max_per_minute=100, cooldown_seconds=100.0)
        for i in range(10005):
            ch._last_sent[f"k{i}"] = float(i)
        ch._last_sent["_minute"] = "trigger-reset"
        ch._check_rate_limit("newkey")
        assert len(ch._last_sent) <= 10001

    def test_check_cooldown_blocks_within_period(self):
        ch = self._make_channel(cooldown_seconds=60.0)
        assert ch._check_cooldown("k") is True
        assert ch._check_cooldown("k") is False

    def test_check_cooldown_allows_after_period(self):
        ch = self._make_channel(cooldown_seconds=0.0)
        assert ch._check_cooldown("k") is True
        assert ch._check_cooldown("k") is True

    async def test_notify_disabled_channel(self):
        ch = self._make_channel(enabled=False)
        ch.send = AsyncMock(return_value=True)
        result = await ch.notify(_make_notification())
        assert result is False
        ch.send.assert_not_awaited()

    async def test_notify_cooldown_blocks(self):
        ch = self._make_channel(cooldown_seconds=60.0)
        ch.send = AsyncMock(return_value=True)
        n = _make_notification()
        assert await ch.notify(n) is True
        assert await ch.notify(n) is False
        ch.send.assert_awaited_once()

    async def test_notify_rate_limit_blocks(self):
        ch = self._make_channel(max_per_minute=1, cooldown_seconds=0.0)
        ch.send = AsyncMock(return_value=True)
        n1 = _make_notification(alarm_id="a1")
        n2 = _make_notification(alarm_id="a2")
        assert await ch.notify(n1) is True
        assert await ch.notify(n2) is False
        ch.send.assert_awaited_once()

    async def test_notify_retries_on_failure(self):
        ch = self._make_channel(cooldown_seconds=0.0)
        ch.send = AsyncMock(side_effect=[False, False, True])
        ch._notify_retry_count = 3
        assert await ch.notify(_make_notification()) is True
        assert ch.send.await_count == 3

    async def test_notify_retries_on_exception(self):
        ch = self._make_channel(cooldown_seconds=0.0)
        ch.send = AsyncMock(side_effect=[RuntimeError("boom"), True])
        ch._notify_retry_count = 2
        assert await ch.notify(_make_notification()) is True
        assert ch.send.await_count == 2

    async def test_notify_all_retries_fail_with_exception(self):
        ch = self._make_channel(cooldown_seconds=0.0)
        ch.send = AsyncMock(side_effect=RuntimeError("always fail"))
        ch._notify_retry_count = 2
        assert await ch.notify(_make_notification()) is False
        assert ch.send.await_count == 2

    async def test_notify_all_retries_fail_with_false(self):
        ch = self._make_channel(cooldown_seconds=0.0)
        ch.send = AsyncMock(return_value=False)
        ch._notify_retry_count = 2
        assert await ch.notify(_make_notification()) is False
        assert ch.send.await_count == 2


# ---------------------------------------------------------------------------
# DingTalkChannel
# ---------------------------------------------------------------------------


class TestDingTalkChannel:
    def _make_channel(self, **cfg):
        defaults = dict(
            webhook_url="https://oapi.dingtalk.com/robot/send?token=x",
            at_mobiles=["13800000000"],
            is_at_all=False,
            message_template="",
        )
        defaults.update(cfg)
        return DingTalkChannel(DingTalkConfig(**defaults))

    async def test_send_no_url(self):
        ch = self._make_channel(webhook_url="")
        assert await ch.send(_make_notification()) is False

    async def test_send_ssrf_blocked(self):
        ch = self._make_channel(webhook_url="https://evil.example.com/x")
        assert await ch.send(_make_notification()) is False

    async def test_send_success(self):
        ch = self._make_channel()
        sess = _make_session_mock(_MockResp(200, {"errcode": 0}))
        ch._get_session = lambda: sess
        assert await ch.send(_make_notification()) is True
        sess.post.assert_called_once()

    async def test_send_api_error(self):
        ch = self._make_channel()
        sess = _make_session_mock(_MockResp(200, {"errcode": 310000, "errmsg": "bad"}))
        ch._get_session = lambda: sess
        assert await ch.send(_make_notification()) is False

    async def test_send_http_error_status(self):
        ch = self._make_channel()
        sess = _make_session_mock(_MockResp(500, {"errcode": 0}))
        ch._get_session = lambda: sess
        assert await ch.send(_make_notification()) is False

    async def test_send_exception(self):
        ch = self._make_channel()
        sess = _make_session_mock(exc=RuntimeError("network down"))
        ch._get_session = lambda: sess
        assert await ch.send(_make_notification()) is False

    async def test_send_with_escalation_and_template(self):
        ch = self._make_channel(message_template="[{severity}] {rule_name}")
        sess = _make_session_mock(_MockResp(200, {"errcode": 0}))
        ch._get_session = lambda: sess
        n = _make_notification(action="escalated", escalation_level=2, original_severity="minor")
        assert await ch.send(n) is True

    async def test_send_recovered_with_duration(self):
        ch = self._make_channel()
        sess = _make_session_mock(_MockResp(200, {"errcode": 0}))
        ch._get_session = lambda: sess
        n = _make_notification(action="recovered", duration_seconds=125.0)
        assert await ch.send(n) is True

    async def test_send_with_at_all(self):
        ch = self._make_channel(is_at_all=True)
        sess = _make_session_mock(_MockResp(200, {"errcode": 0}))
        ch._get_session = lambda: sess
        assert await ch.send(_make_notification()) is True

    async def test_test_no_url(self):
        ch = self._make_channel(webhook_url="")
        ok, msg = await ch.test()
        assert ok is False
        assert "not configured" in msg

    async def test_test_ssrf_blocked(self):
        ch = self._make_channel(webhook_url="https://evil.example.com/x")
        ok, msg = await ch.test()
        assert ok is False
        assert "not allowed" in msg

    async def test_test_success(self):
        ch = self._make_channel()
        sess = _make_session_mock(_MockResp(200, {"errcode": 0}))
        ch._get_session = lambda: sess
        ok, msg = await ch.test()
        assert ok is True
        assert "successfully" in msg

    async def test_test_api_error(self):
        ch = self._make_channel()
        sess = _make_session_mock(_MockResp(200, {"errcode": 1, "errmsg": "denied"}))
        ch._get_session = lambda: sess
        ok, msg = await ch.test()
        assert ok is False
        assert "denied" in msg

    async def test_test_exception(self):
        ch = self._make_channel()
        sess = _make_session_mock(exc=ConnectionError("refused"))
        ch._get_session = lambda: sess
        ok, msg = await ch.test()
        assert ok is False
        assert "failed" in msg

    def test_format_duration(self):
        assert DingTalkChannel._format_duration(45) == "45s"
        assert DingTalkChannel._format_duration(90) == "1.5m"
        assert DingTalkChannel._format_duration(5400) == "1.5h"
        assert DingTalkChannel._format_duration(180000) == "2.1d"


# ---------------------------------------------------------------------------
# WeComChannel
# ---------------------------------------------------------------------------


class TestWeComChannel:
    def _make_channel(self, **cfg):
        defaults = dict(
            webhook_url="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x",
            message_template="",
        )
        defaults.update(cfg)
        return WeComChannel(WeComConfig(**defaults))

    async def test_send_no_url(self):
        ch = self._make_channel(webhook_url="")
        assert await ch.send(_make_notification()) is False

    async def test_send_ssrf_blocked(self):
        ch = self._make_channel(webhook_url="https://evil.example.com/x")
        assert await ch.send(_make_notification()) is False

    async def test_send_success(self):
        ch = self._make_channel()
        sess = _make_session_mock(_MockResp(200, {"errcode": 0}))
        ch._get_session = lambda: sess
        assert await ch.send(_make_notification()) is True

    async def test_send_api_error(self):
        ch = self._make_channel()
        sess = _make_session_mock(_MockResp(200, {"errcode": 93000, "errmsg": "bad"}))
        ch._get_session = lambda: sess
        assert await ch.send(_make_notification()) is False

    async def test_send_exception(self):
        ch = self._make_channel()
        sess = _make_session_mock(exc=RuntimeError("net"))
        ch._get_session = lambda: sess
        assert await ch.send(_make_notification()) is False

    async def test_send_truncates_trigger_values(self):
        ch = self._make_channel()
        sess = _make_session_mock(_MockResp(200, {"errcode": 0}))
        ch._get_session = lambda: sess
        n = _make_notification(trigger_value={f"k{i}": i for i in range(8)})
        assert await ch.send(n) is True

    async def test_send_with_template(self):
        ch = self._make_channel(message_template="{rule_name} {severity}")
        sess = _make_session_mock(_MockResp(200, {"errcode": 0}))
        ch._get_session = lambda: sess
        assert await ch.send(_make_notification()) is True

    async def test_send_escalated(self):
        ch = self._make_channel()
        sess = _make_session_mock(_MockResp(200, {"errcode": 0}))
        ch._get_session = lambda: sess
        n = _make_notification(action="escalated", escalation_level=1, original_severity="minor")
        assert await ch.send(n) is True

    async def test_test_no_url(self):
        ch = self._make_channel(webhook_url="")
        ok, msg = await ch.test()
        assert ok is False
        assert "not configured" in msg

    async def test_test_ssrf_blocked(self):
        ch = self._make_channel(webhook_url="https://evil.example.com/x")
        ok, msg = await ch.test()
        assert ok is False
        assert "not allowed" in msg

    async def test_test_success(self):
        ch = self._make_channel()
        sess = _make_session_mock(_MockResp(200, {"errcode": 0}))
        ch._get_session = lambda: sess
        ok, msg = await ch.test()
        assert ok is True

    async def test_test_api_error(self):
        ch = self._make_channel()
        sess = _make_session_mock(_MockResp(200, {"errcode": 1, "errmsg": "denied"}))
        ch._get_session = lambda: sess
        ok, msg = await ch.test()
        assert ok is False
        assert "denied" in msg

    async def test_test_exception(self):
        ch = self._make_channel()
        sess = _make_session_mock(exc=ConnectionError("refused"))
        ch._get_session = lambda: sess
        ok, msg = await ch.test()
        assert ok is False
        assert "failed" in msg


# ---------------------------------------------------------------------------
# EmailChannel
# ---------------------------------------------------------------------------


class TestEmailChannel:
    def _make_channel(self, **cfg):
        defaults = dict(
            smtp_host="smtp.example.com",
            smtp_port=587,
            smtp_user="user",
            smtp_password="pw",
            from_address="from@example.com",
            to_addresses=["to@example.com"],
            use_tls=True,
            use_ssl=False,
            message_template="",
        )
        defaults.update(cfg)
        return EmailChannel(EmailConfig(**defaults))

    async def test_send_no_recipients(self):
        ch = self._make_channel(to_addresses=[])
        assert await ch.send(_make_notification()) is False

    async def test_send_no_from_address(self):
        ch = self._make_channel(from_address="")
        assert await ch.send(_make_notification()) is False

    async def test_send_success(self):
        ch = self._make_channel()
        ch._send_sync = MagicMock()
        assert await ch.send(_make_notification()) is True
        ch._send_sync.assert_called_once()

    async def test_send_send_sync_exception(self):
        ch = self._make_channel()
        ch._send_sync = MagicMock(side_effect=RuntimeError("smtp fail"))
        assert await ch.send(_make_notification()) is False

    async def test_send_with_template(self):
        ch = self._make_channel(message_template="T:{rule_name}")
        ch._send_sync = MagicMock()
        assert await ch.send(_make_notification()) is True

    async def test_send_escalated(self):
        ch = self._make_channel()
        ch._send_sync = MagicMock()
        n = _make_notification(action="escalated", escalation_level=1, original_severity="minor")
        assert await ch.send(n) is True

    async def test_send_recovered_with_duration(self):
        ch = self._make_channel()
        ch._send_sync = MagicMock()
        n = _make_notification(action="recovered", duration_seconds=125.0)
        assert await ch.send(n) is True

    async def test_send_with_trigger_values(self):
        ch = self._make_channel()
        ch._send_sync = MagicMock()
        n = _make_notification(trigger_value={"a": 1, "b": 2})
        assert await ch.send(n) is True

    def test_send_sync_tls(self):
        ch = self._make_channel(use_tls=True, use_ssl=False)
        with patch("edgelite.services.notification_impl.smtplib.SMTP") as mock_smtp:
            server = MagicMock()
            mock_smtp.return_value = server
            msg = MIMEMultipart("alternative")
            ch._send_sync(msg)
            mock_smtp.assert_called_once_with("smtp.example.com", 587, timeout=30)
            server.starttls.assert_called_once()
            server.login.assert_called_once_with("user", "pw")
            server.sendmail.assert_called_once()
            server.quit.assert_called_once()

    def test_send_sync_ssl(self):
        ch = self._make_channel(use_ssl=True, use_tls=True)
        with patch("edgelite.services.notification_impl.smtplib.SMTP_SSL") as mock_ssl:
            server = MagicMock()
            mock_ssl.return_value = server
            msg = MIMEMultipart("alternative")
            ch._send_sync(msg)
            mock_ssl.assert_called_once_with("smtp.example.com", 587, timeout=30)
            server.starttls.assert_not_called()
            server.login.assert_called_once()
            server.sendmail.assert_called_once()

    def test_send_sync_no_auth(self):
        ch = self._make_channel(smtp_user="", smtp_password="")
        with patch("edgelite.services.notification_impl.smtplib.SMTP") as mock_smtp:
            server = MagicMock()
            mock_smtp.return_value = server
            msg = MIMEMultipart("alternative")
            ch._send_sync(msg)
            server.login.assert_not_called()
            server.sendmail.assert_called_once()

    def test_send_sync_quit_in_finally_on_error(self):
        ch = self._make_channel()
        with patch("edgelite.services.notification_impl.smtplib.SMTP") as mock_smtp:
            server = MagicMock()
            server.sendmail.side_effect = RuntimeError("boom")
            mock_smtp.return_value = server
            msg = MIMEMultipart("alternative")
            with pytest.raises(RuntimeError):
                ch._send_sync(msg)
            server.quit.assert_called_once()

    async def test_test_no_host(self):
        ch = self._make_channel(smtp_host="")
        ok, msg = await ch.test()
        assert ok is False
        assert "not configured" in msg

    async def test_test_success(self):
        ch = self._make_channel()
        ch._test_connection = MagicMock()
        ok, msg = await ch.test()
        assert ok is True
        assert "successful" in msg

    async def test_test_exception(self):
        ch = self._make_channel()
        ch._test_connection = MagicMock(side_effect=RuntimeError("conn fail"))
        ok, msg = await ch.test()
        assert ok is False
        assert "failed" in msg

    def test_test_connection_tls(self):
        ch = self._make_channel(use_tls=True, use_ssl=False)
        with patch("edgelite.services.notification_impl.smtplib.SMTP") as mock_smtp:
            server = MagicMock()
            mock_smtp.return_value = server
            ch._test_connection()
            mock_smtp.assert_called_once()
            server.starttls.assert_called_once()
            server.login.assert_called_once()
            server.quit.assert_called_once()

    def test_test_connection_ssl(self):
        ch = self._make_channel(use_ssl=True)
        with patch("edgelite.services.notification_impl.smtplib.SMTP_SSL") as mock_ssl:
            server = MagicMock()
            mock_ssl.return_value = server
            ch._test_connection()
            mock_ssl.assert_called_once()
            server.starttls.assert_not_called()

    def test_format_duration(self):
        assert EmailChannel._format_duration(45) == "45 seconds"
        assert EmailChannel._format_duration(90) == "1.5 minutes"
        assert EmailChannel._format_duration(5400) == "1.5 hours"
        assert EmailChannel._format_duration(180000) == "2.1 days"


# ---------------------------------------------------------------------------
# WebhookChannel
# ---------------------------------------------------------------------------


class TestWebhookChannel:
    def _make_channel(self, **cfg):
        defaults = dict(
            url="https://example.com/hook",
            method="POST",
            headers={},
            auth_type="none",
            auth_token="",
            auth_username="",
            auth_password="",
            retry_count=2,
            retry_delay=0.0,
            message_template="",
        )
        defaults.update(cfg)
        return WebhookChannel(WebhookConfig(**defaults))

    async def test_send_no_url(self):
        ch = self._make_channel(url="")
        assert await ch.send(_make_notification()) is False

    async def test_send_ssrf_blocked(self):
        ch = self._make_channel(url="https://localhost/hook")
        with patch.object(ni, "_validate_webhook_url", new=AsyncMock(return_value=False)):
            assert await ch.send(_make_notification()) is False

    async def test_send_invalid_method(self):
        ch = self._make_channel(method="NOTAMETHOD")
        with patch.object(ni, "_validate_webhook_url", new=AsyncMock(return_value=True)):
            sess = MagicMock()
            sess.closed = False
            ch._get_session = lambda: sess
            assert await ch.send(_make_notification()) is False

    async def test_send_success(self):
        ch = self._make_channel()
        sess = _make_session_mock(_MockResp(200, text="ok"))
        ch._get_session = lambda: sess
        with patch.object(ni, "_validate_webhook_url", new=AsyncMock(return_value=True)):
            assert await ch.send(_make_notification()) is True
        sess.post.assert_called_once()

    async def test_send_success_2xx_range(self):
        ch = self._make_channel()
        sess = _make_session_mock(_MockResp(201, text="created"))
        ch._get_session = lambda: sess
        with patch.object(ni, "_validate_webhook_url", new=AsyncMock(return_value=True)):
            assert await ch.send(_make_notification()) is True

    async def test_send_server_error_retries(self):
        ch = self._make_channel(retry_count=3)
        sess = _make_session_mock(_MockResp(500, text="err"))
        ch._get_session = lambda: sess
        with patch.object(ni, "_validate_webhook_url", new=AsyncMock(return_value=True)):
            assert await ch.send(_make_notification()) is False
        assert sess.post.call_count == 3

    async def test_send_client_error_no_retry(self):
        ch = self._make_channel(retry_count=3)
        sess = _make_session_mock(_MockResp(404, text="not found"))
        ch._get_session = lambda: sess
        with patch.object(ni, "_validate_webhook_url", new=AsyncMock(return_value=True)):
            assert await ch.send(_make_notification()) is False
        assert sess.post.call_count == 1

    async def test_send_429_retries(self):
        ch = self._make_channel(retry_count=2)
        sess = _make_session_mock(_MockResp(429, text="rate"))
        ch._get_session = lambda: sess
        with patch.object(ni, "_validate_webhook_url", new=AsyncMock(return_value=True)):
            assert await ch.send(_make_notification()) is False
        assert sess.post.call_count == 2

    async def test_send_exception_retries(self):
        ch = self._make_channel(retry_count=2)
        sess = _make_session_mock(exc=RuntimeError("net"))
        ch._get_session = lambda: sess
        with patch.object(ni, "_validate_webhook_url", new=AsyncMock(return_value=True)):
            assert await ch.send(_make_notification()) is False
        assert sess.post.call_count == 2

    async def test_send_bearer_auth(self):
        ch = self._make_channel(auth_type="bearer", auth_token="tok123")
        sess = _make_session_mock(_MockResp(200, text="ok"))
        ch._get_session = lambda: sess
        with patch.object(ni, "_validate_webhook_url", new=AsyncMock(return_value=True)):
            assert await ch.send(_make_notification()) is True
        _, kwargs = sess.post.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer tok123"

    async def test_send_api_key_auth(self):
        ch = self._make_channel(auth_type="api_key", auth_token="key123")
        sess = _make_session_mock(_MockResp(200, text="ok"))
        ch._get_session = lambda: sess
        with patch.object(ni, "_validate_webhook_url", new=AsyncMock(return_value=True)):
            assert await ch.send(_make_notification()) is True
        _, kwargs = sess.post.call_args
        assert kwargs["headers"]["X-API-Key"] == "key123"

    async def test_send_with_template(self):
        ch = self._make_channel(message_template="T:{rule_name}")
        sess = _make_session_mock(_MockResp(200, text="ok"))
        ch._get_session = lambda: sess
        with patch.object(ni, "_validate_webhook_url", new=AsyncMock(return_value=True)):
            assert await ch.send(_make_notification()) is True

    async def test_send_escalated_payload(self):
        ch = self._make_channel()
        sess = _make_session_mock(_MockResp(200, text="ok"))
        ch._get_session = lambda: sess
        n = _make_notification(action="escalated", escalation_level=1, original_severity="minor")
        with patch.object(ni, "_validate_webhook_url", new=AsyncMock(return_value=True)):
            assert await ch.send(n) is True

    async def test_send_recovered_payload(self):
        ch = self._make_channel()
        sess = _make_session_mock(_MockResp(200, text="ok"))
        ch._get_session = lambda: sess
        n = _make_notification(action="recovered", duration_seconds=42.0)
        with patch.object(ni, "_validate_webhook_url", new=AsyncMock(return_value=True)):
            assert await ch.send(n) is True

    async def test_test_no_url(self):
        ch = self._make_channel(url="")
        ok, msg = await ch.test()
        assert ok is False
        assert "not configured" in msg

    async def test_test_ssrf_blocked(self):
        ch = self._make_channel(url="https://localhost/x")
        with patch.object(ni, "_validate_webhook_url", new=AsyncMock(return_value=False)):
            ok, msg = await ch.test()
            assert ok is False
            assert "rejected" in msg

    async def test_test_invalid_method(self):
        ch = self._make_channel(method="BADMETHOD")
        with patch.object(ni, "_validate_webhook_url", new=AsyncMock(return_value=True)):
            sess = MagicMock()
            sess.closed = False
            ch._get_session = lambda: sess
            ok, msg = await ch.test()
            assert ok is False
            assert "Invalid HTTP method" in msg

    async def test_test_success(self):
        ch = self._make_channel()
        sess = _make_session_mock(_MockResp(200, text="ok"))
        ch._get_session = lambda: sess
        with patch.object(ni, "_validate_webhook_url", new=AsyncMock(return_value=True)):
            ok, msg = await ch.test()
            assert ok is True
            assert "successful" in msg

    async def test_test_failure_status(self):
        ch = self._make_channel()
        sess = _make_session_mock(_MockResp(500, text="srv err"))
        ch._get_session = lambda: sess
        with patch.object(ni, "_validate_webhook_url", new=AsyncMock(return_value=True)):
            ok, msg = await ch.test()
            assert ok is False
            assert "500" in msg

    async def test_test_exception(self):
        ch = self._make_channel()
        sess = _make_session_mock(exc=ConnectionError("refused"))
        ch._get_session = lambda: sess
        with patch.object(ni, "_validate_webhook_url", new=AsyncMock(return_value=True)):
            ok, msg = await ch.test()
            assert ok is False
            assert "failed" in msg

    def test_build_ssrf_safe_request_no_cache(self):
        ch = self._make_channel(url="https://example.com/path")
        with patch.object(ni, "_webhook_ip_cache", {}):
            req_url, host, sni = ch._build_ssrf_safe_request()
        assert req_url == "https://example.com/path"
        assert host is None
        assert sni is None

    def test_build_ssrf_safe_request_with_cached_ip(self):
        ch = self._make_channel(url="https://example.com:8443/path?q=1")
        with patch.object(ni, "_webhook_ip_cache", {"example.com": "1.2.3.4"}):
            req_url, host, sni = ch._build_ssrf_safe_request()
        assert "1.2.3.4" in req_url
        assert "8443" in req_url
        assert host == "example.com"
        assert sni == "example.com"

    def test_build_ssrf_safe_request_ipv6(self):
        ch = self._make_channel(url="https://example.com/path")
        with patch.object(ni, "_webhook_ip_cache", {"example.com": "::1"}):
            req_url, host, sni = ch._build_ssrf_safe_request()
        assert "[::1]" in req_url
        assert host == "example.com"

    def test_build_ssrf_safe_request_http_no_sni(self):
        ch = self._make_channel(url="http://example.com/path")
        with patch.object(ni, "_webhook_ip_cache", {"example.com": "1.2.3.4"}):
            req_url, host, sni = ch._build_ssrf_safe_request()
        assert "1.2.3.4" in req_url
        assert sni is None

    def test_build_ssrf_safe_request_ip_same_as_hostname(self):
        ch = self._make_channel(url="https://1.2.3.4/path")
        with patch.object(ni, "_webhook_ip_cache", {"1.2.3.4": "1.2.3.4"}):
            req_url, host, sni = ch._build_ssrf_safe_request()
        assert req_url == "https://1.2.3.4/path"
        assert host is None


# ---------------------------------------------------------------------------
# NotificationManager
# ---------------------------------------------------------------------------


class TestNotificationManager:
    def _make_channel(self, name: str = "ch", enabled: bool = True) -> MagicMock:
        ch = AsyncMock()
        ch.name = name
        ch.is_enabled = enabled
        ch.set_enabled = MagicMock()
        ch.close = AsyncMock()
        ch.notify = AsyncMock(return_value=True)
        ch.test = AsyncMock(return_value=(True, "ok"))
        return ch

    async def test_register_and_get_channel(self):
        mgr = NotificationManager()
        ch = self._make_channel()
        await mgr.register_channel("ch1", ch)
        assert mgr.get_channel("ch1") is ch
        assert mgr.get_channel("missing") is None

    async def test_register_channel_closes_old(self):
        mgr = NotificationManager()
        old = self._make_channel()
        new = self._make_channel()
        await mgr.register_channel("ch1", old)
        await mgr.register_channel("ch1", new)
        assert mgr.get_channel("ch1") is new
        old.close.assert_awaited_once()

    async def test_register_channel_close_old_exception_swallowed(self):
        mgr = NotificationManager()
        old = self._make_channel()
        old.close = AsyncMock(side_effect=RuntimeError("close fail"))
        new = self._make_channel()
        await mgr.register_channel("ch1", old)
        await mgr.register_channel("ch1", new)
        assert mgr.get_channel("ch1") is new

    async def test_unregister_channel(self):
        mgr = NotificationManager()
        ch = self._make_channel()
        await mgr.register_channel("ch1", ch)
        await mgr.unregister_channel("ch1")
        assert mgr.get_channel("ch1") is None
        ch.close.assert_awaited_once()

    async def test_unregister_unknown_channel_noop(self):
        mgr = NotificationManager()
        await mgr.unregister_channel("missing")

    async def test_list_channels(self):
        mgr = NotificationManager()
        ch1 = self._make_channel(name="A")
        ch2 = self._make_channel(name="B")
        await mgr.register_channel("a", ch1)
        await mgr.register_channel("b", ch2)
        listed = mgr.list_channels()
        assert len(listed) == 2
        ids = {item["id"] for item in listed}
        assert ids == {"a", "b"}
        for item in listed:
            assert "name" in item and "enabled" in item

    async def test_set_channel_enabled(self):
        mgr = NotificationManager()
        ch = self._make_channel()
        await mgr.register_channel("ch1", ch)
        assert mgr.set_channel_enabled("ch1", False) is True
        ch.set_enabled.assert_called_once_with(False)
        assert mgr.set_channel_enabled("missing", True) is False

    def test_set_escalation_threshold(self):
        mgr = NotificationManager()
        mgr.set_escalation_threshold("critical", 120)
        assert mgr._escalation_thresholds["critical"] == 120

    def test_configure_channel(self):
        mgr = NotificationManager()
        assert mgr.configure_channel("ch1", {"k": "v"}) is True
        assert mgr._channel_configs["ch1"] == {"k": "v"}

    async def test_send_notification_all_channels(self):
        mgr = NotificationManager()
        ch1 = self._make_channel()
        ch2 = self._make_channel()
        await mgr.register_channel("a", ch1)
        await mgr.register_channel("b", ch2)
        results = await mgr.send_notification(_make_notification())
        assert results == {"a": True, "b": True}

    async def test_send_notification_specific_channels(self):
        mgr = NotificationManager()
        ch1 = self._make_channel()
        ch2 = self._make_channel()
        await mgr.register_channel("a", ch1)
        await mgr.register_channel("b", ch2)
        results = await mgr.send_notification(_make_notification(), channel_ids=["a"])
        assert results == {"a": True}
        ch2.notify.assert_not_awaited()

    async def test_send_notification_unknown_channel_id_ignored(self):
        mgr = NotificationManager()
        ch1 = self._make_channel()
        await mgr.register_channel("a", ch1)
        results = await mgr.send_notification(_make_notification(), channel_ids=["a", "unknown"])
        assert results == {"a": True}

    async def test_send_notification_exception_in_channel(self):
        mgr = NotificationManager()
        ch1 = self._make_channel()
        ch1.notify = AsyncMock(side_effect=RuntimeError("boom"))
        await mgr.register_channel("a", ch1)
        results = await mgr.send_notification(_make_notification())
        assert results == {"a": False}

    async def test_send_notification_timeout_marks_failure(self):
        mgr = NotificationManager()
        ch1 = self._make_channel()

        async def hanging_notify(_):
            await asyncio.get_event_loop().create_future()
            return True

        ch1.notify = hanging_notify
        await mgr.register_channel("a", ch1)

        # FIX: 原测试恢复真实 asyncio.sleep 后依赖 send_notification 内部的 15s
        # asyncio.wait 超时，导致测试在 Windows 上挂起 15s（pytest-timeout 无法
        # 杀死线程）。改为 mock asyncio.wait 立即返回所有任务为 pending，模拟
        # 超时场景，无需真实等待。
        async def _instant_timeout(tasks, timeout=None):
            return set(), set(tasks)

        with patch.object(ni.asyncio, "wait", new=_instant_timeout):
            results = await mgr.send_notification(_make_notification())
        assert results == {"a": False}

    async def test_send_alarm_fired(self):
        mgr = NotificationManager()
        ch1 = self._make_channel()
        await mgr.register_channel("a", ch1)
        results = await mgr.send_alarm_fired("a1", "r1", "rn", "d1", "dn", "critical", "msg", {"v": 1})
        assert results == {"a": True}
        ch1.notify.assert_awaited_once()
        n = ch1.notify.await_args.args[0]
        assert n.action == "firing"
        assert n.alarm_id == "a1"

    async def test_send_alarm_recovered(self):
        mgr = NotificationManager()
        ch1 = self._make_channel()
        await mgr.register_channel("a", ch1)
        results = await mgr.send_alarm_recovered("a1", "r1", "rn", "d1", "dn", "major", 300.0)
        assert results == {"a": True}
        n = ch1.notify.await_args.args[0]
        assert n.action == "recovered"
        assert n.duration_seconds == 300.0

    async def test_send_alarm_acknowledged(self):
        mgr = NotificationManager()
        ch1 = self._make_channel()
        await mgr.register_channel("a", ch1)
        results = await mgr.send_alarm_acknowledged("a1", "r1", "rn", "d1", "dn", "info", "user-1")
        assert results == {"a": True}
        n = ch1.notify.await_args.args[0]
        assert n.action == "acknowledged"
        assert "user-1" in n.message

    async def test_test_channel(self):
        mgr = NotificationManager()
        ch = self._make_channel()
        await mgr.register_channel("a", ch)
        ok, msg = await mgr.test_channel("a")
        assert ok is True

    async def test_test_channel_not_found(self):
        mgr = NotificationManager()
        ok, msg = await mgr.test_channel("missing")
        assert ok is False
        assert "not found" in msg

    async def test_schedule_and_cancel_escalation(self):
        mgr = NotificationManager()
        n = _make_notification()
        await mgr.schedule_escalation("a1", n, 0)
        assert "a1" in mgr._escalation_timers
        await mgr.cancel_escalation("a1")
        assert "a1" not in mgr._escalation_timers

    async def test_cancel_escalation_unknown_alarm(self):
        mgr = NotificationManager()
        await mgr.cancel_escalation("missing")

    async def test_escalate_alarm_sends_escalated_notification(self):
        mgr = NotificationManager()
        ch = self._make_channel()
        await mgr.register_channel("a", ch)
        n = _make_notification(severity="minor")
        await mgr._escalate_alarm("a1", n)
        sent = ch.notify.await_args.args[0]
        assert sent.action == "escalated"
        assert sent.escalation_level == 1
        assert sent.original_severity == "minor"
        assert sent.severity == "major"

    async def test_escalate_alarm_critical_no_reschedule(self):
        mgr = NotificationManager()
        ch = self._make_channel()
        await mgr.register_channel("a", ch)
        n = _make_notification(severity="major")
        await mgr._escalate_alarm("a1", n)
        sent = ch.notify.await_args.args[0]
        assert sent.severity == "critical"
        assert "a1" not in mgr._escalation_timers

    async def test_escalate_alarm_unknown_severity(self):
        mgr = NotificationManager()
        ch = self._make_channel()
        await mgr.register_channel("a", ch)
        n = _make_notification(severity="bogus")
        await mgr._escalate_alarm("a1", n)
        sent = ch.notify.await_args.args[0]
        assert sent.severity == "warning"

    async def test_close_cancels_timers_and_closes_channels(self):
        mgr = NotificationManager()
        ch = self._make_channel()
        await mgr.register_channel("a", ch)

        async def _hang():
            await asyncio.get_event_loop().create_future()

        task = asyncio.ensure_future(_hang())
        mgr._escalation_timers["a1"] = task
        await mgr.close()
        assert task.cancelled() or task.done()
        ch.close.assert_awaited_once()
        assert mgr._escalation_timers == {}


# ---------------------------------------------------------------------------
# Global functions
# ---------------------------------------------------------------------------


class TestGlobalFunctions:
    def test_get_notification_manager_singleton(self):
        m1 = get_notification_manager()
        m2 = get_notification_manager()
        assert m1 is m2
        assert isinstance(m1, NotificationManager)

    async def test_init_notification_manager_dingtalk(self):
        config = {
            "dingtalk": [
                {
                    "enabled": True,
                    "name": "dt",
                    "webhook_url": "https://oapi.dingtalk.com/x",
                    "secret": "s",
                    "at_mobiles": ["1"],
                    "is_at_all": True,
                    "max_per_minute": 5,
                    "cooldown_seconds": 10.0,
                    "message_template": "{rule_name}",
                }
            ]
        }
        mgr = await init_notification_manager(config)
        ch = mgr.get_channel("dingtalk-0")
        assert ch is not None
        assert ch.name == "dt"
        assert ch._webhook_url == "https://oapi.dingtalk.com/x"
        assert ch._secret == "s"
        assert ch._is_at_all is True

    async def test_init_notification_manager_wecom(self):
        config = {
            "wecom": [
                {
                    "enabled": True,
                    "name": "wc",
                    "webhook_url": "https://qyapi.weixin.qq.com/x",
                    "max_per_minute": 3,
                    "cooldown_seconds": 5.0,
                }
            ]
        }
        mgr = await init_notification_manager(config)
        ch = mgr.get_channel("wecom-0")
        assert ch is not None
        assert ch.name == "wc"

    async def test_init_notification_manager_email(self):
        config = {
            "email": [
                {
                    "enabled": True,
                    "name": "mail",
                    "smtp_host": "smtp.test.com",
                    "smtp_port": 465,
                    "smtp_user": "u",
                    "smtp_password": "p",
                    "from_address": "a@b.com",
                    "to_addresses": ["c@d.com"],
                    "use_tls": False,
                    "use_ssl": True,
                    "message_template": "T:{severity}",
                }
            ]
        }
        mgr = await init_notification_manager(config)
        ch = mgr.get_channel("email-0")
        assert ch is not None
        assert ch._smtp_host == "smtp.test.com"
        assert ch._use_ssl is True

    async def test_init_notification_manager_webhook(self):
        config = {
            "webhook": [
                {
                    "enabled": True,
                    "name": "wh",
                    "url": "https://example.com/h",
                    "method": "PUT",
                    "headers": {"X-Custom": "v"},
                    "auth_type": "bearer",
                    "auth_token": "tok",
                    "retry_count": 5,
                    "retry_delay": 2.0,
                }
            ]
        }
        mgr = await init_notification_manager(config)
        ch = mgr.get_channel("webhook-0")
        assert ch is not None
        assert ch._method == "PUT"
        assert ch._auth_type == "bearer"
        assert ch._retry_count == 5

    async def test_init_notification_manager_disabled_channels_skipped(self):
        config = {
            "dingtalk": [{"enabled": False}],
            "wecom": [{"enabled": False}],
            "email": [{"enabled": False}],
            "webhook": [{"enabled": False}],
        }
        mgr = await init_notification_manager(config)
        assert mgr.list_channels() == []

    async def test_init_notification_manager_escalation_thresholds(self):
        config = {"escalation": {"critical": 100, "major": 200}}
        mgr = await init_notification_manager(config)
        assert mgr._escalation_thresholds["critical"] == 100
        assert mgr._escalation_thresholds["major"] == 200

    async def test_init_notification_manager_empty_config(self):
        mgr = await init_notification_manager({})
        assert mgr.list_channels() == []

    async def test_init_notification_manager_channel_name_defaults(self):
        config = {
            "dingtalk": [{"enabled": True, "webhook_url": "https://oapi.dingtalk.com/x"}],
            "wecom": [{"enabled": True, "webhook_url": "https://qyapi.weixin.qq.com/x"}],
            "email": [{"enabled": True}],
            "webhook": [{"enabled": True, "url": "https://example.com/x"}],
        }
        mgr = await init_notification_manager(config)
        assert mgr.get_channel("dingtalk-0").name == "DingTalk-1"
        assert mgr.get_channel("wecom-0").name == "WeCom-1"
        assert mgr.get_channel("email-0").name == "Email-1"
        assert mgr.get_channel("webhook-0").name == "Webhook-1"
        await mgr.close()
