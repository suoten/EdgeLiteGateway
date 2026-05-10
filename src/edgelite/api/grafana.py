"""Grafana集成API路由"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, HTTPException

from edgelite.api.deps import CurrentUser, require_permission
from edgelite.models.common import ApiResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/grafana", tags=["Grafana集成"])


def _get_grafana_config():
    try:
        from edgelite.config import get_config

        config = get_config()
        return getattr(config, "grafana", None)
    except Exception as e:
        logger.warning("获取Grafana配置失败: %s", e)
        return None


@router.get("/config", response_model=ApiResponse)
async def get_grafana_config(
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    from edgelite.services.service_manager import get_service_manager

    try:
        mgr = get_service_manager()
        info = mgr.get_service_info("grafana")

        grafana_config = _get_grafana_config()
        return ApiResponse(
            data={
                "enabled": info.state.value != "disabled",
                "state": info.state.value,
                "url": getattr(grafana_config, "url", "http://localhost:3001")
                if grafana_config
                else "http://localhost:3001",
                "datasource": getattr(grafana_config, "datasource", "InfluxDB")
                if grafana_config
                else "InfluxDB",
                "dependencies": [
                    {"package": d.package, "installed": d.installed, "version": d.version}
                    for d in info.dependencies
                ],
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("获取失败: %s", e)
        raise HTTPException(status_code=500, detail="获取失败") from e


@router.get("/dashboards", response_model=ApiResponse)
async def list_grafana_dashboards(
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    grafana_config = _get_grafana_config()
    if not grafana_config or not getattr(grafana_config, "enabled", False):
        raise HTTPException(status_code=503, detail="Grafana集成未启用")

    grafana_url = getattr(grafana_config, "url", "http://localhost:3001")
    api_key = getattr(grafana_config, "api_key", "")

    try:
        import httpx

        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{grafana_url}/api/search", headers=headers)
            if resp.status_code == 200:
                dashboards = resp.json()
                return ApiResponse(data={"dashboards": dashboards})
            raise HTTPException(status_code=502, detail=f"Grafana返回状态码: {resp.status_code}")
    except ImportError:
        raise HTTPException(status_code=503, detail="httpx未安装，无法连接Grafana") from None
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Grafana连接失败: {str(e)}") from e


@router.get("/embed-url", response_model=ApiResponse)
async def get_grafana_embed_url(
    dashboard_uid: str = "",
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    grafana_config = _get_grafana_config()
    try:
        if not grafana_config or not getattr(grafana_config, "enabled", False):
            raise HTTPException(status_code=503, detail="Grafana集成未启用")

        grafana_url = getattr(grafana_config, "url", "http://localhost:3001")
        if dashboard_uid:
            if not re.match(r"^[a-zA-Z0-9_-]+$", dashboard_uid):
                raise HTTPException(status_code=400, detail="dashboard_uid 包含非法字符")
            embed_url = f"{grafana_url}/d/{dashboard_uid}?kiosk&theme=light"
        else:
            embed_url = f"{grafana_url}/?kiosk&theme=light"
        return ApiResponse(data={"url": embed_url})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("获取失败: %s", e)
        raise HTTPException(status_code=500, detail="获取失败") from e
