"""告警数据模型"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class AlarmResponse(BaseModel):
    """告警响应"""

    alarm_id: str
    rule_id: str
    device_id: str | None
    severity: str
    status: str
    trigger_value: dict[str, Any]
    trigger_count: int
    fired_at: str
    acknowledged_at: str | None = None
    acknowledged_by: str | None = None
    recovered_at: str | None = None


class AlarmAckRequest(BaseModel):
    """告警确认请求（空body，确认人从Token获取）"""

    pass
