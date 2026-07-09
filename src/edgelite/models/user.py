"""用户数据模型"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class UserCreate(BaseModel):
    """创建用户请求"""

    username: str = Field(min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_]+$")
    password: str = Field(
        min_length=8, max_length=72, description="密码8-72字符，必须包含字母、数字和特殊字符"  # FIXED-P1: 原问题-描述仅提"字母和数字"但验证器要求特殊字符，描述误导用户
    )
    role: Literal["admin", "operator", "viewer"]

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        if not re.search(r"[a-zA-Z]", v):
            raise ValueError("密码必须包含字母")
        if not re.search(r"\d", v):
            raise ValueError("密码必须包含数字")
        if not re.search(r"[!@#$%^&*()_+\-=\[\]{}|;':\",.<>?`~]", v):
            raise ValueError("密码必须包含特殊字符")
        return v


class UserUpdate(BaseModel):
    """更新用户请求"""

    password: str | None = Field(default=None, min_length=8, max_length=72, description="密码8-72字符，必须包含字母、数字和特殊字符")  # FIXED-P1: 原问题-与UserCreate描述不一致
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
        if not re.search(r"[!@#$%^&*()_+\-=\[\]{}|;':\",.<>?`~]", v):
            raise ValueError("密码必须包含特殊字符")
        return v


class UserResponse(BaseModel):
    """用户响应"""

    user_id: str
    username: str
    role: str
    enabled: bool
    must_change_password: bool = False
    password_changed_at: str | None = None
    created_at: str
    updated_at: str | None = None  # FIXED-P2: 原问题-updated_at默认为空字符串""而非None，与其他可选字段不一致
    version: int = 1


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
    csrf_token: str | None = None


class UserInfoResponse(BaseModel):
    """当前用户信息响应"""

    user_id: str
    username: str
    role: str
    must_change_password: bool = False


class RefreshTokenRequest(BaseModel):
    """刷新Token请求

    refresh 字段可选，未提供时 fallback 到 HttpOnly Cookie。
    """

    refresh: str | None = Field(default=None, description="refresh token，未提供时从Cookie读取")


class ChangePasswordRequest(BaseModel):
    """修改密码请求

    密码复杂度校验（长度/字母+数字/特殊字符/弱密码）保留在端点中，
    以返回具体的业务错误码（PASSWORD_POLICY 等）。
    """

    old_password: str = Field(min_length=1, max_length=128, description="当前密码")
    new_password: str = Field(min_length=1, max_length=128, description="新密码")


class ForgotPasswordRequest(BaseModel):
    """忘记密码请求"""

    username: str = Field(min_length=1, max_length=32, description="用户名")


class ResetPasswordRequest(BaseModel):
    """重置密码请求

    密码复杂度校验保留在端点中以返回具体业务错误码。
    """

    token: str = Field(min_length=1, description="密码重置token")
    new_password: str = Field(min_length=1, max_length=128, description="新密码")


class LogoutRequest(BaseModel):
    """登出请求

    refresh_token 可选，未提供时仅撤销 Cookie 中的 token。
    """

    refresh_token: str | None = Field(default=None, description="可选的 refresh token")
