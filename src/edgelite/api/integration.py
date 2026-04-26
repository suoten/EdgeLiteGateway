"""EdgeLite v1.0 联调集成API路由"""

import logging
from typing import Any

from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/integration", tags=["integration"])


def _get_integration_endpoint():
    from edgelite.app import get_app_state
    state = get_app_state()
    if not hasattr(state, "integration_endpoint") or not state.integration_endpoint:
        raise HTTPException(status_code=503, detail="Integration not available")
    return state.integration_endpoint


@router.post("/handshake")
async def handshake(request: dict[str, Any]):
    endpoint = _get_integration_endpoint()
    response = await endpoint.handle_handshake(request)
    return response


@router.get("/status")
async def get_integration_status():
    endpoint = _get_integration_endpoint()
    return {"sessions": endpoint.session_count, "session_ids": list(endpoint._sessions.keys())}
