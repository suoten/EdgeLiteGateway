"""服务管理API路由 - 统一管理所有可选服务的启停、依赖和配置"""

from __future__ import annotations

import logging
from enum import Enum

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from edgelite.api.deps import AuditServiceDep, require_permission
from edgelite.api.error_codes import ServiceErrors
from edgelite.models.common import ApiResponse
from edgelite.security.rbac import Permission
from edgelite.services.service_manager import SERVICE_DEFINITIONS, get_service_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/services", tags=["Services"])


class EnableServiceRequest(BaseModel):
    config: dict | None = (
        None  # FIXED: 原问题-dict类型参数无schema校验，此处为动态配置场景，schema由驱动/平台运行时决定
    )


class UpdateServiceConfigRequest(BaseModel):
    config: dict  # FIXED: 原问题-dict类型参数无schema校验，此处为动态配置场景，schema由驱动/平台运行时决定


class InstallDepsRequest(BaseModel):
    package: str | None = None


@router.get("/list", response_model=ApiResponse)
async def list_services(
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    mgr = get_service_manager()
    try:
        services = []
        for info in mgr.list_services():
            svc_def = SERVICE_DEFINITIONS.get(info.name, {})
            services.append(
                {
                    "name": info.name,
                    "display_name": info.display_name,
                    "description": info.description,
                    "icon": svc_def.get("icon", ""),
                    "category": svc_def.get("category", ""),
                    "state": info.state.value,
                    "config_section": info.config_section,
                    "dependencies": [
                        {"package": d.package, "installed": d.installed, "version": d.version}
                        for d in info.dependencies
                    ],
                    "use_cases": svc_def.get("use_cases", []),
                    "related_features": svc_def.get("related_features", []),
                    "setup_guide": svc_def.get("setup_guide", []),
                    "config_schema": info.config_schema,
                    "current_config": info.current_config,
                    "error_message": info.error_message,
                }
            )
        return ApiResponse(data={"services": services})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("list_services failed: %s", e)
        # FIXED: 原问题-中文硬编码detail
        raise HTTPException(status_code=500, detail=ServiceErrors.LIST_FAILED) from e


@router.get("/{service_name}/status", response_model=ApiResponse)
async def get_service_status(
    service_name: str,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    if service_name not in SERVICE_DEFINITIONS:
        # FIXED: 原问题-中文硬编码detail
        raise HTTPException(status_code=404, detail=ServiceErrors.UNKNOWN_SERVICE)
    try:
        mgr = get_service_manager()
        info = mgr.get_service_info(service_name)
        if info is None:
            # FIXED: 原问题-中文硬编码detail
            raise HTTPException(status_code=404, detail=ServiceErrors.NOT_REGISTERED)
        svc_def = SERVICE_DEFINITIONS.get(service_name, {})
        return ApiResponse(
            data={
                "name": info.name,
                "display_name": info.display_name,
                "description": info.description,
                "icon": svc_def.get("icon", ""),
                "category": svc_def.get("category", ""),
                "state": info.state.value,
                "dependencies": [
                    {"package": d.package, "installed": d.installed, "version": d.version} for d in info.dependencies
                ],
                "use_cases": svc_def.get("use_cases", []),
                "related_features": svc_def.get("related_features", []),
                "setup_guide": svc_def.get("setup_guide", []),
                "config_schema": info.config_schema,
                "current_config": info.current_config,
                "running_info": info.running_info,
                "error_message": info.error_message,
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_service_status failed: %s", e)
        # FIXED: 原问题-中文硬编码detail
        raise HTTPException(status_code=500, detail=ServiceErrors.STATUS_FAILED) from e


@router.post("/{service_name}/enable", response_model=ApiResponse)
async def enable_service(
    service_name: str,
    request: Request,
    audit_svc: AuditServiceDep,
    req: EnableServiceRequest | None = None,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    if service_name not in SERVICE_DEFINITIONS:
        raise HTTPException(status_code=404, detail=ServiceErrors.UNKNOWN_SERVICE)
    # 第三轮审计修复: 记录服务启用审计日志
    from edgelite.api.auth import _get_client_ip
    from edgelite.services.audit_service import AuditAction

    client_ip = _get_client_ip(request)
    user_agent = request.headers.get("user-agent", "")
    # 敏感字段脱敏
    _SENSITIVE_KEYS = {"password", "token", "secret", "api_key", "secret_key", "auth_key"}

    def _sanitize(obj):
        if not isinstance(obj, dict):
            return obj
        return {k: "***" if any(s in k.lower() for s in _SENSITIVE_KEYS) else v for k, v in obj.items()}

    try:
        mgr = get_service_manager()
    except Exception as e:
        logger.error("get_service_manager failed: %s", e)
        raise HTTPException(
            status_code=503,
            detail=f"{ServiceErrors.ENABLE_FAILED}: Service manager not available",
        ) from e
    try:
        config_values = req.config if req else None
        result = await mgr.enable_service(service_name, config_values)

        if not result.get("success"):
            if "missing_dependencies" in result:
                deps = ", ".join(result.get("missing_dependencies", []))
                try:
                    await audit_svc.log(
                        AuditAction.SERVICE_START,
                        user_id=user["user_id"],
                        username=user["username"],
                        resource_type="service",
                        resource_id=service_name,
                        ip_address=client_ip,
                        user_agent=user_agent,
                        status="failed",
                        error_message=f"missing dependencies: {deps}",
                    )
                except Exception as e:
                    logger.warning("Audit log failed: %s", e)
                raise HTTPException(
                    status_code=424,
                    detail=f"{result.get('error', ServiceErrors.ENABLE_FAILED)}: missing dependencies [{deps}]",
                )
            error_type = result.get("error_type", "")
            status_code = 409 if error_type == "runtime" else 500
            hint = result.get("hint", "")
            error_code = result.get("error", ServiceErrors.ENABLE_FAILED)
            # FIXED: Enum.value 确保JSON序列化正确
            error_val = error_code.value if isinstance(error_code, Enum) else error_code
            detail_obj = {"error": error_val, "hint": hint} if hint else error_val
            try:
                await audit_svc.log(
                    AuditAction.SERVICE_START,
                    user_id=user["user_id"],
                    username=user["username"],
                    resource_type="service",
                    resource_id=service_name,
                    ip_address=client_ip,
                    user_agent=user_agent,
                    status="failed",
                    error_message=str(error_val),
                )
            except Exception as e:
                logger.warning("Audit log failed: %s", e)
            raise HTTPException(
                status_code=status_code,
                detail=detail_obj,
            )

        try:
            await audit_svc.log(
                AuditAction.SERVICE_START,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="service",
                resource_id=service_name,
                ip_address=client_ip,
                user_agent=user_agent,
                after_value={"config": _sanitize(config_values) if config_values else None, "result": result},
            )
        except Exception as e:
            logger.warning("Audit log failed: %s", e)

        if result.get("warning"):
            return ApiResponse(data={**result, "message": result.get("warning", "")})

        return ApiResponse(data=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("enable_service failed: %s", e)
        try:
            await audit_svc.log(
                AuditAction.SERVICE_START,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="service",
                resource_id=service_name,
                ip_address=client_ip,
                user_agent=user_agent,
                status="failed",
                error_message=str(e),
            )
        except Exception as audit_e:
            logger.warning("Audit log failed: %s", audit_e)
        raise HTTPException(
            status_code=503,
            detail=f"{ServiceErrors.ENABLE_FAILED}: Unexpected error during service enable",
        ) from e


@router.post("/{service_name}/disable", response_model=ApiResponse)
async def disable_service(
    service_name: str,
    request: Request,
    audit_svc: AuditServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    if service_name not in SERVICE_DEFINITIONS:
        raise HTTPException(status_code=404, detail=ServiceErrors.UNKNOWN_SERVICE)
    # 第三轮审计修复: 记录服务禁用审计日志
    from edgelite.api.auth import _get_client_ip
    from edgelite.services.audit_service import AuditAction

    client_ip = _get_client_ip(request)
    user_agent = request.headers.get("user-agent", "")
    try:
        mgr = get_service_manager()
        result = await mgr.disable_service(service_name)

        if not result.get("success"):
            error_code = result.get("error", ServiceErrors.DISABLE_FAILED)
            # FIXED: Enum.value 确保JSON序列化正确
            error_val = error_code.value if isinstance(error_code, Enum) else error_code
            try:
                await audit_svc.log(
                    AuditAction.SERVICE_STOP,
                    user_id=user["user_id"],
                    username=user["username"],
                    resource_type="service",
                    resource_id=service_name,
                    ip_address=client_ip,
                    user_agent=user_agent,
                    status="failed",
                    error_message=str(error_val),
                )
            except Exception as e:
                logger.warning("Audit log failed: %s", e)
            raise HTTPException(status_code=409, detail=error_val)

        try:
            await audit_svc.log(
                AuditAction.SERVICE_STOP,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="service",
                resource_id=service_name,
                ip_address=client_ip,
                user_agent=user_agent,
                after_value=result,
            )
        except Exception as e:
            logger.warning("Audit log failed: %s", e)
        return ApiResponse(data=result)
    except ValueError as e:
        # FIXED(一般): 原问题-str(e)直接作为detail泄漏内部信息; 修复-记录日志并返回标准错误码,保持422状态码
        logger.warning("disable_service ValueError: %s", e)
        raise HTTPException(status_code=422, detail=ServiceErrors.DISABLE_FAILED) from e
    except RuntimeError as e:
        # FIXED(一般): 原问题-str(e)直接作为detail泄漏内部信息; 修复-记录日志并返回标准错误码,保持409状态码
        logger.warning("disable_service RuntimeError: %s", e)
        raise HTTPException(status_code=409, detail=ServiceErrors.DISABLE_FAILED) from e
    except HTTPException:
        raise
    except Exception as e:
        logger.error("disable_service failed: %s", e)
        try:
            await audit_svc.log(
                AuditAction.SERVICE_STOP,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="service",
                resource_id=service_name,
                ip_address=client_ip,
                user_agent=user_agent,
                status="failed",
                error_message=str(e),
            )
        except Exception as audit_e:
            logger.warning("Audit log failed: %s", audit_e)
        raise HTTPException(status_code=500, detail=ServiceErrors.DISABLE_FAILED) from e


@router.post("/{service_name}/start", response_model=ApiResponse)
async def start_service(
    service_name: str,
    request: Request,
    audit_svc: AuditServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    if service_name not in SERVICE_DEFINITIONS:
        raise HTTPException(status_code=404, detail=ServiceErrors.UNKNOWN_SERVICE)
    # 第三轮审计修复: 记录服务启动审计日志
    from edgelite.api.auth import _get_client_ip
    from edgelite.services.audit_service import AuditAction

    client_ip = _get_client_ip(request)
    user_agent = request.headers.get("user-agent", "")
    try:
        mgr = get_service_manager()
        result = await mgr.start_service(service_name)

        if not result.get("success"):
            if result.get("error_type") == "runtime":
                hint = result.get("hint", "")
                error_code = result.get("error", ServiceErrors.START_FAILED)
                # FIXED: Enum.value 确保JSON序列化正确，否则message字段会显示 <Enum: 'VALUE'>
                error_val = error_code.value if isinstance(error_code, Enum) else error_code
                detail_obj = {"error": error_val, "hint": hint} if hint else error_val
                try:
                    await audit_svc.log(
                        AuditAction.SERVICE_START,
                        user_id=user["user_id"],
                        username=user["username"],
                        resource_type="service",
                        resource_id=service_name,
                        ip_address=client_ip,
                        user_agent=user_agent,
                        status="failed",
                        error_message=str(error_val),
                    )
                except Exception as e:
                    logger.warning("Audit log failed: %s", e)
                raise HTTPException(
                    status_code=409,
                    detail=detail_obj,
                )
            hint = result.get("hint", "")
            error_code = result.get("error", ServiceErrors.START_FAILED)
            error_val = error_code.value if isinstance(error_code, Enum) else error_code
            detail_obj = {"error": error_val, "hint": hint} if hint else error_val
            try:
                await audit_svc.log(
                    AuditAction.SERVICE_START,
                    user_id=user["user_id"],
                    username=user["username"],
                    resource_type="service",
                    resource_id=service_name,
                    ip_address=client_ip,
                    user_agent=user_agent,
                    status="failed",
                    error_message=str(error_val),
                )
            except Exception as e:
                logger.warning("Audit log failed: %s", e)
            raise HTTPException(
                status_code=500,
                detail=detail_obj,
            )

        try:
            await audit_svc.log(
                AuditAction.SERVICE_START,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="service",
                resource_id=service_name,
                ip_address=client_ip,
                user_agent=user_agent,
                after_value=result,
            )
        except Exception as e:
            logger.warning("Audit log failed: %s", e)

        return ApiResponse(data=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("start_service failed: %s", e)
        try:
            await audit_svc.log(
                AuditAction.SERVICE_START,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="service",
                resource_id=service_name,
                ip_address=client_ip,
                user_agent=user_agent,
                status="failed",
                error_message=str(e),
            )
        except Exception as audit_e:
            logger.warning("Audit log failed: %s", audit_e)
        raise HTTPException(status_code=500, detail=ServiceErrors.START_FAILED) from e


@router.post("/{service_name}/stop", response_model=ApiResponse)
async def stop_service(
    service_name: str,
    request: Request,
    audit_svc: AuditServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    if service_name not in SERVICE_DEFINITIONS:
        raise HTTPException(status_code=404, detail=ServiceErrors.UNKNOWN_SERVICE)
    # 第三轮审计修复: 记录服务停止审计日志
    from edgelite.api.auth import _get_client_ip
    from edgelite.services.audit_service import AuditAction

    client_ip = _get_client_ip(request)
    user_agent = request.headers.get("user-agent", "")
    try:
        mgr = get_service_manager()
        result = await mgr.stop_service(service_name)

        if not result.get("success"):
            error_code = result.get("error", ServiceErrors.STOP_FAILED)
            # FIXED: Enum.value 确保JSON序列化正确
            error_val = error_code.value if isinstance(error_code, Enum) else error_code
            try:
                await audit_svc.log(
                    AuditAction.SERVICE_STOP,
                    user_id=user["user_id"],
                    username=user["username"],
                    resource_type="service",
                    resource_id=service_name,
                    ip_address=client_ip,
                    user_agent=user_agent,
                    status="failed",
                    error_message=str(error_val),
                )
            except Exception as e:
                logger.warning("Audit log failed: %s", e)
            raise HTTPException(status_code=409, detail=error_val)

        try:
            await audit_svc.log(
                AuditAction.SERVICE_STOP,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="service",
                resource_id=service_name,
                ip_address=client_ip,
                user_agent=user_agent,
                after_value=result,
            )
        except Exception as e:
            logger.warning("Audit log failed: %s", e)
        return ApiResponse(data=result)
    except ValueError as e:
        # FIXED(一般): 原问题-str(e)直接作为detail泄漏内部信息; 修复-记录日志并返回标准错误码,保持422状态码
        logger.warning("stop_service ValueError: %s", e)
        raise HTTPException(status_code=422, detail=ServiceErrors.STOP_FAILED) from e
    except RuntimeError as e:
        # FIXED(一般): 原问题-str(e)直接作为detail泄漏内部信息; 修复-记录日志并返回标准错误码,保持409状态码
        logger.warning("stop_service RuntimeError: %s", e)
        raise HTTPException(status_code=409, detail=ServiceErrors.STOP_FAILED) from e
    except HTTPException:
        raise
    except Exception as e:
        logger.error("stop_service failed: %s", e)
        try:
            await audit_svc.log(
                AuditAction.SERVICE_STOP,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="service",
                resource_id=service_name,
                ip_address=client_ip,
                user_agent=user_agent,
                status="failed",
                error_message=str(e),
            )
        except Exception as audit_e:
            logger.warning("Audit log failed: %s", audit_e)
        raise HTTPException(status_code=500, detail=ServiceErrors.STOP_FAILED) from e


@router.post("/{service_name}/install-deps", response_model=ApiResponse)
async def install_service_dependencies(
    service_name: str,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    # FIXED-P1: 生产环境禁止远程安装依赖，防止供应链攻击
    from edgelite.config import get_config

    config = get_config()
    if not getattr(config.server, "debug_api_enabled", False):
        raise HTTPException(status_code=403, detail="ERR_INSTALL_DEPS_DISABLED_IN_PRODUCTION")
    if service_name not in SERVICE_DEFINITIONS:
        raise HTTPException(status_code=404, detail=ServiceErrors.UNKNOWN_SERVICE)
    try:
        mgr = get_service_manager()
        result = await mgr.install_service_dependencies(service_name)

        if not result.get("all_installed"):
            # FIXED: 原问题-中文硬编码detail
            raise HTTPException(status_code=500, detail=ServiceErrors.DEPS_INSTALL_FAILED)

        return ApiResponse(data=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("install_deps failed: %s", e)
        raise HTTPException(status_code=500, detail=ServiceErrors.INSTALL_FAILED) from e


@router.put("/{service_name}/config", response_model=ApiResponse)
async def update_service_config(
    service_name: str,
    req: UpdateServiceConfigRequest,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    if service_name not in SERVICE_DEFINITIONS:
        raise HTTPException(status_code=404, detail=ServiceErrors.UNKNOWN_SERVICE)
    try:
        mgr = get_service_manager()
        result = await mgr.update_service_config(service_name, req.config)

        if not result.get("success"):
            raise HTTPException(status_code=422, detail=result.get("error", ServiceErrors.CONFIG_UPDATE_FAILED))

        return ApiResponse(data=result)
    except ValueError as e:
        # FIXED(一般): 原问题-str(e)直接作为detail泄漏内部信息; 修复-记录日志并返回标准错误码,保持422状态码
        logger.warning("update_service_config ValueError: %s", e)
        raise HTTPException(status_code=422, detail=ServiceErrors.CONFIG_UPDATE_FAILED) from e
    except HTTPException:
        raise
    except Exception as e:
        logger.error("update_service_config failed: %s", e)
        raise HTTPException(status_code=500, detail=ServiceErrors.CONFIG_UPDATE_FAILED) from e
