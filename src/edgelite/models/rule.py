"""规则数据模型"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class RuleCondition(BaseModel):
    point: str
    operator: Literal[">", ">=", "<", "<=", "==", "!="]  # FIXED-P2: 原问题-缺少"!="运算符，无法表达"不等于"条件
    threshold: float
    type: Literal["threshold", "ai_inference"] = "threshold"
    model_id: str | None = None
    ai_threshold: float | None = None

    @model_validator(mode='after')
    def validate_condition_type(self) -> RuleCondition:
        """R9-S-05: 跨字段校验-条件类型与必填字段"""
        # AI推理条件必须包含 model_id
        if self.type == "ai_inference" and not self.model_id:
            raise ValueError("type='ai_inference' 时，model_id 必填")
        # threshold 类型：threshold 已由 Pydantic 必填字段保证，无需额外校验
        # 注：type='comparison' 不在 Literal 定义中，由 Pydantic 自动拒绝
        return self


# SEC-FIX(修复4): 允许的规则类型，与 evaluator.py 读取的 rule_type 取值对齐
_VALID_RULE_TYPES = ("threshold", "ai_inference", "script")


def _validate_rule_script(script: str | None) -> str | None:
    """SEC-FIX(修复4): 校验规则脚本字段，防止通过数据导入绕过脚本校验。

    优先复用 sandbox._validate_script_ast 的 AST 静态检测(覆盖属性链/字符串拼接逃逸)；
    sandbox 不可用时回退到危险模式黑名单。
    """
    if script is None or script == "":
        return script
    if len(script) > 10000:
        raise ValueError("script length must not exceed 10000 characters")
    # 主校验：AST 静态检测危险属性与字符串拼接构造
    try:
        from edgelite.engine.sandbox import _validate_script_ast
        if not _validate_script_ast(script):
            raise ValueError("script blocked by AST safety check (dangerous attribute or string concat)")
    except ImportError as exc:  # FIXED(P2): 原问题-B904异常链丢失; 修复-添加as exc与from exc
        # sandbox 不可用时回退到危险模式黑名单
        _DANGEROUS = (
            "__import__", "eval(", "exec(", "compile(", "os.system", "subprocess",
            "open(", "globals(", "locals(", "getattr(", "__class__", "__subclasses__",
            "__mro__", "__bases__", "__builtins__",
        )
        lowered = script.lower()
        for kw in _DANGEROUS:
            if kw in lowered:
                raise ValueError(f"script contains forbidden keyword: {kw}") from exc
    return script


class RuleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    device_id: str | None = None  # FIXED-P1: 原问题-device_id为必填但DB RuleORM.device_id为nullable，全局规则无法创建
    conditions: list[RuleCondition] = Field(min_length=1)
    logic: Literal["AND", "OR", "NOT"] = "AND"
    duration: int = Field(default=0, ge=0, le=3600)
    severity: Literal["critical", "major", "warning", "minor", "info"]
    # FIXED: 原问题-min_length=1 拒绝空列表，但 DB 默认 '[]' 允许空列表（表示不通知）。
    # 移除 min_length=1，允许显式传入空列表。
    notify_channels: list[Literal["dingtalk", "email", "wechat", "webhook"]] = Field(
        default=["dingtalk"]
    )
    # SEC-FIX(修复4): 显式声明 script 与 rule_type 字段并校验，防止通过数据导入绕过脚本校验
    # evaluator.py 读取 rule.get("script")/rule.get("rule_type")，原模型缺失这两个字段导致无校验
    script: str | None = None
    rule_type: str | None = None

    @field_validator("logic", mode="before")
    @classmethod
    def _normalize_logic(cls, v):
        # FIXED: 原问题-logic 为 Literal["AND","OR","NOT"]，拒绝小写 "and"/"or"/"not"。
        # DB CHECK 约束与 evaluator 均使用大写，此处统一归一化为大写，兼容大小写输入。
        if isinstance(v, str):
            return v.upper()
        return v

    @field_validator("script")
    @classmethod
    def _validate_script(cls, v: str | None) -> str | None:
        return _validate_rule_script(v)

    @field_validator("rule_type")
    @classmethod
    def _validate_rule_type(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return v
        if v not in _VALID_RULE_TYPES:
            raise ValueError(f"Invalid rule_type: {v}, must be one of {_VALID_RULE_TYPES}")
        return v

    @model_validator(mode='after')
    def validate_rule_conditions(self) -> RuleCreate:
        """R9-S-05: 跨字段校验-AI推理规则条件一致性"""
        if self.rule_type == "ai_inference":
            # AI推理规则必须包含至少一个AI推理条件且带 model_id
            has_ai_condition = any(
                c.type == "ai_inference" and c.model_id
                for c in self.conditions
            )
            if not has_ai_condition:
                raise ValueError(
                    "rule_type='ai_inference' 时，conditions 中至少一个 condition 的 "
                    "type='ai_inference' 且包含 model_id"
                )
        return self


class RuleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    device_id: str | None = None
    conditions: list[RuleCondition] | None = None
    logic: Literal["AND", "OR", "NOT"] | None = None
    duration: int | None = Field(default=None, ge=0, le=3600)
    severity: Literal["critical", "major", "warning", "minor", "info"] | None = None
    notify_channels: list[Literal["dingtalk", "email", "wechat", "webhook"]] | None = None
    # SEC-FIX(修复4): 显式声明 script 与 rule_type 字段并校验，与 RuleCreate 保持一致
    script: str | None = None
    rule_type: str | None = None

    @field_validator("logic", mode="before")
    @classmethod
    def _normalize_logic(cls, v):
        # FIXED: 与 RuleCreate 保持一致，logic 归一化为大写以兼容大小写输入。
        if isinstance(v, str):
            return v.upper()
        return v

    @field_validator("script")
    @classmethod
    def _validate_script(cls, v: str | None) -> str | None:
        return _validate_rule_script(v)

    @field_validator("rule_type")
    @classmethod
    def _validate_rule_type(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return v
        if v not in _VALID_RULE_TYPES:
            raise ValueError(f"Invalid rule_type: {v}, must be one of {_VALID_RULE_TYPES}")
        return v


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
    updated_at: str | None = None  # FIXED-P2: 原问题-RuleResponse缺失updated_at字段，与RuleORM新增字段对应
    created_by: str | None = None
    version: int = 1
    inference_count: int = 0
    error_count: int = 0


class RuleTestRequest(BaseModel):
    point_values: dict[str, float]


# SEC-FIX-RULE-VERSION: 规则版本历史响应模型
class RuleVersionItem(BaseModel):
    """规则版本列表项（不含快照详情）"""
    version: int
    created_by: str | None = None
    created_at: str
    change_summary: str | None = None
    snapshot_hash: str


class RuleVersionDetail(BaseModel):
    """规则版本详情（含快照）"""
    rule_id: str
    version: int
    snapshot: dict
    snapshot_hash: str
    change_summary: str | None = None
    created_by: str | None = None
    created_at: str


class RuleRollbackRequest(BaseModel):
    """规则回滚请求"""
    version: int
