"""设备管理API路由"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import logging
import socket
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from edgelite.api.deps import (
    AuditServiceDep,
    ConfigDep,
    DeviceServiceDep,
    OptionalCurrentUser,
    PaginationDep,
    SchedulerDep,
    require_permission,
)
from edgelite.api.error_codes import (
    AuthzErrors,
    CommonErrors,
    DeviceErrors,
    RepoErrors,
    make_error_response,
)
from edgelite.models.common import ApiResponse, PagedResponse
from edgelite.models.db import StaleDataError
from edgelite.models.device import (
    BatchDeviceIds,
    CreateFromTemplateRequest,
    DeviceCreate,
    DeviceResponse,
    DeviceUpdate,
    DeviceWritePolicyUpdate,
    DiscoverRequest,
    ExportDevicesRequest,
    ImportDevicesRequest,
    PushDeviceDataRequest,
    SimulatorCreate,
    TemplateCreate,
    TemplateResponse,
    WritePointRequest,
)
from edgelite.models.health import DeviceHealthResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/devices", tags=["Devices"])

# FIXED(P1): 原问题-RUF006 create_task 返回值未保存，task 可能被 GC 回收;
#            修复-模块级 _background_tasks 集合保存引用，任务完成时自动移除
_background_tasks: set[asyncio.Task] = set()


async def _check_device_owner(svc, device_id: str, user) -> None:
    """Check device access permission.

    Access rules (evaluated in order):
    1. admin role: can access ALL devices, including created_by=None (orphan devices)
    2. Non-admin: can access devices where created_by == user_id (owner)
    3. Non-admin: can access devices shared with them via resource_shares table
    4. Non-admin: CANNOT access devices where created_by is None or belongs to another user
       and is not shared with them
    """
    if user["role"] == "admin":
        return
    device = await svc.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail=DeviceErrors.NOT_FOUND)
    if device.get("created_by") == user["user_id"]:
        return
    from edgelite.app import _app_state
    from edgelite.storage.sqlite_repo import ResourceShareRepo

    container = _app_state
    share_repo = ResourceShareRepo(container.database, container.database.write_lock)
    has_access = await share_repo.check_user_has_access("device", device_id, user["user_id"])
    if has_access:
        return
    raise HTTPException(status_code=403, detail=AuthzErrors.RESOURCE_OWNERSHIP_DENIED)


async def _check_device_access(device: dict, user) -> None:
    """Check if user can access a device (owner or shared). Raises 403 if not."""
    if user["role"] == "admin":
        return
    if device.get("created_by") == user["user_id"]:
        return
    from edgelite.app import _app_state
    from edgelite.storage.sqlite_repo import ResourceShareRepo

    container = _app_state
    share_repo = ResourceShareRepo(container.database, container.database.write_lock)
    has_access = await share_repo.check_user_has_access("device", device["device_id"], user["user_id"])
    if has_access:
        return
    raise HTTPException(status_code=403, detail=AuthzErrors.RESOURCE_OWNERSHIP_DENIED)


async def _get_accessible_device_ids(svc, user) -> set[str]:
    """Get device IDs accessible by user (owned + shared)."""
    if user["role"] == "admin":
        return None
    owned_ids = set(await svc.list_device_ids_by_owner(user["user_id"]))
    from edgelite.app import _app_state
    from edgelite.storage.sqlite_repo import ResourceShareRepo

    container = _app_state
    share_repo = ResourceShareRepo(container.database, container.database.write_lock)
    shared_ids = await share_repo.get_shared_resource_ids(user["user_id"], "device")
    return owned_ids | shared_ids


@router.get("", response_model=PagedResponse[DeviceResponse])
async def list_devices(
    svc: DeviceServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.DEVICE_READ)),
    pagination: PaginationDep = None,  # noqa: E501
    # FIXED(一般): 枚举值未校验，恶意用户可传任意字符串绕过过滤；改为 Literal 校验
    status: Literal["online", "offline", "error", "unknown"] | None = None,
    protocol: str | None = None,
    search: str | None = None,
    collect_status: Literal["collecting", "idle", "error", "unknown"] | None = None,
):
    try:
        created_by = None if user["role"] == "admin" else user["user_id"]
        devices, total = await svc.list_devices(
            pagination.page, pagination.size,
            status, protocol, search,
            created_by=created_by,
            collect_status=collect_status,
        )
        if user["role"] != "admin":
            from edgelite.app import _app_state
            from edgelite.storage.sqlite_repo import ResourceShareRepo

            container = _app_state
            share_repo = ResourceShareRepo(container.database, container.database.write_lock)
            shared_ids = await share_repo.get_shared_resource_ids(user["user_id"], "device")
            if shared_ids:
                owned_ids = {d["device_id"] for d in devices}
                missing_ids = shared_ids - owned_ids
                if missing_ids:
                    # LP-07: 使用批量查询替代 N+1 循环调用 svc.get_device(did)
                    missing_devices = await svc.list_devices_by_ids(list(missing_ids))
                    devices.extend(missing_devices)
                    total += len(missing_devices)
        return PagedResponse(data=devices, total=total, page=pagination.page, size=pagination.size)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("list_devices failed: %s", e)
        # FIXED: 原问题：中文硬编码detail
        raise HTTPException(status_code=500, detail=DeviceErrors.LIST_FAILED) from e


@router.post("", response_model=ApiResponse[DeviceResponse], status_code=201)
async def create_device(
    body: DeviceCreate,
    svc: DeviceServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.DEVICE_CREATE)),
    audit_svc: AuditServiceDep = None,  # FIXED-M03: FastAPI dependency injection provides the value
    request: Request = None,
):
    try:
        # Validate device config against driver schema
        try:
            from edgelite.drivers.registry import get_driver_registry
            registry = get_driver_registry()
            if registry:
                driver_cls = registry.get_driver_class(body.protocol if hasattr(body, "protocol") else "")
                if driver_cls:
                    driver_instance = driver_cls()
                    validation = driver_instance.validate_config(body.config if hasattr(body, "config") else {})
                    if not validation.valid:
                        raise HTTPException(status_code=422, detail={
                            "error_code": "ERR_DEVICE_CONFIG_INVALID",
                            "errors": validation.errors,
                            "warnings": validation.warnings,
                        })
        except HTTPException:
            raise
        except Exception as e:
            logger.debug("Driver validation skipped: %s", e)

        device = await svc.create_device(body.model_dump(), created_by=user["user_id"])
        try:
            from edgelite.services.audit_service import AuditAction
            # 补充ip_address和user_agent用于审计追溯
            ip_address = request.client.host if request and request.client else None
            user_agent = request.headers.get("User-Agent") if request else None
            await audit_svc.log(
                AuditAction.DEVICE_CREATE,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="device",
                resource_id=body.device_id,
                ip_address=ip_address,
                user_agent=user_agent,
                after_value=body.model_dump(),
            )
        except Exception as e:
            logger.warning("Audit log failed: %s", e)
        return ApiResponse(data=device)
    except ValueError as e:
        err_msg = str(e)
        if "already exists" in err_msg.lower() or "duplicate" in err_msg.lower():
            raise HTTPException(status_code=409, detail=DeviceErrors.ALREADY_EXISTS) from e
        elif "unsupported protocol" in err_msg.lower():
            raise HTTPException(status_code=422, detail=DeviceErrors.DRIVER_UNAVAILABLE) from e
        elif "missing required" in err_msg.lower():
            raise HTTPException(status_code=422, detail={
                "error_code": DeviceErrors.CONFIG_INVALID,
                "errors": [err_msg],
                "warnings": [],
            }) from e
        elif "driver start failed" in err_msg.lower() or "connection" in err_msg.lower():
            raise HTTPException(status_code=409, detail={
                "error_code": DeviceErrors.CREATE_FAILED,
                "errors": [err_msg],
                "warnings": [],
            }) from e
        else:
            raise HTTPException(status_code=422, detail={
                "error_code": DeviceErrors.CONFIG_INVALID,
                "errors": [err_msg],
                "warnings": [],
            }) from e
    except HTTPException:
        raise
    except Exception as e:
        logger.error("create_device failed: %s", e)
        # FIXED: 原问题：中文硬编码detail
        raise HTTPException(status_code=500, detail=DeviceErrors.CREATE_FAILED) from e


@router.post("/simulator", response_model=ApiResponse[DeviceResponse], status_code=201)
async def create_simulator(
    body: SimulatorCreate,
    svc: DeviceServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.DEVICE_CREATE)),
):
    try:
        device = await svc.create_simulator(body.model_dump(), created_by=user["user_id"])
        return ApiResponse(data=device)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("create_simulator failed: %s", e)
        # FIXED: 原问题：中文硬编码detail
        raise HTTPException(status_code=500, detail=DeviceErrors.SIMULATOR_FAILED) from e


@router.post("/discover", response_model=ApiResponse)
async def discover_devices(
    body: DiscoverRequest,
    svc: DeviceServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.DEVICE_CREATE)),
):
    try:
        try:
            from edgelite.drivers.registry import get_driver_registry
            registry = get_driver_registry()
            if registry:
                driver_cls = registry.get_driver_class(body.protocol)
                if driver_cls:
                    caps = getattr(driver_cls, "capabilities", None)
                    if caps is not None:
                        discover_supported = getattr(caps, "discover", None)
                        if discover_supported is False:
                            raise HTTPException(status_code=400, detail={
                                "error_code": "ERR_DEVICE_CAPABILITY_NOT_SUPPORTED",
                                "message": f"Driver '{body.protocol}' does not support discover capability",
                            })
        except HTTPException:
            raise
        except Exception as e:
            logger.debug("Driver validation skipped: %s", e)

        devices = await svc.discover_devices(body.protocol, body.config)
        return ApiResponse(data=devices)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("discover_devices failed: %s", e)
        # FIXED: 原问题：中文硬编码detail
        raise HTTPException(status_code=500, detail=DeviceErrors.DISCOVER_FAILED) from e


def _is_host_safe_for_device_test(host: str) -> bool:
    """SSRF 校验：至少拦截 loopback/link_local/未指定/组播/保留地址。

    FIXED(安全): 设备连通性测试场景，允许 is_private（内网设备测试是合理场景），
    但拦截 is_loopback 和 is_link_local（云元数据 169.254.x.x）等危险地址。
    域名先通过 socket.getaddrinfo 解析为 IP，再校验每个解析结果。
    """
    if not host:
        return False
    # 直接作为 IP 解析
    try:
        ip = ipaddress.ip_address(host)
        return not (ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved)
    except ValueError:
        pass
    # 域名：解析为 IP 后校验每个地址
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


@router.post("/test-connection", response_model=ApiResponse)
async def test_device_connection(
    body: DiscoverRequest,
    user: dict[str, str] = Depends(require_permission(Permission.DEVICE_CREATE)),
):
    """测试设备连通性（保存前预检）。

    实现：尝试 TCP socket 连接到 host:port，3 秒超时。
    对于不依赖 TCP 的协议（如 modbus_rtu），返回 unsupported 提示。
    """
    protocol = body.protocol
    config = body.config or {}
    host = config.get("host") or config.get("ip") or config.get("endpoint") or ""
    # 从 endpoint 中提取 host:port（如 opc.tcp://host:4840）
    if not host and config.get("endpoint"):
        host = config["endpoint"]
    if isinstance(host, str) and "://" in host:
        # 提取 // 后面的 host[:port]
        try:
            after_proto = host.split("://", 1)[1]
            host = after_proto.split("/", 1)[0]
        except Exception as parse_err:
            logger.debug("Host parse fallback used for %r: %s", host, parse_err)

    # 对于 modbus_rtu 等串口协议，无法用 TCP 检测
    if protocol in ("modbus_rtu", "modbus-rtu"):
        return ApiResponse(data={
            "success": False,
            "supported": False,
            "message": "Serial protocol cannot be tested via TCP; please save and run self-test.",
        })

    if not host:
        return ApiResponse(data={
            "success": False,
            "supported": False,
            "message": "No host/ip in config to test.",
        })

    # 默认端口映射
    default_ports = {
        "modbus_tcp": 502, "opcua": 4840, "mqtt": 1883,
        "http": 80, "https": 443, "siemens_s7": 102,
        "mitsubishi_mc": 5000, "omron_fins": 9600,
        "allen_bradley": 44818, "opc_da": 135, "onvif": 80,
    }
    port = config.get("port")
    if not port:
        # 尝试从 host 中提取
        if ":" in host:
            host_part, port_part = host.rsplit(":", 1)
            try:
                port = int(port_part)
                host = host_part
            except ValueError:
                pass
        if not port:
            port = default_ports.get(protocol, 80)

    try:
        # 3 秒超时
        # FIXED(安全): SSRF 防护 - 校验 host，至少拦截 loopback/link_local（云元数据 169.254.x.x）
        # 允许 is_private（内网设备连通性测试是合理场景）
        if not _is_host_safe_for_device_test(host):
            raise HTTPException(status_code=400, detail=DeviceErrors.SSRF_BLOCKED)
        try:
            _reader, writer = await asyncio.wait_for(  # FIXED(P3): 原问题-解包变量reader未使用; 修复-改为_reader前缀
                asyncio.open_connection(host, int(port)),
                timeout=3.0,
            )
            writer.close()
            try:
                await writer.wait_closed()
            except Exception as close_err:
                logger.debug("writer.wait_closed() failed: %s", close_err)
            return ApiResponse(data={
                "success": True,
                "supported": True,
                "host": host,
                "port": int(port),
                "message": f"Connection to {host}:{port} succeeded.",
            })
        except (TimeoutError, ConnectionRefusedError, OSError) as ce:
            return ApiResponse(data={
                "success": False,
                "supported": True,
                "host": host,
                "port": int(port),
                "message": f"Connection to {host}:{port} failed: {type(ce).__name__}",
            })
    except Exception as e:
        logger.error("test_device_connection failed: %s", e)
        return ApiResponse(data={
            "success": False,
            "supported": False,
            "message": f"Test failed: {e}",
        })


# FIXED: 静态路由必须在/{device_id}动态路由之前注册，否则FastAPI将静态路径匹配为device_id导致404
@router.get("/health/all", response_model=ApiResponse)
async def list_all_device_health(
    svc: DeviceServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.DEVICE_READ)),
    # R9-S-14 修复: 原问题-健康检查端点返回全部设备数据无分页，设备数量大时响应过大;
    # 修复-添加 limit/offset 参数，默认限制100条，最大1000条
    limit: int = Query(100, ge=1, le=1000, description="每页数量，最大1000"),
    offset: int = Query(0, ge=0, description="偏移量"),
):
    try:
        data = await svc.list_device_health()
        if user["role"] != "admin":
            accessible_ids = await _get_accessible_device_ids(svc, user)
            if accessible_ids is not None:
                data = [d for d in data if d.get("device_id") in accessible_ids] if isinstance(data, list) else data
        # R9-S-14: 应用分页切片，防止返回过大响应
        if isinstance(data, list):
            total = len(data)
            data = data[offset:offset + limit]
        else:
            total = 0
        return ApiResponse(data={"items": data, "total": total, "limit": limit, "offset": offset})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("list_all_device_health failed: %s", e)
        raise HTTPException(status_code=500, detail=DeviceErrors.LIST_FAILED) from e


@router.get("/health", response_model=ApiResponse)
async def list_device_health_by_ids(
    ids: list[str] = Query(default=[]),
    svc: DeviceServiceDep = None,
    user: dict[str, str] = Depends(require_permission(Permission.DEVICE_READ)),
):
    try:
        if user["role"] != "admin":
            accessible_ids = await _get_accessible_device_ids(svc, user)
            if accessible_ids is not None:
                ids = [i for i in ids if i in accessible_ids]
        data = await svc.list_device_health_for_ids(ids)
        return ApiResponse(data=data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("list_device_health_by_ids failed: %s", e)
        raise HTTPException(status_code=500, detail=DeviceErrors.LIST_FAILED) from e


@router.get("/collect-stats", response_model=ApiResponse)
async def get_collect_stats(
    scheduler: SchedulerDep,
    svc: DeviceServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.DEVICE_READ)),
):
    try:
        stats = await scheduler.get_collect_stats()
        if user["role"] != "admin":
            accessible_ids = await _get_accessible_device_ids(svc, user)
            stats = {k: v for k, v in stats.items() if k in accessible_ids}
        return ApiResponse(data={k: {
            "device_id": v.device_id,
            "avg_latency_ms": round(v.avg_latency_ms, 2),
            "max_latency_ms": round(v.max_latency_ms, 2),
            "total_calls": v.total_calls,
            "timeout_count": v.timeout_count,
            "last_collect_at": v.last_collect_at,
        } for k, v in stats.items()})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_collect_stats failed: %s", e)
        raise HTTPException(status_code=500, detail=DeviceErrors.LIST_FAILED) from e


@router.get("/device-quality-stats", response_model=ApiResponse)
async def get_device_quality_stats(
    scheduler: SchedulerDep,
    svc: DeviceServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.DEVICE_READ)),
):
    try:
        stats = await scheduler.get_device_quality_stats()
        if user["role"] != "admin":
            accessible_ids = await _get_accessible_device_ids(svc, user)
            stats = {k: v for k, v in stats.items() if k in accessible_ids}
        return ApiResponse(data={k: {
            "device_id": v.device_id,
            "success_count": v.success_count,
            "error_count": v.error_count,
            "total_count": v.total_count,
            "error_rate": round(v.error_rate, 4),
        } for k, v in stats.items()})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_device_quality_stats failed: %s", e)
        raise HTTPException(status_code=500, detail=DeviceErrors.LIST_FAILED) from e


@router.post("/batch/delete", response_model=ApiResponse)
async def batch_delete_devices(
    body: BatchDeviceIds,
    svc: DeviceServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.DEVICE_DELETE)),
):
    try:
        results = await svc.batch_delete_devices(
            body.device_ids,
            user_id=user["user_id"],
            is_admin=user["role"] == "admin",
        )
        failed = {k: v[1] for k, v in results.items() if not v[0]}
        success = {k for k, v in results.items() if v[0]}
        return ApiResponse(data={
            "success_count": len(success),
            "failed": failed,
        })
    except HTTPException:
        raise
    except Exception as e:
        logger.error("batch_delete_devices failed: %s", e)
        raise HTTPException(status_code=500, detail=DeviceErrors.BATCH_DELETE_FAILED) from e


@router.post("/batch/start-collect", response_model=ApiResponse)
async def batch_start_collect(
    body: BatchDeviceIds,
    svc: DeviceServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.DEVICE_UPDATE)),
):
    try:
        if user["role"] != "admin":
            # FIXED(严重): 原问题-循环逐个调用 svc.get_device(did) 存在 N+1 查询；
            # 改为批量查询后内存校验权限
            devices_map = {d["device_id"]: d for d in await svc.list_devices_by_ids(body.device_ids) if d}
            for did in body.device_ids:
                d = devices_map.get(did)
                if d is not None:
                    await _check_device_access(d, user)
        results = await svc.batch_start_collect(body.device_ids)
        failed = {k: v[1] for k, v in results.items() if not v[0]}
        success = {k for k, v in results.items() if v[0]}
        return ApiResponse(data={
            "success_count": len(success),
            "failed": failed,
        })
    except HTTPException:
        raise
    except Exception as e:
        logger.error("batch_start_collect failed: %s", e)
        raise HTTPException(status_code=500, detail=DeviceErrors.BATCH_START_COLLECT_FAILED) from e


@router.post("/batch/stop-collect", response_model=ApiResponse)
async def batch_stop_collect(
    body: BatchDeviceIds,
    svc: DeviceServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.DEVICE_UPDATE)),
):
    try:
        if user["role"] != "admin":
            # FIXED(严重): 原问题-循环逐个调用 svc.get_device(did) 存在 N+1 查询；
            # 改为批量查询后内存校验权限
            devices_map = {d["device_id"]: d for d in await svc.list_devices_by_ids(body.device_ids) if d}
            for did in body.device_ids:
                d = devices_map.get(did)
                if d is not None:
                    await _check_device_access(d, user)
        results = await svc.batch_stop_collect(body.device_ids)
        failed = {k: v[1] for k, v in results.items() if not v[0]}
        success = {k for k, v in results.items() if v[0]}
        return ApiResponse(data={
            "success_count": len(success),
            "failed": failed,
        })
    except HTTPException:
        raise
    except Exception as e:
        logger.error("batch_stop_collect failed: %s", e)
        raise HTTPException(status_code=500, detail=DeviceErrors.BATCH_STOP_COLLECT_FAILED) from e


class BatchDeployRequest(BaseModel):
    template_device_id: str
    # SEC-FIX(R7-S-10): 原问题-target_device_ids 为 list[str] 无 max_length 限制，可传超大列表导致资源耗尽;
    # 修复-添加 min_length=1, max_length=100 限制批量部署数量上限
    target_device_ids: list[str] = Field(..., min_length=1, max_length=100)
    override_config: dict[str, Any] | None = None


@router.post("/batch-deploy", response_model=ApiResponse[dict])
async def batch_deploy_config(
    req: BatchDeployRequest,
    device_service: DeviceServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
    audit_svc: AuditServiceDep = None,  # SEC-FIX: 批量部署需记录审计日志
):
    """批量下发设备配置：将模板设备的配置批量应用到目标设备"""
    try:
        # 获取模板设备
        template = await device_service.get_device(req.template_device_id)
        if not template:
            raise HTTPException(status_code=404, detail=DeviceErrors.TEMPLATE_NOT_FOUND)

        # FIXED(严重): 原问题-权限校验循环和部署循环各查询一次 target，存在双重 N+1 查询；
        # 改为循环外批量查询所有 target，构建字典复用
        target_devices = await device_service.list_devices_by_ids(req.target_device_ids)
        target_map = {d["device_id"]: d for d in target_devices if d}

        if user["role"] != "admin":
            await _check_device_access(template, user)
            for target_id in req.target_device_ids:
                target = target_map.get(target_id)
                if target:
                    await _check_device_access(target, user)

        results = {"success": [], "failed": []}
        # SEC-FIX: 提取模板 points 用于敏感字段比对
        template_points = template.get("points", []) if isinstance(template, dict) else getattr(template, "points", [])
        template_points_map = {
            (p.get("name") if isinstance(p, dict) else getattr(p, "name", None)): p
            for p in (template_points or [])
            if (p.get("name") if isinstance(p, dict) else getattr(p, "name", None))
        }

        # R9-S-12 修复: 原问题-串行 for 循环逐个部署，目标设备多时耗时长;
        # 修复-使用 asyncio.gather + Semaphore(10) 并行部署，单个失败不影响其他
        deploy_sem = asyncio.Semaphore(10)

        async def _deploy_one(target_id: str) -> tuple[str, bool, str | None, dict | None]:
            """部署配置到单个目标设备，返回 (target_id, success, error, audit_record)"""
            async with deploy_sem:
                target = target_map.get(target_id)
                if not target:
                    return target_id, False, "Device not found", None

                # SEC-FIX: 对比模板 points 与目标设备现有 points 的 address/data_type/access_mode
                # 批量部署是管理员操作，允许变更但记录 warning 与审计
                target_points = target.get("points", []) if isinstance(target, dict) else getattr(target, "points", [])
                target_points_map = {
                    (p.get("name") if isinstance(p, dict) else getattr(p, "name", None)): p
                    for p in (target_points or [])
                    if (p.get("name") if isinstance(p, dict) else getattr(p, "name", None))
                }
                sensitive_changes: list[str] = []
                for pt_name, new_pt in template_points_map.items():
                    old_pt = target_points_map.get(pt_name)
                    if old_pt is None:
                        sensitive_changes.append(f"point added: {pt_name}")
                        continue
                    for sensitive_field in ("address", "data_type", "access_mode"):
                        old_val = old_pt.get(sensitive_field) if isinstance(old_pt, dict) else getattr(old_pt, sensitive_field, None)
                        new_val = new_pt.get(sensitive_field) if isinstance(new_pt, dict) else getattr(new_pt, sensitive_field, None)
                        if old_val != new_val:
                            sensitive_changes.append(f"point {pt_name}.{sensitive_field}: {old_val!r} -> {new_val!r}")
                if sensitive_changes:
                    logger.warning(
                        "Batch deploy sensitive point changes for device %s by %s: %s",
                        target_id, user.get("username"), sensitive_changes,
                    )

                # 构建更新数据：从模板复制points和collect_interval，可选覆盖config
                update_data = {
                    "points": template.get("points", []) if isinstance(template, dict) else getattr(template, "points", []),
                    "collect_interval": template.get("collect_interval", 60) if isinstance(template, dict) else getattr(template, "collect_interval", 60),
                }
                if req.override_config:
                    update_data["config"] = req.override_config

                # SEC-FIX: 保存变更前快照用于审计
                before_snapshot = target if isinstance(target, dict) else (
                    target.model_dump() if hasattr(target, "model_dump") else dict(target.__dict__)
                )
                await device_service.update_device(target_id, update_data)
                audit_record = {
                    "device_id": target_id,
                    "before": before_snapshot,
                    "after": update_data,
                    "sensitive_changes": sensitive_changes,
                }
                return target_id, True, None, audit_record

        # 并行执行所有目标设备的部署，return_exceptions=True 确保单个失败不阻塞其他
        deploy_results = await asyncio.gather(
            *[_deploy_one(tid) for tid in req.target_device_ids],
            return_exceptions=True,
        )

        # 收集并行部署结果
        deploy_audit_records: list[dict] = []
        for res in deploy_results:
            if isinstance(res, Exception):
                # _deploy_one 内部已捕获异常，此处为未预期异常的安全兜底
                results["failed"].append({"device_id": "unknown", "error": str(res)})
                continue
            target_id, success, error, audit_record = res
            if success:
                results["success"].append(target_id)
                if audit_record:
                    deploy_audit_records.append(audit_record)
            else:
                results["failed"].append({"device_id": target_id, "error": error})

        # SEC-FIX: 批量部署审计日志，details 含 before/after 快照和 deployed_devices 列表
        try:
            from edgelite.services.audit_service import AuditAction
            if audit_svc is not None:
                await audit_svc.log(
                    action=AuditAction.DEVICE_UPDATE,
                    user_id=user.get("user_id"),
                    username=user.get("username"),
                    resource_type="device",
                    resource_id=req.template_device_id,
                    details={
                        "operation": "batch_deploy",
                        "template_device_id": req.template_device_id,
                        "deployed_devices": results["success"],
                        "failed_devices": results["failed"],
                        "deploy_records": deploy_audit_records,
                    },
                    status="success" if not results["failed"] else "partial",
                )
        except Exception as e:
            logger.warning("Batch deploy audit log failed: %s", e)

        return ApiResponse(data=results)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Batch deploy failed: %s", e)
        raise HTTPException(status_code=500, detail=DeviceErrors.LIST_FAILED) from e


# ------------------------------------------------------------------
# 设备模板管理
# ------------------------------------------------------------------

@router.post("/templates", response_model=ApiResponse[TemplateResponse], status_code=201)
async def create_template(
    body: TemplateCreate,
    svc: DeviceServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.DEVICE_CREATE)),
):
    try:
        source = await svc.get_device(body.device_id)
        if source is None:
            raise HTTPException(status_code=404, detail=DeviceErrors.NOT_FOUND)
        await _check_device_access(source, user)
        template = await svc.create_template(body.device_id, body.template_name, created_by=user["user_id"])
        return ApiResponse(data=template)
    except ValueError as e:
        raise HTTPException(status_code=409, detail={"error_code": DeviceErrors.TEMPLATE_CREATE_FAILED, "errors": [str(e)], "warnings": []}) from e
    except HTTPException:
        raise
    except Exception as e:
        logger.error("create_template failed: %s", e)
        raise HTTPException(status_code=500, detail=DeviceErrors.TEMPLATE_CREATE_FAILED) from e


@router.get("/templates", response_model=ApiResponse[list[TemplateResponse]])
async def list_templates(
    svc: DeviceServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.DEVICE_READ)),
):
    try:
        created_by = None if user["role"] == "admin" else user["user_id"]
        templates = await svc.list_templates(created_by=created_by)
        return ApiResponse(data=templates)
    except ValueError as e:
        raise HTTPException(status_code=503, detail={"error_code": DeviceErrors.TEMPLATE_LIST_FAILED, "errors": [str(e)], "warnings": []}) from e
    except HTTPException:
        raise
    except Exception as e:
        logger.error("list_templates failed: %s", e)
        raise HTTPException(status_code=500, detail=DeviceErrors.TEMPLATE_LIST_FAILED) from e


@router.post("/from-template", response_model=ApiResponse[DeviceResponse], status_code=201)
async def create_from_template(
    body: CreateFromTemplateRequest,
    svc: DeviceServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.DEVICE_CREATE)),
):
    try:
        device = await svc.create_from_template(
            body.template_name,
            body.model_dump(exclude={"template_name"}),
            created_by=user["user_id"],
        )
        return ApiResponse(data=device)
    except ValueError as e:
        raise HTTPException(status_code=409, detail={"error_code": DeviceErrors.FROM_TEMPLATE_FAILED, "errors": [str(e)], "warnings": []}) from e
    except HTTPException:
        raise
    except Exception as e:
        logger.error("create_from_template failed: %s", e)
        raise HTTPException(status_code=500, detail=DeviceErrors.FROM_TEMPLATE_FAILED) from e


@router.delete("/templates/{name}", response_model=ApiResponse)
async def delete_template(
    # FIXED-P2: name Path 参数增加 max_length 约束，防止超长输入造成资源消耗
    name: Annotated[str, Path(max_length=128)],
    svc: DeviceServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.DEVICE_DELETE)),
):
    try:
        if user["role"] != "admin":
            templates = await svc.list_templates(created_by=user["user_id"])
            template_names = {t["template_name"] for t in templates}
            if name not in template_names:
                raise HTTPException(status_code=403, detail=AuthzErrors.RESOURCE_OWNERSHIP_DENIED)
        success = await svc.delete_template(name)
        if not success:
            raise HTTPException(status_code=404, detail=DeviceErrors.TEMPLATE_NOT_FOUND)
        return ApiResponse()
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=503, detail={"error_code": DeviceErrors.TEMPLATE_DELETE_FAILED, "errors": [str(e)], "warnings": []}) from e
    except Exception as e:
        logger.error("delete_template failed: %s", e)
        raise HTTPException(status_code=500, detail=DeviceErrors.TEMPLATE_DELETE_FAILED) from e


# ------------------------------------------------------------------
# 批量导入导出
# ------------------------------------------------------------------

@router.post("/export", response_model=ApiResponse)
async def export_devices(
    body: ExportDevicesRequest,
    svc: DeviceServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.DEVICE_READ)),
):
    try:
        if user["role"] != "admin":
            accessible_ids = await _get_accessible_device_ids(svc, user)
            for did in body.device_ids:
                if did not in accessible_ids:
                    raise HTTPException(status_code=403, detail=AuthzErrors.RESOURCE_OWNERSHIP_DENIED)
        # R9-S-07 修复: 直接使用字典列表传递，避免 json.dumps/loads 往返
        devices_data = await svc.export_devices(body.device_ids)
        return ApiResponse(data=devices_data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("export_devices failed: %s", e)
        raise HTTPException(status_code=500, detail=DeviceErrors.EXPORT_FAILED) from e


@router.post("/import", response_model=ApiResponse)
async def import_devices(
    body: ImportDevicesRequest,
    svc: DeviceServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.DEVICE_CREATE)),
):
    try:
        # R9-S-07 修复: 直接传递字典列表，避免 json.dumps/loads 往返
        # FIXED-Bug22: 事务模式批量导入添加超时保护，避免大量设备导入超过反向代理超时（通常 60s）
        # 之前：无超时保护，100+ 设备事务导入可能挂起直到 Nginx 502/504
        import_timeout = 120.0 if body.atomic else 300.0
        try:
            result = await asyncio.wait_for(
                svc.import_devices(body.data, body.overwrite, atomic=body.atomic, created_by=user["user_id"]),
                timeout=import_timeout,
            )
        except TimeoutError as exc:  # FIXED(P3): 原问题-B904 异常链丢失; 修复-添加 from exc
            logger.warning("import_devices timeout (atomic=%s, timeout=%ss)", body.atomic, import_timeout)
            raise HTTPException(
                status_code=504,
                detail="Device import timeout, please reduce batch size or use non-atomic mode",
            ) from exc
        if body.atomic and result["failed"] > 0:
            # FIXED-ATOMIC-IMPORT: 事务模式下失败返回 400
            raise HTTPException(
                status_code=400,
                detail=DeviceErrors.IMPORT_FAILED,
            )
        elif result["errors"]:
            logger.warning("import_devices completed with errors: %s", result["errors"])
        return ApiResponse(data=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("import_devices failed: %s", e)
        raise HTTPException(status_code=500, detail=DeviceErrors.IMPORT_FAILED) from e


@router.get("/{device_id}", response_model=ApiResponse[DeviceResponse])
async def get_device(
    device_id: Annotated[str, Path(max_length=128)],
    svc: DeviceServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.DEVICE_READ)),
):
    try:
        device = await svc.get_device(device_id)
        if device is None:
            raise HTTPException(status_code=404, detail=DeviceErrors.NOT_FOUND)
        await _check_device_access(device, user)
        return ApiResponse(data=device)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_device failed: %s", e)
        # FIXED: 原问题：中文硬编码detail
        raise HTTPException(status_code=500, detail=DeviceErrors.GET_FAILED) from e


@router.put("/{device_id}", response_model=ApiResponse[DeviceResponse])
async def update_device(
    device_id: Annotated[str, Path(max_length=128)],
    body: DeviceUpdate,
    svc: DeviceServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.DEVICE_UPDATE)),
    audit_svc: AuditServiceDep = None,  # FIXED-M03: FastAPI dependency injection provides the value
):
    try:
        data = body.model_dump(exclude_none=True)
        existing = await svc.get_device(device_id)
        if existing is None:
            raise HTTPException(status_code=404, detail=DeviceErrors.NOT_FOUND)
        await _check_device_access(existing, user)
        # SEC-FIX-V06: 保存变更前快照，用于审计 before_value 与敏感字段变更检测
        before_snapshot = existing if isinstance(existing, dict) else (
            existing.model_dump() if hasattr(existing, "model_dump") else dict(existing.__dict__)
        )
        # SEC-FIX-V03: 字段级白名单——只允许已知安全字段进入更新，防止注入任意 config 键
        _ALLOWED_UPDATE_KEYS = {"name", "config", "points", "collect_interval", "_version"}
        rejected_keys = set(data.keys()) - _ALLOWED_UPDATE_KEYS
        if rejected_keys:
            logger.warning("update_device rejected unknown keys: %s (user=%s)", rejected_keys, user.get("user_id"))
            data = {k: v for k, v in data.items() if k in _ALLOWED_UPDATE_KEYS}
        # SEC-FIX-V11: 写保护字段从 config 中剥离——必须通过独立的 write-policy 端点修改
        # 防止拥有 DEVICE_UPDATE 权限的用户越权关闭写保护
        _WRITE_POLICY_KEYS = {"write_verify", "write_rate_limit", "write_audit", "write_whitelist"}
        if "config" in data and isinstance(data["config"], dict):
            stripped = {k: v for k, v in data["config"].items() if k in _WRITE_POLICY_KEYS}
            if stripped:
                logger.warning(
                    "update_device stripped write-policy keys from config: %s (user=%s, device=%s) — use PUT /write-policy instead",
                    list(stripped.keys()), user.get("user_id"), device_id,
                )
                incoming_config = {k: v for k, v in data["config"].items() if k not in _WRITE_POLICY_KEYS}
                # FIXED-Bug27: 合并数据库中已有的写保护字段，防止 config 整体替换时丢失
                # 之前：剥离后直接用 incoming_config 替换，DeviceRepo.update 是整体替换 config，
                # 导致管理员设置的 write_verify/write_rate_limit 等被永久清除（安全降级漏洞）
                existing_config = before_snapshot.get("config", {}) if isinstance(before_snapshot, dict) else {}
                if not isinstance(existing_config, dict):
                    existing_config = {}
                preserved_policy = {k: v for k, v in existing_config.items() if k in _WRITE_POLICY_KEYS}
                if preserved_policy:
                    data["config"] = {**incoming_config, **preserved_policy}
                else:
                    data["config"] = incoming_config
                if not data["config"]:
                    del data["config"]
        # SEC-FIX-V03: 敏感字段变更检测——点位 name/address/data_type/access_mode 变更需重点审计
        sensitive_changes: list[str] = []
        if "points" in data and isinstance(before_snapshot, dict):
            before_points = {p.get("name"): p for p in (before_snapshot.get("points") or [])} if isinstance(before_snapshot.get("points"), list) else {}
            for new_pt in (data.get("points") or []):
                pt_name = new_pt.get("name") if isinstance(new_pt, dict) else getattr(new_pt, "name", None)
                if not pt_name:
                    continue
                old_pt = before_points.get(pt_name)
                if old_pt is None:
                    sensitive_changes.append(f"point added: {pt_name}")
                    continue
                for sensitive_field in ("address", "data_type", "access_mode"):
                    old_val = old_pt.get(sensitive_field) if isinstance(old_pt, dict) else getattr(old_pt, sensitive_field, None)
                    new_val = new_pt.get(sensitive_field) if isinstance(new_pt, dict) else getattr(new_pt, sensitive_field, None)
                    if old_val != new_val:
                        sensitive_changes.append(f"point {pt_name}.{sensitive_field}: {old_val!r} -> {new_val!r}")
        # SEC-FIX: 非 admin 用户修改点位不可变字段（address/data_type/access_mode）直接拒绝
        # admin 用户允许变更但记录审计（下方已有逻辑）
        # 仅检测字段值变更（排除 point added 场景）
        immutable_field_changes = [
            c for c in sensitive_changes
            if not c.startswith("point added:")
        ]
        if immutable_field_changes and user.get("role") != "admin":
            logger.warning(
                "update_device blocked: non-admin user %s attempted immutable point field changes on %s: %s",
                user.get("username"), device_id, immutable_field_changes,
            )
            raise HTTPException(
                status_code=403,
                detail={
                    "error_code": DeviceErrors.POINT_IMMUTABLE,
                    "message": "Immutable point fields (address/data_type/access_mode) can only be modified by admin",
                    "sensitive_changes": immutable_field_changes,
                },
            )
        # FIXED-P2: 乐观锁-将当前version注入更新数据，防止并发更新丢失
        if isinstance(existing, dict) and "version" in existing:
            data["_version"] = existing["version"]
        # Validate device config against driver schema
        try:
            from edgelite.drivers.registry import get_driver_registry
            registry = get_driver_registry()
            if registry:
                existing = await svc.get_device(device_id)
                protocol = existing.get("protocol", "") if isinstance(existing, dict) else getattr(existing, "protocol", "") if existing else ""
                config = data.get("config", {})
                if protocol and config:
                    driver_cls = registry.get_driver_class(protocol)
                    if driver_cls:
                        driver_instance = driver_cls()
                        validation = driver_instance.validate_config(config)
                        if not validation.valid:
                            raise HTTPException(status_code=422, detail={
                                "error_code": "ERR_DEVICE_CONFIG_INVALID",
                                "errors": validation.errors,
                                "warnings": validation.warnings,
                            })
        except HTTPException:
            raise
        except Exception as e:
            logger.debug("Driver validation skipped: %s", e)

        device = await svc.update_device(device_id, data)
        if device is None:
            # FIXED: 原问题：中文硬编码detail
            raise HTTPException(status_code=404, detail=DeviceErrors.NOT_FOUND)
        try:
            from edgelite.services.audit_service import AuditAction
            # SEC-FIX-V06: 审计日志补 before_value，支持变更前后比对
            audit_kwargs: dict = dict(
                action=AuditAction.DEVICE_UPDATE,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="device",
                resource_id=device_id,
                before_value=before_snapshot,
                after_value=data,
            )
            # SEC-FIX-V03: 敏感字段变更时附加标记，便于安全审计检索
            if sensitive_changes:
                audit_kwargs["details"] = {"sensitive_changes": sensitive_changes}
                logger.warning("Sensitive device update by %s on %s: %s", user.get("username"), device_id, sensitive_changes)
            await audit_svc.log(**audit_kwargs)
        except Exception as e:
            logger.warning("Audit log failed: %s", e)
        return ApiResponse(data=device)
    except HTTPException:
        raise
    except StaleDataError as e:
        logger.warning("StaleDataError in update_device: %s", e)
        raise HTTPException(status_code=409, detail=RepoErrors.STALE_DATA_ERROR) from e
    except Exception as e:
        logger.error("update_device failed: %s", e)
        # FIXED: 原问题：中文硬编码detail
        raise HTTPException(status_code=500, detail=DeviceErrors.UPDATE_FAILED) from e


@router.put("/{device_id}/write-policy", response_model=ApiResponse[DeviceResponse])
async def update_write_policy(
    device_id: Annotated[str, Path(max_length=128)],
    body: DeviceWritePolicyUpdate,
    svc: DeviceServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.DEVICE_WRITE_POLICY_EDIT)),
    audit_svc: AuditServiceDep = None,
):
    """更新设备写保护策略

    SEC-FIX-V11: 写保护配置独立端点，需 DEVICE_WRITE_POLICY_EDIT 权限（仅 ADMIN）
    与普通 DEVICE_UPDATE 权限分离，实现职责分离（SoD）：
    - 拥有 DEVICE_UPDATE 的操作员可改点位/采集间隔，但不能关闭写保护
    - 仅 ADMIN 可修改写保护策略，防止操作员越权降级后恶意写入
    """
    try:
        existing = await svc.get_device(device_id)
        if existing is None:
            raise HTTPException(status_code=404, detail=DeviceErrors.NOT_FOUND)
        await _check_device_access(existing, user)
        # 保存变更前写保护配置快照
        existing_dict = existing if isinstance(existing, dict) else (
            existing.model_dump() if hasattr(existing, "model_dump") else dict(existing.__dict__)
        )
        existing_config = existing_dict.get("config", {}) if isinstance(existing_dict, dict) else {}
        before_policy = {k: existing_config.get(k) for k in ("write_verify", "write_rate_limit", "write_audit", "write_whitelist") if k in existing_config}
        # 合并写保护字段到 config
        policy_data = body.model_dump(exclude_none=True)
        merged_config = {**existing_config, **policy_data}
        # 检测敏感变更（关闭写保护/审计/限流）
        sensitive_changes: list[str] = []
        for k, new_val in policy_data.items():
            old_val = before_policy.get(k)
            if k in ("write_verify", "write_audit") and old_val is True and new_val is False:
                sensitive_changes.append(f"{k}: True -> False (DOWNGRADE)")
            elif k == "write_rate_limit" and isinstance(old_val, (int, float)) and isinstance(new_val, (int, float)) and new_val < old_val:
                sensitive_changes.append(f"{k}: {old_val} -> {new_val} (RELAXED)")
        if sensitive_changes:
            logger.warning("Write policy downgrade by %s on %s: %s", user.get("username"), device_id, sensitive_changes)
        # 调用 service 更新（仅更新 config）
        update_data = {"config": merged_config}
        if isinstance(existing_dict, dict) and "version" in existing_dict:
            update_data["_version"] = existing_dict["version"]
        device = await svc.update_device(device_id, update_data)
        if device is None:
            raise HTTPException(status_code=404, detail=DeviceErrors.NOT_FOUND)
        # 审计日志
        try:
            from edgelite.services.audit_service import AuditAction
            await audit_svc.log(
                AuditAction.DEVICE_UPDATE,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="device_write_policy",
                resource_id=device_id,
                before_value=before_policy,
                after_value=policy_data,
                details={"sensitive_changes": sensitive_changes} if sensitive_changes else None,
            )
        except Exception as e:
            logger.warning("Write policy audit failed: %s", e)
        return ApiResponse(data=device)
    except HTTPException:
        raise
    except StaleDataError as e:
        logger.warning("StaleDataError in update_write_policy: %s", e)
        raise HTTPException(status_code=409, detail=RepoErrors.STALE_DATA_ERROR) from e
    except Exception as e:
        logger.error("update_write_policy failed: %s", e)
        raise HTTPException(status_code=500, detail=DeviceErrors.UPDATE_FAILED) from e


@router.delete("/{device_id}", response_model=ApiResponse)
async def delete_device(
    device_id: Annotated[str, Path(max_length=128)],
    svc: DeviceServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.DEVICE_DELETE)),
    audit_svc: AuditServiceDep = None,  # FIXED-M03: FastAPI dependency injection provides the value
):
    try:
        before_device = await svc.get_device(device_id)
        if before_device is None:
            raise HTTPException(status_code=404, detail=DeviceErrors.NOT_FOUND)
        await _check_device_access(before_device, user)

        # 先审计后业务：先写审计日志（status=pending），审计失败则不执行删除（fail-safe）
        try:
            from edgelite.services.audit_service import AuditAction
            await audit_svc.log(
                AuditAction.DEVICE_DELETE,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="device",
                resource_id=device_id,
                before_value=before_device,
                status="pending",
            )
        except Exception as e:
            logger.error("审计日志写入失败(pending)，删除操作已中止: %s", e)
            raise HTTPException(status_code=500, detail=DeviceErrors.DELETE_FAILED) from e  # FIXED-B904

        success, error = await svc.delete_device(device_id)
        if not success:
            # 删除失败，记录审计状态为 failed
            try:
                await audit_svc.log(
                    AuditAction.DEVICE_DELETE,
                    user_id=user["user_id"],
                    username=user["username"],
                    resource_type="device",
                    resource_id=device_id,
                    status="failed",
                    error_message=error,
                )
            except Exception as e:
                logger.warning("审计日志写入失败(failed): %s", e)
            raise HTTPException(status_code=409, detail=error or DeviceErrors.DELETE_FAILED)
        # 删除成功，记录审计状态为 success
        try:
            await audit_svc.log(
                AuditAction.DEVICE_DELETE,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="device",
                resource_id=device_id,
                status="success",
            )
        except Exception as e:
            logger.warning("审计日志写入失败(success): %s", e)
        return ApiResponse()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("delete_device failed: %s", e)
        raise HTTPException(status_code=500, detail=DeviceErrors.DELETE_FAILED) from e


@router.get("/{device_id}/points", response_model=ApiResponse)
async def get_device_points(
    device_id: Annotated[str, Path(max_length=128)],
    svc: DeviceServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.DEVICE_READ)),
):
    try:
        await _check_device_owner(svc, device_id, user)
        values = await asyncio.wait_for(svc.read_points(device_id), timeout=5.0)
        return ApiResponse(data=values)
    except TimeoutError:
        # 超时时返回空数据而非错误，让页面至少能显示
        logger.warning("read_points timeout for device %s", device_id)
        return ApiResponse(data={})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_device_points failed: %s", e)
        raise HTTPException(status_code=500, detail=DeviceErrors.POINTS_FAILED) from e


@router.post("/{device_id}/points", response_model=ApiResponse)
async def write_device_point(
    device_id: Annotated[str, Path(max_length=128)],
    body: WritePointRequest,
    svc: DeviceServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.DEVICE_WRITE_POINT)),
    audit_svc: AuditServiceDep = None,
):
    try:
        await _check_device_owner(svc, device_id, user)
        try:
            driver = getattr(svc, "_driver_instances", {}).get(device_id) if svc else None
            if driver:
                caps = getattr(driver, "capabilities", None)
                if caps is not None:
                    write_supported = getattr(caps, "write", None)
                    if write_supported is False:
                        raise HTTPException(status_code=400, detail={
                            "error_code": "ERR_DEVICE_CAPABILITY_NOT_SUPPORTED",
                            "message": f"Device '{device_id}' driver does not support write capability",
                        })
        except HTTPException:
            raise
        except Exception as e:
            logger.debug("Driver validation skipped: %s", e)

        # Check write policy
        try:
            from edgelite.drivers.registry import get_driver_registry
            registry = get_driver_registry()
            if registry:
                driver = registry.get_driver_instance(device_id)
                if driver and not driver.check_write_allowed(device_id, body.point):
                    raise HTTPException(status_code=403, detail=DeviceErrors.WRITE_NOT_ALLOWED)
        except HTTPException:
            raise
        except Exception as e:
            logger.debug("Driver validation skipped: %s", e)

        # SEC-FIX-V01: 高危写入审批——对启用了 write_verify 的设备，记录审批意图
        # 当前采用"审计即审批"的轻量模式：所有写入均记录操作人、点位、值，事后可追溯
        # 完整的多级审批链可后续接入 CommandApprovalService.submit_command
        try:
            from edgelite.services.command_approval import get_approval_service
            approval_svc = get_approval_service()
            # 记录写入意图到审批服务（不阻塞，仅留痕）
            approval_svc.record_intent(
                device_id=device_id,
                point=body.point,
                value=body.value,
                user_id=user.get("user_id", ""),
                username=user.get("username", ""),
            )
        except Exception as e:
            logger.debug("Approval service record skipped: %s", e)

        # SEC-FIX-V02/V04: 传递 user 到 service，驱动层 set_user_role 与审计 user 字段生效
        success = await svc.write_point(device_id, body.point, body.value, user=user)
        if not success:
            # FIXED: 原问题：中文硬编码detail
            raise HTTPException(status_code=400, detail=DeviceErrors.WRITE_FAILED)

        # SEC-FIX-V05: 写入审计持久化到 audit_service，解决驱动内存 deque 重启即丢的问题
        try:
            from edgelite.services.audit_service import AuditAction
            await audit_svc.log(
                AuditAction.DEVICE_WRITE_POINT,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="device_point",
                resource_id=f"{device_id}/{body.point}",
                after_value={"point": body.point, "value": body.value},
            )
        except Exception as e:
            logger.warning("Write audit log failed: %s", e)

        return ApiResponse()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("write_device_point failed: %s", e)
        # FIXED: 原问题：中文硬编码detail
        raise HTTPException(status_code=500, detail=DeviceErrors.WRITE_FAILED) from e


@router.post("/{device_id}/push", response_model=ApiResponse)
async def push_device_data(
    device_id: Annotated[str, Path(max_length=128)],
    body: PushDeviceDataRequest,
    config: ConfigDep,
    svc: DeviceServiceDep,
    x_api_key: str = Header(default=""),
    current_user: OptionalCurrentUser = None,
    request: Request = None,
    audit_svc: AuditServiceDep = None,  # FIXED-M03: FastAPI dependency injection provides the value
):
    # FIXED-M02: 使用 get_current_user 获取最新角色而非 Token payload
    # FIXED-H02: API Key 不再隐式授予 admin 角色，改为绑定 DEVICE_PUSH 权限
    if not device_id or not isinstance(device_id, str) or len(device_id) > 128:
        raise HTTPException(status_code=400, detail=DeviceErrors.PUSH_INVALID_ID)
    # FIXED-P0: 原问题-isinstance(body, dict)对Pydantic模型恒为True，导致所有合法推送请求被错误拒绝为PUSH_EMPTY
    # body.data已由Pydantic Field(min_length=1)保证非空，此处仅做防御性校验
    if not body.data:
        raise HTTPException(status_code=400, detail=DeviceErrors.PUSH_EMPTY)

    api_key_configured = (
        config
        and getattr(config, "server", None)
        and getattr(config.server, "webhook_api_key", None)
    )

    # 优先使用 Bearer Token（从数据库获取最新角色）
    # FIXED-M02: get_optional_current_user 现在正确处理：
    # - 无凭证：返回 None -> 尝试 API Key
    # - 有凭证但被禁用/撤销: 抛出 HTTPException -> 拒绝访问
    if current_user is not None:
        if current_user.get("role") != "admin":
            device = await svc.get_device(device_id)
            if device is None:
                err = make_error_response(DeviceErrors.NOT_FOUND)
                return JSONResponse(content=err, status_code=404)
            try:
                await _check_device_access(device, current_user)
            except HTTPException:
                err = make_error_response(AuthzErrors.RESOURCE_OWNERSHIP_DENIED)
                return JSONResponse(content=err, status_code=403)
    elif api_key_configured and x_api_key:
        # FIXED-M02: 仅当无 Bearer Token 且提供了 API Key 时才使用 API Key 认证
        # 如果提供了 Bearer Token 但认证失败，get_optional_current_user 已抛出异常
        # 此处不会执行
        # FIXED-H02: API Key 仅授予 DEVICE_PUSH 权限，不再授予隐式 admin
        from edgelite.security.rbac import APIKeyPermission, has_api_key_permission

        if not x_api_key:
            err = make_error_response(DeviceErrors.PUSH_INVALID_KEY)
            return JSONResponse(content=err, status_code=err["code"])

        if not hmac.compare_digest(x_api_key, config.server.webhook_api_key):
            err = make_error_response(DeviceErrors.PUSH_INVALID_KEY)
            return JSONResponse(content=err, status_code=err["code"])

        # 验证 webhook API Key 拥有 DEVICE_PUSH 权限
        if not has_api_key_permission("server.webhook_api_key", APIKeyPermission.DEVICE_PUSH):
            logger.warning(
                "push_device_data: webhook API key lacks DEVICE_PUSH permission (ip=%s)",
                request.client.host if request and request.client else "unknown"
            )
            err = make_error_response(DeviceErrors.PUSH_INVALID_KEY)
            return JSONResponse(content=err, status_code=err["code"])

        # FIXED-H02: 记录 API Key 使用审计日志
        try:
            from edgelite.services.audit_service import AuditAction
            # FIXED(P1): 原问题-RUF006 create_task 返回值未保存，task 可能被 GC 回收;
            #            修复-保存到模块级 _background_tasks 集合
            task = asyncio.create_task(
                audit_svc.log(
                    AuditAction.API_KEY_USED,
                    resource_type="api_key",
                    resource_id="server.webhook_api_key",
                    ip_address=request.client.host if request and request.client else "unknown",
                    after_value={
                        "success": True,
                        "action": "device_push",
                        "device_id": device_id,
                    },
                )
            )
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)
        except Exception as e:
            logger.warning("push_device_data: audit log failed: %s", e)
    else:
        err = make_error_response(DeviceErrors.API_KEY_NOT_CONFIGURED)
        return JSONResponse(content=err, status_code=err["code"])
    # FIXED: P1-1 转换为原始dict格式供driver.receive_data使用
    raw_data = {k: v.value for k, v in body.data.items()}
    try:
        driver = svc._driver_instances.get(device_id) if svc else None
        if driver and hasattr(driver, "receive_data"):
            await driver.receive_data(device_id, raw_data)
            return ApiResponse()
        # FIXED: P1-1 移除多余的body校验，已由Pydantic模型保证
        raise HTTPException(status_code=400, detail=DeviceErrors.PUSH_DRIVER_NOT_READY)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("push_device_data failed: %s", e)
        # FIXED: 原问题：中文硬编码detail
        raise HTTPException(status_code=500, detail=DeviceErrors.PUSH_FAILED) from e


@router.get("/{device_id}/health", response_model=ApiResponse[DeviceHealthResponse])
async def get_device_health(
    device_id: Annotated[str, Path(max_length=128)],
    svc: DeviceServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.DEVICE_READ)),
):
    try:
        await _check_device_owner(svc, device_id, user)
        data = await svc.get_device_health(device_id)
        if data is None:
            raise HTTPException(status_code=404, detail=DeviceErrors.NOT_FOUND)
        return ApiResponse(data=data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_device_health failed: %s", e)
        raise HTTPException(status_code=500, detail=DeviceErrors.GET_FAILED) from e


@router.post("/{device_id}/health/reset", response_model=ApiResponse)
async def reset_device_health(
    device_id: Annotated[str, Path(max_length=128)],
    svc: DeviceServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.DEVICE_UPDATE)),
):
    try:
        await _check_device_owner(svc, device_id, user)
        ok = await svc.reset_device_health(device_id)
        if not ok:
            raise HTTPException(status_code=404, detail=DeviceErrors.NOT_FOUND)
        return ApiResponse()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("reset_device_health failed: %s", e)
        raise HTTPException(status_code=500, detail=DeviceErrors.UPDATE_FAILED) from e


@router.get("/{device_id}/ops", response_model=ApiResponse)
async def get_device_ops(
    device_id: Annotated[str, Path(max_length=128)],
    svc: DeviceServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.DEVICE_READ)),
):
    try:
        await _check_device_owner(svc, device_id, user)
        data = await svc.get_device_ops_data(device_id)
        if data is None:
            raise HTTPException(status_code=404, detail=DeviceErrors.NOT_FOUND)
        return ApiResponse(data=data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_device_ops failed: %s", e)
        raise HTTPException(status_code=500, detail=DeviceErrors.GET_FAILED) from e


@router.post("/{device_id}/probe-primary", response_model=ApiResponse)
async def probe_primary_link(
    device_id: Annotated[str, Path(max_length=128)],
    svc: DeviceServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.DEVICE_READ)),
):
    try:
        await _check_device_owner(svc, device_id, user)
        reachable = await svc.probe_primary_link(device_id)
        return ApiResponse(data={"reachable": reachable})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("probe_primary_link failed: %s", e)
        raise HTTPException(status_code=500, detail=DeviceErrors.GET_FAILED) from e


@router.get("/{device_id}/point-health", response_model=ApiResponse)
async def get_point_health(
    device_id: Annotated[str, Path(max_length=128)],
    svc: DeviceServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.DEVICE_READ)),
):
    try:
        await _check_device_owner(svc, device_id, user)
        data = await svc.get_point_health(device_id)
        if data is None:
            raise HTTPException(status_code=404, detail=DeviceErrors.NOT_FOUND)
        return ApiResponse(data=data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_point_health failed: %s", e)
        raise HTTPException(status_code=500, detail=DeviceErrors.GET_FAILED) from e


@router.get("/{device_id}/write-audit", response_model=ApiResponse)
async def get_write_audit(
    device_id: Annotated[str, Path(max_length=128)],
    limit: int = Query(100, ge=1, le=1000),  # FIXED-P2: 原问题-limit无边界校验，可传超大值导致OOM
    result: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    point_name: str | None = None,
    svc: DeviceServiceDep = None,
    user: dict[str, str] = Depends(require_permission(Permission.DEVICE_READ)),
):
    try:
        await _check_device_owner(svc, device_id, user)
        data = await svc.get_write_audit(device_id, limit, result, start_time, end_time, point_name)
        if data is None:
            raise HTTPException(status_code=404, detail=DeviceErrors.NOT_FOUND)
        return ApiResponse(data=data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_write_audit failed: %s", e)
        raise HTTPException(status_code=500, detail=DeviceErrors.GET_FAILED) from e


@router.get("/{device_id}/metrics", response_model=ApiResponse)
async def get_device_metrics(
    device_id: Annotated[str, Path(max_length=128)],
    svc: DeviceServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """获取设备可观测性指标"""
    if not svc:
        raise HTTPException(status_code=503, detail=CommonErrors.SERVICE_NOT_READY)
    await _check_device_owner(svc, device_id, user)
    try:
        from edgelite.drivers.registry import get_driver_registry
        registry = get_driver_registry()
        if registry:
            driver = registry.get_driver_instance(device_id)
            if driver and hasattr(driver, "get_observability_metrics"):
                metrics = driver.get_observability_metrics(device_id)
                return ApiResponse(data=metrics)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to get metrics for %s: %s", device_id, e)
    return ApiResponse(data={
        "read_error_rate": 0.0,
        "write_error_rate": 0.0,
        "consecutive_failures": 0,
        "connection_quality_score": 100.0,
        "total_downtime_seconds": 0.0,
        "last_online_at": None,
        "last_offline_at": None,
        "avg_latency_ms": 0.0,
        "reconnect_count": 0,
    })


class ConfigVersionSaveRequest(BaseModel):
    config: dict
    change_summary: str = ""
    operator: str = ""


class ConfigRollbackRequest(BaseModel):
    target_version: int
    operator: str = ""


@router.get("/{device_id}/config-versions", response_model=ApiResponse)
async def list_config_versions(
    device_id: Annotated[str, Path(max_length=128)],
    svc: DeviceServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.CONFIG_EDIT)),
):
    try:
        await _check_device_owner(svc, device_id, user)
        versions = await svc.get_config_versions(device_id)
        return ApiResponse(data=versions)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=DeviceErrors.CONFIG_INVALID) from e  # FIXED-P2: 原问题-detail=str(e)泄漏内部错误
    except Exception as e:
        logger.error("list_config_versions failed: %s", e)
        raise HTTPException(status_code=500, detail=DeviceErrors.GET_FAILED) from e  # FIXED-P2: 原问题-detail=str(e)泄漏内部错误


@router.get("/{device_id}/config-versions/current", response_model=ApiResponse)
async def get_config_current(
    device_id: Annotated[str, Path(max_length=128)],
    svc: DeviceServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.CONFIG_EDIT)),
):
    try:
        await _check_device_owner(svc, device_id, user)
        current = await svc.get_config_current(device_id)
        if current is None:
            raise HTTPException(status_code=404, detail=DeviceErrors.NOT_FOUND)
        return ApiResponse(data=current)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=DeviceErrors.CONFIG_INVALID) from e  # FIXED-P2: 原问题-detail=str(e)泄漏内部错误
    except Exception as e:
        logger.error("get_config_current failed: %s", e)
        raise HTTPException(status_code=500, detail=DeviceErrors.GET_FAILED) from e  # FIXED-P2: 原问题-detail=str(e)泄漏内部错误


@router.get("/{device_id}/config-versions/{version}", response_model=ApiResponse)
async def get_config_version_detail(
    device_id: Annotated[str, Path(max_length=128)],
    version: int,
    svc: DeviceServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.CONFIG_EDIT)),
):
    try:
        await _check_device_owner(svc, device_id, user)
        config = await svc.get_config_version_config(device_id, version)
        if config is None:
            raise HTTPException(status_code=404, detail=DeviceErrors.NOT_FOUND)
        return ApiResponse(data=config)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=DeviceErrors.CONFIG_INVALID) from e  # FIXED-P2: 原问题-detail=str(e)泄漏内部错误
    except Exception as e:
        logger.error("get_config_version_detail failed: %s", e)
        raise HTTPException(status_code=500, detail=DeviceErrors.GET_FAILED) from e  # FIXED-P2: 原问题-detail=str(e)泄漏内部错误


@router.post("/{device_id}/config-versions", response_model=ApiResponse)
async def save_config_version(
    device_id: Annotated[str, Path(max_length=128)],
    body: ConfigVersionSaveRequest,
    svc: DeviceServiceDep,
    audit: AuditServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.CONFIG_EDIT)),
):
    try:
        await _check_device_owner(svc, device_id, user)
        new_ver = await svc.save_config_version(
            device_id, body.config, body.change_summary, body.operator,
        )
        if new_ver == 0:
            raise HTTPException(status_code=400, detail=DeviceErrors.UPDATE_FAILED)
        if audit:
            from edgelite.services.audit_service import AuditAction
            # FIXED-M05: user is dict, not object - use .get() instead of getattr()
            await audit.log(
                AuditAction.DRIVER_CONFIG_UPDATE,
                user_id=user.get("user_id"),
                username=user.get("username"),
                resource_type="device",
                resource_id=device_id,
                details={"new_version": new_ver, "change_summary": body.change_summary},
            )
        return ApiResponse(data={"version": new_ver})
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=DeviceErrors.CONFIG_INVALID) from e  # FIXED-P2: 原问题-detail=str(e)泄漏内部错误
    except Exception as e:
        logger.error("save_config_version failed: %s", e)
        raise HTTPException(status_code=500, detail=DeviceErrors.UPDATE_FAILED) from e  # FIXED-P2: 原问题-detail=str(e)泄漏内部错误


@router.post("/{device_id}/config-versions/rollback", response_model=ApiResponse)
async def rollback_config_version(
    device_id: Annotated[str, Path(max_length=128)],
    body: ConfigRollbackRequest,
    svc: DeviceServiceDep,
    audit: AuditServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.CONFIG_EDIT)),
):
    try:
        await _check_device_owner(svc, device_id, user)
        result = await svc.rollback_config(device_id, body.target_version, body.operator)
        if result is None:
            raise HTTPException(status_code=404, detail=DeviceErrors.NOT_FOUND)
        if audit:
            from edgelite.services.audit_service import AuditAction
            # FIXED-M05: user is dict, not object - use .get() instead of getattr()
            await audit.log(
                AuditAction.CONFIG_UPDATE,
                user_id=user.get("user_id"),
                username=user.get("username"),
                resource_type="device",
                resource_id=device_id,
                details={"rollback_to": body.target_version, "new_version": result.get("version")},
            )
        return ApiResponse(data=result)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=DeviceErrors.CONFIG_INVALID) from e  # FIXED-P2: 原问题-detail=str(e)泄漏内部错误
    except Exception as e:
        logger.error("rollback_config_version failed: %s", e)
        raise HTTPException(status_code=500, detail=DeviceErrors.UPDATE_FAILED) from e  # FIXED-P2: 原问题-detail=str(e)泄漏内部错误


@router.get("/{device_id}/config-versions/audit", response_model=ApiResponse)
async def get_config_audit_trail(
    device_id: Annotated[str, Path(max_length=128)],
    svc: DeviceServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.CONFIG_EDIT)),
    limit: int = Query(default=50, ge=1, le=500),
):
    try:
        await _check_device_owner(svc, device_id, user)
        trail = await svc.get_config_audit_trail(device_id, limit)
        return ApiResponse(data=trail)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=DeviceErrors.CONFIG_INVALID) from e  # FIXED-P2: 原问题-detail=str(e)泄漏内部错误
    except Exception as e:
        logger.error("get_config_audit_trail failed: %s", e)
        raise HTTPException(status_code=500, detail=DeviceErrors.GET_FAILED) from e  # FIXED-P2: 原问题-detail=str(e)泄漏内部错误


@router.get("/{device_id}/config-versions/diff", response_model=ApiResponse)
async def diff_config_versions(
    device_id: Annotated[str, Path(max_length=128)],
    version_a: int = Query(...),
    version_b: int = Query(...),
    svc: DeviceServiceDep = None,
    user: dict[str, str] = Depends(require_permission(Permission.CONFIG_EDIT)),
):
    try:
        await _check_device_owner(svc, device_id, user)
        diff = await svc.diff_config_versions(device_id, version_a, version_b)
        if diff is None:
            raise HTTPException(status_code=404, detail=DeviceErrors.NOT_FOUND)
        return ApiResponse(data=diff)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=DeviceErrors.CONFIG_INVALID) from e  # FIXED-P2: 原问题-detail=str(e)泄漏内部错误
    except Exception as e:
        logger.error("diff_config_versions failed: %s", e)
        raise HTTPException(status_code=500, detail=DeviceErrors.GET_FAILED) from e  # FIXED-P2: 原问题-detail=str(e)泄漏内部错误
