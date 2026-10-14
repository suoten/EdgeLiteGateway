"""sandbox.py 白名单改造与 rlimit 恢复回归测试

对应审计修复:
- P0-2: AST 校验从黑名单改为白名单模式，黑名单绕过 PoC
  `"{0.__class__}".format(())` 必须被拒绝；
  正常算术/赋值/方法调用脚本仍可运行。
- P0-2: setrlimit 执行前保存旧值、finally 恢复（恢复失败记录 error）。
"""

from __future__ import annotations

import sys
import inspect
from unittest.mock import patch

import pytest

from edgelite.engine.sandbox import (
    _apply_resource_limits,
    _execute_script_sync,
    _is_forbidden_attr,
    _limit_specs,
    _restore_resource_limits,
    _validate_script_ast,
    run_script_safely,
)


def _unix_only() -> bool:
    """当前平台是否支持 resource rlimit（非 Windows 且模块可用）"""
    if sys.platform == "win32":
        return False
    from edgelite.engine import sandbox

    return sandbox.resource is not None


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


class TestResourceLimitRestore:
    """setrlimit 执行前保存旧值、finally 恢复；恢复失败记录 error"""

    def test_apply_and_restore_roundtrip(self):
        if not _unix_only():
            pytest.skip("resource limits are Unix-only")
        from edgelite.engine import sandbox

        saved = _apply_resource_limits()
        try:
            if not saved:
                pytest.skip("rlimit unavailable in this environment")
            assert isinstance(saved, dict)
        finally:
            _restore_resource_limits(saved)

        # 恢复后再次读取应等于保存值
        for res, old in saved.items():
            assert sandbox.resource.getrlimit(res) == old

    def test_restore_failure_logs_error(self, caplog):
        if not _unix_only():
            pytest.skip("resource limits are Unix-only")
        from edgelite.engine import sandbox

        fake_saved = {sandbox.resource.RLIMIT_CPU: (1, 1)}
        with (
            patch.object(sandbox.resource, "setrlimit", side_effect=OSError("denied")),
            caplog.at_level("ERROR", logger="edgelite.engine.sandbox"),
        ):
            _restore_resource_limits(fake_saved)
        assert any("restore rlimit" in r.message for r in caplog.records)

    def test_apply_failure_rolls_back_and_returns_empty(self):
        if not _unix_only():
            pytest.skip("resource limits are Unix-only")
        from edgelite.engine import sandbox

        with patch.object(sandbox.resource, "setrlimit", side_effect=ValueError("bad limit")):
            saved = _apply_resource_limits()
        assert saved == {}

    def test_execute_script_sync_restores_limits(self):
        if not _unix_only():
            pytest.skip("resource limits are Unix-only")
        from edgelite.engine import sandbox

        before = {res: sandbox.resource.getrlimit(res) for res, _ in _limit_specs()}
        ns = {"result": False}
        _execute_script_sync("result = 1 + 1", ns, "<test>")
        assert ns["result"] == 2
        after = {res: sandbox.resource.getrlimit(res) for res, _ in _limit_specs()}
        assert before == after
