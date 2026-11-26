"""安全脚本沙箱 — 用于执行用户自定义规则脚本

提供两个公共接口:
  - _validate_script_ast(script: str) -> bool: AST 静态分析，拦截危险代码
  - run_script_safely(script, namespace, timeout, filename) -> Any: 在受限线程中执行脚本

安全措施:
  1. AST 静态分析: 白名单模式 — 仅放行常见语句/表达式节点，其余一律拒绝；
     Attribute/方法调用拒绝 dunder 属性与 format/format_map（参考 expression_engine
     的白名单范式，修复黑名单可被 "{0.__class__}".format(()) 等手段绕过的问题）
  2. 到期打断: 编译前在每个循环体开头注入到期检查，到点后脚本线程内的死循环真正被中断
  3. 线程隔离: 脚本在独立守护线程中执行，不阻塞事件循环；并发数有上限（见 run_script_safely）
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
import builtins
import logging
import threading
import time
import traceback
from types import CodeType
from typing import Any

logger = logging.getLogger(__name__)

# FIXED(P1): 同时运行的脚本线程上限。原实现用 asyncio.to_thread 把脚本放进 asyncio 的**全局默认**
# 线程池，且超时只是"不再等待"——死循环脚本每评估一次就永久占用一个满核线程；默认线程池被
# 占满后，全进程 150+ 处 asyncio.to_thread 调用（以及默认的 DNS 解析）会一起卡死。
# 现在：脚本只在本模块自己的守护线程里运行（不占用默认线程池，也不会阻塞进程退出），
# 并发达到上限时新的执行请求立即以 TimeoutError 失败，而不是无限排队。
_MAX_SCRIPT_WORKERS = 4
_script_slots = threading.BoundedSemaphore(_MAX_SCRIPT_WORKERS)
_SCRIPT_THREAD_NAME = "edgelite-rule-script"


class _ScriptTimeout(BaseException):
    """脚本超出执行时限（由循环体开头注入的到期检查在脚本线程内部抛出）

    继承 BaseException：脚本里的 ``except Exception`` 抓不到它。脚本若用裸 except /
    ``except BaseException`` 捕获了它，下一个循环体的到期检查会再次抛出（见
    _DeadlineCheckInjector），不影响超时生效。
    """


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

# FIXED: 白名单允许 try / except / raise，但上面的受限内建里没有任何异常类——脚本一旦写
# `except ZeroDivisionError:`，Python 在匹配处理器时解析这个名字就抛 NameError，脚本被判定为
# 执行失败（result=False），只有裸 `except:` 能用。这里暴露常见的内置异常类。
# 有意不暴露：SystemExit / KeyboardInterrupt / GeneratorExit（会中断宿主的控制流）、
# OSError（带文件名等上下文属性及大量子类）。
for _exc_name in (
    "ArithmeticError",
    "AssertionError",
    "AttributeError",
    "BaseException",
    "Exception",
    "FloatingPointError",
    "IndexError",
    "KeyError",
    "LookupError",
    "MemoryError",
    "NameError",
    "NotImplementedError",
    "OverflowError",
    "RecursionError",
    "RuntimeError",
    "StopIteration",
    "TypeError",
    "UnboundLocalError",
    "UnicodeError",
    "UnicodeDecodeError",
    "UnicodeEncodeError",
    "ValueError",
    "ZeroDivisionError",
):
    _ALLOWED_BUILTINS[_exc_name] = getattr(builtins, _exc_name)


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
        # 纵深防御: 标识符类属性（函数名 / 参数名 / 关键字参数名 / except ... as 名）不得为 dunder。
        # 超时检查是编译前注入到脚本里的一次调用（见 _DEADLINE_CHECK_NAME），脚本若能用 dunder
        # 定义同名函数或同名参数，就能覆盖 / 遮蔽它，使循环里的到期检查失效。
        for attr in ("name", "arg"):
            value = getattr(node, attr, None)
            if isinstance(value, str) and _is_dunder(value):
                self._dangerous.append(f"forbidden identifier '{value}' at line {getattr(node, 'lineno', 0)}")
                return
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


# 注入到脚本里的到期检查所用的名字。dunder 名：白名单禁止脚本引用或定义任何 dunder 标识符，
# 所以脚本既不能调用它，也不能覆盖 / 遮蔽它（见 _ScriptValidator.generic_visit）。
_DEADLINE_CHECK_NAME = "__edgelite_deadline_check__"


def _make_deadline_check(deadline: float) -> Any:
    """构造到期检查函数：当前时刻到达 deadline 即抛出 _ScriptTimeout"""

    def _check() -> None:
        if time.monotonic() >= deadline:
            raise _ScriptTimeout

    return _check


class _DeadlineCheckInjector(ast.NodeTransformer):
    """在每个循环（while / for）体开头注入一条到期检查调用

    为什么不用 sys.settrace：CPython 在跟踪函数抛出异常后会卸载该线程的跟踪函数，脚本只要用
    裸 except / except BaseException（哪怕先起个别名）捕获一次超时异常，之后的无限循环就不再被
    跟踪。把检查直接编进脚本的每个循环体则与此无关：循环每迭代一次必然执行一次检查；到期后
    检查抛出的异常即使被捕获，脚本要继续循环就得再进入某个循环体，于是再次抛出——嵌套的
    try 每层最多吸收一次异常，最终必然退出。必须在白名单校验通过之后再注入。

    无法打断的情形：不含循环的单次超长 C 级运算（如 sum(range(10**12))）——它会自行结束，
    期间占用一个脚本线程名额（名额上限见 _MAX_SCRIPT_WORKERS）。
    """

    def __init__(self) -> None:
        super().__init__()
        self.injected = 0

    def _visit_loop(self, node: ast.While | ast.For | ast.AsyncFor) -> Any:
        self.generic_visit(node)  # 先处理嵌套循环
        check = ast.Expr(value=ast.Call(func=ast.Name(id=_DEADLINE_CHECK_NAME, ctx=ast.Load()), args=[], keywords=[]))
        node.body = [check, *node.body]
        self.injected += 1
        return node

    def visit_While(self, node: ast.While) -> Any:
        return self._visit_loop(node)

    def visit_For(self, node: ast.For) -> Any:
        return self._visit_loop(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> Any:
        return self._visit_loop(node)


def _compile_sandbox_script(script: str, filename: str, deadline: float | None) -> CodeType | None:
    """解析并编译脚本；给定 deadline 时先在每个循环体开头注入到期检查。无法编译返回 None"""
    try:
        tree = ast.parse(script, mode="exec")
        if deadline is not None:
            tree = _DeadlineCheckInjector().visit(tree)
            ast.fix_missing_locations(tree)
        result: CodeType = compile(tree, filename, "exec")
        return result
    except SyntaxError as e:
        logger.warning("sandbox: compile failed: %s", e)
    except (ValueError, RecursionError, MemoryError) as e:
        # 嵌套过深 / 体量过大的脚本同样按"无法编译"处理，不能让异常泄漏到调用线程之外
        logger.warning("sandbox: compile failed (%s): %s", type(e).__name__, e)
    return None


def _execute_script_sync(
    script: str,
    namespace: dict[str, Any],
    filename: str,
    deadline: float | None = None,
) -> Any:
    """同步执行脚本（在脚本线程中调用）

    使用受限 __builtins__ 执行（FIXED-CI-137: 不再施加进程级 rlimit，见模块 docstring），
    执行后返回 namespace["result"]。给定 ``deadline``（time.monotonic() 时基）时，
    编译前会在每个循环体开头注入到期检查，脚本超过该时刻即被打断。
    """
    # 构建受限命名空间
    safe_builtins = dict(_ALLOWED_BUILTINS)
    safe_namespace: dict[str, Any] = {
        "__builtins__": safe_builtins,
        **namespace,
    }
    if deadline is not None:
        safe_namespace[_DEADLINE_CHECK_NAME] = _make_deadline_check(deadline)

    code = _compile_sandbox_script(script, filename, deadline)
    if code is None:
        namespace["result"] = False
        return namespace.get("result", False)

    timed_out = False
    try:
        exec(code, safe_namespace)  # noqa: S102 — 受控沙箱执行
    except _ScriptTimeout:
        timed_out = True
        safe_namespace["result"] = False
    except Exception as e:
        logger.warning("sandbox: script execution error: %s\n%s", e, traceback.format_exc())
        safe_namespace["result"] = False
    except BaseException as e:
        # 脚本 raise 了非 Exception 的 BaseException（受限内建里暴露了 BaseException）：
        # 同样按脚本执行失败处理，不能让它泄漏到调用方的任务里（调用方只捕获 Exception）
        logger.warning("sandbox: script raised %s: %s", type(e).__name__, e)
        safe_namespace["result"] = False

    # 将可能被脚本修改的 namespace 键回写
    for key in namespace:
        if key in safe_namespace:
            namespace[key] = safe_namespace[key]

    if timed_out:
        raise TimeoutError(f"script execution exceeded the deadline ({filename})")
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
        timeout:  超时秒数（到点后脚本线程内的死循环会被打断）
        filename: 编译时的文件名（用于错误追踪）

    Returns:
        namespace["result"] 的值

    Raises:
        TimeoutError: 脚本执行超时，或脚本线程名额已被之前的失控脚本占满
        ValueError: 脚本包含危险代码（AST 检查未通过）
    """
    if not script or not script.strip():
        return namespace.get("result", False)

    # AST 静态分析
    if not _validate_script_ast(script):
        raise ValueError("script blocked by AST safety check")

    loop = asyncio.get_running_loop()
    # 名额非阻塞获取：占满说明已有 _MAX_SCRIPT_WORKERS 个脚本仍在运行（通常是卡在 C 级运算里），
    # 此时立即失败比排队等待（会一直等到 timeout）更诚实，也防止请求无限堆积
    if not _script_slots.acquire(blocking=False):
        raise TimeoutError(
            f"script executor saturated: {_MAX_SCRIPT_WORKERS} script(s) still running; request rejected"
        )

    future: asyncio.Future = loop.create_future()
    deadline = time.monotonic() + timeout

    def _finish(result: Any, exc: BaseException | None) -> None:
        if future.cancelled():
            return
        if exc is not None:
            future.set_exception(exc)
        else:
            future.set_result(result)

    def _worker() -> None:
        result: Any = None
        error: BaseException | None = None
        try:
            result = _execute_script_sync(script, namespace, filename, deadline)
        except BaseException as e:  # 把所有异常交回事件循环，避免"Task exception was never retrieved"
            error = e
        finally:
            _script_slots.release()
        if not loop.is_closed():
            try:
                loop.call_soon_threadsafe(_finish, result, error)
            except RuntimeError:
                pass  # 事件循环已关闭（进程正在退出）

    thread = threading.Thread(target=_worker, name=_SCRIPT_THREAD_NAME, daemon=True)
    try:
        thread.start()
    except RuntimeError:
        # 无法创建新线程（系统资源耗尽）：归还名额并如实上报
        _script_slots.release()
        raise

    try:
        return await asyncio.wait_for(future, timeout=timeout)
    except TimeoutError:
        # 等待方到点返回；脚本线程最迟在同一时刻被跟踪函数打断并释放名额
        logger.warning("sandbox: script timed out after %.2fs (%s)", timeout, filename)
        raise
