"""数据库监控 API 路由

使用 edgelite.services.db_monitor.get_db_monitor() 单例，
提供连接池统计与慢查询信息。

注意：
- DatabaseMonitor 仅维护 active/idle/waiting 计数与慢查询总数，
  不存储具体慢查询语句列表。本端点返回空 queries 列表与计数，
  满足前端契约（不返回 501 占位）。
- 前端期望的 pool_size/min_size/max_size/checked_out/overflow 等字段
  由现有 monitor 字段映射而来；未提供的字段使用 0 默认值。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from edgelite.api.deps import require_permission
from edgelite.api.error_codes import CommonErrors, DatabaseErrors
from edgelite.models.common import ApiResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/db-monitor", tags=["Database Monitor"])


def _get_monitor():
    from edgelite.services.db_monitor import get_db_monitor

    return get_db_monitor()


@router.get("/pool-stats", response_model=ApiResponse)
async def get_pool_stats(
    _user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """获取数据库连接池统计信息。"""
    try:
        monitor = _get_monitor()
        stats = monitor.get_pool_stats()
        # 映射到前端契约字段；monitor 未提供的字段以 0 填充
        data = {
            "pool_name": "default",
            "pool_size": stats.get("active_connections", 0) + stats.get("idle_connections", 0),
            "min_size": 0,
            "max_size": 0,
            "checked_out": stats.get("active_connections", 0),
            "overflow": 0,
            "checked_out_timeout_mins": 0,
            "active_connections": stats.get("active_connections", 0),
            "idle_connections": stats.get("idle_connections", 0),
            "waiting_count": stats.get("waiting_count", 0),
        }
        return ApiResponse(data=data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_pool_stats failed: %s", e)
        raise HTTPException(status_code=503, detail=DatabaseErrors.POOL_STATS_FAILED) from None


@router.get("/slow-queries", response_model=ApiResponse)
async def get_slow_queries(
    _user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
    limit: int = Query(default=50, ge=1, le=500),
):
    """获取慢查询统计信息。

    DatabaseMonitor 仅记录慢查询计数，不存储具体查询文本，
    因此 queries 列表为空，count 反映累计慢查询数。
    """
    try:
        monitor = _get_monitor()
        count = monitor.get_slow_query_count()
        data = {
            "queries": [],
            "count": count,
            "limit": limit,
        }
        return ApiResponse(data=data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_slow_queries failed: %s", e)
        raise HTTPException(status_code=503, detail=DatabaseErrors.SLOW_QUERY_FAILED) from None
