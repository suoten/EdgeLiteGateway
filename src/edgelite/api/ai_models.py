"""AI模型管理API路由"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

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
async def update_model(
    model_id: str, body: AiModelUpdate, user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE))
):
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
async def reload_model(
    model_id: str,
    body: AiModelReloadRequest,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
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
async def inference(
    body: AiInferenceRequest, user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ))
):
    try:
        service = _get_ai_service()
        result = await service.inference(
            body.model_id,
            body.input_data,
            body.device_id,
            body.point_name,
        )
        if result.get("status") == "error":
            error_msg = result.get("error_message", "")
            # FIXED: 推理失败返回具体错误信息，而非笼统500
            raise HTTPException(
                status_code=422,
                detail=f"{AiErrors.INFERENCE_FAILED}: {error_msg}" if error_msg else AiErrors.INFERENCE_FAILED,
            )
        return ApiResponse(code=0, message="success", data=result)
    except HTTPException:
        raise
    except RuntimeError as e:
        # 500-修复: 模型未加载/依赖缺失等 RuntimeError 转为 503 服务不可用，而非 500
        logger.error("inference runtime error: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None
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
        # FIXED: 原问题-直接调用 stats.model_dump()，但服务契约可能返回 dict
        # (如 mock 或 get_inference_summary 风格)，导致 AttributeError → 500。
        # 兼容 Pydantic 模型与 dict 两种返回形态。
        stats_data = stats.model_dump() if hasattr(stats, "model_dump") else stats
        return ApiResponse(code=0, message="success", data=stats_data)
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
        models_dir = (
            os.path.join(getattr(config, "data_dir", "data"), "ai_models")
            if config
            else os.path.join("data", "ai_models")
        )
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
            return ApiResponse(
                code=0,
                message="success",
                data={
                    "model_id": model_id,
                    "file_path": dest_path,
                    "file_size": len(content),
                },
            )
        except Exception as reg_err:
            logger.warning("Auto-register failed for uploaded model %s: %s", safe_name, reg_err)
            return ApiResponse(
                code=0,
                message="success",
                data={
                    "model_id": None,
                    "file_path": dest_path,
                    "file_size": len(content),
                    "note": "Model saved but auto-register failed. Use reload endpoint to load manually.",
                },
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("upload_model failed: %s", e)
        raise HTTPException(status_code=500, detail=AiErrors.INTERNAL_ERROR) from e


class RollbackRequest(BaseModel):
    target_version: str = Field(min_length=1)


@router.get(
    "/models/{model_id}/versions", summary="获取模型版本历史"
)  # FIXED-P1: 移除多余/ai/前缀，router prefix已是/api/v1/ai
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


# --- AI Enhanced 端点：A/B 测试 / 热切换 / 预后处理 / 缓存 / 资源 / 批量 ---

import json as _json
import uuid as _uuid
from datetime import UTC as _UTC, datetime as _datetime

from fastapi import Body as _Body
from fastapi import Query as _Query

_AB_TEST_TABLE = "ai_ab_tests"
_HOT_SWAP_TABLE = "ai_hot_swaps"
_PREPOST_TABLE = "ai_model_prepost"

_AB_TEST_DDL = (
    f"CREATE TABLE IF NOT EXISTS {_AB_TEST_TABLE} ("
    "id TEXT PRIMARY KEY, "
    "name TEXT NOT NULL, "
    "model_a TEXT NOT NULL, "
    "model_b TEXT NOT NULL, "
    "split_ratio REAL NOT NULL DEFAULT 0.5, "
    "traffic REAL NOT NULL DEFAULT 1.0, "
    "status TEXT NOT NULL DEFAULT 'running', "
    "promoted_model TEXT, "
    "created_at TEXT NOT NULL, "
    "updated_at TEXT NOT NULL)"
)

_HOT_SWAP_DDL = (
    f"CREATE TABLE IF NOT EXISTS {_HOT_SWAP_TABLE} ("
    "id TEXT PRIMARY KEY, "
    "model_id TEXT NOT NULL, "
    "target_device TEXT, "
    "status TEXT NOT NULL DEFAULT 'active', "
    "created_at TEXT NOT NULL)"
)

_PREPOST_DDL = (
    f"CREATE TABLE IF NOT EXISTS {_PREPOST_TABLE} ("
    "model_id TEXT PRIMARY KEY, "
    "preprocess TEXT, "
    "postprocess TEXT, "
    "updated_at TEXT NOT NULL)"
)

_PREPROCESS_STEPS = [
    {"name": "normalize", "description": "标准化（zero mean, unit variance）"},
    {"name": "min_max_scale", "description": "线性缩放到 [0,1]"},
    {"name": "clip_outliers", "description": "基于 3σ 截断离群值"},
    {"name": "fill_missing", "description": "缺失值填充（前向/均值）"},
    {"name": "resample", "description": "重采样到固定频率"},
    {"name": "fft_filter", "description": "FFT 频域滤波"},
    {"name": "window_slice", "description": "滑动窗口切片"},
]

_POSTPROCESS_STEPS = [
    {"name": "denormalize", "description": "反标准化到原始量纲"},
    {"name": "threshold", "description": "阈值化（二值化）"},
    {"name": "smooth", "description": "结果平滑（移动平均）"},
    {"name": "clip", "description": "结果裁剪到合理区间"},
    {"name": "aggregate", "description": "多模型结果聚合"},
    {"name": "format_output", "description": "格式化输出结构"},
]


async def _ensure_ai_tables() -> None:
    try:
        from edgelite.app import _app_state

        db = getattr(_app_state, "database", None)
        if db is None:
            return
        async with db.get_session() as session:
            from sqlalchemy import text

            await session.execute(text(_AB_TEST_DDL))
            await session.execute(text(_HOT_SWAP_DDL))
            await session.execute(text(_PREPOST_DDL))
            await session.commit()
    except Exception as e:
        logger.error("ai enhanced ensure tables failed: %s", e)


def _get_inference_cache() -> Any | None:
    try:
        from edgelite.app import _app_state

        svc = getattr(_app_state, "ai_service", None)
        if svc is None:
            return None
        return getattr(svc, "inference_cache", None) or getattr(svc, "_inference_cache", None)
    except Exception as e:
        logger.error("ai get inference cache failed: %s", e)
        return None


class AbTestCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    model_a: str = Field(..., min_length=1, max_length=128)
    model_b: str = Field(..., min_length=1, max_length=128)
    split_ratio: float | None = Field(default=0.5, ge=0.0, le=1.0)
    traffic: float | None = Field(default=1.0, ge=0.0, le=1.0)


class AbTestSplitRequest(BaseModel):
    ratio: float = Field(..., ge=0.0, le=1.0)


class AbTestPromoteRequest(BaseModel):
    model: str = Field(..., min_length=1, max_length=128)


class HotSwapRequest(BaseModel):
    model_id: str = Field(..., min_length=1, max_length=128)
    target_device: str | None = Field(default=None, max_length=128)


class PrePostProcessRequest(BaseModel):
    steps: list[dict[str, Any]] = Field(default_factory=list)


@router.post("/ab-test", response_model=ApiResponse)
async def create_ab_test(
    req: AbTestCreateRequest,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    try:
        from edgelite.app import _app_state

        db = getattr(_app_state, "database", None)
        if db is None:
            raise HTTPException(status_code=503, detail=CommonErrors.DB_NOT_READY)
        await _ensure_ai_tables()
        test_id = str(_uuid.uuid4())
        now = _datetime.now(_UTC).isoformat()
        async with db.get_session() as session:
            from sqlalchemy import text

            await session.execute(
                text(
                    f"INSERT INTO {_AB_TEST_TABLE} "
                    "(id, name, model_a, model_b, split_ratio, traffic, status, created_at, updated_at) "
                    "VALUES (:id, :name, :a, :b, :split, :traffic, 'running', :ts, :ts)"
                ),
                {
                    "id": test_id,
                    "name": req.name,
                    "a": req.model_a,
                    "b": req.model_b,
                    "split": float(req.split_ratio or 0.5),
                    "traffic": float(req.traffic or 1.0),
                    "ts": now,
                },
            )
            await session.commit()
        return ApiResponse(
            data={
                "id": test_id,
                "name": req.name,
                "model_a": req.model_a,
                "model_b": req.model_b,
                "split_ratio": req.split_ratio or 0.5,
                "traffic": req.traffic or 1.0,
                "status": "running",
                "created_at": now,
                "updated_at": now,
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("ai ab-test create failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.get("/ab-test", response_model=PagedResponse)
async def list_ab_tests(
    page: int = _Query(default=1, ge=1),
    size: int = _Query(default=20, ge=1, le=100),
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    try:
        from edgelite.app import _app_state

        db = getattr(_app_state, "database", None)
        if db is None:
            return PagedResponse(data=[], total=0, page=page, size=size)
        await _ensure_ai_tables()
        async with db.get_session() as session:
            from sqlalchemy import text

            total_result = await session.execute(text(f"SELECT COUNT(*) FROM {_AB_TEST_TABLE}"))
            total = int(total_result.scalar() or 0)
            offset = (page - 1) * size
            result = await session.execute(
                text(
                    f"SELECT id, name, model_a, model_b, split_ratio, traffic, status, "
                    "promoted_model, created_at, updated_at "
                    f"FROM {_AB_TEST_TABLE} ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
                ),
                {"limit": size, "offset": offset},
            )
            rows = result.fetchall()
        items = [
            {
                "id": r[0],
                "name": r[1],
                "model_a": r[2],
                "model_b": r[3],
                "split_ratio": r[4],
                "traffic": r[5],
                "status": r[6],
                "promoted_model": r[7],
                "created_at": r[8],
                "updated_at": r[9],
            }
            for r in rows
        ]
        return PagedResponse(data=items, total=total, page=page, size=size)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("ai ab-test list failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.get("/ab-test/{test_id}", response_model=ApiResponse)
async def get_ab_test(
    test_id: str,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    try:
        from edgelite.app import _app_state

        db = getattr(_app_state, "database", None)
        if db is None:
            return ApiResponse(data=None)
        await _ensure_ai_tables()
        async with db.get_session() as session:
            from sqlalchemy import text

            result = await session.execute(
                text(
                    f"SELECT id, name, model_a, model_b, split_ratio, traffic, status, "
                    "promoted_model, created_at, updated_at "
                    f"FROM {_AB_TEST_TABLE} WHERE id=:id"
                ),
                {"id": test_id},
            )
            r = result.fetchone()
        if not r:
            raise HTTPException(status_code=404, detail=AiErrors.MODEL_NOT_FOUND)
        return ApiResponse(
            data={
                "id": r[0],
                "name": r[1],
                "model_a": r[2],
                "model_b": r[3],
                "split_ratio": r[4],
                "traffic": r[5],
                "status": r[6],
                "promoted_model": r[7],
                "created_at": r[8],
                "updated_at": r[9],
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("ai ab-test get failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.post("/ab-test/{test_id}/split", response_model=ApiResponse)
async def update_ab_test_split(
    test_id: str,
    req: AbTestSplitRequest,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    try:
        from edgelite.app import _app_state

        db = getattr(_app_state, "database", None)
        if db is None:
            raise HTTPException(status_code=503, detail=CommonErrors.DB_NOT_READY)
        await _ensure_ai_tables()
        now = _datetime.now(_UTC).isoformat()
        async with db.get_session() as session:
            from sqlalchemy import text

            result = await session.execute(
                text(f"UPDATE {_AB_TEST_TABLE} SET split_ratio=:r, updated_at=:ts WHERE id=:id"),
                {"r": req.ratio, "ts": now, "id": test_id},
            )
            if result.rowcount == 0:
                raise HTTPException(status_code=404, detail=AiErrors.MODEL_NOT_FOUND)
            await session.commit()
        return ApiResponse(data={"id": test_id, "split_ratio": req.ratio, "updated_at": now})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("ai ab-test split failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.post("/ab-test/{test_id}/promote", response_model=ApiResponse)
async def promote_ab_test(
    test_id: str,
    req: AbTestPromoteRequest,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    try:
        from edgelite.app import _app_state

        db = getattr(_app_state, "database", None)
        if db is None:
            raise HTTPException(status_code=503, detail=CommonErrors.DB_NOT_READY)
        await _ensure_ai_tables()
        now = _datetime.now(_UTC).isoformat()
        async with db.get_session() as session:
            from sqlalchemy import text

            result = await session.execute(
                text(
                    f"UPDATE {_AB_TEST_TABLE} SET promoted_model=:m, status='promoted', updated_at=:ts "
                    "WHERE id=:id"
                ),
                {"m": req.model, "ts": now, "id": test_id},
            )
            if result.rowcount == 0:
                raise HTTPException(status_code=404, detail=AiErrors.MODEL_NOT_FOUND)
            await session.commit()
        return ApiResponse(
            data={"id": test_id, "promoted_model": req.model, "status": "promoted", "updated_at": now}
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("ai ab-test promote failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.post("/ab-test/{test_id}/rollback", response_model=ApiResponse)
async def rollback_ab_test(
    test_id: str,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    try:
        from edgelite.app import _app_state

        db = getattr(_app_state, "database", None)
        if db is None:
            raise HTTPException(status_code=503, detail=CommonErrors.DB_NOT_READY)
        await _ensure_ai_tables()
        now = _datetime.now(_UTC).isoformat()
        async with db.get_session() as session:
            from sqlalchemy import text

            result = await session.execute(
                text(
                    f"UPDATE {_AB_TEST_TABLE} SET status='rolled_back', promoted_model=NULL, "
                    "updated_at=:ts WHERE id=:id"
                ),
                {"ts": now, "id": test_id},
            )
            if result.rowcount == 0:
                raise HTTPException(status_code=404, detail=AiErrors.MODEL_NOT_FOUND)
            await session.commit()
        return ApiResponse(data={"id": test_id, "status": "rolled_back", "updated_at": now})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("ai ab-test rollback failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.post("/hot-swap", response_model=ApiResponse)
async def create_hot_swap(
    req: HotSwapRequest,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    try:
        from edgelite.app import _app_state

        db = getattr(_app_state, "database", None)
        if db is None:
            raise HTTPException(status_code=503, detail=CommonErrors.DB_NOT_READY)
        await _ensure_ai_tables()
        swap_id = str(_uuid.uuid4())
        now = _datetime.now(_UTC).isoformat()
        async with db.get_session() as session:
            from sqlalchemy import text

            await session.execute(
                text(
                    f"INSERT INTO {_HOT_SWAP_TABLE} (id, model_id, target_device, status, created_at) "
                    "VALUES (:id, :mid, :dev, 'active', :ts)"
                ),
                {"id": swap_id, "mid": req.model_id, "dev": req.target_device, "ts": now},
            )
            await session.commit()
        return ApiResponse(
            data={
                "id": swap_id,
                "model_id": req.model_id,
                "target_device": req.target_device,
                "status": "active",
                "created_at": now,
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("ai hot-swap create failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.get("/hot-swap", response_model=PagedResponse)
async def list_hot_swaps(
    page: int = _Query(default=1, ge=1),
    size: int = _Query(default=20, ge=1, le=100),
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    try:
        from edgelite.app import _app_state

        db = getattr(_app_state, "database", None)
        if db is None:
            return PagedResponse(data=[], total=0, page=page, size=size)
        await _ensure_ai_tables()
        async with db.get_session() as session:
            from sqlalchemy import text

            total_result = await session.execute(text(f"SELECT COUNT(*) FROM {_HOT_SWAP_TABLE}"))
            total = int(total_result.scalar() or 0)
            offset = (page - 1) * size
            result = await session.execute(
                text(
                    f"SELECT id, model_id, target_device, status, created_at "
                    f"FROM {_HOT_SWAP_TABLE} ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
                ),
                {"limit": size, "offset": offset},
            )
            rows = result.fetchall()
        items = [
            {
                "id": r[0],
                "model_id": r[1],
                "target_device": r[2],
                "status": r[3],
                "created_at": r[4],
            }
            for r in rows
        ]
        return PagedResponse(data=items, total=total, page=page, size=size)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("ai hot-swap list failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.post("/models/{model_id}/preprocess", response_model=ApiResponse)
async def set_preprocess(
    model_id: str,
    req: PrePostProcessRequest,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    try:
        from edgelite.app import _app_state

        db = getattr(_app_state, "database", None)
        if db is None:
            raise HTTPException(status_code=503, detail=CommonErrors.DB_NOT_READY)
        await _ensure_ai_tables()
        now = _datetime.now(_UTC).isoformat()
        async with db.get_session() as session:
            from sqlalchemy import text

            await session.execute(
                text(
                    f"INSERT INTO {_PREPOST_TABLE} (model_id, preprocess, postprocess, updated_at) "
                    "VALUES (:id, :pre, NULL, :ts) "
                    "ON CONFLICT(model_id) DO UPDATE SET preprocess=:pre, updated_at=:ts"
                ),
                {"id": model_id, "pre": _json.dumps(req.steps, ensure_ascii=False), "ts": now},
            )
            await session.commit()
        return ApiResponse(data={"model_id": model_id, "preprocess": req.steps, "updated_at": now})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("ai set preprocess failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.get("/models/{model_id}/preprocess", response_model=ApiResponse)
async def get_preprocess(
    model_id: str,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    try:
        from edgelite.app import _app_state

        db = getattr(_app_state, "database", None)
        if db is None:
            return ApiResponse(data=None)
        await _ensure_ai_tables()
        async with db.get_session() as session:
            from sqlalchemy import text

            result = await session.execute(
                text(f"SELECT preprocess FROM {_PREPOST_TABLE} WHERE model_id=:id"),
                {"id": model_id},
            )
            r = result.fetchone()
        steps = []
        if r and r[0]:
            try:
                steps = _json.loads(r[0])
            except Exception:
                steps = []
        return ApiResponse(data={"model_id": model_id, "preprocess": steps})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("ai get preprocess failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.post("/models/{model_id}/postprocess", response_model=ApiResponse)
async def set_postprocess(
    model_id: str,
    req: PrePostProcessRequest,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    try:
        from edgelite.app import _app_state

        db = getattr(_app_state, "database", None)
        if db is None:
            raise HTTPException(status_code=503, detail=CommonErrors.DB_NOT_READY)
        await _ensure_ai_tables()
        now = _datetime.now(_UTC).isoformat()
        async with db.get_session() as session:
            from sqlalchemy import text

            await session.execute(
                text(
                    f"INSERT INTO {_PREPOST_TABLE} (model_id, preprocess, postprocess, updated_at) "
                    "VALUES (:id, NULL, :post, :ts) "
                    "ON CONFLICT(model_id) DO UPDATE SET postprocess=:post, updated_at=:ts"
                ),
                {"id": model_id, "post": _json.dumps(req.steps, ensure_ascii=False), "ts": now},
            )
            await session.commit()
        return ApiResponse(data={"model_id": model_id, "postprocess": req.steps, "updated_at": now})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("ai set postprocess failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.get("/models/{model_id}/postprocess", response_model=ApiResponse)
async def get_postprocess(
    model_id: str,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    try:
        from edgelite.app import _app_state

        db = getattr(_app_state, "database", None)
        if db is None:
            return ApiResponse(data=None)
        await _ensure_ai_tables()
        async with db.get_session() as session:
            from sqlalchemy import text

            result = await session.execute(
                text(f"SELECT postprocess FROM {_PREPOST_TABLE} WHERE model_id=:id"),
                {"id": model_id},
            )
            r = result.fetchone()
        steps = []
        if r and r[0]:
            try:
                steps = _json.loads(r[0])
            except Exception:
                steps = []
        return ApiResponse(data={"model_id": model_id, "postprocess": steps})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("ai get postprocess failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.get("/preprocess/steps", response_model=ApiResponse)
async def list_preprocess_steps(
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    try:
        return ApiResponse(data=_PREPROCESS_STEPS)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("ai preprocess steps failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.get("/postprocess/steps", response_model=ApiResponse)
async def list_postprocess_steps(
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    try:
        return ApiResponse(data=_POSTPROCESS_STEPS)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("ai postprocess steps failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.get("/cache/stats", response_model=ApiResponse)
async def get_cache_stats(
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    try:
        cache = _get_inference_cache()
        if cache is None:
            return ApiResponse(data={"enabled": False, "size": 0, "hits": 0, "misses": 0})
        stats = {
            "enabled": True,
            "size": int(getattr(cache, "size", 0) or 0),
            "max_size": int(getattr(cache, "max_size", 0) or 0),
            "hits": int(getattr(cache, "hits", 0) or 0),
            "misses": int(getattr(cache, "misses", 0) or 0),
        }
        return ApiResponse(data=stats)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("ai cache stats failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.post("/cache/clear", response_model=ApiResponse)
async def clear_cache(
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    try:
        cache = _get_inference_cache()
        cleared = 0
        if cache is not None:
            clear_method = getattr(cache, "clear", None)
            if callable(clear_method):
                import asyncio as _asyncio

                if _asyncio.iscoroutinefunction(clear_method):
                    await clear_method()
                else:
                    clear_method()
                cleared = int(getattr(cache, "size", 0) or 0)
        return ApiResponse(data={"cleared": cleared})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("ai cache clear failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.get("/resources", response_model=ApiResponse)
async def get_resources(
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    try:
        try:
            import psutil
        except ImportError:
            psutil = None  # type: ignore

        data: dict[str, Any] = {"cpu_percent": None, "memory": None, "gpu": None}
        if psutil is not None:
            data["cpu_percent"] = psutil.cpu_percent(interval=0.1)
            mem = psutil.virtual_memory()
            data["memory"] = {
                "total": mem.total,
                "available": mem.available,
                "used": mem.used,
                "percent": mem.percent,
            }
        # GPU 状态探测
        try:
            import pynvml

            pynvml.nvmlInit()
            count = pynvml.nvmlDeviceGetCount()
            gpus = []
            for i in range(count):
                handle = pynvml.nvmlDeviceGetHandleByIndex(i)
                util = pynvml.nvmlDeviceGetUtilizationRates(handle)
                gpus.append({"index": i, "gpu_util": util.gpu, "memory_util": util.memory})
            data["gpu"] = gpus
        except Exception:
            data["gpu"] = []
        return ApiResponse(data=data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("ai resources failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.get("/latency/{model_id}", response_model=ApiResponse)
async def get_latency_stats(
    model_id: str,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    try:
        from edgelite.app import _app_state

        svc = getattr(_app_state, "ai_service", None)
        if svc is None:
            return ApiResponse(
                data={"model_id": model_id, "count": 0, "avg_ms": 0.0, "p95_ms": 0.0, "p99_ms": 0.0}
            )
        getter = getattr(svc, "get_latency_stats", None)
        if callable(getter):
            import asyncio as _asyncio

            data = await getter(model_id) if _asyncio.iscoroutinefunction(getter) else getter(model_id)
            if isinstance(data, dict):
                data.setdefault("model_id", model_id)
                return ApiResponse(data=data)
        return ApiResponse(
            data={"model_id": model_id, "count": 0, "avg_ms": 0.0, "p95_ms": 0.0, "p99_ms": 0.0}
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("ai latency stats failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.get("/devices", response_model=PagedResponse)
async def list_inference_devices(
    page: int = _Query(default=1, ge=1),
    size: int = _Query(default=20, ge=1, le=100),
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    try:
        devices = [
            {"name": "cpu", "type": "cpu", "available": True},
            {"name": "cuda:0", "type": "gpu", "available": False},
            {"name": "cuda:1", "type": "gpu", "available": False},
        ]
        total = len(devices)
        start_idx = (page - 1) * size
        page_items = devices[start_idx : start_idx + size]
        return PagedResponse(data=page_items, total=total, page=page, size=size)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("ai devices list failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.get("/batch/stats", response_model=ApiResponse)
async def get_batch_stats(
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    try:
        from edgelite.app import _app_state

        svc = getattr(_app_state, "ai_service", None)
        if svc is None:
            return ApiResponse(
                data={"total_batches": 0, "total_inferences": 0, "avg_batch_size": 0.0, "avg_latency_ms": 0.0}
            )
        getter = getattr(svc, "get_batch_stats", None)
        if callable(getter):
            import asyncio as _asyncio

            data = await getter() if _asyncio.iscoroutinefunction(getter) else getter()
            if isinstance(data, dict):
                return ApiResponse(data=data)
        return ApiResponse(
            data={"total_batches": 0, "total_inferences": 0, "avg_batch_size": 0.0, "avg_latency_ms": 0.0}
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("ai batch stats failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None
