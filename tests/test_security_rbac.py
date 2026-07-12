"""RBAC 权限校验单元测试。

覆盖 src/edgelite/security/rbac.py：角色权限矩阵、has_permission、
require_permission 装饰器(同步+异步)、check_permission、API Key 权限范围。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from edgelite.security.rbac import (
    APIKeyPermission,
    Permission,
    Role,
    ROLE_PERMISSIONS,
    check_permission,
    get_api_key_scopes,
    has_api_key_permission,
    has_permission,
    require_permission,)


def _make_user(role: str = "admin"):
    """构造带 role 属性的 user 对象（rbac 用 getattr 取 role，dict 不支持）。"""
    return SimpleNamespace(role=role)


# ─── Role / Permission 枚举 ───


def test_role_values():
    assert Role.ADMIN == "admin"
    assert Role.OPERATOR == "operator"
    assert Role.VIEWER == "viewer"


def test_permission_count():
    """Permission 枚举应包含 30+ 权限。"""
    assert len(list(Permission)) >= 30


# ─── ROLE_PERMISSIONS 矩阵 ───


def test_admin_has_all_permissions():
    """ADMIN 拥有全部权限。"""
    assert ROLE_PERMISSIONS[Role.ADMIN] == frozenset(Permission)


def test_operator_permissions_subset():
    """OPERATOR 权限是 ADMIN 的子集。"""
    assert ROLE_PERMISSIONS[Role.OPERATOR] < ROLE_PERMISSIONS[Role.ADMIN]


def test_viewer_permissions_subset():
    """VIEWER 权限是 OPERATOR 的子集。"""
    assert ROLE_PERMISSIONS[Role.VIEWER] < ROLE_PERMISSIONS[Role.OPERATOR]


def test_admin_has_write_policy_edit():
    """仅 ADMIN 拥有 DEVICE_WRITE_POLICY_EDIT。"""
    assert Permission.DEVICE_WRITE_POLICY_EDIT in ROLE_PERMISSIONS[Role.ADMIN]
    assert Permission.DEVICE_WRITE_POLICY_EDIT not in ROLE_PERMISSIONS[Role.OPERATOR]
    assert Permission.DEVICE_WRITE_POLICY_EDIT not in ROLE_PERMISSIONS[Role.VIEWER]


def test_admin_has_alarm_delete():
    """仅 ADMIN 拥有 ALARM_DELETE。"""
    assert Permission.ALARM_DELETE in ROLE_PERMISSIONS[Role.ADMIN]
    assert Permission.ALARM_DELETE not in ROLE_PERMISSIONS[Role.OPERATOR]
    assert Permission.ALARM_DELETE not in ROLE_PERMISSIONS[Role.VIEWER]


def test_operator_has_config_edit():
    """OPERATOR 拥有 CONFIG_EDIT。"""
    assert Permission.CONFIG_EDIT in ROLE_PERMISSIONS[Role.OPERATOR]


def test_viewer_no_config_edit():
    """VIEWER 无 CONFIG_EDIT。"""
    assert Permission.CONFIG_EDIT not in ROLE_PERMISSIONS[Role.VIEWER]


# ─── has_permission ───


def test_has_permission_admin_yes():
    assert has_permission("admin", Permission.DEVICE_CREATE) is True


def test_has_permission_viewer_read_yes():
    assert has_permission("viewer", Permission.DEVICE_READ) is True


def test_has_permission_viewer_delete_no():
    assert has_permission("viewer", Permission.DEVICE_DELETE) is False


def test_has_permission_operator_alarm_ack_yes():
    assert has_permission("operator", Permission.ALARM_ACK) is True


def test_has_permission_operator_alarm_delete_no():
    assert has_permission("operator", Permission.ALARM_DELETE) is False


def test_has_permission_none_role():
    assert has_permission(None, Permission.DEVICE_READ) is False


def test_has_permission_invalid_role():
    assert has_permission("superadmin", Permission.DEVICE_READ) is False


def test_has_permission_empty_role():
    assert has_permission("", Permission.DEVICE_READ) is False


# ─── check_permission ───


def test_check_permission_ok():
    """有权限时不抛异常。"""
    check_permission("admin", Permission.DEVICE_READ)


def test_check_permission_denied():
    """无权限时抛 403。"""
    with pytest.raises(HTTPException) as exc_info:
        check_permission("viewer", Permission.DEVICE_DELETE)
    assert exc_info.value.status_code == 403


# ─── require_permission 装饰器（同步） ───


def test_require_permission_sync_allowed():
    """同步函数有权限时正常执行。"""

    @require_permission(Permission.DEVICE_READ)
    def sync_func(*, current_user=None):
        return "ok"

    result = sync_func(current_user=_make_user("admin"))
    assert result == "ok"


def test_require_permission_sync_denied():
    """同步函数无权限时抛 403。"""

    @require_permission(Permission.DEVICE_DELETE)
    def sync_func(*, current_user=None):
        return "ok"

    with pytest.raises(HTTPException) as exc_info:
        sync_func(current_user=_make_user("viewer"))
    assert exc_info.value.status_code == 403


def test_require_permission_sync_no_user():
    """同步函数无 current_user 时抛 401。"""

    @require_permission(Permission.DEVICE_READ)
    def sync_func(*, current_user=None):
        return "ok"

    with pytest.raises(HTTPException) as exc_info:
        sync_func()
    assert exc_info.value.status_code == 401


def test_require_permission_sync_allow_system():
    """allow_system=True 允许无 user 的系统内部调用。"""

    @require_permission(Permission.DEVICE_READ, allow_system=True)
    def sync_func(*, current_user=None):
        return "system-ok"

    result = sync_func()
    assert result == "system-ok"


# ─── require_permission 装饰器（异步） ───


@pytest.mark.asyncio
async def test_require_permission_async_allowed():
    """异步函数有权限时正常执行。"""

    @require_permission(Permission.DEVICE_READ)
    async def async_func(*, current_user=None):
        return "async-ok"

    result = await async_func(current_user=_make_user("admin"))
    assert result == "async-ok"


@pytest.mark.asyncio
async def test_require_permission_async_denied():
    """异步函数无权限时抛 403。"""

    @require_permission(Permission.DEVICE_DELETE)
    async def async_func(*, current_user=None):
        return "ok"

    with pytest.raises(HTTPException) as exc_info:
        await async_func(current_user=_make_user("viewer"))
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_require_permission_async_no_user():
    """异步函数无 current_user 时抛 401。"""

    @require_permission(Permission.DEVICE_READ)
    async def async_func(*, current_user=None):
        return "ok"

    with pytest.raises(HTTPException) as exc_info:
        await async_func()
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_require_permission_async_allow_system():
    """异步函数 allow_system=True 允许系统调用。"""

    @require_permission(Permission.DEVICE_READ, allow_system=True)
    async def async_func(*, current_user=None):
        return "system-async-ok"

    result = await async_func()
    assert result == "system-async-ok"


def test_require_permission_preserves_function_name():
    """装饰器保留原函数名。"""

    @require_permission(Permission.DEVICE_READ)
    def my_function(*, current_user=None):
        return "ok"

    assert my_function.__name__ == "my_function"


# ─── APIKeyPermission ───


def test_api_key_permission_values():
    assert APIKeyPermission.METRICS_READ == "metrics:read"
    assert APIKeyPermission.DEVICE_PUSH == "device:push"


def test_get_api_key_scopes_webhook():
    """webhook API key 拥有 DEVICE_PUSH。"""
    scopes = get_api_key_scopes("server.webhook_api_key")
    assert APIKeyPermission.DEVICE_PUSH in scopes


def test_get_api_key_scopes_grafana():
    """grafana API key 拥有 METRICS_READ。"""
    scopes = get_api_key_scopes("grafana.api_key")
    assert APIKeyPermission.METRICS_READ in scopes


def test_get_api_key_scopes_video():
    """video API key 拥有 METRICS_READ。"""
    scopes = get_api_key_scopes("video.pygbsentry.api_key")
    assert APIKeyPermission.METRICS_READ in scopes


def test_get_api_key_scopes_unknown_empty():
    """未知配置路径返回空集。"""
    scopes = get_api_key_scopes("unknown.api_key")
    assert len(scopes) == 0


def test_has_api_key_permission_yes():
    assert has_api_key_permission("server.webhook_api_key", APIKeyPermission.DEVICE_PUSH) is True


def test_has_api_key_permission_no():
    assert has_api_key_permission("server.webhook_api_key", APIKeyPermission.METRICS_READ) is False


def test_has_api_key_permission_unknown_config():
    assert has_api_key_permission("unknown", APIKeyPermission.METRICS_READ) is False
