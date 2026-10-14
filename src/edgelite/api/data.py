"""数据查询API路由"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from edgelite.api.deps import DataServiceDep, require_permission
from edgelite.api.error_codes import AuthzErrors, CommonErrors, DataErrors, DeviceErrors
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
    try:
        has_access = await share_repo.check_user_has_access("device", device_id, user["user_id"])
    except Exception as e:
        # 500-修复: DB 异常不冒泡为 500，转换为 503 服务不可用（权限检查依赖 DB）
        logger.error("check_user_has_access DB failed for device %s: %s", device_id, e)
        raise HTTPException(
            status_code=503,
            detail="ERR_COMMON_DB_NOT_READY",
        ) from None
    if has_access:
        return
    raise HTTPException(status_code=403, detail=AuthzErrors.RESOURCE_OWNERSHIP_DENIED)


# FIX-EL-R2-SEVERE: 查询时间范围上限（防止 start="-10y" 等超大范围导致全表扫描 DoS）
_MAX_QUERY_RANGE = timedelta(days=90)


def _validate_time_range(start: str, stop: str | None) -> None:
    """公共时间范围校验（FIX-EL-R2-SEVERE）。

    规则与 /query 的既有实现一致：
    - stop 提供且两者均可解析时，必须满足 start < stop，且范围不超过 90 天；
    - stop 未提供时默认为 now，start 不得早于 90 天前。
    校验失败抛出 400（INVALID_TIME_RANGE / RANGE_TOO_LARGE）。
    """
    if stop:
        start_dt = _parse_time_to_datetime(start)
        stop_dt = _parse_time_to_datetime(stop)
        if start_dt is not None and stop_dt is not None and start_dt >= stop_dt:
            raise HTTPException(
                status_code=400,
                detail=DataErrors.INVALID_TIME_RANGE,
            )
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
            if (_now - start_dt) > _MAX_QUERY_RANGE:
                raise HTTPException(
                    status_code=400,
                    detail=DataErrors.RANGE_TOO_LARGE,
                )


def _get_access_repos():
    """非 admin 数据导出过滤所需的 (device_service, share_repo)。"""
    from edgelite.app import _app_state
    from edgelite.storage.sqlite_repo import ResourceShareRepo

    device_svc = getattr(_app_state, "device_service", None)
    database = getattr(_app_state, "database", None)
    if device_svc is None or database is None:
        raise HTTPException(status_code=503, detail=CommonErrors.SERVICE_NOT_READY)
    return device_svc, ResourceShareRepo(database, database.write_lock)


async def _get_accessible_device_ids_for_export(user) -> set[str] | None:
    """获取用户可访问（own + shared）的设备 ID 集合；admin 返回 None 表示无限制。"""
    if user["role"] == "admin":
        return None
    device_svc, share_repo = _get_access_repos()
    owned_ids = set(await device_svc.list_device_ids_by_owner(user["user_id"]))
    shared_ids = await share_repo.get_shared_resource_ids(user["user_id"], "device")
    return owned_ids | shared_ids


async def _get_accessible_rule_ids_for_export(user) -> set[str] | None:
    """获取用户可访问（own + shared）的规则 ID 集合；admin 返回 None 表示无限制。"""
    if user["role"] == "admin":
        return None
    from edgelite.app import _app_state
    from edgelite.storage.sqlite_repo import ResourceShareRepo

    rule_svc = getattr(_app_state, "rule_service", None)
    database = getattr(_app_state, "database", None)
    if rule_svc is None or database is None:
        raise HTTPException(status_code=503, detail=CommonErrors.SERVICE_NOT_READY)
    owned_ids: set[str] = set()
    page = 1
    while True:
        rules, total = await rule_svc.list_rules(page=page, size=500, created_by=user["user_id"])
        owned_ids.update(r["rule_id"] for r in rules if r.get("rule_id"))
        if page * 500 >= total or not rules:
            break
        page += 1
    share_repo = ResourceShareRepo(database, database.write_lock)
    shared_ids = await share_repo.get_shared_resource_ids(user["user_id"], "rule")
    return owned_ids | shared_ids


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
    # FIX-EL-R2-SEVERE: 限制最大查询时间范围为 90 天（公共 helper，与 trend/correlation/
    # statistics/multi-point/downsample/export 共用）
    _validate_time_range(start, stop)
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
    # FIX-EL-R2-SEVERE(P2): export 同样受 90 天范围上限约束，防止超大范围导出拖垮存储层
    _validate_time_range(start, stop)
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
    except HTTPException:
        raise
    except (AttributeError, RuntimeError, ConnectionError) as e:
        # 500-修复: 设备服务未初始化/DB 未就绪时返回 503 而非 500
        logger.error("collect stats service not ready: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None
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
    # FIX-EL-R2-SEVERE(P2): 补齐 90 天范围上限（与 /query 公用 helper）
    _validate_time_range(start, stop)
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
    # FIX-EL-R2-SEVERE(P2): 补齐 90 天范围上限（与 /query 公用 helper）
    _validate_time_range(start, stop)
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
    # FIX-EL-R2-SEVERE(P2): 补齐 90 天范围上限（与 /query 公用 helper）
    _validate_time_range(start, stop)
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
    # FIX-EL-R2-SEVERE(P2): 补齐 90 天范围上限（与 /query 公用 helper）
    _validate_time_range(start, stop)
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


# ─── 配置数据导入导出 ───────────────────────────────────────────
# 使用 edgelite.services.data_import_export 中的 DataExportService / DataImportService。
# 与上方时序数据 /export (GET) 不同，此处为配置数据 POST /export 与 POST /import。


class ConfigExportRequest(BaseModel):
    """配置导出请求体。"""

    scope: Literal["devices", "rules", "alarms", "all"] = "all"
    ids: list[str] | None = None
    format: Literal["json", "csv"] = "json"


class ConfigImportRequest(BaseModel):
    """配置导入请求体。"""

    scope: Literal["devices", "rules"] = "devices"
    data: str
    format: Literal["json", "csv"] = "json"
    mode: Literal["skip", "overwrite", "rename", "error"] = "skip"
    atomic: bool = True


def _get_repos() -> tuple[Any, Any, Any]:
    """从 _app_state 获取 (device_repo, rule_repo, alarm_repo)。"""
    from edgelite.app import _app_state

    repos = getattr(_app_state, "_repos", {}) or {}
    device_repo = repos.get("device")
    rule_repo = repos.get("rule")
    alarm_repo = repos.get("alarm")
    if device_repo is None or rule_repo is None:
        raise HTTPException(status_code=503, detail=CommonErrors.DB_NOT_READY)
    return device_repo, rule_repo, alarm_repo


def _get_export_service():
    from edgelite.services.data_import_export import ExportFormat, get_export_service

    device_repo, rule_repo, alarm_repo = _get_repos()
    svc = get_export_service(device_repo, rule_repo, alarm_repo)
    if svc is None:
        raise HTTPException(status_code=503, detail=CommonErrors.SERVICE_NOT_READY)
    return svc, ExportFormat


def _get_import_service():
    from edgelite.services.data_import_export import ExportFormat, ImportMode, get_import_service

    device_repo, rule_repo, _alarm_repo = _get_repos()
    svc = get_import_service(device_repo, rule_repo)
    if svc is None:
        raise HTTPException(status_code=503, detail=CommonErrors.SERVICE_NOT_READY)
    return svc, ExportFormat, ImportMode


@router.post("/export", response_model=ApiResponse)
async def export_config(
    body: ConfigExportRequest,
    user: dict[str, str] = Depends(require_permission(Permission.DATA_EXPORT)),
):
    """导出配置数据（devices/rules/alarms/all）为 JSON 或 CSV。

    FIXED(P1-1 导出越权): 非 admin 用户的导出范围必须以可访问资源为边界——
    device_ids/rule_ids 为 None/空时按 accessible（own + shared）过滤，
    不再允许空 ids 一笔带出全租户配置（含驱动明文凭据）。
    """
    try:
        svc, ExportFormat = _get_export_service()
        fmt = ExportFormat.CSV if body.format == "csv" else ExportFormat.JSON
        # alarms scope 当前 service 不支持，返回空内容以保持契约
        if body.scope == "alarms":
            content = "" if fmt == ExportFormat.CSV else '{"version":"1.0","type":"alarms","count":0,"alarms":[]}'
            return ApiResponse(
                data={
                    "content": content,
                    "scope": body.scope,
                    "format": body.format,
                }
            )
        accessible_device_ids = await _get_accessible_device_ids_for_export(user)
        accessible_rule_ids = (
            await _get_accessible_rule_ids_for_export(user) if body.scope in ("rules", "all") else None
        )

        def _filter_ids(requested: list[str] | None, accessible: set[str] | None) -> list[str] | None:
            """请求 ids 与可访问集合求交；admin（accessible=None）保持原语义。"""
            if accessible is None:
                return requested
            if requested:
                for rid in requested:
                    if rid not in accessible:
                        raise HTTPException(status_code=403, detail=AuthzErrors.RESOURCE_OWNERSHIP_DENIED)
                return requested
            return sorted(accessible)

        if body.scope == "devices":
            device_ids = _filter_ids(body.ids, accessible_device_ids)
            content = await svc.export_devices(device_ids=device_ids, format=fmt)
        elif body.scope == "rules":
            rule_ids = _filter_ids(body.ids, accessible_rule_ids)
            content = await svc.export_rules(rule_ids=rule_ids, format=fmt)
        else:  # all
            if accessible_device_ids is None:
                # admin: 保持原 export_all 全量语义
                content = await svc.export_all(format=fmt)
            else:
                # 非 admin: 以可访问设备/规则组合出 full_backup 形状（export_all 无法传 ids，
                # 在调用方过滤——设备凭据绝不带出可访问范围之外）
                devices_json = json.loads(
                    await svc.export_devices(device_ids=sorted(accessible_device_ids), format=ExportFormat.JSON)
                )
                rules_json = json.loads(
                    await svc.export_rules(rule_ids=sorted(accessible_rule_ids or set()), format=ExportFormat.JSON)
                )
                merged = {
                    "version": "1.0",
                    "type": "full_backup",
                    "exported_at": datetime.now(UTC).isoformat(),
                    "devices": devices_json.get("devices", []),
                    "rules": rules_json.get("rules", []),
                    "device_count": len(devices_json.get("devices", [])),
                    "rule_count": len(rules_json.get("rules", [])),
                }
                content = json.dumps(merged, indent=2, ensure_ascii=False)
        return ApiResponse(
            data={
                "content": content,
                "scope": body.scope,
                "format": body.format,
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("export_config failed: %s", e)
        raise HTTPException(status_code=503, detail=DeviceErrors.EXPORT_FAILED) from None


@router.post("/import", response_model=ApiResponse)
async def import_config(
    body: ConfigImportRequest,
    _user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    """导入配置数据（devices/rules），支持冲突策略与原子事务。"""
    try:
        svc, ExportFormat, ImportMode = _get_import_service()
        fmt = ExportFormat.CSV if body.format == "csv" else ExportFormat.JSON
        mode_map = {
            "skip": ImportMode.SKIP,
            "overwrite": ImportMode.OVERWRITE,
            "rename": ImportMode.RENAME,
            "error": ImportMode.ERROR,
        }
        mode = mode_map.get(body.mode, ImportMode.SKIP)
        if body.scope == "rules":
            result = await svc.import_rules(data=body.data, format=fmt, mode=mode)
        else:
            result = await svc.import_devices(data=body.data, format=fmt, mode=mode)
        return ApiResponse(
            data={
                "success": result.success,
                "total_count": result.total_count,
                "imported_count": result.imported_count,
                "skipped_count": result.skipped_count,
                "error_count": result.error_count,
                "errors": result.errors,
                "warnings": result.warnings,
            }
        )
    except HTTPException:
        raise
    except (ValueError, KeyError) as e:
        # FIXED(P3): 请求体内容解析错误（非法 JSON/CSV、缺字段）属于客户端错误，
        # 返回 400 而非 503——503 语义是服务端暂不可用，会误导调用方重试
        logger.warning("import_config invalid payload: %s", e)
        raise HTTPException(
            status_code=400,
            detail={"error_code": CommonErrors.VALIDATION_FAILED, "message": DeviceErrors.IMPORT_VALIDATION_FAILED},
        ) from None
    except Exception as e:
        logger.error("import_config failed: %s", e)
        raise HTTPException(status_code=503, detail=DeviceErrors.IMPORT_FAILED) from None


# --- 数据降采样端点 ---


class DownsampleRequest(BaseModel):
    device_id: str
    point_name: str
    start: str
    stop: str
    interval: str  # 如 "5m", "1h"
    agg_fn: str = "mean"


_AGG_MAP = {
    "mean": lambda xs: sum(xs) / len(xs) if xs else None,
    "sum": lambda xs: sum(xs) if xs else None,
    "max": lambda xs: max(xs) if xs else None,
    "min": lambda xs: min(xs) if xs else None,
    "first": lambda xs: xs[0] if xs else None,
    "last": lambda xs: xs[-1] if xs else None,
    "count": lambda xs: float(len(xs)),
}

_INTERVAL_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
}


def _parse_interval_seconds(interval: str) -> int:
    import re

    match = re.match(r"^(\d+)([smhdw])$", interval.strip())
    if not match:
        raise ValueError(f"invalid interval format: {interval}")
    return int(match.group(1)) * _INTERVAL_SECONDS.get(match.group(2), 1)


def _extract_value(point: dict) -> float | None:
    if not isinstance(point, dict):
        return None
    for key in ("value", "v", "avg"):
        if key in point and point[key] is not None:
            try:
                return float(point[key])
            except (TypeError, ValueError):
                continue
    return None


def _extract_ts(point: dict) -> float | None:
    if not isinstance(point, dict):
        return None
    for key in ("ts", "time", "timestamp", "t"):
        if key in point and point[key] is not None:
            v = point[key]
            if isinstance(v, (int, float)):
                return float(v)
            try:
                from datetime import datetime as _dt

                if isinstance(v, str):
                    return _dt.fromisoformat(v.replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
    return None


@router.post("/downsample", response_model=ApiResponse)
async def downsample_timeseries(
    svc: DataServiceDep,
    body: DownsampleRequest,
    user: dict[str, str] = Depends(require_permission(Permission.DATA_READ)),
):
    """对原始时序数据按时间间隔聚合降采样"""
    try:
        # FIX-EL-R2-SEVERE(P2): 补齐 90 天范围上限（与 /query 公用 helper）
        _validate_time_range(body.start, body.stop)
        await _check_device_owner(body.device_id, user)
        try:
            interval_sec = _parse_interval_seconds(body.interval)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=DataErrors.INVALID_BUCKET_SIZE) from exc
        # FIXED(P3): interval="0s" 等零间隔会导致分桶除零，直接 400 而非返回误导性 503
        if interval_sec <= 0:
            raise HTTPException(status_code=400, detail=DataErrors.INVALID_BUCKET_SIZE)

        agg_fn = body.agg_fn.lower()
        if agg_fn not in _AGG_MAP:
            raise HTTPException(status_code=400, detail=DataErrors.UNSUPPORTED_AGGREGATE)

        # 取原始数据（最多 50000 条）
        raw = await svc.query_timeseries(
            device_id=body.device_id,
            point_name=body.point_name,
            start=body.start,
            stop=body.stop,
            limit=50000,
        )
        if isinstance(raw, dict):
            points = raw.get("points") or raw.get("data") or []
        elif isinstance(raw, list):
            points = raw
        else:
            points = []

        if not points:
            return ApiResponse(
                data={
                    "device_id": body.device_id,
                    "point_name": body.point_name,
                    "interval": body.interval,
                    "agg_fn": agg_fn,
                    "buckets": [],
                }
            )

        # 按 interval 分桶聚合
        buckets: dict[int, list[float]] = {}
        bucket_meta: dict[int, Any] = {}
        base_ts = None
        for p in points:
            ts = _extract_ts(p) if isinstance(p, dict) else None
            v = _extract_value(p) if isinstance(p, dict) else None
            if ts is None or v is None:
                continue
            if base_ts is None:
                base_ts = ts
            bucket_idx = int((ts - base_ts) // interval_sec)
            buckets.setdefault(bucket_idx, []).append(v)
            bucket_meta.setdefault(bucket_idx, {"ts": ts})

        agg_func = _AGG_MAP[agg_fn]
        result_buckets = []
        for idx in sorted(buckets.keys()):
            values = buckets[idx]
            result_buckets.append(
                {
                    "bucket_index": idx,
                    "ts": bucket_meta[idx]["ts"],
                    "value": agg_func(values),
                    "count": len(values),
                }
            )

        return ApiResponse(
            data={
                "device_id": body.device_id,
                "point_name": body.point_name,
                "interval": body.interval,
                "agg_fn": agg_fn,
                "total_points": len(points),
                "buckets": result_buckets,
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("downsample failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None
