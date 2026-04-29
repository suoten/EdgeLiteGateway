"""API依赖注入"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from jose import JWTError

from edgelite.security.jwt import verify_token
from edgelite.security.rbac import Permission, check_permission

logger = logging.getLogger(__name__)

security_scheme = HTTPBearer()


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(security_scheme)],
) -> dict:
    try:
        payload = verify_token(credentials.credentials, token_type="access")
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token无效或已过期",
            headers={"WWW-Authenticate": "Bearer"},
        )

    jti = payload.get("jti")
    if jti:
        from edgelite.security.token_revocation import is_token_revoked
        if is_token_revoked(jti):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token已撤销",
                headers={"WWW-Authenticate": "Bearer"},
            )
    else:
        logger.warning("Token无jti字段(旧格式)，跳过撤销检查")

    username = payload.get("username", "")
    from edgelite.storage.sqlite_repo import UserRepo
    from edgelite.app import _app_state
    async with _app_state.database.get_session() as session:
        repo = UserRepo(session, _app_state.database.write_lock)
        user = await repo.get_by_username(username)

    if user is None or not user["enabled"]:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户不存在或已禁用",
        )

    return {
        "user_id": user["user_id"],
        "username": user["username"],
        "role": user["role"],
    }


CurrentUser = Annotated[dict, Depends(get_current_user)]


def require_permission(permission: Permission):
    """权限校验依赖"""

    async def _check(user: CurrentUser) -> dict:
        check_permission(user["role"], permission)
        return user

    return _check
