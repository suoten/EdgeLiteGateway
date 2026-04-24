"""告警通知业务逻辑"""

from __future__ import annotations

import hashlib
import hmac
import base64
import json
import logging
import smtplib
import time
import urllib.parse
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

import httpx

from edgelite.config import get_config

logger = logging.getLogger(__name__)


class NotifyService:
    """告警通知服务"""

    def __init__(self):
        self._http_client = httpx.AsyncClient(timeout=10.0)

    async def close(self) -> None:
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
                    logger.warning("未知通知渠道: %s", channel)
            except Exception as e:
                results[channel] = False
                logger.error("通知发送失败: %s - %s", channel, e)

        return results

    # ─── 钉钉 ───

    async def _send_dingtalk(self, alarm_data: dict) -> bool:
        """钉钉Webhook通知（支持加签）"""
        config = get_config()
        dt = config.notify.dingtalk
        if not dt.webhook_url:
            logger.debug("钉钉Webhook未配置，跳过")
            return True

        severity_emoji = {"critical": "🔴", "warning": "🟡", "info": "🟢"}
        emoji = severity_emoji.get(alarm_data.get("severity", ""), "⚪")

        message = {
            "msgtype": "markdown",
            "markdown": {
                "title": f"{emoji} EdgeLite告警",
                "text": f"### {emoji} EdgeLite告警\n\n"
                f"- **设备**: {alarm_data.get('device_id', '')}\n"
                f"- **规则**: {alarm_data.get('rule_id', '')}\n"
                f"- **级别**: {alarm_data.get('severity', '')}\n"
                f"- **状态**: {alarm_data.get('status', '')}\n"
                f"- **触发值**: {json.dumps(alarm_data.get('trigger_value', {}), ensure_ascii=False)}\n"
                f"- **时间**: {alarm_data.get('fired_at', '')}\n",
            },
        }

        url = dt.webhook_url
        # 加签模式
        if dt.secret:
            timestamp = str(int(time.time() * 1000))
            string_to_sign = f"{timestamp}\n{dt.secret}"
            hmac_code = hmac.new(
                dt.secret.encode("utf-8"),
                string_to_sign.encode("utf-8"),
                digestmod=hashlib.sha256,
            ).digest()
            sign = urllib.parse.quote_plus(base64.b64encode(hmac_code).decode("utf-8"))
            url = f"{url}&timestamp={timestamp}&sign={sign}"

        try:
            resp = await self._http_client.post(url, json=message)
            return resp.status_code == 200
        except Exception as e:
            logger.error("钉钉通知发送失败: %s", e)
            return False

    # ─── 邮件 ───

    async def _send_email(self, alarm_data: dict) -> bool:
        """邮件通知（SMTP）"""
        config = get_config()
        email = config.notify.email
        if not email.smtp_host or not email.to_addrs:
            logger.debug("邮件通知未配置，跳过")
            return True

        severity_label = {"critical": "严重", "warning": "警告", "info": "信息"}
        severity = alarm_data.get("severity", "")
        label = severity_label.get(severity, severity)

        subject = f"[EdgeLite告警] {label} - 设备 {alarm_data.get('device_id', '')}"
        body_html = f"""
        <html><body>
        <h2 style="color: {'red' if severity == 'critical' else 'orange' if severity == 'warning' else 'green'}">
            EdgeLite告警通知
        </h2>
        <table border="1" cellpadding="8" cellspacing="0" style="border-collapse:collapse">
            <tr><td><b>设备ID</b></td><td>{alarm_data.get('device_id', '')}</td></tr>
            <tr><td><b>规则ID</b></td><td>{alarm_data.get('rule_id', '')}</td></tr>
            <tr><td><b>告警级别</b></td><td>{label}</td></tr>
            <tr><td><b>告警状态</b></td><td>{alarm_data.get('status', '')}</td></tr>
            <tr><td><b>触发值</b></td><td>{json.dumps(alarm_data.get('trigger_value', {}), ensure_ascii=False)}</td></tr>
            <tr><td><b>触发次数</b></td><td>{alarm_data.get('trigger_count', 1)}</td></tr>
            <tr><td><b>触发时间</b></td><td>{alarm_data.get('fired_at', '')}</td></tr>
        </table>
        </body></html>
        """

        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = email.from_addr or email.smtp_user
            msg["To"] = ", ".join(email.to_addrs)
            msg.attach(MIMEText(body_html, "html", "utf-8"))

            # 在线程池中执行同步SMTP操作
            import asyncio
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._smtp_send, email, msg)

            logger.info("邮件通知已发送: %s -> %s", subject, email.to_addrs)
            return True
        except Exception as e:
            logger.error("邮件通知发送失败: %s", e)
            return False

    @staticmethod
    def _smtp_send(email_config: Any, msg: MIMEMultipart) -> None:
        """同步SMTP发送"""
        if email_config.use_tls:
            server = smtplib.SMTP_SSL(email_config.smtp_host, email_config.smtp_port, timeout=15)
        else:
            server = smtplib.SMTP(email_config.smtp_host, email_config.smtp_port, timeout=15)
            server.starttls()

        try:
            if email_config.smtp_user and email_config.smtp_password:
                server.login(email_config.smtp_user, email_config.smtp_password)
            server.send_message(msg)
        finally:
            server.quit()

    # ─── 企业微信 ───

    async def _send_wechat(self, alarm_data: dict) -> bool:
        """企业微信Webhook通知"""
        config = get_config()
        wechat = config.notify.wechat
        if not wechat.webhook_url:
            logger.debug("企业微信Webhook未配置，跳过")
            return True

        severity_label = {"critical": "严重", "warning": "警告", "info": "信息"}
        severity = alarm_data.get("severity", "")
        label = severity_label.get(severity, severity)

        message = {
            "msgtype": "markdown",
            "markdown": {
                "content": f"### EdgeLite告警\n"
                f"> **级别**: <font color=\"{'warning' if severity == 'critical' else 'info'}\">{label}</font>\n"
                f"> **设备**: {alarm_data.get('device_id', '')}\n"
                f"> **规则**: {alarm_data.get('rule_id', '')}\n"
                f"> **状态**: {alarm_data.get('status', '')}\n"
                f"> **触发值**: {json.dumps(alarm_data.get('trigger_value', {}), ensure_ascii=False)}\n"
                f"> **时间**: {alarm_data.get('fired_at', '')}\n",
            },
        }

        try:
            resp = await self._http_client.post(wechat.webhook_url, json=message)
            return resp.status_code == 200
        except Exception as e:
            logger.error("企业微信通知发送失败: %s", e)
            return False

    # ─── 自定义Webhook ───

    async def _send_webhook(self, alarm_data: dict) -> bool:
        """自定义Webhook通知"""
        config = get_config()
        wh = config.notify.webhook
        if not wh.url:
            logger.debug("Webhook未配置，跳过")
            return True

        try:
            headers = {"Content-Type": "application/json"}
            if wh.headers:
                headers.update(wh.headers)
            resp = await self._http_client.post(wh.url, json=alarm_data, headers=headers)
            return resp.status_code < 400
        except Exception as e:
            logger.error("Webhook通知发送失败: %s", e)
            return False
