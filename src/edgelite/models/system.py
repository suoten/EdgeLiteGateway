from __future__ import annotations

from pydantic import BaseModel


class SystemStatusResponse(BaseModel):
    cpu_percent: float
    memory_total: int
    memory_used: int
    memory_percent: float
    disk_total: int
    disk_used: int
    disk_percent: float
    device_total: int
    device_online: int
    rule_total: int
    rule_enabled: int
    alarm_firing: int
    collect_task_count: int
    uptime: int
    version: str
