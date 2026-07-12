"""SecretManager 单元测试 - 敏感配置加密管理

覆盖 src/edgelite/security/secret_manager.py（792 行）：
- EncryptedValue 数据类
- SecretManager 初始化 / encrypt / decrypt / decrypt_or_plain
- encrypt_value / decrypt_value 递归加解密
- is_sensitive_field 边界匹配（防误匹配）
- mask_config / mask_value 脱敏
- rotate_key 密钥轮换
- 模块级 get_secret_manager / init_secret_manager / sign_script / verify_script_signature

设计要点：
- 使用真实 Fernet 加解密验证端到端正确性（不 mock cryptography）
- autouse fixture 加速 PBKDF2（600000→1 迭代）：测试验证加解密逻辑而非 KDF 安全参数
- 固定 KDF salt（env 注入）避免测试写入 .kdf_salt 文件
"""

from __future__ import annotations

import base64
import hashlib
import json

import pytest

from edgelite.constants import _KDF_SALT_ENV
from edgelite.security import secret_manager as sm_module
from edgelite.security.secret_manager import (
    EncryptedValue,
    SecretManager,
    get_secret_manager,
    init_secret_manager,
    sign_script,
    verify_script_signature,
)

_TEST_MASTER_KEY = "test-master-key-for-unit-testing-only-32chars!"


# ── autouse fixtures ──


@pytest.fixture(autouse=True)
def _fast_kdf(monkeypatch):
    """加速 PBKDF2 派生：600000→1 迭代。

    迭代次数是安全参数（由源码硬编码 600000 保证），测试只需验证加解密/salt/Fernet 逻辑。
    """
    _real_pbkdf2 = hashlib.pbkdf2_hmac

    def _fast(hash_name, password, salt, iterations, dklen=None):
        return _real_pbkdf2(hash_name, password, salt, 1, dklen)

    monkeypatch.setattr(hashlib, "pbkdf2_hmac", _fast)


@pytest.fixture(autouse=True)
def _fixed_kdf_salt(monkeypatch):
    """固定 KDF salt，避免测试写入 .kdf_salt 文件。"""
    salt_b64 = base64.b64encode(b"test-kdf-salt-16b").decode()
    monkeypatch.setenv(_KDF_SALT_ENV, salt_b64)


@pytest.fixture(autouse=True)
def _reset_global_singletons():
    """每个测试前后重置全局单例，防止跨测试污染。"""
    sm_module._secret_manager = None
    sm_module._secret_manager_initialized = False
    sm_module._script_sign_key_cache = None
    yield
    sm_module._secret_manager = None
    sm_module._secret_manager_initialized = False
    sm_module._script_sign_key_cache = None


# ── 业务 fixtures ──


@pytest.fixture
def manager():
    """已初始化的 SecretManager（auto_encrypt=True，真实 Fernet）。"""
    return SecretManager(master_key=_TEST_MASTER_KEY, auto_encrypt=True)


@pytest.fixture
def manager_no_encrypt():
    """已初始化但不自动加密的 SecretManager（用于 mask_config 掩码测试）。"""
    return SecretManager(master_key=_TEST_MASTER_KEY, auto_encrypt=False)


@pytest.fixture
def uninitialized_manager(monkeypatch):
    """未初始化的 SecretManager（无密钥）。"""
    monkeypatch.delenv("EDGELITE_MASTER_KEY", raising=False)
    return SecretManager(master_key=None)


# ──────────────────────────────────────────────────────────
# EncryptedValue 数据类
# ──────────────────────────────────────────────────────────


class TestEncryptedValue:
    def test_to_dict_contains_all_fields(self):
        ev = EncryptedValue(algorithm="fernet", salt="abc", ciphertext="xyz")
        d = ev.to_dict()
        assert d == {"algorithm": "fernet", "salt": "abc", "ciphertext": "xyz"}

    def test_from_dict_roundtrip(self):
        original = EncryptedValue(algorithm="fernet-pbkdf2", salt="s1", ciphertext="c1")
        restored = EncryptedValue.from_dict(original.to_dict())
        assert restored.algorithm == "fernet-pbkdf2"
        assert restored.salt == "s1"
        assert restored.ciphertext == "c1"

    def test_from_dict_defaults(self):
        ev = EncryptedValue.from_dict({})
        assert ev.algorithm == "fernet"
        assert ev.salt == ""
        assert ev.ciphertext == ""

    def test_is_encrypted_true(self):
        assert EncryptedValue(ciphertext="abc").is_encrypted() is True

    def test_is_encrypted_false_when_empty(self):
        assert EncryptedValue(ciphertext="").is_encrypted() is False


# ──────────────────────────────────────────────────────────
# SecretManager 初始化
# ──────────────────────────────────────────────────────────


class TestSecretManagerInit:
    def test_init_with_master_key(self, manager):
        assert manager.is_initialized() is True

    def test_init_with_key_file_generates_and_saves(self, tmp_path):
        key_file = tmp_path / "master_key.json"
        m = SecretManager(key_file=str(key_file))
        assert m.is_initialized() is True
        assert key_file.exists()
        # 文件包含 master_key
        data = json.loads(key_file.read_text(encoding="utf-8"))
        assert data["master_key"]

    def test_init_with_key_file_loads_existing(self, tmp_path):
        key_file = tmp_path / "master_key.json"
        # 第一次创建并保存
        m1 = SecretManager(key_file=str(key_file))
        saved_key = m1._master_key
        # 第二次从文件加载
        m2 = SecretManager(key_file=str(key_file))
        assert m2.is_initialized() is True
        assert m2._master_key == saved_key

    def test_init_with_env_var(self, monkeypatch):
        monkeypatch.setenv("EDGELITE_MASTER_KEY", _TEST_MASTER_KEY)
        m = SecretManager()
        assert m.is_initialized() is True

    def test_init_without_key_not_initialized(self, monkeypatch):
        monkeypatch.delenv("EDGELITE_MASTER_KEY", raising=False)
        m = SecretManager()
        assert m.is_initialized() is False

    def test_default_sensitive_fields_loaded(self, manager):
        assert "password" in manager._sensitive_fields
        assert "token" in manager._sensitive_fields
        assert "api_key" in manager._sensitive_fields

    def test_custom_sensitive_fields(self):
        m = SecretManager(master_key=_TEST_MASTER_KEY, sensitive_fields={"my_secret"})
        assert "my_secret" in m._sensitive_fields
        assert "password" not in m._sensitive_fields  # 自定义覆盖默认


# ──────────────────────────────────────────────────────────
# encrypt / decrypt
# ──────────────────────────────────────────────────────────


class TestEncryptDecrypt:
    def test_encrypt_decrypt_roundtrip(self, manager):
        plaintext = "my-secret-password-123"
        encrypted = manager.encrypt(plaintext)
        # 加密结果是 JSON 字符串
        assert encrypted.startswith("{")
        data = json.loads(encrypted)
        assert data["algorithm"] == "fernet-pbkdf2"
        assert data["salt"]
        assert data["ciphertext"]
        assert data["ciphertext"] != plaintext
        # 解密还原
        decrypted = manager.decrypt(encrypted)
        assert decrypted == plaintext

    def test_encrypt_different_each_time(self, manager):
        """每次加密使用随机 salt，相同明文产生不同密文。"""
        e1 = manager.encrypt("same-value")
        e2 = manager.encrypt("same-value")
        assert e1 != e2
        # 但两者都能解密为同一明文
        assert manager.decrypt(e1) == "same-value"
        assert manager.decrypt(e2) == "same-value"

    def test_encrypt_not_initialized_raises(self, uninitialized_manager):
        with pytest.raises(RuntimeError, match="not initialized"):
            uninitialized_manager.encrypt("test")

    def test_decrypt_not_initialized_raises(self, uninitialized_manager):
        with pytest.raises(RuntimeError, match="not initialized"):
            uninitialized_manager.decrypt('{"algorithm":"fernet","salt":"","ciphertext":"x"}')

    def test_decrypt_non_json_raises(self, manager):
        with pytest.raises(RuntimeError):
            manager.decrypt("not-a-json-string")

    def test_decrypt_empty_ciphertext_raises(self, manager):
        empty = json.dumps(EncryptedValue(ciphertext="").to_dict())
        with pytest.raises(RuntimeError):
            manager.decrypt(empty)


# ──────────────────────────────────────────────────────────
# decrypt_or_plain
# ──────────────────────────────────────────────────────────


class TestDecryptOrPlain:
    def test_non_encrypted_returns_original(self, manager):
        assert manager.decrypt_or_plain("plain-text") == "plain-text"

    def test_empty_string_returns_empty(self, manager):
        assert manager.decrypt_or_plain("") == ""

    def test_encrypted_decrypts_successfully(self, manager):
        encrypted = manager.encrypt("secret")
        assert manager.decrypt_or_plain(encrypted) == "secret"

    def test_invalid_encrypted_format_raises(self, manager):
        """以 {"algorithm": 开头但密文无效 → 抛异常（不返回密文）。"""
        with pytest.raises(RuntimeError):
            manager.decrypt_or_plain('{"algorithm":"fernet","salt":"x","ciphertext":"invalid"}')


# ──────────────────────────────────────────────────────────
# encrypt_value / decrypt_value（递归）
# ──────────────────────────────────────────────────────────


class TestEncryptDecryptValue:
    def test_encrypt_value_dict_roundtrip(self, manager):
        original = {"username": "admin", "password": "secret123"}
        encrypted = manager.encrypt_value(original)
        # 所有字符串值都被加密
        assert encrypted["username"] != original["username"]
        assert encrypted["password"] != original["password"]
        # 解密还原
        decrypted = manager.decrypt_value(encrypted)
        assert decrypted == original

    def test_encrypt_value_list_roundtrip(self, manager):
        original = ["alpha", "beta", "gamma"]
        encrypted = manager.encrypt_value(original)
        assert all(e != o for e, o in zip(encrypted, original, strict=False))
        decrypted = manager.decrypt_value(encrypted)
        assert decrypted == original

    def test_encrypt_value_non_string_passthrough(self, manager):
        assert manager.encrypt_value(42) == 42
        assert manager.encrypt_value(None) is None
        assert manager.encrypt_value(True) is True
        assert manager.encrypt_value(3.14) == 3.14

    def test_decrypt_value_non_encrypted_string_passthrough(self, manager):
        """非加密格式的字符串原样返回。"""
        assert manager.decrypt_value("plain-text") == "plain-text"

    def test_decrypt_value_nested_structure(self, manager):
        original = {"outer": {"inner": "secret", "num": 1}, "list": ["a", "b"]}
        encrypted = manager.encrypt_value(original)
        decrypted = manager.decrypt_value(encrypted)
        assert decrypted == original


# ──────────────────────────────────────────────────────────
# is_sensitive_field（边界匹配，防误匹配）
# ──────────────────────────────────────────────────────────


class TestIsSensitiveField:
    def test_direct_match(self, manager):
        assert manager.is_sensitive_field("password") is True
        assert manager.is_sensitive_field("token") is True
        assert manager.is_sensitive_field("api_key") is True

    def test_case_insensitive(self, manager):
        assert manager.is_sensitive_field("Password") is True
        assert manager.is_sensitive_field("TOKEN") is True

    def test_prefix_match_with_separator(self, manager):
        assert manager.is_sensitive_field("db_password") is True
        assert manager.is_sensitive_field("auth-token") is True
        assert manager.is_sensitive_field("config.secret") is True

    def test_suffix_match_with_separator(self, manager):
        assert manager.is_sensitive_field("my_password") is True
        assert manager.is_sensitive_field("auth-token") is True

    def test_no_false_positive_boundary_match(self, manager):
        """tokenize 不应匹配 token（边界匹配防护）。"""
        assert manager.is_sensitive_field("tokenize") is False
        assert manager.is_sensitive_field("passwordless") is False
        assert manager.is_sensitive_field("username") is False
        assert manager.is_sensitive_field("normal_field") is False

    def test_add_sensitive_field(self, manager):
        # "custom_field" 不在默认敏感字段中，也不通过边界匹配任何默认字段
        assert manager.is_sensitive_field("custom_field") is False
        manager.add_sensitive_field("custom_field")
        assert manager.is_sensitive_field("custom_field") is True

    def test_remove_sensitive_field(self, manager):
        assert manager.is_sensitive_field("password") is True
        manager.remove_sensitive_field("password")
        assert manager.is_sensitive_field("password") is False


# ──────────────────────────────────────────────────────────
# mask_config
# ──────────────────────────────────────────────────────────


class TestMaskConfig:
    def test_sensitive_field_masked(self, manager_no_encrypt):
        config = {"username": "admin", "password": "mysecret123"}
        masked = manager_no_encrypt.mask_config(config)
        assert masked["username"] == "admin"
        assert masked["password"] != "mysecret123"
        assert "****" in masked["password"]

    def test_nested_dict_recursive(self, manager_no_encrypt):
        config = {"db": {"host": "localhost", "password": "secret"}}
        masked = manager_no_encrypt.mask_config(config)
        assert masked["db"]["host"] == "localhost"
        assert masked["db"]["password"] != "secret"

    def test_list_of_dicts(self, manager_no_encrypt):
        config = {"connections": [{"password": "p1"}, {"name": "n2"}]}
        masked = manager_no_encrypt.mask_config(config)
        assert masked["connections"][0]["password"] != "p1"
        assert masked["connections"][1]["name"] == "n2"

    def test_auto_encrypt_when_initialized(self, manager):
        """auto_encrypt=True 且已初始化 → 加密而非掩码。"""
        config = {"password": "mysecret"}
        masked = manager.mask_config(config)
        assert masked["password"].startswith("{")
        # 可解密还原
        assert manager.decrypt(masked["password"]) == "mysecret"

    def test_non_dict_returns_as_is(self, manager_no_encrypt):
        assert manager_no_encrypt.mask_config("string") == "string"
        assert manager_no_encrypt.mask_config(42) == 42

    def test_non_sensitive_passthrough(self, manager_no_encrypt):
        config = {"host": "localhost", "port": 8080}
        masked = manager_no_encrypt.mask_config(config)
        assert masked == config


# ──────────────────────────────────────────────────────────
# mask_value
# ──────────────────────────────────────────────────────────


class TestMaskValue:
    def test_empty_value(self, manager):
        assert manager.mask_value("") == "***"

    def test_short_value(self, manager):
        assert manager.mask_value("ab") == "****"

    def test_long_value(self, manager):
        result = manager.mask_value("mysecretvalue")
        assert result == "my****ue"

    def test_non_string_value(self, manager):
        assert manager.mask_value(12345) == "12****45"


# ──────────────────────────────────────────────────────────
# rotate_key
# ──────────────────────────────────────────────────────────


class TestRotateKey:
    def test_rotate_with_explicit_key(self, manager):
        old_key = manager._master_key
        new_key = "new-master-key-for-rotation-test-32chars!!"
        assert manager.rotate_key(new_key) is True
        assert manager._master_key == new_key
        assert manager._master_key != old_key

    def test_rotate_auto_generates(self, manager):
        old_key = manager._master_key
        assert manager.rotate_key() is True
        assert manager._master_key != old_key
        assert manager.is_initialized() is True

    def test_old_ciphertext_not_decryptable_after_rotation(self, manager):
        """密钥轮换后，旧密文无法用新密钥解密。"""
        plaintext = "secret-value"
        encrypted = manager.encrypt(plaintext)
        # 轮换密钥
        manager.rotate_key("another-new-key-for-rotation-32chars!!!")
        # 旧密文用旧 salt + 旧派生密钥加密，新密钥派生出的密钥不同
        with pytest.raises(RuntimeError):
            manager.decrypt(encrypted)

    def test_rotate_with_key_file_persists(self, tmp_path):
        key_file = tmp_path / "master_key.json"
        m = SecretManager(key_file=str(key_file))
        old_key = m._master_key
        assert m.rotate_key() is True
        assert m._master_key != old_key
        # 文件已更新
        data = json.loads(key_file.read_text(encoding="utf-8"))
        assert data["master_key"] == m._master_key


# ──────────────────────────────────────────────────────────
# 模块级函数：get_secret_manager / init_secret_manager
# ──────────────────────────────────────────────────────────


class TestGlobalSingleton:
    def test_get_secret_manager_returns_same_instance(self, monkeypatch):
        monkeypatch.setenv("EDGELITE_MASTER_KEY", _TEST_MASTER_KEY)
        m1 = get_secret_manager()
        m2 = get_secret_manager()
        assert m1 is m2

    def test_init_secret_manager_with_master_key(self, monkeypatch):
        monkeypatch.delenv("EDGELITE_MASTER_KEY", raising=False)
        m = init_secret_manager(master_key=_TEST_MASTER_KEY)
        assert m.is_initialized() is True
        assert get_secret_manager() is m

    def test_init_secret_manager_idempotent(self, monkeypatch):
        monkeypatch.delenv("EDGELITE_MASTER_KEY", raising=False)
        m1 = init_secret_manager(master_key=_TEST_MASTER_KEY)
        m2 = init_secret_manager(master_key="different-key-32chars-long!!!!!!!")
        # 第二次调用跳过（模块级标志）
        assert m1 is m2

    def test_init_secret_manager_production_no_key_raises(self, monkeypatch):
        """生产模式（DEV_MODE 未设）无密钥 → 拒绝启动。"""
        monkeypatch.delenv("EDGELITE_MASTER_KEY", raising=False)
        monkeypatch.delenv("DEV_MODE", raising=False)
        with pytest.raises(RuntimeError, match="未配置主密钥"):
            init_secret_manager()

    def test_init_secret_manager_dev_mode_no_key_warns(self, monkeypatch):
        """开发模式（DEV_MODE=true）无密钥 → 警告但不报错。"""
        monkeypatch.delenv("EDGELITE_MASTER_KEY", raising=False)
        monkeypatch.setenv("DEV_MODE", "true")
        m = init_secret_manager()
        assert m is not None


# ──────────────────────────────────────────────────────────
# 脚本签名：sign_script / verify_script_signature
# ──────────────────────────────────────────────────────────


class TestScriptSignature:
    def test_sign_and_verify_roundtrip(self, monkeypatch):
        monkeypatch.setenv("EDGELITE_SCRIPT_SIGN_KEY", "test-sign-key-for-unit-test")
        code = "print('hello world')"
        signature = sign_script(code)
        assert isinstance(signature, str)
        assert len(signature) == 64  # HMAC-SHA256 hex = 64 chars
        assert verify_script_signature(code, signature) is True

    def test_verify_wrong_code_returns_false(self, monkeypatch):
        monkeypatch.setenv("EDGELITE_SCRIPT_SIGN_KEY", "test-sign-key-for-unit-test")
        signature = sign_script("original code")
        assert verify_script_signature("tampered code", signature) is False

    def test_verify_empty_signature_returns_false(self, monkeypatch):
        monkeypatch.setenv("EDGELITE_SCRIPT_SIGN_KEY", "test-sign-key")
        assert verify_script_signature("code", "") is False
        assert verify_script_signature("code", None) is False

    def test_sign_same_code_same_signature(self, monkeypatch):
        monkeypatch.setenv("EDGELITE_SCRIPT_SIGN_KEY", "stable-key-for-test")
        code = "x = 1"
        s1 = sign_script(code)
        s2 = sign_script(code)
        assert s1 == s2  # 同密钥同代码 → 同签名
