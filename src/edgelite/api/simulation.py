"""仿真测试 API 路由

支持 Modbus/OPC-UA/MQTT/HTTP 等协议的仿真数据生成、预览、运行、评估、导出。
- types 返回静态支持的仿真类型
- run / preview 用 numpy 生成模拟数据（无 numpy 时 fallback 到 random）
"""

from __future__ import annotations

import logging
import math
import random
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from edgelite.api.deps import require_permission
from edgelite.models.common import ApiResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/simulation", tags=["Simulation"])

# 支持的仿真类型
_SIM_TYPES: list[dict[str, Any]] = [
    {"type": "modbus_rtu", "description": "Modbus RTU 仿真（寄存器/线圈读写）"},
    {"type": "modbus_tcp", "description": "Modbus TCP 仿真（寄存器/线圈读写）"},
    {"type": "opcua", "description": "OPC-UA 节点仿真"},
    {"type": "mqtt", "description": "MQTT 主题消息仿真"},
    {"type": "http", "description": "HTTP REST 接口仿真"},
]


def _get_rng_engine() -> tuple[str, Any]:
    """优先返回 numpy 引擎；不可用时使用 random"""
    try:
        import numpy as np

        return ("numpy", np)
    except ImportError:
        return ("random", random)


def _generate_series(
    sim_type: str,
    config: dict[str, Any],
    duration: float = 1.0,
    count: int = 100,
) -> list[dict[str, Any]]:
    engine_name, engine = _get_rng_engine()
    points: list[dict[str, Any]] = []
    base_ts = datetime.now(UTC).timestamp()
    interval = max(duration, 0.001) / max(count, 1)
    amplitude = float(config.get("amplitude", 10.0))
    offset = float(config.get("offset", 0.0))
    noise = float(config.get("noise", 0.5))
    freq = float(config.get("frequency", 1.0))

    for i in range(count):
        t = i * interval
        phase = 2.0 * math.pi * freq * t
        sine = amplitude * math.sin(phase) + offset
        if engine_name == "numpy":
            jitter = float(engine.random.normal(0, noise))
        else:
            jitter = random.uniform(-noise, noise)
        value = round(sine + jitter, 4)
        points.append(
            {
                "ts": datetime.fromtimestamp(base_ts + t, tz=UTC).isoformat(),
                "index": i,
                "value": value,
                "sim_type": sim_type,
                "engine": engine_name,
            }
        )
    return points


class PreviewRequest(BaseModel):
    type: str = Field(..., max_length=32)
    config: dict[str, Any] = Field(default_factory=dict)


class RunRequest(BaseModel):
    type: str = Field(..., max_length=32)
    config: dict[str, Any] = Field(default_factory=dict)
    duration: float | None = Field(default=1.0, ge=0.001, le=86400)
    count: int | None = Field(default=100, ge=1, le=10000)


class AssessRequest(BaseModel):
    type: str = Field(..., max_length=32)
    config: dict[str, Any] = Field(default_factory=dict)


class ExportRequest(BaseModel):
    format: str = Field(default="json", max_length=16)
    data: Any = None


@router.get("/types", response_model=ApiResponse)
async def list_types(
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """返回支持的仿真类型列表"""
    try:
        return ApiResponse(data=_SIM_TYPES)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("simulation types failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.post("/preview", response_model=ApiResponse)
async def preview(
    req: PreviewRequest,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """生成 10 个预览数据点"""
    try:
        if not any(t["type"] == req.type for t in _SIM_TYPES):
            raise HTTPException(status_code=400, detail="ERR_COMMON_VALIDATION_FAILED")
        points = _generate_series(req.type, req.config, duration=1.0, count=10)
        return ApiResponse(data={"type": req.type, "preview": points})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("simulation preview failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.post("/run", response_model=ApiResponse)
async def run(
    req: RunRequest,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    """运行仿真，生成完整数据序列"""
    try:
        if not any(t["type"] == req.type for t in _SIM_TYPES):
            raise HTTPException(status_code=400, detail="ERR_COMMON_VALIDATION_FAILED")
        duration = float(req.duration or 1.0)
        count = int(req.count or 100)
        points = _generate_series(req.type, req.config, duration=duration, count=count)
        return ApiResponse(
            data={
                "type": req.type,
                "config": req.config,
                "duration": duration,
                "count": len(points),
                "points": points,
                "started_at": datetime.now(UTC).isoformat(),
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("simulation run failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.post("/assess", response_model=ApiResponse)
async def assess(
    req: AssessRequest,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """评估配置合理性（基础校验，返回建议字段）"""
    try:
        issues: list[str] = []
        if not req.config:
            issues.append("config is empty, using defaults")
        if req.config.get("amplitude", 0) < 0:
            issues.append("amplitude should be non-negative")
        if req.config.get("noise", 0) < 0:
            issues.append("noise should be non-negative")
        valid_type = any(t["type"] == req.type for t in _SIM_TYPES)
        if not valid_type:
            issues.append(f"unsupported simulation type: {req.type}")
        return ApiResponse(
            data={
                "type": req.type,
                "valid": valid_type,
                "issues": issues,
                "recommended_duration": 1.0,
                "recommended_count": 100,
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("simulation assess failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.post("/export", response_model=ApiResponse)
async def export(
    req: ExportRequest,
    user: dict[str, str] = Depends(require_permission(Permission.DATA_EXPORT)),
):
    """导出仿真数据（json/csv 占位实现）"""
    try:
        import json as _json

        fmt = (req.format or "json").lower()
        if fmt == "json":
            return ApiResponse(data={"format": "json", "payload": req.data})
        if fmt == "csv":
            data = req.data or []
            if not isinstance(data, list):
                data = [data]
            lines = []
            for row in data:
                if isinstance(row, dict):
                    lines.append(",".join(f"{k}={v}" for k, v in row.items()))
                else:
                    lines.append(str(row))
            return ApiResponse(data={"format": "csv", "payload": "\n".join(lines)})
        raise HTTPException(status_code=400, detail="ERR_COMMON_VALIDATION_FAILED")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("simulation export failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None
