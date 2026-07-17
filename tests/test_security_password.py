"""密码哈希与验证单元测试。

覆盖 src/edgelite/security/password.py：SHA-256 预哈希、bcrypt 哈希、
验证（预哈希路径 + 遗留直接 bcrypt 兼容路径）、空密码拒绝、超长密码处理。
"""

from __future__ import annotations

import bcrypt
import pytest

from edgelite.security.password import (
    _BCRYPT_MAX_PASSWORD_BYTES,
    _BCRYPT_ROUNDS,
    _prehash_password,
    hash_password,
    verify_password,
)

# ─── _prehash_password ───


def test_prehash_returns_bytes():
    result = _prehash_password("test123")
    assert isinstance(result, bytes)


def test_prehash_fixed_length():
    """预哈希结果为固定 44 字节（base64 编码的 32 字节 SHA-256 摘要）。"""
    result = _prehash_password("test123")
    assert len(result) == 44


def test_prehash_deterministic():
    """相同密码产生相同预哈希。"""
    assert _prehash_password("password") == _prehash_password("password")


def test_prehash_different_passwords():
    """不同密码产生不同预哈希。"""
    assert _prehash_password("password1") != _prehash_password("password2")


# ─── hash_password ───


def test_hash_password_returns_str():
    hashed = hash_password("mypassword")
    assert isinstance(hashed, str)


def test_hash_password_bcrypt_format():
    """哈希结果以 $2b$ 开头（bcrypt 格式）。"""
    hashed = hash_password("mypassword")
    assert hashed.startswith("$2b$")


def test_hash_password_uses_configured_rounds():
    """哈希使用配置的 rounds。"""
    hashed = hash_password("mypassword")
    # $2b$14$... — 14 rounds
    parts = hashed.split("$")
    assert parts[2] == str(_BCRYPT_ROUNDS)


def test_hash_password_different_each_time():
    """相同密码每次哈希结果不同（盐随机）。"""
    h1 = hash_password("samepassword")
    h2 = hash_password("samepassword")
    assert h1 != h2


def test_hash_password_empty_rejected():
    """空密码被拒绝。"""
    with pytest.raises(ValueError, match="empty"):
        hash_password("")


# ─── verify_password ───


def test_verify_password_correct():
    """正确密码验证成功。"""
    hashed = hash_password("mypassword123")
    assert verify_password("mypassword123", hashed) is True


def test_verify_password_wrong():
    """错误密码验证失败。"""
    hashed = hash_password("mypassword123")
    assert verify_password("wrongpassword", hashed) is False


def test_verify_password_empty_plain():
    """空明文密码验证失败。"""
    hashed = hash_password("mypassword")
    assert verify_password("", hashed) is False


def test_verify_password_empty_hashed():
    """空哈希验证失败。"""
    assert verify_password("password", "") is False


def test_verify_password_both_empty():
    """双方为空验证失败。"""
    assert verify_password("", "") is False


def test_verify_password_invalid_hash_format():
    """无效哈希格式返回 False（不抛异常）。"""
    assert verify_password("password", "not-a-valid-hash") is False


def test_verify_password_long_password():
    """超长密码（>72字节）正确哈希和验证。"""
    long_password = "a" * 200
    hashed = hash_password(long_password)
    assert verify_password(long_password, hashed) is True


def test_verify_password_long_password_wrong():
    """超长密码验证失败。"""
    long_password = "a" * 200
    hashed = hash_password(long_password)
    assert verify_password("b" * 200, hashed) is False


def test_verify_password_legacy_direct_bcrypt():
    """遗留直接 bcrypt 格式（无预哈希）兼容验证。"""
    plain = "legacypass"
    # 直接用 bcrypt 哈希（不经过预哈希），模拟遗留密码
    salt = bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)
    legacy_hash = bcrypt.hashpw(plain.encode("utf-8"), salt).decode("utf-8")
    # 验证遗留密码
    assert verify_password(plain, legacy_hash) is True


def test_verify_password_72_byte_boundary():
    """72 字节密码（bcrypt 限制边界）正确处理。"""
    # 正好 72 字节的密码
    password_72 = "a" * 72
    hashed = hash_password(password_72)
    assert verify_password(password_72, hashed) is True


def test_verify_password_73_byte_boundary():
    """73 字节密码（超过 bcrypt 限制）通过预哈希正确处理。"""
    password_73 = "a" * 73
    hashed = hash_password(password_73)
    assert verify_password(password_73, hashed) is True


def test_bcrypt_max_password_bytes_value():
    assert _BCRYPT_MAX_PASSWORD_BYTES == 72


def test_bcrypt_rounds_value():
    assert _BCRYPT_ROUNDS == 14
