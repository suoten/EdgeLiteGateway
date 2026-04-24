"""设备数据模型"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class PointDef(BaseModel):
    """测点定义"""

    name: str
    data_type: Literal["int16", "uint16", "float32", "bool", "string"] = "float32"
    unit: str = ""
    address: str = "0"
    access_mode: Literal["r", "w", "rw"] = "r"
    min: float | None = None
    max: float | None = None
    mode: str | None = None  # 模拟器模式: sine/random_walk/fixed/random


class ModbusConfig(BaseModel):
    """Modbus TCP连接配置"""

    host: str = "127.0.0.1"
    port: int = 502
    slave_id: int = 1
    timeout: float = 5.0


class SimulatorConfig(BaseModel):
    """模拟器配置"""

    timeout: float = 5.0


class VideoDeviceConfig(BaseModel):
    """视频设备配置"""

    pygbsentry_device_id: str = ""
    channel_id: str = "1"
    timeout: float = 10.0


class DeviceCreate(BaseModel):
    """创建设备请求"""

    device_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,62}[a-z0-9]$")
    name: str = Field(min_length=1, max_length=64)
    protocol: Literal["modbus_tcp", "modbus_rtu", "opcua", "mqtt", "http", "simulator", "video"]
    config: dict[str, Any] = {}
    points: list[PointDef] = Field(min_length=1)
    collect_interval: int = Field(default=5, ge=1)


class DeviceUpdate(BaseModel):
    """更新设备请求"""

    name: str | None = Field(default=None, min_length=1, max_length=64)
    config: dict[str, Any] | None = None
    points: list[PointDef] | None = None
    collect_interval: int | None = Field(default=None, ge=1)


class DeviceResponse(BaseModel):
    """设备响应"""

    device_id: str
    name: str
    protocol: str
    status: str
    config: dict[str, Any]
    points: list[PointDef]
    collect_interval: int
    created_at: str
    updated_at: str


class SimulatorCreate(BaseModel):
    """创建模拟设备请求"""

    device_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,62}[a-z0-9]$")
    name: str = Field(min_length=1, max_length=64)
    points: list[PointDef] = Field(min_length=1)
    collect_interval: int = Field(default=5, ge=1)


class PointValue(BaseModel):
    """测点值"""

    name: str
    value: float | int | bool | str
    quality: str = "good"
    timestamp: str | None = None


class WritePointRequest(BaseModel):
    """写入测点请求"""

    point: str
    value: float | int | bool | str


class DiscoverRequest(BaseModel):
    """设备发现请求"""

    protocol: Literal["modbus_tcp"] = "modbus_tcp"
    config: dict[str, Any] = {}
