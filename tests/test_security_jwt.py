"""JWT Token 生成与验证单元测试。

覆盖 src/edgelite/security/jwt.py：算法白名单校验、密钥校验、密钥轮换(kid)、
token 生成/验证/解码、jti 撤销检查、session 活跃检查、iat 未来时间检查。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import jwt as pyjwt
import pytest

from edgelite.security.jwt import (
    _ALLOWED_ALGORITHMS,
    _MIN_SECRET_KEY_BYTES,
    _validate_algorithm,
    create_access_token,
    create_refresh_token,
    decode_token,
    verify_token,
)

# verify_token 内部通过 from...import 方式导入 is_token_revoked / is_session_active，
# 需在源模块处 patch 而非 edgelite.security.jwt
_REVOKED_PATCH = "edgelite.security.token_revocation.is_token_revoked"
_SESSION_PATCH = "edgelite.security.session_manager.is_session_active"


# ─── _validate_algorithm ───


def test_validate_algorithm_hs256(mock_config):
    assert _validate_algorithm("HS256") == "HS256"


def test_validate_algorithm_hs384(mock_config):
    assert _validate_algorithm("HS384") == "HS384"


def test_validate_algorithm_hs512(mock_config):
    assert _validate_algorithm("HS512") == "HS512"


def test_validate_algorithm_case_insensitive(mock_config):
    assert _validate_algorithm("hs256") == "HS256"
    assert _validate_algorithm("Hs256") == "HS256"


def test_validate_algorithm_none_rejected(mock_config):
    with pytest.raises(ValueError, match="Only HMAC-SHA"):
        _validate_algorithm("none")


def test_validate_algorithm_rs256_rejected(mock_config):
    with pytest.raises(ValueError, match="Only HMAC-SHA"):
        _validate_algorithm("RS256")


def test_validate_algorithm_none_value(mock_config):
    with pytest.raises(ValueError, match="not configured"):
        _validate_algorithm(None)


def test_validate_algorithm_empty(mock_config):
    with pytest.raises(ValueError):
        _validate_algorithm("")


def test_allowed_algorithms_contents():
    assert frozenset({"HS256", "HS384", "HS512"}) == _ALLOWED_ALGORITHMS


# ─── create_access_token ───


def test_create_access_token_basic(mock_config):
    """基本 access token 生成。"""
    token = create_access_token({"sub": "user1", "role": "admin"})
    payload = pyjwt.decode(
        token,
        mock_config.security.secret_key,
        algorithms=[mock_config.security.algorithm],
    )
    assert payload["sub"] == "user1"
    assert payload["role"] == "admin"
    assert payload["type"] == "access"
    assert "jti" in payload
    assert "exp" in payload
    assert "iat" in payload


def test_create_access_token_preserves_existing_jti(mock_config):
    """已有 jti 不被覆盖。"""
    token = create_access_token({"sub": "u1", "jti": "my-jti-123"})
    payload = pyjwt.decode(
        token,
        mock_config.security.secret_key,
        algorithms=[mock_config.security.algorithm],
    )
    assert payload["jti"] == "my-jti-123"


def test_create_access_token_has_kid_header(mock_config):
    """access token 包含 kid header。"""
    token = create_access_token({"sub": "u1"})
    header = pyjwt.get_unverified_header(token)
    assert header["kid"] == mock_config.security.key_id


def test_create_access_token_explicit_expires_delta(mock_config):
    """显式 expires_delta 生效。"""
    delta = timedelta(minutes=30)
    token = create_access_token({"sub": "u1"}, expires_delta=delta)
    payload = pyjwt.decode(
        token,
        mock_config.security.secret_key,
        algorithms=[mock_config.security.algorithm],
    )
    now_ts = datetime.now(UTC).timestamp()
    assert payload["exp"] > now_ts + 1700  # > ~28min
    assert payload["exp"] < now_ts + 1900  # < ~32min


def test_create_access_token_ttl_capped(mock_config):
    """超过 max_token_ttl_days 的 TTL 被截断。"""
    # 先检查配置的 max_token_ttl_days，确保我们构造的 delta 超过它
    max_days = mock_config.security.max_token_ttl_days
    huge_delta = timedelta(days=max_days + 100)
    token = create_access_token({"sub": "u1"}, expires_delta=huge_delta)
    payload = pyjwt.decode(
        token,
        mock_config.security.secret_key,
        algorithms=[mock_config.security.algorithm],
    )
    now_ts = datetime.now(UTC).timestamp()
    max_exp = now_ts + max_days * 86400
    # 截断后的 exp 不应超过 max_ttl（允许小余量）
    assert payload["exp"] <= max_exp + 60


def test_create_access_token_password_reset_no_kid(mock_config):
    """password_reset 类型 token 不包含 kid header。"""
    token = create_access_token({"sub": "u1", "type": "password_reset"})
    header = pyjwt.get_unverified_header(token)
    assert "kid" not in header


# ─── create_refresh_token ───


def test_create_refresh_token_basic(mock_config):
    token = create_refresh_token({"sub": "user1"})
    payload = pyjwt.decode(
        token,
        mock_config.security.secret_key,
        algorithms=[mock_config.security.algorithm],
    )
    assert payload["sub"] == "user1"
    assert payload["type"] == "refresh"
    assert "jti" in payload
    assert "iat" in payload
    assert "exp" in payload


def test_create_refresh_token_has_kid(mock_config):
    token = create_refresh_token({"sub": "u1"})
    header = pyjwt.get_unverified_header(token)
    assert header["kid"] == mock_config.security.key_id


# ─── verify_token ───


def test_verify_token_valid(mock_config):
    """合法 access token 验证成功。"""
    token = create_access_token({"sub": "user1", "role": "admin"})
    with (
        patch(_REVOKED_PATCH, return_value=False),
        patch(_SESSION_PATCH, return_value=True),
    ):
        payload = verify_token(token)
    assert payload["sub"] == "user1"
    assert payload["type"] == "access"


def test_verify_token_expired(mock_config):
    """过期 token 验证失败。"""
    token = create_access_token(
        {"sub": "u1"},
        expires_delta=timedelta(seconds=-10),
    )
    with (
        patch(_REVOKED_PATCH, return_value=False),
        patch(_SESSION_PATCH, return_value=True),
    ):
        with pytest.raises(pyjwt.PyJWTError):
            verify_token(token)


def test_verify_token_wrong_type(mock_config):
    """refresh token 用 access 类型验证失败。"""
    token = create_refresh_token({"sub": "u1"})
    with (
        patch(_REVOKED_PATCH, return_value=False),
        patch(_SESSION_PATCH, return_value=True),
    ):
        with pytest.raises(pyjwt.PyJWTError, match="Expected access"):
            verify_token(token, token_type="access")


def test_verify_token_revoked(mock_config):
    """已撤销 jti 的 token 验证失败。"""
    token = create_access_token({"sub": "u1"})
    with (
        patch(_REVOKED_PATCH, return_value=True),
        patch(_SESSION_PATCH, return_value=True),
    ):
        with pytest.raises(pyjwt.PyJWTError, match="revoked"):
            verify_token(token)


def test_verify_token_session_inactive(mock_config):
    """session 不活跃时验证失败。"""
    token = create_access_token({"sub": "u1"})
    with (
        patch(_REVOKED_PATCH, return_value=False),
        patch(_SESSION_PATCH, return_value=False),
    ):
        with pytest.raises(pyjwt.PyJWTError, match="superseded"):
            verify_token(token)


def test_verify_token_no_jti_rejected(mock_config):
    """无 jti 的遗留 token 被拒绝。"""
    # 手动构造无 jti 的 token
    payload = {
        "sub": "u1",
        "type": "access",
        "exp": datetime.now(UTC) + timedelta(hours=1),
        "iat": datetime.now(UTC),
    }
    token = pyjwt.encode(
        payload,
        mock_config.security.secret_key,
        algorithm=mock_config.security.algorithm,
    )
    with (
        patch(_REVOKED_PATCH, return_value=False),
        patch(_SESSION_PATCH, return_value=True),
    ):
        with pytest.raises(pyjwt.PyJWTError, match="no jti"):
            verify_token(token)


def test_verify_token_iat_in_future(mock_config):
    """iat 在未来的 token 被拒绝。"""
    future_iat = datetime.now(UTC).timestamp() + 120
    payload = {
        "sub": "u1",
        "type": "access",
        "jti": "test-jti",
        "exp": datetime.now(UTC) + timedelta(hours=1),
        "iat": future_iat,
    }
    token = pyjwt.encode(
        payload,
        mock_config.security.secret_key,
        algorithm=mock_config.security.algorithm,
    )
    with (
        patch(_REVOKED_PATCH, return_value=False),
        patch(_SESSION_PATCH, return_value=True),
    ):
        with pytest.raises(pyjwt.PyJWTError):
            verify_token(token)


def test_verify_token_password_reset_skips_session(mock_config):
    """password_reset token 跳过 session 检查。"""
    token = create_access_token({"sub": "u1", "type": "password_reset"})
    # 即使 session 不活跃，password_reset 也能验证
    with patch(_REVOKED_PATCH, return_value=False):
        payload = verify_token(token, token_type="password_reset")
    assert payload["sub"] == "u1"
    assert payload["type"] == "password_reset"


# ─── decode_token ───


def test_decode_token_basic(mock_config):
    """基本解码。"""
    token = create_access_token({"sub": "u1"})
    payload = decode_token(token)
    assert payload["sub"] == "u1"


def test_decode_token_verify_exp_false(mock_config):
    """verify_exp=False 不验证过期。"""
    token = create_access_token(
        {"sub": "u1"},
        expires_delta=timedelta(seconds=-10),
    )
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        payload = decode_token(token, verify_exp=False)
    assert payload["sub"] == "u1"


def test_decode_token_token_type_check(mock_config):
    """token_type 校验。"""
    token = create_refresh_token({"sub": "u1"})
    with pytest.raises(pyjwt.PyJWTError, match="type mismatch"):
        decode_token(token, token_type="access")


def test_decode_token_key_type_reset(mock_config):
    """key_type=reset 使用 reset 密钥。"""
    token = create_access_token({"sub": "u1", "type": "password_reset"})
    payload = decode_token(token, key_type="reset", token_type="password_reset")
    assert payload["sub"] == "u1"


def test_decode_token_key_type_access(mock_config):
    """key_type=access 使用 access 密钥。"""
    token = create_access_token({"sub": "u1"})
    payload = decode_token(token, key_type="access")
    assert payload["sub"] == "u1"


# ─── _resolve_secret_key / _resolve_previous_secret_key / _resolve_key_by_kid ───


def test_resolve_secret_key_short_key_rejected(mock_config, monkeypatch):
    """短密钥被拒绝。"""
    from edgelite.config import get_config

    cfg = get_config()
    short_key = "short"
    monkeypatch.setattr(cfg.security, "secret_key", short_key)
    from edgelite.security.jwt import _resolve_secret_key

    with pytest.raises(ValueError, match="长度不足"):
        _resolve_secret_key()


def test_min_secret_key_bytes_value():
    assert _MIN_SECRET_KEY_BYTES == 32


def test_resolve_key_by_kid_current(mock_config):
    """kid = 当前 key_id 使用当前密钥。"""
    from edgelite.security.jwt import _resolve_key_by_kid

    key = _resolve_key_by_kid(mock_config.security.key_id)
    assert key == mock_config.security.secret_key


def test_resolve_key_by_kid_none_uses_current(mock_config):
    """kid=None 使用当前密钥。"""
    from edgelite.security.jwt import _resolve_key_by_kid

    key = _resolve_key_by_kid(None)
    assert key == mock_config.security.secret_key


def test_resolve_key_by_kid_unknown_rejected(mock_config):
    """未知 kid 被拒绝。"""
    from edgelite.security.jwt import _resolve_key_by_kid

    with pytest.raises(pyjwt.PyJWTError, match="Unknown key ID"):
        _resolve_key_by_kid("nonexistent-kid")


def test_resolve_key_by_kid_previous(mock_config, monkeypatch):
    """旧 kid 使用旧密钥。"""
    from edgelite.security.jwt import _resolve_key_by_kid

    prev_key = "previous-secret-key-for-testing-only-32chars!"
    monkeypatch.setattr(mock_config.security, "secret_key_previous", prev_key)
    monkeypatch.setattr(mock_config.security, "previous_key_id", "old-kid")
    key = _resolve_key_by_kid("old-kid")
    assert key == prev_key


def test_resolve_key_by_kid_previous_removed(mock_config, monkeypatch):
    """旧 kid 但旧密钥已清除时报错。"""
    from edgelite.security.jwt import _resolve_key_by_kid

    monkeypatch.setattr(mock_config.security, "secret_key_previous", None)
    monkeypatch.setattr(mock_config.security, "previous_key_id", "old-kid")
    with pytest.raises(pyjwt.PyJWTError, match="retired key"):
        _resolve_key_by_kid("old-kid")
