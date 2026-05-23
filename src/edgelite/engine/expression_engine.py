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

import ast
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

_DANGEROUS_NAMES = frozenset(
    {
        "exec",
        "eval",
        "compile",
        "open",
        "input",
        "__import__",
        "globals",
        "locals",
        "vars",
        "dir",
        "getattr",
        "setattr",
        "delattr",
        "hasattr",
        "type",
        "object",
        "__builtins__",
        "__name__",
        "__file__",
        "__class__",
        "__mro__",
        "__subclasses__",
        "__bases__",
        "__dict__",
        "__self__",
    }
)

_ALLOWED_CALL_NAMES = frozenset(
    {
        "abs",
        "round",
        "min",
        "max",
        "pow",
        "int",
        "float",
        "str",
        "bool",
        "sqrt",
        "ceil",
        "floor",
        "log",
        "log10",
    }
)

_SAFE_AST_NODES = {
    ast.Expression,
    ast.Constant,
    ast.Name,
    ast.Load,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.Call,
    ast.IfExp,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.Mod,
    ast.Pow,
    ast.USub,
    ast.UAdd,
    ast.Not,
    ast.And,
    ast.Or,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.Is,
    ast.IsNot,
    ast.In,
    ast.NotIn,
    ast.List,
    ast.Tuple,
    ast.Subscript,  # FIXED-P0: 缺少Subscript节点，列表/字典下标访问被拒绝
    ast.Index,  # FIXED-P0: 缺少Index节点(Python 3.8兼容)
}


class SafeExpressionVisitor(ast.NodeVisitor):
    """AST安全检查访问器"""

    def generic_visit(self, node: ast.AST) -> None:
        if type(node) not in _SAFE_AST_NODES and not isinstance(node, ast.Mod):
            raise ValueError(f"表达式包含不允许的语法: {type(node).__name__}")
        super().generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in _DANGEROUS_NAMES:
            raise ValueError(f"表达式包含危险标识符: {node.id}")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name):
            if node.func.id not in _ALLOWED_CALL_NAMES:
                raise ValueError(f"表达式包含不允许的函数调用: {node.func.id}")
        elif isinstance(node.func, ast.Attribute):
            if node.func.attr in _DANGEROUS_NAMES or node.func.attr.startswith("_"):
                raise ValueError(f"表达式包含不允许的属性访问: {node.func.attr}")
        else:
            raise ValueError("表达式包含不允许的调用方式")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("_") or node.attr in _DANGEROUS_NAMES:
            raise ValueError(f"表达式包含不允许的属性: {node.attr}")
        self.generic_visit(node)


class ExpressionEngine:
    """计算表达式引擎"""

    VARIABLE_PATTERN = re.compile(r"\$\{([^}]+)\}")

    def __init__(self):
        self._custom_functions: dict[str, Any] = {}

    def register_function(self, name: str, func: Any) -> None:
        if name in _DANGEROUS_NAMES:
            raise ValueError(f"不能注册危险名称的函数: {name}")
        self._custom_functions[name] = func

    def evaluate(self, expression: str, variables: dict[str, Any] | None = None) -> Any:
        if not expression or not expression.strip():
            return None

        try:
            resolved = self._resolve_variables(expression, variables or {})
            tree = ast.parse(resolved, mode="eval")
            SafeExpressionVisitor().visit(tree)
            namespace = {**_SAFE_BUILTINS, **self._custom_functions}
            code = compile(tree, "<expression>", "eval")
            result = eval(code, {"__builtins__": {}}, namespace)
            return result
        except ValueError:
            raise
        except Exception as e:
            logger.warning("表达式计算失败 '%s': %s", expression, e)
            return None

    def evaluate_batch(
        self, expressions: dict[str, str], variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        results = {}
        for name, expr in expressions.items():
            results[name] = self.evaluate(expr, variables)
        return results

    def _validate_expression(self, expression: str) -> None:
        """验证表达式安全性（供API调用）"""
        if not expression or not expression.strip():
            return
        tree = ast.parse(expression, mode="eval")
        SafeExpressionVisitor().visit(tree)

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
                return match.group(0)  # FIXED-P2: None值保留原始${...}变量引用，让表达式解析时因未定义变量而明确报错，而非转为字符串"None"参与隐式比较
            if isinstance(value, bool):  # FIXED-P2: bool是int子类，必须先于int判断，否则bool值走int分支被转为"True"/"False"
                return "1" if value else "0"
            if isinstance(value, (int, float)):
                return str(value)
            if isinstance(value, str):
                return repr(value)
            return str(value)

        return self.VARIABLE_PATTERN.sub(replacer, expression)

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
