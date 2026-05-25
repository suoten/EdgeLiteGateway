"""AI推理引擎数据模型"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import Boolean, Float, Integer, String, Text
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


class AiModelUpdate(BaseModel):
    model_name: str | None = Field(default=None, min_length=1, max_length=128)
    model_type: ModelType | None = None
    input_schema: dict | None = None
    output_schema: dict | None = None


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


class AiModelDetailResponse(AiModelResponse):
    inference_count: int = 0
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
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class AiInferenceLogORM(Base):
    __tablename__ = "ai_inference_logs"

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
