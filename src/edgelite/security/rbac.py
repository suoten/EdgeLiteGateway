"""RBAC权限校验"""

from __future__ import annotations

import asyncio
from enum import StrEnum
from functools import wraps
from typing import Any

from fastapi import HTTPException, status

from edgelite.error_codes import AuthzErrors


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
    # SEC-FIX-V11: 写保护策略独立权限——与 DEVICE_UPDATE 分离，实现职责分离（SoD）
    # 拥有 DEVICE_UPDATE 的用户可改点位/采集间隔，但不能关闭写保护
    # 仅 ADMIN 拥有此权限，防止操作员越权降级写保护后恶意写入
    DEVICE_WRITE_POLICY_EDIT = "device:write_policy_edit"
    # FIXED-H02: 独立的 push 权限，不再授予隐式 admin
    DEVICE_PUSH = "device:push"
    # 规则管理
    RULE_CREATE = "rule:create"
    RULE_READ = "rule:read"
    RULE_UPDATE = "rule:update"
    RULE_DELETE = "rule:delete"
    RULE_TOGGLE = "rule:toggle"
    # 告警管理
    ALARM_READ = "alarm:read"
    ALARM_ACK = "alarm:ack"
    ALARM_DELETE = "alarm:delete"  # FIXED(严重): 物理删除告警需独立权限，仅 admin 拥有
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
    # 配置管理
    CONFIG_EDIT = "config:edit"
    # 驱动管理  # FIXED-P1: 缺少DRIVER_READ导致drivers.py模块导入失败→/api/v1/drivers/*全部404
    DRIVER_READ = "driver:read"
    CONFIG_VERSION_READ = "config_version:read"
    CONFIG_VERSION_EDIT = "config_version:edit"
    OTA_MANAGE = "ota:manage"


# RBAC权限矩阵
ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.ADMIN: frozenset(Permission),  # 全部权限
    Role.OPERATOR: frozenset(
        {
            Permission.DEVICE_READ,
            Permission.DRIVER_READ,
            Permission.RULE_READ,
            Permission.RULE_TOGGLE,
            Permission.ALARM_READ,
            Permission.ALARM_ACK,
            Permission.DATA_READ,
            Permission.DATA_EXPORT,
            Permission.VIDEO_READ,
            Permission.VIDEO_CONTROL,
            Permission.SYSTEM_READ,
            Permission.CONFIG_EDIT,
            Permission.CONFIG_VERSION_READ,
            Permission.CONFIG_VERSION_EDIT,
        }
    ),
    Role.VIEWER: frozenset(
        {
            Permission.DEVICE_READ,
            Permission.DRIVER_READ,
            Permission.RULE_READ,
            Permission.ALARM_READ,
            Permission.DATA_READ,
            Permission.DATA_EXPORT,
            Permission.VIDEO_READ,
            Permission.SYSTEM_READ,
            Permission.CONFIG_VERSION_READ,
        }
    ),
}


def has_permission(role: str | None, permission: Permission) -> bool:
    """检查角色是否拥有指定权限"""
    # FIXED-P1: 处理 None role，避免 AttributeError
    if role is None:
        return False
    try:
        role_enum = Role(role)
    except (ValueError, TypeError):
        return False
    return permission in ROLE_PERMISSIONS.get(role_enum, frozenset())


def _check_user_permission(current_user: Any, permission: Permission) -> None:
    """统一的权限校验逻辑。

    FIXED-P1: 提取公共逻辑，供同步/异步装饰器复用。
    处理 current_user 为 None 或缺少 role 属性的情况。
    """
    if current_user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=AuthzErrors.NOT_AUTHENTICATED,
        )
    # FIXED-P1: 使用 getattr 安全获取 role，避免 AttributeError
    role = getattr(current_user, "role", None)
    if not has_permission(role, permission):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=AuthzErrors.PERMISSION_DENIED,
        )


def require_permission(permission: Permission, allow_system: bool = False):
    """权限校验装饰器（用于服务层方法）

    FIXED-P1: 同时支持同步和异步函数。原实现仅支持 async，用于 sync 函数会运行时报错。
    FIXED(一般): 原问题-要求current_user关键字参数，内部系统调用无user上下文时被拦截;
    修复-添加allow_system参数，当allow_system=True且current_user为None时允许系统内部调用。

    Args:
        permission: 所需权限
        allow_system: 若为 True，当 current_user 为 None 时允许系统内部调用。
    """

    def decorator(func):
        if asyncio.iscoroutinefunction(func):
            # 异步函数
            @wraps(func)
            async def async_wrapper(*args, current_user=None, **kwargs):
                # FIXED(一般): allow_system=True时允许无user上下文的系统内部调用
                if current_user is None and allow_system:
                    return await func(*args, current_user=None, **kwargs)
                _check_user_permission(current_user, permission)
                return await func(*args, current_user=current_user, **kwargs)

            return async_wrapper
        else:
            # 同步函数
            @wraps(func)
            def sync_wrapper(*args, current_user=None, **kwargs):
                # FIXED(一般): allow_system=True时允许无user上下文的系统内部调用
                if current_user is None and allow_system:
                    return func(*args, current_user=None, **kwargs)
                _check_user_permission(current_user, permission)
                return func(*args, current_user=current_user, **kwargs)

            return sync_wrapper

    return decorator


def check_permission(role: str, permission: Permission) -> None:
    if not has_permission(role, permission):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=AuthzErrors.PERMISSION_DENIED,  # FIXED: 原问题-中文硬编码detail，改为error_code
        )


# FIXED-H02: API Key permission mappings
# API Key 不再隐式授予 admin 角色，而是绑定最小必要权限
class APIKeyPermission(StrEnum):
    """API Key 可绑定的权限范围（比 Role 更细粒度）"""

    METRICS_READ = "metrics:read"  # 仅可访问 /api/metrics 端点
    DEVICE_PUSH = "device:push"  # 仅可推送设备数据
    # 未来可扩展更多细粒度权限...


# API Key 默认权限（用于向后兼容）
_API_KEY_DEFAULT_SCOPES: dict[str, set[APIKeyPermission]] = {
    "server.webhook_api_key": {APIKeyPermission.DEVICE_PUSH},  # push 端点专用
    "grafana.api_key": {APIKeyPermission.METRICS_READ},  # metrics 端点专用
    "video.pygbsentry.api_key": {APIKeyPermission.METRICS_READ},  # video 服务也需要 metrics
}


def get_api_key_scopes(config_path: str) -> set[APIKeyPermission]:
    """获取指定 API Key 配置的权限范围。

    Args:
        config_path: 配置路径，如 "server.webhook_api_key" 或 "grafana.api_key"

    Returns:
        权限集合，默认为空集（需要显式配置）
    """
    return _API_KEY_DEFAULT_SCOPES.get(config_path, set())


def has_api_key_permission(config_path: str, required: APIKeyPermission) -> bool:
    """检查指定 API Key 是否拥有特定权限。

    Args:
        config_path: 配置路径
        required: 所需的 APIKeyPermission

    Returns:
        是否拥有该权限
    """
    scopes = get_api_key_scopes(config_path)
    return required in scopes
