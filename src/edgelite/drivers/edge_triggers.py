"""边缘触发器执行器 + 安全表达式求值。

``EdgeTriggerExecutor`` 接收规则引擎产生的告警记录，按规则 actions 列表执行
设备写入 / MQTT 发布等动作；``_safe_eval_expr`` 提供 AST 白名单求值，
用于 simulator 的公式配置（避免 ``eval`` 的属性链逃逸风险）。
"""

from __future__ import annotations

import ast
import logging
import math
import operator as _op
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from edgelite.drivers.edge_rule_engine import AlarmRecord

logger = logging.getLogger(__name__)

# 允许的二元/一元运算符
_BIN_OPS: dict[type, Any] = {
    ast.Add: _op.add,
    ast.Sub: _op.sub,
    ast.Mult: _op.mul,
    ast.Div: _op.truediv,
    ast.FloorDiv: _op.floordiv,
    ast.Mod: _op.mod,
    ast.Pow: _op.pow,
}
_UNARY_OPS: dict[type, Any] = {
    ast.UAdd: _op.pos,
    ast.USub: _op.neg,
}
_CMP_OPS: dict[type, Any] = {
    ast.Gt: _op.gt,
    ast.GtE: _op.ge,
    ast.Lt: _op.lt,
    ast.LtE: _op.le,
    ast.Eq: _op.eq,
    ast.NotEq: _op.ne,
}
# 允许的函数（白名单）
_SAFE_FUNCS: dict[str, Any] = {
    "abs": abs,
    "min": min,
    "max": max,
    "round": round,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "sqrt": math.sqrt,
    "log": math.log,
    "log10": math.log10,
    "exp": math.exp,
    "floor": math.floor,
    "ceil": math.ceil,
    "pow": pow,
}
# 允许的常量
_SAFE_CONSTS: dict[str, Any] = {
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
    "inf": math.inf,
}


def _safe_eval_expr(expr: str, context: dict | None = None) -> Any:
    """AST 白名单方式安全求值表达式。

    Args:
        expr: 表达式字符串（如 ``"t"``, ``"min + (max-min)*sin(t)"``）
        context: 变量名到值的映射

    Returns:
        求值结果

    Raises:
        ValueError: 表达式包含禁止的节点类型或未知名称
    """
    if not expr or not isinstance(expr, str):
        raise ValueError(f"invalid expression: {expr!r}")
    context = context or {}
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise ValueError(f"expression syntax error: {e}") from e
    return _eval_node(tree.body, context)


def _eval_node(node: ast.AST, context: dict) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in context:
            return context[node.id]
        if node.id in _SAFE_CONSTS:
            return _SAFE_CONSTS[node.id]
        if node.id in _SAFE_FUNCS:
            return _SAFE_FUNCS[node.id]
        raise ValueError(f"unknown name: {node.id}")
    if isinstance(node, ast.BinOp):
        func = _BIN_OPS.get(type(node.op))
        if func is None:
            raise ValueError(f"forbidden binop: {type(node.op).__name__}")
        return func(_eval_node(node.left, context), _eval_node(node.right, context))
    if isinstance(node, ast.UnaryOp):
        func = _UNARY_OPS.get(type(node.op))
        if func is None:
            raise ValueError(f"forbidden unaryop: {type(node.op).__name__}")
        return func(_eval_node(node.operand, context))
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            result = True
            for v in node.values:
                result = _eval_node(v, context)
                if not result:
                    return result
            return result
        if isinstance(node.op, ast.Or):
            for v in node.values:
                result = _eval_node(v, context)
                if result:
                    return result
            return False
        raise ValueError(f"forbidden boolop: {type(node.op).__name__}")
    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, context)
        for op, comp in zip(node.ops, node.comparators, strict=False):
            func = _CMP_OPS.get(type(op))
            if func is None:
                raise ValueError(f"forbidden compare op: {type(op).__name__}")
            right = _eval_node(comp, context)
            if not func(left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.IfExp):
        return _eval_node(node.body, context) if _eval_node(node.test, context) else _eval_node(node.orelse, context)
    if isinstance(node, ast.Call):
        func = _eval_node(node.func, context)
        if not callable(func):
            raise ValueError("call target is not callable")
        args = [_eval_node(a, context) for a in node.args]
        kwargs = {kw.arg: _eval_node(kw.value, context) for kw in node.keywords if kw.arg}
        return func(*args, **kwargs)
    raise ValueError(f"forbidden node: {type(node).__name__}")


class EdgeTriggerExecutor:
    """边缘触发器执行器

    规则命中时由规则引擎回调 ``execute``，遍历规则的 actions 列表执行相应动作。
    支持的 action 类型:
    - ``{"type": "set_point", "device_id": ..., "point": ..., "value": ...}`` 调用设备写入
    - ``{"type": "mqtt_publish", "topic": ..., "payload": ...}`` 调用 MQTT 发布

    Args:
        device_write_callback: ``async (device_id, point, value) -> dict|bool``
        mqtt_publish_callback: 可选，``async (topic, payload) -> None``
    """

    def __init__(
        self,
        device_write_callback: Callable[..., Awaitable[Any]] | None = None,
        mqtt_publish_callback: Callable[..., Awaitable[Any]] | None = None,
    ) -> None:
        self._device_write_callback = device_write_callback
        self._mqtt_publish_callback = mqtt_publish_callback
        self._stats = {"executed": 0, "writes": 0, "publishes": 0, "errors": 0}

    async def execute(self, alarm: AlarmRecord) -> None:
        """执行告警关联的动作列表"""
        actions = getattr(alarm, "actions", None) or []
        if not actions:
            return
        self._stats["executed"] += 1
        for action in actions:
            if not isinstance(action, dict):
                continue
            atype = action.get("type")
            try:
                if atype in ("set_point", "write") and self._device_write_callback:
                    await self._device_write_callback(
                        action.get("device_id", alarm.device_id),
                        action.get("point", alarm.point_name),
                        action.get("value"),
                    )
                    self._stats["writes"] += 1
                elif atype == "mqtt_publish" and self._mqtt_publish_callback:
                    await self._mqtt_publish_callback(action.get("topic", ""), action.get("payload", {}))
                    self._stats["publishes"] += 1
            except Exception as e:
                self._stats["errors"] += 1
                logger.error("trigger action %s failed: %s", atype, e)

    async def stop(self) -> None:
        """清理资源（保持协程接口一致）"""
        self._device_write_callback = None
        self._mqtt_publish_callback = None

    def get_stats(self) -> dict:
        return dict(self._stats)


__all__ = ["EdgeTriggerExecutor", "_safe_eval_expr"]
