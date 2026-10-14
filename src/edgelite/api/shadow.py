"""设备影子 API 路由

使用 edgelite.services.shadow_service.ShadowService 维护设备 reported/desired 状态副本。
服务实例由 bootstrap 注入到 _app_state.shadow_service。
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel

from edgelite.api.deps import PaginationDep, require_permission
from edgelite.api.error_codes import AuthzErrors, CommonErrors, DeviceErrors
from edgelite.models.common import ApiResponse, PagedResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/shadows", tags=["Device Shadow"])


class ShadowStateUpdate(BaseModel):
    """影子状态更新请求体。

    兼容两种字段命名：
    - task 规范: {"state": {...}}
    - 前端实际调用: {"desired": {...}} / {"reported": {...}}
    解析时优先使用 state，其次 desired/reported。
    """

    state: dict[str, Any] | None = None
    desired: dict[str, Any] | None = None
    reported: dict[str, Any] | None = None
    quality: str | None = None


def _get_shadow_service():
    from edgelite.app import _app_state

    svc = getattr(_app_state, "shadow_service", None)
    if svc is None:
        raise HTTPException(status_code=503, detail=CommonErrors.SERVICE_NOT_READY)
    return svc


async def _check_device_owner(device_id: str, user) -> None:
    """设备归属校验（FIXED P3: 对齐 data.py 的 _check_device_owner 模式）。

    原问题：shadow 全部端点无归属校验，任何持有 DEVICE_READ/UPDATE 权限的用户
    可读写任意设备的影子。规则：
    - admin 直通；
    - 设备不存在（或非本人且未共享）统一 403，防止 404/403 差异枚举设备 ID。
    """
    if user["role"] == "admin":
        return
    from edgelite.app import _app_state

    device_svc = getattr(_app_state, "device_service", None)
    if device_svc is None:
        raise HTTPException(status_code=503, detail=CommonErrors.SERVICE_NOT_READY)
    device = await device_svc.get_device(device_id)
    if device is None or device.get("created_by") != user["user_id"]:
        raise HTTPException(status_code=403, detail=AuthzErrors.RESOURCE_OWNERSHIP_DENIED)
    from edgelite.storage.sqlite_repo import ResourceShareRepo

    share_repo = ResourceShareRepo(_app_state.database, _app_state.database.write_lock)
    try:
        has_access = await share_repo.check_user_has_access("device", device_id, user["user_id"])
    except Exception as e:
        logger.error("shadow check_user_has_access DB failed for device %s: %s", device_id, e)
        raise HTTPException(status_code=503, detail=CommonErrors.DB_NOT_READY) from None
    if not has_access:
        raise HTTPException(status_code=403, detail=AuthzErrors.RESOURCE_OWNERSHIP_DENIED)


async def _get_accessible_device_ids(user) -> set[str] | None:
    """获取用户可访问（own + shared）的设备 ID 集合；admin 返回 None 表示无限制。"""
    if user["role"] == "admin":
        return None
    from edgelite.app import _app_state
    from edgelite.storage.sqlite_repo import ResourceShareRepo

    device_svc = getattr(_app_state, "device_service", None)
    if device_svc is None:
        raise HTTPException(status_code=503, detail=CommonErrors.SERVICE_NOT_READY)
    owned_ids = set(await device_svc.list_device_ids_by_owner(user["user_id"]))
    share_repo = ResourceShareRepo(_app_state.database, _app_state.database.write_lock)
    shared_ids = await share_repo.get_shared_resource_ids(user["user_id"], "device")
    return owned_ids | shared_ids


def _compute_delta(reported: dict[str, Any], desired: dict[str, Any]) -> dict[str, Any]:
    """计算 desired 与 reported 的差异（desired 中存在且与 reported 不同的键）。"""
    delta: dict[str, Any] = {}
    for k, v in desired.items():
        if k not in reported or reported.get(k) != v:
            delta[k] = v
    return delta


def _shadow_version(shadow: dict[str, Any]) -> int:
    """从影子 ts 字段派生版本号（毫秒时间戳）。"""
    ts = shadow.get("ts", 0)
    try:
        return int(float(ts) * 1000)
    except (TypeError, ValueError):
        return 0


def _build_shadow_info(device_id: str, shadow: dict[str, Any]) -> dict[str, Any]:
    reported = shadow.get("reported", {}) or {}
    desired = shadow.get("desired", {}) or {}
    return {
        "device_id": device_id,
        "reported_count": len(reported),
        "desired_count": len(desired),
        "delta_count": len(_compute_delta(reported, desired)),
        "version": _shadow_version(shadow),
        "last_updated": shadow.get("ts", 0),
    }


def _build_shadow_detail(device_id: str, shadow: dict[str, Any]) -> dict[str, Any]:
    reported = shadow.get("reported", {}) or {}
    desired = shadow.get("desired", {}) or {}
    return {
        "device_id": device_id,
        "reported": reported,
        "desired": desired,
        "metadata": {},
        "version": _shadow_version(shadow),
        "last_updated": shadow.get("ts", 0),
        "delta": _compute_delta(reported, desired),
    }


@router.get("", response_model=ApiResponse)
async def list_shadows(
    pagination: PaginationDep,
    device_id: str | None = Query(default=None, description="按设备 ID 过滤", max_length=128),
    user: dict[str, str] = Depends(require_permission(Permission.DEVICE_READ)),
):
    """列出所有设备影子（支持按 device_id 过滤与分页）。"""
    try:
        svc = _get_shadow_service()
        # FIXED(P3): 非 admin 只能看到自己可访问（own + shared）设备的影子
        accessible_ids = await _get_accessible_device_ids(user)
        if device_id:
            await _check_device_owner(device_id, user)
        # ShadowService 使用内存 dict 存储影子，直接遍历 _shadows
        shadows_map: dict[str, dict[str, Any]] = getattr(svc, "_shadows", {}) or {}
        items: list[dict[str, Any]] = []
        for dev_id, shadow in shadows_map.items():
            if device_id and dev_id != device_id:
                continue
            if accessible_ids is not None and dev_id not in accessible_ids:
                continue
            items.append(_build_shadow_info(dev_id, shadow))
        total = len(items)
        page = pagination.page
        size = pagination.size
        start = (page - 1) * size
        end = start + size
        paged = items[start:end]
        return PagedResponse(data=paged, total=total, page=page, size=size)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("list_shadows failed: %s", e)
        raise HTTPException(status_code=503, detail=CommonErrors.SERVICE_NOT_READY) from None


@router.get("/{device_id}", response_model=ApiResponse)
async def get_shadow(
    device_id: Annotated[str, Path(max_length=128)],
    user: dict[str, str] = Depends(require_permission(Permission.DEVICE_READ)),
):
    """获取指定设备的影子详情。"""
    try:
        await _check_device_owner(device_id, user)
        svc = _get_shadow_service()
        shadow = svc.get_shadow(device_id)
        if shadow is None:
            raise HTTPException(status_code=404, detail=DeviceErrors.NOT_FOUND)
        return ApiResponse(data=_build_shadow_detail(device_id, shadow))
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_shadow failed: %s", e)
        raise HTTPException(status_code=503, detail=CommonErrors.SERVICE_NOT_READY) from None


@router.put("/{device_id}/desired", response_model=ApiResponse)
async def update_desired(
    device_id: Annotated[str, Path(max_length=128)],
    body: ShadowStateUpdate,
    user: dict[str, str] = Depends(require_permission(Permission.DEVICE_UPDATE)),
):
    """更新设备 desired 状态（应用层下发）。"""
    try:
        await _check_device_owner(device_id, user)
        svc = _get_shadow_service()
        state = body.state if body.state is not None else (body.desired or {})
        if not isinstance(state, dict):
            raise HTTPException(status_code=400, detail=CommonErrors.VALIDATION_FAILED)
        # FIXED(P3): 对不存在的设备返回 404，而不是静默为幽灵设备创建影子记录
        if svc.get_shadow(device_id) is None:
            raise HTTPException(status_code=404, detail=DeviceErrors.NOT_FOUND)
        svc.update_desired(device_id, state)
        shadow = svc.get_shadow(device_id) or {}
        return ApiResponse(
            data={
                "device_id": device_id,
                "delta": _compute_delta(shadow.get("reported", {}) or {}, shadow.get("desired", {}) or {}),
                "version": _shadow_version(shadow),
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("update_desired failed: %s", e)
        raise HTTPException(status_code=503, detail=CommonErrors.SERVICE_NOT_READY) from None


@router.post("/{device_id}/reported", response_model=ApiResponse)
async def update_reported(
    device_id: Annotated[str, Path(max_length=128)],
    body: ShadowStateUpdate,
    user: dict[str, str] = Depends(require_permission(Permission.DEVICE_UPDATE)),
):
    """更新设备 reported 状态（设备上报）。"""
    try:
        await _check_device_owner(device_id, user)
        svc = _get_shadow_service()
        state = body.state if body.state is not None else (body.reported or {})
        if not isinstance(state, dict):
            raise HTTPException(status_code=400, detail=CommonErrors.VALIDATION_FAILED)
        svc.update_reported(device_id, state)
        shadow = svc.get_shadow(device_id) or {}
        return ApiResponse(
            data={
                "device_id": device_id,
                "version": _shadow_version(shadow),
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("update_reported failed: %s", e)
        raise HTTPException(status_code=503, detail=CommonErrors.SERVICE_NOT_READY) from None


@router.get("/{device_id}/delta", response_model=ApiResponse)
async def get_delta(
    device_id: Annotated[str, Path(max_length=128)],
    user: dict[str, str] = Depends(require_permission(Permission.DEVICE_READ)),
):
    """获取设备 desired 与 reported 的差异。"""
    try:
        await _check_device_owner(device_id, user)
        svc = _get_shadow_service()
        shadow = svc.get_shadow(device_id)
        if shadow is None:
            raise HTTPException(status_code=404, detail=DeviceErrors.NOT_FOUND)
        reported = shadow.get("reported", {}) or {}
        desired = shadow.get("desired", {}) or {}
        return ApiResponse(
            data={
                "device_id": device_id,
                "desired": desired,
                "reported": reported,
                "delta": _compute_delta(reported, desired),
                "version": _shadow_version(shadow),
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_delta failed: %s", e)
        raise HTTPException(status_code=503, detail=CommonErrors.SERVICE_NOT_READY) from None
