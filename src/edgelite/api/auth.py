"""认证API路由"""

from __future__ import annotations

import ipaddress
import logging
import time
from collections import defaultdict

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from jose import JWTError

from edgelite.api.deps import CurrentUser, DatabaseDep, get_current_user
from edgelite.api.error_codes import AuthErrors
from edgelite.constants import _AUTH_ATTEMPTS_LIMIT, _AUTH_MAX_ATTEMPTS, _AUTH_PASSWORD_MAX_LENGTH
from edgelite.models.common import ApiResponse
from edgelite.models.user import LoginRequest, TokenResponse, UserInfoResponse
from edgelite.security.jwt import create_access_token, create_refresh_token, verify_token
from edgelite.security.password import verify_password
from edgelite.storage.sqlite_repo import UserRepo

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
async def login(req: LoginRequest, request: Request, db: DatabaseDep):
    try:
        client_ip = _get_client_ip(request)
        _check_login_rate(client_ip)

        async with db.get_session() as session:
            repo = UserRepo(session, db.write_lock)
            user = await repo.get_by_username_with_password(req.username)

        if user is None or not verify_password(req.password, user["password"]):
            _record_login_attempt(client_ip)
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
        logger.error("Token刷新失败: %s", e)
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
        logger.error("获取失败: %s", e)
        raise HTTPException(status_code=500, detail=AuthErrors.LOGIN_FAILED) from e


@router.post("/change-password", response_model=ApiResponse)
async def change_password(
    db: DatabaseDep,
    old_password: str = Body(..., embed=True),
    new_password: str = Body(..., embed=True),
    user: dict = Depends(get_current_user),
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
        return ApiResponse(data={"message": "password_changed"})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("修改失败: %s", e)
        raise HTTPException(status_code=500, detail=AuthErrors.PASSWORD_CHANGE_FAILED) from e


@router.post("/logout", response_model=ApiResponse)
async def logout(request: Request, user: CurrentUser):
    try:
        from edgelite.security.jwt import decode_token
        from edgelite.security.token_revocation import revoke_token

        tokens_to_revoke = []
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            tokens_to_revoke.append(auth_header[7:])
        cookie_access = request.cookies.get("edgelite_access")
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
                logger.warning("Access Token撤销失败: %s", e)

        refresh_tokens = []
        cookie_refresh = request.cookies.get("edgelite_refresh")
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
                logger.warning("Refresh Token撤销失败: %s", e)

        from fastapi.responses import JSONResponse

        response = JSONResponse(content=ApiResponse().model_dump())
        response.delete_cookie("edgelite_access", path="/api/v1")
        response.delete_cookie("edgelite_refresh", path="/api/v1/auth")
        return response
    except HTTPException:
        raise
    except Exception as e:
        logger.error("操作失败: %s", e)
        raise HTTPException(status_code=500, detail=AuthErrors.LOGOUT_FAILED) from e
