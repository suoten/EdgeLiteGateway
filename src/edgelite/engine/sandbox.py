"""安全脚本沙箱 — 用于执行用户自定义规则脚本

提供两个公共接口:
  - _validate_script_ast(script: str) -> bool: AST 静态分析，拦截危险代码
  - run_script_safely(script, namespace, timeout, filename) -> Any: 在受限线程中执行脚本

安全措施:
  1. AST 静态分析: 白名单模式 — 仅放行常见语句/表达式节点，其余一律拒绝；
     Attribute/方法调用拒绝 dunder 属性与 format/format_map（参考 expression_engine
     的白名单范式，修复黑名单可被 "{0.__class__}".format(()) 等手段绕过的问题）
  2. asyncio.wait_for: 超时强制取消
  3. 线程隔离: 脚本在独立线程中执行，不阻塞事件循环
  4. 受控命名空间: 仅暴露 point_values 和 result，移除 __builtins__ 危险函数

关于 resource.setrlimit（FIXED-CI-137）:
  早期实现曾在线程内对**整个进程**施加 RLIMIT_CPU(2s)/AS(256MB)/FSIZE(0) 等限制
  （POSIX rlimit 是进程属性而非线程属性）。对本进程这是自杀式的：宿主进程运行中
  累计 CPU 早已超过 2s、地址空间早已超过 256MB，设置瞬间即触发 SIGXCPU/SIGKILL
  （CPU 超硬限内核直接 SIGKILL）或令所有线程的分配/文件写入失败——CI 的 pytest
  正是在此被杀（exit 137）。因此**有意不使用进程内 rlimit**；如需硬性资源上限，
  未来应改为 fork/spawn 子进程隔离，在子进程内施加 rlimit。
"""

from __future__ import annotations

import ast
import asyncio
import logging
import traceback
from types import CodeType
from typing import Any

logger = logging.getLogger(__name__)

# ── AST 白名单 ──────────────────────────────────────────────────────
# 白名单模式: 仅允许常见语句/表达式节点，未列出的节点类型一律拒绝。
# 参考 expression_engine.SafeExpressionVisitor 的既有白名单范式。

# 语句节点
_ALLOWED_STATEMENT_NODES: tuple[type, ...] = (
    ast.Module,
    ast.Expr,
    ast.Assign,
    ast.AugAssign,
    ast.AnnAssign,
    ast.If,
    ast.For,
    ast.While,
    ast.Break,
    ast.Continue,
    ast.Pass,
    ast.FunctionDef,
    ast.Return,
    ast.Assert,
    ast.Delete,
    ast.Try,
    ast.ExceptHandler,
    ast.Raise,
)

# 表达式节点
_ALLOWED_EXPR_NODES: tuple[type, ...] = (
    ast.Expression,
    ast.Constant,
    ast.Name,
    ast.Load,
    ast.Store,
    ast.Del,
    ast.Attribute,
    ast.Subscript,
    ast.Slice,
    ast.Index,  # Python 3.8 兼容
    ast.Call,
    ast.List,
    ast.Tuple,
    ast.Set,
    ast.Dict,
    ast.BinOp,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.LShift,
    ast.RShift,
    ast.BitOr,
    ast.BitXor,
    ast.BitAnd,
    ast.MatMult,
    ast.UnaryOp,
    ast.Not,
    ast.USub,
    ast.UAdd,
    ast.Invert,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.Compare,
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
    ast.IfExp,
    ast.Starred,
    ast.keyword,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
    ast.comprehension,
    ast.Lambda,
    ast.arguments,
    ast.arg,
    ast.JoinedStr,
    ast.FormattedValue,
    ast.NamedExpr,
)

_ALLOWED_NODES: frozenset[type] = frozenset(_ALLOWED_STATEMENT_NODES + _ALLOWED_EXPR_NODES)

# 纵深防御: 即使未来内置函数被误注入 namespace，仍按名称拦截高危调用
_DANGEROUS_NAMES: frozenset[str] = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "globals",
        "locals",
        "vars",
        "dir",
        "type",
        "getattr",
        "setattr",
        "delattr",
        "hasattr",
        "input",
        "breakpoint",
        "exit",
        "quit",
        "__import__",
        "open",
        "memoryview",
    }
)

# Attribute/方法调用禁止名单: dunder（首尾都是下划线）之外再显式禁止
# str.format/format_map — 沙箱逃逸的常见入口
_FORBIDDEN_ATTR_NAMES: frozenset[str] = frozenset({"format", "format_map"})

# 幂运算指数上限，防止 10**100000000 等整数爆炸 DoS（与 expression_engine 一致）
_MAX_POW_EXPONENT = 1000


def _is_dunder(name: str) -> bool:
    """判断名称是否为 dunder（首尾都是下划线，长度>=2，单下划线 `_` 除外）"""
    return len(name) >= 2 and name.startswith("_") and name.endswith("_")


def _is_forbidden_attr(attr: str) -> bool:
    """Attribute 属性名是否禁止访问: dunder 与 format/format_map"""
    return attr in _FORBIDDEN_ATTR_NAMES or _is_dunder(attr)


_ALLOWED_BUILTINS: dict[str, Any] = {
    "abs": abs,
    "min": min,
    "max": max,
    "sum": sum,
    "round": round,
    "len": len,
    "range": range,
    "int": int,
    "float": float,
    "str": str,
    "bool": bool,
    "list": list,
    "dict": dict,
    "tuple": tuple,
    "set": set,
    "sorted": sorted,
    "reversed": reversed,
    "enumerate": enumerate,
    "zip": zip,
    "map": map,
    "filter": filter,
    "any": any,
    "all": all,
    "True": True,
    "False": False,
    "None": None,
    "print": lambda *a, **k: None,  # no-op print
}


class _ScriptValidator(ast.NodeVisitor):
    """AST 遍历器：白名单模式，未列出的节点类型一律拒绝"""

    def __init__(self) -> None:
        self._dangerous: list[str] = []

    @property
    def is_safe(self) -> bool:
        return not self._dangerous

    @property
    def reasons(self) -> list[str]:
        return list(self._dangerous)

    def generic_visit(self, node: ast.AST) -> Any:
        if type(node) not in _ALLOWED_NODES:
            lineno = getattr(node, "lineno", 0)
            self._dangerous.append(f"disallowed syntax '{type(node).__name__}' at line {lineno}")
            return  # 白名单拒绝后不再深入遍历
        return super().generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> Any:
        if _is_forbidden_attr(node.attr):
            self._dangerous.append(f"forbidden attribute '{node.attr}' at line {node.lineno}")
            return
        return self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> Any:
        func = node.func
        if isinstance(func, ast.Attribute) and _is_forbidden_attr(func.attr):
            self._dangerous.append(f"forbidden method call '{func.attr}()' at line {node.lineno}")
            return
        if isinstance(func, ast.Name) and func.id in _DANGEROUS_NAMES:
            self._dangerous.append(f"dangerous call '{func.id}()' at line {node.lineno}")
            return
        return self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> Any:
        # 纵深防御: 拦截 __builtins__ 等 dunder 名称引用（合法脚本不需要）
        if node.id in _DANGEROUS_NAMES or _is_dunder(node.id):
            self._dangerous.append(f"forbidden name '{node.id}' at line {node.lineno}")
            return
        return self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> Any:
        # 纵深防御: 拒绝以 dunder 字符串字面量作下标（与 expression_engine 一致）
        sl = node.slice
        key = getattr(sl, "value", sl)
        if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
            key = sl.value
        if isinstance(key, str) and _is_dunder(key):
            self._dangerous.append(f"forbidden subscript key '{key}' at line {node.lineno}")
            return
        return self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp) -> Any:
        # 与 expression_engine 一致: 限制幂运算指数，防止整数爆炸 DoS
        if isinstance(node.op, ast.Pow):
            right = node.right
            if (
                isinstance(right, ast.Constant)
                and isinstance(right.value, int)
                and not isinstance(right.value, bool)
                and abs(right.value) > _MAX_POW_EXPONENT
            ):
                self._dangerous.append(
                    f"pow exponent {right.value} exceeds limit {_MAX_POW_EXPONENT} at line {node.lineno}"
                )
                return
        return self.generic_visit(node)


def _validate_script_ast(script: str) -> bool:
    """AST 静态分析：白名单模式，仅放行安全的常见语法

    Returns:
        True  — 脚本安全，可执行
        False — 脚本包含不允许/危险的语法，拒绝执行
    """
    try:
        tree = ast.parse(script, mode="exec")
    except SyntaxError as e:
        logger.warning("sandbox: script syntax error: %s", e)
        return False

    validator = _ScriptValidator()
    validator.visit(tree)
    if not validator.is_safe:
        for reason in validator.reasons:
            logger.warning("sandbox: AST check failed: %s", reason)
        return False
    return True


def _execute_script_sync(script: str, namespace: dict[str, Any], filename: str) -> Any:
    """同步执行脚本（在 to_thread 中调用）

    使用受限 __builtins__ 执行（FIXED-CI-137: 不再施加进程级 rlimit，见模块 docstring），
    执行后返回 namespace["result"]。
    """
    # 构建受限命名空间
    safe_builtins = dict(_ALLOWED_BUILTINS)
    safe_namespace: dict[str, Any] = {
        "__builtins__": safe_builtins,
        **namespace,
    }

    code: CodeType
    try:
        code = compile(script, filename, "exec")
    except SyntaxError as e:
        logger.warning("sandbox: compile failed: %s", e)
        namespace["result"] = False
        return namespace.get("result", False)

    try:
        exec(code, safe_namespace)  # noqa: S102 — 受控沙箱执行
    except Exception as e:
        logger.warning("sandbox: script execution error: %s\n%s", e, traceback.format_exc())
        safe_namespace["result"] = False

    # 将可能被脚本修改的 namespace 键回写
    for key in namespace:
        if key in safe_namespace:
            namespace[key] = safe_namespace[key]

    return namespace.get("result", False)


async def run_script_safely(
    script: str,
    namespace: dict[str, Any],
    timeout: float = 3.0,
    filename: str = "<rule_script>",
) -> Any:
    """在安全沙箱中异步执行脚本

    Args:
        script:   要执行的 Python 源码
        namespace: 初始命名空间（必须包含 "result" 键）
        timeout:  超时秒数（超时后强制取消）
        filename: 编译时的文件名（用于错误追踪）

    Returns:
        namespace["result"] 的值

    Raises:
        TimeoutError: 脚本执行超时
        ValueError: 脚本包含危险代码（AST 检查未通过）
    """
    if not script or not script.strip():
        return namespace.get("result", False)

    # AST 静态分析
    if not _validate_script_ast(script):
        raise ValueError("script blocked by AST safety check")

    # 在线程中执行，施加超时
    result = await asyncio.wait_for(
        asyncio.to_thread(_execute_script_sync, script, namespace, filename),
        timeout=timeout,
    )
    return result
