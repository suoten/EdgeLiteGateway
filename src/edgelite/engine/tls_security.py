"""TLS/SSL安全配置模块 - 双向认证和证书管理

TLS安全配置提供：
- MQTT TLS双向认证
- HTTP/TLS配置
- OPC UA证书管理
- 设备证书存储和轮换
- 证书自动生成 (自签名CA/设备证书)
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import ssl
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class CertManager:
    """证书管理器 - 管理TLS证书和密钥"""

    def __init__(self, cert_dir: str = "data/certs"):
        self._cert_dir = Path(cert_dir)
        self._cert_dir.mkdir(parents=True, exist_ok=True)
        self._certs: dict[str, dict] = {}

    def get_ca_cert_path(self) -> Path:
        """获取CA证书路径"""
        return self._cert_dir / "ca.crt"

    def get_ca_key_path(self) -> Path:
        """获取CA私钥路径"""
        return self._cert_dir / "ca.key"

    def get_device_cert_path(self, device_id: str) -> Path:
        """获取设备证书路径"""
        return self._cert_dir / f"device_{device_id}.crt"

    def get_device_key_path(self, device_id: str) -> Path:
        """获取设备私钥路径"""
        return self._cert_dir / f"device_{device_id}.key"

    def is_ca_exists(self) -> bool:
        """检查CA证书是否存在"""
        return self.get_ca_cert_path().exists() and self.get_ca_key_path().exists()

    def load_cert(self, cert_path: Path) -> str | None:
        """加载证书文件"""
        try:
            if cert_path.exists():
                return cert_path.read_text(encoding="utf-8")
        except Exception as e:
            logger.error("加载证书失败 %s: %s", cert_path, e)
        return None

    def save_cert(self, cert_path: Path, content: str) -> bool:
        """保存证书文件"""
        try:
            cert_path.parent.mkdir(parents=True, exist_ok=True)
            cert_path.write_text(content, encoding="utf-8")
            return True
        except Exception as e:
            logger.error("保存证书失败 %s: %s", cert_path, e)
            return False

    def get_cert_fingerprint(self, cert_content: str) -> str:
        """获取证书指纹 (SHA256)"""
        return hashlib.sha256(cert_content.encode()).hexdigest()[:16]

    def validate_cert(self, cert_path: Path) -> bool:
        """验证证书是否有效"""
        # 简化实现：检查文件是否存在且未过期
        try:
            if not cert_path.exists():
                return False
            # 检查文件修改时间，30天后认为需要更新
            mtime = cert_path.stat().st_mtime
            age_days = (time.time() - mtime) / 86400
            return age_days < 365  # 1年内有效
        except Exception:
            return False


class TlsConfigBuilder:
    """TLS配置构建器 - 构建各种协议的TLS配置"""

    def __init__(self, cert_manager: CertManager):
        self._cert_manager = cert_manager

    def build_mqtt_ssl_context(
        self,
        ca_cert: str | None = None,
        client_cert: str | None = None,
        client_key: str | None = None,
        verify_mode: int = ssl.CERT_REQUIRED,
    ) -> ssl.SSLContext | None:
        """构建MQTT SSL上下文

        Args:
            ca_cert: CA证书内容
            client_cert: 客户端证书内容
            client_key: 客户端私钥内容
            verify_mode: 验证模式 (CERT_NONE/CERT_OPTIONAL/CERT_REQUIRED)

        Returns:
            SSL上下文，失败返回None
        """
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.verify_mode = verify_mode

            if ca_cert:
                import tempfile
                with tempfile.NamedTemporaryFile(mode="w", suffix=".crt", delete=False) as f:
                    f.write(ca_cert)
                    ca_path = f.name
                context.load_verify_locations(ca_path)

            if client_cert and client_key:
                import tempfile
                with tempfile.NamedTemporaryFile(mode="w", suffix=".crt", delete=False) as f:
                    f.write(client_cert)
                    cert_path = f.name
                with tempfile.NamedTemporaryFile(mode="w", suffix=".key", delete=False) as f:
                    f.write(client_key)
                    key_path = f.name
                context.load_cert_chain(cert_path, key_path)
                # 清理临时文件
                os.unlink(cert_path)
                os.unlink(key_path)

            return context

        except Exception as e:
            logger.error("构建MQTT SSL上下文失败: %s", e)
            return None

    def build_https_context(
        self,
        cert_path: str | Path,
        key_path: str | Path,
        ca_cert_path: str | Path | None = None,
    ) -> ssl.SSLContext | None:
        """构建HTTPS SSL上下文

        Args:
            cert_path: 服务器证书路径
            key_path: 服务器私钥路径
            ca_cert_path: CA证书路径 (可选)

        Returns:
            SSL上下文
        """
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)

            cert = Path(cert_path)
            key = Path(key_path)

            if not cert.exists() or not key.exists():
                logger.error("证书或私钥文件不存在")
                return None

            context.load_cert_chain(str(cert), str(key))

            if ca_cert_path:
                ca = Path(ca_cert_path)
                if ca.exists():
                    context.load_verify_locations(str(ca))

            return context

        except Exception as e:
            logger.error("构建HTTPS SSL上下文失败: %s", e)
            return None


class TlsManager:
    """TLS管理器 - 统一管理所有TLS配置"""

    def __init__(self, cert_dir: str = "data/certs"):
        self._cert_manager = CertManager(cert_dir)
        self._config_builder = TlsConfigBuilder(self._cert_manager)
        self._ssl_contexts: dict[str, ssl.SSLContext] = {}

    async def setup_mqtt_tls(
        self,
        host: str,
        port: int,
        ca_cert: str | None = None,
        client_cert: str | None = None,
        client_key: str | None = None,
        verify_mode: str = "required",
    ) -> dict[str, Any]:
        """配置MQTT TLS连接

        Args:
            host: MQTT Broker主机
            port: MQTT Broker端口
            ca_cert: CA证书
            client_cert: 客户端证书
            client_key: 客户端私钥
            verify_mode: 验证模式 (required/optional/none)

        Returns:
            TLS配置字典
        """
        mode_map = {
            "required": ssl.CERT_REQUIRED,
            "optional": ssl.CERT_OPTIONAL,
            "none": ssl.CERT_NONE,
        }
        ssl_verify = mode_map.get(verify_mode, ssl.CERT_REQUIRED)

        context = self._config_builder.build_mqtt_ssl_context(
            ca_cert, client_cert, client_key, ssl_verify
        )

        if context:
            key = f"mqtt_{host}_{port}"
            self._ssl_contexts[key] = context
            return {
                "host": host,
                "port": port,
                "tls_enabled": True,
                "verify_mode": verify_mode,
                "ssl_context": context,
            }
        else:
            return {
                "host": host,
                "port": port,
                "tls_enabled": False,
                "error": "SSL context creation failed",
            }

    async def setup_opcua_tls(
        self,
        server_url: str,
        cert_path: str | None = None,
        private_key_path: str | None = None,
        ca_cert_path: str | None = None,
    ) -> dict[str, Any]:
        """配置OPC UA TLS

        Args:
            server_url: OPC UA服务器URL
            cert_path: 客户端证书路径
            private_key_path: 私钥路径
            ca_cert_path: CA证书路径

        Returns:
            TLS配置字典
        """
        from pathlib import Path

        config = {
            "server_url": server_url,
            "tls_enabled": False,
        }

        if cert_path and private_key_path:
            cert = Path(cert_path)
            key = Path(private_key_path)
            ca = Path(ca_cert_path) if ca_cert_path else None

            context = self._config_builder.build_https_context(
                cert, key, ca if ca and ca.exists() else None
            )

            if context:
                config["tls_enabled"] = True
                config["security_policy"] = "Basic256Sha256"
                config["ssl_context"] = context

        return config

    async def generate_self_signed_cert(
        self,
        common_name: str,
        organization: str = "EdgeLiteGateway",
        validity_days: int = 365,
    ) -> dict[str, str] | None:
        """生成自签名证书

        Args:
            common_name: 通用名称 (如设备ID或域名)
            organization: 组织名称
            validity_days: 有效期天数

        Returns:
            证书和私钥内容，失败返回None
        """
        try:
            from datetime import datetime, timedelta
            from cryptography import x509
            from cryptography.x509.oid import NameOID
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
            from cryptography.hazmat.backends import default_backend

            # 生成RSA私钥
            private_key = rsa.generate_private_key(
                public_exponent=65537,
                key_size=2048,
                backend=default_backend(),
            )

            # 构建证书
            subject = issuer = x509.Name([
                x509.NameAttribute(NameOID.COUNTRY_NAME, "CN"),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, organization),
                x509.NameAttribute(NameOID.COMMON_NAME, common_name),
            ])

            cert = (
                x509.CertificateBuilder()
                .subject_name(subject)
                .issuer_name(issuer)
                .public_key(private_key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(datetime.utcnow())
                .not_valid_after(datetime.utcnow() + timedelta(days=validity_days))
                .sign(private_key, hashes.SHA256(), default_backend())
            )

            # 编码证书和私钥
            cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
            key_pem = private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            ).decode()

            logger.info("生成自签名证书: %s (有效期%d天)", common_name, validity_days)
            return {"cert": cert_pem, "key": key_pem}

        except ImportError as e:
            logger.error("缺少cryptography库: %s", e)
            return None
        except Exception as e:
            logger.error("生成证书失败: %s", e)
            return None

    async def setup_mutual_tls(
        self,
        host: str,
        port: int,
        ca_cert_path: str,
        device_cert_path: str,
        device_key_path: str,
    ) -> dict[str, Any]:
        """配置双向TLS认证

        Args:
            host: 服务器主机
            port: 服务器端口
            ca_cert_path: CA证书路径
            device_cert_path: 设备证书路径
            device_key_path: 设备私钥路径

        Returns:
            TLS配置
        """
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.verify_mode = ssl.CERT_REQUIRED
            context.load_verify_locations(ca_cert_path)
            context.load_cert_chain(device_cert_path, device_key_path)

            # 设置主机名验证
            context.check_hostname = True
            context.hostname_checks_common_name = True

            return {
                "host": host,
                "port": port,
                "tls_enabled": True,
                "mutual_auth": True,
                "ssl_context": context,
            }

        except Exception as e:
            logger.error("配置双向TLS失败: %s", e)
            return {
                "host": host,
                "port": port,
                "tls_enabled": False,
                "error": str(e),
            }

    def get_ssl_context(self, key: str) -> ssl.SSLContext | None:
        """获取缓存的SSL上下文"""
        return self._ssl_contexts.get(key)

    def clear_cache(self) -> None:
        """清除SSL上下文缓存"""
        self._ssl_contexts.clear()
        logger.info("SSL上下文缓存已清除")


# 全局实例
_tls_manager: TlsManager | None = None


def get_tls_manager() -> TlsManager:
    """获取TLS管理器全局实例"""
    global _tls_manager
    if _tls_manager is None:
        _tls_manager = TlsManager()
    return _tls_manager
