"""用户数据模型"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class UserCreate(BaseModel):
    """创建用户请求"""

    username: str = Field(min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_]+$")
    password: str = Field(
        min_length=8, max_length=72, description="密码8-72字符，必须包含字母和数字"
    )
    role: Literal["admin", "operator", "viewer"]

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        if not re.search(r"[a-zA-Z]", v):
            raise ValueError("密码必须包含字母")
        if not re.search(r"\d", v):
            raise ValueError("密码必须包含数字")
        return v


class UserUpdate(BaseModel):
    """更新用户请求"""

    password: str | None = Field(default=None, min_length=8, max_length=72)
    role: Literal["admin", "operator", "viewer"] | None = None
    enabled: bool | None = None

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not re.search(r"[a-zA-Z]", v):
            raise ValueError("密码必须包含字母")
        if not re.search(r"\d", v):
            raise ValueError("密码必须包含数字")
        return v


class UserResponse(BaseModel):
    """用户响应"""

    user_id: str
    username: str
    role: str
    enabled: bool
    must_change_password: bool = False
    created_at: str
    updated_at: str = ""


class LoginRequest(BaseModel):
    """登录请求"""

    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=72)


class TokenResponse(BaseModel):
    """Token响应"""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(ge=1)


class UserInfoResponse(BaseModel):
    """当前用户信息响应"""

    user_id: str
    username: str
    role: str
    must_change_password: bool = False
