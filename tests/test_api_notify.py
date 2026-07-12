"""通知渠道管理API单元测试。

覆盖 src/edgelite/api/notify.py 全部端点：
- list_channels: 列出所有渠道配置
- update_dingtalk/wecom/email/webhook: 渠道配置更新（含 SSRF 防护）
- test_channel: 渠道连通性测试（含 config_override）
- enable_channel: 启用/禁用渠道
- delete_channel: 删除/清空渠道配置
- _mask_sensitive / _mask_webhook_url: 敏感字段脱敏
- _audit_notify_channel: 审计日志记录（含失败降级）
"""

from __future__ import annotations

import sys

sys.path.insert(0, "src")

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from edgelite.api import notify as notify_module
from edgelite.api.notify import (
    _audit_notify_channel,
    _mask_sensitive,
    _mask_webhook_url,
    router,
)


# ───────────────────────── 辅助构建函数 ─────────────────────────


def _make_notify_config(
    dingtalk_url: str = "",
    dingtalk_secret: str = "",
    wechat_url: str = "",
    smtp_host: str = "",
    webhook_url: str = "",
):
    """构建可变的测试 notify 配置（SimpleNamespace，支持任意属性赋值）。"""
    dingtalk = SimpleNamespace(
        enabled=True,
        name="钉钉通知",
        webhook_url=dingtalk_url,
        secret=dingtalk_secret,
        at_mobiles=[],
        is_at_all=False,
        max_per_minute=10,
        cooldown_seconds=60.0,
    )
    wechat = SimpleNamespace(
        enabled=True,
        name="企业微信通知",
        webhook_url=wechat_url,
        max_per_minute=10,
        cooldown_seconds=60.0,
    )
    email = SimpleNamespace(
        enabled=True,
        name="邮件通知",
        smtp_host=smtp_host,
        smtp_port=587,
        smtp_user="user@example.com",
        smtp_password="pwd",
        from_addr="from@example.com",
        to_addrs=["to@example.com"],
        use_tls=True,
        use_ssl=False,
        max_per_minute=60,
        cooldown_seconds=60.0,
    )
    webhook = SimpleNamespace(
        enabled=True,
        name="自定义Webhook",
        url=webhook_url,
        method="POST",
        headers={},
        auth_type="none",
        auth_token="",
        auth_username="",
        auth_password="",
        max_per_minute=10,
        cooldown_seconds=60.0,
    )
    return SimpleNamespace(
        dingtalk=dingtalk, wechat=wechat, email=email, webhook=webhook
    )


def _make_config(**kwargs):
    """构建测试用顶层配置对象。"""
    return SimpleNamespace(notify=_make_notify_config(**kwargs))


def _services():
    """注入 app.state 所需服务。"""
    from conftest import make_mock_audit_service

    return {"audit_service": make_mock_audit_service()}


@pytest.fixture
async def client():
    """构建挂载 notify router 的测试客户端（admin 角色）。"""
    from conftest import make_app

    app = make_app(router, role="admin", services=_services())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c, app


@pytest.fixture
def mock_config():
    """patch get_config 返回可变测试配置，并 mock save_config。"""
    config = _make_config()
    with (
        patch.object(notify_module, "get_config", return_value=config),
        patch.object(notify_module, "save_config", return_value=None) as saver,
    ):
        yield config, saver


# ───────────────────────── 脱敏函数测试 ─────────────────────────


def test_mask_sensitive_empty():
    """空字典/None 原样返回。"""
    assert _mask_sensitive({}) == {}
    assert _mask_sensitive(None) is None


def test_mask_sensitive_masks_all_fields():
    """所有敏感字段被替换为 ***，非敏感字段保留。"""
    data = {
        "secret": "topsecret",
        "smtp_password": "pw",
        "auth_token": "tok",
        "auth_password": "ap",
        "webhook_url": "https://x/y?token=1",
        "url": "https://y/z",
        "name": "保持原值",
        "smtp_host": "smtp.example.com",
    }
    masked = _mask_sensitive(data)
    for key in ("secret", "smtp_password", "auth_token", "auth_password", "webhook_url", "url"):
        assert masked[key] == "***"
    assert masked["name"] == "保持原值"
    assert masked["smtp_host"] == "smtp.example.com"
    # 原始字典未被修改
    assert data["secret"] == "topsecret"


def test_mask_sensitive_skips_empty_values():
    """空值敏感字段保持为空（不替换为 ***）。"""
    data = {"secret": "", "url": None, "name": "x"}
    masked = _mask_sensitive(data)
    assert masked["secret"] == ""
    assert masked["url"] is None
    assert masked["name"] == "x"


def test_mask_webhook_url_none_and_empty():
    assert _mask_webhook_url(None) is None
    assert _mask_webhook_url("") == ""


def test_mask_webhook_url_strips_query():
    """query 中的 access_token 等凭证被丢弃。"""
    url = "https://oapi.dingtalk.com/robot/send?access_token=SECRET123"
    masked = _mask_webhook_url(url)
    assert "SECRET123" not in masked
    assert masked == "https://oapi.dingtalk.com/robot/send"


def test_mask_webhook_url_keeps_path():
    url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    masked = _mask_webhook_url(url)
    assert masked == "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"


def test_mask_webhook_url_invalid_fallback():
    """解析失败时降级为安全占位符。"""
    with patch.object(notify_module, "urlsplit", side_effect=Exception("boom")):
        assert _mask_webhook_url("not-a-url") == "***"


# ───────────────────────── _audit_notify_channel 测试 ─────────────────────────


async def test_audit_notify_channel_success():
    """正常记录审计日志，before/after 值被脱敏。"""
    audit_svc = AsyncMock()
    request = MagicMock()
    request.headers = {"User-Agent": "ua"}
    user = {"user_id": "u1", "username": "alice"}
    with patch("edgelite.api.auth._get_client_ip", return_value="1.2.3.4"):
        await _audit_notify_channel(
            audit_svc,
            "notify_config_update",
            user,
            request,
            "dingtalk",
            before_value={"secret": "s"},
            after_value={"webhook_url": "https://x?token=1"},
            details={"k": "v"},
        )
    audit_svc.log.assert_awaited_once()
    kwargs = audit_svc.log.call_args.kwargs
    assert kwargs["resource_type"] == "notify_channel"
    assert kwargs["resource_id"] == "dingtalk"
    assert kwargs["ip_address"] == "1.2.3.4"
    assert kwargs["user_agent"] == "ua"
    assert kwargs["before_value"]["secret"] == "***"
    assert kwargs["after_value"]["webhook_url"] == "***"
    assert kwargs["details"] == {"k": "v"}


async def test_audit_notify_channel_no_request():
    """request 为 None 时不报错。"""
    audit_svc = AsyncMock()
    user = {"user_id": "u1", "username": "alice"}
    await _audit_notify_channel(audit_svc, "action", user, None, "email")
    audit_svc.log.assert_awaited_once()
    kwargs = audit_svc.log.call_args.kwargs
    assert kwargs["ip_address"] == ""
    assert kwargs["user_agent"] is None
    assert kwargs["before_value"] is None
    assert kwargs["after_value"] is None


async def test_audit_notify_channel_failure_swallowed():
    """审计服务抛异常时仅记录日志，不向上传播。"""
    audit_svc = AsyncMock()
    audit_svc.log = AsyncMock(side_effect=RuntimeError("db down"))
    user = {"user_id": "u1", "username": "alice"}
    await _audit_notify_channel(audit_svc, "action", user, None, "email")


# ───────────────────────── list_channels 测试 ─────────────────────────


async def test_list_channels_all_configured(client, mock_config):
    """所有渠道已配置时返回 configured 状态。"""
    config, _ = mock_config
    config.notify.dingtalk.webhook_url = "https://oapi.dingtalk.com/x?token=t"
    config.notify.dingtalk.secret = "s"
    config.notify.wechat.webhook_url = "https://qyapi.weixin.qq.com/y?key=k"
    config.notify.email.smtp_host = "smtp.example.com"
    config.notify.webhook.url = "https://hook.example.com/z"

    c, _ = client
    resp = await c.get("/api/v1/notify/channels")
    assert resp.status_code == 200
    body = resp.json()
    channels = body["data"]["channels"]
    assert len(channels) == 4
    by_id = {ch["id"]: ch for ch in channels}
    # 钉钉：webhook_url 的 query 被脱敏，secret 被脱敏
    assert by_id["dingtalk"]["status"] == "configured"
    assert "token=t" not in by_id["dingtalk"]["config"]["webhook_url"]
    assert by_id["dingtalk"]["config"]["secret"] == "***"
    # 企业微信 query 脱敏
    assert by_id["wecom"]["status"] == "configured"
    assert "key=k" not in by_id["wecom"]["config"]["webhook_url"]
    # 邮件
    assert by_id["email"]["status"] == "configured"
    assert by_id["email"]["config"]["smtp_host"] == "smtp.example.com"
    # webhook
    assert by_id["webhook"]["status"] == "configured"
    assert by_id["webhook"]["config"]["method"] == "POST"


async def test_list_channels_none_configured(client, mock_config):
    """所有渠道未配置时返回 not_configured，secret 为空字符串。"""
    c, _ = client
    resp = await c.get("/api/v1/notify/channels")
    assert resp.status_code == 200
    by_id = {ch["id"]: ch for ch in resp.json()["data"]["channels"]}
    for cid in ("dingtalk", "wecom", "email", "webhook"):
        assert by_id[cid]["status"] == "not_configured"
        assert by_id[cid]["enabled"] is False
    # secret 为空时显示空字符串而非 ***
    assert by_id["dingtalk"]["config"]["secret"] == ""


async def test_list_channels_internal_error(client):
    """get_config 抛非 ValueError 异常时返回 500。"""
    with patch.object(notify_module, "get_config", side_effect=RuntimeError("boom")):
        c, _ = client
        resp = await c.get("/api/v1/notify/channels")
    assert resp.status_code == 500


async def test_list_channels_value_error(client):
    """get_config 抛 ValueError 时返回 400。"""
    with patch.object(notify_module, "get_config", side_effect=ValueError("bad")):
        c, _ = client
        resp = await c.get("/api/v1/notify/channels")
    assert resp.status_code == 400


# ───────────────────────── update_dingtalk 测试 ─────────────────────────


async def test_update_dingtalk_success(client, mock_config):
    """成功更新钉钉配置，save_config 被调用。"""
    config, saver = mock_config
    c, _ = client
    resp = await c.post(
        "/api/v1/notify/channels/dingtalk",
        json={
            "enabled": True,
            "name": "钉钉",
            "webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=abc",
            "secret": "SEC123",
            "at_mobiles": ["13800000000"],
            "is_at_all": False,
            "max_per_minute": 5,
            "cooldown_seconds": 30.0,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["data"] == {"channel": "dingtalk", "updated": True}
    saver.assert_called_once_with(config)
    assert config.notify.dingtalk.webhook_url.startswith("https://oapi.dingtalk.com")
    assert config.notify.dingtalk.secret == "SEC123"
    assert config.notify.dingtalk.max_per_minute == 5


async def test_update_dingtalk_ssrf_blocked(client, mock_config):
    """非官方域名触发 SSRF 拦截，返回 400。"""
    with patch.object(notify_module, "_check_dingtalk_host", return_value=False):
        c, _ = client
        resp = await c.post(
            "/api/v1/notify/channels/dingtalk",
            json={"webhook_url": "https://evil.com/hook"},
        )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "ERR_SSRF_BLOCKED"


async def test_update_dingtalk_internal_error(client, mock_config):
    """save_config 抛异常时返回 500。"""
    with patch.object(notify_module, "save_config", side_effect=RuntimeError("io")):
        c, _ = client
        resp = await c.post(
            "/api/v1/notify/channels/dingtalk",
            json={"webhook_url": "https://oapi.dingtalk.com/x"},
        )
    assert resp.status_code == 500


async def test_update_dingtalk_value_error(client, mock_config):
    """save_config 抛 ValueError 时返回 422。"""
    with patch.object(notify_module, "save_config", side_effect=ValueError("bad")):
        c, _ = client
        resp = await c.post(
            "/api/v1/notify/channels/dingtalk",
            json={"webhook_url": "https://oapi.dingtalk.com/x"},
        )
    assert resp.status_code == 422


# ───────────────────────── update_wecom 测试 ─────────────────────────


async def test_update_wecom_success(client, mock_config):
    config, saver = mock_config
    c, _ = client
    resp = await c.post(
        "/api/v1/notify/channels/wecom",
        json={
            "enabled": True,
            "name": "企业微信",
            "webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x",
            "max_per_minute": 8,
            "cooldown_seconds": 20.0,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["channel"] == "wecom"
    saver.assert_called_once()
    assert config.notify.wechat.webhook_url.startswith("https://qyapi.weixin.qq.com")


async def test_update_wecom_ssrf_blocked(client, mock_config):
    with patch.object(notify_module, "_check_wecom_host", return_value=False):
        c, _ = client
        resp = await c.post(
            "/api/v1/notify/channels/wecom",
            json={"webhook_url": "https://evil.com/hook"},
        )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "ERR_SSRF_BLOCKED"


async def test_update_wecom_internal_error(client, mock_config):
    with patch.object(notify_module, "save_config", side_effect=OSError("disk")):
        c, _ = client
        resp = await c.post(
            "/api/v1/notify/channels/wecom",
            json={"webhook_url": "https://qyapi.weixin.qq.com/x"},
        )
    assert resp.status_code == 500


# ───────────────────────── update_email 测试 ─────────────────────────


async def test_update_email_success(client, mock_config):
    config, saver = mock_config
    c, _ = client
    resp = await c.post(
        "/api/v1/notify/channels/email",
        json={
            "enabled": True,
            "name": "邮件",
            "smtp_host": "smtp.example.com",
            "smtp_port": 465,
            "smtp_user": "user",
            "smtp_password": "pw",
            "from_address": "from@example.com",
            "to_addresses": ["a@example.com", "b@example.com"],
            "use_tls": False,
            "use_ssl": True,
            "max_per_minute": 30,
            "cooldown_seconds": 10.0,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["channel"] == "email"
    saver.assert_called_once()
    assert config.notify.email.smtp_host == "smtp.example.com"
    assert config.notify.email.smtp_port == 465
    assert config.notify.email.use_ssl is True
    assert config.notify.email.from_addr == "from@example.com"
    assert config.notify.email.to_addrs == ["a@example.com", "b@example.com"]


async def test_update_email_value_error(client, mock_config):
    with patch.object(notify_module, "save_config", side_effect=ValueError("bad")):
        c, _ = client
        resp = await c.post(
            "/api/v1/notify/channels/email",
            json={"smtp_host": "smtp.example.com", "to_addresses": ["x@y.com"]},
        )
    assert resp.status_code == 422


async def test_update_email_internal_error(client, mock_config):
    with patch.object(notify_module, "save_config", side_effect=RuntimeError("io")):
        c, _ = client
        resp = await c.post(
            "/api/v1/notify/channels/email",
            json={"smtp_host": "smtp.example.com", "to_addresses": ["x@y.com"]},
        )
    assert resp.status_code == 500


# ───────────────────────── update_webhook 测试 ─────────────────────────


async def test_update_webhook_success(client, mock_config):
    config, saver = mock_config
    with patch.object(notify_module, "_validate_webhook_url", new=MagicMock(return_value=True)):
        c, _ = client
        resp = await c.post(
            "/api/v1/notify/channels/webhook",
            json={
                "enabled": True,
                "name": "自定义",
                "url": "https://hook.example.com/incoming",
                "method": "PUT",
                "headers": {"X-Token": "t"},
                "auth_type": "bearer",
                "auth_token": "tok",
                "auth_username": "",
                "auth_password": "",
                "max_per_minute": 20,
                "cooldown_seconds": 5.0,
            },
        )
    assert resp.status_code == 200
    assert resp.json()["data"]["channel"] == "webhook"
    saver.assert_called_once()
    assert config.notify.webhook.url == "https://hook.example.com/incoming"
    assert config.notify.webhook.method == "PUT"
    assert config.notify.webhook.auth_type == "bearer"
    assert config.notify.webhook.auth_token == "tok"


async def test_update_webhook_ssrf_blocked(client, mock_config):
    with patch.object(notify_module, "_validate_webhook_url", new=MagicMock(return_value=False)):
        c, _ = client
        resp = await c.post(
            "/api/v1/notify/channels/webhook",
            json={"url": "http://169.254.169.254/latest"},
        )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "ERR_SSRF_BLOCKED"


async def test_update_webhook_internal_error(client, mock_config):
    with (
        patch.object(notify_module, "_validate_webhook_url", new=MagicMock(return_value=True)),
        patch.object(notify_module, "save_config", side_effect=RuntimeError("io")),
    ):
        c, _ = client
        resp = await c.post(
            "/api/v1/notify/channels/webhook",
            json={"url": "https://hook.example.com/x"},
        )
    assert resp.status_code == 500


async def test_update_webhook_value_error(client, mock_config):
    with (
        patch.object(notify_module, "_validate_webhook_url", new=MagicMock(return_value=True)),
        patch.object(notify_module, "save_config", side_effect=ValueError("v")),
    ):
        c, _ = client
        resp = await c.post(
            "/api/v1/notify/channels/webhook",
            json={"url": "https://hook.example.com/x"},
        )
    assert resp.status_code == 422


# ───────────────────────── test_channel 测试 ─────────────────────────


def _patched_channel(success: bool = True, message: str = "ok"):
    """返回 mock channel 实例，其 test() 返回 (success, message)。"""
    channel_instance = MagicMock()
    channel_instance.test = AsyncMock(return_value=(success, message))
    return channel_instance


async def test_channel_dingtalk_success(client, mock_config):
    """测试钉钉渠道成功。"""
    config, _ = mock_config
    config.notify.dingtalk.webhook_url = "https://oapi.dingtalk.com/x"
    channel_instance = _patched_channel(True, "ok")
    with (
        patch.object(notify_module, "DingTalkChannel", return_value=channel_instance),
        patch.object(notify_module, "DingTalkConfig") as cfg_cls,
    ):
        c, _ = client
        resp = await c.post("/api/v1/notify/channels/dingtalk/test")
    assert resp.status_code == 200
    assert resp.json()["data"]["success"] is True
    cfg_cls.assert_called_once()


async def test_channel_dingtalk_with_override(client, mock_config):
    """通过 config_override 传入 webhook_url 测试（无需已保存配置）。"""
    channel_instance = _patched_channel(True, "ok")
    with (
        patch.object(notify_module, "DingTalkChannel", return_value=channel_instance),
        patch.object(notify_module, "DingTalkConfig") as cfg_cls,
    ):
        c, _ = client
        resp = await c.post(
            "/api/v1/notify/channels/dingtalk/test",
            json={"webhook_url": "https://oapi.dingtalk.com/override", "secret": "s"},
        )
    assert resp.status_code == 200
    _, kwargs = cfg_cls.call_args
    assert kwargs["webhook_url"] == "https://oapi.dingtalk.com/override"


async def test_channel_dingtalk_not_configured(client, mock_config):
    """钉钉未配置 webhook_url 时返回 400。"""
    c, _ = client
    resp = await c.post("/api/v1/notify/channels/dingtalk/test")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "ERR_NOTIFY_CHANNEL_NOT_CONFIGURED"


async def test_channel_wecom_success(client, mock_config):
    config, _ = mock_config
    config.notify.wechat.webhook_url = "https://qyapi.weixin.qq.com/x"
    channel_instance = _patched_channel(True, "ok")
    with (
        patch.object(notify_module, "WeComChannel", return_value=channel_instance),
        patch.object(notify_module, "WeComConfig"),
    ):
        c, _ = client
        resp = await c.post("/api/v1/notify/channels/wecom/test")
    assert resp.status_code == 200
    assert resp.json()["data"]["channel"] == "wecom"


async def test_channel_wecom_not_configured(client, mock_config):
    c, _ = client
    resp = await c.post("/api/v1/notify/channels/wecom/test")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "ERR_NOTIFY_CHANNEL_NOT_CONFIGURED"


async def test_channel_email_success(client, mock_config):
    config, _ = mock_config
    config.notify.email.smtp_host = "smtp.example.com"
    channel_instance = _patched_channel(True, "ok")
    with (
        patch.object(notify_module, "EmailChannel", return_value=channel_instance),
        patch.object(notify_module, "EmailConfig"),
    ):
        c, _ = client
        resp = await c.post("/api/v1/notify/channels/email/test")
    assert resp.status_code == 200


async def test_channel_email_not_configured(client, mock_config):
    c, _ = client
    resp = await c.post("/api/v1/notify/channels/email/test")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "ERR_NOTIFY_SMTP_NOT_CONFIGURED"


async def test_channel_webhook_success(client, mock_config):
    config, _ = mock_config
    config.notify.webhook.url = "https://hook.example.com/x"
    channel_instance = _patched_channel(True, "ok")
    with (
        patch.object(notify_module, "WebhookChannel", return_value=channel_instance),
        patch.object(notify_module, "WebhookConfig"),
    ):
        c, _ = client
        resp = await c.post("/api/v1/notify/channels/webhook/test")
    assert resp.status_code == 200


async def test_channel_webhook_not_configured(client, mock_config):
    c, _ = client
    resp = await c.post("/api/v1/notify/channels/webhook/test")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "ERR_NOTIFY_WEBHOOK_NOT_CONFIGURED"


async def test_channel_unknown_returns_404(client, mock_config):
    c, _ = client
    resp = await c.post("/api/v1/notify/channels/unknown/test")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "ERR_NOTIFY_CHANNEL_NOT_FOUND"


async def test_channel_test_failure_returns_400(client, mock_config):
    """channel.test() 返回失败时返回 400。"""
    config, _ = mock_config
    config.notify.dingtalk.webhook_url = "https://oapi.dingtalk.com/x"
    channel_instance = _patched_channel(False, "connection refused")
    with (
        patch.object(notify_module, "DingTalkChannel", return_value=channel_instance),
        patch.object(notify_module, "DingTalkConfig"),
    ):
        c, _ = client
        resp = await c.post("/api/v1/notify/channels/dingtalk/test")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "ERR_NOTIFY_CHANNEL_TEST_FAILED"


async def test_channel_test_value_error(client, mock_config):
    """channel 构造抛 ValueError 时返回 400。"""
    config, _ = mock_config
    config.notify.dingtalk.webhook_url = "https://oapi.dingtalk.com/x"
    with patch.object(notify_module, "DingTalkConfig", side_effect=ValueError("bad cfg")):
        c, _ = client
        resp = await c.post("/api/v1/notify/channels/dingtalk/test")
    assert resp.status_code == 400


async def test_channel_test_internal_error(client, mock_config):
    """channel.test() 抛非 ValueError 异常时返回 500。"""
    config, _ = mock_config
    config.notify.dingtalk.webhook_url = "https://oapi.dingtalk.com/x"
    channel_instance = MagicMock()
    channel_instance.test = AsyncMock(side_effect=RuntimeError("net"))
    with (
        patch.object(notify_module, "DingTalkChannel", return_value=channel_instance),
        patch.object(notify_module, "DingTalkConfig"),
    ):
        c, _ = client
        resp = await c.post("/api/v1/notify/channels/dingtalk/test")
    assert resp.status_code == 500


# ───────────────────────── enable_channel 测试 ─────────────────────────


async def test_enable_channel_success(client):
    """manager.set_channel_enabled 返回 True 时成功。"""
    manager = MagicMock()
    manager.set_channel_enabled = MagicMock(return_value=True)
    with patch.object(notify_module, "get_notification_manager", return_value=manager):
        c, _ = client
        resp = await c.post(
            "/api/v1/notify/channels/dingtalk/enable",
            json={"enabled": False},
        )
    assert resp.status_code == 200
    assert resp.json()["data"] == {"channel": "dingtalk", "enabled": False}
    manager.set_channel_enabled.assert_called_once_with("dingtalk", False)


async def test_enable_channel_default_enabled(client):
    """未传 body 时默认 enabled=True。"""
    manager = MagicMock()
    manager.set_channel_enabled = MagicMock(return_value=True)
    with patch.object(notify_module, "get_notification_manager", return_value=manager):
        c, _ = client
        resp = await c.post("/api/v1/notify/channels/email/enable")
    assert resp.status_code == 200
    assert resp.json()["data"]["enabled"] is True


async def test_enable_channel_not_found(client):
    """manager.set_channel_enabled 返回 False 时 404。"""
    manager = MagicMock()
    manager.set_channel_enabled = MagicMock(return_value=False)
    with patch.object(notify_module, "get_notification_manager", return_value=manager):
        c, _ = client
        resp = await c.post("/api/v1/notify/channels/unknown/enable")
    assert resp.status_code == 404


async def test_enable_channel_manager_unavailable(client):
    """manager 无 set_channel_enabled 方法时 501。"""
    manager = MagicMock(spec=[])  # 空 spec，无任何方法
    with patch.object(notify_module, "get_notification_manager", return_value=manager):
        c, _ = client
        resp = await c.post("/api/v1/notify/channels/dingtalk/enable")
    assert resp.status_code == 501
    assert resp.json()["detail"] == "ERR_NOTIFY_CHANNEL_MANAGER_UNAVAILABLE"


async def test_enable_channel_internal_error(client):
    """get_notification_manager 抛异常时 500。"""
    with patch.object(
        notify_module, "get_notification_manager", side_effect=RuntimeError("init")
    ):
        c, _ = client
        resp = await c.post("/api/v1/notify/channels/dingtalk/enable")
    assert resp.status_code == 500


async def test_enable_channel_value_error(client):
    """set_channel_enabled 抛 ValueError 时 400。"""
    manager = MagicMock()
    manager.set_channel_enabled = MagicMock(side_effect=ValueError("bad"))
    with patch.object(notify_module, "get_notification_manager", return_value=manager):
        c, _ = client
        resp = await c.post("/api/v1/notify/channels/dingtalk/enable")
    assert resp.status_code == 400


# ───────────────────────── delete_channel 测试 ─────────────────────────


async def test_delete_dingtalk_success(client, mock_config):
    config, saver = mock_config
    config.notify.dingtalk.webhook_url = "https://oapi.dingtalk.com/x"
    config.notify.dingtalk.secret = "s"
    c, _ = client
    resp = await c.delete("/api/v1/notify/channels/dingtalk")
    assert resp.status_code == 200
    assert resp.json()["data"] == {"channel": "dingtalk", "deleted": True}
    saver.assert_called_once()
    assert config.notify.dingtalk.webhook_url == ""
    assert config.notify.dingtalk.secret == ""


async def test_delete_wecom_success(client, mock_config):
    config, saver = mock_config
    config.notify.wechat.webhook_url = "https://qyapi.weixin.qq.com/x"
    c, _ = client
    resp = await c.delete("/api/v1/notify/channels/wecom")
    assert resp.status_code == 200
    assert config.notify.wechat.webhook_url == ""


async def test_delete_email_success(client, mock_config):
    config, saver = mock_config
    config.notify.email.smtp_host = "smtp.example.com"
    config.notify.email.smtp_password = "pw"
    c, _ = client
    resp = await c.delete("/api/v1/notify/channels/email")
    assert resp.status_code == 200
    assert config.notify.email.smtp_host == ""
    assert config.notify.email.smtp_password == ""


async def test_delete_webhook_success(client, mock_config):
    config, saver = mock_config
    config.notify.webhook.url = "https://hook.example.com/x"
    config.notify.webhook.auth_token = "tok"
    c, _ = client
    resp = await c.delete("/api/v1/notify/channels/webhook")
    assert resp.status_code == 200
    assert config.notify.webhook.url == ""
    assert config.notify.webhook.auth_token == ""


async def test_delete_channel_not_found(client, mock_config):
    c, _ = client
    resp = await c.delete("/api/v1/notify/channels/unknown")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "ERR_NOTIFY_CHANNEL_NOT_FOUND"


async def test_delete_channel_value_error(client, mock_config):
    with patch.object(notify_module, "save_config", side_effect=ValueError("bad")):
        c, _ = client
        resp = await c.delete("/api/v1/notify/channels/dingtalk")
    assert resp.status_code == 400


async def test_delete_channel_internal_error(client, mock_config):
    with patch.object(notify_module, "save_config", side_effect=RuntimeError("io")):
        c, _ = client
        resp = await c.delete("/api/v1/notify/channels/dingtalk")
    assert resp.status_code == 500


# ───────────────────────── 权限校验测试 ─────────────────────────


async def test_list_channels_viewer_allowed():
    """VIEWER 角色拥有 ALARM_READ，可访问 list_channels。"""
    from conftest import make_app

    app = make_app(router, role="viewer", services=_services())
    config = _make_config()
    with (
        patch.object(notify_module, "get_config", return_value=config),
        patch.object(notify_module, "save_config"),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            resp = await c.get("/api/v1/notify/channels")
    assert resp.status_code == 200


async def test_update_dingtalk_viewer_forbidden():
    """VIEWER 角色无 CONFIG_EDIT，更新渠道返回 403。"""
    from conftest import make_app

    app = make_app(router, role="viewer", services=_services())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.post(
            "/api/v1/notify/channels/dingtalk",
            json={"webhook_url": "https://oapi.dingtalk.com/x"},
        )
    assert resp.status_code == 403
