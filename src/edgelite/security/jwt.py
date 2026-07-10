"""JWT Token生成与验证"""

from __future__ import annotations

import hashlib
import hmac
import logging
import uuid
from datetime import UTC, datetime, timedelta

import jwt
from jwt import PyJWTError as JWTError  # FIXED-P0: 替换python-jose(CVE-2024-33663/33664)为PyJWT

from edgelite.config import get_config

logger = logging.getLogger(__name__)

# FIXED-P2: 移除未使用的 _CONFIG_INITIALIZED 死代码

# FIXED-L01: JWT 算法白名单 - 仅允许 HMAC-SHA 系列算法
# 明确禁止 none 算法（安全风险）和非对称算法（配置复杂且易误用）
_ALLOWED_ALGORITHMS = frozenset({"HS256", "HS384", "HS512"})

# FIXED-P1: HMAC 密钥最小长度（字节），NIST SP 800-107 推荐 HMAC-SHA256 密钥 >= 32 字节
_MIN_SECRET_KEY_BYTES = 32
# FIXED-P1: iat 容差（秒），允许时钟漂移
_IAT_LEEWAY_SECONDS = 60


def _validate_algorithm(algorithm: str) -> str:
    """Validate JWT algorithm against whitelist.

    FIXED-L01: Prevent 'none' algorithm and other insecure configurations.

    Args:
        algorithm: Algorithm string from config

    Returns:
        Validated algorithm string

    Raises:
        ValueError: If algorithm is not in whitelist
    """
    if algorithm is None:
        raise ValueError(
            "JWT algorithm is not configured. "
            "Please set security.algorithm in your configuration. "
            "Allowed values: HS256, HS384, HS512"
        )

    # Case-insensitive check
    algorithm_upper = algorithm.upper()
    if algorithm_upper not in _ALLOWED_ALGORITHMS:
        raise ValueError(
            f"Invalid JWT algorithm '{algorithm}'. "
            f"Only HMAC-SHA algorithms are allowed: {', '.join(sorted(_ALLOWED_ALGORITHMS))}. "
            f"'none' algorithm is explicitly forbidden due to security risks."
        )

    return algorithm_upper


def _resolve_secret_key() -> str:
    config = get_config()
    # FIXED-L01: Validate algorithm before accepting secret_key
    _validate_algorithm(config.security.algorithm)
    if config.security.secret_key:
        # FIXED-P1: 校验密钥最小长度，短密钥易被暴力破解
        key_bytes = config.security.secret_key.encode("utf-8")
        if len(key_bytes) < _MIN_SECRET_KEY_BYTES:
            raise ValueError(
                f"security.secret_key 长度不足！当前 {len(key_bytes)} 字节，"
                f"最小要求 {_MIN_SECRET_KEY_BYTES} 字节（NIST SP 800-107 推荐）。"
                f"请使用足够长的随机密钥。"
            )
        return config.security.secret_key
    raise ValueError(
        "security.secret_key 未配置！必须在配置文件或环境变量中设置有效的 secret_key。"
        "未配置的 secret_key 会导致重启后所有 JWT Token 失效。"
    )


def _resolve_previous_secret_key() -> str | None:
    """FIXED: 获取轮换前的旧密钥，用于过渡期验证旧 token [2026-06-29]

    返回 None 表示未配置旧密钥（无轮换或轮换过渡期已结束）。
    """
    config = get_config()
    prev_key = config.security.secret_key_previous
    if prev_key and len(prev_key.encode("utf-8")) >= _MIN_SECRET_KEY_BYTES:
        return prev_key
    return None


def _resolve_key_by_kid(kid: str | None) -> str:
    """FIXED: 根据 JWT kid header 选择对应的签名密钥 [2026-06-29]

    密钥轮换流程：
    1. 旧 secret_key 移到 secret_key_previous，设置新 secret_key + key_id
    2. 过渡期内：新 token 用新 key 签发（kid=current key_id），
       旧 token 用 previous key 验证（kid=previous_key_id）
    3. 过渡期后：清除 secret_key_previous，旧 token 自然过期
    """
    config = get_config()
    current_kid = config.security.key_id
    previous_kid = config.security.previous_key_id or "default"

    if kid is None or kid == current_kid:
        return _resolve_secret_key()

    if kid == previous_kid:
        prev_key = _resolve_previous_secret_key()
        if prev_key is not None:
            return prev_key
        # 旧密钥已清除，旧 token 应自然过期（exp 会拒绝）
        raise JWTError(f"Token signed with retired key (kid={kid}), key has been removed")

    # 未知 kid — 可能是伪造或来自不兼容的部署
    raise JWTError(f"Unknown key ID (kid={kid}), possible forgery or misconfigured deployment")


def _get_reset_secret_key() -> str:
    # FIXED-P0: 原实现使用 sha256(base + ":password_reset") 简单拼接，
    # 缺乏密钥分离性。改用 HMAC-SHA256 提供正确的密钥派生，
    # 主密钥作为 HMAC key，域分隔符作为 message，确保 reset 密钥与主密钥独立。
    base = _resolve_secret_key()
    return hmac.new(base.encode(), b"password_reset", hashlib.sha256).hexdigest()


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    """创建Access Token.

    FIXED: 添加 expires_delta 上限检查，最大不超过 max_token_ttl_days 天。
    超过上限时自动截断并记录 warning 日志。
    FIXED-C02: 显式添加 iat 声明，用于密码修改后 Token 失效检查。
    FIXED-L03: Token 中的 role 字段仅用于快速参考，实际权限以数据库查询结果为准。
    角色变更在 API 调用时从数据库实时获取，确保权限变更立即生效。
    """
    config = get_config()
    to_encode = data.copy()
    if "jti" not in to_encode:
        to_encode["jti"] = str(uuid.uuid4())

    # FIXED: 应用过期时间上限
    if expires_delta is not None:
        max_ttl = timedelta(days=config.security.max_token_ttl_days)
        if expires_delta > max_ttl:
            logger.warning(
                "Token TTL (%.1f days) exceeds maximum allowed (%.1f days), truncating to max TTL for token type: %s",
                expires_delta.total_seconds() / 86400,
                config.security.max_token_ttl_days,
                data.get("type", "access"),
            )
            expires_delta = max_ttl

    now = datetime.now(UTC)
    expire = now + (expires_delta or timedelta(minutes=config.security.access_token_expire_minutes))
    # FIXED-C02: 显式添加 iat 用于密码修改后 Token 失效检查
    to_encode.update({"exp": expire, "iat": now, "type": data.get("type", "access")})
    key = _get_reset_secret_key() if data.get("type") == "password_reset" else _resolve_secret_key()
    # FIXED: 添加 kid header 支持密钥轮换 [2026-06-29]
    headers = {"kid": config.security.key_id} if data.get("type") != "password_reset" else None
    return jwt.encode(to_encode, key, algorithm=config.security.algorithm, headers=headers)


def create_refresh_token(data: dict, expires_delta: timedelta | None = None) -> str:
    """创建Refresh Token.

    FIXED-C02: 显式添加 iat 声明，用于密码修改后 Token 失效检查。
    """
    config = get_config()
    to_encode = data.copy()
    if "jti" not in to_encode:
        to_encode["jti"] = str(uuid.uuid4())
    now = datetime.now(UTC)
    expire = now + (expires_delta or timedelta(days=config.security.refresh_token_expire_days))
    # FIXED-C02: 显式添加 iat 用于密码修改后 Token 失效检查
    to_encode.update({"exp": expire, "iat": now, "type": "refresh"})
    # FIXED: 添加 kid header 支持密钥轮换 [2026-06-29]
    headers = {"kid": config.security.key_id}
    return jwt.encode(to_encode, _resolve_secret_key(), algorithm=config.security.algorithm, headers=headers)


def verify_token(token: str, token_type: str = "access") -> dict:
    """验证Token，返回payload。验证失败抛出JWTError"""
    config = get_config()
    # FIXED: 支持 kid header 密钥轮换 — 先解析 header 获取 kid，再选择对应密钥 [2026-06-29]
    if token_type == "password_reset":
        key = _get_reset_secret_key()
    else:
        try:
            unverified_header = jwt.get_unverified_header(token)
            kid = unverified_header.get("kid")
        except Exception:
            kid = None  # 旧 token 无 kid header，使用当前密钥验证
        key = _resolve_key_by_kid(kid)
    payload = jwt.decode(token, key, algorithms=[config.security.algorithm])
    if payload.get("type") != token_type:
        raise JWTError(f"Expected {token_type} token, got {payload.get('type')}")

    # FIXED-P1: 校验 iat 不在未来（防伪造），允许 _IAT_LEEWAY_SECONDS 秒时钟漂移
    iat = payload.get("iat")
    if iat is not None:
        now_ts = datetime.now(UTC).timestamp()
        if iat > now_ts + _IAT_LEEWAY_SECONDS:
            raise JWTError("Token iat is in the future (possible forgery)")

    jti = payload.get("jti", "")
    if jti:
        from edgelite.security.token_revocation import is_token_revoked

        if is_token_revoked(jti):
            raise JWTError("Token has been revoked")
    else:
        # FIXED-P2: 迁移期已于 2026-06-09 结束，直接拒绝无 jti 的遗留 Token
        raise JWTError("Token has no jti field (legacy format rejected)")

    # LP-09: 并发登录控制 - 检查 jti 是否在用户活跃 session 中
    # fail-open: 用户无活跃 session 记录（如重启后）时放行，避免重启后所有 token 失效
    # FIXED(致命): 原问题-password_reset token未注册session，session检查导致密码重置完全失效;
    # 修复-password_reset类型token跳过session检查（一次性短生命周期token，不受session管理约束）
    if token_type != "password_reset":
        user_id = payload.get("sub")
        if user_id and jti:
            from edgelite.security.session_manager import is_session_active

            if not is_session_active(user_id, jti):
                raise JWTError("Token session has been superseded by a newer login")
    return payload


def decode_token(
    token: str,
    verify_exp: bool = True,
    token_type: str | None = None,
    key_type: str | None = None,
) -> dict | None:
    """解码Token。默认验证过期，可选校验token类型。

    FIXED-L01: 移除双密钥回退机制，每个 Token 类型严格使用对应密钥解码。

    Args:
        token: JWT token string
        verify_exp: 是否验证过期时间，默认 True
        token_type: 可选，期望的 token 类型（如 "access", "refresh", "password_reset"）。
                   如果提供，解码后会校验 payload["type"] 是否匹配。
        key_type: 可选，指定密钥类型 ("access" 或 "reset")。
                  如果提供，使用对应密钥解码。
                  如果不提供，根据 token_type 自动推断。

    Returns:
        解码后的 payload dict，失败时返回 None。

    Raises:
        JWTError: 当 token_type 校验失败或密钥类型不匹配时。
    """
    import warnings

    config = get_config()
    if not verify_exp:
        warnings.warn("decode_token(verify_exp=False) 仅用于调试，生产环境请勿使用", stacklevel=2)

    # FIXED-L01: 根据 key_type 选择密钥，不再回退
    if key_type == "reset":
        key = _get_reset_secret_key()
    elif key_type == "access":
        key = _resolve_secret_key()
    else:
        # 根据 token_type 推断密钥类型
        key = _get_reset_secret_key() if token_type == "password_reset" else _resolve_secret_key()

    payload = jwt.decode(
        token,
        key,
        algorithms=[config.security.algorithm],
        options={"verify_exp": verify_exp},
    )

    # FIXED: 校验 token_type
    if token_type is not None:
        actual_type = payload.get("type")
        if actual_type != token_type:
            raise JWTError(f"Token type mismatch: expected '{token_type}', got '{actual_type}'")

    return payload
