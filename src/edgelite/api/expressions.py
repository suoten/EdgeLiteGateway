"""表达式管理API路由"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from edgelite.api.deps import CurrentUser, require_permission
from edgelite.models.common import ApiResponse
from edgelite.security.rbac import Permission

_MAX_EXPR_LEN = 2048
_MAX_BATCH_SIZE = 50

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/expressions", tags=["表达式管理"])


class ExpressionTestRequest(BaseModel):
    expression: str = Field(..., max_length=_MAX_EXPR_LEN)
    variables: dict[str, float | int | str | bool | None] | None = None


class ExpressionBatchRequest(BaseModel):
    expressions: dict[str, str] = Field(..., max_length=_MAX_BATCH_SIZE)
    variables: dict[str, float | int | str | bool | None] | None = None


_engine_instance = None


def _get_engine():
    global _engine_instance
    if _engine_instance is None:
        from edgelite.engine.expression_engine import ExpressionEngine

        _engine_instance = ExpressionEngine()
    return _engine_instance


@router.post("/evaluate", response_model=ApiResponse)
async def evaluate_expression(
    req: ExpressionTestRequest,
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    engine = _get_engine()
    try:
        result = engine.evaluate(req.expression, req.variables or {})
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"表达式求值失败: {e}") from e
    return ApiResponse(data={"expression": req.expression, "result": result})


@router.post("/evaluate-batch", response_model=ApiResponse)
async def evaluate_batch(
    req: ExpressionBatchRequest,
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    engine = _get_engine()
    try:
        results = engine.evaluate_batch(req.expressions, req.variables or {})
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"批量表达式求值失败: {e}") from e
    return ApiResponse(data={"results": results})


@router.post("/validate", response_model=ApiResponse)
async def validate_expression(
    req: ExpressionTestRequest,
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    engine = _get_engine()
    try:
        engine._validate_expression(engine._resolve_variables(req.expression, req.variables or {}))
        return ApiResponse(data={"valid": True, "expression": req.expression})
    except ValueError as e:
        return ApiResponse(data={"valid": False, "expression": req.expression, "error": str(e)})
    except Exception as e:
        return ApiResponse(data={"valid": False, "expression": req.expression, "error": str(e)})


@router.get("/functions", response_model=ApiResponse)
async def list_available_functions(
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    functions = [
        {"name": "abs", "description": "绝对值", "example": "abs(${device.temp})"},
        {"name": "round", "description": "四舍五入", "example": "round(${device.temp}, 2)"},
        {
            "name": "min",
            "description": "最小值",
            "example": "min(${device.temp1}, ${device.temp2})",
        },
        {
            "name": "max",
            "description": "最大值",
            "example": "max(${device.temp1}, ${device.temp2})",
        },
        {"name": "pow", "description": "幂运算", "example": "pow(${device.voltage}, 2)"},
        {"name": "sqrt", "description": "平方根", "example": "sqrt(${device.power})"},
        {"name": "int", "description": "转整数", "example": "int(${device.value})"},
        {"name": "float", "description": "转浮点", "example": "float(${device.value})"},
        {"name": "ceil", "description": "向上取整", "example": "ceil(${device.temp})"},
        {"name": "floor", "description": "向下取整", "example": "floor(${device.temp})"},
        {"name": "log", "description": "自然对数", "example": "log(${device.value})"},
        {"name": "log10", "description": "常用对数", "example": "log10(${device.value})"},
    ]
    try:
        operators = [
            {"symbol": "+", "description": "加法"},
            {"symbol": "-", "description": "减法"},
            {"symbol": "*", "description": "乘法"},
            {"symbol": "/", "description": "除法"},
            {"symbol": "%", "description": "取模"},
            {"symbol": "**", "description": "幂运算"},
            {"symbol": "==", "description": "等于"},
            {"symbol": "!=", "description": "不等于"},
            {"symbol": ">", "description": "大于"},
            {"symbol": "<", "description": "小于"},
            {"symbol": ">=", "description": "大于等于"},
            {"symbol": "<=", "description": "小于等于"},
            {"symbol": "and", "description": "逻辑与"},
            {"symbol": "or", "description": "逻辑或"},
            {"symbol": "not", "description": "逻辑非"},
        ]
        return ApiResponse(data={"functions": functions, "operators": operators})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("获取列表失败: %s", e)
        raise HTTPException(status_code=500, detail="获取列表失败") from e
