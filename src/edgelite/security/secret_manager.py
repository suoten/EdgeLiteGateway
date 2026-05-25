"""敏感配置加密管理 - 使用 Fernet 对称加密保护敏感信息"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 默认敏感字段列表
DEFAULT_SENSITIVE_FIELDS = {
    "password",
    "secret",
    "token",
    "private_key",
    "privateKey",
    "certificate",
    "cert_path",
    "key_path",
    "access_key",
    "accessKey",
    "secret_key",
    "secretKey",
    "api_key",
    "apiKey",
    "auth_key",
    "authKey",
    "encryption_key",
    "encryptionKey",
}


@dataclass
class EncryptedValue:
    """加密值结构"""

    algorithm: str = "fernet"
    salt: str = ""
    ciphertext: str = ""

    def to_dict(self) -> dict:
        return {
            "algorithm": self.algorithm,
            "salt": self.salt,
            "ciphertext": self.ciphertext,
        }

    @classmethod
    def from_dict(cls, data: dict) -> EncryptedValue:
        return cls(
            algorithm=data.get("algorithm", "fernet"),
            salt=data.get("salt", ""),
            ciphertext=data.get("ciphertext", ""),
        )

    def is_encrypted(self) -> bool:
        return bool(self.ciphertext)


class SecretManager:
    """敏感配置加密管理

    功能：
    - Fernet 对称加密
    - 主密钥管理（KDF 派生）
    - 敏感字段自动加解密
    - 密钥轮换支持
    - 配置自动脱敏

    使用方式：
        # 初始化
        manager = SecretManager(master_key="your-master-key")

        # 加密敏感值
        encrypted = manager.encrypt("password123")

        # 解密
        decrypted = manager.decrypt(encrypted)

        # 自动处理配置
        safe_config = manager.mask_config(dangerous_config)
    """

    def __init__(
        self,
        master_key: str | None = None,
        key_file: str | None = None,
        auto_encrypt: bool = True,
        sensitive_fields: set[str] | None = None,
    ):
        """
        Args:
            master_key: 主密钥（32字节 base64 编码）
            key_file: 密钥文件路径（优先级高于 master_key）
            auto_encrypt: 是否自动加密敏感字段
            sensitive_fields: 敏感字段名集合
        """
        self._auto_encrypt = auto_encrypt
        self._sensitive_fields = sensitive_fields or DEFAULT_SENSITIVE_FIELDS.copy()
        self._fernet = None
        self._initialized = False

        # 加载或生成密钥
        if key_file:
            self._key_file = Path(key_file)
            self._load_or_create_key()
        elif master_key:
            self._master_key = master_key
            self._init_fernet()
        else:
            # 尝试从环境变量读取
            env_key = os.environ.get("EDGELITE_MASTER_KEY")
            if env_key:
                self._master_key = env_key
                self._init_fernet()
            else:
                logger.warning(
                    "SecretManager initialized without key. "
                    "Set master_key, key_file, or EDGELITE_MASTER_KEY env var."
                )

    def _load_or_create_key(self) -> None:
        """从文件加载密钥或创建新密钥"""
        if self._key_file.exists():
            try:
                data = json.loads(self._key_file.read_text(encoding="utf-8"))
                self._master_key = data.get("master_key", "")
                if self._master_key:
                    self._init_fernet()
                    logger.info("Master key loaded from: %s", self._key_file)
            except Exception as e:
                logger.error("Failed to load master key: %s", e)
        else:
            # 生成新密钥
            self._generate_new_key()
            self._save_key()
            logger.info("New master key generated and saved to: %s", self._key_file)

    def _generate_new_key(self) -> None:
        """生成新的主密钥"""
        raw_key = secrets.token_bytes(32)
        self._master_key = base64.urlsafe_b64encode(raw_key).decode()
        self._init_fernet()

    def _save_key(self) -> None:
        """保存密钥到文件"""
        try:
            self._key_file.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "master_key": self._master_key,
                "created_at": self._get_timestamp(),
                "version": 1,
            }
            self._key_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
            # 设置文件权限（仅所有者可读写）
            os.chmod(self._key_file, 0o600)
        except Exception as e:
            logger.error("Failed to save master key: %s", e)

    def _init_fernet(self) -> None:
        """初始化 Fernet 加密器"""
        try:
            from cryptography.fernet import Fernet
        except ImportError:
            logger.error(
                "cryptography not installed. Install with: pip install cryptography"
            )
            return

        try:
            # 使用 PBKDF2 派生密钥
            key = self._derive_key(self._master_key)
            self._fernet = Fernet(key)
            self._initialized = True
            logger.info("SecretManager initialized successfully")
        except Exception as e:
            logger.error("Failed to initialize Fernet: %s", e)

    @staticmethod
    def _derive_key(master_key: str, salt: bytes = b"edgelite-secret-v1") -> bytes:
        """使用 PBKDF2 派生加密密钥

        Args:
            master_key: 主密钥
            salt: 盐值

        Returns:
            派生的密钥（32字节）
        """
        # 确保 master_key 是字符串
        key_str = str(master_key)
        # 使用 SHA256 哈希作为派生输入
        key_input = hashlib.sha256(key_str.encode()).digest()
        # PBKDF2 派生
        derived = hashlib.pbkdf2_hmac(
            "sha256",
            key_input,
            salt,
            iterations=100000,
            dklen=32,
        )
        return base64.urlsafe_b64encode(derived)

    @staticmethod
    def _get_timestamp() -> str:
        """获取当前时间戳"""
        from datetime import datetime, UTC

        return datetime.now(UTC).isoformat()

    def is_initialized(self) -> bool:
        """检查是否已初始化"""
        return self._initialized

    def encrypt(self, plaintext: str) -> str:
        """加密明文字符串

        Args:
            plaintext: 要加密的明文

        Returns:
            加密后的 JSON 字符串（包含 algorithm, salt, ciphertext）
        """
        if not self._initialized:
            logger.warning("SecretManager not initialized, returning plaintext")
            return plaintext

        try:
            from cryptography.fernet import Fernet

            # 生成随机盐
            salt = secrets.token_hex(16)
            derived_key = self._derive_key(self._master_key, salt.encode())

            # 使用派生的密钥加密
            fernet = Fernet(derived_key)
            ciphertext = fernet.encrypt(plaintext.encode()).decode()

            encrypted = EncryptedValue(
                algorithm="fernet-pbkdf2",
                salt=salt,
                ciphertext=ciphertext,
            )
            return json.dumps(encrypted.to_dict())

        except Exception as e:
            logger.error("Encryption failed: %s", e)
            return plaintext

    def decrypt(self, encrypted_str: str) -> str:
        """解密密文字符串

        Args:
            encrypted_str: 加密后的 JSON 字符串

        Returns:
            解密后的明文
        """
        if not self._initialized:
            logger.warning("SecretManager not initialized")
            return encrypted_str

        try:
            # 检查是否已经是加密格式
            if not encrypted_str.startswith("{"):
                # 可能是明文，直接返回
                return encrypted_str

            encrypted = EncryptedValue.from_dict(json.loads(encrypted_str))
            if not encrypted.is_encrypted():
                return encrypted_str

            # 使用保存的盐值派生密钥
            derived_key = self._derive_key(self._master_key, encrypted.salt.encode())
            from cryptography.fernet import Fernet

            fernet = Fernet(derived_key)
            plaintext = fernet.decrypt(encrypted.ciphertext.encode()).decode()
            return plaintext

        except Exception as e:
            logger.error("Decryption failed: %s", e)
            return encrypted_str

    def encrypt_value(self, value: Any) -> Any:
        """递归加密字典中的敏感值"""
        if isinstance(value, dict):
            return {k: self.encrypt_value(v) for k, v in value.items()}
        elif isinstance(value, list):
            return [self.encrypt_value(item) for item in value]
        elif isinstance(value, str):
            return self.encrypt(value)
        return value

    def decrypt_value(self, value: Any) -> Any:
        """递归解密字典中的敏感值"""
        if isinstance(value, dict):
            return {k: self.decrypt_value(v) for k, v in value.items()}
        elif isinstance(value, list):
            return [self.decrypt_value(item) for item in value]
        elif isinstance(value, str):
            # 检查是否是加密格式
            if value.startswith('{"algorithm":'):
                return self.decrypt(value)
        return value

    def is_sensitive_field(self, field_name: str) -> bool:
        """检查字段名是否是敏感字段"""
        name_lower = field_name.lower()
        # 直接匹配
        if name_lower in self._sensitive_fields:
            return True
        # 前缀/后缀匹配
        for sensitive in self._sensitive_fields:
            if name_lower.endswith(sensitive.lower()) or name_lower.startswith(sensitive.lower()):
                return True
        return False

    def mask_config(self, config: dict) -> dict:
        """对配置进行脱敏处理

        Args:
            config: 原始配置字典

        Returns:
            脱敏后的配置（敏感字段被替换为掩码）
        """
        if not isinstance(config, dict):
            return config

        masked = {}
        for key, value in config.items():
            if self.is_sensitive_field(key):
                if self._auto_encrypt and isinstance(value, str) and value:
                    # 加密而非掩码
                    masked[key] = self.encrypt(value)
                else:
                    # 掩码
                    masked[key] = self.mask_value(value)
            elif isinstance(value, dict):
                masked[key] = self.mask_config(value)
            else:
                masked[key] = value

        return masked

    def mask_value(self, value: Any) -> str:
        """将值掩码化"""
        if not value:
            return "***"
        value_str = str(value)
        if len(value_str) <= 4:
            return "****"
        # 显示前2后2
        return value_str[:2] + "****" + value_str[-2:]

    def add_sensitive_field(self, field_name: str) -> None:
        """添加敏感字段"""
        self._sensitive_fields.add(field_name.lower())

    def remove_sensitive_field(self, field_name: str) -> None:
        """移除敏感字段"""
        self._sensitive_fields.discard(field_name.lower())

    def rotate_key(self, new_master_key: str | None = None) -> bool:
        """密钥轮换

        Args:
            new_master_key: 新主密钥，默认自动生成

        Returns:
            True 表示成功
        """
        try:
            if new_master_key is None:
                self._generate_new_key()
            else:
                self._master_key = new_master_key
                self._init_fernet()

            if hasattr(self, "_key_file") and self._key_file:
                self._save_key()

            logger.info("Master key rotated successfully")
            return True
        except Exception as e:
            logger.error("Key rotation failed: %s", e)
            return False


# 全局实例
_secret_manager: SecretManager | None = None


def get_secret_manager() -> SecretManager:
    """获取全局 SecretManager 实例"""
    global _secret_manager
    if _secret_manager is None:
        _secret_manager = SecretManager()
    return _secret_manager


def init_secret_manager(
    master_key: str | None = None,
    key_file: str | None = None,
    **kwargs,
) -> SecretManager:
    """初始化全局 SecretManager"""
    global _secret_manager
    _secret_manager = SecretManager(
        master_key=master_key,
        key_file=key_file,
        **kwargs,
    )
    return _secret_manager
