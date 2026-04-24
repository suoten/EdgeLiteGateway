"""API依赖注入"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from jose import JWTError

from edgelite.security.jwt import verify_token
from edgelite.security.rbac import Permission, check_permission
from edgelite.storage.database import Database

security_scheme = HTTPBearer()


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(security_scheme)],
) -> dict:
    """从JWT Token解析当前用户"""
    try:
        payload = verify_token(credentials.credentials, token_type="access")
        return {
            "user_id": payload.get("sub", ""),
            "username": payload.get("username", ""),
            "role": payload.get("role", "viewer"),
        }
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token无效或已过期",
            headers={"WWW-Authenticate": "Bearer"},
        )


CurrentUser = Annotated[dict, Depends(get_current_user)]


def require_permission(permission: Permission):
    """权限校验依赖"""

    async def _check(user: CurrentUser) -> dict:
        check_permission(user["role"], permission)
        return user

    return _check
