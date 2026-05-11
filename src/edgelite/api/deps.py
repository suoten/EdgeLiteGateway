"""API依赖注入"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError

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
            detail="Token无效或已过期",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None

    jti = payload.get("jti")
    if jti:
        from edgelite.security.token_revocation import is_token_revoked

        if is_token_revoked(jti):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token已撤销",
                headers={"WWW-Authenticate": "Bearer"},
            )
    else:
        logger.warning("Token无jti字段(旧格式)，跳过撤销检查")

    username = payload.get("username", "")
    container = _get_container(request)
    from edgelite.storage.sqlite_repo import UserRepo

    async with container.database.get_session() as session:
        repo = UserRepo(session, container.database.write_lock)
        user = await repo.get_by_username(username)

    if user is None or not user["enabled"]:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户不存在或已禁用",
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
    svc = _get_container(request).device_service
    if svc is None:
        raise HTTPException(503, "设备服务未就绪")
    return svc


async def get_rule_service(request: Request):
    svc = _get_container(request).rule_service
    if svc is None:
        raise HTTPException(503, "规则服务未就绪")
    return svc


async def get_alarm_service(request: Request):
    svc = _get_container(request).alarm_service
    if svc is None:
        raise HTTPException(503, "告警服务未就绪")
    return svc


async def get_data_service(request: Request):
    svc = _get_container(request).data_service
    if svc is None:
        raise HTTPException(503, "数据服务未就绪")
    return svc


async def get_system_service(request: Request):
    svc = _get_container(request).system_service
    if svc is None:
        raise HTTPException(503, "系统服务未就绪")
    return svc


async def get_video_service(request: Request):
    svc = _get_container(request).video_service
    if svc is None:
        raise HTTPException(503, "视频服务未就绪")
    return svc


async def get_audit_service(request: Request):
    svc = _get_container(request).audit_service
    if svc is None:
        raise HTTPException(503, "审计服务未就绪")
    return svc


async def get_driver_registry(request: Request):
    reg = _get_container(request).driver_registry
    if reg is None:
        raise HTTPException(503, "驱动注册表未就绪")
    return reg


async def get_database(request: Request):
    db = _get_container(request).database
    if db is None:
        raise HTTPException(503, "数据库未就绪")
    return db


async def get_config(request: Request):
    return _get_container(request).config


async def get_platform_handlers(request: Request):
    return _get_container(request).platform_handlers


async def get_integration_endpoint(request: Request):
    ep = _get_container(request).integration_endpoint
    if ep is None:
        raise HTTPException(503, "集成端点未就绪")
    return ep


async def get_mqtt_server(request: Request):
    return _get_container(request).mqtt_server


async def get_modbus_slave(request: Request):
    return _get_container(request).modbus_slave


async def get_serial_bridge(request: Request):
    return _get_container(request).serial_bridge


async def get_preprocessor(request: Request):
    return _get_container(request).preprocessor


async def get_ota_manager(request: Request):
    return _get_container(request).ota_manager


async def get_plugin_manager(request: Request):
    return _get_container(request).plugin_manager


async def get_event_bus(request: Request):
    return _get_container(request).event_bus


DeviceServiceDep = Annotated[any, Depends(get_device_service)]
RuleServiceDep = Annotated[any, Depends(get_rule_service)]
AlarmServiceDep = Annotated[any, Depends(get_alarm_service)]
DataServiceDep = Annotated[any, Depends(get_data_service)]
SystemServiceDep = Annotated[any, Depends(get_system_service)]
VideoServiceDep = Annotated[any, Depends(get_video_service)]
AuditServiceDep = Annotated[any, Depends(get_audit_service)]
DriverRegistryDep = Annotated[any, Depends(get_driver_registry)]
DatabaseDep = Annotated[any, Depends(get_database)]
ConfigDep = Annotated[any, Depends(get_config)]
PlatformHandlersDep = Annotated[dict, Depends(get_platform_handlers)]
IntegrationEndpointDep = Annotated[any, Depends(get_integration_endpoint)]
MqttServerDep = Annotated[any, Depends(get_mqtt_server)]
ModbusSlaveDep = Annotated[any, Depends(get_modbus_slave)]
SerialBridgeDep = Annotated[any, Depends(get_serial_bridge)]
PreprocessorDep = Annotated[any, Depends(get_preprocessor)]
OtaManagerDep = Annotated[any, Depends(get_ota_manager)]
PluginManagerDep = Annotated[any, Depends(get_plugin_manager)]
EventBusDep = Annotated[any, Depends(get_event_bus)]
