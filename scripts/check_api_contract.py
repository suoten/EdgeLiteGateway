#!/usr/bin/env python3
"""EdgeLite 前后端 API 契约静态校验脚本。

本脚本通过纯静态分析（不依赖网络/不启动服务）比对后端 FastAPI 路由
与前端 TypeScript http 调用，输出三类不匹配报告：
  1. 前端调用但后端不存在（404 风险）— exit code 1
  2. 后端存在但前端未调用（potential dead code）— 仅 warning
  3. 前端使用了未定义的 URL 常量 — exit code 1

================================================================================
用法
================================================================================
    python scripts/check_api_contract.py
    python scripts/check_api_contract.py --json    # 输出 JSON 而非 Markdown
    python scripts/check_api_contract.py --root /path/to/repo

退出码：
    0 = 通过（前后端契约对齐，或仅有 warning）
    1 = 有 404 风险（前端调用了后端不存在的端点）或前端使用了未定义的 URL 常量

================================================================================
CI 集成示例
================================================================================
GitHub Actions (.github/workflows/ci.yml):
    - name: API contract check
      run: python scripts/check_api_contract.py

GitLab CI (.gitlab-ci.yml):
    api:contract:
      stage: lint
      image: python:3.11-slim
      script:
        - python scripts/check_api_contract.py

================================================================================
路径规范化规则
================================================================================
- 后端 `/api/v1/devices/{device_id}` 与前端 `/api/v1/devices/123`、
  `/api/v1/devices/${id}`、`/api/v1/devices/${encodeURIComponent(id)}`
  均视为匹配（路径参数位置统一替换为 `{var}`）。
- 白名单（基础设施路径，不参与契约比对）：
  /health, /health/*, /live, /ready, /metrics, /metrics.json,
  /openapi.json, /docs, /redoc, /docs/oauth2-redirect
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# ── Windows GBK 控制台兼容：强制 stdout/stderr 为 UTF-8 ────────────────────
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════
# 常量
# ═══════════════════════════════════════════════════════════════════════════

# 后端 API 目录（相对于项目根目录）
BACKEND_API_DIR = "src/edgelite/api"

# 后端 app.py 路径（用于解析 WebSocket 路由）
BACKEND_APP_FILE = "src/edgelite/app.py"

# 前端 API 目录（相对于项目根目录）
FRONTEND_API_DIR = "web/src/api"

# 前端 URL 常量定义文件
FRONTEND_URL_CONST_FILE = "web/src/api/index.ts"

# 前端 baseURL（用于将前端相对路径解析为完整路径）
FRONTEND_BASE_URL = "/api/v1"

# 基础设施路径白名单（不参与契约比对）
WHITELIST_PATTERNS = [
    re.compile(r"^/health(/.*)?$"),
    re.compile(r"^/live$"),
    re.compile(r"^/ready$"),
    # 同时匹配根级 /metrics 与带 /api/v1 前缀的 /api/v1/metrics
    re.compile(r"^(/api/v1)?/metrics(\.json)?$"),
    re.compile(r"^/openapi\.json$"),
    re.compile(r"^/docs(/.*)?$"),
    re.compile(r"^/redoc(/.*)?$"),
]

# WebSocket 路径白名单（也不参与 404 比对，单独列出）
WS_WHITELIST_PATTERNS = [
    re.compile(r"^/ws/v1/.+$"),
]

# HTTP 方法集合
HTTP_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH"}


# ═══════════════════════════════════════════════════════════════════════════
# 数据模型
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class BackendRoute:
    """后端路由条目。"""

    method: str  # GET/POST/PUT/DELETE/PATCH/WS
    path: str  # 完整路径，含 prefix
    response_model: str = ""  # response_model 字符串表示
    function: str = ""  # 处理函数名
    file: str = ""  # 文件相对路径
    module: str = ""  # 模块名（如 devices/auth/alarms）


@dataclass
class FrontendCall:
    """前端调用条目。"""

    method: str  # GET/POST/PUT/DELETE/PATCH
    path: str  # 完整路径
    raw_url: str  # 原始 URL 表达式（字符串字面量/URL.X.Y/模板字符串）
    api_name: str = ""  # 调用所属 API 对象名（如 deviceApi/ruleApi）
    file: str = ""  # 文件相对路径
    line: int = 0  # 行号
    unresolved: bool = False  # 是否使用了未定义的 URL 常量


@dataclass
class ContractReport:
    """契约校验报告。"""

    backend_routes: list[BackendRoute] = field(default_factory=list)
    backend_ws_routes: list[BackendRoute] = field(default_factory=list)
    frontend_calls: list[FrontendCall] = field(default_factory=list)
    frontend_ws_calls: list[FrontendCall] = field(default_factory=list)
    undefined_url_constants: list[FrontendCall] = field(default_factory=list)
    frontend_404: list[FrontendCall] = field(default_factory=list)  # 前端调用但后端不存在
    backend_dead: list[BackendRoute] = field(default_factory=list)  # 后端存在但前端未调用
    ws_unmatched: list[FrontendCall] = field(default_factory=list)  # 前端 WS 调用但后端无对应路由

    @property
    def has_errors(self) -> bool:
        return bool(self.frontend_404 or self.undefined_url_constants)

    @property
    def has_warnings(self) -> bool:
        return bool(self.backend_dead or self.ws_unmatched)


# ═══════════════════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════════════════


def is_infrastructure_path(path: str) -> bool:
    """判断路径是否属于基础设施白名单。"""
    for pat in WHITELIST_PATTERNS:
        if pat.match(path):
            return True
    return False


def is_ws_path(path: str) -> bool:
    """判断是否为 WebSocket 路径。"""
    for pat in WS_WHITELIST_PATTERNS:
        if pat.match(path):
            return True
    return False


def normalize_path(path: str) -> str:
    """路径规范化：将路径参数替换为 {var}，便于比对。

    示例：
        /api/v1/devices/123           -> /api/v1/devices/{var}
        /api/v1/devices/${id}         -> /api/v1/devices/{var}
        /api/v1/devices/${encodeURIComponent(id)} -> /api/v1/devices/{var}
        /api/v1/devices/{device_id}   -> /api/v1/devices/{var}
        /api/v1/rules/${ruleId}/test  -> /api/v1/rules/{var}/test
    """
    if not path:
        return path
    # 0) 剥离查询字符串和片段标识符（POLL_API_MAP 中的轮询路径常含 ?size=20&page=1）
    path = path.split("?", 1)[0]
    path = path.split("#", 1)[0]
    # 1) 后端 FastAPI 风格 {name}
    path = re.sub(r"\{[^}/]+\}", "{var}", path)
    # 2) 前端模板字面量 ${expr}（含可能的函数调用如 encodeURIComponent(id)）
    #    需要先用占位符替换避免嵌套 ${} 干扰
    pattern_tmpl = re.compile(r"\$\{[^}]+\}")
    # 反复替换直到稳定（理论上单层即可，防御性循环）
    prev = None
    while prev != path:
        prev = path
        path = pattern_tmpl.sub("{var}", path)
    # 3) 路径中的纯数字段
    path = re.sub(r"/\d+(?=/|$)", "/{var}", path)
    # 4) 多个连续 {var} 合并？保持原样以便位置匹配
    return path


def url_to_full_path(url: str) -> str:
    """将前端 URL 转换为完整路径（拼接 baseURL）。

    前端 baseURL 为 /api/v1，前端调用形如 http.get('/devices')
    表示实际请求 /api/v1/devices。
    """
    if not url:
        return url
    # 已经是完整路径（含 /api/v1 前缀）
    if url.startswith("/api/") or url.startswith("http://") or url.startswith("https://"):
        return url
    # 以 / 开头的相对路径，拼接 baseURL
    if url.startswith("/"):
        return FRONTEND_BASE_URL.rstrip("/") + url
    # 无前导斜杠，按 / 拼接
    return FRONTEND_BASE_URL.rstrip("/") + "/" + url


# ═══════════════════════════════════════════════════════════════════════════
# 后端路由解析（Python AST）
# ═══════════════════════════════════════════════════════════════════════════


def _extract_router_prefixes(tree: ast.Module) -> dict[str, str]:
    """从模块 AST 中提取所有 APIRouter 实例的 prefix。

    匹配 `xxx = APIRouter(prefix="...", ...)` 形式，
    返回 {变量名: prefix} 字典。
    """
    prefixes: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        if not (isinstance(func, ast.Name) and func.id == "APIRouter"):
            continue
        prefix = ""
        for kw in node.value.keywords:
            if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                prefix = kw.value.value or ""
        for target in node.targets:
            if isinstance(target, ast.Name):
                prefixes[target.id] = prefix
    return prefixes


def _decorator_route_info(
    dec: ast.expr, router_prefixes: dict[str, str]
) -> tuple[str, str, str] | None:
    """解析装饰器，返回 (method, full_path, response_model) 或 None。

    支持形式：
        @router.get("/path", response_model=ApiResponse[X])
        @_root_metrics_router.get("/metrics")
    """
    if not isinstance(dec, ast.Call):
        return None
    func = dec.func
    if not isinstance(func, ast.Attribute):
        return None
    method = func.attr.lower()
    if method not in ("get", "post", "put", "delete", "patch", "websocket"):
        return None
    if not isinstance(func.value, ast.Name):
        return None
    router_var = func.value.id
    if router_var not in router_prefixes:
        return None
    prefix = router_prefixes[router_var]
    # 第一个位置参数为路径
    if not dec.args:
        return None
    path_arg = dec.args[0]
    if isinstance(path_arg, ast.Constant):
        path = path_arg.value or ""
    else:
        return None
    response_model = ""
    for kw in dec.keywords:
        if kw.arg == "response_model":
            try:
                response_model = ast.unparse(kw.value)
            except Exception:
                response_model = "<unparseable>"
    full_path = prefix + path
    # 标准化 method
    method_upper = "WS" if method == "websocket" else method.upper()
    return method_upper, full_path, response_model


def parse_backend_file(file_path: Path, rel_path: str) -> list[BackendRoute]:
    """解析单个后端 API 文件，返回路由列表。"""
    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception as exc:
        print(f"[WARN] Cannot read {rel_path}: {exc}", file=sys.stderr)
        return []
    try:
        tree = ast.parse(content)
    except SyntaxError as exc:
        print(f"[WARN] Syntax error in {rel_path}: {exc}", file=sys.stderr)
        return []

    prefixes = _extract_router_prefixes(tree)
    module_name = file_path.stem
    routes: list[BackendRoute] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            info = _decorator_route_info(dec, prefixes)
            if info is None:
                continue
            method, full_path, response_model = info
            routes.append(
                BackendRoute(
                    method=method,
                    path=full_path,
                    response_model=response_model,
                    function=node.name,
                    file=rel_path,
                    module=module_name,
                )
            )
    return routes


def parse_backend_app_ws(file_path: Path, rel_path: str) -> list[BackendRoute]:
    """解析 app.py 中的 @app.websocket(...) 装饰器。

    app.py 使用 `@app.websocket("/ws/v1/realtime")` 形式直接注册 WebSocket
    路由（不走 APIRouter）。
    """
    if not file_path.exists():
        return []
    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception:
        return []
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return []

    routes: list[BackendRoute] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            func = dec.func
            if not isinstance(func, ast.Attribute):
                continue
            if func.attr != "websocket":
                continue
            if not dec.args:
                continue
            path_arg = dec.args[0]
            if not isinstance(path_arg, ast.Constant):
                continue
            path = path_arg.value or ""
            routes.append(
                BackendRoute(
                    method="WS",
                    path=path,
                    function=node.name,
                    file=rel_path,
                    module="app",
                )
            )
    return routes


def collect_backend_routes(root: Path) -> tuple[list[BackendRoute], list[BackendRoute]]:
    """收集所有后端路由，返回 (HTTP 路由列表, WebSocket 路由列表)。"""
    api_dir = root / BACKEND_API_DIR
    http_routes: list[BackendRoute] = []
    ws_routes: list[BackendRoute] = []

    if not api_dir.exists():
        return http_routes, ws_routes

    for py_file in sorted(api_dir.glob("*.py")):
        if py_file.name.startswith("_") or py_file.name == "deps.py":
            continue
        rel = py_file.relative_to(root).as_posix()
        routes = parse_backend_file(py_file, rel)
        for r in routes:
            if r.method == "WS":
                ws_routes.append(r)
            else:
                http_routes.append(r)

    # 解析 app.py 中的 WebSocket 路由
    app_file = root / BACKEND_APP_FILE
    if app_file.exists():
        rel = app_file.relative_to(root).as_posix()
        ws_routes.extend(parse_backend_app_ws(app_file, rel))

    return http_routes, ws_routes


# ═══════════════════════════════════════════════════════════════════════════
# 前端 URL 常量解析（TypeScript）
# ═══════════════════════════════════════════════════════════════════════════


def _find_url_const_block(content: str) -> str | None:
    """定位 `const URL = { ... }` 块并返回其内部内容（不含最外层花括号）。"""
    # 匹配 `const URL = {` 或 `const URL = {...`
    m = re.search(r"\bconst\s+URL\s*=\s*\{", content)
    if not m:
        return None
    start = m.end()  # 指向 `{` 之后第一个字符
    # 平衡花括号查找闭合
    depth = 1
    pos = start
    in_string: str | None = None
    in_template = False
    while pos < len(content) and depth > 0:
        c = content[pos]
        if in_string:
            if c == "\\":
                pos += 2
                continue
            if c == in_string:
                in_string = None
            pos += 1
            continue
        if in_template:
            if c == "\\":
                pos += 2
                continue
            if c == "`":
                in_template = False
            elif c == "$" and pos + 1 < len(content) and content[pos + 1] == "{":
                # 跳过 ${...}
                pos += 2
                inner_depth = 1
                while pos < len(content) and inner_depth > 0:
                    if content[pos] == "{":
                        inner_depth += 1
                    elif content[pos] == "}":
                        inner_depth -= 1
                    pos += 1
                continue
            pos += 1
            continue
        if c in ('"', "'"):
            in_string = c
            pos += 1
            continue
        if c == "`":
            in_template = True
            pos += 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return content[start:pos]
        pos += 1
    return None


def _parse_ts_value_to_path(value: str) -> str | None:
    """将 TS 值表达式解析为路径字符串。

    支持：
      - '字符串字面量' / "字符串字面量"
      - `模板字符串` （${expr} -> {var}）
      - (args) => `模板字符串` 或 (args) => '字符串'
    """
    value = value.strip()
    # 箭头函数
    m = re.match(r"^\([^)]*\)\s*=>\s*(.+)$", value, re.DOTALL)
    if m:
        value = m.group(1).strip()
    # 字符串字面量
    m = re.match(r"^'([^']*)'$", value)
    if m:
        return m.group(1)
    m = re.match(r'^"([^"]*)"$', value)
    if m:
        return m.group(1)
    # 模板字符串
    if value.startswith("`") and value.endswith("`"):
        inner = value[1:-1]
        # 替换 ${...} 为 {var}
        inner = re.sub(r"\$\{[^}]+\}", "{var}", inner)
        return inner
    return None


def parse_url_constants(content: str) -> dict[str, str]:
    """解析 `const URL = { ... }` 中的嵌套常量，返回扁平化字典。

    返回的 key 形如 "URL.OTA.CHECK"，value 为路径字符串（已做 {var} 规范化）。
    对于函数式常量（如 RESOURCE(type, id) => `...`），同样解析为模板字符串。
    """
    block = _find_url_const_block(content)
    if block is None:
        return {}

    result: dict[str, str] = {}

    def _walk(block_text: str, prefix_keys: list[str]) -> None:
        """递归解析嵌套对象。"""
        pos = 0
        while pos < len(block_text):
            # 跳过空白和逗号
            while pos < len(block_text) and block_text[pos] in " \t\n,":
                pos += 1
            if pos >= len(block_text):
                break
            # 读取 key
            key_match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:", block_text[pos:])
            if not key_match:
                # 跳过非键字符（可能是注释或语法不支持）
                pos += 1
                continue
            key = key_match.group(1)
            pos += key_match.end()
            # 跳过空白
            while pos < len(block_text) and block_text[pos] in " \t\n":
                pos += 1
            if pos >= len(block_text):
                break
            ch = block_text[pos]
            if ch == "{":
                # 嵌套对象，找到匹配的 }
                depth = 1
                start = pos + 1
                pos += 1
                in_str: str | None = None
                in_tmpl = False
                while pos < len(block_text) and depth > 0:
                    c = block_text[pos]
                    if in_str:
                        if c == "\\":
                            pos += 2
                            continue
                        if c == in_str:
                            in_str = None
                        pos += 1
                        continue
                    if in_tmpl:
                        if c == "\\":
                            pos += 2
                            continue
                        if c == "`":
                            in_tmpl = False
                        elif c == "$" and pos + 1 < len(block_text) and block_text[pos + 1] == "{":
                            pos += 2
                            idepth = 1
                            while pos < len(block_text) and idepth > 0:
                                if block_text[pos] == "{":
                                    idepth += 1
                                elif block_text[pos] == "}":
                                    idepth -= 1
                                pos += 1
                            continue
                        pos += 1
                        continue
                    if c in ('"', "'"):
                        in_str = c
                        pos += 1
                        continue
                    if c == "`":
                        in_tmpl = True
                        pos += 1
                        continue
                    if c == "{":
                        depth += 1
                    elif c == "}":
                        depth -= 1
                        if depth == 0:
                            inner = block_text[start:pos]
                            pos += 1
                            _walk(inner, prefix_keys + [key])
                            break
                    pos += 1
            elif ch in ('"', "'", "`"):
                # 字符串/模板字面量值
                quote = ch
                if ch == "`":
                    # 模板字符串：包含 ${...}，需特殊处理
                    start = pos + 1
                    pos += 1
                    while pos < len(block_text):
                        if block_text[pos] == "\\":
                            pos += 2
                            continue
                        if block_text[pos] == "`":
                            break
                        if block_text[pos] == "$" and pos + 1 < len(block_text) and block_text[pos + 1] == "{":
                            pos += 2
                            idepth = 1
                            while pos < len(block_text) and idepth > 0:
                                if block_text[pos] == "{":
                                    idepth += 1
                                elif block_text[pos] == "}":
                                    idepth -= 1
                                pos += 1
                            continue
                        pos += 1
                    raw = block_text[start:pos]
                    pos += 1  # 跳过闭合 `
                    full_key = ".".join(["URL"] + prefix_keys + [key])
                    normalized = re.sub(r"\$\{[^}]+\}", "{var}", raw)
                    result[full_key] = normalized
                else:
                    start = pos + 1
                    pos += 1
                    while pos < len(block_text):
                        if block_text[pos] == "\\":
                            pos += 2
                            continue
                        if block_text[pos] == quote:
                            break
                        pos += 1
                    raw = block_text[start:pos]
                    pos += 1  # 跳过闭合 quote
                    full_key = ".".join(["URL"] + prefix_keys + [key])
                    result[full_key] = raw
            elif ch == "(":
                # 箭头函数值：(args) => `...` 或 (args) => '...'
                # 先找到 => 后的内容
                depth = 1
                start = pos + 1
                pos += 1
                while pos < len(block_text) and depth > 0:
                    if block_text[pos] == "(":
                        depth += 1
                    elif block_text[pos] == ")":
                        depth -= 1
                        if depth == 0:
                            break
                    pos += 1
                pos += 1  # 跳过 )
                # 跳过空白
                while pos < len(block_text) and block_text[pos] in " \t\n":
                    pos += 1
                # 期望 =>
                if pos + 1 < len(block_text) and block_text[pos:pos + 2] == "=>":
                    pos += 2
                    while pos < len(block_text) and block_text[pos] in " \t\n":
                        pos += 1
                    if pos < len(block_text) and block_text[pos] in ('"', "'", "`"):
                        quote = block_text[pos]
                        start = pos + 1
                        pos += 1
                        while pos < len(block_text):
                            if block_text[pos] == "\\":
                                pos += 2
                                continue
                            if quote == "`" and block_text[pos] == "$" and pos + 1 < len(block_text) and block_text[pos + 1] == "{":
                                pos += 2
                                idepth = 1
                                while pos < len(block_text) and idepth > 0:
                                    if block_text[pos] == "{":
                                        idepth += 1
                                    elif block_text[pos] == "}":
                                        idepth -= 1
                                    pos += 1
                                continue
                            if block_text[pos] == quote:
                                break
                            pos += 1
                        raw = block_text[start:pos]
                        pos += 1
                        full_key = ".".join(["URL"] + prefix_keys + [key])
                        if quote == "`":
                            normalized = re.sub(r"\$\{[^}]+\}", "{var}", raw)
                        else:
                            normalized = raw
                        result[full_key] = normalized
            else:
                # 其他类型（数字/布尔/标识符引用等），跳过到下一个逗号或闭合
                while pos < len(block_text) and block_text[pos] not in ",}":
                    pos += 1

    _walk(block, [])
    return result


# ═══════════════════════════════════════════════════════════════════════════
# 前端调用解析（TypeScript）
# ═══════════════════════════════════════════════════════════════════════════


def _skip_generic(content: str, pos: int) -> int:
    """如果 pos 位置是 `<` 开始的泛型，跳过整个 `<...>`，返回新位置。"""
    if pos >= len(content) or content[pos] != "<":
        return pos
    depth = 0
    while pos < len(content):
        c = content[pos]
        if c == "<":
            depth += 1
        elif c == ">":
            depth -= 1
            if depth == 0:
                return pos + 1
        elif c in ('"', "'"):
            # 字符串内的 < > 忽略
            quote = c
            pos += 1
            while pos < len(content):
                if content[pos] == "\\":
                    pos += 2
                    continue
                if content[pos] == quote:
                    break
                pos += 1
        pos += 1
    return pos


def _skip_ws(content: str, pos: int) -> int:
    """跳过空白字符。"""
    while pos < len(content) and content[pos] in " \t\r\n":
        pos += 1
    return pos


def _extract_first_arg(content: str, paren_pos: int) -> tuple[str | None, int, int]:
    """从 `(` 位置开始提取第一个参数。

    返回 (arg_text, arg_end_pos, end_paren_pos)。
    arg_text 为 None 表示无参数。
    """
    pos = paren_pos + 1  # 跳过 (
    pos = _skip_ws(content, pos)
    if pos >= len(content) or content[pos] == ")":
        return None, pos, pos + 1
    arg_start = pos
    depth = 0
    while pos < len(content):
        c = content[pos]
        if c in "([{":
            depth += 1
            pos += 1
            continue
        if c in ")]}":
            if depth == 0:
                # 到达 ) 闭合
                return content[arg_start:pos].strip(), pos, pos + 1
            depth -= 1
            pos += 1
            continue
        if c == "," and depth == 0:
            return content[arg_start:pos].strip(), pos, pos
        if c == "`":
            # 模板字符串
            pos += 1
            while pos < len(content):
                if content[pos] == "\\":
                    pos += 2
                    continue
                if content[pos] == "`":
                    pos += 1
                    break
                if content[pos] == "$" and pos + 1 < len(content) and content[pos + 1] == "{":
                    pos += 2
                    idepth = 1
                    while pos < len(content) and idepth > 0:
                        if content[pos] == "{":
                            idepth += 1
                        elif content[pos] == "}":
                            idepth -= 1
                        pos += 1
                    continue
                pos += 1
            continue
        if c in ('"', "'"):
            quote = c
            pos += 1
            while pos < len(content):
                if content[pos] == "\\":
                    pos += 2
                    continue
                if content[pos] == quote:
                    pos += 1
                    break
                pos += 1
            continue
        # 单行注释
        if c == "/" and pos + 1 < len(content) and content[pos + 1] == "/":
            while pos < len(content) and content[pos] != "\n":
                pos += 1
            continue
        # 多行注释
        if c == "/" and pos + 1 < len(content) and content[pos + 1] == "*":
            pos += 2
            while pos + 1 < len(content):
                if content[pos] == "*" and content[pos + 1] == "/":
                    pos += 2
                    break
                pos += 1
            continue
        pos += 1
    return content[arg_start:pos].strip(), pos, pos


def _resolve_url_arg(
    arg: str, url_constants: dict[str, str]
) -> tuple[str, bool]:
    """将 URL 参数表达式解析为完整路径字符串。

    返回 (full_path, unresolved)。
    - 字符串字面量：直接返回
    - 模板字符串：拼接 baseURL，${...} -> {var}
    - URL.X.Y 常量引用：从 url_constants 查找
    - 其他形式：标记为 unresolved

    Note: 拼接 baseURL 由调用方完成；本函数仅返回 URL 常量值或字面量原文。
    """
    arg = arg.strip()
    # 1) 字符串字面量
    m = re.match(r"^'([^']*)'$", arg)
    if m:
        return m.group(1), False
    m = re.match(r'^"([^"]*)"$', arg)
    if m:
        return m.group(1), False
    # 2) 模板字符串
    if arg.startswith("`") and arg.endswith("`"):
        inner = arg[1:-1]
        inner = re.sub(r"\$\{[^}]+\}", "{var}", inner)
        return inner, False
    # 3) URL.X.Y 常量引用
    if arg.startswith("URL."):
        # 取整个标识符链（去掉可能的尾部空白）
        m = re.match(r"^(URL\.[A-Za-z_][A-Za-z0-9_.]*)", arg)
        if m:
            key = m.group(1)
            if key in url_constants:
                return url_constants[key], False
            return arg, True  # 未定义的 URL 常量
        return arg, True
    # 4) 其他形式（如变量引用、字符串拼接等）— 无法静态解析
    return arg, True


def parse_frontend_file(
    file_path: Path, rel_path: str, url_constants: dict[str, str]
) -> list[FrontendCall]:
    """解析前端 TS 文件，提取所有 http.METHOD 调用。"""
    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception as exc:
        print(f"[WARN] Cannot read {rel_path}: {exc}", file=sys.stderr)
        return []

    calls: list[FrontendCall] = []
    # 查找所有 http.METHOD 出现位置
    method_pattern = re.compile(r"\bhttp\.(get|post|put|delete|patch)\b")
    # 当前所属 API 对象名（最近一个 `export const xxxApi = {` 之前的内容）
    current_api_name = _detect_current_api(content)

    for match in method_pattern.finditer(content):
        method = match.group(1).upper()
        pos = match.end()
        pos = _skip_ws(content, pos)
        # 跳过泛型
        pos = _skip_generic(content, pos)
        pos = _skip_ws(content, pos)
        if pos >= len(content) or content[pos] != "(":
            continue
        # 提取第一个参数
        arg_text, _, _ = _extract_first_arg(content, pos)
        if arg_text is None:
            continue
        raw_url = arg_text
        path, unresolved = _resolve_url_arg(raw_url, url_constants)
        if not unresolved:
            full_path = url_to_full_path(path)
        else:
            full_path = raw_url  # 保留原始形式以便报告
        # 计算行号
        line = content.count("\n", 0, match.start()) + 1
        # 判断当前所属 API 对象
        api_name = _find_api_name_at(content, match.start(), current_api_name)
        # WebSocket 路径单独处理（虽然 http.X 不会调用 WS，但路径以 /ws/ 开头时归类）
        is_ws = is_ws_path(full_path)
        call = FrontendCall(
            method=method,
            path=full_path,
            raw_url=raw_url,
            api_name=api_name,
            file=rel_path,
            line=line,
            unresolved=unresolved,
        )
        if is_ws:
            # http 调用不应出现 ws 路径，但为防御性处理归入 WS
            calls.append(call)
        else:
            calls.append(call)
    return calls


def _detect_current_api(content: str) -> str:
    """简单返回默认 API 名（实际通过 _find_api_name_at 精确定位）。"""
    return ""


def _find_api_name_at(content: str, pos: int, default: str = "") -> str:
    """找到 pos 位置之前最近的 `export const xxxApi = {` 的 xxxApi 名。"""
    # 在 [0, pos] 范围内查找最后一个 `export const NAME = {` 或 `export const NAME = {` 形式
    prefix = content[:pos]
    matches = list(re.finditer(r"export\s+const\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\{", prefix))
    if matches:
        return matches[-1].group(1)
    # 也匹配非 export 的 const NAME = {
    matches = list(re.finditer(r"\bconst\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\{", prefix))
    if matches:
        return matches[-1].group(1)
    return default


def parse_frontend_ws_calls(content: str, rel_path: str) -> list[FrontendCall]:
    """从前端 TS 文件解析 WebSocket 通道使用。

    识别形如 `CHANNELS = { realtime: '/ws/v1/realtime', ... }` 的定义。
    返回的 FrontendCall 列表 method 为 'WS'。
    """
    calls: list[FrontendCall] = []
    # 匹配 CHANNELS 对象内的字符串值
    channels_match = re.search(r"CHANNELS\s*=\s*\{([^}]+)\}", content, re.DOTALL)
    if channels_match:
        block = channels_match.group(1)
        for m in re.finditer(r"([A-Za-z_][A-Za-z0-9_]*)\s*:\s*['\"`]([^'\"`]+)['\"`]", block):
            path = m.group(2)
            calls.append(
                FrontendCall(
                    method="WS",
                    path=path,
                    raw_url=path,
                    api_name="CHANNELS",
                    file=rel_path,
                    line=content.count("\n", 0, m.start()) + 1,
                )
            )
    # 同时识别 POLL_API_MAP 中的 /api/v1/... 路径（这些是 HTTP 调用，归入 HTTP）
    return calls


def parse_poll_api_map(content: str, rel_path: str) -> list[FrontendCall]:
    """从 POLL_API_MAP 中提取 HTTP 轮询降级路径。"""
    calls: list[FrontendCall] = []
    m = re.search(r"POLL_API_MAP\s*[^=]*=\s*\{([^}]+)\}", content, re.DOTALL)
    if not m:
        return calls
    block = m.group(1)
    for mm in re.finditer(r":\s*['\"`]([^'\"`]+)['\"`]", block):
        path = mm.group(1)
        # POLL_API_MAP 中的路径是完整路径（含 /api/v1）
        # 剥离查询字符串，避免 /api/v1/alarms?size=20 被误判为 404
        path = path.split("?", 1)[0]
        calls.append(
            FrontendCall(
                method="GET",  # 轮询都是 GET
                path=path,
                raw_url=mm.group(1),
                api_name="POLL_API_MAP",
                file=rel_path,
                line=content.count("\n", 0, mm.start()) + 1,
            )
        )
    return calls


def collect_frontend_calls(root: Path) -> tuple[list[FrontendCall], list[FrontendCall], list[FrontendCall]]:
    """收集前端调用，返回 (HTTP 调用, WebSocket 调用, 未定义 URL 常量调用)。"""
    api_dir = root / FRONTEND_API_DIR
    if not api_dir.exists():
        return [], [], []

    # 先解析 URL 常量
    url_const_file = root / FRONTEND_URL_CONST_FILE
    url_constants: dict[str, str] = {}
    if url_const_file.exists():
        try:
            url_constants = parse_url_constants(url_const_file.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[WARN] Failed to parse URL constants: {exc}", file=sys.stderr)

    http_calls: list[FrontendCall] = []
    ws_calls: list[FrontendCall] = []
    undefined_calls: list[FrontendCall] = []

    for ts_file in sorted(api_dir.glob("*.ts")):
        rel = ts_file.relative_to(root).as_posix()
        # 跳过类型定义文件
        if ts_file.name in ("env.d.ts",):
            continue
        try:
            content = ts_file.read_text(encoding="utf-8")
        except Exception:
            continue
        # 解析 http.X 调用
        calls = parse_frontend_file(ts_file, rel, url_constants)
        for c in calls:
            if c.unresolved and not is_ws_path(c.path):
                # 检查是否是 URL.X.X 形式（未定义常量）
                if c.raw_url.startswith("URL."):
                    undefined_calls.append(c)
                    continue
                # 其他形式（变量引用等）— 标记为无法解析但不计入未定义常量
                # 仍然加入 http_calls 以便后续比对时跳过
                http_calls.append(c)
                continue
            if is_ws_path(c.path):
                ws_calls.append(c)
            else:
                http_calls.append(c)
        # 解析 WebSocket CHANNELS 定义
        ws_calls.extend(parse_frontend_ws_calls(content, rel))
        # 解析 POLL_API_MAP 降级路径
        http_calls.extend(parse_poll_api_map(content, rel))

    return http_calls, ws_calls, undefined_calls


# ═══════════════════════════════════════════════════════════════════════════
# 契约比对
# ═══════════════════════════════════════════════════════════════════════════


def build_backend_index(routes: list[BackendRoute]) -> dict[tuple[str, str], BackendRoute]:
    """构建后端路由索引：key=(method, normalized_path)，value=route。"""
    index: dict[tuple[str, str], BackendRoute] = {}
    for r in routes:
        if is_infrastructure_path(r.path):
            continue
        key = (r.method, normalize_path(r.path))
        if key not in index:
            index[key] = r
    return index


def build_frontend_index(calls: list[FrontendCall]) -> dict[tuple[str, str], list[FrontendCall]]:
    """构建前端调用索引：key=(method, normalized_path)，value=call 列表。"""
    index: dict[tuple[str, str], list[FrontendCall]] = {}
    for c in calls:
        if is_infrastructure_path(c.path):
            continue
        if c.unresolved:
            continue
        key = (c.method, normalize_path(c.path))
        index.setdefault(key, []).append(c)
    return index


def compare_contracts(
    backend_routes: list[BackendRoute],
    frontend_calls: list[FrontendCall],
    undefined_calls: list[FrontendCall],
) -> tuple[list[FrontendCall], list[BackendRoute]]:
    """比对前后端契约，返回 (前端 404 风险列表, 后端 dead code 列表)。"""
    backend_idx = build_backend_index(backend_routes)
    frontend_idx = build_frontend_index(frontend_calls)

    # 前端调用但后端不存在
    frontend_404: list[FrontendCall] = []
    seen_keys: set[tuple[str, str]] = set()
    for key, calls in frontend_idx.items():
        if key not in backend_idx:
            # 检查后端是否有任意方法匹配该路径（method 不匹配但路径匹配）
            path_match = any(k[1] == key[1] for k in backend_idx.keys())
            for c in calls:
                if path_match:
                    # method 不匹配，仍视为 404（但记录路径有匹配）
                    frontend_404.append(c)
                else:
                    frontend_404.append(c)
        seen_keys.add(key)

    # 后端存在但前端未调用
    backend_dead: list[BackendRoute] = []
    for key, route in backend_idx.items():
        if key not in frontend_idx:
            backend_dead.append(route)

    return frontend_404, backend_dead


def compare_ws(
    backend_ws: list[BackendRoute], frontend_ws: list[FrontendCall]
) -> list[FrontendCall]:
    """比对 WebSocket 路由，返回前端调用但后端未定义的 WS 列表。"""
    backend_paths = {normalize_path(r.path) for r in backend_ws}
    unmatched: list[FrontendCall] = []
    seen: set[tuple[str, str]] = set()
    for c in frontend_ws:
        np = normalize_path(c.path)
        key = (c.method, np)
        if key in seen:
            continue
        seen.add(key)
        if np not in backend_paths:
            unmatched.append(c)
    return unmatched


# ═══════════════════════════════════════════════════════════════════════════
# 报告渲染
# ═══════════════════════════════════════════════════════════════════════════


def render_markdown_report(report: ContractReport, root: Path) -> str:
    """渲染 Markdown 报告。"""
    lines: list[str] = []
    lines.append("# API Contract Check Report")
    lines.append("")
    lines.append(f"- Backend HTTP routes: **{len(report.backend_routes)}**")
    lines.append(f"- Backend WebSocket routes: **{len(report.backend_ws_routes)}**")
    lines.append(f"- Frontend HTTP calls: **{len(report.frontend_calls)}**")
    lines.append(f"- Frontend WebSocket calls: **{len(report.frontend_ws_calls)}**")
    lines.append("")

    # ── 错误：前端 404 风险 ──
    if report.frontend_404:
        lines.append("## [FAIL] Frontend calls without backend route (404 risk)")
        lines.append("")
        lines.append("| Method | Frontend Path | Source |")
        lines.append("|--------|---------------|--------|")
        # 去重显示
        seen: set[tuple[str, str, str]] = set()
        for c in report.frontend_404:
            key = (c.method, c.path, c.file)
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"| {c.method} | `{c.path}` | `{c.file}:{c.line}` |")
        lines.append("")

    # ── 错误：未定义的 URL 常量 ──
    if report.undefined_url_constants:
        lines.append("## [FAIL] Undefined URL constants in frontend")
        lines.append("")
        lines.append("| Constant | Source |")
        lines.append("|----------|--------|")
        seen_const: set[tuple[str, str]] = set()
        for c in report.undefined_url_constants:
            key = (c.raw_url, c.file)
            if key in seen_const:
                continue
            seen_const.add(key)
            lines.append(f"| `{c.raw_url}` | `{c.file}:{c.line}` |")
        lines.append("")

    # ── 警告：后端 dead code ──
    if report.backend_dead:
        lines.append("## [WARN] Backend routes not called by frontend (potential dead code)")
        lines.append("")
        lines.append("| Method | Backend Path | Module | File |")
        lines.append("|--------|--------------|--------|------|")
        # 按模块分组排序
        sorted_dead = sorted(
            report.backend_dead, key=lambda r: (r.module, r.method, r.path)
        )
        for r in sorted_dead:
            lines.append(
                f"| {r.method} | `{r.path}` | {r.module} | `{r.file}` |"
            )
        lines.append("")

    # ── 警告：WS 不匹配 ──
    if report.ws_unmatched:
        lines.append("## [WARN] Frontend WebSocket channels without backend route")
        lines.append("")
        lines.append("| Frontend WS Path | Source |")
        lines.append("|------------------|--------|")
        for c in report.ws_unmatched:
            lines.append(f"| `{c.path}` | `{c.file}:{c.line}` |")
        lines.append("")

    # ── 总结 ──
    lines.append("## Summary")
    lines.append("")
    if report.has_errors:
        lines.append("**Result: [FAIL]**")
    elif report.has_warnings:
        lines.append("**Result: [PASS] (with warnings)**")
    else:
        lines.append("**Result: [PASS]**")
    lines.append("")
    lines.append(f"- Errors (404 risk / undefined constants): {len(report.frontend_404) + len(report.undefined_url_constants)}")
    lines.append(f"- Warnings (dead code / unmatched WS): {len(report.backend_dead) + len(report.ws_unmatched)}")
    lines.append("")

    return "\n".join(lines)


def render_json_report(report: ContractReport) -> str:
    """渲染 JSON 报告。"""
    return json.dumps(
        {
            "backend_routes": [asdict(r) for r in report.backend_routes],
            "backend_ws_routes": [asdict(r) for r in report.backend_ws_routes],
            "frontend_calls": [asdict(c) for c in report.frontend_calls],
            "frontend_ws_calls": [asdict(c) for c in report.frontend_ws_calls],
            "undefined_url_constants": [asdict(c) for c in report.undefined_url_constants],
            "frontend_404": [asdict(c) for c in report.frontend_404],
            "backend_dead": [asdict(r) for r in report.backend_dead],
            "ws_unmatched": [asdict(c) for c in report.ws_unmatched],
            "has_errors": report.has_errors,
            "has_warnings": report.has_warnings,
        },
        indent=2,
        ensure_ascii=False,
    )


# ═══════════════════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════════════════


def find_project_root(start: Path | None = None) -> Path:
    """从 start 开始向上查找项目根目录（包含 src/edgelite/api 与 web/src/api 的目录）。"""
    if start is None:
        start = Path(__file__).resolve().parent
    current = start
    for _ in range(10):
        if (current / BACKEND_API_DIR).exists() or (current / FRONTEND_API_DIR).exists():
            return current
        if current.parent == current:
            break
        current = current.parent
    # 回退到脚本所在目录的父目录
    return Path(__file__).resolve().parent.parent


def run_check(root: Path) -> ContractReport:
    """执行契约校验，返回报告对象。"""
    backend_http, backend_ws = collect_backend_routes(root)
    frontend_http, frontend_ws, undefined_calls = collect_frontend_calls(root)

    frontend_404, backend_dead = compare_contracts(
        backend_http, frontend_http, undefined_calls
    )
    ws_unmatched = compare_ws(backend_ws, frontend_ws)

    return ContractReport(
        backend_routes=backend_http,
        backend_ws_routes=backend_ws,
        frontend_calls=frontend_http,
        frontend_ws_calls=frontend_ws,
        undefined_url_constants=undefined_calls,
        frontend_404=frontend_404,
        backend_dead=backend_dead,
        ws_unmatched=ws_unmatched,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="EdgeLite API contract checker (static analysis)"
    )
    parser.add_argument(
        "--root",
        type=str,
        default=None,
        help="Project root directory (default: auto-detect)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON report instead of Markdown",
    )
    args = parser.parse_args(argv)

    if args.root:
        root = Path(args.root).resolve()
    else:
        root = find_project_root()

    if not (root / BACKEND_API_DIR).exists():
        print(
            f"[FAIL] Backend API directory not found: {root / BACKEND_API_DIR}",
            file=sys.stderr,
        )
        return 1
    if not (root / FRONTEND_API_DIR).exists():
        print(
            f"[FAIL] Frontend API directory not found: {root / FRONTEND_API_DIR}",
            file=sys.stderr,
        )
        return 1

    report = run_check(root)

    if args.json:
        print(render_json_report(report))
    else:
        print(render_markdown_report(report, root))

    if report.has_errors:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
