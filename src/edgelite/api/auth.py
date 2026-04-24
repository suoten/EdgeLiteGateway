"""认证API路由"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, HTTPException, status

from edgelite.models.user import LoginRequest, TokenResponse
from edgelite.models.common import ApiResponse
from edgelite.security.jwt import create_access_token, create_refresh_token, verify_token
from edgelite.security.password import verify_password
from edgelite.storage.sqlite_repo import UserRepo

router = APIRouter(prefix="/api/v1/auth", tags=["认证"])


def _get_user_repo() -> UserRepo:
    from edgelite.app import _app_state
    return UserRepo(_app_state.db_conn)


@router.post("/login", response_model=ApiResponse[TokenResponse])
async def login(req: LoginRequest):
    """用户登录"""
    repo = _get_user_repo()
    user = await repo.get_by_username(req.username)

    if user is None or not verify_password(req.password, user["password"]):
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
async def refresh_token(refresh: str):
    """刷新Access Token"""
    try:
        payload = verify_token(refresh, token_type="refresh")
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh Token无效")

    access_token = create_access_token(
        data={"sub": payload["sub"], "username": payload["username"], "role": payload["role"]}
    )
    new_refresh = create_refresh_token(
        data={"sub": payload["sub"], "username": payload["username"], "role": payload["role"]}
    )

    from edgelite.config import get_config
    config = get_config()

    return ApiResponse(data=TokenResponse(
        access_token=access_token,
        refresh_token=new_refresh,
        expires_in=config.security.access_token_expire_minutes * 60,
    ))
