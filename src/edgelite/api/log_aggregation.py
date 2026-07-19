"""日志聚合 API 路由

使用 edgelite.engine.log_aggregator.get_log_aggregator() 单例，
提供日志查询、统计、级别控制与归档/清理。

注意：
- LogAggregator 是内存环形缓冲区，仅保存最近 N 条日志记录，
  archive/cleanup 操作仅在内存生效（archive 为 no-op，cleanup 清空缓冲区）。
- /filters 返回当前缓冲区中可用的 level 与 module 列表。
- /level 通过 Python logging.getLogger(module).setLevel(level) 动态调整日志级别。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from edgelite.api.deps import PaginationDep, require_permission
from edgelite.api.error_codes import AuditErrors, CommonErrors
from edgelite.models.common import ApiResponse, PagedResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/logs", tags=["Log Aggregation"])

# 合法日志级别（Python logging 标准级别）
_VALID_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "NOTSET"}


class LogLevelUpdate(BaseModel):
    """日志级别更新请求体。"""

    module: str
    level: str


class LogArchiveRequest(BaseModel):
    """日志归档请求体。"""

    before_date: str | None = None


class LogCleanupRequest(BaseModel):
    """日志清理请求体。"""

    before_date: str | None = None
    confirm: bool = False


def _get_aggregator():
    from edgelite.engine.log_aggregator import get_log_aggregator

    return get_log_aggregator()


def _parse_time(time_str: str | None) -> float | None:
    """将 ISO8601 字符串解析为 Unix 时间戳，失败返回 None。"""
    if not time_str:
        return None
    try:
        dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            from datetime import UTC

            dt = dt.replace(tzinfo=UTC)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def _filter_entries(
    entries: list[dict[str, Any]],
    level: str | None,
    module: str | None,
    search: str | None,
    start_ts: float | None,
    end_ts: float | None,
) -> list[dict[str, Any]]:
    """按条件过滤日志条目。"""
    result = entries
    if level:
        result = [e for e in result if e.get("level") == level]
    if module:
        result = [e for e in result if e.get("module") == module or e.get("logger") == module]
    if search:
        kw = search.lower()
        result = [e for e in result if kw in str(e.get("message", "")).lower()]
    if start_ts is not None:
        result = [e for e in result if e.get("timestamp", 0) >= start_ts]
    if end_ts is not None:
        result = [e for e in result if e.get("timestamp", 0) <= end_ts]
    return result


@router.get("/query", response_model=PagedResponse)
async def query_logs(
    pagination: PaginationDep,
    level: str | None = Query(default=None),
    module: str | None = Query(default=None),
    search: str | None = Query(default=None, description="消息关键字模糊匹配"),
    start: str | None = Query(default=None, description="ISO8601 起始时间"),
    end: str | None = Query(default=None, description="ISO8601 结束时间"),
    _user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """查询聚合日志，支持按级别/模块/时间/关键字过滤。"""
    try:
        agg = _get_aggregator()
        # 取较大上限后内存中分页
        entries = agg.get_entries(level=None, limit=10000)
        start_ts = _parse_time(start)
        end_ts = _parse_time(end)
        filtered = _filter_entries(entries, level, module, search, start_ts, end_ts)
        # 按时间倒序
        filtered.sort(key=lambda e: e.get("timestamp", 0), reverse=True)
        total = len(filtered)
        page = pagination.page
        size = pagination.size
        start_idx = (page - 1) * size
        end_idx = start_idx + size
        paged = filtered[start_idx:end_idx]
        return PagedResponse(data=paged, total=total, page=page, size=size)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("query_logs failed: %s", e)
        raise HTTPException(status_code=503, detail=CommonErrors.SERVICE_NOT_READY) from None


@router.get("/stats", response_model=ApiResponse)
async def get_log_stats(
    _user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
):
    """获取日志统计信息（按级别分桶计数）。"""
    try:
        agg = _get_aggregator()
        entries = agg.get_entries(level=None, limit=10000)
        start_ts = _parse_time(start)
        end_ts = _parse_time(end)
        if start_ts is not None:
            entries = [e for e in entries if e.get("timestamp", 0) >= start_ts]
        if end_ts is not None:
            entries = [e for e in entries if e.get("timestamp", 0) <= end_ts]
        by_level: dict[str, int] = {}
        by_module: dict[str, int] = {}
        for entry in entries:
            lvl = entry.get("level", "UNKNOWN")
            by_level[lvl] = by_level.get(lvl, 0) + 1
            mod = entry.get("module") or entry.get("logger") or "unknown"
            by_module[mod] = by_module.get(mod, 0) + 1
        data = {
            "total": len(entries),
            "by_level": by_level,
            "by_module": by_module,
        }
        return ApiResponse(data=data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_log_stats failed: %s", e)
        raise HTTPException(status_code=503, detail=CommonErrors.SERVICE_NOT_READY) from None


@router.get("/filters", response_model=ApiResponse)
async def get_filters(
    _user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """返回当前缓冲区中可选的 level 与 module 列表。"""
    try:
        agg = _get_aggregator()
        entries = agg.get_entries(level=None, limit=10000)
        levels: set[str] = set()
        modules: set[str] = set()
        for entry in entries:
            lvl = entry.get("level")
            if lvl:
                levels.add(lvl)
            mod = entry.get("module") or entry.get("logger")
            if mod:
                modules.add(mod)
        data = {
            "levels": sorted(levels),
            "modules": sorted(modules),
        }
        return ApiResponse(data=data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_filters failed: %s", e)
        raise HTTPException(status_code=503, detail=CommonErrors.SERVICE_NOT_READY) from None


@router.put("/level", response_model=ApiResponse)
async def set_log_level(
    body: LogLevelUpdate,
    _user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    """动态调整指定 logger 的日志级别。"""
    try:
        level_upper = body.level.upper()
        if level_upper not in _VALID_LEVELS:
            raise HTTPException(status_code=400, detail=CommonErrors.VALIDATION_FAILED)
        target_logger = logging.getLogger(body.module)
        target_logger.setLevel(getattr(logging, level_upper))
        return ApiResponse(
            data={
                "module": body.module,
                "level": level_upper,
                "updated": True,
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("set_log_level failed: %s", e)
        raise HTTPException(status_code=503, detail=CommonErrors.SERVICE_NOT_READY) from None


@router.post("/archive", response_model=ApiResponse)
async def archive_logs(
    body: LogArchiveRequest,
    _user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    """归档指定时间之前的日志。

    注意：当前 LogAggregator 仅维护内存缓冲区，archive 操作为 no-op，
    返回归档元数据以保持 API 契约。
    """
    try:
        before_ts = _parse_time(body.before_date) if body.before_date else None
        # 内存实现：归档为 no-op，仅返回元数据
        data = {
            "archived": True,
            "before_date": body.before_date,
            "before_timestamp": before_ts,
            "archived_count": 0,
            "note": "In-memory aggregator: archive is a no-op",
        }
        return ApiResponse(data=data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("archive_logs failed: %s", e)
        raise HTTPException(status_code=503, detail=AuditErrors.CLEANUP_FAILED) from None


@router.post("/cleanup", response_model=ApiResponse)
async def cleanup_logs(
    body: LogCleanupRequest,
    _user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    """清理日志缓冲区，需 confirm=true 二次确认。"""
    if not body.confirm:
        raise HTTPException(status_code=422, detail=CommonErrors.CONFIRM_REQUIRED)
    try:
        agg = _get_aggregator()
        # 内存实现：直接清空缓冲区
        agg.clear()
        data = {
            "deleted": True,
            "before_date": body.before_date,
            "deleted_count": 0,
        }
        return ApiResponse(data=data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("cleanup_logs failed: %s", e)
        raise HTTPException(status_code=503, detail=AuditErrors.CLEANUP_FAILED) from None
