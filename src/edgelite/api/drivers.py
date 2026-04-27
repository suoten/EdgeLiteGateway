"""驱动配置管理API路由"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from edgelite.models.common import ApiResponse
from edgelite.api.deps import CurrentUser, require_permission
from edgelite.security.rbac import Permission

router = APIRouter(prefix="/api/v1/drivers", tags=["驱动配置"])


def _get_registry():
    from edgelite.app import _app_state
    return getattr(_app_state, "driver_registry", None)


@router.get("/list", response_model=ApiResponse)
async def list_drivers(
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    registry = _get_registry()
    if not registry:
        raise HTTPException(status_code=501, detail="驱动注册表未初始化")

    drivers = []
    for name, driver_cls in registry._drivers.items():
        instance = driver_cls() if isinstance(driver_cls, type) else driver_cls
        drivers.append({
            "name": getattr(instance, "plugin_name", name),
            "version": getattr(instance, "plugin_version", "1.0.0"),
            "protocols": getattr(instance, "supported_protocols", []),
            "description": getattr(instance, "__doc__", "") or "",
        })

    return ApiResponse(data={"drivers": drivers, "total": len(drivers)})


@router.get("/protocols", response_model=ApiResponse)
async def list_protocols(
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    registry = _get_registry()
    if not registry:
        raise HTTPException(status_code=501, detail="驱动注册表未初始化")

    protocols = registry.get_supported_protocols()
    return ApiResponse(data={"protocols": protocols})


@router.get("/{driver_name}/config-schema", response_model=ApiResponse)
async def get_driver_config_schema(
    driver_name: str,
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    registry = _get_registry()
    if not registry:
        raise HTTPException(status_code=501, detail="驱动注册表未初始化")

    driver_cls = registry._drivers.get(driver_name)
    if not driver_cls:
        raise HTTPException(status_code=404, detail=f"驱动 {driver_name} 不存在")

    schemas = {
        "modbus_tcp": {
            "fields": [
                {"name": "host", "type": "string", "label": "IP地址", "default": "127.0.0.1", "required": True},
                {"name": "port", "type": "integer", "label": "端口", "default": 502, "required": True},
                {"name": "slave_id", "type": "integer", "label": "从站ID", "default": 1, "required": True},
                {"name": "timeout", "type": "number", "label": "超时(秒)", "default": 3.0},
            ]
        },
        "opcua": {
            "fields": [
                {"name": "endpoint", "type": "string", "label": "OPC UA端点", "default": "opc.tcp://localhost:4840", "required": True},
                {"name": "username", "type": "string", "label": "用户名"},
                {"name": "password", "type": "string", "label": "密码", "secret": True},
                {"name": "security_mode", "type": "string", "label": "安全模式", "default": "None", "options": ["None", "Sign", "SignAndEncrypt"]},
            ]
        },
        "s7": {
            "fields": [
                {"name": "host", "type": "string", "label": "IP地址", "default": "192.168.1.1", "required": True},
                {"name": "rack", "type": "integer", "label": "机架号", "default": 0},
                {"name": "slot", "type": "integer", "label": "槽号", "default": 1},
            ]
        },
        "bacnet": {
            "fields": [
                {"name": "ip", "type": "string", "label": "BACnet设备IP", "default": "", "required": True},
                {"name": "port", "type": "integer", "label": "UDP端口", "default": 47808},
                {"name": "device_id", "type": "string", "label": "设备ID"},
                {"name": "subnet", "type": "string", "label": "子网掩码"},
            ]
        },
        "serial_port": {
            "fields": [
                {"name": "port", "type": "string", "label": "串口设备", "default": "COM1", "required": True},
                {"name": "baudrate", "type": "integer", "label": "波特率", "default": 9600, "options": [9600, 19200, 38400, 57600, 115200]},
                {"name": "bytesize", "type": "integer", "label": "数据位", "default": 8, "options": [5, 6, 7, 8]},
                {"name": "parity", "type": "string", "label": "校验位", "default": "N", "options": ["N", "E", "O"]},
                {"name": "stopbits", "type": "number", "label": "停止位", "default": 1, "options": [1, 1.5, 2]},
                {"name": "protocol", "type": "string", "label": "上层协议", "default": "raw", "options": ["raw", "modbus_rtu"]},
            ]
        },
        "database_source": {
            "fields": [
                {"name": "db_type", "type": "string", "label": "数据库类型", "default": "mysql", "required": True, "options": ["mysql", "postgresql", "sqlite", "mssql"]},
                {"name": "host", "type": "string", "label": "主机", "default": "localhost"},
                {"name": "port", "type": "integer", "label": "端口", "default": 3306},
                {"name": "database", "type": "string", "label": "数据库名", "required": True},
                {"name": "username", "type": "string", "label": "用户名"},
                {"name": "password", "type": "string", "label": "密码", "secret": True},
                {"name": "pool_size", "type": "integer", "label": "连接池大小", "default": 5},
            ]
        },
        "barcode_scanner": {
            "fields": [
                {"name": "port", "type": "string", "label": "串口设备", "default": "COM1", "required": True},
                {"name": "baudrate", "type": "integer", "label": "波特率", "default": 9600},
                {"name": "prefix", "type": "string", "label": "条码前缀"},
                {"name": "suffix", "type": "string", "label": "条码后缀", "default": "\\r"},
            ]
        },
        "mqtt_client": {
            "fields": [
                {"name": "broker", "type": "string", "label": "Broker地址", "default": "localhost", "required": True},
                {"name": "port", "type": "integer", "label": "端口", "default": 1883},
                {"name": "username", "type": "string", "label": "用户名"},
                {"name": "password", "type": "string", "label": "密码", "secret": True},
                {"name": "topic", "type": "string", "label": "订阅主题", "required": True},
            ]
        },
        "http_webhook": {
            "fields": [
                {"name": "path", "type": "string", "label": "Webhook路径", "default": "/webhook/data", "required": True},
                {"name": "secret", "type": "string", "label": "签名密钥"},
                {"name": "method", "type": "string", "label": "HTTP方法", "default": "POST", "options": ["POST", "PUT"]},
            ]
        },
    }

    schema = schemas.get(driver_name, {
        "fields": [
            {"name": "host", "type": "string", "label": "主机地址", "default": "localhost", "required": True},
            {"name": "port", "type": "integer", "label": "端口", "default": 0},
        ]
    })

    return ApiResponse(data={"driver_name": driver_name, "schema": schema})


@router.post("/{driver_name}/discover", response_model=ApiResponse)
async def discover_devices(
    driver_name: str,
    config: dict = None,
    user: CurrentUser = require_permission(Permission.DEVICE_MANAGE),
):
    registry = _get_registry()
    if not registry:
        raise HTTPException(status_code=501, detail="驱动注册表未初始化")

    driver = registry.get_driver(driver_name)
    if not driver:
        raise HTTPException(status_code=404, detail=f"驱动 {driver_name} 不存在")

    try:
        devices = await driver.discover_devices(config or {})
        return ApiResponse(data={"devices": devices})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"设备发现失败: {e}")
