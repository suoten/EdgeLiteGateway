"""数据查询API路由"""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

import io

from edgelite.models.common import ApiResponse
from edgelite.api.deps import CurrentUser, require_permission
from edgelite.security.rbac import Permission

router = APIRouter(prefix="/api/v1/data", tags=["数据查询"])


def _get_data_service():
    from edgelite.app import _app_state
    return _app_state.data_service


def _safe_filename(name: str) -> str:
    """清理文件名中的特殊字符，防止HTTP头注入"""
    return re.sub(r'[^\w\-.]', '_', name)


@router.get("/query", response_model=ApiResponse)
async def query_timeseries(
    device_id: str = Query(...),
    point_name: str = Query(...),
    start: str = Query(..., description="开始时间(RFC3339或相对时间如-1h)"),
    stop: str | None = None,
    aggregate: str | None = None,
    user: CurrentUser = require_permission(Permission.DATA_READ),
):
    _VALID_AGGREGATES = {"mean", "max", "min", "last", "first", "sum", "count", "median", "stddev"}
    if aggregate and aggregate.lower() not in _VALID_AGGREGATES:
        raise HTTPException(status_code=400, detail=f"不支持的聚合函数: {aggregate}，可选值: {', '.join(sorted(_VALID_AGGREGATES))}")
    try:
        svc = _get_data_service()
        data = await svc.query_timeseries(device_id, point_name, start, stop, aggregate)
        return ApiResponse(data=data)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="时序数据查询失败，请检查参数或稍后重试")


@router.get("/export")
async def export_data(
    device_id: str = Query(...),
    point_name: str = Query(...),
    start: str = Query(...),
    stop: str | None = None,
    format: str = Query("csv", pattern="^(csv|json)$"),
    user: CurrentUser = require_permission(Permission.DATA_EXPORT),
):
    try:
        svc = _get_data_service()
        content = await svc.export_data(device_id, point_name, start, stop, format)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="数据导出失败，请检查参数或稍后重试")

    media_type = "text/csv" if format == "csv" else "application/json"
    filename = f"{_safe_filename(device_id)}_{_safe_filename(point_name)}.{format}"

    if format == "csv":
        content_bytes = b"\xef\xbb\xbf" + content.encode("utf-8-sig")
        buf = io.BytesIO(content_bytes)
    else:
        buf = io.BytesIO(content.encode("utf-8"))

    return StreamingResponse(
        buf,
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
