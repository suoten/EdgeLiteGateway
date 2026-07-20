"""告警通知业务逻辑"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import ipaddress
import json
import logging
import smtplib
import socket
import threading
import urllib.parse
from collections import OrderedDict
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]

from edgelite.config import get_config
from edgelite.constants import (  # FIXED: 原问题-散落timeout魔法数字
    _NOTIFY_HTTP_TIMEOUT,
    _NOTIFY_SMTP_TIMEOUT,
)
from edgelite.utils import timestamp_ms

logger = logging.getLogger(__name__)


def _sanitize_email_header(value: str) -> str:
    """FIXED(严重): 过滤邮件头中的 CRLF 字符，防止 CRLF 注入。

    html.escape() 不会转义 \\r\\n，攻击者可通过 device_id 等用户可控输入
    向 Subject/From/To 注入额外的邮件头（如 Bcc），导致邮件头注入。
    此函数剥离 \\r 和 \\n，确保头值单行。
    """
    if not isinstance(value, str):
        return value
    return value.replace("\r", "").replace("\n", "")


# FIXED(高危): DNS Rebinding 防护 - 校验时解析域名IP并缓存，发送请求时使用缓存的IP
_webhook_ip_cache: OrderedDict[str, str] = OrderedDict()  # hostname -> resolved IP
_webhook_ip_cache_lock = threading.Lock()
_WEBHOOK_IP_CACHE_MAXLEN = 100  # 修复资源泄漏：LRU淘汰上限，防止无界增长

# FIXED(高危): 扩展域名黑名单 - 覆盖主流云元数据服务地址
_BLOCKED_WEBHOOK_HOSTNAMES = frozenset(
    {
        "localhost",
        "metadata.google.internal",  # GCP 元数据服务
        "metadata",  # GCP 短名
        "metadata.azure.com",  # Azure 元数据服务
        "169.254.169.254",  # AWS/GCP/Azure 元数据 IP
        "169.254.170.2",  # AWS ECS 任务元数据
        "169.254.169.253",  # 阿里云元数据服务
    }
)


def _is_ip_safe_for_webhook(ip: Any) -> bool:
    """检查 IP 地址是否安全（非内网/回环/链路本地/未指定/保留/组播）。

    FIXED(高危): 原问题-0.0.0.0未拦截、is_reserved未检查、is_multicast未检查;
    修复-增加 is_unspecified/is_reserved/is_multicast 检查。
    """
    if ip.is_private or ip.is_loopback or ip.is_link_local:
        return False
    # 0.0.0.0/:: (is_unspecified)、保留地址、组播地址均禁止
    return not (ip.is_unspecified or ip.is_reserved or ip.is_multicast)


async def _validate_webhook_url(url: str) -> bool:
    """校验 webhook URL，防止 SSRF。

    FIXED(高危): 原问题-_validate_webhook_url存在 DNS Rebinding（校验后请求时重新解析DNS）、
    0.0.0.0未拦截、is_reserved未检查、域名黑名单不完整;
    修复-增加 is_unspecified/is_reserved/is_multicast 检查，扩展域名黑名单，
    校验通过后解析域名为IP并缓存到 _webhook_ip_cache，发送请求时使用该IP消除 DNS Rebinding。
    FIX-P1: 原 socket.getaddrinfo 为阻塞式 DNS 解析，在 async 上下文中直接调用
    会阻塞事件循环。改为 async 函数并通过 asyncio.to_thread 在工作线程中执行。
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    hostname = parsed.hostname
    if not hostname:
        return False

    # 域名/IP 黑名单检查
    if hostname in _BLOCKED_WEBHOOK_HOSTNAMES:
        return False

    # IP 地址校验
    try:
        ip = ipaddress.ip_address(hostname)
        if not _is_ip_safe_for_webhook(ip):
            return False
        # IP 地址直接缓存为自身（消除 DNS Rebinding）
        with _webhook_ip_cache_lock:
            _webhook_ip_cache[hostname] = hostname
            _webhook_ip_cache.move_to_end(hostname)  # 标记为最近使用
            while len(_webhook_ip_cache) > _WEBHOOK_IP_CACHE_MAXLEN:
                _webhook_ip_cache.popitem(last=False)  # 淘汰最久未使用
        return True
    except ValueError:
        pass  # 域名，继续解析

    # 域名解析为 IP（消除 DNS Rebinding：校验时解析，请求时使用缓存的 IP）
    # FIX-P1: socket.getaddrinfo 为阻塞式 DNS 解析，改用 asyncio.to_thread
    # 在工作线程中执行，避免在 async 上下文中阻塞事件循环。
    try:
        addrs = await asyncio.to_thread(socket.getaddrinfo, hostname, None)
    except socket.gaierror:
        return False

    if not addrs:
        return False

    # 校验所有解析到的 IP 地址，并优先选取 IPv4 作为缓存值
    resolved_ip: str | None = None
    for _family, _, _, _, sockaddr in addrs:
        ip_str = str(sockaddr[0])
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if not _is_ip_safe_for_webhook(ip):
            return False
        if resolved_ip is None and ip.version == 4:
            resolved_ip = ip_str

    if resolved_ip is None:
        # 无 IPv4 地址时回退到第一个可用地址
        resolved_ip = str(addrs[0][4][0])

    # 缓存 hostname -> resolved_ip，发送请求时使用
    with _webhook_ip_cache_lock:
        _webhook_ip_cache[hostname] = resolved_ip
        _webhook_ip_cache.move_to_end(hostname)  # 标记为最近使用
        while len(_webhook_ip_cache) > _WEBHOOK_IP_CACHE_MAXLEN:
            _webhook_ip_cache.popitem(last=False)  # 淘汰最久未使用
    return True


class NotifyService:
    """告警通知服务"""

    def __init__(self):
        self._http_client = (
            httpx.AsyncClient(timeout=_NOTIFY_HTTP_TIMEOUT) if httpx else None
        )  # FIXED: 原问题-散落timeout魔法数字

    async def close(self) -> None:
        # FIXED-P2: 原问题-close后未置空_http_client，重复调用close会操作已关闭的client
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

    async def send_notification(self, channels: list[str], alarm_data: dict, retry_count: int = 0) -> dict[str, bool]:
        """根据通知渠道并行分发通知，返回各渠道发送结果"""
        channel_map = {
            "dingtalk": self._send_dingtalk,
            "email": self._send_email,
            "wechat": self._send_wechat,
            "webhook": self._send_webhook,
        }

        async def _send_one(channel: str) -> tuple[str, bool]:
            handler = channel_map.get(channel)
            if handler is None:
                logger.warning("Unknown notification channel: %s", channel)
                return (channel, False)
            # FIXED-P1: 原问题-retry_count参数被忽略，失败后直接返回False无重试
            # 改为按retry_count重试，指数退避（1s, 2s, 4s... 上限10s）
            # FIXED-BugR15: handler 内部 try/except 返回 False 而非抛异常，原重试逻辑永远不触发
            # 改为对 handler 返回 False 也触发重试
            for attempt in range(retry_count + 1):
                last_error: Exception | None = None
                try:
                    success = await handler(alarm_data)
                except Exception as e:
                    success = False
                    last_error = e
                if success:
                    return (channel, True)
                # handler 返回 False 或抛出异常，均触发重试
                if attempt < retry_count:
                    logger.warning(
                        "Notification %s attempt %d/%d failed: %s, retrying...",
                        channel,
                        attempt + 1,
                        retry_count + 1,
                        last_error or "handler returned False",
                    )
                    await asyncio.sleep(min(2**attempt, 10))
                else:
                    logger.error(
                        "Notification send failed after %d attempts: %s - %s",
                        retry_count + 1,
                        channel,
                        last_error or "handler returned False",
                    )
            return (channel, False)

        results_list = await asyncio.gather(*[_send_one(ch) for ch in channels])
        return dict(results_list)

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
        # FIXED(安全): SSRF 防护 - 钉钉 webhook 仅允许官方域名
        from urllib.parse import urlparse

        _parsed = urlparse(url)
        if _parsed.hostname not in ("oapi.dingtalk.com",):
            logger.warning("DingTalk webhook URL host not allowed: %s", _parsed.hostname)
            return False
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

        subject = (
            f"[EdgeLite Alert] {label} - Device {html.escape(alarm_data.get('device_id', ''))}"  # FIXED-P3: 中文→英文
        )
        body_html = f"""
        <html><body>
        <h2 style="color: {"red" if severity == "critical" else "orange" if severity == "warning" else "green"}">
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
            <td>{html.escape(json.dumps(alarm_data.get("trigger_value", {}), ensure_ascii=False))}</td></tr>
            <tr><td><b>Trigger Count</b></td>  <!-- FIXED-P3: 中文→英文 -->
            <td>{alarm_data.get("trigger_count", 1)}</td></tr>
            <tr><td><b>Fired At</b></td>  <!-- FIXED-P3: 中文→英文 -->
            <td>{html.escape(str(alarm_data.get("fired_at", "")))}</td></tr>
        </table>
        </body></html>
        """

        try:
            msg = MIMEMultipart("alternative")
            # FIXED(严重): 对所有拼接到邮件头的用户可控输入过滤 CRLF，防止邮件头注入
            # alarm_data.get('device_id') 等用户可控值经 html.escape 后仍可能含 CRLF
            msg["Subject"] = _sanitize_email_header(subject)
            msg["From"] = _sanitize_email_header(email.from_addr or email.smtp_user)
            msg["To"] = _sanitize_email_header(", ".join(email.to_addrs))
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
        server: smtplib.SMTP
        if email_config.use_tls:
            server = smtplib.SMTP_SSL(
                email_config.smtp_host, email_config.smtp_port, timeout=_NOTIFY_SMTP_TIMEOUT
            )  # FIXED: 原问题-散落timeout魔法数字
        else:
            server = smtplib.SMTP(
                email_config.smtp_host, email_config.smtp_port, timeout=_NOTIFY_SMTP_TIMEOUT
            )  # FIXED: 原问题-散落timeout魔法数字
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
                f"{'warning' if severity == 'critical' else 'info'}"
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
            # FIXED(安全): SSRF 防护 - 企业微信 webhook 仅允许官方域名
            from urllib.parse import urlparse as _urlparse

            _w_parsed = _urlparse(wechat.webhook_url)
            if _w_parsed.hostname not in ("qyapi.weixin.qq.com",):
                logger.warning("WeChat webhook URL host not allowed: %s", _w_parsed.hostname)
                return False
            resp = await self._http_client.post(wechat.webhook_url, json=message)
            if resp.status_code != 200:
                return False
            # FIXED-P1: 原问题-企业微信API返回200但body中errcode可能非0(如限流/URL失效)，需检查errcode
            body = resp.json()
            return body.get("errcode", 0) == 0
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

        # FIXED(严重): 原问题-自定义webhook URL无SSRF防护;
        # 修复-校验URL，禁止内网/回环/元数据服务地址，校验失败记录warning并跳过
        if not await _validate_webhook_url(wh.url):
            logger.warning("Webhook URL rejected by SSRF protection, skipping: %s", wh.url)
            return False

        try:
            headers = {"Content-Type": "application/json"}
            if wh.headers:
                headers.update(wh.headers)
            if not self._http_client:
                logger.error("httpx not installed, cannot send Webhook notification")  # FIXED-P3: 中文日志→英文
                return False

            # FIXED(高危): DNS Rebinding 防护 - 使用校验时缓存的 IP 发送请求
            # 用解析后的 IP 替换 URL 中的 hostname，Host 头保持原域名
            # HTTPS 通过 sni_hostname 扩展保持原域名用于 SNI 和证书验证
            parsed = urllib.parse.urlparse(wh.url)
            hostname = parsed.hostname or ""
            with _webhook_ip_cache_lock:
                resolved_ip = _webhook_ip_cache.get(hostname)

            request_url = wh.url
            request_kwargs: dict[str, Any] = {"json": alarm_data, "headers": headers}
            if resolved_ip and resolved_ip != hostname:
                if ":" in resolved_ip:  # IPv6
                    new_netloc = f"[{resolved_ip}]:{parsed.port}" if parsed.port else f"[{resolved_ip}]"
                else:
                    new_netloc = f"{resolved_ip}:{parsed.port}" if parsed.port else resolved_ip
                request_url = urllib.parse.urlunparse(
                    (
                        parsed.scheme,
                        new_netloc,
                        parsed.path or "/",
                        parsed.params,
                        parsed.query,
                        parsed.fragment,
                    )
                )
                headers["Host"] = hostname
                if parsed.scheme == "https":
                    # 通过 sni_hostname 扩展保持原域名用于 TLS SNI 和证书验证
                    request_kwargs["extensions"] = {"sni_hostname": hostname.encode("ascii", errors="ignore")}

            resp = await self._http_client.post(request_url, **request_kwargs)
            return resp.status_code < 400
        except Exception as e:
            logger.error("Webhook notification send failed: %s", e)  # FIXED-P3: 中文日志→英文
            return False
