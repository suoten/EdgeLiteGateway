"""API依赖注入"""

from __future__ import annotations

import logging
from typing import Annotated, Any  # FIXED: 原问题-缺少Any导入，依赖注入使用小写any

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError

from edgelite.api.error_codes import AuthErrors, CommonErrors
from edgelite.models.common import PaginationParams
from edgelite.security.jwt import verify_token
from edgelite.security.rbac import Permission, check_permission

logger = logging.getLogger(__name__)

security_scheme = HTTPBearer()


def _get_container(request: Request):
    return request.app.state


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(security_scheme)],
    request: Request,
) -> dict[str, str]:
    try:
        payload = verify_token(credentials.credentials, token_type="access")
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
        logger.warning("Token无jti字段(旧格式)，跳过撤销检查")

    username = payload.get("username", "")
    container = _get_container(request)
    # FIXED: 原问题-get_current_user中数据库查询无try-except保护，数据库不可用时返回500而非503
    try:
        from edgelite.storage.sqlite_repo import UserRepo

        async with container.database.get_session() as session:
            repo = UserRepo(session, container.database.write_lock)
            user = await repo.get_by_username(username)
    except Exception as e:
        logger.error("认证查询用户失败: %s", e)
        raise HTTPException(503, CommonErrors.DB_NOT_READY) from None

    if user is None or not user["enabled"]:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=AuthErrors.USER_NOT_FOUND,
        )

    return {
        "user_id": user["user_id"],
        "username": user["username"],
        "role": user["role"],
    }


CurrentUser = Annotated[dict[str, str], Depends(get_current_user)]


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


async def get_ota_manager(request: Request):
    try:  # FIXED: 原问题-属性不存在时抛出AttributeError导致500
        svc = _get_container(request).ota_manager
    except AttributeError:
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY) from None
    # FIXED: 原问题-返回可能为None的对象，调用方直接访问属性崩溃
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
DatabaseDep = Annotated[Any, Depends(get_database)]
ConfigDep = Annotated[Any, Depends(get_config)]
PlatformHandlersDep = Annotated[dict, Depends(get_platform_handlers)]
IntegrationEndpointDep = Annotated[Any, Depends(get_integration_endpoint)]
MqttServerDep = Annotated[Any, Depends(get_mqtt_server)]
ModbusSlaveDep = Annotated[Any, Depends(get_modbus_slave)]
SerialBridgeDep = Annotated[Any, Depends(get_serial_bridge)]
PreprocessorDep = Annotated[Any, Depends(get_preprocessor)]
OtaManagerDep = Annotated[Any, Depends(get_ota_manager)]
PluginManagerDep = Annotated[Any, Depends(get_plugin_manager)]
EventBusDep = Annotated[Any, Depends(get_event_bus)]
SchedulerDep = Annotated[Any, Depends(get_scheduler)]
PaginationDep = Annotated[PaginationParams, Depends()]
