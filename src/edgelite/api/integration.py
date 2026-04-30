"""EdgeLite v1.0 联调集成API路由"""

import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from edgelite.models.common import ApiResponse
from edgelite.api.deps import CurrentUser, require_permission
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/integration", tags=["integration"])


def _get_integration_endpoint():
    from edgelite.app import _app_state
    if not hasattr(_app_state, "integration_endpoint") or not _app_state.integration_endpoint:
        raise HTTPException(status_code=503, detail="Integration not available")
    return _app_state.integration_endpoint


@router.post("/handshake", response_model=ApiResponse)
async def handshake(
    request: dict[str, Any],
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    endpoint = _get_integration_endpoint()
    response = await endpoint.handle_handshake(request)
    return ApiResponse(data=response)


@router.get("/status", response_model=ApiResponse)
async def get_integration_status(
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    endpoint = _get_integration_endpoint()
    session_ids = list(endpoint._sessions.keys())
    return ApiResponse(data={
        "connected": bool(endpoint.session_count),
        "session_id": session_ids[0] if session_ids else None,
        "cloud_url": None,
        "sessions": endpoint.session_count,
        "session_ids": session_ids,
    })
