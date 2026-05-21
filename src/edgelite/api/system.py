"""系统管理API路由"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Body, HTTPException

from edgelite.api.deps import CurrentUser, SystemServiceDep, require_permission
from edgelite.api.error_codes import CascadeErrors, SystemErrors
from edgelite.models.common import ApiResponse
from edgelite.models.system import SystemStatusResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/system", tags=["System"])


@router.get("/status", response_model=ApiResponse[SystemStatusResponse])
async def get_system_status(
    svc: SystemServiceDep,
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    try:
        status_data = await svc.get_status()
        return ApiResponse(data=status_data)
    except Exception as e:
        logger.error("get_system_status failed: %s", e)
        raise HTTPException(status_code=500, detail=SystemErrors.STATUS_FAILED) from e  # FIXED: 原问题-中文硬编码detail，改为error_code


@router.get("/backup", response_model=ApiResponse)
async def list_backups(
    svc: SystemServiceDep,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    try:
        backups = await svc.list_backups()
        return ApiResponse(data=backups)
    except Exception as e:
        logger.error("list_backups failed: %s", e)
        raise HTTPException(status_code=500, detail=SystemErrors.BACKUP_LIST_FAILED) from e  # FIXED: 原问题-中文硬编码detail，改为error_code


@router.post("/backup", response_model=ApiResponse, status_code=201)
async def create_backup(
    svc: SystemServiceDep,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    try:
        backup = await svc.create_backup()
        return ApiResponse(data=backup)
    except Exception as e:
        logger.error("create_backup failed: %s", e)
        raise HTTPException(status_code=500, detail=SystemErrors.BACKUP_CREATE_FAILED) from e  # FIXED: 原问题-中文硬编码detail，改为error_code


@router.post("/restore", response_model=ApiResponse)
async def restore_backup(
    svc: SystemServiceDep,
    backup_id: str = Body(..., embed=True),
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    if not re.match(r"^[a-zA-Z0-9_-]+$", backup_id):
        raise HTTPException(status_code=400, detail=SystemErrors.INVALID_BACKUP_ID)  # FIXED: 原问题-中文硬编码detail，改为error_code
    try:
        success = await svc.restore_backup(backup_id)
        if not success:
            raise HTTPException(status_code=404, detail=SystemErrors.BACKUP_NOT_FOUND)  # FIXED: 原问题-中文硬编码detail，改为error_code
        return ApiResponse()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("restore_backup failed: %s", e)
        raise HTTPException(status_code=500, detail=SystemErrors.RESTORE_FAILED) from e  # FIXED: 原问题-中文硬编码detail，改为error_code


# ── 级联管理 API ──


@router.get("/cascade/topology", response_model=ApiResponse)
async def get_cascade_topology(
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    """获取级联拓扑结构。

    返回当前网关在级联拓扑中的角色、父节点、子节点和邻居信息。
    """
    try:
        from edgelite.engine.cascade_manager import CascadeManager

        manager = _get_cascade_manager()
        if manager is None:
            return ApiResponse(data={"status": "standalone", "parent_id": None, "children": [], "peers": []})
        topology = manager.build_topology()
        return ApiResponse(data={
            "local_id": topology.local_id,
            "status": topology.status,
            "parent_id": topology.parent_id,
            "children": topology.children,
            "peers": [
                {
                    "neighbor_id": n.neighbor_id,
                    "host": n.host,
                    "port": n.port,
                    "role": n.role,
                    "last_seen": n.last_seen,
                }
                for n in topology.peers
            ],
            "updated_at": topology.updated_at,
        })
    except Exception as e:
        logger.error("get_cascade_topology failed: %s", e)
        raise HTTPException(status_code=500, detail=CascadeErrors.TOPOLOGY_FAILED) from e


@router.get("/cascade/neighbors", response_model=ApiResponse)
async def get_cascade_neighbors(
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    """获取级联邻居列表。

    返回所有已发现的邻居网关信息。
    """
    try:
        manager = _get_cascade_manager()
        if manager is None:
            return ApiResponse(data=[])
        neighbors = manager.neighbors
        return ApiResponse(data=[
            {
                "neighbor_id": n.neighbor_id,
                "host": n.host,
                "port": n.port,
                "role": n.role,
                "properties": n.properties,
                "last_seen": n.last_seen,
            }
            for n in neighbors
        ])
    except Exception as e:
        logger.error("get_cascade_neighbors failed: %s", e)
        raise HTTPException(status_code=500, detail=CascadeErrors.NEIGHBORS_FAILED) from e


@router.post("/cascade/config", response_model=ApiResponse)
async def update_cascade_config(
    config: dict = Body(..., description="级联配置(parent_host/parent_port/role)"),
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    """更新级联配置。

    支持设置父节点地址、端口和本节点角色。
    """
    try:
        manager = _get_cascade_manager()
        if manager is None:
            raise HTTPException(status_code=503, detail=CascadeErrors.NOT_ENABLED)
        await manager.update_config(config)
        return ApiResponse()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("update_cascade_config failed: %s", e)
        raise HTTPException(status_code=500, detail=CascadeErrors.CONFIG_UPDATE_FAILED) from e


@router.delete("/cascade/neighbors/{neighbor_id}", response_model=ApiResponse)
async def remove_cascade_neighbor(
    neighbor_id: str,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    """移除指定级联邻居。

    Args:
        neighbor_id: 邻居网关ID。
    """
    try:
        manager = _get_cascade_manager()
        if manager is None:
            raise HTTPException(status_code=503, detail=CascadeErrors.NOT_ENABLED)
        removed = await manager.remove_neighbor(neighbor_id)
        if not removed:
            raise HTTPException(status_code=404, detail=CascadeErrors.NEIGHBOR_NOT_FOUND)
        return ApiResponse()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("remove_cascade_neighbor failed: %s", e)
        raise HTTPException(status_code=500, detail=CascadeErrors.REMOVE_FAILED) from e


def _get_cascade_manager():
    """获取级联管理器实例(延迟导入)。"""
    try:
        from edgelite.bootstrap import _app_state
        return getattr(_app_state, "cascade_manager", None)
    except Exception:
        return None
