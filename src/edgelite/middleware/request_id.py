"""请求级追踪 ID 中间件 + 日志 Filter。

FIXED: 重建丢失的 request_id 中间件（app.py:656/699 引用但文件不存在）[2026-06-30]

设计:
- RequestIdMiddleware: 每个请求生成/继承 X-Request-Id，写入 contextvar 与响应头
- RequestIdFilter: logging.Filter，从 contextvar 读取 request_id 注入 logrecord，
  配合 config.logging.format 中的 %(request_id)s 占位符实现请求级日志串联（G-02）

线程安全: contextvars.ContextVar 天然异步安全，无需锁。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

# 请求级 contextvar —— 日志 Filter 与下游中间件/路由均可读取
request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")

_REQUEST_ID_HEADER = "X-Request-Id"

# 安全的请求头字符集（避免注入）；UUID4 hex 满足，外部传入时做白名单校验
import re as _re

_VALID_REQUEST_ID = _re.compile(r"^[A-Za-z0-9\-_]{1,128}$")


def _sanitize_request_id(raw: str | None) -> str:
    """对外部传入的 X-Request-Id 做白名单校验，不合法则生成新 ID。

    防止攻击者通过伪造请求头注入日志（log injection）或污染追踪链。
    """
    if raw and _VALID_REQUEST_ID.match(raw):
        return raw
    return uuid.uuid4().hex


class RequestIdMiddleware(BaseHTTPMiddleware):
    """为每个请求生成/继承 request_id，写入 contextvar 与响应头。"""

    def __init__(self, app: ASGIApp, header_name: str = _REQUEST_ID_HEADER) -> None:
        super().__init__(app)
        self._header_name = header_name

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        raw = request.headers.get(self._header_name)
        rid = _sanitize_request_id(raw)
        # 设置 contextvar，下游所有日志记录均能通过 Filter 读取
        token = request_id_ctx.set(rid)
        try:
            response = await call_next(request)
            # 始终回写 request_id 便于客户端/网关串联
            response.headers[self._header_name] = rid
            return response
        finally:
            request_id_ctx.reset(token)


class RequestIdFilter(logging.Filter):
    """日志 Filter：将 contextvar 中的 request_id 注入 LogRecord。

    配合 logging format 中的 %(request_id)s 占位符使用（见 config.py LoggingConfig.format）。
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - logging API
        record.request_id = request_id_ctx.get()
        return True
