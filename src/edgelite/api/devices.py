"""设备管理API路由"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Header, Query

from edgelite.models.device import (
    DeviceCreate,
    DeviceUpdate,
    DeviceResponse,
    SimulatorCreate,
    WritePointRequest,
    DiscoverRequest,
)
from edgelite.models.common import ApiResponse, PagedResponse
from edgelite.api.deps import CurrentUser, require_permission
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/devices", tags=["设备管理"])


def _get_device_service():
    from edgelite.app import _app_state
    return _app_state.device_service


@router.get("", response_model=PagedResponse[DeviceResponse])
async def list_devices(
    user: CurrentUser = require_permission(Permission.DEVICE_READ),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=1000),
    status: str | None = None,
    protocol: str | None = None,
    search: str | None = None,
):
    try:
        svc = _get_device_service()
        devices, total = await svc.list_devices(page, size, status, protocol, search)
        return PagedResponse(data=devices, total=total, page=page, size=size)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("获取设备列表失败: %s", e)
        raise HTTPException(status_code=500, detail=f"获取设备列表失败: {e}")


@router.post("", response_model=ApiResponse[DeviceResponse], status_code=201)
async def create_device(
    body: DeviceCreate,
    user: CurrentUser = require_permission(Permission.DEVICE_CREATE),
):
    try:
        svc = _get_device_service()
        device = await svc.create_device(body.model_dump())
        return ApiResponse(data=device)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("创建设备失败: %s", e)
        raise HTTPException(status_code=500, detail=f"创建设备失败: {e}")


@router.get("/{device_id}", response_model=ApiResponse[DeviceResponse])
async def get_device(device_id: str, user: CurrentUser = require_permission(Permission.DEVICE_READ)):
    try:
        svc = _get_device_service()
        device = await svc.get_device(device_id)
        if device is None:
            raise HTTPException(status_code=404, detail="设备不存在")
        return ApiResponse(data=device)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("获取设备详情失败: %s", e)
        raise HTTPException(status_code=500, detail=f"获取设备详情失败: {e}")


@router.put("/{device_id}", response_model=ApiResponse[DeviceResponse])
async def update_device(
    device_id: str,
    body: DeviceUpdate,
    user: CurrentUser = require_permission(Permission.DEVICE_UPDATE),
):
    try:
        svc = _get_device_service()
        data = body.model_dump(exclude_none=True)
        device = await svc.update_device(device_id, data)
        if device is None:
            raise HTTPException(status_code=404, detail="设备不存在")
        return ApiResponse(data=device)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("更新设备失败: %s", e)
        raise HTTPException(status_code=500, detail=f"更新设备失败: {e}")


@router.delete("/{device_id}", response_model=ApiResponse)
async def delete_device(device_id: str, user: CurrentUser = require_permission(Permission.DEVICE_DELETE)):
    try:
        svc = _get_device_service()
        success, error = await svc.delete_device(device_id)
        if not success:
            raise HTTPException(status_code=409, detail=error or "删除失败")
        return ApiResponse()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("删除设备失败: %s", e)
        raise HTTPException(status_code=500, detail=f"删除设备失败: {e}")


@router.get("/{device_id}/points", response_model=ApiResponse)
async def get_device_points(device_id: str, user: CurrentUser = require_permission(Permission.DEVICE_READ)):
    try:
        svc = _get_device_service()
        values = await svc.read_points(device_id)
        return ApiResponse(data=values)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("读取设备测点失败: %s", e)
        raise HTTPException(status_code=500, detail=f"读取设备测点失败: {e}")


@router.post("/{device_id}/points", response_model=ApiResponse)
async def write_device_point(
    device_id: str,
    body: WritePointRequest,
    user: CurrentUser = require_permission(Permission.DEVICE_WRITE_POINT),
):
    try:
        svc = _get_device_service()
        success = await svc.write_point(device_id, body.point, body.value)
        if not success:
            raise HTTPException(status_code=400, detail="写入失败")
        return ApiResponse()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("写入测点值失败: %s", e)
        raise HTTPException(status_code=500, detail=f"写入测点值失败: {e}")


@router.post("/simulator", response_model=ApiResponse[DeviceResponse], status_code=201)
async def create_simulator(
    body: SimulatorCreate,
    user: CurrentUser = require_permission(Permission.DEVICE_CREATE),
):
    try:
        svc = _get_device_service()
        device = await svc.create_simulator(body.model_dump())
        return ApiResponse(data=device)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("创建模拟设备失败: %s", e)
        raise HTTPException(status_code=500, detail=f"创建模拟设备失败: {e}")


@router.post("/discover", response_model=ApiResponse)
async def discover_devices(
    body: DiscoverRequest,
    user: CurrentUser = require_permission(Permission.DEVICE_CREATE),
):
    try:
        svc = _get_device_service()
        devices = await svc.discover_devices(body.protocol, body.config)
        return ApiResponse(data=devices)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("设备发现失败: %s", e)
        raise HTTPException(status_code=500, detail=f"设备发现失败: {e}")


@router.post("/{device_id}/push", response_model=ApiResponse)
async def push_device_data(device_id: str, body: dict, x_api_key: str = Header(default=""), authorization: str = Header(default="")):
    """HTTP Webhook数据推送端点"""
    if not device_id or not isinstance(device_id, str) or len(device_id) > 128:
        raise HTTPException(status_code=400, detail="无效的设备ID")
    if not isinstance(body, dict) or not body:
        raise HTTPException(status_code=400, detail="推送数据不能为空")
    from edgelite.app import _app_state
    config = _app_state.config

    if config and hasattr(config, 'webhook_auth'):
        from edgelite.engine.webhook_auth import WebhookAuthMiddleware
        auth_mw = WebhookAuthMiddleware(
            mode=config.webhook_auth.mode,
            token=config.webhook_auth.token,
            username=config.webhook_auth.username,
            password=config.webhook_auth.password,
        )
        if not auth_mw.verify(authorization):
            raise HTTPException(status_code=401, detail="Webhook认证失败")

    if config and getattr(config, 'server', None) and getattr(config.server, 'webhook_api_key', None):
        import hmac
        if not x_api_key or not hmac.compare_digest(x_api_key, config.server.webhook_api_key):
            raise HTTPException(status_code=401, detail="Invalid API Key")
    else:
        if not (config and hasattr(config, 'webhook_auth') and config.webhook_auth.mode != "none"):
            raise HTTPException(status_code=401, detail="API Key not configured")

    try:
        driver = _app_state.driver_registry.get_driver_class("http_webhook")
        if driver and hasattr(driver, "receive_data"):
            await driver.receive_data(device_id, body)
            return ApiResponse()
        raise HTTPException(status_code=400, detail="HTTP Webhook驱动未启动")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("推送设备数据失败: %s", e)
        raise HTTPException(status_code=500, detail=f"推送设备数据失败: {e}")
