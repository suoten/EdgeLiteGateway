"""MQTT TLS/SSL 配置模块"""

from __future__ import annotations

import logging
import ssl
from pathlib import Path

logger = logging.getLogger(__name__)


class MqttTlsHelper:
    """MQTT TLS配置辅助类"""

    @staticmethod
    def create_ssl_context(
        ca_cert: str = "",
        client_cert: str = "",
        client_key: str = "",
        cert_reqs: str = "required",
    ) -> ssl.SSLContext | None:
        """构建SSL上下文"""
        if not ca_cert and not client_cert:
            return None

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2  # FIXED-P1: 强制最低TLS 1.2，禁用不安全的TLS 1.0/1.1

        if cert_reqs == "none":
            import os

            if (
                os.getenv("EDGELITE_ALLOW_INSECURE_TLS") != "1"
            ):  # FIXED-P2: cert_reqs=none默认禁止，需显式设置环境变量允许，防止生产环境误配置中间人攻击
                raise ValueError(
                    "MQTT TLS: cert_reqs=none is prohibited by default. Set EDGELITE_ALLOW_INSECURE_TLS=1 to override (NOT recommended for production)"  # noqa: E501
                )
            logger.warning("MQTT TLS: cert_reqs=none 完全禁用证书验证，存在中间人攻击风险")
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        elif cert_reqs == "optional":
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_OPTIONAL
        else:
            ctx.check_hostname = True
            ctx.verify_mode = ssl.CERT_REQUIRED

        if ca_cert:
            if MqttTlsHelper.validate_cert_file(ca_cert):
                ctx.load_verify_locations(ca_cert)
            else:
                raise ValueError(f"CA证书文件无效: {ca_cert}")

        if client_cert and client_key:
            if MqttTlsHelper.validate_cert_file(client_cert) and MqttTlsHelper.validate_cert_file(client_key):
                ctx.load_cert_chain(client_cert, client_key)
            else:
                raise ValueError(f"客户端证书/密钥文件无效: {client_cert} / {client_key}")

        return ctx

    @staticmethod
    def validate_cert_file(path: str) -> bool:
        """校验证书文件存在性"""
        if not path:
            return False
        p = Path(path)
        if not p.is_file():
            logger.error("证书文件不存在: %s", path)
            return False
        try:  # FIXED: 原问题-p.stat()存在TOCTOU竞态，文件可能在is_file()和stat()之间被删除
            if p.stat().st_size == 0:
                logger.error("证书文件为空: %s", path)
                return False
        except FileNotFoundError:
            logger.error("证书文件不存在: %s", path)
            return False
        return True
