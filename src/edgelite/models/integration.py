"""EdgeLite v1.0 联调集成数据模型"""

from __future__ import annotations

from pydantic import BaseModel, Field


class HandshakeRequest(BaseModel):
    version: str = "1.0"
    protocols: list[str] = Field(default_factory=list)  # FIXED-P2: 原问题-可变默认值 list=[] 在实例间共享
    capabilities: list[str] = Field(default_factory=list)  # FIXED-P2: 原问题-可变默认值
    heartbeat_interval: float = 30.0


class HandshakeResponse(BaseModel):
    version: str = "1.0"
    protocols: list[str] = Field(default_factory=list)  # FIXED-P2: 原问题-可变默认值
    capabilities: list[str] = Field(default_factory=list)  # FIXED-P2: 原问题-可变默认值
    session_id: str = ""


class BackhaulConfig(BaseModel):
    enabled: bool = False
    device_filter: list[str] = Field(default_factory=list)
    point_filter: list[str] = Field(default_factory=list)
    change_threshold: float = 0.0
    rate_limit: float = Field(default=10.0, gt=0)  # FIXED-P2: 原问题-缺少正数验证
    buffer_size: int = Field(default=1000, gt=0)  # FIXED-P2: 原问题-缺少正数验证
