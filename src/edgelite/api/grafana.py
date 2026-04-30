"""Grafana集成API路由"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from edgelite.models.common import ApiResponse
from edgelite.api.deps import CurrentUser, require_permission
from edgelite.security.rbac import Permission

router = APIRouter(prefix="/api/v1/grafana", tags=["Grafana集成"])


def _get_grafana_config():
    try:
        from edgelite.config import get_config
        config = get_config()
        return getattr(config, "grafana", None)
    except Exception:
        return None


@router.get("/config", response_model=ApiResponse)
async def get_grafana_config(
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    grafana_config = _get_grafana_config()
    if not grafana_config:
        return ApiResponse(data={
            "enabled": False,
            "url": "",
            "datasource": "",
        })
    return ApiResponse(data={
        "enabled": getattr(grafana_config, "enabled", False),
        "url": getattr(grafana_config, "url", "http://localhost:3000"),
        "datasource": getattr(grafana_config, "datasource", "InfluxDB"),
    })


@router.get("/dashboards", response_model=ApiResponse)
async def list_grafana_dashboards(
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    grafana_config = _get_grafana_config()
    if not grafana_config or not getattr(grafana_config, "enabled", False):
        return ApiResponse(data={"dashboards": [], "message": "Grafana集成未启用"})

    grafana_url = getattr(grafana_config, "url", "http://localhost:3000")
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
            return ApiResponse(data={"dashboards": [], "message": f"Grafana返回状态码: {resp.status_code}"})
    except ImportError:
        return ApiResponse(data={"dashboards": [], "message": "httpx未安装"})
    except Exception as e:
        return ApiResponse(data={"dashboards": [], "message": f"Grafana连接失败: {str(e)}"})


@router.get("/embed-url", response_model=ApiResponse)
async def get_grafana_embed_url(
    dashboard_uid: str = "",
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    grafana_config = _get_grafana_config()
    if not grafana_config or not getattr(grafana_config, "enabled", False):
        raise HTTPException(status_code=503, detail="Grafana集成未启用")

    grafana_url = getattr(grafana_config, "url", "http://localhost:3000")
    if dashboard_uid:
        embed_url = f"{grafana_url}/d/{dashboard_uid}?kiosk&theme=light"
    else:
        embed_url = f"{grafana_url}/?kiosk&theme=light"
    return ApiResponse(data={"url": embed_url})
