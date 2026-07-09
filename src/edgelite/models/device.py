"""设备数据模型"""

from __future__ import annotations

import logging
import math
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger(__name__)


class PointDef(BaseModel):
    """测点定义"""

    name: str = Field(max_length=128)
    data_type: Literal[
        "bool", "int16", "int32", "uint16", "uint32", "float32", "float64", "string"
    ] = "float32"
    unit: str = ""
    address: str = "0"
    access_mode: Literal["r", "w", "rw"] = "r"
    min: float | None = None
    max: float | None = None
    mode: str | None = None
    # R9-S-05: 线性变换字段 value = raw * scale + offset
    scale: float | None = None
    offset: float | None = None

    @model_validator(mode='after')
    def validate_point_ranges(self) -> PointDef:
        """R9-S-05: 跨字段校验-测点范围与缩放参数"""
        # min 必须 < max（若两者都存在）
        if self.min is not None and self.max is not None and self.min >= self.max:
            raise ValueError(f"min({self.min}) 必须 < max({self.max})")
        # scale 和 offset 不能同时为 0（若两者都存在）
        if self.scale is not None and self.offset is not None:
            if self.scale == 0 and self.offset == 0:
                raise ValueError("scale 和 offset 不能同时为 0")
        return self


class ModbusConfig(BaseModel):
    """Modbus TCP连接配置"""

    host: str = "127.0.0.1"
    port: int = Field(default=5020, ge=1, le=65535)
    slave_id: int = Field(default=1, ge=1, le=247)  # R8-S-12: 原问题-ge=0 允许 0（广播地址），但协议规范要求从站地址 1-247；修复-改为 ge=1
    timeout: float = Field(default=5.0, ge=0.1)


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
    protocol: str
    config: dict[str, Any] = {}
    points: list[PointDef] = Field(min_length=1)
    collect_interval: int = Field(default=5, ge=1)

    @field_validator("protocol")
    @classmethod
    def validate_protocol(cls, v: str) -> str:
        try:
            from edgelite.constants import VALID_DEVICE_PROTOCOLS, normalize_protocol

            # FIXED-P0: 原问题-Pydantic允许"video"但DB CheckConstraint仅允许"video_ai"，设备创建时DB约束拒绝
            # 统一使用 constants.VALID_DEVICE_PROTOCOLS 作为协议校验单一来源
            canonical = normalize_protocol(v)
            if canonical is None:
                raise ValueError(
                    f"Unsupported protocol '{v}'. Valid protocols: {sorted(VALID_DEVICE_PROTOCOLS)}"
                )
            return canonical
        except ImportError:
            logger.warning(
                "Constants module not available, "
                "protocol validation skipped for: %s",
                v,
            )
        return v

    @model_validator(mode='after')
    def validate_protocol_config(self) -> DeviceCreate:
        """R9-S-05: 跨字段校验-根据协议类型校验 config 必填字段"""
        cfg = self.config or {}
        proto = self.protocol
        if proto in ("modbus_tcp", "modbus_rtu"):
            # Modbus 协议必须包含从站地址（slave_id 或 unit_id）
            if "slave_id" not in cfg and "unit_id" not in cfg:
                raise ValueError(
                    f"protocol '{proto}' 的 config 必须包含 slave_id 或 unit_id"
                )
        elif proto in ("opc_ua", "opcua"):
            # OPC UA 协议必须包含端点 URL
            if "endpoint_url" not in cfg and "endpoint" not in cfg:
                raise ValueError(
                    "protocol 'opc_ua' 的 config 必须包含 endpoint_url"
                )
        elif proto in ("mqtt_client", "mqtt"):
            # MQTT 协议必须包含 broker 和 port
            missing = []
            if "broker" not in cfg:
                missing.append("broker")
            if "port" not in cfg:
                missing.append("port")
            if missing:
                raise ValueError(
                    f"protocol 'mqtt' 的 config 必须包含 {' 和 '.join(missing)}"
                )
        return self


class DeviceUpdate(BaseModel):
    """更新设备请求"""

    name: str | None = Field(default=None, min_length=1, max_length=64)
    config: dict[str, Any] | None = None
    points: list[PointDef] | None = None
    collect_interval: int | None = Field(default=None, ge=1)
    # SEC-FIX(修复3): 强制更新标志——绕过运行中配置锁定。仅 admin/有权限用户可设 true，
    # 审计 details 会标记 forced_update=True。默认 False。
    force: bool = False


class DeviceWritePolicyUpdate(BaseModel):
    """设备写保护策略更新请求

    SEC-FIX-V11: 写保护配置与普通设备配置分离，需独立权限 DEVICE_WRITE_POLICY_EDIT
    防止拥有 DEVICE_UPDATE 权限的用户越权关闭写保护后恶意写入
    """

    write_verify: bool | None = Field(
        default=None, description="写入后是否回读校验"
    )
    write_rate_limit: int | None = Field(
        default=None, ge=0, le=1000, description="写入频率限制（次/分钟），0 表示不限制"
    )
    write_audit: bool | None = Field(
        default=None, description="是否记录写入审计日志"
    )
    write_whitelist: list[str] | None = Field(
        default=None, description="可写点位白名单，空列表表示全部可写"
    )


class DeviceResponse(BaseModel):
    """设备响应"""

    device_id: str
    name: str
    protocol: str
    status: Literal["online", "offline", "error", "unknown"]  # FIXED-P2: 原问题-status为str无枚举约束，与DB CheckConstraint不一致，可能返回非法状态值
    config: dict[str, Any]
    points: list[PointDef]
    collect_interval: int
    created_by: str | None = None
    created_at: str
    updated_at: str
    version: int = 1


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

    @field_validator("point")
    @classmethod
    def validate_point(cls, v: str) -> str:
        # SEC-FIX: 服务端校验点位名非空且长度受限，防止注入超长字段
        if not v or not v.strip():
            raise ValueError("point must not be empty")
        if len(v) > 128:
            raise ValueError("point length must not exceed 128 characters")
        return v

    @field_validator("value")
    @classmethod
    def validate_value(cls, v: float | int | bool | str) -> float | int | bool | str:
        # SEC-FIX: 服务端值校验——拒绝 NaN/Inf 数值，限制字符串长度
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                raise ValueError("value must not be NaN or Inf")
            return v
        if isinstance(v, str):
            if len(v) > 256:
                raise ValueError("string value length must not exceed 256 characters")
            return v
        return v


class DiscoverRequest(BaseModel):
    """设备发现请求"""

    protocol: str = "modbus_tcp"
    config: dict[str, Any] = {}


class BatchDeviceIds(BaseModel):
    """Batch device IDs request"""

    device_ids: list[str] = Field(..., min_length=1, max_length=100)


class PushDataPointValue(BaseModel):
    """推送数据点值"""

    value: float | int | bool | str
    quality: str = "good"
    timestamp: str | None = None


class PushDeviceDataRequest(BaseModel):
    """设备数据推送请求"""

    data: dict[str, PushDataPointValue] = Field(min_length=1, max_length=100)

    @field_validator("data")
    @classmethod
    def validate_data_keys(cls, v: dict) -> dict:
        for key in v:
            if len(key) > 128:
                raise ValueError(f"Point name too long: {key}")
        return v


class TemplateCreate(BaseModel):
    """创建设备模板请求"""

    device_id: str
    template_name: str = Field(min_length=1, max_length=64)


class TemplateResponse(BaseModel):
    """设备模板响应"""

    name: str
    protocol: str
    config_template: dict[str, Any] = {}
    point_templates: list[PointDef] = []
    created_at: str


class CreateFromTemplateRequest(BaseModel):
    """从模板创建设备请求"""

    template_name: str
    device_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,62}[a-z0-9]$")
    name: str = Field(min_length=1, max_length=64)
    config: dict[str, Any] | None = None
    collect_interval: int = Field(default=5, ge=1)


class ExportDevicesRequest(BaseModel):
    """导出设备请求"""

    # FIXED: 添加上限防止一次性导出过多设备导致内存溢出
    device_ids: list[str] | None = Field(default=None, max_length=500)


class ImportDevicesRequest(BaseModel):
    """导入设备请求"""

    # FIXED: 添加max_length=500上限，防止批量导入过多设备导致内存溢出和长事务
    data: list[dict[str, Any]] = Field(min_length=1, max_length=500)
    overwrite: bool = False
    atomic: bool = Field(default=False, description="事务模式：全部成功才导入，任一失败全部回滚")
