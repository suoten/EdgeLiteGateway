"""JWT Token生成与验证"""

from __future__ import annotations

import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from jose import JWTError, jwt

from edgelite.config import get_config

logger = logging.getLogger(__name__)

_fallback_secret: str | None = None


def _resolve_secret_key() -> str:
    """获取有效的JWT签名密钥；若配置为空则生成随机密钥并警告"""
    global _fallback_secret
    config = get_config()
    if config.security.secret_key:
        return config.security.secret_key
    if _fallback_secret is None:
        _fallback_secret = secrets.token_urlsafe(32)
        logger.warning(
            "security.secret_key 为空，已生成临时随机密钥，重启后Token将失效！"
            "请在配置文件中设置 security.secret_key"
        )
    return _fallback_secret


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    """创建Access Token"""
    config = get_config()
    to_encode = data.copy()
    if "jti" not in to_encode:
        to_encode["jti"] = str(uuid.uuid4())
    expire = datetime.now(UTC) + (
        expires_delta or timedelta(minutes=config.security.access_token_expire_minutes)
    )
    to_encode.update({"exp": expire, "type": "access"})
    return jwt.encode(to_encode, _resolve_secret_key(), algorithm=config.security.algorithm)  # FIXED: 原问题-secret_key为空时使用不安全签名，现改为_resolve_secret_key()自动生成随机密钥


def create_refresh_token(data: dict, expires_delta: timedelta | None = None) -> str:
    """创建Refresh Token"""
    config = get_config()
    to_encode = data.copy()
    if "jti" not in to_encode:
        to_encode["jti"] = str(uuid.uuid4())
    expire = datetime.now(UTC) + (
        expires_delta or timedelta(days=config.security.refresh_token_expire_days)
    )
    to_encode.update({"exp": expire, "type": "refresh"})
    return jwt.encode(to_encode, _resolve_secret_key(), algorithm=config.security.algorithm)  # FIXED: 原问题-secret_key为空时使用不安全签名，现改为_resolve_secret_key()自动生成随机密钥


def verify_token(token: str, token_type: str = "access") -> dict:
    """验证Token，返回payload。验证失败抛出JWTError"""
    config = get_config()
    payload = jwt.decode(token, _resolve_secret_key(), algorithms=[config.security.algorithm])  # FIXED: 原问题-secret_key为空时解码不安全，现改为_resolve_secret_key()
    if payload.get("type") != token_type:
        raise JWTError(f"Expected {token_type} token, got {payload.get('type')}")
    jti = payload.get("jti", "")
    if jti:
        from edgelite.security.token_revocation import is_token_revoked

        if is_token_revoked(jti):
            raise JWTError("Token has been revoked")
    return payload


def decode_token(token: str, verify_exp: bool = True) -> dict:
    """解码Token。默认验证过期，仅调试时可设 verify_exp=False"""
    import warnings

    config = get_config()
    if not verify_exp:
        warnings.warn("decode_token(verify_exp=False) 仅用于调试，生产环境请勿使用", stacklevel=2)
    return jwt.decode(
        token,
        _resolve_secret_key(),  # FIXED: 原问题-secret_key为空时解码不安全，现改为_resolve_secret_key()
        algorithms=[config.security.algorithm],
        options={"verify_exp": verify_exp},
    )
