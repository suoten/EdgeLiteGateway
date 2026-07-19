"""数据质量 API 路由

从 ``_app_state.data_service`` 查询时序数据计算质量指标（完整率、异常率等）。
- 数据不可用时返回零值
- reset 用于清空指定设备 / 位点的质量统计缓存
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel

from edgelite.api.deps import require_permission
from edgelite.models.common import ApiResponse, PagedResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/data-quality", tags=["Data Quality"])

# 内存统计缓存（device_id -> point_name -> stats）
_quality_cache: dict[str, dict[str, dict[str, Any]]] = {}


def _get_data_service() -> Any | None:
    try:
        from edgelite.app import _app_state

        return getattr(_app_state, "data_service", None)
    except Exception as e:
        logger.error("data_quality get data_service failed: %s", e)
        return None


async def _query_points(
    device_id: str, point_name: str, start: str, end: str
) -> list[dict[str, Any]]:
    svc = _get_data_service()
    if svc is None:
        return []
    try:
        # 兼容多种查询方法签名
        method = getattr(svc, "query_timeseries", None) or getattr(svc, "query", None)
        if not callable(method):
            return []
        import asyncio

        if asyncio.iscoroutinefunction(method):
            result = await method(device_id, point_name, start, end)
        else:
            result = method(device_id, point_name, start, end)
        if isinstance(result, dict):
            return list(result.get("points", []) or result.get("data", []) or [])
        return list(result or [])
    except Exception as e:
        logger.error("data_quality query points failed: %s", e)
        return []


def _compute_quality(points: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(points)
    if not total:
        return {
            "count": 0,
            "completeness_rate": 0.0,
            "anomaly_rate": 0.0,
            "min_value": None,
            "max_value": None,
            "avg_value": 0.0,
        }
    values: list[float] = []
    null_count = 0
    anomaly_count = 0
    for p in points:
        v = p.get("value") if isinstance(p, dict) else None
        if v is None or v == "":
            null_count += 1
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            null_count += 1
            continue
        values.append(fv)
    valid_count = len(values)
    if valid_count:
        avg = sum(values) / valid_count
        # 简单异常检测：值偏离均值 3σ 视为异常
        variance = sum((v - avg) ** 2 for v in values) / valid_count
        stddev = variance ** 0.5
        for v in values:
            if stddev > 0 and abs(v - avg) > 3 * stddev:
                anomaly_count += 1
    else:
        avg = 0.0
        stddev = 0.0
    completeness = round((valid_count / total) * 100, 2) if total else 0.0
    anomaly_rate = round((anomaly_count / valid_count) * 100, 2) if valid_count else 0.0
    return {
        "count": total,
        "valid_count": valid_count,
        "null_count": null_count,
        "anomaly_count": anomaly_count,
        "completeness_rate": completeness,
        "anomaly_rate": anomaly_rate,
        "min_value": min(values) if values else None,
        "max_value": max(values) if values else None,
        "avg_value": round(avg, 4) if values else 0.0,
        "stddev": round(stddev, 4) if values else 0.0,
    }


@router.get("/trend", response_model=ApiResponse)
async def get_trend(
    device_id: str = Query(..., max_length=128),
    point_name: str = Query(..., max_length=128),
    start: str = Query(..., description="ISO8601 起始时间"),
    end: str = Query(..., description="ISO8601 结束时间"),
    user: dict[str, str] = Depends(require_permission(Permission.DATA_READ)),
):
    """返回指定设备/位点的时间窗口质量趋势"""
    try:
        points = await _query_points(device_id, point_name, start, end)
        quality = _compute_quality(points)
        return ApiResponse(
            data={
                "device_id": device_id,
                "point_name": point_name,
                "start": start,
                "end": end,
                "quality": quality,
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("data_quality trend failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.get("/summary", response_model=ApiResponse)
async def get_summary(
    start: str = Query(...),
    end: str = Query(...),
    user: dict[str, str] = Depends(require_permission(Permission.DATA_READ)),
):
    """返回所有缓存的质量统计摘要"""
    try:
        summaries: list[dict[str, Any]] = []
        for device_id, points_map in _quality_cache.items():
            for point_name, stats in points_map.items():
                summaries.append(
                    {
                        "device_id": device_id,
                        "point_name": point_name,
                        "stats": stats,
                    }
                )
        return ApiResponse(
            data={
                "start": start,
                "end": end,
                "total_devices": len(_quality_cache),
                "summaries": summaries,
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("data_quality summary failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.get("/devices", response_model=PagedResponse)
async def list_devices(
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    user: dict[str, str] = Depends(require_permission(Permission.DATA_READ)),
):
    """分页返回有质量统计的设备列表"""
    try:
        items = [
            {"device_id": did, "point_count": len(points_map)}
            for did, points_map in _quality_cache.items()
        ]
        total = len(items)
        start_idx = (page - 1) * size
        page_items = items[start_idx : start_idx + size]
        return PagedResponse(data=page_items, total=total, page=page, size=size)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("data_quality devices list failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.get("/devices/{device_id}", response_model=ApiResponse)
async def get_device(
    device_id: str = Path(..., max_length=128),
    user: dict[str, str] = Depends(require_permission(Permission.DATA_READ)),
):
    """返回指定设备所有位点的质量统计"""
    try:
        points_map = _quality_cache.get(device_id, {})
        return ApiResponse(
            data={
                "device_id": device_id,
                "point_count": len(points_map),
                "points": [
                    {"point_name": pn, "stats": st} for pn, st in points_map.items()
                ],
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("data_quality device detail failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.get("/devices/{device_id}/points", response_model=ApiResponse)
async def list_points(
    device_id: str = Path(..., max_length=128),
    user: dict[str, str] = Depends(require_permission(Permission.DATA_READ)),
):
    """返回指定设备下所有有质量统计的位点"""
    try:
        points_map = _quality_cache.get(device_id, {})
        return ApiResponse(
            data={
                "device_id": device_id,
                "points": list(points_map.keys()),
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("data_quality points list failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.get("/report", response_model=ApiResponse)
async def get_report(
    device_id: str | None = Query(default=None, max_length=128),
    user: dict[str, str] = Depends(require_permission(Permission.DATA_READ)),
):
    """返回质量报告（按设备聚合）"""
    try:
        target_devices = [device_id] if device_id else list(_quality_cache.keys())
        report: list[dict[str, Any]] = []
        for did in target_devices:
            points_map = _quality_cache.get(did, {})
            if not points_map:
                continue
            all_completeness: list[float] = []
            all_anomaly_rate: list[float] = []
            for stats in points_map.values():
                q = stats.get("completeness_rate", 0.0) if isinstance(stats, dict) else 0.0
                a = stats.get("anomaly_rate", 0.0) if isinstance(stats, dict) else 0.0
                all_completeness.append(float(q))
                all_anomaly_rate.append(float(a))
            avg_completeness = (
                round(sum(all_completeness) / len(all_completeness), 2) if all_completeness else 0.0
            )
            avg_anomaly = (
                round(sum(all_anomaly_rate) / len(all_anomaly_rate), 2) if all_anomaly_rate else 0.0
            )
            report.append(
                {
                    "device_id": did,
                    "point_count": len(points_map),
                    "avg_completeness_rate": avg_completeness,
                    "avg_anomaly_rate": avg_anomaly,
                }
            )
        return ApiResponse(
            data={
                "generated_at": datetime.now(UTC).isoformat(),
                "device_count": len(report),
                "devices": report,
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("data_quality report failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


class ResetRequest(BaseModel):
    device_id: str | None = None
    point_name: str | None = None


@router.post("/reset", response_model=ApiResponse)
async def reset(
    req: ResetRequest,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    """清空质量统计缓存"""
    try:
        cleared: dict[str, Any] = {}
        if req.device_id and req.point_name:
            points_map = _quality_cache.get(req.device_id, {})
            cleared = {
                "device_id": req.device_id,
                "point_name": req.point_name,
                "existed": req.point_name in points_map,
            }
            points_map.pop(req.point_name, None)
            if not points_map:
                _quality_cache.pop(req.device_id, None)
        elif req.device_id:
            cleared = {
                "device_id": req.device_id,
                "existed": req.device_id in _quality_cache,
                "point_count": len(_quality_cache.get(req.device_id, {})),
            }
            _quality_cache.pop(req.device_id, None)
        else:
            cleared = {"device_count": len(_quality_cache)}
            _quality_cache.clear()
        return ApiResponse(data={"cleared": cleared})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("data_quality reset failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None
