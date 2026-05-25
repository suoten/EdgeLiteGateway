"""AI模型管理API路由"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query

from edgelite.api.deps import CurrentUser, require_permission
from edgelite.api.error_codes import AiErrors
from edgelite.models.ai_model import (
    AiInferenceRequest,
    AiInferenceResponse,
    AiModelReloadRequest,
    AiModelUpdate,
    AiStatsResponse,
    ScheduleInferenceRequest,
)
from edgelite.models.common import ApiResponse, PagedResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/ai", tags=["AI Inference"])


def _get_ai_service():
    from edgelite.app import _app_state

    service = getattr(_app_state, "ai_service", None)
    if service is None:
        # FIXED: P1-1/P2-6 HTTPException.detail必须为字符串，而非嵌套ApiResponse字典
        raise HTTPException(status_code=503, detail=AiErrors.ENGINE_NOT_INITIALIZED)
    return service


@router.get("/models", summary="AI模型列表")
async def list_models(
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100, alias="size"),
):
    try:
        service = _get_ai_service()
        result = await service.list_models(page, page_size)
        return PagedResponse(data=result["items"], total=result["total"], page=page, size=page_size)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("list_models failed: %s", e)
        raise HTTPException(status_code=500, detail=AiErrors.INTERNAL_ERROR) from e


@router.get("/models/{model_id}", summary="AI模型详情")
async def get_model(model_id: str, user: CurrentUser = require_permission(Permission.SYSTEM_READ)):
    try:
        service = _get_ai_service()
        model = await service.get_model(model_id)
        if not model:
            # FIXED: P1-1/P2-6 HTTPException.detail必须为字符串
            raise HTTPException(status_code=404, detail=AiErrors.MODEL_NOT_FOUND)
        return ApiResponse(code=0, message="success", data=model.model_dump())
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_model failed: %s", e)
        raise HTTPException(status_code=500, detail=AiErrors.INTERNAL_ERROR) from e


@router.put("/models/{model_id}", summary="更新AI模型元信息")
async def update_model(model_id: str, body: AiModelUpdate, user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE)):
    try:
        service = _get_ai_service()
        result = await service.update_model(model_id, body.model_dump(exclude_unset=True))
        if not result:
            # FIXED: P1-1/P2-6 HTTPException.detail必须为字符串
            raise HTTPException(status_code=404, detail=AiErrors.MODEL_NOT_FOUND)
        return ApiResponse(code=0, message="success", data=result.model_dump())
    except HTTPException:
        raise
    except Exception as e:
        logger.error("update_model failed: %s", e)
        raise HTTPException(status_code=500, detail=AiErrors.INTERNAL_ERROR) from e


@router.delete("/models/{model_id}", summary="删除AI模型(预置不可删)")
async def delete_model(model_id: str, user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE)):
    try:
        service = _get_ai_service()
        deleted = await service.delete_model(model_id)
        if not deleted:
            wrapper = service._engine.get_model(model_id)
            if wrapper and wrapper.is_preset:
                # FIXED: P1-1/P2-6 HTTPException.detail必须为字符串
                raise HTTPException(status_code=403, detail=AiErrors.MODEL_DELETE_PRESET)
            # FIXED: P1-1/P2-6 HTTPException.detail必须为字符串
            raise HTTPException(status_code=404, detail=AiErrors.MODEL_NOT_FOUND)
        return ApiResponse(code=0, message="success", data=None)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("delete_model failed: %s", e)
        raise HTTPException(status_code=500, detail=AiErrors.INTERNAL_ERROR) from e


@router.post("/models/{model_id}/enable", summary="启用AI模型")
async def enable_model(model_id: str, user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE)):
    try:
        service = _get_ai_service()
        ok = await service.enable_model(model_id)
        if not ok:
            # FIXED: P1-1/P2-6 HTTPException.detail必须为字符串
            raise HTTPException(status_code=404, detail=AiErrors.MODEL_NOT_FOUND)
        return ApiResponse(code=0, message="success", data=None)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("enable_model failed: %s", e)
        raise HTTPException(status_code=500, detail=AiErrors.INTERNAL_ERROR) from e


@router.post("/models/{model_id}/disable", summary="停用AI模型")
async def disable_model(model_id: str, user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE)):
    try:
        service = _get_ai_service()
        ok = await service.disable_model(model_id)
        if not ok:
            # FIXED: P1-1/P2-6 HTTPException.detail必须为字符串
            raise HTTPException(status_code=404, detail=AiErrors.MODEL_NOT_FOUND)
        return ApiResponse(code=0, message="success", data=None)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("disable_model failed: %s", e)
        raise HTTPException(status_code=500, detail=AiErrors.INTERNAL_ERROR) from e


@router.post("/models/{model_id}/reload", summary="模型热加载")
async def reload_model(model_id: str, body: AiModelReloadRequest, user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE)):
    try:
        service = _get_ai_service()
        ok = await service.reload_model(model_id, body.model_file_path)
        if not ok:
            # FIXED: P1-1/P2-6 HTTPException.detail必须为字符串
            raise HTTPException(status_code=500, detail=AiErrors.MODEL_RELOAD_FAILED)
        return ApiResponse(code=0, message="success", data=None)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("reload_model failed: %s", e)
        raise HTTPException(status_code=500, detail=AiErrors.INTERNAL_ERROR) from e


@router.post("/inference", summary="手动触发AI推理")
async def inference(body: AiInferenceRequest, user: CurrentUser = require_permission(Permission.SYSTEM_READ)):
    try:
        service = _get_ai_service()
        result = await service.inference(
            body.model_id, body.input_data, body.device_id, body.point_name,
        )
        if result.get("status") == "error":
            # FIXED: P1-1/P2-6 HTTPException.detail必须为字符串，推理失败返回错误码
            raise HTTPException(status_code=500, detail=AiErrors.INFERENCE_FAILED)
        return ApiResponse(code=0, message="success", data=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("inference failed: %s", e)
        raise HTTPException(status_code=500, detail=AiErrors.INTERNAL_ERROR) from e


@router.get("/inference/logs", summary="推理日志查询")
async def get_inference_logs(
    model_id: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100, alias="size"),
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    try:
        service = _get_ai_service()
        result = await service.get_inference_logs(model_id, page, page_size)
        return PagedResponse(data=result["items"], total=result["total"], page=page, size=page_size)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_inference_logs failed: %s", e)
        raise HTTPException(status_code=500, detail=AiErrors.INTERNAL_ERROR) from e


@router.get("/stats", summary="推理统计概览")
async def get_stats(user: CurrentUser = require_permission(Permission.SYSTEM_READ)):
    try:
        service = _get_ai_service()
        stats = await service.get_stats()
        return ApiResponse(code=0, message="success", data=stats.model_dump())
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_stats failed: %s", e)
        raise HTTPException(status_code=500, detail=AiErrors.INTERNAL_ERROR) from e


@router.get("/models/{model_id}/stats", summary="单模型推理统计")
async def get_model_stats(model_id: str, user: CurrentUser = require_permission(Permission.SYSTEM_READ)):
    try:
        service = _get_ai_service()
        stats = await service.get_model_stats(model_id)
        if not stats:
            # FIXED: P1-1/P2-6 HTTPException.detail必须为字符串
            raise HTTPException(status_code=404, detail=AiErrors.MODEL_NOT_FOUND)
        return ApiResponse(code=0, message="success", data=stats)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_model_stats failed: %s", e)
        raise HTTPException(status_code=500, detail=AiErrors.INTERNAL_ERROR) from e


@router.post("/models/{model_id}/schedule", response_model=ApiResponse, summary="启动定时推理")
async def schedule_inference(
    model_id: str,
    body: ScheduleInferenceRequest,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    try:
        service = _get_ai_service()
        engine = service._engine
        try:
            await engine.start_scheduled_inference(
                model_id=model_id,
                device_id=body.device_id,
                point_name=body.point_name,
                interval_seconds=body.interval_seconds,
                input_window_size=body.input_window_size,
            )
        except ValueError as e:
            err_msg = str(e)
            if "already exists" in err_msg:
                raise HTTPException(status_code=409, detail=AiErrors.SCHEDULE_ALREADY_EXISTS) from e
            if "not available" in err_msg:
                raise HTTPException(status_code=400, detail=AiErrors.SCHEDULE_START_FAILED) from e
            raise HTTPException(status_code=400, detail=AiErrors.SCHEDULE_START_FAILED) from e
        return ApiResponse(code=0, message="success", data=None)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("schedule_inference failed: %s", e)
        raise HTTPException(status_code=500, detail=AiErrors.INTERNAL_ERROR) from e


@router.delete("/models/{model_id}/schedule", response_model=ApiResponse, summary="停止定时推理")
async def cancel_scheduled_inference(
    model_id: str,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    try:
        service = _get_ai_service()
        engine = service._engine
        stopped = await engine.stop_scheduled_inference(model_id)
        if not stopped:
            raise HTTPException(status_code=404, detail=AiErrors.SCHEDULE_NOT_FOUND)
        return ApiResponse(code=0, message="success", data=None)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("cancel_scheduled_inference failed: %s", e)
        raise HTTPException(status_code=500, detail=AiErrors.INTERNAL_ERROR) from e


@router.get("/schedules", response_model=ApiResponse, summary="获取所有定时推理配置")
async def list_scheduled_inferences(
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    try:
        service = _get_ai_service()
        engine = service._engine
        schedules = engine.get_scheduled_inferences()
        return ApiResponse(code=0, message="success", data=schedules)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("list_scheduled_inferences failed: %s", e)
        raise HTTPException(status_code=500, detail=AiErrors.INTERNAL_ERROR) from e
