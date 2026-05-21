"""AI模型管理API路由"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query

from edgelite.api.deps import CurrentUser
from edgelite.api.error_codes import AiErrors
from edgelite.models.ai_model import (
    AiInferenceRequest,
    AiInferenceResponse,
    AiModelReloadRequest,
    AiModelUpdate,
    AiStatsResponse,
)
from edgelite.models.common import ApiResponse, PagedResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/ai", tags=["AI Inference"])


def _get_ai_service():
    from edgelite.app import _app_state

    service = getattr(_app_state, "ai_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail=ApiResponse(
            code=1, message=AiErrors.ENGINE_NOT_INITIALIZED, data=None,
        ).model_dump())
    return service


@router.get("/models", summary="AI模型列表")
async def list_models(
    _user: CurrentUser = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100, alias="size"),
):
    service = _get_ai_service()
    result = await service.list_models(page, page_size)
    return PagedResponse(data=result["items"], total=result["total"], page=page, size=page_size)


@router.get("/models/{model_id}", summary="AI模型详情")
async def get_model(model_id: str, _user: CurrentUser = None):
    service = _get_ai_service()
    model = await service.get_model(model_id)
    if not model:
        raise HTTPException(status_code=404, detail=ApiResponse(
            code=1, message=AiErrors.MODEL_NOT_FOUND, data=None,
        ).model_dump())
    return ApiResponse(code=0, message="success", data=model.model_dump())


@router.put("/models/{model_id}", summary="更新AI模型元信息")
async def update_model(model_id: str, body: AiModelUpdate, _user: CurrentUser = None):
    service = _get_ai_service()
    result = await service.update_model(model_id, body.model_dump(exclude_unset=True))
    if not result:
        raise HTTPException(status_code=404, detail=ApiResponse(
            code=1, message=AiErrors.MODEL_NOT_FOUND, data=None,
        ).model_dump())
    return ApiResponse(code=0, message="success", data=result.model_dump())


@router.delete("/models/{model_id}", summary="删除AI模型(预置不可删)")
async def delete_model(model_id: str, _user: CurrentUser = None):
    service = _get_ai_service()
    deleted = await service.delete_model(model_id)
    if not deleted:
        wrapper = service._engine.get_model(model_id)
        if wrapper and wrapper.is_preset:
            raise HTTPException(status_code=403, detail=ApiResponse(
                code=1, message=AiErrors.MODEL_DELETE_PRESET, data=None,
            ).model_dump())
        raise HTTPException(status_code=404, detail=ApiResponse(
            code=1, message=AiErrors.MODEL_NOT_FOUND, data=None,
        ).model_dump())
    return ApiResponse(code=0, message="success", data=None)


@router.post("/models/{model_id}/enable", summary="启用AI模型")
async def enable_model(model_id: str, _user: CurrentUser = None):
    service = _get_ai_service()
    ok = await service.enable_model(model_id)
    if not ok:
        raise HTTPException(status_code=404, detail=ApiResponse(
            code=1, message=AiErrors.MODEL_NOT_FOUND, data=None,
        ).model_dump())
    return ApiResponse(code=0, message="success", data=None)


@router.post("/models/{model_id}/disable", summary="停用AI模型")
async def disable_model(model_id: str, _user: CurrentUser = None):
    service = _get_ai_service()
    ok = await service.disable_model(model_id)
    if not ok:
        raise HTTPException(status_code=404, detail=ApiResponse(
            code=1, message=AiErrors.MODEL_NOT_FOUND, data=None,
        ).model_dump())
    return ApiResponse(code=0, message="success", data=None)


@router.post("/models/{model_id}/reload", summary="模型热加载")
async def reload_model(model_id: str, body: AiModelReloadRequest, _user: CurrentUser = None):
    service = _get_ai_service()
    ok = await service.reload_model(model_id, body.model_file_path)
    if not ok:
        raise HTTPException(status_code=500, detail=ApiResponse(
            code=1, message=AiErrors.MODEL_RELOAD_FAILED, data=None,
        ).model_dump())
    return ApiResponse(code=0, message="success", data=None)


@router.post("/inference", summary="手动触发AI推理")
async def inference(body: AiInferenceRequest, _user: CurrentUser = None):
    service = _get_ai_service()
    result = await service.inference(
        body.model_id, body.input_data, body.device_id, body.point_name,
    )
    if result.get("status") == "error":
        raise HTTPException(status_code=500, detail=ApiResponse(
            code=1, message=AiErrors.INFERENCE_FAILED, data=result,
        ).model_dump())
    return ApiResponse(code=0, message="success", data=result)


@router.get("/inference/logs", summary="推理日志查询")
async def get_inference_logs(
    model_id: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100, alias="size"),
    _user: CurrentUser = None,
):
    service = _get_ai_service()
    result = await service.get_inference_logs(model_id, page, page_size)
    return PagedResponse(data=result["items"], total=result["total"], page=page, size=page_size)


@router.get("/stats", summary="推理统计概览")
async def get_stats(_user: CurrentUser = None):
    service = _get_ai_service()
    stats = await service.get_stats()
    return ApiResponse(code=0, message="success", data=stats.model_dump())


@router.get("/models/{model_id}/stats", summary="单模型推理统计")
async def get_model_stats(model_id: str, _user: CurrentUser = None):
    service = _get_ai_service()
    stats = await service.get_model_stats(model_id)
    if not stats:
        raise HTTPException(status_code=404, detail=ApiResponse(
            code=1, message=AiErrors.MODEL_NOT_FOUND, data=None,
        ).model_dump())
    return ApiResponse(code=0, message="success", data=stats)
