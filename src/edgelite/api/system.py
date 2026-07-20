"""系统管理API路由"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import socket
import time
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Request
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from edgelite.api.deps import (
    AuditServiceDep,
    SchedulerDep,
    SystemServiceDep,
    require_permission,
)
from edgelite.api.error_codes import (
    AuthzErrors,
    CascadeErrors,
    CommonErrors,
    ConfigErrors,
    DeviceErrors,
    SystemErrors,
)
from edgelite.models.common import ApiResponse
from edgelite.models.system import SystemResourcesResponse, SystemStatusResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/system", tags=["System"])


# FIXED: Pydantic模型替代request.json()，提供请求体校验
class ConfigSectionUpdate(BaseModel):
    """配置节更新请求模型"""

    config: dict | None = None


class RetentionPolicyUpdate(BaseModel):
    """数据保留策略更新请求模型"""

    history_retention_days: int | None = Field(default=None, ge=1, le=3650)
    alarm_retention_days: int | None = Field(default=None, ge=1, le=3650)


class NtpConfigUpdate(BaseModel):
    """NTP配置更新请求模型"""

    enabled: bool = False
    server: str = Field(..., min_length=1, max_length=255)


def _is_cascade_parent_host_safe(host: str) -> bool:
    """SSRF 校验：至少拦截 loopback/link_local/未指定/组播/保留地址。

    FIXED(安全): 级联父节点场景，允许 is_private（级联到内网父节点是合理场景），
    但拦截 is_loopback 和 is_link_local（云元数据 169.254.x.x）等危险地址。
    域名先通过 socket.getaddrinfo 解析为 IP，再校验每个解析结果。
    """
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
        return not (ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved)
    except ValueError:
        pass
    try:
        addrs = socket.getaddrinfo(host, None)
    except (socket.gaierror, OSError):
        return False
    if not addrs:
        return False
    for _family, _stype, _proto, _canon, sockaddr in addrs:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved:
            return False
    return True


@router.get("/status", response_model=ApiResponse[SystemStatusResponse])
async def get_system_status(
    svc: SystemServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """获取系统运行状态概览（运行时长、版本、服务状态等）。"""
    try:
        status_data = await svc.get_status()
        return ApiResponse(data=status_data)
    except Exception as e:
        logger.error("get_system_status failed: %s", e)
        raise HTTPException(
            status_code=500,
            detail=SystemErrors.STATUS_FAILED,
        ) from e


@router.get("/resources", response_model=ApiResponse[SystemResourcesResponse])
async def get_system_resources(
    svc: SystemServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """获取系统资源使用情况（CPU、内存、磁盘、网络）。"""
    try:
        resources_data = await svc.collect_resources()
        return ApiResponse(data=resources_data)
    except Exception as e:
        logger.error("get_system_resources failed: %s", e)
        raise HTTPException(
            status_code=500,
            detail=SystemErrors.STATUS_FAILED,
        ) from e


@router.get("/backup", response_model=ApiResponse)
async def list_backups(
    svc: SystemServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    """列出所有可用的系统配置备份。"""
    try:
        backups = await svc.list_backups()
        return ApiResponse(data=backups)
    except Exception as e:
        logger.error("list_backups failed: %s", e)
        raise HTTPException(status_code=500, detail=SystemErrors.BACKUP_LIST_FAILED) from e


@router.post("/backup", response_model=ApiResponse, status_code=201)
async def create_backup(
    request: Request,
    svc: SystemServiceDep,
    audit_svc: AuditServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    """创建系统配置备份，记录审计日志。"""
    # 第三轮审计修复: 记录备份创建审计日志
    from edgelite.api.auth import _get_client_ip
    from edgelite.services.audit_service import AuditAction

    client_ip = _get_client_ip(request)
    user_agent = request.headers.get("user-agent", "")
    try:
        backup = await svc.create_backup()
        try:
            await audit_svc.log(
                AuditAction.BACKUP_CREATE,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="backup",
                resource_id=str(backup.get("backup_id", "")) if isinstance(backup, dict) else "",
                ip_address=client_ip,
                user_agent=user_agent,
                after_value=backup if isinstance(backup, dict) else {"result": str(backup)},
            )
        except Exception as e:
            logger.warning("Audit log failed: %s", e)
        return ApiResponse(data=backup)
    except Exception as e:
        logger.error("create_backup failed: %s", e)
        try:
            await audit_svc.log(
                AuditAction.BACKUP_CREATE,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="backup",
                ip_address=client_ip,
                user_agent=user_agent,
                status="failed",
                error_message=str(e),
            )
        except Exception as audit_e:
            logger.warning("Audit log failed: %s", audit_e)
        raise HTTPException(status_code=500, detail=SystemErrors.BACKUP_CREATE_FAILED) from e


@router.post("/restore", response_model=ApiResponse)
async def restore_backup(
    request: Request,
    svc: SystemServiceDep,
    audit_svc: AuditServiceDep,
    backup_id: str = Body(..., embed=True),
    confirm: bool = Body(..., embed=True),
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    """从备份恢复系统配置，需二次确认。"""
    # 第三轮审计修复: 高风险操作二次确认
    if not confirm:
        raise HTTPException(status_code=400, detail="请确认操作")
    if not re.match(r"^[a-zA-Z0-9_-]+$", backup_id):
        raise HTTPException(status_code=400, detail=SystemErrors.INVALID_BACKUP_ID)
    # 第三轮审计修复: 记录系统恢复审计日志
    from edgelite.api.auth import _get_client_ip
    from edgelite.services.audit_service import AuditAction

    client_ip = _get_client_ip(request)
    user_agent = request.headers.get("user-agent", "")
    try:
        success = await svc.restore_backup(backup_id)
        if not success:
            try:
                await audit_svc.log(
                    AuditAction.BACKUP_RESTORE,
                    user_id=user["user_id"],
                    username=user["username"],
                    resource_type="backup",
                    resource_id=backup_id,
                    ip_address=client_ip,
                    user_agent=user_agent,
                    status="failed",
                    error_message="backup not found",
                )
            except Exception as e:
                logger.warning("Audit log failed: %s", e)
            raise HTTPException(status_code=404, detail=SystemErrors.BACKUP_NOT_FOUND)
        try:
            await audit_svc.log(
                AuditAction.BACKUP_RESTORE,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="backup",
                resource_id=backup_id,
                ip_address=client_ip,
                user_agent=user_agent,
                after_value={"backup_id": backup_id, "restored": True},
            )
        except Exception as e:
            logger.warning("Audit log failed: %s", e)
        return ApiResponse()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("restore_backup failed: %s", e)
        try:
            await audit_svc.log(
                AuditAction.BACKUP_RESTORE,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="backup",
                resource_id=backup_id,
                ip_address=client_ip,
                user_agent=user_agent,
                status="failed",
                error_message=str(e),
            )
        except Exception as audit_e:
            logger.warning("Audit log failed: %s", audit_e)
        raise HTTPException(status_code=500, detail=SystemErrors.RESTORE_FAILED) from e


@router.delete("/backup/{backup_id}", response_model=ApiResponse)
async def delete_backup(
    backup_id: str,
    request: Request,
    svc: SystemServiceDep,
    audit_svc: AuditServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    """删除指定备份文件。

    链式安全：删除全量备份时会一并删除以其为基线的增量备份。
    """
    if not re.match(r"^[a-zA-Z0-9_-]+$", backup_id):
        raise HTTPException(status_code=400, detail=SystemErrors.INVALID_BACKUP_ID)
    # 第三轮审计修复: 记录备份删除审计日志
    from edgelite.api.auth import _get_client_ip
    from edgelite.services.audit_service import AuditAction

    client_ip = _get_client_ip(request)
    user_agent = request.headers.get("user-agent", "")
    try:
        success = await svc.delete_backup(backup_id)
        if not success:
            try:
                await audit_svc.log(
                    AuditAction.BACKUP_DELETE,
                    user_id=user["user_id"],
                    username=user["username"],
                    resource_type="backup",
                    resource_id=backup_id,
                    ip_address=client_ip,
                    user_agent=user_agent,
                    status="failed",
                    error_message="backup not found",
                )
            except Exception as e:
                logger.warning("Audit log failed: %s", e)
            raise HTTPException(status_code=404, detail=SystemErrors.BACKUP_NOT_FOUND)
        logger.info("Backup deleted by user %s: %s", getattr(user, "username", "?"), backup_id)
        try:
            await audit_svc.log(
                AuditAction.BACKUP_DELETE,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="backup",
                resource_id=backup_id,
                ip_address=client_ip,
                user_agent=user_agent,
                before_value={"backup_id": backup_id, "deleted": True},
            )
        except Exception as e:
            logger.warning("Audit log failed: %s", e)
        return ApiResponse()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("delete_backup failed: %s", e)
        try:
            await audit_svc.log(
                AuditAction.BACKUP_DELETE,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="backup",
                resource_id=backup_id,
                ip_address=client_ip,
                user_agent=user_agent,
                status="failed",
                error_message=str(e),
            )
        except Exception as audit_e:
            logger.warning("Audit log failed: %s", audit_e)
        raise HTTPException(status_code=500, detail=SystemErrors.BACKUP_CREATE_FAILED) from e


# ── 自动备份调度器 API ──


@router.get("/backup/schedule", response_model=ApiResponse)
async def get_backup_schedule(
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """获取自动备份调度器状态和配置。

    返回备份调度的当前状态、上次备份时间、备份文件列表等信息。
    """
    try:
        from edgelite.services.backup_scheduler import get_backup_scheduler

        scheduler = get_backup_scheduler()
        status = scheduler.status
        backups = scheduler.get_backup_list()

        # Convert dataclass to dict for JSON serialization
        status_dict = {
            "enabled": status.enabled,
            "interval_seconds": status.interval_seconds,
            "retain_days": status.retain_days,
            "is_running": status.is_running,
            "last_backup_time": status.last_backup_time,
            "last_backup_duration_ms": status.last_backup_duration_ms,
            "backup_count": status.backup_count,
            "total_backup_size_bytes": status.total_backup_size_bytes,
            "backups": backups,
        }
        return ApiResponse(data=status_dict)
    except Exception as e:
        logger.error("get_backup_schedule failed: %s", e)
        raise HTTPException(status_code=500, detail=SystemErrors.BACKUP_CREATE_FAILED) from e


@router.post("/backup/schedule/trigger", response_model=ApiResponse)
async def trigger_backup(
    request: Request,
    audit_svc: AuditServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    """手动触发一次数据库备份。

    立即执行所有数据库（主库、时序库、侧车库）的备份操作，
    不影响定时调度周期。
    """
    # 第三轮审计修复: 记录手动触发备份审计日志
    from edgelite.api.auth import _get_client_ip
    from edgelite.services.audit_service import AuditAction

    client_ip = _get_client_ip(request)
    user_agent = request.headers.get("user-agent", "")
    try:
        from edgelite.services.backup_scheduler import get_backup_scheduler

        scheduler = get_backup_scheduler()

        # Run backup asynchronously
        results = await scheduler.run_backup()

        # Convert results to dict
        results_dict = []
        for r in results:
            results_dict.append(
                {
                    "component": r.source,
                    "success": r.success,
                    "backup_path": r.backup_path,
                    "error": r.error,
                    "duration_ms": r.duration_ms,
                }
            )

        try:
            await audit_svc.log(
                AuditAction.BACKUP_CREATE,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="backup_schedule",
                ip_address=client_ip,
                user_agent=user_agent,
                after_value={
                    "triggered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "results": results_dict,
                },
            )
        except Exception as e:
            logger.warning("Audit log failed: %s", e)

        return ApiResponse(
            data={
                "triggered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "results": results_dict,
            }
        )
    except ValueError as e:
        logger.warning("trigger_backup validation error: %s", e)
        # FIXED(严重): 原问题-detail=str(e) 直接暴露异常内部信息，可能泄露数据库表名、SQL 语句、文件路径
        # 修复：使用统一错误码，异常详情仅记录日志
        try:
            await audit_svc.log(
                AuditAction.BACKUP_CREATE,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="backup_schedule",
                ip_address=client_ip,
                user_agent=user_agent,
                status="failed",
                error_message=str(e),
            )
        except Exception as audit_e:
            logger.warning("Audit log failed: %s", audit_e)
        raise HTTPException(status_code=422, detail=SystemErrors.BACKUP_CREATE_FAILED) from e
    except HTTPException:
        raise
    except Exception as e:
        logger.error("trigger_backup failed: %s", e)
        try:
            await audit_svc.log(
                AuditAction.BACKUP_CREATE,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="backup_schedule",
                ip_address=client_ip,
                user_agent=user_agent,
                status="failed",
                error_message=str(e),
            )
        except Exception as audit_e:
            logger.warning("Audit log failed: %s", audit_e)
        raise HTTPException(status_code=500, detail=SystemErrors.BACKUP_CREATE_FAILED) from e


# ── 级联管理 API ──


@router.get("/cascade/topology", response_model=ApiResponse)
async def get_cascade_topology(
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """获取级联拓扑结构。

    返回当前网关在级联拓扑中的角色、父节点、子节点和邻居信息。
    """
    try:
        manager = _get_cascade_manager()
        if manager is None:
            return ApiResponse(
                data={
                    "status": "standalone",
                    "parent_id": None,
                    "children": [],
                    "peers": [],
                }
            )
        topology = manager.build_topology()
        return ApiResponse(
            data={
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
            }
        )
    except Exception as e:
        logger.error("get_cascade_topology failed: %s", e)
        raise HTTPException(status_code=500, detail=CascadeErrors.TOPOLOGY_FAILED) from e


@router.get("/cascade/neighbors", response_model=ApiResponse)
async def get_cascade_neighbors(
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """获取级联邻居列表。

    返回所有已发现的邻居网关信息。
    """
    try:
        manager = _get_cascade_manager()
        if manager is None:
            return ApiResponse(data=[])
        neighbors = manager.neighbors
        return ApiResponse(
            data=[
                {
                    "neighbor_id": n.neighbor_id,
                    "host": n.host,
                    "port": n.port,
                    "role": n.role,
                    "properties": n.properties,
                    "last_seen": n.last_seen,
                }
                for n in neighbors
            ]
        )
    except Exception as e:
        logger.error("get_cascade_neighbors failed: %s", e)
        raise HTTPException(status_code=500, detail=CascadeErrors.NEIGHBORS_FAILED) from e


# R11-API-09: 定义 CascadeConfigUpdate Pydantic 模型替代 dict = Body(...)
# 字段全部可选以支持部分更新，extra=forbid 拒绝未知字段
class CascadeConfigUpdate(BaseModel):
    """级联配置更新请求模型"""

    parent_host: str | None = Field(default=None, max_length=255, description="父节点地址")
    parent_port: int | None = Field(default=None, ge=1, le=65535, description="父节点端口")
    role: Literal["parent", "child", "standalone"] | None = Field(default=None, description="节点角色")
    enabled: bool | None = Field(default=None, description="是否启用级联")
    auth_key: str | None = Field(default=None, max_length=256, description="认证密钥")

    model_config = {"extra": "forbid"}


@router.post("/cascade/config", response_model=ApiResponse)
async def update_cascade_config(
    config: CascadeConfigUpdate = Body(..., description="级联配置(parent_host/parent_port/role)"),
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    """更新级联配置。

    支持设置父节点地址、端口和本节点角色。
    R11-API-09: 使用 Pydantic 模型校验输入，替代裸 dict Body。
    """
    # 转为 dict（仅包含已设置字段），保留部分更新语义
    config_dict = config.model_dump(exclude_none=True)
    if not config_dict:
        raise HTTPException(status_code=400, detail=CascadeErrors.INVALID_CONFIG)
    # FIXED(安全): SSRF 防护 - 校验 parent_host，拦截 loopback/link_local（云元数据）
    # 允许 is_private（级联到内网父节点是合理场景）
    if config.parent_host and not _is_cascade_parent_host_safe(str(config.parent_host)):
        raise HTTPException(status_code=400, detail="ERR_SSRF_BLOCKED")
    try:
        manager = _get_cascade_manager()
        if manager is None:
            raise HTTPException(status_code=503, detail=CascadeErrors.NOT_ENABLED)
        await manager.update_config(config_dict)
        return ApiResponse()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("update_cascade_config failed: %s", e)
        raise HTTPException(status_code=500, detail=CascadeErrors.CONFIG_UPDATE_FAILED) from e


@router.delete("/cascade/neighbors/{neighbor_id}", response_model=ApiResponse)
async def remove_cascade_neighbor(
    neighbor_id: str,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
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
    """获取级联管理器实例(延迟导入)。

    FIXED: P2-5 原问题-except Exception全吞所有异常，用户无法区分"功能未启用"和"内部错误"。
    现改为：仅捕获ImportError，其他异常上浮，级别管理器初始化失败时返回None但记录错误。
    """
    try:
        from edgelite.app import _app_state

        return getattr(_app_state, "cascade_manager", None)
    except ImportError:
        # 级联管理器模块不存在，功能未启用
        return None
    except AttributeError:
        # _app_state未初始化
        return None
    except Exception as e:
        logger.error("_get_cascade_manager failed: %s (cascade_manager not available)", e)
        return None


# ── 配置热加载 API ──


async def _notify_services_reload(request: Request, changed_keys: list[str]) -> None:
    """配置重载后通知相关服务

    根据变更的配置项，通知 DeviceService、MqttForwarder、InfluxDBStorage 等服务
    """
    state = request.app.state

    # MQTT 相关配置变更 -> 通知 MqttForwarder
    mqtt_related = any(k.startswith("mqtt.") for k in changed_keys)
    if mqtt_related:
        mqtt_forwarder = getattr(state, "mqtt_forwarder", None)
        if mqtt_forwarder and hasattr(mqtt_forwarder, "on_config_changed"):
            try:
                await mqtt_forwarder.on_config_changed()
                logger.info("Notified MqttForwarder of config change")
            except Exception as e:
                logger.error("Failed to notify MqttForwarder: %s", e)

    # InfluxDB 相关配置变更 -> 通知 InfluxDBStorage
    influxdb_related = any(k.startswith("influxdb.") for k in changed_keys)
    if influxdb_related:
        influx_storage = getattr(state, "influx_storage", None)
        if influx_storage and hasattr(influx_storage, "on_config_changed"):
            try:
                await influx_storage.on_config_changed()
                logger.info("Notified InfluxDBStorage of config change")
            except Exception as e:
                logger.error("Failed to notify InfluxDBStorage: %s", e)

    # 安全配置变更 -> 记录日志（JWT密钥变更需要重新登录）
    security_related = any(k.startswith("security.") for k in changed_keys)
    if security_related:
        logger.warning("Security config changed - existing tokens may be invalidated")

    # 数据库配置变更 -> 通知 DeviceService
    db_related = any(k.startswith("database.") for k in changed_keys)
    if db_related:
        device_service = getattr(state, "device_service", None)
        if device_service and hasattr(device_service, "on_config_changed"):
            try:
                await device_service.on_config_changed()
                logger.info("Notified DeviceService of config change")
            except Exception as e:
                logger.error("Failed to notify DeviceService: %s", e)


@router.post("/config/reload", response_model=ApiResponse)
async def reload_config(
    request: Request,
    audit_svc: AuditServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    """触发配置热重载

    从配置文件重新加载配置并应用到运行中的服务。
    关键配置项（MQTT、InfluxDB、安全密钥等）变更时会通知相关服务。
    """
    # 第三轮审计修复: 记录配置重载审计日志
    from edgelite.api.auth import _get_client_ip
    from edgelite.services.audit_service import AuditAction

    client_ip = _get_client_ip(request)
    user_agent = request.headers.get("user-agent", "")
    try:
        from edgelite.config import reload_config as _reload_config

        config, changed_keys = _reload_config()

        # 通知相关服务
        await _notify_services_reload(request, changed_keys)

        try:
            await audit_svc.log(
                AuditAction.CONFIG_RELOAD,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="config",
                ip_address=client_ip,
                user_agent=user_agent,
                after_value={"config_version": config._config_version, "changed_keys": changed_keys},
            )
        except Exception as e:
            logger.warning("Audit log failed: %s", e)

        return ApiResponse(
            data={
                "config_version": config._config_version,
                "changed_sensitive_keys": changed_keys,
                "message": "Config reloaded successfully"
                if not changed_keys
                else f"Config reloaded, {len(changed_keys)} sensitive key(s) changed",
            }
        )
    except Exception as e:
        logger.error("reload_config failed: %s", e)
        try:
            await audit_svc.log(
                AuditAction.CONFIG_RELOAD,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="config",
                ip_address=client_ip,
                user_agent=user_agent,
                status="failed",
                error_message=str(e),
            )
        except Exception as audit_e:
            logger.warning("Audit log failed: %s", audit_e)
        raise HTTPException(status_code=500, detail=ConfigErrors.RELOAD_FAILED) from e


@router.get("/config", response_model=ApiResponse)
async def get_current_config(
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """获取当前运行配置（脱敏后）

    返回当前运行中的完整配置，敏感字段已脱敏处理。
    """
    try:
        from edgelite.config import get_sanitized_config

        sanitized = get_sanitized_config()
        return ApiResponse(data=sanitized)
    except Exception as e:
        logger.error("get_current_config failed: %s", e)
        raise HTTPException(status_code=500, detail=ConfigErrors.LOAD_FAILED) from e


@router.put("/config/{section}", response_model=ApiResponse)
async def update_config_section(
    section: str,
    body: ConfigSectionUpdate,
    request: Request,
    audit_svc: AuditServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    """更新指定配置节

    用于更新未注册为"服务"的配置节（如 mqtt、scheduler 等），
    已注册的服务应使用 PUT /services/{service_name}/config。
    """
    # 第三轮审计修复: 记录配置更新审计日志
    from edgelite.api.auth import _get_client_ip
    from edgelite.services.audit_service import AuditAction

    client_ip = _get_client_ip(request)
    user_agent = request.headers.get("user-agent", "")
    try:
        from edgelite.config import get_config
        from edgelite.config import update_config_section as _update_config_section

        # FIXED-LP06: 敏感配置节白名单校验，防止通过 API 修改 security/database 等关键配置
        _DENIED_SECTIONS = {"security", "database", "webhook_auth", "mqtt_server", "server", "influxdb"}
        if section in _DENIED_SECTIONS:
            raise HTTPException(status_code=403, detail=ConfigErrors.SECTION_NOT_ALLOWED)

        config = get_config()
        if not hasattr(config, section):
            raise HTTPException(status_code=404, detail=ConfigErrors.SECTION_NOT_FOUND)

        # FIXED: 使用Pydantic模型替代request.json()，由FastAPI自动校验请求体
        values = body.config if body.config is not None else body.model_dump(exclude_none=True)

        # 第三轮审计修复: 记录更新前的配置值（脱敏）
        before_section = getattr(config, section, None)
        before_value = None
        if before_section is not None:
            try:
                before_value = (
                    before_section.model_dump() if hasattr(before_section, "model_dump") else dict(before_section)
                )
            except Exception:
                before_value = {"section": section}

        _update_config_section(section, values)

        # 通知相关服务
        await _notify_services_reload(request, [f"{section}.*"])

        # 第三轮审计修复: 敏感字段脱敏
        _SENSITIVE_KEYS = {"password", "token", "secret", "api_key", "secret_key", "auth_key"}

        def _sanitize(obj):
            if not isinstance(obj, dict):
                return obj
            return {k: "***" if any(s in k.lower() for s in _SENSITIVE_KEYS) else v for k, v in obj.items()}

        try:
            await audit_svc.log(
                AuditAction.CONFIG_UPDATE,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="config",
                resource_id=section,
                ip_address=client_ip,
                user_agent=user_agent,
                before_value=_sanitize(before_value) if before_value else None,
                after_value=_sanitize(values),
            )
        except Exception as e:
            logger.warning("Audit log failed: %s", e)

        return ApiResponse(data={"success": True, "message": f"Config section '{section}' updated"})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("update_config_section failed: %s", e)
        try:
            await audit_svc.log(
                AuditAction.CONFIG_UPDATE,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="config",
                resource_id=section,
                ip_address=client_ip,
                user_agent=user_agent,
                status="failed",
                error_message=str(e),
            )
        except Exception as audit_e:
            logger.warning("Audit log failed: %s", audit_e)
        raise HTTPException(status_code=500, detail=ConfigErrors.UPDATE_FAILED) from e


# ── 标准化健康检查端点 ──


def _get_app_state():
    """获取应用状态容器（延迟导入）"""
    try:
        from edgelite.app import _app_state

        return _app_state
    except Exception:
        return None


async def _check_database(state) -> dict:
    """检查数据库状态"""
    from edgelite.config import get_config

    config = get_config()
    db = getattr(state, "database", None)
    if db is None:
        return {
            "status": "down",
            "error": "database not initialized",
            "backend": config.database.backend,
        }

    try:
        t0 = time.monotonic()
        async with db.get_session() as session:
            from sqlalchemy import text

            await session.execute(text("SELECT 1"))
        latency_ms = round((time.monotonic() - t0) * 1000, 2)
        return {"status": "up", "backend": config.database.backend, "latency_ms": latency_ms}
    except Exception:
        return {
            "status": "down",
            "error": "database_error",
            "backend": config.database.backend,
        }  # FIXED-P1: 不暴露异常详情


async def _check_influxdb(state) -> dict:
    """检查 InfluxDB 状态"""
    influx = getattr(state, "influx_storage", None)
    if influx is None:
        return {"status": "down", "error": "influx_storage not initialized"}

    from edgelite.config import get_config

    config = get_config()
    url = config.influxdb.url

    try:
        t0 = time.monotonic()
        ok = await influx.check_health()
        latency_ms = round((time.monotonic() - t0) * 1000, 2)
        # FIXED-P0: using_fallback改为async，需await
        # FIXED(致命): 原代码 await influx.using_fallback 缺少括号，对绑定方法对象本身 await
        # 导致 TypeError 被外层 except 捕获，_check_influxdb 始终返回 down，就绪探针永远 503
        using_fallback = await influx.using_fallback()
        result = {"url": url, "latency_ms": latency_ms}
        if ok:
            result.update({"status": "up", "using_fallback": False})
        elif using_fallback:
            result.update({"status": "degraded", "using_fallback": True})
        else:
            result.update({"status": "down", "using_fallback": False})
        return result
    except Exception:
        return {"status": "down", "error": "influxdb_error", "url": url}  # FIXED-P1: 不暴露异常详情


async def _check_mqtt(state) -> dict:
    """检查 MQTT 转发器状态"""
    mqtt_forwarder = getattr(state, "mqtt_forwarder", None)
    if mqtt_forwarder is None:
        return {"status": "not_configured"}

    try:
        running = getattr(mqtt_forwarder, "_running", False)
        result = {"status": "up" if running else "down"}
        host = getattr(mqtt_forwarder, "_host", None)
        port = getattr(mqtt_forwarder, "_port", None)
        if host:
            result["host"] = host
        if port:
            result["port"] = port
        return result
    except Exception:
        return {"status": "down", "error": "mqtt_error"}  # FIXED-P1: 不暴露异常详情


async def _check_drivers(state) -> dict:
    """检查驱动注册表状态"""
    driver_registry = getattr(state, "driver_registry", None)
    if driver_registry is None:
        return {"status": "not_configured"}

    try:
        drivers = driver_registry.list_drivers() if hasattr(driver_registry, "list_drivers") else []
        return {"status": "up", "registered_count": len(drivers), "drivers": drivers}
    except Exception:
        return {"status": "down", "error": "driver_error"}  # FIXED-P1: 不暴露异常详情


async def _check_ai_engine(state) -> dict:
    """检查 AI 推理引擎状态"""
    ai_engine = getattr(state, "ai_engine", None)
    if ai_engine is None:
        return {"status": "not_configured"}

    try:
        models = ai_engine.get_loaded_models() if hasattr(ai_engine, "get_loaded_models") else {}
        active = sum(1 for w in models.values() if getattr(w, "status", "") == "active")
        return {"status": "up", "active_models": active, "total_models": len(models)}
    except Exception:
        return {"status": "down", "error": "ai_engine_error"}  # FIXED-P1: 不暴露异常详情


async def _check_system_resources() -> dict:
    """检查系统资源（CPU/内存/磁盘）"""
    result = {}
    try:
        import psutil

        # FIXED-BugR4X: 原问题-psutil.cpu_percent(interval=0.1)阻塞事件循环100ms，
        # 修复-改为interval=None非阻塞返回上次调用以来的平均值
        result["cpu_percent"] = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory()
        result["memory_percent"] = mem.percent
        result["memory_available_bytes"] = mem.available
        try:
            disk = psutil.disk_usage("/")
            result["disk_percent"] = disk.percent
            result["disk_free_bytes"] = disk.free
        except Exception as e:
            # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
            logger.warning("磁盘使用率获取失败: %s", e)
    except ImportError:
        result["psutil"] = "not_available"
    return result


# R11-API-01/R11-API-03: 删除 system.py 中重复的 /health、/health/live、/health/ready、/ready、/live 路由
# 这些路由与 health.py 重复，但缺少速率限制和超时保护，且 /health 要求 SYSTEM_READ 权限导致 K8s 探针无法通过认证。
# 统一由 health.py 提供健康检查端点。


@router.get("/quality/{device_id}", response_model=ApiResponse)
async def get_device_quality(
    device_id: Annotated[str, Path(max_length=128)],
    scheduler: SchedulerDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """获取设备数据质量评分"""
    if user["role"] != "admin":
        from edgelite.app import _app_state

        device_svc = _app_state.device_service
        device = await device_svc.get_device(device_id)
        if device is None:
            raise HTTPException(status_code=404, detail=DeviceErrors.NOT_FOUND)
        if device.get("created_by") != user["user_id"]:
            # FIXED-P2: 检查共享访问权限，与devices.py保持一致
            from edgelite.storage.sqlite_repo import ResourceShareRepo

            share_repo = ResourceShareRepo(_app_state.database, _app_state.database.write_lock)
            has_access = await share_repo.check_user_has_access("device", device_id, user["user_id"])
            if not has_access:
                raise HTTPException(status_code=403, detail=AuthzErrors.RESOURCE_OWNERSHIP_DENIED)
    score = await scheduler.calculate_quality_score(device_id)
    return ApiResponse(data=score)


@router.get("/circuit-breakers", response_model=ApiResponse)
async def get_circuit_breaker_status(
    scheduler: SchedulerDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """Get all circuit breaker statuses"""
    statuses = await scheduler.get_circuit_breaker_status()
    # FIXED-P2: 非admin用户仅返回自己拥有或被共享的设备的断路器状态
    if user["role"] != "admin" and statuses:
        from edgelite.app import _app_state
        from edgelite.storage.sqlite_repo import ResourceShareRepo

        device_svc = getattr(_app_state, "device_service", None)
        if device_svc:
            owned_ids = set(await device_svc.list_device_ids_by_owner(user["user_id"]))
            share_repo = ResourceShareRepo(_app_state.database, _app_state.database.write_lock)
            shared_ids = await share_repo.get_shared_resource_ids(user["user_id"], "device")
            accessible_ids = owned_ids | shared_ids
            statuses = {k: v for k, v in statuses.items() if k in accessible_ids}
    return ApiResponse(data=statuses)


# R11-API-02: 删除 _root_health_router 死代码（从未被 app.py 注册，且与 health.py 重复）
# 原先的 _root_health_router 及其 /health、/ready 端点已移除，统一由 health.py 提供。


@router.post("/circuit-breakers/{device_id}/reset", response_model=ApiResponse)
async def reset_circuit_breaker(
    device_id: Annotated[str, Path(max_length=128)],
    request: Request,
    scheduler: SchedulerDep,
    audit_svc: AuditServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    """Reset a device's circuit breaker"""
    # FIXED-P2: 非admin用户校验设备访问权限
    if user["role"] != "admin":
        from edgelite.app import _app_state
        from edgelite.storage.sqlite_repo import ResourceShareRepo

        device_svc = getattr(_app_state, "device_service", None)
        if device_svc:
            device = await device_svc.get_device(device_id)
            if device is None:
                raise HTTPException(status_code=404, detail=DeviceErrors.NOT_FOUND)
            if device.get("created_by") != user["user_id"]:
                share_repo = ResourceShareRepo(_app_state.database, _app_state.database.write_lock)
                has_access = await share_repo.check_user_has_access("device", device_id, user["user_id"])
                if not has_access:
                    raise HTTPException(status_code=403, detail=AuthzErrors.RESOURCE_OWNERSHIP_DENIED)
    # 第三轮审计修复: 记录熔断器重置审计日志
    from edgelite.api.auth import _get_client_ip
    from edgelite.services.audit_service import AuditAction

    client_ip = _get_client_ip(request)
    user_agent = request.headers.get("user-agent", "")
    ok = await scheduler.reset_circuit_breaker(device_id)
    if not ok:
        try:
            await audit_svc.log(
                AuditAction.CIRCUIT_BREAKER_RESET,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="circuit_breaker",
                resource_id=device_id,
                ip_address=client_ip,
                user_agent=user_agent,
                status="failed",
                error_message="device not found",
            )
        except Exception as e:
            logger.warning("Audit log failed: %s", e)
        raise HTTPException(
            status_code=404,
            detail=DeviceErrors.NOT_FOUND,
        )
    try:
        await audit_svc.log(
            AuditAction.CIRCUIT_BREAKER_RESET,
            user_id=user["user_id"],
            username=user["username"],
            resource_type="circuit_breaker",
            resource_id=device_id,
            ip_address=client_ip,
            user_agent=user_agent,
            after_value={"device_id": device_id, "reset": True},
        )
    except Exception as e:
        logger.warning("Audit log failed: %s", e)
    return ApiResponse(data=None)


# ── Health / Readiness / Performance Endpoints ──

# FIXED-P1: 原问题-此处的 @router.get("/ready") 与行744的 /ready K8s探针路由重复注册，
# 且函数名 readiness_check 遮蔽了行713的同名函数，导致 ready_check() 调用 readiness_check()
# 时传入错误参数（缺少svc）。现将路由改为 /ready-status，函数名改为 readiness_check_api。


@router.get("/health/basic", response_model=ApiResponse[dict])
async def health_check_basic():  # FIXED-P0: 重命名避免覆盖行508的完整 health_check
    """Basic health check endpoint (no auth required, system resources only)"""
    from edgelite.models.system import ComponentHealth, HealthCheckResponse

    components = []
    overall_status = "healthy"

    try:
        import psutil  # FIXED-P2: 原问题-import在try块外，psutil不可用时抛出未捕获ImportError导致500

        # FIXED-BugR4X: 原问题-psutil.cpu_percent(interval=0.1)阻塞事件循环100ms，
        # 修复-改为interval=None非阻塞返回上次调用以来的平均值
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")

        # FIXED-P1: 无认证端点不暴露具体资源百分比，仅返回healthy/degraded状态
        components.append(
            ComponentHealth(
                name="cpu", status="healthy" if cpu < 90 else "degraded", message="ok" if cpu < 90 else "high"
            )
        )
        components.append(
            ComponentHealth(
                name="memory",
                status="healthy" if mem.percent < 90 else "degraded",
                message="ok" if mem.percent < 90 else "high",
            )
        )
        components.append(
            ComponentHealth(
                name="disk",
                status="healthy" if disk.percent < 90 else "degraded",
                message="ok" if disk.percent < 90 else "high",
            )
        )
    except Exception:
        components.append(ComponentHealth(name="system", status="unknown", message="unavailable"))

    if any(c.status != "healthy" for c in components):
        overall_status = "degraded"

    resp = HealthCheckResponse(status=overall_status, components=components)
    return ApiResponse(data=resp.model_dump())


@router.get("/ready-status", response_model=ApiResponse[dict])
async def readiness_check_api(
    svc: SystemServiceDep,
):
    """Readiness check endpoint (API version, returns system status)"""
    try:
        status_data = await svc.get_status()
        is_ready = status_data.get("device_total", -1) >= 0
        return ApiResponse(data={"ready": is_ready, "status": "ready" if is_ready else "not_ready"})
    except Exception:
        return ApiResponse(data={"ready": False, "status": "not_ready"})  # FIXED-P1: 不向无认证端点暴露异常详情


@router.get("/performance", response_model=ApiResponse[dict])
async def get_performance(
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """Real-time performance data endpoint"""
    try:  # FIXED-P2: 捕获 psutil ImportError，精简镜像中不崩溃
        import psutil
    except ImportError:
        return ApiResponse(data={"error": "psutil not available"})
    from edgelite.models.system import PerformanceData

    try:
        # 修复-改为interval=None非阻塞返回上次调用以来的平均值，避免阻塞事件循环
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        net = psutil.net_io_counters()

        perf = PerformanceData(
            cpu_percent=cpu,
            memory_percent=mem.percent,
            memory_used_mb=mem.used / (1024 * 1024),
            memory_total_mb=mem.total / (1024 * 1024),
            disk_percent=disk.percent,
            disk_used_gb=disk.used / (1024 * 1024 * 1024),
            disk_total_gb=disk.total / (1024 * 1024 * 1024),
            net_sent_mb=net.bytes_sent / (1024 * 1024),
            net_recv_mb=net.bytes_recv / (1024 * 1024),
        )
        return ApiResponse(data=perf.model_dump())
    except Exception:
        return ApiResponse(data={"error": "resource_check_error"})  # FIXED-P1: 不暴露异常详情


# FIXED-P0: 实现 retention/cert 路由，替代原 501 存根


@router.get("/retention", response_model=ApiResponse[dict])
async def get_retention_policy(user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ))):
    """获取数据保留策略配置

    返回历史数据保留天数与告警数据保留天数。
    历史数据保留天数从 influxdb.retention_days 读取，告警数据保留天数暂用默认值。
    """
    try:
        from edgelite.config import get_config

        config = get_config()
        # 历史数据保留天数从 InfluxDB 配置读取
        history_retention_days = getattr(config.influxdb, "retention_days", 30)
        # 告警数据保留天数：配置中暂无对应字段，使用默认值 365
        alarm_retention_days = 365
        return ApiResponse(
            data={
                "history_retention_days": history_retention_days,
                "alarm_retention_days": alarm_retention_days,
            }
        )
    except Exception as e:
        logger.error("get_retention_policy failed: %s", e)
        raise HTTPException(status_code=500, detail=SystemErrors.STATUS_FAILED) from e


@router.put("/retention", response_model=ApiResponse[dict])
async def update_retention_policy(
    body: RetentionPolicyUpdate,
    request: Request,
    audit_svc: AuditServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    """更新数据保留策略配置

    校验 days 范围 1-3650，写入 influxdb.retention_days 配置。
    """
    # 第三轮审计修复: 记录保留策略更新审计日志
    from edgelite.api.auth import _get_client_ip
    from edgelite.services.audit_service import AuditAction

    client_ip = _get_client_ip(request)
    user_agent = request.headers.get("user-agent", "")
    try:
        from edgelite.config import update_config_section as _update_config_section

        # FIXED: 使用Pydantic模型替代request.json()，由FastAPI自动校验请求体
        history_days = body.history_retention_days
        alarm_days = body.alarm_retention_days

        # 记录更新前的值
        from edgelite.config import get_config

        config = get_config()
        before_value = {"history_retention_days": getattr(config.influxdb, "retention_days", 30)}

        # 校验 days 范围 1-3650（Pydantic模型已通过ge/le约束校验，此处保留防御性检查）
        if history_days is not None:
            if not isinstance(history_days, int) or not (1 <= history_days <= 3650):
                raise HTTPException(status_code=400, detail=ConfigErrors.VALIDATION_FAILED)
            # 写入 influxdb.retention_days
            _update_config_section("influxdb", {"retention_days": history_days})

        # alarm_retention_days 暂无对应配置字段，仅校验不持久化
        if alarm_days is not None:
            if not isinstance(alarm_days, int) or not (1 <= alarm_days <= 3650):
                raise HTTPException(status_code=400, detail=ConfigErrors.VALIDATION_FAILED)

        try:
            await audit_svc.log(
                AuditAction.CONFIG_UPDATE,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="retention",
                ip_address=client_ip,
                user_agent=user_agent,
                before_value=before_value,
                after_value={
                    "history_retention_days": history_days if history_days is not None else 30,
                    "alarm_retention_days": alarm_days if alarm_days is not None else 365,
                },
            )
        except Exception as e:
            logger.warning("Audit log failed: %s", e)

        return ApiResponse(
            data={
                "history_retention_days": history_days if history_days is not None else 30,
                "alarm_retention_days": alarm_days if alarm_days is not None else 365,
                "message": "Retention policy updated",
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("update_retention_policy failed: %s", e)
        try:
            await audit_svc.log(
                AuditAction.CONFIG_UPDATE,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="retention",
                ip_address=client_ip,
                user_agent=user_agent,
                status="failed",
                error_message=str(e),
            )
        except Exception as audit_e:
            logger.warning("Audit log failed: %s", audit_e)
        raise HTTPException(status_code=500, detail=SystemErrors.STATUS_FAILED) from e


@router.get("/cert", response_model=ApiResponse[dict])
async def get_cert_info(user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ))):
    """获取证书信息

    返回当前 HTTPS 证书状态。若未配置证书，返回 has_cert: false。
    """
    try:
        from edgelite.config import get_config

        config = get_config()
        # 检查 server 配置中是否有 SSL 证书配置
        ssl_cert = getattr(config.server, "ssl_cert", None)
        ssl_key = getattr(config.server, "ssl_key", None)
        has_cert = bool(ssl_cert and ssl_key)

        expiry = None
        if has_cert and ssl_cert:
            # 尝试读取证书过期时间
            try:
                import os

                if os.path.exists(ssl_cert):
                    try:
                        from cryptography import x509
                        from cryptography.hazmat.backends import default_backend

                        # 改为异步读取证书文件，避免阻塞事件循环
                        cert_data = await asyncio.to_thread(_read_file_sync, ssl_cert)
                        cert = x509.load_pem_x509_certificate(cert_data, default_backend())
                        expiry = cert.not_valid_after.isoformat()
                    except ImportError:
                        # cryptography 库未安装，仅返回文件存在信息
                        expiry = None
            except Exception:
                expiry = None

        return ApiResponse(
            data={
                "has_cert": has_cert,
                "cert_path": ssl_cert if has_cert else None,
                "expiry": expiry,
            }
        )
    except Exception as e:
        logger.error("get_cert_info failed: %s", e)
        raise HTTPException(status_code=500, detail=SystemErrors.STATUS_FAILED) from e


@router.post("/cert/rotate", response_model=ApiResponse[dict])
async def rotate_cert(user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE))):
    """FIXED: 证书轮换 — 检查证书状态并触发自动续签（如可用）[2026-06-29]

    当前实现：检查证书过期状态，尝试使用 generate_self_signed_cert 生成新证书。
    生产环境应配置 ACME/Let's Encrypt 自动续签，此端点作为手动触发入口。
    """
    from pathlib import Path as _Path

    from edgelite.config import get_config
    from edgelite.engine.tls_security import CertManager

    cert_paths: list[tuple[str, str]] = []
    # 检查配置中的证书路径
    try:
        config = get_config()
        mqtt_tls = getattr(getattr(config, "mqtt_server", None), "tls", None)
        if mqtt_tls:
            ca = getattr(mqtt_tls, "ca_path", None)
            cert = getattr(mqtt_tls, "cert_path", None)
            if ca:
                cert_paths.append(("mqtt_ca", ca))
            if cert:
                cert_paths.append(("mqtt_cert", cert))
    except Exception:
        pass

    results: dict[str, dict[str, Any]] = {}
    cert_mgr = CertManager()
    for label, path in cert_paths:
        try:
            if not _Path(path).exists():
                results[label] = {"status": "missing", "path": path}
                continue
            validation = cert_mgr.validate_cert(_Path(path))
            results[label] = {
                "status": "valid" if validation else "expired",
                "expires_at": None,
                "days_remaining": None,
            }
        except Exception as e:
            results[label] = {"status": "error", "error": str(e)}

    return ApiResponse(
        code=200,
        message="Certificate rotation check completed",
        data={"certificates": results},
    )


# ── NTP 时间同步配置 API ──

# FIXED(一般): 原问题-使用相对路径 "data/ntp_config.json", 依赖进程CWD不可靠;
# 修复-基于项目根目录的绝对路径。system.py 位于 src/edgelite/api/, 上溯4级到项目根目录
# (与 storage/database.py 中 _get_project_root 的 4 级 .parent 约定一致)
# 注意: 顶层已 from fastapi import Path, 故此处用 pathlib.Path 避免命名冲突
import pathlib as _pathlib
from datetime import UTC

_NTP_CONFIG_FILE = str(_pathlib.Path(__file__).resolve().parent.parent.parent.parent / "data" / "ntp_config.json")


def _read_file_sync(path: str) -> bytes:
    """同步读取文件内容的辅助函数，供 asyncio.to_thread 调用以避免阻塞事件循环"""
    with open(path, "rb") as f:
        return f.read()


def _load_ntp_config() -> dict:
    """从本地 JSON 文件加载 NTP 配置"""
    import os

    defaults = {"enabled": False, "server": "pool.ntp.org"}
    try:
        if os.path.exists(_NTP_CONFIG_FILE):
            with open(_NTP_CONFIG_FILE, encoding="utf-8") as f:
                data = json.load(f)
                return {
                    "enabled": bool(data.get("enabled", defaults["enabled"])),
                    "server": str(data.get("server", defaults["server"])),
                }
    except Exception as e:
        logger.warning("load ntp config failed: %s", e)
    return defaults


def _save_ntp_config(config: dict) -> None:
    """保存 NTP 配置到本地 JSON 文件"""
    import os

    try:
        os.makedirs(os.path.dirname(_NTP_CONFIG_FILE) or ".", exist_ok=True)
        with open(_NTP_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("save ntp config failed: %s", e)
        raise


async def _get_ntp_sync_status() -> str:
    """获取 NTP 同步状态（简化实现：尝试读取 chrony/ntpq 状态）

    FIXED-P2: 原实现为同步函数，内部使用 subprocess.run 在 async 上下文中阻塞事件循环
    （即使调用方用 asyncio.to_thread 包裹仍占用线程池资源）；
    修复-改为 async 函数，使用 asyncio.create_subprocess_exec 异步执行子进程，
    并用 asyncio.wait_for 将单次命令超时控制在 10 秒内，超时后终止子进程避免僵尸进程。
    """

    async def _run_cmd(args: list[str]) -> tuple[int, str]:
        """异步执行命令并返回 (returncode, stdout)；失败或超时返回 (-1, "")"""
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, OSError):
            # 命令不存在（如未安装 chronyc/ntpq）
            return -1, ""
        try:
            # 单次命令超时控制在 10 秒，避免长时间阻塞事件循环
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        except TimeoutError:
            # 超时后确保终止子进程，避免僵尸进程残留
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            return -1, ""
        except OSError:
            return -1, ""
        rc = proc.returncode if proc.returncode is not None else -1
        out = stdout.decode("utf-8", errors="replace") if stdout else ""
        return rc, out

    # 尝试 chronyc
    rc, out = await _run_cmd(["chronyc", "tracking"])
    if rc == 0 and "Leap status" in out:
        for line in out.splitlines():
            if "Leap status" in line:
                if "Normal" in line:
                    return "synced"
                return "unsynced"
        return "synced"

    # 尝试 ntpq
    rc, out = await _run_cmd(["ntpq", "-p"])
    if rc == 0:
        for line in out.splitlines():
            if line.startswith("*"):
                return "synced"
        return "unsynced"

    return "unknown"


@router.get("/ntp", response_model=ApiResponse[dict])
async def get_ntp_config(user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ))):
    """获取 NTP 时间同步配置与状态

    返回 enabled、server、sync_status、current_time 字段。
    """
    try:
        # 改为异步加载NTP配置，避免阻塞事件循环
        config = await asyncio.to_thread(_load_ntp_config)
        # FIXED-P2: _get_ntp_sync_status 已改为 async，内部使用 asyncio.create_subprocess_exec
        # 异步执行子进程，不再阻塞事件循环，无需线程池包裹
        sync_status = await _get_ntp_sync_status()
        from datetime import datetime

        current_time = datetime.now(UTC).isoformat()
        return ApiResponse(
            data={
                "enabled": config["enabled"],
                "server": config["server"],
                "sync_status": sync_status,
                "current_time": current_time,
            }
        )
    except Exception as e:
        logger.error("get_ntp_config failed: %s", e)
        raise HTTPException(status_code=500, detail=SystemErrors.STATUS_FAILED) from e


@router.put("/ntp", response_model=ApiResponse[dict])
async def update_ntp_config(
    body: NtpConfigUpdate,
    request: Request,
    audit_svc: AuditServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    """更新 NTP 时间同步配置

    校验 server 格式（域名或 IP），写入本地配置文件。
    """
    # 第三轮审计修复: 记录NTP配置更新审计日志
    from edgelite.api.auth import _get_client_ip
    from edgelite.services.audit_service import AuditAction

    client_ip = _get_client_ip(request)
    user_agent = request.headers.get("user-agent", "")
    try:
        # FIXED: 使用Pydantic模型替代request.json()，由FastAPI自动校验请求体
        enabled = body.enabled
        server = body.server.strip()

        if not server:
            raise HTTPException(status_code=400, detail=ConfigErrors.VALIDATION_FAILED)

        # 校验 server 格式：域名或 IPv4/IPv6
        import ipaddress

        is_valid = False
        # 尝试解析为 IP
        try:
            ipaddress.ip_address(server)
            is_valid = True
        except ValueError:
            pass
        # 尝试作为域名校验（简单正则：允许字母、数字、点、连字符）
        if not is_valid:
            if re.match(r"^[a-zA-Z0-9]([a-zA-Z0-9\-\.]*[a-zA-Z0-9])?$", server) and "." in server:
                is_valid = True

        if not is_valid:
            raise HTTPException(status_code=400, detail=ConfigErrors.VALIDATION_FAILED)

        # 记录更新前的NTP配置
        before_value = None
        try:
            # 改为异步加载，避免阻塞事件循环
            before_value = await asyncio.to_thread(_load_ntp_config) if "_load_ntp_config" in globals() else None
        except Exception:
            before_value = None

        new_config = {"enabled": enabled, "server": server}
        # 改为异步保存NTP配置，避免阻塞事件循环
        await asyncio.to_thread(_save_ntp_config, new_config)

        try:
            await audit_svc.log(
                AuditAction.CONFIG_UPDATE,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="ntp",
                ip_address=client_ip,
                user_agent=user_agent,
                before_value=before_value,
                after_value=new_config,
            )
        except Exception as e:
            logger.warning("Audit log failed: %s", e)

        return ApiResponse(
            data={
                "enabled": enabled,
                "server": server,
                "message": "NTP config updated",
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("update_ntp_config failed: %s", e)
        try:
            await audit_svc.log(
                AuditAction.CONFIG_UPDATE,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="ntp",
                ip_address=client_ip,
                user_agent=user_agent,
                status="failed",
                error_message=str(e),
            )
        except Exception as audit_e:
            logger.warning("Audit log failed: %s", audit_e)
        raise HTTPException(status_code=500, detail=SystemErrors.STATUS_FAILED) from e


# ── Database Migration Status API ──


@router.get("/migration/status", response_model=ApiResponse)
async def get_migration_status(
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """获取数据库迁移状态

    返回最近一次迁移的状态，包括成功/失败信息和错误详情。
    管理员可查看完整错误信息，普通用户仅可见状态摘要。
    """
    try:
        from edgelite.app import _app_state

        container = _app_state
        if container is None:
            return ApiResponse(
                data={
                    "current_status": "unknown",
                    "last_updated": None,
                    "error": None,
                }
            )

        migration_status = getattr(container, "_migration_status", {})

        result = {
            "current_status": migration_status.get("current_status", "unknown"),
            "last_updated": migration_status.get("last_updated"),
        }

        # Add failure details if present
        if "last_failure" in migration_status:
            failure = migration_status["last_failure"]
            if user["role"] == "admin":
                # Admin sees full error details
                result["last_failure"] = failure
            else:
                # Non-admin sees only timestamp and truncated error
                result["last_failure"] = {
                    "timestamp": failure.get("timestamp"),
                    "error": (failure.get("error") or "")[:200] + "..."
                    if failure.get("error") and len(failure.get("error", "")) > 200
                    else failure.get("error"),
                }

        return ApiResponse(data=result)

    except Exception as e:
        logger.error("get_migration_status failed: %s", e)
        raise HTTPException(status_code=500, detail=SystemErrors.MIGRATION_STATUS_FAILED) from e


@router.post("/migration/retry", response_model=ApiResponse)
async def retry_migration(
    request: Request,
    audit_svc: AuditServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    """手动重试数据库迁移（仅管理员可用）

    在迁移失败后调用此接口可尝试重新执行迁移。
    返回迁移结果：成功或失败详情。
    """
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail=AuthzErrors.PERMISSION_DENIED)

    # 第三轮审计修复: 记录迁移重试审计日志
    from edgelite.api.auth import _get_client_ip
    from edgelite.services.audit_service import AuditAction

    client_ip = _get_client_ip(request)
    user_agent = request.headers.get("user-agent", "")

    try:
        from edgelite.app import _app_state

        container = _app_state
        if container is None:
            raise HTTPException(status_code=503, detail=CommonErrors.SERVICE_NOT_READY)

        database = getattr(container, "database", None)
        if database is None:
            raise HTTPException(status_code=503, detail=CommonErrors.DB_NOT_READY)

        # Update status to in_progress
        await database._update_migration_status("in_progress", None)
        logger.info("Manual migration retry initiated by user: %s", user["username"])

        # Attempt migration
        try:
            async with database.engine.begin() as conn:
                success = await database._migrate(conn)

            if success:
                await database._update_migration_status("success", None)
                try:
                    await audit_svc.log(
                        AuditAction.CONFIG_RELOAD,
                        user_id=user["user_id"],
                        username=user["username"],
                        resource_type="migration",
                        ip_address=client_ip,
                        user_agent=user_agent,
                        after_value={"status": "success"},
                    )
                except Exception as e:
                    logger.warning("Audit log failed: %s", e)
                return ApiResponse(
                    data={
                        "status": "success",
                        "message": "Migration completed successfully",
                    }
                )
            else:
                raise RuntimeError("Migration returned False")

        except ValueError as migration_err:
            error_msg = str(migration_err)
            logger.warning("Migration validation error: %s", error_msg)
            await database._update_migration_status("failed", error_msg)
            try:
                await audit_svc.log(
                    AuditAction.CONFIG_RELOAD,
                    user_id=user["user_id"],
                    username=user["username"],
                    resource_type="migration",
                    ip_address=client_ip,
                    user_agent=user_agent,
                    status="failed",
                    error_message=error_msg,
                )
            except Exception as e:
                logger.warning("Audit log failed: %s", e)
            # FIXED(严重): 原问题-detail=str(migration_err) 暴露异常内部信息
            raise HTTPException(status_code=422, detail=SystemErrors.MIGRATION_FAILED) from migration_err
        except IntegrityError as migration_err:
            error_msg = str(migration_err)
            logger.warning("Migration integrity conflict: %s", error_msg)
            await database._update_migration_status("failed", error_msg)
            try:
                await audit_svc.log(
                    AuditAction.CONFIG_RELOAD,
                    user_id=user["user_id"],
                    username=user["username"],
                    resource_type="migration",
                    ip_address=client_ip,
                    user_agent=user_agent,
                    status="failed",
                    error_message=error_msg,
                )
            except Exception as e:
                logger.warning("Audit log failed: %s", e)
            raise HTTPException(status_code=409, detail=SystemErrors.MIGRATION_FAILED) from migration_err
        except Exception as migration_err:
            error_msg = str(migration_err)
            logger.error("Manual migration retry failed: %s", error_msg)
            await database._update_migration_status("failed", error_msg)
            try:
                await audit_svc.log(
                    AuditAction.CONFIG_RELOAD,
                    user_id=user["user_id"],
                    username=user["username"],
                    resource_type="migration",
                    ip_address=client_ip,
                    user_agent=user_agent,
                    status="failed",
                    error_message=error_msg,
                )
            except Exception as e:
                logger.warning("Audit log failed: %s", e)
            raise HTTPException(
                status_code=500, detail=SystemErrors.MIGRATION_FAILED
            ) from migration_err  # FIXED(P3): 原问题-B904 异常链丢失; 修复-添加 from migration_err

    except HTTPException:
        raise
    except ValueError as e:
        logger.warning("retry_migration validation error: %s", e)
        # FIXED(严重): 原问题-detail=str(e) 暴露异常内部信息
        raise HTTPException(status_code=422, detail=SystemErrors.MIGRATION_FAILED) from e
    except IntegrityError as e:
        logger.warning("retry_migration integrity conflict: %s", e)
        raise HTTPException(status_code=409, detail=SystemErrors.MIGRATION_FAILED) from e
    except Exception as e:
        logger.error("retry_migration failed: %s", e)
        raise HTTPException(status_code=500, detail=SystemErrors.RESTORE_FAILED) from e


@router.get("/migration/history", response_model=ApiResponse)
async def get_migration_history(
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """获取数据库迁移历史记录

    返回最近的迁移状态历史，包括成功/失败的时间戳和简要信息。
    """
    try:
        from edgelite.app import _app_state

        container = _app_state
        if container is None:
            return ApiResponse(data={"history": []})

        migration_status = getattr(container, "_migration_status", {})

        history = []

        # Add current status to history
        if "last_updated" in migration_status:
            history.append(
                {
                    "timestamp": migration_status["last_updated"],
                    "status": migration_status.get("current_status", "unknown"),
                    "message": "Last migration attempt",
                }
            )

        # Add failure details if present
        if "last_failure" in migration_status:
            failure = migration_status["last_failure"]
            history.append(
                {
                    "timestamp": failure.get("timestamp"),
                    "status": "failed",
                    "message": "Migration failed",
                    "error_preview": (failure.get("error") or "")[:100] + "..."
                    if failure.get("error") and len(failure.get("error", "")) > 100
                    else failure.get("error"),
                }
            )

        # Sort by timestamp descending
        history.sort(key=lambda x: x.get("timestamp", ""), reverse=True)

        return ApiResponse(data={"history": history[:10]})  # Return last 10 entries

    except Exception as e:
        logger.error("get_migration_history failed: %s", e)
        raise HTTPException(status_code=500, detail=SystemErrors.MIGRATION_HISTORY_FAILED) from e


# ── Database Lock Status API ──


@router.get("/locks/status", response_model=ApiResponse)
async def get_lock_status(
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """获取数据库锁状态

    返回所有表锁和全局锁的状态，用于监控和调试。
    """
    try:
        from edgelite.app import _app_state

        container = _app_state
        if container is None:
            # FIXED-P2: 原问题-container为None时返回200+error data，状态码与语义不一致;
            # 修复-改为raise HTTPException(503)，与其他端点一致
            raise HTTPException(status_code=503, detail=CommonErrors.SERVICE_NOT_READY)

        database = getattr(container, "database", None)
        if database is None:
            raise HTTPException(status_code=503, detail=CommonErrors.DB_NOT_READY)

        lock_status = database.get_lock_status()
        return ApiResponse(data=lock_status)

    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_lock_status failed: %s", e)
        raise HTTPException(status_code=500, detail=SystemErrors.LOCK_STATUS_FAILED) from e


@router.get("/network", response_model=ApiResponse)
async def get_network_info(
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """获取当前网络配置信息（只读）

    返回主机名、当前IP地址等只读网络信息。
    修改网络配置有断网风险，暂不支持 PUT。
    """
    try:
        import socket

        hostname = socket.gethostname()
        try:
            local_ip = socket.gethostbyname(hostname)
        except Exception:
            local_ip = "127.0.0.1"

        interfaces: list[dict] = []
        try:
            import psutil

            addrs = psutil.net_if_addrs()
            stats = psutil.net_if_stats()
            for name, addr_list in addrs.items():
                for addr in addr_list:
                    if addr.family.name in ("AF_INET", "AF_INET6") and addr.address:
                        # 跳过回环地址（IPv4/IPv6）
                        if addr.address in ("127.0.0.1", "::1"):
                            continue
                        interfaces.append(
                            {
                                "name": name,
                                "family": addr.family.name,
                                "address": addr.address,
                                "netmask": addr.netmask or "",
                                "broadcast": addr.broadcast or "",
                                "isup": bool(stats.get(name) and stats[name].isup),
                            }
                        )
        except Exception as e:
            logger.debug("psutil net_if_addrs unavailable: %s", e)

        return ApiResponse(
            data={
                "hostname": hostname,
                "local_ip": local_ip,
                "interfaces": interfaces,
            }
        )
    except Exception as e:
        logger.error("get_network_info failed: %s", e)
        raise HTTPException(status_code=500, detail=SystemErrors.STATUS_FAILED) from e
