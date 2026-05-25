"""通知渠道管理API"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from edgelite.api.deps import CurrentUser, require_permission
from edgelite.models.common import ApiResponse
from edgelite.security.rbac import Permission
from edgelite.services.notification_impl import (
    DingTalkConfig,
    DingTalkChannel,
    EmailConfig,
    EmailChannel,
    NotificationManager,
    NotificationChannelConfig,
    WebhookChannel,
    WebhookConfig,
    WeComConfig,
    WeComChannel,
    get_notification_manager,
)
from edgelite.config import get_config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/notify", tags=["Notification"])


class DingTalkConfigUpdate(BaseModel):
    """钉钉配置"""
    enabled: bool = True
    name: str = "钉钉通知"
    webhook_url: str = Field(..., description="钉钉机器人Webhook地址")
    secret: str = Field("", description="加签密钥(可选)")
    at_mobiles: list[str] = Field(default_factory=list, description="@指定手机号")
    is_at_all: bool = Field(False, description="@所有人")
    max_per_minute: int = Field(10, ge=1, le=100)
    cooldown_seconds: float = Field(60.0, ge=0)


class WeComConfigUpdate(BaseModel):
    """企业微信配置"""
    enabled: bool = True
    name: str = "企业微信通知"
    webhook_url: str = Field(..., description="企业微信机器人Webhook地址")
    max_per_minute: int = Field(10, ge=1, le=100)
    cooldown_seconds: float = Field(60.0, ge=0)


class EmailConfigUpdate(BaseModel):
    """邮件配置"""
    enabled: bool = True
    name: str = "邮件通知"
    smtp_host: str = Field(..., description="SMTP服务器地址")
    smtp_port: int = Field(587, ge=1, le=65535)
    smtp_user: str = Field("", description="SMTP用户名")
    smtp_password: str = Field("", description="SMTP密码")
    from_address: str = Field("", description="发件人地址")
    to_addresses: list[str] = Field(..., min_length=1, description="收件人地址列表")
    use_tls: bool = Field(True, description="使用TLS")
    use_ssl: bool = Field(False, description="使用SSL")
    max_per_minute: int = Field(10, ge=1, le=100)
    cooldown_seconds: float = Field(60.0, ge=0)


class WebhookConfigUpdate(BaseModel):
    """自定义Webhook配置"""
    enabled: bool = True
    name: str = "自定义Webhook"
    url: str = Field(..., description="Webhook地址")
    method: str = Field("POST", description="请求方法")
    headers: dict[str, str] = Field(default_factory=dict, description="自定义请求头")
    auth_type: str = Field("none", description="认证类型: none/basic/bearer/api_key")
    auth_token: str = Field("", description="Bearer/API Key Token")
    auth_username: str = Field("", description="Basic认证用户名")
    auth_password: str = Field("", description="Basic认证密码")
    max_per_minute: int = Field(10, ge=1, le=100)
    cooldown_seconds: float = Field(60.0, ge=0)


class ChannelStatus(BaseModel):
    """渠道状态"""
    id: str
    name: str
    type: str
    enabled: bool
    status: str = "unknown"
    last_test: str | None = None
    config: dict = Field(default_factory=dict)


@router.get("/channels", response_model=ApiResponse)
async def list_channels(
    user: CurrentUser = require_permission(Permission.ALARM_READ),
):
    """获取所有通知渠道配置"""
    config = get_config()
    notify_config = config.notify

    channels = []

    # 钉钉
    channels.append(ChannelStatus(
        id="dingtalk",
        name="钉钉通知",
        type="dingtalk",
        enabled=bool(notify_config.dingtalk.webhook_url),
        status="configured" if notify_config.dingtalk.webhook_url else "not_configured",
        config={
            "webhook_url": notify_config.dingtalk.webhook_url,
            "secret": "***" if notify_config.dingtalk.secret else "",
        }
    ))

    # 企业微信
    channels.append(ChannelStatus(
        id="wecom",
        name="企业微信",
        type="wecom",
        enabled=bool(notify_config.wechat.webhook_url),
        status="configured" if notify_config.wechat.webhook_url else "not_configured",
        config={
            "webhook_url": notify_config.wechat.webhook_url,
        }
    ))

    # 邮件
    channels.append(ChannelStatus(
        id="email",
        name="邮件通知",
        type="email",
        enabled=bool(notify_config.email.smtp_host),
        status="configured" if notify_config.email.smtp_host else "not_configured",
        config={
            "smtp_host": notify_config.email.smtp_host,
            "smtp_port": notify_config.email.smtp_port,
            "from_address": notify_config.email.from_addr,
            "to_addresses": notify_config.email.to_addrs,
        }
    ))

    # Webhook
    channels.append(ChannelStatus(
        id="webhook",
        name="自定义Webhook",
        type="webhook",
        enabled=bool(notify_config.webhook.url),
        status="configured" if notify_config.webhook.url else "not_configured",
        config={
            "url": notify_config.webhook.url,
            "method": notify_config.webhook.method,
        }
    ))

    return ApiResponse(data={"channels": channels})


@router.post("/channels/dingtalk", response_model=ApiResponse)
async def update_dingtalk(
    cfg: DingTalkConfigUpdate,
    user: CurrentUser = require_permission(Permission.CONFIG_EDIT),
):
    """配置钉钉通知渠道"""
    config = get_config()

    config.notify.dingtalk.webhook_url = cfg.webhook_url
    config.notify.dingtalk.secret = cfg.secret
    config.notify.dingtalk.at_mobiles = cfg.at_mobiles
    config.notify.dingtalk.is_at_all = cfg.is_at_all

    # 保存到配置文件
    try:
        config.save()
        logger.info("DingTalk config updated by %s", user.username)
        return ApiResponse(data={"channel": "dingtalk", "updated": True})
    except Exception as e:
        logger.error("Failed to save DingTalk config: %s", e)
        raise HTTPException(status_code=500, detail="配置保存失败")


@router.post("/channels/wecom", response_model=ApiResponse)
async def update_wecom(
    cfg: WeComConfigUpdate,
    user: CurrentUser = require_permission(Permission.CONFIG_EDIT),
):
    """配置企业微信通知渠道"""
    config = get_config()

    config.notify.wechat.webhook_url = cfg.webhook_url

    try:
        config.save()
        logger.info("WeCom config updated by %s", user.username)
        return ApiResponse(data={"channel": "wecom", "updated": True})
    except Exception as e:
        logger.error("Failed to save WeCom config: %s", e)
        raise HTTPException(status_code=500, detail="配置保存失败")


@router.post("/channels/email", response_model=ApiResponse)
async def update_email(
    cfg: EmailConfigUpdate,
    user: CurrentUser = require_permission(Permission.CONFIG_EDIT),
):
    """配置邮件通知渠道"""
    config = get_config()

    config.notify.email.smtp_host = cfg.smtp_host
    config.notify.email.smtp_port = cfg.smtp_port
    config.notify.email.smtp_user = cfg.smtp_user
    config.notify.email.smtp_password = cfg.smtp_password
    config.notify.email.from_addr = cfg.from_address
    config.notify.email.to_addrs = cfg.to_addresses
    config.notify.email.use_tls = cfg.use_tls
    config.notify.email.use_ssl = cfg.use_ssl

    try:
        config.save()
        logger.info("Email config updated by %s", user.username)
        return ApiResponse(data={"channel": "email", "updated": True})
    except Exception as e:
        logger.error("Failed to save Email config: %s", e)
        raise HTTPException(status_code=500, detail="配置保存失败")


@router.post("/channels/webhook", response_model=ApiResponse)
async def update_webhook(
    cfg: WebhookConfigUpdate,
    user: CurrentUser = require_permission(Permission.CONFIG_EDIT),
):
    """配置自定义Webhook渠道"""
    config = get_config()

    config.notify.webhook.url = cfg.url
    config.notify.webhook.method = cfg.method
    config.notify.webhook.headers = cfg.headers
    config.notify.webhook.auth_type = cfg.auth_type
    config.notify.webhook.auth_token = cfg.auth_token
    config.notify.webhook.auth_username = cfg.auth_username
    config.notify.webhook.auth_password = cfg.auth_password

    try:
        config.save()
        logger.info("Webhook config updated by %s", user.username)
        return ApiResponse(data={"channel": "webhook", "updated": True})
    except Exception as e:
        logger.error("Failed to save Webhook config: %s", e)
        raise HTTPException(status_code=500, detail="配置保存失败")


@router.post("/channels/{channel_id}/test", response_model=ApiResponse)
async def test_channel(
    channel_id: str,
    user: CurrentUser = require_permission(Permission.CONFIG_EDIT),
):
    """测试通知渠道"""
    config = get_config()
    notify_config = config.notify

    if channel_id == "dingtalk":
        if not notify_config.dingtalk.webhook_url:
            raise HTTPException(status_code=400, detail="钉钉Webhook地址未配置")

        channel_cfg = DingTalkConfig(
            webhook_url=notify_config.dingtalk.webhook_url,
            secret=notify_config.dingtalk.secret,
        )
        channel = DingTalkChannel(channel_cfg)

    elif channel_id == "wecom":
        if not notify_config.wechat.webhook_url:
            raise HTTPException(status_code=400, detail="企业微信Webhook地址未配置")

        channel_cfg = WeComConfig(
            webhook_url=notify_config.wechat.webhook_url,
        )
        channel = WeComChannel(channel_cfg)

    elif channel_id == "email":
        if not notify_config.email.smtp_host:
            raise HTTPException(status_code=400, detail="SMTP服务器未配置")

        channel_cfg = EmailConfig(
            smtp_host=notify_config.email.smtp_host,
            smtp_port=notify_config.email.smtp_port,
            smtp_user=notify_config.email.smtp_user,
            smtp_password=notify_config.email.smtp_password,
            from_address=notify_config.email.from_address,
            to_addresses=notify_config.email.to_addrs,
            use_tls=notify_config.email.use_tls,
            use_ssl=notify_config.email.use_ssl,
        )
        channel = EmailChannel(channel_cfg)

    elif channel_id == "webhook":
        if not notify_config.webhook.url:
            raise HTTPException(status_code=400, detail="Webhook地址未配置")

        channel_cfg = WebhookConfig(
            url=notify_config.webhook.url,
            method=notify_config.webhook.method,
            headers=notify_config.webhook.headers,
            auth_type=notify_config.webhook.auth_type,
            auth_token=notify_config.webhook.auth_token,
        )
        channel = WebhookChannel(channel_cfg)

    else:
        raise HTTPException(status_code=404, detail=f"未知渠道: {channel_id}")

    success, message = await channel.test()

    if success:
        logger.info("Channel %s test successful by %s", channel_id, user.username)
        return ApiResponse(data={"channel": channel_id, "success": True, "message": message})
    else:
        logger.warning("Channel %s test failed by %s: %s", channel_id, user.username, message)
        raise HTTPException(status_code=400, detail=message)


@router.post("/channels/{channel_id}/enable", response_model=ApiResponse)
async def enable_channel(
    channel_id: str,
    enabled: bool = True,
    user: CurrentUser = require_permission(Permission.CONFIG_EDIT),
):
    """启用/禁用通知渠道"""
    manager = get_notification_manager()

    if hasattr(manager, 'set_channel_enabled'):
        result = manager.set_channel_enabled(channel_id, enabled)
        if result:
            logger.info("Channel %s %s by %s", channel_id, "enabled" if enabled else "disabled", user.username)
            return ApiResponse(data={"channel": channel_id, "enabled": enabled})
        else:
            raise HTTPException(status_code=404, detail=f"渠道不存在: {channel_id}")
    else:
        raise HTTPException(status_code=501, detail="渠道管理功能暂不可用")


@router.delete("/channels/{channel_id}", response_model=ApiResponse)
async def delete_channel(
    channel_id: str,
    user: CurrentUser = require_permission(Permission.CONFIG_EDIT),
):
    """删除/禁用通知渠道配置"""
    config = get_config()
    notify_config = config.notify

    if channel_id == "dingtalk":
        notify_config.dingtalk.webhook_url = ""
        notify_config.dingtalk.secret = ""
    elif channel_id == "wecom":
        notify_config.wechat.webhook_url = ""
    elif channel_id == "email":
        notify_config.email.smtp_host = ""
        notify_config.email.smtp_password = ""
    elif channel_id == "webhook":
        notify_config.webhook.url = ""
        notify_config.webhook.auth_token = ""
    else:
        raise HTTPException(status_code=404, detail=f"未知渠道: {channel_id}")

    try:
        config.save()
        logger.info("Channel %s cleared by %s", channel_id, user.username)
        return ApiResponse(data={"channel": channel_id, "deleted": True})
    except Exception as e:
        logger.error("Failed to delete channel config: %s", e)
        raise HTTPException(status_code=500, detail="删除配置失败")
