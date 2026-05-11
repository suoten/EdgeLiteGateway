"""用户管理API路由"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query

from edgelite.api.deps import CurrentUser, require_permission
from edgelite.models.common import ApiResponse, PagedResponse
from edgelite.models.user import UserCreate, UserResponse, UserUpdate
from edgelite.security.password import hash_password
from edgelite.security.rbac import Permission
from edgelite.storage.sqlite_repo import UserRepo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/users", tags=["用户管理"])


@router.get("", response_model=PagedResponse[UserResponse])
async def list_users(
    user: CurrentUser = require_permission(Permission.USER_READ),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=1000),
):
    try:
        db = svc
        async with db.get_session() as session:
            repo = UserRepo(session, db.write_lock)
            users, total = await repo.list_all(page, size)
        return PagedResponse(data=users, total=total, page=page, size=size)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("获取用户列表失败: %s", e)
        raise HTTPException(status_code=500, detail="获取用户列表失败") from e


@router.post("", response_model=ApiResponse[UserResponse], status_code=201)
async def create_user(
    body: UserCreate, user: CurrentUser = require_permission(Permission.USER_CREATE)
):
    try:
        db = svc
        async with db.get_session() as session:
            repo = UserRepo(session, db.write_lock)
            existing = await repo.get_by_username(body.username)
            if existing:
                raise HTTPException(status_code=409, detail="用户名已存在")
            data = body.model_dump()
            data["password"] = hash_password(data["password"])
            new_user = await repo.create(data)
        return ApiResponse(data=new_user)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("创建用户失败: %s", e)
        raise HTTPException(status_code=500, detail="创建用户失败") from e


@router.put("/{user_id}", response_model=ApiResponse[UserResponse])
async def update_user(
    user_id: str, body: UserUpdate, user: CurrentUser = require_permission(Permission.USER_UPDATE)
):
    try:
        db = svc
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
                        raise HTTPException(status_code=403, detail="不能移除最后一个管理员的角色")
            updated = await repo.update(user_id, data)
        if updated is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        return ApiResponse(data=updated)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("更新用户失败: %s", e)
        raise HTTPException(status_code=500, detail="更新用户失败") from e


@router.delete("/{user_id}", response_model=ApiResponse)
async def delete_user(user_id: str, user: CurrentUser = require_permission(Permission.USER_DELETE)):
    try:
        if user_id == user.get("user_id"):
            raise HTTPException(status_code=400, detail="不能删除自己")
        db = svc
        async with db.get_session() as session:
            repo = UserRepo(session, db.write_lock)
            target = await repo.get(user_id)
            if target and target["role"] == "admin":
                admin_count = await repo.count_by_role("admin")
                if admin_count <= 1:
                    raise HTTPException(status_code=403, detail="不能删除最后一个管理员")
            success = await repo.delete(user_id)
        if not success:
            raise HTTPException(status_code=404, detail="用户不存在")
        return ApiResponse()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("删除用户失败: %s", e)
        raise HTTPException(status_code=500, detail="删除用户失败") from e
