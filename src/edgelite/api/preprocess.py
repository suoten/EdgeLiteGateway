"""数据预处理配置API路由"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from edgelite.models.common import ApiResponse
from edgelite.api.deps import CurrentUser, require_permission
from edgelite.security.rbac import Permission

router = APIRouter(prefix="/api/v1/preprocess", tags=["数据预处理"])


class PreprocessGlobalModel(BaseModel):
    enabled: bool = False
    default_deadband: float = Field(default=0.0, ge=0.0)
    default_filter_window: int = Field(default=3, ge=1, le=21)
    default_aggregate_window_sec: int = Field(default=0, ge=0)


class PreprocessPointConfigModel(BaseModel):
    deadband: float | None = Field(default=None, ge=0.0)
    filter_window: int | None = Field(default=None, ge=1, le=21)
    aggregate_window_sec: int | None = Field(default=None, ge=0)

    model_config = {"extra": "allow"}


class PreprocessUpdateRequest(BaseModel):
    global_config: PreprocessGlobalModel | None = Field(default=None, alias="global")
    points: dict[str, dict] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}


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

    preprocess_config = getattr(config, "preprocess", None)

    return ApiResponse(data={
        "enabled": getattr(preprocess_config, "enabled", False) if preprocess_config else False,
        "default_deadband": getattr(preprocess_config, "default_deadband", 0.0) if preprocess_config else 0.0,
        "default_filter_window": getattr(preprocess_config, "default_filter_window", 3) if preprocess_config else 3,
        "default_aggregate_window_sec": getattr(preprocess_config, "default_aggregate_window_sec", 0) if preprocess_config else 0,
        "point_configs": point_configs,
    })


@router.put("/config", response_model=ApiResponse)
async def update_preprocess_config(
    req: PreprocessUpdateRequest,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    preprocessor = _get_preprocessor()
    if not preprocessor:
        raise HTTPException(status_code=503, detail="预处理器未初始化")

    if req.global_config:
        config = _get_config()
        if hasattr(config, "preprocess") and config.preprocess:
            if req.global_config.enabled is not None:
                config.preprocess.enabled = req.global_config.enabled
            if req.global_config.default_deadband is not None:
                config.preprocess.default_deadband = req.global_config.default_deadband
            if req.global_config.default_filter_window is not None:
                config.preprocess.default_filter_window = req.global_config.default_filter_window
            if req.global_config.default_aggregate_window_sec is not None:
                config.preprocess.default_aggregate_window_sec = req.global_config.default_aggregate_window_sec

    for point_key, config in req.points.items():
        preprocessor.configure(point_key, config)

    return ApiResponse(data={"status": "updated", "points_configured": len(req.points)})
