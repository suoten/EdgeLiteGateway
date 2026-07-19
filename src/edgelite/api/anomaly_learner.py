"""异常自学习 API 路由

使用 edgelite.engine.anomaly_self_learner.AnomalySelfLearner 与
edgelite.engine.edge_ai_inference.AiInferenceEngine 提供异常检测自学习闭环：
- initialize: 初始化 learner 并可选注入初始样本
- infer: 通过 ai_engine 调用已加载的 ONNX 模型进行异常评分
- feedback: 提交反馈，达到阈值自动重训练
- dashboard: 返回 learner 状态与最近推理结果
- status: 返回简要状态

注意：
- 模型 ID 固定为 elg-anomaly-v1，输入维度 100。
- 若 ai_engine 未初始化，infer 返回 status=unavailable 而非抛 503。
- learner 实例按 model_id 缓存在模块级字典中，models_dir 取自 ai_engine._models_dir。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from edgelite.api.deps import require_permission
from edgelite.api.error_codes import AiErrors, CommonErrors
from edgelite.models.common import ApiResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/anomaly-learner", tags=["Anomaly Self-Learner"])

_MODEL_ID = "elg-anomaly-v1"
_INPUT_DIM = 100

# 模块级 learner 实例缓存：model_id -> AnomalySelfLearner
_learners: dict[str, Any] = {}


class AnomalyInitRequest(BaseModel):
    """异常自学习初始化请求体。

    兼容 task 规范 {device_id, point_name, config?} 与
    前端调用 {model_id?, device_type?, device_params?, anomaly_threshold?, initial_data?}。
    """

    device_id: str | None = None
    point_name: str | None = None
    config: dict[str, Any] | None = None
    model_id: str | None = None
    device_type: str | None = None
    device_params: dict[str, Any] | None = None
    anomaly_threshold: float | None = None
    initial_data: list[float] | None = None


class AnomalyInferRequest(BaseModel):
    """异常推理请求体。"""

    device_id: str | None = None
    point_name: str | None = None
    value: float | None = None
    input_window: list[float] | None = None
    model_id: str | None = None


class AnomalyFeedbackRequest(BaseModel):
    """异常反馈请求体。"""

    device_id: str | None = None
    point_name: str | None = None
    is_anomaly: bool
    value: float | None = None
    score: float | None = None
    feedback: str | None = None
    model_id: str | None = None


def _get_ai_engine():
    from edgelite.app import _app_state

    return getattr(_app_state, "ai_engine", None)


def _get_models_dir() -> str:
    """从 ai_engine 获取 models_dir，回退到包内默认目录。"""
    ai_engine = _get_ai_engine()
    if ai_engine is not None:
        models_dir = getattr(ai_engine, "_models_dir", None)
        if models_dir is not None:
            return str(models_dir)
    from pathlib import Path

    return str(Path(__file__).resolve().parent.parent / "ai_models")


def _get_or_create_learner(model_id: str):
    """获取或创建 AnomalySelfLearner 实例。"""
    if model_id in _learners:
        return _learners[model_id]
    from edgelite.engine.anomaly_self_learner import AnomalySelfLearner

    learner = AnomalySelfLearner(models_dir=_get_models_dir(), ai_engine=_get_ai_engine())
    _learners[model_id] = learner
    return learner


@router.post("/initialize", response_model=ApiResponse)
async def initialize(
    body: AnomalyInitRequest,
    _user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    """初始化异常自学习器，可选注入初始样本并触发训练。"""
    try:
        model_id = body.model_id or _MODEL_ID
        learner = _get_or_create_learner(model_id)
        if body.initial_data:
            learner.add_sample(_pad_or_truncate(body.initial_data, _INPUT_DIM))
        result = {"model_id": model_id, "initialized": True, "sample_count": learner.get_sample_count()}
        # 若提供初始数据且达到最小训练样本数，触发一次训练
        if body.initial_data and learner.get_sample_count() >= learner.min_samples:
            train_result = await learner.train_and_export(force=False)
            result["train"] = train_result
        result["dashboard"] = learner.get_dashboard()
        return ApiResponse(data=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("anomaly initialize failed: %s", e)
        raise HTTPException(status_code=503, detail=CommonErrors.SERVICE_NOT_READY) from None


@router.post("/infer", response_model=ApiResponse)
async def infer(
    body: AnomalyInferRequest,
    _user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """调用异常评分模型进行推理。"""
    try:
        model_id = body.model_id or _MODEL_ID
        # 构造输入窗口：优先使用 input_window，其次用 value 重复填充
        if body.input_window:
            input_data = _pad_or_truncate(body.input_window, _INPUT_DIM)
        elif body.value is not None:
            input_data = [float(body.value)] * _INPUT_DIM
        else:
            raise HTTPException(status_code=400, detail=CommonErrors.VALIDATION_FAILED)
        ai_engine = _get_ai_engine()
        if ai_engine is None:
            return ApiResponse(
                data={
                    "model_id": model_id,
                    "status": "unavailable",
                    "error_message": "AI engine not initialized",
                    "output_data": {},
                    "latency_ms": 0,
                }
            )
        result = await ai_engine.infer(model_id, input_data)
        # 记录推理结果到 learner 仪表盘
        learner = _get_or_create_learner(model_id)
        learner.add_result({"input": input_data[:10], "result": result.output_data, "status": result.status})
        return ApiResponse(
            data={
                "model_id": result.model_id,
                "output_data": result.output_data,
                "latency_ms": result.latency_ms,
                "status": result.status,
                "error_message": result.error_message,
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("anomaly infer failed: %s", e)
        raise HTTPException(status_code=503, detail=CommonErrors.SERVICE_NOT_READY) from None


@router.post("/feedback", response_model=ApiResponse)
async def feedback(
    body: AnomalyFeedbackRequest,
    _user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    """提交异常反馈，累积达到阈值自动触发重训练。"""
    try:
        model_id = body.model_id or _MODEL_ID
        learner = _get_or_create_learner(model_id)
        fb_kwargs: dict[str, Any] = {
            "is_anomaly": body.is_anomaly,
            "value": body.value,
            "score": body.score,
            "feedback": body.feedback,
            "device_id": body.device_id,
            "point_name": body.point_name,
        }
        # 移除 None 值避免干扰子类实现
        fb_kwargs = {k: v for k, v in fb_kwargs.items() if v is not None}
        result = await learner.submit_feedback(**fb_kwargs)
        return ApiResponse(data=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("anomaly feedback failed: %s", e)
        raise HTTPException(status_code=503, detail=CommonErrors.SERVICE_NOT_READY) from None


@router.get("/dashboard", response_model=ApiResponse)
async def dashboard(
    _user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
    device_id: str | None = Query(default=None, description="预留参数，当前按 model_id 聚合"),
    model_id: str | None = Query(default=None),
):
    """返回异常自学习仪表盘数据。"""
    try:
        mid = model_id or _MODEL_ID
        learner = _get_or_create_learner(mid)
        data = learner.get_dashboard()
        if device_id:
            data["device_id"] = device_id
        return ApiResponse(data=data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("anomaly dashboard failed: %s", e)
        raise HTTPException(status_code=503, detail=CommonErrors.SERVICE_NOT_READY) from None


@router.get("/status", response_model=ApiResponse)
async def status(
    _user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
    device_id: str | None = Query(default=None),
):
    """返回异常自学习简要状态。"""
    try:
        learner = _get_or_create_learner(_MODEL_ID)
        dash = learner.get_dashboard()
        data = {
            "model_id": dash.get("model_id"),
            "sample_count": dash.get("sample_count", 0),
            "train_count": dash.get("train_count", 0),
            "ready_to_train": dash.get("ready_to_train", False),
            "last_train_time": dash.get("last_train_time"),
            "last_train_error": dash.get("last_train_error"),
            "device_id": device_id,
        }
        return ApiResponse(data=data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("anomaly status failed: %s", e)
        raise HTTPException(status_code=503, detail=CommonErrors.SERVICE_NOT_READY) from None


def _pad_or_truncate(data: list[float], target_dim: int) -> list[float]:
    """将输入数组填充或截断到目标维度。"""
    if len(data) >= target_dim:
        return [float(x) for x in data[:target_dim]]
    padded = [float(x) for x in data] + [0.0] * (target_dim - len(data))
    return padded
