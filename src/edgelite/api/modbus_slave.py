"""内置Modbus Slave管理API路由"""

from __future__ import annotations

import ipaddress
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

from edgelite.api.deps import ModbusSlaveDep, require_permission
from edgelite.api.error_codes import CommonErrors, DeviceErrors, ServiceErrors
from edgelite.models.common import ApiResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/modbus-slave", tags=["Modbus Slave"])

# FIXED-P0: Modbus 协议无认证机制，非 loopback 地址暴露时风险极高
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _is_loopback(host: str) -> bool:
    """判断 host 是否为 loopback 地址（支持 IPv4/IPv6/主机名）。"""
    if host in _LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class ModbusSlaveConfigModel(BaseModel):
    host: str = "127.0.0.1"  # FIXED-P4: 默认绑定localhost，与config层一致
    port: int = Field(default=502, ge=1, le=65535)
    holding_size: int = Field(default=1000, ge=1, le=65535)  # FIXED-P0: 增加 Modbus 地址空间上限
    input_size: int = Field(default=1000, ge=1, le=65535)
    coil_size: int = Field(default=1000, ge=1, le=65535)
    discrete_size: int = Field(default=1000, ge=1, le=65535)
    # FIXED-P0: 非 loopback 地址时要求二次确认，防止 Modbus 无认证服务意外暴露公网
    confirm_expose: bool = Field(
        default=False,
        description="当 host 非 loopback 时必须显式设置为 true 以确认暴露风险",
    )
    # FIXED-P0: 可选 IP 白名单，非 loopback 时建议配置以限制访问来源
    ip_whitelist: list[str] = Field(
        default_factory=list,
        description="允许访问的客户端 IP/CIDR 列表，为空则不限（仅 loopback 安全）",
    )

    @model_validator(mode="after")
    def _validate_host_exposure(self) -> ModbusSlaveConfigModel:
        """FIXED-P0: 非 loopback 地址强制要求二次确认。

        Modbus 协议本身无认证机制，一旦 host 配置为 0.0.0.0 或外网 IP，
        任何网络可达的客户端都可读写寄存器数据。
        """
        if not _is_loopback(self.host) and not self.confirm_expose:
            raise ValueError(
                f"Modbus Slave host={self.host} is non-loopback. Modbus protocol has no authentication. "
                "Set confirm_expose=true to acknowledge the risk of exposing the slave to the network."
            )
        if not _is_loopback(self.host):
            logger.warning(
                "[modbus_slave] code=NON_LOOPBACK_EXPOSED host=%s whitelist_count=%d "
                "msg=Modbus Slave exposed to non-loopback address (no protocol-level auth)",
                self.host,
                len(self.ip_whitelist),
            )
        return self


@router.get("/status", response_model=ApiResponse)
async def get_modbus_slave_status(
    _slave: ModbusSlaveDep,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    from edgelite.services.service_manager import get_service_manager

    try:
        mgr = get_service_manager()
        info = mgr.get_service_info("modbus_slave")
        if info is None:
            raise HTTPException(status_code=404, detail=ServiceErrors.NOT_REGISTERED)

        _cfg = info.current_config or {}
        return ApiResponse(
            data={
                "enabled": info.state.value != "disabled",
                "running": info.state.value == "running",
                "state": info.state.value,
                "host": _cfg.get("host", "127.0.0.1"),  # FIXED-P4: 默认绑定localhost
                "port": _cfg.get("port", 502),
                "holding_size": _cfg.get("holding_size", 100),
                "input_size": _cfg.get("input_size", 100),
                "dependencies": [
                    {"package": d.package, "installed": d.installed, "version": d.version}
                    for d in (info.dependencies or [])  # FIXED-P1: dependencies可能为None导致迭代崩溃
                ],
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Get modbus slave status failed: %s", e)
        raise HTTPException(status_code=500, detail=ServiceErrors.STATUS_FAILED) from e


@router.post("/start", response_model=ApiResponse)
async def start_modbus_slave(
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    from edgelite.services.service_manager import get_service_manager

    try:
        mgr = get_service_manager()
        result = await mgr.start_service("modbus_slave")
        if not result.get("success"):
            raise HTTPException(status_code=500, detail=result.get("error", ServiceErrors.START_FAILED))
        return ApiResponse(data=result)
    except HTTPException:
        raise
    except ValueError as e:
        logger.warning("start_modbus_slave validation error: %s", e)
        raise HTTPException(status_code=422, detail=CommonErrors.VALIDATION_FAILED) from e
    except RuntimeError as e:
        logger.warning("start_modbus_slave conflict: %s", e)
        raise HTTPException(status_code=409, detail=CommonErrors.VALIDATION_FAILED) from e
    except Exception as e:
        logger.error("start_modbus_slave failed: %s", e)
        raise HTTPException(status_code=500, detail=ServiceErrors.START_FAILED) from e


@router.post("/stop", response_model=ApiResponse)
async def stop_modbus_slave(
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    from edgelite.services.service_manager import get_service_manager

    try:
        mgr = get_service_manager()
        result = await mgr.stop_service("modbus_slave")
        if not result.get("success"):
            raise HTTPException(status_code=500, detail=result.get("error", ServiceErrors.STOP_FAILED))
        return ApiResponse(data=result)
    except HTTPException:
        raise
    except RuntimeError as e:
        logger.warning("stop_modbus_slave conflict: %s", e)
        raise HTTPException(status_code=409, detail=CommonErrors.VALIDATION_FAILED) from e
    except Exception as e:
        logger.error("stop_modbus_slave failed: %s", e)
        raise HTTPException(status_code=500, detail=ServiceErrors.STOP_FAILED) from e


@router.put("/config", response_model=ApiResponse)
async def update_modbus_slave_config(
    config: ModbusSlaveConfigModel,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    from edgelite.services.service_manager import get_service_manager

    try:
        mgr = get_service_manager()
        result = await mgr.update_service_config("modbus_slave", config.model_dump())
        if not result.get("success"):
            raise HTTPException(status_code=500, detail=result.get("error", ServiceErrors.CONFIG_UPDATE_FAILED))
        return ApiResponse(data=result)
    except HTTPException:
        raise
    except ValueError as e:
        logger.warning("update_modbus_slave_config validation error: %s", e)
        raise HTTPException(status_code=422, detail=CommonErrors.VALIDATION_FAILED) from e
    except Exception as e:
        logger.error("update_modbus_slave_config failed: %s", e)
        raise HTTPException(status_code=500, detail=ServiceErrors.CONFIG_UPDATE_FAILED) from e


@router.get("/devices/{device_id}/ops", response_model=ApiResponse)
async def get_slave_ops(
    device_id: str,
    user: dict[str, str] = Depends(require_permission(Permission.DEVICE_READ)),
):
    from edgelite.services.device_service import get_device_service

    try:
        svc = get_device_service()
        driver = svc._driver_instances.get(device_id)
        if driver is None:
            raise HTTPException(status_code=404, detail=DeviceErrors.NOT_FOUND)
        if not hasattr(driver, "get_slave_ops_data"):
            raise HTTPException(status_code=400, detail=DeviceErrors.CAPABILITY_NOT_SUPPORTED)
        data = await driver.get_slave_ops_data(device_id)
        return ApiResponse(data=data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_slave_ops failed: %s", e)
        raise HTTPException(status_code=500, detail=DeviceErrors.GET_FAILED) from e
