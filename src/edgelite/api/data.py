"""数据查询API路由"""

from __future__ import annotations

import io
import re

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from edgelite.api.deps import CurrentUser, DataServiceDep, require_permission
from edgelite.models.common import ApiResponse
from edgelite.security.rbac import Permission

router = APIRouter(prefix="/api/v1/data", tags=["数据查询"])


def _safe_filename(name: str) -> str:
    return re.sub(r"[^\w\-.]", "_", name)


@router.get("/query", response_model=ApiResponse)
async def query_timeseries(
    svc: DataServiceDep,
    device_id: str = Query(...),
    point_name: str = Query(...),
    start: str = Query(..., description="开始时间(RFC3339或相对时间如-1h)"),
    stop: str | None = None,
    aggregate: str | None = None,
    user: CurrentUser = require_permission(Permission.DATA_READ),
):
    valid_aggregates = {"mean", "max", "min", "last", "first", "sum", "count", "median", "stddev"}
    if aggregate and aggregate.lower() not in valid_aggregates:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的聚合函数: {aggregate}，可选值: {', '.join(sorted(valid_aggregates))}",
        )
    try:
        data = await svc.query_timeseries(device_id, point_name, start, stop, aggregate)
        return ApiResponse(data=data)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=500, detail="时序数据查询失败，请检查参数或稍后重试"
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
        raise HTTPException(status_code=500, detail="数据导出失败，请检查参数或稍后重试") from None

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
