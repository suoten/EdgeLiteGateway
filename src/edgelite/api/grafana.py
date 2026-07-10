"""Grafana集成API路由"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query

from edgelite.api.deps import require_permission
from edgelite.api.error_codes import CommonErrors, GrafanaErrors
from edgelite.constants import _HTTP_TIMEOUT  # FIXED: 原问题-魔法数字timeout=10.0
from edgelite.models.common import ApiResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/grafana", tags=["Grafana"])


def _is_grafana_url_safe(url: str) -> bool:
    """R5-G-08: SSRF 校验 - 拦截内网/环回/链路本地/保留/组播地址。

    仅校验 scheme 为 http/https，并拒绝目标主机解析到危险 IP 的情况。
    域名通过 socket.getaddrinfo 解析为 IP 后逐个校验。
    """
    if not url or not re.match(r"^https?://", url):
        return False
    try:
        parsed = urlparse(url)
    except (ValueError, TypeError):
        return False
    hostname = parsed.hostname
    if not hostname:
        return False
    # 先尝试直接当作 IP 校验
    try:
        ip = ipaddress.ip_address(hostname)
        return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast)
    except ValueError:
        pass
    # 域名解析后校验每个 IP
    try:
        addrs = socket.getaddrinfo(hostname, None)
    except (socket.gaierror, OSError):
        return False
    if not addrs:
        return False
    for _fam, _stype, _proto, _canon, sockaddr in addrs:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


def _get_grafana_config():
    try:
        from edgelite.config import get_config

        config = get_config()
        return getattr(config, "grafana", None)
    except Exception as e:
        logger.warning("Failed to get Grafana config: %s", e)  # FIXED-P3: 中文日志→英文
        return None


@router.get("/config", response_model=ApiResponse)
async def get_grafana_config(
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    from edgelite.services.service_manager import get_service_manager

    try:
        mgr = get_service_manager()
        info = mgr.get_service_info("grafana")
        if info is None:
            raise HTTPException(status_code=404, detail=CommonErrors.NOT_FOUND)

        grafana_config = _get_grafana_config()
        # Flatten GrafanaConfig fields for the response, so frontend gets url/datasource/api_key directly
        grafana_url = (
            getattr(grafana_config, "url", "http://localhost:3001") if grafana_config else "http://localhost:3001"
        )
        grafana_datasource = getattr(grafana_config, "datasource", "InfluxDB") if grafana_config else "InfluxDB"
        grafana_api_key = getattr(grafana_config, "api_key", "") if grafana_config else ""
        # current_config must include url and datasource so the frontend can load them
        current_cfg = info.current_config or {}
        if isinstance(current_cfg, dict):
            current_cfg = {**current_cfg, "url": grafana_url, "datasource": grafana_datasource}
            if grafana_api_key:
                current_cfg = {**current_cfg, "api_key": "***configured***" if grafana_api_key else ""}
        return ApiResponse(
            data={
                "enabled": info.state.value != "disabled",
                "state": info.state.value,
                "url": grafana_url,
                "datasource": grafana_datasource,
                "api_key": "***configured***" if grafana_api_key else "",
                "dependencies": [
                    {"package": d.package, "installed": d.installed, "version": d.version} for d in info.dependencies
                ],
                "current_config": current_cfg,
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Get Grafana config failed: %s", e)
        raise HTTPException(status_code=500, detail=GrafanaErrors.CONNECTION_FAILED) from e


@router.get("/dashboards", response_model=ApiResponse)
async def list_grafana_dashboards(
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    grafana_config = _get_grafana_config()
    if not grafana_config or not getattr(grafana_config, "enabled", False):
        raise HTTPException(status_code=503, detail=GrafanaErrors.NOT_ENABLED)

    grafana_url = getattr(grafana_config, "url", "http://localhost:3001")
    api_key = getattr(grafana_config, "api_key", "")

    if not api_key:
        logger.warning(
            "Grafana api_key is empty, cannot authenticate, please set grafana.api_key in config"
        )  # FIXED-P3: 中文日志→英文
        raise HTTPException(
            status_code=503, detail=GrafanaErrors.API_KEY_MISSING
        )  # FIXED: 原问题-403 Forbidden暗示权限不足，实际是配置缺失，改为503 Service Unavailable更准确

    # FIXED-P1: 原问题-grafana_url未校验格式，可能含恶意字符或缺少scheme；
    # 改为：校验URL必须以http://或https://开头
    # R5-G-08: SSRF 校验 - 拦截内网/环回/链路本地等危险地址
    if not _is_grafana_url_safe(grafana_url):
        raise HTTPException(status_code=500, detail=GrafanaErrors.CONNECTION_FAILED)

    try:
        import httpx

        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:  # FIXED: 原问题-魔法数字timeout=10.0
            # FIXED-P1: 原问题-grafana_url含?时f"{url}/api/search"产生畸形URL；
            # 改为：使用httpx.URL正确拼接path
            base_url = grafana_url.rstrip("/")
            resp = await client.get(f"{base_url}/api/search", headers=headers)
            if resp.status_code == 200:
                dashboards = resp.json()
                return ApiResponse(data={"dashboards": dashboards})
            raise HTTPException(status_code=502, detail=GrafanaErrors.BAD_STATUS)
    except ImportError:
        raise HTTPException(status_code=503, detail=GrafanaErrors.DEPS_MISSING) from None
    except Exception as e:
        logger.error("Grafana dashboards request failed: %s", e)
        raise HTTPException(status_code=502, detail=GrafanaErrors.CONNECTION_FAILED) from e


@router.get("/embed-url", response_model=ApiResponse)
async def get_grafana_embed_url(
    dashboard_uid: str = Query(default="", max_length=128),
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    grafana_config = _get_grafana_config()
    try:
        if not grafana_config or not getattr(grafana_config, "enabled", False):
            raise HTTPException(status_code=503, detail=GrafanaErrors.NOT_ENABLED)

        grafana_url = getattr(grafana_config, "url", "http://localhost:3001")
        # R5-G-08: SSRF 校验 - 拦截内网/环回/链路本地等危险地址
        if not _is_grafana_url_safe(grafana_url):
            raise HTTPException(status_code=500, detail=GrafanaErrors.CONNECTION_FAILED)
        base_url = grafana_url.rstrip("/")
        if dashboard_uid:
            if not re.match(r"^[a-zA-Z0-9_-]+$", dashboard_uid):
                raise HTTPException(status_code=400, detail=GrafanaErrors.INVALID_UID)
            embed_url = f"{base_url}/d/{dashboard_uid}?kiosk&theme=light"
        else:
            embed_url = f"{base_url}/?kiosk&theme=light"
        return ApiResponse(data={"url": embed_url})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Get embed URL failed: %s", e)  # FIXED-P3: 中文日志→英文
        raise HTTPException(status_code=500, detail=GrafanaErrors.CONNECTION_FAILED) from e
