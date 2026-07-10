"""用户管理API路由"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request

# FIX-EL-GENERAL: 从 auth.py 共享弱密码字典，确保管理员改密也走弱密码检查
from edgelite.api.auth import _WEAK_PASSWORDS
from edgelite.api.deps import (
    AuditServiceDep,
    ConfigDep,
    DatabaseDep,
    PaginationDep,
    require_permission,
)
from edgelite.api.error_codes import AuthErrors, RepoErrors, UserErrors
from edgelite.models.common import ApiResponse, PagedResponse
from edgelite.models.db import StaleDataError
from edgelite.models.user import UserCreate, UserResponse, UserUpdate
from edgelite.security.password import hash_password
from edgelite.security.rbac import Permission
from edgelite.storage.sqlite_repo import DeviceRepo, RuleRepo, UserRepo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/users", tags=["Users"])


@router.get("", response_model=PagedResponse[UserResponse])
async def list_users(
    db: DatabaseDep,
    user: dict[str, str] = Depends(require_permission(Permission.USER_READ)),
    pagination: PaginationDep = None,  # FIXED: 原问题-默认值None导致类型检查误判，但Python语法要求有默认值（前参有默认值）  # noqa: E501
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
    user: dict[str, str] = Depends(require_permission(Permission.USER_CREATE)),
    request: Request = None,
    audit_svc: AuditServiceDep = None,  # FIXED-M03: FastAPI dependency injection provides the value
):
    try:
        async with db.get_session() as session:
            repo = UserRepo(session, db.write_lock)
            existing = await repo.get_by_username(body.username)
            if existing:
                raise HTTPException(status_code=409, detail=UserErrors.USERNAME_EXISTS)
            data = body.model_dump()
            data["password"] = hash_password(data["password"])
            new_user = await repo.create(data)
        try:
            # FIXED-M04: Use _get_client_ip for trusted proxy support
            from edgelite.api.auth import _get_client_ip
            from edgelite.services.audit_service import AuditAction

            client_ip = _get_client_ip(request) if request else ""
            await audit_svc.log(
                AuditAction.USER_CREATE,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="user",
                resource_id=new_user.get("user_id", ""),
                ip_address=client_ip,
                after_value={"username": body.username, "role": body.role if hasattr(body, "role") else ""},
            )
        except Exception as e:
            logger.warning("Audit log failed: %s", e)
        return ApiResponse(data=new_user)
    except ValueError as e:
        raise HTTPException(
            status_code=409, detail={"error_code": UserErrors.CREATE_FAILED, "errors": [str(e)], "warnings": []}
        ) from e
    except HTTPException:
        raise
    except Exception as e:
        logger.error("create_user failed: %s", e)
        # FIXED: 原问题-中文硬编码detail
        raise HTTPException(status_code=500, detail=UserErrors.CREATE_FAILED) from e


@router.get("/{user_id}", response_model=ApiResponse[UserResponse])
async def get_user(
    user_id: Annotated[str, Path(max_length=128)],
    db: DatabaseDep,
    user: dict[str, str] = Depends(require_permission(Permission.USER_READ)),
):
    """获取用户详情"""
    try:
        async with db.get_session() as session:
            repo = UserRepo(session, db.write_lock)
            target = await repo.get(user_id)
        if target is None:
            # FIXED: 原问题-中文硬编码detail
            raise HTTPException(status_code=404, detail=UserErrors.USER_NOT_FOUND)
        return ApiResponse(data=target)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_user failed: %s", e)
        # FIXED: 原问题-中文硬编码detail
        raise HTTPException(status_code=500, detail=UserErrors.LIST_FAILED) from e


@router.put("/{user_id}", response_model=ApiResponse[UserResponse])
async def update_user(
    user_id: Annotated[str, Path(max_length=128)],
    body: UserUpdate,
    db: DatabaseDep,
    user: dict[str, str] = Depends(require_permission(Permission.USER_UPDATE)),
    config: ConfigDep = None,
    request: Request = None,
    audit_svc: AuditServiceDep = None,  # FIXED-M03: FastAPI dependency injection provides the value
):
    # FIXED-M04: Protect admin role users from sensitive field modifications
    # Change from hardcoded username "admin" to role-based protection
    _SENSITIVE_FIELDS = frozenset({"password", "enabled", "role", "must_change_password"})
    _DEFAULT_PROTECTED_ROLES = ["admin"]

    try:
        async with db.get_session() as session:
            repo = UserRepo(session, db.write_lock)
            before_user = await repo.get(user_id)
            target_role = before_user["role"] if before_user else None
            data = body.model_dump(exclude_none=True)

            # FIXED-M04: Protect users with protected roles from sensitive field modifications
            protected_roles = _DEFAULT_PROTECTED_ROLES
            if config and hasattr(config, "security") and hasattr(config.security, "protected_roles"):
                protected_roles = config.security.protected_roles

            target_username = before_user["username"] if before_user else None
            if target_role and target_role in protected_roles:
                attempted_sensitive = _SENSITIVE_FIELDS & set(data.keys())
                if attempted_sensitive:
                    logger.warning(
                        "Blocked sensitive field modification on protected role '%s' account: "
                        "user=%s target_id=%s attempted_fields=%s",
                        target_role,
                        user.get("username"),
                        user_id,
                        list(attempted_sensitive),
                    )
                    raise HTTPException(
                        status_code=403,
                        detail=UserErrors.SENSITIVE_FIELD_DENIED,
                    )

            if "password" in data:
                # FIX-EL-GENERAL: 管理员改密也需走弱密码字典检查，与 auth.py change_password/reset_password 保持一致
                _raw_pwd = data["password"]
                if isinstance(_raw_pwd, str) and _raw_pwd.lower() in _WEAK_PASSWORDS:
                    raise HTTPException(status_code=400, detail=AuthErrors.PASSWORD_POLICY)
                data["password"] = hash_password(_raw_pwd)
            if "role" in data and data["role"] == "admin":
                if user.get("role") != "admin":
                    raise HTTPException(status_code=403, detail=AuthErrors.PERMISSION_DENIED)
            if "role" in data and data["role"] != "admin":
                # FIXED-P2: 原问题-重复调用repo.get(user_id)，before_user已在上方获取，直接复用
                if before_user and before_user["role"] == "admin":
                    admin_count = await repo.count_by_role("admin")
                    if admin_count <= 1:
                        # FIXED: 原问题-中文硬编码detail
                        raise HTTPException(status_code=403, detail=UserErrors.CANNOT_REMOVE_LAST_ADMIN)
            updated = await repo.update(user_id, data)
        if updated is None:
            # FIXED: 原问题-中文硬编码detail
            raise HTTPException(status_code=404, detail=UserErrors.USER_NOT_FOUND)

        # FIXED: Invalidate token renewal cache when user role or enabled status changes
        if target_username and ("role" in data or "enabled" in data):
            try:
                from edgelite.middleware.token_renewal import _invalidate_user_cache

                _invalidate_user_cache(target_username)
                logger.info("Token renewal cache invalidated for user %s after role/enabled update", target_username)
            except Exception as e:
                logger.warning("Failed to invalidate token cache for user %s: %s", target_username, e)

        # 第四轮修复: 检测 enabled 从 True→False 时主动撤销用户所有活跃 token
        _was_enabled = bool(before_user.get("enabled")) if before_user else False
        _now_disabled = data.get("enabled") is False
        if _was_enabled and _now_disabled:
            try:
                from edgelite.security.token_revocation import revoke_all_tokens_for_user

                revoked = await revoke_all_tokens_for_user(user_id)
                logger.info("Disabled user %s, revoked %d active token(s)", user_id, revoked)
            except Exception as e:
                logger.warning("Failed to revoke tokens for disabled user %s: %s", user_id, e)

        # FIX-EL-R2-SEVERE: 管理员通过 update_user 修改用户密码后必须撤销该用户所有活跃 token
        # 原问题：仅 auth.py change_password（用户自己改密）会撤销 session，admin 路径不撤销；
        # 攻击者获取旧密码后即使管理员重置密码，旧 token 在过期前仍可继续访问。
        # 修复：与 change_password 保持一致——密码变更后强制下线，要求重新登录。
        if "password" in data:
            try:
                from edgelite.security.token_revocation import revoke_all_tokens_for_user

                revoked = await revoke_all_tokens_for_user(user_id)
                logger.info(
                    "Password updated by admin for user %s, revoked %d active token(s)",
                    user_id,
                    revoked,
                )
            except Exception as e:
                logger.warning(
                    "Failed to revoke tokens after admin password update for user %s: %s",
                    user_id,
                    e,
                )

        try:
            # FIXED-M04: Use _get_client_ip for trusted proxy support
            from edgelite.api.auth import _get_client_ip
            from edgelite.services.audit_service import AuditAction

            client_ip = _get_client_ip(request) if request else ""
            before_safe = {k: v for k, v in (before_user or {}).items() if k != "password"} if before_user else None
            after_safe = {k: v for k, v in (updated or {}).items() if k != "password"} if updated else None
            await audit_svc.log(
                AuditAction.USER_UPDATE,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="user",
                resource_id=user_id,
                ip_address=client_ip,
                before_value=before_safe,
                after_value=after_safe,
            )
        except Exception as e:
            logger.warning("Audit log failed: %s", e)
        return ApiResponse(data=updated)
    except HTTPException:
        raise
    except StaleDataError as e:
        logger.warning("StaleDataError in update_user: %s", e)
        raise HTTPException(status_code=409, detail=RepoErrors.STALE_DATA_ERROR) from e
    except Exception as e:
        logger.error("update_user failed: %s", e)
        # FIXED: 原问题-中文硬编码detail
        raise HTTPException(status_code=500, detail=UserErrors.UPDATE_FAILED) from e


@router.delete("/{user_id}", response_model=ApiResponse)
async def delete_user(
    user_id: Annotated[str, Path(max_length=128)],
    db: DatabaseDep,
    user: dict[str, str] = Depends(require_permission(Permission.USER_DELETE)),
    config: ConfigDep = None,
    request: Request = None,
    audit_svc: AuditServiceDep = None,  # FIXED-M03: FastAPI dependency injection provides the value
):
    try:
        target_username = None
        if user_id == user.get("user_id"):
            # FIXED: 原问题-中文硬编码detail
            raise HTTPException(status_code=400, detail=UserErrors.CANNOT_DELETE_SELF)
        async with db.get_session() as session:
            repo = UserRepo(session, db.write_lock)
            target = await repo.get(user_id)
            # FIXED-M04: Role-based deletion protection (was hardcoded username "admin")
            _DEFAULT_PROTECTED_ROLES = ["admin"]
            protected_roles = _DEFAULT_PROTECTED_ROLES
            if config and hasattr(config, "security") and hasattr(config.security, "protected_roles"):
                protected_roles = config.security.protected_roles
            if target and target.get("role") in protected_roles:
                raise HTTPException(status_code=403, detail=UserErrors.CANNOT_DELETE_ADMIN)
            if target and target["role"] == "admin":
                admin_count = await repo.count_by_role("admin")
                if admin_count <= 1:
                    # FIXED: 原问题-中文硬编码detail
                    raise HTTPException(status_code=403, detail=UserErrors.CANNOT_DELETE_LAST_ADMIN)

            # FIXED-P1: 原问题-删除用户未检查其拥有的资源（设备、规则），直接删除导致孤儿数据；
            # 修复-删除前检查用户拥有的设备与规则，若有资源则返回 409 Conflict，
            # 提示用户先转移或删除相关资源（选项A：更安全，不提供 force 强删）
            if target:
                device_repo = DeviceRepo(session, db.write_lock)
                rule_repo = RuleRepo(session, db.write_lock)
                owned_device_ids = await device_repo.list_device_ids_by_owner(user_id)
                _, rule_total = await rule_repo.list_all(page=1, size=1, created_by=user_id)
                if owned_device_ids or rule_total > 0:
                    raise HTTPException(status_code=409, detail=UserErrors.HAS_RESOURCES)

            # Save before value for audit and cache invalidation
            target_username = target.get("username") if target else None
            before_safe = {k: v for k, v in (target or {}).items() if k != "password"} if target else None
            success = await repo.delete(user_id)
        if not success:
            # FIXED: 原问题-中文硬编码detail
            raise HTTPException(status_code=404, detail=UserErrors.USER_NOT_FOUND)

        # FIXED: Invalidate token renewal cache when user is deleted
        if target_username:
            try:
                from edgelite.middleware.token_renewal import _invalidate_user_cache

                _invalidate_user_cache(target_username)
                logger.info("Token renewal cache invalidated for deleted user %s", target_username)
            except Exception as e:
                logger.warning("Failed to invalidate token cache for user %s: %s", target_username, e)

        # 第四轮修复: 删除用户时主动撤销其所有活跃 token，防止已签发 token 继续有效
        try:
            from edgelite.security.token_revocation import revoke_all_tokens_for_user

            revoked = await revoke_all_tokens_for_user(user_id)
            if revoked:
                logger.info("Deleted user %s, revoked %d active token(s)", user_id, revoked)
        except Exception as e:
            logger.warning("Failed to revoke tokens for deleted user %s: %s", user_id, e)

        try:
            # FIXED-M04: Use _get_client_ip for trusted proxy support
            from edgelite.api.auth import _get_client_ip
            from edgelite.services.audit_service import AuditAction

            client_ip = _get_client_ip(request) if request else ""
            await audit_svc.log(
                AuditAction.USER_DELETE,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="user",
                resource_id=user_id,
                ip_address=client_ip,
                before_value=before_safe,
            )
        except Exception as e:
            logger.warning("Audit log failed: %s", e)
        return ApiResponse()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("delete_user failed: %s", e)
        # FIXED: 原问题-中文硬编码detail
        raise HTTPException(status_code=500, detail=UserErrors.DELETE_FAILED) from e
