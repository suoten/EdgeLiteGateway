"""通用响应模型"""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    """统一API响应格式"""

    code: int = 0
    message: str = "success"
    data: T | None = None


class PagedResponse(BaseModel, Generic[T]):
    """分页API响应格式"""

    code: int = 0
    message: str = "success"
    data: list[T] = []
    total: int = 0
    page: int = 1
    size: int = 20


class ErrorResponse(BaseModel):
    """错误响应"""

    code: int
    message: str
    data: None = None


class PaginationParams(BaseModel):
    """分页请求参数"""

    page: int = 1
    size: int = 20
