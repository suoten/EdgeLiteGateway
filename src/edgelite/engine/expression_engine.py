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
import concurrent.futures
import logging
import math
import re
import threading
import types
from typing import Any

from edgelite.constants import _EXPRESSION_EVAL_MAX_WORKERS, _EXPRESSION_EVAL_TIMEOUT

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
        # FIXED-P1: 统一危险名称集合，包含所有可被间接调用绕过沙箱的内部属性
        "__class__",
        "__bases__",
        "__subclasses__",
        "__mro__",
        "__self__",
        "__globals__",
        "__code__",
        "__func__",
        "__dict__",
        "__closure__",
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


def _sanitize_namespace(namespace: dict[str, Any]) -> dict[str, Any]:
    """FIXED(低危): 清理 eval namespace 中的危险键，作为 AST visitor 的纵深防御。

    原问题-eval namespace 中的对象仍可通过 __class__/__bases__/__subclasses__
    等 dunder 属性进行沙箱逃逸（理论风险）。
    修复-移除 namespace 中所有属于 _DANGEROUS_NAMES 的键，并显式设置 __builtins__ 为空 dict，
    确保即使 AST visitor 被绕过，namespace 层也无危险入口。
    注意：Python 内置对象的 __class__ 等属性无法被修改（不可变），
    但 AST visitor 已拦截所有以 _ 开头的属性访问，此处仅清理 namespace 键名。
    """
    sanitized: dict[str, Any] = {}
    for key, value in namespace.items():
        if key in _DANGEROUS_NAMES:
            logger.warning("Removed dangerous key from eval namespace: %s", key)
            continue
        if isinstance(key, str) and key.startswith("__") and key.endswith("__"):
            logger.warning("Removed dunder key from eval namespace: %s", key)
            continue
        sanitized[key] = value
    # 显式确保 __builtins__ 为空，防止通过 namespace 注入内置函数
    sanitized["__builtins__"] = {}
    return sanitized


class SafeExpressionVisitor(ast.NodeVisitor):
    """AST安全检查访问器"""

    def __init__(self, allowed_call_names: frozenset | None = None):
        self._allowed_call_names = allowed_call_names or _ALLOWED_CALL_NAMES

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
            if node.func.id not in self._allowed_call_names:
                raise ValueError(f"表达式包含不允许的函数调用: {node.func.id}")
        elif isinstance(node.func, ast.Attribute):
            if node.func.attr in _DANGEROUS_NAMES or node.func.attr.startswith("_"):
                raise ValueError(f"表达式包含不允许的属性访问: {node.func.attr}")
        else:
            raise ValueError("表达式包含不允许的调用方式")
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:  # FIXED-P2: Subscript允许dict/list下标访问，需校验下标值不含__dunder__以防止属性链逃逸
        if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
            if node.slice.value.startswith("__") and node.slice.value.endswith("__"):
                raise ValueError(f"表达式包含危险的字典键访问: {node.slice.value}")
        self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        # R6-S-20: 限制幂运算指数上限，防止 10**100000000 等表达式导致整数爆炸 DoS
        if isinstance(node.op, ast.Pow):
            right = node.right
            if isinstance(right, ast.Constant) and isinstance(right.value, int) and right.value > 1000:
                raise ValueError(f"幂运算指数上限为 1000，当前指数 {right.value} 超限，可能导致 DoS")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("_") or node.attr in _DANGEROUS_NAMES:
            raise ValueError(f"表达式包含不允许的属性: {node.attr}")
        self.generic_visit(node)


class ExpressionEngine:
    """计算表达式引擎"""

    VARIABLE_PATTERN = re.compile(r"\$\{([^}]+)\}")

    def __init__(self, max_workers: int | None = None, eval_timeout: float | None = None):
        self._custom_functions: dict[str, Any] = {}
        self._max_workers = max_workers if max_workers is not None else _EXPRESSION_EVAL_MAX_WORKERS
        self._eval_timeout = eval_timeout if eval_timeout is not None else _EXPRESSION_EVAL_TIMEOUT
        self._pool_lock = threading.Lock()
        self._eval_pool = concurrent.futures.ThreadPoolExecutor(max_workers=self._max_workers, thread_name_prefix="expr_eval")  # FIXED-P2: 复用线程池避免孤儿线程

    def register_function(self, name: str, func: Any) -> None:
        if name in _DANGEROUS_NAMES:
            raise ValueError(f"不能注册危险名称的函数: {name}")
        if name.startswith("__") and name.endswith("__"):  # FIXED-P2: 禁止注册__dunder__名称函数，防止绕过沙箱访问Python内部属性
            raise ValueError(f"不能注册dunder名称的函数: {name}")
        _DANGEROUS_CODE_NAMES = frozenset({
            "__import__", "__builtins__", "eval", "exec", "open", "os", "sys",
            "subprocess", "shutil", "pathlib", "socket", "ctypes", "signal", "io",
            # FIXED-P1: 扩展危险代码名称集合，增加socket/ctypes/signal/io
            # R6-S-02修复: 补充内省和动态执行相关危险函数，防止沙箱逃逸
            "getattr", "setattr", "delattr", "type", "globals", "locals", "vars",
            "dir", "compile", "memoryview", "breakpoint", "help", "input",
        })
        if callable(func):
            try:
                code_obj = getattr(func, "__code__", None)
                if code_obj is None:
                    raise ValueError(
                        f"不能注册无 __code__ 属性的可调用对象: {name} "
                        f"(type={type(func).__name__})，仅允许纯Python函数"
                    )
                checked_names = set(getattr(code_obj, "co_names", ())) | set(getattr(code_obj, "co_freevars", ()))
                for dangerous in _DANGEROUS_CODE_NAMES:
                    if dangerous in checked_names:
                        raise ValueError(f"不能注册包含危险引用的函数: {name} (引用了 {dangerous})")  # FIXED-P0: register_function添加函数安全检查
                # FIXED-P1: 检查函数闭包中引用的对象是否包含危险模块
                # co_freevars仅包含变量名，不包含实际引用的对象类型
                # 通过检查func.__closure__中的cell_contents进一步验证
                closure = getattr(func, "__closure__", None)
                if closure:
                    for cell in closure:
                        try:
                            cell_val = cell.cell_contents
                        except ValueError:
                            # cell_contents may raise ValueError if cell is empty
                            continue
                        # FIXED-P0: 原问题-危险模块检测抛出的 ValueError 被
                        # except ValueError: pass 吞掉，安全检查形同虚设
                        # 修复：分离 cell_contents 读取与危险模块检测
                        cell_type_name = type(cell_val).__module__
                        if cell_type_name in ("os", "sys", "subprocess", "socket", "ctypes", "signal", "io", "shutil", "pathlib"):
                            raise ValueError(f"不能注册闭包中引用危险模块的函数: {name} (模块 {cell_type_name})")
            except ValueError:
                raise
            except Exception as e:
                raise ValueError(f"自定义函数安全检查失败，拒绝注册: {name} - {e}") from e
        self._custom_functions[name] = func

    def evaluate(self, expression: str, variables: dict[str, Any] | None = None) -> Any:
        if not expression or not expression.strip():
            return None

        try:
            resolved = self._resolve_variables(expression, variables or {})
            tree = ast.parse(resolved, mode="eval")
            allowed = _ALLOWED_CALL_NAMES | frozenset(self._custom_functions.keys())
            SafeExpressionVisitor(allowed).visit(tree)
            namespace = {**_SAFE_BUILTINS, **self._custom_functions}
            # FIXED(低危): 原问题-eval namespace 中的对象仍可通过 __class__/__bases__/__subclasses__
            # 等 dunder 属性进行沙箱逃逸（理论风险，AST visitor 已拦截但缺乏纵深防御）;
            # 修复-在 namespace 传入 eval 前调用 _sanitize_namespace 清除危险键，
            # 确保 __builtins__ 保持为空且 namespace 中无危险名称。
            namespace = _sanitize_namespace(namespace)
            code = compile(tree, "<expression>", "eval")
            # 自定义函数可能在 eval 中调用协变函数（如 asyncio.sleep），
            # 因此在线程池中执行 eval 并用 wait_for 限制总时长
            result = self._eval_with_timeout(code, namespace)  # FIXED-P2: 自定义函数无超时保护，恶意/阻塞性自定义函数会导致评估线程永久卡死
            return result
        except ValueError:
            raise
        except Exception as e:
            logger.warning("表达式计算失败 '%s': %s", expression, e)
            return None

    def _eval_with_timeout(self, code: types.CodeType, namespace: dict) -> Any:
        """在线程池中执行 eval 并限制最大执行时间，避免阻塞性自定义函数永久卡死"""

        try:
            with self._pool_lock:
                future = self._eval_pool.submit(eval, code, {"__builtins__": {}}, namespace)
            return future.result(timeout=self._eval_timeout)
        except concurrent.futures.TimeoutError as exc:  # FIXED(P2): 原问题-B904异常链丢失; 修复-添加as exc与from exc
            # FIXED: 超时后调用 future.cancel() 取消 future
            future.cancel()
            # FIXED-P2: 超时后线程无法终止，重建线程池丢弃卡死的线程
            self._rebuild_eval_pool()
            raise ValueError(f"表达式执行超时（超过{self._eval_timeout}秒），可能是阻塞性自定义函数") from exc

    def _rebuild_eval_pool(self) -> None:
        """FIXED-P2: 重建线程池，丢弃卡死的线程"""
        with self._pool_lock:
            try:
                self._eval_pool.shutdown(wait=False)
            # FIXED-P1: 原问题-except Exception: pass 静默吞没异常，改为至少 logger.debug
            except Exception as e:
                logger.debug("[expression_engine] eval_pool shutdown (rebuild) failed: %s", e)
            self._eval_pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=self._max_workers, thread_name_prefix="expr_eval"
            )

    def close(self) -> None:
        """FIXED-P2: 资源清理方法，关闭线程池"""
        try:
            self._eval_pool.shutdown(wait=True)
        # FIXED-P1: 原问题-except Exception: pass 静默吞没异常，改为至少 logger.debug
        except Exception as e:
            logger.debug("[expression_engine] eval_pool shutdown (close) failed: %s", e)

    def __del__(self):
        """FIXED-P2: 析构时关闭线程池，防止线程泄漏"""
        try:
            self._eval_pool.shutdown(wait=False)
        # FIXED-P1: 原问题-except Exception: pass 静默吞没异常，改为至少 logger.debug
        except Exception as e:
            logger.debug("[expression_engine] eval_pool shutdown (__del__) failed: %s", e)

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
        missing_vars: list[str] = []
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
                missing_vars.append(var_path)
                return "None"  # FIXED-P2: 未定义变量记录后抛出 ValueError，避免静默返回 None
            if isinstance(value, bool):  # FIXED-P2: bool是int子类，必须先于int判断，否则bool值走int分支被转为"True"/"False"
                return "1" if value else "0"
            if isinstance(value, (int, float)):
                return str(value)
            if isinstance(value, str):
                return repr(value)
            return str(value)

        result = self.VARIABLE_PATTERN.sub(replacer, expression)
        if missing_vars:
            raise ValueError(f"表达式引用了未定义的变量: {', '.join(missing_vars)}")
        return result

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
