"""设备管理API路由"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Header, Query, status

from edgelite.models.device import DeviceCreate, DeviceUpdate, DeviceResponse, SimulatorCreate, WritePointRequest, DiscoverRequest
from edgelite.models.common import ApiResponse, PagedResponse
from edgelite.api.deps import CurrentUser, require_permission
from edgelite.security.rbac import Permission

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
):
    svc = _get_device_service()
    devices, total = await svc.list_devices(page, size, status, protocol)
    return PagedResponse(data=devices, total=total, page=page, size=size)


@router.post("", response_model=ApiResponse[DeviceResponse], status_code=201)
async def create_device(
    body: DeviceCreate,
    user: CurrentUser = require_permission(Permission.DEVICE_CREATE),
):
    svc = _get_device_service()
    device = await svc.create_device(body.model_dump())
    return ApiResponse(data=device)


@router.get("/{device_id}", response_model=ApiResponse[DeviceResponse])
async def get_device(device_id: str, user: CurrentUser = require_permission(Permission.DEVICE_READ)):
    svc = _get_device_service()
    device = await svc.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="设备不存在")
    return ApiResponse(data=device)


@router.put("/{device_id}", response_model=ApiResponse[DeviceResponse])
async def update_device(
    device_id: str,
    body: DeviceUpdate,
    user: CurrentUser = require_permission(Permission.DEVICE_UPDATE),
):
    svc = _get_device_service()
    data = body.model_dump(exclude_none=True)
    device = await svc.update_device(device_id, data)
    if device is None:
        raise HTTPException(status_code=404, detail="设备不存在")
    return ApiResponse(data=device)


@router.delete("/{device_id}", response_model=ApiResponse)
async def delete_device(device_id: str, user: CurrentUser = require_permission(Permission.DEVICE_DELETE)):
    svc = _get_device_service()
    success, error = await svc.delete_device(device_id)
    if not success:
        raise HTTPException(status_code=409, detail=error or "删除失败")
    return ApiResponse()


@router.get("/{device_id}/points", response_model=ApiResponse)
async def get_device_points(device_id: str, user: CurrentUser = require_permission(Permission.DEVICE_READ)):
    svc = _get_device_service()
    values = await svc.read_points(device_id)
    return ApiResponse(data=values)


@router.post("/{device_id}/points", response_model=ApiResponse)
async def write_device_point(
    device_id: str,
    body: WritePointRequest,
    user: CurrentUser = require_permission(Permission.DEVICE_WRITE_POINT),
):
    svc = _get_device_service()
    success = await svc.write_point(device_id, body.point, body.value)
    if not success:
        raise HTTPException(status_code=400, detail="写入失败")
    return ApiResponse()


@router.post("/simulator", response_model=ApiResponse[DeviceResponse], status_code=201)
async def create_simulator(
    body: SimulatorCreate,
    user: CurrentUser = require_permission(Permission.DEVICE_CREATE),
):
    svc = _get_device_service()
    device = await svc.create_simulator(body.model_dump())
    return ApiResponse(data=device)


@router.post("/discover", response_model=ApiResponse)
async def discover_devices(
    body: DiscoverRequest,
    user: CurrentUser = require_permission(Permission.DEVICE_CREATE),
):
    svc = _get_device_service()
    devices = await svc.discover_devices(body.protocol, body.config)
    return ApiResponse(data=devices)


@router.post("/{device_id}/push", response_model=ApiResponse)
async def push_device_data(device_id: str, body: dict, x_api_key: str = Header(default="")):
    """HTTP Webhook数据推送端点（需API Key认证）"""
    from edgelite.app import _app_state

    config = _app_state.config if hasattr(_app_state, 'config') else None
    if config and hasattr(config, 'server') and hasattr(config.server, 'webhook_api_key') and config.server.webhook_api_key:
        if x_api_key != config.server.webhook_api_key:
            raise HTTPException(status_code=401, detail="Invalid API Key")

    driver = _app_state.driver_registry.get_driver_class("http_webhook")
    if driver and hasattr(driver, "receive_data"):
        await driver.receive_data(device_id, body)
        return ApiResponse()
    raise HTTPException(status_code=400, detail="HTTP Webhook驱动未启动")
