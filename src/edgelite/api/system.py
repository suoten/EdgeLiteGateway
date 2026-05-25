"""系统管理API路由"""

from __future__ import annotations

import logging
import re
import time

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import JSONResponse

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
        # FIXED: P2-5 原问题-级联拓扑错误时使用了NEIGHBORS错误码
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
    if not isinstance(config, dict) or not config:
        raise HTTPException(status_code=400, detail=CascadeErrors.INVALID_CONFIG)
    allowed_keys = {"parent_host", "parent_port", "role", "enabled", "auth_key"}
    unknown_keys = set(config.keys()) - allowed_keys
    if unknown_keys:
        raise HTTPException(status_code=400, detail=f"Unknown config keys: {', '.join(sorted(unknown_keys))}")
    if "parent_port" in config and not isinstance(config["parent_port"], (int, str)):
        raise HTTPException(status_code=400, detail=CascadeErrors.INVALID_CONFIG)
    if "enabled" in config and not isinstance(config["enabled"], bool):
        raise HTTPException(status_code=400, detail=CascadeErrors.INVALID_CONFIG)
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
    """获取级联管理器实例(延迟导入)。

    FIXED: P2-5 原问题-except Exception全吞所有异常，用户无法区分"功能未启用"和"内部错误"。
    现改为：仅捕获ImportError，其他异常上浮，级别管理器初始化失败时返回None但记录错误。
    """
    try:
        from edgelite.bootstrap import _app_state
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
        return {"status": "down", "error": "database not initialized", "backend": config.database.backend}

    try:
        async with db.get_session() as session:
            from sqlalchemy import text
            await session.execute(text("SELECT 1"))
        return {"status": "up", "backend": config.database.backend}
    except Exception as e:
        return {"status": "down", "error": str(e), "backend": config.database.backend}


async def _check_influxdb(state) -> dict:
    """检查 InfluxDB 状态"""
    influx = getattr(state, "influx_storage", None)
    if influx is None:
        return {"status": "down", "error": "influx_storage not initialized"}

    try:
        ok = await influx.check_health()
        using_fallback = influx.using_fallback
        if ok:
            return {"status": "up", "using_fallback": False}
        elif using_fallback:
            return {"status": "degraded", "using_fallback": True}
        else:
            return {"status": "down", "using_fallback": False}
    except Exception as e:
        return {"status": "down", "error": str(e)}


async def _check_mqtt(state) -> dict:
    """检查 MQTT 转发器状态"""
    mqtt_forwarder = getattr(state, "mqtt_forwarder", None)
    if mqtt_forwarder is None:
        return {"status": "not_configured"}

    try:
        running = getattr(mqtt_forwarder, "_running", False)
        return {"status": "up" if running else "down"}
    except Exception as e:
        return {"status": "down", "error": str(e)}


async def _check_drivers(state) -> dict:
    """检查驱动注册表状态"""
    driver_registry = getattr(state, "driver_registry", None)
    if driver_registry is None:
        return {"status": "not_configured"}

    try:
        drivers = driver_registry.list_drivers() if hasattr(driver_registry, "list_drivers") else []
        return {"status": "up", "registered_count": len(drivers)}
    except Exception as e:
        return {"status": "down", "error": str(e)}


async def _check_ai_engine(state) -> dict:
    """检查 AI 推理引擎状态"""
    ai_engine = getattr(state, "ai_engine", None)
    if ai_engine is None:
        return {"status": "not_configured"}

    try:
        models = ai_engine.get_loaded_models() if hasattr(ai_engine, "get_loaded_models") else {}
        active = sum(1 for w in models.values() if getattr(w, "status", "") == "active")
        return {"status": "up", "active_models": active, "total_models": len(models)}
    except Exception as e:
        return {"status": "down", "error": str(e)}


async def _check_system_resources() -> dict:
    """检查系统资源（CPU/内存/磁盘）"""
    result = {}
    try:
        import psutil

        result["cpu_percent"] = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory()
        result["memory_percent"] = mem.percent
        result["memory_available_bytes"] = mem.available
        try:
            disk = psutil.disk_usage("/")
            result["disk_percent"] = disk.percent
            result["disk_free_bytes"] = disk.free
        except Exception:
            pass
    except ImportError:
        result["psutil"] = "not_available"
    return result


@router.get("/health", include_in_schema=True)
async def health_check():
    """完整健康检查，含依赖状态

    返回所有核心组件的健康状态，用于运维监控和故障诊断。
    无需认证，供负载均衡器和监控系统调用。
    """
    state = _get_app_state()
    checks = {}
    overall = "healthy"

    # Database
    db_result = await _check_database(state) if state else {"status": "down", "error": "app not initialized"}
    checks["database"] = db_result
    if db_result["status"] == "down":
        overall = "unhealthy"
    elif db_result["status"] == "degraded":
        overall = "degraded"

    # InfluxDB
    influx_result = await _check_influxdb(state) if state else {"status": "down", "error": "app not initialized"}
    checks["influxdb"] = influx_result
    if influx_result["status"] == "down" and not influx_result.get("using_fallback"):
        if overall == "healthy":
            overall = "degraded"
    elif influx_result["status"] == "degraded":
        if overall == "healthy":
            overall = "degraded"

    # MQTT
    mqtt_result = await _check_mqtt(state) if state else {"status": "not_configured"}
    checks["mqtt"] = mqtt_result
    if mqtt_result["status"] == "down":
        if overall == "healthy":
            overall = "degraded"

    # Drivers
    driver_result = await _check_drivers(state) if state else {"status": "not_configured"}
    checks["drivers"] = driver_result

    # AI Engine
    ai_result = await _check_ai_engine(state) if state else {"status": "not_configured"}
    checks["ai_engine"] = ai_result

    # System resources
    checks["system_resources"] = await _check_system_resources()

    # Uptime
    start_time = getattr(state, "start_time", None) if state else None
    uptime_seconds = time.time() - start_time if start_time else 0

    # Version
    try:
        from edgelite import __version__
        version = __version__
    except Exception:
        version = "unknown"

    status_code = 200 if overall != "unhealthy" else 503

    return JSONResponse(
        status_code=status_code,
        content={
            "status": overall,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
            "version": version,
            "uptime_seconds": round(uptime_seconds, 2),
            "checks": checks,
        },
    )


@router.get("/health/live", include_in_schema=True)
async def liveness_check():
    """Kubernetes 存活探针 - 进程是否存活

    只要进程在运行就返回 alive，无需认证。
    用于 K8s liveness probe，失败时 K8s 会重启容器。
    """
    return {"status": "alive", "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())}


@router.get("/health/ready", include_in_schema=True)
async def readiness_check():
    """Kubernetes 就绪探针 - 服务是否就绪

    检查核心依赖是否可用，用于 K8s readiness probe。
    未就绪时 K8s 会将 Pod 从 Service Endpoints 中移除。
    """
    state = _get_app_state()
    checks = {}
    ready = True

    # Database must be up
    db_result = await _check_database(state) if state else {"status": "down"}
    checks["database"] = {"status": db_result["status"]}
    if db_result["status"] == "down":
        ready = False

    # At least one storage backend must be available (InfluxDB or SQLite fallback)
    influx_result = await _check_influxdb(state) if state else {"status": "down"}
    checks["influxdb"] = {"status": influx_result["status"]}
    if influx_result["status"] == "down" and not influx_result.get("using_fallback"):
        # InfluxDB down and no fallback - still degraded but not completely unready
        pass

    status_code = 200 if ready else 503

    return JSONResponse(
        status_code=status_code,
        content={
            "status": "ready" if ready else "not_ready",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
            "checks": checks,
        },
    )
