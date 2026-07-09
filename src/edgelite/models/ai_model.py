"""AI推理引擎数据模型"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

# FIXED(P3): 原问题-F401未使用导入sqlalchemy.CheckConstraint; 修复-从导入中移除该名称
from sqlalchemy import Boolean, Float, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from edgelite.models.db import Base, _utcnow


class ModelType(StrEnum):
    ANOMALY = "anomaly"
    TREND = "trend"
    THRESHOLD = "threshold"
    CUSTOM = "custom"


class ModelStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    LOADING = "loading"
    ERROR = "error"
    UNAVAILABLE = "unavailable"


class AiModelCreate(BaseModel):
    model_name: str = Field(min_length=1, max_length=128)
    model_version: str = Field(pattern=r"^v\d+\.\d+\.\d+$")
    model_type: ModelType
    model_file_path: str = Field(pattern=r".+\.onnx$")
    input_schema: dict = Field(default_factory=dict)
    output_schema: dict = Field(default_factory=dict)
    is_preset: bool = False
    preprocess_config: list[dict] = Field(default_factory=list)
    postprocess_config: list[dict] = Field(default_factory=list)
    batch_size: int = Field(default=1, ge=1, le=128)
    max_concurrent: int = Field(default=4, ge=1, le=32)
    timeout_ms: int = Field(default=30000, ge=100, le=300000)
    device_preference: str = Field(default="auto", pattern=r"^(auto|cpu|cuda|directml|openvino)$")


class AiModelUpdate(BaseModel):
    model_name: str | None = Field(default=None, min_length=1, max_length=128)
    model_type: ModelType | None = None
    input_schema: dict | None = None
    output_schema: dict | None = None
    preprocess_config: list[dict] | None = None
    postprocess_config: list[dict] | None = None
    batch_size: int | None = Field(default=None, ge=1, le=128)
    max_concurrent: int | None = Field(default=None, ge=1, le=32)
    timeout_ms: int | None = Field(default=None, ge=100, le=300000)
    device_preference: str | None = Field(default=None, pattern=r"^(auto|cpu|cuda|directml|openvino)$")


class AiModelResponse(BaseModel):
    model_id: str
    model_name: str
    model_version: str
    model_type: str
    model_file_path: str
    status: str
    is_preset: bool
    input_schema: dict
    output_schema: dict
    created_at: str
    updated_at: str
    preprocess_config: list[dict] = Field(default_factory=list)
    postprocess_config: list[dict] = Field(default_factory=list)
    batch_size: int = 1
    max_concurrent: int = 4
    timeout_ms: int = 30000
    device_preference: str = "auto"


class AiModelDetailResponse(AiModelResponse):
    inference_count: int = 0
    error_count: int = 0
    avg_latency_ms: int = 0
    last_inference_at: str | None = None


class AiModelReloadRequest(BaseModel):
    model_file_path: str = Field(pattern=r".+\.onnx$")


class ScheduleInferenceRequest(BaseModel):
    """Scheduled inference request"""

    device_id: str
    point_name: str
    interval_seconds: int = Field(ge=5, le=3600, default=60)
    input_window_size: int = Field(ge=1, le=10000, default=100)


class AiInferenceRequest(BaseModel):
    model_id: str
    input_data: list[float]
    device_id: str | None = None
    point_name: str | None = None


class AiInferenceResponse(BaseModel):
    model_id: str
    output_data: dict
    latency_ms: int
    timestamp: str


class AiStatsResponse(BaseModel):
    model_count: int = 0
    total_calls: int
    total_errors: int
    avg_latency_ms: int
    model_distribution: dict[str, int]


class AiModelStatsResponse(BaseModel):
    model_id: str
    model_name: str
    call_count: int
    error_count: int
    avg_latency_ms: int
    max_latency_ms: int
    min_latency_ms: int


class AiInferenceLogResponse(BaseModel):
    log_id: str
    model_id: str
    model_name: str
    device_id: str | None
    point_name: str | None
    input_summary: str
    output_summary: str
    latency_ms: int
    status: str
    error_message: str | None
    timestamp: str


class AiModelORM(Base):
    __tablename__ = "ai_models"

    model_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    model_version: Mapped[str] = mapped_column(String(16), nullable=False)
    model_type: Mapped[str] = mapped_column(String(16), nullable=False)
    model_file_path: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="inactive")
    is_preset: Mapped[bool] = mapped_column(Boolean, default=False)
    input_schema: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    output_schema: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    # FIXED-P1: 原问题-ORM缺少 preprocess_config/postprocess_config/batch_size/max_concurrent/timeout_ms/device_preference 字段
    # 这些字段在 AiModelCreate/AiModelResponse 中定义，但未持久化到数据库，导致创建后配置丢失
    preprocess_config: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    postprocess_config: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    batch_size: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    max_concurrent: Mapped[int] = mapped_column(Integer, nullable=False, default=4)
    timeout_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=30000)
    device_preference: Mapped[str] = mapped_column(String(16), nullable=False, default="auto")
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class AiInferenceLogORM(Base):
    __tablename__ = "ai_inference_logs"
    # FIXED(严重): 原问题-无任何索引，ai_service 启动时执行 GROUP BY model_id /
    # WHERE status='error' GROUP BY model_id 等聚合查询全表扫描，日志增长后严重影响启动速度。
    # 修复：为 model_id、status、timestamp 添加索引。
    __table_args__ = (
        Index("idx_ai_logs_model", "model_id"),
        Index("idx_ai_logs_status", "status"),
        Index("idx_ai_logs_timestamp", "timestamp"),
    )

    log_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    model_id: Mapped[str] = mapped_column(String(36), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    device_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    point_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_summary: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    output_summary: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="success")
    error_message: Mapped[str | None] = mapped_column(String(512), nullable=True)
    timestamp: Mapped[datetime] = mapped_column(default=_utcnow)


class AiModelVersionORM(Base):
    __tablename__ = "ai_model_versions"

    version_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    model_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(16), nullable=False)
    model_path: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="inactive")
    success_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    avg_latency_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)

    # FIXED(P2): 原问题-AiModelVersionORM缺少(model_id,version)唯一约束，可插入重复版本记录;
    # 修复-添加唯一约束
    __table_args__ = (
        UniqueConstraint("model_id", "version", name="uq_ai_model_versions_id_ver"),
    )


class ABTestCreateRequest(BaseModel):
    model_id: str
    variant_a_version: str
    variant_b_version: str
    traffic_split_b: float = Field(default=0.1, ge=0.0, le=1.0)


class ABTestUpdateRequest(BaseModel):
    traffic_split_b: float = Field(ge=0.0, le=1.0)


class ABTestResponse(BaseModel):
    model_id: str
    variant_a_version: str
    variant_b_version: str
    traffic_split_b: float
    enabled: bool
    total_requests: int
    variant_a_requests: int
    variant_b_requests: int
    created_at: str


class HotSwapRequest(BaseModel):
    model_id: str
    new_version: str
    new_model_path: str = Field(pattern=r".+\.onnx$")
    warmup_target: int = Field(default=3, ge=1, le=20)


class PreprocessConfigUpdate(BaseModel):
    preprocess_config: list[dict] = Field(default_factory=list)


class PostprocessConfigUpdate(BaseModel):
    postprocess_config: list[dict] = Field(default_factory=list)
