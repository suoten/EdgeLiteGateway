"""数据查询API路由"""

from __future__ import annotations

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

import io

from edgelite.models.common import ApiResponse
from edgelite.api.deps import CurrentUser, require_permission
from edgelite.security.rbac import Permission

router = APIRouter(prefix="/api/v1/data", tags=["数据查询"])


def _get_data_service():
    from edgelite.app import _app_state
    return _app_state.data_service


@router.get("/query", response_model=ApiResponse)
async def query_timeseries(
    device_id: str = Query(...),
    point_name: str = Query(...),
    start: str = Query(..., description="开始时间(RFC3339或相对时间如-1h)"),
    stop: str | None = None,
    aggregate: str | None = None,
    user: CurrentUser = require_permission(Permission.DATA_READ),
):
    svc = _get_data_service()
    data = await svc.query_timeseries(device_id, point_name, start, stop, aggregate)
    return ApiResponse(data=data)


@router.get("/export")
async def export_data(
    device_id: str = Query(...),
    point_name: str = Query(...),
    start: str = Query(...),
    stop: str | None = None,
    format: str = Query("csv", pattern="^(csv|json)$"),
    user: CurrentUser = require_permission(Permission.DATA_EXPORT),
):
    svc = _get_data_service()
    content = await svc.export_data(device_id, point_name, start, stop, format)

    media_type = "text/csv" if format == "csv" else "application/json"
    filename = f"{device_id}_{point_name}.{format}"

    return StreamingResponse(
        io.StringIO(content),
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
