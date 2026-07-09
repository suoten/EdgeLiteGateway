"""内置MQTT Server管理API路由"""

from __future__ import annotations

import ipaddress
import logging
import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

from edgelite.api.deps import MqttServerDep, require_permission
from edgelite.api.error_codes import ServiceErrors
from edgelite.models.common import ApiResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/mqtt-server", tags=["MQTT Server"])

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

# R5-S-05: 允许的证书目录（相对项目根目录）和扩展名
_ALLOWED_CERT_DIRS = tuple(Path(p).resolve() for p in ("data/certs", "configs/certs"))
_ALLOWED_CERT_EXTS = frozenset({".pem", ".crt", ".key"})


def _validate_tls_path(field_name: str, raw_path: str) -> None:
    """R5-S-05: 校验 TLS 证书/密钥路径，防止路径遍历攻击。

    - 拒绝绝对路径、UNC 路径、盘符路径、包含 .. 的路径
    - resolve() 后必须位于允许的证书目录内
    - 扩展名必须为 .pem/.crt/.key
    """
    if not raw_path:
        return

    # 拒绝绝对路径、UNC 路径、盘符路径
    if os.path.isabs(raw_path):
        raise ValueError(f"{field_name} must not be an absolute path: {raw_path}")
    if raw_path.startswith("\\\\"):
        raise ValueError(f"{field_name} must not be a UNC path: {raw_path}")
    # 检测 Windows 盘符（如 C:\、D:/）
    if len(raw_path) >= 2 and raw_path[1] == ":" and raw_path[0].isalpha():
        raise ValueError(f"{field_name} must not contain a drive letter: {raw_path}")
    # 拒绝包含 .. 的路径遍历
    parts = Path(raw_path).parts
    if ".." in parts:
        raise ValueError(f"{field_name} must not contain '..' traversal: {raw_path}")

    # resolve() 后必须位于允许的证书目录内
    resolved = Path(raw_path).resolve()
    if not any(
        resolved == allowed or allowed in resolved.parents
        for allowed in _ALLOWED_CERT_DIRS
    ):
        raise ValueError(
            f"{field_name} must be within allowed cert directories "
            f"({', '.join(str(p) for p in _ALLOWED_CERT_DIRS)}): {raw_path}"
        )

    # 扩展名必须为 .pem/.crt/.key
    if resolved.suffix.lower() not in _ALLOWED_CERT_EXTS:
        raise ValueError(
            f"{field_name} extension must be one of {sorted(_ALLOWED_CERT_EXTS)}: {raw_path}"
        )


def _is_loopback(host: str) -> bool:
    if host in _LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class MqttServerConfigModel(BaseModel):
    host: str = "127.0.0.1"  # FIXED-P4: 默认绑定localhost，与config层一致
    port: int = Field(default=1888, ge=1, le=65535)
    ws_port: int = Field(default=8083, ge=1, le=65535)
    username: str = ""
    password: str = ""
    # FIXED-P0: 增加 TLS 配置项，配置凭据时强制要求 TLS 防止明文嗅探
    tls_enabled: bool = Field(
        default=False,
        description="启用 TLS 加密通信（配置 username/password 时强烈建议启用）",
    )
    tls_cert_path: str = Field(
        default="",
        description="TLS 证书文件路径（PEM 格式），tls_enabled=true 时必填",
    )
    tls_key_path: str = Field(
        default="",
        description="TLS 私钥文件路径（PEM 格式），tls_enabled=true 时必填",
    )
    tls_ca_path: str = Field(
        default="",
        description="TLS CA 证书路径（用于双向认证，可选）",
    )
    # FIXED-P0: 非 loopback 暴露时要求二次确认
    confirm_expose: bool = Field(
        default=False,
        description="当 host 非 loopback 时必须显式设置为 true 以确认暴露风险",
    )

    @model_validator(mode="after")
    def _validate_security(self) -> MqttServerConfigModel:
        """FIXED-P0: 安全配置校验。

        1. 配置了 username/password 且未启用 TLS 时，记录警告（凭据明文传输可被嗅探）
        2. 非 loopback 暴露时要求二次确认
        3. tls_enabled=true 时要求提供 cert 和 key 路径
        """
        # 凭据 + 明文 = 高风险
        if (self.username or self.password) and not self.tls_enabled:
            logger.warning(
                "[mqtt_server] code=CREDS_OVER_PLAINTEXT "
                "msg=MQTT credentials configured without TLS, credentials can be sniffed over plaintext TCP"
            )
        # TLS 启用但缺少证书
        if self.tls_enabled and not (self.tls_cert_path and self.tls_key_path):
            raise ValueError(
                "tls_enabled=true requires both tls_cert_path and tls_key_path"
            )
        # R5-S-05: 校验 TLS 证书/密钥/CA 路径，防止路径遍历
        _validate_tls_path("tls_cert_path", self.tls_cert_path)
        _validate_tls_path("tls_key_path", self.tls_key_path)
        _validate_tls_path("tls_ca_path", self.tls_ca_path)
        # 非 loopback 二次确认
        if not _is_loopback(self.host) and not self.confirm_expose:
            raise ValueError(
                f"MQTT Server host={self.host} is non-loopback. "
                "Set confirm_expose=true to acknowledge the risk of exposing the server to the network."
            )
        if not _is_loopback(self.host):
            logger.warning(
                "[mqtt_server] code=NON_LOOPBACK_EXPOSED host=%s tls=%s "
                "msg=MQTT Server exposed to non-loopback address",
                self.host, self.tls_enabled,
            )
        return self


@router.get("/status", response_model=ApiResponse)
async def get_mqtt_server_status(
    mqtt_server: MqttServerDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    from edgelite.services.service_manager import get_service_manager

    try:
        mgr = get_service_manager()
        info = mgr.get_service_info("mqtt_server")
        # FIXED: get_service_info()可能返回None导致500
        if info is None:
            raise HTTPException(status_code=404, detail=ServiceErrors.NOT_REGISTERED)

        connections = 0
        if mqtt_server and hasattr(mqtt_server, "get_client_count"):
            try:
                connections = mqtt_server.get_client_count()
            except Exception as e:
                logger.warning("Failed to get MQTT client count: %s", e)  # FIXED-P3: 中文日志→英文

        # FIXED: 原问题-info.current_config可能为None时直接调用.get()崩溃
        _cfg = info.current_config or {}
        return ApiResponse(
            data={
                "enabled": info.state.value != "disabled",
                "running": info.state.value == "running",
                "state": info.state.value,
                "host": _cfg.get("host", "127.0.0.1"),  # FIXED-P4: 默认绑定localhost
                "port": _cfg.get("port", 1883),
                "ws_port": _cfg.get("ws_port", 8083),
                "connections": connections,
                "dependencies": [
                    {"package": d.package, "installed": d.installed, "version": d.version}
                    for d in (info.dependencies or [])  # FIXED-P1: dependencies可能为None导致迭代崩溃
                ],
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Get MQTT server status failed: %s", e)  # FIXED-P3: 中文日志→英文
        raise HTTPException(status_code=500, detail=ServiceErrors.STATUS_FAILED) from e


@router.post("/start", response_model=ApiResponse)
async def start_mqtt_server(
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    from edgelite.services.service_manager import get_service_manager

    try:
        mgr = get_service_manager()
        result = await mgr.start_service("mqtt_server")
        if not result.get("success"):
            # FIXED: 原问题-中文硬编码detail
            raise HTTPException(status_code=500, detail=result.get("error", ServiceErrors.START_FAILED))
        return ApiResponse(data=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("start_mqtt_server failed: %s", e)
        raise HTTPException(status_code=500, detail=ServiceErrors.START_FAILED) from e


@router.post("/stop", response_model=ApiResponse)
async def stop_mqtt_server(
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    from edgelite.services.service_manager import get_service_manager

    try:
        mgr = get_service_manager()
        result = await mgr.stop_service("mqtt_server")
        if not result.get("success"):
            # FIXED: 原问题-中文硬编码detail
            raise HTTPException(status_code=500, detail=result.get("error", ServiceErrors.STOP_FAILED))
        return ApiResponse(data=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("stop_mqtt_server failed: %s", e)
        raise HTTPException(status_code=500, detail=ServiceErrors.STOP_FAILED) from e


@router.put("/config", response_model=ApiResponse)
async def update_mqtt_server_config(
    config: MqttServerConfigModel,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    from edgelite.services.service_manager import get_service_manager

    try:
        mgr = get_service_manager()
        result = await mgr.update_service_config("mqtt_server", config.model_dump())
        if not result.get("success"):
            # FIXED: 原问题-中文硬编码detail
            raise HTTPException(status_code=500, detail=result.get("error", ServiceErrors.CONFIG_UPDATE_FAILED))
        return ApiResponse(data=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("update_mqtt_server_config failed: %s", e)
        raise HTTPException(status_code=500, detail=ServiceErrors.CONFIG_UPDATE_FAILED) from e
