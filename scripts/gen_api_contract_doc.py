#!/usr/bin/env python3
"""根据 check_api_contract.py 输出的 JSON 报告生成 docs/api_contract.md。

用法：
    python scripts/check_api_contract.py --json > scripts/contract_report.json
    python scripts/gen_api_contract_doc.py
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
REPORT_FILE = ROOT / "scripts" / "contract_report.json"
OUTPUT_FILE = ROOT / "docs" / "api_contract.md"

METHOD_ORDER = {"GET": 1, "POST": 2, "PUT": 3, "DELETE": 4, "PATCH": 5, "WS": 6}


def normalize_method(m: str) -> str:
    return m.upper() if m else "?"


def method_emoji(m: str) -> str:
    # ASCII only per user requirement
    return {
        "GET": "GET",
        "POST": "POST",
        "PUT": "PUT",
        "DELETE": "DEL",
        "PATCH": "PATCH",
        "WS": "WS",
    }.get(m.upper(), m.upper())


def main() -> int:
    if not REPORT_FILE.exists():
        print(f"[FAIL] report not found: {REPORT_FILE}")
        print("       run `python scripts/check_api_contract.py --json > scripts/contract_report.json` first")
        return 1

    data: dict[str, Any] = json.loads(REPORT_FILE.read_text(encoding="utf-8"))

    backend_routes: list[dict] = data.get("backend_routes", [])
    backend_ws: list[dict] = data.get("backend_ws_routes", [])
    frontend_calls: list[dict] = data.get("frontend_calls", [])
    frontend_ws: list[dict] = data.get("frontend_ws_calls", [])
    frontend_404: list[dict] = data.get("frontend_404", [])
    backend_dead: list[dict] = data.get("backend_dead", [])
    undefined_consts: list[dict] = data.get("undefined_url_constants", [])

    # 构造后端索引：(method, normalized_path) -> route
    backend_index: dict[tuple[str, str], dict] = {}
    for r in backend_routes:
        key = (r["method"].upper(), r["path"])
        if key not in backend_index:
            backend_index[key] = r

    # 构造前端调用索引：(method, normalized_path) -> list of calls
    frontend_index: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for c in frontend_calls:
        key = (c["method"].upper(), c["path"])
        frontend_index[key].append(c)

    # 按模块分组后端路由
    by_module: dict[str, list[dict]] = defaultdict(list)
    for r in backend_routes:
        by_module[r.get("module", "?")].append(r)
    for routes in by_module.values():
        routes.sort(key=lambda r: (r["path"], METHOD_ORDER.get(r["method"].upper(), 9)))

    # 按前端 api_name 分组前端调用
    by_api: dict[str, list[dict]] = defaultdict(list)
    for c in frontend_calls:
        by_api[c.get("api_name", "?")].append(c)
    for calls in by_api.values():
        calls.sort(key=lambda c: (c["path"], METHOD_ORDER.get(c["method"].upper(), 9)))

    # WebSocket 路由
    ws_backend_index: dict[tuple[str, str], dict] = {}
    for r in backend_ws:
        ws_backend_index[(r["method"].upper(), r["path"])] = r

    lines: list[str] = []
    lines.append("# EdgeLite API 契约清单")
    lines.append("")
    lines.append("> 本文档由 `scripts/check_api_contract.py` 与 `scripts/gen_api_contract_doc.py` 自动生成。")
    lines.append("> 任何对 `src/edgelite/api/*.py` 或 `web/src/api/*.ts` 的修改都应同步更新本文档。")
    lines.append("> CI 中可通过 `python scripts/check_api_contract.py` 自动校验前后端契约一致性。")
    lines.append("")
    lines.append("---")
    lines.append("")

    # ── 概览 ──────────────────────────────────────────────────────────────
    lines.append("## 1. 概览")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("|------|------|")
    lines.append(f"| 后端 HTTP 路由数 | {len(backend_routes)} |")
    lines.append(f"| 后端 WebSocket 路由数 | {len(backend_ws)} |")
    lines.append(f"| 前端 HTTP 调用数 | {len(frontend_calls)} |")
    lines.append(f"| 前端 WebSocket 调用数 | {len(frontend_ws)} |")
    lines.append(f"| 404 风险（前端调用但后端无路由） | {len(frontend_404)} |")
    lines.append(f"| Dead Code 警告（后端有路由但前端未调用） | {len(backend_dead)} |")
    lines.append(f"| 未定义 URL 常量 | {len(undefined_consts)} |")
    lines.append("")
    lines.append("**响应格式约定**：除特殊说明外，所有端点统一返回 `{code, message, data}` 结构，")
    lines.append("其中 `code` 为业务错误码（0 表示成功），`message` 为可读消息，`data` 为业务数据。")
    lines.append("分页端点返回 `{code, message, data, total, page, size}`。")
    lines.append("")
    lines.append("**路径参数规范化**：本文档使用 `{var}` 表示路径参数位置。例如 `/api/v1/devices/{device_id}` ")
    lines.append("在前端可能写作 `/api/v1/devices/123`、`/api/v1/devices/${id}` 或 `/api/v1/devices/${encodeURIComponent(id)}`。")
    lines.append("")

    # ── 后端路由清单 ──────────────────────────────────────────────────────
    lines.append("---")
    lines.append("")
    lines.append("## 2. 后端路由清单（按模块分组）")
    lines.append("")
    lines.append("下表列出 `src/edgelite/api/*.py` 中所有 `@router.METHOD(...)` 装饰器定义的路由。")
    lines.append("")
    lines.append("**列说明**：")
    lines.append("- `Method` HTTP 方法")
    lines.append("- `Path` 完整路径（已拼接 `APIRouter(prefix=...)`）")
    lines.append("- `Function` 路由处理函数名")
    lines.append("- `Response` `response_model`（如有）")
    lines.append("- `Frontend` 前端是否调用：`[OK]` 已调用、`[WARN]` 未调用（potential dead code）")
    lines.append("")

    for module in sorted(by_module.keys()):
        routes = by_module[module]
        lines.append(f"### 2.{list(sorted(by_module.keys())).index(module) + 1} 模块 `{module}`")
        lines.append("")
        lines.append(f"文件：`{routes[0].get('file', '?')}`")
        lines.append("")
        lines.append("| Method | Path | Function | Response | Frontend |")
        lines.append("|--------|------|----------|----------|----------|")
        for r in routes:
            method = normalize_method(r.get("method", ""))
            path = r.get("path", "?")
            fn = r.get("function", "?")
            resp = r.get("response_model", "") or "-"
            key = (method, path)
            called = key in frontend_index
            mark = "[OK]" if called else "[WARN]"
            lines.append(f"| {method} | `{path}` | `{fn}` | `{resp}` | {mark} |")
        lines.append("")

    # ── 前端调用清单 ──────────────────────────────────────────────────────
    lines.append("---")
    lines.append("")
    lines.append("## 3. 前端调用清单（按 api_name 分组）")
    lines.append("")
    lines.append("下表列出 `web/src/api/*.ts` 中所有 `http.METHOD(...)` 调用（含 POLL_API_MAP 轮询降级路径）。")
    lines.append("URL 常量引用（如 `URL.OTA.CHECK`）已通过常量传播解析为完整路径。")
    lines.append("")
    lines.append("**列说明**：")
    lines.append("- `Method` HTTP 方法")
    lines.append("- `Path` 完整路径（已拼接 baseURL `/api/v1`）")
    lines.append("- `Raw URL` 前端源码中的 URL 表达式")
    lines.append("- `Source` 调用位置（文件:行号）")
    lines.append("- `Backend` 后端是否实现：`[OK]` 已实现、`[FAIL]` 未实现（404 风险）")
    lines.append("")

    api_names = sorted(by_api.keys())
    for i, api_name in enumerate(api_names, 1):
        calls = by_api[api_name]
        lines.append(f"### 3.{i} API `{api_name}`")
        lines.append("")
        lines.append("| Method | Path | Raw URL | Source | Backend |")
        lines.append("|--------|------|---------|--------|---------|")
        for c in calls:
            method = normalize_method(c.get("method", ""))
            path = c.get("path", "?")
            raw = c.get("raw_url", "?")
            src = f"{c.get('file', '?')}:{c.get('line', '?')}"
            key = (method, path)
            implemented = key in backend_index
            mark = "[OK]" if implemented else "[FAIL]"
            # 转义 raw url 中的反引号和管道符
            raw_escaped = str(raw).replace("|", "\\|").replace("`", "\\`")
            lines.append(f"| {method} | `{path}` | `{raw_escaped}` | `{src}` | {mark} |")
        lines.append("")

    # ── WebSocket 路由 ────────────────────────────────────────────────────
    lines.append("---")
    lines.append("")
    lines.append("## 4. WebSocket 路由")
    lines.append("")
    lines.append("### 4.1 后端 WebSocket 路由")
    lines.append("")
    if backend_ws:
        lines.append("| Path | Function | File | Frontend |")
        lines.append("|------|----------|------|----------|")
        for r in backend_ws:
            path = r.get("path", "?")
            fn = r.get("function", "?")
            f = r.get("file", "?")
            called = any(w.get("path") == path for w in frontend_ws)
            mark = "[OK]" if called else "[WARN]"
            lines.append(f"| `{path}` | `{fn}` | `{f}` | {mark} |")
        lines.append("")
    else:
        lines.append("（无）")
        lines.append("")

    lines.append("### 4.2 前端 WebSocket 调用")
    lines.append("")
    if frontend_ws:
        lines.append("| Path | Source | Backend |")
        lines.append("|------|--------|---------|")
        for c in frontend_ws:
            path = c.get("path", "?")
            src = f"{c.get('file', '?')}:{c.get('line', '?')}"
            implemented = any(r.get("path") == path for r in backend_ws)
            mark = "[OK]" if implemented else "[FAIL]"
            lines.append(f"| `{path}` | `{src}` | {mark} |")
        lines.append("")
    else:
        lines.append("（无）")
        lines.append("")

    # ── 不匹配项 ─────────────────────────────────────────────────────────
    lines.append("---")
    lines.append("")
    lines.append("## 5. 契约不匹配项")
    lines.append("")

    lines.append("### 5.1 404 风险（前端调用但后端无路由）")
    lines.append("")
    if frontend_404:
        lines.append("| Method | Frontend Path | Source | Raw URL |")
        lines.append("|--------|---------------|--------|---------|")
        for c in frontend_404:
            method = normalize_method(c.get("method", ""))
            path = c.get("path", "?")
            src = f"{c.get('file', '?')}:{c.get('line', '?')}"
            raw = str(c.get("raw_url", "?")).replace("|", "\\|")
            lines.append(f"| {method} | `{path}` | `{src}` | `{raw}` |")
        lines.append("")
    else:
        lines.append("[PASS] 前端所有调用都有对应的后端路由。")
        lines.append("")

    lines.append("### 5.2 Dead Code 警告（后端有路由但前端未调用）")
    lines.append("")
    if backend_dead:
        lines.append("> 注意：dead code 仅是 warning，不阻断 CI。某些端点可能由外部系统调用或保留以备未来使用。")
        lines.append("")
        lines.append("| Method | Backend Path | Module | Function |")
        lines.append("|--------|--------------|--------|----------|")
        for r in backend_dead:
            method = normalize_method(r.get("method", ""))
            path = r.get("path", "?")
            mod = r.get("module", "?")
            fn = r.get("function", "?")
            lines.append(f"| {method} | `{path}` | `{mod}` | `{fn}` |")
        lines.append("")
    else:
        lines.append("[PASS] 后端所有路由都被前端调用。")
        lines.append("")

    lines.append("### 5.3 未定义 URL 常量")
    lines.append("")
    if undefined_consts:
        lines.append("| Method | Path | Source | Raw URL |")
        lines.append("|--------|------|--------|---------|")
        for c in undefined_consts:
            method = normalize_method(c.get("method", ""))
            path = c.get("path", "?")
            src = f"{c.get('file', '?')}:{c.get('line', '?')}"
            raw = str(c.get("raw_url", "?")).replace("|", "\\|")
            lines.append(f"| {method} | `{path}` | `{src}` | `{raw}` |")
        lines.append("")
    else:
        lines.append("[PASS] 前端所有 URL 常量引用都已定义。")
        lines.append("")

    # ── CI 集成说明 ──────────────────────────────────────────────────────
    lines.append("---")
    lines.append("")
    lines.append("## 6. CI 集成")
    lines.append("")
    lines.append("项目已集成 `scripts/check_api_contract.py` 作为 CI 校验步骤：")
    lines.append("")
    lines.append("- **GitHub Actions**：`.github/workflows/ci.yml` 的 `lint` job 中运行 `python scripts/check_api_contract.py`")
    lines.append("- **GitLab CI**：`.gitlab-ci.yml` 的 `lint` stage 中运行 `python scripts/check_api_contract.py`")
    lines.append("")
    lines.append("**退出码**：")
    lines.append("- `0` 契约对齐（或仅有 dead code warning）")
    lines.append("- `1` 存在 404 风险或未定义 URL 常量")
    lines.append("")
    lines.append("**重新生成本文档**：")
    lines.append("")
    lines.append("```bash")
    lines.append("python scripts/check_api_contract.py --json > scripts/contract_report.json")
    lines.append("python scripts/gen_api_contract_doc.py")
    lines.append("```")
    lines.append("")

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text("\n".join(lines), encoding="utf-8")
    print(f"[PASS] generated {OUTPUT_FILE} ({OUTPUT_FILE.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
