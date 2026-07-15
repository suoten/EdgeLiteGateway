"""数据查询API路由"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from edgelite.api.deps import DataServiceDep, require_permission
from edgelite.api.error_codes import AuthzErrors, DataErrors
from edgelite.models.common import ApiResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/data", tags=["Data"])


def _safe_filename(name: str) -> str:
    return re.sub(r"[^\w\-.]", "_", name)


# FIXED: 时间范围校验——将 Flux 风格时间字符串解析为可比较的 datetime
# 支持相对时间（-1h/-30m/-2d 等）和绝对时间（RFC3339/ISO8601），纯数字按纳秒处理
_REL_TIME_RE = re.compile(r"^-(\d+)([smhdwMy])$")
_REL_UNIT_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
    "M": 2592000,
    "y": 31536000,
}


def _parse_time_to_datetime(time_str: str) -> datetime | None:
    """将时间字符串解析为 UTC datetime，无法解析时返回 None。"""
    # 相对时间: -1h, -30m, -2d 等
    rel_match = _REL_TIME_RE.match(time_str)
    if rel_match:
        amount = int(rel_match.group(1))
        unit = rel_match.group(2)
        seconds = amount * _REL_UNIT_SECONDS.get(unit, 1)
        return datetime.now(UTC) - timedelta(seconds=seconds)
    # 绝对时间: 2024-01-01T00:00:00Z
    try:
        dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (ValueError, TypeError):
        pass
    # 纯数字（纳秒时间戳）
    try:
        ns = int(time_str)
        return datetime.fromtimestamp(ns / 1e9, tz=UTC)
    except (ValueError, TypeError, OSError, OverflowError):
        pass
    return None


async def _check_device_owner(device_id: str, user) -> None:
    if user["role"] == "admin":
        return
    from edgelite.app import _app_state

    device_svc = _app_state.device_service
    device = await device_svc.get_device(device_id)
    if device is None:
        # FIXED-P2: 设备不存在也返回403而非404，防止攻击者通过404/403差异枚举设备ID
        # 之前：设备不存在返回404，权限不足返回403，攻击者可枚举有效设备ID
        # 之后：统一返回403，不泄露设备是否存在的信息
        raise HTTPException(status_code=403, detail=AuthzErrors.RESOURCE_OWNERSHIP_DENIED)
    if device.get("created_by") == user["user_id"]:
        return
    from edgelite.storage.sqlite_repo import (
        ResourceShareRepo,  # FIXED-P1: 检查resource_shares共享权限
    )

    share_repo = ResourceShareRepo(_app_state.database, _app_state.database.write_lock)
    has_access = await share_repo.check_user_has_access("device", device_id, user["user_id"])
    if has_access:
        return
    raise HTTPException(status_code=403, detail=AuthzErrors.RESOURCE_OWNERSHIP_DENIED)


@router.get("/query", response_model=ApiResponse)
async def query_timeseries(
    svc: DataServiceDep,
    device_id: str = Query(...),
    point_name: str = Query(...),
    start: str = Query(..., description="Start time (RFC3339 or relative like -1h)"),
    stop: str | None = None,
    aggregate: str | None = None,
    interval: str | None = Query(None, description="Aggregation window e.g. 5m, 1h (required when aggregate is set)"),
    limit: int = Query(
        10000, ge=1, le=50000, description="Max records to return"
    ),  # FIXED-P4: 降低单次查询上限从100000到50000，防止OOM
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    user: dict[str, str] = Depends(require_permission(Permission.DATA_READ)),
):
    """查询设备位点的时序数据，支持聚合和分页。"""
    valid_aggregates = {"mean", "max", "min", "last", "first", "sum", "count", "median", "stddev"}
    if aggregate and aggregate.lower() not in valid_aggregates:
        raise HTTPException(
            status_code=400,
            detail=DataErrors.UNSUPPORTED_AGGREGATE,
        )
    # FIXED: 校验时间范围 start < stop，防止无效范围查询浪费资源
    # 当 stop 提供且两者均可解析为时间时，必须满足 start < stop，否则返回 400
    if stop:
        start_dt = _parse_time_to_datetime(start)
        stop_dt = _parse_time_to_datetime(stop)
        if start_dt is not None and stop_dt is not None and start_dt >= stop_dt:
            raise HTTPException(
                status_code=400,
                detail=DataErrors.INVALID_TIME_RANGE,
            )
        # FIX-EL-R2-SEVERE: 限制最大查询时间范围为 90 天，防止 start="-10y" 等超大范围
        # 导致 InfluxDB 全表扫描 CPU/内存耗尽（DoS）。即使有 limit 限制返回行数，
        # InfluxDB 仍需扫描整个时间范围再取前 N 条。
        _MAX_QUERY_RANGE = timedelta(days=90)
        if start_dt is not None and stop_dt is not None and (stop_dt - start_dt) > _MAX_QUERY_RANGE:
            raise HTTPException(
                status_code=400,
                detail=DataErrors.RANGE_TOO_LARGE,
            )
    else:
        # stop 未提供时默认为 now，校验 start 不超过 90 天前
        start_dt = _parse_time_to_datetime(start)
        if start_dt is not None:
            _now = datetime.now(UTC)
            if (_now - start_dt) > timedelta(days=90):
                raise HTTPException(
                    status_code=400,
                    detail=DataErrors.RANGE_TOO_LARGE,
                )
    # FIXED-Bug31: aggregate 是聚合函数名，interval 是窗口大小（如 "5m"、"1h"）
    # 之前：API 校验 aggregate 为函数名，但存储层期望窗口大小（^\d+[smh]$），两条路径都不可用
    # 现在：传给存储层的 aggregate 参数使用 interval（窗口大小），函数名通过 agg_fn 传入
    # FIXED(严重): 原代码未传 agg_fn，导致存储层硬编码 mean，用户请求 max/min 始终返回 mean
    await _check_device_owner(device_id, user)
    try:
        data = await svc.query_timeseries(
            device_id,
            point_name,
            start,
            stop,
            aggregate=interval if interval else None,
            agg_fn=aggregate if aggregate else None,
            limit=limit,
            offset=offset,
        )
        return ApiResponse(data=data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Timeseries query failed: %s", e)  # FIXED-P1: 原问题-500异常无日志，生产环境无法排查
        raise HTTPException(status_code=500, detail=DataErrors.QUERY_FAILED) from None
    # FIXED-P2: 时序数据查询添加分页限制


@router.get("/export")
async def export_data(
    svc: DataServiceDep,
    device_id: str = Query(...),
    point_name: str = Query(...),
    start: str = Query(...),
    stop: str | None = None,
    _format: str = Query("csv", pattern="^(csv|json)$", alias="format"),
    limit: int = Query(
        50000, ge=1, le=50000, description="Max records to export"
    ),  # FIXED(严重): 原问题-export_data无limit参数,service层默认100000,大范围导出可致OOM;修复-API层添加limit参数,上限50000  # noqa: E501
    user: dict[str, str] = Depends(require_permission(Permission.DATA_EXPORT)),
):
    """导出设备时序数据为 CSV 或 JSON 格式，支持流式输出防止 OOM。"""
    _fmt = _format
    await _check_device_owner(device_id, user)

    # R9-S-16 修复: 原问题-export_data 加载所有结果到 BytesIO，非真正流式;
    # 修复-使用 StreamingResponse + 生成器函数，分批查询（1000条/批）并流式输出
    media_type = "text/csv" if _fmt == "csv" else "application/json"
    filename = f"{_safe_filename(device_id)}_{_safe_filename(point_name)}.{_fmt}"

    async def _stream_generator():
        """流式生成器：分批查询数据并逐步输出"""
        try:
            if _fmt == "csv":
                # CSV 格式：先输出 BOM 头（UTF-8-SIG）
                yield b"\xef\xbb\xbf"
            async for chunk in svc.stream_export_data(
                device_id, point_name, start, stop, _fmt, limit=limit, batch_size=1000
            ):
                yield chunk
        except Exception as e:
            logger.error("Streaming export failed: %s", e)

    return StreamingResponse(
        _stream_generator(),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/stats", response_model=ApiResponse)
async def get_collect_stats(
    user: dict[str, str] = Depends(require_permission(Permission.DATA_READ)),
):
    """获取设备数据采集统计信息（采集频率、成功率、延迟等）。"""
    try:
        device_svc = _get_device_service()
        # R9-S-08 修复: 原问题-分页拉取全部设备到内存做聚合，设备数量大时内存占用高;
        # 修复-使用SQL聚合查询(COUNT GROUP BY status)替代全量加载，仅返回计数字典
        if user["role"] == "admin":
            # admin: 统计全部设备，无需过滤
            status_counts = await device_svc.get_status_counts()
        else:
            # 非admin: 先获取可访问设备ID(拥有+共享)，再按ID范围聚合统计
            from edgelite.app import _app_state
            from edgelite.storage.sqlite_repo import ResourceShareRepo

            _dsvc = _app_state.device_service
            owned_ids_list = await _dsvc.list_device_ids_by_owner(user["user_id"])
            owned_ids = set(owned_ids_list)
            # FIXED-P2: 包含共享设备的访问权限，与_check_device_owner逻辑一致
            share_repo = ResourceShareRepo(_app_state.database, _app_state.database.write_lock)
            shared_ids = await share_repo.get_shared_resource_ids(user["user_id"], "device")
            accessible_ids = list(owned_ids | shared_ids)
            status_counts = await device_svc.get_status_counts(accessible_ids)

        total_devices = sum(status_counts.values())
        online_devices = status_counts.get("online", 0)
        error_devices = status_counts.get("error", 0)
        stats = {
            # today_points 字段在设备表中不存在，保持为0（原实现也始终为0）
            "total_points_today": 0,
            "device_stats": {
                "total": total_devices,
                "online": online_devices,
                "offline": total_devices - online_devices - error_devices,
                "error": error_devices,
            },
            "success_rate": round(online_devices / total_devices, 4) if total_devices > 0 else 0.0,
        }
        return ApiResponse(data=stats)
    except Exception as e:
        logger.error("Failed to get collection stats: %s", e)
        raise HTTPException(status_code=500, detail=DataErrors.QUERY_FAILED) from e


def _get_device_service():
    from edgelite.app import _app_state

    return _app_state.device_service


@router.get("/trend", response_model=ApiResponse)
async def query_trend(
    svc: DataServiceDep,
    device_id: str = Query(...),
    point_name: str = Query(...),
    start: str = Query("-24h"),
    stop: str | None = None,
    bucket_size: str = Query("1h"),
    user: dict[str, str] = Depends(require_permission(Permission.DATA_READ)),
):
    """Query data trend with linear regression analysis"""
    # FIXED(一般): 原问题-bucket_size无格式校验,恶意值可注入Flux查询;修复-校验为合法时间窗口格式
    if not re.match(r"^\d+[smhdwMy]$", bucket_size):
        raise HTTPException(status_code=400, detail=DataErrors.INVALID_BUCKET_SIZE)
    try:
        await _check_device_owner(device_id, user)
        data = await svc.query_trend(device_id, point_name, start, stop, bucket_size)
        return ApiResponse(data=data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Trend query failed: %s", e)  # FIXED-P1: 原问题-500异常无日志
        raise HTTPException(status_code=500, detail=DataErrors.QUERY_FAILED) from None


@router.get("/correlation", response_model=ApiResponse)
async def query_correlation(
    svc: DataServiceDep,
    device_id: str = Query(...),
    point1: str = Query(...),
    point2: str = Query(...),
    start: str = Query("-24h"),
    stop: str | None = None,
    user: dict[str, str] = Depends(require_permission(Permission.DATA_READ)),
):
    """Calculate correlation between two data points"""
    try:
        await _check_device_owner(device_id, user)
        data = await svc.query_correlation(device_id, point1, point2, start, stop)
        return ApiResponse(data=data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Correlation query failed: %s", e)  # FIXED-P1: 原问题-500异常无日志
        raise HTTPException(status_code=500, detail=DataErrors.QUERY_FAILED) from None


@router.get("/statistics", response_model=ApiResponse)
async def get_statistics(
    svc: DataServiceDep,
    device_id: str = Query(...),
    point_name: str = Query(...),
    start: str = Query("-24h"),
    stop: str | None = None,
    aggregate: str | None = None,
    user: dict[str, str] = Depends(require_permission(Permission.DATA_READ)),
):
    """Get statistical summary of data points"""
    # R5-G-09: API 层 aggregate 白名单校验，与 /query 端点一致
    valid_aggregates = {"mean", "max", "min", "last", "first", "sum", "count", "median", "stddev"}
    if aggregate and aggregate.lower() not in valid_aggregates:
        raise HTTPException(status_code=400, detail=DataErrors.UNSUPPORTED_AGGREGATE)
    try:
        await _check_device_owner(device_id, user)
        data = await svc.get_statistics(device_id, point_name, start, stop, aggregate)
        return ApiResponse(data=data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Statistics query failed: %s", e)  # FIXED-P1: 原问题-500异常无日志
        raise HTTPException(status_code=500, detail=DataErrors.QUERY_FAILED) from None


@router.get("/multi-point", response_model=ApiResponse)
async def query_multi_point(
    svc: DataServiceDep,
    device_id: str = Query(...),
    point_names: str = Query(..., description="Comma-separated point names"),
    start: str = Query("-1h"),
    stop: str | None = None,
    aggregate: str | None = None,
    user: dict[str, str] = Depends(require_permission(Permission.DATA_READ)),
):
    """Query multiple data points at once"""
    # R5-G-09: API 层 aggregate 白名单校验，与 /query 端点一致
    valid_aggregates = {"mean", "max", "min", "last", "first", "sum", "count", "median", "stddev"}
    if aggregate and aggregate.lower() not in valid_aggregates:
        raise HTTPException(status_code=400, detail=DataErrors.UNSUPPORTED_AGGREGATE)
    await _check_device_owner(device_id, user)
    try:
        names = [n.strip() for n in point_names.split(",") if n.strip()]
        if not names:
            raise HTTPException(
                status_code=400, detail=DataErrors.POINT_NAME_REQUIRED
            )  # FIXED-P2: 原问题-空point_names返回UNSUPPORTED_AGGREGATE错误码，语义不匹配
        # FIXED-P2: 原问题-point_names无数量上限，可传超长逗号列表导致查询性能退化/OOM;
        # 修复-限制单次最多查询100个点位
        if len(names) > 100:
            raise HTTPException(status_code=400, detail=DataErrors.TOO_MANY_POINTS)
        data = await svc.query_multi_point(device_id, names, start, stop, aggregate)
        return ApiResponse(data=data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Multi-point query failed: %s", e)  # FIXED-P1: 原问题-500异常无日志
        raise HTTPException(status_code=500, detail=DataErrors.QUERY_FAILED) from None
