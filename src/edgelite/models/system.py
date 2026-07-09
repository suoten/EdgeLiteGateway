from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


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


class ComponentHealth(BaseModel):
    name: str
    status: str = "healthy"
    message: str = ""
    latency_ms: float = 0.0


class HealthCheckResponse(BaseModel):
    status: str = "healthy"
    version: str = ""
    uptime: int = 0
    components: list[ComponentHealth] = Field(default_factory=list)


class PerformanceData(BaseModel):
    cpu_percent: float = 0.0
    memory_percent: float = 0.0
    memory_used_mb: float = 0.0
    memory_total_mb: float = 0.0
    disk_percent: float = 0.0
    disk_used_gb: float = 0.0
    disk_total_gb: float = 0.0
    net_sent_mb: float = 0.0
    net_recv_mb: float = 0.0


class SystemResourcesResponse(BaseModel):
    cpu_percent: float = 0.0
    cpu_count: int = 0
    cpu_count_logical: int = 0
    memory_total: int = 0
    memory_used: int = 0
    memory_available: int = 0
    memory_percent: float = 0.0
    disk_total: int = 0
    disk_used: int = 0
    disk_free: int = 0
    disk_percent: float = 0.0
    net_bytes_sent: int = 0
    net_bytes_recv: int = 0
    load_avg_1m: float = 0.0
    load_avg_5m: float = 0.0
    load_avg_15m: float = 0.0
    collected_at: str = ""


class CascadeConfigRequest(BaseModel):
    """级联配置更新请求

    所有字段可选，仅传入需要更新的字段。SSRF 校验保留在端点中。
    """

    parent_host: str | None = None
    parent_port: int | str | None = None
    role: str | None = None
    enabled: bool | None = None
    auth_key: str | None = None


class ConfigSectionUpdateRequest(BaseModel):
    """配置节更新请求

    支持两种请求格式：
    1. {"config": {...}} - 显式 config 字段包裹
    2. {...} - 直接传入配置键值对（通过 extra fields 接收）
    """

    config: dict | None = None
    model_config = {"extra": "allow"}

    def get_values(self) -> dict:
        """获取配置值，兼容两种请求格式。"""
        if self.config is not None:
            return self.config
        return self.model_dump(exclude={"config"}, exclude_none=True)


class RetentionPolicyRequest(BaseModel):
    """数据保留策略更新请求"""

    history_retention_days: int | None = Field(default=None, ge=1, le=3650, description="历史数据保留天数(1-3650)")
    alarm_retention_days: int | None = Field(default=None, ge=1, le=3650, description="告警数据保留天数(1-3650)")


class NtpConfigRequest(BaseModel):
    """NTP配置更新请求"""

    enabled: bool = False
    server: str = Field(min_length=1, description="NTP服务器地址(IP或域名)")

    @field_validator("server")
    @classmethod
    def normalize_server(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("server must not be empty")
        return v
