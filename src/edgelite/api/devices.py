"""设备管理API路由"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Header, HTTPException

from edgelite.api.deps import (
    ConfigDep,
    CurrentUser,
    DeviceServiceDep,
    PaginationDep,
    SchedulerDep,
    require_permission,
)
from edgelite.api.error_codes import DeviceErrors
from edgelite.models.common import ApiResponse, PagedResponse
from edgelite.models.device import (
    DeviceCreate,
    DeviceResponse,
    DeviceUpdate,
    DiscoverRequest,
    SimulatorCreate,
    WritePointRequest,
)
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/devices", tags=["Devices"])


@router.get("", response_model=PagedResponse[DeviceResponse])
async def list_devices(
    svc: DeviceServiceDep,
    user: CurrentUser = require_permission(Permission.DEVICE_READ),
    pagination: PaginationDep = None,  # FIXED: 原问题-默认值None导致类型检查误判，但Python语法要求有默认值（前参有默认值）
    status: str | None = None,
    protocol: str | None = None,
    search: str | None = None,
):
    try:
        devices, total = await svc.list_devices(pagination.page, pagination.size, status, protocol, search)
        return PagedResponse(data=devices, total=total, page=pagination.page, size=pagination.size)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("list_devices failed: %s", e)
        # FIXED: 原问题-中文硬编码detail
        raise HTTPException(status_code=500, detail=DeviceErrors.LIST_FAILED) from e


@router.post("", response_model=ApiResponse[DeviceResponse], status_code=201)
async def create_device(
    body: DeviceCreate,
    svc: DeviceServiceDep,
    user: CurrentUser = require_permission(Permission.DEVICE_CREATE),
):
    try:
        device = await svc.create_device(body.model_dump())
        return ApiResponse(data=device)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
        logger.error("create_device failed: %s", e)
        # FIXED: 原问题-中文硬编码detail
        raise HTTPException(status_code=500, detail=DeviceErrors.CREATE_FAILED) from e


@router.get("/{device_id}", response_model=ApiResponse[DeviceResponse])
async def get_device(
    device_id: str,
    svc: DeviceServiceDep,
    user: CurrentUser = require_permission(Permission.DEVICE_READ),
):
    try:
        device = await svc.get_device(device_id)
        if device is None:
            # FIXED: 原问题-中文硬编码detail
            raise HTTPException(status_code=404, detail=DeviceErrors.NOT_FOUND)
        return ApiResponse(data=device)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_device failed: %s", e)
        # FIXED: 原问题-中文硬编码detail
        raise HTTPException(status_code=500, detail=DeviceErrors.GET_FAILED) from e


@router.put("/{device_id}", response_model=ApiResponse[DeviceResponse])
async def update_device(
    device_id: str,
    body: DeviceUpdate,
    svc: DeviceServiceDep,
    user: CurrentUser = require_permission(Permission.DEVICE_UPDATE),
):
    try:
        data = body.model_dump(exclude_none=True)
        device = await svc.update_device(device_id, data)
        if device is None:
            # FIXED: 原问题-中文硬编码detail
            raise HTTPException(status_code=404, detail=DeviceErrors.NOT_FOUND)
        return ApiResponse(data=device)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("update_device failed: %s", e)
        # FIXED: 原问题-中文硬编码detail
        raise HTTPException(status_code=500, detail=DeviceErrors.UPDATE_FAILED) from e


@router.delete("/{device_id}", response_model=ApiResponse)
async def delete_device(
    device_id: str,
    svc: DeviceServiceDep,
    user: CurrentUser = require_permission(Permission.DEVICE_DELETE),
):
    try:
        success, error = await svc.delete_device(device_id)
        if not success:
            # FIXED: 原问题-中文硬编码detail
            raise HTTPException(status_code=409, detail=error or DeviceErrors.DELETE_FAILED)
        return ApiResponse()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("delete_device failed: %s", e)
        # FIXED: 原问题-中文硬编码detail
        raise HTTPException(status_code=500, detail=DeviceErrors.DELETE_FAILED) from e


@router.get("/{device_id}/points", response_model=ApiResponse)
async def get_device_points(
    device_id: str,
    svc: DeviceServiceDep,
    user: CurrentUser = require_permission(Permission.DEVICE_READ),
):
    try:
        values = await svc.read_points(device_id)
        return ApiResponse(data=values)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_device_points failed: %s", e)
        # FIXED: 原问题-中文硬编码detail
        raise HTTPException(status_code=500, detail=DeviceErrors.POINTS_FAILED) from e


@router.post("/{device_id}/points", response_model=ApiResponse)
async def write_device_point(
    device_id: str,
    body: WritePointRequest,
    svc: DeviceServiceDep,
    user: CurrentUser = require_permission(Permission.DEVICE_WRITE_POINT),
):
    try:
        success = await svc.write_point(device_id, body.point, body.value)
        if not success:
            # FIXED: 原问题-中文硬编码detail
            raise HTTPException(status_code=400, detail=DeviceErrors.WRITE_FAILED)
        return ApiResponse()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("write_device_point failed: %s", e)
        # FIXED: 原问题-中文硬编码detail
        raise HTTPException(status_code=500, detail=DeviceErrors.WRITE_FAILED) from e


@router.post("/simulator", response_model=ApiResponse[DeviceResponse], status_code=201)
async def create_simulator(
    body: SimulatorCreate,
    svc: DeviceServiceDep,
    user: CurrentUser = require_permission(Permission.DEVICE_CREATE),
):
    try:
        device = await svc.create_simulator(body.model_dump())
        return ApiResponse(data=device)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("create_simulator failed: %s", e)
        # FIXED: 原问题-中文硬编码detail
        raise HTTPException(status_code=500, detail=DeviceErrors.SIMULATOR_FAILED) from e


@router.post("/discover", response_model=ApiResponse)
async def discover_devices(
    body: DiscoverRequest,
    svc: DeviceServiceDep,
    user: CurrentUser = require_permission(Permission.DEVICE_CREATE),
):
    try:
        devices = await svc.discover_devices(body.protocol, body.config)
        return ApiResponse(data=devices)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("discover_devices failed: %s", e)
        # FIXED: 原问题-中文硬编码detail
        raise HTTPException(status_code=500, detail=DeviceErrors.DISCOVER_FAILED) from e


@router.post("/{device_id}/push", response_model=ApiResponse)
async def push_device_data(
    device_id: str,
    body: dict,
    config: ConfigDep,
    svc: DeviceServiceDep,
    x_api_key: str = Header(default=""),
    authorization: str = Header(default=""),
):
    if not device_id or not isinstance(device_id, str) or len(device_id) > 128:
        raise HTTPException(status_code=400, detail=DeviceErrors.PUSH_INVALID_ID)
    if not isinstance(body, dict) or not body:
        raise HTTPException(status_code=400, detail=DeviceErrors.PUSH_EMPTY)
    # FIXED: 原问题-push_data的body:dict无schema校验，可注入任意字段
    # 现添加基础结构校验：值必须为dict且键为字符串
    for k, v in body.items():
        if not isinstance(k, str) or len(k) > 128:
            raise HTTPException(status_code=400, detail=DeviceErrors.PUSH_INVALID_KEY)

    # FIXED: 原问题-config.webhook_auth链式属性访问无空值保护，webhook_auth为None时崩溃
    webhook_auth = getattr(config, "webhook_auth", None) if config else None
    if webhook_auth and webhook_auth.mode != "none":
        from edgelite.engine.webhook_auth import WebhookAuthMiddleware

        auth_mw = WebhookAuthMiddleware(
            mode=webhook_auth.mode,
            token=getattr(webhook_auth, "token", ""),
            username=getattr(webhook_auth, "username", ""),
            password=getattr(webhook_auth, "password", ""),
        )
        if not auth_mw.verify(authorization):
            # FIXED: 原问题-中文硬编码detail
            raise HTTPException(status_code=401, detail=DeviceErrors.WEBHOOK_AUTH_FAILED)

    if (
        config
        and getattr(config, "server", None)
        and getattr(config.server, "webhook_api_key", None)
    ):
        import hmac

        if not x_api_key or not hmac.compare_digest(x_api_key, config.server.webhook_api_key):
            raise HTTPException(status_code=401, detail=DeviceErrors.API_KEY_INVALID)
    else:
        if not (webhook_auth and webhook_auth.mode != "none"):
            raise HTTPException(status_code=401, detail=DeviceErrors.API_KEY_NOT_CONFIGURED)

    try:
        driver = svc._driver_instances.get(device_id) if svc else None
        if driver and hasattr(driver, "receive_data"):
            await driver.receive_data(device_id, body)
            return ApiResponse()
        # FIXED: 原问题-中文硬编码detail
        raise HTTPException(status_code=400, detail=DeviceErrors.PUSH_DRIVER_NOT_READY)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("push_device_data failed: %s", e)
        # FIXED: 原问题-中文硬编码detail
        raise HTTPException(status_code=500, detail=DeviceErrors.PUSH_FAILED) from e


@router.get("/collect-stats", response_model=ApiResponse)
async def get_collect_stats(
    scheduler: SchedulerDep,
    user: CurrentUser = require_permission(Permission.DEVICE_READ),
):
    try:
        stats = scheduler.get_collect_stats()
        return ApiResponse(data={k: {
            "device_id": v.device_id,
            "avg_latency_ms": round(v.avg_latency_ms, 2),
            "max_latency_ms": round(v.max_latency_ms, 2),
            "total_calls": v.total_calls,
            "timeout_count": v.timeout_count,
            "last_collect_at": v.last_collect_at,
        } for k, v in stats.items()})
    except Exception as e:
        logger.error("get_collect_stats failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/device-quality-stats", response_model=ApiResponse)
async def get_device_quality_stats(
    scheduler: SchedulerDep,
    user: CurrentUser = require_permission(Permission.DEVICE_READ),
):
    try:
        stats = scheduler.get_device_quality_stats()
        return ApiResponse(data={k: {
            "device_id": v.device_id,
            "success_count": v.success_count,
            "error_count": v.error_count,
            "total_count": v.total_count,
            "error_rate": round(v.error_rate, 4),
        } for k, v in stats.items()})
    except Exception as e:
        logger.error("get_device_quality_stats failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e
