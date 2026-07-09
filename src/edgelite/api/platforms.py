from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from edgelite.api.deps import AuditServiceDep, PlatformHandlersDep, require_permission
from edgelite.api.error_codes import PlatformErrors
from edgelite.models.common import ApiResponse
from edgelite.security.rbac import Permission
from edgelite.services.platform_service import PlatformService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/platforms", tags=["Platforms"])


class PlatformConnectRequest(BaseModel):
    config: dict


class ReloadConfigRequest(BaseModel):
    config: dict


def _get_service(handlers: dict) -> PlatformService:
    return PlatformService(handlers)


@router.get("/list", response_model=ApiResponse)
async def list_platforms(
    handlers: PlatformHandlersDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    try:
        svc = _get_service(handlers)
        platforms = svc.list_platforms()
        supported = svc.list_supported()
        return ApiResponse(data={"platforms": platforms, "supported": supported})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to list platforms: %s", e)
        raise HTTPException(status_code=500, detail=PlatformErrors.CONNECT_FAILED) from e


@router.get("/config-schema/{platform_name}", response_model=ApiResponse)
async def get_platform_config_schema(
    # FIXED-P2: platform_name Path 参数增加长度与字符集约束，防止超长/特殊字符注入
    platform_name: Annotated[str, Path(max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")],
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    try:
        svc = PlatformService()
        schema = svc.get_config_schema(platform_name)
        if not schema:
            raise HTTPException(status_code=404, detail=PlatformErrors.CONFIG_SCHEMA_NOT_FOUND)
        return ApiResponse(data={"platform_name": platform_name, "config_schema": schema})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to get config schema: %s", e)
        raise HTTPException(status_code=500, detail=PlatformErrors.CONNECT_FAILED) from e


@router.post("/connect/{platform_name}", response_model=ApiResponse)
async def connect_platform(
    platform_name: Annotated[str, Path(max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")],
    req: PlatformConnectRequest,
    request: Request,
    handlers: PlatformHandlersDep,
    audit_svc: AuditServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    if not req.config:
        raise HTTPException(status_code=400, detail=PlatformErrors.MISSING_CONFIG)

    # 第三轮审计修复: 记录平台连接审计日志
    from edgelite.api.auth import _get_client_ip
    from edgelite.services.audit_service import AuditAction

    client_ip = _get_client_ip(request)
    user_agent = request.headers.get("user-agent", "")
    # 敏感字段脱敏
    _SENSITIVE_KEYS = {"password", "token", "secret", "api_key", "secret_key", "auth_key", "access_token"}
    def _sanitize(obj):
        if not isinstance(obj, dict):
            return obj
        return {k: "***" if any(s in k.lower() for s in _SENSITIVE_KEYS) else v for k, v in obj.items()}
    safe_config = _sanitize(req.config)
    try:
        svc = _get_service(handlers)
        result = await svc.connect(platform_name, req.config)
        try:
            await audit_svc.log(
                AuditAction.PLATFORM_CONNECT,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="platform",
                resource_id=platform_name,
                ip_address=client_ip,
                user_agent=user_agent,
                after_value={"platform_name": platform_name, "config": safe_config, "result": result},
            )
        except Exception as e:
            logger.warning("Audit log failed: %s", e)
        return ApiResponse(data=result)
    except ValueError as e:
        try:
            await audit_svc.log(
                AuditAction.PLATFORM_CONNECT,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="platform",
                resource_id=platform_name,
                ip_address=client_ip,
                user_agent=user_agent,
                status="failed",
                error_message=str(e),
                after_value={"platform_name": platform_name, "config": safe_config},
            )
        except Exception as audit_e:
            logger.warning("Audit log failed: %s", audit_e)
        raise HTTPException(status_code=400, detail=PlatformErrors.MISSING_CONFIG) from e  # FIXED-P2: 原问题-detail=str(e)泄漏内部错误
    except Exception as e:
        logger.error("connect_platform failed: %s", e)
        try:
            await audit_svc.log(
                AuditAction.PLATFORM_CONNECT,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="platform",
                resource_id=platform_name,
                ip_address=client_ip,
                user_agent=user_agent,
                status="failed",
                error_message=str(e),
            )
        except Exception as audit_e:
            logger.warning("Audit log failed: %s", audit_e)
        raise HTTPException(status_code=500, detail=PlatformErrors.CONNECT_FAILED) from e


@router.post("/disconnect/{platform_name}", response_model=ApiResponse)
async def disconnect_platform(
    platform_name: Annotated[str, Path(max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")],
    request: Request,
    handlers: PlatformHandlersDep,
    audit_svc: AuditServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    # 第三轮审计修复: 记录平台断开审计日志
    from edgelite.api.auth import _get_client_ip
    from edgelite.services.audit_service import AuditAction

    client_ip = _get_client_ip(request)
    user_agent = request.headers.get("user-agent", "")
    try:
        svc = _get_service(handlers)
        result = await svc.disconnect(platform_name)
        try:
            await audit_svc.log(
                AuditAction.PLATFORM_DISCONNECT,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="platform",
                resource_id=platform_name,
                ip_address=client_ip,
                user_agent=user_agent,
                after_value={"platform_name": platform_name, "result": result},
            )
        except Exception as e:
            logger.warning("Audit log failed: %s", e)
        return ApiResponse(data=result)
    except KeyError as e:
        try:
            await audit_svc.log(
                AuditAction.PLATFORM_DISCONNECT,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="platform",
                resource_id=platform_name,
                ip_address=client_ip,
                user_agent=user_agent,
                status="failed",
                error_message="platform not found",
            )
        except Exception as audit_e:
            logger.warning("Audit log failed: %s", audit_e)
        raise HTTPException(status_code=404, detail=PlatformErrors.CONNECT_FAILED) from e  # FIXED-P2: 原问题-detail=str(e)泄漏内部错误
    except Exception as e:
        logger.error("disconnect_platform failed: %s", e)
        try:
            await audit_svc.log(
                AuditAction.PLATFORM_DISCONNECT,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="platform",
                resource_id=platform_name,
                ip_address=client_ip,
                user_agent=user_agent,
                status="failed",
                error_message=str(e),
            )
        except Exception as audit_e:
            logger.warning("Audit log failed: %s", audit_e)
        raise HTTPException(status_code=500, detail=PlatformErrors.DISCONNECT_FAILED) from e


class TestConnectionRequest(BaseModel):
    config: dict


@router.post("/test-connection/{platform_name}", response_model=ApiResponse)
async def test_connection(
    platform_name: Annotated[str, Path(max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")],
    req: TestConnectionRequest,
    request: Request,
    handlers: PlatformHandlersDep,
    audit_svc: AuditServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),  # FIXED-P2: test_connection可被用于SSRF探测，权限从SYSTEM_READ提升为SYSTEM_MANAGE
):
    # 第三轮审计修复: 记录平台连接测试审计日志
    from edgelite.api.auth import _get_client_ip
    from edgelite.services.audit_service import AuditAction

    client_ip = _get_client_ip(request)
    user_agent = request.headers.get("user-agent", "")
    try:
        svc = _get_service(handlers)
        result = await svc.test_connection(platform_name, req.config)
        try:
            await audit_svc.log(
                AuditAction.PLATFORM_CONNECT,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="platform",
                resource_id=platform_name,
                ip_address=client_ip,
                user_agent=user_agent,
                after_value={"platform_name": platform_name, "test": True, "result": result},
            )
        except Exception as e:
            logger.warning("Audit log failed: %s", e)
        return ApiResponse(data=result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=PlatformErrors.MISSING_CONFIG) from e  # FIXED-P2: 原问题-detail=str(e)泄漏内部错误
    except TimeoutError as e:
        try:
            await audit_svc.log(
                AuditAction.PLATFORM_CONNECT,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="platform",
                resource_id=platform_name,
                ip_address=client_ip,
                user_agent=user_agent,
                status="failed",
                error_message="timeout",
            )
        except Exception as audit_e:
            logger.warning("Audit log failed: %s", audit_e)
        raise HTTPException(status_code=504, detail=PlatformErrors.CONNECT_FAILED) from e
    except Exception as e:
        logger.error("test_connection failed: %s", e)
        try:
            await audit_svc.log(
                AuditAction.PLATFORM_CONNECT,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="platform",
                resource_id=platform_name,
                ip_address=client_ip,
                user_agent=user_agent,
                status="failed",
                error_message=str(e),
            )
        except Exception as audit_e:
            logger.warning("Audit log failed: %s", audit_e)
        raise HTTPException(status_code=500, detail=PlatformErrors.CONNECT_FAILED) from e


@router.get("/status/{platform_name}", response_model=ApiResponse)
async def get_platform_status(
    platform_name: Annotated[str, Path(max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")],
    handlers: PlatformHandlersDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    try:
        svc = _get_service(handlers)
        status = svc.get_status(platform_name)
        return ApiResponse(data=status)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to get platform status: %s", e)
        raise HTTPException(status_code=500, detail=PlatformErrors.CONNECT_FAILED) from e


@router.get("/dashboard", response_model=ApiResponse)
async def get_platform_dashboard(
    handlers: PlatformHandlersDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    try:
        svc = _get_service(handlers)
        data = await svc.get_dashboard_data()  # FIXED-P0: get_dashboard_data已改为async
        return ApiResponse(data=data)
    except Exception as e:
        logger.error("Failed to get platform dashboard: %s", e)
        raise HTTPException(status_code=500, detail=PlatformErrors.DASHBOARD_FAILED) from e


@router.get("/metrics", response_class=PlainTextResponse)
async def get_north_metrics(
    handlers: PlatformHandlersDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),  # FIXED-P1: 端点无认证，任何人可访问北向指标
):
    try:
        svc = _get_service(handlers)
        metrics_text = svc.get_north_metrics()
        return PlainTextResponse(
            content=metrics_text,
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )
    except Exception as e:
        logger.error("Failed to get north metrics: %s", e)
        return PlainTextResponse(content="# Error: internal server error", status_code=500)  # FIXED-P1: 原问题-f"# Error: {e}"泄漏内部异常详情到响应体


@router.post("/reload/{platform_name}", response_model=ApiResponse)
async def reload_platform_config(
    platform_name: Annotated[str, Path(max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")],
    req: ReloadConfigRequest,
    request: Request,
    handlers: PlatformHandlersDep,
    audit_svc: AuditServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    # 第三轮审计修复: 记录平台配置重载审计日志
    from edgelite.api.auth import _get_client_ip
    from edgelite.services.audit_service import AuditAction

    client_ip = _get_client_ip(request)
    user_agent = request.headers.get("user-agent", "")
    # 敏感字段脱敏
    _SENSITIVE_KEYS = {"password", "token", "secret", "api_key", "secret_key", "auth_key", "access_token"}
    def _sanitize(obj):
        if not isinstance(obj, dict):
            return obj
        return {k: "***" if any(s in k.lower() for s in _SENSITIVE_KEYS) else v for k, v in obj.items()}
    safe_config = _sanitize(req.config)
    try:
        svc = _get_service(handlers)
        result = await svc.reload_config(platform_name, req.config)
        try:
            await audit_svc.log(
                AuditAction.PLATFORM_CONNECT,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="platform",
                resource_id=platform_name,
                ip_address=client_ip,
                user_agent=user_agent,
                after_value={"platform_name": platform_name, "config": safe_config, "result": result},
            )
        except Exception as e:
            logger.warning("Audit log failed: %s", e)
        return ApiResponse(data=result)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=PlatformErrors.CONNECT_FAILED) from e  # FIXED-P2: 原问题-detail=str(e)泄漏内部错误
    except ValueError as e:
        raise HTTPException(status_code=400, detail=PlatformErrors.MISSING_CONFIG) from e  # FIXED-P2: 原问题-detail=str(e)泄漏内部错误
    except Exception as e:
        logger.error("Failed to reload platform config: %s", e)
        try:
            await audit_svc.log(
                AuditAction.PLATFORM_CONNECT,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="platform",
                resource_id=platform_name,
                ip_address=client_ip,
                user_agent=user_agent,
                status="failed",
                error_message=str(e),
            )
        except Exception as audit_e:
            logger.warning("Audit log failed: %s", audit_e)
        raise HTTPException(status_code=500, detail=PlatformErrors.CONNECT_FAILED) from e


@router.get("/message-preview/{platform_name}", response_model=ApiResponse)
async def get_message_preview(
    platform_name: Annotated[str, Path(max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")],
    handlers: PlatformHandlersDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    try:
        svc = _get_service(handlers)
        data = svc.get_message_preview(platform_name)
        return ApiResponse(data=data)
    except Exception as e:
        logger.error("Failed to get message preview: %s", e)
        raise HTTPException(status_code=500, detail=PlatformErrors.DATA_QUERY_FAILED) from e


@router.get("/broker-quality/{platform_name}", response_model=ApiResponse)
async def get_broker_quality(
    platform_name: Annotated[str, Path(max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")],
    handlers: PlatformHandlersDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    try:
        svc = _get_service(handlers)
        data = svc.get_broker_quality(platform_name)
        return ApiResponse(data=data)
    except Exception as e:
        logger.error("Failed to get broker quality: %s", e)
        raise HTTPException(status_code=500, detail=PlatformErrors.DATA_QUERY_FAILED) from e


class ValidateTopicRequest(BaseModel):
    template: str


@router.post("/validate-topic", response_model=ApiResponse)
async def validate_topic_template(
    req: ValidateTopicRequest,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    try:
        svc = PlatformService()
        result = svc.validate_topic_template(req.template)
        return ApiResponse(data=result)
    except Exception as e:
        logger.error("Failed to validate topic template: %s", e)
        raise HTTPException(status_code=500, detail=PlatformErrors.TEMPLATE_FAILED) from e


@router.get("/tb/devices/{platform_name}", response_model=ApiResponse)
async def get_tb_devices(
    platform_name: Annotated[str, Path(max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")],
    handlers: PlatformHandlersDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    try:
        svc = _get_service(handlers)
        data = svc.get_tb_devices(platform_name)
        return ApiResponse(data=data)
    except Exception as e:
        logger.error("Failed to get TB devices: %s", e)
        raise HTTPException(status_code=500, detail=PlatformErrors.DATA_QUERY_FAILED) from e


@router.get("/tb/rpc-logs/{platform_name}", response_model=ApiResponse)
async def get_tb_rpc_logs(
    platform_name: Annotated[str, Path(max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")],
    handlers: PlatformHandlersDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    try:
        svc = _get_service(handlers)
        data = svc.get_tb_rpc_logs(platform_name)
        return ApiResponse(data=data)
    except Exception as e:
        logger.error("Failed to get TB RPC logs: %s", e)
        raise HTTPException(status_code=500, detail=PlatformErrors.DATA_QUERY_FAILED) from e


@router.get("/tb/alarms/{platform_name}", response_model=ApiResponse)
async def get_tb_alarm_records(
    platform_name: Annotated[str, Path(max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")],
    handlers: PlatformHandlersDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    try:
        svc = _get_service(handlers)
        data = svc.get_tb_alarm_records(platform_name)
        return ApiResponse(data=data)
    except Exception as e:
        logger.error("Failed to get TB alarm records: %s", e)
        raise HTTPException(status_code=500, detail=PlatformErrors.DATA_QUERY_FAILED) from e


@router.get("/tb/sync-status/{platform_name}", response_model=ApiResponse)
async def get_tb_sync_status(
    platform_name: Annotated[str, Path(max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")],
    handlers: PlatformHandlersDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    try:
        svc = _get_service(handlers)
        data = svc.get_tb_sync_status(platform_name)
        return ApiResponse(data=data)
    except Exception as e:
        logger.error("Failed to get TB sync status: %s", e)
        raise HTTPException(status_code=500, detail=PlatformErrors.DATA_QUERY_FAILED) from e


@router.get("/shadow/{platform_name}", response_model=ApiResponse)
async def get_platform_shadow(
    platform_name: Annotated[str, Path(max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")],
    handlers: PlatformHandlersDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    try:
        svc = _get_service(handlers)
        data = svc.get_platform_shadow(platform_name)
        return ApiResponse(data=data)
    except Exception as e:
        logger.error("Failed to get platform shadow: %s", e)
        raise HTTPException(status_code=500, detail=PlatformErrors.DATA_QUERY_FAILED) from e


@router.get("/command-logs/{platform_name}", response_model=ApiResponse)
async def get_platform_command_logs(
    platform_name: Annotated[str, Path(max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")],
    handlers: PlatformHandlersDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    try:
        svc = _get_service(handlers)
        data = svc.get_platform_command_logs(platform_name)
        return ApiResponse(data=data)
    except Exception as e:
        logger.error("Failed to get command logs: %s", e)
        raise HTTPException(status_code=500, detail=PlatformErrors.DATA_QUERY_FAILED) from e


@router.get("/alarm-records/{platform_name}", response_model=ApiResponse)
async def get_platform_alarm_records(
    platform_name: Annotated[str, Path(max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")],
    handlers: PlatformHandlersDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    try:
        svc = _get_service(handlers)
        data = svc.get_platform_alarm_records(platform_name)
        return ApiResponse(data=data)
    except Exception as e:
        logger.error("Failed to get alarm records: %s", e)
        raise HTTPException(status_code=500, detail=PlatformErrors.DATA_QUERY_FAILED) from e


@router.get("/device-mapping/{platform_name}", response_model=ApiResponse)
async def get_platform_device_mapping(
    platform_name: Annotated[str, Path(max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")],
    handlers: PlatformHandlersDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    try:
        svc = _get_service(handlers)
        data = svc.get_platform_device_mapping(platform_name)
        return ApiResponse(data=data)
    except Exception as e:
        logger.error("Failed to get device mapping: %s", e)
        raise HTTPException(status_code=500, detail=PlatformErrors.DATA_QUERY_FAILED) from e


class ExportConfigRequest(BaseModel):
    pass


class ImportConfigRequest(BaseModel):
    # R11-API-17: dict 字段添加 max_length 限制键数量（Pydantic v2 对 dict 的 max_length 限制键数量）
    config_data: dict = Field(..., max_length=50)


class AdvancedTemplateValidateRequest(BaseModel):
    # R11-API-19: template 添加 max_length 约束
    template: str = Field(..., max_length=65536)
    # R11-API-18: template_type 添加 Literal 约束
    template_type: Literal["payload", "header", "url", "script"] = "payload"


class TemplatePreviewRequest(BaseModel):
    # R11-API-19: template 添加 max_length 约束
    template: str = Field(..., max_length=65536)
    # R11-API-17: test_data 添加 max_length 限制键数量
    test_data: dict = Field(..., max_length=50)
    # R11-API-18: template_type 添加 Literal 约束
    template_type: Literal["payload", "header", "url", "script"] = "payload"


class ScriptValidateRequest(BaseModel):
    # R11-API-19: script 添加 max_length 约束
    script: str = Field(..., max_length=10000)


class ScriptTestRequest(BaseModel):
    # R11-API-19: script 添加 max_length 约束
    script: str = Field(..., max_length=10000)
    # R11-API-17: test_payload 添加 max_length 限制键数量
    test_payload: dict = Field(..., max_length=50)
    test_context: dict | None = None


class MqttTestPublishRequest(BaseModel):
    topic: str
    payload: str
    # R11-API-18: qos 添加 Literal 约束
    qos: Literal[0, 1, 2] = 0


@router.get("/export/{platform_name}", response_model=ApiResponse)
async def export_platform_config(
    platform_name: Annotated[str, Path(max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")],
    handlers: PlatformHandlersDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),  # FIXED(安全): 导出配置含敏感信息，权限从 SYSTEM_READ 提升到 SYSTEM_MANAGE（仅ADMIN），防止 VIEWER 获取凭据
):
    try:
        svc = _get_service(handlers)
        data = svc.export_config(platform_name)
        return ApiResponse(data=data)
    except Exception as e:
        logger.error("Failed to export config: %s", e)
        raise HTTPException(status_code=500, detail=PlatformErrors.EXPORT_FAILED) from e


@router.post("/import/{platform_name}", response_model=ApiResponse)
async def import_platform_config(
    platform_name: Annotated[str, Path(max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")],
    req: ImportConfigRequest,
    handlers: PlatformHandlersDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    try:
        svc = _get_service(handlers)
        result = svc.import_config(platform_name, req.config_data)
        return ApiResponse(data=result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=PlatformErrors.MISSING_CONFIG) from e  # FIXED-P2: 原问题-detail=str(e)泄漏内部错误
    except Exception as e:
        logger.error("Failed to import config: %s", e)
        raise HTTPException(status_code=500, detail=PlatformErrors.IMPORT_FAILED) from e


@router.get("/broker-status/{platform_name}", response_model=ApiResponse)
async def get_broker_status(
    platform_name: Annotated[str, Path(max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")],
    handlers: PlatformHandlersDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    try:
        svc = _get_service(handlers)
        data = svc.get_broker_status(platform_name)
        return ApiResponse(data=data)
    except Exception as e:
        logger.error("Failed to get broker status: %s", e)
        raise HTTPException(status_code=500, detail=PlatformErrors.DATA_QUERY_FAILED) from e


@router.post("/validate-advanced-template", response_model=ApiResponse)
async def validate_advanced_template(
    req: AdvancedTemplateValidateRequest,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    try:
        svc = PlatformService()
        result = svc.validate_advanced_template(req.template, req.template_type)
        return ApiResponse(data=result)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=PlatformErrors.TEMPLATE_FAILED) from e  # FIXED-P2: 原问题-detail=str(e)泄漏内部错误
    except Exception as e:
        logger.error("Failed to validate advanced template: %s", e)
        raise HTTPException(status_code=500, detail=PlatformErrors.TEMPLATE_FAILED) from e


@router.post("/preview-template", response_model=ApiResponse)
async def preview_template(
    req: TemplatePreviewRequest,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    try:
        svc = PlatformService()
        result = svc.preview_template(req.template, req.test_data, req.template_type)
        return ApiResponse(data=result)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=PlatformErrors.TEMPLATE_FAILED) from e  # FIXED-P2: 原问题-detail=str(e)泄漏内部错误
    except Exception as e:
        logger.error("Failed to preview template: %s", e)
        raise HTTPException(status_code=500, detail=PlatformErrors.TEMPLATE_FAILED) from e


@router.post("/validate-script", response_model=ApiResponse)
async def validate_script(
    req: ScriptValidateRequest,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    try:
        svc = PlatformService()
        result = svc.validate_script(req.script)
        return ApiResponse(data=result)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=PlatformErrors.TEMPLATE_FAILED) from e  # FIXED-P2: 原问题-detail=str(e)泄漏内部错误
    except Exception as e:
        logger.error("Failed to validate script: %s", e)
        raise HTTPException(status_code=500, detail=PlatformErrors.TEMPLATE_FAILED) from e


@router.post("/test-script", response_model=ApiResponse)
async def test_script(
    req: ScriptTestRequest,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),  # FIXED(严重): 原为 SYSTEM_READ，viewer 可执行任意 JS 代码，提升为 SYSTEM_MANAGE 与 scripts.py 保持一致
):
    try:
        svc = PlatformService()
        result = await svc.test_script(req.script, req.test_payload, req.test_context)
        return ApiResponse(data=result)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=PlatformErrors.TEMPLATE_FAILED) from e  # FIXED-P2: 原问题-detail=str(e)泄漏内部错误
    except Exception as e:
        logger.error("Failed to test script: %s", e)
        raise HTTPException(status_code=500, detail=PlatformErrors.TEMPLATE_FAILED) from e


@router.post("/mqtt-test-publish/{platform_name}", response_model=ApiResponse)
async def mqtt_test_publish(
    platform_name: Annotated[str, Path(max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")],
    req: MqttTestPublishRequest,
    handlers: PlatformHandlersDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    try:
        svc = _get_service(handlers)
        result = await svc.mqtt_test_publish(platform_name, req.topic, req.payload, req.qos)
        return ApiResponse(data=result)
    except Exception as e:
        logger.error("Failed to test publish: %s", e)
        raise HTTPException(status_code=500, detail=PlatformErrors.MQTT_PUBLISH_FAILED) from e
