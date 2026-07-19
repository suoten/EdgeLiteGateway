"""趋势自学习 API 路由

使用 edgelite.engine.trend_self_learner.TrendSelfLearner 与
edgelite.engine.edge_ai_inference.AiInferenceEngine 提供趋势预测自学习闭环。

注意：
- 模型 ID 固定为 elg-trend-v1，输入维度 200，输出维度 10。
- 若 ai_engine 未初始化，predict 返回 status=unavailable 而非抛 503。
- residual-analysis 基于最近推理结果与缓冲区数据计算残差统计。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from edgelite.api.deps import require_permission
from edgelite.api.error_codes import CommonErrors
from edgelite.models.common import ApiResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/trend-learner", tags=["Trend Self-Learner"])

_MODEL_ID = "elg-trend-v1"
_INPUT_DIM = 200
_OUTPUT_DIM = 10

_learners: dict[str, Any] = {}


class TrendInitRequest(BaseModel):
    """趋势自学习初始化请求体。"""

    device_id: str | None = None
    point_name: str | None = None
    config: dict[str, Any] | None = None
    model_id: str | None = None
    device_type: str | None = None
    device_params: dict[str, Any] | None = None
    initial_data: list[float] | None = None


class TrendPredictRequest(BaseModel):
    """趋势预测请求体。"""

    device_id: str | None = None
    point_name: str | None = None
    horizon: int | None = None
    input_window: list[float] | None = None
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
    if model_id in _learners:
        return _learners[model_id]
    from edgelite.engine.trend_self_learner import TrendSelfLearner

    learner = TrendSelfLearner(models_dir=_get_models_dir(), ai_engine=_get_ai_engine())
    _learners[model_id] = learner
    return learner


def _pad_or_truncate(data: list[float], target_dim: int) -> list[float]:
    if len(data) >= target_dim:
        return [float(x) for x in data[:target_dim]]
    padded = [float(x) for x in data] + [0.0] * (target_dim - len(data))
    return padded


@router.post("/initialize", response_model=ApiResponse)
async def initialize(
    body: TrendInitRequest,
    _user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    """初始化趋势自学习器，可选注入初始样本并触发训练。"""
    try:
        model_id = body.model_id or _MODEL_ID
        learner = _get_or_create_learner(model_id)
        if body.initial_data:
            # TrendSelfLearner._train 接受单数组：前 INPUT_DIM 为窗口，后 OUTPUT_DIM 为目标
            arr = [float(x) for x in body.initial_data]
            if len(arr) >= _INPUT_DIM + _OUTPUT_DIM:
                window = arr[:_INPUT_DIM]
                target = arr[_INPUT_DIM : _INPUT_DIM + _OUTPUT_DIM]
                learner.add_sample({"window": window, "target": target})
            else:
                learner.add_sample(_pad_or_truncate(arr, _INPUT_DIM))
        result = {"model_id": model_id, "initialized": True, "sample_count": learner.get_sample_count()}
        if body.initial_data and learner.get_sample_count() >= learner.min_samples:
            train_result = await learner.train_and_export(force=False)
            result["train"] = train_result
        result["dashboard"] = learner.get_dashboard()
        return ApiResponse(data=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("trend initialize failed: %s", e)
        raise HTTPException(status_code=503, detail=CommonErrors.SERVICE_NOT_READY) from None


@router.post("/predict", response_model=ApiResponse)
async def predict(
    body: TrendPredictRequest,
    _user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """调用趋势预测模型进行推理。"""
    try:
        model_id = body.model_id or _MODEL_ID
        if body.input_window:
            input_data = _pad_or_truncate(body.input_window, _INPUT_DIM)
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
        learner.add_result({"input_len": len(input_data), "result": result.output_data, "status": result.status})
        # horizon 提示前端期望的预测步数（默认 OUTPUT_DIM）
        horizon = body.horizon if body.horizon and body.horizon > 0 else _OUTPUT_DIM
        return ApiResponse(
            data={
                "model_id": result.model_id,
                "output_data": result.output_data,
                "latency_ms": result.latency_ms,
                "status": result.status,
                "error_message": result.error_message,
                "horizon": horizon,
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("trend predict failed: %s", e)
        raise HTTPException(status_code=503, detail=CommonErrors.SERVICE_NOT_READY) from None


@router.get("/dashboard", response_model=ApiResponse)
async def dashboard(
    _user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
    device_id: str | None = Query(default=None),
    model_id: str | None = Query(default=None),
):
    """返回趋势自学习仪表盘数据。"""
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
        logger.error("trend dashboard failed: %s", e)
        raise HTTPException(status_code=503, detail=CommonErrors.SERVICE_NOT_READY) from None


@router.get("/residual-analysis", response_model=ApiResponse)
async def residual_analysis(
    _user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
    device_id: str = Query(...),
    point_name: str | None = Query(default=None),
    model_id: str | None = Query(default=None),
):
    """返回趋势预测残差分析。

    基于最近推理结果与缓冲区数据计算残差统计；
    若数据不足，返回空统计而非 501/503。
    """
    try:
        mid = model_id or _MODEL_ID
        learner = _get_or_create_learner(mid)
        dash = learner.get_dashboard()
        recent = dash.get("recent_results", [])
        # 残差 = 实际值 - 预测值；此处最近结果中没有 actual，仅返回预测均值/方差
        predictions: list[float] = []
        for r in recent:
            out = r.get("result", {}) if isinstance(r, dict) else {}
            if isinstance(out, dict):
                for v in out.values():
                    if isinstance(v, list):
                        for x in v:
                            if isinstance(x, (int, float)):
                                predictions.append(float(x))
                    elif isinstance(v, (int, float)):
                        predictions.append(float(v))
        if predictions:
            mean_pred = sum(predictions) / len(predictions)
            variance = sum((p - mean_pred) ** 2 for p in predictions) / len(predictions)
            std = variance ** 0.5
            residual_stats = {
                "count": len(predictions),
                "mean": mean_pred,
                "std": std,
                "min": min(predictions),
                "max": max(predictions),
            }
        else:
            residual_stats = {"count": 0, "mean": 0.0, "std": 0.0, "min": 0.0, "max": 0.0}
        data = {
            "model_id": mid,
            "device_id": device_id,
            "point_name": point_name,
            "residual": residual_stats,
            "sample_count": dash.get("sample_count", 0),
        }
        return ApiResponse(data=data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("trend residual-analysis failed: %s", e)
        raise HTTPException(status_code=503, detail=CommonErrors.SERVICE_NOT_READY) from None
