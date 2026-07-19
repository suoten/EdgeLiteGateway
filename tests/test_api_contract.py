"""前后端 API 契约校验脚本 (scripts/check_api_contract.py) 的单元测试。

覆盖：
- test_contract_check_passes    : 运行脚本主流程，断言 exit code == 0
                                  （当前存在 25 个真实 404 缺口，标记 xfail 并说明）
- test_backend_routes_extracted : 断言脚本能从 src/edgelite/api/*.py 提取后端路由
- test_frontend_calls_extracted : 断言脚本能从 web/src/api/*.ts 提取前端调用
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

# ── 加载 scripts/check_api_contract.py 作为模块（scripts 目录不是包） ────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "check_api_contract.py"


def _load_contract_module():
    """以 importlib 方式加载 check_api_contract 模块，避免污染 sys.path。"""
    spec = importlib.util.spec_from_file_location(
        "check_api_contract", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # 必须先注册到 sys.modules，否则 @dataclass 装饰器在 Python 3.12 上
    # 会因 sys.modules[cls.__module__] 返回 None 而抛出 AttributeError。
    sys.modules["check_api_contract"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def contract_module():
    """加载契约校验脚本模块。"""
    return _load_contract_module()


@pytest.fixture(scope="module")
def contract_report(contract_module):
    """运行契约校验并返回 ContractReport 对象（执行一次，模块级缓存）。"""
    return contract_module.run_check(PROJECT_ROOT)


# ═══════════════════════════════════════════════════════════════════════════
# 测试 1：契约校验整体通过
# ═══════════════════════════════════════════════════════════════════════════
# 当前已知存在 25 个真实 404 缺口（前端调用了后端未实现的端点），按用户要求标记 xfail。
# 这些缺口需要在后端补全相应端点后才能移除 xfail 标记。
#
# 25 个 404 缺口分布：
#   - rules 模块 (3): /rules/{id}/versions, /rules/{id}/versions/{version},
#                     /rules/{id}/versions/rollback
#   - system 模块 (3): /system/health, /system/ready, PUT /system/backup/schedule
#   - ai 模块 (5): PUT /ai/ab-test/{id}/split, DELETE /ai/ab-test/{id},
#                   GET /ai/hot-swap/{id}, PUT /ai/models/{id}/preprocess,
#                   PUT /ai/models/{id}/postprocess
#   - shadow 模块 (2): PUT /shadows/{id}/reported, DELETE /shadows/{id}
#   - bridge 模块 (1): DELETE /bridge/{name}
#   - linkage 模块 (2): /linkage/rules/{id}/enable, /linkage/rules/{id}/disable
#   - logs 模块 (3): POST /logs/filters, DELETE /logs/filters, POST /logs/level
#   - config 模块 (1): DELETE /config/versions/{id}
#   - resource-shares 模块 (1): POST /resource-shares/transfer
#   - scripts 模块 (1): DELETE /scripts/{id}
#   - observability 模块 (3): POST /observability/alerts/rules,
#                              PUT /observability/alerts/rules/{id},
#                              DELETE /observability/alerts/rules/{id}
#
# 修复方案有两种：
#   A) 在 src/edgelite/api/*.py 中补全缺失的端点
#   B) 修正 web/src/api/index.ts 中错误的 HTTP 方法或删除未使用的调用
# 当前选择 xfail，等待后续修复（避免在契约校验任务中引入过大的后端改动）。
@pytest.mark.xfail(
    reason=(
        "项目当前存在 25 个真实 404 缺口（前端调用了后端未实现的端点）。"
        "详见 docs/api_contract.md 第 5.1 节。"
        "需在后端补全端点或修正前端调用后才能移除 xfail。"
    ),
    strict=True,
)
def test_contract_check_passes():
    """运行 scripts/check_api_contract.py，断言 exit code == 0。

    当前预期 xfail（存在 25 个真实 404 缺口）。
    """
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, (
        f"contract check failed with exit code {result.returncode}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# 测试 2：后端路由提取
# ═══════════════════════════════════════════════════════════════════════════
def test_backend_routes_extracted(contract_report):
    """断言脚本能从 src/edgelite/api/*.py 正确提取后端路由清单。

    验证点：
    1. 提取到的 HTTP 路由数应 > 100（项目有 45+ 个 API 文件，路由总数 379）
    2. 提取到的 WebSocket 路由数应 >= 5（app.py 中注册了 5 个 WS 路由）
    3. 路由路径应以 /api/v1/ 开头（除根级 health/metrics 外）
    4. 每条路由应包含 method、path、function、file、module 字段
    """
    # 1. 数量校验
    assert len(contract_report.backend_routes) > 100, (
        f"backend routes count too low: {len(contract_report.backend_routes)}"
    )

    # 2. WebSocket 路由数
    assert len(contract_report.backend_ws_routes) >= 5, (
        f"backend WS routes count too low: {len(contract_report.backend_ws_routes)}"
    )

    # 3. 抽样校验字段和路径前缀
    sample = contract_report.backend_routes[0]
    assert hasattr(sample, "method"), "route missing 'method' field"
    assert hasattr(sample, "path"), "route missing 'path' field"
    assert hasattr(sample, "function"), "route missing 'function' field"
    assert hasattr(sample, "file"), "route missing 'file' field"
    assert hasattr(sample, "module"), "route missing 'module' field"

    # 4. 大多数路由应以 /api/v1/ 开头（除根级 health/live/ready/metrics 外）
    api_v1_routes = [
        r for r in contract_report.backend_routes
        if r.path.startswith("/api/v1/")
    ]
    assert len(api_v1_routes) > 90, (
        f"expected most routes under /api/v1/, got {len(api_v1_routes)}/"
        f"{len(contract_report.backend_routes)}"
    )

    # 5. 校验关键模块（devices/auth/system）的路由存在
    all_paths = {r.path for r in contract_report.backend_routes}
    assert "/api/v1/devices" in all_paths, "missing GET /api/v1/devices"
    assert any(p.startswith("/api/v1/auth") for p in all_paths), "missing /api/v1/auth/*"
    assert any(p.startswith("/api/v1/system") for p in all_paths), "missing /api/v1/system/*"


# ═══════════════════════════════════════════════════════════════════════════
# 测试 3：前端调用提取
# ═══════════════════════════════════════════════════════════════════════════
def test_frontend_calls_extracted(contract_report):
    """断言脚本能从 web/src/api/*.ts 正确提取前端调用清单。

    验证点：
    1. 提取到的 HTTP 调用数应 > 100（前端 index.ts 有 50+ 个 api 对象）
    2. 提取到的 WebSocket 调用数应 == 5（websocket.ts 中 CHANNELS 定义 5 个通道）
    3. 调用路径应以 /api/v1/ 开头（baseURL 拼接后）
    4. 每条调用应包含 method、path、raw_url、api_name、file、line 字段
    5. TypeScript 泛型语法应正确解析（不出现 <ApiResponse<...>> 残留）
    6. URL 常量引用应正确传播（如 URL.OTA.CHECK → /api/v1/ota/check）
    """
    # 1. 数量校验
    assert len(contract_report.frontend_calls) > 100, (
        f"frontend calls count too low: {len(contract_report.frontend_calls)}"
    )

    # 2. WebSocket 调用数
    assert len(contract_report.frontend_ws_calls) == 5, (
        f"expected 5 frontend WS calls, got {len(contract_report.frontend_ws_calls)}"
    )

    # 3. 抽样校验字段
    sample = contract_report.frontend_calls[0]
    assert hasattr(sample, "method"), "call missing 'method' field"
    assert hasattr(sample, "path"), "call missing 'path' field"
    assert hasattr(sample, "raw_url"), "call missing 'raw_url' field"
    assert hasattr(sample, "api_name"), "call missing 'api_name' field"
    assert hasattr(sample, "file"), "call missing 'file' field"
    assert hasattr(sample, "line"), "call missing 'line' field"

    # 4. 大多数调用路径应以 /api/v1/ 开头
    api_v1_calls = [
        c for c in contract_report.frontend_calls
        if c.path.startswith("/api/v1/")
    ]
    assert len(api_v1_calls) > 90, (
        f"expected most calls under /api/v1/, got {len(api_v1_calls)}/"
        f"{len(contract_report.frontend_calls)}"
    )

    # 5. 不应有 TypeScript 泛型残留（如 http.get<ApiResponse<...>>(url) 中的 <...>）
    for c in contract_report.frontend_calls:
        assert "<" not in c.path, (
            f"path contains '<' (TS generic not stripped?): {c.path}"
        )
        assert ">" not in c.path, (
            f"path contains '>' (TS generic not stripped?): {c.path}"
        )

    # 6. URL 常量传播校验：URL.OTA.CHECK 应被解析为 /api/v1/ota/check
    all_paths = {c.path for c in contract_report.frontend_calls}
    assert "/api/v1/ota/check" in all_paths, (
        "URL.OTA.CHECK constant not propagated (expected /api/v1/ota/check)"
    )

    # 7. 校验关键 API 模块（authApi/deviceApi/systemApi）的调用存在
    api_names = {c.api_name for c in contract_report.frontend_calls}
    assert "authApi" in api_names, "missing authApi calls"
    assert "deviceApi" in api_names, "missing deviceApi calls"
    assert "systemApi" in api_names, "missing systemApi calls"


# ═══════════════════════════════════════════════════════════════════════════
# 附加测试：脚本帮助信息和退出码
# ═══════════════════════════════════════════════════════════════════════════
def test_script_help_works():
    """脚本的 --help 参数应正常工作（验证脚本可执行）。"""
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--help"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, f"--help failed: {result.stderr}"
    assert "contract" in result.stdout.lower() or "api" in result.stdout.lower()
