"""告警数据模型"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class AlarmResponse(BaseModel):
    """告警响应"""

    model_config = ConfigDict(from_attributes=True)  # FIXED-P2: 原问题-缺少from_attributes配置，无法从ORM对象直接构造响应

    alarm_id: str
    rule_id: str
    device_id: str | None
    severity: Literal["critical", "major", "warning", "minor", "info"]  # FIXED-P1: 原问题-severity为str无枚举约束，可能返回非法级别
    status: Literal["firing", "acknowledged", "recovered"]  # FIXED-P1: 原问题-status为str无枚举约束，状态流转可能产生非法状态
    message: str = ""
    trigger_value: dict[str, Any]
    trigger_count: int
    fired_at: str
    acknowledged_at: str | None = None
    acknowledged_by: str | None = None
    recovered_at: str | None = None
    rule_type: Literal["threshold", "ai_inference", "script"] = "threshold"  # R8-S-07: 原问题-枚举包含"trend"但 RuleCreate._VALID_RULE_TYPES 为("threshold","ai_inference","script")，script 类型告警无法表示；修复-与规则侧合法类型对齐为 script
    version: int = 1


class AlarmAckRequest(BaseModel):
    """告警确认请求（空body，确认人从Token获取）"""

    pass


class AlarmFilter(BaseModel):
    """告警过滤请求"""

    status: Literal["firing", "acknowledged", "recovered"] | None = None
    severity: Literal["critical", "major", "warning", "minor", "info"] | None = None
    device_id: str | None = None
    rule_id: str | None = None
