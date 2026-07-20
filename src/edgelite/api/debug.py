"""Protocol debug API - packet sniffing and signal simulation"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Literal, cast

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel, Field

from edgelite.api.deps import require_permission
from edgelite.api.error_codes import CommonErrors, DebugErrors
from edgelite.models.common import ApiResponse
from edgelite.protocol_keys import normalize_protocol_key
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)


# Field type normalization: driver schema types → debug UI types
def _normalize_field_type(driver_type: str) -> str:
    """Map driver config_schema field types to debug UI field types."""
    type_map = {
        "string": "text",
        "integer": "number",
        "number": "number",
        "boolean": "select",
        "array": "textarea",
        "select": "select",
    }
    return type_map.get(driver_type, "text")


router = APIRouter(prefix="/api/v1/debug", tags=["debug"])


# FIXED: Pydantic模型替代原始dict参数，提供字段约束校验
class SimulateParams(BaseModel):
    """信号模拟参数模型

    覆盖常见协议字段（以Modbus为主），其他协议特定字段通过extra字段透传。
    """

    # Modbus 通用字段
    function_code: str | None = Field(default=None, pattern=r"^(0[1-6]|1[5-6])$")
    start_address: int | None = Field(default=None, ge=0, le=65535)
    quantity: int | None = Field(default=None, ge=1, le=125)
    slave_id: int | None = Field(default=None, ge=1, le=247)
    write_value: int | float | str | None = None
    # OPC UA 字段
    node_id: str | None = None
    operation: str | None = None
    # S7 字段
    area: str | None = None
    db_number: int | None = Field(default=None, ge=0)
    start: int | None = Field(default=None, ge=0)
    size: int | None = Field(default=None, ge=1)
    # MC/FINS 通用字段
    device_type: str | None = None
    address: int | None = Field(default=None, ge=0)
    count: int | None = Field(default=None, ge=1, le=9999)
    data_type: str | None = None
    # MQTT 字段
    topic: str | None = None
    payload: str | None = None
    qos: int | None = Field(default=None, ge=0, le=2)
    # 通用值字段
    value: int | float | str | bool | None = None
    point_name: str | None = None
    fault_mode: str | None = None

    model_config = {"extra": "allow"}


def _get_real_client_ip(request: Request) -> str | None:
    """获取真实客户端 IP，考虑反向代理 X-Forwarded-For 头。

    FIXED(中危): 原问题-_check_debug_ip_whitelist 使用 request.client.host，
    未考虑反向代理场景，攻击者可通过代理绕过 IP 白名单;
    修复-复用 rate_limit 中 trusted_proxies 逻辑，仅信任来自可信代理的 X-Forwarded-For。
    """
    import ipaddress

    direct_client = request.client.host if request.client else None

    # 从配置读取可信代理列表
    try:
        from edgelite.config import get_config

        config = get_config()
        trusted_proxies = getattr(config.server, "trusted_proxies", []) if hasattr(config, "server") else []
    except Exception:
        trusted_proxies = []

    if not trusted_proxies or not direct_client:
        return direct_client

    # 检查直连客户端是否为可信代理
    is_trusted = False
    for proxy in trusted_proxies:
        proxy = proxy.strip()
        if not proxy:
            continue
        try:
            if "/" in proxy:
                # CIDR 表示法
                network = ipaddress.ip_network(proxy, strict=False)
                if ipaddress.ip_address(direct_client) in network:
                    is_trusted = True
                    break
            else:
                # 精确匹配
                if direct_client == proxy:
                    is_trusted = True
                    break
        except ValueError:
            continue

    if not is_trusted:
        # 直连客户端不是可信代理，使用直连 IP（防止 IP 伪造）
        return direct_client

    # 直连客户端是可信代理，从 X-Forwarded-For 提取真实客户端 IP
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        ip_str = forwarded.split(",")[0].strip()
        if ip_str:
            try:
                ipaddress.ip_address(ip_str)
                return ip_str
            except ValueError:
                pass

    # 回退到 X-Real-IP
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        try:
            ipaddress.ip_address(real_ip)
            return real_ip
        except ValueError:
            pass

    return direct_client


def _check_debug_ip_whitelist(request: Request) -> None:
    """FIXED-P1: Debug API IP whitelist check - reject requests from non-allowed IPs.

    FIXED(中危): 原问题-使用 request.client.host 未考虑反向代理;
    修复-使用 _get_real_client_ip 从可信代理的 X-Forwarded-For 提取真实客户端 IP。
    FIXED(一般-R2): 原问题-空列表时 `if not allowed_ips: return` 跳过检查=允许所有IP，
    与配置注释"空列表=拒绝所有"矛盾; 修复-空列表时拒绝所有请求。
    """
    from edgelite.config import get_config

    config = get_config()
    allowed_ips = getattr(config.server, "debug_api_allowed_ips", None)
    if not allowed_ips:
        # 空列表=拒绝所有，与配置注释一致
        raise HTTPException(status_code=403, detail=DebugErrors.IP_NOT_ALLOWED)
    client_ip = _get_real_client_ip(request)
    if client_ip not in allowed_ips:
        raise HTTPException(status_code=403, detail=DebugErrors.IP_NOT_ALLOWED)


# FIXED(架构P0): record_packet 及缓冲区管理已下沉到 edgelite.packet_recorder 中立模块，
# 消除 engine/drivers 层对 edgelite.api.debug 的跨层依赖（原依赖强制加载 FastAPI 栈）。
from edgelite.packet_recorder import _packet_buffers, record_packet
from edgelite.packet_recorder import get_buffer as _get_buffer

# Modbus function code definitions for signal simulator
_MODBUS_FUNCTIONS = {
    "01": "Read Coils",
    "02": "Read Discrete Inputs",
    "03": "Read Holding Registers",
    "04": "Read Input Registers",
    "05": "Write Single Coil",
    "06": "Write Single Register",
    "15": "Write Multiple Coils",
    "16": "Write Multiple Registers",
}

# Protocol form schemas for the signal simulator
_PROTOCOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "modbus_tcp": {
        "name": "Modbus TCP",
        "fields": [
            {
                "key": "function_code",
                "label": "Function Code",
                "type": "select",
                "options": [{"value": k, "label": f"{k} - {v}"} for k, v in _MODBUS_FUNCTIONS.items()],
            },
            {"key": "start_address", "label": "Start Address", "type": "number", "default": 0, "min": 0, "max": 65535},
            {"key": "quantity", "label": "Quantity", "type": "number", "default": 1, "min": 1, "max": 125},
            {"key": "slave_id", "label": "Slave ID", "type": "number", "default": 1, "min": 1, "max": 247},
            {"key": "write_value", "label": "Write Value (for write functions)", "type": "number", "optional": True},
        ],
    },
    "modbus_rtu": {
        "name": "Modbus RTU",
        "fields": [
            {
                "key": "function_code",
                "label": "Function Code",
                "type": "select",
                "options": [{"value": k, "label": f"{k} - {v}"} for k, v in _MODBUS_FUNCTIONS.items()],
            },
            {"key": "start_address", "label": "Start Address", "type": "number", "default": 0, "min": 0, "max": 65535},
            {"key": "quantity", "label": "Quantity", "type": "number", "default": 1, "min": 1, "max": 125},
            {"key": "slave_id", "label": "Slave ID", "type": "number", "default": 1, "min": 1, "max": 247},
            {"key": "write_value", "label": "Write Value (for write functions)", "type": "number", "optional": True},
        ],
    },
    "opcua": {
        "name": "OPC UA",
        "fields": [
            {"key": "node_id", "label": "Node ID", "type": "text", "placeholder": "ns=2;i=1"},
            {
                "key": "operation",
                "label": "Operation",
                "type": "select",
                "options": [
                    {"value": "read", "label": "Read"},
                    {"value": "write", "label": "Write"},
                    {"value": "browse", "label": "Browse"},
                    {"value": "subscribe", "label": "Subscribe"},
                ],
            },
            {"key": "write_value", "label": "Write Value", "type": "text", "optional": True},
            {
                "key": "deadband_type",
                "label": "Deadband Type",
                "type": "select",
                "options": [
                    {"value": "None", "label": "None"},
                    {"value": "Absolute", "label": "Absolute"},
                    {"value": "Percent", "label": "Percent"},
                ],
            },
            {"key": "deadband_value", "label": "Deadband Value", "type": "number", "default": 0},
        ],
    },
    "mqtt_client": {
        "name": "MQTT",
        "fields": [
            {"key": "topic", "label": "Topic", "type": "text", "placeholder": "sensor/data"},
            {"key": "payload", "label": "Payload", "type": "textarea", "placeholder": '{"key": "value"}'},
            {
                "key": "qos",
                "label": "QoS",
                "type": "select",
                "options": [
                    {"value": "0", "label": "0 - At most once"},
                    {"value": "1", "label": "1 - At least once"},
                    {"value": "2", "label": "2 - Exactly once"},
                ],
            },
            {
                "key": "retain",
                "label": "Retain",
                "type": "select",
                "options": [
                    {"value": "false", "label": "No"},
                    {"value": "true", "label": "Yes"},
                ],
            },
            {"key": "will_topic", "label": "Will Topic", "type": "text", "optional": True},
            {"key": "will_message", "label": "Will Message", "type": "text", "optional": True},
        ],
    },
    "s7": {
        "name": "Siemens S7",
        "fields": [
            {
                "key": "area",
                "label": "Area",
                "type": "select",
                "options": [
                    {"value": "DB", "label": "Data Block (DB)"},
                    {"value": "I", "label": "Input (I)"},
                    {"value": "Q", "label": "Output (Q)"},
                    {"value": "M", "label": "Marker (M)"},
                ],
            },
            {"key": "db_number", "label": "DB Number", "type": "number", "default": 1},
            {"key": "start", "label": "Start Address", "type": "number", "default": 0},
            {"key": "size", "label": "Size (bytes)", "type": "number", "default": 1, "min": 1},
        ],
    },
    "mc": {
        "name": "Mitsubishi MC",
        "fields": [
            {
                "key": "operation",
                "label": "Operation",
                "type": "select",
                "options": [
                    {"value": "read", "label": "Read Device"},
                    {"value": "write", "label": "Write Device"},
                    {"value": "read_bit", "label": "Read Bit Device"},
                    {"value": "write_bit", "label": "Write Bit Device"},
                ],
            },
            {
                "key": "device_type",
                "label": "Device Type",
                "type": "select",
                "options": [
                    {"value": "D", "label": "D - Data Register"},
                    {"value": "M", "label": "M - Internal Relay"},
                    {"value": "X", "label": "X - Input"},
                    {"value": "Y", "label": "Y - Output"},
                    {"value": "W", "label": "W - Link Register"},
                    {"value": "R", "label": "R - File Register"},
                ],
            },
            {"key": "address", "label": "Address", "type": "number", "default": 0},
            {"key": "count", "label": "Count", "type": "number", "default": 1, "min": 1, "max": 960},
            {"key": "write_value", "label": "Write Value", "type": "number", "optional": True},
        ],
    },
    "fins": {
        "name": "Omron FINS",
        "fields": [
            {
                "key": "operation",
                "label": "Operation",
                "type": "select",
                "options": [
                    {"value": "read", "label": "Memory Area Read (0101)"},
                    {"value": "write", "label": "Memory Area Write (0102)"},
                    {"value": "fill", "label": "Memory Area Fill (0103)"},
                    {"value": "read_multiple", "label": "Read Multiple (0104)"},
                ],
            },
            {
                "key": "area",
                "label": "Memory Area",
                "type": "select",
                "options": [
                    {"value": "D", "label": "D - DM Area"},
                    {"value": "CIO", "label": "CIO - Core I/O"},
                    {"value": "W", "label": "W - Work Area"},
                    {"value": "H", "label": "H - HR Area"},
                ],
            },
            {"key": "address", "label": "Address", "type": "number", "default": 0},
            {"key": "count", "label": "Count", "type": "number", "default": 1, "min": 1, "max": 9999},
            {
                "key": "data_type",
                "label": "Data Type",
                "type": "select",
                "options": [
                    {"value": "w", "label": "Word (16-bit)"},
                    {"value": "r", "label": "Real (32-bit float)"},
                    {"value": "b", "label": "Bit"},
                    {"value": "i", "label": "Signed Integer (32-bit)"},
                ],
            },
            {"key": "write_value", "label": "Write Value", "type": "number", "optional": True},
        ],
    },
    "allen_bradley": {
        "name": "Allen-Bradley CIP/PCCC",
        "fields": [
            {
                "key": "operation",
                "label": "Operation",
                "type": "select",
                "options": [
                    {"value": "read", "label": "Read Tag (CIP)"},
                    {"value": "write", "label": "Write Tag (CIP)"},
                    {"value": "read_pccc", "label": "Typed Read (PCCC)"},
                    {"value": "write_pccc", "label": "Typed Write (PCCC)"},
                    {"value": "discover_tags", "label": "Discover Tags"},
                ],
            },
            {
                "key": "tag_name",
                "label": "Tag Name / Address",
                "type": "text",
                "placeholder": "Program:Main.TagName or N7:0",
            },
            {"key": "value", "label": "Write Value", "type": "text", "optional": True},
            {
                "key": "connection_type",
                "label": "Connection Type",
                "type": "select",
                "options": [
                    {"value": "CIP", "label": "CIP (ControlLogix)"},
                    {"value": "PCCC", "label": "PCCC (MicroLogix)"},
                ],
            },
        ],
    },
    "opc_da": {
        "name": "OPC DA Client",
        "fields": [
            {
                "key": "operation",
                "label": "Operation",
                "type": "select",
                "options": [
                    {"value": "read", "label": "Read Items"},
                    {"value": "write", "label": "Write Item"},
                    {"value": "browse", "label": "Browse Server"},
                    {"value": "list_servers", "label": "List Servers"},
                ],
            },
            {"key": "item_id", "label": "OPC Item ID", "type": "text", "placeholder": "Simulation.Items.Random"},
            {"key": "value", "label": "Write Value", "type": "text", "optional": True},
        ],
    },
    "onvif": {
        "name": "ONVIF",
        "fields": [
            {
                "key": "operation",
                "label": "Operation",
                "type": "select",
                "options": [
                    {"value": "discover", "label": "WS-Discovery"},
                    {"value": "get_rtsp", "label": "Get RTSP URL"},
                    {"value": "get_snapshot", "label": "Get Snapshot URI"},
                    {"value": "ptz_continuous", "label": "PTZ Continuous Move"},
                    {"value": "ptz_absolute", "label": "PTZ Absolute Move"},
                    {"value": "ptz_relative", "label": "PTZ Relative Move"},
                    {"value": "ptz_stop", "label": "PTZ Stop"},
                    {"value": "preset_set", "label": "Set Preset"},
                    {"value": "preset_goto", "label": "Goto Preset"},
                    {"value": "preset_remove", "label": "Remove Preset"},
                    {"value": "subscribe_events", "label": "Subscribe Events"},
                ],
            },
            {"key": "pan", "label": "Pan", "type": "number", "default": 0, "min": -1, "max": 1},
            {"key": "tilt", "label": "Tilt", "type": "number", "default": 0, "min": -1, "max": 1},
            {"key": "zoom", "label": "Zoom", "type": "number", "default": 0, "min": -1, "max": 1},
            {"key": "preset_name", "label": "Preset Name", "type": "text", "optional": True},
        ],
    },
    "http_webhook": {
        "name": "HTTP Webhook",
        "fields": [
            {
                "key": "operation",
                "label": "Operation",
                "type": "select",
                "options": [
                    {"value": "send", "label": "Send Request"},
                    {"value": "test_auth", "label": "Test Authentication"},
                ],
            },
            {
                "key": "method",
                "label": "Method",
                "type": "select",
                "options": [
                    {"value": "GET", "label": "GET"},
                    {"value": "POST", "label": "POST"},
                    {"value": "PUT", "label": "PUT"},
                    {"value": "DELETE", "label": "DELETE"},
                    {"value": "PATCH", "label": "PATCH"},
                ],
            },
            {"key": "url", "label": "URL", "type": "text"},
            {"key": "body", "label": "Body", "type": "textarea", "optional": True},
        ],
    },
    "simulator": {
        "name": "Simulator",
        "fields": [
            {
                "key": "operation",
                "label": "Operation",
                "type": "select",
                "options": [
                    {"value": "read", "label": "Read Points"},
                    {"value": "write", "label": "Write Point"},
                    {"value": "set_fault", "label": "Set Fault Mode"},
                ],
            },
            {"key": "point_name", "label": "Point Name", "type": "text", "default": "temperature"},
            {"key": "value", "label": "Value", "type": "number", "optional": True},
            {
                "key": "fault_mode",
                "label": "Fault Mode",
                "type": "select",
                "options": [
                    {"value": "none", "label": "None"},
                    {"value": "timeout", "label": "Timeout"},
                    {"value": "disconnect", "label": "Disconnect"},
                    {"value": "data_error", "label": "Data Error"},
                ],
            },
        ],
    },
}


@router.get("/protocols", response_model=ApiResponse)
async def list_debug_protocols(
    request: Request,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
) -> ApiResponse:
    _check_debug_ip_whitelist(request)  # FIXED-P1: Debug API IP whitelist
    """List protocols available for debugging with their form schemas"""
    try:
        from edgelite.drivers.registry import DRIVER_DISPLAY_NAMES
    except ImportError:
        DRIVER_DISPLAY_NAMES = {}

    protocols = []
    for proto_key, schema in _PROTOCOL_SCHEMAS.items():
        display_name = DRIVER_DISPLAY_NAMES.get(proto_key, {}).get("en", schema["name"])
        protocols.append(
            {
                "key": proto_key,
                "name": display_name,
                "schema": schema,
            }
        )

    # Also expose common frontend aliases so UI can pick either key
    try:
        from edgelite.protocol_keys import protocol_key_aliases

        aliases = protocol_key_aliases
    except Exception:
        aliases = {}

    for alias_key, canonical_key in aliases.items():
        if canonical_key not in DRIVER_DISPLAY_NAMES:
            continue
        if any(p["key"] == alias_key for p in protocols):
            continue
        # Try canonical_key first, then alias_key as fallback
        base_schema = _PROTOCOL_SCHEMAS.get(canonical_key) or _PROTOCOL_SCHEMAS.get(alias_key)
        if base_schema is None:
            continue
        protocols.append(
            {
                "key": alias_key,
                "name": DRIVER_DISPLAY_NAMES.get(canonical_key, {}).get("en", alias_key),
                "schema": base_schema,
                "alias_of": canonical_key,
            }
        )

    # Add remaining protocols without detailed schemas
    for proto_key, name_info in DRIVER_DISPLAY_NAMES.items():
        if proto_key not in _PROTOCOL_SCHEMAS:
            # Try to get schema from driver
            driver_schema_fields = []
            try:
                from edgelite.drivers.registry import get_driver_registry

                registry = get_driver_registry()
                driver_cls = registry.get_driver_class(proto_key)
                if driver_cls and hasattr(driver_cls, "config_schema"):
                    raw_fields = driver_cls.config_schema.get("fields", [])
                    for f in raw_fields:
                        field_key = f.get("name", "")
                        if not field_key:
                            continue
                        field_entry = {
                            "key": field_key,
                            "label": f.get("label", field_key),
                            "type": _normalize_field_type(f.get("type", "text")),
                            "description": f.get("description", ""),
                        }
                        if "options" in f:
                            field_entry["options"] = [{"value": o, "label": str(o)} for o in f["options"]]
                        if "default" in f:
                            field_entry["default"] = f["default"]
                        if f.get("required"):
                            field_entry["required"] = True
                        driver_schema_fields.append(field_entry)
            except Exception as e:
                # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                logger.warning("驱动配置schema获取失败(%s): %s", proto_key, e)

            if driver_schema_fields:
                schema_to_use = {
                    "name": name_info.get("en", proto_key),
                    "fields": driver_schema_fields,
                }
            else:
                schema_to_use = {
                    "name": name_info.get("en", proto_key),
                    "fields": [
                        {
                            "key": "operation",
                            "label": "Operation",
                            "type": "select",
                            "options": [
                                {"value": "read", "label": "Read"},
                                {"value": "write", "label": "Write"},
                                {"value": "discover", "label": "Discover"},
                            ],
                        },
                        {"key": "params", "label": "Parameters (JSON)", "type": "textarea", "placeholder": "{}"},
                    ],
                }
            protocols.append(
                {
                    "key": proto_key,
                    "name": name_info.get("en", proto_key),
                    "schema": schema_to_use,
                }
            )

    return ApiResponse(data={"protocols": protocols})


@router.post("/simulate", response_model=ApiResponse)
async def simulate_signal(
    request: Request,
    # R11-API-14: protocol 添加 Literal 约束，仅允许核心协议类型
    protocol: Literal["modbus_tcp", "modbus_rtu", "opcua", "mqtt", "http"] = Query(..., description="Protocol type"),
    device_id: str = Query(..., description="Device ID"),
    # R11-API-14: operation 添加 Literal 约束，与文档描述 read/write/discover 一致
    # "test" 作为 connect 的别名（下方转换为 "connect"），需在 Literal 中放行
    operation: Literal["read", "write", "discover", "test"] = Query(
        "read", description="Operation: read/write/discover"
    ),
    params: SimulateParams | None = None,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
) -> ApiResponse:
    _check_debug_ip_whitelist(request)  # FIXED-P1: Debug API IP whitelist
    """Send a test signal to a device and return the raw request/response"""

    protocol = cast(
        "Literal['modbus_tcp', 'modbus_rtu', 'opcua', 'mqtt', 'http']",
        normalize_protocol_key(protocol) or protocol,
    )
    if operation == "test":
        operation = cast("Literal['read', 'write', 'discover', 'test']", "connect")

    # Get device info to validate it exists
    try:
        from edgelite.app import _app_state

        container = _app_state
    except ImportError as exc:  # FIXED(P3): 原问题-B904 异常链丢失; 修复-添加 from exc
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY) from exc

    if not hasattr(container, "device_service") or container.device_service is None:
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY)

    try:
        device = await container.device_service.get_device(device_id)
    except Exception as exc:  # FIXED(P3): 原问题-B904 异常链丢失; 修复-添加 from exc
        raise HTTPException(404, f"Device not found: {device_id}") from exc

    if not device:
        raise HTTPException(404, f"Device not found: {device_id}")

    # Execute the operation based on protocol type
    # FIXED: 将SimulateParams模型转为dict，兼容_simulate_*函数签名
    params_dict: dict[str, Any] = params.model_dump(exclude_none=True) if params else {}
    start_time = time.time()
    result: dict[str, Any] = {
        "protocol": protocol,
        "device_id": device_id,
        "operation": operation,
        "params": params_dict,
        "request_raw": None,
        "response_raw": None,
        "values": None,
        "error": None,
        "elapsed_ms": 0,
    }

    try:
        # FIXED-P1: 原问题-simulate_signal调用driver.read/write无超时保护，设备不可达时请求无限挂起；
        # 对比debug_read有timeout=30.0，此处补充超时保护
        async def _do_simulate():
            if protocol in ("modbus_tcp", "modbus-rtu", "modbus_rtu"):
                result_update = await _simulate_modbus(container, device, operation, params_dict)
                result.update(result_update)
            elif protocol == "opcua":
                result_update = await _simulate_opcua(container, device, operation, params_dict)
                result.update(result_update)
            elif protocol in ("mqtt_client", "mqtt"):
                result_update = await _simulate_mqtt(container, device, operation, params_dict)
                result.update(result_update)
            elif protocol in ("siemens_s7", "s7"):
                result_update = await _simulate_s7(container, device, operation, params_dict)
                result.update(result_update)
            elif protocol in ("opc_da", "opcda"):
                result_update = await _simulate_opc_da(container, device, operation, params_dict)
                result.update(result_update)
            elif protocol in ("mitsubishi_mc", "mc"):
                result_update = await _simulate_mc(container, device, operation, params_dict)
                result.update(result_update)
            elif protocol in ("omron_fins", "fins"):
                result_update = await _simulate_fins(container, device, operation, params_dict)
                result.update(result_update)
            elif protocol in ("allen_bradley", "ab", "ab_cip", "ab_pccc"):
                result_update = await _simulate_allen_bradley(container, device, operation, params_dict)
                result.update(result_update)
            elif protocol in ("onvif",):
                result_update = await _simulate_onvif(container, device, operation, params_dict)
                result.update(result_update)
            elif protocol in ("http", "webhook", "http_webhook"):
                result_update = await _simulate_http_webhook(container, device, operation, params_dict)
                result.update(result_update)
            elif protocol in ("simulator",):
                result_update = await _simulate_simulator(container, device, operation, params_dict)
                result.update(result_update)
            else:
                # Generic: try read/write via device service
                result_update = await _simulate_generic(container, device, operation, params_dict)
                result.update(result_update)

        await asyncio.wait_for(_do_simulate(), timeout=30.0)
    except TimeoutError:
        result["error"] = "simulate_timeout"
        logger.warning("Simulate signal timed out for %s/%s after 30s", protocol, device_id)
    except Exception as e:
        # FIXED(安全): 不向客户端泄露内部异常详情，仅记录到日志
        result["error"] = "simulate_failed"
        logger.warning("Simulate signal failed for %s/%s: %s", protocol, device_id, e, exc_info=True)

    result["elapsed_ms"] = round((time.time() - start_time) * 1000, 1)

    # Record the packet for sniffing
    if result.get("request_raw"):
        record_packet("tx", protocol, device_id, result["request_raw"])
    if result.get("response_raw"):
        record_packet("rx", protocol, device_id, result["response_raw"])

    return ApiResponse(data=result)


async def _simulate_modbus(container: Any, device: dict, operation: str, params: dict) -> dict[str, Any]:
    """Simulate Modbus read/write operation"""
    function_code = params.get("function_code", "03")
    start_address = int(params.get("start_address", 0))
    quantity = int(params.get("quantity", 1))
    slave_id = int(params.get("slave_id", 1))
    write_value = params.get("write_value")

    driver = None
    if hasattr(container, "driver_registry") and container.driver_registry:
        proto = "modbus_tcp" if device.get("protocol") == "modbus_tcp" else "modbus_rtu"
        driver_cls = container.driver_registry.get(proto)
        if driver_cls:
            driver = driver_cls(device.get("config", {}))

    if driver is None:
        return {"error": "Modbus driver not available", "values": None}

    request_raw = f"FC={function_code} Addr={start_address} Qty={quantity} Slave={slave_id}"
    if operation == "write" or function_code in ("05", "06", "15", "16"):
        if write_value is not None:
            try:
                await driver.write(start_address, write_value)
                response_raw = f"Write OK: Addr={start_address} Value={write_value}"
                return {"request_raw": request_raw, "response_raw": response_raw, "values": {"written": write_value}}
            except Exception:  # FIXED-P3: 原问题-未使用的变量e; 修复-移除as e
                return {"request_raw": request_raw, "response_raw": None, "error": "simulate_failed"}
        return {"request_raw": request_raw, "error": "write_value required for write operation"}

    # Read operation
    try:
        values = await driver.read(start_address, quantity)
        response_raw = f"Read OK: Addr={start_address} Values={values}"
        return {"request_raw": request_raw, "response_raw": response_raw, "values": values}
    except Exception:  # FIXED-P3: 原问题-未使用的变量e; 修复-移除as e
        return {"request_raw": request_raw, "response_raw": None, "error": "simulate_failed"}


async def _simulate_opcua(container: Any, device: dict, operation: str, params: dict) -> dict[str, Any]:
    """Simulate OPC UA read/write operation"""
    node_id = params.get("node_id", "ns=2;i=1")
    write_value = params.get("write_value")

    driver = None
    if hasattr(container, "driver_registry") and container.driver_registry:
        driver_cls = container.driver_registry.get("opcua")
        if driver_cls:
            driver = driver_cls(device.get("config", {}))

    if driver is None:
        return {"error": "OPC UA driver not available", "values": None}

    request_raw = f"NodeId={node_id} Op={operation}"
    try:
        if operation == "write" and write_value is not None:
            await driver.write(node_id, write_value)
            response_raw = f"Write OK: NodeId={node_id} Value={write_value}"
            return {"request_raw": request_raw, "response_raw": response_raw, "values": {"written": write_value}}
        elif operation == "browse":
            result = await driver.browse(node_id)
            response_raw = f"Browse OK: {len(result) if isinstance(result, list) else 1} items"
            return {"request_raw": request_raw, "response_raw": response_raw, "values": result}
        else:
            values = await driver.read(node_id)
            response_raw = f"Read OK: NodeId={node_id} Value={values}"
            return {"request_raw": request_raw, "response_raw": response_raw, "values": values}
    except Exception:  # FIXED-P3: 原问题-未使用的变量e; 修复-移除as e
        return {"request_raw": request_raw, "response_raw": None, "error": "simulate_failed"}


async def _simulate_mqtt(container: Any, device: dict, operation: str, params: dict) -> dict[str, Any]:
    """Simulate MQTT publish/subscribe operation"""
    topic = params.get("topic", "test/debug")
    payload = params.get("payload", "")
    qos = int(params.get("qos", 0))

    request_raw = f"Topic={topic} QoS={qos} Payload={payload[:200]}"
    try:
        # MQTT publish via the driver if available
        driver = None
        if hasattr(container, "driver_registry") and container.driver_registry:
            driver_cls = container.driver_registry.get("mqtt_client")
            if driver_cls:
                driver = driver_cls(device.get("config", {}))

        if driver and hasattr(driver, "publish"):
            await driver.publish(topic, payload, qos)
            response_raw = f"Publish OK: Topic={topic}"
            return {"request_raw": request_raw, "response_raw": response_raw, "values": {"published": True}}
        return {"request_raw": request_raw, "error": "MQTT driver not available or publish not supported"}
    except Exception:  # FIXED-P3: 原问题-未使用的变量e; 修复-移除as e
        return {"request_raw": request_raw, "response_raw": None, "error": "simulate_failed"}


async def _simulate_s7(container: Any, device: dict, operation: str, params: dict) -> dict[str, Any]:
    """Simulate Siemens S7 read/write operation"""
    area = params.get("area", "DB")
    db_number = int(params.get("db_number", 1))
    start = int(params.get("start", 0))
    size = int(params.get("size", 1))

    request_raw = f"Area={area} DB={db_number} Start={start} Size={size}"
    driver = None
    if hasattr(container, "driver_registry") and container.driver_registry:
        driver_cls = container.driver_registry.get("s7")
        if driver_cls:
            driver = driver_cls(device.get("config", {}))

    if driver is None:
        return {"error": "S7 driver not available", "values": None}

    try:
        values = await driver.read(area, db_number, start, size)
        response_raw = f"Read OK: Area={area} DB={db_number} Values={values}"
        return {"request_raw": request_raw, "response_raw": response_raw, "values": values}
    except Exception:  # FIXED-P3: 原问题-未使用的变量e; 修复-移除as e
        return {"request_raw": request_raw, "response_raw": None, "error": "simulate_failed"}


async def _simulate_mc(container: Any, device: dict, operation: str, params: dict) -> dict[str, Any]:
    """三菱MC协议专用模拟器"""
    result = {"success": True, "message": ""}

    device_type = params.get("device_type", "D")
    address = params.get("address", 0)
    count = params.get("count", 1)

    if operation in ("read", "read_bit"):
        result["message"] = f"MC Read: {device_type}{address} x{count}"
        # 模拟3E帧请求/响应
        request_hex = f"5000 00 FF 03 00 0C 00 01 00 00 00 01 00 00 00 01 00 {device_type.encode().hex()} {address:04X} {count:04X}"  # noqa: E501
        result["request_raw"] = request_hex
        # 模拟响应
        values = [i * 10 for i in range(count)]
        result["response_raw"] = f"D000 00 FF 03 00 data: {values}"
        result["data"] = {"device_type": device_type, "address": address, "count": count, "values": values}
    elif operation in ("write", "write_bit"):
        write_value = params.get("write_value", 0)
        result["message"] = f"MC Write: {device_type}{address} = {write_value}"
        result["data"] = {"device_type": device_type, "address": address, "value": write_value, "status": "written"}
    else:
        result["message"] = f"MC {operation}: executed"

    return result


async def _simulate_fins(container: Any, device: dict, operation: str, params: dict) -> dict[str, Any]:
    """欧姆龙FINS协议专用模拟器"""
    result = {"success": True, "message": ""}

    area = params.get("area", "D")
    address = params.get("address", 0)
    count = params.get("count", 1)
    data_type = params.get("data_type", "w")

    if operation == "read":
        result["message"] = f"FINS Memory Read (0101): {area}{address} x{count}"
        # 模拟FINS帧
        request_hex = f"8000 02 00 00 00 00 00 00 00 00 00 01 01 01 {area.encode().hex()} {address:04X} 00 {count:04X}"
        result["request_raw"] = request_hex
        values = [i * 100 for i in range(count)]
        result["response_raw"] = f"C000 02 00 00 00 00 00 00 00 00 00 01 01 01 data: {values}"
        result["data"] = {"area": area, "address": address, "count": count, "data_type": data_type, "values": values}
    elif operation == "write":
        write_value = params.get("write_value", 0)
        result["message"] = f"FINS Memory Write (0102): {area}{address} = {write_value}"
        result["data"] = {"area": area, "address": address, "value": write_value, "status": "written"}
    elif operation == "fill":
        result["message"] = f"FINS Memory Fill (0103): {area}{address}"
        result["data"] = {"area": area, "address": address, "status": "filled"}
    elif operation == "read_multiple":
        result["message"] = f"FINS Read Multiple (0104): {area}{address}"
        result["data"] = {"area": area, "address": address, "count": count, "status": "read"}
    else:
        result["message"] = f"FINS {operation}: executed"

    return result


async def _simulate_allen_bradley(container: Any, device: dict, operation: str, params: dict) -> dict[str, Any]:
    """Allen-Bradley CIP/PCCC协议专用模拟器"""
    result = {"success": True, "message": ""}

    tag_name = params.get("tag_name", "Program:Main.TagName")
    value = params.get("value")
    params.get("connection_type", "CIP")

    if operation == "read":
        result["message"] = f"AB CIP Read Tag: {tag_name}"
        result["request_raw"] = f"CIP Forward Open → Read Tag: {tag_name}"
        result["data"] = {"tag": tag_name, "value": 42, "type": "DINT", "status": "Success"}
    elif operation == "write":
        result["message"] = f"AB CIP Write Tag: {tag_name} = {value}"
        result["data"] = {"tag": tag_name, "value": value, "status": "Written"}
    elif operation == "read_pccc":
        result["message"] = f"AB PCCC Typed Read: {tag_name}"
        result["request_raw"] = f"PCCC Encapsulated: Typed Read {tag_name}"
        result["data"] = {"address": tag_name, "value": 100, "type": "Integer", "status": "Success"}
    elif operation == "write_pccc":
        result["message"] = f"AB PCCC Typed Write: {tag_name} = {value}"
        result["data"] = {"address": tag_name, "value": value, "status": "Written"}
    elif operation == "discover_tags":
        result["message"] = "AB Discover Tags: listing controller tags"
        result["data"] = {"tags": ["Program:Main.MyTag", "MyDINT", "MyREAL"], "count": 3}
    else:
        result["message"] = f"AB {operation}: executed"

    return result


async def _simulate_onvif(container: Any, device: dict, operation: str, params: dict) -> dict[str, Any]:
    """ONVIF协议专用模拟器"""
    result = {"success": True, "message": ""}
    pan = params.get("pan", 0)
    tilt = params.get("tilt", 0)
    zoom = params.get("zoom", 0)

    if operation == "discover":
        result["message"] = "ONVIF WS-Discovery: scanning..."
        result["data"] = {"devices": []}  # FIXED-P1: 移除硬编码内网IP，返回空列表
    elif operation == "get_rtsp":
        result["message"] = "ONVIF GetStreamUri"
        result["data"] = {"rtsp_url": ""}  # FIXED-P1: 移除硬编码RTSP URL
    elif operation == "get_snapshot":
        result["message"] = "ONVIF GetSnapshotUri"
        result["data"] = {"snapshot_uri": ""}  # FIXED-P1: 移除硬编码snapshot URL
    elif operation.startswith("ptz_"):
        result["message"] = f"ONVIF PTZ {operation}: pan={pan} tilt={tilt} zoom={zoom}"
        result["data"] = {"operation": operation, "pan": pan, "tilt": tilt, "zoom": zoom}
    elif operation.startswith("preset_"):
        result["message"] = f"ONVIF Preset {operation}"
        result["data"] = {"operation": operation, "preset_name": params.get("preset_name", "")}
    elif operation == "subscribe_events":
        result["message"] = "ONVIF PullPoint subscription created"
        result["data"] = {"status": "subscribed"}
    else:
        result["message"] = f"ONVIF {operation} executed"
    return result


async def _simulate_http_webhook(container: Any, device: dict, operation: str, params: dict) -> dict[str, Any]:
    """HTTP Webhook协议专用模拟器"""
    result = {"success": True, "message": ""}
    method = params.get("method", "POST")
    url = params.get("url", "http://localhost/webhook")
    body = params.get("body", "")

    if operation == "send":
        result["message"] = f"HTTP {method} {url}"
        result["request_raw"] = f"{method} {url} HTTP/1.1\nContent-Type: application/json\n\n{body}"
        result["response_raw"] = 'HTTP/1.1 200 OK\nContent-Type: application/json\n\n{"status":"ok"}'
        result["data"] = {"status_code": 200, "body": {"status": "ok"}}
    elif operation == "test_auth":
        result["message"] = f"HTTP Auth Test: {url}"
        result["data"] = {"auth_valid": True, "token_type": "Bearer"}
    else:
        result["message"] = f"HTTP {operation} executed"
    return result


async def _simulate_serial(container: Any, device: dict, operation: str, params: dict) -> dict[str, Any]:
    """串口协议专用模拟器"""
    result = {"success": True, "message": ""}
    data = params.get("data", "")
    encoding = params.get("encoding", "ascii")

    if operation == "send":
        result["message"] = f"Serial TX: {data}"
        result["request_raw"] = f"TX ({encoding}): {data}"
        result["response_raw"] = "RX: OK"
        result["data"] = {"sent": data, "encoding": encoding, "response": "OK"}
    elif operation == "send_hex":
        result["message"] = f"Serial TX (Hex): {data}"
        result["request_raw"] = f"TX (hex): {data}"
        result["data"] = {"sent_hex": data, "response": "ACK"}
    elif operation == "read":
        result["message"] = "Serial RX: waiting..."
        result["data"] = {"received": "sample data", "encoding": encoding}
    else:
        result["message"] = f"Serial {operation} executed"
    return result


async def _simulate_simulator(container: Any, device: dict, operation: str, params: dict) -> dict[str, Any]:
    """模拟器协议专用模拟器"""
    result = {"success": True, "message": ""}
    point_name = params.get("point_name", "temperature")
    value = params.get("value")
    fault_mode = params.get("fault_mode", "none")

    if operation == "read":
        result["message"] = f"Simulator Read: {point_name}"
        result["data"] = {"point": point_name, "value": 42.5, "mode": "sine"}
    elif operation == "write":
        result["message"] = f"Simulator Write: {point_name} = {value}"
        result["data"] = {"point": point_name, "value": value, "status": "written"}
    elif operation == "set_fault":
        result["message"] = f"Simulator Fault: {fault_mode}"
        result["data"] = {"fault_mode": fault_mode, "status": "active" if fault_mode != "none" else "cleared"}
    else:
        result["message"] = f"Simulator {operation} executed"
    return result


async def _simulate_generic(container: Any, device: dict, operation: str, params: dict) -> dict[str, Any]:
    """Generic simulation for protocols without specific handlers.

    Supports:
    - connect: run driver.health_check(device_id) when possible
    - read: read either specified points or all device points
    - write: write either one point or a dict of points
    - discover: driver.discover_devices(config)
    """
    request_raw = f"Op={operation} Params={params}"
    try:
        device_id = device.get("device_id") or ""
        proto = device.get("protocol")

        if operation == "connect":
            driver = (
                container.device_service._driver_instances.get(device_id)
                if hasattr(container, "device_service")
                else None
            )
            if driver and hasattr(driver, "health_check"):
                ok = await driver.health_check(device_id)
                response_raw = f"HealthCheck: {ok}"
                return {"request_raw": request_raw, "response_raw": response_raw, "values": {"connected": bool(ok)}}
            return {"request_raw": request_raw, "error": "Driver health_check not available"}

        if operation == "discover":
            discover_config = params.get("config") or device.get("config") or {}
            driver_cls = (
                container.driver_registry.get_driver_class(proto) if hasattr(container, "driver_registry") else None
            )
            if driver_cls is None:
                return {"request_raw": request_raw, "error": f"Driver not available for protocol: {proto}"}
            driver = driver_cls()
            # FIXED-P1: 驱动操作添加超时保护，防止驱动挂起导致请求无限阻塞
            await asyncio.wait_for(driver.start({}), timeout=30.0)
            try:
                values = await asyncio.wait_for(driver.discover_devices(discover_config), timeout=60.0)
            finally:
                await asyncio.wait_for(driver.stop(), timeout=10.0)
            response_raw = f"Discover OK: {len(values)} devices"
            return {"request_raw": request_raw, "response_raw": response_raw, "values": values}

        if operation == "read":
            point_names = params.get("points")
            if not point_names:
                points = device.get("points", [])
                if points:
                    point_names = [p.get("name", "") for p in points if p.get("name")]
            if point_names:
                values = await container.device_service.read_points(device_id)
                response_raw = f"Read OK: {values}"
                return {"request_raw": request_raw, "response_raw": response_raw, "values": values}
            return {"request_raw": request_raw, "error": "No points specified/defined on device"}

        if operation == "write":
            point = params.get("point")
            value = params.get("value")
            points_map = params.get("points")
            if point is not None:
                await container.device_service.write_point(device_id, point, value)
                response_raw = f"Write OK: {point}={value}"
                return {"request_raw": request_raw, "response_raw": response_raw, "values": {"written": {point: value}}}
            if isinstance(points_map, dict) and points_map:
                for point_name, v in points_map.items():
                    await container.device_service.write_point(device_id, point_name, v)
                response_raw = f"Write OK: {points_map}"
                return {"request_raw": request_raw, "response_raw": response_raw, "values": {"written": points_map}}
            return {"request_raw": request_raw, "error": "No write parameters provided (point/value or points dict)"}

        return {"request_raw": request_raw, "error": f"Unsupported operation: {operation}"}
    except Exception:  # FIXED-P3: 原问题-未使用的变量e; 修复-移除as e
        return {"request_raw": request_raw, "response_raw": None, "error": "simulate_failed"}


async def _simulate_opc_da(container: dict, device: str, operation: str, params: dict) -> dict:
    """OPC DA协议专用模拟器"""
    result = {"success": True, "message": ""}

    if operation == "read":
        item_id = params.get("item_id", "Simulation.Items.Random")
        result["message"] = f"OPC DA Read: {item_id}"
        import random

        result["data"] = {"item_id": item_id, "value": round(random.uniform(0, 100), 2), "quality": "Good"}
    elif operation == "write":
        item_id = params.get("item_id", "")
        value = params.get("value", "")
        result["message"] = f"OPC DA Write: {item_id}={value}"
        result["data"] = {"item_id": item_id, "value": value, "status": "written"}
    elif operation == "browse":
        result["message"] = "OPC DA Browse: listing items"
        result["data"] = {"items": ["Simulation.Items.Random", "Simulation.Items.Sawtooth", "Simulation.Items.Square"]}
    elif operation == "list_servers":
        result["message"] = "OPC DA List Servers"
        result["data"] = {"servers": ["Matrikon.OPC.Simulation", "Matrikon.OPC.Analog"]}
    else:
        result["message"] = f"OPC DA {operation}: executed"

    return result


@router.get("/packets", response_model=ApiResponse)
async def get_recent_packets(
    protocol: str | None = None,
    device_id: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
) -> ApiResponse:
    """Get recent protocol packets for sniffing"""
    # FIXED-P1: 移除失效的按用户隔离查询逻辑（record_packet 从未写入 _user_packet_buffers），
    # 直接使用全局 _packet_buffers——所有 admin 用户共享同一缓冲区。
    buffer = _get_buffer(protocol) if protocol else _get_buffer("__all__")

    packets = list(buffer)

    # Filter by device_id if specified
    if device_id:
        packets = [p for p in packets if p.get("device_id") == device_id]

    # Apply limit (return most recent)
    packets = packets[-limit:]

    return ApiResponse(data={"packets": packets, "total": len(packets)})


@router.delete("/packets", response_model=ApiResponse)
async def clear_packets(
    protocol: str | None = None,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
) -> ApiResponse:
    """Clear packet buffer"""
    key = protocol or "__all__"

    # FIXED-P1: 移除失效的按用户隔离分支（record_packet 从未写入 _user_packet_buffers）。
    # 所有 admin 用户共享同一全局缓冲区 _packet_buffers。
    if key in _packet_buffers:
        count = len(_packet_buffers[key])
        _packet_buffers[key].clear()
        return ApiResponse(data={"cleared": count})

    return ApiResponse(data={"cleared": 0})


@router.get("/devices", response_model=ApiResponse)
async def list_debug_devices(
    protocol: str | None = None,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
) -> ApiResponse:
    """List devices available for debugging"""
    try:
        from edgelite.app import _app_state

        container = _app_state
    except ImportError as exc:  # FIXED(P3): 原问题-B904 异常链丢失; 修复-添加 from exc
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY) from exc

    if not hasattr(container, "device_service") or container.device_service is None:
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY)

    try:
        devices, _ = await container.device_service.list_devices()
        if protocol:
            devices = [d for d in devices if d.get("protocol") == protocol]
        result = [
            {
                "device_id": d.get("device_id", ""),
                "name": d.get("name", ""),
                "protocol": d.get("protocol", ""),
                "status": d.get("status", "unknown"),
            }
            for d in devices
        ]
        return ApiResponse(data={"devices": result})
    except Exception as e:
        logger.error("Failed to list debug devices: %s", e)
        raise HTTPException(500, DebugErrors.LIST_FAILED) from None


@router.post("/read", response_model=ApiResponse)
async def debug_read(
    protocol: str = Query(...),
    device_id: str = Query(...),
    points: list[str] = Query([]),
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
) -> ApiResponse:
    try:
        from edgelite.app import _app_state

        container = _app_state
    except ImportError as exc:  # FIXED(P3): 原问题-B904 异常链丢失; 修复-添加 from exc
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY) from exc
    if not hasattr(container, "plugin_manager") or container.plugin_manager is None:
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY)
    protocol = normalize_protocol_key(protocol) or protocol
    driver = container.plugin_manager.get_driver(protocol)
    if driver is None:
        raise HTTPException(404, f"Driver not found: {protocol}")
    try:
        import asyncio

        values = await asyncio.wait_for(driver.read_points(device_id, points), timeout=30.0)
        recent_packets = list(_get_buffer(protocol))[-20:]
        return ApiResponse(
            data={
                "device_id": device_id,
                "protocol": protocol,
                "values": values,
                "raw_packets": recent_packets,
            }
        )
    except Exception as e:
        logger.error("Debug read failed: %s", e)
        raise HTTPException(
            500, DebugErrors.READ_FAILED
        ) from e  # FIXED-P1: 不暴露异常详情给前端  # FIXED(P3): 原问题-B904 异常链丢失; 修复-添加 from e


@router.post("/write", response_model=ApiResponse)
async def debug_write(
    protocol: str = Query(...),
    device_id: str = Query(...),
    point: str = Query(...),
    # FIXED: 将value: Any改为联合类型，限制可接受的值类型
    value: str | int | float | bool | None = None,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
) -> ApiResponse:
    try:
        from edgelite.app import _app_state

        container = _app_state
    except ImportError as exc:  # FIXED(P3): 原问题-B904 异常链丢失; 修复-添加 from exc
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY) from exc
    if not hasattr(container, "plugin_manager") or container.plugin_manager is None:
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY)
    protocol = normalize_protocol_key(protocol) or protocol
    driver = container.plugin_manager.get_driver(protocol)
    if driver is None:
        raise HTTPException(404, f"Driver not found: {protocol}")
    try:
        # SEC-FIX: debug 写入必须遵守写保护策略，不得绕过 check_write_allowed/审计
        # 优先通过 device_service.write_point 复用正常写入路径的校验与审计
        debug_user = {"username": "debug", "role": user.get("role", "admin"), "source": "debug"}
        device_service = getattr(container, "device_service", None)
        success = False
        if device_service is not None and getattr(device_service, "_driver_instances", {}).get(device_id) is not None:
            # 复用正常写入路径：device_service.write_point 会设置用户角色供驱动层 RBAC 校验
            success = await device_service.write_point(device_id, point, value, user=debug_user)
        else:
            # device_service 无该设备驱动实例时，至少执行 check_write_allowed + 审计后直连驱动
            if hasattr(driver, "check_write_allowed"):
                try:
                    allowed = driver.check_write_allowed(device_id, point)
                except Exception as ce:
                    allowed = False
                    logger.warning("[debug] check_write_allowed raised: %s", ce)
                if not allowed:
                    raise HTTPException(403, detail=DebugErrors.WRITE_NOT_ALLOWED)
            # 设置用户角色供驱动层 RBAC 校验
            set_role = getattr(driver, "set_user_role", None)
            if set_role is not None:
                try:
                    await set_role(debug_user["role"]) if __import__("inspect").iscoroutinefunction(
                        set_role
                    ) else set_role(debug_user["role"])
                except Exception as e:
                    logger.debug("[debug] set_user_role failed (non-fatal): %s", e)
            driver._current_write_user = debug_user.get("username", "")  # type: ignore[attr-defined]
            # FIXED-P1: 原问题-debug_write调用driver.write_point无超时保护，设备不可达时请求无限挂起；
            # 对比debug_read有timeout=30.0，此处补充超时保护
            success = await asyncio.wait_for(driver.write_point(device_id, point, value), timeout=30.0)
        # 审计日志记录
        try:
            from edgelite.app import _app_state as _state

            audit_svc = getattr(_state, "audit_service", None)
            if audit_svc is not None:
                from edgelite.services.audit_service import AuditAction

                await audit_svc.log(
                    action=AuditAction.DEVICE_WRITE_POINT,
                    user_id=user.get("user_id", "debug"),
                    username=user.get("username", "debug"),
                    resource_type="device_point",
                    resource_id=f"{device_id}/{point}",
                    after_value={"point": point, "value": value, "source": "debug"},
                    status="success" if success else "failed",
                )
        except Exception as e:
            logger.warning("[debug] Write audit log failed: %s", e)
        recent_packets = list(_get_buffer(protocol))[-20:]
        return ApiResponse(
            data={
                "device_id": device_id,
                "protocol": protocol,
                "point": point,
                "value": value,
                "success": success,
                "raw_packets": recent_packets,
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Debug write failed: %s", e)
        raise HTTPException(
            500, DebugErrors.WRITE_FAILED
        ) from e  # FIXED-P1: 不暴露异常详情给前端  # FIXED(P3): 原问题-B904 异常链丢失; 修复-添加 from e


@router.websocket("/monitor")
async def debug_monitor(ws: WebSocket) -> None:
    """WebSocket endpoint for real-time packet monitoring.

    FIXED: Now validates user role before allowing access. Only users with
    SYSTEM_MANAGE permission (admin) can connect to the debug monitor.
    FIXED-P0: Authentication via first-frame message (not URL query parameter)
    to prevent Token leakage in nginx logs/Referer/browser history.
    之前：token = ws.query_params.get("token") 通过 URL 查询参数传递，Token 会泄露到
          反向代理日志、CDN、浏览器历史、Referer 头
    之后：先 accept 连接，从首帧 JSON 消息 {"type":"auth","token":"..."} 提取 token，
          认证失败则关闭连接，与 app.py 其他 WS 端点保持一致
    """
    # FIXED-P0: 先 accept 连接，再从首帧消息获取 token
    client_ip = "unknown"
    if ws.client:
        client_ip = ws.client.host or "unknown"

    await ws.accept()

    # FIXED-P0: 从首帧消息中提取 auth token（与 app.py _recv_auth_token 模式一致）
    token = None
    try:
        import json as _json

        raw = await asyncio.wait_for(ws.receive_text(), timeout=5.0)
        data = _json.loads(raw)
        if isinstance(data, dict) and data.get("type") == "auth":
            token = data.get("token")
    except TimeoutError:
        logger.warning("[debug_ws] code=AUTH_TIMEOUT ip=%s msg=First-frame auth timed out after 5s", client_ip)
        await ws.close(code=4001, reason="Authentication timeout: send auth message within 5s")
        return
    except Exception as e:
        logger.warning("[debug_ws] code=AUTH_RECV_FAILED ip=%s msg=Failed to receive auth frame: %s", client_ip, e)
        await ws.close(code=4001, reason="Authentication failed: invalid auth frame")
        return

    if not token:
        logger.warning("[debug_ws] code=AUTH_MISSING_TOKEN ip=%s msg=No token in auth frame", client_ip)
        await ws.close(
            code=4001, reason='Authentication required: send {"type":"auth","token":"<your_token>"} as first message'
        )
        return

    try:
        from edgelite.security.jwt import decode_token

        payload = decode_token(token, verify_exp=True, token_type="access")
        if payload is None:
            logger.warning("Debug monitor WebSocket rejected: ip=%s - invalid token", client_ip)
            await ws.close(code=4001, reason="Authentication failed")
            return

        username = payload.get("username", "unknown")

        jti = payload.get("jti")
        if jti:
            from edgelite.security.token_revocation import is_token_revoked

            if is_token_revoked(jti):
                logger.warning("Debug monitor WebSocket rejected: user=%s ip=%s - token revoked", username, client_ip)
                await ws.close(code=4001, reason="Token revoked")
                return

        from edgelite.security.rbac import Permission, has_permission
        from edgelite.storage.sqlite_repo import UserRepo

        container = ws.app.state
        try:
            async with container.database.get_session() as session:
                repo = UserRepo(session, container.database.write_lock)
                user = await repo.get_by_username(username)
        except Exception as e:
            logger.error("Debug monitor auth query user failed: user=%s ip=%s error=%s", username, client_ip, e)
            await ws.close(code=4001, reason="Authentication failed")
            return

        if user is None or not user["enabled"]:
            logger.warning(
                "Debug monitor WebSocket rejected: user=%s ip=%s - user disabled or not found", username, client_ip
            )
            await ws.close(code=1008, reason="User disabled or not found")
            return

        db_role = user["role"]
        if not has_permission(db_role, Permission.SYSTEM_MANAGE):
            logger.warning(
                "Debug monitor WebSocket rejected: user=%s db_role=%s ip=%s - "
                "insufficient permissions (requires admin)",
                username,
                db_role,
                client_ip,
            )
            await ws.close(code=1008, reason="Insufficient permissions: admin role required")
            return

        logger.info("Debug monitor WebSocket connected: user=%s role=%s ip=%s", username, db_role, client_ip)
    except Exception as e:
        logger.warning("Debug monitor WebSocket auth failed: ip=%s error=%s", client_ip, e)
        await ws.close(code=4001, reason="Authentication failed")
        return

    # FIXED-P0: 连接已在认证前 accept，此处不再重复 accept
    protocol_filter = None
    try:
        init_msg = await ws.receive_json()
        protocol_filter = init_msg.get("protocol")
    except Exception as e:
        # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
        logger.warning("WebSocket初始化消息解析失败: %s", e)
    try:
        sent_count = 0
        # 修复P1-6: 用单调递增的 seq 跟踪每个缓冲区已发送位置，替代原 len(buf) 偏移。
        # deque(maxlen=N) 滚动后 len 恒为 N，原逻辑无法识别新增包导致漏发/重发。
        last_seq: dict[int, int] = {}
        while True:
            await asyncio.sleep(0.2)
            buffers_to_check = (
                [_get_buffer(protocol_filter)]
                if protocol_filter
                else [_get_buffer(p) for p in _packet_buffers if p != "__all__"]
            )
            for buf in buffers_to_check:
                key = id(buf)
                last = last_seq.get(key, 0)
                # 按 seq 单调递增筛选未发送的新包，deque 内包按追加顺序（即 seq 递增）排列
                new_items = [pkt for pkt in buf if pkt.get("seq", 0) > last]
                if not new_items:
                    continue
                for pkt in new_items:
                    try:
                        await ws.send_json(pkt)
                        sent_count += 1
                    except Exception:
                        return
                # 更新已发送位置为最后一条包的 seq
                last_seq[key] = new_items[-1].get("seq", last)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug("Debug monitor WebSocket error: %s", e)
    finally:
        try:
            await ws.close()
        except Exception as e:
            # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
            logger.warning("WebSocket关闭失败: %s", e)
