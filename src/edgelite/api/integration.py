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
