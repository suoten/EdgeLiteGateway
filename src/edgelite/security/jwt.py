"""JWT Token生成与验证"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt

from edgelite.config import get_config


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    """创建Access Token"""
    config = get_config()
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=config.security.access_token_expire_minutes)
    )
    to_encode.update({"exp": expire, "type": "access"})
    return jwt.encode(to_encode, config.security.secret_key, algorithm=config.security.algorithm)


def create_refresh_token(data: dict, expires_delta: timedelta | None = None) -> str:
    """创建Refresh Token"""
    config = get_config()
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(days=config.security.refresh_token_expire_days)
    )
    to_encode.update({"exp": expire, "type": "refresh"})
    return jwt.encode(to_encode, config.security.secret_key, algorithm=config.security.algorithm)


def verify_token(token: str, token_type: str = "access") -> dict:
    """验证Token，返回payload。验证失败抛出JWTError"""
    config = get_config()
    payload = jwt.decode(token, config.security.secret_key, algorithms=[config.security.algorithm])
    if payload.get("type") != token_type:
        raise JWTError(f"Expected {token_type} token, got {payload.get('type')}")
    return payload


def decode_token(token: str) -> dict:
    """解码Token（不验证过期），用于调试"""
    config = get_config()
    return jwt.decode(
        token, config.security.secret_key, algorithms=[config.security.algorithm], options={"verify_exp": False}
    )
