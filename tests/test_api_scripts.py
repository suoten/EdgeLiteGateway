"""脚本管理 API 安全回归测试（审计 P1-1 修复）

覆盖 src/edgelite/api/scripts.py：
- P1-1: test 端点不再使用本地 exec + _SAFE_BUILTINS（print.__self__ 一行逃逸 → RCE），
  统一走 engine/sandbox.run_script_safely（AST 拦截 + 受限内建 + 超时保护）；
- test_script 仅允许 approved/enabled 状态脚本执行，draft 返回 409。
"""

from __future__ import annotations

import inspect
import sys

sys.path.insert(0, "src")

import pytest
from fastapi import HTTPException

from edgelite.api.scripts import (
    _ERR_SCRIPT_NOT_TESTABLE,
    _assert_script_testable,
    _execute_in_sandbox,
)


# ── P1-1: status 检查 ──


class TestScriptStatusGate:
    def test_draft_rejected_with_409(self):
        """P1-1 回归: draft 脚本不允许测试执行（未经审批的代码不得即时运行）。"""
        with pytest.raises(HTTPException) as e:
            _assert_script_testable("draft")
        assert e.value.status_code == 409
        assert e.value.detail == _ERR_SCRIPT_NOT_TESTABLE

    @pytest.mark.parametrize("status", ["pending_review", "rejected"])
    def test_other_nonapproved_rejected(self, status):
        with pytest.raises(HTTPException) as e:
            _assert_script_testable(status)
        assert e.value.status_code == 409

    @pytest.mark.parametrize("status", ["approved", "enabled"])
    def test_approved_enabled_allowed(self, status):
        _assert_script_testable(status)  # 不应抛异常


# ── P1-1: 沙箱执行核心 ──


class TestSandboxExecution:
    async def test_normal_script_computes_result(self):
        """正常脚本应可执行并回传 result。"""
        output_lines: list[str] = []
        result, err = await _execute_in_sandbox("result = 1 + 1", {}, output_lines, "s1")
        assert err == ""
        assert result == 2

    async def test_output_channel_captures(self):
        """沙箱通过命名空间中的 output 回调收集输出（print 在沙箱内为 no-op）。"""
        output_lines: list[str] = []
        await _execute_in_sandbox("output('hello')\nresult = 'ok'", {}, output_lines, "s2")
        assert output_lines == ["hello"]

    async def test_builtins_escape_blocked(self):
        """P1-1 回归: 原沙箱逃逸 PoC（print.__self__ → builtins → __import__）必须被拦截。"""
        output_lines: list[str] = []
        poc = "result = print.__self__.__import__('os').getcwd()"
        result, err = await _execute_in_sandbox(poc, {}, output_lines, "s3")
        assert result is None
        assert "blocked" in err

    async def test_import_blocked(self):
        output_lines: list[str] = []
        result, err = await _execute_in_sandbox("import os\nresult = os.getcwd()", {}, output_lines, "s4")
        assert result is None
        assert "blocked" in err

    async def test_dunder_chain_blocked(self):
        """AST 黑名单应拦截 dunder 属性链（如 format 字符串属性链）。"""
        output_lines: list[str] = []
        poc = "result = '{0.__class__.__mro__}'.format(())"
        result, err = await _execute_in_sandbox(poc, {}, output_lines, "s5")
        assert result is None
        assert "blocked" in err

    async def test_timeout_error_mapped_to_error_text(self, monkeypatch):
        """P1-1 回归: 沙箱超时（asyncio.wait_for TimeoutError）必须转为 error_text 返回，
        而非挂死请求或抛 500。真实死循环脚本的线程级终止由 engine/sandbox 负责
        （Windows 线程无法强杀，此处以注入 TimeoutError 验证端点侧语义）。"""

        async def fake_run(script, namespace, timeout=3.0, filename="<rule_script>"):
            raise TimeoutError()

        monkeypatch.setattr("edgelite.api.scripts.run_script_safely", fake_run)
        result, err = await _execute_in_sandbox("result = 1", {}, [], "s6")
        assert result is None
        assert "timed out" in err

    async def test_syntax_error_reported_not_raised(self):
        output_lines: list[str] = []
        result, err = await _execute_in_sandbox("def broken(:", {}, output_lines, "s7")
        assert result is None
        assert err != ""


# ── P1-1: 源码结构断言 ──


def test_no_local_exec_sandbox_left_in_scripts_module():
    """P1-1 结构断言: scripts.py 不得再残留本地 exec 执行路径或 _SAFE_BUILTINS 定义。"""
    from edgelite.api import scripts as scripts_module

    src = inspect.getsource(scripts_module)
    assert "_SAFE_BUILTINS = {" not in src
    assert "__builtins__" not in src
    assert "compile(" not in src
    assert "run_script_safely" in src


def test_sandbox_module_is_engine_sandbox():
    """test 端点必须依赖 engine/sandbox 的 run_script_safely（加固由沙箱模块统一收敛）。"""
    from edgelite.api import scripts as scripts_module

    assert scripts_module.run_script_safely.__module__ == "edgelite.engine.sandbox"
