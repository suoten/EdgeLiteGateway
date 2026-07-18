"""驱动配置管理API路由"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from edgelite.api.deps import (
    DriverRegistryDep,
    DriverRegistryDepOptional,
    PluginManagerDep,
    require_permission,
)
from edgelite.api.error_codes import (
    AuthzErrors,
    CommonErrors,
    DeviceErrors,
    DriverErrors,
)
from edgelite.drivers.base import DriverCapabilities, DriverExceptionMapper
from edgelite.models.common import ApiResponse
from edgelite.models.health import DriverHealthResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/drivers", tags=["Drivers"])


def _driver_supports_method(driver_cls: type, method_name: str) -> bool:
    """Best-effort detection whether a driver supports an optional method.

    We treat abstract methods on base class as supported (implemented by all drivers).
    For optional methods (discover_devices, add_device, health_check...), we check overrides.
    """
    try:
        base = __import__("edgelite.drivers.base", fromlist=["DriverPlugin"]).DriverPlugin
        base_attr = getattr(base, method_name, None)
    except Exception:
        base_attr = None

    cls_attr = getattr(driver_cls, method_name, None)
    if cls_attr is None:
        return False

    if base_attr is None:
        return True

    # If it's exactly the base implementation, treat as "not supported" for optional ones.
    return cls_attr is not base_attr


class DriverInfo(BaseModel):
    name: str
    version: str = "1.0.0"
    protocols: list[str] = []
    description: str = ""


class DriverListResponse(BaseModel):
    drivers: list[DriverInfo]
    total: int


class DriverProtocolsResponse(BaseModel):
    protocols: list[str]


class DriverConfigSchemaResponse(BaseModel):
    driver_name: str
    config_schema: dict


class DriverStatusInfo(BaseModel):
    name: str
    class_: str = Field(alias="class")
    module: str
    custom: bool | None = None
    loaded: bool | None = None
    error: str | None = None
    dependencies: list[str] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class DriverDiscoverResponse(BaseModel):
    devices: list[dict]


@router.get("/list", response_model=ApiResponse[DriverListResponse])
async def list_drivers(
    registry: DriverRegistryDepOptional,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    try:
        if not registry:
            return ApiResponse(data={"drivers": [], "total": 0})

        drivers = []
        for name, driver_cls in registry.items():
            try:
                instance = driver_cls() if isinstance(driver_cls, type) else driver_cls
                drivers.append(
                    {
                        "name": getattr(instance, "plugin_name", name),
                        "version": getattr(instance, "plugin_version", "1.0.0"),
                        "protocols": getattr(instance, "supported_protocols", []),
                        "description": getattr(instance, "__doc__", "") or "",
                    }
                )
            except Exception as e:
                # FIXED(一般): 原问题-str(e)泄漏内部模块路径/依赖信息;
                # 修复-返回通用错误消息，详细错误记录日志
                logger.warning("Driver '%s' instantiation failed: %s", name, e)
                drivers.append({"name": name, "error": "Driver initialization failed"})

        return ApiResponse(data={"drivers": drivers, "total": len(drivers)})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to list drivers: %s", e)  # FIXED: 原问题-中文硬编码日志
        raise HTTPException(
            status_code=500, detail=DriverErrors.LIST_FAILED
        ) from e  # FIXED: 原问题-中文硬编码detail，改为error_code


@router.get("/protocols", response_model=ApiResponse[DriverProtocolsResponse])
async def list_protocols(
    registry: DriverRegistryDepOptional,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    try:
        if not registry:
            return ApiResponse(data={"protocols": []})

        protocols = registry.get_all_protocol_keys()
        return ApiResponse(data={"protocols": protocols})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to list protocols: %s", e)  # FIXED: 原问题-中文硬编码日志
        raise HTTPException(
            status_code=500, detail=DriverErrors.LIST_FAILED
        ) from e  # FIXED: 原问题-中文硬编码detail，改为error_code


@router.get("/{driver_name}/config-schema", response_model=ApiResponse[DriverConfigSchemaResponse])
async def get_driver_config_schema(
    driver_name: str,
    registry: DriverRegistryDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    try:
        if not registry:
            raise HTTPException(
                status_code=501, detail=DriverErrors.REGISTRY_NOT_INIT
            )  # FIXED: 原问题-中文硬编码detail，改为error_code

        # 先用原始名称查找注册表（支持已注册的 kebab-case 别名如 "modbus-tcp-slave"）
        driver_cls = registry.get_driver_class(driver_name)
        if not driver_cls:
            # 原始名称未找到，尝试归一化后查找
            try:
                from edgelite.protocol_keys import normalize_protocol_key

                normalized = normalize_protocol_key(driver_name) or driver_name
                if normalized != driver_name:
                    driver_cls = registry.get_driver_class(normalized)
            except Exception as e:
                logger.warning("协议键名归一化失败: %s", e)
        if not driver_cls:
            raise HTTPException(
                status_code=404, detail=DriverErrors.NOT_FOUND
            )  # FIXED: 原问题-中文硬编码detail，改为error_code

        schema = getattr(driver_cls, "config_schema", None)
        if not schema:
            schema = {
                "fields": [
                    {"name": "host", "type": "string", "label": "Host", "default": "localhost", "required": True},
                    {"name": "port", "type": "integer", "label": "Port", "default": 0},
                ]
            }

        return ApiResponse(data={"driver_name": driver_name, "config_schema": schema})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to get driver config schema: %s", e)  # FIXED: 原问题-中文硬编码日志
        raise HTTPException(
            status_code=500, detail=DriverErrors.GET_FAILED
        ) from e  # FIXED: 原问题-中文硬编码detail，改为error_code


@router.get("", response_model=ApiResponse[list[DriverStatusInfo]])
async def list_all_drivers(
    registry: DriverRegistryDep,
    plugin_manager: PluginManagerDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    try:
        drivers_info = []
        if registry:
            for name, cls in registry.items():
                drivers_info.append(
                    {
                        "name": name,
                        "class": cls.__name__,
                        "module": cls.__module__,
                    }
                )
        if plugin_manager:
            for info in plugin_manager.list_plugins():
                drivers_info.append(
                    {
                        "name": info.name,
                        "class": info.class_name,
                        "module": info.module_path,
                        "custom": info.is_custom,
                        "loaded": info.is_loaded,
                        "error": info.error,
                    }
                )
        return ApiResponse(data=drivers_info)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to list all drivers: %s", e)  # FIXED: 原问题-中文硬编码日志
        raise HTTPException(
            status_code=500, detail=DriverErrors.LIST_FAILED
        ) from e  # FIXED: 原问题-中文硬编码detail，改为error_code


class DriverDiscoverRequest(BaseModel):
    config: dict = {}


_DISCOVER_TIMEOUT = 60.0


@router.post("/{driver_name}/discover", response_model=ApiResponse[DriverDiscoverResponse])
async def discover_devices(
    driver_name: str,
    registry: DriverRegistryDep,
    req: DriverDiscoverRequest | None = None,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    # Accept frontend protocol ids (kebab-case) and common aliases.
    try:
        from edgelite.protocol_keys import normalize_protocol_key

        driver_name = normalize_protocol_key(driver_name) or driver_name
    except Exception as e:
        # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
        logger.warning("协议键名归一化失败(discover): %s", e)
    if not registry:
        raise HTTPException(status_code=501, detail=DriverErrors.REGISTRY_NOT_INIT)

    driver_cls = registry.get_driver_class(driver_name)
    if not driver_cls:
        raise HTTPException(status_code=404, detail=DriverErrors.NOT_FOUND)

    try:
        driver = driver_cls()
        driver_config = req.config if req else {}
        await driver.start(driver_config)
    except Exception as e:
        logger.error("Driver %s start failed for discover: %s", driver_name, e)
        raise HTTPException(
            status_code=503,
            detail={
                "message": DriverErrors.START_FAILED,
                "hint": f"Driver {driver_name} failed to start, check configuration and connectivity",
            },
        ) from e  # FIXED-P2: 移除detail=str(e)返回前端，防止泄露内部IP/端口/协议错误码

    try:
        devices = await asyncio.wait_for(
            driver.discover_devices(driver_config),
            timeout=_DISCOVER_TIMEOUT,
        )
    except TimeoutError:
        raise HTTPException(
            status_code=504,
            detail={
                "message": DriverErrors.DISCOVER_FAILED,
                "hint": f"Discovery timed out ({_DISCOVER_TIMEOUT}s), target may be unreachable or no devices responded",  # noqa: E501
            },
        ) from None  # FIXED: 原问题-驱动扫描无超时保护，前端15s超时后看到网络错误而非业务提示
    except NotImplementedError:
        raise HTTPException(
            status_code=501,
            detail={
                "message": DriverErrors.DISCOVER_FAILED,
                "hint": f"Driver {driver_name} does not support device discovery",
            },
        ) from None  # FIXED: 原问题-驱动不支持discover时抛500，改为501+明确提示
    except ConnectionRefusedError as e:
        raise HTTPException(
            status_code=503,
            detail={
                "message": DriverErrors.DISCOVER_FAILED,
                "hint": f"Connection refused, target {driver_name} service may not be running",
            },
        ) from e  # FIXED: 原问题-连接被拒时返回500，改为503+友好提示
    except OSError as e:
        raise HTTPException(
            status_code=503,
            detail={
                "message": DriverErrors.DISCOVER_FAILED,
                "hint": "Network error, check target address and connectivity",
            },  # FIXED-P1: 不暴露异常详情
        ) from e  # FIXED: 原问题-网络错误返回500，改为503+友好提示
    except Exception as e:
        error_code = DriverExceptionMapper.map_exception(e, driver_name)
        err_msg = str(e).lower()
        if "timeout" in err_msg or "timed out" in err_msg:
            raise HTTPException(
                status_code=504,
                detail={
                    "message": DriverErrors.DISCOVER_FAILED,
                    "error_code": error_code,
                    "hint": "Discovery timed out, target may be unreachable",
                },
            ) from e
        if "refused" in err_msg or "connection" in err_msg:
            raise HTTPException(
                status_code=503,
                detail={
                    "message": DriverErrors.DISCOVER_FAILED,
                    "error_code": error_code,
                    "hint": f"Cannot connect to target, check if {driver_name} service is running and reachable",
                },
            ) from e
        raise HTTPException(
            status_code=500,
            detail={"message": DriverErrors.DISCOVER_FAILED, "error_code": error_code},
        ) from e  # FIXED-P2: 移除detail=str(e)返回前端，防止泄露内部信息
    finally:
        try:
            await driver.stop()
        except Exception as e:
            logger.warning("Failed to stop driver: %s", e)

    return ApiResponse(data={"devices": devices})


@router.get("/load-status", response_model=ApiResponse[dict])
async def get_driver_load_status(
    registry: DriverRegistryDepOptional,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """获取所有驱动的加载状态（已加载/因依赖缺失跳过）"""
    try:
        if not registry:
            return ApiResponse(data={"drivers": {}, "loaded_count": 0, "skipped_count": 0})

        status = registry.get_load_status()
        loaded_count = sum(1 for v in status.values() if v.get("loaded"))
        skipped_count = sum(1 for v in status.values() if not v.get("loaded"))

        # Include dependency check results
        dep_results = {}
        try:
            dep_results = registry.get_dependency_results()
        except Exception:
            dep_results = {}

        # Merge dependency results into load status
        for _label, info in status.items():
            # FIXED-P2: 原问题-info.get("class", "")结果未使用，为死代码，已移除
            for _plugin_name, dep_info in dep_results.items():
                if not dep_info.get("available", True):
                    info["dependency_check"] = dep_info

        return ApiResponse(
            data={
                "drivers": status,
                "loaded_count": loaded_count,
                "skipped_count": skipped_count,
                "dependency_results": dep_results,
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to get driver load status: %s", e)
        raise HTTPException(status_code=500, detail=DriverErrors.GET_FAILED) from e


@router.get("/meta", response_model=ApiResponse)
async def list_driver_meta(
    registry: DriverRegistryDepOptional,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """List driver capabilities/constraints for UI and self-test gating."""
    from edgelite.drivers.registry import DRIVER_DISPLAY_NAMES

    if not registry:
        return ApiResponse(data={"drivers": [], "total": 0})

    drivers: list[dict] = []

    # Map label -> load status info for dependency diagnostics.
    load_status: dict = {}
    try:
        load_status = registry.get_load_status() if hasattr(registry, "get_load_status") else {}
    except Exception:
        load_status = {}

    for protocol, driver_cls in registry.items():
        plugin_name = getattr(driver_cls, "plugin_name", protocol)
        display = DRIVER_DISPLAY_NAMES.get(plugin_name, DRIVER_DISPLAY_NAMES.get(protocol, {}))

        # Get capabilities from DriverCapabilities dataclass or dict
        declared_caps = getattr(driver_cls, "capabilities", None)
        if isinstance(declared_caps, DriverCapabilities):
            caps = {
                "discover": declared_caps.discover,
                "read": declared_caps.read,
                "write": declared_caps.write,
                "subscribe": declared_caps.subscribe,
                "batch_read": declared_caps.batch_read,
                "batch_write": declared_caps.batch_write,
            }
        elif isinstance(declared_caps, dict):
            caps = {
                "read": True,
                "write": True,
                "discover": _driver_supports_method(driver_cls, "discover_devices"),
                "subscribe": _driver_supports_method(driver_cls, "on_data"),
                "browse": _driver_supports_method(driver_cls, "browse"),
                "server": bool(plugin_name.endswith("_server")) or bool(declared_caps.get("server")),
            }
            for k, v in declared_caps.items():
                if isinstance(v, bool):
                    caps[k] = v
        else:
            # Fallback: detect from method overrides
            caps = {
                "read": True,
                "write": _driver_supports_method(driver_cls, "write_point"),
                "discover": _driver_supports_method(driver_cls, "discover_devices"),
                "subscribe": _driver_supports_method(driver_cls, "on_data"),
                "browse": _driver_supports_method(driver_cls, "browse"),
                "batch_read": _driver_supports_method(driver_cls, "write_points_batch"),
                "batch_write": _driver_supports_method(driver_cls, "write_points_batch"),
            }

        constraints = getattr(driver_cls, "constraints", None)
        if not isinstance(constraints, list):
            constraints = []

        # Best-effort dependency diagnostics: try match by label/module
        dep_info = None
        try:
            dep_info = load_status.get(display.get("en", ""))
        except Exception:
            dep_info = None

        drivers.append(
            {
                "name": getattr(driver_cls, "plugin_name", protocol),
                "version": getattr(driver_cls, "plugin_version", "1.0.0"),
                "protocol": protocol,
                "plugin_name": plugin_name,
                "plugin_version": getattr(driver_cls, "plugin_version", None),
                "display_name_en": display.get("en"),
                "display_name_zh": display.get("zh"),
                "experimental": bool(getattr(driver_cls, "experimental", False)),
                "capabilities": caps,
                "constraints": constraints,
                "config_schema": getattr(driver_cls, "config_schema", None),
                "load_status": dep_info,
                "dependency": dep_info,
            }
        )

    return ApiResponse(data={"drivers": drivers, "total": len(drivers)})


@router.get("/{driver_name}/environment-check", response_model=ApiResponse)
async def driver_environment_check(
    driver_name: str,
    registry: DriverRegistryDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """Check if the runtime environment meets driver requirements"""
    try:
        from edgelite.protocol_keys import normalize_protocol_key

        driver_name = normalize_protocol_key(driver_name) or driver_name
    except Exception as e:
        # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
        logger.warning("协议键名归一化失败(environment-check): %s", e)

    if not registry:
        raise HTTPException(status_code=501, detail=DriverErrors.REGISTRY_NOT_INIT)

    driver_cls = registry.get_driver_class(driver_name)
    if not driver_cls:
        raise HTTPException(status_code=404, detail=DriverErrors.NOT_FOUND)

    try:
        instance = driver_cls()
        if hasattr(instance, "environment_check"):
            result = instance.environment_check()
            return ApiResponse(data=result)
        return ApiResponse(
            data={
                "protocol": driver_name,
                "ready": True,
                "issues": [],
                "mode": "standard",
            }
        )
    except Exception as e:
        logger.error("Environment check failed for %s: %s", driver_name, e)
        raise HTTPException(
            status_code=500, detail=DriverErrors.SELF_TEST_FAILED
        ) from e  # FIXED-P0: 异常详情不返回前端


class OpcUaBrowseRequest(BaseModel):
    device_id: str
    node_id: str | None = None
    max_depth: int = 1


@router.post("/opcua/browse", response_model=ApiResponse[list[dict]])
async def browse_opcua_nodes(
    req: OpcUaBrowseRequest,
    registry: DriverRegistryDep,
    user: dict[str, str] = Depends(require_permission(Permission.DRIVER_READ)),
):
    """浏览OPC UA服务器节点树"""
    try:
        from edgelite.protocol_keys import normalize_protocol_key

        driver_name = normalize_protocol_key("opcua") or "opcua"
    except Exception:
        driver_name = "opcua"

    if not registry:
        raise HTTPException(status_code=501, detail=DriverErrors.REGISTRY_NOT_INIT)

    driver_cls = registry.get_driver_class(driver_name)
    if not driver_cls:
        raise HTTPException(status_code=404, detail=DriverErrors.NOT_FOUND)

    try:
        # 获取已运行的驱动实例（通过device_service）
        from edgelite.app import _app_state

        device_svc = getattr(_app_state, "device_service", None)
        if not device_svc:
            raise HTTPException(status_code=503, detail=CommonErrors.SERVICE_NOT_READY)

        driver = getattr(device_svc, "_drivers", {}).get(req.device_id)
        if not driver or not isinstance(driver, driver_cls):
            raise HTTPException(status_code=404, detail=DeviceErrors.NOT_FOUND)

        # FIXED-P4: 非admin用户校验设备所有权，防止越权浏览他人设备节点树
        if user["role"] != "admin":
            device = await device_svc.get_device(req.device_id)
            if not device or (device.get("created_by") and device.get("created_by") != user["user_id"]):
                # FIXED-P2: 补充共享访问检查，与devices.py保持一致
                from edgelite.app import _app_state
                from edgelite.storage.sqlite_repo import ResourceShareRepo

                share_repo = ResourceShareRepo(_app_state.database, _app_state.database.write_lock)
                has_access = await share_repo.check_user_has_access("device", req.device_id, user["user_id"])
                if not has_access:
                    raise HTTPException(status_code=403, detail=AuthzErrors.RESOURCE_OWNERSHIP_DENIED)

        nodes = await driver.browse(req.device_id, req.node_id, req.max_depth)
        return ApiResponse(data=nodes)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("OPC UA browse failed: %s", e)
        raise HTTPException(status_code=500, detail=DriverErrors.GET_FAILED) from e  # FIXED-P0: 异常详情不返回前端


@router.get("/opcua/certificate-status", response_model=ApiResponse)
async def get_opcua_certificate_status(
    user: dict[str, str] = Depends(require_permission(Permission.DRIVER_READ)),
):
    """Query OPC UA certificate status for all devices"""
    try:
        from edgelite.drivers.opcua import OpcUaDriver

        # FIXED: get_certificate_status is an instance method; use __new__ to avoid
        # calling with self=None. The method only reads _certificate_status which
        # defaults to {} in __init__, so a bare instance is safe for this read-only query.
        _instance = OpcUaDriver.__new__(OpcUaDriver)
        _instance._certificate_status = {}
        status = _instance.get_certificate_status()
        return ApiResponse(data={"certificates": status, "device_count": len(status)})
    except Exception as e:
        logger.error("Failed to get OPC UA certificate status: %s", e)
        raise HTTPException(status_code=500, detail=DriverErrors.GET_FAILED) from e


@router.get("/opc-da/servers", response_model=ApiResponse)
async def list_opc_da_servers(
    host: str = Query(default="localhost", max_length=256),
    plugin_manager: PluginManagerDep = None,
    current_user: dict[str, str] = Depends(require_permission(Permission.DRIVER_READ)),
) -> ApiResponse:
    """列出指定主机上的OPC DA服务器

    Args:
        host: 目标主机名或IP，默认localhost
    """
    # FIXED-P1: 原问题-host参数无长度限制和格式校验，可能被用于SSRF或注入
    if not host or len(host) > 255 or any(c in host for c in (" ", ";", "|", "&", "$", "`")):
        raise HTTPException(status_code=400, detail=DriverErrors.GET_FAILED)
    # FIXED(一般): 原问题-host参数缺少SSRF防护, 可被用于探测内网/云元数据服务;
    # 修复-复用 system.py 的 _is_cascade_parent_host_safe 校验(拦截loopback/link_local/
    # unspecified/multicast/reserved等危险地址, 域名先解析再校验)
    from edgelite.api.system import _is_cascade_parent_host_safe

    if not _is_cascade_parent_host_safe(host):
        raise HTTPException(status_code=400, detail=DriverErrors.GET_FAILED)
    try:
        # 获取OPC DA驱动实例
        driver = plugin_manager.get_driver("opc_da")
        if driver is None:
            raise HTTPException(status_code=404, detail=DriverErrors.NOT_FOUND)

        servers = await driver.list_servers(host)
        return ApiResponse(data={"servers": servers, "host": host})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("OPC DA server enumeration failed: %s", e)
        raise HTTPException(status_code=500, detail=DriverErrors.GET_FAILED) from e  # FIXED-P0: 异常详情不返回前端


@router.get("/health", response_model=ApiResponse)
async def get_all_drivers_health(
    plugin_manager: PluginManagerDep = None,
    current_user: dict[str, str] = Depends(require_permission(Permission.DRIVER_READ)),
) -> ApiResponse:
    try:
        from edgelite.models.health import DeviceHealthResponse

        result = []
        if plugin_manager:
            for driver_name, driver in getattr(plugin_manager, "_drivers", {}).items():
                all_stats = driver.get_all_health_stats()
                devices = []
                healthy = degraded = offline = 0
                score_sum = 0.0
                for dev_id, stats in all_stats.items():
                    # FIXED-P4: 非admin用户仅返回自己创建的设备健康信息
                    if current_user["role"] != "admin":
                        from edgelite.app import _app_state
                        from edgelite.storage.sqlite_repo import ResourceShareRepo

                        device_svc = getattr(_app_state, "device_service", None)
                        if device_svc:
                            dev = await device_svc.get_device(dev_id)
                            if dev and dev.get("created_by") and dev.get("created_by") != current_user["user_id"]:
                                # FIXED-P2: 补充共享访问检查，共享设备也可见
                                share_repo = ResourceShareRepo(_app_state.database, _app_state.database.write_lock)
                                has_access = await share_repo.check_user_has_access(
                                    "device", dev_id, current_user["user_id"]
                                )
                                if not has_access:
                                    continue
                    is_connected = driver.is_device_connected(dev_id)
                    dev_resp = DeviceHealthResponse(
                        device_id=dev_id,
                        is_connected=is_connected,
                        connection_quality_score=int(stats.connection_quality_score),
                        consecutive_failures=stats.consecutive_failures,
                        total_reads=stats.total_reads,
                        failed_reads=stats.failed_reads,
                        total_writes=stats.total_writes,
                        failed_writes=stats.failed_writes,
                        last_success_read=stats.last_success_read.isoformat() if stats.last_success_read else None,
                        last_failed_read=stats.last_failed_read.isoformat() if stats.last_failed_read else None,
                        last_offline_at=stats.last_offline_at.isoformat() if stats.last_offline_at else None,
                        total_downtime_seconds=stats.total_downtime_seconds,
                        avg_latency_ms=stats.avg_latency_ms,
                        p95_latency_ms=stats.p95_latency_ms,
                        health_score=stats.health_score,
                        total_reconnects=stats.total_reconnects,
                        effective_state=stats.effective_state,
                        read_error_rate=stats.read_error_rate,
                        degradation_reason=stats.degradation_reason,
                    )
                    devices.append(dev_resp)
                    score_sum += stats.health_score
                    state = stats.effective_state
                    if state == "connected":
                        healthy += 1
                    elif state == "degraded":
                        degraded += 1
                    else:
                        offline += 1
                avg_score = score_sum / len(all_stats) if all_stats else 0.0
                result.append(
                    DriverHealthResponse(
                        driver_name=driver_name,
                        device_count=len(all_stats),
                        healthy_count=healthy,
                        degraded_count=degraded,
                        offline_count=offline,
                        avg_health_score=avg_score,
                        devices=devices,
                    )
                )
        return ApiResponse(data=result)
    except Exception as e:
        logger.error("get_all_drivers_health failed: %s", e)
        raise HTTPException(status_code=500, detail=DriverErrors.GET_FAILED) from e  # FIXED-P0: 异常详情不返回前端
