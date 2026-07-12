"""告警通知服务测试 - services/notify_service.py

覆盖：
- _sanitize_email_header: CRLF 注入防护
- _is_ip_safe_for_webhook: IP 安全性判定
- _validate_webhook_url: SSRF 防护与 DNS Rebinding 缓存
- NotifyService: init/close/send_notification (并行/重试/未知渠道)
- _send_dingtalk: 未配置/域名白名单/加签/errcode/异常
- _send_email: 未配置/SMTP 成功失败/CRLF 过滤
- _smtp_send: TLS/明文/STARTTLS/登录/quit 异常
- _send_wechat: 未配置/域名白名单/errcode/异常
- _send_webhook: 未配置/SSRF 拒绝/IP 替换(IPv4/IPv6/SNI)/headers/异常
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, "src")

from edgelite.services import notify_service as ns
from edgelite.services.notify_service import (
    NotifyService,
    _is_ip_safe_for_webhook,
    _sanitize_email_header,
    _validate_webhook_url,
)


# ───────────────────────── 辅助函数 ─────────────────────────


def _make_notify_config(
    *,
    dingtalk_url: str = "",
    dingtalk_secret: str = "",
    smtp_host: str = "",
    smtp_port: int = 587,
    smtp_user: str = "",
    smtp_password: str = "",
    use_tls: bool = True,
    use_starttls: bool = False,
    from_addr: str = "",
    to_addrs=None,
    wechat_url: str = "",
    webhook_url: str = "",
    webhook_headers=None,
):
    dingtalk = SimpleNamespace(webhook_url=dingtalk_url, secret=dingtalk_secret)
    email = SimpleNamespace(
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_user=smtp_user,
        smtp_password=smtp_password,
        use_tls=use_tls,
        use_starttls=use_starttls,
        from_addr=from_addr,
        to_addrs=to_addrs or [],
    )
    wechat = SimpleNamespace(webhook_url=wechat_url)
    webhook = SimpleNamespace(url=webhook_url, headers=webhook_headers or {})
    notify = SimpleNamespace(dingtalk=dingtalk, email=email, wechat=wechat, webhook=webhook)
    return SimpleNamespace(notify=notify)


def _alarm_data(**overrides):
    base = {
        "device_id": "dev-001",
        "rule_id": "rule-001",
        "severity": "critical",
        "status": "fired",
        "trigger_value": {"temp": 90},
        "fired_at": "2026-07-11T10:00:00Z",
        "trigger_count": 2,
    }
    base.update(overrides)
    return base


def _mock_resp(status_code: int = 200, body=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = body if body is not None else {}
    return resp


def _af_inet():
    import socket as _s
    return _s.AF_INET


def _af_inet6():
    import socket as _s
    return _s.AF_INET6


@pytest.fixture
def patch_config():
    holder = {"config": _make_notify_config()}
    patcher = patch(
        "edgelite.services.notify_service.get_config",
        side_effect=lambda: holder["config"],
    )
    patcher.start()
    yield lambda cfg: holder.__setitem__("config", cfg)
    patcher.stop()


@pytest.fixture
def clear_ip_cache():
    with ns._webhook_ip_cache_lock:
        ns._webhook_ip_cache.clear()
    yield
    with ns._webhook_ip_cache_lock:
        ns._webhook_ip_cache.clear()


@pytest.fixture
def fast_sleep():
    with patch("edgelite.services.notify_service.asyncio.sleep", new=AsyncMock(return_value=None)):
        yield


@pytest.fixture
async def svc():
    """创建带 mock HTTP 客户端的 NotifyService。

    FIX: 原 fixture 创建真实 NotifyService（含真实 httpx.AsyncClient），
    直接用 mock 替换 _http_client 后真实 client 从未关闭，导致连接泄漏。
    70+ 次测试后累积的未关闭 AsyncClient 导致事件循环清理时挂起。
    改为异步 fixture，创建后立即关闭真实 client。
    """
    s = NotifyService()
    if s._http_client:
        await s._http_client.aclose()
    client = AsyncMock()
    client.post = AsyncMock(return_value=_mock_resp(200, {"errcode": 0}))
    s._http_client = client
    yield s
    s._http_client = None


# ───────────────────────── _sanitize_email_header ─────────────────────────


class TestSanitizeEmailHeader:
    def test_strips_cr_and_lf(self):
        assert _sanitize_email_header("a\r\nb\n") == "ab"

    def test_strips_only_cr(self):
        assert _sanitize_email_header("a\rb") == "ab"

    def test_no_change_for_clean_string(self):
        assert _sanitize_email_header("normal subject") == "normal subject"

    def test_non_string_passthrough(self):
        assert _sanitize_email_header(123) == 123
        assert _sanitize_email_header(None) is None

    def test_empty_string(self):
        assert _sanitize_email_header("") == ""

    def test_strips_bcc_injection_attempt(self):
        assert _sanitize_email_header("x\r\nBcc: evil@x") == "xBcc: evil@x"


# ───────────────────────── _is_ip_safe_for_webhook ─────────────────────────


class TestIsIpSafeForWebhook:
    def test_private_is_unsafe(self):
        import ipaddress
        assert _is_ip_safe_for_webhook(ipaddress.ip_address("10.0.0.1")) is False
        assert _is_ip_safe_for_webhook(ipaddress.ip_address("192.168.1.1")) is False
        assert _is_ip_safe_for_webhook(ipaddress.ip_address("172.16.0.1")) is False

    def test_loopback_is_unsafe(self):
        import ipaddress
        assert _is_ip_safe_for_webhook(ipaddress.ip_address("127.0.0.1")) is False

    def test_link_local_is_unsafe(self):
        import ipaddress
        assert _is_ip_safe_for_webhook(ipaddress.ip_address("169.254.1.1")) is False

    def test_unspecified_is_unsafe(self):
        import ipaddress
        assert _is_ip_safe_for_webhook(ipaddress.ip_address("0.0.0.0")) is False

    def test_ipv6_unspecified_is_unsafe(self):
        import ipaddress
        assert _is_ip_safe_for_webhook(ipaddress.ip_address("::")) is False

    def test_multicast_is_unsafe(self):
        import ipaddress
        assert _is_ip_safe_for_webhook(ipaddress.ip_address("224.0.0.1")) is False

    def test_public_is_safe(self):
        import ipaddress
        assert _is_ip_safe_for_webhook(ipaddress.ip_address("8.8.8.8")) is True
        assert _is_ip_safe_for_webhook(ipaddress.ip_address("1.1.1.1")) is True

    def test_ipv6_loopback_is_unsafe(self):
        import ipaddress
        assert _is_ip_safe_for_webhook(ipaddress.ip_address("::1")) is False


# ───────────────────────── _validate_webhook_url ─────────────────────────


class TestValidateWebhookUrl:
    async def test_rejects_non_http_scheme(self, clear_ip_cache):
        assert await _validate_webhook_url("ftp://example.com/x") is False
        assert await _validate_webhook_url("file:///etc/passwd") is False

    async def test_rejects_missing_hostname(self, clear_ip_cache):
        assert await _validate_webhook_url("http:///path") is False

    async def test_rejects_localhost(self, clear_ip_cache):
        assert await _validate_webhook_url("http://localhost/admin") is False

    async def test_rejects_blocked_metadata_hosts(self, clear_ip_cache):
        assert await _validate_webhook_url("http://169.254.169.254/latest/meta-data") is False
        assert await _validate_webhook_url("http://metadata.google.internal/computeMetadata") is False
        assert await _validate_webhook_url("http://metadata") is False
        assert await _validate_webhook_url("http://169.254.170.2") is False
        assert await _validate_webhook_url("http://169.254.169.253") is False

    async def test_rejects_private_ip(self, clear_ip_cache):
        assert await _validate_webhook_url("http://10.0.0.1/x") is False
        assert await _validate_webhook_url("http://192.168.1.1/x") is False

    async def test_rejects_loopback_ip(self, clear_ip_cache):
        assert await _validate_webhook_url("http://127.0.0.1/x") is False

    async def test_rejects_unspecified_ip(self, clear_ip_cache):
        assert await _validate_webhook_url("http://0.0.0.0/x") is False

    async def test_accepts_public_ip_and_caches(self, clear_ip_cache):
        result = await _validate_webhook_url("https://8.8.8.8/hook")
        assert result is True
        with ns._webhook_ip_cache_lock:
            assert ns._webhook_ip_cache.get("8.8.8.8") == "8.8.8.8"

    async def test_accepts_domain_resolving_to_safe_ip(self, clear_ip_cache):
        fake_addrs = [(_af_inet(), 0, 0, "", ("93.184.216.34", 0))]
        with patch.object(ns.socket, "getaddrinfo", return_value=fake_addrs):
            result = await _validate_webhook_url("https://example.com/hook")
        assert result is True
        with ns._webhook_ip_cache_lock:
            assert ns._webhook_ip_cache.get("example.com") == "93.184.216.34"

    async def test_rejects_domain_resolving_to_unsafe_ip(self, clear_ip_cache):
        fake_addrs = [(_af_inet(), 0, 0, "", ("10.0.0.5", 0))]
        with patch.object(ns.socket, "getaddrinfo", return_value=fake_addrs):
            result = await _validate_webhook_url("https://example.com/hook")
        assert result is False

    async def test_rejects_when_dns_resolution_fails(self, clear_ip_cache):
        import socket as _socket
        with patch.object(ns.socket, "getaddrinfo", side_effect=_socket.gaierror("dns fail")):
            result = await _validate_webhook_url("https://nonexistent.invalid/hook")
        assert result is False

    async def test_rejects_empty_addr_list(self, clear_ip_cache):
        with patch.object(ns.socket, "getaddrinfo", return_value=[]):
            result = await _validate_webhook_url("https://example.com/hook")
        assert result is False

    async def test_falls_back_to_first_addr_when_no_ipv4(self, clear_ip_cache):
        fake_addrs = [(_af_inet6(), 0, 0, "", ("2001:4860:4860::8888", 0, 0, 0))]
        with patch.object(ns.socket, "getaddrinfo", return_value=fake_addrs):
            result = await _validate_webhook_url("https://example.com/hook")
        assert result is True
        with ns._webhook_ip_cache_lock:
            assert ns._webhook_ip_cache.get("example.com") == "2001:4860:4860::8888"

    async def test_skips_unparseable_addr(self, clear_ip_cache):
        fake_addrs = [
            (_af_inet(), 0, 0, "", ("not-an-ip", 0)),
            (_af_inet(), 0, 0, "", ("93.184.216.34", 0)),
        ]
        with patch.object(ns.socket, "getaddrinfo", return_value=fake_addrs):
            result = await _validate_webhook_url("https://example.com/hook")
        assert result is True

    async def test_lru_eviction_enforced(self, clear_ip_cache):
        with patch.object(ns, "_WEBHOOK_IP_CACHE_MAXLEN", 2):
            with patch.object(ns.socket, "getaddrinfo") as mock_gai:
                mock_gai.side_effect = lambda host, *a, **k: [
                    (_af_inet(), 0, 0, "", (f"1.2.3.{len(ns._webhook_ip_cache) + 1}", 0))
                ]
                await _validate_webhook_url("https://host1.example.com/hook")
                await _validate_webhook_url("https://host2.example.com/hook")
                await _validate_webhook_url("https://host3.example.com/hook")
                with ns._webhook_ip_cache_lock:
                    assert len(ns._webhook_ip_cache) <= 2
                    assert "host1.example.com" not in ns._webhook_ip_cache


# ───────────────────────── NotifyService init/close ─────────────────────────


class TestNotifyServiceLifecycle:
    async def test_init_creates_http_client(self):
        s = NotifyService()
        assert s._http_client is not None
        await s._http_client.aclose()
        s._http_client = None

    async def test_close_closes_client_and_nulls(self):
        s = NotifyService()
        client = s._http_client
        client.aclose = AsyncMock()
        await s.close()
        client.aclose.assert_awaited_once()
        assert s._http_client is None

    async def test_close_idempotent(self):
        s = NotifyService()
        client = s._http_client
        client.aclose = AsyncMock()
        await s.close()
        await s.close()
        client.aclose.assert_awaited_once()

    async def test_close_when_no_client(self):
        s = NotifyService.__new__(NotifyService)
        s._http_client = None
        await s.close()


# ───────────────────────── send_notification ─────────────────────────


class TestSendNotification:
    async def test_parallel_dispatch_multiple_channels(self, svc, patch_config):
        patch_config(_make_notify_config(
            dingtalk_url="https://oapi.dingtalk.com/robot/send",
            smtp_host="smtp.example.com", to_addrs=["a@b.com"],
            wechat_url="https://qyapi.weixin.qq.com/cgi-bin/webhook",
            webhook_url="https://8.8.8.8/hook",
        ))
        svc._http_client.post = AsyncMock(return_value=_mock_resp(200, {"errcode": 0}))
        with patch.object(NotifyService, "_smtp_send", return_value=None):
            result = await svc.send_notification(
                ["dingtalk", "email", "wechat", "webhook"], _alarm_data()
            )
        assert result == {"dingtalk": True, "email": True, "wechat": True, "webhook": True}

    async def test_unknown_channel_returns_false(self, svc, patch_config):
        result = await svc.send_notification(["unknown_channel"], _alarm_data())
        assert result == {"unknown_channel": False}

    async def test_mixed_known_and_unknown_channels(self, svc, patch_config):
        patch_config(_make_notify_config(dingtalk_url="https://oapi.dingtalk.com/robot/send"))
        svc._http_client.post = AsyncMock(return_value=_mock_resp(200, {"errcode": 0}))
        result = await svc.send_notification(["dingtalk", "unknown"], _alarm_data())
        assert result == {"dingtalk": True, "unknown": False}

    async def test_retry_succeeds_on_second_attempt(self, svc, patch_config, fast_sleep):
        patch_config(_make_notify_config(dingtalk_url="https://oapi.dingtalk.com/robot/send"))
        svc._http_client.post = AsyncMock(
            side_effect=[_mock_resp(200, {"errcode": 130101}), _mock_resp(200, {"errcode": 0})]
        )
        result = await svc.send_notification(["dingtalk"], _alarm_data(), retry_count=1)
        assert result == {"dingtalk": True}
        assert svc._http_client.post.await_count == 2

    async def test_retry_exhausted_on_failure(self, svc, patch_config, fast_sleep):
        patch_config(_make_notify_config(dingtalk_url="https://oapi.dingtalk.com/robot/send"))
        svc._http_client.post = AsyncMock(return_value=_mock_resp(200, {"errcode": 130101}))
        result = await svc.send_notification(["dingtalk"], _alarm_data(), retry_count=2)
        assert result == {"dingtalk": False}
        assert svc._http_client.post.await_count == 3

    async def test_retry_on_exception(self, svc, patch_config, fast_sleep):
        patch_config(_make_notify_config(dingtalk_url="https://oapi.dingtalk.com/robot/send"))
        svc._http_client.post = AsyncMock(
            side_effect=[ConnectionError("boom"), _mock_resp(200, {"errcode": 0})]
        )
        result = await svc.send_notification(["dingtalk"], _alarm_data(), retry_count=2)
        assert result == {"dingtalk": True}

    async def test_no_retry_when_retry_count_zero(self, svc, patch_config, fast_sleep):
        patch_config(_make_notify_config(dingtalk_url="https://oapi.dingtalk.com/robot/send"))
        svc._http_client.post = AsyncMock(return_value=_mock_resp(200, {"errcode": 130101}))
        result = await svc.send_notification(["dingtalk"], _alarm_data(), retry_count=0)
        assert result == {"dingtalk": False}
        assert svc._http_client.post.await_count == 1

    async def test_retry_exception_exhausted(self, svc, patch_config, fast_sleep):
        patch_config(_make_notify_config(dingtalk_url="https://oapi.dingtalk.com/robot/send"))
        svc._http_client.post = AsyncMock(side_effect=ConnectionError("boom"))
        result = await svc.send_notification(["dingtalk"], _alarm_data(), retry_count=1)
        assert result == {"dingtalk": False}
        assert svc._http_client.post.await_count == 2

    async def test_empty_channels_returns_empty_dict(self, svc, patch_config):
        result = await svc.send_notification([], _alarm_data())
        assert result == {}


# ───────────────────────── _send_dingtalk ─────────────────────────


class TestSendDingtalk:
    async def test_skip_when_not_configured(self, svc, patch_config):
        patch_config(_make_notify_config(dingtalk_url=""))
        assert await svc._send_dingtalk(_alarm_data()) is True
        svc._http_client.post.assert_not_called()

    async def test_rejects_non_official_host(self, svc, patch_config):
        patch_config(_make_notify_config(dingtalk_url="https://evil.com/robot/send"))
        assert await svc._send_dingtalk(_alarm_data()) is False
        svc._http_client.post.assert_not_called()

    async def test_success_without_secret(self, svc, patch_config):
        patch_config(_make_notify_config(dingtalk_url="https://oapi.dingtalk.com/robot/send"))
        svc._http_client.post = AsyncMock(return_value=_mock_resp(200, {"errcode": 0}))
        assert await svc._send_dingtalk(_alarm_data()) is True

    async def test_success_with_secret_signing(self, svc, patch_config):
        patch_config(_make_notify_config(
            dingtalk_url="https://oapi.dingtalk.com/robot/send?access_token=x",
            dingtalk_secret="SECabc",
        ))
        svc._http_client.post = AsyncMock(return_value=_mock_resp(200, {"errcode": 0}))
        with patch("edgelite.services.notify_service.timestamp_ms", return_value=1700000000000):
            assert await svc._send_dingtalk(_alarm_data()) is True
        called_url = svc._http_client.post.await_args.args[0]
        assert "timestamp=1700000000000" in called_url
        assert "sign=" in called_url

    async def test_non_200_status_returns_false(self, svc, patch_config):
        patch_config(_make_notify_config(dingtalk_url="https://oapi.dingtalk.com/robot/send"))
        svc._http_client.post = AsyncMock(return_value=_mock_resp(500, {}))
        assert await svc._send_dingtalk(_alarm_data()) is False

    async def test_errcode_non_zero_returns_false(self, svc, patch_config):
        patch_config(_make_notify_config(dingtalk_url="https://oapi.dingtalk.com/robot/send"))
        svc._http_client.post = AsyncMock(return_value=_mock_resp(200, {"errcode": 130101}))
        assert await svc._send_dingtalk(_alarm_data()) is False

    async def test_post_exception_returns_false(self, svc, patch_config):
        patch_config(_make_notify_config(dingtalk_url="https://oapi.dingtalk.com/robot/send"))
        svc._http_client.post = AsyncMock(side_effect=ConnectionError("network down"))
        assert await svc._send_dingtalk(_alarm_data()) is False

    async def test_no_http_client_returns_false(self, patch_config):
        patch_config(_make_notify_config(dingtalk_url="https://oapi.dingtalk.com/robot/send"))
        s = NotifyService.__new__(NotifyService)
        s._http_client = None
        assert await s._send_dingtalk(_alarm_data()) is False

    async def test_emoji_mapping_for_severities(self, svc, patch_config):
        patch_config(_make_notify_config(dingtalk_url="https://oapi.dingtalk.com/robot/send"))
        svc._http_client.post = AsyncMock(return_value=_mock_resp(200, {"errcode": 0}))
        for sev in ("critical", "warning", "info", "unknown"):
            assert await svc._send_dingtalk(_alarm_data(severity=sev)) is True


# ───────────────────────── _send_email ─────────────────────────


class TestSendEmail:
    async def test_skip_when_not_configured(self, svc, patch_config):
        patch_config(_make_notify_config(smtp_host="", to_addrs=[]))
        assert await svc._send_email(_alarm_data()) is True

    async def test_skip_when_no_recipients(self, svc, patch_config):
        patch_config(_make_notify_config(smtp_host="smtp.example.com", to_addrs=[]))
        assert await svc._send_email(_alarm_data()) is True

    async def test_success(self, svc, patch_config):
        patch_config(_make_notify_config(
            smtp_host="smtp.example.com", to_addrs=["a@b.com"], from_addr="from@x.com",
        ))
        with patch.object(NotifyService, "_smtp_send", return_value=None) as mock_smtp:
            assert await svc._send_email(_alarm_data()) is True
            mock_smtp.assert_called_once()

    async def test_failure_returns_false(self, svc, patch_config):
        patch_config(_make_notify_config(
            smtp_host="smtp.example.com", to_addrs=["a@b.com"],
        ))
        with patch.object(NotifyService, "_smtp_send", side_effect=RuntimeError("smtp fail")):
            assert await svc._send_email(_alarm_data()) is False

    async def test_crlf_in_device_id_sanitized(self, svc, patch_config):
        patch_config(_make_notify_config(
            smtp_host="smtp.example.com", to_addrs=["a@b.com"], from_addr="from@x.com",
        ))
        captured = {}

        def _capture(email_cfg, msg):
            captured["subject"] = msg["Subject"]

        with patch.object(NotifyService, "_smtp_send", side_effect=_capture):
            await svc._send_email(_alarm_data(device_id="dev\r\nBcc: evil@x"))
        assert "\r" not in captured["subject"]
        assert "\n" not in captured["subject"]
        assert "Bcc" in captured["subject"]

    async def test_severity_label_mapping(self, svc, patch_config):
        patch_config(_make_notify_config(
            smtp_host="smtp.example.com", to_addrs=["a@b.com"],
        ))
        for sev in ("critical", "warning", "info", "unknown"):
            with patch.object(NotifyService, "_smtp_send", return_value=None):
                assert await svc._send_email(_alarm_data(severity=sev)) is True

    async def test_from_addr_falls_back_to_smtp_user(self, svc, patch_config):
        patch_config(_make_notify_config(
            smtp_host="smtp.example.com", to_addrs=["a@b.com"],
            smtp_user="user@x.com", from_addr="",
        ))
        captured = {}

        def _capture(email_cfg, msg):
            captured["from"] = msg["From"]

        with patch.object(NotifyService, "_smtp_send", side_effect=_capture):
            await svc._send_email(_alarm_data())
        assert captured["from"] == "user@x.com"


# ───────────────────────── _smtp_send ─────────────────────────


class TestSmtpSend:
    def _email_cfg(self, **kw):
        defaults = dict(
            smtp_host="smtp.example.com", smtp_port=465,
            smtp_user="user", smtp_password="pass",
            use_tls=True, use_starttls=False,
        )
        defaults.update(kw)
        return SimpleNamespace(**defaults)

    def _msg(self):
        from email.mime.multipart import MIMEMultipart
        return MIMEMultipart("alternative")

    def test_tls_uses_smtp_ssl(self):
        cfg = self._email_cfg(use_tls=True)
        with patch("edgelite.services.notify_service.smtplib.SMTP_SSL") as mock_ssl:
            mock_server = MagicMock()
            mock_ssl.return_value = mock_server
            NotifyService._smtp_send(cfg, self._msg())
            mock_ssl.assert_called_once()
            mock_server.login.assert_called_once_with("user", "pass")
            mock_server.send_message.assert_called_once()
            mock_server.quit.assert_called_once()

    def test_plain_uses_smtp(self):
        cfg = self._email_cfg(use_tls=False, smtp_port=587)
        with patch("edgelite.services.notify_service.smtplib.SMTP") as mock_smtp:
            mock_server = MagicMock()
            mock_smtp.return_value = mock_server
            NotifyService._smtp_send(cfg, self._msg())
            mock_smtp.assert_called_once()
            mock_server.send_message.assert_called_once()

    def test_starttls_called_when_configured(self):
        cfg = self._email_cfg(use_tls=False, use_starttls=True)
        with patch("edgelite.services.notify_service.smtplib.SMTP") as mock_smtp:
            mock_server = MagicMock()
            mock_smtp.return_value = mock_server
            NotifyService._smtp_send(cfg, self._msg())
            mock_server.starttls.assert_called_once()

    def test_no_login_when_no_credentials(self):
        cfg = self._email_cfg(smtp_user="", smtp_password="")
        with patch("edgelite.services.notify_service.smtplib.SMTP_SSL") as mock_ssl:
            mock_server = MagicMock()
            mock_ssl.return_value = mock_server
            NotifyService._smtp_send(cfg, self._msg())
            mock_server.login.assert_not_called()
            mock_server.send_message.assert_called_once()

    def test_quit_failure_handled_gracefully(self):
        cfg = self._email_cfg()
        with patch("edgelite.services.notify_service.smtplib.SMTP_SSL") as mock_ssl:
            mock_server = MagicMock()
            mock_server.quit.side_effect = Exception("quit failed")
            mock_ssl.return_value = mock_server
            NotifyService._smtp_send(cfg, self._msg())
            mock_server.send_message.assert_called_once()

    def test_smtp_timeout_constant_used(self):
        from edgelite.constants import _NOTIFY_SMTP_TIMEOUT
        cfg = self._email_cfg(use_tls=False)
        with patch("edgelite.services.notify_service.smtplib.SMTP") as mock_smtp:
            NotifyService._smtp_send(cfg, self._msg())
            _, kwargs = mock_smtp.call_args
            assert kwargs.get("timeout") == _NOTIFY_SMTP_TIMEOUT


# ───────────────────────── _send_wechat ─────────────────────────


class TestSendWechat:
    async def test_skip_when_not_configured(self, svc, patch_config):
        patch_config(_make_notify_config(wechat_url=""))
        assert await svc._send_wechat(_alarm_data()) is True
        svc._http_client.post.assert_not_called()

    async def test_rejects_non_official_host(self, svc, patch_config):
        patch_config(_make_notify_config(wechat_url="https://evil.com/webhook"))
        assert await svc._send_wechat(_alarm_data()) is False
        svc._http_client.post.assert_not_called()

    async def test_success(self, svc, patch_config):
        patch_config(_make_notify_config(wechat_url="https://qyapi.weixin.qq.com/cgi-bin/webhook"))
        svc._http_client.post = AsyncMock(return_value=_mock_resp(200, {"errcode": 0}))
        assert await svc._send_wechat(_alarm_data()) is True

    async def test_non_200_status_returns_false(self, svc, patch_config):
        patch_config(_make_notify_config(wechat_url="https://qyapi.weixin.qq.com/cgi-bin/webhook"))
        svc._http_client.post = AsyncMock(return_value=_mock_resp(500, {}))
        assert await svc._send_wechat(_alarm_data()) is False

    async def test_errcode_non_zero_returns_false(self, svc, patch_config):
        patch_config(_make_notify_config(wechat_url="https://qyapi.weixin.qq.com/cgi-bin/webhook"))
        svc._http_client.post = AsyncMock(return_value=_mock_resp(200, {"errcode": 45009}))
        assert await svc._send_wechat(_alarm_data()) is False

    async def test_post_exception_returns_false(self, svc, patch_config):
        patch_config(_make_notify_config(wechat_url="https://qyapi.weixin.qq.com/cgi-bin/webhook"))
        svc._http_client.post = AsyncMock(side_effect=ConnectionError("down"))
        assert await svc._send_wechat(_alarm_data()) is False

    async def test_no_http_client_returns_false(self, patch_config):
        patch_config(_make_notify_config(wechat_url="https://qyapi.weixin.qq.com/cgi-bin/webhook"))
        s = NotifyService.__new__(NotifyService)
        s._http_client = None
        assert await s._send_wechat(_alarm_data()) is False

    async def test_severity_color_mapping(self, svc, patch_config):
        patch_config(_make_notify_config(wechat_url="https://qyapi.weixin.qq.com/cgi-bin/webhook"))
        svc._http_client.post = AsyncMock(return_value=_mock_resp(200, {"errcode": 0}))
        for sev in ("critical", "warning", "info", "unknown"):
            assert await svc._send_wechat(_alarm_data(severity=sev)) is True


# ───────────────────────── _send_webhook ─────────────────────────


class TestSendWebhook:
    async def test_skip_when_not_configured(self, svc, patch_config, clear_ip_cache):
        patch_config(_make_notify_config(webhook_url=""))
        assert await svc._send_webhook(_alarm_data()) is True
        svc._http_client.post.assert_not_called()

    async def test_rejects_ssrf_url(self, svc, patch_config, clear_ip_cache):
        patch_config(_make_notify_config(webhook_url="http://169.254.169.254/latest"))
        assert await svc._send_webhook(_alarm_data()) is False

    async def test_rejects_internal_ip(self, svc, patch_config, clear_ip_cache):
        patch_config(_make_notify_config(webhook_url="http://10.0.0.1/internal"))
        assert await svc._send_webhook(_alarm_data()) is False

    async def test_success_with_public_ip(self, svc, patch_config, clear_ip_cache):
        patch_config(_make_notify_config(webhook_url="https://8.8.8.8/hook"))
        svc._http_client.post = AsyncMock(return_value=_mock_resp(200, {}))
        assert await svc._send_webhook(_alarm_data()) is True
        svc._http_client.post.assert_awaited_once()

    async def test_failure_on_4xx_status(self, svc, patch_config, clear_ip_cache):
        patch_config(_make_notify_config(webhook_url="https://8.8.8.8/hook"))
        svc._http_client.post = AsyncMock(return_value=_mock_resp(404, {}))
        assert await svc._send_webhook(_alarm_data()) is False

    async def test_failure_on_5xx_status(self, svc, patch_config, clear_ip_cache):
        patch_config(_make_notify_config(webhook_url="https://8.8.8.8/hook"))
        svc._http_client.post = AsyncMock(return_value=_mock_resp(500, {}))
        assert await svc._send_webhook(_alarm_data()) is False

    async def test_post_exception_returns_false(self, svc, patch_config, clear_ip_cache):
        patch_config(_make_notify_config(webhook_url="https://8.8.8.8/hook"))
        svc._http_client.post = AsyncMock(side_effect=ConnectionError("down"))
        assert await svc._send_webhook(_alarm_data()) is False

    async def test_no_http_client_returns_false(self, patch_config, clear_ip_cache):
        patch_config(_make_notify_config(webhook_url="https://8.8.8.8/hook"))
        s = NotifyService.__new__(NotifyService)
        s._http_client = None
        assert await s._send_webhook(_alarm_data()) is False

    async def test_custom_headers_merged(self, svc, patch_config, clear_ip_cache):
        patch_config(_make_notify_config(
            webhook_url="https://8.8.8.8/hook",
            webhook_headers={"X-Token": "abc", "Authorization": "Bearer t"},
        ))
        svc._http_client.post = AsyncMock(return_value=_mock_resp(200, {}))
        await svc._send_webhook(_alarm_data())
        kwargs = svc._http_client.post.await_args.kwargs
        headers = kwargs["headers"]
        assert headers["Content-Type"] == "application/json"
        assert headers["X-Token"] == "abc"
        assert headers["Authorization"] == "Bearer t"

    async def test_ipv4_url_rewrite_when_resolved_ip_differs(self, svc, patch_config, clear_ip_cache):
        patch_config(_make_notify_config(webhook_url="https://example.com/hook"))
        with ns._webhook_ip_cache_lock:
            ns._webhook_ip_cache["example.com"] = "93.184.216.34"
        svc._http_client.post = AsyncMock(return_value=_mock_resp(200, {}))
        with patch("edgelite.services.notify_service._validate_webhook_url", new=AsyncMock(return_value=True)):
            await svc._send_webhook(_alarm_data())
        called_url = svc._http_client.post.await_args.args[0]
        kwargs = svc._http_client.post.await_args.kwargs
        assert "93.184.216.34" in called_url
        assert kwargs["headers"]["Host"] == "example.com"

    async def test_ipv6_url_rewrite_brackets(self, svc, patch_config, clear_ip_cache):
        patch_config(_make_notify_config(webhook_url="https://example.com/hook"))
        with ns._webhook_ip_cache_lock:
            ns._webhook_ip_cache["example.com"] = "2001:4860:4860::8888"
        svc._http_client.post = AsyncMock(return_value=_mock_resp(200, {}))
        with patch("edgelite.services.notify_service._validate_webhook_url", new=AsyncMock(return_value=True)):
            await svc._send_webhook(_alarm_data())
        called_url = svc._http_client.post.await_args.args[0]
        assert "[2001:4860:4860::8888]" in called_url

    async def test_https_sni_hostname_extension(self, svc, patch_config, clear_ip_cache):
        patch_config(_make_notify_config(webhook_url="https://example.com:8443/hook"))
        with ns._webhook_ip_cache_lock:
            ns._webhook_ip_cache["example.com"] = "93.184.216.34"
        svc._http_client.post = AsyncMock(return_value=_mock_resp(200, {}))
        with patch("edgelite.services.notify_service._validate_webhook_url", new=AsyncMock(return_value=True)):
            await svc._send_webhook(_alarm_data())
        kwargs = svc._http_client.post.await_args.kwargs
        assert "extensions" in kwargs
        assert kwargs["extensions"]["sni_hostname"] == b"example.com"

    async def test_http_no_sni_extension(self, svc, patch_config, clear_ip_cache):
        patch_config(_make_notify_config(webhook_url="http://example.com/hook"))
        with ns._webhook_ip_cache_lock:
            ns._webhook_ip_cache["example.com"] = "93.184.216.34"
        svc._http_client.post = AsyncMock(return_value=_mock_resp(200, {}))
        with patch("edgelite.services.notify_service._validate_webhook_url", new=AsyncMock(return_value=True)):
            await svc._send_webhook(_alarm_data())
        kwargs = svc._http_client.post.await_args.kwargs
        assert "extensions" not in kwargs

    async def test_no_rewrite_when_ip_equals_hostname(self, svc, patch_config, clear_ip_cache):
        patch_config(_make_notify_config(webhook_url="https://8.8.8.8/hook"))
        svc._http_client.post = AsyncMock(return_value=_mock_resp(200, {}))
        await svc._send_webhook(_alarm_data())
        called_url = svc._http_client.post.await_args.args[0]
        assert called_url == "https://8.8.8.8/hook"
        kwargs = svc._http_client.post.await_args.kwargs
        assert "Host" not in kwargs["headers"]

    async def test_url_with_port_preserved_on_rewrite(self, svc, patch_config, clear_ip_cache):
        patch_config(_make_notify_config(webhook_url="https://example.com:9000/hook"))
        with ns._webhook_ip_cache_lock:
            ns._webhook_ip_cache["example.com"] = "93.184.216.34"
        svc._http_client.post = AsyncMock(return_value=_mock_resp(200, {}))
        with patch("edgelite.services.notify_service._validate_webhook_url", new=AsyncMock(return_value=True)):
            await svc._send_webhook(_alarm_data())
        called_url = svc._http_client.post.await_args.args[0]
        assert "93.184.216.34:9000" in called_url
