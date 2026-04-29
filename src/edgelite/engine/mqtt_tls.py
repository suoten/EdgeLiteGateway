"""MQTT TLS/SSL 配置模块"""

from __future__ import annotations
import logging
import ssl
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class MqttTlsHelper:
    """MQTT TLS配置辅助类"""

    @staticmethod
    def create_ssl_context(
        ca_cert: str = "",
        client_cert: str = "",
        client_key: str = "",
        cert_reqs: str = "required",
    ) -> Optional[ssl.SSLContext]:
        """构建SSL上下文"""
        if not ca_cert and not client_cert:
            return None

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

        if cert_reqs == "none":
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
        if p.stat().st_size == 0:
            logger.error("证书文件为空: %s", path)
            return False
        return True
