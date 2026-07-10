"""API依赖注入"""

from __future__ import annotations

import contextvars
import logging
from datetime import UTC, datetime  # FIXED-P3: 原问题-timedelta导入未使用; 修复-移除
from typing import Annotated, Any  # FIXED: 原问题-缺少Any导入，依赖注入使用小写any

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWTError as JWTError  # FIXED-P0: 替换python-jose为PyJWT

from edgelite.api.error_codes import AuthErrors, CommonErrors
from edgelite.models.common import PaginationParams
from edgelite.security.jwt import verify_token
from edgelite.security.rbac import Permission, check_permission

logger = logging.getLogger(__name__)

_JTI_MANDATORY_AFTER = datetime(2026, 6, 9, 0, 0, 0, tzinfo=UTC)

# LP-02: HTTPBearer auto_error=False 以支持 Cookie fallback
security_scheme = HTTPBearer(auto_error=False)

# LP-02: Cookie 名称与 auth.py 保持一致
_AUTH_COOKIE_ACCESS = "edgelite_access"

_current_request: contextvars.ContextVar[Request | None] = contextvars.ContextVar("_current_request", default=None)


def _get_request() -> Request:  # FIXED-P0: 7个API模块依赖此函数获取Request对象
    request = _current_request.get()
    if request is None:
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY)
    return request


def _get_container(request: Request):
    return request.app.state


def _extract_token_from_request(request: Request, credentials: HTTPAuthorizationCredentials | None) -> str:
    """LP-02: 从 Authorization header 或 HttpOnly Cookie 提取 access token。

    优先从 Authorization header 读取（向后兼容），fallback 到 Cookie。
    """
    if credentials and credentials.credentials:
        return credentials.credentials
    # Fallback: 从 HttpOnly Cookie 提取
    return request.cookies.get(_AUTH_COOKIE_ACCESS, "")


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security_scheme)],
    request: Request,
) -> dict[str, str]:
    """Authenticate user and return user info.

    LP-02: 支持 Authorization header 和 HttpOnly Cookie 两种认证方式。
    优先使用 Authorization header（向后兼容），fallback 到 Cookie。

    FIXED-L03: Role refresh mechanism:
    - Token payload contains 'role' field for quick reference, but this is NOT authoritative
    - On every API call, this function queries the database for the current role
    - The returned role is ALWAYS from the database, ensuring role changes take effect immediately
    - Role changes take effect on the NEXT API call (no token refresh needed)

    This design prevents privilege escalation: even if a user's role is demoted,
    the new permissions take effect immediately on the next request.
    """
    token = _extract_token_from_request(request, credentials)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=AuthErrors.TOKEN_INVALID,
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = verify_token(token, token_type="access")
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=AuthErrors.TOKEN_INVALID,
            headers={"WWW-Authenticate": "Bearer"},
        ) from None

    jti = payload.get("jti")
    if jti:
        from edgelite.security.token_revocation import is_token_revoked

        if is_token_revoked(jti):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=AuthErrors.TOKEN_REVOKED,
                headers={"WWW-Authenticate": "Bearer"},
            )
    else:
        now = datetime.now(UTC)
        if now >= _JTI_MANDATORY_AFTER:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=AuthErrors.TOKEN_LEGACY_FORMAT,
                headers={"WWW-Authenticate": "Bearer"},
            )
        remaining = _JTI_MANDATORY_AFTER - now
        logger.warning(
            "Token has no jti field (legacy format), skipping revocation check. "
            "jti will become mandatory in %.1f days. username=%s",
            remaining.total_seconds() / 86400,
            payload.get("username", "unknown"),
        )

    username = payload.get("username", "")
    container = _get_container(request)

    # FIXED-C02: 需要读取完整 user 数据以检查 password_changed_at
    try:
        from edgelite.storage.sqlite_repo import (
            UserRepo,  # FIXED-P3: 原问题-UserORM导入未使用; 修复-移除
        )

        async with container.database.get_session() as session:
            repo = UserRepo(session, container.database.write_lock)
            user = await repo.get_by_username(username)
    except Exception as e:
        logger.error("Auth query user failed: %s", e)  # FIXED-P3: 中文日志→英文
        raise HTTPException(503, CommonErrors.DB_NOT_READY) from None

    if user is None or not user["enabled"]:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=AuthErrors.USER_NOT_FOUND,
        )

    # FIXED-C02: 检查 Token 是否在密码修改之前签发
    token_iat = payload.get("iat")
    password_changed_at = user.get("password_changed_at")
    if token_iat and password_changed_at:
        # 将 password_changed_at 转换为 Unix 时间戳进行比较
        if isinstance(password_changed_at, str):
            try:
                pwd_changed_ts = datetime.fromisoformat(password_changed_at).timestamp()
            except (ValueError, TypeError):
                pwd_changed_ts = 0
        elif isinstance(password_changed_at, datetime):
            pwd_changed_ts = password_changed_at.timestamp()
        else:
            pwd_changed_ts = 0

        if token_iat < pwd_changed_ts:
            logger.warning(
                "Token rejected: issued before password change. username=%s token_iat=%s password_changed_at=%s",
                username,
                token_iat,
                pwd_changed_ts,
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=AuthErrors.TOKEN_PASSWORD_CHANGED,
                headers={"WWW-Authenticate": "Bearer"},
            )

    if user.get("must_change_password"):
        request_path = request.url.path
        allowed_paths = {"/api/v1/auth/change-password", "/api/v1/auth/me", "/api/v1/auth/logout"}
        if request_path not in allowed_paths:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=AuthErrors.MUST_CHANGE_PASSWORD,
            )

    return {
        "user_id": user["user_id"],
        "username": user["username"],
        "role": user["role"],
    }


CurrentUser = Annotated[dict[str, str], Depends(get_current_user)]


async def get_optional_current_user(
    request: Request,
) -> dict[str, str] | None:
    """FIXED-M02: Optional authentication - returns None only if no credentials provided.

    Use this when authentication is optional (e.g., API key or Bearer token).

    LP-02: 支持 Authorization header 和 HttpOnly Cookie 两种认证方式。

    Returns:
        - User dict: if valid Bearer token or Cookie token provided
        - None: if no Authorization header and no Cookie, or credentials are invalid/missing
        - Raises HTTPException: if credentials are provided but account is disabled,
          token is revoked, or other auth failure (not missing credentials)
    """
    # Check if Authorization header is present and has Bearer prefix
    auth_header = request.headers.get("Authorization", "")
    cookie_token = request.cookies.get(_AUTH_COOKIE_ACCESS, "")

    if not auth_header.startswith("Bearer ") and not cookie_token:
        # No valid Bearer credentials and no Cookie provided at all
        return None

    # Credentials provided - must validate them properly
    # If validation fails due to disabled account, revoked token, etc.,
    # the exception will propagate (not caught here)
    try:
        if auth_header.startswith("Bearer "):
            # FIXED-P0-5: get_current_user需要credentials参数，手动构造HTTPAuthorizationCredentials
            from fastapi.security import HTTPAuthorizationCredentials

            token = auth_header[7:]  # 去掉 "Bearer " 前缀
            credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        else:
            # LP-02: 使用 Cookie 中的 token
            credentials = None
        return await get_current_user(credentials=credentials, request=request)
    except HTTPException as e:
        # Only return None for missing/invalid credentials (401)
        # Re-raise 403 (disabled account), 503 (db error), etc.
        if e.status_code == status.HTTP_401_UNAUTHORIZED:
            return None
        raise


OptionalCurrentUser = Annotated[dict[str, str] | None, Depends(get_optional_current_user)]


async def get_optional_user(request: Request) -> dict[str, str] | None:
    """R11-API-07: 尝试获取已认证用户，未认证或认证失败时返回 None（不抛异常）。

    公共复用函数，供 integration_health_check 等需要"可选认证"的端点使用，
    避免在各端点内联重复的认证逻辑。参考 health.py:_get_optional_user 实现。
    与 get_optional_current_user 的差异：本函数在禁用用户/撤销 token/DB 异常时
    一律返回 None（降级为未认证），适用于健康检查等非关键路径。
    """
    try:
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return None

        token = auth_header[7:]
        payload = verify_token(token, token_type="access")
        if not payload:
            return None

        jti = payload.get("jti", "")
        if jti:
            from edgelite.security.token_revocation import is_token_revoked

            if is_token_revoked(jti):
                return None

        username = payload.get("username", "")
        if not username:
            return None

        container = _get_container(request)
        from edgelite.storage.sqlite_repo import UserRepo

        async with container.database.get_session() as session:
            repo = UserRepo(session, container.database.write_lock)
            user = await repo.get_by_username(username)

        if user is None or not user["enabled"]:
            return None

        return {
            "user_id": user["user_id"],
            "username": user["username"],
            "role": user["role"],
        }
    except Exception as e:
        logger.warning("get_optional_user failed: %s", e)
        return None


def require_permission(permission: Permission):
    async def _check(user: CurrentUser) -> dict[str, str]:
        check_permission(user["role"], permission)
        return user

    return _check


async def get_device_service(request: Request):
    try:  # FIXED: 原问题-属性不存在时抛出AttributeError导致500
        svc = _get_container(request).device_service
    except AttributeError:
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY) from None
    if svc is None:
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY)
    return svc


async def get_rule_service(request: Request):
    try:  # FIXED: 原问题-属性不存在时抛出AttributeError导致500
        svc = _get_container(request).rule_service
    except AttributeError:
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY) from None
    if svc is None:
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY)
    return svc


async def get_alarm_service(request: Request):
    try:  # FIXED: 原问题-属性不存在时抛出AttributeError导致500
        svc = _get_container(request).alarm_service
    except AttributeError:
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY) from None
    if svc is None:
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY)
    return svc


async def get_data_service(request: Request):
    try:  # FIXED: 原问题-属性不存在时抛出AttributeError导致500
        svc = _get_container(request).data_service
    except AttributeError:
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY) from None
    if svc is None:
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY)
    return svc


async def get_system_service(request: Request):
    try:  # FIXED: 原问题-属性不存在时抛出AttributeError导致500
        svc = _get_container(request).system_service
    except AttributeError:
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY) from None
    if svc is None:
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY)
    return svc


async def get_video_service(request: Request):
    try:  # FIXED: 原问题-属性不存在时抛出AttributeError导致500
        svc = _get_container(request).video_service
    except AttributeError:
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY) from None
    if svc is None:
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY)
    return svc


async def get_audit_service(request: Request):
    try:  # FIXED: 原问题-属性不存在时抛出AttributeError导致500
        svc = _get_container(request).audit_service
    except AttributeError:
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY) from None
    if svc is None:
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY)
    return svc


async def get_driver_registry(request: Request):
    try:  # FIXED: 原问题-属性不存在时抛出AttributeError导致500
        reg = _get_container(request).driver_registry
    except AttributeError:
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY) from None
    if reg is None:
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY)
    return reg


async def get_driver_registry_optional(request: Request):
    try:
        reg = _get_container(request).driver_registry
    except AttributeError:
        return None
    return reg


async def get_database(request: Request):
    try:  # FIXED: 原问题-属性不存在时抛出AttributeError导致500
        db = _get_container(request).database
    except AttributeError:
        raise HTTPException(503, CommonErrors.DB_NOT_READY) from None
    if db is None:
        raise HTTPException(503, CommonErrors.DB_NOT_READY)
    return db


async def get_config(request: Request):
    try:  # FIXED: 原问题-属性不存在时抛出AttributeError导致500
        config = _get_container(request).config
    except AttributeError:
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY) from None
    # FIXED: 原问题-config可能为None时直接返回，调用方访问属性崩溃
    if config is None:
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY)
    return config


async def get_platform_handlers(request: Request):
    try:  # FIXED: 原问题-属性不存在时抛出AttributeError导致500
        handlers = _get_container(request).platform_handlers
    except AttributeError:
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY) from None
    # FIXED: 原问题-platform_handlers可能为None时直接返回
    if handlers is None:
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY)
    return handlers


async def get_integration_endpoint(request: Request):
    try:  # FIXED: 原问题-属性不存在时抛出AttributeError导致500
        ep = _get_container(request).integration_endpoint
    except AttributeError:
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY) from None
    if ep is None:
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY)
    return ep


async def get_mqtt_server(request: Request):
    try:  # FIXED: 原问题-属性不存在时抛出AttributeError导致500
        svc = _get_container(request).mqtt_server
    except AttributeError:
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY) from None
    # FIXED: 原问题-返回可能为None的对象，调用方直接访问属性崩溃
    if svc is None:
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY)
    return svc


async def get_modbus_slave(request: Request):
    try:  # FIXED: 原问题-属性不存在时抛出AttributeError导致500
        svc = _get_container(request).modbus_slave
    except AttributeError:
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY) from None
    # FIXED: 原问题-返回可能为None的对象，调用方直接访问属性崩溃
    if svc is None:
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY)
    return svc


async def get_serial_bridge(request: Request):
    try:  # FIXED: 原问题-属性不存在时抛出AttributeError导致500
        svc = _get_container(request).serial_bridge
    except AttributeError:
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY) from None
    # FIXED: 原问题-返回可能为None的对象，调用方直接访问属性崩溃
    if svc is None:
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY)
    return svc


async def get_preprocessor(request: Request):
    try:  # FIXED: 原问题-属性不存在时抛出AttributeError导致500
        svc = _get_container(request).preprocessor
    except AttributeError:
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY) from None
    # FIXED: 原问题-返回可能为None的对象，调用方直接访问属性崩溃
    if svc is None:
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY)
    return svc


async def get_app_updater(request: Request):
    try:
        svc = _get_container(request).app_updater
    except AttributeError:
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY) from None
    if svc is None:
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY)
    return svc


async def get_plugin_manager(request: Request):
    try:  # FIXED: 原问题-属性不存在时抛出AttributeError导致500
        svc = _get_container(request).plugin_manager
    except AttributeError:
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY) from None
    # FIXED: 原问题-返回可能为None的对象，调用方直接访问属性崩溃
    if svc is None:
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY)
    return svc


async def get_event_bus(request: Request):
    try:
        svc = _get_container(request).event_bus
    except AttributeError:
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY) from None
    if svc is None:
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY)
    return svc


async def get_scheduler(request: Request):
    try:
        svc = _get_container(request).scheduler
    except AttributeError:
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY) from None
    if svc is None:
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY)
    return svc


DeviceServiceDep = Annotated[Any, Depends(get_device_service)]  # FIXED: 原问题-小写any导致类型信息丢失
RuleServiceDep = Annotated[Any, Depends(get_rule_service)]
AlarmServiceDep = Annotated[Any, Depends(get_alarm_service)]
DataServiceDep = Annotated[Any, Depends(get_data_service)]
SystemServiceDep = Annotated[Any, Depends(get_system_service)]
VideoServiceDep = Annotated[Any, Depends(get_video_service)]
AuditServiceDep = Annotated[Any, Depends(get_audit_service)]
DriverRegistryDep = Annotated[Any, Depends(get_driver_registry)]
DriverRegistryDepOptional = Annotated[Any | None, Depends(get_driver_registry_optional)]
DatabaseDep = Annotated[Any, Depends(get_database)]
ConfigDep = Annotated[Any, Depends(get_config)]
PlatformHandlersDep = Annotated[dict, Depends(get_platform_handlers)]
IntegrationEndpointDep = Annotated[Any, Depends(get_integration_endpoint)]
MqttServerDep = Annotated[Any, Depends(get_mqtt_server)]
ModbusSlaveDep = Annotated[Any, Depends(get_modbus_slave)]
SerialBridgeDep = Annotated[Any, Depends(get_serial_bridge)]
PreprocessorDep = Annotated[Any, Depends(get_preprocessor)]
AppUpdaterDep = Annotated[Any, Depends(get_app_updater)]
PluginManagerDep = Annotated[Any, Depends(get_plugin_manager)]
EventBusDep = Annotated[Any, Depends(get_event_bus)]
SchedulerDep = Annotated[Any, Depends(get_scheduler)]
PaginationDep = Annotated[PaginationParams, Depends()]
