"""用户数据模型"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class UserCreate(BaseModel):
    """创建用户请求"""

    username: str = Field(min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_]+$")
    password: str = Field(min_length=8, description="密码至少8字符，包含字母和数字")
    role: Literal["admin", "operator", "viewer"]


class UserUpdate(BaseModel):
    """更新用户请求"""

    password: str | None = Field(default=None, min_length=8)
    role: Literal["admin", "operator", "viewer"] | None = None
    enabled: bool | None = None


class UserResponse(BaseModel):
    """用户响应"""

    user_id: str
    username: str
    role: str
    enabled: bool
    created_at: str


class LoginRequest(BaseModel):
    """登录请求"""

    username: str
    password: str


class TokenResponse(BaseModel):
    """Token响应"""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
