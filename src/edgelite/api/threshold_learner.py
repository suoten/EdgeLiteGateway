"""阈值自学习 API 路由

使用 edgelite.engine.threshold_self_learner.ThresholdSelfLearner 与
edgelite.engine.edge_ai_inference.AiInferenceEngine 提供动态阈值自学习闭环。

注意：
- 模型 ID 固定为 elg-threshold-v1，输入维度 50，输出维度 1。
- infer 端点接收单个 value，内部填充为 50 维窗口后调用模型。
- 若 ai_engine 未初始化，infer 返回 status=unavailable 而非抛 503。
- decomposition 端点返回训练得到的 k 值与默认分解信息。
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from edgelite.api.deps import require_permission
from edgelite.api.error_codes import CommonErrors
from edgelite.models.common import ApiResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/threshold-learner", tags=["Threshold Self-Learner"])

_MODEL_ID = "elg-threshold-v1"
_INPUT_DIM = 50

# FIXED(P2): 模块级 learner 缓存加 LRU 上限——原实现按任意客户端 model_id 永久缓存，
# viewer 可脚本打爆内存。改为 OrderedDict LRU（maxlen=100，淘汰最久未用）。
_LEARNER_CACHE_MAX = 100
_learners: OrderedDict[str, Any] = OrderedDict()

# FIXED(P2): initial_data 长度上限——防止超大数组导致内存/CPU 放大
_MAX_INITIAL_DATA_LEN = 10000


class ThresholdInitRequest(BaseModel):
    """阈值自学习初始化请求体。"""

    device_id: str | None = None
    point_name: str | None = None
    config: dict[str, Any] | None = None
    model_id: str | None = None
    device_range: list[float] | None = None
    spec_limits: list[float] | None = None
    initial_data: list[float] | None = None


class ThresholdInferRequest(BaseModel):
    """阈值推理请求体。"""

    device_id: str | None = None
    point_name: str | None = None
    value: float | None = None
    input_window: list[float] | None = None
    model_id: str | None = None


class ThresholdFeedbackRequest(BaseModel):
    """阈值反馈请求体。"""

    device_id: str | None = None
    point_name: str | None = None
    in_range: bool
    value: float | None = None
    feedback_type: str | None = None
    reason: str | None = None
    model_id: str | None = None


def _get_ai_engine():
    from edgelite.app import _app_state

    return getattr(_app_state, "ai_engine", None)


def _get_models_dir() -> str:
    ai_engine = _get_ai_engine()
    if ai_engine is not None:
        models_dir = getattr(ai_engine, "_models_dir", None)
        if models_dir is not None:
            return str(models_dir)
    from pathlib import Path

    return str(Path(__file__).resolve().parent.parent / "ai_models")


def _get_or_create_learner(model_id: str):
    """获取或创建 learner 实例（LRU 缓存：命中移到队尾，超限淘汰最久未用）。"""
    if model_id in _learners:
        _learners.move_to_end(model_id)
        return _learners[model_id]
    from edgelite.engine.threshold_self_learner import ThresholdSelfLearner

    learner = ThresholdSelfLearner(models_dir=_get_models_dir(), ai_engine=_get_ai_engine())
    _learners[model_id] = learner
    while len(_learners) > _LEARNER_CACHE_MAX:
        evicted_id, _ = _learners.popitem(last=False)
        logger.debug("LRU evicted threshold learner: %s", evicted_id)
    return learner


def _pad_or_truncate(data: list[float], target_dim: int) -> list[float]:
    if len(data) >= target_dim:
        return [float(x) for x in data[:target_dim]]
    padded = [float(x) for x in data] + [0.0] * (target_dim - len(data))
    return padded


@router.post("/initialize", response_model=ApiResponse)
async def initialize(
    body: ThresholdInitRequest,
    _user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    """初始化阈值自学习器，可选注入初始样本并触发训练。"""
    try:
        # FIXED(P2): initial_data 长度上限校验（超出 400），防止超大数组打爆内存
        if body.initial_data is not None and len(body.initial_data) > _MAX_INITIAL_DATA_LEN:
            raise HTTPException(
                status_code=400,
                detail={
                    "error_code": CommonErrors.VALIDATION_FAILED,
                    "message": f"initial_data exceeds maximum length {_MAX_INITIAL_DATA_LEN}",
                },
            )
        model_id = body.model_id or _MODEL_ID
        learner = _get_or_create_learner(model_id)
        if body.initial_data:
            learner.add_sample(_pad_or_truncate(body.initial_data, _INPUT_DIM))
        result = {"model_id": model_id, "initialized": True, "sample_count": learner.get_sample_count()}
        if body.initial_data and learner.get_sample_count() >= learner.min_samples:
            train_result = await learner.train_and_export(force=False)
            result["train"] = train_result
        result["dashboard"] = learner.get_dashboard()
        return ApiResponse(data=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("threshold initialize failed: %s", e)
        raise HTTPException(status_code=503, detail=CommonErrors.SERVICE_NOT_READY) from None


@router.post("/infer", response_model=ApiResponse)
async def infer(
    body: ThresholdInferRequest,
    _user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """调用动态阈值模型进行推理。"""
    try:
        model_id = body.model_id or _MODEL_ID
        if body.input_window:
            input_data = _pad_or_truncate(body.input_window, _INPUT_DIM)
        elif body.value is not None:
            # 单值填充为 50 维窗口
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
        learner = _get_or_create_learner(model_id)
        learner.add_result({"input_value": body.value, "result": result.output_data, "status": result.status})
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
        logger.error("threshold infer failed: %s", e)
        raise HTTPException(status_code=503, detail=CommonErrors.SERVICE_NOT_READY) from None


@router.post("/feedback", response_model=ApiResponse)
async def feedback(
    body: ThresholdFeedbackRequest,
    _user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    """提交阈值反馈，累积达到阈值自动触发重训练。"""
    try:
        model_id = body.model_id or _MODEL_ID
        learner = _get_or_create_learner(model_id)
        fb_kwargs: dict[str, Any] = {
            "in_range": body.in_range,
            "value": body.value,
            "feedback_type": body.feedback_type,
            "reason": body.reason,
            "device_id": body.device_id,
            "point_name": body.point_name,
        }
        fb_kwargs = {k: v for k, v in fb_kwargs.items() if v is not None}
        result = await learner.submit_feedback(**fb_kwargs)
        return ApiResponse(data=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("threshold feedback failed: %s", e)
        raise HTTPException(status_code=503, detail=CommonErrors.SERVICE_NOT_READY) from None


@router.get("/dashboard", response_model=ApiResponse)
async def dashboard(
    _user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
    device_id: str | None = Query(default=None),
    model_id: str | None = Query(default=None),
):
    """返回阈值自学习仪表盘数据。"""
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
        logger.error("threshold dashboard failed: %s", e)
        raise HTTPException(status_code=503, detail=CommonErrors.SERVICE_NOT_READY) from None


@router.get("/decomposition", response_model=ApiResponse)
async def decomposition(
    _user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
    device_id: str = Query(...),
    point_name: str | None = Query(default=None),
    model_id: str | None = Query(default=None),
):
    """返回阈值分解信息（mean + k*std 模型参数）。

    ThresholdSelfLearner 训练后 _train 返回 k_squared 与 k，
    但这些权重不会持久化到 learner 实例上。
    本端点返回默认分解信息（k=3.0，3-sigma 规则）与最近训练统计。
    """
    try:
        mid = model_id or _MODEL_ID
        learner = _get_or_create_learner(mid)
        dash = learner.get_dashboard()
        data = {
            "model_id": mid,
            "device_id": device_id,
            "point_name": point_name,
            "method": "mean + k*std",
            "default_k": 3.0,
            "input_dim": _INPUT_DIM,
            "sample_count": dash.get("sample_count", 0),
            "train_count": dash.get("train_count", 0),
            "last_train_time": dash.get("last_train_time"),
            "last_train_error": dash.get("last_train_error"),
        }
        return ApiResponse(data=data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("threshold decomposition failed: %s", e)
        raise HTTPException(status_code=503, detail=CommonErrors.SERVICE_NOT_READY) from None
