"""可观测性 API 路由

提供延迟、追踪、告警、指标聚合查询。
- 优先从 ``_app_state.metrics`` 读取；不存在时返回空列表/零值。
- traces 持久化到 SQLite 表 ``observability_traces``，告警事件持久化到 ``observability_alert_events``。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from edgelite.api.deps import require_permission
from edgelite.models.common import ApiResponse, PagedResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/observability", tags=["Observability"])

_TRACES_TABLE = "observability_traces"
_ALERT_EVENTS_TABLE = "observability_alert_events"

_TRACES_DDL = (
    f"CREATE TABLE IF NOT EXISTS {_TRACES_TABLE} ("
    "id TEXT PRIMARY KEY, "
    "trace_id TEXT, "
    "node TEXT, "
    "operation TEXT, "
    "duration_ms REAL, "
    "status TEXT, "
    "payload TEXT, "
    "started_at TEXT NOT NULL)"
)

_ALERT_EVENTS_DDL = (
    f"CREATE TABLE IF NOT EXISTS {_ALERT_EVENTS_TABLE} ("
    "id TEXT PRIMARY KEY, "
    "name TEXT NOT NULL, "
    "severity TEXT, "
    "status TEXT NOT NULL, "
    "message TEXT, "
    "context TEXT, "
    "raised_at TEXT NOT NULL, "
    "resolved_at TEXT)"
)


async def _ensure_tables() -> None:
    try:
        from edgelite.app import _app_state

        db = getattr(_app_state, "database", None)
        if db is None:
            return
        async with db.get_session() as session:
            from sqlalchemy import text

            await session.execute(text(_TRACES_DDL))
            await session.execute(text(_ALERT_EVENTS_DDL))
            await session.commit()
    except Exception as e:
        logger.error("observability ensure tables failed: %s", e)


def _get_metrics_source() -> Any | None:
    try:
        from edgelite.app import _app_state

        return getattr(_app_state, "metrics", None)
    except Exception as e:
        logger.error("observability get metrics source failed: %s", e)
        return None


@router.get("/overview", response_model=ApiResponse)
async def get_overview(
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """返回可观测性概览（请求数、错误率、平均延迟）"""
    try:
        metrics = _get_metrics_source()
        if metrics is None:
            return ApiResponse(
                data={
                    "requests_total": 0,
                    "errors_total": 0,
                    "error_rate": 0.0,
                    "avg_latency_ms": 0.0,
                    "uptime_seconds": 0,
                }
            )
        data = {
            "requests_total": int(getattr(metrics, "requests_total", 0) or 0),
            "errors_total": int(getattr(metrics, "errors_total", 0) or 0),
            "error_rate": float(getattr(metrics, "error_rate", 0.0) or 0.0),
            "avg_latency_ms": float(getattr(metrics, "avg_latency_ms", 0.0) or 0.0),
            "uptime_seconds": int(getattr(metrics, "uptime_seconds", 0) or 0),
        }
        return ApiResponse(data=data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("observability overview failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.get("/latency", response_model=ApiResponse)
async def get_latency(
    start: str = Query(..., description="ISO8601 起始时间"),
    end: str = Query(..., description="ISO8601 结束时间"),
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """返回指定时间窗口内的延迟序列"""
    try:
        metrics = _get_metrics_source()
        if metrics is None:
            return ApiResponse(data={"series": [], "start": start, "end": end})
        series = []
        getter = getattr(metrics, "get_latency_series", None)
        if callable(getter):
            series = list(await getter(start, end)) if _is_async(getter) else list(getter(start, end))
        return ApiResponse(data={"series": series, "start": start, "end": end})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("observability latency failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


def _is_async(func: Any) -> bool:
    import asyncio

    return asyncio.iscoroutinefunction(func)


@router.get("/latency/percentiles", response_model=ApiResponse)
async def get_latency_percentiles(
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """返回延迟百分位"""
    try:
        metrics = _get_metrics_source()
        defaults = {"p50": 0.0, "p90": 0.0, "p95": 0.0, "p99": 0.0}
        if metrics is None:
            return ApiResponse(data=defaults)
        for key in defaults:
            defaults[key] = float(getattr(metrics, f"latency_{key}", 0.0) or 0.0)
        return ApiResponse(data=defaults)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("observability latency percentiles failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.get("/latency/histogram", response_model=ApiResponse)
async def get_latency_histogram(
    buckets: int = Query(default=10, ge=1, le=100),
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """返回延迟直方图"""
    try:
        metrics = _get_metrics_source()
        if metrics is None:
            return ApiResponse(data={"buckets": [], "count": 0})
        getter = getattr(metrics, "get_latency_histogram", None)
        if callable(getter):
            result = await getter(buckets) if _is_async(getter) else getter(buckets)
            return ApiResponse(data=result if isinstance(result, dict) else {"buckets": list(result)})
        return ApiResponse(data={"buckets": [], "count": 0})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("observability histogram failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.get("/alerts/rules", response_model=ApiResponse)
async def list_alert_rules(
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """返回告警规则列表"""
    try:
        metrics = _get_metrics_source()
        if metrics is None:
            return ApiResponse(data=[])
        getter = getattr(metrics, "get_alert_rules", None)
        if not callable(getter):
            return ApiResponse(data=[])
        rules = await getter() if _is_async(getter) else getter()
        return ApiResponse(data=list(rules) if rules else [])
    except HTTPException:
        raise
    except Exception as e:
        logger.error("observability alerts rules failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.get("/alerts/events", response_model=ApiResponse)
async def list_alert_events(
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """返回告警事件列表"""
    try:
        from edgelite.app import _app_state

        db = getattr(_app_state, "database", None)
        if db is None:
            return ApiResponse(data=[])
        await _ensure_tables()
        async with db.get_session() as session:
            from sqlalchemy import text

            result = await session.execute(
                text(
                    f"SELECT id, name, severity, status, message, context, raised_at, resolved_at "
                    f"FROM {_ALERT_EVENTS_TABLE} ORDER BY raised_at DESC LIMIT 200"
                )
            )
            rows = result.fetchall()
        events = [
            {
                "id": r[0],
                "name": r[1],
                "severity": r[2],
                "status": r[3],
                "message": r[4],
                "context": r[5],
                "raised_at": r[6],
                "resolved_at": r[7],
            }
            for r in rows
        ]
        return ApiResponse(data=events)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("observability alerts events failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.post("/alerts/events/{name}/{ts}/resolve", response_model=ApiResponse)
async def resolve_alert_event(
    name: str = Path(..., max_length=128),
    ts: str = Path(..., max_length=64),
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    """将指定告警事件标记为已解决"""
    try:
        from edgelite.app import _app_state

        db = _app_state.database
        await _ensure_tables()
        async with db.get_session() as session:
            from sqlalchemy import text

            now = datetime.now(UTC).isoformat()
            await session.execute(
                text(
                    f"UPDATE {_ALERT_EVENTS_TABLE} SET status='resolved', resolved_at=:now "
                    "WHERE name=:name AND raised_at=:ts"
                ),
                {"name": name, "ts": ts, "now": now},
            )
            await session.commit()
        return ApiResponse(data={"name": name, "raised_at": ts, "resolved": True, "resolved_at": now})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("observability resolve alert failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.get("/traces", response_model=PagedResponse)
async def list_traces(
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """分页返回 traces"""
    try:
        from edgelite.app import _app_state

        db = getattr(_app_state, "database", None)
        if db is None:
            return PagedResponse(data=[], total=0, page=page, size=size)
        await _ensure_tables()
        async with db.get_session() as session:
            from sqlalchemy import text

            total_result = await session.execute(text(f"SELECT COUNT(*) FROM {_TRACES_TABLE}"))
            total = int(total_result.scalar() or 0)
            offset = (page - 1) * size
            result = await session.execute(
                text(
                    f"SELECT id, trace_id, node, operation, duration_ms, status, payload, started_at "
                    f"FROM {_TRACES_TABLE} ORDER BY started_at DESC LIMIT :limit OFFSET :offset"
                ),
                {"limit": size, "offset": offset},
            )
            rows = result.fetchall()
        items = [
            {
                "id": r[0],
                "trace_id": r[1],
                "node": r[2],
                "operation": r[3],
                "duration_ms": r[4],
                "status": r[5],
                "payload": r[6],
                "started_at": r[7],
            }
            for r in rows
        ]
        return PagedResponse(data=items, total=total, page=page, size=size)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("observability traces list failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.get("/traces/{trace_id}", response_model=ApiResponse)
async def get_trace(
    trace_id: str = Path(..., max_length=128),
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """返回单个 trace 详情"""
    try:
        from edgelite.app import _app_state

        db = getattr(_app_state, "database", None)
        if db is None:
            return ApiResponse(data=None)
        await _ensure_tables()
        async with db.get_session() as session:
            from sqlalchemy import text

            result = await session.execute(
                text(
                    f"SELECT id, trace_id, node, operation, duration_ms, status, payload, started_at "
                    f"FROM {_TRACES_TABLE} WHERE trace_id=:tid OR id=:tid LIMIT 1"
                ),
                {"tid": trace_id},
            )
            r = result.fetchone()
        if not r:
            return ApiResponse(data=None)
        return ApiResponse(
            data={
                "id": r[0],
                "trace_id": r[1],
                "node": r[2],
                "operation": r[3],
                "duration_ms": r[4],
                "status": r[5],
                "payload": r[6],
                "started_at": r[7],
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("observability trace detail failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.get("/traces/stats/{node}", response_model=ApiResponse)
async def get_trace_stats(
    node: str = Path(..., max_length=128),
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """返回指定节点的 trace 统计"""
    try:
        from edgelite.app import _app_state

        db = getattr(_app_state, "database", None)
        if db is None:
            return ApiResponse(data={"node": node, "count": 0, "avg_duration_ms": 0.0, "max_duration_ms": 0.0})
        await _ensure_tables()
        async with db.get_session() as session:
            from sqlalchemy import text

            result = await session.execute(
                text(
                    f"SELECT COUNT(*), AVG(duration_ms), MAX(duration_ms) "
                    f"FROM {_TRACES_TABLE} WHERE node=:node"
                ),
                {"node": node},
            )
            r = result.fetchone()
        count = int(r[0] or 0) if r else 0
        avg = float(r[1] or 0.0) if r else 0.0
        mx = float(r[2] or 0.0) if r else 0.0
        return ApiResponse(
            data={
                "node": node,
                "count": count,
                "avg_duration_ms": round(avg, 2),
                "max_duration_ms": round(mx, 2),
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("observability trace stats failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.get("/metrics", response_model=ApiResponse)
async def get_metrics(
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """返回原始 metrics 摘要"""
    try:
        metrics = _get_metrics_source()
        if metrics is None:
            return ApiResponse(data={})
        # 尝试调用 snapshot 方法，否则按属性聚合
        snap = getattr(metrics, "snapshot", None)
        if callable(snap):
            data = await snap() if _is_async(snap) else snap()
            return ApiResponse(data=data if isinstance(data, dict) else {"snapshot": data})
        return ApiResponse(data={"available": True})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("observability metrics failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None
