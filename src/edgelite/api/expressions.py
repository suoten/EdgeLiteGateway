"""表达式管理API路由"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from edgelite.api.deps import require_permission
from edgelite.api.error_codes import ExpressionErrors
from edgelite.constants import _EXPRESSION_BATCH_LIMIT, _EXPRESSION_MAX_LENGTH
from edgelite.models.common import ApiResponse
from edgelite.security.rbac import Permission

_MAX_EXPR_LEN = _EXPRESSION_MAX_LENGTH  # FIXED: 原问题-硬编码魔法数字，现引用constants.py
_MAX_BATCH_SIZE = _EXPRESSION_BATCH_LIMIT
# FIXED(P3): 单条表达式硬上限 10000 字符——evaluate-batch 的 dict 值没有 Pydantic 约束，
# 批量接口可夹带超长表达式绕过 /evaluate 的 2048 上限；超出返回 400
_MAX_EXPR_HARD_LIMIT = 10000

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/expressions", tags=["Expressions"])


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


def _validate_batch_expression_lengths(expressions: dict[str, str]) -> None:
    """批量表达式逐条长度校验（FIXED P3：超出硬上限返回 400）。"""
    for name, expr in expressions.items():
        if len(expr) > _MAX_EXPR_HARD_LIMIT:
            raise HTTPException(
                status_code=400,
                detail={
                    "error_code": ExpressionErrors.EVALUATE_FAILED,
                    "message": f"expression '{name}' exceeds maximum length {_MAX_EXPR_HARD_LIMIT}",
                },
            )


@router.post("/evaluate", response_model=ApiResponse)
async def evaluate_expression(
    req: ExpressionTestRequest,
    user: dict[str, str] = Depends(
        require_permission(Permission.CONFIG_EDIT)
    ),  # SEC-FIX: 表达式引擎可执行计算，权限从 SYSTEM_READ 提升为 CONFIG_EDIT（viewer 无此权限）
):
    engine = _get_engine()
    try:
        # FIXED(P3): engine.evaluate 是同步 CPU 操作，包 asyncio.to_thread 避免阻塞事件循环
        result = await asyncio.to_thread(engine.evaluate, req.expression, req.variables or {})
    except Exception as e:
        # FIXED: 原问题-中文硬编码detail，改为error_code
        raise HTTPException(status_code=400, detail=ExpressionErrors.EVALUATE_FAILED) from e
    return ApiResponse(data={"expression": req.expression, "result": result})


@router.post("/evaluate-batch", response_model=ApiResponse)
async def evaluate_batch(
    req: ExpressionBatchRequest,
    user: dict[str, str] = Depends(
        require_permission(Permission.CONFIG_EDIT)
    ),  # SEC-FIX: 权限从 SYSTEM_READ 提升为 CONFIG_EDIT（viewer 无此权限）
):
    _validate_batch_expression_lengths(req.expressions)
    engine = _get_engine()
    try:
        # FIXED(P3): engine.evaluate_batch 是同步 CPU 操作，包 asyncio.to_thread 避免阻塞事件循环
        results = await asyncio.to_thread(engine.evaluate_batch, req.expressions, req.variables or {})
    except Exception as e:
        # FIXED: 原问题-中文硬编码detail，改为error_code
        raise HTTPException(status_code=400, detail=ExpressionErrors.BATCH_EVALUATE_FAILED) from e
    return ApiResponse(data={"results": results})


@router.post("/validate", response_model=ApiResponse)
async def validate_expression(
    req: ExpressionTestRequest,
    user: dict[str, str] = Depends(
        require_permission(Permission.CONFIG_EDIT)
    ),  # SEC-FIX: 权限从 SYSTEM_READ 提升为 CONFIG_EDIT（viewer 无此权限）
):
    engine = _get_engine()
    try:
        engine._validate_expression(engine._resolve_variables(req.expression, req.variables or {}))
        return ApiResponse(data={"valid": True, "expression": req.expression})
    except ValueError:
        return ApiResponse(
            data={"valid": False, "expression": req.expression, "error": ExpressionErrors.VALIDATE_FAILED},
            error_code=ExpressionErrors.VALIDATE_FAILED,
        )  # FIXED-P1: 不暴露异常详情
    except Exception:
        return ApiResponse(
            data={"valid": False, "expression": req.expression, "error": ExpressionErrors.VALIDATE_FAILED},
            error_code=ExpressionErrors.VALIDATE_FAILED,
        )  # FIXED-P1: 不暴露异常详情


@router.get("/functions", response_model=ApiResponse)
async def list_available_functions(
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    functions = [
        {"name": "abs", "description": "Absolute value", "example": "abs(${device.temp})"},  # FIXED-P3: 中文硬编码→英文
        {"name": "round", "description": "Round to N decimals", "example": "round(${device.temp}, 2)"},
        {
            "name": "min",
            "description": "Minimum value",
            "example": "min(${device.temp1}, ${device.temp2})",
        },
        {
            "name": "max",
            "description": "Maximum value",
            "example": "max(${device.temp1}, ${device.temp2})",
        },
        {"name": "pow", "description": "Power", "example": "pow(${device.voltage}, 2)"},
        {"name": "sqrt", "description": "Square root", "example": "sqrt(${device.power})"},
        {"name": "int", "description": "Convert to integer", "example": "int(${device.value})"},
        {"name": "float", "description": "Convert to float", "example": "float(${device.value})"},
        {"name": "ceil", "description": "Ceiling", "example": "ceil(${device.temp})"},
        {"name": "floor", "description": "Floor", "example": "floor(${device.temp})"},
        {"name": "log", "description": "Natural logarithm", "example": "log(${device.value})"},
        {"name": "log10", "description": "Base-10 logarithm", "example": "log10(${device.value})"},
    ]
    try:
        operators = [
            {"symbol": "+", "description": "Addition"},
            {"symbol": "-", "description": "Subtraction"},
            {"symbol": "*", "description": "Multiplication"},
            {"symbol": "/", "description": "Division"},
            {"symbol": "%", "description": "Modulo"},
            {"symbol": "**", "description": "Exponentiation"},
            {"symbol": "==", "description": "Equal"},
            {"symbol": "!=", "description": "Not equal"},
            {"symbol": ">", "description": "Greater than"},
            {"symbol": "<", "description": "Less than"},
            {"symbol": ">=", "description": "Greater or equal"},
            {"symbol": "<=", "description": "Less or equal"},
            {"symbol": "and", "description": "Logical AND"},
            {"symbol": "or", "description": "Logical OR"},
            {"symbol": "not", "description": "Logical NOT"},
        ]
        return ApiResponse(data={"functions": functions, "operators": operators})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("list_available_functions failed: %s", e)
        # FIXED: 原问题-中文硬编码detail，改为error_code
        raise HTTPException(status_code=500, detail=ExpressionErrors.EVALUATE_FAILED) from e
