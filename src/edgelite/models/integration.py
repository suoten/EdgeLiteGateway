"""EdgeLite v1.0 联调集成数据模型"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class HandshakeRequest(BaseModel):
    version: str = "1.0"
    protocols: list[str] = []
    capabilities: list[str] = []
    heartbeat_interval: float = 30.0


class HandshakeResponse(BaseModel):
    version: str = "1.0"
    protocols: list[str] = []
    capabilities: list[str] = []
    session_id: str = ""


class BackhaulConfig(BaseModel):
    enabled: bool = False
    device_filter: list[str] = Field(default_factory=list)
    point_filter: list[str] = Field(default_factory=list)
    change_threshold: float = 0.0
    rate_limit: float = 10.0
    buffer_size: int = 1000
