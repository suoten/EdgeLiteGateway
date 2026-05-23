"""告警通知业务逻辑"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import json
import logging
import smtplib
import urllib.parse
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

try:
    import httpx
except ImportError:
    httpx = None

from edgelite.config import get_config
from edgelite.constants import _NOTIFY_HTTP_TIMEOUT, _NOTIFY_SMTP_TIMEOUT  # FIXED: 原问题-散落timeout魔法数字

logger = logging.getLogger(__name__)


class NotifyService:
    """告警通知服务"""

    def __init__(self):
        self._http_client = httpx.AsyncClient(timeout=_NOTIFY_HTTP_TIMEOUT) if httpx else None  # FIXED: 原问题-散落timeout魔法数字

    async def close(self) -> None:
        if self._http_client:
            await self._http_client.aclose()

    async def send_notification(
        self, channels: list[str], alarm_data: dict, retry_count: int = 0
    ) -> dict[str, bool]:
        """根据通知渠道分发通知，返回各渠道发送结果"""
        results = {}
        for channel in channels:
            try:
                if channel == "dingtalk":
                    results[channel] = await self._send_dingtalk(alarm_data)
                elif channel == "email":
                    results[channel] = await self._send_email(alarm_data)
                elif channel == "wechat":
                    results[channel] = await self._send_wechat(alarm_data)
                elif channel == "webhook":
                    results[channel] = await self._send_webhook(alarm_data)
                else:
                    results[channel] = False
                    logger.warning("Unknown notification channel: %s", channel)  # FIXED-P3: 中文日志→英文
            except Exception as e:
                results[channel] = False
                logger.error("Notification send failed: %s - %s", channel, e)  # FIXED-P3: 中文日志→英文

        return results

    # ─── 钉钉 ───

    async def _send_dingtalk(self, alarm_data: dict) -> bool:
        """钉钉Webhook通知（支持加签）"""
        config = get_config()
        dt = config.notify.dingtalk
        if not dt.webhook_url:
            logger.debug("DingTalk webhook not configured, skipping")  # FIXED-P3: 中文日志→英文
            return True

        severity_emoji = {"critical": "🔴", "warning": "🟡", "info": "🟢"}
        emoji = severity_emoji.get(alarm_data.get("severity", ""), "⚪")

        message = {
            "msgtype": "markdown",
            "markdown": {
                "title": f"{emoji} EdgeLite Alert",  # FIXED-P3: 中文标题→英文
                "text": f"### {emoji} EdgeLite Alert\n\n"  # FIXED-P3: 中文标题→英文
                f"- **Device**: {alarm_data.get('device_id', '')}\n"  # FIXED-P3: 中文标签→英文
                f"- **Rule**: {alarm_data.get('rule_id', '')}\n"
                f"- **Severity**: {alarm_data.get('severity', '')}\n"
                f"- **Status**: {alarm_data.get('status', '')}\n"
                f"- **Trigger Value**: "  # FIXED-P3: 中文标签→英文
                f"{json.dumps(alarm_data.get('trigger_value', {}), ensure_ascii=False)}\n"
                f"- **Time**: {alarm_data.get('fired_at', '')}\n",  # FIXED-P3: 中文标签→英文
            },
        }

        url = dt.webhook_url
        # 加签模式
        if dt.secret:
            timestamp = str(timestamp_ms())  # FIXED: 原问题-直接调用int(time.time()*1000)，未使用统一工具函数
            string_to_sign = f"{timestamp}\n{dt.secret}"
            hmac_code = hmac.new(
                dt.secret.encode("utf-8"),
                string_to_sign.encode("utf-8"),
                digestmod=hashlib.sha256,
            ).digest()
            sign = urllib.parse.quote_plus(base64.b64encode(hmac_code).decode("utf-8"))
            url = f"{url}&timestamp={timestamp}&sign={sign}"

        try:
            if not self._http_client:
                logger.error("httpx not installed, cannot send DingTalk notification")  # FIXED-P3: 中文日志→英文
                return False
            resp = await self._http_client.post(url, json=message)
            if resp.status_code != 200:
                return False
            body = resp.json()
            return body.get("errcode", 0) == 0  # FIXED-P2: 钉钉API返回200但body中errcode可能非0(如限流)，需检查errcode
        except Exception as e:
            logger.error("DingTalk notification send failed: %s", e)  # FIXED-P3: 中文日志→英文
            return False

    # ─── 邮件 ───

    async def _send_email(self, alarm_data: dict) -> bool:
        """邮件通知（SMTP）"""
        config = get_config()
        email = config.notify.email
        if not email.smtp_host or not email.to_addrs:
            logger.debug("Email notification not configured, skipping")  # FIXED-P3: 中文日志→英文
            return True

        severity_label = {"critical": "Critical", "warning": "Warning", "info": "Info"}  # FIXED-P3: 中文→英文
        severity = alarm_data.get("severity", "")
        label = severity_label.get(severity, severity)

        subject = f"[EdgeLite Alert] {label} - Device {html.escape(alarm_data.get('device_id', ''))}"  # FIXED-P3: 中文→英文
        body_html = f"""
        <html><body>
        <h2 style="color: {
                    "red" if severity == "critical"
                    else "orange" if severity == "warning"
                    else "green"
                }">
            EdgeLite Alert Notification  <!-- FIXED-P3: 中文→英文 -->
        </h2>
        <table border="1" cellpadding="8" cellspacing="0" style="border-collapse:collapse">
            <tr><td><b>Device ID</b></td>  <!-- FIXED-P3: 中文→英文 -->
            <td>{html.escape(str(alarm_data.get("device_id", "")))}</td></tr>
            <tr><td><b>Rule ID</b></td>  <!-- FIXED-P3: 中文→英文 -->
            <td>{html.escape(str(alarm_data.get("rule_id", "")))}</td></tr>
            <tr><td><b>Severity</b></td><td>{html.escape(label)}</td></tr>  <!-- FIXED-P3: 中文→英文 -->
            <tr><td><b>Status</b></td>  <!-- FIXED-P3: 中文→英文 -->
            <td>{html.escape(str(alarm_data.get("status", "")))}</td></tr>
            <tr><td><b>Trigger Value</b></td>  <!-- FIXED-P3: 中文→英文 -->
            <td>{html.escape(json.dumps(
                alarm_data.get("trigger_value", {}), ensure_ascii=False
            ))}</td></tr>
            <tr><td><b>Trigger Count</b></td>  <!-- FIXED-P3: 中文→英文 -->
            <td>{alarm_data.get("trigger_count", 1)}</td></tr>
            <tr><td><b>Fired At</b></td>  <!-- FIXED-P3: 中文→英文 -->
            <td>{html.escape(str(alarm_data.get("fired_at", "")))}</td></tr>
        </table>
        </body></html>
        """

        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = email.from_addr or email.smtp_user
            msg["To"] = ", ".join(email.to_addrs)
            msg.attach(MIMEText(body_html, "html", "utf-8"))

            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._smtp_send, email, msg)

            logger.info("Email notification sent: %s -> %s", subject, email.to_addrs)  # FIXED-P3: 中文日志→英文
            return True
        except Exception as e:
            logger.error("Email notification send failed: %s", e)  # FIXED-P3: 中文日志→英文
            return False

    @staticmethod
    def _smtp_send(email_config: Any, msg: MIMEMultipart) -> None:
        """同步SMTP发送"""
        if email_config.use_tls:
            server = smtplib.SMTP_SSL(email_config.smtp_host, email_config.smtp_port, timeout=_NOTIFY_SMTP_TIMEOUT)  # FIXED: 原问题-散落timeout魔法数字
        else:
            server = smtplib.SMTP(email_config.smtp_host, email_config.smtp_port, timeout=_NOTIFY_SMTP_TIMEOUT)  # FIXED: 原问题-散落timeout魔法数字
            if getattr(email_config, "use_starttls", False):
                server.starttls()

        try:
            if email_config.smtp_user and email_config.smtp_password:
                server.login(email_config.smtp_user, email_config.smtp_password)
            server.send_message(msg)
        finally:
            try:
                server.quit()
            except Exception as e:
                logger.debug("SMTP quit failed: %s", e)  # FIXED-P3: 中文日志→英文

    # ─── 企业微信 ───

    async def _send_wechat(self, alarm_data: dict) -> bool:
        """企业微信Webhook通知"""
        config = get_config()
        wechat = config.notify.wechat
        if not wechat.webhook_url:
            logger.debug("WeChat webhook not configured, skipping")  # FIXED-P3: 中文日志→英文
            return True

        severity_label = {"critical": "Critical", "warning": "Warning", "info": "Info"}  # FIXED-P3: 中文→英文
        severity = alarm_data.get("severity", "")
        label = severity_label.get(severity, severity)

        message = {
            "msgtype": "markdown",
            "markdown": {
                "content": f"### EdgeLite Alert\n"  # FIXED-P3: 中文→英文
                f'> **Severity**: <font color="'  # FIXED-P3: 中文→英文
                f'{"warning" if severity == "critical" else "info"}'
                f'">{label}</font>\n'
                f"> **Device**: {alarm_data.get('device_id', '')}\n"  # FIXED-P3: 中文→英文
                f"> **Rule**: {alarm_data.get('rule_id', '')}\n"
                f"> **Status**: {alarm_data.get('status', '')}\n"
                f"> **Trigger Value**: "  # FIXED-P3: 中文→英文
                f"{json.dumps(alarm_data.get('trigger_value', {}), ensure_ascii=False)}\n"
                f"> **Time**: {alarm_data.get('fired_at', '')}\n",  # FIXED-P3: 中文→英文
            },
        }

        try:
            if not self._http_client:
                logger.error("httpx not installed, cannot send WeChat notification")  # FIXED-P3: 中文日志→英文
                return False
            resp = await self._http_client.post(wechat.webhook_url, json=message)
            return resp.status_code == 200
        except Exception as e:
            logger.error("WeChat notification send failed: %s", e)  # FIXED-P3: 中文日志→英文
            return False

    # ─── 自定义Webhook ───

    async def _send_webhook(self, alarm_data: dict) -> bool:
        """自定义Webhook通知"""
        config = get_config()
        wh = config.notify.webhook
        if not wh.url:
            logger.debug("Webhook not configured, skipping")  # FIXED-P3: 中文日志→英文
            return True

        try:
            headers = {"Content-Type": "application/json"}
            if wh.headers:
                headers.update(wh.headers)
            if not self._http_client:
                logger.error("httpx not installed, cannot send Webhook notification")  # FIXED-P3: 中文日志→英文
                return False
            resp = await self._http_client.post(wh.url, json=alarm_data, headers=headers)
            return resp.status_code < 400
        except Exception as e:
            logger.error("Webhook notification send failed: %s", e)  # FIXED-P3: 中文日志→英文
            return False
