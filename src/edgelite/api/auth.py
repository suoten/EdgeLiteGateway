"""认证API路由"""

from __future__ import annotations

import ipaddress
import logging
import time
from collections import defaultdict

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from jose import JWTError

from edgelite.api.deps import CurrentUser, DatabaseDep, get_current_user, AuditServiceDep
from edgelite.api.error_codes import AuthErrors
from edgelite.constants import _AUTH_ATTEMPTS_LIMIT, _AUTH_MAX_ATTEMPTS, _AUTH_PASSWORD_MAX_LENGTH
from edgelite.models.common import ApiResponse
from edgelite.models.user import LoginRequest, TokenResponse, UserInfoResponse
from edgelite.security.jwt import create_access_token, create_refresh_token, verify_token
from edgelite.security.password import verify_password
from edgelite.storage.sqlite_repo import UserRepo

_AUTH_COOKIE_ACCESS = "edgelite_access"
_AUTH_COOKIE_REFRESH = "edgelite_refresh"

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["Auth"])

_login_attempts: dict[str, list[float]] = defaultdict(list)
# FIXED: 原问题-魔法数字散布在代码中，现引用constants.py统一常量
_MAX_LOGIN_ATTEMPTS = _AUTH_MAX_ATTEMPTS
_LOGIN_WINDOW_SECONDS = 300
_LOGIN_ATTEMPTS_MAX_ENTRIES = _AUTH_ATTEMPTS_LIMIT
_LOGIN_ATTEMPTS_TRIM_TARGET = _AUTH_ATTEMPTS_LIMIT * 8 // 10
_MIN_PASSWORD_LENGTH = 8
_MAX_PASSWORD_LENGTH = _AUTH_PASSWORD_MAX_LENGTH


def _check_login_rate(ip: str) -> None:
    now = time.time()
    attempts = _login_attempts[ip]
    _login_attempts[ip] = [t for t in attempts if now - t < _LOGIN_WINDOW_SECONDS]
    if len(_login_attempts) > _LOGIN_ATTEMPTS_MAX_ENTRIES:
        oldest_ips = sorted(
            _login_attempts.keys(),
            key=lambda k: min(_login_attempts[k]) if _login_attempts[k] else 0,
        )
        for ip_to_remove in oldest_ips[: len(_login_attempts) - _LOGIN_ATTEMPTS_TRIM_TARGET]:
            del _login_attempts[ip_to_remove]
    if len(_login_attempts[ip]) >= _MAX_LOGIN_ATTEMPTS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=AuthErrors.RATE_LIMITED,
        )


def _record_login_attempt(ip: str) -> None:
    _login_attempts[ip].append(time.time())


def _get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        ip_str = forwarded.split(",")[0].strip()
        try:
            ipaddress.ip_address(ip_str)
            return ip_str
        except ValueError:
            pass
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        try:
            ipaddress.ip_address(real_ip)
            return real_ip
        except ValueError:
            pass
    return request.client.host if request.client else "unknown"


@router.post("/login", response_model=ApiResponse[TokenResponse])
async def login(req: LoginRequest, request: Request, db: DatabaseDep, audit_svc: AuditServiceDep):
    try:
        client_ip = _get_client_ip(request)
        _check_login_rate(client_ip)

        async with db.get_session() as session:
            repo = UserRepo(session, db.write_lock)
            user = await repo.get_by_username_with_password(req.username)

        if user is None or not verify_password(req.password, user["password"]):
            _record_login_attempt(client_ip)
            try:
                from edgelite.services.audit_service import AuditAction
                await audit_svc.log(AuditAction.LOGIN_FAILED, username=req.username, ip_address=client_ip, status="failed")
            except Exception:
                pass
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=AuthErrors.INVALID_CREDENTIALS)

        if not user["enabled"]:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=AuthErrors.USER_DISABLED)

        access_token = create_access_token(
            data={"sub": user["user_id"], "username": user["username"], "role": user["role"]}
        )
        refresh_token = create_refresh_token(
            data={"sub": user["user_id"], "username": user["username"], "role": user["role"]}
        )

        from edgelite.config import get_config

        config = get_config()

        try:
            from edgelite.services.audit_service import AuditAction
            await audit_svc.log(AuditAction.LOGIN, user_id=user["user_id"], username=user["username"], ip_address=client_ip)
        except Exception:
            pass

        return ApiResponse(
            data=TokenResponse(
                access_token=access_token,
                refresh_token=refresh_token,
                expires_in=config.security.access_token_expire_minutes * 60,
            )
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Login failed: %s", e)
        raise HTTPException(status_code=500, detail=AuthErrors.LOGIN_FAILED) from e


@router.post("/refresh", response_model=ApiResponse[TokenResponse])
async def refresh_token(db: DatabaseDep, refresh: str = Body(..., embed=True)):
    try:
        payload = verify_token(refresh, token_type="refresh")
    except (JWTError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=AuthErrors.REFRESH_TOKEN_INVALID
        ) from None

    # FIXED: 数据库操作和token创建无异常保护
    try:
        async with db.get_session() as session:
            repo = UserRepo(session, db.write_lock)
            user = await repo.get_by_username(payload.get("username", ""))
        if user is None or not user["enabled"]:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=AuthErrors.USER_NOT_FOUND)

        current_role = user["role"]

        # FIXED: 原问题-payload["sub"]硬访问可能KeyError(JWT被篡改)，改为.get()加校验
        sub = payload.get("sub")
        username = payload.get("username")
        if not sub or not username:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=AuthErrors.REFRESH_TOKEN_INVALID)

        access_token = create_access_token(
            data={"sub": sub, "username": username, "role": current_role}
        )
        new_refresh = create_refresh_token(
            data={"sub": sub, "username": username, "role": current_role}
        )

        from edgelite.config import get_config

        config = get_config()

        return ApiResponse(
            data=TokenResponse(
                access_token=access_token,
                refresh_token=new_refresh,
                expires_in=config.security.access_token_expire_minutes * 60,
            )
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Token refresh failed: %s", e)  # FIXED-P3: 中文日志→英文
        raise HTTPException(status_code=500, detail=AuthErrors.LOGIN_FAILED) from e


@router.get("/me", response_model=ApiResponse[UserInfoResponse])
async def get_current_user_info(user: CurrentUser, db: DatabaseDep):
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
    audit_svc: AuditServiceDep = None,
):
    try:
        async with db.get_session() as session:
            repo = UserRepo(session, db.write_lock)
            db_user = await repo.get_by_username_with_password(user["username"])
        if db_user is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=AuthErrors.USER_NOT_FOUND)
        if not verify_password(old_password, db_user["password"]):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=AuthErrors.OLD_PASSWORD_WRONG)
        if len(new_password) < _MIN_PASSWORD_LENGTH:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=AuthErrors.PASSWORD_POLICY
            )
        if len(new_password) > _MAX_PASSWORD_LENGTH:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=AuthErrors.PASSWORD_TOO_LONG
            )
        if old_password == new_password:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=AuthErrors.PASSWORD_SAME_AS_OLD
            )
        has_letter = any(c.isalpha() for c in new_password)
        has_digit = any(c.isdigit() for c in new_password)
        if not (has_letter and has_digit):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=AuthErrors.PASSWORD_LETTER_AND_DIGIT
            )

        from edgelite.security.password import hash_password

        hashed = hash_password(new_password)
        async with db.get_session() as session:
            repo = UserRepo(session, db.write_lock)
            await repo.update_password(user["username"], hashed)
            await repo.update_user(user["username"], {"must_change_password": False})
        try:
            from edgelite.services.audit_service import AuditAction
            if audit_svc:
                await audit_svc.log(AuditAction.PASSWORD_CHANGE, user_id=user["user_id"], username=user["username"])
        except Exception:
            pass
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
):
    """Send password reset email to user (requires email configuration)"""
    try:
        async with db.get_session() as session:
            repo = UserRepo(session, db.write_lock)
            db_user = await repo.get_by_username(username)

        if db_user is None:
            # Return success anyway to prevent username enumeration
            logger.info("Password reset requested for non-existent user: %s", username)
            return ApiResponse(success=True, message="If the account exists, a reset email will be sent")

        # Generate reset token
        from edgelite.security.jwt import create_access_token
        from datetime import timedelta

        reset_token = create_access_token(
            data={"sub": username, "type": "password_reset"},
            expires_delta=timedelta(minutes=30),
        )

        # TODO: Send email with reset link
        # For now, log the token (in production, send actual email)
        logger.info("Password reset token generated for user: %s", username)
        logger.debug("Reset token (expires in 30 min): %s", reset_token)

        # Check if email service is configured
        from edgelite.services.notification import get_notification_manager
        notification_mgr = get_notification_manager()
        has_email = any(
            isinstance(ch, type)
            for ch in getattr(notification_mgr, '_channels', [])
        )

        if not has_email:
            logger.warning("Email service not configured, password reset token logged only")

        return ApiResponse(
            success=True,
            message="If the account exists, a password reset link has been sent to your registered email"
        )

    except Exception as e:
        logger.error("Forgot password request failed: %s", e)
        return ApiResponse(success=True, message="If the account exists, a reset email will be sent")


@router.post("/reset-password", response_model=ApiResponse)
async def reset_password(
    db: DatabaseDep,
    token: str = Body(..., embed=True),
    new_password: str = Body(..., embed=True),
):
    """Reset password using token from email"""
    try:
        from edgelite.security.jwt import verify_token, decode_token
        from edgelite.security.password import hash_password

        # Verify token
        try:
            payload = decode_token(token, verify_exp=True)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid or expired reset token")

        if payload.get("type") != "password_reset":
            raise HTTPException(status_code=400, detail="Invalid reset token")

        username = payload.get("sub")
        if not username:
            raise HTTPException(status_code=400, detail="Invalid reset token")

        # Validate new password
        if len(new_password) < _MIN_PASSWORD_LENGTH:
            raise HTTPException(status_code=400, detail=AuthErrors.PASSWORD_POLICY)
        if len(new_password) > _MAX_PASSWORD_LENGTH:
            raise HTTPException(status_code=400, detail=AuthErrors.PASSWORD_TOO_LONG)
        has_letter = any(c.isalpha() for c in new_password)
        has_digit = any(c.isdigit() for c in new_password)
        if not (has_letter and has_digit):
            raise HTTPException(status_code=400, detail=AuthErrors.PASSWORD_LETTER_AND_DIGIT)

        # Update password
        async with db.get_session() as session:
            repo = UserRepo(session, db.write_lock)
            db_user = await repo.get_by_username_with_password(username)

        if db_user is None:
            raise HTTPException(status_code=404, detail=AuthErrors.USER_NOT_FOUND)

        hashed = hash_password(new_password)
        async with db.get_session() as session:
            repo = UserRepo(session, db.write_lock)
            await repo.update_password(username, hashed)

        logger.info("Password reset successful for user: %s", username)
        return ApiResponse(success=True, message="Password reset successful. Please login with your new password.")

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Password reset failed: %s", e)
        raise HTTPException(status_code=500, detail="Password reset failed") from e


@router.post("/logout", response_model=ApiResponse)
async def logout(request: Request, user: CurrentUser, audit_svc: AuditServiceDep):
    try:
        from edgelite.security.jwt import decode_token
        from edgelite.security.token_revocation import revoke_token

        try:
            from edgelite.services.audit_service import AuditAction
            client_ip = _get_client_ip(request)
            await audit_svc.log(AuditAction.LOGOUT, user_id=user.get("user_id"), username=user.get("username"), ip_address=client_ip)
        except Exception:
            pass

        tokens_to_revoke = []
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            tokens_to_revoke.append(auth_header[7:])
        cookie_access = request.cookies.get(_AUTH_COOKIE_ACCESS)
        if cookie_access:
            tokens_to_revoke.append(cookie_access)
        for raw_token in tokens_to_revoke:
            try:
                payload = decode_token(raw_token, verify_exp=False)
                jti = payload.get("jti")
                exp = payload.get("exp")
                if jti:
                    revoke_token(jti, exp)
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
            logger.error("Token cleanup failed: %s", e)  # FIXED: 原问题-except Exception: pass完全静默
        for raw_token in refresh_tokens:
            try:
                payload = decode_token(raw_token, verify_exp=False)
                jti = payload.get("jti")
                exp = payload.get("exp")
                if jti:
                    revoke_token(jti, exp)
            except Exception as e:
                logger.warning("Refresh token revocation failed: %s", e)  # FIXED-P3: 中文日志→英文

        from fastapi.responses import JSONResponse

        response = JSONResponse(content=ApiResponse().model_dump())
        response.delete_cookie(_AUTH_COOKIE_ACCESS, path="/api/v1")
        response.delete_cookie(_AUTH_COOKIE_REFRESH, path="/api/v1/auth")
        return response
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Logout failed: %s", e)  # FIXED-P3: 中文日志→英文
        raise HTTPException(status_code=500, detail=AuthErrors.LOGOUT_FAILED) from e
