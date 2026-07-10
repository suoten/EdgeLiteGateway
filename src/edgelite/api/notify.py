"""通知渠道管理API"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from edgelite.api.deps import AuditServiceDep, require_permission
from edgelite.api.error_codes import CommonErrors, NotifyErrors
from edgelite.config import get_config, save_config
from edgelite.models.common import ApiResponse
from edgelite.security.rbac import Permission
from edgelite.services.notification_impl import (
    DingTalkChannel,
    DingTalkConfig,
    EmailChannel,
    EmailConfig,
    WebhookChannel,
    WebhookConfig,
    WeComChannel,
    WeComConfig,
    _check_dingtalk_host,
    _check_wecom_host,
    get_notification_manager,
)
from edgelite.services.notify_service import _validate_webhook_url

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/notify", tags=["Notification"])

# 第四轮修复: 通知渠道审计日志敏感字段脱敏
_SENSITIVE_FIELDS = frozenset({"secret", "smtp_password", "auth_token", "auth_password", "webhook_url", "url"})


def _mask_sensitive(data: dict) -> dict:
    """对配置字典中的敏感字段进行脱敏，返回脱敏后的副本。"""
    if not data:
        return data
    masked = dict(data)
    for k in list(masked.keys()):
        if k in _SENSITIVE_FIELDS and masked[k]:
            masked[k] = "***"
    return masked


def _mask_webhook_url(url: str | None) -> str | None:
    """脱敏 webhook URL——仅返回 scheme+host+path，丢弃 query 中的敏感凭证。

    FIXED-P1: list_channels 响应中 webhook_url 的 query 部分常嵌入 access_token/key
    等敏感凭证，而同响应的 secret 字段已脱敏为 "***"，处理不一致；此处统一脱敏，
    防止 VIEWER 角色（仅有 ALARM_READ 权限）越权读取凭证。若 url 为空或 None 则原样返回。
    """
    if not url:
        return url
    try:
        parsed = urlsplit(url)
        # 仅保留 scheme+netloc+path，丢弃 query 与 fragment 中的敏感凭证
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    except Exception:
        # 解析失败时降级为完全脱敏，避免泄露原始凭证
        return "***"


async def _audit_notify_channel(
    audit_svc,
    action,
    user,
    request,
    channel_id,
    before_value=None,
    after_value=None,
    details=None,
):
    """记录通知渠道操作审计日志的辅助函数（非阻塞，失败仅记日志）。"""
    try:
        from edgelite.api.auth import _get_client_ip

        client_ip = _get_client_ip(request) if request else ""
        user_agent = request.headers.get("User-Agent") if request else None
        await audit_svc.log(
            action,
            user_id=user["user_id"],
            username=user["username"],
            resource_type="notify_channel",
            resource_id=channel_id,
            ip_address=client_ip,
            user_agent=user_agent,
            before_value=_mask_sensitive(before_value) if before_value else None,
            after_value=_mask_sensitive(after_value) if after_value else None,
            details=details,
        )
    except Exception as e:
        logger.warning("Audit log failed: %s", e)


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


class ChannelTestRequest(BaseModel):
    """通知渠道测试请求体（支持按渠道类型传入任意覆盖字段）"""

    model_config = {"extra": "allow"}


class ChannelEnableRequest(BaseModel):
    """通知渠道启用/禁用请求体"""

    enabled: bool = True


@router.get("/channels", response_model=ApiResponse)
async def list_channels(
    user: dict[str, str] = Depends(require_permission(Permission.ALARM_READ)),
):
    """获取所有通知渠道配置"""
    try:
        config = get_config()
        notify_config = config.notify

        channels = []

        # 钉钉
        channels.append(
            ChannelStatus(
                id="dingtalk",
                name="钉钉通知",
                type="dingtalk",
                enabled=bool(notify_config.dingtalk.webhook_url),
                status="configured" if notify_config.dingtalk.webhook_url else "not_configured",
                config={
                    "webhook_url": _mask_webhook_url(notify_config.dingtalk.webhook_url),
                    "secret": "***" if notify_config.dingtalk.secret else "",
                },
            )
        )

        # 企业微信
        channels.append(
            ChannelStatus(
                id="wecom",
                name="企业微信",
                type="wecom",
                enabled=bool(notify_config.wechat.webhook_url),
                status="configured" if notify_config.wechat.webhook_url else "not_configured",
                config={
                    "webhook_url": _mask_webhook_url(notify_config.wechat.webhook_url),
                },
            )
        )

        # 邮件
        channels.append(
            ChannelStatus(
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
                },
            )
        )

        # Webhook
        channels.append(
            ChannelStatus(
                id="webhook",
                name="自定义Webhook",
                type="webhook",
                enabled=bool(notify_config.webhook.url),
                status="configured" if notify_config.webhook.url else "not_configured",
                config={
                    "url": _mask_webhook_url(notify_config.webhook.url),
                    "method": notify_config.webhook.method,
                },
            )
        )

        return ApiResponse(data={"channels": channels})
    except HTTPException:
        raise
    except ValueError as e:
        logger.warning("list_channels validation error: %s", e)
        raise HTTPException(
            status_code=400, detail=NotifyErrors.CHANNEL_SAVE_FAILED
        ) from e  # FIXED-P1: 原问题-detail=str(e)泄露内部错误
    except Exception as e:
        logger.error("list_channels failed: %s", e)
        raise HTTPException(
            status_code=500, detail=NotifyErrors.CHANNEL_SAVE_FAILED
        ) from e  # FIXED-P1: 原问题-detail=str(e)泄露内部错误


@router.post("/channels/dingtalk", response_model=ApiResponse)
async def update_dingtalk(
    cfg: DingTalkConfigUpdate,
    user: dict[str, str] = Depends(require_permission(Permission.CONFIG_EDIT)),
    request: Request = None,
    audit_svc: AuditServiceDep = None,
):
    """配置钉钉通知渠道"""
    try:
        config = get_config()

        # FIXED(安全): SSRF 防护 - 钉钉 webhook 仅允许官方域名 oapi.dingtalk.com
        if cfg.webhook_url and not _check_dingtalk_host(cfg.webhook_url):
            raise HTTPException(status_code=400, detail="ERR_SSRF_BLOCKED")

        # 第四轮修复: 记录变更前配置用于审计
        before_value = {
            "enabled": config.notify.dingtalk.enabled,
            "name": config.notify.dingtalk.name,
            "webhook_url": config.notify.dingtalk.webhook_url,
            "secret": config.notify.dingtalk.secret,
        }

        config.notify.dingtalk.enabled = cfg.enabled
        config.notify.dingtalk.name = cfg.name
        config.notify.dingtalk.webhook_url = cfg.webhook_url
        config.notify.dingtalk.secret = cfg.secret
        config.notify.dingtalk.at_mobiles = cfg.at_mobiles
        config.notify.dingtalk.is_at_all = cfg.is_at_all
        config.notify.dingtalk.max_per_minute = cfg.max_per_minute
        config.notify.dingtalk.cooldown_seconds = cfg.cooldown_seconds

        # 保存到配置文件
        save_config(config)
        logger.info("DingTalk config updated by %s", user["username"])

        # 第四轮修复: 审计日志
        from edgelite.services.audit_service import AuditAction

        after_value = {
            "enabled": cfg.enabled,
            "name": cfg.name,
            "webhook_url": cfg.webhook_url,
            "secret": cfg.secret,
        }
        await _audit_notify_channel(
            audit_svc,
            AuditAction.NOTIFY_CONFIG_UPDATE,
            user,
            request,
            "dingtalk",
            before_value=before_value,
            after_value=after_value,
        )

        return ApiResponse(data={"channel": "dingtalk", "updated": True})
    except HTTPException:
        raise
    except ValueError as e:
        logger.warning("DingTalk config validation error: %s", e)
        raise HTTPException(status_code=422, detail=CommonErrors.VALIDATION_FAILED) from e
    except Exception as e:
        logger.error("Failed to save DingTalk config: %s", e)
        raise HTTPException(
            status_code=500, detail=NotifyErrors.CHANNEL_SAVE_FAILED
        ) from e  # FIXED(P3): 原问题-B904 异常链丢失; 修复-添加 from e


@router.post("/channels/wecom", response_model=ApiResponse)
async def update_wecom(
    cfg: WeComConfigUpdate,
    user: dict[str, str] = Depends(require_permission(Permission.CONFIG_EDIT)),
    request: Request = None,
    audit_svc: AuditServiceDep = None,
):
    """配置企业微信通知渠道"""
    try:
        config = get_config()

        # FIXED(安全): SSRF 防护 - 企业微信 webhook 仅允许官方域名 qyapi.weixin.qq.com
        if cfg.webhook_url and not _check_wecom_host(cfg.webhook_url):
            raise HTTPException(status_code=400, detail="ERR_SSRF_BLOCKED")

        # 第四轮修复: 记录变更前配置用于审计
        before_value = {
            "enabled": config.notify.wechat.enabled,
            "name": config.notify.wechat.name,
            "webhook_url": config.notify.wechat.webhook_url,
        }

        config.notify.wechat.enabled = cfg.enabled
        config.notify.wechat.name = cfg.name
        config.notify.wechat.webhook_url = cfg.webhook_url
        config.notify.wechat.max_per_minute = cfg.max_per_minute
        config.notify.wechat.cooldown_seconds = cfg.cooldown_seconds

        save_config(config)
        logger.info("WeCom config updated by %s", user["username"])

        # 第四轮修复: 审计日志
        from edgelite.services.audit_service import AuditAction

        after_value = {
            "enabled": cfg.enabled,
            "name": cfg.name,
            "webhook_url": cfg.webhook_url,
        }
        await _audit_notify_channel(
            audit_svc,
            AuditAction.NOTIFY_CONFIG_UPDATE,
            user,
            request,
            "wecom",
            before_value=before_value,
            after_value=after_value,
        )

        return ApiResponse(data={"channel": "wecom", "updated": True})
    except HTTPException:
        raise
    except ValueError as e:
        logger.warning("WeCom config validation error: %s", e)
        raise HTTPException(status_code=422, detail=CommonErrors.VALIDATION_FAILED) from e
    except Exception as e:
        logger.error("Failed to save WeCom config: %s", e)
        raise HTTPException(
            status_code=500, detail=NotifyErrors.CHANNEL_SAVE_FAILED
        ) from e  # FIXED(P3): 原问题-B904 异常链丢失; 修复-添加 from e


@router.post("/channels/email", response_model=ApiResponse)
async def update_email(
    cfg: EmailConfigUpdate,
    user: dict[str, str] = Depends(require_permission(Permission.CONFIG_EDIT)),
    request: Request = None,
    audit_svc: AuditServiceDep = None,
):
    """配置邮件通知渠道"""
    try:
        config = get_config()

        # 第四轮修复: 记录变更前配置用于审计
        before_value = {
            "enabled": config.notify.email.enabled,
            "name": config.notify.email.name,
            "smtp_host": config.notify.email.smtp_host,
            "smtp_port": config.notify.email.smtp_port,
            "smtp_user": config.notify.email.smtp_user,
            "smtp_password": config.notify.email.smtp_password,
            "from_address": config.notify.email.from_addr,
        }

        config.notify.email.enabled = cfg.enabled
        config.notify.email.name = cfg.name
        config.notify.email.smtp_host = cfg.smtp_host
        config.notify.email.smtp_port = cfg.smtp_port
        config.notify.email.smtp_user = cfg.smtp_user
        config.notify.email.smtp_password = cfg.smtp_password
        config.notify.email.from_addr = cfg.from_address
        config.notify.email.to_addrs = cfg.to_addresses
        config.notify.email.use_tls = cfg.use_tls
        config.notify.email.use_ssl = cfg.use_ssl
        config.notify.email.max_per_minute = cfg.max_per_minute
        config.notify.email.cooldown_seconds = cfg.cooldown_seconds

        save_config(config)
        logger.info("Email config updated by %s", user["username"])

        # 第四轮修复: 审计日志
        from edgelite.services.audit_service import AuditAction

        after_value = {
            "enabled": cfg.enabled,
            "name": cfg.name,
            "smtp_host": cfg.smtp_host,
            "smtp_port": cfg.smtp_port,
            "smtp_user": cfg.smtp_user,
            "smtp_password": cfg.smtp_password,
            "from_address": cfg.from_address,
        }
        await _audit_notify_channel(
            audit_svc,
            AuditAction.NOTIFY_CONFIG_UPDATE,
            user,
            request,
            "email",
            before_value=before_value,
            after_value=after_value,
        )

        return ApiResponse(data={"channel": "email", "updated": True})
    except HTTPException:
        raise
    except ValueError as e:
        logger.warning("Email config validation error: %s", e)
        raise HTTPException(status_code=422, detail=CommonErrors.VALIDATION_FAILED) from e
    except Exception as e:
        logger.error("Failed to save Email config: %s", e)
        raise HTTPException(
            status_code=500, detail=NotifyErrors.CHANNEL_SAVE_FAILED
        ) from e  # FIXED(P3): 原问题-B904 异常链丢失; 修复-添加 from e


@router.post("/channels/webhook", response_model=ApiResponse)
async def update_webhook(
    cfg: WebhookConfigUpdate,
    user: dict[str, str] = Depends(require_permission(Permission.CONFIG_EDIT)),
    request: Request = None,
    audit_svc: AuditServiceDep = None,
):
    """配置自定义Webhook渠道"""
    try:
        config = get_config()

        # FIXED(安全): SSRF 防护 - 自定义 webhook 调用 _validate_webhook_url 完整校验
        if cfg.url and not _validate_webhook_url(cfg.url):
            raise HTTPException(status_code=400, detail="ERR_SSRF_BLOCKED")

        # 第四轮修复: 记录变更前配置用于审计
        before_value = {
            "url": config.notify.webhook.url,
            "method": config.notify.webhook.method,
            "auth_type": config.notify.webhook.auth_type,
            "auth_token": config.notify.webhook.auth_token,
            "auth_username": config.notify.webhook.auth_username,
            "auth_password": config.notify.webhook.auth_password,
        }

        config.notify.webhook.url = cfg.url
        config.notify.webhook.method = cfg.method
        config.notify.webhook.headers = cfg.headers
        config.notify.webhook.auth_type = cfg.auth_type
        config.notify.webhook.auth_token = cfg.auth_token
        config.notify.webhook.auth_username = cfg.auth_username
        config.notify.webhook.auth_password = cfg.auth_password
        if cfg.max_per_minute is not None:
            config.notify.webhook.max_per_minute = cfg.max_per_minute
        if cfg.cooldown_seconds is not None:
            config.notify.webhook.cooldown_seconds = cfg.cooldown_seconds

        save_config(config)
        logger.info("Webhook config updated by %s", user["username"])

        # 第四轮修复: 审计日志
        from edgelite.services.audit_service import AuditAction

        after_value = {
            "url": cfg.url,
            "method": cfg.method,
            "auth_type": cfg.auth_type,
            "auth_token": cfg.auth_token,
            "auth_username": cfg.auth_username,
            "auth_password": cfg.auth_password,
        }
        await _audit_notify_channel(
            audit_svc,
            AuditAction.NOTIFY_CONFIG_UPDATE,
            user,
            request,
            "webhook",
            before_value=before_value,
            after_value=after_value,
        )

        return ApiResponse(data={"channel": "webhook", "updated": True})
    except HTTPException:
        raise
    except ValueError as e:
        logger.warning("Webhook config validation error: %s", e)
        raise HTTPException(status_code=422, detail=CommonErrors.VALIDATION_FAILED) from e
    except Exception as e:
        logger.error("Failed to save Webhook config: %s", e)
        raise HTTPException(
            status_code=500, detail=NotifyErrors.CHANNEL_SAVE_FAILED
        ) from e  # FIXED(P3): 原问题-B904 异常链丢失; 修复-添加 from e


@router.post("/channels/{channel_id}/test", response_model=ApiResponse)
async def test_channel(
    channel_id: str,
    user: dict[str, str] = Depends(require_permission(Permission.CONFIG_EDIT)),
    req: ChannelTestRequest | None = None,
    request: Request = None,
    audit_svc: AuditServiceDep = None,
):
    """测试通知渠道。支持传入 config_override 使用表单数据测试（无需先保存）。"""
    try:
        config = get_config()
        notify_config = config.notify
        config_override = req.model_dump(exclude_none=True) if req else None

        if channel_id == "dingtalk":
            # Use override values if provided, otherwise fall back to saved config
            webhook_url = (config_override or {}).get("webhook_url") or notify_config.dingtalk.webhook_url
            secret = (config_override or {}).get("secret", "") if config_override else notify_config.dingtalk.secret
            at_mobiles = (
                (config_override or {}).get("at_mobiles", []) if config_override else notify_config.dingtalk.at_mobiles
            )
            is_at_all = (
                (config_override or {}).get("is_at_all", False) if config_override else notify_config.dingtalk.is_at_all
            )

            if not webhook_url:
                raise HTTPException(status_code=400, detail=NotifyErrors.CHANNEL_NOT_CONFIGURED)

            channel_cfg = DingTalkConfig(
                webhook_url=webhook_url,
                secret=secret,
                at_mobiles=at_mobiles,
                is_at_all=is_at_all,
            )
            channel = DingTalkChannel(channel_cfg)

        elif channel_id == "wecom":
            webhook_url = (config_override or {}).get("webhook_url") or notify_config.wechat.webhook_url

            if not webhook_url:
                raise HTTPException(status_code=400, detail=NotifyErrors.CHANNEL_NOT_CONFIGURED)

            channel_cfg = WeComConfig(
                webhook_url=webhook_url,
            )
            channel = WeComChannel(channel_cfg)

        elif channel_id == "email":
            smtp_host = (config_override or {}).get("smtp_host") or notify_config.email.smtp_host
            smtp_port = (config_override or {}).get("smtp_port") or notify_config.email.smtp_port
            smtp_user = (
                (config_override or {}).get("smtp_user", "") if config_override else notify_config.email.smtp_user
            )
            smtp_password = (
                (config_override or {}).get("smtp_password", "")
                if config_override
                else notify_config.email.smtp_password
            )
            from_address = (
                (config_override or {}).get("from_address", "") if config_override else notify_config.email.from_addr
            )
            to_addresses = (config_override or {}).get("to_addresses") or notify_config.email.to_addrs
            use_tls = (config_override or {}).get("use_tls", True) if config_override else notify_config.email.use_tls
            use_ssl = (config_override or {}).get("use_ssl", False) if config_override else notify_config.email.use_ssl

            if not smtp_host:
                raise HTTPException(status_code=400, detail=NotifyErrors.SMTP_NOT_CONFIGURED)

            channel_cfg = EmailConfig(
                smtp_host=smtp_host,
                smtp_port=smtp_port,
                smtp_user=smtp_user,
                smtp_password=smtp_password,
                from_address=from_address,
                to_addresses=to_addresses,
                use_tls=use_tls,
                use_ssl=use_ssl,
            )
            channel = EmailChannel(channel_cfg)

        elif channel_id == "webhook":
            url = (config_override or {}).get("url") or notify_config.webhook.url
            method = (config_override or {}).get("method", "POST") if config_override else notify_config.webhook.method
            headers = (config_override or {}).get("headers", {}) if config_override else notify_config.webhook.headers
            auth_type = (
                (config_override or {}).get("auth_type", "none") if config_override else notify_config.webhook.auth_type
            )
            auth_token = (
                (config_override or {}).get("auth_token", "") if config_override else notify_config.webhook.auth_token
            )

            if not url:
                raise HTTPException(status_code=400, detail=NotifyErrors.WEBHOOK_NOT_CONFIGURED)

            channel_cfg = WebhookConfig(
                url=url,
                method=method,
                headers=headers,
                auth_type=auth_type,
                auth_token=auth_token,
            )
            channel = WebhookChannel(channel_cfg)

        else:
            raise HTTPException(status_code=404, detail=NotifyErrors.CHANNEL_NOT_FOUND)

        success, message = await channel.test()

        # 第四轮修复: 审计日志记录渠道测试结果
        from edgelite.services.audit_service import AuditAction

        await _audit_notify_channel(
            audit_svc,
            AuditAction.NOTIFY_CHANNEL_TEST,
            user,
            request,
            channel_id,
            details={"success": success},
        )

        if success:
            logger.info("Channel %s test successful by %s", channel_id, user["username"])
            return ApiResponse(data={"channel": channel_id, "success": True, "message": message})
        else:
            logger.warning("Channel %s test failed by %s: %s", channel_id, user["username"], message)
            raise HTTPException(
                status_code=400, detail=NotifyErrors.CHANNEL_TEST_FAILED
            )  # FIXED-P1: 原问题-detail=message可能泄露内部信息，改用错误码
    except HTTPException:
        raise
    except ValueError as e:
        logger.warning("test_channel validation error: %s", e)
        raise HTTPException(
            status_code=400, detail=NotifyErrors.CHANNEL_TEST_FAILED
        ) from e  # FIXED-P1: 原问题-detail=str(e)泄露内部错误
    except Exception as e:
        logger.error("test_channel failed: %s", e)
        raise HTTPException(
            status_code=500, detail=NotifyErrors.CHANNEL_TEST_FAILED
        ) from e  # FIXED-P1: 原问题-detail=str(e)泄露内部错误


@router.post("/channels/{channel_id}/enable", response_model=ApiResponse)
async def enable_channel(
    channel_id: str,
    user: dict[str, str] = Depends(require_permission(Permission.CONFIG_EDIT)),
    req: ChannelEnableRequest | None = None,
    request: Request = None,
    audit_svc: AuditServiceDep = None,
):
    """启用/禁用通知渠道"""
    try:
        enabled = req.enabled if req else True
        manager = get_notification_manager()

        if hasattr(manager, "set_channel_enabled"):
            result = manager.set_channel_enabled(channel_id, enabled)
            if result:
                logger.info("Channel %s %s by %s", channel_id, "enabled" if enabled else "disabled", user["username"])
                # 第四轮修复: 审计日志记录启用/禁用操作
                from edgelite.services.audit_service import AuditAction

                await _audit_notify_channel(
                    audit_svc,
                    AuditAction.NOTIFY_CHANNEL_TOGGLE,
                    user,
                    request,
                    channel_id,
                    before_value={"enabled": not enabled},
                    after_value={"enabled": enabled},
                )
                return ApiResponse(data={"channel": channel_id, "enabled": enabled})
            else:
                raise HTTPException(status_code=404, detail=NotifyErrors.CHANNEL_NOT_FOUND)
        else:
            raise HTTPException(status_code=501, detail=NotifyErrors.CHANNEL_MANAGER_UNAVAILABLE)
    except HTTPException:
        raise
    except ValueError as e:
        logger.warning("enable_channel validation error: %s", e)
        raise HTTPException(
            status_code=400, detail=NotifyErrors.CHANNEL_SAVE_FAILED
        ) from e  # FIXED-P1: 原问题-detail=str(e)泄露内部错误
    except Exception as e:
        logger.error("enable_channel failed: %s", e)
        raise HTTPException(
            status_code=500, detail=NotifyErrors.CHANNEL_SAVE_FAILED
        ) from e  # FIXED-P1: 原问题-detail=str(e)泄露内部错误


@router.delete("/channels/{channel_id}", response_model=ApiResponse)
async def delete_channel(
    channel_id: str,
    user: dict[str, str] = Depends(require_permission(Permission.CONFIG_EDIT)),
    request: Request = None,
    audit_svc: AuditServiceDep = None,
):
    """删除/禁用通知渠道配置"""
    try:
        config = get_config()
        notify_config = config.notify

        # 第四轮修复: 记录删除前配置用于审计
        before_value = {}
        if channel_id == "dingtalk":
            before_value = {
                "webhook_url": notify_config.dingtalk.webhook_url,
                "secret": notify_config.dingtalk.secret,
            }
            notify_config.dingtalk.webhook_url = ""
            notify_config.dingtalk.secret = ""
        elif channel_id == "wecom":
            before_value = {"webhook_url": notify_config.wechat.webhook_url}
            notify_config.wechat.webhook_url = ""
        elif channel_id == "email":
            before_value = {
                "smtp_host": notify_config.email.smtp_host,
                "smtp_password": notify_config.email.smtp_password,
            }
            notify_config.email.smtp_host = ""
            notify_config.email.smtp_password = ""
        elif channel_id == "webhook":
            before_value = {
                "url": notify_config.webhook.url,
                "auth_token": notify_config.webhook.auth_token,
            }
            notify_config.webhook.url = ""
            notify_config.webhook.auth_token = ""
        else:
            raise HTTPException(status_code=404, detail=NotifyErrors.CHANNEL_NOT_FOUND)

        save_config(config)
        logger.info("Channel %s cleared by %s", channel_id, user["username"])

        # 第四轮修复: 审计日志记录渠道删除操作
        from edgelite.services.audit_service import AuditAction

        await _audit_notify_channel(
            audit_svc,
            AuditAction.NOTIFY_CHANNEL_DELETE,
            user,
            request,
            channel_id,
            before_value=before_value,
        )

        return ApiResponse(data={"channel": channel_id, "deleted": True})
    except HTTPException:
        raise
    except ValueError as e:
        logger.warning("delete_channel validation error: %s", e)
        raise HTTPException(
            status_code=400, detail=NotifyErrors.CHANNEL_DELETE_FAILED
        ) from e  # FIXED-P1: 原问题-detail=str(e)泄露内部错误
    except Exception as e:
        logger.error("Failed to delete channel config: %s", e)
        raise HTTPException(status_code=500, detail=NotifyErrors.CHANNEL_DELETE_FAILED) from e
