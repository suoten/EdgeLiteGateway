"""敏感配置加密管理 - 使用 Fernet 对称加密保护敏感信息"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from edgelite.constants import _KDF_SALT_ENV

logger = logging.getLogger(__name__)

# SEC-FIX-SCRIPT-SIGN: 脚本签名密钥环境变量名
_SCRIPT_SIGN_KEY_ENV = "EDGELITE_SCRIPT_SIGN_KEY"
# 脚本签名密钥持久化文件（与 KDF salt 同目录）
_SCRIPT_SIGN_KEY_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
    ".script_sign_key",
)
_script_sign_key_lock = threading.Lock()
_script_sign_key_cache: str | None = None

# R5-F-01: 主密钥持久化文件（与 KDF salt 同目录）
_MASTER_KEY_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
    ".master_key",
)

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


_SALT_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", ".kdf_salt")


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
            # FIXED-P0: Windows上os.chmod无效，使用win32api设置ACL；非Windows使用os.chmod
            import sys
            if sys.platform == "win32":
                try:
                    import win32security
                    sd = win32security.ConvertStringSecurityDescriptorToSDDL("D:P(A;;FA;;;OW)(A;;FR;;;OW)")
                    win32security.SetFileSecurity(str(self._key_file), win32security.DACL_SECURITY_INFORMATION, sd)
                except ImportError:
                    logger.warning("win32security not available, master key file permissions not restricted on Windows")
            else:
                os.chmod(self._key_file, 0o600)
        except Exception as e:
            logger.error("Failed to save master key: %s", e)

    def _init_fernet(self) -> None:
        from cryptography.fernet import Fernet

        try:
            salt = self._load_kdf_salt()
            key = self._derive_key(self._master_key, salt)
            self._fernet = Fernet(key)
            self._initialized = True
            logger.info("SecretManager initialized successfully")
        except Exception as e:
            logger.error("Failed to initialize Fernet: %s", e)

    @staticmethod
    def _load_kdf_salt() -> bytes:
        env_salt = os.environ.get(_KDF_SALT_ENV, "")
        if env_salt:
            try:
                return base64.b64decode(env_salt)
            except Exception:
                logger.warning("Invalid %s value, falling back to file", _KDF_SALT_ENV)
        if os.path.exists(_SALT_FILE):
            try:
                with open(_SALT_FILE, "rb") as f:
                    salt = f.read()
                    if len(salt) >= 16:
                        return salt[:16]
            except Exception:
                logger.warning("Failed to read salt file")
        salt = secrets.token_bytes(16)
        try:
            os.makedirs(os.path.dirname(_SALT_FILE), exist_ok=True)
            # FIXED-P2: 原问题-salt直接write，断电可写空文件导致加密失败
            # 改为tempfile+os.replace原子写入
            import tempfile
            fd, tmp_path = tempfile.mkstemp(
                suffix=".tmp", prefix=".kdf_salt.", dir=os.path.dirname(_SALT_FILE)
            )
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(salt)
                os.replace(tmp_path, _SALT_FILE)
                # R5-S-14: 与 master key 文件保持一致，设置 0o600 权限防止其他用户读取
                import sys
                if sys.platform == "win32":
                    try:
                        import win32security
                        sd = win32security.ConvertStringSecurityDescriptorToSDDL("D:P(A;;FA;;;OW)(A;;FR;;;OW)")
                        win32security.SetFileSecurity(_SALT_FILE, win32security.DACL_SECURITY_INFORMATION, sd)
                    except ImportError:
                        logger.warning("win32security not available, kdf_salt file permissions not restricted on Windows")
                else:
                    os.chmod(_SALT_FILE, 0o600)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError as e:
                    # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                    logger.warning("删除临时salt文件失败: %s", e)
                raise
            logger.info("Generated and saved KDF salt to %s", _SALT_FILE)
        except Exception as e:
            logger.warning("Failed to persist KDF salt: %s. Set %s for reproducible encryption.", e, _KDF_SALT_ENV)
        return salt

    @staticmethod
    def _derive_key(master_key: str, salt: bytes) -> bytes:
        """使用 PBKDF2 派生加密密钥

        Args:
            master_key: 主密钥
            salt: 盐值

        Returns:
            派生的密钥（32字节）

        FIXED-P1: 迭代次数 100000 低于 OWASP 2023 推荐值 600000。
        提升至 600000 以满足 OWASP 2023 推荐。
        注意：此变更会使已加密数据无法用旧迭代次数解密；
        新部署自动使用新值，已有部署需通过 rotate_key 重新加密。
        """
        key_str = str(master_key)
        key_input = hashlib.sha256(key_str.encode()).digest()
        effective_salt = hashlib.sha256(salt + key_input).digest()[:16]  # FIXED-P2: 盐值结合master_key派生，不同实例产生不同derived key
        derived = hashlib.pbkdf2_hmac(
            "sha256",
            key_input,
            effective_salt,
            iterations=600000,  # FIXED-P1: 100000 → 600000 (OWASP 2023 推荐)
            dklen=32,
        )
        return base64.urlsafe_b64encode(derived)

    @staticmethod
    def _get_timestamp() -> str:
        """获取当前时间戳"""
        from datetime import UTC, datetime

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
            raise RuntimeError("SecretManager not initialized, cannot encrypt")  # FIXED-P2: 未初始化时返回明文导致敏感信息暴露，改为抛出异常

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
            raise RuntimeError(f"Encryption failed, refusing to return plaintext: {e}") from e  # FIXED-P0: 加密失败返回明文→抛异常

    def decrypt(self, encrypted_str: str) -> str:
        """解密密文字符串

        Args:
            encrypted_str: 加密后的 JSON 字符串

        Returns:
            解密后的明文
        """
        if not self._initialized:
            raise RuntimeError("SecretManager not initialized, cannot decrypt")  # FIXED-P2: 未初始化时返回原始字符串可能暴露密文，改为抛出异常

        try:
            # 检查是否已经是加密格式
            if not encrypted_str.startswith("{"):
                raise ValueError("Input is not an encrypted format (does not start with '{')")  # FIXED-P2: decrypt对非加密输入抛异常，添加decrypt_or_plain兼容方法

            encrypted = EncryptedValue.from_dict(json.loads(encrypted_str))
            if not encrypted.is_encrypted():
                raise ValueError("EncryptedValue has no ciphertext")  # FIXED-P2: decrypt对非加密输入抛异常，添加decrypt_or_plain兼容方法

            # 使用保存的盐值派生密钥
            derived_key = self._derive_key(self._master_key, encrypted.salt.encode())
            from cryptography.fernet import Fernet

            fernet = Fernet(derived_key)
            plaintext = fernet.decrypt(encrypted.ciphertext.encode()).decode()
            return plaintext

        except Exception as e:
            logger.error("Decryption failed: %s", e)
            raise RuntimeError(f"Decryption failed, refusing to return encrypted string: {e}") from e  # FIXED-P0: 解密失败返回密文→抛异常

    def decrypt_or_plain(self, encrypted_str: str) -> str:
        """尝试解密，若输入非加密格式则原样返回。

        FIXED-P2: decrypt对非加密输入抛异常，添加decrypt_or_plain兼容方法
        FIXED-P0: 原实现在解密失败时返回密文作为明文，可能泄漏密文。
        改为：仅当输入明显非加密格式时返回原值；若输入看起来是加密格式但解密失败，抛出异常。
        """
        # 非加密格式（不以 {"algorithm": 开头）直接返回原值
        if not encrypted_str or not encrypted_str.startswith('{"algorithm":'):
            return encrypted_str
        # 看起来是加密格式，必须成功解密，否则抛异常（不返回密文）
        return self.decrypt(encrypted_str)

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
        # FIXED-P1: 原实现使用 startswith/endswith 导致误匹配
        # （如 "tokenize" 匹配 "token"，"passwordless" 匹配 "password"）
        # 改为仅在常见分隔符（_、-、.）边界处匹配
        for sensitive in self._sensitive_fields:
            sensitive_lower = sensitive.lower()
            # 前缀匹配：field 以 sensitive + 分隔符开头
            if name_lower.startswith(sensitive_lower + "_") or \
               name_lower.startswith(sensitive_lower + "-") or \
               name_lower.startswith(sensitive_lower + "."):
                return True
            # 后缀匹配：field 以 分隔符 + sensitive 结尾
            if name_lower.endswith("_" + sensitive_lower) or \
               name_lower.endswith("-" + sensitive_lower) or \
               name_lower.endswith("." + sensitive_lower):
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
                if self._auto_encrypt and self._initialized and isinstance(value, str) and value:  # FIXED-P0: 未初始化时回退到掩码模式，不调用encrypt
                    masked[key] = self.encrypt(value)
                else:
                    masked[key] = self.mask_value(value)
            elif isinstance(value, dict):
                masked[key] = self.mask_config(value)
            elif isinstance(value, list):
                # FIXED-P1: 原实现未处理列表类型，列表中的敏感字典不会被脱敏
                masked[key] = [
                    self.mask_config(item) if isinstance(item, dict) else item
                    for item in value
                ]
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
# FIXED-P1: 全局单例初始化锁，防止多线程并发创建多个实例
_secret_manager_lock = threading.Lock()
# R5-F-01: 模块级初始化标志，确保 init_secret_manager 只执行一次
_secret_manager_initialized: bool = False


def get_secret_manager() -> SecretManager:
    """获取全局 SecretManager 实例"""
    global _secret_manager
    if _secret_manager is None:
        # FIXED-P1: 双重检查锁定，确保线程安全
        with _secret_manager_lock:
            if _secret_manager is None:
                _secret_manager = SecretManager()
    return _secret_manager


def init_secret_manager(
    master_key: str | None = None,
    key_file: str | None = None,
    **kwargs,
) -> SecretManager:
    """初始化全局 SecretManager

    R5-F-01: 在 bootstrap 启动流程中调用，确保加密系统可用。

    主密钥来源优先级：
    1. 显式传入的 master_key 参数
    2. 显式传入的 key_file 参数
    3. 环境变量 EDGELITE_MASTER_KEY
    4. 持久化文件 data/.master_key

    若未配置主密钥：
    - 开发模式（DEV_MODE=true）：记录 WARNING，以明文模式运行
    - 生产模式（DEV_MODE=false）：记录 ERROR 并拒绝启动（raise RuntimeError）
    """
    global _secret_manager, _secret_manager_initialized

    # 确保初始化只执行一次（模块级标志）
    if _secret_manager_initialized:
        logger.debug("SecretManager 已初始化，跳过重复初始化")
        return _secret_manager  # type: ignore[return-value]

    with _secret_manager_lock:
        # 双重检查，防止并发重复初始化
        if _secret_manager_initialized:
            return _secret_manager  # type: ignore[return-value]

        # 确定主密钥来源
        resolved_master_key = master_key
        resolved_key_file = key_file

        if not resolved_master_key and not resolved_key_file:
            # 尝试从环境变量 EDGELITE_MASTER_KEY 加载
            env_key = os.environ.get("EDGELITE_MASTER_KEY")
            if env_key:
                resolved_master_key = env_key
                logger.info("主密钥已从环境变量 EDGELITE_MASTER_KEY 加载")
            else:
                # 尝试从持久化文件 data/.master_key 加载
                master_key_path = Path(_MASTER_KEY_FILE)
                if master_key_path.exists():
                    try:
                        stored = master_key_path.read_text(encoding="utf-8").strip()
                        if stored:
                            resolved_master_key = stored
                            logger.info("主密钥已从文件加载: %s", master_key_path)
                    except Exception as e:
                        logger.warning("读取主密钥文件失败: %s", e)

        # 若仍未获取到主密钥，根据运行模式决定行为
        if not resolved_master_key and not resolved_key_file:
            dev_mode = os.environ.get("DEV_MODE", "").lower() in ("true", "1", "yes")
            if dev_mode:
                # 开发模式：记录 WARNING，继续以明文模式运行
                logger.warning(
                    "SecretManager 未配置主密钥（DEV_MODE=true），"
                    "敏感配置将以明文存储！请设置 EDGELITE_MASTER_KEY 环境变量"
                    "或创建 data/.master_key 文件以启用加密。"
                )
            else:
                # 生产模式：记录 ERROR 并拒绝启动
                logger.error(
                    "SecretManager 未配置主密钥，生产环境拒绝启动！"
                    "请通过以下方式之一配置主密钥：\n"
                    "  1. 设置环境变量 EDGELITE_MASTER_KEY\n"
                    "  2. 创建 data/.master_key 文件\n"
                    "  3. 在开发模式下设置 DEV_MODE=true 跳过此检查"
                )
                raise RuntimeError(
                    "SecretManager 未配置主密钥，生产环境拒绝启动。"
                    "请设置 EDGELITE_MASTER_KEY 环境变量或创建 data/.master_key 文件。"
                )

        _secret_manager = SecretManager(
            master_key=resolved_master_key,
            key_file=resolved_key_file,
            **kwargs,
        )
        _secret_manager_initialized = True
    return _secret_manager


# ─── SEC-FIX-SCRIPT-SIGN: 脚本签名机制 ───
# 使用 HMAC-SHA256 对脚本代码签名，防止脚本被篡改后启用/执行。
# 密钥来源优先级：环境变量 EDGELITE_SCRIPT_SIGN_KEY > 持久化文件 > 随机生成并持久化。


def _load_or_create_script_sign_key() -> str:
    """加载或创建脚本签名密钥（HMAC key）。

    优先级：
    1. 环境变量 EDGELITE_SCRIPT_SIGN_KEY（部署时注入，便于多实例共享）
    2. 持久化文件 data/.script_sign_key（首次生成后复用，重启后签名仍可验证）
    3. 随机生成 32 字节密钥并持久化到文件

    返回 hex 编码的密钥字符串。
    """
    global _script_sign_key_cache
    with _script_sign_key_lock:
        if _script_sign_key_cache is not None:
            return _script_sign_key_cache

        env_key = os.environ.get(_SCRIPT_SIGN_KEY_ENV, "")
        if env_key:
            _script_sign_key_cache = env_key
            return env_key

        key_path = Path(_SCRIPT_SIGN_KEY_FILE)
        if key_path.exists():
            try:
                stored = key_path.read_text(encoding="utf-8").strip()
                if stored:
                    _script_sign_key_cache = stored
                    return stored
            except Exception as e:
                logger.warning("Failed to read script sign key file: %s", e)

        # 生成新密钥并持久化
        new_key = secrets.token_hex(32)
        try:
            key_path.parent.mkdir(parents=True, exist_ok=True)
            # 原子写入：tempfile + os.replace
            import tempfile
            fd, tmp_path = tempfile.mkstemp(
                suffix=".tmp", prefix=".script_sign_key.", dir=str(key_path.parent)
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(new_key)
                os.replace(tmp_path, str(key_path))
                # 限制文件权限
                import sys
                if sys.platform != "win32":
                    os.chmod(str(key_path), 0o600)
                else:
                    # SEC-FIX: Windows 平台使用 icacls 设置文件 ACL，
                    # 仅当前用户可读，防止其他用户读取密钥文件
                    _apply_windows_acl(str(key_path))
            except Exception:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)
                raise
            # SEC-FIX: 强烈建议生产环境通过环境变量提供密钥
            # 文件持久化仅适用于开发/测试环境，生产环境应使用 EDGELITE_SCRIPT_SIGN_KEY
            logger.warning(
                "Generated and persisted script sign key to %s. "
                "WARNING: For production deployments, set the %s environment variable "
                "instead of relying on file-based key storage for better security.",
                key_path, _SCRIPT_SIGN_KEY_ENV,
            )
        except Exception as e:
            logger.warning(
                "Failed to persist script sign key: %s. Set %s env var for stable signing.",
                e, _SCRIPT_SIGN_KEY_ENV,
            )

        _script_sign_key_cache = new_key
        return new_key


def _apply_windows_acl(filepath: str) -> None:
    """SEC-FIX: 在 Windows 平台为密钥文件设置 ACL，仅当前用户可读。

    使用 icacls 命令移除继承的权限并仅授予当前用户读权限，
    防止其他用户（包括同机非管理员用户）读取密钥文件。
    失败时记录警告日志，不阻断主流程（密钥已写入文件）。
    """
    import subprocess
    import sys

    if sys.platform != "win32":
        return
    try:
        # 优先使用 USERNAME，回退到 USERPROFILE 解析
        username = os.environ.get("USERNAME") or os.environ.get("USER")
        if not username:
            logger.warning(
                "Cannot apply Windows ACL to script sign key file: "
                "unable to determine current username"
            )
            return
        # /inheritance:r 移除继承的 ACE
        # /grant:r 显式授予（替换而非追加）
        # {username}:R 仅读权限（签名验证只需读取）
        subprocess.run(
            ["icacls", filepath, "/inheritance:r", "/grant:r", f"{username}:R"],
            check=True,
            capture_output=True,
            timeout=10,
        )
        logger.info(
            "Applied Windows ACL to script sign key file %s (owner=%s, read-only)",
            filepath, username,
        )
    except subprocess.CalledProcessError as cpe:
        logger.warning(
            "Failed to set ACL on script sign key file (icacls exit %s): %s. "
            "Recommend setting %s env var in production.",
            cpe.returncode,
            (cpe.stderr or b"").decode("utf-8", errors="replace").strip(),
            _SCRIPT_SIGN_KEY_ENV,
        )
    except Exception as e:
        logger.warning(
            "Failed to set ACL on script sign key file: %s. "
            "Recommend setting %s env var in production.",
            e, _SCRIPT_SIGN_KEY_ENV,
        )


def sign_script(code: str) -> str:
    """对脚本代码计算 HMAC-SHA256 签名。

    Args:
        code: 脚本源代码字符串

    Returns:
        hex 编码的 HMAC 摘要字符串
    """
    key_hex = _load_or_create_script_sign_key()
    key_bytes = key_hex.encode("utf-8")
    return hmac.new(key_bytes, code.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_script_signature(code: str, signature: str) -> bool:
    """校验脚本代码签名是否匹配。

    使用 hmac.compare_digest 进行常量时间比较，防止时序攻击。
    签名为空或类型错误时返回 False。

    Args:
        code: 脚本源代码字符串
        signature: 待校验的 hex 签名

    Returns:
        True 表示签名匹配，False 表示不匹配或签名无效
    """
    if not signature or not isinstance(signature, str):
        return False
    expected = sign_script(code)
    return hmac.compare_digest(expected, signature)
