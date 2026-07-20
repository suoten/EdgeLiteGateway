#!/usr/bin/env python3
"""EdgeLite 验收门禁检查 — 部署后端到端点遍历验证。

本脚本对运行中的 EdgeLite 后端和前端执行端到端验收检查：

1. **API 端点检查**：通过 ``from edgelite.app import create_app`` 导入 FastAPI
   app 实例，遍历 ``app.routes`` 动态提取所有已注册路由。先登录获取
   access_token，再对每个 GET 端点发送带 ``Authorization: Bearer <token>``
   头的请求。断言无 500 错误，且无 404（端点已注册却 404 说明路由异常）。
   允许 401/403（权限不足）、422（参数验证）、400（业务错误）作为可接受
   的非 500 响应。

2. **前端路由检查**：遍历 ``web/src/router/index.ts`` 中定义的所有前端
   路由，断言 HTTP 200 且无 JS 控制台错误（如果 Playwright 可用）。
   Playwright 不可用时退化为 httpx HTTP 200 检查。

前置条件：
    - 后端服务已启动（默认 http://127.0.0.1:8080），可通过 ``/health/live``
      检查通过。
    - 前端 dev server 已启动（默认 http://127.0.0.1:5173），或后端已挂载
      前端静态文件。
    - ``EDGELITE_ADMIN_PASSWORD`` 环境变量已设置，或通过 ``--password``
      参数传入。

用法::

    # 默认：后端 8080 + 前端 5173
    python scripts/acceptance_check.py

    # 自定义后端 URL 和密码
    python scripts/acceptance_check.py --base-url http://localhost:8080 \\
        --password secret

    # 仅检查 API，跳过前端
    python scripts/acceptance_check.py --skip-frontend

    # 从环境变量读取密码（默认行为）
    EDGELITE_ADMIN_PASSWORD=mysecret python scripts/acceptance_check.py

退出码：
    0 — 所有检查通过
    1 — 一个或多个检查失败
    2 — 健康检查失败（后端未启动或不可达）
    3 — 缺少必填参数（如密码未设置）
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

# ── 路径准备：确保能 import edgelite（脚本可独立运行）────────────────────
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
_SRC = _ROOT / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# ── Windows 控制台 UTF-8 输出（emoji + 中文）────────────────────────────
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import httpx  # noqa: E402

# ── 常量 ────────────────────────────────────────────────────────────────

# 前端路由硬编码列表（与 web/src/router/index.ts 同步，作为文件解析失败的兜底）
# 仅包含无路径参数的路由；带 :param 的路由（如 /devices/:id）不在此列
_DEFAULT_FRONTEND_ROUTES: list[str] = [
    "/login",
    "/",
    "/devices",
    "/devices/templates",
    "/devices/shadow",
    "/rules",
    "/alarms",
    "/alarms/trend",
    "/alarms/correlation",
    "/data",
    "/data/quality",
    "/data/quality-monitor",
    "/data/downsample",
    "/report",
    "/system",
    "/system/services",
    "/system/drivers",
    "/system/platforms",
    "/system/platforms/dashboard",
    "/system/platforms/tb-monitor",
    "/system/expressions",
    "/system/preprocess",
    "/system/audit",
    "/system/serial-bridge",
    "/system/bridge",
    "/system/pipeline",
    "/system/mqtt-server",
    "/system/modbus-slave",
    "/system/app-update",
    "/system/grafana",
    "/system/mcp",
    "/system/ai-model",
    "/system/ai-monitor",
    "/system/ai-ab-test",
    "/system/ai-test",
    "/system/ai-center",
    "/system/ai-report",
    "/system/linkage",
    "/system/profiler",
    "/system/log-aggregator",
    "/system/firmware-signature",
    "/system/notify",
    "/system/integration",
    "/system/debug",
    "/system/metrics",
    "/system/config-version",
    "/system/self-test",
    "/system/data-export",
    "/system/data-import",
    "/system/resource-sharing",
    "/system/db-monitor",
    "/system/backup-schedule",
    "/system/config",
    "/system/scripts",
    "/system/simulation",
    "/system/anomaly-learner",
    "/system/trend-learner",
    "/system/threshold-learner",
    "/system/calibration",
    "/system/physics-calibrator",
    "/system/physics-param-db",
    "/system/precision-test",
    "/system/evolution-verify",
    "/system/boundary-test",
    "/system/stress-test",
    "/observability",
    "/observability/overview",
    "/observability/rules",
    "/observability/events",
    "/observability/traces",
    "/observability/metrics",
    "/modbus-ops",
    "/users",
    "/digital-twin",
    "/scada",
    "/dashboard/large-screen",
]


# ── 路由提取 ────────────────────────────────────────────────────────────


def _collect_api_routes(app: Any) -> list[tuple[str, str]]:
    """从 FastAPI app 实例提取所有 (HTTP_method, full_path) 对。

    FastAPI 0.110+ 中 ``include_router`` 会将子 router 包成
    ``_IncludedRouter``，需通过 ``original_router`` 解包。
    """
    routes: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def _add(method: str, path: str) -> None:
        key = (method, path)
        if key not in seen:
            seen.add(key)
            routes.append(key)

    def _walk(r: Any) -> None:
        # _IncludedRouter 解包
        inner = getattr(r, "original_router", None) or r
        # 直接挂载的路由（APIRoute/APIWebSocketRoute/Mount）有 path + methods
        path = getattr(r, "path", None)
        methods = getattr(r, "methods", None)
        if path and methods:
            for m in methods:
                if m in ("GET", "POST", "PUT", "DELETE", "PATCH"):
                    _add(m, path)
        # 子 router 递归
        if hasattr(inner, "routes"):
            for sub in inner.routes:
                _walk(sub)

    for r in app.routes:
        _walk(r)
    return routes


def _is_excluded_path(path: str) -> bool:
    """判断路径是否应排除（SPA catch-all / WebSocket / 静态资源等）。"""
    # SPA 前端 catch-all 路由（如 /{path:path}）
    if "{path:path}" in path:
        return True
    # WebSocket 路由
    if path.startswith("/ws/"):
        return True
    # 静态资源挂载
    if path.startswith("/assets/"):
        return True
    return False


def _has_path_params(path: str) -> bool:
    """判断路径是否包含 {param} 形式的路径参数。"""
    return bool(re.search(r"\{[^}]+\}", path))


def _substitute_path_params(path: str) -> str:
    """将路径参数 {xxx} 替换为合法占位值 ``1``。

    例：``/api/v1/devices/{device_id}`` → ``/api/v1/devices/1``
    """
    return re.sub(r"\{[^}]+\}", "1", path)


# ── 前端路由解析 ────────────────────────────────────────────────────────


def _parse_frontend_routes_from_file() -> list[str] | None:
    """尝试从 ``web/src/router/index.ts`` 解析前端路由列表。

    解析失败时返回 None，调用方应使用硬编码列表作为兜底。
    """
    router_file = _ROOT / "web" / "src" / "router" / "index.ts"
    if not router_file.is_file():
        return None
    try:
        content = router_file.read_text(encoding="utf-8")
        # 匹配 path: 'xxx' 或 path: "xxx"
        matches = re.findall(r"path:\s*['\"]([^'\"]+)['\"]", content)
        if not matches:
            return None
        # 去重 + 过滤带参数的路由 + 排除通配符 + 规范化前导斜杠
        # Vue Router 子路由的 path 没有 leading /（如 'devices'），
        # 但父路由 path 为 '/'，拼接后是 /devices，因此统一补前导 /
        seen: set[str] = set()
        routes: list[str] = []
        for p in matches:
            # 规范化：确保以 / 开头
            if not p.startswith("/"):
                p = "/" + p
            if p in seen:
                continue
            seen.add(p)
            # 跳过带 :param 的路由
            if ":" in p:
                continue
            # 跳过通配符路由
            if "*" in p:
                continue
            routes.append(p)
        return sorted(routes)
    except Exception:
        return None


# ── HTTP 工具函数 ───────────────────────────────────────────────────────


def _wait_for_health(client: httpx.Client, attempts: int = 3, interval: float = 1.0) -> bool:
    """轮询 ``/health/live`` 健康检查，最多 attempts 次。"""
    for _ in range(attempts):
        try:
            r = client.get("/health/live", timeout=5.0)
            if r.status_code == 200 and r.json().get("status") == "ok":
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def _login(client: httpx.Client, username: str, password: str) -> str | None:
    """登录获取 access_token，失败返回 None。

    Token 返回格式：``response.json()["data"]["access_token"]``
    """
    try:
        r = client.post(
            "/api/v1/auth/login",
            json={"username": username, "password": password},
        )
        if r.status_code != 200:
            print(f"  ❌ 登录失败: HTTP {r.status_code} {r.text[:200]}")
            return None
        data = r.json()
        # 优先从 data.data.access_token 取（项目约定格式）
        token = (
            data.get("data", {}).get("access_token")
            if isinstance(data.get("data"), dict)
            else None
        )
        # 兜底：直接从顶层取
        if not token:
            token = data.get("access_token")
        return token
    except Exception as e:
        print(f"  ❌ 登录异常: {type(e).__name__}: {e}")
        return None


def _probe_api_endpoint(
    client: httpx.Client,
    method: str,
    path: str,
    token: str,
    timeout: float,
) -> tuple[int, str]:
    """对单个 API 端点发起请求，返回 (status_code, detail)。

    detail 包含状态码和（如失败时）响应体前 200 字符。
    status_code 为 -1 表示请求异常（超时/连接错误）。
    """
    test_path = _substitute_path_params(path)
    headers = {"Authorization": f"Bearer {token}"}
    try:
        if method == "GET":
            r = client.get(test_path, headers=headers, timeout=timeout)
        elif method == "POST":
            r = client.post(test_path, json={}, headers=headers, timeout=timeout)
        elif method == "PUT":
            r = client.put(test_path, json={}, headers=headers, timeout=timeout)
        elif method == "DELETE":
            r = client.delete(test_path, headers=headers, timeout=timeout)
        elif method == "PATCH":
            r = client.patch(test_path, json={}, headers=headers, timeout=timeout)
        else:
            return -1, f"unsupported method {method}"
        detail = f"HTTP {r.status_code}"
        if r.status_code >= 400:
            body = r.text[:200].replace("\n", " ")
            detail += f" body={body}"
        return r.status_code, detail
    except httpx.TimeoutException:
        return -1, f"timeout after {timeout}s"
    except Exception as e:
        return -1, f"exception: {type(e).__name__}: {e}"


def _is_api_failure(status: int, has_path_params: bool) -> bool:
    """判断状态码是否构成验收失败。

    - -1（异常）→ 失败
    - 5xx → 失败
    - 404 且无路径参数 → 失败（端点已注册却 404 说明路由异常）
    - 404 且有路径参数 → 可接受（资源不存在，非路由问题）
    - 其他 4xx → 可接受（业务/权限/参数校验错误）
    """
    if status == -1:
        return True
    if status >= 500:
        return True
    if status == 404 and not has_path_params:
        return True
    return False


# ── 检查主流程 ──────────────────────────────────────────────────────────


def run_api_checks(
    base_url: str,
    username: str,
    password: str,
    timeout: float,
) -> tuple[int, int, list[tuple[str, str, int, str]]]:
    """执行 API 端点遍历检查。

    Returns:
        (passed, total, failures)
        - passed: 通过端点数
        - total: 总测试端点数
        - failures: 失败列表，每项 (method, path, status, detail)
    """
    print("\n[API 端点检查]")
    print(f"  目标: {base_url}")

    # 1. 提取路由（导入 FastAPI app，遍历 app.routes）
    try:
        # 设置必要环境变量以避免 create_app 失败
        os.environ.setdefault("DEV_MODE", "true")
        os.environ.setdefault(
            "EDGELITE_SECURITY__SECRET_KEY",
            "acceptance-check-secret-key-32+chars-long!",
        )
        from edgelite.app import create_app

        app = create_app()
        all_routes = _collect_api_routes(app)
    except Exception as e:
        print(f"  ❌ 无法导入 FastAPI app 或提取路由: {type(e).__name__}: {e}")
        print("  请确保 edgelite 包已安装 (pip install -e .) 或 src/ 目录可访问")
        return 0, 0, [("IMPORT", "create_app", -1, str(e)[:200])]

    # 2. 过滤：仅测试 GET 端点（POST/PUT/DELETE 可能修改数据）
    get_routes: list[tuple[str, str]] = []
    skipped_excluded = 0
    for m, p in all_routes:
        if m != "GET":
            continue
        if _is_excluded_path(p):
            skipped_excluded += 1
            continue
        get_routes.append((m, p))

    print(
        f"  发现 {len(all_routes)} 个总端点，"
        f"其中 {len(get_routes)} 个 GET 端点待测试（排除 {skipped_excluded} 个）"
    )

    if not get_routes:
        print("  ⚠️  未发现任何可测试的 GET 端点，跳过 API 检查")
        return 0, 0, []

    # 3. 启动 HTTP 客户端
    with httpx.Client(base_url=base_url, timeout=timeout) as client:
        # 4. 登录
        print(f"  登录用户: {username}")
        token = _login(client, username, password)
        if not token:
            print("  ❌ 登录失败，无法获取 access_token，API 检查中止")
            return 0, len(get_routes), [
                ("POST", "/api/v1/auth/login", -1, "login failed")
            ]

        # 5. 遍历所有 GET 端点
        passed = 0
        failures: list[tuple[str, str, int, str]] = []
        for method, path in get_routes:
            has_params = _has_path_params(path)
            status, detail = _probe_api_endpoint(client, method, path, token, timeout)
            if _is_api_failure(status, has_params):
                failures.append((method, path, status, detail))
                print(f"  ❌ FAIL  {method:4s} {path:50s}  {detail}")
            else:
                passed += 1
                print(f"  ✅ PASS  {method:4s} {path:50s}  HTTP {status}")

    return passed, len(get_routes), failures


def _check_frontend_with_playwright(
    frontend_url: str,
    routes: list[str],
    timeout: float,
) -> tuple[int, int, list[tuple[str, int, str]]]:
    """使用 Playwright 检查前端路由（含 JS 控制台错误检测）。

    Returns:
        (passed, total, failures)
        返回 (-1, 0, []) 表示 Playwright 不可用，调用方需降级。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return -1, 0, []

    passed = 0
    failures: list[tuple[str, int, str]] = []
    total = len(routes)

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as e:
            print(f"  ⚠️  Playwright 浏览器启动失败: {type(e).__name__}: {e}")
            print("     请运行 `playwright install chromium` 安装浏览器")
            return -1, 0, []

        try:
            context = browser.new_context()
            for route in routes:
                url = frontend_url.rstrip("/") + route
                page = context.new_page()
                console_errors: list[str] = []

                def _on_console(msg: Any) -> None:
                    if msg.type == "error":
                        console_errors.append(msg.text)

                def _on_pageerror(err: Any) -> None:
                    console_errors.append(f"pageerror: {err}")

                page.on("console", _on_console)
                page.on("pageerror", _on_pageerror)
                try:
                    response = page.goto(
                        url, wait_until="networkidle", timeout=int(timeout * 1000)
                    )
                    status = response.status if response else 0
                    if status != 200:
                        failures.append((route, status, f"HTTP {status}"))
                        print(f"  ❌ FAIL  {route:40s}  HTTP {status}")
                    elif console_errors:
                        err_summary = "; ".join(console_errors[:3])
                        failures.append(
                            (route, status, f"console errors: {err_summary[:200]}")
                        )
                        print(f"  ❌ FAIL  {route:40s}  console errors: {err_summary[:200]}")
                    else:
                        passed += 1
                        print(f"  ✅ PASS  {route:40s}  HTTP {status}")
                except Exception as e:
                    failures.append(
                        (route, 0, f"exception: {type(e).__name__}: {e}")
                    )
                    print(f"  ❌ FAIL  {route:40s}  exception: {e}")
                finally:
                    page.close()
        finally:
            browser.close()
    return passed, total, failures


def _check_frontend_with_httpx(
    frontend_url: str,
    routes: list[str],
    timeout: float,
) -> tuple[int, int, list[tuple[str, int, str]]]:
    """使用 httpx 检查前端路由（仅 HTTP 200 检查，无 JS 错误检测）。

    Returns:
        (passed, total, failures)
    """
    passed = 0
    failures: list[tuple[str, int, str]] = []
    total = len(routes)

    with httpx.Client(
        base_url=frontend_url, timeout=timeout, follow_redirects=True
    ) as client:
        for route in routes:
            try:
                r = client.get(route)
                if r.status_code == 200:
                    passed += 1
                    print(f"  ✅ PASS  {route:40s}  HTTP {r.status_code}")
                else:
                    body = r.text[:200].replace("\n", " ")
                    failures.append(
                        (route, r.status_code, f"HTTP {r.status_code} body={body}")
                    )
                    print(f"  ❌ FAIL  {route:40s}  HTTP {r.status_code} body={body}")
            except Exception as e:
                failures.append(
                    (route, 0, f"exception: {type(e).__name__}: {e}")
                )
                print(f"  ❌ FAIL  {route:40s}  exception: {e}")
    return passed, total, failures


def run_frontend_checks(
    frontend_url: str,
    timeout: float,
) -> tuple[int, int, list[tuple[str, int, str]]]:
    """执行前端路由遍历检查。

    Returns:
        (passed, total, failures)
    """
    print("\n[前端路由检查]")
    print(f"  目标: {frontend_url}")

    # 1. 获取前端路由列表（优先解析 router/index.ts，兜底用硬编码）
    routes = _parse_frontend_routes_from_file()
    if routes:
        print(f"  从 web/src/router/index.ts 解析到 {len(routes)} 个路由")
    else:
        routes = list(_DEFAULT_FRONTEND_ROUTES)
        print(f"  使用硬编码路由列表：{len(routes)} 个路由（router/index.ts 解析失败）")

    if not routes:
        print("  ⚠️  未发现前端路由，跳过前端检查")
        return 0, 0, []

    # 2. 尝试用 Playwright，失败则降级到 httpx
    print("  尝试使用 Playwright 检查（含 JS 控制台错误检测）...")
    result = _check_frontend_with_playwright(frontend_url, routes, timeout)
    if result[0] == -1:
        print("  Playwright 不可用，降级为 httpx HTTP 200 检查")
        return _check_frontend_with_httpx(frontend_url, routes, timeout)
    return result


# ── CLI 入口 ────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description="EdgeLite 验收门禁检查 — 遍历所有 API 端点和前端路由",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("EDGELITE_BASE_URL", "http://127.0.0.1:8080"),
        help="后端 API 基础 URL (默认: http://127.0.0.1:8080)",
    )
    parser.add_argument(
        "--frontend-url",
        default=os.environ.get("EDGELITE_FRONTEND_URL", "http://127.0.0.1:5173"),
        help="前端 dev server URL (默认: http://127.0.0.1:5173)",
    )
    parser.add_argument(
        "--user",
        default="admin",
        help="登录用户名 (默认: admin)",
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("EDGELITE_ADMIN_PASSWORD"),
        help="登录密码 (默认: 从 EDGELITE_ADMIN_PASSWORD 环境变量读取)",
    )
    parser.add_argument(
        "--skip-frontend",
        action="store_true",
        help="跳过前端路由检查",
    )
    parser.add_argument(
        "--skip-api",
        action="store_true",
        help="跳过 API 端点检查",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="单请求超时秒数 (默认: 10)",
    )
    args = parser.parse_args()

    # 校验密码
    if not args.skip_api and not args.password:
        print("❌ 未提供密码。请通过 --password 参数或 EDGELITE_ADMIN_PASSWORD 环境变量设置")
        print("   示例: EDGELITE_ADMIN_PASSWORD=mysecret python scripts/acceptance_check.py")
        return 3

    # 打印头部
    print("\n" + "=" * 60)
    print("  EdgeLite 验收门禁检查")
    print("=" * 60)
    print(f"  后端: {args.base_url}")
    if not args.skip_frontend:
        print(f"  前端: {args.frontend_url}")
    print(f"  用户: {args.user}")
    print(f"  超时: {args.timeout}s")

    # 1. 健康检查（确认后端已启动，不自动启动后端）
    print("\n[健康检查]")
    try:
        with httpx.Client(base_url=args.base_url, timeout=5.0) as client:
            if not _wait_for_health(client, attempts=3, interval=1.0):
                print(f"  ❌ 后端健康检查失败: {args.base_url}/health/live 不可达")
                print("  请先启动后端服务：")
                print("    python main.py")
                print("  或通过 Docker：")
                print("    docker-compose up -d")
                return 2
            print("  ✅ 后端健康检查通过: /health/live → {'status':'ok'}")
    except Exception as e:
        print(f"  ❌ 无法连接后端: {type(e).__name__}: {e}")
        print("  请先启动后端服务：python main.py")
        return 2

    # 2. API 端点检查
    api_passed = 0
    api_total = 0
    api_failures: list[tuple[str, str, int, str]] = []
    if not args.skip_api:
        api_passed, api_total, api_failures = run_api_checks(
            args.base_url, args.user, args.password or "", args.timeout
        )
    else:
        print("\n[API 端点检查] 已跳过 (--skip-api)")

    # 3. 前端路由检查
    fe_passed = 0
    fe_total = 0
    fe_failures: list[tuple[str, int, str]] = []
    if not args.skip_frontend:
        fe_passed, fe_total, fe_failures = run_frontend_checks(
            args.frontend_url, args.timeout
        )
    else:
        print("\n[前端路由检查] 已跳过 (--skip-frontend)")

    # 4. 汇总报告
    print("\n" + "=" * 60)
    print("  汇总")
    print("=" * 60)

    api_pct = (api_passed / api_total * 100) if api_total > 0 else 100.0
    fe_pct = (fe_passed / fe_total * 100) if fe_total > 0 else 100.0

    api_status = "✅ PASS" if not api_failures else "❌ FAIL"
    fe_status = "✅ PASS" if not fe_failures else "❌ FAIL"

    print(
        f"  API  {api_passed}/{api_total} 通过 ({api_pct:.1f}%)  {api_status}"
    )
    print(f"  前端 {fe_passed}/{fe_total} 通过 ({fe_pct:.1f}%)  {fe_status}")

    # 失败详情（最多 30 条）
    if api_failures:
        print("\n  API 失败详情:")
        for method, path, status, detail in api_failures[:30]:
            print(f"    {method:4s} {path}")
            print(f"          {detail}")
        if len(api_failures) > 30:
            print(f"    ... 还有 {len(api_failures) - 30} 个")

    if fe_failures:
        print("\n  前端失败详情:")
        for route, status, detail in fe_failures[:30]:
            print(f"    {route}")
            print(f"          {detail}")
        if len(fe_failures) > 30:
            print(f"    ... 还有 {len(fe_failures) - 30} 个")

    # 总体结论
    overall_pass = not api_failures and not fe_failures
    print("\n" + "=" * 60)
    if overall_pass:
        print("  总体: ✅ PASSED")
    else:
        print("  总体: ❌ FAILED")
    print("=" * 60 + "\n")

    return 0 if overall_pass else 1


if __name__ == "__main__":
    sys.exit(main())
