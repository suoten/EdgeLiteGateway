"""性能分析器API路由

提供请求耗时统计、内存分配跟踪、慢请求查询等能力。
- 内存中维护最近 N 条请求耗时记录（deque）
- tracemalloc 提供内存分配统计
- enable/disable 开关持久化到 SQLite key-value 表
"""

from __future__ import annotations

import logging
import tracemalloc
from collections import deque
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from edgelite.api.deps import require_permission
from edgelite.models.common import ApiResponse, PagedResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/profiler", tags=["Profiler"])

# 内存请求耗时记录（最近 1000 条）
_requests_buffer: deque[dict[str, Any]] = deque(maxlen=1000)

_PROFILER_KV_TABLE = "profiler_settings"
_KV_DDL = (
    f"CREATE TABLE IF NOT EXISTS {_PROFILER_KV_TABLE} ("
    "key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
)


async def _ensure_kv_table() -> None:
    try:
        from edgelite.app import _app_state

        db = getattr(_app_state, "database", None)
        if db is None:
            return
        async with db.get_session() as session:
            from sqlalchemy import text

            await session.execute(text(_KV_DDL))
            await session.commit()
    except Exception as e:
        logger.error("profiler ensure table failed: %s", e)


async def _load_enabled_flag() -> bool:
    try:
        from edgelite.app import _app_state

        db = getattr(_app_state, "database", None)
        if db is None:
            return False
        await _ensure_kv_table()
        async with db.get_session() as session:
            from sqlalchemy import text

            result = await session.execute(
                text(f"SELECT value FROM {_PROFILER_KV_TABLE} WHERE key = 'enabled'")
            )
            row = result.fetchone()
            return bool(row and row[0] == "1")
    except Exception as e:
        logger.error("profiler load state failed: %s", e)
        return False


async def _save_enabled_flag(enabled: bool) -> None:
    from edgelite.app import _app_state

    db = _app_state.database
    await _ensure_kv_table()
    async with db.get_session() as session:
        from sqlalchemy import text

        now = datetime.now(UTC).isoformat()
        await session.execute(
            text(
                f"INSERT INTO {_PROFILER_KV_TABLE} (key, value, updated_at) "
                "VALUES ('enabled', :v, :t) "
                "ON CONFLICT(key) DO UPDATE SET value = :v, updated_at = :t"
            ),
            {"v": "1" if enabled else "0", "t": now},
        )
        await session.commit()


@router.get("/stats", response_model=ApiResponse)
async def get_stats(
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """返回当前性能分析统计"""
    try:
        enabled = await _load_enabled_flag()
        total = len(_requests_buffer)
        avg_ms = 0.0
        max_ms = 0.0
        if total:
            durations = [r.get("duration_ms", 0) for r in _requests_buffer]
            avg_ms = round(sum(durations) / total, 2)
            max_ms = round(max(durations), 2)
        mem: dict[str, Any] = {}
        if tracemalloc.is_tracing():
            current, peak = tracemalloc.get_traced_memory()
            mem = {"tracing": True, "current_bytes": current, "peak_bytes": peak}
        else:
            mem = {"tracing": False}
        return ApiResponse(
            data={
                "enabled": enabled,
                "total_requests": total,
                "avg_duration_ms": avg_ms,
                "max_duration_ms": max_ms,
                "tracemalloc": mem,
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("profiler stats failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.get("/slowest", response_model=ApiResponse)
async def get_slowest(
    limit: int = Query(default=20, ge=1, le=200),
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """返回最慢的 N 条请求记录"""
    try:
        items = sorted(
            _requests_buffer, key=lambda x: x.get("duration_ms", 0), reverse=True
        )[:limit]
        return ApiResponse(data=list(items))
    except HTTPException:
        raise
    except Exception as e:
        logger.error("profiler slowest failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.get("/memory", response_model=ApiResponse)
async def get_memory(
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """返回 tracemalloc 内存分配统计"""
    try:
        if not tracemalloc.is_tracing():
            return ApiResponse(data={"tracing": False})
        snapshot = tracemalloc.take_snapshot()
        top_stats = snapshot.statistics("lineno")[:20]
        return ApiResponse(
            data={
                "tracing": True,
                "top": [
                    {
                        "filename": str(stat.traceback),
                        "size": stat.size,
                        "count": stat.count,
                    }
                    for stat in top_stats
                ],
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("profiler memory failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.get("/requests", response_model=PagedResponse)
async def list_requests(
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """分页返回请求耗时记录"""
    try:
        items = list(_requests_buffer)
        total = len(items)
        start_idx = (page - 1) * size
        page_items = items[start_idx : start_idx + size]
        return PagedResponse(data=page_items, total=total, page=page, size=size)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("profiler list requests failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.post("/enable", response_model=ApiResponse)
async def enable_profiler(
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    """启用性能分析（开启 tracemalloc + 持久化开关）"""
    try:
        if not tracemalloc.is_tracing():
            tracemalloc.start()
        await _save_enabled_flag(True)
        return ApiResponse(data={"enabled": True})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("profiler enable failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.post("/disable", response_model=ApiResponse)
async def disable_profiler(
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    """关闭性能分析"""
    try:
        if tracemalloc.is_tracing():
            tracemalloc.stop()
        await _save_enabled_flag(False)
        return ApiResponse(data={"enabled": False})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("profiler disable failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.post("/reset", response_model=ApiResponse)
async def reset_profiler(
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    """清空请求耗时缓存"""
    try:
        _requests_buffer.clear()
        return ApiResponse(data={"cleared": True})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("profiler reset failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.get("/export", response_model=ApiResponse)
async def export_profiler(
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """导出当前性能分析数据"""
    try:
        return ApiResponse(
            data={
                "requests": list(_requests_buffer),
                "exported_at": datetime.now(UTC).isoformat(),
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("profiler export failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None
