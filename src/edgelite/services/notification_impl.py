"""Multi-channel alarm notification service

Supports: DingTalk, WeCom (Enterprise WeChat), Email (SMTP), Webhook
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import random
import smtplib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urlparse, urlunparse

import aiohttp

# FIXED(安全): 导入 DNS Rebinding 防护所需的 IP 缓存及锁，用于 send()/test() 发送请求时使用校验时缓存的 IP
from edgelite.services.notify_service import (
    _validate_webhook_url,
    _webhook_ip_cache,
    _webhook_ip_cache_lock,
)

logger = logging.getLogger(__name__)


def _sanitize_email_header(value: str) -> str:
    """FIXED(严重): 过滤邮件头中的 CRLF 字符，防止 CRLF 注入。

    html.escape() 不会转义 \\r\\n，攻击者可通过 rule_name、device_id 等用户可控输入
    向 Subject/From/To 注入额外的邮件头（如 Bcc），导致邮件头注入。
    此函数剥离 \\r 和 \\n，确保头值单行。
    """
    if not isinstance(value, str):
        return value
    return value.replace("\r", "").replace("\n", "")


def _interpolate_template(template: str, notification: AlarmNotification) -> str:
    """修复23: 模板变量插值 - 将 {variable} 占位符替换为告警实际值

    支持的变量:
      {alarm_id}, {rule_id}, {rule_name}, {device_id}, {device_name},
      {severity}, {action}, {message}, {timestamp}, {trigger_count},
      {escalation_level}, {original_severity}, {duration_seconds}
    """
    if not template:
        return template
    variables = {
        "alarm_id": notification.alarm_id,
        "rule_id": notification.rule_id,
        "rule_name": notification.rule_name,
        "device_id": notification.device_id,
        "device_name": notification.device_name,
        "severity": notification.severity,
        "action": notification.action,
        "message": notification.message or "",
        "timestamp": notification.timestamp,
        "trigger_count": str(notification.trigger_count),
        "escalation_level": str(notification.escalation_level),
        "original_severity": notification.original_severity,
        "duration_seconds": str(notification.duration_seconds),
    }
    try:
        return template.format_map(_SafeDict(variables))
    except Exception:
        return template


class _SafeDict(dict):
    """字典子类：缺失键返回占位符原样，避免 KeyError 中断通知发送"""
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _check_dingtalk_host(url: str) -> bool:
    """钉钉 webhook 仅允许官方域名 oapi.dingtalk.com"""
    try:
        hostname = urlparse(url).hostname
    except (ValueError, TypeError):
        return False
    return hostname == "oapi.dingtalk.com"


def _check_wecom_host(url: str) -> bool:
    """企业微信 webhook 仅允许官方域名 qyapi.weixin.qq.com"""
    try:
        hostname = urlparse(url).hostname
    except (ValueError, TypeError):
        return False
    return hostname == "qyapi.weixin.qq.com"


@dataclass
class AlarmNotification:
    """Alarm notification payload"""
    alarm_id: str
    rule_id: str
    rule_name: str
    device_id: str
    device_name: str
    severity: str  # critical/major/minor/warning/info
    action: str  # firing/recovered/acknowledged/escalated
    message: str
    trigger_value: dict = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    # Escalation info
    escalation_level: int = 0
    original_severity: str = ""
    # Statistics
    trigger_count: int = 1
    duration_seconds: float = 0.0


@dataclass
class NotificationChannelConfig:
    """Base configuration for notification channels"""
    enabled: bool = True
    name: str = ""
    # Rate limiting
    max_per_minute: int = 10
    cooldown_seconds: float = 60.0
    # 修复23: 自定义消息模板（支持 {alarm_id}/{rule_name}/{device_name}/{severity} 等变量插值）
    message_template: str = ""


@dataclass
class DingTalkConfig(NotificationChannelConfig):
    """DingTalk webhook configuration"""
    webhook_url: str = ""
    secret: str = ""  # For signature
    at_mobiles: list[str] = field(default_factory=list)
    is_at_all: bool = False


@dataclass
class WeComConfig(NotificationChannelConfig):
    """WeCom (Enterprise WeChat) webhook configuration"""
    webhook_url: str = ""
    corp_id: str = ""
    agent_id: str = ""


@dataclass
class EmailConfig(NotificationChannelConfig):
    """Email SMTP configuration"""
    smtp_host: str = "localhost"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    from_address: str = ""
    to_addresses: list[str] = field(default_factory=list)
    use_tls: bool = True
    use_ssl: bool = False


@dataclass
class WebhookConfig(NotificationChannelConfig):
    """Generic webhook configuration"""
    url: str = ""
    method: str = "POST"  # POST or PUT
    headers: dict[str, str] = field(default_factory=dict)
    auth_type: str = "none"  # none/basic/bearer/api_key
    auth_token: str = ""
    auth_username: str = ""
    auth_password: str = ""
    retry_count: int = 3
    retry_delay: float = 1.0


class NotificationChannel(ABC):
    """Base class for notification channels"""

    def __init__(self, config: NotificationChannelConfig):
        self._config = config
        self._enabled = config.enabled
        self._last_sent: dict[str, float] = {}  # key -> last sent timestamp
        self._rate_limiter: dict[str, int] = {}  # key -> count in current minute
        # R6-S-08: 基类统一重试参数（指数退避+抖动），覆盖 DingTalk/WeCom/Email 渠道
        # WebhookChannel 已有内部重试逻辑，在其 __init__ 中将此值置为 1 以避免双重重试
        self._notify_retry_count: int = 3
        self._notify_retry_base_delay: float = 1.0  # 基础退避延迟(秒)，实际延迟 = base * 2^attempt * (0.5~1.0 抖动)
        # 共享的 aiohttp.ClientSession，复用连接池，避免每次 send/test 都新建 session
        self._session: aiohttp.ClientSession | None = None

    @property
    def name(self) -> str:
        return self._config.name or self.__class__.__name__

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled

    def _get_session(self) -> aiohttp.ClientSession:
        """获取共享的 aiohttp.ClientSession，复用连接池。

        各通知渠道的 send/test 方法应复用此 session，避免每次调用都新建
        ClientSession 导致连接池无法复用、增加 TLS 握手开销。
        """
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        """清理共享的 aiohttp.ClientSession，释放连接池资源"""
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    def _check_rate_limit(self, key: str = "default") -> bool:
        """Check if rate limit is exceeded. Returns True if can send."""
        now = datetime.now(UTC).timestamp()
        minute_key = f"{key}:{int(now / 60)}"

        # Reset counter if minute changed
        if minute_key != self._last_sent.get("_minute"):
            self._rate_limiter.clear()
            self._last_sent["_minute"] = minute_key
            # R6-S-07: 清理 _last_sent 中过期的 per-key 条目，防止内存无限增长
            # 保留 _minute 键；移除已超过 cooldown_seconds 的条目（冷却已过，不再需要判断）
            cooldown = self._config.cooldown_seconds
            expired_keys = [
                k for k, ts in self._last_sent.items()
                if k != "_minute" and (now - ts) > cooldown
            ]
            for k in expired_keys:
                del self._last_sent[k]
            # R6-S-07: 上限保护，防止极端情况下 _last_sent 仍无限增长（淘汰最旧条目）
            _MAX_LAST_SENT_KEYS = 10000
            if len(self._last_sent) > _MAX_LAST_SENT_KEYS:
                sorted_keys = sorted(
                    (k for k in self._last_sent if k != "_minute"),
                    key=lambda k: self._last_sent[k],
                )
                excess = len(self._last_sent) - _MAX_LAST_SENT_KEYS
                for k in sorted_keys[:excess]:
                    del self._last_sent[k]

        count = self._rate_limiter.get(key, 0)
        if count >= self._config.max_per_minute:
            return False

        self._rate_limiter[key] = count + 1
        return True

    def _check_cooldown(self, key: str = "default") -> bool:
        """Check if cooldown period has passed. Returns True if can send."""
        now = datetime.now(UTC).timestamp()
        last = self._last_sent.get(key, 0)
        if now - last < self._config.cooldown_seconds:
            return False
        self._last_sent[key] = now
        return True

    @abstractmethod
    async def send(self, notification: AlarmNotification) -> bool:
        """Send notification. Returns True on success."""
        pass

    @abstractmethod
    async def test(self) -> tuple[bool, str]:
        """Test channel connectivity. Returns (success, message)."""
        pass

    async def notify(self, notification: AlarmNotification) -> bool:
        """Send notification with rate limiting and cooldown checks."""
        if not self._enabled:
            logger.debug("[%s] Channel disabled, skipping notification", self.name)
            return False

        key = f"{notification.alarm_id}:{notification.action}"
        if not self._check_cooldown(key):
            logger.debug("[%s] Cooldown active for %s", self.name, key)
            return False

        if not self._check_rate_limit(key):
            logger.warning("[%s] Rate limit exceeded for %s", self.name, key)
            return False

        # R6-S-08: 统一重试逻辑（指数退避+抖动），覆盖 DingTalk/WeCom/Email 渠道
        # WebhookChannel _notify_retry_count=1，由其 send 内部重试逻辑处理
        last_error: Exception | None = None
        for attempt in range(self._notify_retry_count):
            try:
                result = await self.send(notification)
                if result:
                    return True
            except Exception as e:
                last_error = e
                logger.warning(
                    "[%s] Attempt %d/%d failed: %s",
                    self.name, attempt + 1, self._notify_retry_count, e,
                )
            # 最后一次尝试不再等待
            if attempt < self._notify_retry_count - 1:
                # 指数退避 + 抖动，避免多实例惊群
                delay = self._notify_retry_base_delay * (2 ** attempt) * (0.5 + random.random() * 0.5)
                await asyncio.sleep(delay)

        if last_error:
            logger.error(
                "[%s] All %d retry attempts failed: %s",
                self.name, self._notify_retry_count, last_error,
            )
        else:
            logger.error(
                "[%s] All %d retry attempts failed (send returned False)",
                self.name, self._notify_retry_count,
            )
        return False


class DingTalkChannel(NotificationChannel):
    """DingTalk (DingMessage) webhook notification channel"""

    def __init__(self, config: DingTalkConfig):
        super().__init__(config)
        self._webhook_url = config.webhook_url
        self._secret = config.secret
        self._at_mobiles = config.at_mobiles
        self._is_at_all = config.is_at_all
        self._message_template = config.message_template

    async def send(self, notification: AlarmNotification) -> bool:
        """Send DingTalk notification via webhook"""
        if not self._webhook_url:
            logger.warning("[DingTalk] Webhook URL not configured")
            return False

        # FIXED(安全): SSRF 防护 - 钉钉 webhook 仅允许官方域名 oapi.dingtalk.com
        if not _check_dingtalk_host(self._webhook_url):
            logger.warning("[DingTalk] Webhook URL host not allowed, SSRF blocked")
            return False

        # 修复23: 应用自定义消息模板
        notification_message = (
            _interpolate_template(self._message_template, notification)
            if self._message_template else notification.message
        )

        # Build message based on severity
        severity_emoji = {
            "critical": "🔴 CRITICAL",
            "major": "🟠 MAJOR",
            "minor": "🟡 MINOR",
            "warning": "⚠️ WARNING",
            "info": "ℹ️ INFO",
        }
        action_text = {
            "firing": "ALARM TRIGGERED",
            "recovered": "ALARM RECOVERED",
            "acknowledged": "ALARM ACKNOWLEDGED",
            "escalated": "ALARM ESCALATED",
        }

        emoji = severity_emoji.get(notification.severity, "⚠️")
        action = action_text.get(notification.action, notification.action.upper())

        # Build markdown content
        content = f"## {emoji} {action}\n\n"
        content += f"**Alarm ID**: {notification.alarm_id}\n"
        content += f"**Rule**: {notification.rule_name}\n"
        content += f"**Device**: {notification.device_name} ({notification.device_id})\n"
        content += f"**Severity**: {notification.severity.upper()}\n"
        content += f"**Time**: {notification.timestamp}\n"

        if notification_message:
            content += f"**Message**: {notification_message}\n"

        if notification.trigger_value:
            content += "\n**Trigger Values**:\n"
            for k, v in notification.trigger_value.items():
                content += f"- {k}: {v}\n"

        if notification.action == "escalated":
            content += f"\n**Escalation Level**: {notification.escalation_level}\n"
            content += f"**Original Severity**: {notification.original_severity.upper()}\n"

        if notification.action == "recovered" and notification.duration_seconds > 0:
            duration_str = self._format_duration(notification.duration_seconds)
            content += f"\n**Duration**: {duration_str}\n"

        # Build payload
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": f"{emoji} {action} - {notification.rule_name}",
                "text": content,
            },
        }

        # Add at info
        if self._at_mobiles or self._is_at_all:
            payload["at"] = {
                "atMobiles": self._at_mobiles,
                "isAtAll": self._is_at_all,
            }

        try:
            session = self._get_session()
            async with session.post(
                self._webhook_url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                result = await resp.json()
                if resp.status == 200 and result.get("errcode") == 0:
                    logger.info("[DingTalk] Notification sent: %s", notification.alarm_id)
                    return True
                else:
                    logger.error("[DingTalk] Send failed: %s", result)
                    return False
        except Exception as e:
            logger.error("[DingTalk] Request failed: %s", e)
            return False

    async def test(self) -> tuple[bool, str]:
        """Test DingTalk webhook connectivity"""
        if not self._webhook_url:
            return False, "Webhook URL not configured"

        # FIXED(安全): SSRF 防护 - 钉钉 webhook 仅允许官方域名 oapi.dingtalk.com
        if not _check_dingtalk_host(self._webhook_url):
            return False, "Webhook URL host not allowed"

        # FIXED-P0: 原代码创建了 AlarmNotification 对象但未使用（第273-282行），已删除无用代码

        try:
            session = self._get_session()
            async with session.post(
                self._webhook_url,
                json={"msgtype": "text", "text": {"content": "EdgeLiteGateway Test Message"}},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                result = await resp.json()
                if resp.status == 200 and result.get("errcode") == 0:
                    return True, "Test message sent successfully"
                return False, f"API error: {result.get('errmsg', 'Unknown error')}"
        except Exception as e:
            return False, f"Connection failed: {str(e)}"

    @staticmethod
    def _format_duration(seconds: float) -> str:
        """Format duration in human-readable format"""
        if seconds < 60:
            return f"{seconds:.0f}s"
        elif seconds < 3600:
            return f"{seconds / 60:.1f}m"
        elif seconds < 86400:
            return f"{seconds / 3600:.1f}h"
        else:
            return f"{seconds / 86400:.1f}d"


class WeComChannel(NotificationChannel):
    """WeCom (Enterprise WeChat) webhook notification channel"""

    def __init__(self, config: WeComConfig):
        super().__init__(config)
        self._webhook_url = config.webhook_url
        self._corp_id = config.corp_id
        self._agent_id = config.agent_id
        self._message_template = config.message_template

    async def send(self, notification: AlarmNotification) -> bool:
        """Send WeCom notification via webhook"""
        if not self._webhook_url:
            logger.warning("[WeCom] Webhook URL not configured")
            return False

        # FIXED(安全): SSRF 防护 - 企业微信 webhook 仅允许官方域名 qyapi.weixin.qq.com
        if not _check_wecom_host(self._webhook_url):
            logger.warning("[WeCom] Webhook URL host not allowed, SSRF blocked")
            return False

        # 修复23: 应用自定义消息模板
        notification_message = (
            _interpolate_template(self._message_template, notification)
            if self._message_template else notification.message
        )

        # Build message based on action
        action_text = {
            "firing": "ALARM TRIGGERED",
            "recovered": "ALARM RECOVERED",
            "acknowledged": "ALARM ACKNOWLEDGED",
            "escalated": "ALARM ESCALATED",
        }
        action = action_text.get(notification.action, notification.action.upper())

        # Build text content
        content = f"{action}\n\n"
        content += f"Alarm ID: {notification.alarm_id}\n"
        content += f"Rule: {notification.rule_name}\n"
        content += f"Device: {notification.device_name}\n"
        content += f"Severity: {notification.severity.upper()}\n"
        content += f"Time: {notification.timestamp}\n"

        if notification_message:
            content += f"\nMessage: {notification_message}\n"

        if notification.trigger_value:
            content += "\nTrigger Values:\n"
            for k, v in list(notification.trigger_value.items())[:5]:
                content += f"• {k}: {v}\n"
            if len(notification.trigger_value) > 5:
                content += f"... and {len(notification.trigger_value) - 5} more\n"

        if notification.action == "escalated":
            content += f"\nEscalation Level: {notification.escalation_level}\n"

        payload = {"msgtype": "text", "text": {"content": content}}

        try:
            session = self._get_session()
            async with session.post(
                self._webhook_url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                result = await resp.json()
                if resp.status == 200 and result.get("errcode") == 0:
                    logger.info("[WeCom] Notification sent: %s", notification.alarm_id)
                    return True
                else:
                    logger.error("[WeCom] Send failed: %s", result)
                    return False
        except Exception as e:
            logger.error("[WeCom] Request failed: %s", e)
            return False

    async def test(self) -> tuple[bool, str]:
        """Test WeCom webhook connectivity"""
        if not self._webhook_url:
            return False, "Webhook URL not configured"

        # FIXED(安全): SSRF 防护 - 企业微信 webhook 仅允许官方域名 qyapi.weixin.qq.com
        if not _check_wecom_host(self._webhook_url):
            return False, "Webhook URL host not allowed"

        try:
            session = self._get_session()
            async with session.post(
                self._webhook_url,
                json={"msgtype": "text", "text": {"content": "EdgeLiteGateway Test Message"}},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                result = await resp.json()
                if resp.status == 200 and result.get("errcode") == 0:
                    return True, "Test message sent successfully"
                return False, f"API error: {result.get('errmsg', 'Unknown error')}"
        except Exception as e:
            return False, f"Connection failed: {str(e)}"


class EmailChannel(NotificationChannel):
    """Email notification channel via SMTP"""

    def __init__(self, config: EmailConfig):
        super().__init__(config)
        self._smtp_host = config.smtp_host
        self._smtp_port = config.smtp_port
        self._smtp_user = config.smtp_user
        self._smtp_password = config.smtp_password
        self._from_address = config.from_address
        self._to_addresses = config.to_addresses
        self._use_tls = config.use_tls
        self._use_ssl = config.use_ssl
        self._message_template = config.message_template

    async def send(self, notification: AlarmNotification) -> bool:
        """Send email notification via SMTP"""
        if not self._to_addresses:
            logger.warning("[Email] No recipient addresses configured")
            return False

        if not self._from_address:
            logger.warning("[Email] Sender address not configured")
            return False

        # 修复23: 应用自定义消息模板
        notification_message = (
            _interpolate_template(self._message_template, notification)
            if self._message_template else notification.message
        )

        # Build email content
        action_text = {
            "firing": "ALARM TRIGGERED",
            "recovered": "ALARM RECOVERED",
            "acknowledged": "ALARM ACKNOWLEDGED",
            "escalated": "ALARM ESCALATED",
        }
        action = action_text.get(notification.action, notification.action.upper())

        severity_colors = {
            "critical": "#DC3545",
            "major": "#FD7E14",
            "minor": "#FFC107",
            "warning": "#17A2B8",
            "info": "#6C757D",
        }
        color = severity_colors.get(notification.severity, "#6C757D")

        html_content = f"""
        <html>
        <body style="font-family: Arial, sans-serif; margin: 20px;">
            <div style="background-color: {color}; color: white; padding: 15px; border-radius: 5px;">
                <h2 style="margin: 0;">{action}</h2>
            </div>
            <div style="padding: 20px; border: 1px solid #ddd; border-top: none; border-radius: 0 0 5px 5px;">
                <table style="width: 100%; border-collapse: collapse;">
                    <tr>
                        <td style="padding: 8px; font-weight: bold; width: 140px;">Alarm ID:</td>
                        <td style="padding: 8px;">{notification.alarm_id}</td>
                    </tr>
                    <tr style="background-color: #f9f9f9;">
                        <td style="padding: 8px; font-weight: bold;">Rule:</td>
                        <td style="padding: 8px;">{html.escape(str(notification.rule_name))}</td>
                    </tr>
                    <tr>
                        <td style="padding: 8px; font-weight: bold;">Device:</td>
                        <td style="padding: 8px;">{html.escape(str(notification.device_name))} ({html.escape(str(notification.device_id))})</td>
                    </tr>
                    <tr style="background-color: #f9f9f9;">
                        <td style="padding: 8px; font-weight: bold;">Severity:</td>
                        <td style="padding: 8px;"><span style="color: {color}; font-weight: bold;">{notification.severity.upper()}</span></td>
                    </tr>
                    <tr>
                        <td style="padding: 8px; font-weight: bold;">Time:</td>
                        <td style="padding: 8px;">{notification.timestamp}</td>
                    </tr>
                </table>
        """

        if notification_message:
            html_content += f"""
                <div style="margin-top: 15px; padding: 10px; background-color: #f0f0f0; border-left: 4px solid {color};">
                    <strong>Message:</strong><br/>
                    {html.escape(str(notification_message))}
                </div>
            """

        if notification.trigger_value:
            html_content += """
                <h3 style="margin-top: 20px;">Trigger Values</h3>
                <table style="width: 100%; border-collapse: collapse;">
            """
            for k, v in notification.trigger_value.items():
                html_content += f"""
                    <tr>
                        <td style="padding: 6px; border-bottom: 1px solid #ddd;">{html.escape(str(k))}</td>
                        <td style="padding: 6px; border-bottom: 1px solid #ddd; font-family: monospace;">{html.escape(str(v))}</td>
                    </tr>
                """
            html_content += "</table>"

        if notification.action == "escalated":
            html_content += f"""
                <div style="margin-top: 15px; padding: 10px; background-color: #fff3cd; border-left: 4px solid #856404;">
                    <strong>Escalation Notice:</strong><br/>
                    Level {notification.escalation_level} escalation - Original severity: {notification.original_severity.upper()}
                </div>
            """

        if notification.action == "recovered" and notification.duration_seconds > 0:
            duration_str = self._format_duration(notification.duration_seconds)
            html_content += f"""
                <div style="margin-top: 15px; padding: 10px; background-color: #d4edda; border-left: 4px solid #155724;">
                    <strong>Recovery Info:</strong><br/>
                    Duration: {duration_str}
                </div>
            """

        html_content += """
                <hr style="margin: 20px 0;"/>
                <p style="color: #666; font-size: 12px;">
                    This is an automated notification from <strong>EdgeLiteGateway</strong>.<br/>
                    Please do not reply to this email.
                </p>
            </div>
        </body>
        </html>
        """

        # Plain text version
        text_content = f"""
{action}
{'=' * 50}

Alarm ID: {notification.alarm_id}
Rule: {notification.rule_name}
Device: {notification.device_name} ({notification.device_id})
Severity: {notification.severity.upper()}
Time: {notification.timestamp}

"""

        if notification_message:
            text_content += f"Message: {notification_message}\n\n"

        if notification.trigger_value:
            text_content += "Trigger Values:\n"
            for k, v in notification.trigger_value.items():
                text_content += f"  {k}: {v}\n"
            text_content += "\n"

        if notification.action == "escalated":
            text_content += f"Escalation Notice: Level {notification.escalation_level}\n\n"

        if notification.action == "recovered" and notification.duration_seconds > 0:
            text_content += f"Duration: {self._format_duration(notification.duration_seconds)}\n\n"

        text_content += "--\nThis is an automated notification from EdgeLiteGateway."

        # Create message
        msg = MIMEMultipart("alternative")
        # FIXED(严重): 对所有拼接到邮件头的用户可控输入过滤 CRLF，防止邮件头注入
        # notification.rule_name、notification.severity、action 等均可能含用户可控内容
        msg["Subject"] = _sanitize_email_header(
            f"[{notification.severity.upper()}] {action} - {notification.rule_name}"
        )
        msg["From"] = _sanitize_email_header(self._from_address)
        msg["To"] = _sanitize_email_header(", ".join(self._to_addresses))

        msg.attach(MIMEText(text_content, "plain"))
        msg.attach(MIMEText(html_content, "html"))

        # Send email
        try:
            # R11-SVC-05: asyncio.get_event_loop() 在 Python 3.10+ 已弃用，改用 get_running_loop()
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._send_sync, msg)
            logger.info("[Email] Notification sent: %s", notification.alarm_id)
            return True
        except Exception as e:
            logger.error("[Email] Send failed: %s", e)
            return False

    def _send_sync(self, msg: MIMEMultipart) -> None:
        """Synchronous email send (runs in thread pool)"""
        # FIXED-P1: 原代码无超时控制，SMTP 连接可能长时间阻塞线程池
        # 添加 30 秒超时
        if self._use_ssl:
            server = smtplib.SMTP_SSL(self._smtp_host, self._smtp_port, timeout=30)
        else:
            server = smtplib.SMTP(self._smtp_host, self._smtp_port, timeout=30)

        try:
            if self._use_tls and not self._use_ssl:
                server.starttls()

            if self._smtp_user and self._smtp_password:
                server.login(self._smtp_user, self._smtp_password)

            server.sendmail(self._from_address, self._to_addresses, msg.as_string())
        finally:
            with contextlib.suppress(Exception):
                server.quit()

    async def test(self) -> tuple[bool, str]:
        """Test SMTP connectivity"""
        if not self._smtp_host:
            return False, "SMTP host not configured"

        try:
            # R11-SVC-05: asyncio.get_event_loop() 在 Python 3.10+ 已弃用，改用 get_running_loop()
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                lambda: self._test_connection(),
            )
            return True, "SMTP connection successful"
        except Exception as e:
            return False, f"Connection failed: {str(e)}"

    def _test_connection(self) -> None:
        """Test SMTP connection (runs in thread pool)"""
        # FIXED-P1: 原代码无超时控制，添加 30 秒超时
        if self._use_ssl:
            server = smtplib.SMTP_SSL(self._smtp_host, self._smtp_port, timeout=30)
        else:
            server = smtplib.SMTP(self._smtp_host, self._smtp_port, timeout=30)

        try:
            if self._use_tls and not self._use_ssl:
                server.starttls()
            if self._smtp_user and self._smtp_password:
                server.login(self._smtp_user, self._smtp_password)
        finally:
            with contextlib.suppress(Exception):
                server.quit()

    @staticmethod
    def _format_duration(seconds: float) -> str:
        """Format duration in human-readable format"""
        if seconds < 60:
            return f"{seconds:.0f} seconds"
        elif seconds < 3600:
            return f"{seconds / 60:.1f} minutes"
        elif seconds < 86400:
            return f"{seconds / 3600:.1f} hours"
        else:
            return f"{seconds / 86400:.1f} days"


class WebhookChannel(NotificationChannel):
    """Generic webhook notification channel"""

    def __init__(self, config: WebhookConfig):
        super().__init__(config)
        self._url = config.url
        self._method = config.method.upper()
        self._headers = dict(config.headers)
        self._auth_type = config.auth_type
        self._auth_token = config.auth_token
        self._auth_username = config.auth_username
        self._auth_password = config.auth_password
        self._retry_count = config.retry_count
        self._retry_delay = config.retry_delay
        self._message_template = config.message_template
        # R6-S-08: WebhookChannel 已有内部重试逻辑（send 方法中的 for attempt 循环），
        # 禁用基类 notify 重试以避免双重重试导致总尝试次数 = 3*3=9
        self._notify_retry_count = 1

    def _build_ssrf_safe_request(self) -> tuple[str, str | None, str | None]:
        """构建 SSRF 安全的请求 URL 及相关参数。

        FIXED(安全): DNS Rebinding 防护 - _validate_webhook_url 校验时解析 DNS 并缓存 IP，
        但实际请求若使用原始 URL（含域名），aiohttp 重新解析 DNS 时攻击者可返回内网 IP
        绕过 SSRF 防护。修复：使用校验时缓存的 IP 替换 URL 中的 hostname 发起请求，
        Host 头保持原域名，HTTPS 通过 server_hostname 保持 TLS SNI 和证书验证。
        参考 notify_service.py 的 _send_webhook 修复方式。
        """
        parsed = urlparse(self._url)
        hostname = parsed.hostname or ""
        with _webhook_ip_cache_lock:
            resolved_ip = _webhook_ip_cache.get(hostname)

        # 无缓存 IP 或 IP 与域名相同（如直接用 IP 访问），使用原始 URL
        if not resolved_ip or resolved_ip == hostname:
            return self._url, None, None

        # 用缓存的 IP 替换 URL 中的 hostname
        if ":" in resolved_ip:  # IPv6
            new_netloc = f"[{resolved_ip}]:{parsed.port}" if parsed.port else f"[{resolved_ip}]"
        else:
            new_netloc = f"{resolved_ip}:{parsed.port}" if parsed.port else resolved_ip
        request_url = urlunparse((
            parsed.scheme, new_netloc, parsed.path or "/",
            parsed.params, parsed.query, parsed.fragment,
        ))
        # Host 头保持原域名；HTTPS 通过 server_hostname 保持 SNI
        server_hostname = hostname if parsed.scheme == "https" else None
        return request_url, hostname, server_hostname

    async def send(self, notification: AlarmNotification) -> bool:
        """Send notification via webhook"""
        if not self._url:
            logger.warning("[Webhook] URL not configured")
            return False

        # FIXED(安全): SSRF 防护 - 自定义 webhook 调用 _validate_webhook_url 完整校验
        if not _validate_webhook_url(self._url):
            logger.warning("[Webhook] URL rejected by SSRF protection: %s", self._url)
            return False

        # FIXED(安全): DNS Rebinding 防护 - 使用校验时缓存的 IP 构造请求 URL，避免 aiohttp 重新解析 DNS 绕过 SSRF
        request_url, host_header, server_hostname = self._build_ssrf_safe_request()

        # 修复23: 应用自定义消息模板
        notification_message = (
            _interpolate_template(self._message_template, notification)
            if self._message_template else notification.message
        )

        # Build payload
        payload = {
            "alarm_id": notification.alarm_id,
            "rule_id": notification.rule_id,
            "rule_name": notification.rule_name,
            "device_id": notification.device_id,
            "device_name": notification.device_name,
            "severity": notification.severity,
            "action": notification.action,
            "message": notification_message,
            "trigger_value": notification.trigger_value,
            "timestamp": notification.timestamp,
        }

        if notification.action == "escalated":
            payload["escalation"] = {
                "level": notification.escalation_level,
                "original_severity": notification.original_severity,
            }

        if notification.action == "recovered":
            payload["recovery"] = {
                "duration_seconds": notification.duration_seconds,
            }

        # Build headers
        headers = dict(self._headers)
        headers["Content-Type"] = "application/json"

        # Add auth
        if self._auth_type == "bearer" and self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"
        elif self._auth_type == "api_key" and self._auth_token:
            headers["X-API-Key"] = self._auth_token

        # FIXED(安全): DNS Rebinding 防护 - Host 头保持原域名，确保目标服务正确路由
        if host_header:
            headers["Host"] = host_header

        # Send with retry
        # FIXED(一般): 原问题-ClientSession 在重试循环内重复创建，每次重试新建并销毁连接池，
        # TCP 连接无法复用且增加 TLS 握手开销；修复：复用基类共享的 ClientSession
        last_error = None
        session = self._get_session()
        # FIXED-P1: 原代码 getattr(session, self._method.lower()) 可能返回 None，
        # 调用 None() 会抛出 TypeError。添加方法有效性检查
        method_func = getattr(session, self._method.lower(), None)
        if method_func is None or not callable(method_func):
            last_error = f"Invalid HTTP method: {self._method}"
            logger.error("[Webhook] %s", last_error)
            return False
        for attempt in range(self._retry_count):
            try:
                # FIXED(安全): DNS Rebinding 防护 - 使用缓存的 IP 发送请求，避免 aiohttp 重新解析 DNS
                req_kwargs: dict = {
                    "json": payload,
                    "headers": headers,
                    "timeout": aiohttp.ClientTimeout(total=30),
                }
                if server_hostname:
                    req_kwargs["server_hostname"] = server_hostname
                async with method_func(request_url, **req_kwargs) as resp:
                    if 200 <= resp.status < 300:
                        logger.info("[Webhook] Notification sent: %s", notification.alarm_id)
                        return True
                    text = await resp.text()
                    last_error = f"HTTP {resp.status}: {text[:200]}"
                    logger.warning("[Webhook] Attempt %d failed: %s", attempt + 1, last_error)
                    # FIXED-P1: 4xx 客户端错误不应重试（请求格式错误，重试无意义）
                    if 400 <= resp.status < 500 and resp.status != 429:
                        logger.warning("[Webhook] Client error %d, skipping retry", resp.status)
                        break

            except Exception as e:
                last_error = str(e)
                logger.warning("[Webhook] Attempt %d failed: %s", attempt + 1, e)

            if attempt < self._retry_count - 1:
                await asyncio.sleep(self._retry_delay * (attempt + 1) * (0.5 + random.random() * 0.5))  # FIXED-P2: 原问题-重试退避无抖动，多实例惊群

        logger.error("[Webhook] All retry attempts failed: %s", last_error)
        return False

    async def test(self) -> tuple[bool, str]:
        """Test webhook connectivity"""
        if not self._url:
            return False, "Webhook URL not configured"

        # FIXED(安全): SSRF 防护 - 自定义 webhook 调用 _validate_webhook_url 完整校验
        if not _validate_webhook_url(self._url):
            return False, "Webhook URL rejected by SSRF protection"

        # FIXED(安全): DNS Rebinding 防护 - 使用校验时缓存的 IP 构造请求 URL，避免 aiohttp 重新解析 DNS 绕过 SSRF
        request_url, host_header, server_hostname = self._build_ssrf_safe_request()

        test_payload = {
            "type": "test",
            "source": "EdgeLiteGateway",
            "timestamp": datetime.now(UTC).isoformat(),
            "message": "This is a test notification from EdgeLiteGateway",
        }

        headers = dict(self._headers)
        headers["Content-Type"] = "application/json"

        if self._auth_type == "bearer" and self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"
        elif self._auth_type == "api_key" and self._auth_token:
            headers["X-API-Key"] = self._auth_token

        # FIXED(安全): DNS Rebinding 防护 - Host 头保持原域名，确保目标服务正确路由
        if host_header:
            headers["Host"] = host_header

        try:
            session = self._get_session()
            # FIXED(P1): 原问题-test()方法getattr(session, self._method.lower())未做None/可调用性检查，
            # 与send()方法修复不一致；非法method会抛TypeError。修复-对齐send()方法的校验逻辑
            method_func = getattr(session, self._method.lower(), None)
            if method_func is None or not callable(method_func):
                return False, f"Invalid HTTP method: {self._method}"
            # FIXED(安全): DNS Rebinding 防护 - 使用缓存的 IP 发送请求，避免 aiohttp 重新解析 DNS
            req_kwargs: dict = {
                "json": test_payload,
                "headers": headers,
                "timeout": aiohttp.ClientTimeout(total=10),
            }
            if server_hostname:
                req_kwargs["server_hostname"] = server_hostname
            async with method_func(request_url, **req_kwargs) as resp:
                if 200 <= resp.status < 300:
                    return True, "Webhook test successful"
                text = await resp.text()
                return False, f"HTTP {resp.status}: {text[:200]}"
        except Exception as e:
            return False, f"Connection failed: {str(e)}"


class NotificationManager:
    """Manages all notification channels and dispatches alarm notifications"""

    def __init__(self):
        self._channels: dict[str, NotificationChannel] = {}
        self._channel_configs: dict[str, dict] = {}
        self._escalation_timers: dict[str, asyncio.Task] = {}
        # Severity escalation thresholds (seconds)
        self._escalation_thresholds: dict[str, int] = {
            "critical": 300,   # 5 minutes
            "major": 900,      # 15 minutes
            "minor": 1800,     # 30 minutes
            "warning": 3600,   # 1 hour
        }

    def register_channel(self, channel_id: str, channel: NotificationChannel) -> None:
        """Register a notification channel"""
        # FIX-EL-SEVERE: 覆盖旧渠道前必须关闭其 ClientSession，否则 TCP 连接池/TLS 上下文泄漏。
        # 配置热重载时 init_notification_manager 会重新注册所有渠道，旧渠道 session 永不释放。
        old = self._channels.get(channel_id)
        if old is not None:
            try:
                asyncio.ensure_future(old.close())
                logger.info("Closed old channel session before re-register: %s", channel_id)
            except Exception as _close_err:
                logger.warning("Failed to close old channel %s: %s", channel_id, _close_err)
        self._channels[channel_id] = channel
        logger.info("Notification channel registered: %s (%s)", channel_id, channel.name)

    def unregister_channel(self, channel_id: str) -> None:
        """Unregister a notification channel"""
        if channel_id in self._channels:
            channel = self._channels.pop(channel_id)
            # 清理该渠道共享的 aiohttp.ClientSession，释放连接池资源
            asyncio.ensure_future(channel.close())
            logger.info("Notification channel unregistered: %s", channel_id)

    def get_channel(self, channel_id: str) -> NotificationChannel | None:
        """Get a notification channel by ID"""
        return self._channels.get(channel_id)

    def list_channels(self) -> list[dict]:
        """List all registered channels"""
        return [
            {
                "id": channel_id,
                "name": channel.name,
                "enabled": channel.is_enabled,
            }
            for channel_id, channel in self._channels.items()
        ]

    def set_channel_enabled(self, channel_id: str, enabled: bool) -> bool:
        """Enable or disable a notification channel"""
        channel = self._channels.get(channel_id)
        if channel:
            channel.set_enabled(enabled)
            logger.info("Notification channel %s %s", channel_id, "enabled" if enabled else "disabled")
            return True
        return False

    def set_escalation_threshold(self, severity: str, seconds: int) -> None:
        """Set escalation threshold for a severity level"""
        self._escalation_thresholds[severity] = seconds
        logger.info("Escalation threshold set: %s -> %ds", severity, seconds)

    def configure_channel(self, channel_id: str, config: dict) -> bool:
        """Update channel configuration"""
        self._channel_configs[channel_id] = config
        return True

    async def send_notification(
        self,
        notification: AlarmNotification,
        channel_ids: list[str] | None = None,
    ) -> dict[str, bool]:
        """Send notification to specified channels or all channels"""
        results = {}

        # Determine target channels
        if channel_ids:
            targets = {cid: self._channels[cid] for cid in channel_ids if cid in self._channels}
        else:
            # 创建副本，避免在 asyncio.wait 等待期间其他协程修改 _channels 导致迭代异常
            targets = dict(self._channels)

        # Send to each channel
        tasks = []
        channel_keys = []
        for channel_id, channel in targets.items():
            tasks.append(channel.notify(notification))
            channel_keys.append(channel_id)

        # R6-S-09: 为通知分发添加整体超时(15s)，防止慢渠道阻塞 gather
        # 使用 asyncio.wait 而非 asyncio.wait_for(gather(...)) ，以便超时后仍能保留已完成渠道的结果
        _GATHER_TIMEOUT = 15.0
        task_objs = [asyncio.ensure_future(t) for t in tasks]
        done, pending = await asyncio.wait(task_objs, timeout=_GATHER_TIMEOUT)

        # 取消超时未完成的任务，防止资源泄漏
        for t in pending:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t

        # 收集结果：已完成渠道取实际返回值，超时未完成渠道记为失败
        results_list = []
        for t in task_objs:
            if t in done:
                try:
                    results_list.append(t.result())
                except Exception as e:
                    results_list.append(e)
            else:
                # R6-S-09: 超时后未完成的渠道记为失败
                results_list.append(TimeoutError(f"channel send timed out after {_GATHER_TIMEOUT}s"))

        for channel_id, result in zip(channel_keys, results_list, strict=False):
            if isinstance(result, Exception):
                results[channel_id] = False
                logger.error("[%s] Notification failed with exception: %s", channel_id, result)
            else:
                results[channel_id] = result

        return results

    async def send_alarm_fired(
        self,
        alarm_id: str,
        rule_id: str,
        rule_name: str,
        device_id: str,
        device_name: str,
        severity: str,
        message: str,
        trigger_value: dict,
        channels: list[str] | None = None,
    ) -> dict[str, bool]:
        """Convenience method to send alarm firing notification"""
        notification = AlarmNotification(
            alarm_id=alarm_id,
            rule_id=rule_id,
            rule_name=rule_name,
            device_id=device_id,
            device_name=device_name,
            severity=severity,
            action="firing",
            message=message,
            trigger_value=trigger_value,
        )
        return await self.send_notification(notification, channels)

    async def send_alarm_recovered(
        self,
        alarm_id: str,
        rule_id: str,
        rule_name: str,
        device_id: str,
        device_name: str,
        severity: str,
        duration_seconds: float,
        channels: list[str] | None = None,
    ) -> dict[str, bool]:
        """Convenience method to send alarm recovery notification"""
        notification = AlarmNotification(
            alarm_id=alarm_id,
            rule_id=rule_id,
            rule_name=rule_name,
            device_id=device_id,
            device_name=device_name,
            severity=severity,
            action="recovered",
            message="",
            duration_seconds=duration_seconds,
        )
        return await self.send_notification(notification, channels)

    async def send_alarm_acknowledged(
        self,
        alarm_id: str,
        rule_id: str,
        rule_name: str,
        device_id: str,
        device_name: str,
        severity: str,
        ack_by: str,
        channels: list[str] | None = None,
    ) -> dict[str, bool]:
        """Convenience method to send alarm acknowledgment notification"""
        notification = AlarmNotification(
            alarm_id=alarm_id,
            rule_id=rule_id,
            rule_name=rule_name,
            device_id=device_id,
            device_name=device_name,
            severity=severity,
            action="acknowledged",
            message=f"Acknowledged by {ack_by}",
        )
        return await self.send_notification(notification, channels)

    async def schedule_escalation(
        self,
        alarm_id: str,
        notification: AlarmNotification,
        delay_seconds: int,
    ) -> None:
        """Schedule an alarm for escalation after delay"""
        # Cancel existing escalation for this alarm
        await self.cancel_escalation(alarm_id)

        async def _do_escalate():
            await asyncio.sleep(delay_seconds)
            await self._escalate_alarm(alarm_id, notification)

        task = asyncio.create_task(_do_escalate(), name=f"escalate-{alarm_id}")
        self._escalation_timers[alarm_id] = task
        logger.info("Escalation scheduled for %s in %ds", alarm_id, delay_seconds)

    async def cancel_escalation(self, alarm_id: str) -> None:
        """Cancel scheduled escalation for an alarm"""
        task = self._escalation_timers.pop(alarm_id, None)
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            logger.info("Escalation cancelled for %s", alarm_id)

    async def _escalate_alarm(self, alarm_id: str, notification: AlarmNotification) -> None:
        """Perform alarm escalation"""
        self._escalation_timers.pop(alarm_id, None)

        escalation_level = notification.escalation_level + 1

        # Determine escalated severity
        severity_order = ["info", "warning", "minor", "major", "critical"]
        # FIXED-P0: 原代码使用 severity_order.index(notification.severity) 可能抛出 ValueError
        # 虽然有 if ... in ... else 0 保护，但代码结构不清晰，改为更明确的处理
        current_index = severity_order.index(notification.severity) if notification.severity in severity_order else 0
        new_index = min(current_index + 1, len(severity_order) - 1)
        escalated_severity = severity_order[new_index]

        escalated_notification = AlarmNotification(
            alarm_id=notification.alarm_id,
            rule_id=notification.rule_id,
            rule_name=notification.rule_name,
            device_id=notification.device_id,
            device_name=notification.device_name,
            severity=escalated_severity,
            action="escalated",
            message=notification.message,
            trigger_value=notification.trigger_value,
            escalation_level=escalation_level,
            original_severity=notification.severity,
        )

        logger.warning(
            "Alarm escalated: %s (level %d, %s -> %s)",
            alarm_id, escalation_level, notification.severity, escalated_severity
        )

        await self.send_notification(escalated_notification)

        # Schedule next escalation if not at critical
        if escalated_severity != "critical":
            next_threshold = self._escalation_thresholds.get(escalated_severity, 3600)
            await self.schedule_escalation(alarm_id, escalated_notification, next_threshold)

    async def test_channel(self, channel_id: str) -> tuple[bool, str]:
        """Test a notification channel"""
        channel = self._channels.get(channel_id)
        if not channel:
            return False, f"Channel not found: {channel_id}"
        return await channel.test()

    async def close(self) -> None:
        """Cleanup notification manager"""
        # Cancel all escalation timers
        for task in self._escalation_timers.values():
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._escalation_timers.clear()
        # 关闭所有渠道共享的 aiohttp.ClientSession，释放连接池资源
        for channel in self._channels.values():
            with contextlib.suppress(Exception):
                await channel.close()
        logger.info("Notification manager closed")


# Global notification manager instance
_notification_manager: NotificationManager | None = None


def get_notification_manager() -> NotificationManager:
    """Get the global notification manager instance"""
    global _notification_manager
    if _notification_manager is None:
        _notification_manager = NotificationManager()
    return _notification_manager


async def init_notification_manager(config: dict) -> NotificationManager:
    """Initialize notification manager with configuration"""
    manager = get_notification_manager()

    # Load DingTalk channels
    dingtalk_configs = config.get("dingtalk", [])
    for i, cfg in enumerate(dingtalk_configs):
        if cfg.get("enabled"):
            channel_config = DingTalkConfig(
                enabled=True,
                name=cfg.get("name", f"DingTalk-{i+1}"),
                webhook_url=cfg.get("webhook_url", ""),
                secret=cfg.get("secret", ""),
                at_mobiles=cfg.get("at_mobiles", []),
                is_at_all=cfg.get("is_at_all", False),
                max_per_minute=cfg.get("max_per_minute", 10),
                cooldown_seconds=cfg.get("cooldown_seconds", 60.0),
                message_template=cfg.get("message_template", ""),
            )
            channel = DingTalkChannel(channel_config)
            manager.register_channel(f"dingtalk-{i}", channel)

    # Load WeCom channels
    wecom_configs = config.get("wecom", [])
    for i, cfg in enumerate(wecom_configs):
        if cfg.get("enabled"):
            channel_config = WeComConfig(
                enabled=True,
                name=cfg.get("name", f"WeCom-{i+1}"),
                webhook_url=cfg.get("webhook_url", ""),
                max_per_minute=cfg.get("max_per_minute", 10),
                cooldown_seconds=cfg.get("cooldown_seconds", 60.0),
                message_template=cfg.get("message_template", ""),
            )
            channel = WeComChannel(channel_config)
            manager.register_channel(f"wecom-{i}", channel)

    # Load Email channels
    email_configs = config.get("email", [])
    for i, cfg in enumerate(email_configs):
        if cfg.get("enabled"):
            channel_config = EmailConfig(
                enabled=True,
                name=cfg.get("name", f"Email-{i+1}"),
                smtp_host=cfg.get("smtp_host", "localhost"),
                smtp_port=cfg.get("smtp_port", 587),
                smtp_user=cfg.get("smtp_user", ""),
                smtp_password=cfg.get("smtp_password", ""),
                from_address=cfg.get("from_address", ""),
                to_addresses=cfg.get("to_addresses", []),
                use_tls=cfg.get("use_tls", True),
                use_ssl=cfg.get("use_ssl", False),
                max_per_minute=cfg.get("max_per_minute", 10),
                cooldown_seconds=cfg.get("cooldown_seconds", 60.0),
                message_template=cfg.get("message_template", ""),
            )
            channel = EmailChannel(channel_config)
            manager.register_channel(f"email-{i}", channel)

    # Load Webhook channels
    webhook_configs = config.get("webhook", [])
    for i, cfg in enumerate(webhook_configs):
        if cfg.get("enabled"):
            channel_config = WebhookConfig(
                enabled=True,
                name=cfg.get("name", f"Webhook-{i+1}"),
                url=cfg.get("url", ""),
                method=cfg.get("method", "POST"),
                headers=cfg.get("headers", {}),
                auth_type=cfg.get("auth_type", "none"),
                auth_token=cfg.get("auth_token", ""),
                auth_username=cfg.get("auth_username", ""),
                auth_password=cfg.get("auth_password", ""),
                max_per_minute=cfg.get("max_per_minute", 10),
                cooldown_seconds=cfg.get("cooldown_seconds", 60.0),
                retry_count=cfg.get("retry_count", 3),
                retry_delay=cfg.get("retry_delay", 1.0),
                message_template=cfg.get("message_template", ""),
            )
            channel = WebhookChannel(channel_config)
            manager.register_channel(f"webhook-{i}", channel)

    # Load escalation thresholds
    escalation_config = config.get("escalation", {})
    for severity, seconds in escalation_config.items():
        manager.set_escalation_threshold(severity, seconds)

    logger.info(
        "Notification manager initialized: %d channels",
        len(manager.list_channels()),
    )

    return manager
