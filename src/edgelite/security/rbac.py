"""RBAC权限校验"""

from __future__ import annotations

from enum import Enum

try:
    from enum import StrEnum
except ImportError:
    class StrEnum(str, Enum):
        pass
from functools import wraps

from fastapi import HTTPException, status


class Role(StrEnum):
    ADMIN = "admin"
    OPERATOR = "operator"
    VIEWER = "viewer"


class Permission(StrEnum):
    # 设备管理
    DEVICE_CREATE = "device:create"
    DEVICE_READ = "device:read"
    DEVICE_UPDATE = "device:update"
    DEVICE_DELETE = "device:delete"
    DEVICE_WRITE_POINT = "device:write_point"
    # 规则管理
    RULE_CREATE = "rule:create"
    RULE_READ = "rule:read"
    RULE_UPDATE = "rule:update"
    RULE_DELETE = "rule:delete"
    RULE_TOGGLE = "rule:toggle"
    # 告警管理
    ALARM_READ = "alarm:read"
    ALARM_ACK = "alarm:ack"
    # 数据查询
    DATA_READ = "data:read"
    DATA_EXPORT = "data:export"
    # 视频接入
    VIDEO_READ = "video:read"
    VIDEO_CONTROL = "video:control"
    # 系统管理
    SYSTEM_READ = "system:read"
    SYSTEM_MANAGE = "system:manage"
    # 用户管理
    USER_CREATE = "user:create"
    USER_READ = "user:read"
    USER_UPDATE = "user:update"
    USER_DELETE = "user:delete"


# RBAC权限矩阵
ROLE_PERMISSIONS: dict[Role, set[Permission]] = {
    Role.ADMIN: set(Permission),  # 全部权限
    Role.OPERATOR: {
        Permission.DEVICE_READ,
        Permission.RULE_READ,
        Permission.RULE_TOGGLE,
        Permission.ALARM_READ,
        Permission.ALARM_ACK,
        Permission.DATA_READ,
        Permission.DATA_EXPORT,
        Permission.VIDEO_READ,
        Permission.VIDEO_CONTROL,
        Permission.SYSTEM_READ,
    },
    Role.VIEWER: {
        Permission.DEVICE_READ,
        Permission.RULE_READ,
        Permission.ALARM_READ,
        Permission.DATA_READ,
        Permission.DATA_EXPORT,
        Permission.VIDEO_READ,
        Permission.SYSTEM_READ,
    },
}


def has_permission(role: str, permission: Permission) -> bool:
    """检查角色是否拥有指定权限"""
    try:
        role_enum = Role(role)
    except ValueError:
        return False
    return permission in ROLE_PERMISSIONS.get(role_enum, set())


def require_permission(permission: Permission):
    """权限校验装饰器（用于服务层方法）"""

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, current_user=None, **kwargs):
            if current_user is None:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未认证")
            if not has_permission(current_user.role, permission):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="权限不足")
            return await func(*args, current_user=current_user, **kwargs)

        return wrapper

    return decorator


def check_permission(role: str, permission: Permission) -> None:
    """权限校验函数（用于API依赖注入）"""
    if not has_permission(role, permission):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"权限不足: 需要 {permission.value}",
        )
