"""设备/驱动健康状态响应模型。

FIXED: 重建丢失的 models/health 模块（api/devices.py:50, api/drivers.py:27 引用但文件不存在，
导致 create_app() 崩溃）[2026-06-30]

字段依据 api/drivers.py 中 DeviceHealthResponse / DriverHealthResponse 的实际构造推导。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class DeviceHealthResponse(BaseModel):
    """单设备健康状态响应。"""

    device_id: str
    is_connected: bool = False
    connection_quality_score: int = 0
    consecutive_failures: int = 0
    total_reads: int = 0
    failed_reads: int = 0
    total_writes: int = 0
    failed_writes: int = 0
    last_success_read: str | None = None
    last_failed_read: str | None = None
    last_offline_at: str | None = None
    total_downtime_seconds: float = 0.0
    avg_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    health_score: float = 0.0
    total_reconnects: int = 0
    effective_state: str = "unknown"
    read_error_rate: float = 0.0
    degradation_reason: str | None = None


class DriverHealthResponse(BaseModel):
    """单驱动下所有设备的聚合健康状态响应。"""

    driver_name: str
    device_count: int = 0
    healthy_count: int = 0
    degraded_count: int = 0
    offline_count: int = 0
    avg_health_score: float = 0.0
    devices: list[DeviceHealthResponse] = Field(default_factory=list)
