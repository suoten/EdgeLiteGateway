"""用户管理API路由"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from edgelite.api.deps import CurrentUser, DatabaseDep, PaginationDep, require_permission
from edgelite.api.error_codes import UserErrors
from edgelite.models.common import ApiResponse, PagedResponse
from edgelite.models.user import UserCreate, UserResponse, UserUpdate
from edgelite.security.password import hash_password
from edgelite.security.rbac import Permission
from edgelite.storage.sqlite_repo import UserRepo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/users", tags=["Users"])


@router.get("", response_model=PagedResponse[UserResponse])
async def list_users(
    db: DatabaseDep,
    user: CurrentUser = require_permission(Permission.USER_READ),
    pagination: PaginationDep = None,  # FIXED: 原问题-默认值None导致类型检查误判，但Python语法要求有默认值（前参有默认值）
):
    try:
        async with db.get_session() as session:
            repo = UserRepo(session, db.write_lock)
            users, total = await repo.list_all(pagination.page, pagination.size)
        return PagedResponse(data=users, total=total, page=pagination.page, size=pagination.size)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("list_users failed: %s", e)
        # FIXED: 原问题-中文硬编码detail，现使用错误码
        raise HTTPException(status_code=500, detail=UserErrors.LIST_FAILED) from e


@router.post("", response_model=ApiResponse[UserResponse], status_code=201)
async def create_user(
    body: UserCreate,
    db: DatabaseDep,
    user: CurrentUser = require_permission(Permission.USER_CREATE),
):
    try:
        async with db.get_session() as session:
            repo = UserRepo(session, db.write_lock)
            existing = await repo.get_by_username(body.username)
            if existing:
                # FIXED: 原问题-中文硬编码detail
                raise HTTPException(status_code=409, detail=UserErrors.USERNAME_EXISTS)
            data = body.model_dump()
            data["password"] = hash_password(data["password"])
            new_user = await repo.create(data)
        return ApiResponse(data=new_user)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
        logger.error("create_user failed: %s", e)
        # FIXED: 原问题-中文硬编码detail
        raise HTTPException(status_code=500, detail=UserErrors.CREATE_FAILED) from e


@router.put("/{user_id}", response_model=ApiResponse[UserResponse])
async def update_user(
    user_id: str,
    body: UserUpdate,
    db: DatabaseDep,
    user: CurrentUser = require_permission(Permission.USER_UPDATE),
):
    try:
        async with db.get_session() as session:
            repo = UserRepo(session, db.write_lock)
            data = body.model_dump(exclude_none=True)
            if "password" in data:
                data["password"] = hash_password(data["password"])
            if "role" in data and data["role"] != "admin":
                target = await repo.get(user_id)
                if target and target["role"] == "admin":
                    admin_count = await repo.count_by_role("admin")
                    if admin_count <= 1:
                        # FIXED: 原问题-中文硬编码detail
                        raise HTTPException(status_code=403, detail=UserErrors.CANNOT_REMOVE_LAST_ADMIN)
            updated = await repo.update(user_id, data)
        if updated is None:
            # FIXED: 原问题-中文硬编码detail
            raise HTTPException(status_code=404, detail=UserErrors.USER_NOT_FOUND)
        return ApiResponse(data=updated)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("update_user failed: %s", e)
        # FIXED: 原问题-中文硬编码detail
        raise HTTPException(status_code=500, detail=UserErrors.UPDATE_FAILED) from e


@router.delete("/{user_id}", response_model=ApiResponse)
async def delete_user(
    user_id: str,
    db: DatabaseDep,
    user: CurrentUser = require_permission(Permission.USER_DELETE),
):
    try:
        if user_id == user.get("user_id"):
            # FIXED: 原问题-中文硬编码detail
            raise HTTPException(status_code=400, detail=UserErrors.CANNOT_DELETE_SELF)
        async with db.get_session() as session:
            repo = UserRepo(session, db.write_lock)
            target = await repo.get(user_id)
            # FIXED: 原问题-admin用户删除保护仅前端，后端无保护，现添加admin用户名403检查
            if target and target.get("username") == "admin":
                raise HTTPException(status_code=403, detail=UserErrors.CANNOT_DELETE_ADMIN)
            if target and target["role"] == "admin":
                admin_count = await repo.count_by_role("admin")
                if admin_count <= 1:
                    # FIXED: 原问题-中文硬编码detail
                    raise HTTPException(status_code=403, detail=UserErrors.CANNOT_DELETE_LAST_ADMIN)
            success = await repo.delete(user_id)
        if not success:
            # FIXED: 原问题-中文硬编码detail
            raise HTTPException(status_code=404, detail=UserErrors.USER_NOT_FOUND)
        return ApiResponse()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("delete_user failed: %s", e)
        # FIXED: 原问题-中文硬编码detail
        raise HTTPException(status_code=500, detail=UserErrors.DELETE_FAILED) from e
