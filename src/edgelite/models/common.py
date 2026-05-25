"""通用响应模型"""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, Field

from edgelite.constants import _DEFAULT_PAGE_SIZE, _MAX_QUERY_SIZE

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    """统一API响应格式"""

    code: int = 0
    message: str = "success"
    data: T | None = None
    error_code: str | None = None  # 业务错误码 (如 ERR_DEVICE_NOT_FOUND)
    trace_id: str | None = None  # 请求追踪ID


class PagedResponse(BaseModel, Generic[T]):
    """分页API响应格式"""

    code: int = 0
    message: str = "success"
    data: list[T] = []
    total: int = 0
    page: int = 1
    size: int = _DEFAULT_PAGE_SIZE


class ErrorResponse(BaseModel):
    """错误响应"""

    code: int
    message: str
    data: None = None
    error_code: str | None = None  # 业务错误码 (如 ERR_DEVICE_NOT_FOUND)
    trace_id: str | None = None  # 请求追踪ID


class PaginationParams(BaseModel):
    """分页请求参数"""

    page: int = Field(default=1, ge=1)
    size: int = Field(default=_DEFAULT_PAGE_SIZE, ge=1, le=_MAX_QUERY_SIZE, alias="page_size")

    model_config = {"populate_by_name": True}
