"""EdgeLite v1.0 联调集成API路由"""

import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from edgelite.models.common import ApiResponse
from edgelite.api.deps import CurrentUser, require_permission
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/integration", tags=["integration"])


class HandshakeRequest(BaseModel):
    cloud_url: str = Field(default="", description="云端地址")
    protocol_version: str = Field(default="1.0", description="协议版本")
    device_id: Optional[str] = Field(default=None, description="设备ID")

    model_config = {"extra": "allow"}


def _get_integration_endpoint():
    from edgelite.app import _app_state
    if not hasattr(_app_state, "integration_endpoint") or not _app_state.integration_endpoint:
        raise HTTPException(status_code=503, detail="Integration not available")
    return _app_state.integration_endpoint


@router.post("/handshake", response_model=ApiResponse)
async def handshake(
    req: HandshakeRequest,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    endpoint = _get_integration_endpoint()
    response = await endpoint.handle_handshake(req.model_dump())
    return ApiResponse(data=response)


@router.get("/status", response_model=ApiResponse)
async def get_integration_status(
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    endpoint = _get_integration_endpoint()
    sessions = getattr(endpoint, "_sessions", {})
    session_ids = list(sessions.keys())
    return ApiResponse(data={
        "connected": len(session_ids) > 0,
        "session_id": session_ids[0] if session_ids else None,
        "sessions": len(session_ids),
        "session_ids": session_ids,
    })
