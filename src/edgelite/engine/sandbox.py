"""安全脚本沙箱 — 用于执行用户自定义规则脚本

提供两个公共接口:
  - _validate_script_ast(script: str) -> bool: AST 静态分析，拦截危险代码
  - run_script_safely(script, namespace, timeout, filename) -> Any: 在受限线程中执行脚本

安全措施:
  1. AST 静态分析: 拦截 import/exec/eval/compile/dunder 属性访问/全局内建访问
  2. resource.setrlimit: 限制 CPU 时间(2s) 和内存(256MB)
  3. asyncio.wait_for: 超时强制取消
  4. 线程隔离: 脚本在独立线程中执行，不阻塞事件循环
  5. 受控命名空间: 仅暴露 point_values 和 result，移除 __builtins__ 危险函数
"""

from __future__ import annotations

import ast
import asyncio
import logging
import sys
import traceback
from types import CodeType
from typing import Any

try:
    import resource  # Unix-only
except ImportError:
    resource = None  # Windows 无此模块

logger = logging.getLogger(__name__)

# ── 危险 AST 节点类型 ──────────────────────────────────────────────────
_DANGEROUS_ATTRS: frozenset[str] = frozenset({
    "__import__", "__builtins__", "__subclasses__", "__mro__", "__bases__",
    "__class__", "__globals__", "__code__", "__func__", "__self__",
    "__dict__", "__module__", "gi_frame", "gi_code", "cr_frame", "cr_code",
    "f_locals", "f_globals", "f_builtins", "f_code",
})

_DANGEROUS_NAMES: frozenset[str] = frozenset({
    "eval", "exec", "compile", "globals", "locals", "vars",
    "dir", "type", "getattr", "setattr", "delattr", "hasattr",
    "input", "breakpoint", "exit", "quit",
    "__import__", "open", "memoryview",
})

_ALLOWED_BUILTINS: dict[str, Any] = {
    "abs": abs, "min": min, "max": max, "sum": sum,
    "round": round, "len": len, "range": range,
    "int": int, "float": float, "str": str, "bool": bool,
    "list": list, "dict": dict, "tuple": tuple, "set": set,
    "sorted": sorted, "reversed": reversed, "enumerate": enumerate,
    "zip": zip, "map": map, "filter": filter, "any": any, "all": all,
    "True": True, "False": False, "None": None,
    "print": lambda *a, **k: None,  # no-op print
}


class _ScriptValidator(ast.NodeVisitor):
    """AST 遍历器：检测危险节点"""

    def __init__(self) -> None:
        self._dangerous: list[str] = []

    @property
    def is_safe(self) -> bool:
        return not self._dangerous

    @property
    def reasons(self) -> list[str]:
        return list(self._dangerous)

    def visit_Import(self, node: ast.Import) -> Any:
        self._dangerous.append(f"import statement at line {node.lineno}")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> Any:
        self._dangerous.append(f"from-import at line {node.lineno}")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> Any:
        if node.attr in _DANGEROUS_ATTRS:
            self._dangerous.append(
                f"dangerous attribute '{node.attr}' at line {node.lineno}"
            )
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> Any:
        if node.id in _DANGEROUS_NAMES:
            self._dangerous.append(
                f"dangerous name '{node.id}' at line {node.lineno}"
            )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> Any:
        func = node.func
        if isinstance(func, ast.Name) and func.id == "exec":
            self._dangerous.append(f"exec() call at line {node.lineno}")
        elif isinstance(func, ast.Name) and func.id == "eval":
            self._dangerous.append(f"eval() call at line {node.lineno}")
        elif isinstance(func, ast.Name) and func.id == "compile":
            self._dangerous.append(f"compile() call at line {node.lineno}")
        elif isinstance(func, ast.Name) and func.id == "open":
            self._dangerous.append(f"open() call at line {node.lineno}")
        self.generic_visit(node)


def _validate_script_ast(script: str) -> bool:
    """AST 静态分析：检测脚本中是否包含危险代码

    Returns:
        True  — 脚本安全，可执行
        False — 脚本包含危险代码，拒绝执行
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


def _set_resource_limits() -> None:
    """设置线程级资源限制（仅 Unix 可用）"""
    if sys.platform == "win32":
        return
    try:
        # CPU 时间限制 2 秒（软/硬）
        resource.setrlimit(resource.RLIMIT_CPU, (2, 2))
        # 内存限制 256MB
        mem_limit = 256 * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem_limit, mem_limit))
        # 文件大小限制 0（禁止写文件）
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
        # 进程数限制 0（禁止 fork）
        if hasattr(resource, "RLIMIT_NPROC"):
            resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
    except (ValueError, OSError, AttributeError) as e:
        logger.debug("sandbox: resource limit set failed: %s", e)


def _execute_script_sync(script: str, namespace: dict[str, Any], filename: str) -> Any:
    """同步执行脚本（在 to_thread 中调用）

    在执行前设置资源限制，使用受限 __builtins__，
    执行后返回 namespace["result"]。
    """
    _set_resource_limits()

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
