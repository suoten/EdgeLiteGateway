#!/usr/bin/env python3
"""EdgeLite 冒烟测试 — 部署后核心流程验证。

用法:
    python scripts/smoke_test.py --base-url http://localhost:8080
    python scripts/smoke_test.py --base-url https://api.example.com --user admin --pass secret

退出码:
    0 — 所有冒烟测试通过
    1 — 一个或多个测试失败
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import httpx


def smoke_test(base_url: str, username: str, password: str) -> bool:
    """执行冒烟测试套件，返回是否全部通过。"""
    results: list[tuple[str, bool, str]] = []
    client = httpx.Client(base_url=base_url, timeout=10.0)

    # ── 1. Liveness 健康检查 ──────────────────────────────────────────
    try:
        r = client.get("/health/live")
        ok = r.status_code == 200 and r.json().get("status") == "ok"
        results.append(("health/live", ok, f"HTTP {r.status_code}"))
    except Exception as e:
        results.append(("health/live", False, str(e)))

    # ── 2. Readiness 健康检查 ─────────────────────────────────────────
    try:
        r = client.get("/health")
        ok = r.status_code == 200
        results.append(("health/ready", ok, f"HTTP {r.status_code}"))
    except Exception as e:
        results.append(("health/ready", False, str(e)))

    # ── 3. 登录获取 Token ─────────────────────────────────────────────
    token = None
    try:
        r = client.post(
            "/api/auth/login",
            json={"username": username, "password": password},
        )
        if r.status_code == 200:
            data = r.json()
            token = data.get("data", {}).get("access_token") or data.get("access_token")
            ok = token is not None
            results.append(("auth/login", ok, f"HTTP {r.status_code} token={'yes' if token else 'no'}"))
        else:
            results.append(("auth/login", False, f"HTTP {r.status_code}: {r.text[:100]}"))
    except Exception as e:
        results.append(("auth/login", False, str(e)))

    # ── 4. 设备列表（需要认证）─────────────────────────────────────────
    if token:
        headers = {"Authorization": f"Bearer {token}"}
        try:
            r = client.get("/api/devices", headers=headers)
            ok = r.status_code == 200
            results.append(("GET /api/devices", ok, f"HTTP {r.status_code}"))
        except Exception as e:
            results.append(("GET /api/devices", False, str(e)))

        # ── 5. 系统信息 ───────────────────────────────────────────────
        try:
            r = client.get("/api/system/info", headers=headers)
            ok = r.status_code == 200
            results.append(("GET /api/system/info", ok, f"HTTP {r.status_code}"))
        except Exception as e:
            results.append(("GET /api/system/info", False, str(e)))

        # ── 6. 指标端点 (Prometheus) ──────────────────────────────────
        try:
            r = client.get("/metrics")
            ok = r.status_code == 200 and "# HELP" in r.text
            results.append(("GET /metrics", ok, f"HTTP {r.status_code} len={len(r.text)}"))
        except Exception as e:
            results.append(("GET /metrics", False, str(e)))
    else:
        for endpoint in ["GET /api/devices", "GET /api/system/info", "GET /metrics"]:
            results.append((endpoint, False, "skipped: no auth token"))

    client.close()

    # ── 输出结果 ──────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print(f"  EdgeLite Smoke Test — {base_url}")
    print("=" * 60)
    all_pass = True
    for name, ok, detail in results:
        status = "✅ PASS" if ok else "❌ FAIL"
        print(f"  {status}  {name:30s}  {detail}")
        if not ok:
            all_pass = False
    print("=" * 60)
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"  结果: {passed}/{total} 通过")
    print("=" * 60 + "\n")
    return all_pass


def main():
    parser = argparse.ArgumentParser(description="EdgeLite 冒烟测试")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("EDGELITE_TEST_BASE", "http://127.0.0.1:8080"),
        help="EdgeLite 基础 URL (默认: http://127.0.0.1:8080)",
    )
    parser.add_argument(
        "--user",
        default=os.environ.get("EDGELITE_TEST_USER", "admin"),
        help="测试用户名 (默认: admin)",
    )
    parser.add_argument(
        "--pass",
        dest="password",
        default=os.environ.get("EDGELITE_TEST_PASS", "admin"),
        help="测试密码 (默认: admin)",
    )
    parser.add_argument(
        "--retry",
        type=int,
        default=3,
        help="重试次数（服务可能还在启动中）",
    )
    args = parser.parse_args()

    for attempt in range(1, args.retry + 1):
        print(f"\n🧪 冒烟测试尝试 {attempt}/{args.retry}...")
        if smoke_test(args.base_url, args.user, args.password):
            sys.exit(0)
        if attempt < args.retry:
            print("⏳ 等待 10 秒后重试...")
            time.sleep(10)

    sys.exit(1)


if __name__ == "__main__":
    main()
