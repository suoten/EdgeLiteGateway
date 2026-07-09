"""通用响应模型"""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, Field, model_validator

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
    data: list[T] = Field(default_factory=list)  # FIXED-P2: 原问题-使用可变默认值[]，改为default_factory
    total: int = 0
    page: int = 1
    size: int = _DEFAULT_PAGE_SIZE
    total_pages: int = 0  # FIXED-P1: 原问题-缺少total_pages字段，前端需自行计算且total=0时除零异常

    @model_validator(mode="after")
    def _compute_total_pages(self) -> PagedResponse[T]:
        """FIXED-P1: 自动计算总页数，防止前端 total=0 时除零"""
        if self.size > 0 and self.total_pages == 0:
            self.total_pages = (self.total + self.size - 1) // self.size
        return self


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


class SortParams(BaseModel):
    """排序请求参数"""

    sort_by: str | None = Field(default=None, description="排序字段名")
    sort_order: str = Field(default="asc", pattern=r"^(asc|desc)$")  # FIXED-P1: 原问题-缺少排序参数模型，列表API无法统一校验排序字段和方向

    model_config = {"populate_by_name": True}
