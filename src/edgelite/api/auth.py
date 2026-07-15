"""认证API路由"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import time

# FIXED(P3): 原问题-F401未使用导入collections.defaultdict; 修复-删除该导入行
from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from jwt import PyJWTError as JWTError  # FIXED-P0: 替换python-jose为PyJWT

from edgelite.api.deps import AuditServiceDep, CurrentUser, DatabaseDep, get_current_user
from edgelite.api.error_codes import AuthErrors
from edgelite.config import (
    get_config,  # FIXED-P0: _check_account_lockout/_record_lockout_failure 需要 get_config
)

# FIXED(P3): 原问题-F401未使用导入_AUTH_ATTEMPTS_LIMIT; 修复-从导入中移除该名称
from edgelite.constants import _AUTH_MAX_ATTEMPTS, _AUTH_PASSWORD_MAX_LENGTH
from edgelite.models.common import ApiResponse
from edgelite.models.user import LoginRequest, TokenResponse, UserInfoResponse
from edgelite.security.jwt import create_access_token, create_refresh_token, verify_token
from edgelite.security.password import verify_password
from edgelite.storage.sqlite_repo import RateLimitRepo, UserRepo

_AUTH_COOKIE_ACCESS = "edgelite_access"
_AUTH_COOKIE_REFRESH = "edgelite_refresh"

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["Auth"])


def _is_dev_mode() -> bool:
    """Check if running in development mode (HTTP localhost)."""
    return os.environ.get("DEV_MODE", "").lower() in ("true", "1", "yes")


def _set_token_cookies(response: Response, access_token: str, refresh_token: str) -> None:
    """LP-02: 设置 HttpOnly Cookie 存储 Token，防止 XSS 窃取。

    access_token: path=/ 覆盖 API 和 WS 端点
    refresh_token: path=/api/v1/auth 限制仅 auth 路径可访问
    """
    secure = not _is_dev_mode()
    samesite = "lax" if _is_dev_mode() else "strict"
    config = get_config()
    access_max_age = config.security.access_token_expire_minutes * 60
    refresh_max_age = config.security.refresh_token_expire_days * 86400

    response.set_cookie(
        key=_AUTH_COOKIE_ACCESS,
        value=access_token,
        httponly=True,
        secure=secure,
        samesite=samesite,
        max_age=access_max_age,
        path="/",
    )
    response.set_cookie(
        key=_AUTH_COOKIE_REFRESH,
        value=refresh_token,
        httponly=True,
        secure=secure,
        samesite=samesite,
        max_age=refresh_max_age,
        path="/api/v1/auth",
    )


def _clear_token_cookies(response: Response) -> None:
    """LP-02: 清除 Token Cookie。"""
    response.delete_cookie(key=_AUTH_COOKIE_ACCESS, path="/")
    response.delete_cookie(key=_AUTH_COOKIE_REFRESH, path="/api/v1/auth")


# FIXED-H03: 使用 SQLite 持久化存储替代内存存储，支持多 worker 部署
_login_window_seconds = 300
_MAX_LOGIN_attempts = _AUTH_MAX_ATTEMPTS
_MIN_PASSWORD_LENGTH = 8
_MAX_PASSWORD_LENGTH = _AUTH_PASSWORD_MAX_LENGTH
_WEAK_PASSWORDS = {  # FIXED-P0: 补充常见极弱密码
    "password",
    "123456",
    "12345678",
    "123456789",
    "1234567890",
    "admin",
    "admin123",
    "admin888",
    "root",
    "root123",
    "test",
    "test123",
    "guest",
    "guest123",
    "qwerty",
    "qwerty12",
    "abc123",
    "abc12345",
    "password1",
    "iloveyou",
    "sunshine1",
    "welcome1",
    "letmein",
    "master",
    "monkey",
    "dragon",
}


async def _check_login_rate(ip: str) -> None:
    """Check if IP is rate limited. FIXED-H03: Uses persistent storage."""
    # FIXED(P3): 原问题-F841未使用局部变量window_start(及仅服务于它的now); 修复-删除死代码
    # Check current attempt count from persistent storage
    attempt_count = await RateLimitRepo.check_login_rate(ip)

    if attempt_count >= _MAX_LOGIN_attempts:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=AuthErrors.RATE_LIMITED,
        )


async def _record_login_attempt(ip: str) -> None:
    """Record a failed login attempt. FIXED-H03: Uses persistent storage."""
    await RateLimitRepo.record_login_attempt(ip)


async def _check_account_lockout(username: str, ip: str) -> None:
    """Check if the account is locked for this username+IP combination.

    FIXED-H03: Uses persistent storage for multi-worker deployments.
    """
    now = time.time()
    lockout_info = await RateLimitRepo.get_lockout_info(username, ip)

    if lockout_info:
        locked_until = lockout_info.get("locked_until", 0)
        if locked_until > now:
            # R5-F-04 修复(致命): 不向客户端返回剩余锁定秒数，避免攻击者推算锁定窗口
            remaining = int(locked_until - now)
            logger.warning("Account locked: username=%s ip=%s remaining=%ds", username, ip, remaining)
            raise HTTPException(
                status_code=423,
                detail=AuthErrors.ACCOUNT_LOCKED,
            )


async def _record_lockout_failure(username: str, ip: str) -> None:
    """Record a failed login attempt for account lockout tracking.

    FIXED-H03: Uses persistent storage for multi-worker deployments.
    """
    await RateLimitRepo.record_lockout_failure(username, ip)


async def _clear_lockout(username: str, ip: str) -> None:
    """Clear lockout on successful login. FIXED-H03: Uses persistent storage."""
    await RateLimitRepo.clear_lockout(username, ip)


def _is_ip_in_cidr(ip_str: str, cidr: str) -> bool:
    """Check if an IP address is in a CIDR range."""
    try:
        ip = ipaddress.ip_address(ip_str)
        network = ipaddress.ip_network(cidr, strict=False)
        return ip in network
    except ValueError:
        return False


def _is_trusted_proxy(client_host: str, trusted_proxies: list[str]) -> bool:
    """Check if the direct client IP is in the trusted proxies list."""
    if not client_host or not trusted_proxies:
        return False
    for proxy in trusted_proxies:
        proxy = proxy.strip()
        if "/" in proxy:
            # CIDR notation
            if _is_ip_in_cidr(client_host, proxy):
                return True
        else:
            # Exact match
            if client_host == proxy:
                return True
    return False


def _get_client_ip(request: Request) -> str:
    """Get the real client IP address with trusted proxy support.

    FIXED-P0: Only trust X-Forwarded-For/X-Real-IP headers when the direct client
    is from a trusted proxy. This prevents IP spoofing attacks that could bypass
    rate limiting and account lockout mechanisms.

    Args:
        request: FastAPI Request object

    Returns:
        Real client IP address
    """
    # Get trusted proxies from config
    try:
        config = get_config()
        trusted_proxies = getattr(config.server, "trusted_proxies", []) if hasattr(config, "server") else []
    except Exception as e:
        trusted_proxies = []
        logger.warning("Failed to read trusted_proxies config: %s", e)  # FIXED-P1: 原问题-安全配置失效不可知

    # Get direct client IP
    direct_client = request.client.host if request.client else None

    # Only trust proxy headers if direct client is a trusted proxy
    if trusted_proxies and direct_client and _is_trusted_proxy(direct_client, trusted_proxies):
        # Check X-Forwarded-For
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            ip_str = forwarded.split(",")[0].strip()
            if ip_str:
                try:
                    ipaddress.ip_address(ip_str)
                    return ip_str
                except ValueError:
                    pass

        # Check X-Real-IP
        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            try:
                ipaddress.ip_address(real_ip)
                return real_ip
            except ValueError:
                pass

    # Return direct client IP (safe default - no spoofing possible)
    return direct_client if direct_client else "unknown"


@router.post("/login", response_model=ApiResponse[TokenResponse])
async def login(req: LoginRequest, request: Request, db: DatabaseDep, audit_svc: AuditServiceDep):
    """用户登录端点。

    支持用户名密码认证，包含全局失败速率检查、账户级锁定、
    初次登录强制改密、审计日志记录等安全机制。
    成功后返回 access_token 和 refresh_token（通过 HttpOnly Cookie 下发）。
    """
    try:
        client_ip = _get_client_ip(request)

        # FIXED-M03: Check global failure rate first
        global_failure_count = await RateLimitRepo.check_global_failure_rate()
        config = get_config()
        if global_failure_count >= config.security.global_failure_rate_threshold:
            logger.warning(
                "Global login failure rate exceeded: %d/min (threshold: %d)",
                global_failure_count,
                config.security.global_failure_rate_threshold,
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"{AuthErrors.RATE_LIMITED}:global_rate_exceeded",
            )

        # FIXED-M03: Check global account lockout (username-level, IP-agnostic)
        global_lockout = await RateLimitRepo.check_global_account_lockout(req.username)
        if global_lockout:
            # R5-F-04 修复(致命): 原响应 detail=f"{ACCOUNT_LOCKED}:{remaining}" 泄漏剩余锁定秒数，
            # 攻击者可据此推算锁定窗口、调整爆破节奏。改为不返回剩余时间，仅返回锁定状态。
            # 日志保留 remaining 便于运维排查。
            remaining = int(global_lockout.get("locked_until", 0) - time.time())
            logger.warning("Global lockout active for username %s, remaining: %ds", req.username, remaining)
            raise HTTPException(
                status_code=423,
                detail=AuthErrors.ACCOUNT_LOCKED,
            )

        await _check_login_rate(client_ip)
        await _check_account_lockout(req.username, client_ip)

        async with db.get_session() as session:
            repo = UserRepo(session, db.write_lock)
            user = await repo.get_by_username_with_password(req.username)

        if user is None:
            # FIXED(严重): 原问题-dummy hash含非法字符X，verify_password立即返回False不做bcrypt计算，
            # 导致时序攻击可枚举用户名; 修复-使用合法bcrypt hash确保执行完整bcrypt比较
            # 合法bcrypt hash: $2b$14$开头的60字符hash（对"dummy"密码的bcrypt哈希）
            verify_password(req.password, "$2b$14$LZUQcaDskZGqC9KWXaQs5O1Ry0LXdI.vEBRexe77byPJe3dYhKpsC")
            await RateLimitRepo.record_global_failure(req.username, client_ip)
            await RateLimitRepo.record_global_account_failure(req.username)
            await _record_login_attempt(client_ip)
            await _record_lockout_failure(req.username, client_ip)
            try:
                from edgelite.services.audit_service import AuditAction

                await audit_svc.log(
                    AuditAction.LOGIN_FAILED, username=req.username, ip_address=client_ip, status="failed"
                )
            except Exception as e:
                logger.warning("Audit log failed: %s", e)
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=AuthErrors.INVALID_CREDENTIALS)

        if not verify_password(req.password, user["password"]):
            await RateLimitRepo.record_global_failure(req.username, client_ip)
            await RateLimitRepo.record_global_account_failure(req.username)
            await _record_login_attempt(client_ip)
            await _record_lockout_failure(req.username, client_ip)
            try:
                from edgelite.services.audit_service import AuditAction

                await audit_svc.log(
                    AuditAction.LOGIN_FAILED, username=req.username, ip_address=client_ip, status="failed"
                )
            except Exception as e:
                logger.warning("Audit log failed: %s", e)
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=AuthErrors.INVALID_CREDENTIALS)

        # FIXED(安全): 用户枚举防护 - 禁用用户也返回统一的 INVALID_CREDENTIALS，避免通过状态码差异枚举用户名
        if not user["enabled"]:
            await RateLimitRepo.record_global_failure(req.username, client_ip)
            await RateLimitRepo.record_global_account_failure(req.username)
            try:
                from edgelite.services.audit_service import AuditAction

                await audit_svc.log(
                    AuditAction.LOGIN_FAILED, username=req.username, ip_address=client_ip, status="disabled"
                )
            except Exception as e:
                logger.warning("Audit log failed: %s", e)
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=AuthErrors.INVALID_CREDENTIALS)

        # Clear lockouts on successful login (including global)
        await _clear_lockout(req.username, client_ip)
        await RateLimitRepo.clear_global_account_lockout(req.username)

        # R5-F-02 修复(致命): 初始管理员密码文件（含明文密码）删除失败时仅 warning 即放行登录，
        # 攻击者可继续读取该文件获取明文密码。修复-删除失败时拒绝登录并要求运维介入。
        _pw_file = os.path.join(os.path.dirname(get_config().database.sqlite_path), ".initial_admin_password")
        if os.path.exists(_pw_file):
            try:
                os.remove(_pw_file)
                logger.info("Initial admin password file deleted after successful login")
            except OSError as e:
                # 删除失败可能因权限/文件系统只读，明文密码仍残留磁盘，必须拒绝登录
                logger.error(
                    "Failed to remove initial admin password file %s: %s. "
                    "Refusing login to prevent plaintext password leak. "
                    "Remove the file manually or fix permissions before retrying.",
                    _pw_file,
                    e,
                )
                # R6-F-01 修复(致命): 原清理块引用 new_jtis（在下方 token 签发后才定义），
                # 导致 NameError 被 except Exception: pass 静默吞没。
                # 此时 token 尚未签发，无需撤销 session，直接拒绝登录即可。
                raise HTTPException(
                    status_code=500,
                    detail="Initial admin password file could not be removed; contact administrator",
                ) from e

        access_token = create_access_token(
            data={"sub": user["user_id"], "username": user["username"], "role": user["role"]}
        )
        refresh_token = create_refresh_token(
            data={"sub": user["user_id"], "username": user["username"], "role": user["role"]}
        )

        # LP-09: 并发登录控制 - 撤销该用户旧 session，注册新 session
        from edgelite.security.jwt import decode_token
        from edgelite.security.session_manager import revoke_old_sessions

        access_payload = decode_token(access_token, verify_exp=False, token_type="access")
        refresh_payload = decode_token(refresh_token, verify_exp=False, token_type="refresh")
        new_jtis = []
        if access_payload and access_payload.get("jti"):
            new_jtis.append(access_payload["jti"])
        if refresh_payload and refresh_payload.get("jti"):
            new_jtis.append(refresh_payload["jti"])
        # FIXED(一般): 原问题-session注册失败后继续返回token，用户拿到token后所有API调用401;
        # 修复-确保新jti注册成功，注册失败则返回500
        if new_jtis:
            try:
                await revoke_old_sessions(user["user_id"], new_jtis)
            except Exception as e:
                logger.warning("LP-09: Failed to revoke old sessions for user %s: %s", user["user_id"], e)
                # 即使撤销旧session失败，也要确保新jti已注册
                from edgelite.security.session_manager import register_session

                for jti in new_jtis:
                    try:
                        register_session(user["user_id"], jti)
                    except Exception as exc:  # FIXED(P2): 原问题-B904异常链丢失; 修复-添加as exc与from exc
                        logger.error(
                            "LP-09: Failed to register new session, login will fail for user %s", user["user_id"]
                        )
                        raise HTTPException(status_code=500, detail="Session registration failed") from exc

        config = get_config()

        try:
            from edgelite.services.audit_service import AuditAction

            await audit_svc.log(
                AuditAction.LOGIN, user_id=user["user_id"], username=user["username"], ip_address=client_ip
            )
        except Exception as e:
            logger.warning("Audit log failed: %s", e)

        # Generate CSRF token for this session
        from edgelite.middleware.csrf import generate_csrf_token

        csrf_token = generate_csrf_token(user["user_id"])

        from fastapi.responses import JSONResponse

        response_data = ApiResponse(
            data=TokenResponse(
                access_token=access_token,
                refresh_token=refresh_token,
                expires_in=config.security.access_token_expire_minutes * 60,
                csrf_token=csrf_token,
            )
        )
        response = JSONResponse(content=response_data.model_dump())
        response.headers["X-CSRF-Token"] = csrf_token
        # LP-02: 设置 HttpOnly Cookie 存储 Token
        _set_token_cookies(response, access_token, refresh_token)
        return response
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Login failed: %s", e)
        raise HTTPException(status_code=500, detail=AuthErrors.LOGIN_FAILED) from e


@router.post("/refresh", response_model=ApiResponse[TokenResponse])
async def refresh_token(request: Request, db: DatabaseDep, refresh: str = Body(None, embed=True)):
    """刷新 access_token。

    使用 refresh_token 换取新的 access_token，支持从请求体或 HttpOnly Cookie 读取。
    旧 refresh_token 会被吊销，实现轮换防重放。
    """
    # LP-02: 优先从请求体读取 refresh_token，fallback 到 HttpOnly Cookie
    if not refresh:
        refresh = request.cookies.get(_AUTH_COOKIE_REFRESH)
    if not refresh:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=AuthErrors.REFRESH_TOKEN_INVALID)
    try:
        payload = verify_token(refresh, token_type="refresh")
    except (JWTError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=AuthErrors.REFRESH_TOKEN_INVALID) from None

    # R5-F-01 修复(致命): refresh_token 端点未校验 jti 是否已撤销，登出/管理员撤销后
    # 旧 refresh token 仍可换发新 access token，形成会话绕过链。
    # 修复-verify_token 仅校验签名/过期/类型，需显式调用 is_token_revoked 检查黑名单。
    old_jti = payload.get("jti")
    if old_jti:
        try:
            from edgelite.security.token_revocation import is_token_revoked

            if is_token_revoked(old_jti):
                logger.warning("Refresh token rejected (revoked jti=%s)", old_jti)
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=AuthErrors.REFRESH_TOKEN_INVALID,
                )
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to check refresh token revocation: %s", e)
            # 校验服务异常时拒绝请求，避免放行已撤销 token
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=AuthErrors.REFRESH_TOKEN_INVALID,
            ) from e

    # FIXED: 数据库操作和token创建无异常保护
    try:
        async with db.get_session() as session:
            repo = UserRepo(session, db.write_lock)
            user = await repo.get_by_username(payload.get("username", ""))
        if user is None or not user["enabled"]:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=AuthErrors.USER_NOT_FOUND)

        # FIXED(严重): 原问题-refresh token未检查password_changed_at，密码修改后旧token仍可用;
        # 修复-检查token签发时间是否早于密码修改时间
        password_changed_at = user.get("password_changed_at")
        if password_changed_at:
            token_iat = payload.get("iat", 0)
            from datetime import datetime

            if isinstance(password_changed_at, str):
                try:
                    pwd_changed_ts = datetime.fromisoformat(password_changed_at).timestamp()
                except (ValueError, TypeError):
                    pwd_changed_ts = 0
            elif isinstance(password_changed_at, datetime):
                pwd_changed_ts = password_changed_at.timestamp()
            else:
                pwd_changed_ts = 0
            if token_iat < pwd_changed_ts:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=AuthErrors.TOKEN_PASSWORD_CHANGED,
                )

        current_role = user["role"]

        # FIXED: 原问题-payload["sub"]硬访问可能KeyError(JWT被篡改)，改为.get()加校验
        sub = payload.get("sub")
        username = payload.get("username")
        if not sub or not username:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=AuthErrors.REFRESH_TOKEN_INVALID)

        access_token = create_access_token(data={"sub": sub, "username": username, "role": current_role})
        new_refresh = create_refresh_token(data={"sub": sub, "username": username, "role": current_role})

        old_exp = payload.get("exp")
        # 注: old_jti 已在上方 R5-F-01 修复中提前获取并校验撤销状态

        # LP-09: 先注册新 session，成功后再撤销旧 session
        # FIXED(严重-R2): 原问题-先撤销旧session再注册新session，若register_session失败
        # 则旧session已移除但新session未注册，用户被永久锁定无法使用新token
        # 修复-反转顺序：先注册新session，成功后再撤销旧token和移除旧session
        try:
            from edgelite.security.jwt import decode_token
            from edgelite.security.session_manager import register_session

            new_access_payload = decode_token(access_token, verify_exp=False, token_type="access")
            new_refresh_payload = decode_token(new_refresh, verify_exp=False, token_type="refresh")
            if new_access_payload and new_access_payload.get("jti"):
                register_session(sub, new_access_payload["jti"])
            if new_refresh_payload and new_refresh_payload.get("jti"):
                register_session(sub, new_refresh_payload["jti"])
        except Exception as e:
            logger.error("LP-09: Failed to register new session on refresh for user %s: %s", sub, e, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=AuthErrors.REFRESH_TOKEN_INVALID
            ) from e  # FIXED-B904

        # 新 session 注册成功后，安全地撤销旧 token 和移除旧 session
        if old_jti:
            from edgelite.security.token_revocation import revoke_token

            revoke_token(old_jti, old_exp)
            from edgelite.security.session_manager import remove_session

            remove_session(sub, old_jti)

        config = get_config()

        # 生成新的CSRF token，防止token刷新后CSRF token丢失
        from edgelite.middleware.csrf import generate_csrf_token

        new_csrf_token = generate_csrf_token(sub)

        from fastapi.responses import JSONResponse

        response_data = ApiResponse(
            data=TokenResponse(
                access_token=access_token,
                refresh_token=new_refresh,
                expires_in=config.security.access_token_expire_minutes * 60,
                csrf_token=new_csrf_token,
            )
        )
        response = JSONResponse(content=response_data.model_dump())
        response.headers["X-CSRF-Token"] = new_csrf_token
        # LP-02: 更新 HttpOnly Cookie 中的 Token
        _set_token_cookies(response, access_token, new_refresh)
        return response
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Token refresh failed: %s", e)  # FIXED-P3: 中文日志→英文
        raise HTTPException(status_code=500, detail=AuthErrors.LOGIN_FAILED) from e


@router.get("/me", response_model=ApiResponse[UserInfoResponse])
async def get_current_user_info(user: CurrentUser, db: DatabaseDep):
    """获取当前登录用户的详细信息，包含角色和是否需要强制改密。"""
    try:
        from edgelite.storage.sqlite_repo import UserRepo

        must_change = False
        async with db.get_session() as session:
            repo = UserRepo(session, db.write_lock)
            db_user = await repo.get_by_username(user["username"])
            if db_user:
                must_change = db_user.get("must_change_password", False)
        return ApiResponse(
            data={
                "user_id": user["user_id"],
                "username": user["username"],
                "role": user["role"],
                "must_change_password": must_change,
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Get user info failed: %s", e)  # FIXED-P3: 中文日志→英文
        raise HTTPException(status_code=500, detail=AuthErrors.LOGIN_FAILED) from e


@router.post("/change-password", response_model=ApiResponse)
async def change_password(
    db: DatabaseDep,
    old_password: str = Body(..., embed=True),
    new_password: str = Body(..., embed=True),
    user: dict = Depends(get_current_user),
    audit_svc: AuditServiceDep = None,  # FIXED-M03: FastAPI dependency injection provides the value
    request: Request = None,
):
    """Change the current user's password.

    FIXED-M05: This endpoint uses Depends(get_current_user) instead of
    require_permission(Permission.XXX) by design. Reason: any authenticated
    user is allowed to change their own password — no specific role/permission
    is required. The security guarantee comes from:
    1. The user must be authenticated (valid access token)
    2. The user must provide the correct old password
    3. The operation only affects the authenticated user's own password
    This endpoint is also whitelisted in deps.py must_change_password check
    (allowed_paths) so that users forced to change password on first login
    can still access this endpoint.

    FIXED-T01: Password update and must_change_password flag are now
    updated within a single database session and single transaction.
    Previously they were split across two sessions (update_password and
    update_user), causing inconsistency if the second step failed after
    the first succeeded.
    """
    try:
        # Validate old password first (read-only, uses separate session for clarity)
        async with db.get_session() as session:
            repo = UserRepo(session, db.write_lock)
            db_user = await repo.get_by_username_with_password(user["username"])
        if db_user is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=AuthErrors.USER_NOT_FOUND)
        if not verify_password(old_password, db_user["password"]):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=AuthErrors.OLD_PASSWORD_WRONG)

        # Validate new password policy
        if len(new_password) < _MIN_PASSWORD_LENGTH:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=AuthErrors.PASSWORD_POLICY)
        if len(new_password) > _MAX_PASSWORD_LENGTH:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=AuthErrors.PASSWORD_TOO_LONG)
        if old_password == new_password:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=AuthErrors.PASSWORD_SAME_AS_OLD)
        has_letter = any(c.isalpha() for c in new_password)
        has_digit = any(c.isdigit() for c in new_password)
        if not (has_letter and has_digit):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=AuthErrors.PASSWORD_LETTER_AND_DIGIT)
        has_special = any(c in "!@#$%^&*()_+-=[]{}|;':\",./<>?`~" for c in new_password)
        if not has_special:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=AuthErrors.PASSWORD_NEED_SPECIAL)
        if new_password.lower() in _WEAK_PASSWORDS:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=AuthErrors.PASSWORD_POLICY)

        from edgelite.security.password import hash_password

        # FIXED-T01: Use atomic combined method so password update and
        # must_change_password flag change share one transaction. Either both
        # succeed or both roll back — no intermediate inconsistent state.
        async with db.get_session() as session:
            repo = UserRepo(session, db.write_lock)
            success = await repo.update_password_and_clear_flag(user["username"], hash_password(new_password))
            if not success:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=AuthErrors.USER_NOT_FOUND)

        # FIXED-P1: 原问题-修改密码后未撤销现有token和清理session，旧token在过期前仍可使用；
        # 修复-清除用户所有session并撤销对应token，强制重新登录
        try:
            import time as _time

            from edgelite.security.session_manager import clear_user_sessions
            from edgelite.security.token_revocation import revoke_token_async

            removed_jtis = clear_user_sessions(user["user_id"])
            for old_jti in removed_jtis:
                try:
                    await revoke_token_async(old_jti, _time.time() + 3600)
                except Exception as e:
                    logger.warning("Failed to revoke token after password change, jti=%s: %s", old_jti, e)
            if removed_jtis:
                logger.info("Password changed for user %s, revoked %d session(s)", user["username"], len(removed_jtis))
        except Exception as e:
            logger.warning("Session cleanup after password change failed: %s", e)

        try:
            from edgelite.services.audit_service import AuditAction

            if audit_svc:
                # 补充ip_address和user_agent用于审计追溯
                # FIX-EL-GENERAL: 改用 _get_client_ip 走可信代理逻辑，与 login/logout/forgot_password 保持一致
                ip_address = _get_client_ip(request) if request else None
                user_agent = request.headers.get("User-Agent") if request else None
                await audit_svc.log(
                    AuditAction.PASSWORD_CHANGE,
                    user_id=user["user_id"],
                    username=user["username"],
                    ip_address=ip_address,
                    user_agent=user_agent,
                )
        except Exception as e:
            logger.warning("Audit log failed: %s", e)
        return ApiResponse(data={"message": "password_changed"})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Change password failed: %s", e)  # FIXED-P3: 中文日志→英文
        raise HTTPException(status_code=500, detail=AuthErrors.PASSWORD_CHANGE_FAILED) from e


@router.post("/forgot-password", response_model=ApiResponse)
async def forgot_password(
    db: DatabaseDep,
    username: str = Body(..., embed=True),
    request: Request = None,
    audit_svc: AuditServiceDep = None,  # FIXED-M03: FastAPI dependency injection provides the value
):
    """Send password reset email to user (requires email configuration).

    FIXED: 添加审计日志记录具体错误类型，但对外仍返回统一消息防止用户名枚举。
    FIXED-H01: 添加独立的 IP 和用户名维度速率限制，防止滥用重置功能。
    """
    _UNIFIED_MESSAGE = "If the account exists, a reset email will be sent"
    _DEFAULT_MESSAGE = "Password reset request rate limited. Please try again later."

    # FIXED-P0: 删除不安全的局部_get_client_ip，改用模块级版本（含_is_trusted_proxy验证）
    client_ip = _get_client_ip(request) if request else "unknown"

    # FIXED-H01: Check IP rate limit first
    from edgelite.storage.sqlite_repo import RateLimitRepo

    async def _check_rate_limits() -> tuple[bool, int]:
        """Check both IP and username rate limits.

        Returns:
            Tuple of (is_allowed, retry_after_seconds).
            is_allowed=False means rate limited, retry_after_seconds > 0.
        """
        # Check IP rate limit
        ip_count, ip_retry = await RateLimitRepo.check_password_reset_ip_rate(client_ip)
        if ip_count == -1:
            return False, ip_retry

        # Check username rate limit
        user_count, user_retry = await RateLimitRepo.check_password_reset_user_rate(username)
        if user_count == -1:
            return False, user_retry

        return True, 0

    async def _log_rate_limit_audit(limit_type: str, retry_after: int) -> None:
        """Log rate limit event for audit."""
        if audit_svc is None:
            return
        try:
            from edgelite.services.audit_service import AuditAction

            await audit_svc.log(
                AuditAction.FORGOT_PASSWORD_RATE_LIMITED,
                resource_type="password_reset",
                resource_id=username,
                ip_address=client_ip,
                after_value={"limit_type": limit_type, "retry_after": retry_after},
            )
        except Exception as e:
            logger.warning("Audit log failed: %s", e)

    # Perform rate limit checks
    allowed, retry_after = await _check_rate_limits()
    if not allowed:
        logger.warning(
            "Password reset rate limited: ip=%s username=%s retry_after=%ds", client_ip, username, retry_after
        )
        await _log_rate_limit_audit("ip_or_user", retry_after)
        return JSONResponse(
            status_code=429,
            content={
                "code": 429,
                "message": _DEFAULT_MESSAGE,
                "data": None,
                "error_code": AuthErrors.RATE_LIMITED,
            },
            headers={"Retry-After": str(retry_after)},
        )

    # Record this attempt for rate limiting (even if user doesn't exist)
    # This prevents username enumeration through timing differences
    ip_count = await RateLimitRepo.record_password_reset_ip_attempt(client_ip)
    if ip_count >= 5:  # From _AUTH_RESET_IP_MAX constant
        logger.warning("Password reset IP rate limit warning: ip=%s count=%d", client_ip, ip_count)
        await _log_rate_limit_audit("ip", 3600)

    user_count = await RateLimitRepo.record_password_reset_user_attempt(username)
    if user_count >= 3:  # From _AUTH_RESET_USER_MAX constant
        logger.warning("Password reset user rate limit warning: username=%s count=%d", username, user_count)
        await _log_rate_limit_audit("username", 3600)

    async def _log_audit(action: str, error_detail: str | None = None) -> None:
        if audit_svc is None:
            return
        try:
            from edgelite.services.audit_service import AuditAction

            details = {"username": username}
            if error_detail:
                details["error"] = error_detail
            await audit_svc.log(
                getattr(AuditAction, action),
                resource_type="password_reset",
                resource_id=username,
                ip_address=client_ip,  # FIXED-P2: 原问题-重复调用_get_client_ip(request)，复用已计算的client_ip
                after_value=details,
            )
        except Exception as e:
            logger.warning("Audit log failed: %s", e)

    try:
        async with db.get_session() as session:
            repo = UserRepo(session, db.write_lock)
            db_user = await repo.get_by_username(username)

        if db_user is None:
            # FIXED: 审计日志记录用户不存在，但不暴露给客户端
            logger.debug("Password reset requested for non-existent user")
            await _log_audit("FORGOT_PASSWORD_USER_NOT_FOUND")
            # R6-S-07 修复(严重): 时序侧信道——不存在用户立即返回，存在用户需 token 生成+SMTP
            # 发送（数百毫秒），攻击者可通过响应耗时差异枚举有效用户名。
            # 修复-对不存在用户执行等耗时 dummy 操作（hash 计算 + 随机延迟），模糊时序差异。
            import hashlib
            import secrets as _secrets

            _dummy = hashlib.pbkdf2_hmac("sha256", _secrets.token_bytes(32), _secrets.token_bytes(16), 100000)
            await asyncio.sleep(0.3 + _secrets.randbelow(200) / 1000.0)  # 模拟 SMTP 发送耗时
            return ApiResponse(message=_UNIFIED_MESSAGE)

        # Generate reset token
        from datetime import timedelta

        from edgelite.security.jwt import create_access_token

        reset_token = create_access_token(
            data={"sub": username, "type": "password_reset"},
            expires_delta=timedelta(minutes=15),  # FIXED-P2: 缩短密码重置Token有效期从30分钟到15分钟，减少截获利用窗口
        )

        logger.info("Password reset token generated for user: %s", username)

        config = get_config()
        email_cfg = getattr(config, "notify", None)
        email_cfg = getattr(email_cfg, "email", None) if email_cfg else None
        smtp_host = getattr(email_cfg, "smtp_host", "") if email_cfg else ""
        smtp_port = getattr(email_cfg, "smtp_port", 465) if email_cfg else 465
        smtp_user = getattr(email_cfg, "smtp_user", "") if email_cfg else ""
        smtp_password = getattr(email_cfg, "smtp_password", "") if email_cfg else ""
        from_addr = getattr(email_cfg, "from_addr", "") if email_cfg else ""

        if not smtp_host or not from_addr:
            logger.error("Email service not configured for password reset: user=%s", username)
            await _log_audit("FORGOT_PASSWORD_EMAIL_ERROR", "email_not_configured")
            return ApiResponse(message=_UNIFIED_MESSAGE)

        user_email = db_user.get("email") if db_user else None
        if not user_email:
            logger.warning("User %s has no email address, cannot send reset link", username)
            await _log_audit("FORGOT_PASSWORD_EMAIL_ERROR", "user_has_no_email")
            return ApiResponse(message=_UNIFIED_MESSAGE)

        frontend_base = os.environ.get("EDGELITE_FRONTEND_URL")
        if not frontend_base:  # FIXED-P4: 未配置FRONTEND_URL时返回错误而非默认localhost
            logger.error("EDGELITE_FRONTEND_URL not configured, cannot send reset link")
            # FIX-EL-SEVERE: 原 ApiResponse(success=False, ...) 使用了不存在的 success 字段，
            # Pydantic extra='ignore' 会静默丢弃，导致 code 默认为 0(成功)，
            # 客户端误认为密码重置邮件已发送。
            return ApiResponse(
                code=500,
                message=AuthErrors.PASSWORD_RESET_URL_NOT_CONFIGURED,
                data=None,
                error_code=AuthErrors.PASSWORD_RESET_URL_NOT_CONFIGURED,
            )
        # FIXED(安全): 强制 HTTPS - 防止密码重置 token 明文传输
        from urllib.parse import urlparse

        _parsed_frontend = urlparse(frontend_base)
        if _parsed_frontend.scheme != "https":
            # 允许 localhost 用于开发环境
            _host = _parsed_frontend.hostname or ""
            if _host not in ("localhost", "127.0.0.1", "::1"):
                logger.error("EDGELITE_FRONTEND_URL must use HTTPS in production: %s", frontend_base)
                return ApiResponse(
                    code=500,
                    message=AuthErrors.PASSWORD_RESET_URL_NOT_CONFIGURED,
                    data=None,
                    error_code=AuthErrors.PASSWORD_RESET_URL_NOT_CONFIGURED,
                )
        # FIXED-M01: 使用 hash fragment 传递 token，避免出现在 URL 参数中
        # Hash fragment (#...) 不会发送到服务器日志，不会出现在 Referer 头中
        reset_link = f"{frontend_base}/reset-password#token={reset_token}"

        html_body = f"""<html><body>
<h2>Password Reset Request</h2>
<p>You have requested to reset your password for EdgeLite.</p>
<p>Click the link below to set a new password (valid for 15 minutes):</p>
<p><a href="{reset_link}">{reset_link}</a></p>
<p>If you did not request this, please ignore this email.</p>
</body></html>"""

        def _send_reset_email_sync(
            smtp_host: str,
            smtp_port: int,
            smtp_user: str,
            smtp_password: str,
            from_addr: str,
            user_email: str,
            msg_body: str,
            use_starttls: bool,
        ) -> None:
            """同步发送密码重置邮件（在独立线程中执行，避免阻塞事件循环）"""
            import smtplib
            from email.mime.multipart import MIMEMultipart
            from email.mime.text import MIMEText

            msg = MIMEMultipart("alternative")
            msg["Subject"] = "EdgeLite Password Reset"
            msg["From"] = from_addr
            msg["To"] = user_email
            msg.attach(MIMEText(msg_body, "html"))

            if smtp_port == 465:
                server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=10)
            else:
                server = smtplib.SMTP(smtp_host, smtp_port, timeout=10)
                if use_starttls:
                    server.starttls()
            try:
                if smtp_user and smtp_password:
                    server.login(smtp_user, smtp_password)
                server.sendmail(from_addr, [user_email], msg.as_string())
            finally:
                server.quit()

        try:
            await asyncio.to_thread(
                _send_reset_email_sync,
                smtp_host,
                smtp_port,
                smtp_user,
                smtp_password,
                from_addr,
                user_email,
                html_body,
                getattr(email_cfg, "use_starttls", False) if email_cfg else False,
            )
            logger.info("Password reset email sent to user: %s", username)
            await _log_audit("FORGOT_PASSWORD_REQUEST")
        except Exception as email_err:
            # FIXED: 记录邮件发送失败到审计日志
            logger.error("Failed to send password reset email: %s", email_err)
            await _log_audit("FORGOT_PASSWORD_EMAIL_ERROR", str(email_err))
            return ApiResponse(message=_UNIFIED_MESSAGE)

        return ApiResponse(message=AuthErrors.PASSWORD_RESET_SENT)

    except Exception as e:
        # FIXED: 数据库或其他错误也记录审计日志
        logger.error("Forgot password request failed: %s", e)
        await _log_audit("FORGOT_PASSWORD_DB_ERROR", str(e))
        return ApiResponse(message=_UNIFIED_MESSAGE)


@router.post("/reset-password", response_model=ApiResponse)
async def reset_password(
    db: DatabaseDep,
    token: str = Body(..., embed=True),
    new_password: str = Body(..., embed=True),
    request: Request = None,
    audit_svc: AuditServiceDep = None,  # FIXED-M03: FastAPI dependency injection provides the value
):
    """Reset password using token from email.

    FIXED-H03: Enhanced security:
    - Token is single-use (recorded after successful reset)
    - IP rate limiting: max 3 attempts per hour per IP
    - Returns 410 Gone for already-used tokens
    """
    _RATE_LIMIT_MESSAGE = "Password reset rate limited. Please try again later."
    client_ip = _get_client_ip(request) if request else "unknown"

    # FIXED-H03: Check IP rate limit for reset usage
    from edgelite.storage.sqlite_repo import RateLimitRepo

    ip_count, ip_retry = await RateLimitRepo.check_reset_usage_ip_rate(client_ip)
    if ip_count == -1:
        logger.warning("Password reset rate limited: ip=%s retry_after=%ds", client_ip, ip_retry)
        if audit_svc:
            try:
                from edgelite.services.audit_service import AuditAction

                await audit_svc.log(
                    AuditAction.PASSWORD_RESET_RATELIMITED,
                    resource_type="password_reset",
                    resource_id="unknown",
                    ip_address=client_ip,
                    after_value={"retry_after": ip_retry},
                )
            except Exception as e:
                # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                logger.warning("密码重置速率限制审计日志失败: %s", e)
        return JSONResponse(
            status_code=429,
            content={
                "code": 429,
                "message": _RATE_LIMIT_MESSAGE,
                "data": None,
                "error_code": AuthErrors.RATE_LIMITED,
            },
            headers={"Retry-After": str(ip_retry)},
        )

    # Record this attempt for IP rate limiting
    await RateLimitRepo.record_reset_usage_attempt(client_ip)

    try:
        import hashlib

        from edgelite.security.jwt import verify_token
        from edgelite.security.password import hash_password

        # Verify token
        try:
            payload = verify_token(token, token_type="password_reset")
        except Exception as exc:  # FIXED(P2): 原问题-B904异常链丢失; 修复-添加as exc与from exc
            raise HTTPException(status_code=400, detail=AuthErrors.TOKEN_INVALID) from exc

        if payload.get("type") != "password_reset":
            raise HTTPException(status_code=400, detail=AuthErrors.TOKEN_INVALID)

        username = payload.get("sub")
        if not username:
            raise HTTPException(status_code=400, detail=AuthErrors.TOKEN_INVALID)

        # FIXED-H03: Check if token has already been used (one-time use)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        if await RateLimitRepo.is_password_reset_token_used(token_hash):
            logger.warning("Password reset token already used: username=%s ip=%s", username, client_ip)
            if audit_svc:
                try:
                    from edgelite.services.audit_service import AuditAction

                    await audit_svc.log(
                        AuditAction.PASSWORD_RESET_REUSED,
                        resource_type="password_reset",
                        resource_id=username,
                        ip_address=client_ip,
                    )
                except Exception as e:
                    # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                    logger.warning("密码重置令牌重用审计日志失败: %s", e)
            return JSONResponse(
                status_code=410,
                content={
                    "code": 410,
                    "message": AuthErrors.TOKEN_ALREADY_USED,
                    "data": None,
                    "error_code": AuthErrors.TOKEN_ALREADY_USED,
                },
            )

        # Validate new password
        if len(new_password) < _MIN_PASSWORD_LENGTH:
            raise HTTPException(status_code=400, detail=AuthErrors.PASSWORD_POLICY)
        if len(new_password) > _MAX_PASSWORD_LENGTH:
            raise HTTPException(status_code=400, detail=AuthErrors.PASSWORD_TOO_LONG)
        has_letter = any(c.isalpha() for c in new_password)
        has_digit = any(c.isdigit() for c in new_password)
        if not (has_letter and has_digit):
            raise HTTPException(status_code=400, detail=AuthErrors.PASSWORD_LETTER_AND_DIGIT)
        has_special = any(c in "!@#$%^&*()_+-=[]{}|;':\",./<>?`~" for c in new_password)
        if not has_special:
            raise HTTPException(status_code=400, detail=AuthErrors.PASSWORD_NEED_SPECIAL)
        if new_password.lower() in _WEAK_PASSWORDS:
            raise HTTPException(status_code=400, detail=AuthErrors.PASSWORD_POLICY)

        # FIXED-H03: Atomically mark token as used BEFORE updating password
        if not await RateLimitRepo.mark_password_reset_token_used(token_hash, username):
            logger.error("Failed to mark reset token as used, rejecting reset: username=%s", username)
            raise HTTPException(status_code=500, detail=AuthErrors.TOKEN_PROCESSING_FAILED)

        # Update password
        async with db.get_session() as session:
            repo = UserRepo(session, db.write_lock)
            db_user = await repo.get_by_username_with_password(username)

        if db_user is None:
            raise HTTPException(status_code=404, detail=AuthErrors.USER_NOT_FOUND)

        # FIXED-P0: Check if account is disabled before allowing password reset
        if not db_user.get("enabled"):
            logger.warning("Password reset blocked for disabled account: username=%s", username)
            # Return success message to prevent username enumeration
            return ApiResponse(message=AuthErrors.PASSWORD_RESET_SENT)

        hashed = hash_password(new_password)
        async with db.get_session() as session:
            repo = UserRepo(session, db.write_lock)
            await repo.update_password(username, hashed)

        # FIXED-H03: Revoke the jti to invalidate token (already marked as used above)
        jti = payload.get("jti")
        exp = payload.get("exp")
        if jti:
            try:
                from edgelite.security.token_revocation import revoke_token

                revoke_token(jti, exp)
            except Exception as e:
                # Log but don't fail - token is already marked as used
                logger.warning("Token revocation failed (non-critical): %s", e)

        logger.info("Password reset successful for user: %s", username)

        # FIXED-H03: Audit log for successful reset
        if audit_svc:
            try:
                from edgelite.services.audit_service import AuditAction

                await audit_svc.log(
                    AuditAction.PASSWORD_RESET_USED,
                    resource_type="password_reset",
                    resource_id=username,
                    ip_address=client_ip,
                )
            except Exception as e:
                # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                logger.warning("密码重置成功审计日志失败: %s", e)

        return ApiResponse(message=AuthErrors.PASSWORD_RESET_SUCCESS)

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Password reset failed: %s", e)
        raise HTTPException(status_code=500, detail=AuthErrors.PASSWORD_CHANGE_FAILED) from e


@router.post("/logout", response_model=ApiResponse)
async def logout(request: Request, user: CurrentUser, audit_svc: AuditServiceDep):
    """用户登出端点。

    吊销当前 access_token 并清除 HttpOnly Cookie，记录审计日志。
    """
    try:
        from edgelite.security.jwt import decode_token
        from edgelite.security.token_revocation import revoke_token

        try:
            from edgelite.services.audit_service import AuditAction

            client_ip = _get_client_ip(request)
            await audit_svc.log(
                AuditAction.LOGOUT, user_id=user.get("user_id"), username=user.get("username"), ip_address=client_ip
            )
        except Exception as e:
            logger.warning("Audit log failed: %s", e)

        tokens_to_revoke = []
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            tokens_to_revoke.append(auth_header[7:])
        cookie_access = request.cookies.get(_AUTH_COOKIE_ACCESS)
        if cookie_access:
            tokens_to_revoke.append(cookie_access)
        for raw_token in tokens_to_revoke:
            try:
                # FIXED: 验证 token_type 为 access
                payload = decode_token(raw_token, verify_exp=False, token_type="access")
                jti = payload.get("jti")
                exp = payload.get("exp")
                if jti:
                    revoke_token(jti, exp)
                    # LP-09: 从活跃 session 中移除
                    from edgelite.security.session_manager import remove_session

                    remove_session(user.get("user_id", ""), jti)
            except Exception as e:
                logger.warning("Access token revocation failed: %s", e)  # FIXED-P3: 中文日志→英文

        refresh_tokens = []
        cookie_refresh = request.cookies.get(_AUTH_COOKIE_REFRESH)
        if cookie_refresh:
            refresh_tokens.append(cookie_refresh)
        try:
            body = await request.json()
            body_refresh = body.get("refresh_token")
            if body_refresh:
                refresh_tokens.append(body_refresh)
        except Exception as e:
            logger.debug("No refresh token in request body: %s: %s", type(e).__name__, e)
        for raw_token in refresh_tokens:
            try:
                # FIXED: 验证 token_type 为 refresh
                payload = decode_token(raw_token, verify_exp=False, token_type="refresh")
                jti = payload.get("jti")
                exp = payload.get("exp")
                if jti:
                    revoke_token(jti, exp)
                    # LP-09: 从活跃 session 中移除
                    from edgelite.security.session_manager import remove_session

                    remove_session(user.get("user_id", ""), jti)
            except Exception as e:
                logger.warning("Refresh token revocation failed: %s", e)  # FIXED-P3: 中文日志→英文

        from fastapi.responses import JSONResponse

        from edgelite.middleware.csrf import remove_csrf_token

        remove_csrf_token(user["user_id"])

        response = JSONResponse(content=ApiResponse().model_dump())
        # LP-02: 清除 HttpOnly Token Cookie
        _clear_token_cookies(response)
        return response
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Logout failed: %s", e)  # FIXED-P3: 中文日志→英文
        raise HTTPException(status_code=500, detail=AuthErrors.LOGOUT_FAILED) from e
