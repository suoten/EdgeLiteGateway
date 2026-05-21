"""EdgeLite v1.0 联调集成API路由"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from edgelite.api.deps import CurrentUser, IntegrationEndpointDep, require_permission
from edgelite.api.error_codes import IntegrationErrors
from edgelite.models.common import ApiResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/integration", tags=["integration"])


class HandshakeRequest(BaseModel):
    cloud_url: str = Field(default="", description="云端地址")
    protocol_version: str = Field(default="1.0", description="协议版本")
    device_id: str | None = Field(default=None, description="设备ID")

    model_config = {"extra": "allow"}


class RpcExecuteRequest(BaseModel):
    """RPC指令执行请求模型"""
    method: str = Field(..., description="指令方法名")
    device_id: str = Field(..., description="目标设备ID")
    params: dict = Field(default_factory=dict, description="指令参数(JSON)")
    timeout: float = Field(default=10.0, ge=1.0, le=120.0, description="超时时间(秒)")


class RpcExecuteResponse(BaseModel):
    """RPC指令执行响应模型"""
    command_id: str = Field(..., description="指令ID")
    success: bool = Field(..., description="是否执行成功")
    result: object = Field(default=None, description="执行结果")
    error: str | None = Field(default=None, description="错误信息")
    elapsed_ms: float = Field(default=0.0, description="耗时(毫秒)")


@router.post("/handshake", response_model=ApiResponse)
async def handshake(
    req: HandshakeRequest,
    endpoint: IntegrationEndpointDep,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    try:
        response = await endpoint.handle_handshake(req.model_dump())
        return ApiResponse(data=response)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("handshake failed: %s", e)
        raise HTTPException(status_code=500, detail=IntegrationErrors.HANDSHAKE_FAILED) from e  # FIXED: 原问题-中文硬编码detail，改为error_code


@router.get("/status", response_model=ApiResponse)
async def get_integration_status(
    endpoint: IntegrationEndpointDep,
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
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
        raise HTTPException(status_code=500, detail=IntegrationErrors.STATUS_FAILED) from e  # FIXED: 原问题-中文硬编码detail，改为error_code


@router.post("/rpc/execute", response_model=ApiResponse[RpcExecuteResponse])
async def execute_rpc_command(
    req: RpcExecuteRequest,
    endpoint: IntegrationEndpointDep,
    user: CurrentUser = require_permission(Permission.DEVICE_WRITE_POINT),
) -> ApiResponse[RpcExecuteResponse]:
    """执行RPC反向控制指令。

    通过BackhaulManager调用驱动写方法对设备执行控制操作，
    并记录审计日志和执行历史。
    """
    try:
        from edgelite.engine.integration.backhaul import RpcCommand

        backhaul = getattr(endpoint, "_backhaul", None)
        if backhaul is None:
            raise HTTPException(status_code=503, detail=IntegrationErrors.BACKHAUL_NOT_READY)

        device_service = getattr(endpoint, "_device_service", None)

        command = RpcCommand(
            method=req.method,
            device_id=req.device_id,
            params=req.params,
            timeout=req.timeout,
        )
        result = await backhaul.handle_rpc_command(command, device_service=device_service)

        return ApiResponse(data=RpcExecuteResponse(
            command_id=result.command_id,
            success=result.success,
            result=result.result,
            error=result.error,
            elapsed_ms=result.elapsed_ms,
        ))
    except HTTPException:
        raise
    except Exception as e:
        logger.error("execute_rpc_command failed: %s", e)
        raise HTTPException(status_code=500, detail=IntegrationErrors.RPC_EXECUTE_FAILED) from e


@router.get("/rpc/history", response_model=ApiResponse)
async def get_rpc_history(
    limit: int = 50,
    endpoint: IntegrationEndpointDep = None,
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
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
        history = backhaul.get_rpc_history(limit=limit)
        return ApiResponse(data=history)
    except Exception as e:
        logger.error("get_rpc_history failed: %s", e)
        raise HTTPException(status_code=500, detail=IntegrationErrors.RPC_HISTORY_FAILED) from e
