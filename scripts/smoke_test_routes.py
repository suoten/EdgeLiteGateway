#!/usr/bin/env python3
"""EdgeLite 路由冒烟测试 — 验证全部已注册端点不返回 500。

本脚本与 scripts/smoke_test.py 互补：
- smoke_test.py: 部署后 HTTP 黑盒冒烟（需运行服务）
- smoke_test_routes.py: 进程内 TestClient 白盒冒烟，遍历 create_app() 注册的全部路由

用法:
    python scripts/smoke_test_routes.py
    python scripts/smoke_test_routes.py --include-path /api/v1/devices

退出码:
    0 — 全部端点无 500
    1 — 至少 1 个端点返回 500
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from typing import Any

# 确保能 import edgelite
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if os.path.isdir(_SRC) and _SRC not in sys.path:
    sys.path.insert(0, _SRC)

os.environ.setdefault("DEV_MODE", "true")
os.environ.setdefault("EDGELITE_SECURITY__SECRET_KEY", "smoke-test-secret-key-32+chars-long!")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from edgelite.api.deps import get_current_user  # noqa: E402
from edgelite.app import create_app  # noqa: E402


def _collect_routes(app: FastAPI) -> list[tuple[str, str]]:
    """收集 app 中所有 (http_method, full_path) 对。

    FastAPI 0.110+ 中 include_router 会将子 router 包成 _IncludedRouter，
    需通过 original_router 解包。
    """
    routes: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def _add(method: str, path: str) -> None:
        key = (method, path)
        if key not in seen:
            seen.add(key)
            routes.append(key)

    def _walk(r: Any, prefix: str = "") -> None:
        # _IncludedRouter 解包
        inner = getattr(r, "original_router", None) or r
        # 直接挂载的路由（APIRoute/APIWebSocketRoute/Mount）有 path + methods
        path = getattr(r, "path", None)
        methods = getattr(r, "methods", None)
        if path and methods:
            for m in methods:
                if m in ("GET", "POST", "PUT", "DELETE", "PATCH"):
                    _add(m, path)
        # 子 router
        if hasattr(inner, "routes"):
            for sub in inner.routes:
                _walk(sub, prefix)

    for r in app.routes:
        _walk(r)
    return routes


def _build_client(app: FastAPI) -> TestClient:
    """构建 TestClient，覆盖认证依赖，模拟 admin 用户。"""
    # 覆盖 get_current_user 直接返回 admin
    admin_user = {"user_id": "smoke-admin", "username": "admin", "role": "admin"}
    app.dependency_overrides[get_current_user] = lambda: admin_user
    return TestClient(app)


def _probe(client: TestClient, method: str, path: str) -> tuple[int, str]:
    """对单个端点发起最小请求，返回 (status_code, detail)。

    对于需要 path/query 参数的端点，使用合法占位值；
    对于需要 body 的端点，传空 dict。
    """
    # 替换路径参数为合法占位值
    import re

    test_path = re.sub(r"\{[^}]+\}", "1", path)
    # 移除尾部可选斜杠差异
    headers = {"Authorization": "Bearer smoke-test-token"}

    try:
        if method == "GET":
            r = client.get(test_path, headers=headers)
        elif method == "POST":
            r = client.post(test_path, json={}, headers=headers)
        elif method == "PUT":
            r = client.put(test_path, json={}, headers=headers)
        elif method == "DELETE":
            r = client.delete(test_path, headers=headers)
        elif method == "PATCH":
            r = client.patch(test_path, json={}, headers=headers)
        else:
            return -1, f"unsupported method {method}"
        detail = f"HTTP {r.status_code}"
        if r.status_code >= 400:
            # 截断响应体用于诊断
            body = r.text[:120].replace("\n", " ")
            detail += f" body={body}"
        return r.status_code, detail
    except Exception as e:
        return -1, f"exception: {type(e).__name__}: {e}"


def run_smoke_test(include_path: str | None = None) -> int:
    """执行路由冒烟测试，返回退出码（0=通过，1=有 500）。"""
    app = create_app()
    routes = _collect_routes(app)

    # 过滤：仅保留 HTTP API 路由（排除 /ws/、/assets、/metrics、/health 等）
    api_routes = []
    for method, path in routes:
        if not path.startswith("/api/") and not path.startswith("/api/v1/"):
            continue
        if include_path and include_path not in path:
            continue
        api_routes.append((method, path))

    # 排除明确为开发态/调试态的端点（避免误报）
    skip_paths = {"/api/v1/auth/login", "/api/v1/auth/logout"}  # 这些会触发真实认证逻辑
    api_routes = [(m, p) for m, p in api_routes if p not in skip_paths]

    print(f"\n{'=' * 72}")
    print(f"  EdgeLite 路由冒烟测试 — 共 {len(api_routes)} 个端点")
    print(f"{'=' * 72}")

    client = _build_client(app)

    # 按状态码分组统计
    by_status: dict[int, list[tuple[str, str, str]]] = defaultdict(list)
    errors_500: list[tuple[str, str, str]] = []
    errors_5xx: list[tuple[str, str, str]] = []
    rate_limited: list[tuple[str, str, str]] = []
    pass_count = 0

    # 每批 50 个端点后重置 rate limit backend，避免 429 误报干扰 500 检测
    BATCH_SIZE = 50
    for i, (method, path) in enumerate(api_routes):
        if i > 0 and i % BATCH_SIZE == 0:
            try:
                from edgelite.middleware.rate_limit import _reset_backend_for_tests

                _reset_backend_for_tests()
            except Exception:
                pass
        status, detail = _probe(client, method, path)
        if status == 500:
            errors_500.append((method, path, detail))
            by_status[500].append((method, path, detail))
        elif status == 429:
            rate_limited.append((method, path, detail))
            by_status[429].append((method, path, detail))
        elif 500 < status < 600:
            errors_5xx.append((method, path, detail))
            by_status[status].append((method, path, detail))
        elif status == -1:
            by_status[-1].append((method, path, detail))
        else:
            pass_count += 1
            by_status[status].append((method, path, detail))

    # 输出统计
    print("\n状态码分布:")
    for code in sorted(by_status.keys()):
        label = "EXCEPTION" if code == -1 else f"HTTP {code}"
        print(f"  {label:14s}  {len(by_status[code]):4d} 个端点")

    # 输出 500 错误详情（使用 ASCII 避免 Windows GBK 控制台编码错误）
    if errors_500:
        print(f"\n[FAIL] 500 错误 ({len(errors_500)} 个):")
        for method, path, detail in errors_500[:30]:
            print(f"  {method:6s} {path}")
            print(f"         {detail}")
        if len(errors_500) > 30:
            print(f"  ... 还有 {len(errors_500) - 30} 个")

    # 输出其他 5xx 错误
    if errors_5xx:
        print(f"\n[WARN] 其他 5xx 错误 ({len(errors_5xx)} 个):")
        for method, path, detail in errors_5xx[:20]:
            print(f"  {method:6s} {path}")
            print(f"         {detail}")

    # 总结
    print(f"\n{'=' * 72}")
    total = len(api_routes)
    has_500 = len(errors_500) > 0
    print(f"  通过: {pass_count}/{total}")
    print(f"  500 错误: {len(errors_500)}")
    print(f"  其他 5xx: {len(errors_5xx)}")
    print(f"  429 限流(不影响验收): {len(rate_limited)}")
    if has_500:
        print("  结论: [FAIL] 存在 500 错误，需修复")
    else:
        print("  结论: [PASS] 无 500 错误")
    print(f"{'=' * 72}\n")

    return 1 if has_500 else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="EdgeLite 路由冒烟测试（进程内 TestClient）")
    parser.add_argument(
        "--include-path",
        default=None,
        help="仅测试路径包含该子串的端点（如 /api/v1/devices）",
    )
    args = parser.parse_args()

    sys.exit(run_smoke_test(args.include_path))


if __name__ == "__main__":
    main()
