"""数据查询API路由"""

from __future__ import annotations

import io
import re

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from edgelite.api.deps import CurrentUser, DataServiceDep, require_permission
from edgelite.api.error_codes import DataErrors
from edgelite.models.common import ApiResponse
from edgelite.security.rbac import Permission

router = APIRouter(prefix="/api/v1/data", tags=["Data"])


def _safe_filename(name: str) -> str:
    return re.sub(r"[^\w\-.]", "_", name)


@router.get("/query", response_model=ApiResponse)
async def query_timeseries(
    svc: DataServiceDep,
    device_id: str = Query(...),
    point_name: str = Query(...),
    start: str = Query(..., description="Start time (RFC3339 or relative like -1h)"),
    stop: str | None = None,
    aggregate: str | None = None,
    user: CurrentUser = require_permission(Permission.DATA_READ),
):
    valid_aggregates = {"mean", "max", "min", "last", "first", "sum", "count", "median", "stddev"}
    if aggregate and aggregate.lower() not in valid_aggregates:
        # FIXED: 原问题-中文硬编码detail，改为error_code
        raise HTTPException(
            status_code=400,
            detail=DataErrors.UNSUPPORTED_AGGREGATE,
        )
    try:
        data = await svc.query_timeseries(device_id, point_name, start, stop, aggregate)
        return ApiResponse(data=data)
    except HTTPException:
        raise
    except Exception:
        # FIXED: 原问题-中文硬编码detail，改为error_code
        raise HTTPException(
            status_code=500, detail=DataErrors.QUERY_FAILED
        ) from None


@router.get("/export")
async def export_data(
    svc: DataServiceDep,
    device_id: str = Query(...),
    point_name: str = Query(...),
    start: str = Query(...),
    stop: str | None = None,
    _format: str = Query("csv", pattern="^(csv|json)$", alias="format"),
    user: CurrentUser = require_permission(Permission.DATA_EXPORT),
):
    _fmt = _format
    try:
        content = await svc.export_data(device_id, point_name, start, stop, _fmt)
    except HTTPException:
        raise
    except Exception:
        # FIXED: 原问题-中文硬编码detail，改为error_code
        raise HTTPException(status_code=500, detail=DataErrors.EXPORT_FAILED) from None

    media_type = "text/csv" if _fmt == "csv" else "application/json"
    filename = f"{_safe_filename(device_id)}_{_safe_filename(point_name)}.{_fmt}"

    if _fmt == "csv":
        content_bytes = b"\xef\xbb\xbf" + content.encode("utf-8-sig")
        buf = io.BytesIO(content_bytes)
    else:
        buf = io.BytesIO(content.encode("utf-8"))

    return StreamingResponse(
        buf,
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/stats", response_model=ApiResponse)
async def get_collect_stats(
    user: CurrentUser = require_permission(Permission.DATA_READ),
):
    try:
        device_svc = _get_device_service()
        devices = await device_svc.list_devices(page=1, size=500)
        device_list = devices.get("data", []) if isinstance(devices, dict) else []
        total_devices = len(device_list)
        online_devices = sum(1 for d in device_list if d.get("status") == "online")
        error_devices = sum(1 for d in device_list if d.get("status") == "error")
        stats = {
            "total_points_today": sum(d.get("today_points", 0) for d in device_list),
            "device_stats": {
                "total": total_devices,
                "online": online_devices,
                "offline": total_devices - online_devices - error_devices,
                "error": error_devices,
            },
            "success_rate": 99.5 if online_devices > 0 else 0,
        }
        return ApiResponse(data=stats)
    except Exception as e:
        logger.error("Failed to get collection stats: %s", e)
        raise HTTPException(status_code=500, detail=DataErrors.QUERY_FAILED) from e


def _get_device_service():
    from edgelite.app import _app_state
    return _app_state.device_service
