"""EdgeLite v1.0 联调集成API路由"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from edgelite.api.deps import (
    AuditServiceDep,
    IntegrationEndpointDep,
    get_optional_user,
    require_permission,
)
from edgelite.api.error_codes import DeviceErrors, IntegrationErrors
from edgelite.models.common import ApiResponse
from edgelite.security.rbac import Permission

_DEVICE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}[a-z0-9]$")

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/integration", tags=["integration"])


class HandshakeRequest(BaseModel):
    cloud_url: str = Field(default="", description="云端地址")
    protocol_version: str = Field(default="1.0", description="协议版本")
    device_id: str | None = Field(default=None, description="设备ID")

    model_config = {"extra": "allow"}


# R11-API-08: 为 RpcExecuteRequest.params 定义嵌套 Pydantic 模型，保留 extra=allow 向后兼容
class RpcExecuteParams(BaseModel):
    """RPC 指令参数模型（允许额外字段以保持向后兼容）"""

    model_config = {"extra": "allow"}


class RpcExecuteRequest(BaseModel):
    """RPC指令执行请求模型"""

    method: str = Field(..., description="指令方法名")
    device_id: str = Field(..., description="目标设备ID")
    params: RpcExecuteParams = Field(default_factory=RpcExecuteParams, description="指令参数(JSON)")
    timeout: float = Field(default=10.0, ge=1.0, le=120.0, description="超时时间(秒)")

    model_config = {"extra": "allow"}


class RpcExecuteResponse(BaseModel):
    """RPC指令执行响应模型"""

    command_id: str = Field(..., description="指令ID")
    success: bool = Field(..., description="是否执行成功")
    result: object = Field(default=None, description="执行结果")
    error: str | None = Field(default=None, description="错误信息")
    elapsed_ms: float = Field(default=0.0, description="耗时(毫秒)")


# R11-API-08: 为 PushDeviceRequest.points 定义嵌套 Pydantic 模型，保留 extra=allow 向后兼容
class PushDevicePoint(BaseModel):
    """推送设备测点模型（允许额外字段以保持向后兼容）"""

    point_name: str = Field(default="", description="测点名称")
    address: str = Field(default="", description="测点地址")
    data_type: str = Field(default="", description="数据类型")

    model_config = {"extra": "allow"}


class PushDeviceRequest(BaseModel):
    device_id: str = Field(..., description="设备ID")
    name: str = Field(..., description="设备名称")
    protocol: str = Field(..., description="通信协议")
    points: list[PushDevicePoint] = Field(..., description="测点列表")
    collect_interval: int = Field(default=5, description="采集间隔(秒)")

    model_config = {"extra": "allow"}


def _validate_push_device(req: PushDeviceRequest) -> list[str]:
    errors: list[str] = []
    if not _DEVICE_ID_PATTERN.match(req.device_id):
        errors.append(f"device_id format invalid: {req.device_id}")
    if not req.name or len(req.name) > 64:
        errors.append("name must be non-empty and <= 64 characters")
    if not req.protocol:
        errors.append("protocol is required")
    else:
        try:
            from edgelite.drivers.registry import get_driver_registry

            registry = get_driver_registry()
            if (
                registry
                and registry.get_driver_class(req.protocol) is None
                and req.protocol not in ("video", "simulator", "modbus_rtu")
            ):
                errors.append(f"protocol '{req.protocol}' not registered in DriverRegistry")
        except Exception as e:
            # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
            logger.warning("协议注册表校验失败: %s", e)
    if not req.points:
        errors.append("points must be a non-empty list")
    if req.collect_interval < 1:
        errors.append("collect_interval must be >= 1")
    return errors


@router.post("/push-device", response_model=ApiResponse)
async def push_device(
    req: PushDeviceRequest,
    endpoint: IntegrationEndpointDep,
    user: dict[str, str] = Depends(require_permission(Permission.DEVICE_CREATE)),
):
    validation_errors = _validate_push_device(req)
    if validation_errors:
        raise HTTPException(
            status_code=422, detail={"error_code": DeviceErrors.CONFIG_INVALID, "errors": validation_errors}
        )
    try:
        device_service = getattr(endpoint, "_device_service", None)
        if device_service is None:
            raise HTTPException(status_code=503, detail=IntegrationErrors.BACKHAUL_NOT_READY)
        device = await device_service.create_device(req.model_dump())
        return ApiResponse(data=device)
    except ValueError as e:
        err_msg = str(e).lower()
        # FIX: 增加 err_repo_device_exists 匹配 — DeviceRepo.create() 在 IntegrityError 时
        # raise ValueError(RepoErrors.DEVICE_EXISTS) 即 "ERR_REPO_DEVICE_EXISTS"，
        # 原代码仅检查 "already exists"/"duplicate" 文本，导致此错误落入 422 而非 409
        if "already exists" in err_msg or "duplicate" in err_msg or "err_repo_device_exists" in err_msg:
            raise HTTPException(status_code=409, detail=DeviceErrors.ALREADY_EXISTS) from e
        elif "unsupported protocol" in err_msg:
            raise HTTPException(status_code=422, detail=DeviceErrors.DRIVER_UNAVAILABLE) from e
        elif "driver start failed" in err_msg or "connection" in err_msg:
            raise HTTPException(
                status_code=409,
                detail={
                    "error_code": DeviceErrors.CREATE_FAILED,
                    "errors": [str(e)],
                    "warnings": [],
                },
            ) from e
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": DeviceErrors.CONFIG_INVALID,
                "errors": [str(e)],
                "warnings": [],
            },
        ) from e
    except HTTPException:
        raise
    except Exception as e:
        logger.error("push_device failed: %s", e)
        raise HTTPException(status_code=500, detail=DeviceErrors.CREATE_FAILED) from e


@router.post("/handshake", response_model=ApiResponse)
async def handshake(
    req: HandshakeRequest,
    endpoint: IntegrationEndpointDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    try:
        response = await endpoint.handle_handshake(req.model_dump())
        return ApiResponse(data=response)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("handshake failed: %s", e)
        raise HTTPException(
            status_code=500, detail=IntegrationErrors.HANDSHAKE_FAILED
        ) from e  # FIXED: 原问题-中文硬编码detail，改为error_code


@router.get("/status", response_model=ApiResponse)
async def get_integration_status(
    endpoint: IntegrationEndpointDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    try:
        sessions = getattr(endpoint, "_sessions", {})
        session_ids = list(sessions.keys())
        return ApiResponse(
            data={
                "connected": len(session_ids) > 0,
                "session_id": session_ids[0] if session_ids else None,
                "sessions": len(session_ids),
                "session_ids": session_ids,
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_integration_status failed: %s", e)
        raise HTTPException(
            status_code=500, detail=IntegrationErrors.STATUS_FAILED
        ) from e  # FIXED: 原问题-中文硬编码detail，改为error_code


@router.post("/rpc/execute", response_model=ApiResponse[RpcExecuteResponse])
async def execute_rpc_command(
    req: RpcExecuteRequest,
    request: Request,
    endpoint: IntegrationEndpointDep,
    audit_svc: AuditServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.DEVICE_WRITE_POINT)),
) -> ApiResponse[RpcExecuteResponse]:
    """执行RPC反向控制指令。

    通过BackhaulManager调用驱动写方法对设备执行控制操作，
    并记录审计日志和执行历史。
    """
    # 第三轮审计修复: 记录RPC执行审计日志
    from edgelite.api.auth import _get_client_ip
    from edgelite.services.audit_service import AuditAction

    client_ip = _get_client_ip(request)
    user_agent = request.headers.get("user-agent", "")
    try:
        from edgelite.engine.integration.backhaul import RpcCommand

        backhaul = getattr(endpoint, "_backhaul", None)
        if backhaul is None:
            raise HTTPException(status_code=503, detail=IntegrationErrors.BACKHAUL_NOT_READY)

        device_service = getattr(endpoint, "_device_service", None)

        command = RpcCommand(
            method=req.method,
            device_id=req.device_id,
            params=req.params.model_dump(),  # R11-API-08: params 改为嵌套模型后需 model_dump 还原为 dict
            timeout=req.timeout,
        )
        result = await backhaul.handle_rpc_command(command, device_service=device_service)

        try:
            await audit_svc.log(
                AuditAction.RPC_EXECUTE,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="rpc",
                resource_id=req.device_id,
                ip_address=client_ip,
                user_agent=user_agent,
                after_value={
                    "method": req.method,
                    "device_id": req.device_id,
                    "command_id": result.command_id,
                    "success": result.success,
                    "elapsed_ms": result.elapsed_ms,
                },
                status="success" if result.success else "failed",
                error_message=result.error if not result.success else None,
            )
        except Exception as e:
            logger.warning("Audit log failed: %s", e)

        return ApiResponse(
            data=RpcExecuteResponse(
                command_id=result.command_id,
                success=result.success,
                result=result.result,
                error=result.error,
                elapsed_ms=result.elapsed_ms,
            )
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("execute_rpc_command failed: %s", e)
        try:
            await audit_svc.log(
                AuditAction.RPC_EXECUTE,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="rpc",
                resource_id=req.device_id,
                ip_address=client_ip,
                user_agent=user_agent,
                status="failed",
                error_message=str(e),
                after_value={"method": req.method, "device_id": req.device_id},
            )
        except Exception as audit_e:
            logger.warning("Audit log failed: %s", audit_e)
        raise HTTPException(status_code=500, detail=IntegrationErrors.RPC_EXECUTE_FAILED) from e


@router.get("/rpc/history", response_model=ApiResponse)
async def get_rpc_history(
    limit: int = Query(50, ge=1, le=500),  # FIXED-P2: 原问题-limit无边界校验，可传超大值导致内存溢出
    endpoint: IntegrationEndpointDep = None,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
) -> ApiResponse:
    """获取RPC执行历史记录。

    Args:
        limit: 返回记录数上限，默认50。

    Returns:
        按时间倒序排列的RPC执行历史列表。
    """
    try:
        backhaul = getattr(endpoint, "_backhaul", None) if endpoint else None
        if backhaul is None:
            return ApiResponse(data=[])
        history = await backhaul.get_rpc_history(limit=limit)
        return ApiResponse(data=history)
    except Exception as e:
        logger.error("get_rpc_history failed: %s", e)
        raise HTTPException(status_code=500, detail=IntegrationErrors.RPC_HISTORY_FAILED) from e


@router.get("/health", response_model=ApiResponse)
async def integration_health_check(request: Request):
    """集成通道健康检查。

    FIXED-P1: 无认证时仅返回status，有认证时返回完整详情（与health.py模式一致）。
    R11-API-06: 返回值改为 ApiResponse(data={...}) 统一响应格式。
    R11-API-07: 复用 deps.get_optional_user 公共函数替代内联认证逻辑。
    """
    # R11-API-07: 复用公共可选认证函数，避免内联重复认证逻辑
    user = await get_optional_user(request)
    is_authenticated = user is not None

    try:
        from edgelite.app import _app_state

        endpoint = getattr(_app_state, "integration_endpoint", None)
    except Exception:
        endpoint = None

    sessions: dict = {}
    session_count = 0
    connection_count = 0
    if endpoint:
        try:
            sessions = getattr(endpoint, "_sessions", {})
            session_count = len(sessions)
            connection_count = len(getattr(endpoint, "_connections", {}))
        except Exception as e:
            logger.warning("集成端点会话信息获取失败: %s", e)

    backhaul = getattr(endpoint, "_backhaul", None) if endpoint else None
    buffer_size = 0
    rpc_available = False
    if backhaul:
        try:
            buffer_size = len(getattr(backhaul, "_buffer", []))
            rpc_available = True
        except Exception as e:
            logger.warning("回传管理器缓冲区信息获取失败: %s", e)

    healthy = endpoint is not None and (backhaul is not None)

    # R11-API-06: 返回 ApiResponse 统一响应格式，替代直接返回 dict
    if is_authenticated:
        return ApiResponse(
            data={
                "status": "healthy" if healthy else "degraded",
                "endpoint_initialized": endpoint is not None,
                "active_sessions": session_count,
                "active_connections": connection_count,
                "backhaul_available": backhaul is not None,
                "buffer_backlog": buffer_size,
                "rpc_available": rpc_available,
            }
        )
    else:
        # 无认证仅返回状态，不暴露内部细节
        return ApiResponse(data={"status": "healthy" if healthy else "degraded"})
