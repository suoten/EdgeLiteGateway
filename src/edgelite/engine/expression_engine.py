"""计算表达式引擎 - 支持设备变量的表达式计算

支持：
- 算术运算: +, -, *, /, %, **
- 比较运算: ==, !=, >, <, >=, <=
- 逻辑运算: and, or, not
- 数学函数: abs, round, min, max, pow, sqrt
- 类型转换: int, float, str, bool
- 条件表达式: value_if_true if condition else value_if_false
- 变量引用: ${device_id.point_name}
"""

from __future__ import annotations

import logging
import math
import re
from typing import Any

logger = logging.getLogger(__name__)

_SAFE_BUILTINS = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "pow": pow,
    "int": int,
    "float": float,
    "str": str,
    "bool": bool,
    "sqrt": math.sqrt,
    "ceil": math.ceil,
    "floor": math.floor,
    "log": math.log,
    "log10": math.log10,
    "pi": math.pi,
    "e": math.e,
    "True": True,
    "False": False,
    "None": None,
}

_DANGEROUS_NAMES = {
    "exec", "eval", "compile", "open", "input", "__import__",
    "globals", "locals", "vars", "dir", "getattr", "setattr",
    "delattr", "hasattr", "type", "object", "class",
    "__builtins__", "__name__", "__file__",
}


class ExpressionEngine:
    """计算表达式引擎"""

    VARIABLE_PATTERN = re.compile(r"\$\{([^}]+)\}")

    def __init__(self):
        self._custom_functions: dict[str, Any] = {}

    def register_function(self, name: str, func: Any) -> None:
        self._custom_functions[name] = func

    def evaluate(self, expression: str, variables: dict[str, Any] | None = None) -> Any:
        if not expression or not expression.strip():
            return None

        try:
            resolved = self._resolve_variables(expression, variables or {})
            self._validate_expression(resolved)
            namespace = {**_SAFE_BUILTINS, **self._custom_functions}
            result = eval(resolved, {"__builtins__": {}}, namespace)
            return result
        except Exception as e:
            logger.warning("表达式计算失败 '%s': %s", expression, e)
            return None

    def evaluate_batch(self, expressions: dict[str, str], variables: dict[str, Any] | None = None) -> dict[str, Any]:
        results = {}
        for name, expr in expressions.items():
            results[name] = self.evaluate(expr, variables)
        return results

    def _resolve_variables(self, expression: str, variables: dict[str, Any]) -> str:
        def replacer(match: re.Match) -> str:
            var_path = match.group(1)
            parts = var_path.split(".")
            if len(parts) >= 2:
                device_id = parts[0]
                point_name = ".".join(parts[1:])
                key = f"{device_id}.{point_name}"
            else:
                key = var_path

            value = variables.get(key, variables.get(var_path))
            if value is None:
                return "None"
            if isinstance(value, (int, float)):
                return str(value)
            if isinstance(value, bool):
                return str(value)
            if isinstance(value, str):
                return repr(value)
            return str(value)

        return self.VARIABLE_PATTERN.sub(replacer, expression)

    def _validate_expression(self, expression: str) -> None:
        for name in _DANGEROUS_NAMES:
            if name in expression:
                raise ValueError(f"表达式包含危险标识符: {name}")

        for pattern in ["__", "import", "exec", "eval", "open(", "compile("]:
            if pattern in expression:
                raise ValueError(f"表达式包含不允许的模式: {pattern}")

    @staticmethod
    def create_point_expression(
        source_point: str,
        expression: str,
        output_name: str,
    ) -> dict:
        return {
            "source": source_point,
            "expression": expression,
            "output": output_name,
        }
