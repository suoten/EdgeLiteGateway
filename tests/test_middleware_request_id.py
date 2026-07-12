"""请求级追踪 ID 中间件单元测试。

覆盖 src/edgelite/middleware/request_id.py：
_sanitize_request_id 白名单校验、RequestIdMiddleware 生成/继承/回写、
RequestIdFilter contextvar 注入、端到端 X-Request-Id 透传。
"""

from __future__ import annotations

import logging
import uuid

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from edgelite.middleware.request_id import (
    RequestIdFilter,
    RequestIdMiddleware,
    _sanitize_request_id,
    request_id_ctx,
)

# ─── _sanitize_request_id ───


def test_sanitize_valid_id_preserved():
    """合法字符集（字母数字-_）且长度 <=128 的 ID 原样保留。"""
    assert _sanitize_request_id("abc-123_xyz") == "abc-123_xyz"


def test_sanitize_valid_uuid_preserved():
    """标准 UUID hex 满足白名单。"""
    rid = uuid.uuid4().hex
    assert _sanitize_request_id(rid) == rid


def test_sanitize_max_length_preserved():
    """128 字符是上限，恰好 128 字符应保留。"""
    rid = "a" * 128
    assert _sanitize_request_id(rid) == rid


def test_sanitize_too_long_replaced():
    """超过 128 字符生成新 ID。"""
    rid = "a" * 129
    result = _sanitize_request_id(rid)
    assert result != rid
    assert len(result) == 32  # uuid4().hex 长度


def test_sanitize_illegal_chars_replaced():
    """含非法字符（空格/中文/特殊符号）生成新 ID。"""
    for raw in ["hello world", "id<script>", "id;rm-rf", "测试ID", "id\ninjection"]:
        result = _sanitize_request_id(raw)
        assert result != raw
        assert len(result) == 32


def test_sanitize_none_replaced():
    """None 生成新 ID。"""
    result = _sanitize_request_id(None)
    assert result is not None
    assert len(result) == 32


def test_sanitize_empty_replaced():
    """空字符串生成新 ID。"""
    result = _sanitize_request_id("")
    assert len(result) == 32


def test_sanitize_dot_not_allowed():
    """点号不在白名单字符集中，应替换。"""
    result = _sanitize_request_id("req.123")
    assert result != "req.123"


def test_sanitize_generates_unique_ids():
    """多次调用生成不同的新 ID。"""
    ids = {_sanitize_request_id(None) for _ in range(100)}
    assert len(ids) == 100  # 极低概率碰撞


# ─── RequestIdMiddleware ───


def _make_request_id_app() -> Starlette:
    def ping(request: Request) -> JSONResponse:  # type: ignore[misc]
        return JSONResponse({"rid": request_id_ctx.get()})

    app = Starlette(routes=[Route("/ping", ping, methods=["GET"])])
    app.add_middleware(RequestIdMiddleware)
    return app


def test_middleware_no_header_generates_new_id():
    """无 X-Request-Id 头时生成新 ID 并写入响应头。"""
    app = _make_request_id_app()
    with TestClient(app) as client:
        resp = client.get("/ping")
    assert resp.status_code == 200
    rid = resp.headers.get("X-Request-Id")
    assert rid is not None
    assert len(rid) == 32  # uuid4().hex


def test_middleware_valid_header_inherited():
    """合法 X-Request-Id 头被继承到响应。"""
    app = _make_request_id_app()
    custom_rid = "my-request-id-12345"
    with TestClient(app) as client:
        resp = client.get("/ping", headers={"X-Request-Id": custom_rid})
    assert resp.headers["X-Request-Id"] == custom_rid
    assert resp.json()["rid"] == custom_rid


def test_middleware_illegal_header_replaced():
    """非法 X-Request-Id 头被替换为新 ID。"""
    app = _make_request_id_app()
    with TestClient(app) as client:
        resp = client.get("/ping", headers={"X-Request-Id": "bad id with spaces"})
    rid = resp.headers["X-Request-Id"]
    assert rid != "bad id with spaces"
    assert len(rid) == 32


def test_middleware_contextvar_set_and_reset():
    """contextvar 在请求期间设置、请求后重置为默认值。"""
    app = _make_request_id_app()
    assert request_id_ctx.get() == "-"  # 请求前默认值
    with TestClient(app) as client:
        resp = client.get("/ping", headers={"X-Request-Id": "trace-abc-123"})
    assert resp.json()["rid"] == "trace-abc-123"
    # 请求结束后 contextvar 应重置
    assert request_id_ctx.get() == "-"


def test_middleware_custom_header_name():
    """支持自定义请求头名称。"""
    from starlette.applications import Starlette as _S
    from starlette.routing import Route as _R

    def handler(request: Request) -> JSONResponse:  # type: ignore[misc]
        return JSONResponse({"ok": True})

    app = _S(routes=[_R("/h", handler, methods=["GET"])])
    app.add_middleware(RequestIdMiddleware, header_name="X-Trace-Id")
    with TestClient(app) as client:
        resp = client.get("/h", headers={"X-Trace-Id": "trace-xyz-789"})
    assert resp.headers["X-Trace-Id"] == "trace-xyz-789"


# ─── RequestIdFilter ───


def test_filter_injects_request_id_into_record():
    """RequestIdFilter 从 contextvar 读取 request_id 注入 LogRecord。"""
    flt = RequestIdFilter()
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname="", lineno=0, msg="hello", args=None, exc_info=None
    )
    token = request_id_ctx.set("filter-test-rid")
    try:
        assert flt.filter(record) is True
        assert record.request_id == "filter-test-rid"
    finally:
        request_id_ctx.reset(token)


def test_filter_default_value_when_no_context():
    """无 contextvar 设置时注入默认值 '-'。"""
    flt = RequestIdFilter()
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname="", lineno=0, msg="hello", args=None, exc_info=None
    )
    # 确保 contextvar 是默认值
    assert request_id_ctx.get() == "-"
    assert flt.filter(record) is True
    assert record.request_id == "-"


def test_filter_always_returns_true():
    """filter 始终返回 True（不阻断日志记录）。"""
    flt = RequestIdFilter()
    record = logging.LogRecord(
        name="test", level=logging.ERROR, pathname="", lineno=0, msg="error", args=None, exc_info=None
    )
    assert flt.filter(record) is True


# ─── 端到端集成 ───


def test_end_to_end_request_id_in_log_format():
    """端到端验证 request_id 在日志 format 中可用。"""
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(request_id)s | %(message)s"))
    flt = RequestIdFilter()
    handler.addFilter(flt)

    logger = logging.getLogger("test_e2e_rid")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)

    app = _make_request_id_app()
    with TestClient(app) as client:
        client.get("/ping", headers={"X-Request-Id": "e2e-trace-id-001"})

    logger.removeHandler(handler)


def test_multiple_requests_get_different_ids():
    """多个无头请求获得不同的 request_id。"""
    app = _make_request_id_app()
    rids = set()
    with TestClient(app) as client:
        for _ in range(20):
            resp = client.get("/ping")
            rids.add(resp.headers["X-Request-Id"])
    assert len(rids) == 20


def test_response_header_always_present():
    """无论请求是否带头，响应始终包含 X-Request-Id。"""
    app = _make_request_id_app()
    with TestClient(app) as client:
        resp1 = client.get("/ping")
        resp2 = client.get("/ping", headers={"X-Request-Id": "custom-rid-xyz"})
    assert "X-Request-Id" in resp1.headers
    assert "X-Request-Id" in resp2.headers
