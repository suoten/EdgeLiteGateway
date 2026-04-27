"""认证API路由"""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import timedelta

from fastapi import APIRouter, Body, HTTPException, Request, status

from edgelite.models.user import LoginRequest, TokenResponse
from edgelite.models.common import ApiResponse
from edgelite.security.jwt import create_access_token, create_refresh_token, verify_token
from edgelite.security.password import verify_password
from edgelite.storage.sqlite_repo import UserRepo

router = APIRouter(prefix="/api/v1/auth", tags=["认证"])

_login_attempts: dict[str, list[float]] = defaultdict(list)
_MAX_LOGIN_ATTEMPTS = 5
_LOGIN_WINDOW_SECONDS = 300


def _check_login_rate(ip: str) -> None:
    now = time.time()
    attempts = _login_attempts[ip]
    _login_attempts[ip] = [t for t in attempts if now - t < _LOGIN_WINDOW_SECONDS]
    if len(_login_attempts[ip]) >= _MAX_LOGIN_ATTEMPTS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"登录尝试过多，请{_LOGIN_WINDOW_SECONDS // 60}分钟后再试",
        )


def _record_login_attempt(ip: str) -> None:
    _login_attempts[ip].append(time.time())


def _get_user_repo() -> UserRepo:
    from edgelite.app import _app_state
    return UserRepo(_app_state.db_conn, getattr(_app_state, 'write_lock', None))


@router.post("/login", response_model=ApiResponse[TokenResponse])
async def login(req: LoginRequest, request: Request):
    """用户登录"""
    client_ip = request.client.host if request.client else "unknown"
    _check_login_rate(client_ip)

    repo = _get_user_repo()
    user = await repo.get_by_username(req.username, include_password=True)

    if user is None or not verify_password(req.password, user["password"]):
        _record_login_attempt(client_ip)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")

    if not user["enabled"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="用户已禁用")

    access_token = create_access_token(
        data={"sub": user["user_id"], "username": user["username"], "role": user["role"]}
    )
    refresh_token = create_refresh_token(
        data={"sub": user["user_id"], "username": user["username"], "role": user["role"]}
    )

    from edgelite.config import get_config
    config = get_config()

    return ApiResponse(data=TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=config.security.access_token_expire_minutes * 60,
    ))


@router.post("/refresh", response_model=ApiResponse[TokenResponse])
async def refresh_token(refresh: str = Body(..., embed=True)):
    """刷新Access Token"""
    try:
        payload = verify_token(refresh, token_type="refresh")
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh Token无效")

    repo = _get_user_repo()
    user = await repo.get_by_username(payload.get("username", ""), include_password=False)
    if user is None or not user["enabled"]:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在或已禁用")

    current_role = user["role"]

    access_token = create_access_token(
        data={"sub": payload["sub"], "username": payload["username"], "role": current_role}
    )
    new_refresh = create_refresh_token(
        data={"sub": payload["sub"], "username": payload["username"], "role": current_role}
    )

    from edgelite.config import get_config
    config = get_config()

    return ApiResponse(data=TokenResponse(
        access_token=access_token,
        refresh_token=new_refresh,
        expires_in=config.security.access_token_expire_minutes * 60,
    ))
