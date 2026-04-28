"""用户管理API路由"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from edgelite.models.user import UserCreate, UserUpdate, UserResponse
from edgelite.models.common import ApiResponse, PagedResponse
from edgelite.api.deps import CurrentUser, require_permission
from edgelite.security.rbac import Permission
from edgelite.security.password import hash_password

router = APIRouter(prefix="/api/v1/users", tags=["用户管理"])


def _get_user_repo():
    from edgelite.app import _app_state
    from edgelite.storage.sqlite_repo import UserRepo
    return UserRepo(_app_state.db_conn, _app_state.write_lock)


@router.get("", response_model=PagedResponse[UserResponse])
async def list_users(
    user: CurrentUser = require_permission(Permission.USER_READ),
    page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=1000),
):
    repo = _get_user_repo()
    users, total = await repo.list_all(page, size)
    return PagedResponse(data=users, total=total, page=page, size=size)


@router.post("", response_model=ApiResponse[UserResponse], status_code=201)
async def create_user(body: UserCreate, user: CurrentUser = require_permission(Permission.USER_CREATE)):
    repo = _get_user_repo()
    existing = await repo.get_by_username(body.username)
    if existing:
        raise HTTPException(status_code=409, detail="用户名已存在")
    data = body.model_dump()
    data["password"] = hash_password(data["password"])
    new_user = await repo.create(data)
    return ApiResponse(data=new_user)


@router.put("/{user_id}", response_model=ApiResponse[UserResponse])
async def update_user(user_id: str, body: UserUpdate, user: CurrentUser = require_permission(Permission.USER_UPDATE)):
    repo = _get_user_repo()
    data = body.model_dump(exclude_none=True)
    if "password" in data:
        data["password"] = hash_password(data["password"])
    # 保护最后管理员：角色从admin改为其他时检查
    if "role" in data and data["role"] != "admin":
        target = await repo.get(user_id)
        if target and target["role"] == "admin":
            all_users, _ = await repo.list_all(size=10000)
            admin_count = sum(1 for u in all_users if u["role"] == "admin")
            if admin_count <= 1:
                raise HTTPException(status_code=403, detail="不能移除最后一个管理员的角色")
    updated = await repo.update(user_id, data)
    if updated is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    return ApiResponse(data=updated)


@router.delete("/{user_id}", response_model=ApiResponse)
async def delete_user(user_id: str, user: CurrentUser = require_permission(Permission.USER_DELETE)):
    if user_id == user.get("user_id"):
        raise HTTPException(status_code=400, detail="不能删除自己")
    repo = _get_user_repo()
    # 保护最后管理员
    target = await repo.get(user_id)
    if target and target["role"] == "admin":
        all_users, _ = await repo.list_all(size=10000)
        admin_count = sum(1 for u in all_users if u["role"] == "admin")
        if admin_count <= 1:
            raise HTTPException(status_code=403, detail="不能删除最后一个管理员")
    success = await repo.delete(user_id)
    if not success:
        raise HTTPException(status_code=404, detail="用户不存在")
    return ApiResponse()
