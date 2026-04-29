"""数据预处理配置API路由"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from edgelite.models.common import ApiResponse
from edgelite.api.deps import CurrentUser, require_permission
from edgelite.security.rbac import Permission

router = APIRouter(prefix="/api/v1/preprocess", tags=["数据预处理"])


def _get_preprocessor():
    from edgelite.app import _app_state
    return getattr(_app_state, "preprocessor", None)


def _get_config():
    from edgelite.config import get_config
    return get_config()


@router.get("/config", response_model=ApiResponse)
async def get_preprocess_config(
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    config = _get_config()
    preprocessor = _get_preprocessor()

    point_configs = {}
    if preprocessor:
        point_configs = getattr(preprocessor, "_configs", {})

    return ApiResponse(data={
        "enabled": getattr(config.preprocess, "enabled", False),
        "default_deadband": getattr(config.preprocess, "default_deadband", 0.0),
        "default_filter_window": getattr(config.preprocess, "default_filter_window", 3),
        "default_aggregate_window_sec": getattr(config.preprocess, "default_aggregate_window_sec", 0),
        "point_configs": point_configs,
    })


@router.put("/config", response_model=ApiResponse)
async def update_preprocess_config(
    data: dict,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    preprocessor = _get_preprocessor()
    if not preprocessor:
        raise HTTPException(status_code=503, detail="预处理器未初始化")

    global_config = data.get("global", {})
    point_configs = data.get("points", {})

    for point_key, config in point_configs.items():
        preprocessor.configure(point_key, config)

    return ApiResponse(data={"status": "updated", "points_configured": len(point_configs)})
