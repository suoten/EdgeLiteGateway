"""sandbox.py AST 白名单回归测试

对应审计修复:
- P0-2: AST 校验从黑名单改为白名单模式，黑名单绕过 PoC
  `"{0.__class__}".format(())` 必须被拒绝；
  正常算术/赋值/方法调用脚本仍可运行。

FIXED-CI-137: 进程内 setrlimit 已整体移除（POSIX rlimit 是进程属性，对长生命周期
宿主进程设置低于现状的 CPU/AS 限制会立刻 SIGXCPU/SIGKILL，CI pytest 即被杀于
exit 137）。沙箱防护 = AST 白名单 + 受限内建 + 外层超时；不再有 rlimit 相关接口。
"""

from __future__ import annotations

import inspect

import pytest

from edgelite.engine.sandbox import (
    _execute_script_sync,
    _is_forbidden_attr,
    _validate_script_ast,
    run_script_safely,
)


class TestAstWhitelistBlocks:
    """黑名单绕过 PoC 必须被拒绝"""

    @pytest.mark.parametrize(
        "poc",
        [
            # 经典 format 沙箱逃逸 PoC
            '"{0.__class__}".format(())',
            'result = "{0.__class__}".format(())',
            'result = "{0.__class__}".format_map({})',
            # dunder 属性链逃逸
            "result = ().__class__.__bases__[0].__subclasses__()",
            "result = x.__dict__",
            "result = x.__globals__",
            "result = str.__class__",
            # 动态执行
            'result = eval("1+1")',
            'exec("import os")',
            'open("/etc/passwd")',
            # import / 系统
            "import os",
            "from os import system",
            # 资源耗尽
            "result = 10**100000000",
            # dunder 下标
            'result = point_values["__class__"]',
        ],
    )
    def test_poc_rejected(self, poc: str):
        assert _validate_script_ast(poc) is False, f"PoC must be rejected: {poc}"

    def test_format_attr_forbidden(self):
        assert _is_forbidden_attr("format") is True
        assert _is_forbidden_attr("format_map") is True
        assert _is_forbidden_attr("__class__") is True
        assert _is_forbidden_attr("upper") is False

    def test_disallowed_statement_rejected(self):
        # with/async/yield 等未白名单语句应被拒绝
        assert _validate_script_ast("with open('x') as f:\n    pass") is False
        assert _validate_script_ast("async def f():\n    pass") is False
        assert _validate_script_ast("def g():\n    yield 1") is False

    def test_syntax_error_rejected(self):
        assert _validate_script_ast("def broken(:") is False


class TestAstWhitelistAllows:
    """正常算术/赋值/方法调用脚本仍可运行"""

    @pytest.mark.parametrize(
        "script",
        [
            'result = point_values["temp"] * 2',
            "result = 1 + 2 * 3 - 4 / 2",
            "x = 0\nfor i in range(10):\n    x += i\nresult = x",
            'if point_values.get("a", 0) > 5:\n    result = "high"\nelse:\n    result = "low"',
            'result = str(point_values.get("a", "")).upper()',
            "result = min(1, 2, 3)",
            "d = {'a': 1}\nresult = d.get('a') * 2",
            "lst = [1, 2, 3]\nresult = sum(lst) + len(lst)",
            "def helper(v):\n    return v * 2\nresult = helper(21)",
            "result = [v for v in (1, 2, 3) if v > 1]",
            'result = f"value={int(1)}"',
            "result = 2 ** 10",
            "result, other = 1, 2",
            "while False:\n    break\nresult = True",
            "try:\n    result = 1 / 0\nexcept ZeroDivisionError:\n    result = 0",
            "x = 1\nassert x == 1\nresult = x",
            "i = 0\nwhile i < 3:\n    i = i + 1\nresult = i",
        ],
    )
    def test_normal_script_allowed(self, script: str):
        assert _validate_script_ast(script) is True, f"Normal script must pass: {script!r}"

    def test_empty_script_allowed(self):
        assert _validate_script_ast("") is True


class TestRunScriptSafely:
    """端到端执行保持原有语义（api/scripts.py 与 evaluator.py 依赖的对外签名不变）"""

    @pytest.mark.asyncio
    async def test_normal_script_executes(self):
        ns = {"point_values": {"temp": 21.5}, "result": False}
        r = await run_script_safely('result = point_values["temp"] * 2', ns)
        assert r == 43.0

    @pytest.mark.asyncio
    async def test_method_call_script_executes(self):
        ns = {"point_values": {"a": "x"}, "result": False}
        r = await run_script_safely('result = str(point_values["a"]).upper()', ns)
        assert r == "X"

    @pytest.mark.asyncio
    async def test_poc_rejected_at_runtime(self):
        ns = {"point_values": {}, "result": False}
        with pytest.raises(ValueError, match="script blocked by AST safety check"):
            await run_script_safely('result = "{0.__class__}".format(())', ns)

    @pytest.mark.asyncio
    async def test_empty_script_returns_result(self):
        ns = {"result": 42}
        assert await run_script_safely("", ns) == 42

    @pytest.mark.asyncio
    async def test_runtime_error_returns_false(self):
        ns = {"point_values": {}, "result": False}
        r = await run_script_safely("result = point_values['missing'] * 2", ns)
        assert r is False

    def test_signature_unchanged(self):
        sig = inspect.signature(run_script_safely)
        assert list(sig.parameters) == ["script", "namespace", "timeout", "filename"]
        assert callable(_validate_script_ast)


class TestNoProcessLevelLimits:
    """FIXED-CI-137: 沙箱不得对宿主进程施加 rlimit（执行前后进程资源状态不变）"""

    def test_execute_script_sync_leaves_limits_alone(self):
        import sys

        if sys.platform == "win32":
            pytest.skip("resource module is POSIX-only")
        import resource

        spec = [
            getattr(resource, "RLIMIT_CPU", None),
            getattr(resource, "RLIMIT_AS", None),
            getattr(resource, "RLIMIT_FSIZE", None),
        ]
        before = {r: resource.getrlimit(r) for r in spec if r is not None}
        ns = {"result": False}
        _execute_script_sync("result = 1 + 1", ns, "<test>")
        assert ns["result"] == 2
        after = {r: resource.getrlimit(r) for r in spec if r is not None}
        assert before == after
