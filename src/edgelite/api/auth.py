"""认证API路由"""

from __future__ import annotations

import time
from collections import defaultdict

from fastapi import APIRouter, Body, HTTPException, Request, status

from edgelite.models.user import LoginRequest, TokenResponse
from edgelite.models.common import ApiResponse
from edgelite.security.jwt import create_access_token, create_refresh_token, verify_token
from edgelite.security.password import verify_password
from edgelite.storage.sqlite_repo import UserRepo
from edgelite.api.deps import CurrentUser

router = APIRouter(prefix="/api/v1/auth", tags=["认证"])

_login_attempts: dict[str, list[float]] = defaultdict(list)
_MAX_LOGIN_ATTEMPTS = 5
_LOGIN_WINDOW_SECONDS = 300


def _check_login_rate(ip: str) -> None:
    now = time.time()
    attempts = _login_attempts[ip]
    _login_attempts[ip] = [t for t in attempts if now - t < _LOGIN_WINDOW_SECONDS]
    # 容量限制：超过10000个IP条目时清理最旧的
    if len(_login_attempts) > 10000:
        oldest_ips = sorted(_login_attempts.keys(), key=lambda ip: min(_login_attempts[ip]) if _login_attempts[ip] else 0)
        for ip_to_remove in oldest_ips[:len(_login_attempts) - 8000]:
            del _login_attempts[ip_to_remove]
    if len(_login_attempts[ip]) >= _MAX_LOGIN_ATTEMPTS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"登录尝试过多，请{_LOGIN_WINDOW_SECONDS // 60}分钟后再试",
        )


def _record_login_attempt(ip: str) -> None:
    _login_attempts[ip].append(time.time())


def _get_user_repo():
    from edgelite.app import _app_state
    return _app_state.database, _app_state.database.write_lock


def _get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip
    return request.client.host if request.client else "unknown"


@router.post("/login", response_model=ApiResponse[TokenResponse])
async def login(req: LoginRequest, request: Request):
    """用户登录"""
    client_ip = _get_client_ip(request)
    _check_login_rate(client_ip)

    db, write_lock = _get_user_repo()
    async with db.get_session() as session:
        repo = UserRepo(session, write_lock)
        user = await repo.get_by_username_with_password(req.username)

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

    db, write_lock = _get_user_repo()
    async with db.get_session() as session:
        repo = UserRepo(session, write_lock)
        user = await repo.get_by_username(payload.get("username", ""))
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


@router.get("/me", response_model=ApiResponse)
async def get_current_user_info(user: CurrentUser):
    from edgelite.app import _app_state
    from edgelite.storage.sqlite_repo import UserRepo
    db = _app_state.database
    must_change = False
    async with db.get_session() as session:
        repo = UserRepo(session, db.write_lock)
        db_user = await repo.get_by_username_with_password(user["username"])
        if db_user:
            from edgelite.security.password import verify_password
            must_change = verify_password("admin123", db_user.get("password", ""))
    return ApiResponse(data={
        "user_id": user["user_id"],
        "username": user["username"],
        "role": user["role"],
        "must_change_password": must_change,
    })


@router.post("/logout", response_model=ApiResponse)
async def logout(request: Request):
    """用户登出，撤销Token"""
    from edgelite.security.token_revocation import revoke_token
    from edgelite.security.jwt import decode_token

    access_token = request.cookies.get("edgelite_access")
    if access_token:
        try:
            payload = decode_token(access_token, verify_exp=False)
            jti = payload.get("jti")
            exp = payload.get("exp")
            if jti:
                revoke_token(jti, exp)
        except Exception:
            pass

    refresh_token = request.cookies.get("edgelite_refresh")
    if refresh_token:
        try:
            payload = decode_token(refresh_token, verify_exp=False)
            jti = payload.get("jti")
            exp = payload.get("exp")
            if jti:
                revoke_token(jti, exp)
        except Exception:
            pass

    from fastapi.responses import JSONResponse
    response = JSONResponse(content={"code": 0, "message": "success", "data": None})
    response.delete_cookie("edgelite_access", path="/api/v1")
    response.delete_cookie("edgelite_refresh", path="/api/v1/auth")
    return response
