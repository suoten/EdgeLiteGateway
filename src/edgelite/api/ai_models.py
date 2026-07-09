"""AI模型管理API路由"""

from __future__ import annotations

import asyncio
import logging
import os

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from edgelite.api.deps import require_permission
from edgelite.api.error_codes import AiErrors, CommonErrors
from edgelite.models.ai_model import (
    AiInferenceRequest,
    AiModelReloadRequest,
    AiModelUpdate,
    ScheduleInferenceRequest,
)
from edgelite.models.common import ApiResponse, PagedResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/ai", tags=["AI Inference"])

# R11-API-24: 提取模型上传大小上限为模块常量，消除魔法数字
_MAX_MODEL_SIZE = 200 * 1024 * 1024


def _get_ai_service():
    from edgelite.app import _app_state

    service = getattr(_app_state, "ai_service", None)
    if service is None:
        # FIXED: P1-1/P2-6 HTTPException.detail必须为字符串，而非嵌套ApiResponse字典
        raise HTTPException(status_code=503, detail=AiErrors.ENGINE_NOT_INITIALIZED)
    return service


def _write_file_sync(path: str, content: bytes) -> None:
    """同步写入文件的辅助函数，供 asyncio.to_thread 调用以避免阻塞事件循环"""
    with open(path, "wb") as f:
        f.write(content)


@router.get("/models", summary="AI模型列表")
async def list_models(
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
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
async def get_model(model_id: str, user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ))):
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
async def update_model(model_id: str, body: AiModelUpdate, user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE))):
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
async def delete_model(model_id: str, user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE))):
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
async def enable_model(model_id: str, user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE))):
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
async def disable_model(model_id: str, user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE))):
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
async def reload_model(model_id: str, body: AiModelReloadRequest, user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE))):
    # FIXED(安全): 路径遍历防护 - 拒绝绝对路径/UNC路径/盘符路径/父目录引用，
    # 并使用 resolve() 解析后校验在允许的 ai_models 目录内（与 inference_api.py 防护逻辑一致）
    from pathlib import Path

    _model_path = Path(body.model_file_path)
    if (
        _model_path.is_absolute()
        or ".." in body.model_file_path
        or ":" in body.model_file_path[:3]
        or "\\\\" in body.model_file_path
    ):
        raise HTTPException(status_code=400, detail=AiErrors.MODEL_RELOAD_FAILED)
    resolved = _model_path.resolve()
    allowed_base = (Path(__file__).resolve().parent.parent / "ai_models").resolve()
    if not str(resolved).startswith(str(allowed_base) + os.sep) and resolved != allowed_base:
        raise HTTPException(status_code=403, detail=AiErrors.MODEL_RELOAD_FAILED)

    try:
        service = _get_ai_service()
        ok = await service.reload_model(model_id, body.model_file_path)
        if not ok:
            # FIXED: reload失败返回422（参数/格式错误），而非500
            raise HTTPException(status_code=422, detail=AiErrors.MODEL_RELOAD_FAILED)
        return ApiResponse(code=0, message="success", data=None)
    except ValueError as e:
        logger.warning("reload_model 验证失败: %s", e)
        raise HTTPException(status_code=422, detail=CommonErrors.VALIDATION_FAILED) from e
    except HTTPException:
        raise
    except Exception as e:
        logger.error("reload_model failed: %s", e)
        raise HTTPException(status_code=500, detail=AiErrors.INTERNAL_ERROR) from e


@router.post("/inference", summary="手动触发AI推理")
async def inference(body: AiInferenceRequest, user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ))):
    try:
        service = _get_ai_service()
        result = await service.inference(
            body.model_id, body.input_data, body.device_id, body.point_name,
        )
        if result.get("status") == "error":
            error_msg = result.get("error_message", "")
            # FIXED: 推理失败返回具体错误信息，而非笼统500
            raise HTTPException(status_code=422, detail=f"{AiErrors.INFERENCE_FAILED}: {error_msg}" if error_msg else AiErrors.INFERENCE_FAILED)
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
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
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
async def get_stats(user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ))):
    try:
        service = _get_ai_service()
        stats = await service.get_stats()
        return ApiResponse(code=0, message="success", data=stats.model_dump())
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_stats failed: %s", e)
        raise HTTPException(status_code=500, detail=AiErrors.INTERNAL_ERROR) from e


@router.get("/summary", summary="AI推理汇总统计")
async def get_inference_summary(user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ))):
    try:
        service = _get_ai_service()
        summary = await service.get_inference_summary()
        return ApiResponse(code=0, message="success", data=summary)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_inference_summary failed: %s", e)
        raise HTTPException(status_code=500, detail=AiErrors.INTERNAL_ERROR) from e


@router.get("/models/{model_id}/stats", summary="单模型推理统计")
async def get_model_stats(model_id: str, user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ))):
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
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
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
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
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
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
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


@router.post("/models/upload", summary="上传AI模型文件")
async def upload_model(
    file: UploadFile = File(...),
    # R11-API-24: name 添加 max_length 约束，防止超长输入
    name: str = Form(default="", max_length=128),
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    try:
        # Validate file type
        # FIXED-LP10: .pt/.pth/.h5 基于 pickle 序列化，存在 RCE 风险，仅允许安全格式
        allowed_extensions = {".onnx", ".tflite", ".pmml"}
        _, ext = os.path.splitext(file.filename or "")
        if ext.lower() not in allowed_extensions:
            raise HTTPException(
                status_code=400,
                detail=AiErrors.MODEL_FORMAT_INVALID,
            )

        # Save to models directory
        from edgelite.app import _app_state
        config = getattr(_app_state, "config", None)
        models_dir = os.path.join(
            getattr(config, "data_dir", "data"), "ai_models"
        ) if config else os.path.join("data", "ai_models")
        os.makedirs(models_dir, exist_ok=True)

        model_name = name or os.path.splitext(file.filename or "uploaded_model")[0]
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in model_name)
        dest_path = os.path.join(models_dir, f"{safe_name}{ext.lower()}")

        # FIXED(P2): 原问题-上传模型文件无重名检查，同名上传会覆盖正在使用的模型文件;
        # 修复-文件已存在时返回409 Conflict，避免覆盖正在使用的模型
        if os.path.exists(dest_path):
            raise HTTPException(
                status_code=409,
                detail=f"{AiErrors.MODEL_FORMAT_INVALID}: Model file already exists",
            )

        # FIXED-P1: 限制上传文件大小为200MB，防止磁盘耗尽攻击
        # R11-API-24: 使用模块常量 _MAX_MODEL_SIZE 替代魔法数字
        max_file_size = _MAX_MODEL_SIZE
        content = await file.read(max_file_size + 1)
        if len(content) > max_file_size:
            raise HTTPException(
                status_code=413,
                detail=f"{AiErrors.MODEL_FORMAT_INVALID}: File size exceeds {max_file_size // (1024 * 1024)}MB limit",
            )

        # 改为异步写入，避免阻塞事件循环
        await asyncio.to_thread(_write_file_sync, dest_path, content)

        logger.info("Model uploaded: %s -> %s (%d bytes)", file.filename, dest_path, len(content))

        # Auto-register the model
        service = _get_ai_service()
        try:
            model_id = await service.register_uploaded_model(safe_name, dest_path)
            return ApiResponse(code=0, message="success", data={
                "model_id": model_id,
                "file_path": dest_path,
                "file_size": len(content),
            })
        except Exception as reg_err:
            logger.warning("Auto-register failed for uploaded model %s: %s", safe_name, reg_err)
            return ApiResponse(code=0, message="success", data={
                "model_id": None,
                "file_path": dest_path,
                "file_size": len(content),
                "note": "Model saved but auto-register failed. Use reload endpoint to load manually.",
            })
    except HTTPException:
        raise
    except Exception as e:
        logger.error("upload_model failed: %s", e)
        raise HTTPException(status_code=500, detail=AiErrors.INTERNAL_ERROR) from e


class RollbackRequest(BaseModel):
    target_version: str = Field(min_length=1)


@router.get("/models/{model_id}/versions", summary="获取模型版本历史")  # FIXED-P1: 移除多余/ai/前缀，router prefix已是/api/v1/ai
async def get_model_versions(
    model_id: str,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    try:
        service = _get_ai_service()
        engine = service._engine
        history = engine.get_model_version_history(model_id)
        return ApiResponse(data=history)
    except Exception as e:
        logger.error("get model versions failed: %s", e)
        raise HTTPException(status_code=500, detail=AiErrors.INTERNAL_ERROR) from e


@router.post("/models/{model_id}/rollback", summary="回滚到指定版本")  # FIXED-P1: 移除多余/ai/前缀
async def rollback_model(
    model_id: str,
    body: RollbackRequest,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    try:
        service = _get_ai_service()
        engine = service._engine
        ok = await engine.rollback_model_version(model_id, body.target_version)
        return ApiResponse(data={"rolled_back": ok, "target_version": body.target_version})
    except ValueError as e:
        # 版本格式错误或参数无效
        logger.warning("rollback_model 验证失败: %s", e)
        raise HTTPException(status_code=422, detail=CommonErrors.VALIDATION_FAILED) from e
    except KeyError as e:
        # 版本不存在
        raise HTTPException(status_code=404, detail=AiErrors.MODEL_NOT_FOUND) from e
    except HTTPException:
        raise
    except Exception as e:
        logger.error("rollback model failed: %s", e)
        raise HTTPException(status_code=500, detail=AiErrors.INTERNAL_ERROR) from e
