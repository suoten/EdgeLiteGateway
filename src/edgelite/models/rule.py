"""规则数据模型"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class RuleCondition(BaseModel):
    point: str
    operator: Literal[">", ">=", "<", "<=", "=="]
    threshold: float


class RuleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    device_id: str
    conditions: list[RuleCondition] = Field(min_length=1)
    logic: Literal["AND", "OR"] = "AND"
    duration: int = Field(default=0, ge=0, le=3600)
    severity: Literal["critical", "warning", "info"]
    notify_channels: list[Literal["dingtalk", "email", "wechat", "webhook"]] = Field(
        min_length=1, default=["dingtalk"]
    )


class RuleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    conditions: list[RuleCondition] | None = None
    logic: Literal["AND", "OR"] | None = None
    duration: int | None = Field(default=None, ge=0, le=3600)
    severity: Literal["critical", "warning", "info"] | None = None
    notify_channels: list[Literal["dingtalk", "email", "wechat", "webhook"]] | None = None


class RuleResponse(BaseModel):
    rule_id: str
    name: str
    device_id: str | None
    conditions: list[RuleCondition]
    logic: str
    duration: int
    severity: str
    enabled: bool
    notify_channels: list[str]
    created_at: str


class RuleTestRequest(BaseModel):
    point_values: dict[str, float]
